"""Terrain class (All-Terrain / Mud-Terrain) — the single definition.

Season (Winter / All-Season / All-Weather) and terrain are independent facts.
A tire can be both an A/T and a 3PMSF all-weather tire. The old `Type_` tag
could only carry one of them, and every supplier except Formula had no terrain
value at all, so a "Trail Blade A/T" with the snowflake became "All Weather"
and the A/T was lost (about 1,500 A/T and 170 M/T tires in the 2026-11-19
build). Terrain now rides in its own tag namespace, `Terrain_`.

Only name-level evidence counts: A/T, M/T, "all terrain", "mud terrain". R/T
(rugged/hybrid) and H/T (highway) are deliberately NOT terrain classes here.
"""
import re

ALL_TERRAIN = "All-Terrain"
MUD_TERRAIN = "Mud-Terrain"

# "A/TX", "A/T4W", "A/TIII" all start with A/T, so no trailing boundary.
# Bare "AT3"/"AT4W" model codes need a digit; plain "AT" would hit too much.
_AT = re.compile(r"(?<![A-Z0-9])A/T|(?<![A-Z0-9])AT(?![A-Z0-9])|(?<![A-Z0-9])AT\d|ALL[\s\-]?TERRAIN", re.I)
_MT = re.compile(r"(?<![A-Z0-9])M/T|(?<![A-Z0-9])MT\d|MUD[\s\-]?(?:TERRAIN|TRAC)\b", re.I)

# Street tires whose name happens to contain "A/T" (Eagle F1 Asymmetric SUV AT
# is an ultra-high-performance road tire, not an off-road one).
_NOT_TERRAIN = re.compile(r"EAGLE\s+F1\s+ASYM", re.I)


def detect_terrain(*texts) -> str:
    """Return 'Mud-Terrain', 'All-Terrain' or '' from model/description text.

    Mud wins when both appear ("All Terrain / Mud Terrain" marketing copy is
    rare; a true M/T is the narrower claim).
    """
    s = " ".join(str(t) for t in texts if t and str(t).lower() != "nan")
    if _NOT_TERRAIN.search(s):
        return ""
    if _MT.search(s):
        return MUD_TERRAIN
    if _AT.search(s):
        return ALL_TERRAIN
    return ""


def terrain_tag(*texts) -> str:
    """The `Terrain_...` tag for this text, or '' when it has no terrain class."""
    t = detect_terrain(*texts)
    return f"Terrain_{t}" if t else ""
