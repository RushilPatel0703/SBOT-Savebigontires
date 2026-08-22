"""
Guards on the enrichment driver (pipelines/shared/enrich_runner.py).

This is the one button in the dashboard that spends the client's money, so the
tests here are weighted toward the things that must NOT happen: no search
without an explicit budget, no search while the kill switch is set, no search
for a row whose answer has nowhere to land, and no silent failure to rebuild
the Shopify CSV once a fix has been applied.

Every test stubs enrichment.run_live and subprocess.run, so nothing in this
file can make a network call or launch a pipeline.
"""

import json
import sys
from pathlib import Path

import pytest

_PIPELINES_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_PIPELINES_DIR / "shared"))

import enrich_runner as R  # noqa: E402


@pytest.fixture()
def no_network(monkeypatch):
    """Record calls to run_live instead of making them. Returns the record."""
    calls = []

    def _fake_run_live(fields, supplier, budget, **kw):
        calls.append(dict(fields=list(fields), supplier=supplier, budget=budget, **kw))

    monkeypatch.setattr(R.E, "run_live", _fake_run_live)
    return calls


@pytest.fixture()
def no_augment(monkeypatch):
    """Record augment invocations instead of running them. Returns the record."""
    runs = []

    class _Result:
        returncode = 0

    def _fake_run(cmd, cwd, env):
        runs.append((Path(cwd).name, cmd[-1]))
        return _Result()

    monkeypatch.setattr(R.subprocess, "run", _fake_run)
    return runs


@pytest.fixture()
def quiet_io(monkeypatch, tmp_path):
    """Keep scan/result JSON writes inside tmp_path."""
    monkeypatch.setattr(R, "SCAN_FILE",   str(tmp_path / ".enrich_scan.json"))
    monkeypatch.setattr(R, "RESULT_FILE", str(tmp_path / ".enrich_result.json"))
    return tmp_path


def _stub_apply(monkeypatch, files_written=(), fields=("image",)):
    """Make apply_cached/apply_spec_cache report a given set of rewritten files
    without touching disk. Both are stubbed together — a test that approves a
    specs budget without this would otherwise hit real disk I/O through
    apply_spec_cache, which _apply_and_rebuild calls separately from apply_cached.
    """
    def _fake(fields_arg, supplier, write):
        return {f: dict(total_missing=0, cache_hits=len(files_written), cache_hits_empty=0,
                        still_needs_paid=0, files_written=list(files_written))
                for f in fields_arg}
    monkeypatch.setattr(R.E, "apply_cached", _fake)
    monkeypatch.setattr(R.E, "apply_spec_cache",
                        lambda supplier, write: dict(
                            total_missing=0, cache_hits=len(files_written), cache_hits_partial=0,
                            still_needs_paid=0, files_written=list(files_written)))


# ── Spend gates ───────────────────────────────────────────────────────────────

def test_run_refuses_without_a_budget(no_network, quiet_io):
    """No budget is not 'unlimited' — it is a refusal."""
    assert R.run(image_budget=0, designation_budget=0) == 1
    assert no_network == [], "a search was attempted with nothing approved"


def test_run_refuses_negative_budget(no_network, quiet_io):
    assert R.run(image_budget=-50, designation_budget=-1) == 1
    assert no_network == []


def test_kill_switch_blocks_the_live_run(no_network, quiet_io, monkeypatch, tmp_path):
    switch = tmp_path / "SERP_KILL_SWITCH"
    switch.write_text("", encoding="utf-8")
    monkeypatch.setattr(R.E, "KILL_SWITCH", str(switch))

    assert R.run(image_budget=500, designation_budget=500) == 1
    assert no_network == [], "kill switch did not stop the search"


def test_budgets_stay_in_their_own_field(no_network, no_augment, quiet_io, monkeypatch):
    """A pooled budget would let the image queue swallow designation's share.

    Each field must be searched separately with its own cap.
    """
    _stub_apply(monkeypatch)
    R.run(image_budget=40, designation_budget=3)

    assert [c["fields"] for c in no_network] == [["image"], ["designation"]]
    assert [c["budget"] for c in no_network] == [40, 3]


def test_unapproved_field_is_never_searched(no_network, no_augment, quiet_io, monkeypatch):
    _stub_apply(monkeypatch)
    R.run(image_budget=10, designation_budget=0)

    assert [c["fields"] for c in no_network] == [["image"]]


def test_unapproved_field_is_never_applied(no_network, no_augment, quiet_io, monkeypatch):
    """Approving images must not quietly rewrite designation columns too."""
    applied = []
    monkeypatch.setattr(R.E, "apply_cached",
                        lambda fields, supplier, write: (applied.extend(fields), {})[1])
    R.run(image_budget=10, designation_budget=0)

    assert applied == ["image"]


def test_paid_results_are_still_applied_when_a_search_dies_midway(
        no_augment, quiet_io, monkeypatch):
    """run_live() exits on a missing key. Whatever was already bought is cached,
    so the apply phase must still run rather than stranding it."""
    def _boom(*a, **kw):
        raise SystemExit(1)
    monkeypatch.setattr(R.E, "run_live", _boom)

    applied = []
    monkeypatch.setattr(R.E, "apply_cached",
                        lambda fields, supplier, write: (applied.extend(fields), {})[1])

    R.run(image_budget=10, designation_budget=0)
    assert applied == ["image"], "a mid-run exit stranded results that were paid for"


# ── Rebuild wiring ────────────────────────────────────────────────────────────

def test_every_source_maps_to_a_real_augment_step():
    """A fix written into a cleaned CSV only reaches Shopify if that supplier's
    augment step re-runs. An unmapped source silently drops the fix."""
    import enrichment as E

    unmapped = {(s["supplier"], s["type"]) for s in E.SOURCES
                if (s["supplier"], s["type"]) not in R.AUGMENT_STEPS}
    assert not unmapped, f"no augment step mapped for {sorted(unmapped)}"


def test_augment_step_names_exist_in_their_run_pipeline():
    """Catches a typo or a rename on the run_pipeline.py side."""
    import re

    missing = []
    for (supplier, ptype), step in R.AUGMENT_STEPS.items():
        src   = (_PIPELINES_DIR / supplier / "run_pipeline.py").read_text(encoding="utf-8")
        names = re.findall(r'\("([a-z_]+)",\s+"[a-z_./]+\.py"', src)
        if step not in names:
            missing.append((supplier, ptype, step))
    assert not missing, f"step name not in that supplier's STEPS: {missing}"


def test_only_changed_suppliers_are_rebuilt(no_augment, quiet_io, monkeypatch):
    """Re-running augment for a supplier whose wheels were never scraped this
    cycle fails on a missing input — so rebuild only what actually changed."""
    sources = R._source_files()
    if not sources:
        pytest.skip("no supplier CSVs on disk to exercise the mapping against")

    target, target_file = sorted(sources.items())[0]
    _stub_apply(monkeypatch, files_written=[target_file])

    R._apply_and_rebuild(["image"])

    assert no_augment == [(target[0], R.AUGMENT_STEPS[target])]


def test_nothing_changed_means_no_rebuild(no_augment, quiet_io, monkeypatch):
    _stub_apply(monkeypatch, files_written=[])
    stats, failures = R._apply_and_rebuild(["image"])

    assert no_augment == []
    assert failures == []


def test_a_failed_augment_is_reported_not_swallowed(quiet_io, monkeypatch):
    """A rebuild that fails leaves the Shopify CSV without the fix. The run has
    to end non-zero so the dashboard shows it red instead of green."""
    sources = R._source_files()
    if not sources:
        pytest.skip("no supplier CSVs on disk to exercise the mapping against")

    target, target_file = sorted(sources.items())[0]
    _stub_apply(monkeypatch, files_written=[target_file])

    class _Failed:
        returncode = 3
    monkeypatch.setattr(R.subprocess, "run", lambda cmd, cwd, env: _Failed())

    stats, failures = R._apply_and_rebuild(["image"])
    assert failures, "a failed augment was reported as success"

    monkeypatch.setattr(R, "scan", lambda: None)
    assert R._finish("run", stats, failures, approved=1) == 1


# ── Scan output ───────────────────────────────────────────────────────────────

def test_scan_makes_no_network_call_and_writes_json(quiet_io, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("scan must never search")
    monkeypatch.setattr(R.E, "run_live", _boom)

    payload = R.scan()
    written = json.loads(Path(R.SCAN_FILE).read_text(encoding="utf-8"))

    assert written == payload
    assert set(payload["fields"]) == set(R.E.SUPPORTED_FIELDS)
    assert payload["total_needs_paid"] >= 0


def test_scan_excludes_report_only_rows_from_the_approvable_total(quiet_io, monkeypatch):
    """A row in a file with no column to write into can never ship an answer,
    so it must not appear in the number someone is asked to approve."""
    monkeypatch.setattr(R.E, "per_source_breakdown",
                        lambda field, sup=None, quality_cache=None: {
        ("acme", "tires"): dict(missing=10, cache_hit=1, needs_paid=6, writable=True),
        ("acme", "rims"):  dict(missing=4,  cache_hit=0, needs_paid=4, writable=False),
    })
    payload = R.scan()

    for field in R.E.SUPPORTED_FIELDS:
        f = payload["fields"][field]
        assert f["needs_paid"] == 6, "report-only rows leaked into the approvable total"
        assert f["report_only"] == 4
        assert f["missing"] == 14, "report-only rows should still be visible as a gap"


# ── Spec-field completeness ───────────────────────────────────────────────────

def test_zero_placeholder_counts_as_blank():
    """Costar writes an unknown numeric spec as "0.0000". On a product page
    that reads as a real measurement, so it is a gap, not a value."""
    for placeholder in ("", "  ", "0", "0.0", "0.0000", "00", "Unknown", "nan", "N/A"):
        assert R._blank_spec(placeholder), f"{placeholder!r} should count as blank"
    for real in ("70", "95", "107.5", "XL", "D", "All-Season"):
        assert not R._blank_spec(real), f"{real!r} should count as present"


def test_completeness_reads_the_finished_shopify_csv(quiet_io):
    """The spec block is built in augment*.py, downstream of every stage
    gap_report.py measures — so it has to be read back out of the output."""
    comp = R.field_completeness()
    if not comp["tire_rows_scanned"]:
        pytest.skip("no Shopify CSVs on disk")

    assert set(comp["totals"]) == set(R.SPEC_FIELDS)
    assert all(v >= 0 for v in comp["totals"].values())
    # Wheel files must not be scored against tire fields they never carry.
    for row in comp["rows"]:
        if row["kind"] == "wheels":
            assert row["missing"] == {}, "a wheel file was scored on tire specs"


def test_wheel_files_are_classified_by_content_not_filename(quiet_io):
    """'wheelsmart_tires' contains the word 'wheel' but is a tire file. A
    filename check silently drops 328 real tire rows from the count."""
    comp = R.field_completeness()
    if not comp["tire_rows_scanned"]:
        pytest.skip("no Shopify CSVs on disk")

    names = {r["file"]: r["kind"] for r in comp["rows"]}
    for name, kind in names.items():
        if "wheelsmart_tires" in name:
            assert kind == "tires", f"{name} misclassified as {kind}"


# ── The load-index fix ────────────────────────────────────────────────────────

def test_coerce_load_index_skips_the_zero_placeholder():
    """The bug this replaced: `str("0.0000").strip() or parse(...)` keeps the
    placeholder, because a non-empty string is truthy."""
    sys.path.insert(0, str(_PIPELINES_DIR / "shared"))
    from size_parser import coerce_load_index

    assert "0.0000".strip(), "precondition: the placeholder is truthy"
    assert coerce_load_index("0.0000", "Trail Blade X/T SxS - 70N 8PR") == "70"
    assert coerce_load_index("0", "98W XL") == "98"
    assert coerce_load_index("", "107Y XL") == "107"


def test_coerce_load_index_prefers_a_real_spec_value():
    from size_parser import coerce_load_index
    assert coerce_load_index("103", "should be ignored 88T") == "103"


def test_coerce_load_index_normalises_the_display_form():
    """A storefront should say 95, not 95.0000."""
    from size_parser import coerce_load_index, tidy_load_index
    assert coerce_load_index("95.0000", "") == "95"
    assert tidy_load_index("107.5") == "107.5"
    assert tidy_load_index("abc") == "abc"


def test_coerce_load_index_invents_nothing():
    """No index in the spec and none in the text means blank — an honest gap
    beats a number nobody can source."""
    from size_parser import coerce_load_index
    assert coerce_load_index("0.0000", "K3204R KLEVER XT - 8PR") == ""
    assert coerce_load_index("", "") == ""
    assert coerce_load_index("", "Roadian ATX - 6PR") == ""


def test_coerce_load_index_takes_the_first_source_that_answers():
    from size_parser import coerce_load_index
    assert coerce_load_index("0.0000", "no index here", "107Y XL") == "107"


# ── The load-range default ────────────────────────────────────────────────────

PASSENGER_SIZES = [
    "245/60R18",    # plain metric
    "P225/65R17",   # P-metric
    "205/55ZR16",   # Z speed symbol ahead of the construction letter
    "285/45RF21",   # run-flat
    "185-65-15",    # Formula's dashed notation, before normalize_size runs
    "  245/60R18 ",
]

# Rated by letter load range (C/D/E/…). A missing letter on one of these is
# genuinely unknown, and guessing it would misstate load capacity.
NON_PASSENGER_SIZES = [
    "LT265/70R17",  # light truck
    "LT225-75-16",  # light truck, dashed
    "ST175/80R13",  # special trailer
    "195/75R16C",   # commercial van — C maps to load range C *or* D
    "33X12.50R20",  # flotation
    "28X10R14",     # ATV
    "11R24.5",      # no-ratio truck
    "",
]


def test_passenger_sizes_default_to_standard_load():
    """A passenger tire carries either SL or XL. Suppliers mark XL because it
    is the exception and leave SL blank because it is the default — so a blank
    on a passenger size IS the standard rating, not a missing one."""
    from size_parser import default_passenger_load_range

    for size in PASSENGER_SIZES:
        assert default_passenger_load_range(size) == "SL", size


def test_non_passenger_sizes_are_left_blank():
    from size_parser import default_passenger_load_range

    for size in NON_PASSENGER_SIZES:
        assert default_passenger_load_range(size) == "", size


def test_load_range_default_never_overrides_a_real_value():
    """It is a default, applied last. Every caller must reach it only after the
    spec column and the description parse have both come up empty."""
    import re

    for path, call in [
        ("northcountry/augment_tires.py", 'default_passenger_load_range(r["size"])'),
        ("emergetires/augment_tires.py",  'default_passenger_load_range(r["size"])'),
        ("formula_distributors/augment.py", 'default_passenger_load_range(r["size"])'),
        ("wheelsmart/augment_tires.py",   'default_passenger_load_range(r["_size"])'),
    ]:
        src = (_PIPELINES_DIR / path).read_text(encoding="utf-8")
        assert call in src, f"{path} does not apply the default at all"
        # It must sit on the right-hand side of an `or`, never on its own.
        for line in src.splitlines():
            if call in line:
                assert "or " in line, f"{path}: default is not guarded by `or`: {line.strip()}"
                break


def test_run_live_never_queues_a_report_only_candidate(monkeypatch):
    """The same rule, enforced in the engine rather than the report."""
    import enrichment as E

    monkeypatch.setattr(E, "discover_candidates",
                        lambda field, sup=None, quality_cache=None: [
        dict(cache_key="image:a|b|c", writable=False, query="q", brand="a",
             model="b", type="tires", supplier="acme"),
    ])
    monkeypatch.setattr(E, "load_cache", lambda: {})

    def _boom(*a, **kw):
        raise AssertionError("a search was bought for a report-only row")

    serpapi = type(sys)("serpapi")
    serpapi.GoogleSearch = _boom
    monkeypatch.setitem(sys.modules, "serpapi", serpapi)

    # run_live() resolves the API key with a bare `from config import ...`,
    # which picks up whichever supplier's config.py another test already
    # imported. A real run is a fresh subprocess that imports none of them, so
    # pin the module rather than let test ordering decide.
    cfg = type(sys)("config")
    cfg.SERPAPI_KEY_CLIENT = "test-key-never-used"
    monkeypatch.setitem(sys.modules, "config", cfg)

    infer = type(sys)("tire_designation_inference")
    infer._keyword_lookup = lambda text: ""
    monkeypatch.setitem(sys.modules, "tire_designation_inference", infer)

    E.run_live(["image"], None, budget=5, verify_images=False)


# ── specs: candidate discovery must never overcount ────────────────────────────
#
# specs is the field this codebase already got wrong once: an earlier version
# of this module counted a blank spec_loadrange as a gap even on rows augment.py
# fills for free with 'SL'. discover_spec_candidates() has to replicate each
# supplier's free layer exactly, using the SAME shared functions augment*.py
# calls, or every test below would need real CSVs to catch the same class of
# bug again. These build synthetic rows instead, so the assertion is about the
# free-layer PRECEDENCE, not about today's catalog contents.

def _spec_src(**overrides):
    base = dict(
        supplier="acme", type="tires",
        glob="pipelines/acme/Data/cleaned/cleaned_tires_*.csv",
        brand_col="brand", model_col="model", size_col="size", extra_col=None,
        desc_col="description",
        load_index_col="spec_loadindex", speed_rating_col="spec_speedrating",
        load_range_col="spec_loadrange",
        width_col="spec_sectionwidth", ratio_col="spec_aspectratio", rim_col="spec_rimsize",
    )
    base.update(overrides)
    return base


def test_missing_spec_subfields_ignores_the_zero_placeholder():
    """A '0.0000' load index is Costar's unknown-value placeholder, not a real
    one — the same trap fixed for augment*.py must not be reintroduced here."""
    import enrichment as E

    row = dict(size="245/60R18", description="", spec_loadindex="0.0000",
              spec_speedrating="V", spec_loadrange="E",
              spec_sectionwidth="245", spec_aspectratio="60", spec_rimsize="18")
    missing = E._missing_spec_subfields(row, _spec_src())
    assert missing == {"load_index"}, missing


def test_missing_spec_subfields_applies_the_passenger_sl_default():
    """A blank spec_loadrange on a plain passenger size is NOT a gap — augment.py
    fills it with 'SL' for free. Counting it as missing would buy a search for
    information the free layer already has."""
    import enrichment as E

    row = dict(size="245/60R18", description="94V", spec_loadindex="94",
              spec_speedrating="V", spec_loadrange="",
              spec_sectionwidth="245", spec_aspectratio="60", spec_rimsize="18")
    missing = E._missing_spec_subfields(row, _spec_src())
    assert "load_range" not in missing, missing


def test_missing_spec_subfields_does_not_default_lt_load_range():
    """The same blank load_range on an LT size is a genuine gap — LT tires are
    letter-rated with no default, unlike passenger SL/XL."""
    import enrichment as E

    row = dict(size="LT265/70R17", description="", spec_loadindex="121",
              spec_speedrating="Q", spec_loadrange="",
              spec_sectionwidth="", spec_aspectratio="", spec_rimsize="")
    missing = E._missing_spec_subfields(row, _spec_src())
    assert "load_range" in missing, missing


def test_missing_spec_subfields_falls_back_to_description_parse():
    """No spec column value at all — the free-layer regex parse still has to
    run before anything is called a gap, same as augment*.py does."""
    import enrichment as E

    row = dict(size="205/55R16", description="Continental TrueContact 91H",
              spec_loadindex="", spec_speedrating="", spec_loadrange="",
              spec_sectionwidth="", spec_aspectratio="", spec_rimsize="")
    missing = E._missing_spec_subfields(row, _spec_src())
    assert missing == set(), missing   # width/ratio/rim from size; load_index/speed from description; SL default


def test_missing_spec_subfields_honours_a_source_with_no_spec_columns():
    """formula/dttire have no separate width/ratio/rim spec columns at all —
    width_col=None must not be treated as 'column exists but happens to be
    None', it must fall straight to the size-string parse."""
    import enrichment as E

    src = _spec_src(width_col=None, ratio_col=None, rim_col=None)
    row = dict(size="245/60R18", description="", spec_loadindex="94",
              spec_speedrating="V", spec_loadrange="")
    missing = E._missing_spec_subfields(row, src)
    assert missing == set(), missing


def test_discover_spec_candidates_drops_unwritable_subfields():
    """wheelsmart has no column for speed_rating/load_range at all — a search
    bought for either would have nowhere to write the answer. The candidate
    must still be produced for its writable sub-fields (load_index here), just
    stripped of the ones that can never be saved."""
    import enrichment as E

    src = _spec_src(supplier="wheelsmart", speed_rating_col=None, load_range_col=None)
    monkeypatch_sources = [src]
    import pandas as pd
    df = pd.DataFrame([dict(size="245/60R18", description="", spec_loadindex="",
                            spec_speedrating="", spec_loadrange="",
                            brand="Acme", model="Road King")])

    orig_sources = E.SPEC_SOURCES
    orig_latest = E.latest
    try:
        E.SPEC_SOURCES = monkeypatch_sources
        E.latest = lambda pattern: "fake.csv"
        real_read_csv = pd.read_csv
        pd.read_csv = lambda *a, **kw: df.copy()
        try:
            cands = E.discover_spec_candidates()
        finally:
            pd.read_csv = real_read_csv
    finally:
        E.SPEC_SOURCES = orig_sources
        E.latest = orig_latest

    assert len(cands) == 1, cands
    assert cands[0]["needed"] == {"load_index"}, cands[0]["needed"]


def test_parse_spec_result_only_returns_what_was_needed():
    import enrichment as E
    found = E._parse_spec_result("104H XL Load Range: E", "", {"load_index"})
    assert found == {"load_index": "104"}, found


def test_parse_spec_result_reads_a_labeled_load_range():
    """extract_load_range() alone has no pattern for a labeled field the way a
    retailer listing states it ('Load Range: E') — only a trailing letter on a
    size string or a bare PR/XL mention in free text."""
    import enrichment as E
    found = E._parse_spec_result("Michelin Defender 245/60R18 Load Range: E All Season",
                                 "", {"load_range"})
    assert found == {"load_range": "E"}, found


def test_parse_spec_result_extracts_size_components_from_result_text():
    import enrichment as E
    found = E._parse_spec_result("Continental TerrainContact 265/70R17 tire",
                                 "", {"section_width", "aspect_ratio", "rim_diameter"})
    assert found == {"section_width": "265", "aspect_ratio": "70", "rim_diameter": "17"}, found


def test_parse_spec_result_finds_nothing_rather_than_guessing():
    import enrichment as E
    found = E._parse_spec_result("unrelated page text with no spec info anywhere",
                                 "", {"load_index", "speed_rating", "load_range"})
    assert found == {}, found


def test_apply_spec_cache_requires_every_needed_subfield_for_a_full_hit(monkeypatch):
    """A row needing load_index AND speed_rating with only load_index found —
    and speed_rating never actually searched for (no 'tried' record covering
    it) — must still be offered for another search. Only a subfield that was
    genuinely searched and came up empty is a dead end, not one that simply
    hasn't been asked about yet."""
    import enrichment as E

    cand = dict(supplier="acme", type="tires", row_index=0, source_file="fake.csv",
               needed={"load_index", "speed_rating"}, cache_key="specs:a|b|c",
               spec_cols=dict(load_index="spec_loadindex", speed_rating="spec_speedrating"))
    monkeypatch.setattr(E, "discover_spec_candidates", lambda sup=None: [cand])
    monkeypatch.setattr(E, "load_cache",
                        lambda: {"specs:a|b|c": {"specs": {"load_index": "94"}}})

    stats = E.apply_spec_cache(write=False)
    assert stats["cache_hits"] == 0
    assert stats["still_needs_paid"] == 1, \
        "speed_rating has never been tried — this must still be offered for search"
    assert stats["cache_hits_empty"] == 0


def test_apply_spec_cache_treats_an_exhausted_partial_hit_as_a_dead_end(monkeypatch):
    """load_index WAS found; speed_rating was searched for (both are in
    'tried') but never turned up. Every needed sub-field has now been asked
    about at least once, so this is a dead end — offering it for another
    search would buy the identical question a second time."""
    import enrichment as E

    cand = dict(supplier="acme", type="tires", row_index=0, source_file="fake.csv",
               needed={"load_index", "speed_rating"}, cache_key="specs:a|b|c",
               spec_cols=dict(load_index="spec_loadindex", speed_rating="spec_speedrating"))
    monkeypatch.setattr(E, "discover_spec_candidates", lambda sup=None: [cand])
    monkeypatch.setattr(E, "load_cache", lambda: {
        "specs:a|b|c": {"specs": {"load_index": "94"},
                        "tried": ["load_index", "speed_rating"]},
    })

    stats = E.apply_spec_cache(write=False)
    assert stats["cache_hits"] == 0
    assert stats["still_needs_paid"] == 0, "an exhausted row must not be offered for spend again"
    assert stats["cache_hits_empty"] == 1
    assert stats["cache_hits_partial"] == 1, "load_index WAS found, so this should be flagged as partial"


def test_apply_spec_cache_never_writes_a_dead_end_row(monkeypatch, tmp_path):
    """A dead end has nothing to write — confirm write=True doesn't touch the
    CSV or report a file as rewritten for a row where nothing was found."""
    import enrichment as E
    import pandas as pd

    csv_path = tmp_path / "fake.csv"
    pd.DataFrame([dict(spec_loadindex="", spec_speedrating="")]).to_csv(csv_path, index=False)

    cand = dict(supplier="acme", type="tires", row_index=0, source_file=str(csv_path),
               needed={"load_index", "speed_rating"}, cache_key="specs:a|b|c",
               spec_cols=dict(load_index="spec_loadindex", speed_rating="spec_speedrating"))
    monkeypatch.setattr(E, "discover_spec_candidates", lambda sup=None: [cand])
    monkeypatch.setattr(E, "load_cache", lambda: {
        "specs:a|b|c": {"specs": {}, "tried": ["load_index", "speed_rating"]},
    })

    stats = E.apply_spec_cache(write=True)
    assert stats["files_written"] == []
    after = pd.read_csv(csv_path, dtype=str).fillna("")
    assert after.loc[0, "spec_loadindex"] == ""
    assert after.loc[0, "spec_speedrating"] == ""


def test_a_dead_end_search_is_never_repeated(monkeypatch):
    """The bug this whole section exists to prevent: an empty search result
    must be remembered, or the exact same question gets bought again on
    every future run for a row that will never resolve."""
    import enrichment as E

    cand = dict(supplier="acme", type="tires", row_index=0, source_file="fake.csv",
               needed={"load_index"}, cache_key="specs:a|b|c", query="q", size="245/60R18",
               spec_cols=dict(load_index="spec_loadindex"))
    monkeypatch.setattr(E, "discover_spec_candidates", lambda sup=None: [cand])
    saved = {}
    monkeypatch.setattr(E, "load_cache", lambda: dict(saved))
    monkeypatch.setattr(E, "save_cache", lambda cache: saved.update(cache))

    calls = []
    class _EmptyResult:
        def get_dict(self):
            return {"shopping_results": [], "organic_results": []}
    serpapi = type(sys)("serpapi")
    serpapi.GoogleSearch = lambda params: (calls.append(params["q"]), _EmptyResult())[1]
    monkeypatch.setitem(sys.modules, "serpapi", serpapi)
    cfg = type(sys)("config")
    cfg.SERPAPI_KEY_CLIENT = "test-key"
    monkeypatch.setitem(sys.modules, "config", cfg)
    infer = type(sys)("tire_designation_inference")
    infer._keyword_lookup = lambda text: ""
    monkeypatch.setitem(sys.modules, "tire_designation_inference", infer)

    E.run_live(["specs"], None, budget=5, verify_images=False)
    assert len(calls) == 1, "first run should search once"
    assert saved["specs:a|b|c"]["tried"] == ["load_index"]

    E.run_live(["specs"], None, budget=5, verify_images=False)
    assert len(calls) == 1, "second run must not repeat a search already known to be empty"


def test_run_live_specs_never_searches_a_field_with_no_column(monkeypatch):
    """Mirrors test_run_live_never_queues_a_report_only_candidate, but for the
    specs field's per-subfield writability rather than per-source."""
    import enrichment as E

    cand = dict(supplier="wheelsmart", type="tires", row_index=0, source_file="fake.csv",
               needed={"load_index"}, cache_key="specs:a|b|c", query="q", size="245/60R18",
               spec_cols=dict(load_index="load_index", speed_rating=None, load_range=None))
    monkeypatch.setattr(E, "discover_spec_candidates", lambda sup=None: [cand])
    monkeypatch.setattr(E, "load_cache", lambda: {})
    monkeypatch.setattr(E, "save_cache", lambda cache: None)

    calls = []
    def _fake_search(params):
        calls.append(params["q"])
        class _R:
            def get_dict(self):
                return {"shopping_results": [{"title": "94V", "snippet": ""}]}
        return _R()
    serpapi = type(sys)("serpapi")
    serpapi.GoogleSearch = _fake_search
    monkeypatch.setitem(sys.modules, "serpapi", serpapi)
    cfg = type(sys)("config")
    cfg.SERPAPI_KEY_CLIENT = "test-key"
    monkeypatch.setitem(sys.modules, "config", cfg)
    infer = type(sys)("tire_designation_inference")
    infer._keyword_lookup = lambda text: ""
    monkeypatch.setitem(sys.modules, "tire_designation_inference", infer)

    E.run_live(["specs"], None, budget=5, verify_images=False)
    assert len(calls) == 1, "exactly one search expected for the one writable candidate"


def test_run_live_specs_merges_into_existing_cache_rather_than_overwriting(monkeypatch):
    """A candidate whose load_index was already found on an earlier run and
    now only needs speed_rating must keep the cached load_index after this
    run, not lose it."""
    import enrichment as E

    cand = dict(supplier="acme", type="tires", row_index=0, source_file="fake.csv",
               needed={"speed_rating"}, cache_key="specs:a|b|c", query="q", size="245/60R18",
               spec_cols=dict(load_index="spec_loadindex", speed_rating="spec_speedrating"))
    monkeypatch.setattr(E, "discover_spec_candidates", lambda sup=None: [cand])
    monkeypatch.setattr(E, "load_cache",
                        lambda: {"specs:a|b|c": {"specs": {"load_index": "94"}, "query": "q"}})
    saved = {}
    monkeypatch.setattr(E, "save_cache", lambda cache: saved.update(cache))

    class _R:
        def get_dict(self):
            return {"shopping_results": [{"title": "94V", "snippet": ""}]}
    serpapi = type(sys)("serpapi")
    serpapi.GoogleSearch = lambda params: _R()
    monkeypatch.setitem(sys.modules, "serpapi", serpapi)
    cfg = type(sys)("config")
    cfg.SERPAPI_KEY_CLIENT = "test-key"
    monkeypatch.setitem(sys.modules, "config", cfg)
    infer = type(sys)("tire_designation_inference")
    infer._keyword_lookup = lambda text: ""
    monkeypatch.setitem(sys.modules, "tire_designation_inference", infer)

    E.run_live(["specs"], None, budget=5, verify_images=False)
    entry = saved["specs:a|b|c"]["specs"]
    assert entry.get("load_index") == "94", "earlier cached result was lost"
    assert entry.get("speed_rating") == "V", "the newly-searched-for field was not added"


def test_specs_budget_is_isolated_from_image_and_designation(no_network, no_augment,
                                                              quiet_io, monkeypatch):
    _stub_apply(monkeypatch)
    R.run(image_budget=0, designation_budget=0, specs_budget=25)

    fields_called = [c["fields"] for c in no_network]
    assert fields_called == [["specs"]], fields_called


def test_specs_result_file_records_its_own_budget(no_network, no_augment, quiet_io, monkeypatch):
    _stub_apply(monkeypatch)
    R.run(image_budget=3, designation_budget=0, specs_budget=25)

    result = json.loads((quiet_io / ".enrich_result.json").read_text(encoding="utf-8"))
    assert result["image_budget"] == 3
    assert result["specs_budget"] == 25
    assert result["designation_budget"] == 0


# ── parse_load_speed on noisy SERP text ─────────────────────────────────────────
#
# Found by checking a real live run's output before it shipped: a bare size
# string like "225/60ZR18" has the exact same digit/digit+letter shape as a
# genuine dual load-index token ("143/141J"), and the construction letter (Z,
# H, V — from ZR/HR/VR) looks like a plausible speed symbol on its own. On a
# curated supplier description field this basically never comes up; on raw
# SERP result text, which routinely repeats the product's own size, it wrote
# "Load index: 225" (equal to the tire's section width) and "Load index: 112,
# Speed rating: MP" (from "...112 MPH...") to the live storefront. 178 of 632
# specs answers from that run were affected — see the "Remember empty specs
# searches" commit's follow-up repair.

def test_parse_load_speed_rejects_a_bare_size_string():
    from size_parser import parse_load_speed
    assert parse_load_speed("Lexani LXUHP-207 225/60ZR18 tire size") == ("", "")
    assert parse_load_speed("205/50R17 tire") == ("", "")
    assert parse_load_speed("195/55HR16 continental") == ("", "")
    assert parse_load_speed("LT285/70VR17 tire") == ("", "")


def test_parse_load_speed_rejects_a_coincidental_two_letter_match():
    """[A-Z]{1,2} used to accept 2 letters, so "...112 MPH..." was read as
    load_index=112, speed_rating="MP" — a real, if rare, phrase in shopping
    result snippets, and "MP" is not a real tire speed symbol."""
    from size_parser import parse_load_speed
    assert parse_load_speed("Nexen Roadian ATX 30x9.50R15 max speed 112 MPH") == ("", "")


def test_parse_load_speed_still_finds_genuine_ratings():
    from size_parser import parse_load_speed
    assert parse_load_speed("LT265/70R17 143/141J 10PR") == ("143", "J")
    assert parse_load_speed("LT265/70R17 143/141J XL") == ("143", "J")
    assert parse_load_speed("245/60R18 104H XL") == ("104", "H")
    assert parse_load_speed("225/45R17 94V") == ("94", "V")
    assert parse_load_speed("225/45R17 94(Y)") == ("94", "Y")
    assert parse_load_speed("185/65R15 88H SL BSW AKLIMATE All Weather") == ("88", "H")
    assert parse_load_speed("265/70R17 121/118S") == ("121", "S")
    assert parse_load_speed("30x9.50R15 112Q") == ("112", "Q")


def test_parse_load_speed_rejects_a_non_speed_letter():
    """A, I, O, X are never real tire speed symbols."""
    from size_parser import parse_load_speed
    assert parse_load_speed("94A tire") == ("", "")
    assert parse_load_speed("94I tire") == ("", "")
    assert parse_load_speed("94O tire") == ("", "")
    assert parse_load_speed("94X tire") == ("", "")


# ── slugify: "+" must not collapse two different products onto one Handle ──────
#
# Found the same way as the load-speed bug: checking a real upload bundle
# before it shipped. "BFGoodrich TRAIL-TERRAIN T/A" and the genuinely
# different "T/A+" trim produced the identical Handle, because slugify's
# `[^\w\s-]` strip deletes "+" with nothing to mark it ever having been
# there. Shopify groups CSV rows into one product by Handle, so importing
# these as-is would have merged two different listings — 11 such pairs (30
# Handle collisions total, the rest a separate, unrelated near-duplicate-row
# issue) existed in one real snapshot of this catalog.

SLUGIFY_FILES = [
    "dttire/augment_tires.py", "dttire/augment_wheels.py",
    "emergetires/augment_tires.py", "emergetires/augment_wheels.py",
    "formula_distributors/augment.py", "formula_distributors/augment_wheels.py",
    "northcountry/augment_tires.py", "northcountry/augment_wheels.py",
    "wheelsmart/augment_rims.py", "wheelsmart/augment_tires.py",
]


def _load_slugify(rel_path):
    """slugify() is duplicated per (supplier, product type) rather than
    shared — import each copy directly by path so a fix applied to one file
    but not another is caught, instead of testing one copy and assuming the
    rest match. Extracts just the function body rather than importing the
    whole module, which would execute that supplier's module-level side
    effects (logging setup, sys.path manipulation); `re` is injected
    manually since it doesn't travel with a body-only extraction the way a
    real module import would carry its file's other top-level imports."""
    import re as _re
    path = _PIPELINES_DIR / rel_path
    src = path.read_text(encoding="utf-8")
    func_src = src[src.index("def slugify"):]
    func_src = func_src[:func_src.index("\n\n\n")]
    ns = {"re": _re}
    exec(compile(func_src, str(path), "exec"), ns)
    return ns["slugify"]


@pytest.mark.parametrize("rel_path", SLUGIFY_FILES)
def test_slugify_distinguishes_a_plus_suffixed_trim(rel_path):
    slugify = _load_slugify(rel_path)
    base = slugify("BFGoodrich TRAIL-TERRAIN T/A - All terrain 245/70R17 114T XL")
    plus = slugify("BFGoodrich TRAIL-TERRAIN T/A+ - All terrain 245/70R17 114T XL")
    assert base != plus, f"{rel_path}: T/A and T/A+ still collide on one Handle"
    assert "plus" in plus


@pytest.mark.parametrize("rel_path", SLUGIFY_FILES)
def test_slugify_still_produces_a_normal_handle(rel_path):
    slugify = _load_slugify(rel_path)
    # "plus" gets space-padded before the collapse step so it reads as its own
    # word ("t-plus-h") rather than fusing into the next letter ("t-plush").
    assert slugify("Michelin Defender T+H 245/60R18") == "michelin-defender-t-plus-h-24560r18"
    assert slugify("  Extra   Spaces--and--dashes  ") == "extra-spaces-and-dashes"


# ── image cache key: one photo per model, not per size ───────────────────────

import enrichment as _E  # noqa: E402


class TestImageCacheKeyIsModelLevel:
    """A tire photo is a picture of a tread pattern; suppliers store one per
    model. Keying image searches by size bought the same picture once per
    size — 4,586 searches for North Country's 535 distinct low-res models.
    See enrichment.IMAGE_KEY_INCLUDES_SIZE."""

    def test_two_sizes_of_one_model_share_an_image_key(self):
        a = _E.cache_key("image", "Cooper", "Endeavor Plus", "245/60R18")
        b = _E.cache_key("image", "Cooper", "Endeavor Plus", "225/65R17")
        assert a == b == "image:cooper|endeavor plus|"

    def test_different_models_still_have_different_keys(self):
        assert (_E.cache_key("image", "Cooper", "Endeavor Plus", "245/60R18")
                != _E.cache_key("image", "Cooper", "EvoWin", "245/60R18"))

    def test_different_brands_still_have_different_keys(self):
        assert (_E.cache_key("image", "Cooper", "Endeavor", "245/60R18")
                != _E.cache_key("image", "Toyo", "Endeavor", "245/60R18"))

    def test_specs_and_designation_keep_size(self):
        """Load index and speed rating genuinely differ per size — only the
        photo is model-level."""
        for field in ("specs", "designation"):
            a = _E.cache_key(field, "Cooper", "Endeavor Plus", "245/60R18")
            b = _E.cache_key(field, "Cooper", "Endeavor Plus", "225/65R17")
            assert a != b, field
            assert "245/60r18" in a

    def test_a_row_named_only_by_its_description_keeps_size(self):
        """_is_queryable lets a row qualify on `extra` alone, leaving brand AND
        model empty. Dropping size there would collapse every such row in the
        catalogue onto "image:||" — one photo for thousands of unrelated
        products. This is the guard against that."""
        a = _E.cache_key("image", "", "", "17X7 6X139.7 CB106")
        b = _E.cache_key("image", "", "", "18X8 5X114.3 CB64")
        assert a != b
        assert a == "image:||17x7 6x139.7 cb106"

    def test_a_brand_with_no_model_is_still_model_level(self):
        assert _E.cache_key("image", "Rovelo", "", "225/65R17") == "image:rovelo||"

    def test_query_matches_the_key_and_omits_size(self):
        """A model-level key demands a model-level query, or two different
        searches would share one cached answer."""
        q = _E.build_query("image", "Cooper", "Endeavor Plus", "245/60R18")
        assert "245" not in q
        assert q == "Cooper Endeavor Plus tire product photo"

    def test_description_only_query_still_carries_size(self):
        q = _E.build_query("image", "", "", "17X7 6X139.7",
                           extra="STEEL WHEEL", product_type="rims")
        assert "17X7" in q and "wheel product photo" in q

    def test_the_size_behaviour_is_a_single_flag(self, monkeypatch):
        monkeypatch.setattr(_E, "IMAGE_KEY_INCLUDES_SIZE", True)
        a = _E.cache_key("image", "Cooper", "Endeavor Plus", "245/60R18")
        b = _E.cache_key("image", "Cooper", "Endeavor Plus", "225/65R17")
        assert a != b
        assert "245/60r18" in a
        assert "245/60R18" in _E.build_query("image", "Cooper", "Endeavor Plus",
                                             "245/60R18")


class TestEverySourceReportsItsSearchCount:
    """A field whose breakdown forgets paid_keys reports "0 searches", which
    caps that field's dashboard budget at zero and silently blocks spend the
    client approved. specs uses a different breakdown function from
    image/designation, so it is the easy one to miss."""

    def test_spec_breakdown_carries_paid_keys(self, monkeypatch):
        monkeypatch.setattr(_E, "load_cache", lambda: {})
        monkeypatch.setattr(_E, "discover_spec_candidates", lambda *a, **k: [
            dict(supplier="s", type="tires", needed={"load_index"},
                 cache_key="specs:a|b|245/60R18"),
            dict(supplier="s", type="tires", needed={"load_index"},
                 cache_key="specs:a|b|225/65R17"),
            dict(supplier="s", type="tires", needed={"load_index"},
                 cache_key="specs:a|b|245/60R18"),   # same key as the first
        ])
        rows = _E.per_spec_source_breakdown()
        r = rows[("s", "tires")]
        assert r["needs_paid"] == 3, "three rows are missing a spec"
        assert len(r["paid_keys"]) == 2, "but only two distinct searches buy them"

    def test_image_breakdown_carries_paid_keys(self, monkeypatch):
        monkeypatch.setattr(_E, "load_cache", lambda: {})
        monkeypatch.setattr(_E, "discover_candidates", lambda *a, **k: [
            dict(supplier="s", type="tires", writable=True, verdict="low_res",
                 cache_key="image:cooper|endeavor|"),
            dict(supplier="s", type="tires", writable=True, verdict="low_res",
                 cache_key="image:cooper|endeavor|"),
        ])
        r = _E.per_source_breakdown("image")[("s", "tires")]
        assert r["needs_paid"] == 2 and len(r["paid_keys"]) == 1
