"""
tire_designation_inference.py
Infers tire seasonal designation from Formula Distributors product data.

5-layer inference (in priority order):
  Layer 1   — API boolean flags (is_winter_tire, is_all_weather, etc.)
  Layer 2   — Structural size markers (3PMSF, C-suffix commercial, LT prefix)
  Layer 3   — Model/brand name lookup against MODEL_MAP (~80 known models)
  Layer 3.5 — Keyword sweep on brand + model + description
  Layer 4   — UTQG treadwear number heuristic
  Layer 5   — Speed rating fallback

Output designation values (space-separated, matching augment.py DESIGNATION_MAP):
  Winter | All Weather | All Season | All Season (LT) | Summer |
  Mud Snow | Mud Terrain | All Terrain | All Summer | Commercial | Unknown
"""

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Layer 3 — Model/Brand reference table
# Keys are lowercase; longer keys are matched first to prevent partial hits.
# ---------------------------------------------------------------------------

MODEL_MAP: dict[str, str] = {

    # ── WINTER ──────────────────────────────────────────────────────────────
    "blizzak ws90":         "Winter",
    "blizzak dm-v3":        "Winter",
    "blizzak":              "Winter",
    "wintercontact":        "Winter",
    "winterforce":          "Winter",
    "winter i*cept":        "Winter",
    "icept":                "Winter",
    "ice zero":             "Winter",
    "ice contact":          "Winter",
    "icecontact":           "Winter",
    "grip 20":              "Winter",
    "grip 21":              "Winter",
    "antares grip":         "Winter",
    "arctictrek":           "Winter",
    "winguard":             "Winter",
    "win-gia":              "Winter",
    "ultragrip":            "Winter",
    "ultra grip":           "Winter",
    "northtrek":            "Winter",
    "north trek":           "Winter",
    "w-drive":              "Winter",
    "x-ice":                "Winter",
    "xice":                 "Winter",
    "nordic":               "Winter",
    "weather master":       "Winter",   # BFGoodrich WeatherMaster — dedicated winter
    "weathermaster":        "Winter",
    "arctia":               "Winter",
    "hakkapeliitta":        "Winter",
    "sottozero":            "Winter",
    "krisalp":              "Winter",
    "nordman":              "Winter",
    "alpin ":               "Winter",   # Michelin Alpin series (space prevents matching "alpine")
    "victoria":             "Winter",   # Pirelli Winter Sottozero/Victoria line

    # ── ALL-WEATHER (3PMSF-rated, usable year-round) ─────────────────────
    "cinturato all season":      "All Weather",
    "efficientgrip all season":  "All Weather",
    "vector 4seasons":           "All Weather",
    "vector4seasons":            "All Weather",
    "kinergy 4s":                "All Weather",
    "cross climate":             "All Weather",
    "crossclimate":              "All Weather",
    "weather peak":              "All Weather",
    "weatherpeak":               "All Weather",
    "all climate":               "All Weather",
    "allclimate":                "All Weather",
    "quatrac":                   "All Weather",
    "celsius":                   "All Weather",   # Toyo Celsius — all-weather confirmed

    # ── ALL-SEASON ──────────────────────────────────────────────────────────
    "all season contact":  "All Season",
    "pilot sport a/s":     "All Season",
    "pilot sport as":      "All Season",
    "cinturato p7 as":     "All Season",
    "cinturato p7as":      "All Season",
    "premier ltx":         "All Season",
    "latitude tour":       "All Season",
    "defender ltx":        "All Season",
    "defender t+h":        "All Season",
    "defender":            "All Season",
    "sentury touring":     "All Season",
    "ingens a1":           "All Season",
    "su-830":              "All Season",
    "su830":               "All Season",
    "assurance":           "All Season",
    "symmetry":            "All Season",
    "integrity":           "All Season",
    "harmony":             "All Season",
    "serenity":            "All Season",
    "advanta":             "All Season",
    "solus":               "All Season",
    "kinergy gt":          "All Season",
    "kinergy pt":          "All Season",
    "wildpeak h/t":        "All Season",
    "grabber hts":         "All Season",
    "duravis":             "All Season",   # Bridgestone Duravis — LT/van all-season
    "destino":             "All Season",
    "turanza er33":        "All Season",   # AS variant of Turanza
    "turanza quiettrack":  "All Season",
    "potenza re980as":     "All Season",   # Bridgestone — AS variant

    # ── ALL-SEASON (LT / All-Terrain) ────────────────────────────────────
    "open country a/t":    "All Season (LT)",
    "open country at":     "All Season (LT)",
    "open country m/t":    "All Season (LT)",
    "wildpeak a/t":        "All Season (LT)",
    "discoverer a/t":      "All Season (LT)",
    "discoverer at3":      "All Season (LT)",
    "grabber at":          "All Season (LT)",
    "grandtrek":           "All Season (LT)",
    "deegan 38 at":        "All Season (LT)",
    "rugged terrain":      "All Season (LT)",
    "ridge grappler":      "All Season (LT)",
    "terra grappler":      "All Season (LT)",
    "mud grappler":        "All Season (LT)",
    "baja boss":           "All Season (LT)",
    "baja champion":       "All Season (LT)",
    "ko2":                 "All Season (LT)",   # BFGoodrich All-Terrain T/A KO2
    "ko3":                 "All Season (LT)",

    # ── SUMMER ──────────────────────────────────────────────────────────────
    "pilot sport 4s":       "Summer",
    "pilot sport 4":        "Summer",
    "pilot sport 5":        "Summer",
    "pilot sport cup":      "Summer",
    "pilot super sport":    "Summer",
    "re-71rs":              "Summer",
    "re-71":                "Summer",
    "re71":                 "Summer",
    "sport maxx rt2":       "Summer",
    "sport maxx rt":        "Summer",
    "sport maxx race":      "Summer",
    "sport maxx":           "Summer",
    "eagle f1 asymmetric":  "Summer",
    "eagle f1":             "Summer",
    "hankook k120":         "Summer",
    "ventus v12":           "Summer",
    "ventus s1 evo":        "Summer",
    "conti sport contact":  "Summer",
    "sportcontact 7":       "Summer",
    "sportcontact 6":       "Summer",
    "sportcontact 5":       "Summer",
    "sportcontact":         "Summer",
    "sp sport maxx":        "Summer",
    "azenis fk520":         "Summer",
    "azenis fk510":         "Summer",
    "azenis rs660":         "Summer",
    "azenis":               "Summer",
    "proxes sport":         "Summer",   # Toyo Proxes Sport (not Proxes A/T or Sport A/S)
    "ps91":                 "Summer",   # Bridgestone Potenza PS91
    "potenza s007":         "Summer",
    "potenza sport":        "Summer",
    "p zero corsa":         "Summer",
    "p zero trofeo":        "Summer",
    "p zero":               "Summer",
    "cinturato p1":         "Summer",   # European summer
}


# ---------------------------------------------------------------------------
# Speed rating classification
# ---------------------------------------------------------------------------

WINTER_SPEED_RATINGS = {"L", "M", "N", "P", "Q", "R", "S"}   # ≤ 180 km/h
STANDARD_AS_RATINGS  = {"T", "H"}                              # 190–210 km/h
PERFORMANCE_RATINGS  = {"V", "W", "Y", "Z", "ZR"}             # ≥ 240 km/h


# ---------------------------------------------------------------------------
# Layer 3.5 — Keyword patterns (brand + model + description text)
# Checked in priority order; word boundaries used for short/ambiguous terms.
# ---------------------------------------------------------------------------

_KEYWORD_PATTERNS = [
    ("Winter", [
        r"winter", r"\bice\b", r"x-ice", r"xice",
        r"\bsnow\b", r"snowflake", r"studded", r"studless",
        r"blizzak", r"hakkapeliitta", r"\balpin\b",
        r"sottozero", r"coldcontact", r"krisalp", r"nordman",
        r"winterforce", r"wintercontact", r"winguard",
        r"winsport", r"wintersport", r"eurowin",
        r"arctictrak", r"arcticfrost", r"\bnordik\b", r"\bnordica\b",
        r"\barctic\b", r"\bvantra\b", r"nieve", r"neve",
        r"hiver", r"inverno",                              # French/Italian winter keywords
        r"friktion", r"piikkiton",                         # Nordic market keywords
    ]),
    ("All Weather", [
        r"all[- ]weather", r"\ba/w\b",
        r"cross[- ]?climate", r"weather[- ]?ready",
        r"\bcelsius\b", r"4[- ]season", r"four[- ]season",
        r"discoverer\s+awv", r"all[- ]climate",
    ]),
    ("All Terrain", [
        r"all[- ]terrain", r"\ba/t\b",
        r"\bko2\b", r"\bko3\b",
        r"wildpeak\s+a/t", r"grabber\s+at",
        r"dueler\s+a/t", r"destination\s+a/t",
        r"open\s+country\s+a/t", r"rugged\s+terrain",
        r"\btrail\b", r"wrangler\s+at",
    ]),
    ("Mud Terrain", [
        r"mud[- ]terrain", r"\bm/t\b",
        r"\bbogger\b", r"\bswamper\b", r"\bcrawler\b",
        r"\bthornbird\b", r"\bbaja\b",
        r"extreme\s+terrain", r"super\s+swamper",
        r"rock\s+terrain",
    ]),
    ("Mud Snow", [
        r"mud[- ]snow", r"\bmud\b",
    ]),
    ("All Summer", [
        r"all[- ]summer",
    ]),
    ("All Season", [
        r"all[- ]season", r"\ba/s\b",
        r"grand\s+touring", r"sport\s+touring", r"highway\s+touring",
        r"\bh/t\b", r"\btouring\b",
        r"\bassurance\b", r"\bprimacy\b",
        r"energy\s+saver", r"\bcomfortride\b",
    ]),
    ("Summer", [
        r"\bsummer\b", r"\buhp\b",
        r"ultra[- ]high[- ]performance",
        r"\btrack\b", r"\bracing\b", r"\bcompetition\b", r"\bcircuit\b",
        r"pilot\s+sport", r"eagle\s+f1",
        r"\bpotenza\b", r"\badvan\b", r"\bsupercar\b",
        r"\bps4s?\b", r"\bre-71\b", r"\bazenis\b",
        r"\bsport\b",
    ]),
]

_COMPILED_KEYWORD_PATTERNS = [
    (desig, [re.compile(p, re.IGNORECASE) for p in patterns])
    for desig, patterns in _KEYWORD_PATTERNS
]


# ---------------------------------------------------------------------------
# Core inference function
# ---------------------------------------------------------------------------

def infer_designation(row) -> tuple[str, str]:
    """
    Returns (designation, reason) for a single product row (pandas Series or dict).

    Columns consumed (all optional — function degrades gracefully if absent):
        is_winter_tire, is_all_weather, is_all_season, is_mud_snow  → API booleans
        size                                                         → e.g. "LT245/75R16"
        description                                                  → product description
        model                                                        → model name
        brand_name                                                   → brand
        utqg_treadwear                                               → e.g. "500"
        speed_rating                                                 → e.g. "H"
    """

    def get(col, *aliases):
        for name in (col, *aliases):
            val = row.get(name) if hasattr(row, "get") else getattr(row, name, None)
            if val is not None and str(val).strip() not in ("", "nan", "None"):
                return str(val).strip()
        return ""

    # ── Layer 1: API boolean flags ─────────────────────────────────────────
    if _truthy(get("is_winter_tire", "is_winter")):
        return "Winter", "Layer 1: is_winter_tire flag"
    if _truthy(get("is_all_weather")):
        return "All Weather", "Layer 1: is_all_weather flag"
    if _truthy(get("is_all_season")):
        return "All Season", "Layer 1: is_all_season flag"
    if _truthy(get("is_mud_snow")):
        return "Mud Snow", "Layer 1: is_mud_snow flag"

    # ── Layer 2: Structural size markers ──────────────────────────────────
    size        = get("size", "tire_size")
    description = get("description", "title")
    combined    = f"{size} {description}".upper()

    if re.search(r"3PMSF|THREE.?PEAK", combined):
        return "All Weather", "Layer 2: 3PMSF marker in size/description"

    is_lt = bool(re.match(r"LT\d", size.upper())) or bool(re.search(r"\bLT\b", combined))

    if re.search(r"\d{2,3}R\d{2}C\b", size.upper()):
        return "Commercial", "Layer 2: C-suffix commercial size"

    # ── Layer 3: Model/brand name lookup ──────────────────────────────────
    model      = get("model", "model_name").lower()
    brand      = get("brand_name", "brand").lower()
    model_text = f"{brand} {model}".strip()

    designation = _model_lookup(model_text) or _model_lookup(model)
    if designation:
        if is_lt and designation == "All Season":
            return "All Season (LT)", f"Layer 3: model '{model}' + LT size"
        return designation, f"Layer 3: model match → '{model}'"

    # ── Layer 3.5: Keyword sweep on brand + model + description ───────────
    search_text = f"{brand} {model} {description}"
    kw = _keyword_lookup(search_text)
    if kw:
        if is_lt and kw in ("All Season", "All Terrain"):
            return "All Season (LT)", f"Layer 3.5: keyword '{kw}' + LT size"
        return kw, f"Layer 3.5: keyword match → '{kw}'"

    # ── Layer 4: UTQG treadwear heuristic ─────────────────────────────────
    utqg_num = _parse_utqg(get("utqg_treadwear", "utqg"))

    if utqg_num is None:
        if is_lt:
            return "All Season (LT)", "Layer 4: no UTQG + LT size → LT tire"
        return "Winter", "Layer 4: no UTQG → likely dedicated winter"

    if utqg_num <= 160:
        return "Summer", f"Layer 4: UTQG {utqg_num} ≤ 160 → UHP summer"
    if utqg_num <= 360:
        return "Summer", f"Layer 4: UTQG {utqg_num} ≤ 360 → performance summer"
    if utqg_num >= 400:
        base = "All Season (LT)" if is_lt else "All Season"
        return base, f"Layer 4: UTQG {utqg_num} ≥ 400 → all-season"

    # ── Layer 5: Speed rating fallback ────────────────────────────────────
    speed = get("speed_rating").upper().rstrip("R")   # strip trailing R (e.g. ZR → Z)

    if speed in WINTER_SPEED_RATINGS:
        return "Winter", f"Layer 5: speed rating {speed} → winter range"
    if speed in PERFORMANCE_RATINGS:
        return "Summer", f"Layer 5: speed rating {speed} → performance summer"
    if speed in STANDARD_AS_RATINGS:
        base = "All Season (LT)" if is_lt else "All Season"
        return base, f"Layer 5: speed rating {speed} → all-season"

    if "M+S" in combined or "MUD AND SNOW" in combined or "M/S" in combined:
        base = "All Season (LT)" if is_lt else "All Season"
        return base, "Layer 5: M+S marker"

    return "Unknown", "No signals matched — manual review required"


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _truthy(val: str) -> bool:
    return val.lower() in ("1", "true", "yes", "y")


def _model_lookup(text: str) -> Optional[str]:
    text = text.lower()
    for key in sorted(MODEL_MAP, key=len, reverse=True):
        if key in text:
            return MODEL_MAP[key]
    return None


def _keyword_lookup(text: str) -> str:
    text = str(text)
    for desig, compiled in _COMPILED_KEYWORD_PATTERNS:
        if any(p.search(text) for p in compiled):
            return desig
    return ""


def _parse_utqg(utqg_raw: str) -> Optional[int]:
    if not utqg_raw:
        return None
    match = re.search(r"\d{2,4}", str(utqg_raw))
    return int(match.group()) if match else None
