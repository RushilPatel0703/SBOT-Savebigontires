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
    """Make apply_cached report a given set of rewritten files without touching disk."""
    def _fake(fields_arg, supplier, write):
        return {f: dict(total_missing=0, cache_hits=len(files_written), cache_hits_empty=0,
                        still_needs_paid=0, files_written=list(files_written))
                for f in fields_arg}
    monkeypatch.setattr(R.E, "apply_cached", _fake)


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
    monkeypatch.setattr(R.E, "per_source_breakdown", lambda field, sup=None: {
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

    monkeypatch.setattr(E, "discover_candidates", lambda field, sup=None: [
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
