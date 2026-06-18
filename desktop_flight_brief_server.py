#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pypdf import PdfReader


ROOT = Path(os.environ.get("FLIGHT_BRIEF_ROOT", Path(__file__).resolve().parent))
GENERATOR = ROOT / "generate_flight_release_brief.py"
DEFAULT_OUTPUT_DIR = Path("/Users/brianhope/Desktop/Flight Plan/New Flights")
OUTPUT_DIR = Path(os.environ.get("FLIGHT_BRIEF_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR))).expanduser()
UPLOAD_DIR = OUTPUT_DIR / "uploads"
CHUNK_DIR = OUTPUT_DIR / "upload_chunks"
RESOURCE_DIR = OUTPUT_DIR / "resources"
LATEST_RESULT_FALLBACK_PATH = Path("/tmp/flight-brief-latest-result.json")
ACTIVE_UPLOADS_PATH = OUTPUT_DIR / "active_uploads.json"
REFERENCE_DIR = Path(
    os.environ.get("FLIGHT_BRIEF_REFERENCE_DIR", "/Users/brianhope/Desktop/Flight Plan/Gold Standard Pilot Brief")
).expanduser()
SENIORITY_SOURCE_PDF = REFERENCE_DIR / "Contract and Bidding" / "Category Summary June 2026.pdf"
PHONE_PROJECT_NAME = "flight-briefs-brian-hope"
PHONE_APP_URL = "https://flight-briefs-brian-hope-c6t.pages.dev/"
CLOUD_MODE = os.environ.get("FLIGHT_BRIEF_CLOUD_MODE", "").lower() in {"1", "true", "yes"}
GENERATE_JOBS: dict[str, dict] = {}
GENERATE_JOBS_LOCK = threading.Lock()
CLOUD_TRIP_KIT_MAX_BYTES = int(os.environ.get("FLIGHT_BRIEF_CLOUD_TRIP_KIT_MAX_BYTES", str(40 * 1024 * 1024)))
NPX_CANDIDATES = [
    Path("/Users/brianhope/.nvm/versions/node/v24.14.0/bin/npx"),
    Path("/opt/homebrew/bin/npx"),
    Path("/usr/local/bin/npx"),
]
PDFTOPPM_CANDIDATES = [
    Path("/opt/homebrew/bin/pdftoppm"),
    Path("/usr/local/bin/pdftoppm"),
    Path("/usr/bin/pdftoppm"),
]
HOST = "0.0.0.0" if CLOUD_MODE else "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))
WEB_APP_DIR = Path(__file__).resolve().parent / "flight-brief-app"
FULL_BRIEF_OVERRIDES = [
    Path("/Users/brianhope/Downloads/UA1818_Gold_Standard_Brief_v3_4_FULL.pdf"),
    Path("/Users/brianhope/Downloads/UA1818_Gold_Standard_Brief_v3_5_FIXED.pdf"),
]
STATIC_FILES = {
    "/": WEB_APP_DIR / "index.html",
    "/index.html": WEB_APP_DIR / "index.html",
    "/synopsis": WEB_APP_DIR / "synopsis.html",
    "/synopsis.html": WEB_APP_DIR / "synopsis.html",
    "/app.js": WEB_APP_DIR / "app.js",
    "/styles.css": WEB_APP_DIR / "styles.css",
    "/manifest.webmanifest": WEB_APP_DIR / "manifest.webmanifest",
    "/sw.js": WEB_APP_DIR / "sw.js",
    "/apple-touch-icon.png": WEB_APP_DIR / "icons" / "icon-180.png",
    "/icon-192.png": WEB_APP_DIR / "icons" / "icon-192.png",
    "/icon-512.png": WEB_APP_DIR / "icons" / "icon-512.png",
}


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, indent=2).encode("utf-8")


def sanitize_filename(name: str) -> str:
    clean = Path(name).name.replace("\x00", "").strip()
    return clean or "flight_plan.pdf"


def save_upload(file_item, label: str) -> dict[str, str]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamped_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{label}-{sanitize_filename(file_item.filename)}"
    upload_path = UPLOAD_DIR / stamped_name
    with upload_path.open("wb") as handle:
        shutil.copyfileobj(file_item.file, handle)
    return {
        "name": upload_path.name,
        "path": str(upload_path),
        "url": upload_path.as_uri(),
    }


def upload_record(path: Path) -> dict[str, str]:
    return {
        "name": path.name,
        "path": str(path),
        "url": path.as_uri(),
    }


class UploadedField:
    def __init__(self, filename: str, content: bytes, value: str = "") -> None:
        self.filename = filename
        self.file = io.BytesIO(content)
        self.value = value


class MultipartForm(dict):
    def getvalue(self, key: str, default: str = "") -> str:
        item = self.get(key)
        if item is None:
            return default
        if isinstance(item, list):
            item = item[0] if item else None
        if item is None:
            return default
        return getattr(item, "value", default)


def parse_multipart_form(headers, stream) -> MultipartForm:
    content_type = headers.get("Content-Type", "")
    length = int(headers.get("Content-Length", "0") or "0")
    body = stream.read(length)
    raw = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8") + body
    message = BytesParser(policy=policy.default).parsebytes(raw)
    form = MultipartForm()
    if not message.is_multipart():
        return form

    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_param("filename", header="content-disposition") or ""
        payload = part.get_payload(decode=True) or b""
        value = "" if filename else payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        field = UploadedField(filename, payload, value)
        if name in form:
            existing = form[name]
            if isinstance(existing, list):
                existing.append(field)
            else:
                form[name] = [existing, field]
        else:
            form[name] = field
    return form


def build_command(pdf_path: Path, form: dict[str, str]) -> list[str]:
    command = [sys.executable, str(GENERATOR), "--pdf", str(pdf_path), "--out-dir", str(OUTPUT_DIR)]
    field_map = {
        "trip_id": "--trip-id",
        "pairing_note": "--pairing-note",
        "captain": "--captain",
        "first_officer": "--first-officer",
        "iro": "--iro",
        "purser": "--purser",
        "fa": "--fa",
        "pickup_time": "--pickup-time",
        "report_time": "--report-time",
        "pairing_pdf": "--pairing-pdf",
        "trip_kit_pdf": "--trip-kit-pdf",
    }
    for key, flag in field_map.items():
        value = (form.get(key) or "").strip()
        if value:
            command.extend([flag, value])
    return command


def parse_output_paths(stdout: str) -> dict[str, str]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) < 3:
        return {}
    txt_path = Path(lines[-3])
    card_pdf = Path(lines[-2])
    full_pdf = Path(lines[-1])
    return {
        "txt_path": str(txt_path),
        "card_pdf_path": str(card_pdf),
        "full_pdf_path": str(full_pdf),
        "txt_url": "/api/download?kind=txt" if CLOUD_MODE else txt_path.as_uri(),
        "card_pdf_url": "/api/download?kind=card" if CLOUD_MODE else card_pdf.as_uri(),
        "full_pdf_url": "/api/download?kind=full" if CLOUD_MODE else full_pdf.as_uri(),
    }


def normalize_space(value: str) -> str:
    return " ".join(value.split()).strip()


def parse_generated_times(txt_path: Path | str | None) -> dict[str, str]:
    if not txt_path:
        return {"pickup_time": "", "report_time": ""}
    try:
        text = Path(txt_path).read_text(errors="ignore")
    except OSError:
        return {"pickup_time": "", "report_time": ""}
    match = re.search(r"Pickup\s+([^|\n]+?)\s*\|\s*Report\s+([^\n]+)", text, re.I)
    if not match:
        return {"pickup_time": "", "report_time": ""}
    pickup = match.group(1).strip()
    report = match.group(2).strip()
    return {
        "pickup_time": "" if pickup.upper() == "N/A" else pickup,
        "report_time": "" if report.upper() == "N/A" else report,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FlightBriefDesktop/1.0"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/api/health", "/"}:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json" if parsed.path == "/api/health" else "text/html")
            self.end_headers()
            return
        path = STATIC_FILES.get(parsed.path, WEB_APP_DIR / "index.html")
        if path.exists() and path.is_file():
            mime, _ = mimetypes.guess_type(path.name)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/generate/status":
            self.handle_generate_status(parsed.query)
            return
        if parsed.path == "/api/download":
            self.handle_latest_download(parsed.query)
            return
        if parsed.path == "/api/synopsis/download":
            self.handle_synopsis_download()
            return
        if parsed.path == "/api/resources":
            self.handle_resources()
            return
        if parsed.path == "/api/resources/search":
            self.handle_resource_search(parsed.query)
            return
        if parsed.path == "/api/resources/download":
            self.handle_resource_download(parsed.query)
            return
        if parsed.path == "/api/timeline":
            self.handle_timeline()
            return
        if parsed.path == "/api/health":
            self.respond_json(
                {
                    "ok": True,
                    "service": "flight-brief-desktop",
                    "generator": str(GENERATOR),
                    "output_dir": str(OUTPUT_DIR),
                    "reference_dir": str(REFERENCE_DIR),
                    "reference_dir_exists": REFERENCE_DIR.exists(),
                    "seniority_source_pdf": str(SENIORITY_SOURCE_PDF),
                    "seniority_source_exists": SENIORITY_SOURCE_PDF.exists(),
                    "time": datetime.now().isoformat(timespec="seconds"),
                }
            )
            return

        if parsed.path == "/api/references":
            self.respond_json({"ok": True, "references": self.read_reference_inventory()})
            return

        if parsed.path == "/api/latest":
            latest = self.read_latest_result()
            self.respond_json({"ok": True, "latest": latest, "active_uploads": self.read_active_uploads()})
            return

        self.serve_static(STATIC_FILES.get(parsed.path, WEB_APP_DIR / "index.html"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/generate":
            if parsed.path == "/api/resources":
                try:
                    payload = self.handle_resource_upload()
                    self.respond_json(payload)
                except Exception as exc:  # noqa: BLE001
                    self.respond_json(
                        {"ok": False, "error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return
            if parsed.path == "/api/upload-chunk":
                try:
                    self.respond_json(self.handle_upload_chunk(parsed.query))
                except Exception as exc:  # noqa: BLE001
                    self.respond_json(
                        {"ok": False, "error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return
            if parsed.path == "/api/publish":
                try:
                    self.respond_json(self.handle_publish_to_phone())
                except Exception as exc:  # noqa: BLE001
                    self.respond_json(
                        {"ok": False, "error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return
            if parsed.path == "/api/synopsis":
                try:
                    self.respond_json(self.handle_build_synopsis())
                except Exception as exc:  # noqa: BLE001
                    self.respond_json(
                        {"ok": False, "error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return
            self.respond_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return

        try:
            payload = self.handle_generate()
            self.respond_json(payload)
        except Exception as exc:  # noqa: BLE001
            self.respond_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def handle_upload_chunk(self, query: str) -> dict:
        if not CLOUD_MODE:
            raise ValueError("Chunked upload is only available in cloud mode.")
        params = parse_qs(query)
        slot = (params.get("slot") or [""])[0]
        filename = sanitize_filename((params.get("filename") or ["upload.pdf"])[0])
        upload_id = re.sub(r"[^a-zA-Z0-9_-]", "", (params.get("upload_id") or [""])[0])
        index = int((params.get("index") or ["0"])[0])
        total = int((params.get("total") or ["0"])[0])
        labels = {
            "flight_plan": "flight-plan",
            "trip_kit": "trip-kit",
            "pairing": "pairing",
        }
        if slot not in labels:
            raise ValueError("Unknown upload slot.")
        if not upload_id or total <= 0 or index < 0 or index >= total:
            raise ValueError("Invalid upload chunk.")

        length = int(self.headers.get("Content-Length", "0") or "0")
        chunk = self.rfile.read(length)
        chunk_dir = CHUNK_DIR / upload_id / slot
        chunk_dir.mkdir(parents=True, exist_ok=True)
        (chunk_dir / f"{index:06d}.part").write_bytes(chunk)

        received = len(list(chunk_dir.glob("*.part")))
        if received < total:
            return {
                "ok": True,
                "status": "receiving",
                "slot": slot,
                "received": received,
                "total": total,
            }

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{labels[slot]}-{filename}"
        target = UPLOAD_DIR / target_name
        with target.open("wb") as output:
            for chunk_index in range(total):
                part_path = chunk_dir / f"{chunk_index:06d}.part"
                if not part_path.exists():
                    raise ValueError("Upload chunk missing. Try the upload again.")
                with part_path.open("rb") as part:
                    shutil.copyfileobj(part, output)
        shutil.rmtree(CHUNK_DIR / upload_id, ignore_errors=True)

        active_uploads = self.read_active_uploads()
        active_uploads[slot] = upload_record(target)
        self.write_active_uploads(active_uploads)
        return {
            "ok": True,
            "status": "complete",
            "slot": slot,
            "upload": active_uploads[slot],
            "active_uploads": active_uploads,
        }

    def handle_generate(self) -> dict:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("Upload must use multipart/form-data.")

        form = parse_multipart_form(self.headers, self.rfile)
        if CLOUD_MODE and self.headers.get("X-Flight-Brief-Wait") != "1":
            return self.start_generate_job(form)

        return self.handle_generate_form(form)

    def start_generate_job(self, form: MultipartForm) -> dict:
        job_id = uuid.uuid4().hex
        with GENERATE_JOBS_LOCK:
            GENERATE_JOBS[job_id] = {
                "ok": True,
                "job_id": job_id,
                "status": "working",
                "message": "Brief generation started.",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }

        def run_job() -> None:
            try:
                payload = self.handle_generate_form(form)
                with GENERATE_JOBS_LOCK:
                    GENERATE_JOBS[job_id] = {
                        "ok": True,
                        "job_id": job_id,
                        "status": "done",
                        "message": "Brief generated successfully.",
                        "payload": payload,
                        "result": payload.get("result"),
                        "finished_at": datetime.now().isoformat(timespec="seconds"),
                    }
            except Exception as exc:  # noqa: BLE001
                with GENERATE_JOBS_LOCK:
                    GENERATE_JOBS[job_id] = {
                        "ok": True,
                        "job_id": job_id,
                        "status": "error",
                        "error": str(exc),
                        "finished_at": datetime.now().isoformat(timespec="seconds"),
                    }

        threading.Thread(target=run_job, daemon=True).start()
        return {
            "ok": True,
            "job_id": job_id,
            "status": "working",
            "message": "Brief generation started.",
        }

    def handle_generate_status(self, query: str) -> None:
        job_id = (parse_qs(query).get("job_id") or [""])[0]
        with GENERATE_JOBS_LOCK:
            job = dict(GENERATE_JOBS.get(job_id) or {})
        if not job:
            self.respond_json({"ok": False, "error": "Generation job not found."}, status=HTTPStatus.NOT_FOUND)
            return
        self.respond_json(job)

    def handle_generate_form(self, form: MultipartForm) -> dict:

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        active_uploads = self.read_active_uploads()

        file_item = form["pdf"] if "pdf" in form else None
        if file_item is not None and getattr(file_item, "filename", ""):
            flight_plan_upload = save_upload(file_item, "flight-plan")
        else:
            flight_plan_upload = active_uploads.get("flight_plan") or {}
            if not flight_plan_upload.get("path") or not Path(flight_plan_upload["path"]).exists():
                raise ValueError("Please choose a release PDF before generating.")
        upload_path = Path(flight_plan_upload["path"])

        supplemental_uploads: dict[str, dict[str, str]] = {}
        for key, label in {
            "trip_kit": "trip-kit",
            "pairing": "pairing",
        }.items():
            if key not in form:
                continue
            supplemental_item = form[key]
            if getattr(supplemental_item, "filename", ""):
                supplemental_uploads[key] = save_upload(supplemental_item, label)
        for key in ("trip_kit", "pairing"):
            if key not in supplemental_uploads:
                prior = active_uploads.get(key) or {}
                if prior.get("path") and Path(prior["path"]).exists():
                    supplemental_uploads[key] = prior

        data_fields: dict[str, str] = {}
        for key in [
            "trip_id",
            "flying_id",
            "current_flying_id",
            "pairing_note",
            "captain",
            "first_officer",
            "iro",
            "purser",
            "fa",
            "pickup_time",
            "report_time",
        ]:
            if key in form:
                data_fields[key] = form.getvalue(key, "")
        if "trip_id" not in data_fields:
            for alias in ("flying_id", "current_flying_id"):
                value = (form.getvalue(alias, "") or "").strip()
                if value:
                    data_fields["trip_id"] = value
                    break
        if "pairing" in supplemental_uploads:
            data_fields["pairing_pdf"] = supplemental_uploads["pairing"]["path"]
        trip_kit_skipped = ""
        if "trip_kit" in supplemental_uploads:
            trip_kit_path = Path(supplemental_uploads["trip_kit"]["path"])
            if CLOUD_MODE and trip_kit_path.exists() and trip_kit_path.stat().st_size > CLOUD_TRIP_KIT_MAX_BYTES:
                size_mb = trip_kit_path.stat().st_size / (1024 * 1024)
                limit_mb = CLOUD_TRIP_KIT_MAX_BYTES / (1024 * 1024)
                trip_kit_skipped = (
                    f"Trip kit uploaded ({size_mb:.0f} MB) but skipped during cloud generation "
                    f"to stay under the Render memory limit ({limit_mb:.0f} MB)."
                )
            else:
                data_fields["trip_kit_pdf"] = supplemental_uploads["trip_kit"]["path"]

        command = build_command(upload_path, data_fields)
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Brief generation failed.")

        output_paths = parse_output_paths(result.stdout)
        if not output_paths:
            raise RuntimeError("Brief generator finished, but output paths were not returned.")

        active_uploads = {
            "flight_plan": flight_plan_upload,
            **supplemental_uploads,
        }
        self.write_active_uploads(active_uploads)
        catalog_entry = self.latest_catalog_entry()
        generated_times = parse_generated_times(output_paths.get("txt_path"))

        latest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_pdf_path": str(upload_path),
            "source_pdf_url": upload_path.as_uri(),
            "source_pdf_name": upload_path.name,
            "uploads": active_uploads,
            "active_uploads": active_uploads,
            "pickup_time": catalog_entry.get("pickup_time") or generated_times["pickup_time"],
            "report_time": catalog_entry.get("report_time") or generated_times["report_time"],
            **output_paths,
        }
        if trip_kit_skipped:
            latest["trip_kit_note"] = trip_kit_skipped
        self.update_latest_catalog_times(latest["pickup_time"], latest["report_time"])
        self.write_latest_result(latest)

        return {
            "ok": True,
            "message": "Brief generated successfully.",
            "result": latest,
        }

    @staticmethod
    def find_executable(candidates: list[Path], name: str) -> str | None:
        found = next((candidate for candidate in candidates if candidate.exists()), None)
        return str(found) if found else shutil.which(name)

    def latest_full_brief_path(self) -> Path | None:
        latest = self.read_latest_result() or {}
        full_path = Path(str(latest.get("full_pdf_path") or ""))
        if full_path.exists():
            return full_path

        catalog_path = WEB_APP_DIR / "briefs.json"
        try:
            catalog = json.loads(catalog_path.read_text())
        except Exception:
            catalog = []
        for item in catalog if isinstance(catalog, list) else []:
            full_pdf = str(item.get("full_pdf") or "").lstrip("./")
            candidate = WEB_APP_DIR / full_pdf
            if candidate.exists():
                return candidate
        return None

    def latest_catalog_entry(self) -> dict:
        catalog_path = WEB_APP_DIR / "briefs.json"
        try:
            catalog = json.loads(catalog_path.read_text())
        except Exception:
            return {}
        if isinstance(catalog, list) and catalog and isinstance(catalog[0], dict):
            return catalog[0]
        return {}

    def synopsis_output_path(self, source: Path) -> Path:
        return OUTPUT_DIR / f"{source.stem}_brief_synopsis.pdf"

    def handle_build_synopsis(self) -> dict:
        source = self.latest_full_brief_path()
        if not source:
            raise RuntimeError("No full brief is available yet. Generate a brief first.")

        from PIL import Image, ImageDraw, ImageFont

        record = self.latest_catalog_entry()
        if not record:
            raise RuntimeError("The flight data for the synopsis is not available yet. Generate a brief first.")

        def font(name: str, size: int) -> ImageFont.FreeTypeFont:
            candidates = [
                f"/System/Library/Fonts/Supplemental/{name}.ttf",
                f"/Library/Fonts/{name}.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
            ]
            for candidate in candidates:
                try:
                    return ImageFont.truetype(candidate, size)
                except OSError:
                    continue
            return ImageFont.load_default()

        font_title = font("Arial Bold", 56)
        font_h2 = font("Arial Bold", 22)
        font_body = font("Arial", 20)
        font_body_bold = font("Arial Bold", 20)
        font_small = font("Arial", 16)
        font_small_bold = font("Arial Bold", 16)
        font_tiny = font("Arial", 13)
        font_tiny_bold = font("Arial Bold", 13)

        navy = (9, 42, 91)
        blue = (23, 88, 158)
        ocean = (233, 244, 250)
        red = (184, 41, 47)
        amber = (205, 135, 38)
        green = (24, 132, 78)
        ink = (25, 36, 50)
        muted = (92, 106, 124)
        border = (199, 211, 224)
        page_bg = (244, 247, 250)
        white = (255, 255, 255)
        light_red = (255, 238, 238)
        light_amber = (255, 247, 228)
        light_green = (235, 249, 242)
        light_blue = (235, 244, 255)

        def text_size(text: str, use_font) -> tuple[int, int]:
            box = draw.textbbox((0, 0), text, font=use_font)
            return box[2] - box[0], box[3] - box[1]

        def ellipsize(text: str, use_font, width: int) -> str:
            text = normalize_space(str(text or ""))
            if text_size(text, use_font)[0] <= width:
                return text
            while text and text_size(text + "...", use_font)[0] > width:
                text = text[:-1].rstrip()
            return text + "..." if text else ""

        def wrap_text(text: str, use_font, width: int, max_lines: int | None = None) -> list[str]:
            words = normalize_space(str(text or "")).split()
            if not words:
                return []
            lines: list[str] = []
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if text_size(candidate, use_font)[0] <= width:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                current = word
            if current:
                lines.append(current)
            if max_lines and len(lines) > max_lines:
                clipped = lines[:max_lines]
                clipped[-1] = ellipsize(clipped[-1], use_font, width)
                return clipped
            return lines

        def draw_wrapped(text: str, x: int, y: int, width: int, use_font, fill=ink, line_gap: int = 5, max_lines: int | None = None) -> int:
            for line in wrap_text(text, use_font, width, max_lines):
                draw.text((x, y), line, fill=fill, font=use_font)
                y += text_size(line, use_font)[1] + line_gap
            return y

        def panel(rect: tuple[int, int, int, int], title: str, accent=blue, fill=white) -> tuple[int, int, int, int]:
            x, y, x2, y2 = rect
            draw.rounded_rectangle(rect, radius=14, fill=fill, outline=border, width=2)
            draw.rounded_rectangle((x, y, x2, y + 42), radius=14, fill=accent)
            draw.rectangle((x, y + 22, x2, y + 42), fill=accent)
            draw.text((x + 16, y + 9), title.upper(), fill=white, font=font_small_bold)
            return x + 16, y + 58, x2 - 16, y2 - 16

        def badge(x: int, y: int, label: str, value: str, accent=blue, width: int = 132) -> None:
            draw.rounded_rectangle((x, y, x + width, y + 58), radius=10, fill=(247, 250, 253), outline=border, width=1)
            draw.text((x + 10, y + 8), label.upper(), fill=accent, font=font_tiny_bold)
            draw.text((x + 10, y + 29), ellipsize(value or "--", font_body_bold, width - 20), fill=ink, font=font_body_bold)

        def bullet_list(items: list[str], x: int, y: int, width: int, limit: int, accent=blue, use_font=font_small) -> int:
            count = 0
            for item in [normalize_space(str(item or "")) for item in items if normalize_space(str(item or ""))]:
                if count >= limit:
                    break
                draw.ellipse((x, y + 6, x + 8, y + 14), fill=accent)
                y = draw_wrapped(item, x + 18, y, width - 18, use_font, ink, line_gap=3, max_lines=2) + 5
                count += 1
            if count == 0:
                y = draw_wrapped("No item extracted. Verify against the OFP.", x, y, width, use_font, muted, max_lines=2)
            return y

        def get_list(key: str) -> list[str]:
            value = record.get(key)
            return value if isinstance(value, list) else []

        def first_matching_timeline(pattern: str) -> dict:
            regex = re.compile(pattern, re.I)
            for point in get_list("timeline_points"):
                if regex.search(str(point.get("event", ""))) or regex.search(str(point.get("fuel_alt", ""))):
                    return point
            return {}

        def timeline_value(pattern: str, fallback: str = "--") -> str:
            point = first_matching_timeline(pattern)
            if not point:
                return fallback
            return str(point.get("fuel_alt") or point.get("eet") or fallback)

        def timeline_time(pattern: str, fallback: str = "--") -> str:
            point = first_matching_timeline(pattern)
            return str(point.get("eet") or fallback) if point else fallback

        def route_points() -> list[str]:
            points: list[str] = []
            for point in get_list("timeline_points"):
                event = str(point.get("event") or "")
                token = re.sub(r"\s.*", "", event).replace("/", "")
                if token and token.upper() not in {"STEP", "ETOPS"} and token not in points:
                    points.append(token)
            fallback = [record.get("departure_icao"), "CP-1", record.get("destination_icao")]
            return [str(p) for p in (points or fallback) if p][:9]

        def draw_route_map(rect: tuple[int, int, int, int]) -> None:
            x, y, x2, y2 = rect
            draw.rounded_rectangle(rect, radius=12, fill=ocean, outline=(188, 207, 222), width=1)
            draw.polygon([(x - 20, y2), (x + 105, y + 42), (x + 174, y2)], fill=(217, 236, 218))
            draw.polygon([(x2 + 24, y2), (x2 - 112, y + 34), (x2 - 202, y2)], fill=(217, 236, 218))
            pts = route_points()
            count = max(2, len(pts))
            path = []
            for index in range(count):
                px = int(x + 54 + index * ((x2 - x - 108) / (count - 1)))
                phase = index / max(1, count - 1)
                py = int(y2 - 48 - 92 * (1 - abs(phase - 0.5) * 2))
                path.append((px, py))
            for index in range(len(path) - 1):
                draw.line((path[index], path[index + 1]), fill=navy, width=5)
            for index, (px, py) in enumerate(path):
                color = amber if "CP" in pts[index].upper() else blue
                draw.ellipse((px - 10, py - 10, px + 10, py + 10), fill=white, outline=color, width=4)
                label = ellipsize(pts[index], font_tiny_bold, 74)
                tx = max(x + 4, min(px - text_size(label, font_tiny_bold)[0] // 2, x2 - 74))
                draw.text((tx, py + 14), label, fill=navy, font=font_tiny_bold)
            draw.text((x + 14, y + 12), f"{record.get('departure_icao') or 'DEP'} to {record.get('destination_icao') or 'ARR'}", fill=navy, font=font_body_bold)
            draw.text((x + 14, y + 40), f"ETOPS: {', '.join(get_list('etops_airports')[:3]) or 'verify suitability'}", fill=muted, font=font_small)

        def draw_timeline(x: int, y: int, width: int, limit: int = 7) -> int:
            points = get_list("timeline_points")[:limit]
            row_h = 34
            draw.rectangle((x, y, x + width, y + 28), fill=(242, 247, 252))
            draw.text((x + 8, y + 7), "EET", fill=blue, font=font_tiny_bold)
            draw.text((x + 68, y + 7), "EVENT", fill=blue, font=font_tiny_bold)
            draw.text((x + width - 104, y + 7), "FUEL / ALT", fill=blue, font=font_tiny_bold)
            y += 30
            for point in points:
                draw.line((x, y, x + width, y), fill=(225, 232, 240), width=1)
                draw.text((x + 8, y + 8), str(point.get("eet") or "--"), fill=ink, font=font_tiny_bold)
                draw.text((x + 68, y + 8), ellipsize(str(point.get("event") or "--"), font_tiny, width - 188), fill=ink, font=font_tiny)
                draw.text((x + width - 104, y + 8), ellipsize(str(point.get("fuel_alt") or "--"), font_tiny_bold, 96), fill=muted, font=font_tiny_bold)
                y += row_h
            return y

        def system_focus() -> tuple[str, list[str]]:
            titles = [
                "Fuel", "Hydraulics", "Electrical", "Pneumatics / Pressurization", "Flight Controls",
                "Navigation / FMS", "Fire Protection", "Engines / APU", "Autoflight", "Landing Gear / Brakes",
                "Anti-Ice / Rain", "Communications / Surveillance", "Instruments / Displays",
                "Warning Systems", "Oxygen / Emergency Equipment",
            ]
            prompts = {
                "Fuel": ["Crossfeed/imbalance triggers", "Fuel jettison or leak memory hooks", "ETOPS fuel trend at CP"],
                "Hydraulics": ["Primary pump status", "Demand pump logic", "Landing config impacts"],
                "Electrical": ["Generator sources", "Backup power path", "Bus isolation review"],
                "Pneumatics / Pressurization": ["Pack status", "Cabin altitude response", "Driftdown plan"],
                "Flight Controls": ["ACE/PFC awareness", "Alternate mode cues", "Trim and speed margins"],
                "Navigation / FMS": ["Oceanic route check", "Position verification", "RNP / raw-data backup"],
                "Fire Protection": ["Engine/APU/cargo loops", "Bottle count", "Diversion priority"],
                "Engines / APU": ["Relight envelope", "APU start envelope", "Single-engine drift"],
                "Autoflight": ["Mode awareness", "MCP/FMA calls", "Automation downgrade plan"],
                "Landing Gear / Brakes": ["RTO logic", "Brake energy", "Autobrake and antiskid"],
                "Anti-Ice / Rain": ["Wing/engine criteria", "TAT/SAT review", "Approach visibility"],
                "Communications / Surveillance": ["HF/SELCAL", "CPDLC/ADS", "Lost comm path"],
                "Instruments / Displays": ["PFD/ND source", "Standby instruments", "EFIS transfer"],
                "Warning Systems": ["EICAS priorities", "Recall limits", "Checklist discipline"],
                "Oxygen / Emergency Equipment": ["Mask use", "Passenger oxygen", "Smoke/fume routing"],
            }
            seed_text = "|".join(str(record.get(key, "")) for key in ("flight", "date_code", "route", "release"))
            index = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8], 16) % len(titles)
            title = titles[index]
            return f"{title} ({index + 1}/{len(titles)})", prompts.get(title, [])

        W, H = 2550, 1650
        sheet = Image.new("RGB", (W, H), page_bg)
        draw = ImageDraw.Draw(sheet)
        for band in range(0, H, 22):
            shade = 244 + (band // 22) % 2
            draw.rectangle((0, band, W, band + 22), fill=(shade, min(250, shade + 2), min(253, shade + 5)))

        margin = 34
        gap = 18
        header_h = 142
        footer_h = 34
        card_w = int((W - margin * 2 - gap * 4) / 5)
        card_h = int((H - header_h - footer_h - gap * 3) / 2)
        cards: list[tuple[int, int, int, int]] = []
        for row in range(2):
            for col in range(5):
                x = margin + col * (card_w + gap)
                y = header_h + gap + row * (card_h + gap)
                cards.append((x, y, x + card_w, y + card_h))

        draw.rounded_rectangle((margin, 24, W - margin, header_h - 10), radius=16, fill=navy)
        title = f"{str(record.get('flight') or 'FLIGHT').replace(' ', '')}  {record.get('route') or ''}".strip()
        draw.text((margin + 26, 42), title, fill=white, font=font_title)
        subtitle = " | ".join(part for part in [str(record.get("date_code") or ""), str(record.get("aircraft") or ""), f"Release {record.get('release')}" if record.get("release") else ""] if part)
        draw.text((margin + 28, 103), subtitle, fill=(213, 228, 244), font=font_body_bold)
        status_text = f"OUT {record.get('out_local_time') or record.get('out_time') or '--'}   ETA {record.get('eta_local_time') or record.get('eta') or '--'}   BLOCK {record.get('block') or '--'}"
        sw = text_size(status_text, font_body_bold)[0]
        draw.rounded_rectangle((W - margin - sw - 42, 52, W - margin - 22, 104), radius=12, fill=(15, 118, 77))
        draw.text((W - margin - sw - 22, 68), status_text, fill=white, font=font_body_bold)

        x, y, x2, y2 = panel(cards[0], "1  Flight Snapshot", navy)
        draw.text((x, y), record.get("route") or "--", fill=navy, font=font_title)
        y += 72
        badge(x, y, "Out", str(record.get("out_local_time") or record.get("out_time") or "--"), blue, 120)
        badge(x + 132, y, "ETA", str(record.get("eta_local_time") or record.get("eta") or "--"), blue, 120)
        badge(x + 264, y, "Block", str(record.get("block") or "--"), blue, 120)
        y += 82
        badge(x, y, "Runway", str(record.get("departure_runway") or "--"), amber, 120)
        badge(x + 132, y, "SID", str(record.get("departure_sid") or "--"), amber, 120)
        badge(x + 264, y, "Alt", str(record.get("alternate") or "--"), green, 120)
        y += 82
        y = draw_wrapped(f"{record.get('departure_icao') or 'DEP'} departure to {record.get('destination_icao') or 'ARR'} arrival. STAR {record.get('arrival_star') or 'verify'} runway {record.get('arrival_runway') or 'verify'}.", x, y, x2 - x, font_body, muted, max_lines=3) + 16
        draw.rounded_rectangle((x, y, x2, y + 124), radius=12, fill=light_blue, outline=(190, 211, 235))
        draw.text((x + 12, y + 15), "Dispatch / Release", fill=blue, font=font_h2)
        summary = f"Sector {record.get('dispatch_sector') or '--'}; ETOPS alternates {', '.join(get_list('etops_airports')[:2]) or 'verify'}; release {record.get('release') or '--'}."
        draw_wrapped(summary, x + 12, y + 48, x2 - x - 24, font_small, ink, max_lines=3)

        x, y, x2, y2 = panel(cards[1], "2  PA / Cabin", blue)
        pa = [
            f"Welcome aboard {str(record.get('flight') or 'this flight').replace('UAL', 'United')} to {record.get('destination_icao') or 'destination'}.",
            f"Planned airborne time about {record.get('block') or '--'} with arrival near {record.get('eta_local_time') or record.get('eta') or '--'}.",
            "Expected ride: review route weather and turbulence window before the PA.",
        ]
        for line in pa:
            y = draw_wrapped(line, x, y, x2 - x, font_body, ink, max_lines=2) + 9
        draw.rounded_rectangle((x, y + 8, x2, y + 94), radius=12, fill=light_green, outline=(190, 224, 205))
        draw.text((x + 14, y + 20), "FA TEST", fill=green, font=font_h2)
        draw.text((x + 14, y + 52), "Type  Evacuation  Special instructions  Time", fill=ink, font=font_small_bold)
        y += 118
        draw.text((x, y), "Cabin Notes", fill=blue, font=font_h2)
        bullet_list(get_list("fa_discussion_points"), x, y + 34, x2 - x, 4, blue, font_small)

        x, y, x2, y2 = panel(cards[2], "3  Threats First", red, (255, 252, 252))
        y = bullet_list(get_list("top_threats") + get_list("dispatcher_notes"), x, y, x2 - x, 6, red, font_small)
        dep_top = y + 8
        draw.rounded_rectangle((x, dep_top, x2, dep_top + 128), radius=12, fill=light_amber, outline=(236, 211, 166))
        draw.text((x + 12, dep_top + 12), "Departure Brief", fill=amber, font=font_h2)
        dep = f"RWY {record.get('departure_runway') or '--'} / {record.get('departure_sid') or '--'}; step climb {', '.join(get_list('step_climbs')[:2]) or 'verify'}."
        draw_wrapped(dep, x + 12, dep_top + 44, x2 - x - 24, font_small, ink, max_lines=3)
        cap_top = dep_top + 146
        draw.text((x, cap_top), "Captain / PF Brief", fill=blue, font=font_h2)
        bullet_list(get_list("captain_discussion_points") + get_list("pilot_flying_points"), x, cap_top + 34, x2 - x, 4, blue, font_tiny)

        x, y, x2, y2 = panel(cards[3], "4  Timeline / Fuel", navy)
        y = draw_timeline(x, y, x2 - x, 8) + 12
        badge(x, y, "T/O", timeline_value(r"T/O", "--"), green, 120)
        badge(x + 132, y, "CP-1", timeline_time(r"CP-1", "--"), amber, 120)
        badge(x + 264, y, "LDG", timeline_value(r"LDG|KSFO IN", "--"), green, 120)

        x, y, x2, y2 = panel(cards[4], "5  ETOPS / Diversion", blue)
        draw_route_map((x, y, x2, y + 285))
        y += 306
        cp_text = "; ".join(get_list("etops_cp_details")[:2]) or "Verify CP details in OFP."
        y = draw_wrapped(cp_text, x, y, x2 - x, font_small, ink, max_lines=3) + 8
        badge(x, y, "FAR", str(record.get("far_reserve_fuel") or "--"), red, 108)
        badge(x + 120, y, "Consv", str(record.get("conservative_fuel") or "--"), green, 108)
        badge(x + 240, y, "Pair", " / ".join(get_list("etops_airports")[:2]) or "--", blue, 148)

        x, y, x2, y2 = panel(cards[5], "6  Route / Weather", blue)
        draw_route_map((x, y, x2, y + 250))
        y += 272
        weather_items = [item for item in get_list("dispatcher_notes") if re.search(r"TURB|WX|WIND|WEATHER|CB|TS|FL", item, re.I)]
        y = bullet_list(weather_items or get_list("dispatcher_notes"), x, y, x2 - x, 4, amber, font_small)
        draw.rounded_rectangle((x, y + 6, x2, y + 80), radius=12, fill=light_blue, outline=(190, 211, 235))
        draw.text((x + 12, y + 19), "Diversion Pair", fill=blue, font=font_h2)
        draw.text((x + 180, y + 22), " / ".join(get_list("etops_airports")[:2]) or "Verify", fill=navy, font=font_h2)

        x, y, x2, y2 = panel(cards[6], "7  Arrival Plan", navy)
        badge(x, y, "Airport", str(record.get("destination_icao") or "--"), blue, 120)
        badge(x + 132, y, "Runway", str(record.get("arrival_runway") or "--"), blue, 120)
        badge(x + 264, y, "STAR", str(record.get("arrival_star") or "--"), blue, 120)
        y += 82
        y = bullet_list(get_list("arrival_brief_points"), x, y, x2 - x, 5, green, font_small)
        draw.rounded_rectangle((x, y + 4, x2, y + 90), radius=12, fill=light_green, outline=(190, 224, 205))
        draw_wrapped("Stable gates: 1500 gear down, 1000 checklist/speed complete, 500 stable call.", x + 12, y + 18, x2 - x - 24, font_small_bold, green, max_lines=3)

        x, y, x2, y2 = panel(cards[7], "8  Operational Review", blue)
        review_rows = [
            ("ORCA", "Fly, Navigate, Communicate, checklist, diversion"),
            ("Driftdown", "Offset 5 NM, minimize descent until offset, 500 ft vertical offset as required"),
            ("Cargo Fire", "Immediate diversion mindset; evaluate nearest suitable"),
            ("Comms", "HF 121.5  VHF 123.45  SELCAL / CPDLC as required"),
        ]
        for label, value in review_rows:
            draw.rounded_rectangle((x, y, x2, y + 74), radius=10, fill=(247, 250, 253), outline=(220, 229, 237))
            draw.text((x + 12, y + 12), label, fill=blue, font=font_body_bold)
            draw_wrapped(value, x + 142, y + 12, x2 - x - 154, font_small, ink, max_lines=2)
            y += 85

        x, y, x2, y2 = panel(cards[8], "9  777 Knowledge", green)
        system_title, prompts = system_focus()
        draw.rounded_rectangle((x, y, x2, y + 74), radius=12, fill=light_green, outline=(190, 224, 205))
        draw.text((x + 12, y + 13), "Today's System", fill=green, font=font_h2)
        draw.text((x + 12, y + 43), ellipsize(system_title, font_body_bold, x2 - x - 24), fill=ink, font=font_body_bold)
        y += 94
        y = bullet_list(prompts, x, y, x2 - x, 4, green, font_body)
        draw.rounded_rectangle((x, y + 8, x2, y + 102), radius=12, fill=light_red, outline=(238, 197, 197))
        draw.text((x + 12, y + 20), "Knowledge Validation", fill=red, font=font_h2)
        draw_wrapped("Below 1,000 feet AFE, approach path corrections must be coordinated and authoritative.", x + 12, y + 52, x2 - x - 24, font_small, ink, max_lines=2)

        x, y, x2, y2 = panel(cards[9], "10  Captain's Corner", navy)
        draw.rounded_rectangle((x, y, x2, y + 88), radius=12, fill=light_blue, outline=(190, 211, 235))
        draw.text((x + 12, y + 15), "Flight Assessment", fill=blue, font=font_h2)
        assessment = [
            ("Aircraft / Systems", "Normal"),
            ("Fuel / Weights", "Strong"),
            ("ETOPS / Oceanic", "Strong"),
            ("Weather / Ride", "Monitor"),
            ("Arrival / Destination", "Normal"),
        ]
        ay = y + 103
        for label, value in assessment:
            color = green if value in {"Normal", "Strong"} else amber
            draw.text((x + 8, ay), label, fill=ink, font=font_small_bold)
            draw.text((x2 - 84, ay), value, fill=color, font=font_small_bold)
            ay += 32
        draw.rounded_rectangle((x, ay + 10, x2, y2), radius=12, fill=light_amber, outline=(236, 211, 166))
        draw.text((x + 12, ay + 24), "Final Takeaway", fill=amber, font=font_h2)
        takeaway = "Well-fueled, oceanic/ETOPS leg: manage turbulence window, CP fuel check, driftdown reminder, and a disciplined SFO arrival setup."
        draw_wrapped(takeaway, x + 12, ay + 58, x2 - x - 24, font_small, ink, max_lines=4)

        footer = "Gold Standard Pilot Brief synopsis. Visual reference only. Verify against official OFP, FMS, ATIS, NOTAMs, charts, and company manuals."
        draw.text((margin, H - 28), footer, fill=muted, font=font_small)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        draw.text((W - margin - text_size(stamp, font_small)[0], H - 28), stamp, fill=muted, font=font_small)

        synopsis_path = self.synopsis_output_path(source)
        synopsis_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(synopsis_path, "PDF", resolution=144.0)

        synopsis_static_url = self.add_synopsis_to_phone_catalog(synopsis_path)
        latest = self.read_latest_result() or {}
        if latest:
            latest["synopsis_pdf_path"] = str(synopsis_path)
            latest["synopsis_pdf_url"] = "/api/synopsis/download"
            self.write_latest_result(latest)

        return {
            "ok": True,
            "message": "Brief synopsis created.",
            "source": str(source),
            "synopsis_path": str(synopsis_path),
            "synopsis_url": "/api/synopsis/download",
            "synopsis_static_url": synopsis_static_url,
        }

    def write_latest_result(self, latest: dict) -> None:
        latest_payload = json.dumps(latest, indent=2)
        for latest_path in (OUTPUT_DIR / "latest_desktop_result.json", LATEST_RESULT_FALLBACK_PATH):
            try:
                latest_path.write_text(latest_payload)
            except OSError:
                continue

    def read_active_uploads(self) -> dict:
        def existing_uploads(uploads: dict) -> dict:
            return {
                key: value
                for key, value in uploads.items()
                if isinstance(value, dict) and value.get("path") and Path(value["path"]).exists()
            }

        try:
            if ACTIVE_UPLOADS_PATH.exists():
                payload = json.loads(ACTIVE_UPLOADS_PATH.read_text())
                if isinstance(payload, dict):
                    return existing_uploads(payload)
        except (OSError, json.JSONDecodeError):
            pass
        latest = self.read_latest_result() or {}
        uploads = latest.get("active_uploads") or latest.get("uploads") or {}
        return existing_uploads(uploads) if isinstance(uploads, dict) else {}

    def write_active_uploads(self, uploads: dict) -> None:
        try:
            ACTIVE_UPLOADS_PATH.write_text(json.dumps(uploads, indent=2))
        except OSError:
            pass

    def update_latest_catalog_times(self, pickup_time: str, report_time: str) -> None:
        if not pickup_time and not report_time:
            return
        catalog_path = WEB_APP_DIR / "briefs.json"
        try:
            catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else []
            if isinstance(catalog, list) and catalog:
                if pickup_time:
                    catalog[0]["pickup_time"] = pickup_time
                if report_time:
                    catalog[0]["report_time"] = report_time
                catalog_path.write_text(json.dumps(catalog, indent=2))
        except Exception:
            pass

    def add_synopsis_to_phone_catalog(self, synopsis_path: Path) -> str:
        briefs_dir = WEB_APP_DIR / "briefs"
        catalog_path = WEB_APP_DIR / "briefs.json"
        static_url = f"./briefs/{synopsis_path.name}"
        target = briefs_dir / synopsis_path.name
        try:
            briefs_dir.mkdir(parents=True, exist_ok=True)
            if synopsis_path.resolve() != target.resolve():
                shutil.copy2(synopsis_path, target)
        except Exception:
            if not target.exists():
                return ""
        try:
            catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else []
            if isinstance(catalog, list) and catalog:
                catalog[0]["synopsis"] = static_url
                catalog_path.write_text(json.dumps(catalog, indent=2))
        except Exception:
            pass
        return static_url

    def handle_synopsis_download(self) -> None:
        source = self.latest_full_brief_path()
        if not source:
            self.respond_json({"ok": False, "error": "No full brief is available yet"}, status=HTTPStatus.NOT_FOUND)
            return
        path = self.synopsis_output_path(source)
        if not path.exists():
            self.respond_json({"ok": False, "error": "Synopsis has not been created yet"}, status=HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def handle_publish_to_phone(self) -> dict:
        if CLOUD_MODE:
            latest = self.read_latest_result()
            return {
                "ok": True,
                "message": "Brief is available on this cloud page.",
                "phone_url": "/",
                "preview_url": "",
                "latest": latest,
                "output": "",
            }

        npx = self.find_executable(NPX_CANDIDATES, "npx")
        if not npx:
            raise RuntimeError("Could not find npx for publishing to Cloudflare Pages.")

        if not WEB_APP_DIR.exists():
            raise RuntimeError("Phone app folder is missing.")

        command = [
            str(npx),
            "wrangler",
            "pages",
            "deploy",
            str(WEB_APP_DIR),
            "--project-name",
            PHONE_PROJECT_NAME,
        ]
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join(
            [
                str(Path(npx).parent),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                env.get("PATH", ""),
            ]
        )
        result = subprocess.run(
            command,
            cwd=str(WEB_APP_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        if result.returncode != 0:
            raise RuntimeError(output or "Publish failed.")

        preview_match = re.search(r"https://[^\s]+\.pages\.dev", output)
        latest = self.read_latest_result()
        return {
            "ok": True,
            "message": "Published to iPhone.",
            "phone_url": PHONE_APP_URL,
            "preview_url": preview_match.group(0) if preview_match else "",
            "latest": latest,
            "output": output[-2000:],
        }

    def handle_resource_upload(self) -> dict:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("Upload must use multipart/form-data.")

        form = parse_multipart_form(self.headers, self.rfile)
        if "resources" not in form:
            raise ValueError("Choose one or more resource files before uploading.")

        RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
        batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        batch_dir = RESOURCE_DIR / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)

        items = form["resources"]
        if not isinstance(items, list):
            items = [items]

        saved: list[dict[str, str]] = []
        for index, item in enumerate(items, start=1):
            filename = getattr(item, "filename", "") or ""
            if not filename:
                continue
            rel_path = self.safe_resource_path(filename)
            target = batch_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                shutil.copyfileobj(item.file, handle)
            saved.append(
                {
                    "name": rel_path.name,
                    "display_name": str(rel_path),
                    "path": str(target),
                    "url": f"/api/resources/download?path={self.url_escape(str(target.relative_to(RESOURCE_DIR)))}",
                    "batch": batch_id,
                }
            )

        return {
            "ok": True,
            "message": f"Saved {len(saved)} resource file(s).",
            "resources": saved,
        }

    def read_latest_result(self) -> dict | None:
        candidates = [OUTPUT_DIR / "latest_desktop_result.json", LATEST_RESULT_FALLBACK_PATH]
        candidates = sorted(candidates, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        for latest_path in candidates:
            try:
                if not latest_path.exists():
                    continue
                return json.loads(latest_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
        return None

    @staticmethod
    def url_escape(value: str) -> str:
        from urllib.parse import quote

        return quote(value, safe="")

    @staticmethod
    def safe_resource_path(filename: str) -> Path:
        pieces: list[str] = []
        for raw in re.split(r"[\\/]+", filename):
            clean = raw.strip().replace("\x00", "")
            if not clean or clean in {".", ".."}:
                continue
            clean = re.sub(r"[^A-Za-z0-9._-]+", "_", clean)
            pieces.append(clean)
        if not pieces:
            pieces = ["resource"]
        return Path(*pieces)

    def list_resources(self) -> list[dict[str, str]]:
        if not RESOURCE_DIR.exists():
            return []
        allowed_suffixes = {".pdf", ".txt", ".md", ".docx", ".png", ".jpg", ".jpeg"}
        records: list[dict[str, str]] = []
        for path in sorted(RESOURCE_DIR.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            relative = path.relative_to(RESOURCE_DIR)
            records.append(
                {
                    "name": path.name,
                    "display_name": str(relative),
                    "path": str(path),
                    "url": f"/api/resources/download?path={self.url_escape(str(relative))}",
                    "size": str(path.stat().st_size),
                    "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes"),
                }
            )
        return records

    def handle_resources(self) -> None:
        self.respond_json({"ok": True, "resources": self.list_resources()})

    @staticmethod
    def extract_resource_text(path: Path) -> str:
        suffix = path.suffix.lower()
        try:
            if suffix == ".pdf":
                reader = PdfReader(str(path))
                return "\n".join((page.extract_text() or "") for page in reader.pages[:80])
            if suffix in {".txt", ".md"}:
                return path.read_text(errors="ignore")
            if suffix == ".docx":
                with zipfile.ZipFile(path) as archive:
                    xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
                text = re.sub(r"<[^>]+>", " ", xml)
                return html.unescape(text)
        except Exception:
            return ""
        return ""

    @staticmethod
    def search_snippet(text: str, query: str, radius: int = 120) -> str:
        compact = normalize_space(text)
        if not compact:
            return ""
        index = compact.lower().find(query.lower())
        if index < 0:
            return compact[: radius * 2].strip()
        start = max(0, index - radius)
        end = min(len(compact), index + len(query) + radius)
        prefix = "..." if start else ""
        suffix = "..." if end < len(compact) else ""
        return f"{prefix}{compact[start:end].strip()}{suffix}"

    def search_resources(self, query: str) -> list[dict[str, str]]:
        query = normalize_space(query)
        if not query or not RESOURCE_DIR.exists():
            return []

        allowed_suffixes = {".pdf", ".txt", ".md", ".docx"}
        results: list[dict[str, str]] = []
        for path in sorted(RESOURCE_DIR.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            relative = path.relative_to(RESOURCE_DIR)
            display_name = str(relative)
            haystack = display_name
            text = ""
            if query.lower() not in haystack.lower():
                text = self.extract_resource_text(path)
                haystack = f"{display_name}\n{text}"
            if query.lower() not in haystack.lower():
                continue
            if not text:
                text = self.extract_resource_text(path)
            results.append(
                {
                    "name": path.name,
                    "display_name": display_name,
                    "url": f"/api/resources/download?path={self.url_escape(str(relative))}",
                    "snippet": self.search_snippet(text or display_name, query),
                    "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes"),
                }
            )
            if len(results) >= 25:
                break
        return results

    def handle_resource_search(self, query: str) -> None:
        params = parse_qs(query)
        search_text = (params.get("q", [""])[0] or "").strip()
        if not search_text:
            self.respond_json({"ok": True, "query": "", "results": []})
            return
        self.respond_json({"ok": True, "query": search_text, "results": self.search_resources(search_text)})

    def handle_resource_download(self, query: str) -> None:
        params = parse_qs(query)
        rel = (params.get("path", [""])[0] or "").strip()
        if not rel:
            self.respond_json({"ok": False, "error": "Missing resource path"}, status=HTTPStatus.BAD_REQUEST)
            return
        candidate = RESOURCE_DIR / Path(rel)
        try:
            candidate.resolve().relative_to(RESOURCE_DIR.resolve())
        except Exception:
            self.respond_json({"ok": False, "error": "Invalid resource path"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not candidate.exists() or not candidate.is_file():
            self.respond_json({"ok": False, "error": "Resource not found"}, status=HTTPStatus.NOT_FOUND)
            return
        mime, _ = mimetypes.guess_type(candidate.name)
        content = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Disposition", f'inline; filename=\"{candidate.name}\"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def read_reference_inventory(self) -> dict:
        categories = {
            "gold_standard_examples": [REFERENCE_DIR / "UA200_GUM-HNL_Gold_Standard_Brief_v3_4.pdf"],
            "seniority": [SENIORITY_SOURCE_PDF],
            "briefings": sorted((REFERENCE_DIR / "Briefings").glob("*")) if (REFERENCE_DIR / "Briefings").exists() else [],
            "training": sorted((REFERENCE_DIR / "777 ALL Training Notes").glob("*")) if (REFERENCE_DIR / "777 ALL Training Notes").exists() else [],
            "routes": sorted((REFERENCE_DIR / "UA Routes").glob("*")) if (REFERENCE_DIR / "UA Routes").exists() else [],
            "contract_and_bidding": sorted((REFERENCE_DIR / "Contract and Bidding").glob("*")) if (REFERENCE_DIR / "Contract and Bidding").exists() else [],
            "root": sorted(REFERENCE_DIR.glob("*")) if REFERENCE_DIR.exists() else [],
        }
        allowed_suffixes = {".pdf", ".txt", ".md", ".docx"}
        return {
            name: [
                {
                    "name": path.name,
                    "path": str(path),
                    "exists": path.exists(),
                }
                for path in paths
                if path.is_file() and path.suffix.lower() in allowed_suffixes
            ]
            for name, paths in categories.items()
        }

    def respond_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_latest_download(self, query: str) -> None:
        params = parse_qs(query)
        kind = (params.get("kind", [""])[0] or "").strip()
        latest = self.read_latest_result() or {}
        mapping = {
            "txt": latest.get("txt_path"),
            "card": latest.get("card_pdf_path"),
            "full": latest.get("full_pdf_path"),
        }
        if kind == "full" and not CLOUD_MODE:
            for candidate in FULL_BRIEF_OVERRIDES:
                if candidate.exists():
                    mapping["full"] = str(candidate)
                    break
        target = mapping.get(kind)
        if not target:
            self.respond_json({"ok": False, "error": "Unknown file kind"}, status=HTTPStatus.NOT_FOUND)
            return

        path = Path(target)
        if not path.exists():
            self.respond_json({"ok": False, "error": "File not found"}, status=HTTPStatus.NOT_FOUND)
            return

        if path.suffix.lower() == ".pdf":
            mime = "application/pdf"
            disposition = "inline"
        elif path.suffix.lower() == ".txt":
            mime = "text/plain; charset=utf-8"
            disposition = "inline"
        else:
            mime, _ = mimetypes.guess_type(path.name)
            mime = mime or "application/octet-stream"
            disposition = "attachment"

        try:
            content = path.read_bytes()
        except OSError:
            self.respond_json({"ok": False, "error": "File is not available to the desktop helper."}, status=HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", f'{disposition}; filename="{path.name}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def resolve_timeline_source(self) -> Path | None:
        latest = self.read_latest_result() or {}
        latest_path = Path(str(latest.get("full_pdf_path") or ""))
        if latest_path.exists():
            try:
                if len(PdfReader(str(latest_path)).pages) >= 8:
                    return latest_path
            except Exception:
                pass
        catalog_path = WEB_APP_DIR / "briefs.json"
        try:
            catalog = json.loads(catalog_path.read_text())
            for item in catalog if isinstance(catalog, list) else []:
                full_pdf = str(item.get("full_pdf") or "").lstrip("./")
                candidate = WEB_APP_DIR / full_pdf
                if candidate.exists() and len(PdfReader(str(candidate)).pages) >= 8:
                    return candidate
        except Exception:
            pass
        for candidate in FULL_BRIEF_OVERRIDES:
            try:
                if candidate.exists() and len(PdfReader(str(candidate)).pages) >= 8:
                    return candidate
            except Exception:
                continue
        return None

    def parse_timeline_points(self, pdf_path: Path) -> list[dict[str, str]]:
        reader = PdfReader(str(pdf_path))
        page_text = reader.pages[3].extract_text() or ""
        lines = [
            normalize_space(line.replace("\x7f", "").replace("\x00", ""))
            for line in page_text.splitlines()
        ]
        lines = [line for line in lines if line]
        try:
            start = lines.index("TIMELINE / ETOPS / FUEL")
        except ValueError:
            return []
        end = len(lines)
        for marker in ("ORCA - ENGINE FAILURE", "ORCA", "HF / OCEANIC"):
            if marker in lines[start + 1 :]:
                end = start + 1 + lines[start + 1 :].index(marker)
                break
        block = [line for line in lines[start + 1 : end] if line not in {"EET", "Event", "Fuel / Alt", "Action"}]
        points: list[dict[str, str]] = []
        idx = 0
        while idx + 3 < len(block):
            time_text = block[idx]
            if not re.fullmatch(r"\d{1,2}:\d{2}", time_text):
                idx += 1
                continue
            points.append(
                {
                    "eet": time_text,
                    "minutes": str(self.time_to_minutes(time_text)),
                    "event": block[idx + 1],
                    "fuel_alt": block[idx + 2],
                    "action": block[idx + 3],
                }
            )
            idx += 4
        return points

    @staticmethod
    def time_to_minutes(value: str) -> int:
        hours, minutes = value.split(":")
        return int(hours) * 60 + int(minutes)

    def handle_timeline(self) -> None:
        source = self.resolve_timeline_source()
        if not source:
            self.respond_json({"ok": False, "error": "Timeline source not available"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            points = self.parse_timeline_points(source)
        except Exception as exc:  # noqa: BLE001
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        latest = self.read_latest_result() or {}
        self.respond_json(
            {
                "ok": True,
                "source": str(source),
                "source_name": source.name,
                "points": points,
                "latest_source": latest.get("source_pdf_name"),
            }
        )

    def serve_static(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.respond_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        mime, _ = mimetypes.guess_type(path.name)
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local self-serve flight brief generator.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    WEB_APP_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Flight Brief desktop helper running at http://{args.host}:{args.port}")
    print(f"Output folder: {OUTPUT_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
