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
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

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
CLOUD_TRIP_KIT_MAX_BYTES = int(os.environ.get("FLIGHT_BRIEF_CLOUD_TRIP_KIT_MAX_BYTES", str(160 * 1024 * 1024)))
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "").strip()
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
CURRENT_BRIEF_DIR = WEB_APP_DIR / "briefs" / "current"
CURRENT_LATEST_PATH = CURRENT_BRIEF_DIR / "latest_result.json"
CURRENT_UPLOADS_DIR = CURRENT_BRIEF_DIR / "uploads"
CURRENT_ACTIVE_UPLOADS_PATH = CURRENT_BRIEF_DIR / "active_uploads.json"
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


def validate_schedule_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def display_flight_time(value: str) -> str:
    text = str(value or "").strip()
    if " " in text:
        return text.rsplit(" ", 1)[-1]
    return text


def extract_google_flights(payload: dict, origin: str, destination: str) -> list[dict[str, str]]:
    results = []
    for itinerary in (payload.get("best_flights") or []) + (payload.get("other_flights") or []):
        segments = itinerary.get("flights") or []
        if len(segments) != 1:
            continue
        segment = segments[0]
        departure = segment.get("departure_airport") or {}
        arrival = segment.get("arrival_airport") or {}
        if departure.get("id") != origin or arrival.get("id") != destination:
            continue
        airline = str(segment.get("airline") or "")
        flight_number = str(segment.get("flight_number") or "")
        if "United" not in airline and not flight_number.upper().startswith("UA"):
            continue
        results.append(
            {
                "flight": flight_number or airline or "United",
                "airline": airline or "United",
                "depart": display_flight_time(departure.get("time", "")),
                "arrive": display_flight_time(arrival.get("time", "")),
                "origin": origin,
                "destination": destination,
                "duration_min": itinerary.get("total_duration") or segment.get("duration") or "",
                "aircraft": segment.get("airplane") or "",
                "source": "Google Flights via SerpApi",
            }
        )
    deduped = []
    seen = set()
    for result in results:
        key = (result["flight"], result["depart"], result["arrive"], result["origin"], result["destination"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped


def fetch_google_flights(origin: str, destination: str, travel_date: str) -> list[dict[str, str]]:
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": travel_date,
        "type": "2",
        "adults": "1",
        "currency": "USD",
        "hl": "en",
        "gl": "us",
        "stops": "1",
        "include_airlines": "UA",
        "show_hidden": "true",
        "api_key": SERPAPI_KEY,
    }
    request = urllib.request.Request(
        f"https://serpapi.com/search.json?{urlencode(params)}",
        headers={"User-Agent": "FlightBrief/1.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return extract_google_flights(payload, origin, destination)


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
        if parsed.path == "/api/commute-flights":
            self.handle_commute_flights(parsed.query)
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

    def handle_commute_flights(self, query: str) -> None:
        params = parse_qs(query)
        try:
            travel_date = validate_schedule_date((params.get("date") or [""])[0] or datetime.now().date().isoformat())
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if not SERPAPI_KEY:
            self.respond_json(
                {
                    "ok": True,
                    "configured": False,
                    "date": travel_date,
                    "message": "Live Google Flights schedule lookup needs SERPAPI_KEY configured on the server.",
                    "routes": {"pscSfo": [], "sfoPsc": []},
                }
            )
            return

        try:
            self.respond_json(
                {
                    "ok": True,
                    "configured": True,
                    "date": travel_date,
                    "routes": {
                        "pscSfo": fetch_google_flights("PSC", "SFO", travel_date),
                        "sfoPsc": fetch_google_flights("SFO", "PSC", travel_date),
                    },
                }
            )
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            self.respond_json(
                {
                    "ok": False,
                    "configured": True,
                    "date": travel_date,
                    "error": f"Live flight lookup failed: {exc}",
                },
                status=HTTPStatus.BAD_GATEWAY,
            )

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
        if slot == "flight_plan":
            self.clear_current_brief()
        active_uploads[slot] = upload_record(target)
        active_uploads = self.write_active_uploads(active_uploads)
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
        active_uploads = self.write_active_uploads(active_uploads)
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
        latest = self.read_latest_result() or {}
        catalog_path = WEB_APP_DIR / "briefs.json"
        try:
            catalog = json.loads(catalog_path.read_text())
        except Exception:
            return {}
        if not isinstance(catalog, list):
            return {}
        entries = [item for item in catalog if isinstance(item, dict)]
        if not entries:
            return {}

        latest_full_name = Path(str(latest.get("full_pdf_path") or "")).name
        latest_source_path = str(latest.get("source_pdf_path") or "")
        latest_source_name = str(latest.get("source_pdf_name") or "")

        def matches_latest(entry: dict) -> bool:
            full_pdf_name = Path(str(entry.get("full_pdf") or "")).name
            source_pdf = str(entry.get("source_pdf") or "")
            return bool(
                (latest_full_name and full_pdf_name == latest_full_name)
                or (latest_source_path and source_pdf == latest_source_path)
                or (latest_source_name and latest_source_name in source_pdf)
            )

        selected = next((entry for entry in entries if matches_latest(entry)), entries[0])
        selected = dict(selected)
        if latest.get("pickup_time"):
            selected["pickup_time"] = latest["pickup_time"]
        if latest.get("report_time"):
            selected["report_time"] = latest["report_time"]
        if latest.get("trip_kit_note"):
            selected["trip_kit_note"] = latest["trip_kit_note"]
        uploads = latest.get("active_uploads") or latest.get("uploads") or {}
        if isinstance(uploads, dict):
            selected["source_documents"] = {
                key: value.get("name")
                for key, value in uploads.items()
                if isinstance(value, dict) and value.get("name")
            }
        return selected

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

        font_title = font("Arial Bold", 72)
        font_h2 = font("Arial Bold", 28)
        font_body = font("Arial", 24)
        font_body_bold = font("Arial Bold", 24)
        font_small = font("Arial", 20)
        font_small_bold = font("Arial Bold", 20)
        font_tiny = font("Arial", 16)
        font_tiny_bold = font("Arial Bold", 16)

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
            draw.rounded_rectangle(rect, radius=18, fill=fill, outline=border, width=2)
            draw.rounded_rectangle((x, y, x2, y + 54), radius=18, fill=accent)
            draw.rectangle((x, y + 28, x2, y + 54), fill=accent)
            draw.text((x + 20, y + 13), title.upper(), fill=white, font=font_small_bold)
            return x + 20, y + 74, x2 - 20, y2 - 20

        def badge(x: int, y: int, label: str, value: str, accent=blue, width: int = 132) -> None:
            draw.rounded_rectangle((x, y, x + width, y + 72), radius=13, fill=(247, 250, 253), outline=border, width=1)
            draw.text((x + 12, y + 10), label.upper(), fill=accent, font=font_tiny_bold)
            draw.text((x + 12, y + 38), ellipsize(value or "--", font_body_bold, width - 24), fill=ink, font=font_body_bold)

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

        def compact_list(items: list[str], x: int, y: int, width: int, limit: int, accent=blue) -> int:
            count = 0
            for item in [normalize_space(str(item or "")) for item in items if normalize_space(str(item or ""))]:
                if count >= limit:
                    break
                draw.ellipse((x, y + 8, x + 8, y + 16), fill=accent)
                draw.text((x + 18, y + 2), ellipsize(item, font_tiny_bold, width - 18), fill=ink, font=font_tiny_bold)
                y += 28
                count += 1
            if count == 0:
                draw.text((x, y), "Verify against OFP.", fill=muted, font=font_tiny_bold)
                y += 28
            return y

        def get_list(key: str) -> list[str]:
            value = record.get(key)
            return value if isinstance(value, list) else []

        def source_documents_text() -> str:
            docs = record.get("source_documents") or {}
            if not isinstance(docs, dict):
                return ""
            labels = {
                "flight_plan": "Flight plan",
                "trip_kit": "Trip kit",
                "pairing": "Pairing",
            }
            parts = [
                f"{labels.get(key, key)}: {value}"
                for key, value in docs.items()
                if value
            ]
            return " | ".join(parts)

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

        def route_path(rect: tuple[int, int, int, int], count: int) -> list[tuple[int, int]]:
            x, y, x2, y2 = rect
            count = max(2, count)
            path: list[tuple[int, int]] = []
            for index in range(count):
                px = int(x + 54 + index * ((x2 - x - 108) / (count - 1)))
                phase = index / max(1, count - 1)
                py = int(y2 - 48 - 92 * (1 - abs(phase - 0.5) * 2))
                path.append((px, py))
            return path

        def draw_route_map(rect: tuple[int, int, int, int], weather: bool = False, title: str | None = None) -> None:
            x, y, x2, y2 = rect
            draw.rounded_rectangle(rect, radius=12, fill=ocean, outline=(188, 207, 222), width=1)
            draw.polygon([(x - 20, y2), (x + 105, y + 42), (x + 174, y2)], fill=(217, 236, 218))
            draw.polygon([(x2 + 24, y2), (x2 - 112, y + 34), (x2 - 202, y2)], fill=(217, 236, 218))
            pts = route_points()
            path = route_path(rect, len(pts))
            if weather:
                weather_text = " ".join(get_list("dispatcher_notes") + get_list("top_threats"))
                colors = [(104, 185, 110), (236, 183, 71), (214, 81, 76)]
                labels = ["light", "moderate", "significant"]
                swath_count = 3 if re.search(r"TS|CB|CONV|SEV", weather_text, re.I) else 2
                for idx in range(swath_count):
                    if len(path) < 2:
                        break
                    start = path[min(idx, len(path) - 2)]
                    end = path[min(idx + 1, len(path) - 1)]
                    offset = 24 + idx * 12
                    shape = [
                        (start[0] - 24, start[1] + offset),
                        (end[0] + 26, end[1] + offset - 20),
                        (end[0] + 44, end[1] + offset + 28),
                        (start[0] - 8, start[1] + offset + 48),
                    ]
                    draw.polygon(shape, fill=colors[min(idx, len(colors) - 1)] + (96,) if sheet.mode == "RGBA" else colors[min(idx, len(colors) - 1)])
                    draw.text((shape[0][0] + 10, shape[0][1] + 8), labels[min(idx, len(labels) - 1)].upper(), fill=white, font=font_tiny_bold)
            for index in range(len(path) - 1):
                draw.line((path[index], path[index + 1]), fill=navy, width=5)
            for index, (px, py) in enumerate(path):
                color = amber if "CP" in pts[index].upper() else blue
                draw.ellipse((px - 10, py - 10, px + 10, py + 10), fill=white, outline=color, width=4)
                label = ellipsize(pts[index], font_tiny_bold, 74)
                tx = max(x + 4, min(px - text_size(label, font_tiny_bold)[0] // 2, x2 - 74))
                draw.text((tx, py + 14), label, fill=navy, font=font_tiny_bold)
            map_title = title or f"{record.get('departure_icao') or 'DEP'} to {record.get('destination_icao') or 'ARR'}"
            draw.text((x + 14, y + 12), map_title, fill=navy, font=font_body_bold)
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

        def draw_timeline_rail(rect: tuple[int, int, int, int]) -> None:
            x, y, x2, y2 = rect
            points = get_list("timeline_points")[:7]
            draw.rounded_rectangle(rect, radius=14, fill=(247, 250, 253), outline=(220, 229, 237))
            draw.text((x + 16, y + 12), "Event Rail", fill=blue, font=font_h2)
            rail_y = y + 88
            draw.line((x + 48, rail_y, x2 - 48, rail_y), fill=(171, 190, 208), width=5)
            if not points:
                draw_wrapped("Timeline did not extract. Use the full PDF timeline page.", x + 16, y + 58, x2 - x - 32, font_small, muted, max_lines=2)
                return
            total = max([int(str(point.get("minutes") or "0") or 0) for point in points] + [1])
            for point in points:
                minutes = int(str(point.get("minutes") or "0") or 0)
                px = int(x + 48 + (x2 - x - 96) * minutes / total)
                event = str(point.get("event") or "")
                color = amber if re.search(r"CP|ETOPS|EEP|EXP", event, re.I) else green if re.search(r"T/O|LDG|IN", event, re.I) else blue
                draw.ellipse((px - 13, rail_y - 13, px + 13, rail_y + 13), fill=white, outline=color, width=5)
                draw.text((px - 24, rail_y - 48), str(point.get("eet") or "--"), fill=color, font=font_tiny_bold)
                draw.text((max(x + 8, min(px - 44, x2 - 104)), rail_y + 22), ellipsize(event, font_tiny_bold, 96), fill=navy, font=font_tiny_bold)

        def draw_fuel_ladder(rect: tuple[int, int, int, int]) -> None:
            x, y, x2, y2 = rect
            rows = [
                ("Plan gate", record.get("plan_gate_fuel")),
                ("Takeoff", record.get("plan_takeoff_fuel") or timeline_value(r"T/O", "")),
                ("CP-1", timeline_value(r"CP-1", "")),
                ("Trip", record.get("trip_fuel")),
                ("Landing", record.get("landing_fuel") or timeline_value(r"LDG|\bIN\b", "")),
                ("Reserve", record.get("far_reserve_fuel")),
                ("Consv", record.get("conservative_fuel")),
            ]
            rows = [(label, str(value)) for label, value in rows if value]
            draw.rounded_rectangle(rect, radius=14, fill=(247, 250, 253), outline=(220, 229, 237))
            draw.text((x + 16, y + 12), "Fuel Picture", fill=green, font=font_h2)
            if not rows:
                draw_wrapped("Fuel summary not extracted. Verify OFP fuel page.", x + 16, y + 48, x2 - x - 32, font_small, muted, max_lines=3)
                return
            bar_x = x + 20
            bar_top = y + 56
            bar_h = min(42, max(24, int((y2 - bar_top - 16) / len(rows)) - 7))
            for idx, (label, value) in enumerate(rows[:6]):
                yy = bar_top + idx * (bar_h + 7)
                color = green if idx < 4 else amber if "Consv" in label else red
                draw.rounded_rectangle((bar_x, yy, x2 - 18, yy + bar_h), radius=9, fill=white, outline=(220, 229, 237))
                draw.rectangle((bar_x, yy, bar_x + 9, yy + bar_h), fill=color)
                draw.text((bar_x + 18, yy + 6), label, fill=muted, font=font_tiny_bold)
                draw.text((x2 - 118, yy + 6), ellipsize(value, font_tiny_bold, 98), fill=ink, font=font_tiny_bold)

        def draw_procedure_chain(rect: tuple[int, int, int, int], title: str, steps: list[str], accent=blue) -> None:
            x, y, x2, y2 = rect
            draw.rounded_rectangle(rect, radius=14, fill=(247, 250, 253), outline=(220, 229, 237))
            draw.text((x + 14, y + 12), title, fill=accent, font=font_h2)
            top = y + 58
            lane_x = x + 34
            draw.line((lane_x, top + 16, lane_x, y2 - 24), fill=(187, 201, 216), width=3)
            for idx, step in enumerate(steps[:5], start=1):
                yy = top + (idx - 1) * max(44, int((y2 - top - 18) / max(1, min(len(steps), 5))))
                draw.ellipse((lane_x - 15, yy - 2, lane_x + 15, yy + 28), fill=white, outline=accent, width=3)
                draw.text((lane_x - 6, yy + 2), str(idx), fill=accent, font=font_tiny_bold)
                draw_wrapped(step, lane_x + 28, yy - 2, x2 - lane_x - 40, font_tiny_bold if idx == 1 else font_tiny, ink, max_lines=2)

        def draw_status_matrix(rect: tuple[int, int, int, int]) -> None:
            x, y, x2, y2 = rect
            threat_text = " ".join(get_list("top_threats") + get_list("dispatcher_notes"))
            statuses = [
                ("Aircraft / Systems", "Normal", green),
                ("Fuel / Weights", "Strong" if record.get("conservative_fuel") or record.get("far_reserve_fuel") else "Verify", green if record.get("conservative_fuel") or record.get("far_reserve_fuel") else amber),
                ("ETOPS / Oceanic", "Strong" if get_list("etops_airports") else "Verify", green if get_list("etops_airports") else amber),
                ("Weather / Ride", "Monitor" if re.search(r"TURB|WX|WIND|CIVIT|FL", threat_text, re.I) else "Normal", amber if re.search(r"TURB|WX|WIND|CIVIT|FL", threat_text, re.I) else green),
                ("Arrival / Destination", "Normal" if record.get("arrival_runway") or record.get("arrival_star") else "Verify", green if record.get("arrival_runway") or record.get("arrival_star") else amber),
            ]
            draw.rounded_rectangle(rect, radius=14, fill=(247, 250, 253), outline=(220, 229, 237))
            draw.text((x + 14, y + 12), "Flight Assessment", fill=blue, font=font_h2)
            yy = y + 58
            for label, value, color in statuses:
                draw.ellipse((x + 16, yy + 7, x + 32, yy + 23), fill=color)
                draw.text((x + 42, yy), label, fill=ink, font=font_small_bold)
                draw.text((x2 - 96, yy), value, fill=color, font=font_small_bold)
                yy += 38

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

        W, H = 3600, 2200
        sheet = Image.new("RGB", (W, H), page_bg)
        draw = ImageDraw.Draw(sheet)
        for band in range(0, H, 22):
            shade = 244 + (band // 22) % 2
            draw.rectangle((0, band, W, band + 22), fill=(shade, min(250, shade + 2), min(253, shade + 5)))

        margin = 46
        gap = 24
        header_h = 190
        draw.rounded_rectangle((margin, 28, W - margin, header_h - 12), radius=20, fill=navy)
        title = f"{str(record.get('flight') or 'FLIGHT').replace(' ', '')}  {record.get('route') or ''}".strip()
        draw.text((margin + 34, 48), title, fill=white, font=font_title)
        subtitle = " | ".join(part for part in [str(record.get("date_code") or ""), str(record.get("aircraft") or ""), f"Release {record.get('release')}" if record.get("release") else ""] if part)
        draw.text((margin + 36, 128), subtitle, fill=(213, 228, 244), font=font_body_bold)
        status_text = f"OUT {record.get('out_local_time') or record.get('out_time') or '--'}   ETA {record.get('eta_local_time') or record.get('eta') or '--'}   BLOCK {record.get('block') or '--'}"
        sw = text_size(status_text, font_body_bold)[0]
        draw.rounded_rectangle((W - margin - sw - 56, 64, W - margin - 26, 124), radius=15, fill=(15, 118, 77))
        draw.text((W - margin - sw - 36, 82), status_text, fill=white, font=font_body_bold)

        top_y = header_h + 24
        top_h = 900
        bottom_y = top_y + top_h + gap
        bottom_h = H - bottom_y - 62
        left_w = 670
        right_w = 760
        center_w = W - margin * 2 - gap * 2 - left_w - right_w
        left_rect = (margin, top_y, margin + left_w, top_y + top_h)
        center_rect = (left_rect[2] + gap, top_y, left_rect[2] + gap + center_w, top_y + top_h)
        right_rect = (center_rect[2] + gap, top_y, W - margin, top_y + top_h)
        bottom_w = int((W - margin * 2 - gap * 4) / 5)
        bottom_cards = [
            (margin + index * (bottom_w + gap), bottom_y, margin + index * (bottom_w + gap) + bottom_w, bottom_y + bottom_h)
            for index in range(5)
        ]

        x, y, x2, y2 = panel(left_rect, "1  Flight Snapshot", navy)
        draw.text((x, y), record.get("route") or "--", fill=navy, font=font_title)
        y += 72
        badge_w = max(142, int((x2 - x - 24) / 3))
        badge(x, y, "Out", str(record.get("out_local_time") or record.get("out_time") or "--"), blue, badge_w)
        badge(x + badge_w + 12, y, "ETA", str(record.get("eta_local_time") or record.get("eta") or "--"), blue, badge_w)
        badge(x + (badge_w + 12) * 2, y, "Block", str(record.get("block") or "--"), blue, badge_w)
        y += 94
        badge(x, y, "Runway", str(record.get("departure_runway") or "--"), amber, badge_w)
        badge(x + badge_w + 12, y, "SID", str(record.get("departure_sid") or "--"), amber, badge_w)
        badge(x + (badge_w + 12) * 2, y, "Alt", str(record.get("alternate") or "--"), green, badge_w)
        y += 94
        y = draw_wrapped(f"{record.get('departure_icao') or 'DEP'} departure to {record.get('destination_icao') or 'ARR'} arrival. STAR {record.get('arrival_star') or 'verify'} runway {record.get('arrival_runway') or 'verify'}.", x, y, x2 - x, font_body, muted, max_lines=3) + 16
        draw.rounded_rectangle((x, y, x2, y + 124), radius=12, fill=light_blue, outline=(190, 211, 235))
        draw.text((x + 12, y + 15), "Dispatch / Release", fill=blue, font=font_h2)
        summary = f"Sector {record.get('dispatch_sector') or '--'}; ETOPS alternates {', '.join(get_list('etops_airports')[:2]) or 'verify'}; release {record.get('release') or '--'}."
        draw_wrapped(summary, x + 12, y + 48, x2 - x - 24, font_small, ink, max_lines=2)
        source_text = source_documents_text()
        if source_text:
            draw_wrapped(source_text, x + 12, y + 88, x2 - x - 24, font_tiny, muted, max_lines=2)
        crew_top = y + 136
        crew_bottom = min(y2, crew_top + 150)
        draw.rounded_rectangle((x, crew_top, x2, crew_bottom), radius=12, fill=(247, 250, 253), outline=(220, 229, 237))
        draw.text((x + 12, crew_top + 14), "Crew / Pairing", fill=blue, font=font_h2)
        crew_lines = [
            f"CA {record.get('captain') or '--'}",
            f"FO {record.get('first_officer') or '--'}",
            f"Purser {record.get('purser') or '--'}",
            f"Pickup {record.get('pickup_time') or '--'}  Report {record.get('report_time') or '--'}",
        ]
        cy = crew_top + 48
        for line in crew_lines:
            if cy + 24 > crew_bottom:
                break
            draw.text((x + 12, cy), ellipsize(line, font_small_bold, x2 - x - 24), fill=ink, font=font_small_bold)
            cy += 26

        x, y, x2, y2 = panel(center_rect, "2  Visual Flight Board", navy)
        draw_route_map((x, y, x2, y + 390), weather=True, title="Route / Weather / ETOPS Picture")
        y += 412
        draw_timeline_rail((x, y, x2, y + 150))
        y += 172
        mini_gap = 18
        mini_w = int((x2 - x - mini_gap * 2) / 3)
        draw_fuel_ladder((x, y, x + mini_w, y2))
        mx = x + mini_w + mini_gap
        draw.rounded_rectangle((mx, y, mx + mini_w, y2), radius=14, fill=(247, 250, 253), outline=(220, 229, 237))
        draw.text((mx + 16, y + 12), "Critical Times", fill=blue, font=font_h2)
        time_items = []
        for pattern in (r"T/O", r"ETOPS Entry", r"CP-1", r"ETOPS Exit", r"\bIN\b|LDG"):
            point = first_matching_timeline(pattern)
            if point:
                time_items.append(f"{point.get('eet')}  {point.get('event')}  {point.get('fuel_alt')}")
        compact_list(time_items, mx + 18, y + 56, mini_w - 36, 4, blue)
        mx2 = mx + mini_w + mini_gap
        draw.rounded_rectangle((mx2, y, x2, y2), radius=14, fill=light_amber, outline=(236, 211, 166))
        draw.text((mx2 + 16, y + 12), "What To Watch", fill=amber, font=font_h2)
        watch_items = (get_list("top_threats") + get_list("dispatcher_notes") + get_list("arrival_brief_points"))[:5]
        compact_list(watch_items, mx2 + 18, y + 56, x2 - mx2 - 36, 4, amber)

        x, y, x2, y2 = panel(right_rect, "3  Threats / ETOPS", red, (255, 252, 252))
        draw.text((x, y), "Threats First", fill=red, font=font_h2)
        y = bullet_list(get_list("top_threats") + get_list("dispatcher_notes"), x, y + 38, x2 - x, 5, red, font_small) + 10
        draw.rounded_rectangle((x, y, x2, y + 145), radius=12, fill=light_blue, outline=(190, 211, 235))
        draw.text((x + 14, y + 14), "ETOPS / Diversion Pair", fill=blue, font=font_h2)
        cp_text = "; ".join(get_list("etops_cp_details")[:2]) or "Verify CP details in OFP."
        draw_wrapped(cp_text, x + 14, y + 50, x2 - x - 28, font_small, ink, max_lines=3)
        y += 164
        badge_w = max(150, int((x2 - x - 24) / 3))
        badge(x, y, "FAR", str(record.get("far_reserve_fuel") or "--"), red, badge_w)
        badge(x + badge_w + 12, y, "Consv", str(record.get("conservative_fuel") or "--"), green, badge_w)
        badge(x + (badge_w + 12) * 2, y, "Pair", " / ".join(get_list("etops_airports")[:2]) or "--", blue, badge_w)
        y += 94
        draw.rounded_rectangle((x, y, x2, y2), radius=12, fill=(247, 250, 253), outline=(220, 229, 237))
        draw.text((x + 14, y + 14), "Key Checkpoints", fill=blue, font=font_h2)
        checkpoint_items = []
        for pattern in (r"ETOPS Entry", r"CP-1", r"ETOPS Exit", r"\bIN\b|LDG"):
            point = first_matching_timeline(pattern)
            if point:
                checkpoint_items.append(f"{point.get('event')}  {point.get('eet')}  {point.get('fuel_alt')}")
        bullet_list(checkpoint_items, x + 16, y + 52, x2 - x - 32, 4, blue, font_small)

        x, y, x2, y2 = panel(bottom_cards[0], "4  PA / Cabin", blue)
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

        x, y, x2, y2 = panel(bottom_cards[1], "5  Departure", amber)
        draw.rounded_rectangle((x, y, x2, y + 92), radius=12, fill=light_amber, outline=(236, 211, 166))
        draw.text((x + 14, y + 14), "Departure Brief", fill=amber, font=font_h2)
        dep = f"RWY {record.get('departure_runway') or '--'} / {record.get('departure_sid') or '--'}; step climb {', '.join(get_list('step_climbs')[:2]) or 'verify'}."
        draw_wrapped(dep, x + 14, y + 48, x2 - x - 28, font_small, ink, max_lines=2)
        y += 112
        draw.text((x, y), "Captain / PF", fill=blue, font=font_h2)
        bullet_list(get_list("captain_discussion_points") + get_list("pilot_flying_points"), x, y + 34, x2 - x, 6, blue, font_small)

        x, y, x2, y2 = panel(bottom_cards[2], "6  Arrival Plan", navy)
        badge_w = max(142, int((x2 - x - 24) / 3))
        badge(x, y, "Airport", str(record.get("destination_icao") or "--"), blue, badge_w)
        badge(x + badge_w + 12, y, "Runway", str(record.get("arrival_runway") or "--"), blue, badge_w)
        badge(x + (badge_w + 12) * 2, y, "STAR", str(record.get("arrival_star") or "--"), blue, badge_w)
        y += 94
        y = bullet_list(get_list("arrival_brief_points"), x, y, x2 - x, 5, green, font_small)
        draw.rounded_rectangle((x, y + 4, x2, min(y2, y + 96)), radius=12, fill=light_green, outline=(190, 224, 205))
        draw_wrapped("Stable gates: 1500 gear down, 1000 checklist/speed complete, 500 stable call.", x + 12, y + 18, x2 - x - 24, font_small_bold, green, max_lines=3)

        x, y, x2, y2 = panel(bottom_cards[3], "7  Operational Review", blue)
        half = int((y2 - y - 18) / 2)
        draw_procedure_chain(
            (x, y, x2, y + half),
            "ORCA / Engine",
            ["Fly the airplane", "Navigate and offset as required", "Communicate", "Checklist", "Diversion decision"],
            blue,
        )
        draw_procedure_chain(
            (x, y + half + 18, x2, y2),
            "Driftdown",
            ["Offset 5 NM L/R using LNAV", "Minimize descent until offset", "Below FL290 decision point", "500 ft vertical offset", "Proceed toward suitable diversion"],
            amber,
        )

        x, y, x2, y2 = panel(bottom_cards[4], "8  Captain's Corner", navy)
        draw_status_matrix((x, y, x2, y + 250))
        ay = y + 268
        system_title, prompts = system_focus()
        draw.rounded_rectangle((x, ay, x2, ay + 150), radius=12, fill=light_green, outline=(190, 224, 205))
        draw.text((x + 12, ay + 14), "Today's System", fill=green, font=font_h2)
        draw.text((x + 12, ay + 44), ellipsize(system_title, font_body_bold, x2 - x - 24), fill=ink, font=font_body_bold)
        bullet_list(prompts, x + 12, ay + 78, x2 - x - 24, 3, green, font_tiny)
        ay += 170
        draw.rounded_rectangle((x, ay, x2, y2), radius=12, fill=light_amber, outline=(236, 211, 166))
        draw.text((x + 12, ay + 14), "Final Takeaway", fill=amber, font=font_h2)
        destination = record.get("destination_icao") or record.get("destination") or "destination"
        threat = (get_list("top_threats") or get_list("dispatcher_notes") or ["route and arrival workload"])[0]
        takeaway = (
            f"{record.get('route') or 'This trip'}: protect fuel checks and route monitoring, "
            f"keep {threat} in view, and set up a disciplined {destination} arrival."
        )
        draw_wrapped(takeaway, x + 12, ay + 48, x2 - x - 24, font_small, ink, max_lines=6)

        footer = "Gold Standard Pilot Brief visual board from latest uploaded docs. Visual reference only. Verify against official OFP, FMS, ATIS, NOTAMs, charts, and company manuals."
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
        latest = self.persist_current_brief(latest)
        latest_payload = json.dumps(latest, indent=2)
        for latest_path in (OUTPUT_DIR / "latest_desktop_result.json", LATEST_RESULT_FALLBACK_PATH):
            try:
                latest_path.write_text(latest_payload)
            except OSError:
                continue

    def persist_current_brief(self, latest: dict) -> dict:
        persistent = dict(latest)
        try:
            CURRENT_BRIEF_DIR.mkdir(parents=True, exist_ok=True)
            for key in ("txt_path", "card_pdf_path", "full_pdf_path", "synopsis_pdf_path"):
                source_value = persistent.get(key)
                if not source_value:
                    continue
                source = Path(str(source_value))
                if not source.exists() or not source.is_file():
                    continue
                target = CURRENT_BRIEF_DIR / source.name
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                persistent[key] = str(target)
            CURRENT_LATEST_PATH.write_text(json.dumps(persistent, indent=2))
        except OSError:
            return latest
        return persistent

    def clear_current_brief(self) -> None:
        try:
            if CURRENT_BRIEF_DIR.exists():
                for child in CURRENT_BRIEF_DIR.iterdir():
                    if child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        shutil.rmtree(child)
            for latest_path in (OUTPUT_DIR / "latest_desktop_result.json", LATEST_RESULT_FALLBACK_PATH):
                if latest_path.exists():
                    latest_path.unlink()
        except OSError:
            pass

    def read_active_uploads(self) -> dict:
        def existing_uploads(uploads: dict) -> dict:
            return {
                key: value
                for key, value in uploads.items()
                if isinstance(value, dict) and value.get("path") and Path(value["path"]).exists()
            }

        for active_path in (ACTIVE_UPLOADS_PATH, CURRENT_ACTIVE_UPLOADS_PATH):
            try:
                if not active_path.exists():
                    continue
                payload = json.loads(active_path.read_text())
                if isinstance(payload, dict):
                    existing = existing_uploads(payload)
                    if existing:
                        return existing
            except (OSError, json.JSONDecodeError):
                continue
        latest = self.read_latest_result() or {}
        uploads = latest.get("active_uploads") or latest.get("uploads") or {}
        return existing_uploads(uploads) if isinstance(uploads, dict) else {}

    def persist_active_uploads(self, uploads: dict) -> dict:
        persistent: dict[str, dict[str, str]] = {}
        try:
            CURRENT_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            for key, value in uploads.items():
                if not isinstance(value, dict) or not value.get("path"):
                    continue
                source = Path(str(value["path"]))
                if not source.exists() or not source.is_file():
                    continue
                target = CURRENT_UPLOADS_DIR / sanitize_filename(value.get("name") or source.name)
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                persistent[key] = upload_record(target)
            if persistent:
                CURRENT_ACTIVE_UPLOADS_PATH.write_text(json.dumps(persistent, indent=2))
        except OSError:
            return uploads
        return persistent or uploads

    def write_active_uploads(self, uploads: dict) -> dict:
        uploads = self.persist_active_uploads(uploads)
        try:
            ACTIVE_UPLOADS_PATH.write_text(json.dumps(uploads, indent=2))
        except OSError:
            pass
        latest = self.read_latest_result() or {}
        if latest:
            latest["active_uploads"] = uploads
            latest["uploads"] = uploads
            self.write_latest_result(latest)
        return uploads

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
        candidates = [OUTPUT_DIR / "latest_desktop_result.json", LATEST_RESULT_FALLBACK_PATH, CURRENT_LATEST_PATH]
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
