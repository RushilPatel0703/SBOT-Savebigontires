"""Pause and resume for the North Country scrape (shared/scrape_journal.py).

The property that matters: a run that is paused, crashed or cut off and then
resumed writes exactly what an uninterrupted run writes, without repeating a
request it already has an answer for. The scrapers are driven end to end here
against a fake catalogue; nothing touches the network.
"""

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PIPELINES = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PIPELINES / "shared"))

import scrape_journal as sj  # noqa: E402
from costar_session import SessionDeathError  # noqa: E402


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, _PIPELINES / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the journal ──────────────────────────────────────────────────────────────

def test_resume_replays_entries_and_keeps_the_original_meta(tmp_path):
    path = tmp_path / "j.jsonl"
    j = sj.Journal.begin(path, meta={"terms": ["a", "b"]})
    j.add("term", term="a", count=3)

    j2 = sj.Journal.begin(path, meta={"terms": ["changed"]}, resume=True)
    assert j2.resumed
    assert j2.meta == {"terms": ["a", "b"]}
    assert [e["term"] for e in j2.entries("term")] == ["a"]
    j2.finish()
    assert not path.exists()


def test_a_fresh_run_discards_old_progress(tmp_path):
    path = tmp_path / "j.jsonl"
    sj.Journal.begin(path, meta={}).add("term", term="a", count=1)
    j = sj.Journal.begin(path, meta={})
    assert not j.resumed and j.entries("term") == []


def test_resume_of_a_different_run_starts_fresh(tmp_path):
    path = tmp_path / "j.jsonl"
    sj.Journal.begin(path, meta={"base_file": "old.csv"}).add("brand", brand="A", results=[])
    j = sj.Journal.begin(path, meta={"base_file": "new.csv"}, resume=True, match=("base_file",))
    assert not j.resumed and j.entries("brand") == []


def test_a_torn_last_line_is_dropped_and_the_next_append_starts_clean(tmp_path):
    path = tmp_path / "j.jsonl"
    j = sj.Journal.begin(path, meta={})
    j.add("term", term="a", count=1)
    with open(path, "ab") as fh:
        fh.write(b'{"kind": "term", "term": "b", "cou')      # power cut mid-write

    j2 = sj.Journal.begin(path, meta={}, resume=True)
    assert [e["term"] for e in j2.entries("term")] == ["a"]
    j2.add("term", term="c", count=2)
    j3 = sj.Journal.begin(path, meta={}, resume=True)
    assert [e["term"] for e in j3.entries("term")] == ["a", "c"]


def test_a_whole_entry_missing_only_its_newline_is_kept(tmp_path):
    path = tmp_path / "j.jsonl"
    sj.Journal.begin(path, meta={})
    with open(path, "ab") as fh:
        fh.write(b'{"kind": "term", "term": "a", "count": 1}')
    j2 = sj.Journal.begin(path, meta={}, resume=True)
    j2.add("term", term="b", count=1)
    j3 = sj.Journal.begin(path, meta={}, resume=True)
    assert [e["term"] for e in j3.entries("term")] == ["a", "b"]


def test_damage_mid_file_is_not_replayed_around(tmp_path):
    path = tmp_path / "j.jsonl"
    sj.Journal.begin(path, meta={})
    with open(path, "ab") as fh:
        fh.write(b'garbage\n{"kind": "term", "term": "a", "count": 1}\n')
    assert not sj.Journal.begin(path, meta={}, resume=True).resumed


def test_terms_before_a_session_death_that_came_back_empty_are_searched_again(tmp_path):
    j = sj.Journal.begin(tmp_path / "j.jsonl", meta={})
    for term, count in [("a", 5), ("b", 0), ("c", 0)]:
        j.add("term", term=term, count=count, products=[], still_capped=[])
    j.add("session_death", retry=sj.trailing_zero_terms({"a": 5, "b": 0, "c": 0}))
    assert list(sj.done_terms(j)) == ["a"]
    # Searched again after the resume, a zero is believed.
    j.add("term", term="b", count=0, products=[], still_capped=[])
    assert list(sj.done_terms(j)) == ["a", "b"]


def test_summary_reports_the_latest_phase(tmp_path):
    j = sj.Journal.begin(tmp_path / "j.jsonl", meta={})
    j.phase("term", "Search", 3, "terms")
    j.add("term", term="a", count=1)
    j.phase("info", "Images + specs", 10, "batches")
    for _ in range(4):
        j.add("info", guids=[], result=[{}, {}])
    assert sj.summary(j.path) == "Images + specs: 4 of 10 batches"
    assert sj.summary(tmp_path / "missing.jsonl") is None


def test_a_failed_batch_is_not_journaled_so_a_resume_asks_again(tmp_path):
    j = sj.Journal.begin(tmp_path / "j.jsonl", meta={})
    seen = []

    def fetch(batch):
        if batch == ["g3"]:
            raise RuntimeError("timeout")
        return {g: 1 for g in batch}

    sj.fetch_batches(j, "cost", "Cost", ["g1", "g2", "g3"], 2, fetch, seen.append,
                     sj.Halt(tmp_path))
    assert [e["guids"] for e in j.entries("cost")] == [["g1", "g2"]]

    asked = []
    sj.fetch_batches(j, "cost", "Cost", ["g1", "g2", "g3"], 2,
                     lambda b: asked.append(b) or {g: 1 for g in b}, seen.append,
                     sj.Halt(tmp_path))
    assert asked == [["g3"]]


# ── the tire scraper, end to end ─────────────────────────────────────────────

# term -> guids its search returns. t1 repeats one of t0's (dedupe), t5 is empty.
CATALOGUE = {f"t{i}": [f"g{i}a", f"g{i}b"] for i in range(10)}
CATALOGUE["t1"].append("g0a")
CATALOGUE["t5"] = []


class FakeNC:
    """The three NC endpoints, counting every request."""

    def __init__(self, mod, home, pause_after=None, die_at=None, dead_from=None):
        self.mod, self.home = mod, home
        self.pause_after, self.die_at, self.dead_from = pause_after, die_at, dead_from
        self.searches, self.info_calls, self.cost_calls = [], [], []

    def _tick(self, n):
        if self.pause_after is not None and n == self.pause_after:
            sj.request_pause(self.home)

    def search(self, term):
        self.searches.append(term)
        if term == self.die_at:
            raise SessionDeathError("dead")
        if self.dead_from and term >= self.dead_from:
            return []                                   # a dead session answers empty
        self._tick(len(self.searches) + len(self.info_calls))
        return [{"productGuid": g, "brandName": "B", "size": term} for g in CATALOGUE[term]]

    def info(self, guids):
        self.info_calls.append(list(guids))
        self._tick(len(self.searches) + len(self.info_calls))
        return ({g: f"http://img/{g}.jpg" for g in guids},
                {g: {"SPD": "H", "LR": "SL"} for g in guids})

    def cost(self, guids):
        self.cost_calls.append(list(guids))
        return {g: "12.34" for g in guids}


@pytest.fixture
def tires(tmp_path, monkeypatch):
    mod = _load("northcountry/scraper_tires.py", "_nc_scraper_tires_under_test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "_HERE", str(tmp_path))
    monkeypatch.setattr(mod, "JOURNAL_FILE", sj.state_dir(tmp_path) / "tires.jsonl")
    monkeypatch.setattr(mod, "SEARCH_TERMS", sorted(CATALOGUE))
    monkeypatch.setattr(mod, "PRODUCT_INFO_BATCH_SIZE", 4)
    return mod


def _run(mod, fake, monkeypatch, *argv):
    monkeypatch.setattr(mod, "search_with_drilldown", fake.search)
    monkeypatch.setattr(mod, "get_product_info", fake.info)
    monkeypatch.setattr(mod, "get_cost_prices", fake.cost)
    monkeypatch.setattr(sys, "argv", ["scraper_tires.py", *argv])
    mod.STILL_CAPPED.clear()
    mod.main()


def _output(tmp):
    files = sorted(Path(tmp, "Data", "raw").glob("northcountry_tires_2*.csv"))
    assert len(files) == 1, files
    with open(files[0], newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _baseline(tires, tmp_path, monkeypatch):
    base = tmp_path / "baseline"
    base.mkdir()
    monkeypatch.chdir(base)
    monkeypatch.setattr(tires, "_HERE", str(base))
    monkeypatch.setattr(tires, "JOURNAL_FILE", sj.state_dir(base) / "tires.jsonl")
    _run(tires, FakeNC(tires, base), monkeypatch)
    rows = _output(base)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tires, "_HERE", str(tmp_path))
    monkeypatch.setattr(tires, "JOURNAL_FILE", sj.state_dir(tmp_path) / "tires.jsonl")
    return rows


@pytest.mark.parametrize("pause_after", [3, 12], ids=["during search", "during ProductInfo"])
def test_paused_then_resumed_writes_what_an_uninterrupted_run_writes(
        tires, tmp_path, monkeypatch, pause_after):
    expected = _baseline(tires, tmp_path, monkeypatch)

    first = FakeNC(tires, tmp_path, pause_after=pause_after)
    with pytest.raises(SystemExit) as stop:
        _run(tires, first, monkeypatch)
    assert stop.value.code == sj.PAUSE_EXIT
    assert not sj.pause_requested(tmp_path)              # the flag is consumed
    assert not list(Path("Data/raw").glob("northcountry_tires_2*.csv"))

    second = FakeNC(tires, tmp_path)
    _run(tires, second, monkeypatch, "--resume")

    assert _output(tmp_path) == expected
    # Nothing asked twice across the two runs.
    assert sorted(first.searches + second.searches) == sorted(CATALOGUE)
    asked = [g for b in first.info_calls + second.info_calls for g in b]
    assert sorted(asked) == sorted(r["product_guid"] for r in expected)
    assert not tires.JOURNAL_FILE.exists()


def test_a_run_cut_off_without_warning_resumes_the_same_way(tires, tmp_path, monkeypatch):
    expected = _baseline(tires, tmp_path, monkeypatch)

    class PowerCut(BaseException):
        pass

    first = FakeNC(tires, tmp_path)
    real = first.search
    first.search = lambda t: (_ for _ in ()).throw(PowerCut()) if t == "t6" else real(t)
    with pytest.raises(PowerCut):
        _run(tires, first, monkeypatch)

    second = FakeNC(tires, tmp_path)
    _run(tires, second, monkeypatch, "--resume")
    assert _output(tmp_path) == expected
    assert second.searches == ["t6", "t7", "t8", "t9"]


def test_a_session_death_searches_its_suspect_zero_streak_again(tires, tmp_path, monkeypatch):
    expected = _baseline(tires, tmp_path, monkeypatch)

    # t3 and t4 come back empty because the session is already dead; t5 raises.
    first = FakeNC(tires, tmp_path, dead_from="t3", die_at="t5")
    with pytest.raises(SessionDeathError):
        _run(tires, first, monkeypatch)
    assert first.searches == ["t0", "t1", "t2", "t3", "t4", "t5"]   # nothing after the death

    second = FakeNC(tires, tmp_path)
    _run(tires, second, monkeypatch, "--resume")
    assert second.searches[:3] == ["t3", "t4", "t5"]
    assert _output(tmp_path) == expected


def test_without_resume_old_progress_is_not_used(tires, tmp_path, monkeypatch):
    first = FakeNC(tires, tmp_path, pause_after=3)
    with pytest.raises(SystemExit):
        _run(tires, first, monkeypatch)
    second = FakeNC(tires, tmp_path)
    _run(tires, second, monkeypatch)
    assert second.searches == sorted(CATALOGUE)


# ── the brand sweep ──────────────────────────────────────────────────────────

def test_brand_sweep_resumes_and_merges_atomically(tmp_path, monkeypatch):
    # brand_sweep does `from scraper_tires import ...`; other tests may already
    # have a different supplier's scraper_tires cached under that name.
    monkeypatch.delitem(sys.modules, "scraper_tires", raising=False)
    monkeypatch.syspath_prepend(str(_PIPELINES / "northcountry"))
    bs = _load("northcountry/brand_sweep.py", "_nc_brand_sweep_under_test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bs, "_HERE", str(tmp_path))
    monkeypatch.setattr(bs, "JOURNAL_FILE", sj.state_dir(tmp_path) / "brands.jsonl")
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    monkeypatch.setattr(bs, "get_product_info", lambda guids: ({}, {g: {} for g in guids}))
    monkeypatch.setattr(bs, "get_cost_prices", lambda guids: {g: "50.00" for g in guids})
    raw = tmp_path / "Data" / "raw"
    raw.mkdir(parents=True)
    base = raw / "northcountry_tires_20261025_000000.csv"
    with open(base, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["product_guid", "brand"])
        w.writeheader()
        w.writerows([{"product_guid": f"k{b}", "brand": b} for b in "ABCD"])

    asked = []

    def search(brand):
        asked.append(brand)
        if brand == "B":
            sj.request_pause(tmp_path)
        return [{"productGuid": f"k{brand}"}, {"productGuid": f"gap{brand}"}]

    monkeypatch.setattr(bs, "brand_search_with_drilldown", search)
    argv = ["brand_sweep.py", "--file", str(base), "--merge"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as stop:
        bs.main()
    assert stop.value.code == sj.PAUSE_EXIT
    assert asked == ["A", "B"]

    monkeypatch.setattr(sys, "argv", argv + ["--resume"])
    bs.main()
    assert asked == ["A", "B", "C", "D"]
    with open(base, newline="", encoding="utf-8") as fh:
        merged = list(csv.DictReader(fh))
    assert sorted(r["product_guid"] for r in merged) == sorted(
        [f"k{b}" for b in "ABCD"] + [f"gap{b}" for b in "ABCD"])
    # A gap row without a cost price is quarantined by clean.py, so it must have one.
    assert {r["cost_price"] for r in merged if r["product_guid"].startswith("gap")} == {"50.00"}
    assert not list(raw.glob("*.tmp"))
    assert not bs.JOURNAL_FILE.exists()


# ── the pipeline runner ──────────────────────────────────────────────────────

@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mod = _load("northcountry/run_pipeline.py", "_nc_run_pipeline_under_test")
    monkeypatch.setattr(mod, "_HERE", str(tmp_path))
    calls = []
    outcomes = {}
    current = [None]

    class Done:
        def __init__(self, rc):
            self.returncode = rc

    # enrich_tires and enrich_wheels run the same script with the same args,
    # so the step name comes from run_step rather than the command line.
    real_run_step = mod.run_step

    def named_run_step(name, *a, **kw):
        current[0] = name
        return real_run_step(name, *a, **kw)

    def fake_run(cmd, env=None):
        step = current[0]
        calls.append((step, cmd[2:]))
        effect = outcomes.pop(step, 0)
        if effect == "pause-after":
            sj.request_pause(tmp_path)
            return Done(0)
        return Done(effect)

    monkeypatch.setattr(mod, "run_step", named_run_step)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod.calls, mod.outcomes = calls, outcomes
    return mod


DASHBOARD_ARGS = ["--skip", "create_products", "update_images", "create_wheels",
                  "update_images_wheels"]


def _main(mod, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    mod.main()


def test_runner_resumes_the_interrupted_step_with_its_journal(runner, tmp_path, monkeypatch):
    runner.outcomes["brand_sweep"] = sj.PAUSE_EXIT
    with pytest.raises(SystemExit) as stop:
        _main(runner, monkeypatch, *DASHBOARD_ARGS, "--full")
    assert stop.value.code == sj.PAUSE_EXIT
    progress = sj.load_progress(tmp_path)
    assert progress["current"] == "brand_sweep" and progress["done"] == ["scraper_tires"]

    runner.calls.clear()
    _main(runner, monkeypatch, "--resume")
    steps = [s for s, _ in runner.calls]
    assert steps == ["brand_sweep", "clean_tires", "enrich_tires", "augment_tires",
                     "scraper_wheels", "clean_wheels", "enrich_wheels", "augment_wheels"]
    args = dict(runner.calls)
    assert "--resume" in args["brand_sweep"]
    assert "--resume" not in args["scraper_wheels"]       # never reached: starts clean
    assert "--full" in args["scraper_wheels"]             # the original run's flags
    assert sj.load_progress(tmp_path) is None


def test_a_pause_between_steps_never_repeats_the_finished_one(runner, tmp_path, monkeypatch):
    runner.outcomes["scraper_tires"] = "pause-after"
    with pytest.raises(SystemExit) as stop:
        _main(runner, monkeypatch, *DASHBOARD_ARGS)
    assert stop.value.code == sj.PAUSE_EXIT
    assert [s for s, _ in runner.calls] == ["scraper_tires"]

    runner.calls.clear()
    _main(runner, monkeypatch, "--resume")
    first, args = runner.calls[0]
    assert first == "brand_sweep" and "--resume" not in args


def test_a_failed_step_can_be_resumed_too(runner, tmp_path, monkeypatch):
    runner.outcomes["scraper_wheels"] = 1
    with pytest.raises(SystemExit) as stop:
        _main(runner, monkeypatch, *DASHBOARD_ARGS)
    assert stop.value.code == 1
    runner.calls.clear()
    _main(runner, monkeypatch, "--resume")
    first, args = runner.calls[0]
    assert first == "scraper_wheels" and "--resume" in args


def test_a_one_off_step_leaves_a_paused_run_resumable(runner, tmp_path, monkeypatch):
    runner.outcomes["brand_sweep"] = sj.PAUSE_EXIT
    with pytest.raises(SystemExit):
        _main(runner, monkeypatch, *DASHBOARD_ARGS)
    _main(runner, monkeypatch, "--only", "augment_tires")
    assert sj.load_progress(tmp_path)["current"] == "brand_sweep"


def test_resume_with_nothing_recorded_refuses(runner, monkeypatch):
    with pytest.raises(SystemExit) as stop:
        _main(runner, monkeypatch, "--resume")
    assert stop.value.code == 1 and runner.calls == []
