"""Shared tire/wheel size parsing for all supplier pipelines.

Union of every per-supplier parser (see SIZE_PARSER_SPEC.md for the audit):
- anchored construction accepts R, D (bias-ply, from dttire) and RF run-flat
- description fallback (formula/NC) and slash-triplet 285/75/16 (emergetires)
- wheel parser is the NC/emergetires multi-format version (superset of formula's)

Supplier-specific quirks stay in their pipelines: wheelsmart decode_size,
emergetires spec-column preference. coerce_load_index() started as one of
those (emergetires' _coerce_load_index) and moved here once it turned out
every Costar-backed supplier needed the same zero-placeholder guard.
"""
import re

_SIZE_ANCHORED = re.compile(
    r'^(?P<prefix>LT|ST|P)?'
    r'(?P<width>\d{3})'
    r'/(?P<ratio>\d{2,3})'
    r'[A-Za-z]*[RrDd][Ff]?'
    r'(?P<rim>\d{2,3}(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_SEARCH = re.compile(
    r'(?P<width>\d{3})/(?P<ratio>\d{2,3})[A-Za-z]*[RrDd][Ff]?(?P<rim>\d{2,3}(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_SLASH_TRIPLET = re.compile(
    r'(?P<prefix>LT|ST|P)?'
    r'(?P<width>\d{3})'
    r'/(?P<ratio>\d{2,3})'
    r'/(?P<rim>\d{2,3}(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_FLOTATION_SLASH = re.compile(   # 31/10.50R15, 33/12.50R20LT — new in shared parser;
    r'^(?P<prefix>LT)?'                # failed in every per-supplier parser (2-digit width).
    r'(?P<width>\d{2})'                # Decimal ratio required so plain 2-digit metric widths
    r'/(?P<ratio>\d+\.\d+)'            # never match here.
    r'[Rr](?P<rim>\d{2}(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_FLOTATION = re.compile(   # 35X12.50R17LT, 33X12.50R18
    r'^(?P<prefix>LT)?'
    r'(?P<width>\d{2,3})[Xx]'
    r'(?P<ratio>\d+(?:\.\d+)?)'
    r'[Rr](?P<rim>\d{2,3}(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_ATV = re.compile(          # 26X10-12, 18.5x8.50-8, 20.5X8.0-10
    r'^(?P<width>\d+(?:\.\d+)?)[Xx]'
    r'(?P<ratio>\d+(?:\.\d+)?)'
    r'-(?P<rim>\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_NO_RATIO = re.compile(     # 11R24.5, 185R14C, 8.75R16.5LT, ST145R12
    r'^(?P<prefix>LT|ST)?'
    r'(?P<width>\d+(?:\.\d+)?)'
    r'[Rr](?P<rim>\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
_EMPTY_PARSE = {'prefix': '', 'width': '', 'ratio': '', 'rim_display': '', 'rim_tag': ''}


def _result(match):
    d = match.groupdict()
    rim = d.get('rim', '')
    return {
        'prefix':      (d.get('prefix') or '').upper(),
        'width':       d.get('width', ''),
        'ratio':       d.get('ratio', ''),
        'rim_display': rim,
        'rim_tag':     rim.replace('.', ''),
    }


def parse_tire_size(size, description=""):
    """Extract (prefix, width, ratio, rim) from a tire size string.

    Eight-layer fallback:
      1. Anchored match on the size field (standard slash/R format, R/D/RF).
      2. Search anywhere in the size field.
      3. Search inside the description field.
      4-5. Slash-triplet 285/75/16 in size, then description.
      6. Flotation format  (35X12.50R17).
      7. ATV X-dash format (26X10-12).
      8. No-ratio format   (11R24.5, 185R14C, ST145R12).
    Returns a dict with keys: prefix, width, ratio, rim_display, rim_tag
    (all '' when nothing matches — never raises).
    """
    size = str(size)
    description = str(description)
    for pat, text in (
        (_SIZE_ANCHORED,      size),
        (_SIZE_SEARCH,        size),
        (_SIZE_SEARCH,        description),
        (_SIZE_SLASH_TRIPLET, size),
        (_SIZE_SLASH_TRIPLET, description),
    ):
        m = pat.match(text) if pat is _SIZE_ANCHORED else pat.search(text)
        if m:
            return _result(m)
    for pat in (_SIZE_FLOTATION_SLASH, _SIZE_FLOTATION, _SIZE_ATV, _SIZE_NO_RATIO):
        m = pat.match(size)
        if m:
            return _result(m)
    return dict(_EMPTY_PARSE)


_FLOTATION_TAG_NO_DECIMAL = re.compile(
    r'^(?P<prefix>LT)?(?P<dia>\d{2})X(?P<width>\d{3,4})R(?P<rim>\d{2})$',
    re.IGNORECASE,
)


def costar_search_term(size):
    """Convert a widget size tag to the form Costar full-text search expects.

    Widget flotation tags store the width with the decimal stripped
    (33X1250R20), but Costar search only matches the decimal form
    (33X12.50R20) — the two queries return different results. Re-insert
    the decimal two digits from the end of the width. The LT prefix is
    dropped from the query: Costar stores it as a trailing suffix
    (33X12.50R20LT) and a leading-LT search returns nothing; the plain
    query returns both LT and non-LT rows, which the caller filters via
    normalize_costar_size(). Sizes that aren't decimal-stripped flotation
    tags (2-digit widths like 33X10R15, metric sizes) are returned
    unchanged.
    """
    m = _FLOTATION_TAG_NO_DECIMAL.match(str(size))
    if not m:
        return size
    d = m.groupdict()
    width = f"{d['width'][:-2]}.{d['width'][-2:]}"
    return f"{d['dia']}X{width}R{d['rim']}"


def normalize_costar_size(size):
    """Canonicalize a size string for widget-tag vs Costar-result comparison.

    Handles the ways Costar result sizes diverge from widget tags:
    decimals stripped in tags (12.50 -> 1250), trailing load-range tokens
    ("33X12.50R20 E"), and the LT marker stored as a suffix
    ("33X12.50R20LT") where widget tags use a prefix (LT33X1250R20).
    """
    s = str(size).upper().replace('.', '').strip()
    s = s.split()[0] if s.split() else ''
    if s.endswith('LT') and not s.startswith(('LT', 'ST', 'P')):
        s = 'LT' + s[:-2]
    return s


def parse_load_speed(description):
    """Return (load_index, speed_rating) from a description string."""
    s = str(description).upper()
    # Dual load index format: 143/141J — letter must not precede a digit (avoids 205/50R17)
    m = re.search(r'(?<!\d)(\d{2,3})/\d{2,3}([A-Z])(?!\d)', s)
    if m:
        return m.group(1), m.group(2)
    # Standard formats: 94V, 93 V, 94(Y), 94(Y) XL — load index 60-129
    m = re.search(r'(?<!\d)([6-9]\d|1[0-2]\d)\s*\(?([A-Z]{1,2})\)?(?!\d)', s)
    if m:
        return m.group(1), m.group(2)
    return "", ""


_ZERO_PLACEHOLDER = re.compile(r'^0+(?:\.0+)?$')


def tidy_load_index(value):
    """'95.0000' -> '95'. Anything non-numeric is returned unchanged."""
    s = str(value or "").strip()
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s
    return str(int(f)) if f == int(f) else str(f)


def coerce_load_index(spec_value, *fallback_texts):
    """Return a usable load index: the spec column if it holds one, else the
    first fallback text that parses.

    Two things make the obvious `str(spec).strip() or parse_load_speed(d)[0]`
    wrong on real supplier data, and both were shipping to the storefront:

    * Costar records an UNKNOWN numeric spec as "0.0000" rather than leaving
      the cell blank. That string is truthy, so the `or` fallback never runs
      and the placeholder ships — 2,433 North Country rows read
      "Load index: 0.0000" while the real index sat in their own description
      ("Trail Blade X/T SxS - 70N 8PR").
    * Genuine values arrive as "95.0000" too, and a product page should say 95.

    Suppliers keep the load index in different places, so pass whichever texts
    that supplier has (description, service description, ...); the first that
    yields an index wins. Returns "" when none do — an honest blank rather
    than a number nobody can source.
    """
    val = str(spec_value or "").strip()
    if _ZERO_PLACEHOLDER.match(val):
        val = ""
    if val:
        return tidy_load_index(val)
    for text in fallback_texts:
        parsed = parse_load_speed(str(text))[0]
        if parsed:
            return tidy_load_index(parsed)
    return ""


# A plain passenger/euro-metric size: 245/60R18, P225/65R17, 285/45RF21
# (RF = run-flat) and 205/55ZR16 (Z speed symbol ahead of the construction
# letter — a performance passenger tire, not a truck one).
#
# Deliberately excludes:
#   LT265/70R17   light-truck        — rated by letter load range (C/D/E/…)
#   ST175/80R13   special trailer    — likewise
#   33X12.50R20   flotation          — likewise
#   195/75R16C    commercial van     — the trailing C is a construction type
#                                      that maps to load range C *or* D
#                                      depending on ply, so it is not safe to
#                                      infer; load capacity is a safety spec.
# A missing letter on any of those is genuinely unknown, and stays blank.
_PASSENGER_METRIC = re.compile(
    r'^\s*P?\d{3}/\d{2,3}[A-Z]{0,2}R[Ff]?\d{2}(?:\.\d+)?\s*$', re.IGNORECASE)

# Formula's feed writes the same size as 185-65-15. normalize_size() converts
# it, but not until after load range has already been resolved — so match the
# dashed form here rather than depend on statement order inside one supplier's
# augment script. The LT/ST exclusions hold either way (the ^P?\d{3} anchor
# rejects a leading LT).
_PASSENGER_DASHED = re.compile(r'^\s*P?\d{3}-\d{2,3}-\d{2}(?:\.\d+)?\s*$', re.IGNORECASE)


def default_passenger_load_range(size):
    """'SL' for a passenger tire with no load range marking, else ''.

    Passenger tires carry exactly two load ratings: Standard Load and Extra
    Load. Suppliers mark XL explicitly because it is the exception, and leave
    the cell empty for SL because it is the default — so an empty cell on a
    passenger size is not missing information, it *is* the standard rating.

    dttire's feed states it outright (3,317 rows arrive already marked "SL"),
    which is how the convention was confirmed rather than assumed. North
    Country, Emerge and Formula supply only the XL side, leaving 3,247
    standard-load passenger tires blank on the storefront while an identical
    tire from dttire correctly reads "SL".

    Callers must apply this LAST, after every real source of a load range has
    come up empty — it is a default, not a parse.
    """
    s = str(size or "")
    return "SL" if (_PASSENGER_METRIC.match(s) or _PASSENGER_DASHED.match(s)) else ""


def parse_wheel_size(size_str):
    """Parse size string → (diameter: int, width: float|None). Returns (None, None) on failure.

    Handles:
      '20X9', '17X7.5'            — standard DxW
      '22.5X8.25'                  — decimal diameter
      '8.25X22.5'                  — reversed truck wheel (width first)
      '17 5-114.3 (7.5)'           — NC portal format with width
      '15 4-100/4-114.3 (7)'       — NC dual-bolt with width
      '16 5-130 (6,5)'             — NC with comma decimal separator
      '17 8-165.1'                 — NC format without width
      '15 5on4.5'                  — NC imperial bolt pattern notation
      '22.5"', '15" 6 HOLE'        — diameter with quote
      '15 x 8 5 hole'              — space-separated DxW
    """
    s = str(size_str).strip()
    if not s:
        return None, None

    # Normalize comma decimal separator (European: '7,5' → '7.5')
    s = re.sub(r'(\d),(\d)', r'\1.\2', s)

    # DxW with optional decimal diameter: '20X9', '22.5X8.25', '8.25X22.5'
    m = re.match(r'^(\d+\.?\d*)\s*[Xx]\s*(\d+\.?\d*)', s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        # Reversed truck wheel: width listed before diameter (e.g. 8.25X22.5)
        if a < 12 and b > a:
            return int(round(b)), a
        return int(round(a)), b

    # NC portal format with parenthetical width: '17 5-114.3 (7.5)' / '16 5-130 (6.5)'
    m = re.match(r'^(\d+\.?\d*)\s+\S.+\((\d+\.?\d*)\)\s*$', s)
    if m:
        return int(float(m.group(1))), float(m.group(2))

    # NC format without width: '17 8-165.1', '15 5on4.5'
    m = re.match(r'^(\d+\.?\d*)\s+\d+[-xXon]', s, re.IGNORECASE)
    if m:
        return int(float(m.group(1))), None

    # Diameter with quote: '22.5"', '15" 6 HOLE'
    m = re.match(r'^(\d+\.?\d*)"', s)
    if m:
        return int(round(float(m.group(1)))), None

    # Space-separated: '15 x 8 5 hole', '14.5 x 6'
    m = re.match(r'^(\d+\.?\d*)\s+[xX]\s+(\d+\.?\d*)', s)
    if m:
        return int(round(float(m.group(1)))), float(m.group(2))

    return None, None


def format_wheel_size(size_str):
    """Normalize to dot-free integer tenths: '20X9' → '20X90', '17X7.5' → '17X75'.
    If width is unavailable, returns just the diameter (e.g. '17').
    """
    d, w = parse_wheel_size(size_str)
    if d is None:
        return str(size_str).strip()
    if w is None:
        return str(d)
    return f"{d}X{int(round(w * 10))}"
