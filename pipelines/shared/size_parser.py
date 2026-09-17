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


# Real speed symbols only: B C D E F G J K L M N P Q R S T U H V W Y Z. Deliberately
# excludes A/I/O/X (never used as a speed symbol) and is exactly one character —
# on a curated description field a generic [A-Z]{1,2} rarely misfires, but this
# function is also used to parse raw SERP search-result text (enrichment.py's
# specs field), where stray nearby text is common. [A-Z]{1,2} against that text
# once matched "MP" out of "...112 MPH...", writing speed_rating="MP" to a
# storefront. Restricting to the real symbol set makes that class of match
# impossible rather than merely less likely.
_SPEED_SYMBOL = "BCDEFGHJKLMNPQRSTUVWYZ"


def parse_load_speed(description):
    """Return (load_index, speed_rating) from a description string."""
    s = str(description).upper()
    # Dual load index format: 143/141J. The trailing (?![A-Z0-9]) is why this
    # doesn't fire on a bare size string: "225/60ZR18" has the same digit/digit
    # +letter shape (225, 60, Z), and Z looks like a plausible speed symbol on
    # its own — the guard used to be (?!\d), which blocks "205/50R17" (the "R"
    # it captures is followed by a rim-diameter digit) but not "225/60ZR18"
    # (the "Z" it captures is followed by another LETTER, "R", not a digit).
    # Rejecting a letter immediately after the match closes that gap: nothing
    # in a genuine dual-rating token ("143/141J", "143/141J XL") is ever
    # followed directly by another letter.
    m = re.search(rf'(?<!\d)(\d{{2,3}})/\d{{2,3}}([{_SPEED_SYMBOL}])(?![A-Z0-9])', s)
    if m:
        return m.group(1), m.group(2)
    # Standard formats: 94V, 93 V, 94(Y), 94(Y) XL — load index 60-129
    m = re.search(rf'(?<!\d)([6-9]\d|1[0-2]\d)\s*\(?([{_SPEED_SYMBOL}])\)?(?![A-Z0-9])', s)
    if m:
        return m.group(1), m.group(2)
    return "", ""


_LR_PR_TO_LETTER = {
    '4': 'B', '6': 'C', '8': 'D', '10': 'E', '12': 'F', '14': 'G', '16': 'H',
}
_LR_PR_PATTERN = re.compile(r'\b(4|6|8|10|12|14|16)\s*-?\s*(?:PR|PLY)\b', re.IGNORECASE)


def extract_load_range(size, description=""):
    """Extract load range letter from a size string or description.

    Checks in order:
      1. Explicit letter at end of size  (e.g. 'LT225/75R16 E' -> 'E')
      2. PR notation in size field       (e.g. '6PR' or '6 PR' -> 'C')
      3. PR notation in description      (e.g. '10 PR All Season' -> 'E')
      4. XL / Extra Load keyword in description

    Identical logic to what northcountry/emergetires/formula_distributors
    each carry as a local copy in their own augment*.py — this is the one
    enrichment.py (supplier-agnostic) calls from, so it exists here too
    rather than picking one supplier's copy to import from. The per-supplier
    copies are left as-is; deduplicating them is a separate change.
    """
    size = str(size)
    m = re.search(r'\s+([B-N])\s*$', size.strip())
    if m:
        return m.group(1)
    m = _LR_PR_PATTERN.search(size)
    if m:
        return _LR_PR_TO_LETTER.get(m.group(1), '')
    m = _LR_PR_PATTERN.search(str(description))
    if m:
        return _LR_PR_TO_LETTER.get(m.group(1), '')
    if re.search(r'\bXL\b|\bExtra.?Load\b', str(description), re.IGNORECASE):
        return 'XL'
    return ''


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


# ── Consumer-tire filter: passenger (P-metric/euro-metric) + light truck only ──
# The supplier feeds carry ATV, UTV, golf-cart and heavy-commercial sizes in the
# same files as the tires the store actually sells. Client-specified rules,
# evaluated in this exact priority order:
#
#   1. Any size carrying an LT marker is kept, whatever shape it has
#      (LT265/70R17, 35X12.50R17LT, 7.50R16LT) — except a half-inch rim,
#      which is a commercial fitment, not a pickup one.
#   2. Otherwise the rim diameter decides: 8/9/10/12 is ATV, UTV or golf cart,
#      and 14 is ATV *only* when the size is written in the X/dash flotation
#      form (30X10R14) — plenty of real passenger tires are 14" metric.
#   3. What survives must look like a passenger size: a P prefix, or a
#      3-digit section width in 135-335 followed by a slash.
#   4. Trailer (ST) and commercial half-inch rims are excluded outright.
#
# Rule 4 is applied ahead of rule 3 because it is an exclusion and rule 3 is an
# accept — 275/70R22.5 matches the passenger shape but is a semi-truck tire.

# Rim diameter is the last number introduced by a size separator, which skips
# trailing load-index/speed tokens ("265/70R17 112T" → 17) and the section
# width ("25X8-12" → 12). Diameters are 1-2 digits, optionally .5, and the
# 3-digit lookahead is what keeps a load index out ("LT265/70R17 121/118S").
# R and D take an optional F: on a run-flat (285/45RF21) the construction
# letter is followed by the F, and without it the rim read as the aspect
# ratio — 45 rather than 21.
_RIM_AFTER_SEP = re.compile(r'(?:[-/X]|[RD]F?)(\d{1,2}(?:\.\d+)?)(?!\d)')

_FILTER_PASSENGER_P     = re.compile(r'^P\s*\d')     # P215/65R16
_FILTER_PASSENGER_WIDTH = re.compile(r'^(\d{3})/')   # 205/55R16


def _rim_diameter(upper_size):
    """Last separator-introduced number in an upper-cased size, or None."""
    matches = _RIM_AFTER_SEP.findall(upper_size)
    return float(matches[-1]) if matches else None


# Formula writes its sizes as 185-65-15. Its augment step rewrites that to
# 185/65R15 before the Shopify CSV is built, which left the two roads to the
# store disagreeing about 14 real passenger tires: the bundle saw the slash
# form and kept them, ingest saw the dashed form and dropped them as
# unrecognized — and rule 2 would have read a dashed 14" rim as an ATV. The
# spelling is not a different tire, so undo it before the rules run. Pattern
# copied from formula_distributors/augment.py normalize_size(); the two must
# not drift.
_DASHED_SIZE = re.compile(r'^(LT|ST|P)?(\d+)-(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$')


def _undash(upper_size):
    """'185-65-15' → '185/65R15'. Anything else is returned unchanged."""
    m = _DASHED_SIZE.match(upper_size)
    return f"{m.group(1) or ''}{m.group(2)}/{m.group(3)}R{m.group(4)}" if m else upper_size


def is_consumer_tire_size(size):
    """True for a passenger or light-truck size, False for ATV/UTV/golf/commercial.

    Deliberately strict: a size this cannot recognize is dropped rather than
    listed, because an ATV tire sold as a passenger tire is a safety problem
    while a missing SKU is a revenue one. Takes a size in any spelling the
    feeds use — slash, dashed or the decimal-free tag form once
    size_from_tags() has read it — so that the same tire gets the same verdict
    whichever layer asks.
    """
    s = _undash(str(size or "").strip().upper())
    if not s:
        return False

    rim = _rim_diameter(s)
    # A half-inch rim is a commercial fitment — 16.5, 17.5, 19.5, 22.5, 24.5 —
    # and nothing a passenger car or a pickup wears. The rules name it twice:
    # as the exception to the LT keep, and as "commercial rim sizes like 22.5
    # or 24.5". Reading it as one test rather than two is what makes those
    # agree: taken literally, 8.75R16.5LT was excluded while 225/70R19.5, a
    # medium-duty truck tire, was kept — 57 such rows across the current feeds.
    commercial_rim = (rim is not None and rim % 1 == 0.5) or "22.5" in s or "24.5" in s

    # 1. LT force-keep, minus those commercial rims.
    if "LT" in s:
        return not commercial_rim

    # 2. ATV / UTV / golf-cart rim boundary.
    if rim is not None:
        if rim in (8, 9, 10, 12):
            return False
        if rim == 14 and ("X" in s or "-" in s):
            return False

    # 4. Trailer and heavy-commercial exclusions (before the rule-3 accept).
    if s.startswith("ST") or commercial_rim:
        return False

    # 3. Standard passenger shapes.
    if _FILTER_PASSENGER_P.match(s):
        return True
    m = _FILTER_PASSENGER_WIDTH.match(s)
    return bool(m and 135 <= int(m.group(1)) <= 335)


# A filter that reads the wrong column drops everything, and "everything" does
# not look like an error downstream — it looks like a supplier that stopped
# carrying tires, which the sync answers by zeroing that supplier's inventory
# in the live store. So the whole-feed decision is a separate call that refuses
# rather than proceeds, and refuses OPEN: listing an ATV tire for another week
# is recoverable, zeroing thousands of real products is not.
NON_CONSUMER_DROP_LIMIT_PCT = 25.0   # worst real feed today is 6.3% (formula)
# A share is only evidence on a feed big enough to have one. Under this many
# rows the proportion says nothing — a 4-row file with two ATV tires in it is
# 50% and perfectly correct — so small inputs are filtered on their merits.
# 50 matches config.MIN_ROWS_PER_SUPPLIER, this repo's existing threshold for
# "a tire feed this short is a broken scrape, not a small supplier".
NON_CONSUMER_GUARD_MIN_ROWS = 50


def consumer_size_filter(sizes, limit_pct=NON_CONSUMER_DROP_LIMIT_PCT,
                         min_rows=NON_CONSUMER_GUARD_MIN_ROWS):
    """Apply is_consumer_tire_size across one feed.

    Returns (keep_flags, dropped_sizes, refusal) — `refusal` is '' when the
    result is usable, and otherwise explains why the filter declined to act,
    in which case keep_flags is all-True and the caller must log loudly rather
    than treat the feed as clean.
    """
    sizes = list(sizes)
    keep = [is_consumer_tire_size(s) for s in sizes]
    dropped = sorted({str(s).strip() for s, k in zip(sizes, keep) if not k})
    n, n_dropped = len(keep), keep.count(False)
    if n >= min_rows and n_dropped * 100.0 / n > limit_pct:
        return ([True] * n, dropped,
                f"would drop {n_dropped} of {n} rows ({n_dropped * 100.0 / n:.1f}%), "
                f"over the {limit_pct}% limit — the size column is probably not the "
                f"one this feed keeps sizes in; feed passed through UNFILTERED")
    return keep, dropped, ""


# The Shopify import CSVs carry no size column; the size lives in a structured
# tag the augment step writes ("Brand_Atturo,Size_28X10R14,Width_28,…"), in the
# decimal-stripped widget spelling (33X1250R20 for 33X12.50R20).
_SIZE_TAG = re.compile(r'(?:^|,)\s*Size_([^,]*)')


# …and that spelling is lossy in the one way these rules turn on. A half-inch
# rim loses its point (255/70R22.5 → Size_255/70R225), which defeats both the
# literal "22.5" exclusion and the rim parse, so 33 North Country semi-truck
# tires read as ordinary 255-section passenger tires and were bundled. No tire
# has a 3-digit rim diameter — the largest made is 30 — so a 3-digit run after
# the construction letter is always a stripped half-inch. Restoring it here,
# at the point the tag is read, keeps is_consumer_tire_size taking one kind of
# input: a size as a human would write it.
_TAG_STRIPPED_RIM = re.compile(r'(?<=[RD])(\d{2})(\d)(?!\d)')


def size_from_tags(tags):
    """The Size_ tag out of a Shopify import row's Tags cell, or ''.

    Returned in ordinary size spelling, not the tag's decimal-free one.
    """
    m = _SIZE_TAG.search(str(tags or ""))
    if not m:
        return ""
    return _TAG_STRIPPED_RIM.sub(lambda hit: hit.group(1) + "." + hit.group(2),
                                 m.group(1).strip())
