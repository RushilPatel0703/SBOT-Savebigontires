"""Tests for is_consumer_tire_size (shared/size_parser.py).

The filter decides what reaches the storefront, so the cases worth pinning are
the boundaries the client wrote the rules around: an LT marker outranking an
otherwise-ATV-looking flotation size, the 14" rim that is ATV in the X/dash
form and passenger in the metric one, and the half-inch rims that separate a
pickup fitment from a commercial one.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from size_parser import is_consumer_tire_size  # noqa: E402


@pytest.mark.parametrize("size", [
    # 1. LT force-keep, in every spelling the feeds use
    "LT33X12.50R15", "LT265/70R17", "35X12.50R17LT", "7.50R16LT",
    "lt245/75r16",                      # feeds are not consistently cased
    # 3. passenger shapes
    "P215/65R16", "205/55R16", "135/80R13", "335/25R22",
    "265/70R17 112T",                   # trailing load index / speed symbol
    "195/65R14",                        # 14" metric is passenger, not ATV
])
def test_keeps(size):
    assert is_consumer_tire_size(size) is True


@pytest.mark.parametrize("size", [
    # 2. ATV / UTV / golf cart by rim diameter
    "25X8-12", "18X8.50-8", "205/50-10", "23X10.50-9",
    "30X10R14", "215/35-12",            # 14" only when written X/dash
    # 1-exception + 4. commercial half-inch rims
    "11R22.5", "285/75R24.5", "8.75R16.5LT",
    # 4. trailer
    "ST205/75R14", "ST145R12",
    # 3. nothing recognizable survives
    "7.50R16", "120/70R17", "400/60R15.5", "", None,
])
def test_discards(size):
    assert is_consumer_tire_size(size) is False


# ── ingest wiring ─────────────────────────────────────────────────────────────
# The filter is only worth anything if it actually reaches every tire feed.
# WheelMart is the one ingested from its raw file, and the one most likely to
# carry ATV stock — so the column-resolution fallback is covered here.

PRODUCTION = Path(__file__).resolve().parent.parent.parent / "production"


@pytest.fixture
def ingest(monkeypatch):
    """production/steps/ingest.py, imported against production's own config.

    The repo has several modules named `config` (one per pipeline plus
    production's). Whichever ran first is already in sys.modules by the time
    the full suite reaches this file, and ingest needs production's
    PIPELINES_DIR — so drop the cached ones and let the prepended path decide.
    """
    import importlib
    monkeypatch.syspath_prepend(str(PRODUCTION))
    for name in [n for n in sys.modules if n == "config" or n.startswith("steps")]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module("steps.ingest")


def test_size_column_preferred(ingest):
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"size": ["205/55R16"], "description": ["25X8-12 ATV"]})
    assert ingest.filter_size_series(df).tolist() == ["205/55R16"]


def test_description_fallback_takes_leading_token_only(ingest):
    """WheelMart's raw feed has no 'size'; its size_raw is a digit blob."""
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({
        "size_raw":    ["2657017121118-10", "235451898"],
        "description": ["LT265/70R17 121/118S - 10PR POWERHUB PATH RANGER A/T",
                        "235/45R18 98VXL LANVIGATOR WINTERGRIP UHP SNOW"],
    })
    assert ingest.filter_size_series(df).tolist() == ["LT265/70R17", "235/45R18"]


def test_description_fallback_does_not_scan_free_text(ingest):
    """A model name carrying 'LT' (XLT, BOLT) must not read as a light-truck marker."""
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"description": ["25X8-12 SUPERGRIP XLT ATV"]})
    sizes = ingest.filter_size_series(df)
    assert sizes.tolist() == ["25X8-12"]
    assert is_consumer_tire_size(sizes[0]) is False


def test_no_usable_column_returns_none(ingest):
    pd = pytest.importorskip("pandas")
    assert ingest.filter_size_series(pd.DataFrame({"sku": ["X1"]})) is None


# ── rim extraction ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("size,rim", [
    ("285/45RF21", 21.0),              # run-flat: the F sits between R and the rim
    ("225/50RF17", 17.0),
    ("205/55ZR16", 16.0),              # Z speed symbol ahead of the construction letter
    ("265/70R17 112T", 17.0),          # trailing load index / speed symbol
    ("LT265/70R17 121/118S", 17.0),    # dual load index, slash-separated
    ("33X12.50R20 E", 20.0),           # load range as a separate token
    ("25X8-12", 12.0),                 # X/dash: rim is the last number, not the width
    ("8.75R16.5LT", 16.5),
    ("185-65-15", 15.0),               # Formula's dashed spelling
    ("33X1250R20", 20.0),              # decimal-stripped widget/tag spelling
    ("5X114.3", None),                 # a bolt pattern is not a rim diameter
])
def test_rim_diameter(size, rim):
    from size_parser import _rim_diameter
    assert _rim_diameter(size.upper()) == rim


# ── whole-feed guard ──────────────────────────────────────────────────────────
# A filter pointed at the wrong column drops every row, and an empty feed does
# not read as an error downstream — merge sees a supplier that stopped carrying
# tires and sync answers by zeroing that supplier's stock in the live store.

def test_feed_filter_normal_case_applies():
    from size_parser import consumer_size_filter
    keep, dropped, refusal = consumer_size_filter(
        ["205/55R16", "P225/65R17", "LT265/70R17", "25X8-12"])
    assert refusal == ""
    assert keep == [True, True, True, False]
    assert dropped == ["25X8-12"]


def test_feed_filter_refuses_open_when_it_would_gut_the_feed():
    from size_parser import consumer_size_filter
    sizes = ["2355018101"] * 100          # WheelMart's size_raw digit blob
    keep, _dropped, refusal = consumer_size_filter(sizes)
    assert refusal, "a 100% drop must refuse, not proceed"
    assert "UNFILTERED" in refusal
    assert all(keep), "refusal must fail open — an unfiltered feed beats an empty one"


def test_feed_filter_limit_is_a_boundary_not_a_cliff():
    from size_parser import consumer_size_filter
    # exactly at the limit still applies; over it refuses
    at_limit = ["205/55R16"] * 75 + ["25X8-12"] * 25
    assert consumer_size_filter(at_limit, limit_pct=25.0)[2] == ""
    over = ["205/55R16"] * 74 + ["25X8-12"] * 26
    assert consumer_size_filter(over, limit_pct=25.0)[2] != ""


def test_small_input_is_filtered_on_merit_not_on_share():
    """Four rows, two of them ATV, is 50% — and entirely correct."""
    from size_parser import consumer_size_filter
    keep, _dropped, refusal = consumer_size_filter(
        ["28X10R14", "205/55R16", "LT265/70R17", "18X850-8"])
    assert refusal == ""
    assert keep == [False, True, True, False]


def test_empty_feed_does_not_divide_by_zero():
    from size_parser import consumer_size_filter
    assert consumer_size_filter([]) == ([], [], "")


# ── Shopify import files (the dashboard's manual bundle) ──────────────────────

def test_size_from_tags():
    from size_parser import size_from_tags
    assert size_from_tags("Brand_Atturo,Size_28X10R14,Width_28,RimDia_14") == "28X10R14"
    assert size_from_tags("Brand_Kenda,Size_205/55R16,Ratio_55") == "205/55R16"
    assert size_from_tags("Brand_Kenda,Width_205") == ""
    assert size_from_tags("") == ""
    # Oversize_ must not be mistaken for the Size_ tag
    assert size_from_tags("Brand_X,Oversize_99,Size_205/55R16") == "205/55R16"


@pytest.fixture
def bundle(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "shared"))
    import importlib
    return importlib.import_module("upload_bundle")


def test_bundle_filters_tires_files(bundle):
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame({
        "Variant SKU": ["A", "B", "C", "D"],
        "Title": ["Atturo 28X10R14 Trail Blade X/T SxS",
                  "Kenda 205/55R16 Vezda",
                  "Toyo LT265/70R17 Open Country",
                  "Carlisle 18X8.50-8 Turf Saver"],
        "Tags": ["Brand_Atturo,Size_28X10R14", "Brand_Kenda,Size_205/55R16",
                 "Brand_Toyo,Size_LT265/70R17", "Brand_Carlisle,Size_18X850-8"],
    })
    kept, dropped, refusal = bundle.filter_consumer_tires(
        frame, "Shopify_SEO_northcountry_tires_20260910_222414.csv")
    assert refusal == ""
    assert dropped == 2
    assert kept["Variant SKU"].tolist() == ["B", "C"]


def test_bundle_leaves_wheels_files_alone(bundle):
    """A wheel's size (20X9) reads as an ATV rim; the type token is the guard."""
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame({"Tags": ["Brand_Fuel,Size_20X9", "Brand_Fuel,Size_17X8"]})
    for name in ("Shopify_SEO_dttire_wheels_20260910_150725.csv",
                 "Shopify_SEO_wheelsmart_rims_20260910_132738.csv"):
        kept, dropped, refusal = bundle.filter_consumer_tires(frame, name)
        assert (len(kept), dropped, refusal) == (2, 0, "")


def test_bundle_passes_through_when_tags_are_missing(bundle):
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame({"Variant SKU": ["A", "B"]})
    kept, dropped, refusal = bundle.filter_consumer_tires(
        frame, "Shopify_SEO_tires_20260910_134452.csv")
    assert (len(kept), dropped) == (2, 0)
    assert "UNFILTERED" in refusal


def test_bundle_refuses_rather_than_empties_a_file(bundle):
    """If the Size_ tag stops being written, the bundle must not become empty."""
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame({"Tags": ["Brand_Kenda,Width_205"] * 50})
    kept, dropped, refusal = bundle.filter_consumer_tires(
        frame, "Shopify_SEO_tires_20260910_134452.csv")
    assert (len(kept), dropped) == (50, 0)
    assert "UNFILTERED" in refusal


@pytest.mark.parametrize("name,expected", [
    ("Shopify_SEO_dttire_tires_20260910_150352.csv", "tires"),
    ("Shopify_SEO_tires_20260910_134452.csv", "tires"),          # formula: no supplier token
    ("Shopify_SEO_wheelsmart_rims_20260910_132738.csv", "wheels"),
    ("Shopify_SEO_northcountry_wheels_20260910_191213.csv", "wheels"),
    ("something_else.csv", ""),
])
def test_bundle_source_product_type(bundle, name, expected):
    assert bundle.source_product_type(name) == expected


# ── coverage: the filter has to actually reach every tire feed ────────────────
# WheelMart's raw feed names its column size_raw, not size, so the first cut of
# this filter skipped that supplier entirely and said nothing. That is the
# failure mode worth a standing test: not "is the rule right" but "did the rule
# run". Runs against whatever feeds are on disk, and skips when there are none.

def test_every_configured_tire_feed_resolves_a_usable_size(ingest):
    pd = pytest.importorskip("pandas")
    import config
    from size_parser import consumer_size_filter

    checked = []
    for supplier, cfg in config.SUPPLIERS.items():
        if "tires" not in cfg["types"]:
            continue
        file_type = cfg.get("type_alias", {}).get("tires", "tires")
        path = ingest.find_latest(config.PIPELINES_DIR / cfg["pattern"].format(type=file_type))
        if not path:
            continue
        df = pd.read_csv(path, dtype=str, low_memory=False).fillna("")
        sizes = ingest.filter_size_series(df)
        assert sizes is not None, f"{supplier}: no size column — feed would go unfiltered"
        refusal = consumer_size_filter(sizes)[2]
        assert not refusal, f"{supplier}: {refusal}"
        checked.append(supplier)

    if not checked:
        pytest.skip("no supplier feeds on disk")
    assert checked, "no tire feed was actually exercised"


# ── half-inch rims are commercial, wherever they appear ───────────────────────
# The written rules name this twice and disagree with themselves: it is the
# exception to the LT keep, and separately "commercial rim sizes like 22.5 or
# 24.5". Read literally, 225/70R19.5 — a medium-duty truck tire — is kept.

@pytest.mark.parametrize("size", [
    "225/70R19.5", "245/70R19.5", "215/75R17.5", "235/75R17.5",   # non-LT
    "255/70R22.5", "295/75R22.5", "285/75R24.5",
    "8.75R16.5LT", "LT8.75R16.5",                                  # LT side
])
def test_half_inch_rim_is_commercial(size):
    assert is_consumer_tire_size(size) is False


def test_tag_spelling_restores_the_stripped_rim_decimal():
    """Size_255/70R225 is 22.5 — no tire has a 3-digit rim."""
    from size_parser import size_from_tags
    assert size_from_tags("Brand_X,Size_255/70R225") == "255/70R22.5"
    assert size_from_tags("Brand_X,Size_225/70R195") == "225/70R19.5"
    assert is_consumer_tire_size(size_from_tags("Brand_X,Size_255/70R225")) is False


@pytest.mark.parametrize("tag,expected", [
    ("Size_205/55R16",  "205/55R16"),     # 2-digit rim untouched
    ("Size_LT265/70R17", "LT265/70R17"),
    ("Size_33X1250R20", "33X1250R20"),    # the X-form width is not a rim
    ("Size_205X80-10",  "205X80-10"),     # no construction letter, nothing to restore
    ("Size_255/70R22.5", "255/70R22.5"),  # already correct, must stay correct
])
def test_tag_restore_leaves_everything_else_alone(tag, expected):
    from size_parser import size_from_tags
    assert size_from_tags("Brand_X," + tag) == expected


# ── Formula's dashed spelling ─────────────────────────────────────────────────
# The same tire reached the store by two roads and got two verdicts: the
# bundle is built from augment output (185/65R15, kept) and ingest from the
# cleaned CSV (185-65-15, dropped). The spelling is not a different tire.

@pytest.mark.parametrize("dashed,slashed", [
    ("185-65-15",   "185/65R15"),
    ("305-50-20",   "305/50R20"),
    ("LT265-70-17", "LT265/70R17"),
    ("ST205-75-15", "ST205/75R15"),
    ("LT35-12.50-20", "LT35/12.50R20"),
    ("25X8-12",     "25X8-12"),      # ATV X/dash is a different shape, untouched
    ("205/55R16",   "205/55R16"),
])
def test_undash_matches_formula_augment(dashed, slashed):
    from size_parser import _undash
    assert _undash(dashed.upper()) == slashed.upper()


@pytest.mark.parametrize("size", ["185-65-15", "205-60-16", "215-55-17", "305-50-20"])
def test_dashed_passenger_is_kept(size):
    assert is_consumer_tire_size(size) is True


@pytest.mark.parametrize("size", ["ST205-75-15", "22-11-8", "25-8-12"])
def test_dashed_does_not_smuggle_in_trailer_or_atv(size):
    assert is_consumer_tire_size(size) is False


def test_both_roads_agree_on_the_same_tire():
    """The bundle reads a Size_ tag, ingest reads the cleaned CSV column."""
    from size_parser import size_from_tags
    for cleaned, tags in [("185-65-15",      "Brand_X,Size_185/65R15"),
                          ("255/70R22.5 H",  "Brand_X,Size_255/70R225"),
                          ("35X12.50R20 E",  "Brand_X,Size_35X1250R20"),
                          ("LT265/70R17",    "Brand_X,Size_LT265/70R17")]:
        assert is_consumer_tire_size(cleaned) == is_consumer_tire_size(size_from_tags(tags)), cleaned
