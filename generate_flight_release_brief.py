from __future__ import annotations

import argparse
import hashlib
import shutil
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


SECTION_BREAK = "===== PAGE "
HEADER_COLOR = HexColor("#143a52")
THREAT_COLOR = HexColor("#7a1f1f")
TEXT_COLOR = HexColor("#111111")
MUTED_COLOR = HexColor("#4b5563")
PANEL_BG = HexColor("#f4f7f9")
THREAT_BG = HexColor("#fbefef")
FUEL_BG = HexColor("#eef6ed")
ETOPS_BG = HexColor("#eef3f8")
ACCENT_BLUE = HexColor("#0f5ea8")
ACCENT_GOLD = HexColor("#d9a441")
SFO_ENGINE_OUT_PROCEDURE = "SFO departure engine-out: at 2 NM from SFO VOR, right turn heading 310 to intercept the SFO 294 radial."
FM_STABILIZED_APPROACH = (
    "777 FM gates: by 1500 feet gear down; by 1000 feet RA speedbrake armed, landing checklist complete, target speed set; "
    "at 500 feet RA call stable or unstable/go-around."
)
FM_GO_AROUND = (
    "777 go-around: TO/GA, verify thrust, announce 'Going around, flaps 20', pitch to FD or 15 degrees, positive rate gear up, set missed approach altitude."
)
FM_RTO = (
    "777 RTO: if RTO selected before takeoff, maximum braking applies above 85 knots when both thrust levers are retarded to idle; "
    "below 85 knots autobrakes do not apply."
)
FM_WINDSHEAR_ESCAPE = (
    "777 reactive windshear escape: TO/GA, maximum thrust, stow speedbrake, wings level, rotate toward 15 degrees, follow FD if available, "
    "do not change flap, gear, or trim."
)
ETOPS_DRIFTDOWN_OFFSET = [
    "Oceanic contingency: offset 5 NM left or right using LNAV; minimize descent until established on offset.",
    "If descending below FL290: parallel original course until below FL290, then descend to desired 500 ft vertical offset and proceed toward diversion.",
    "If not descending below FL290: descend to desired 500 ft vertical offset and proceed toward diversion.",
    "Once on offset, consider increased descent rate to facilitate earlier turn toward the diversion airport; obtain/follow ATC clearance as able.",
]

SYSTEM_REVIEW_BANK = [
    {
        "title": "Fuel",
        "items": [
            "Cross-check gate, taxi, takeoff, landing, FAR reserve, and conservative fuel before push.",
            "Landing fuel target: {landing_fuel}; FAR reserve {far_reserve_fuel}; conservative fuel {conservative_fuel}.",
            "At each CP compare actual fuel to OFP required fuel, not just planned fuel.",
            "Minimum fuel: advise ATC when committed to land with no undue delay; emergency fuel when predicted below required threshold.",
        ],
    },
    {
        "title": "Hydraulics",
        "items": [
            "Review center / left / right hydraulic system consumers and backup paths before oceanic entry.",
            "If a hydraulic system is lost, slow down, divide QRH work, and protect landing configuration planning.",
            "Confirm brake source, steering capability, autobrake expectations, and landing distance before committing.",
            "Brief dispatch / maintenance coordination early if system loss affects ETOPS diversion suitability.",
        ],
    },
    {
        "title": "Electrical",
        "items": [
            "Review normal sources: engine generators, APU generator, external power, main and transfer buses.",
            "For electrical non-normal, stabilize aircraft first, then confirm source/bus status before switch action.",
            "Protect navigation, communication, fuel, anti-ice, and pressurization requirements during diversion planning.",
            "If on standby/alternate power path, brief time limits, equipment loss, and approach capability early.",
        ],
    },
    {
        "title": "Pneumatics / Pressurization",
        "items": [
            "Review packs, bleeds, isolation logic, cabin rate, and cabin altitude cues before long overwater segment.",
            "Cabin altitude: oxygen masks first, establish crew comms, then emergency descent / diversion flow.",
            "For bleed/pack issues, confirm anti-ice, pressurization, performance, and fuel effects before continuing.",
            "Brief safe altitude, terrain, weather, and nearest suitable diversion before checklist tunnel vision.",
        ],
    },
    {
        "title": "Flight Controls",
        "items": [
            "Review primary flight control indications, trim, speedbrake, and autopilot/flight director implications.",
            "For flight control non-normal, avoid abrupt inputs and let the PF focus on attitude, speed, and path.",
            "Plan a longer setup: configuration, controllability check if directed, landing distance, and runway selection.",
            "Assign PM to QRH, radios, and performance while PF keeps the airplane simple and stable.",
        ],
    },
    {
        "title": "Navigation / FMS",
        "items": [
            "Before oceanic / Class II, verify clearance against route, waypoints, coordinates, and FMS legs.",
            "Confirm position, next two fixes, fuel trend, step plan, and gross error checks at appropriate gates.",
            "If rerouting or diverting, build then cross-check the route before activating; protect terrain and fuel.",
            "Keep raw data / map scale awareness when weather, offset, or contingency routing starts moving quickly.",
        ],
    },
    {
        "title": "Fire Protection",
        "items": [
            "Review engine, APU, cargo, wheel well, and lavatory fire cues and memory-item posture.",
            "Cargo fire mindset: memory/QRH, immediate diversion assessment, TEST cabin plan, and landing priority.",
            "Engine fire/severe damage: fly, memory items, QRH, communicate, then evaluate drift-down and diversion.",
            "Brief who talks to cabin, who talks to ATC/dispatch, and who manages performance/fuel.",
        ],
    },
    {
        "title": "Engines / APU",
        "items": [
            "Review engine limit cues, EICAS priorities, autothrottle mode awareness, and engine-out flight path.",
            "Engine non-normal: fly first, memory items if required, QRH, then drift-down/diversion/fuel plan.",
            "APU availability matters for electrical, pneumatic, ETOPS, and ground-support planning after diversion.",
            "Brief start limits, abnormal start mindset, and who coordinates with dispatch / maintenance.",
        ],
    },
    {
        "title": "Autoflight",
        "items": [
            "Review AFDS mode awareness: active mode, armed mode, target speed/altitude, and FMA callouts.",
            "If automation surprises you, simplify: heading/vertical speed/altitude hold or hand-fly as appropriate.",
            "Oceanic / ETOPS: protect lateral path, altitude constraints, step climbs, and route clearance compliance.",
            "Arrival setup: verify VNAV path, MCP altitude, missed approach altitude, and approach mode captures.",
        ],
    },
    {
        "title": "Landing Gear / Brakes",
        "items": [
            "Review gear extension limits, alternate extension mindset, brake source, autobrake, and steering effects.",
            "Before landing with gear/brake abnormal, confirm runway length, braking action, wind, and stopping margin.",
            "Rejected takeoff: know RTO autobrake logic, reject criteria, and who owns directional control/calls.",
            "Diversion planning: runway length, ARFF, tow capability, taxi/turnoff plan, and passenger handling.",
        ],
    },
    {
        "title": "Anti-Ice / Rain",
        "items": [
            "Review icing definition, engine anti-ice use, wing anti-ice logic, and performance effects.",
            "Before descent/approach, compare TAT/OAT, visible moisture, cloud tops, and runway contamination risk.",
            "If icing or heavy rain is present, brief thrust, speed additives, braking action, and go-around path.",
            "Confirm probe/window heat, wipers, and radar strategy before weather or night operations get busy.",
        ],
    },
    {
        "title": "Communications / Surveillance",
        "items": [
            "Review VHF/HF/SatVoice/CPDLC plan, SELCAL, guard, and dispatch contact path for the route.",
            "Oceanic: confirm position reporting, contingency broadcasts, transponder/ADS-C expectations, and comm backup.",
            "If comm is degraded, prioritize aviate/navigate, assigned route/altitude, broadcasts, and predictable actions.",
            "Diversion: establish ATC, dispatch, cabin, company, and emergency-service communication plan early.",
        ],
    },
    {
        "title": "Instruments / Displays",
        "items": [
            "Review PFD/ND/EICAS scan, comparator flags, standby instruments, and source-selector discipline.",
            "Unreliable airspeed or display disagreement: pitch/thrust, attitude, QRH, and clear PF/PM roles.",
            "Before oceanic entry, verify altimeters, RVSM requirements, navigation accuracy, and display range/terrain.",
            "If displays degrade, simplify the flight path and brief raw-data/standby-instrument expectations.",
        ],
    },
    {
        "title": "Warning Systems",
        "items": [
            "Review EICAS priority, master warning/caution discipline, and when to stop checklist work to fly.",
            "GPWS/terrain, windshear, TCAS, and stall warnings each need immediate recognition and clean crew calls.",
            "Before takeoff, brief which alerts drive an RTO and which alerts continue above 80 knots.",
            "In cruise, use alert priority to assign PF flying, PM checklist/radios, and relief pilot support.",
        ],
    },
    {
        "title": "Oxygen / Emergency Equipment",
        "items": [
            "Review crew oxygen mask use, interphone comms, passenger oxygen assumptions, and emergency descent flow.",
            "Rapid decompression: masks, establish comms, descent, transponder/ATC, cabin, terrain, and diversion.",
            "Before overwater, brief life vests/rafts, TEST cabin coordination, and time-to-landing communication.",
            "Confirm emergency equipment mindset before long oceanic segments, especially with relief crew transitions.",
        ],
    },
]

MEMORY_REVIEW_BANK = [
    "Airspeed unreliable: memory items, pitch/thrust, QRH, communicate roles.",
    "Cabin altitude / rapid decompression: oxygen masks, crew comms, emergency descent, passenger signs.",
    "Engine fire/severe damage/separation: memory items then QRH; overwater diversion mindset.",
    "Windshear escape: TO/GA, max thrust, stow speedbrake, wings level, follow FD if available.",
    "RTO: reject only for master warning, engine failure/fire, predictive windshear, unsafe/unable above 80 knots.",
    "Terrain escape: TO/GA, maximum thrust, pitch to guidance, verify positive climb, avoid configuration changes until safe.",
    "Rejected landing: TO/GA, verify thrust, pitch / go-around mode, and be alert for low-altitude mode changes.",
    "Smoke/fumes: oxygen masks and comms first, source isolation, cabin coordination, immediate landing evaluation.",
    "Dual FMC / navigation degradation: maintain heading/track awareness, raw data, ATC coordination, and fuel impact.",
    "Engine-out drift-down: fly profile, declare intentions, offset/oceanic contingency, diversion and fuel check.",
]
AIRPORT_NOTES_PATH = Path(__file__).with_name("airport-reference-library") / "airport_briefing_notes.json"
FLIGHT_OVERRIDES_PATH = Path(__file__).with_name("airport-reference-library") / "flight_brief_overrides.json"
GOLD_STANDARD_REFERENCE_DIR = Path(
    os.environ.get(
        "FLIGHT_BRIEF_REFERENCE_DIR",
        "/Users/brianhope/Desktop/Flight Plan/Gold Standard Pilot Brief",
    )
)
GOLD_STANDARD_EXAMPLE_PDF = GOLD_STANDARD_REFERENCE_DIR / "UA200_GUM-HNL_Gold_Standard_Brief_v3_4.pdf"
REFERENCE_BRIEFINGS_DIR = GOLD_STANDARD_REFERENCE_DIR / "Briefings"
REFERENCE_TRAINING_DIR = GOLD_STANDARD_REFERENCE_DIR / "777 ALL Training Notes"
REFERENCE_ROUTES_DIR = GOLD_STANDARD_REFERENCE_DIR / "UA Routes"
REFERENCE_CONTRACT_DIR = GOLD_STANDARD_REFERENCE_DIR / "Contract and Bidding"
SENIORITY_SOURCE_PDF = REFERENCE_CONTRACT_DIR / "Category Summary June 2026.pdf"
INTEGRATED_SENIORITY_SOURCE_PDF = REFERENCE_CONTRACT_DIR / "Integrated Seniority List.pdf"
SENIORITY_CACHE_PATH = GOLD_STANDARD_REFERENCE_DIR / "seniority_cache.json"
APP_HOME_URL = "https://flight-briefs-brian-hope.pages.dev"


@dataclass
class BriefData:
    raw_text: str = field(default="", repr=False)
    source_pdf_path: str = ""
    flight: str = ""
    date_code: str = ""
    trip_id: str = ""
    pairing_note: str = ""
    captain: str = ""
    first_officer: str = ""
    iros: list[str] = field(default_factory=list)
    purser: str = ""
    flight_attendants: list[str] = field(default_factory=list)
    route: str = ""
    departure_runway: str = ""
    departure_sid: str = ""
    arrival_star: str = ""
    arrival_runway: str = ""
    dispatch_sector: str = ""
    departure_gusty: bool = False
    arrival_gusty: bool = False
    departure: str = ""
    destination: str = ""
    departure_icao: str = ""
    destination_icao: str = ""
    aircraft_reg: str = ""
    aircraft_type: str = ""
    release_number: str = ""
    out_time: str = ""
    out_local_time: str = ""
    eta: str = ""
    eta_local_time: str = ""
    block: str = ""
    block_variance: str = ""
    pickup_time: str = ""
    report_time: str = ""
    etops_minutes: str = ""
    dispatch_alternate: str = ""
    minimum_takeoff_fuel: str = ""
    arrival_gate: str = ""
    takeoff_fuel: str = ""
    landing_fuel: str = ""
    extra_fuel: str = ""
    taxi_fuel: str = ""
    far_reserve_fuel: str = ""
    conservative_fuel: str = ""
    zfw_actual: str = ""
    zfw_limit: str = ""
    tow_actual: str = ""
    tow_limit: str = ""
    lw_actual: str = ""
    lw_limit: str = ""
    step_climbs: list[str] = field(default_factory=list)
    etops_airports: list[str] = field(default_factory=list)
    etops_cp_details: list[str] = field(default_factory=list)
    route_alternates: list[str] = field(default_factory=list)
    dispatcher_remarks: list[str] = field(default_factory=list)
    etops_summary: list[str] = field(default_factory=list)
    departure_notes: list[str] = field(default_factory=list)
    destination_notes: list[str] = field(default_factory=list)
    alternate_notes: list[str] = field(default_factory=list)
    aircraft_notes: list[str] = field(default_factory=list)
    weather_threats: list[str] = field(default_factory=list)
    threats: list[str] = field(default_factory=list)
    fa_discussion_points: list[str] = field(default_factory=list)
    captain_discussion_points: list[str] = field(default_factory=list)
    pilot_flying_points: list[str] = field(default_factory=list)
    arrival_brief_points: list[str] = field(default_factory=list)
    passenger_pa_notes: list[str] = field(default_factory=list)
    crew_members: list[dict[str, str]] = field(default_factory=list)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    chunks: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        chunks.append(f"{SECTION_BREAK}{index} =====")
        chunks.append((page.extract_text() or "").replace("\x00", "").strip())
    return "\n".join(chunks)


def extract_trip_kit_relevant_text(pdf_path: Path, airports: list[str], max_pages: int = 28) -> str:
    reader = PdfReader(str(pdf_path))
    airport_tokens = {
        token.upper()
        for airport in airports
        for token in (airport, airport[-3:] if len(airport) >= 3 else airport)
        if token
    }
    keep_markers = (
        "AERODROME INFORMATION",
        "OFFICIAL PILOT BRIEFING",
        "10-7",
        "NOTAM",
        "TAXI",
        "RUNWAY",
        "RWY",
        "TWY",
        "AIRPORT",
    )
    chunks: list[str] = []
    kept = 0
    for index, page in enumerate(reader.pages, start=1):
        try:
            page_text = (page.extract_text() or "").replace("\x00", "").strip()
        except Exception:
            continue
        upper = page_text.upper()
        has_airport = any(token in upper for token in airport_tokens)
        has_marker = any(marker in upper for marker in keep_markers)
        if not (has_airport or (has_marker and kept < 6)):
            continue
        chunks.append(f"{SECTION_BREAK}{index} =====")
        chunks.append(page_text)
        kept += 1
        if kept >= max_pages:
            break
    return "\n".join(chunks)


def match_group(pattern: str, text: str, group: int = 1, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    return normalize_space(match.group(group)) if match else ""


def parse_route_alternates(ofp_text: str) -> list[str]:
    match = re.search(r"RALT/([A-Z0-9\s]+?)\s+RMK/", ofp_text, re.S)
    if not match:
        return []
    raw_tokens = [item for item in normalize_space(match.group(1)).split() if item]
    merged: list[str] = []
    idx = 0
    while idx < len(raw_tokens):
        token = raw_tokens[idx]
        if len(token) == 1 and idx + 1 < len(raw_tokens) and len(raw_tokens[idx + 1]) == 3:
            merged.append(token + raw_tokens[idx + 1])
            idx += 2
            continue
        if len(token) == 2 and idx + 1 < len(raw_tokens) and len(raw_tokens[idx + 1]) == 2:
            merged.append(token + raw_tokens[idx + 1])
            idx += 2
            continue
        merged.append(token)
        idx += 1
    return merged


def parse_dispatcher_remarks(ofp_text: str) -> list[str]:
    match = re.search(
        r"DISPATCHER REMARKS\s+(.*?)\s+SYSTEM INFO",
        ofp_text,
        re.S,
    )
    if not match:
        return []
    remarks = [normalize_space(line) for line in match.group(1).splitlines() if line.strip()]
    return remarks


def parse_threats_from_remarks(remarks: list[str]) -> list[str]:
    threats: list[str] = []
    for remark in remarks:
        upper = remark.upper()
        if "TURB" in upper:
            threats.append(remark)
        if "CB" in upper or "TS" in upper:
            threats.append(remark)
    return threats


def extract_turbulence_timing(data: BriefData) -> str:
    windows: list[str] = []
    route_based: list[str] = []

    for item in data.threats:
        upper = item.upper()
        if "TURB" not in upper:
            continue

        for start, end in re.findall(r"(\d{1,2}:\d{2}|\d{4})\s*-\s*(\d{1,2}:\d{2}|\d{4})", item):
            windows.append(f"{normalize_eet_time(start)}-{normalize_eet_time(end)} EET")

        if not re.search(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}", item) and re.search(r"\b[A-Z0-9]+\s*-\s*[A-Z0-9]+\b", item):
            route_based.append(item)

    windows = dedupe_preserve_order(windows)
    if windows:
        window_text = windows[0] if len(windows) == 1 else ", ".join(windows)
        if route_based:
            return f"Expected ride enroute near {route_based[0]}; main timed window about {window_text}."
        if len(windows) == 1:
            return f"Expected ride window about {windows[0]}."
        return "Expected ride windows about " + ", ".join(windows) + "."

    if route_based:
        return f"Ride expected enroute: {route_based[0]}."

    return "No specific turbulence timing extracted from the release."


def normalize_eet_time(value: str) -> str:
    clean = value.strip()
    if re.fullmatch(r"\d{4}", clean):
        return f"{clean[:2]}:{clean[2:]}"
    return clean


def parse_etops_summary(text: str) -> list[str]:
    rows = []
    for cp, remain, req in re.findall(
        r"(CP-\d)\s+[^\\n]*?FUEL REMAINING\s+(\d+)\s+FUEL REQD\s+(\d+)",
        text,
        re.S,
    ):
        rows.append(f"{cp}: remaining {format_fuel_int(remain)} / required {format_fuel_int(req)}")
    return rows


def parse_etops_airports_and_details(text: str) -> tuple[list[str], list[str]]:
    airports: list[str] = []
    details: list[str] = []

    suitable_matches = re.findall(r"([A-Z]{4}/[A-Z]{3})\s+SUITABLE", text)
    for airport in suitable_matches:
        if airport not in airports:
            airports.append(airport)

    cp_pattern = re.compile(
        r"(CP-\d)\s+([A-Z]{4})-(\d+)/([A-Z]{4})-(\d+)\s+[0-9:]+\s+([NS]\d{4}\.\d)\s+([WE]\d{5}\.\d)"
    )
    for cp, airport_a, dist_a, airport_b, dist_b, lat, lon in cp_pattern.findall(text):
        details.append(
            f"{cp}: {lat} {lon} | {airport_a} {dist_a}NM | {airport_b} {dist_b}NM"
        )

    return airports, details


def parse_step_climbs(text: str) -> list[str]:
    route_match = re.search(r"20 ECON F320 (.*?) DOLSU/F", text, re.S)
    route_targets: dict[str, str] = {}
    if route_match:
        for point, level in re.findall(r"([0-9A-Z]+)/(F\d{3})", route_match.group(1)):
            route_targets[level] = point

    climbs: list[str] = []
    for et, level in re.findall(r"DCT \(T/C\).*?\n\s*([0-9]{1,2}:[0-9]{2})\s*\n\s*\d+\s+(F\d{3})", text, re.S):
        try:
            flight_level = int(level[1:])
        except ValueError:
            continue
        if flight_level <= 320:
            continue
        point = route_targets.get(level, "")
        if point:
            climbs.append(f"{level} at {point} around {et} EET")
        else:
            climbs.append(f"{level} around {et} EET")
    return climbs


def parse_departure_runway(text: str) -> str:
    match = re.search(r"\bR(\d{2}[LRC]?)\s+[A-Z0-9]+\b", text)
    return match.group(1) if match else ""


def parse_departure_sid(text: str) -> str:
    fpl_match = re.search(r"\([^\n]*FPL-[\s\S]*?-(?:[A-Z]{4})\d{4}\s*\n-([^\n]+(?:\n[^\n]+)*?)\n-[A-Z]{4}\d{4}", text)
    if fpl_match:
        route_lines = normalize_space(fpl_match.group(1).replace("\n", " "))
        route_lines = re.sub(r"^(?:(?:[A-Z]?\d{4}[FS]\d{3}|M\d{3}F\d{3})\s+)+", "", route_lines)
        sid_match = re.search(r"^(?:DCT\s+)?([A-Z0-9]+(?:\d[A-Z]?)?)\b", route_lines)
        if sid_match:
            return normalize_space(sid_match.group(1))

    match = re.search(r"-N\d{4}F\d{3}\s+([A-Z0-9]+\d[A-Z]?)\s", text)
    return normalize_space(match.group(1)) if match else ""


def parse_arrival_runway_and_star(text: str) -> tuple[str, str]:
    runway = ""
    star = ""

    fpl_match = re.search(r"\([^\n]*FPL-[\s\S]*?-(?:[A-Z]{4})\d{4}\s*\n-([^\n]+(?:\n[^\n]+)*?)\n-([A-Z]{4})\d{4}", text)
    if fpl_match:
        route_lines = normalize_space(fpl_match.group(1).replace("\n", " "))
        star_match = re.search(r"\b([A-Z0-9]+\d[A-Z]?)\s*$", route_lines)
        if star_match:
            star = normalize_space(star_match.group(1))

    if star:
        runway_matches = re.findall(rf"\bR(\d{{2}}[LRC]?)\s+(?:\(T/D\)\s+)?(?:[A-Z0-9]+\s+)?{re.escape(star[:-1] if star.endswith(tuple('0123456789')) else star)}", text)
        if runway_matches:
            runway = runway_matches[-1]

    if not runway:
        td_matches = re.findall(r"\bR(\d{2}[LRC]?)\s+\(T/D\)", text)
        if td_matches:
            runway = td_matches[-1]

    runway_match = re.search(r"\bYBBN\s+\d+\s+FT\b", text)
    if runway_match:
        runway = ""

    return runway, star


def parse_dispatch_sector(text: str) -> str:
    match = re.search(r"DISPATCH SECTOR\s*[:\-]?\s*([A-Z0-9]+)", text, re.I)
    if match:
        return normalize_space(match.group(1))

    channel_match = re.search(r"\bCH\s*([0-9]{1,3})\b", text, re.I)
    if channel_match:
        return normalize_space(channel_match.group(1))

    return ""


def format_fuel_int(value: str) -> str:
    try:
        number = int(value)
    except ValueError:
        return value
    return f"{number / 1000:.1f}"


def format_fuel_float(value: float) -> str:
    return f"{value:.1f}"


def format_weight_margin(value: int) -> str:
    return f"{value:,} lb"


def parse_weight_pair(text: str, label: str) -> tuple[str, str]:
    match = re.search(rf"\b{label}\s+(\d+)\s*/\s*(\d+)[A-Z]?", text)
    if match:
        return match.group(1), match.group(2)

    column_match = re.search(
        r"ZFW\s+TOW\s+LW\s+(\d+)\s+(\d+)\s+(\d+)\s+/\s+/\s+/.*?(\d+)[A-Z]\s+(\d+)[A-Z]\s+(\d+)[A-Z]",
        text,
        re.S,
    )
    if not column_match:
        return "", ""

    actuals = {
        "ZFW": column_match.group(1),
        "TOW": column_match.group(2),
        "LW": column_match.group(3),
    }
    limits = {
        "ZFW": column_match.group(4),
        "TOW": column_match.group(5),
        "LW": column_match.group(6),
    }
    return actuals.get(label, ""), limits.get(label, "")


def classify_weight_threats(data: BriefData) -> list[str]:
    threats: list[str] = []
    close_items: list[str] = []
    threshold = 2000

    for label, actual, limit in [
        ("ZFW", data.zfw_actual, data.zfw_limit),
        ("TOW", data.tow_actual, data.tow_limit),
        ("LW", data.lw_actual, data.lw_limit),
    ]:
        if not actual or not limit:
            continue
        try:
            margin = int(limit) - int(actual)
        except ValueError:
            continue
        if margin <= threshold:
            close_items.append(f"{label} margin {format_weight_margin(margin)}")

    if close_items:
        threats.append("Weight-restriction risk: " + "; ".join(close_items) + ".")

    return threats


def airport_weather_section(text: str, code: str) -> str:
    if not code:
        return ""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(rf"^{re.escape(code)}\s+-", normalize_space(line)):
            start = index
            break
    if start is None:
        return ""

    block: list[str] = []
    for line in lines[start:]:
        clean = normalize_space(line)
        if block and re.match(r"^[A-Z]{4}\s+-", clean):
            break
        block.append(line)
    return "\n".join(block)


def first_taf_block(text: str, code: str) -> str:
    section = airport_weather_section(text, code)
    pattern = r"(TAF .*?=)"
    match = re.search(pattern, section, re.S)
    return normalize_space(match.group(1)) if match else ""


def first_metar_block(text: str, code: str) -> str:
    section = airport_weather_section(text, code)
    pattern = r"(METAR .*?=)"
    match = re.search(pattern, section, re.S)
    return normalize_space(match.group(1)) if match else ""


def parse_block_variance(text: str) -> str:
    return match_group(r"\n[0-9]{1,2}:[0-9]{2}/([0-9]{2}:\d{2}[EL])\s+V(?:RNT|RT)", text)


def minutes_to_hours_minutes(value: str) -> str:
    if not value:
        return ""
    parts = value.split(":")
    if len(parts) != 2:
        return value
    hours = int(parts[0])
    minutes = int(parts[1])
    total_minutes = hours * 60 + minutes
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours}:{minutes:02d}"
    if hours:
        return f"{hours}:00"
    return f"0:{minutes:02d}"


def describe_schedule_status(data: BriefData) -> str:
    planned = data.block or "block not extracted"
    if not data.block_variance:
        return f"Planned flight time for this leg is {planned}; on-time status not extracted."
    variance = data.block_variance
    direction = variance[-1].upper()
    amount = variance[:-1]
    if direction == "E":
        return f"Planned flight time for this leg is {planned}; planned {amount} early."
    if direction == "L":
        return f"Planned flight time for this leg is {planned}; planned {amount} late."
    return f"Planned flight time for this leg is {planned}; schedule variance {variance}."


def weather_code_to_words(code: str) -> str:
    code = code.upper()
    mapping = {
        "FEW": "few clouds",
        "SCT": "scattered clouds",
        "BKN": "broken clouds",
        "OVC": "overcast",
        "CLR": "clear",
        "SKC": "clear",
    }
    return mapping.get(code, code.lower())


def c_to_f(celsius: int) -> int:
    return round((celsius * 9 / 5) + 32)


def extract_destination_weather_summary(text: str, dest_code: str) -> str:
    taf = first_taf_block(text, dest_code)
    metar = first_metar_block(text, dest_code)
    source = taf or metar
    if not source:
        return "Destination weather near arrival not extracted."

    vis = ""
    vis_match = re.search(r"\b(P?\d+(?:/\d+)?SM)\b", source.upper())
    if vis_match:
        vis = f"{vis_match.group(1)} visibility"
    else:
        meter_matches = [int(item) for item in re.findall(r"\b(\d{4})\b", source) if int(item) <= 9999]
        if meter_matches:
            vis_meters = meter_matches[-1]
            vis_sm = round(vis_meters / 1609.34, 1)
            vis = f"{vis_meters}m ({vis_sm}SM) visibility"

    clouds = ""
    cloud_match = re.search(r"\b(FEW|SCT|BKN|OVC|CLR|SKC)(\d{3})?\b", source.upper())
    if cloud_match:
        layer = weather_code_to_words(cloud_match.group(1))
        if cloud_match.group(2):
            clouds = f"{layer} at {cloud_match.group(2)}00 ft"
        else:
            clouds = layer

    temp = ""
    temp_match = re.search(r"\b(M?\d{2})/(M?\d{2})\b", metar.upper() if metar else "")
    if temp_match:
        c_text = temp_match.group(1)
        c_value = -int(c_text[1:]) if c_text.startswith("M") else int(c_text)
        temp = f"{c_value}C / {c_to_f(c_value)}F"

    parts = [part for part in [clouds, vis, temp] if part]
    if not parts:
        return "Destination weather near arrival not extracted."
    return ", ".join(parts) + "."


def extract_airport_forecast_reminder(text: str, code: str) -> str:
    taf = first_taf_block(text, code)
    metar = first_metar_block(text, code)
    source = taf or metar
    if not source:
        return f"{code}: forecast not extracted"

    source_upper = source.upper()
    wind = match_group(r"\b((?:\d{3}|VRB)\d{2,3}(?:G\d{2,3})?KT)\b", source_upper)
    vis = match_group(r"\b(P?\d+(?:/\d+)?SM)\b", source_upper)
    wx_tokens = re.findall(r"\b(?:VC)?[-+]?(?:TS|SH|RA|DZ|SN|FG|BR|HZ|FU|DU|SA|SQ|FC|PL|GR|GS|UP)+\b", source_upper)
    clouds = re.findall(r"\b(?:FEW|SCT|BKN|OVC|CLR|SKC)\d{0,3}\b", source_upper)

    parts = [wind, vis] + wx_tokens[:2] + clouds[:2]
    if not parts:
        summary = extract_destination_weather_summary(text, code)
        if "not extracted" not in summary:
            return f"{code}: {summary.rstrip('.')}"
        return f"{code}: forecast available in release"
    return f"{code}: {' '.join(parts)}"


def has_gusty_winds(text: str, code: str) -> bool:
    block = " ".join(filter(None, [first_metar_block(text, code), first_taf_block(text, code)]))
    return bool(re.search(r"\b\d{3}\d{2,3}G\d{2,3}KT\b", block.upper()))


def airport_notam_snippets(text: str, airport: str, limit: int = 4) -> list[str]:
    snippets: list[str] = []
    for line in text.splitlines():
        clean = normalize_space(line)
        if not clean:
            continue
        upper = clean.upper()
        if airport in upper and (
            "CLSD" in upper
            or "U/S" in upper
            or "HAZARD" in upper
            or "CRANE" in upper
            or "WORK" in upper
            or "RFFS" in upper
            or "NO REROUTING" in upper
        ):
            snippets.append(clean)
        if len(snippets) >= limit:
            break
    return snippets


def aerodrome_notam_block(text: str, airport: str) -> str:
    if not airport:
        return ""
    code = airport.upper()
    match = re.search(
        rf"AERODROME INFORMATION\s+{re.escape(code)}\s*/\s*[A-Z]{{3}}\s+(.*?)(?=\n\s*AERODROME INFORMATION\s+[A-Z]{{4}}\s*/|\n\s*Official Pilot Briefing|\n\s*FIR/UIR INFORMATION|\Z)",
        text,
        re.S | re.I,
    )
    return match.group(1) if match else ""


def operational_notam_snippets(text: str, airport: str, limit: int = 10) -> list[str]:
    block = aerodrome_notam_block(text, airport)
    if not block:
        return []
    snippets: list[str] = []
    keywords = (
        "RWY",
        "TWY",
        "TAXIWAY",
        "NAV",
        "ILS",
        "PAPI",
        "ALS",
        "CLSD",
        "U/S",
        "CONST",
        "AIRSPACE",
        "SMALL ARMS",
        "ADS-B",
        "TIS-B",
        "FIS-B",
        "BEACON",
        "TFR",
    )
    skip_patterns = (
        r"^\*?[A-Z]{4}[A-Z]\d{4}/\d{2}$",
        r"^\(\d{2}\s+[A-Z]{3}\s+\d{2}\)$",
        r"^\*\d{2}\s+[A-Z]{3}\s+\d{4}",
        r"^UAL\s+\d+/\d+",
    )
    pending = ""
    for raw_line in block.splitlines():
        clean = normalize_space(raw_line).replace("E)", "").replace("D)", "")
        if not clean:
            continue
        if "===== PAGE" in clean:
            continue
        if any(re.search(pattern, clean) for pattern in skip_patterns):
            continue
        upper = clean.upper()
        if any(keyword in upper for keyword in keywords):
            pending = clean
            snippets.append(pending)
            pending = ""
        elif snippets and len(clean) < 90 and not re.match(r"^[A-Z0-9]{4,}/\d+", clean):
            previous = snippets[-1]
            if len(previous) < 130 and not previous.endswith("."):
                snippets[-1] = normalize_space(previous + " " + clean)
        if len(snippets) >= limit:
            break
    return dedupe_preserve_order(snippets)


def load_airport_reference_notes(airport: str, phase: str) -> list[str]:
    if not airport or not AIRPORT_NOTES_PATH.exists():
        return []
    try:
        payload = json.loads(AIRPORT_NOTES_PATH.read_text())
    except Exception:
        return []
    airport_entry = payload.get(str(airport).upper(), {})
    notes = airport_entry.get(phase, [])
    return [normalize_space(item) for item in notes if normalize_space(item)]


def load_flight_overrides(flight: str, date_code: str) -> dict[str, str]:
    if not FLIGHT_OVERRIDES_PATH.exists():
        return {}
    try:
        payload = json.loads(FLIGHT_OVERRIDES_PATH.read_text())
    except Exception:
        return {}
    key = f"{flight.replace(' ', '').upper()}_{date_code.upper()}"
    return payload.get(key, {})


def parse_aircraft_notes(text: str) -> list[str]:
    notes: list[str] = []
    mel_line = match_group(r"MEL ITEM INFO\s+([^\n]+)", text)
    if mel_line:
        notes.append(mel_line)
    if re.search(r"MEL/CDL ITEMS-+\s+NONE", text):
        notes.append("No active MEL/CDL items")
    if re.search(r"DISPATCH ITEMS-+\s+NONE", text):
        notes.append("No active dispatch items")
    return notes


def classify_weather_threats(text: str, dep: str, dest: str, alt: str) -> list[str]:
    threats: list[str] = []
    dest_taf = first_taf_block(text, dest)
    alt_taf = first_taf_block(text, alt) if alt else ""
    if dest_taf:
        upper = dest_taf.upper()
        if "TSRA" in upper or "CB" in upper or "VCTS" in upper:
            threats.append(f"{dest}: convective signal in TAF")
        if re.search(r"\b(5000|4000|3000|2000|2SM|1SM)\b", upper) or "BKN013" in upper or "BKN010" in upper:
            threats.append(f"{dest}: lower ceiling/visibility window near arrival")
    if alt_taf:
        upper = alt_taf.upper()
        if "-SHRA" in upper or "SHRA" in upper or "TSRA" in upper:
            threats.append(f"{alt}: alternate also carries showers/weather")
    dep_metar = first_metar_block(text, dep)
    if dep_metar and ("BR" in dep_metar.upper() or "RA" in dep_metar.upper()):
        threats.append(f"{dep}: weather may add departure workload")
    return threats


def parse_brief(pdf_path: Path) -> BriefData:
    text = extract_text(pdf_path)
    data = BriefData()
    data.raw_text = text
    data.flight = match_group(r"BRIEFING PACKAGE FOR FLIGHT\s*:\s*([A-Z]+\s*\d+)", text)
    data.date_code = match_group(r"BRIEFING PACKAGE FOR FLIGHT\s*:\s*[A-Z]+\s*\d+\s+(\d{2}[A-Z]{3}\d{2})", text)
    data.route = match_group(r"BRIEFING PACKAGE FOR FLIGHT\s*:\s*[A-Z]+\s*\d+\s+\d{2}[A-Z]{3}\d{2}\s+([A-Z]{3}-[A-Z]{3})", text)
    if data.route and "-" in data.route:
        data.departure, data.destination = data.route.split("-", 1)
    data.departure_runway = parse_departure_runway(text)
    data.departure_sid = parse_departure_sid(text)
    data.arrival_runway, data.arrival_star = parse_arrival_runway_and_star(text)
    data.dispatch_sector = parse_dispatch_sector(text)
    data.departure_icao = match_group(r"UAL\d+-\d+\s+\d{2}[A-Z]{3}\d{2}\s+([A-Z]{4})/[A-Z]{3}", text)
    data.aircraft_reg = match_group(r"ACFT\s+([A-Z0-9]+)", text)
    data.destination_icao = match_group(rf"{re.escape(data.aircraft_reg)}/\d+\s+[A-Z0-9]{{3,4}}\s+([A-Z]{{4}})/[A-Z]{{3}}", text)
    if not data.destination_icao:
        data.destination_icao = match_group(r"[A-Z0-9]+/\d+\s+[A-Z0-9]{3,4}\s+([A-Z]{4})/[A-Z]{3}", text)
    data.release_number = match_group(r"RELEASE NUMBER\s*:\s*(\d+)", text)
    data.out_time = match_group(r"OUT\s+(\d{4}Z)\s*/\s*\d{4}L", text)
    data.out_local_time = match_group(r"OUT\s+\d{4}Z\s*/\s*(\d{4}L)", text)
    data.eta = match_group(r"ON\s+(\d{4}Z)\s*/\s*\d{4}L", text)
    data.eta_local_time = match_group(r"IN\s+\d{4}Z\s*/\s*(\d{4}L)", text)
    data.block = match_group(r"\n([0-9]{1,2}:[0-9]{2})/[0-9]{1,2}:[0-9]{2}(?:[EW])?\s+V(?:RNT|RT)", text)
    data.block_variance = parse_block_variance(text)
    data.aircraft_type = match_group(rf"{re.escape(data.aircraft_reg)}/\d+\s+([A-Z0-9]{{3,4}})\s+{data.destination_icao or '[A-Z]{4}'}/[A-Z]{{3}}", text) or match_group(
        r"UAL\d+/\d{2}[A-Z]{3}\s+ACFT\s+[A-Z0-9]+\s+[A-Z]{3}-[A-Z]{3}\s*\n.*?\n.*?([A-Z0-9]{3,4})",
        text,
        flags=re.S,
    )
    data.etops_minutes = match_group(r"(\d+\s+MINUTE ETOPS)", text)
    intended_dest_block = re.search(r"INTENDED DEST\s+(.*?)\s+MIN T/O", text, re.S)
    if intended_dest_block:
        airport_matches = re.findall(r"\b([A-Z]{4})/[A-Z]{3}\b", intended_dest_block.group(1))
        if len(airport_matches) >= 2:
            data.dispatch_alternate = airport_matches[1]
        elif airport_matches:
            data.dispatch_alternate = airport_matches[0]
    data.takeoff_fuel = format_fuel_int(match_group(r"PLAN T/O\s+[0-9:]+\s+(\d+)", text))
    data.minimum_takeoff_fuel = format_fuel_int(match_group(r"MIN T/O\s+[0-9:]+\s+(\d+)", text))
    data.landing_fuel = format_fuel_int(match_group(r"REMF\s+[0-9:]+\s+(\d+)", text))
    data.extra_fuel = format_fuel_int(match_group(r"EXTRA\s+[0-9:]+\s+(\d+)", text))
    data.taxi_fuel = format_fuel_int(match_group(r"TAXI\s+[0-9:]+\s+(\d+)", text))
    data.zfw_actual, data.zfw_limit = parse_weight_pair(text, "ZFW")
    data.tow_actual, data.tow_limit = parse_weight_pair(text, "TOW")
    data.lw_actual, data.lw_limit = parse_weight_pair(text, "LW")
    far_fuel = match_group(r"FAR\s+[0-9:]+\s+(\d+)", text)
    alternate_fuel = match_group(rf"{re.escape(data.dispatch_alternate)}/[A-Z]{{3}}\s+[0-9:]+\s+(\d+)", text) if data.dispatch_alternate else ""
    if far_fuel and alternate_fuel:
        data.far_reserve_fuel = format_fuel_int(str(int(far_fuel) + int(alternate_fuel)))
    if data.landing_fuel:
        try:
            data.conservative_fuel = format_fuel_float(float(data.landing_fuel) - 2.0)
        except ValueError:
            data.conservative_fuel = ""
    data.step_climbs = parse_step_climbs(text)
    data.etops_airports, data.etops_cp_details = parse_etops_airports_and_details(text)
    data.route_alternates = parse_route_alternates(text)
    data.dispatcher_remarks = parse_dispatcher_remarks(text)
    data.etops_summary = parse_etops_summary(text)
    data.departure_notes = dedupe_preserve_order(
        operational_notam_snippets(text, data.departure_icao or data.departure)
        + airport_notam_snippets(text, data.departure_icao or data.departure)
        + load_airport_reference_notes(data.departure_icao or data.departure, "departure_notes")
    )
    data.destination_notes = dedupe_preserve_order(
        operational_notam_snippets(text, data.destination_icao or data.destination)
        + airport_notam_snippets(text, data.destination_icao or data.destination)
        + load_airport_reference_notes(data.destination_icao or data.destination, "arrival_notes")
    )
    data.alternate_notes = dedupe_preserve_order(
        airport_notam_snippets(text, data.dispatch_alternate)
        + load_airport_reference_notes(data.dispatch_alternate, "arrival_notes")
    )
    data.aircraft_notes = parse_aircraft_notes(text)
    data.weather_threats = classify_weather_threats(
        text,
        data.departure_icao or data.departure,
        data.destination_icao or data.destination,
        data.dispatch_alternate,
    )
    data.departure_gusty = has_gusty_winds(text, data.departure_icao or data.departure)
    data.arrival_gusty = has_gusty_winds(text, data.destination_icao or data.destination)
    overrides = load_flight_overrides(data.flight, data.date_code)
    if overrides.get("departure_sid"):
        data.departure_sid = overrides["departure_sid"]
    if overrides.get("arrival_star"):
        data.arrival_star = overrides["arrival_star"]
    if overrides.get("arrival_runway"):
        data.arrival_runway = overrides["arrival_runway"]
    if overrides.get("departure_runway"):
        data.departure_runway = overrides["departure_runway"]

    threats = parse_threats_from_remarks(data.dispatcher_remarks)
    threats.extend(data.weather_threats)
    threats.extend(classify_weight_threats(data))
    if is_sfo_departure(data):
        threats.append(SFO_ENGINE_OUT_PROCEDURE)
    if any("RFFS" in note.upper() for note in data.alternate_notes):
        threats.append(f"{data.dispatch_alternate}: confirm ETOPS alternate suitability against RFFS hours")
    data.threats = dedupe_preserve_order(threats)
    data.fa_discussion_points = build_fa_discussion_points(data)
    data.captain_discussion_points = build_captain_discussion_points(data)
    data.pilot_flying_points = build_pilot_flying_points(data)
    data.arrival_brief_points = build_arrival_brief_points(data)
    data.passenger_pa_notes = build_passenger_pa_notes(data, text)
    return data


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def join_known(values: list[str]) -> str:
    return ", ".join([value for value in values if value]) or "None listed"


def summarize_aircraft_status(data: BriefData) -> str:
    active_lines = [item for item in data.aircraft_notes if item.upper().startswith("NO ACTIVE")]
    if active_lines:
        return ", ".join(active_lines)
    return join_known(data.aircraft_notes)


def is_sfo_departure(data: BriefData) -> bool:
    return (data.departure_icao or "").upper() == "KSFO" or (data.departure or "").upper() == "SFO"


def build_fa_discussion_points(data: BriefData) -> list[str]:
    flight_time = f"{data.block} block" if data.block else "Block not extracted"
    weather = data.weather_threats[0] if data.weather_threats else "Arrival weather is the main watch item"
    turbulence = next((item for item in data.threats if "TURB" in item.upper()), "No turbulence note auto-detected")
    ride_timing = extract_turbulence_timing(data)
    cabin_items = summarize_aircraft_status(data)
    time_to_landing = data.eta_local_time or data.eta or "ETA not extracted"

    return [
        "Threat Lens: personal, environmental, technical; verify any riders onboard.",
        f"Time / Arrival: {flight_time}; planned arrival {time_to_landing}.",
        f"Ride Timing: {ride_timing}",
        f"Weather / Ride: {weather}; {turbulence}.",
        f"Cabin / Overwater: {cabin_items}; ETOPS overwater segment, life vests apply.",
        f"T.E.S.T.: Type of Emergency, Evacuation, Special Instructions, Time To Landing {time_to_landing}.",
        f"Dispatch Sector: {data.dispatch_sector or 'not shown in release; verify with dispatch if needed'}.",
    ]


def build_captain_discussion_points(data: BriefData) -> list[str]:
    dep_airport = data.departure_icao or data.departure or "departure airport"
    taxi_plan = data.departure_notes[0] if data.departure_notes else f"Review current {dep_airport} taxi routing and closure impact"
    mx_status = summarize_aircraft_status(data)
    air_return = (
        f"{data.dispatch_alternate}: dispatch alternate listed; verify takeoff alternate / air return suitability."
        if data.dispatch_alternate
        else "No alternate extracted; verify takeoff alternate / air return plan."
    )

    items = [
        f"Threat Lens / Duties: personal, environmental, technical; confirm PF/PM and relief roles; release {data.release_number or 'N/A'} for {data.flight or 'flight'} {data.route or ''}.".strip(),
        f"Fuel / Alternate: Min T/O {data.minimum_takeoff_fuel or 'N/A'}, T/O {data.takeoff_fuel or 'N/A'}, LDG {data.landing_fuel or 'N/A'}, FAR {data.far_reserve_fuel or 'N/A'}, conservative {data.conservative_fuel or 'N/A'}, alt {data.dispatch_alternate or 'N/A'}.",
        f"Departure / Arrival Procedures: RWY {data.departure_runway or 'verify'} SID {data.departure_sid or 'verify'} | STAR {data.arrival_star or 'not shown'} RWY {data.arrival_runway or 'verify with ATIS'}.",
        f"Mx / Pubs: {mx_status}; verify EFB, PEDs, and current pubs.",
        f"Taxi / NOTAMs: {taxi_plan}.",
        f"RTO / Return: {FM_RTO} {air_return}",
    ]
    if is_sfo_departure(data):
        items.append(f"Engine-Out: {SFO_ENGINE_OUT_PROCEDURE}")
    return items


def build_pilot_flying_points(data: BriefData) -> list[str]:
    turbulence = next((item for item in data.threats if "TURB" in item.upper()), "No turbulence note auto-detected")
    first_step = data.step_climbs[0] if data.step_climbs else "No step climb extracted"
    dep_airport = data.departure_icao or data.departure or "departure airport"
    taxi_plan = data.departure_notes[0] if data.departure_notes else f"Review current {dep_airport} taxi routing, hotspots, and closure impact"
    convective = next((item for item in data.threats if "CB" in item.upper() or "TS" in item.upper()), "No convective note auto-detected")
    items = [
        "Threat Lens: personal, environmental, technical.",
        f"Departure / Taxi: {taxi_plan}.",
        f"Weather / Climb: {turbulence}; {convective}.",
        f"T/O Data / Profile: verify planned takeoff data, takeoff and engine-failure profile; first step {first_step}.",
        "Terrain / Automation: review terrain, obstacles, automation mode plan, transition altitude, and upset response.",
    ]
    if data.departure_gusty:
        items.append(f"Windshear Escape: {FM_WINDSHEAR_ESCAPE}")
    if is_sfo_departure(data):
        items.insert(2, f"Engine-Out Procedure: {SFO_ENGINE_OUT_PROCEDURE}")
    return items


def build_arrival_brief_points(data: BriefData) -> list[str]:
    weather = data.weather_threats[0] if data.weather_threats else "No arrival weather threat auto-detected"
    dest_note = data.destination_notes[0] if data.destination_notes else "Review destination runway, taxi, and field condition NOTAMs"
    alt_note = (
        data.alternate_notes[0]
        if data.alternate_notes
        else f"Alternate {data.dispatch_alternate}: no specific note extracted" if data.dispatch_alternate else "No alternate extracted"
    )
    items = [
        "Threat Lens: personal, environmental, technical.",
        f"ATIS / Weather / Fuel: {weather}; planned landing {data.landing_fuel or 'N/A'}.",
        f"Arrival / Runway / Taxi: STAR {data.arrival_star or 'not shown'}; runway {data.arrival_runway or 'verify with ATIS'}; {dest_note}.",
        f"Stabilized Approach: {FM_STABILIZED_APPROACH}",
        f"Alternate / Go-Around: {alt_note}. {FM_GO_AROUND}",
        "Approach Setup: review FMC programming, transition level, terrain, windshear/PWS, and landing performance.",
    ]
    if data.arrival_gusty:
        items.append(f"Windshear Escape: {FM_WINDSHEAR_ESCAPE}")
    return items


def build_passenger_pa_notes(data: BriefData, text: str) -> list[str]:
    turbulence = next((item for item in data.threats if "TURB" in item.upper()), "")
    ride = "Ride should be mostly smooth."
    if turbulence:
        ride = f"Expect possible {turbulence.lower()}."
    gate = data.arrival_gate or "Arrival gate not shown in the release."
    weather = extract_destination_weather_summary(text, data.destination_icao or data.destination)
    return [
        describe_schedule_status(data),
        ride,
        f"Arrival gate: {gate}",
        f"Destination weather near arrival: {weather}",
    ]


def build_pdf_etops_items(data: BriefData) -> list[str]:
    items: list[str] = []
    summary_by_cp: dict[str, str] = {}
    for item in data.etops_summary:
        cp = item.split(":", 1)[0]
        summary_by_cp[cp] = item.split(":", 1)[1].strip() if ":" in item else item

    for detail in data.etops_cp_details:
        cp = detail.split(":", 1)[0]
        summary = summary_by_cp.get(cp)
        if summary:
            items.append(f"{cp}: {summary} | {detail.split(':', 1)[1].strip()}")
        else:
            items.append(detail)

    if data.step_climbs:
        compact_steps = []
        for step in data.step_climbs:
            compact_steps.append(step.replace(" around ", " ").replace(" EET", ""))
        items.append("Step climbs: " + ", ".join(compact_steps))

    return items or ["No ETOPS detail auto-extracted"]


def build_operational_timeline_rows(data: BriefData) -> list[list[str]]:
    rows = [["EET", "Event", "Fuel / Alt", "Action / Note"]]
    rows.append(
        [
            "0:00",
            f"{data.departure_icao or data.departure or 'DEP'} OFF / {data.departure_sid or 'SID'}",
            f"T/O {data.takeoff_fuel or '--'}",
            "FMS position verified; RVSM altimeter check after takeoff.",
        ]
    )
    if data.step_climbs:
        rows.append(["0:30", "Step climb", data.step_climbs[0].split()[0], data.step_climbs[0]])

    entry_match = re.search(
        r"ETOPS ENTRY\s+([A-Z]{4})\s+([0-9:]+)\s+(\d+)NM\s+([NS]\d{4}\.\d\s+[WE]\d{5}\.\d)\s+(\d+)",
        data.raw_text,
    )
    if entry_match:
        airport, eet, distance, coords, fuel = entry_match.groups()
        rows.append([eet, f"ETOPS Entry {airport}", f"{format_fuel_int(fuel)} / {distance}NM", coords])

    route_actions = {
        "CEBEN": "Established enroute before ETOPS entry.",
        "CIVIT": "Ride window begins; Class II / oceanic checks and HF/SELCAL as required.",
        "CORTT": "Ride window ends near this segment; compare actual ride to dispatcher remarks.",
        "CUNDU": "Continue CP fuel tracking and diversion awareness.",
        "CREAN": "Approaching arrival environment; begin descent/arrival setup flow.",
        "HUNTS": "Arrival/descent segment.",
        "PASIF": "Arrival/descent segment.",
        "PIRAT": f"STAR {data.arrival_star or 'arrival'} / runway {data.arrival_runway or 'verify'}.",
        "BRINY": "Final arrival path / prepare landing configuration.",
    }
    route_rows: list[list[str]] = []
    for fix, action in route_actions.items():
        match = re.search(
            rf"(?:R\d+[A-Z]?\s+)?{fix}\s*\n[NS]\d{{4}}\.\d\s+[WE]\d{{5}}\.\d.*?\n\s*(\d:\d{{2}})\s*\n\s*\d+\s+([A-Z0-9]{{3,4}})",
            data.raw_text,
            re.S,
        )
        if match:
            eet, altitude = match.groups()
            route_rows.append([eet, fix, altitude, action])

    cp_matches = re.findall(
        r"(CP-\d)\s+([A-Z]{4})-(\d+)/([A-Z]{4})-(\d+)\s+([0-9:]+)\s+([NS]\d{4}\.\d\s+[WE]\d{5}\.\d)\s+(\d+)/(\d+)",
        data.raw_text,
    )
    for cp, airport_a, dist_a, airport_b, dist_b, eet, coords, remaining, required in cp_matches:
        weather_reminder = "; ".join(
            extract_airport_forecast_reminder(data.raw_text, airport)
            for airport in [airport_a, airport_b]
        )
        route_rows.append(
            [
                eet,
                f"{cp} {airport_a}/{airport_b}",
                f"Rem {format_fuel_int(remaining)} / Req {format_fuel_int(required)}",
                f"{coords}; {airport_a} {dist_a}NM / {airport_b} {dist_b}NM. Forecast: {weather_reminder}.",
            ]
        )

    exit_match = re.search(
        r"ETOPS EXIT\s+([A-Z]{4})\s+([0-9:]+)\s+(\d+)NM\s+([NS]\d{4}\.\d\s+[WE]\d{5}\.\d)\s+(\d+)",
        data.raw_text,
    )
    if exit_match:
        airport, eet, distance, coords, fuel = exit_match.groups()
        route_rows.append([eet, f"ETOPS Exit {airport}", f"{format_fuel_int(fuel)} / {distance}NM", coords])

    def eet_minutes(row: list[str]) -> int:
        match = re.match(r"(\d+):(\d{2})", row[0])
        if not match:
            return 9999
        return int(match.group(1)) * 60 + int(match.group(2))

    for row in sorted(route_rows, key=eet_minutes):
        if row not in rows:
            rows.append(row)

    if data.eta or data.eta_local_time:
        rows.append(
            [
                data.block or "END",
                f"{data.destination_icao or data.destination or 'DEST'} IN",
                f"LDG {data.landing_fuel or '--'}",
                f"ETA {data.eta or '--'} / {data.eta_local_time or '--'}; alternate {data.dispatch_alternate or '--'}.",
            ]
        )
    return [rows[0]] + sorted(rows[1:], key=eet_minutes)


def review_seed(data: BriefData) -> int:
    seed_text = "|".join(
        [
            data.flight,
            data.date_code,
            data.route,
            data.release_number,
            data.aircraft_reg,
            data.source_pdf_path,
        ]
    )
    digest = hashlib.sha256(seed_text.encode("utf-8", errors="ignore")).hexdigest()
    return int(digest[:12], 16)


def review_system_index(data: BriefData) -> int:
    flight_number = match_group(r"(\d+)", data.flight)
    release_number = match_group(r"(\d+)", data.release_number)
    day = match_group(r"^(\d{1,2})", data.date_code)
    month_text = match_group(r"\d{1,2}([A-Z]{3})", data.date_code.upper())
    month_index = {
        "JAN": 1,
        "FEB": 2,
        "MAR": 3,
        "APR": 4,
        "MAY": 5,
        "JUN": 6,
        "JUL": 7,
        "AUG": 8,
        "SEP": 9,
        "OCT": 10,
        "NOV": 11,
        "DEC": 12,
    }.get(month_text, 0)
    numeric_seed = int(flight_number or 0) + int(release_number or 0) + int(day or 0) + month_index
    if data.trip_id:
        numeric_seed += sum(ord(char) for char in data.trip_id)
    if numeric_seed:
        return numeric_seed % len(SYSTEM_REVIEW_BANK)
    return review_seed(data) % len(SYSTEM_REVIEW_BANK)


def rotate_list(items: list[str], seed: int, limit: int) -> list[str]:
    if not items:
        return []
    start = seed % len(items)
    ordered = items[start:] + items[:start]
    return ordered[:limit]


def format_review_item(template: str, data: BriefData) -> str:
    values = {
        "landing_fuel": f"{data.landing_fuel or '--'} lb",
        "far_reserve_fuel": f"{data.far_reserve_fuel or '--'} lb",
        "conservative_fuel": f"{data.conservative_fuel or '--'} lb",
        "step_climbs": ", ".join(data.step_climbs) if data.step_climbs else "verify planned cruise and step climbs",
        "route": data.route or "--",
        "alternate": data.dispatch_alternate or "--",
    }
    return template.format(**values)


def build_rotating_review(data: BriefData) -> tuple[str, int, int, list[str], list[str]]:
    seed = review_seed(data)
    system_index = review_system_index(data)
    system = SYSTEM_REVIEW_BANK[system_index]
    system_items = [format_review_item(item, data) for item in system["items"]]
    memory_items = rotate_list(MEMORY_REVIEW_BANK, seed // max(1, len(SYSTEM_REVIEW_BANK)), 4)
    return str(system["title"]), system_index + 1, len(SYSTEM_REVIEW_BANK), system_items[:4], memory_items


def parse_csv_people(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def compact_flight(value: str) -> str:
    compact = value.replace("UAL", "UA").replace(" ", "").upper()
    match = re.search(r"UA(\d{1,4})", compact)
    return f"UA{match.group(1)}" if match else compact


def parse_pairing_crew(pairing_pdf: Path, flight: str, pairing_id: str = "") -> tuple[str, list[dict[str, str]]]:
    if not pairing_pdf or not pairing_pdf.exists():
        return "", []
    try:
        text = extract_text(pairing_pdf)
    except Exception:
        return "", []

    flight_key = compact_flight(flight)
    section_match = re.search(
        rf"\b{re.escape(flight_key)}\s*[•\-]\s*(\d{{2}}[A-Za-z]{{3}})(.*?)(?=\nUA\d+\s*[•\-]|\nDuty\s+\d+\s*[•\-]|\nBlock DH|\nTrip Summary|\Z)",
        text,
        re.S,
    )
    if not section_match:
        return "", []

    duty_date = section_match.group(1)
    block = section_match.group(2)
    members: list[dict[str, str]] = []
    for name, employee_id, role, source_pairing, source_date in re.findall(
        r"([A-Za-z][A-Za-z .'\-]+?)\s+\((U\d{6})\)\s+([A-Z]{2}\d{2})\s*\|\s*([A-Z0-9]+)\s*-\s*(\d{2}[A-Za-z]{3})",
        block,
    ):
        clean_name = normalize_space(name)
        if pairing_id and role in {"CA01", "FO01"} and source_pairing.upper() != pairing_id.upper():
            continue
        members.append(
            {
                "name": clean_name,
                "employee_id": employee_id,
                "role": role,
                "source_pairing": source_pairing,
                "source_date": source_date,
            }
        )
    return duty_date, dedupe_crew_members(members)


def parse_pairing_times(pairing_pdf: Path, flight: str) -> tuple[str, str]:
    if not pairing_pdf or not pairing_pdf.exists():
        return "", ""
    try:
        text = extract_text(pairing_pdf)
    except Exception:
        return "", ""

    flight_key = compact_flight(flight)
    flight_match = re.search(rf"\b{re.escape(flight_key)}\b\s*[•\-]", text, re.I)
    if not flight_match:
        return "", ""
    before_flight = text[max(0, flight_match.start() - 1800) : flight_match.start()]
    report_matches = list(
        re.finditer(
            r"Duty\s+\d+\s*[•\-]\s*\d{2}[A-Za-z]{3}\s+Report:\s*([A-Z]?\d{1,2}:\d{2})",
            before_flight,
            re.I,
        )
    )
    if not report_matches:
        return "", ""
    report_match = report_matches[-1]
    pickup_matches = list(
        re.finditer(
            r"Pickup\s+time\s+is\s+([A-Z]?\d{1,2}:\d{2})",
            before_flight[: report_match.start()],
            re.I,
        )
    )
    pickup = ""
    if pickup_matches:
        pickup = pickup_matches[-1].group(1)
    return pickup, report_match.group(1)


def dedupe_crew_members(members: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for member in members:
        key = (member.get("employee_id", ""), member.get("role", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(member)
    return result


def parse_seniority_row(line: str) -> dict[str, str] | None:
    clean = normalize_space(line)
    match = re.match(
        r"^(U\d{6})\s+(.+?)\s+(\d+)\s+(?:(LTA|SUP)\s+)?([A-Z]{3}\d{3}[A-Z]{2})\s+([A-Z]{3}\d{3}[A-Z]{2})\s+(\d+)\s+([0-9.]+)\s+([A-Z]+)",
        clean,
    )
    if not match:
        return None
    return {
        "employee_id": match.group(1),
        "category_name": match.group(2),
        "system_seniority": match.group(3),
        "status": match.group(4) or "",
        "starting_position": match.group(5),
        "awarded_position": match.group(6),
        "base_seniority": match.group(7),
        "base_percentage": match.group(8),
        "awardee_type": match.group(9),
    }


def load_seniority_by_employee_ids(employee_ids: set[str]) -> dict[str, dict[str, str]]:
    if not employee_ids or not SENIORITY_SOURCE_PDF.exists():
        return {}
    found: dict[str, dict[str, str]] = {}
    try:
        reader = PdfReader(str(SENIORITY_SOURCE_PDF))
        for page in reader.pages:
            for line in (page.extract_text() or "").splitlines():
                clean = normalize_space(line)
                if not clean.startswith("U"):
                    continue
                employee_id = clean.split(" ", 1)[0]
                if employee_id not in employee_ids:
                    continue
                row = parse_seniority_row(clean)
                if row:
                    found[employee_id] = row
                if employee_ids.issubset(found.keys()):
                    return found
    except Exception:
        return found
    return found


def load_seniority_cache_by_employee_ids(employee_ids: set[str]) -> dict[str, dict[str, str]]:
    if not employee_ids or not SENIORITY_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(SENIORITY_CACHE_PATH.read_text())
    except Exception:
        return {}
    employees = payload.get("employees", {})
    return {
        employee_id: employees[employee_id]
        for employee_id in employee_ids
        if employee_id in employees
    }


def parse_integrated_seniority_row(line: str) -> dict[str, str] | None:
    clean = normalize_space(line)
    match = re.match(
        r"^(UA|CO)\s+(.+?)\s+0?(\d{5,6})\s+(\d{1,2}/\d{1,2}/\d{2})\s+(\d{1,2}/\d{1,2}/\d{2})\s+([A-Z]{3})\s+([0-9A-Z]{3})\s+([A-Z]{2})\s+(\d+)\s+([0-9.]+)%\s+(\d+)\s+([0-9.]+)%",
        clean,
    )
    if not match:
        return None
    return {
        "legacy_source": match.group(1),
        "integrated_name": match.group(2),
        "employee_number_legacy": match.group(3),
        "date_of_birth": match.group(4),
        "date_of_hire": match.group(5),
        "isl_base": match.group(6),
        "isl_equipment": match.group(7),
        "isl_status": match.group(8),
        "pre_merger_seniority": match.group(9),
        "pre_merger_percentage": match.group(10),
        "integrated_seniority": match.group(11),
        "integrated_percentage": match.group(12),
    }


def load_integrated_seniority_by_employee_ids(employee_ids: set[str]) -> dict[str, dict[str, str]]:
    if not employee_ids or not INTEGRATED_SENIORITY_SOURCE_PDF.exists():
        return {}
    employee_numbers = {employee_id.replace("U", "") for employee_id in employee_ids}
    found: dict[str, dict[str, str]] = {}
    try:
        reader = PdfReader(str(INTEGRATED_SENIORITY_SOURCE_PDF))
        for page in reader.pages:
            for line in (page.extract_text() or "").splitlines():
                clean = normalize_space(line)
                if not (clean.startswith("UA ") or clean.startswith("CO ")):
                    continue
                for employee_number in list(employee_numbers):
                    if re.search(rf"\b0?{re.escape(employee_number)}\b", clean):
                        row = parse_integrated_seniority_row(clean)
                        if row:
                            found[f"U{employee_number}"] = row
                        break
                if employee_ids.issubset(found.keys()):
                    return found
    except Exception:
        return found
    return found


def enrich_crew_with_seniority(members: list[dict[str, str]]) -> list[dict[str, str]]:
    employee_ids = {member["employee_id"] for member in members if member.get("employee_id")}
    cache = load_seniority_cache_by_employee_ids(employee_ids)
    use_cache_only = SENIORITY_CACHE_PATH.exists()
    missing_ids = set() if use_cache_only else employee_ids - set(cache)
    seniority = load_seniority_by_employee_ids(missing_ids)
    integrated = load_integrated_seniority_by_employee_ids(missing_ids)
    enriched: list[dict[str, str]] = []
    for member in members:
        employee_id = member.get("employee_id", "")
        row = cache.get(employee_id) or seniority.get(employee_id)
        isl_row = {} if cache.get(employee_id) else integrated.get(employee_id, {})
        enriched_member = dict(member)
        if row:
            enriched_member.update(row)
        if isl_row:
            enriched_member.update(isl_row)
        enriched.append(enriched_member)
    return enriched


def apply_pairing_crew(data: BriefData, pairing_pdf: Path) -> None:
    duty_date, members = parse_pairing_crew(pairing_pdf, data.flight, data.trip_id)
    pickup_time, report_time = parse_pairing_times(pairing_pdf, data.flight)
    if pickup_time and not data.pickup_time:
        data.pickup_time = pickup_time
    if report_time and not data.report_time:
        data.report_time = report_time
    if not members:
        return
    data.crew_members = enrich_crew_with_seniority(members)
    if not data.trip_id:
        for member in data.crew_members:
            if member.get("source_pairing"):
                data.trip_id = member["source_pairing"]
                break
    if duty_date and not data.pairing_note:
        data.pairing_note = f"Pairing crew extracted for {compact_flight(data.flight)} on {duty_date}"

    def names_for(prefix: str) -> list[str]:
        return [member["name"] for member in data.crew_members if member.get("role", "").startswith(prefix)]

    if not data.captain:
        data.captain = next(iter(names_for("CA")), "")
    if not data.first_officer:
        data.first_officer = next(iter(names_for("FO")), "")
    if not data.purser:
        data.purser = next(iter(names_for("FM")), "")
    if not data.flight_attendants:
        data.flight_attendants = names_for("FA")


def apply_trip_kit_notes(data: BriefData, trip_kit_pdf: Path) -> None:
    if not trip_kit_pdf or not trip_kit_pdf.exists():
        return
    try:
        trip_text = extract_trip_kit_relevant_text(
            trip_kit_pdf,
            [data.departure_icao or data.departure, data.destination_icao or data.destination],
        )
    except Exception:
        return
    if not trip_text:
        return
    dep_notes = operational_notam_snippets(trip_text, data.departure_icao or data.departure)
    dep_notes.extend(airport_10_7_notes(trip_text, data.departure_icao or data.departure))
    dest_notes = operational_notam_snippets(trip_text, data.destination_icao or data.destination)
    dest_notes.extend(airport_10_7_notes(trip_text, data.destination_icao or data.destination))
    if dep_notes:
        data.departure_notes = dedupe_preserve_order(dep_notes + data.departure_notes)
    if dest_notes:
        data.destination_notes = dedupe_preserve_order(dest_notes + data.destination_notes)


def refresh_derived_brief_points(data: BriefData) -> None:
    data.fa_discussion_points = build_fa_discussion_points(data)
    data.captain_discussion_points = build_captain_discussion_points(data)
    data.pilot_flying_points = build_pilot_flying_points(data)
    data.arrival_brief_points = build_arrival_brief_points(data)
    data.passenger_pa_notes = build_passenger_pa_notes(data, data.raw_text)


def airport_10_7_notes(text: str, airport: str, limit: int = 6) -> list[str]:
    if not airport:
        return []
    code = airport.upper()
    airport_tail = code[-3:]
    notes: list[str] = []
    operational_keywords = (
        "10-7",
        "TAXI",
        "RUNWAY",
        "RWY",
        "HOT SPOT",
        "HOTSPOT",
        "CONSTRUCTION",
        "RAMP",
        "GATE",
        "HOLD",
        "SPEED",
        "PARKING",
        "CLEARANCE",
    )
    pages = text.split(SECTION_BREAK)
    for page in pages:
        upper_page = page.upper()
        if "10-7" not in upper_page:
            continue
        if code not in upper_page and airport_tail not in upper_page:
            continue
        for raw_line in page.splitlines():
            clean = normalize_space(raw_line)
            upper = clean.upper()
            if len(clean) < 12 or len(clean) > 150:
                continue
            if any(keyword in upper for keyword in operational_keywords):
                notes.append(f"10-7: {clean}")
            if len(notes) >= limit:
                return dedupe_preserve_order(notes)
    return dedupe_preserve_order(notes)


def crew_seniority_note(member: dict[str, str]) -> str:
    if member.get("system_seniority") and member.get("base_seniority"):
        position = member.get("awarded_position") or member.get("starting_position") or ""
        note = (
            f"System {member['system_seniority']} / {position} base {member['base_seniority']} / "
            f"{member.get('base_percentage', '--')}%"
        )
        if member.get("date_of_hire") or member.get("legacy_source"):
            note += f" / DOH {member.get('date_of_hire', '--')} / Legacy {member.get('legacy_source', '--')}"
        return note
    if member.get("integrated_seniority"):
        return (
            f"ISL {member['integrated_seniority']} / {member.get('integrated_percentage', '--')}% / "
            f"DOH {member.get('date_of_hire', '--')} / Legacy {member.get('legacy_source', '--')}"
        )
    if member.get("employee_id"):
        return "Employee number from pairing; seniority not found in Category Summary or ISL"
    return "Pairing source"


def crew_table_rows(data: BriefData) -> list[list[str]]:
    rows = [["Position", "Name", "Emp #", "Role", "Seniority / Base"]]
    if data.crew_members:
        used_ids: set[str] = set()
        for role_prefix, position in [("CA", "CA"), ("FO", "FO"), ("FM", "FM")]:
            member = next((item for item in data.crew_members if item.get("role", "").startswith(role_prefix)), None)
            if not member:
                continue
            used_ids.add(member.get("employee_id", ""))
            rows.append(
                [
                    position,
                    member.get("name", "--"),
                    member.get("employee_id", "--"),
                    member.get("role", "--"),
                    crew_seniority_note(member) if role_prefix in {"CA", "FO"} else "Pairing source - cabin/flight manager seniority as available",
                ]
            )
        fa_members = [member for member in data.crew_members if member.get("role", "").startswith("FA")]
        if fa_members:
            rows.append(
                [
                    "FA",
                    "; ".join(member.get("name", "") for member in fa_members),
                    "see pairing",
                    "Cabin crew",
                    "Use pairing for names; cabin seniority when available from cabin sources",
                ]
            )
        return rows

    rows.extend(
        [
            ["CA", data.captain or "not entered", "--", "CA01", "No pairing crew extracted"],
            ["FO", data.first_officer or "not entered", "--", "FO01", "No pairing crew extracted"],
        ]
    )
    if data.iros:
        rows.append(["IRO", "; ".join(data.iros), "--", "Relief", "Manual entry"])
    if data.purser:
        rows.append(["FM", data.purser, "--", "FM01", "Manual entry"])
    if data.flight_attendants:
        rows.append(["FA", "; ".join(data.flight_attendants), "--", "Cabin crew", "Manual entry"])
    return rows


def render_text(data: BriefData) -> str:
    title = f"{data.flight.replace(' ', '')} Brief"
    subtitle = f"{data.route} {data.date_code}".strip()
    lines = [
        title,
        subtitle,
    ]
    if data.trip_id or data.pairing_note or data.captain or data.first_officer or data.iros or data.purser or data.flight_attendants:
        lines.extend(
            [
                "",
                "Trip / Crew",
            ]
        )
        if data.trip_id:
            lines.append(f"- Trip ID {data.trip_id}")
        if data.pairing_note:
            lines.append(f"- {data.pairing_note}")
        if data.captain:
            lines.append(f"- Captain {data.captain}")
        if data.first_officer:
            lines.append(f"- First Officer {data.first_officer}")
        if data.iros:
            lines.append(f"- IRO {', '.join(data.iros)}")
        if data.purser:
            lines.append(f"- Purser {data.purser}")
        if data.flight_attendants:
            lines.append(f"- Flight Attendants {', '.join(data.flight_attendants)}")
    lines.extend(
        [
            "",
            "Flight",
            f"- {data.flight} | {data.aircraft_type} {data.aircraft_reg} | Release {data.release_number}",
            f"- OUT {data.out_time} / {data.out_local_time} | ETA {data.eta} / {data.eta_local_time} | Block {data.block}",
            f"- Pickup {data.pickup_time or 'N/A'} | Report {data.report_time or 'N/A'}",
            f"- ETOPS {data.etops_minutes or 'N/A'} | Alt {data.dispatch_alternate or 'N/A'}",
            f"- Departure RWY {data.departure_runway or 'verify'} | SID {data.departure_sid or 'verify'} | STAR {data.arrival_star or 'not shown'} | Arrival RWY {data.arrival_runway or 'verify with ATIS'}",
            "",
            "Fuel",
            f"- Minimum Takeoff {data.minimum_takeoff_fuel or 'N/A'}",
            f"- Takeoff {data.takeoff_fuel or 'N/A'} | Landing {data.landing_fuel or 'N/A'}",
            f"- FAR Reserve {data.far_reserve_fuel or 'N/A'} | Conservative {data.conservative_fuel or 'N/A'}",
            f"- Taxi {data.taxi_fuel or 'N/A'}",
            "",
            "Top Threats",
        ]
    )
    if data.threats:
        lines.extend(f"- {item}" for item in data.threats)
    else:
        lines.append("- No high-risk threats auto-detected")

    if data.dispatcher_remarks:
        lines.extend(["", "Dispatcher Notes"])
        lines.extend(f"- {item}" for item in data.dispatcher_remarks)

    if data.fa_discussion_points:
        lines.extend(["", "Flight Attendant Discussion Points"])
        lines.extend(f"- {item}" for item in data.fa_discussion_points)

    if data.captain_discussion_points:
        lines.extend(["", "Captain Departure Discussion Points"])
        lines.extend(f"- {item}" for item in data.captain_discussion_points)

    if data.pilot_flying_points:
        lines.extend(["", "Pilot Flying Discussion Points"])
        lines.extend(f"- {item}" for item in data.pilot_flying_points)

    if data.arrival_brief_points:
        lines.extend(["", "Arrival Brief Discussion Points"])
        lines.extend(f"- {item}" for item in data.arrival_brief_points)

    if data.passenger_pa_notes:
        lines.extend(["", "Passenger PA Notes"])
        lines.extend(f"- {item}" for item in data.passenger_pa_notes)

    lines.extend(["", "ETOPS"])
    if data.route_alternates:
        lines.append(f"- Alternates: {' - '.join(data.route_alternates)}")
    for item in build_pdf_etops_items(data):
        lines.append(f"- {item}")

    if data.departure_notes:
        lines.extend(["", f"{data.departure} Notes"])
        lines.extend(f"- {item}" for item in data.departure_notes)

    if data.destination_notes:
        lines.extend(["", f"{data.destination} Notes"])
        lines.extend(f"- {item}" for item in data.destination_notes)

    if data.alternate_notes:
        lines.extend(["", f"{data.dispatch_alternate} Notes"])
        lines.extend(f"- {item}" for item in data.alternate_notes)

    if data.aircraft_notes:
        lines.extend(["", "Aircraft"])
        lines.extend(f"- {item}" for item in data.aircraft_notes)

    lines.extend(
        [
            "",
            "Bottom Line",
            "- Clean airplane if no active MEL/CDL and dispatch items.",
            "- Biggest risks are typically enroute ride, convective deviations, and destination weather/workload.",
        ]
    )
    return "\n".join(lines) + "\n"


def wrap(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if stringWidth(word, font_name, font_size) > max_width:
            if current:
                lines.append(current)
                current = ""
            chunk = ""
            for char in word:
                candidate = f"{chunk}{char}"
                if chunk and stringWidth(candidate, font_name, font_size) > max_width:
                    lines.append(chunk)
                    chunk = char
                else:
                    chunk = candidate
            current = chunk
            continue
        candidate = word if not current else f"{current} {word}"
        if stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def draw_fit_string(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_width: float,
    font_name: str,
    font_size: float,
    min_font_size: float = 6.8,
    align: str = "left",
) -> None:
    value = text or "--"
    size = font_size
    while size > min_font_size and stringWidth(value, font_name, size) > max_width:
        size -= 0.25
    while len(value) > 1 and stringWidth(value, font_name, size) > max_width:
        value = value[:-2] + "…"
    c.setFont(font_name, size)
    if align == "right":
        c.drawRightString(x + max_width, y, value)
    elif align == "center":
        c.drawCentredString(x + max_width / 2, y, value)
    else:
        c.drawString(x, y, value)


def draw_wrapped(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    font_name: str,
    font_size: float,
    color,
    line_height: float,
    bullet: bool = False,
) -> float:
    c.setFillColor(color)
    c.setFont(font_name, font_size)
    wrapped = wrap(text, font_name, font_size, width - (10 if bullet else 0))
    for i, line in enumerate(wrapped):
        prefix = "• " if bullet and i == 0 else ""
        indent = 10 if bullet else 0
        c.drawString(x + indent, y, prefix + line)
        y -= line_height
    return y


def draw_panel(
    c: canvas.Canvas,
    title: str,
    items: list[str],
    x: float,
    y_top: float,
    width: float,
    bg_color,
    title_color,
    body_color=TEXT_COLOR,
    min_height: float = 0.95 * inch,
) -> float:
    title_font = 10.0
    body_font = 9.1
    title_lines = wrap(title, "Helvetica-Bold", title_font, width - 22)
    body_line_count = 0
    for item in items:
        body_line_count += max(1, len(wrap(item, "Helvetica", body_font, width - 30)))
    height = max(min_height, 22 + len(title_lines) * 14 + body_line_count * 14 + 16)
    y_bottom = y_top - height
    c.setFillColor(bg_color)
    c.roundRect(x, y_bottom, width, height, 10, stroke=0, fill=1)
    c.setFillColor(title_color)
    c.setFont("Helvetica-Bold", title_font)
    y = y_top - 13
    for line in title_lines:
        c.drawString(x + 12, y, line)
        y -= 14
    y -= 6
    for item in items:
        y = draw_wrapped(c, item, x + 8, y, width - 16, "Helvetica", body_font, body_color, 13, bullet=True)
        y -= 2
    return y_bottom


def draw_box(c: canvas.Canvas, x: float, y: float, w: float, h: float, line_width: float = 1.0) -> None:
    c.setLineWidth(line_width)
    c.setStrokeColor(TEXT_COLOR)
    c.rect(x, y, w, h, stroke=1, fill=0)


def draw_label_value(
    c: canvas.Canvas,
    label: str,
    value: str,
    x: float,
    y: float,
    label_w: float,
    font_size: float = 9.5,
    value_font_size: float = 10.5,
    max_width: float | None = None,
) -> None:
    c.setFillColor(MUTED_COLOR)
    c.setFont("Helvetica-Bold", font_size)
    c.drawString(x, y, label)
    c.setFillColor(TEXT_COLOR)
    value_x = x + label_w
    draw_fit_string(
        c,
        value or "--",
        value_x,
        y,
        max_width if max_width is not None else 1.0 * inch,
        "Helvetica-Bold",
        value_font_size,
    )


def render_pdf(_text_output: str, pdf_path: Path, title: str, data: BriefData) -> None:
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    width, height = letter
    left = 0.42 * inch
    right = width - 0.42 * inch
    top = height - 0.38 * inch
    bottom = 0.38 * inch
    c.setTitle(title)
    c.setFillColor(HexColor("#ffffff"))
    c.rect(0, 0, width, height, stroke=0, fill=1)

    outer_w = right - left
    outer_h = top - bottom
    draw_box(c, left, bottom, outer_w, outer_h, 1.5)

    top_h = 2.38 * inch
    top_y = top - top_h
    draw_box(c, left, top_y, outer_w, top_h, 1.0)

    left_w = outer_w * 0.58
    right_x = left + left_w
    right_w = outer_w - left_w
    c.line(right_x, top_y, right_x, top)

    row1_y = top - 0.46 * inch
    c.setFont("Helvetica-Bold", 20)
    c.setFillColor(TEXT_COLOR)
    flight_label = (data.flight or "").replace("UAL ", "")
    draw_fit_string(c, flight_label, left + 12, row1_y, 1.50 * inch, "Helvetica-Bold", 20, 12)
    button_w = 0.98 * inch
    button_h = 0.26 * inch
    button_x = right_x - button_w - 12
    button_y = top - 0.33 * inch
    c.setFillColor(HEADER_COLOR)
    c.roundRect(button_x, button_y, button_w, button_h, 4, stroke=0, fill=1)
    c.setFillColor(HexColor("#ffffff"))
    draw_fit_string(c, "Back to App", button_x + 5, button_y + 0.09 * inch, button_w - 10, "Helvetica-Bold", 8.4, 6.4, "center")
    c.linkURL(APP_HOME_URL, (button_x, button_y, button_x + button_w, button_y + button_h), relative=0)
    c.setFillColor(TEXT_COLOR)
    draw_fit_string(c, data.date_code or "", left + 1.60 * inch, row1_y, button_x - (left + 1.72 * inch), "Helvetica", 10.5, 7, "right")
    c.line(left, top - 0.62 * inch, right_x, top - 0.62 * inch)
    c.line(left, top - 1.22 * inch, right_x, top - 1.22 * inch)
    c.line(left, top_y + 0.60 * inch, right_x, top_y + 0.60 * inch)

    inner_pad = 12
    usable_left_w = left_w - inner_pad * 2
    col_gap = 8
    dep_col_w = (usable_left_w - col_gap * 2) * 0.31
    arr_col_w = dep_col_w
    meta_col_w = usable_left_w - dep_col_w - arr_col_w - col_gap * 2
    dep_col = left + inner_pad
    arr_col = dep_col + dep_col_w + col_gap
    meta_col = arr_col + arr_col_w + col_gap
    draw_label_value(c, "DEP:", data.departure or "--", dep_col, top - 0.85 * inch, 34, max_width=dep_col_w - 34)
    draw_label_value(c, "ARR:", data.destination or "--", arr_col, top - 0.85 * inch, 34, max_width=arr_col_w - 34)
    draw_label_value(c, "TRIP:", data.trip_id or "--", meta_col, top - 0.85 * inch, 40, max_width=meta_col_w - 40)
    draw_label_value(c, "ICAO:", data.departure_icao or "--", dep_col, top - 1.10 * inch, 34, max_width=dep_col_w - 34)
    draw_label_value(c, "ICAO:", data.destination_icao or "--", arr_col, top - 1.10 * inch, 34, max_width=arr_col_w - 34)
    draw_label_value(c, "FLT:", flight_label, meta_col, top - 1.10 * inch, 30, value_font_size=16, max_width=meta_col_w - 30)

    route_line = (
        f"{data.route or '--'} | {data.aircraft_type or '--'} {data.aircraft_reg or '--'} | "
        f"Release {data.release_number or '--'} | Block {data.block or '--'}"
    )
    c.setFillColor(TEXT_COLOR)
    draw_fit_string(c, route_line, left + inner_pad, top_y + 0.82 * inch, usable_left_w, "Helvetica-Bold", 10.8, 7.2)

    time_labels = [
        ("OUT:", f"{data.out_time or '--'} / {data.out_local_time or '--'}"),
        ("OFF:", "--"),
        ("ON:", f"{data.eta or '--'} / {data.eta_local_time or '--'}"),
        ("IN:", "--"),
    ]
    time_cell_w = usable_left_w / 4
    label_y = top_y + 0.38 * inch
    value_y = top_y + 0.17 * inch
    for idx, (label, value) in enumerate(time_labels):
        x = left + inner_pad + idx * time_cell_w
        c.setFillColor(MUTED_COLOR)
        draw_fit_string(c, label, x, label_y, time_cell_w - 6, "Helvetica-Bold", 9.4, 7)
        c.setFillColor(TEXT_COLOR)
        draw_fit_string(c, value, x, value_y, time_cell_w - 8, "Helvetica-Bold", 11.2, 7.2)

    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(TEXT_COLOR)
    c.drawString(right_x + 10, top - 18, "FUEL")
    draw_fit_string(c, f"A/C {data.aircraft_reg or '--'}", right_x + 0.78 * inch, top - 18, right_w - 0.95 * inch, "Helvetica-Bold", 9.6, 6.8, "right")
    fuel_header_h = 0.34 * inch
    c.line(right_x, top - fuel_header_h, right, top - fuel_header_h)
    fuel_rows = [
        ("Gate", ""),
        ("Min T/O", data.minimum_takeoff_fuel or "--"),
        ("T/O", data.takeoff_fuel or "--"),
        ("Arr", data.landing_fuel or "--"),
        ("Alt", data.far_reserve_fuel or "--"),
    ]
    row_h = (top_h - fuel_header_h) / len(fuel_rows)
    for idx, (label, value) in enumerate(fuel_rows):
        y = top - fuel_header_h - idx * row_h
        c.line(right_x, y - row_h, right, y - row_h)
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(MUTED_COLOR)
        text_y = y - row_h + (row_h - 10) / 2
        c.drawString(right_x + 10, text_y, f"{label}:")
        c.setFillColor(TEXT_COLOR)
        draw_fit_string(c, value or "--", right_x + 0.75 * inch, text_y, right_w - 0.92 * inch, "Helvetica-Bold", 12.4 if label == "T/O" else 11.5, 7, "right")

    body_y = top_y
    body_h = outer_h - top_h
    draw_box(c, left, bottom, outer_w, body_h, 1.0)

    panel_gap = 0.14 * inch
    col_w = (outer_w - panel_gap) / 2
    left_x = left + 8
    right_col_x = left_x + col_w + panel_gap
    panel_w = col_w - 8
    y_cursor_left = body_y - 10
    y_cursor_right = body_y - 10

    crew_items = []
    if data.trip_id:
        crew_items.append(f"Trip {data.trip_id}")
    if data.captain:
        crew_items.append(f"CA: {data.captain}")
    if data.first_officer:
        crew_items.append(f"FO: {data.first_officer}")
    if data.iros:
        crew_items.append(f"IRO: {', '.join(data.iros)}")
    if data.purser:
        crew_items.append(f"FM01: {data.purser}")
    if data.flight_attendants:
        crew_items.append(f"FA: {', '.join(data.flight_attendants)}")
    if not crew_items:
        crew_items = ["Crew not loaded"]

    flight_items = [
        f"{data.route or '--'} | {data.aircraft_type or '--'} {data.aircraft_reg or ''}".strip(),
        f"OUT {data.out_time or '--'} / {data.out_local_time or '--'} | ETA {data.eta or '--'} / {data.eta_local_time or '--'}",
        f"Pickup {data.pickup_time or '--'} | Report {data.report_time or '--'}",
        f"Block {data.block or '--'} | Alt {data.dispatch_alternate or '--'} | Sector {data.dispatch_sector or '--'}",
        f"Dep {data.departure_runway or '--'} / {data.departure_sid or '--'}",
        f"Arr {data.arrival_star or 'not shown'} / {data.arrival_runway or '--'}",
    ]

    fuel_items = [
        f"Minimum T/O {data.minimum_takeoff_fuel or '--'}",
        f"Takeoff {data.takeoff_fuel or '--'}",
        f"Landing {data.landing_fuel or '--'}",
        f"FAR Reserve {data.far_reserve_fuel or '--'}",
        f"Conservative {data.conservative_fuel or '--'}",
        f"Taxi {data.taxi_fuel or '--'}",
    ]

    threat_items = data.threats[:4] if data.threats else ["No major threats extracted"]
    if data.dispatcher_remarks:
        for note in data.dispatcher_remarks[:2]:
            if note not in threat_items:
                threat_items.append(note)
            if len(threat_items) >= 5:
                break

    etops_items = []
    if data.etops_airports:
        etops_items.append(" - ".join(data.etops_airports))
    etops_items.extend(data.etops_cp_details[:3])
    if not etops_items:
        etops_items = [data.etops_minutes or "ETOPS not extracted"]

    y_cursor_left = draw_panel(c, "Crew", crew_items, left_x, y_cursor_left, panel_w, PANEL_BG, HEADER_COLOR)
    y_cursor_left -= 8
    y_cursor_left = draw_panel(c, "Flight", flight_items, left_x, y_cursor_left, panel_w, PANEL_BG, HEADER_COLOR)
    y_cursor_left -= 8
    draw_panel(c, "Fuel", fuel_items, left_x, y_cursor_left, panel_w, PANEL_BG, HEADER_COLOR)

    y_cursor_right = draw_panel(c, "Threats", threat_items, right_col_x, y_cursor_right, panel_w, THREAT_BG, THREAT_COLOR)
    y_cursor_right -= 8
    draw_panel(c, "ETOPS", etops_items, right_col_x, y_cursor_right, panel_w, PANEL_BG, HEADER_COLOR)

    c.save()


def render_full_brief_pdf(_text_output: str, pdf_path: Path, title: str, data: BriefData) -> None:
    page_size = landscape(letter)
    c = canvas.Canvas(str(pdf_path), pagesize=page_size)
    width, height = page_size
    margin_x = 0.38 * inch
    top = height - 0.42 * inch
    bottom = 0.42 * inch
    right = width - margin_x
    usable_w = right - margin_x
    blue = HexColor("#12579a")
    amber = HexColor("#d08600")
    red = HexColor("#c51f1a")
    green = HexColor("#148a28")
    grid = HexColor("#b7c4d3")
    light_blue = HexColor("#eef4fb")
    light_green = HexColor("#eef8ee")
    light_amber = HexColor("#fff6dc")
    light_red = HexColor("#fdeaea")
    light_gray = HexColor("#f5f7fa")
    footer = (
        f"Gold Standard Pilot Brief v3.4 - {data.flight.replace(' ', '')} {data.route} - "
        "crew briefing aid - verify all data with official OFP, FMS, ATIS, NOTAMs and company manuals"
    )
    page_num = 0
    temp_images = tempfile.TemporaryDirectory()

    def source_page_image(page_number: int) -> Path | None:
        if not data.source_pdf_path or not shutil.which("pdftoppm"):
            return None
        source = Path(data.source_pdf_path)
        if not source.exists():
            return None
        prefix = Path(temp_images.name) / f"source-page-{page_number}"
        try:
            subprocess.run(
                [
                    "pdftoppm",
                    "-png",
                    "-singlefile",
                    "-r",
                    "150",
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    str(source),
                    str(prefix),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None
        image_path = prefix.with_suffix(".png")
        return image_path if image_path.exists() else None

    def draw_image_map_box(
        title_text: str,
        image_path: Path | None,
        x: float,
        y_top: float,
        w: float,
        h: float,
        notes: list[str],
        title_color=blue,
    ) -> bool:
        if not image_path or not image_path.exists():
            return False
        c.setFillColor(title_color)
        c.setFont("Helvetica-Bold", 9.1)
        c.drawString(x, y_top - 2, title_text.upper())
        y_box_top = y_top - 12
        c.setFillColor(HexColor("#ffffff"))
        c.setStrokeColor(grid)
        c.setLineWidth(0.45)
        c.rect(x, y_box_top - h, w, h, stroke=1, fill=1)

        from PIL import Image

        with Image.open(image_path) as img:
            img_w, img_h = img.size
        note_h = 0.66 * inch if notes else 0
        image_area_w = w - 0.18 * inch
        image_area_h = h - note_h - 0.14 * inch
        scale = min(image_area_w / img_w, image_area_h / img_h)
        draw_w = img_w * scale
        draw_h = img_h * scale
        image_x = x + (w - draw_w) / 2
        image_y = y_box_top - 0.07 * inch - draw_h
        c.drawImage(str(image_path), image_x, image_y, draw_w, draw_h, preserveAspectRatio=False, mask="auto")

        if notes:
            c.setFillColor(TEXT_COLOR)
            c.setFont("Helvetica", 8.0)
            note_y = y_box_top - h + note_h - 0.16 * inch
            for item in notes[:3]:
                for idx, line in enumerate(wrap(item, "Helvetica", 8.0, w - 0.42 * inch)):
                    if note_y < y_box_top - h + 0.10 * inch:
                        return True
                    c.drawString(x + 0.20 * inch, note_y, ("• " if idx == 0 else "  ") + line)
                    note_y -= 10
        return True

    def lb(value: str) -> str:
        if not value:
            return "--"
        clean = value.replace(",", "").strip()
        try:
            amount = float(clean)
            if amount < 1000:
                amount *= 1000
            return f"{amount:,.0f} lb"
        except ValueError:
            return value

    def fuel_sum(*values: str) -> str:
        total = 0.0
        found = False
        for value in values:
            clean = (value or "").replace(",", "").strip()
            try:
                amount = float(clean)
            except ValueError:
                continue
            found = True
            total += amount * 1000 if amount < 1000 else amount
        return f"{total:,.0f} lb" if found else "--"

    def compact_etops(value: str) -> str:
        return (value or "--").replace(" MINUTE ETOPS", " min").replace("MINUTE ETOPS", "min")

    def weather_lines(code: str, label: str) -> list[str]:
        metar = first_metar_block(data.raw_text, code)
        taf = first_taf_block(data.raw_text, code)
        lines: list[str] = []
        if metar:
            lines.append(f"{label} METAR: {metar}")
        if taf:
            lines.append(f"{label} TAF: {taf}")
        return lines

    def start_page(page_title: str = "", subtitle: bool = True) -> None:
        nonlocal page_num
        if page_num:
            c.showPage()
        page_num += 1
        c.setFillColor(HexColor("#ffffff"))
        c.rect(0, 0, width, height, stroke=0, fill=1)
        y = top
        if page_title:
            c.setFillColor(HexColor("#202936"))
            c.setFont("Helvetica", 22)
            c.drawCentredString(width / 2, y, page_title)
            y -= 0.34 * inch
        if subtitle:
            c.setFillColor(blue)
            c.setFont("Helvetica", 12)
            c.drawCentredString(width / 2, y, f"{data.flight.replace(' ', '')} {data.route} | {data.aircraft_reg or '--'} | Gold Standard Pilot Brief v3.4")
        c.setFillColor(HexColor("#777777"))
        c.setFont("Helvetica", 6)
        c.drawString(margin_x + 6, 0.20 * inch, footer[:155])
        c.drawRightString(right - 6, 0.20 * inch, f"Page {page_num}")

    def panel(title_text: str, items: list[str], x: float, y_top: float, w: float, h: float, title_color=blue, bg=light_gray, font_size: float = 8.4) -> None:
        c.setFillColor(title_color)
        c.setFont("Helvetica-Bold", 9.1)
        c.drawString(x, y_top - 2, title_text.upper())
        y_box_top = y_top - 12
        c.setFillColor(bg)
        c.setStrokeColor(grid)
        c.setLineWidth(0.45)
        c.rect(x, y_box_top - h, w, h, stroke=1, fill=1)
        y = y_box_top - 17
        c.setFillColor(TEXT_COLOR)
        c.setFont("Helvetica", font_size)
        for item in items:
            for idx, line in enumerate(wrap(item, "Helvetica", font_size, w - 24)):
                if y < y_box_top - h + 9:
                    return
                c.drawString(x + 10, y, ("• " if idx == 0 else "  ") + line)
                y -= font_size + 3.4

    def table(rows: list[list[str]], col_widths: list[float], x: float, y_top: float, row_h: float, header=True, font_size: float = 7.6) -> float:
        y = y_top
        for r_idx, row in enumerate(rows):
            x_cursor = x
            fill = blue if header and r_idx == 0 else HexColor("#ffffff")
            c.setFillColor(fill)
            c.setStrokeColor(grid)
            c.setLineWidth(0.45)
            c.rect(x, y - row_h, sum(col_widths), row_h, stroke=1, fill=1)
            for idx, cell in enumerate(row):
                if idx:
                    c.line(x_cursor, y, x_cursor, y - row_h)
                c.setFillColor(HexColor("#ffffff") if header and r_idx == 0 else TEXT_COLOR)
                c.setFont("Helvetica-Bold" if header and r_idx == 0 else "Helvetica", font_size)
                c.drawString(x_cursor + 4, y - row_h + 6, str(cell)[:70])
                x_cursor += col_widths[idx]
            y -= row_h
        return y

    def wrapped_table(rows: list[list[str]], col_widths: list[float], x: float, y_top: float, max_h: float, header=True, font_size: float = 7.1) -> float:
        y = y_top
        bottom_limit = y_top - max_h
        for r_idx, row in enumerate(rows):
            wrapped_cells = [
                wrap(str(cell), "Helvetica-Bold" if header and r_idx == 0 else "Helvetica", font_size, col_widths[idx] - 8)
                for idx, cell in enumerate(row)
            ]
            row_h = max(0.20 * inch if r_idx == 0 else 0.24 * inch, 7 + max(len(lines) for lines in wrapped_cells) * (font_size + 2.4))
            if y - row_h < bottom_limit:
                break
            x_cursor = x
            fill = blue if header and r_idx == 0 else HexColor("#ffffff")
            c.setFillColor(fill)
            c.setStrokeColor(grid)
            c.setLineWidth(0.45)
            c.rect(x, y - row_h, sum(col_widths), row_h, stroke=1, fill=1)
            for idx, lines in enumerate(wrapped_cells):
                if idx:
                    c.line(x_cursor, y, x_cursor, y - row_h)
                c.setFillColor(HexColor("#ffffff") if header and r_idx == 0 else TEXT_COLOR)
                c.setFont("Helvetica-Bold" if header and r_idx == 0 else "Helvetica", font_size)
                text_y = y - font_size - 5
                for line in lines:
                    c.drawString(x_cursor + 4, text_y, line)
                    text_y -= font_size + 2.4
                x_cursor += col_widths[idx]
            y -= row_h
        return y

    def threat_items(limit: int = 6) -> list[str]:
        items = data.threats[:limit]
        if not items and data.dispatcher_remarks:
            items = data.dispatcher_remarks[:limit]
        return items or ["No high-risk threats auto-detected; verify OFP, ATIS, NOTAMs, and dispatch remarks."]

    def compact_route_points(limit: int = 16) -> list[str]:
        route_section = match_group(r"20 ECON F\d{3}\s+(.*?)\s+DOLSU/F", data.raw_text, flags=re.S)
        candidates = re.findall(r"\b[A-Z0-9]{4,6}\b", route_section)
        filtered = [
            point
            for point in candidates
            if not point.startswith("F")
            and point not in {"ECON", "DCT", "TRUE", "MAG", "WIND", "TEMP", "TROP", "MORA", "MSA"}
        ]
        points = dedupe_preserve_order(filtered)
        if points:
            return points[:limit]
        route = (data.route or "").replace("-", " ").split()
        return route or [data.departure_icao or data.departure or "DEP", data.destination_icao or data.destination or "ARR"]

    def draw_map_box(title_text: str, x: float, y_top: float, w: float, h: float, points: list[str], notes: list[str], title_color=blue) -> None:
        c.setFillColor(title_color)
        c.setFont("Helvetica-Bold", 9.1)
        c.drawString(x, y_top - 2, title_text.upper())
        y_box_top = y_top - 12
        c.setFillColor(light_blue)
        c.setStrokeColor(grid)
        c.setLineWidth(0.45)
        c.rect(x, y_box_top - h, w, h, stroke=1, fill=1)

        map_y = y_box_top - h * 0.38
        start_x = x + 0.62 * inch
        end_x = x + w - 0.62 * inch
        c.setStrokeColor(blue)
        c.setLineWidth(2.0)
        c.line(start_x, map_y, end_x, map_y)

        shown_points = dedupe_preserve_order([point for point in points if point])
        shown_points = shown_points[:6]
        if len(shown_points) < 2:
            shown_points = [data.departure_icao or data.departure or "DEP", data.destination_icao or data.destination or "ARR"]
        step = (end_x - start_x) / max(1, len(shown_points) - 1)
        for idx, point in enumerate(shown_points):
            px = start_x + idx * step
            c.setFillColor(HexColor("#ffffff"))
            c.setStrokeColor(blue)
            c.circle(px, map_y, 4.2, stroke=1, fill=1)
            c.setFillColor(TEXT_COLOR)
            c.setFont("Helvetica-Bold", 7.2)
            label_w = 0.70 * inch
            c.drawCentredString(px, map_y - 0.18 * inch if idx % 2 else map_y + 0.14 * inch, point[:8])

        note_y = y_box_top - h * 0.70
        c.setFillColor(TEXT_COLOR)
        c.setFont("Helvetica", 8.3)
        for item in notes[:5]:
            if note_y < y_box_top - h + 0.16 * inch:
                break
            for idx, line in enumerate(wrap(item, "Helvetica", 8.3, w - 0.45 * inch)):
                c.drawString(x + 0.22 * inch, note_y, ("• " if idx == 0 else "  ") + line)
                note_y -= 10.8

    col_gap = 0.08 * inch
    col_w = (usable_w - col_gap) / 2
    third_w = (usable_w - col_gap * 2) / 3
    right_x = margin_x + col_w + col_gap

    start_page("PAGE 1 - FLIGHT SUMMARY")
    top_panel_y = top - 0.78 * inch
    flight_summary_items = [
        f"Flight: {data.flight.replace('UAL ', 'UA') or '--'} / {data.date_code or '--'}",
        f"Route: {(data.route or '--').replace('-', ' to ')}",
        f"Aircraft: {data.aircraft_reg or '--'} {data.aircraft_type or ''}".strip(),
        f"Release: {data.release_number or '--'}",
    ]
    timing_items = [
        f"OUT: {data.out_time or '--'} / {data.out_local_time or '--'}",
        f"ETA: {data.eta or '--'} / {data.eta_local_time or '--'}",
        f"Block: {data.block or '--'}",
        f"ETOPS: {compact_etops(data.etops_minutes)}",
    ]
    crew_summary_items = []
    if data.trip_id:
        crew_summary_items.append(f"Trip ID: {data.trip_id}")
    crew_summary_items.extend(
        [
            f"CA: {data.captain or 'not entered'}",
            f"FO: {data.first_officer or 'not entered'}",
            f"Dispatcher sector: {data.dispatch_sector or '--'}",
        ]
    )
    panel("Flight", flight_summary_items, margin_x, top_panel_y, third_w, 0.90 * inch, blue, light_gray, font_size=8.2)
    panel("Timing", timing_items, margin_x + third_w + col_gap, top_panel_y, third_w, 0.90 * inch, blue, light_blue, font_size=8.2)
    panel("Crew / Dispatch", crew_summary_items, margin_x + (third_w + col_gap) * 2, top_panel_y, third_w, 0.90 * inch, blue, light_gray, font_size=8.2)

    crew_detail_items = []
    for row in crew_table_rows(data)[1:]:
        position, name, emp, role, seniority = row
        crew_detail_items.append(f"{position}: {name} | {role} | Emp {emp} | {seniority}")
    panel("Crew Detail", crew_detail_items or ["Crew not loaded"], margin_x, top - 1.98 * inch, usable_w, 0.72 * inch, blue, light_gray, font_size=7.7)

    p_y = top - 3.02 * inch
    fuel_items = [
        f"Plan Gate: {fuel_sum(data.takeoff_fuel, data.taxi_fuel)}",
        f"Taxi: {lb(data.taxi_fuel)}",
        f"Plan Takeoff: {lb(data.takeoff_fuel)}",
        f"Landing fuel / REMF: {lb(data.landing_fuel)}",
        f"Extra: {lb(data.extra_fuel)}",
        f"Alternate / FAR: {lb(data.far_reserve_fuel)}",
    ]
    perf_items = [
        f"ZFW: {lb(data.zfw_actual)} / limit {lb(data.zfw_limit)}",
        f"TOW: {lb(data.tow_actual)} / limit {lb(data.tow_limit)}",
        f"LW: {lb(data.lw_actual)} / limit {lb(data.lw_limit)}",
        f"Takeoff: {data.departure_icao or data.departure} R{data.departure_runway or 'verify'}",
        f"Arrival: {data.destination_icao or data.destination} R{data.arrival_runway or 'verify'}",
        f"SID/STAR: {data.departure_sid or 'verify'} / {data.arrival_star or 'not shown'}",
    ]
    dep_code = data.departure_icao or data.departure
    dest_code = data.destination_icao or data.destination
    wx_items = weather_lines(dep_code, data.departure or dep_code)[:2] + weather_lines(dest_code, data.destination or dest_code)[:2]
    wx_items.append(extract_turbulence_timing(data))
    mx_items = summarize_aircraft_status(data).split("; ") if data.aircraft_notes else ["No aircraft notes extracted; verify MEL/CDL, dispatch items, UTO/NEF."]
    panel("Fuel", fuel_items, margin_x, p_y, col_w, 1.24 * inch, green, light_green)
    panel("Weights / Perf", perf_items, margin_x + col_w + col_gap, p_y, col_w, 1.24 * inch, blue, light_gray)
    lower_y = p_y - 1.50 * inch
    panel("Weather Snapshot", wx_items, margin_x, lower_y, col_w, 0.82 * inch, blue, light_blue)
    panel("MX / Cabin", mx_items, margin_x + col_w + col_gap, lower_y, col_w, 0.82 * inch, amber, light_amber)

    start_page("PAGE 1B - CABIN / PA BRIEF")
    panel("Passenger PA", data.passenger_pa_notes, margin_x, top - 0.74 * inch, col_w, 1.18 * inch, blue, light_gray, font_size=8.5)
    panel("FA Brief", data.fa_discussion_points, margin_x + col_w + col_gap, top - 0.74 * inch, col_w, 1.42 * inch, blue, light_gray, font_size=8.5)
    panel("Cabin Coordination", ["Seatbelt strategy: brief timing based on ride forecast.", "Arrival prep: coordinate cabin secure target before descent.", "TEST review: Type, Evacuation, Special instructions, Time."], margin_x, top - 2.48 * inch, col_w, 0.96 * inch, amber, light_amber, font_size=8.5)
    panel("Arrival PA / Customer Notes", [data.passenger_pa_notes[-1] if data.passenger_pa_notes else "Arrival PA not generated.", f"Arrival gate: {data.arrival_gate or 'not shown in release'}", f"Destination weather: {extract_destination_weather_summary(data.raw_text, data.destination_icao or data.destination)}"], margin_x + col_w + col_gap, top - 2.48 * inch, col_w, 0.96 * inch, blue, light_blue, font_size=8.5)

    start_page("PAGE 2 - DEPARTURE PLAN")
    y = top - 0.74 * inch
    panel("Threats First", threat_items(), margin_x, y, usable_w, 0.96 * inch, amber, light_amber)
    y -= 1.24 * inch
    left_w = col_w
    dep_items = [
        f"Runway planned: {data.departure_icao or data.departure} {data.departure_runway or 'verify'}",
        f"Initial route/SID: {data.departure_sid or 'verify'}",
        f"Climb / step plan: {data.step_climbs[0] if data.step_climbs else 'No step climb extracted'}",
        "Verify aircraft/engine, runway, intersection, TOW, packs, wind, and FMS position.",
        "RVSM altimeter check after takeoff.",
    ]
    captain_items = data.captain_discussion_points[:6]
    notam_items = data.departure_notes[:8] or ["No operational departure NOTAM snippets extracted; verify current NOTAM package."]
    windshear_items = [
        "Use max thrust when required.",
        "Longest suitable runway / avoid tailwind if possible.",
        "Verify actual winds vs performance.",
        FM_WINDSHEAR_ESCAPE,
        "Microburst or >=30 kt alert for runway: no takeoff initiation or descent below 1000 AGL.",
    ]
    taxi_items = data.departure_notes[4:10] or data.departure_notes[:5] or ["Review airport 10-7, hotspots, construction, taxiway closures, and runway crossings."]
    panel("Departure Brief", dep_items, margin_x, y, left_w, 1.10 * inch, blue, light_gray)
    panel("Captain / PF Brief", captain_items, right_x, y, left_w, 1.10 * inch, blue, light_gray)
    y -= 1.42 * inch
    panel(f"Operational NOTAMs - {data.departure or dep_code}", notam_items, margin_x, y, left_w, 1.24 * inch, red, light_red)
    panel("Windshear / Microburst", windshear_items, right_x, y, left_w, 1.02 * inch, red, light_red)
    y -= 1.58 * inch
    panel("Taxi / 10-7 Style Notes", taxi_items, margin_x, y, usable_w, 0.86 * inch, blue, light_gray)

    start_page("PAGE 3 - TIMELINE / ETOPS / ORCA")
    orca = [
        "Engine failure: memory items, drift down, offset procedures, diversion evaluation, dispatch coordination.",
        "Rapid decompression: oxygen masks, emergency descent, safe altitude, diversion planning.",
        "Weather deviation: clearance request, offset procedures, position reporting.",
        "Cargo fire: memory items, immediate diversion, TEST coordination, landing planning.",
        "Medical: MedLink, diversion assessment, cabin coordination.",
    ]
    c.setFillColor(blue)
    c.setFont("Helvetica-Bold", 9.1)
    c.drawString(margin_x, top - 0.76 * inch, "START-TO-FINISH TIMELINE")
    c.setFillColor(TEXT_COLOR)
    c.setFont("Helvetica-Bold", 8.6)
    c.drawRightString(right_x + col_w, top - 0.76 * inch, "ACTUAL T/O TIME: __________  Z / LOCAL")
    c.setFont("Helvetica", 6.4)
    c.drawString(margin_x, top - 0.85 * inch, "Running clock reference: actual takeoff time + EET shown in the left column.")
    timeline_top = top - 0.99 * inch
    timeline_bottom = wrapped_table(
        build_operational_timeline_rows(data),
        [0.45 * inch, 1.38 * inch, 1.15 * inch, usable_w - 2.98 * inch],
        margin_x,
        timeline_top,
        3.97 * inch,
        font_size=5.65,
    )
    lower_panel_top = timeline_bottom - 0.18 * inch
    panel("ORCA Action Guide", orca[:3], margin_x, lower_panel_top, col_w, 0.64 * inch, red, light_red, font_size=6.45)
    panel("Dispatcher / ETOPS Notes", (data.dispatcher_remarks[:3] + build_pdf_etops_items(data)[:2]) or ["No dispatcher remarks extracted."], right_x, lower_panel_top, col_w, 0.64 * inch, amber, light_amber, font_size=6.45)
    panel("ETOPS Driftdown / Offset Reminder", ETOPS_DRIFTDOWN_OFFSET, margin_x, lower_panel_top - 1.06 * inch, usable_w, 0.90 * inch, red, light_red, font_size=5.75)

    start_page("PAGE 4 - ARRIVAL PLAN")
    arrival_items = data.arrival_brief_points[:7]
    dest_notes = data.destination_notes[:7] or ["No operational destination NOTAM snippets extracted; verify current NOTAM package."]
    alt_notes = data.alternate_notes[:6] or [f"Alternate {data.dispatch_alternate or '--'}: verify suitability, weather, approaches, and fuel."]
    panel("Arrival Brief", arrival_items, margin_x, top - 0.74 * inch, usable_w, 1.26 * inch, blue, light_gray)
    panel(f"Operational NOTAMs - {data.destination or dest_code}", dest_notes, margin_x, top - 2.30 * inch, col_w, 1.18 * inch, red, light_red)
    panel("Alternate / Diversion", alt_notes, right_x, top - 2.30 * inch, col_w, 1.18 * inch, amber, light_amber)
    panel("Stabilized Approach / Go-Around", [FM_STABILIZED_APPROACH, FM_GO_AROUND], margin_x, top - 3.78 * inch, usable_w, 0.82 * inch, blue, light_blue)

    route_points = compact_route_points()
    threat_waypoints = dedupe_preserve_order(
        point
        for threat in data.threats
        for pair in re.findall(r"\b([A-Z0-9]{3,6})\s*-\s*([A-Z0-9]{3,6})\b", threat)
        for point in pair
        if not point.startswith("FL") and not point.isdigit()
    )
    etops_summary_items = build_pdf_etops_items(data)
    etops_airport_line = " - ".join(data.etops_airports) if data.etops_airports else f"{data.departure_icao or data.departure or '--'} - {data.dispatch_alternate or '--'} - {data.destination_icao or data.destination or '--'}"
    etops_map_notes = [
        f"Route: {(data.route or '--').replace('-', ' to ')}",
        f"ETOPS alternates: {etops_airport_line}",
        f"Critical points: {', '.join(item.split(':', 1)[0] for item in data.etops_cp_details) if data.etops_cp_details else 'verify CPs in OFP'}",
        f"Primary fuel check: landing {lb(data.landing_fuel)} / FAR {lb(data.far_reserve_fuel)} / conservative {lb(data.conservative_fuel)}",
        extract_turbulence_timing(data),
    ]
    start_page("PAGE 5 - ETOPS MAP")
    etops_map_points = [data.departure_icao or data.departure or "DEP"]
    etops_map_points.extend(item.split(":", 1)[0] for item in data.etops_cp_details[:3])
    etops_map_points.append(data.destination_icao or data.destination or "ARR")
    etops_image_drawn = draw_image_map_box("ETOPS / Weather Map", source_page_image(2), margin_x, top - 0.74 * inch, usable_w, 5.45 * inch, [], blue)
    if not etops_image_drawn:
        draw_map_box("ETOPS Map Notes", margin_x, top - 0.74 * inch, usable_w, 2.25 * inch, etops_map_points, etops_map_notes, blue)
        panel("ETOPS Alternates / Suitability", data.etops_airports or ["No ETOPS suitable-airport list extracted; verify dispatch release."], margin_x, top - 3.36 * inch, col_w, 1.06 * inch, blue, light_gray)
        panel("ETOPS Weather / Dispatch Watch", wx_items[:4] + threat_items(2), right_x, top - 3.36 * inch, col_w, 1.06 * inch, amber, light_amber)

    start_page("PAGE 5 - ETOPS DETAILS")
    panel("ETOPS Critical Points", data.etops_cp_details or ["No CP detail extracted; verify ETOPS summary in the OFP."], margin_x, top - 0.74 * inch, usable_w, 1.30 * inch, blue, light_blue)
    panel("Fuel Decision Gates", etops_summary_items or ["Compare actual fuel to OFP required fuel at each CP."], margin_x, top - 2.34 * inch, col_w, 1.24 * inch, green, light_green)
    panel("ORCA / Diversion Reminders", orca, right_x, top - 2.34 * inch, col_w, 1.24 * inch, red, light_red)
    panel("HF / Oceanic Checks", ["Confirm HF/SatVoice requirements for route segment.", "SELCAL check when required.", "Position reports / CP tracking per clearance.", "Cabin and dispatch coordination before diversion commitment."], margin_x, top - 3.90 * inch, usable_w, 0.92 * inch, blue, light_gray)

    route_notes = [
        f"{' - '.join(route_points[:12])}",
        f"Initial altitude / step plan: {', '.join(data.step_climbs) if data.step_climbs else 'verify planned cruise and step climbs'}",
        f"Route alternates: {', '.join(data.route_alternates) if data.route_alternates else data.dispatch_alternate or 'verify alternates'}",
        extract_turbulence_timing(data),
        "Brief Class II / oceanic checks where applicable; verify route clearance against FMS legs.",
    ]
    start_page("PAGE 6 - ROUTE / JEPP OVERVIEW MAP")
    route_map_points = [data.departure_icao or data.departure or "DEP"]
    route_map_points.extend(threat_waypoints[:3] or [point for point in route_points if point not in {data.departure, data.destination, data.departure_icao, data.destination_icao}][:3])
    route_map_points.append(data.destination_icao or data.destination or "ARR")
    route_image_drawn = draw_image_map_box("Route Chart", source_page_image(5), margin_x, top - 0.74 * inch, usable_w, 5.45 * inch, [], blue)
    if not route_image_drawn:
        draw_map_box("Route Overview", margin_x, top - 0.74 * inch, usable_w, 2.38 * inch, route_map_points, route_notes, blue)
        panel("Departure / Arrival Anchors", [f"Departure: {data.departure_icao or data.departure or '--'} R{data.departure_runway or 'verify'} / {data.departure_sid or 'verify'}", f"Arrival: {data.destination_icao or data.destination or '--'} R{data.arrival_runway or 'verify'} / {data.arrival_star or 'not shown'}", f"Alternate: {data.dispatch_alternate or '--'}"], margin_x, top - 3.50 * inch, col_w, 1.02 * inch, blue, light_gray)
        panel("Weather / Ride Areas", wx_items[:3] + threat_items(3), right_x, top - 3.50 * inch, col_w, 1.02 * inch, amber, light_amber)

    start_page("PAGE 6 - ROUTE NOTES")
    panel("Route Notes", route_notes, margin_x, top - 0.74 * inch, usable_w, 1.12 * inch, blue, light_blue)
    panel("Dispatcher Remarks", data.dispatcher_remarks[:8] or ["No dispatcher remarks extracted."], margin_x, top - 2.14 * inch, col_w, 1.36 * inch, amber, light_amber)
    panel("Aircraft / Dispatch Status", mx_items + ["Verify MEL/CDL, dispatch items, UTO/NEF, EFBs, and cabin writeups before push."], right_x, top - 2.14 * inch, col_w, 1.36 * inch, blue, light_gray)
    panel("Route Threat Review", threat_items(6), margin_x, top - 3.82 * inch, usable_w, 0.94 * inch, red, light_red)

    system_title, system_number, system_total, system_items, memory_items = build_rotating_review(data)
    jim_items = [
        "Threat lens: personal, environmental, technical; assign PF/PM and relief duties clearly.",
        "Arrival gate: stable by 1000 feet; call stable/unstable at 500 feet.",
        "TEST cabin communication: Type, Evacuation, Special instructions, Time.",
        "Keep the brief short, operational, and easy to update as ATIS and clearance change.",
    ]
    captain_flow_rows = [
        ["Phase", "Trigger", "Captain scan / action"],
        ["Pre-brief", "Before crew brief", "Threat lens, roles, fuel, MEL/CDL, dispatch remarks, ETOPS suitability, personal workload."],
        ["Gate", "Before push", "Final OFP/clearance/FMS route check, doors/cabin, performance, takeoff alternate/diversion mindset."],
        ["Taxi", "Before takeoff", f"Runway {data.departure_runway or 'verify'}, SID {data.departure_sid or 'verify'}, hot spots, RTO plan, windshear/terrain."],
        ["Takeoff", "After airborne", "Enter actual T/O time in app/PDF, confirm navigation, RVSM altimeter check, first fuel trend."],
        ["Climb", "TOC / step climb", f"Step plan: {', '.join(data.step_climbs[:2]) if data.step_climbs else 'verify cruise/step climbs'}; ride and weather scan."],
        ["Oceanic / ETOPS", "Entry to CP", "HF/SELCAL/CPDLC as required, position awareness, cabin status, compare fuel to OFP required."],
        ["Critical Point", "At CP", "Best alternate, forecast, fuel remaining vs required, drift-down/decompression path, dispatch coordination."],
        ["Arrival setup", "TOD / ATIS", f"STAR {data.arrival_star or 'verify'}, runway {data.arrival_runway or 'verify'}, braking/winds, terrain, gate/taxi threats."],
        ["Shutdown", "At gate", "Block in, fuel remaining, writeups, cabin/security, crew fatigue/connection issues, customer-impact notes."],
        ["Post-flight", "After paperwork", "Log defects while fresh, debrief threats/lessons, save useful notes for next brief/reference search."],
    ]
    postflight_items = [
        f"Arrive / block target: ETA {data.eta or '--'} / block {data.block or '--'}; planned landing fuel {lb(data.landing_fuel)}.",
        "Capture discrepancies immediately: aircraft, EFB/PEDs, cabin, gate/ramp, ATC, weather, ride, fuel.",
        "Debrief one thing to repeat and one thing to improve; add useful notes to reference files.",
    ]
    start_page("PAGE 7 - CAPTAIN FLOW / 777 REVIEW")
    flow_bottom = wrapped_table(
        captain_flow_rows,
        [0.88 * inch, 1.10 * inch, usable_w - 1.98 * inch],
        margin_x,
        top - 0.74 * inch,
        2.82 * inch,
        font_size=6.15,
    )
    review_top = flow_bottom - 0.18 * inch
    panel(f"System of the Day - {system_title} ({system_number}/{system_total})", system_items[:4], margin_x, review_top, col_w, 0.98 * inch, green, light_green, font_size=6.75)
    panel("Memory / Limitations Review", memory_items[:4], right_x, review_top, col_w, 0.98 * inch, red, light_red, font_size=6.75)
    lower_review_top = review_top - 1.20 * inch
    panel("Captain Technique", jim_items, margin_x, lower_review_top, col_w, 0.88 * inch, blue, light_gray, font_size=6.75)
    panel("Post-flight Closeout", postflight_items, right_x, lower_review_top, col_w, 0.88 * inch, amber, light_amber, font_size=6.75)
    panel("Bottom Line", ["Brief in time order, fly the plan, update the plan at decision gates, and capture lessons before they fade.", "Verify all data against official OFP, FMS, ATIS, NOTAMs, and company manuals."], margin_x, lower_review_top - 1.06 * inch, usable_w, 0.56 * inch, amber, light_amber, font_size=6.9)

    c.save()
    temp_images.cleanup()


def build_output_paths(pdf_path: Path, out_dir: Path) -> tuple[Path, Path, Path]:
    stem = pdf_path.stem.replace(" ", "_")
    txt_path = out_dir / f"{stem}_airplane_card.txt"
    out_pdf = out_dir / f"{stem}_airplane_card.pdf"
    full_pdf = out_dir / f"{stem}_flight_briefs.pdf"
    return txt_path, out_pdf, full_pdf


def update_catalog(data: BriefData, source_pdf: Path, txt_path: Path, pdf_path: Path, full_pdf_path: Path) -> None:
    app_dir = Path(__file__).resolve().parent / "flight-brief-app"
    briefs_dir = app_dir / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = app_dir / "briefs.json"
    existing: list[dict] = []
    if catalog_path.exists():
        try:
            existing = json.loads(catalog_path.read_text())
        except json.JSONDecodeError:
            existing = []

    deployed_pdf = briefs_dir / pdf_path.name
    deployed_full_pdf = briefs_dir / full_pdf_path.name
    deployed_txt = briefs_dir / txt_path.name
    shutil.copy2(pdf_path, deployed_pdf)
    shutil.copy2(full_pdf_path, deployed_full_pdf)
    shutil.copy2(txt_path, deployed_txt)

    def timeline_minutes(eet: str) -> int:
        match = re.match(r"(\d+):(\d{2})", eet or "")
        if not match:
            return 0
        return int(match.group(1)) * 60 + int(match.group(2))

    timeline_points = [
        {
            "eet": row[0],
            "event": row[1],
            "fuel_alt": row[2],
            "action": row[3],
            "minutes": timeline_minutes(row[0]),
        }
        for row in build_operational_timeline_rows(data)[1:]
    ]

    entry = {
        "id": pdf_path.stem,
        "title": f"{data.flight.replace(' ', '')} {data.route}".strip(),
        "subtitle": f"{data.date_code} | {data.aircraft_type} {data.aircraft_reg}".strip(" |"),
        "trip_id": data.trip_id,
        "pairing_note": data.pairing_note,
        "captain": data.captain,
        "first_officer": data.first_officer,
        "iros": data.iros,
        "purser": data.purser,
        "flight_attendants": data.flight_attendants,
        "route": data.route,
        "departure_icao": data.departure_icao,
        "destination_icao": data.destination_icao,
        "departure_runway": data.departure_runway,
        "departure_sid": data.departure_sid,
        "arrival_star": data.arrival_star,
        "arrival_runway": data.arrival_runway,
        "dispatch_sector": data.dispatch_sector,
        "date_code": data.date_code,
        "flight": data.flight,
        "aircraft": f"{data.aircraft_type} {data.aircraft_reg}".strip(),
        "release": data.release_number,
        "out_time": data.out_time,
        "out_local_time": data.out_local_time,
        "eta": data.eta,
        "eta_local_time": data.eta_local_time,
        "pickup_time": data.pickup_time,
        "report_time": data.report_time,
        "block": data.block,
        "alternate": data.dispatch_alternate,
        "far_reserve_fuel": data.far_reserve_fuel,
        "conservative_fuel": data.conservative_fuel,
        "step_climbs": data.step_climbs,
        "etops_airports": data.etops_airports,
        "etops_cp_details": data.etops_cp_details,
        "dispatcher_notes": data.dispatcher_remarks,
        "fa_discussion_points": data.fa_discussion_points,
        "captain_discussion_points": data.captain_discussion_points,
        "pilot_flying_points": data.pilot_flying_points,
        "arrival_brief_points": data.arrival_brief_points,
        "top_threats": data.threats[:3],
        "timeline_points": timeline_points,
        "pdf": f"./briefs/{deployed_pdf.name}",
        "full_pdf": f"./briefs/{deployed_full_pdf.name}",
        "text": f"./briefs/{deployed_txt.name}",
        "source_pdf": str(source_pdf),
    }

    try:
        catalog_path.write_text(json.dumps([entry], indent=2))
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a shareable airplane card from a United flight release PDF.")
    parser.add_argument("--pdf", required=True, help="Path to release PDF")
    parser.add_argument("--out-dir", default="output/pdf", help="Directory for txt/pdf outputs")
    parser.add_argument("--trip-id", default="", help="Trip or pairing id (for example F2174)")
    parser.add_argument("--pairing-note", default="", help="Trip-wide note that applies to the whole pairing")
    parser.add_argument("--captain", default="", help="Captain name")
    parser.add_argument("--first-officer", default="", help="First officer name")
    parser.add_argument("--iro", default="", help="Comma-separated IRO / relief pilot names")
    parser.add_argument("--purser", default="", help="Purser name (FM01)")
    parser.add_argument("--fa", default="", help="Comma-separated flight attendant names")
    parser.add_argument("--pickup-time", default="", help="Pickup time for the leg, usually local station time")
    parser.add_argument("--report-time", default="", help="Report time for the leg, usually local station time")
    parser.add_argument("--pairing-pdf", default="", help="Optional pairing PDF for crew and trip ID extraction")
    parser.add_argument("--trip-kit-pdf", default="", help="Optional trip kit PDF for NOTAM and 10-7 extraction")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser()
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    data = parse_brief(pdf_path)
    data.source_pdf_path = str(pdf_path)
    data.trip_id = args.trip_id.strip()
    data.pairing_note = args.pairing_note.strip()
    data.captain = args.captain.strip()
    data.first_officer = args.first_officer.strip()
    data.iros = parse_csv_people(args.iro)
    data.purser = args.purser.strip()
    data.flight_attendants = parse_csv_people(args.fa)
    data.pickup_time = args.pickup_time.strip()
    data.report_time = args.report_time.strip()
    if args.pairing_pdf:
        apply_pairing_crew(data, Path(args.pairing_pdf).expanduser())
    if args.trip_kit_pdf:
        apply_trip_kit_notes(data, Path(args.trip_kit_pdf).expanduser())
    if args.pairing_pdf or args.trip_kit_pdf:
        refresh_derived_brief_points(data)
    txt_output = render_text(data)
    txt_path, out_pdf, full_pdf = build_output_paths(pdf_path, out_dir)
    txt_path.write_text(txt_output)
    render_pdf(txt_output, out_pdf, f"{data.flight.replace(' ', '')} Brief", data)
    render_full_brief_pdf(txt_output, full_pdf, f"{data.flight.replace(' ', '')} Flight Briefs", data)
    update_catalog(data, pdf_path, txt_path, out_pdf, full_pdf)

    print(txt_path)
    print(out_pdf)
    print(full_pdf)


if __name__ == "__main__":
    main()
