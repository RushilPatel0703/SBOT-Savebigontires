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
