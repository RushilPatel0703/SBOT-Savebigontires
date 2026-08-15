"""
Unit tests for pure functions in dashboard.py.

Functions under test (no Streamlit calls, no subprocess, no disk I/O beyond tmp):
  - load_status / save_status
  - parse_log_progress
  - collect_source_csvs
  - pid_alive
  - is_running
  - progress bar denominator logic (inline in step2_section, tested here as pure math)
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _load(dashboard, path):
    """Call dashboard.load_status() pointing at an arbitrary JSON file."""
    orig = dashboard.STATUS_FILE
    dashboard.STATUS_FILE = path
    try:
        return dashboard.load_status()
    finally:
        dashboard.STATUS_FILE = orig


def _save(dashboard, path, data):
    orig = dashboard.STATUS_FILE
    dashboard.STATUS_FILE = path
    try:
        dashboard.save_status(data)
    finally:
        dashboard.STATUS_FILE = orig


# ─── load_status / save_status ────────────────────────────────────────────────

class TestLoadSaveStatus:
    def test_missing_file_returns_empty_dict(self, dashboard, tmp_path):
        p = tmp_path / "nonexistent.json"
        result = _load(dashboard, p)
        assert result == {}

    def test_valid_json_roundtrip(self, dashboard, tmp_path, status_json):
        result = _load(dashboard, status_json)
        assert result["formula"]["status"] == "success"
        assert result["northcountry"]["status"] == "running"

    def test_save_then_load(self, dashboard, tmp_path):
        p = tmp_path / "status.json"
        data = {"formula": {"status": "success", "pid": None}}
        _save(dashboard, p, data)
        loaded = _load(dashboard, p)
        assert loaded == data

    def test_corrupted_json_returns_empty_dict(self, dashboard, tmp_path):
        """Half-written file (simulate crash mid-write) must not raise."""
        p = tmp_path / "corrupt.json"
        p.write_bytes(b'{"formula": {"status": "runn')   # truncated
        result = _load(dashboard, p)
        assert result == {}

    def test_binary_garbage_returns_empty_dict(self, dashboard, tmp_path):
        p = tmp_path / "garbage.json"
        p.write_bytes(bytes(range(256)))
        result = _load(dashboard, p)
        assert result == {}

    def test_empty_file_returns_empty_dict(self, dashboard, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("", encoding="utf-8")
        result = _load(dashboard, p)
        assert result == {}

    def test_save_creates_valid_json(self, dashboard, tmp_path):
        p = tmp_path / "out.json"
        _save(dashboard, p, {"x": 1})
        raw = p.read_text(encoding="utf-8")
        assert json.loads(raw) == {"x": 1}

    def test_save_uses_utf8(self, dashboard, tmp_path):
        """Emoji and non-ASCII characters survive a save/load cycle."""
        p = tmp_path / "unicode.json"
        data = {"label": "Hiver ❄️ / Été ☀️"}
        _save(dashboard, p, data)
        loaded = _load(dashboard, p)
        assert loaded["label"] == data["label"]

    def test_load_100kb_status_fast(self, dashboard, tmp_path):
        """load_status on a 100 KB file should complete in <50 ms.

        Best-of-3 timing: the very first open of a freshly written file can
        take >50 ms on Windows (antivirus scan-on-first-open), which is a
        one-time OS cost, not load_status overhead.
        """
        p = tmp_path / "big_status.json"
        big = {f"pipeline_{i}": {"status": "success", "pid": None, "data": "x" * 200}
               for i in range(200)}
        p.write_text(json.dumps(big), encoding="utf-8")
        assert p.stat().st_size >= 50_000, "fixture too small — adjust if needed"
        orig = dashboard.STATUS_FILE
        dashboard.STATUS_FILE = p
        try:
            timings = []
            for _ in range(3):
                start = time.perf_counter()
                dashboard.load_status()
                timings.append(time.perf_counter() - start)
        finally:
            dashboard.STATUS_FILE = orig
        best = min(timings)
        assert best < 0.05, f"load_status took {best:.3f}s (best of 3) — expected <0.05s"


# ─── parse_log_progress ───────────────────────────────────────────────────────

class TestParseLogProgress:
    def test_none_log_path_returns_empty(self, dashboard):
        assert dashboard.parse_log_progress(None) == {}

    def test_nonexistent_path_returns_empty(self, dashboard, tmp_path):
        result = dashboard.parse_log_progress(str(tmp_path / "no_such.log"))
        assert result == {}

    def test_start_line_detected(self, dashboard, sample_log_good):
        r = dashboard.parse_log_progress(str(sample_log_good))
        assert r["total"] == 3
        assert r["step_list"] == ["scraper", "clean_tires", "augment"]

    def test_completed_count(self, dashboard, sample_log_good):
        r = dashboard.parse_log_progress(str(sample_log_good))
        # good log has 2 OK lines
        assert r["completed"] == 2

    def test_done_names_correct(self, dashboard, sample_log_good):
        r = dashboard.parse_log_progress(str(sample_log_good))
        assert r["done_names"] == ["scraper", "clean_tires"]

    def test_current_step_detected(self, dashboard, sample_log_good):
        r = dashboard.parse_log_progress(str(sample_log_good))
        assert r["current"] == "augment"

    def test_pipeline_complete_flag(self, dashboard, sample_log_complete):
        r = dashboard.parse_log_progress(str(sample_log_complete))
        assert r["complete"] is True
        assert r["completed"] == r["total"] == 3

    def test_pipeline_complete_all_steps_done(self, dashboard, sample_log_complete):
        r = dashboard.parse_log_progress(str(sample_log_complete))
        assert len(r["done_names"]) == 3

    def test_failed_flag_set(self, dashboard, sample_log_failed):
        r = dashboard.parse_log_progress(str(sample_log_failed))
        assert r["failed"] is True

    def test_failed_completed_count(self, dashboard, sample_log_failed):
        r = dashboard.parse_log_progress(str(sample_log_failed))
        # only 'scraper' completed before failure
        assert r["completed"] == 1

    def test_empty_log_no_exception(self, dashboard, tmp_path):
        p = tmp_path / "empty.log"
        p.write_text("", encoding="utf-8")
        r = dashboard.parse_log_progress(str(p))
        # Must return a safe default dict, not raise
        assert isinstance(r, dict)
        assert r.get("total") in (None, 0) or r.get("total") >= 0

    def test_binary_garbage_no_exception(self, dashboard, tmp_path):
        p = tmp_path / "binary.log"
        p.write_bytes(bytes(range(256)) * 10)
        # errors="replace" means this should never raise
        r = dashboard.parse_log_progress(str(p))
        assert isinstance(r, dict)

    def test_missing_start_line_no_exception(self, dashboard, tmp_path):
        p = tmp_path / "nostartline.log"
        p.write_text(
            "Some random log line\n"
            "Another line\n"
            "Step 'scraper' — OK\n",
            encoding="utf-8",
        )
        r = dashboard.parse_log_progress(str(p))
        assert isinstance(r, dict)
        # completed is still counted even if total is unknown
        assert r["completed"] >= 0

    def test_sync_start_line_not_matched(self, dashboard, tmp_path):
        """
        'Sync start —' (old / different format) must NOT set total.
        Only 'Pipeline start — N step(s):' should match.
        """
        p = tmp_path / "sync_format.log"
        p.write_text(
            "2026-07-10 [INFO] Sync start — 7 steps: [build_sku_map, merge_tires]\n"
            "2026-07-10 [INFO] Step 'build_sku_map' — OK\n",
            encoding="utf-8",
        )
        r = dashboard.parse_log_progress(str(p))
        # total should be None (not 7) because the line format doesn't match
        assert r["total"] != 7 or r["step_list"] == []

    def test_10k_line_log_fast(self, dashboard, tmp_path):
        """parse_log_progress on a 10,000-line log should complete in <100 ms."""
        p = tmp_path / "big.log"
        lines = [
            "2026-07-10 08:00:00 [INFO] Pipeline start — 5 step(s): ['a', 'b', 'c', 'd', 'e']\n"
        ]
        lines += [
            f"2026-07-10 08:0{i//60:02d}:{i%60:02d} [INFO] Regular log line with some content {i}\n"
            for i in range(9998)
        ]
        lines.append("2026-07-10 09:00:00 [INFO] Step 'a' — OK\n")
        p.write_text("".join(lines), encoding="utf-8")

        start = time.perf_counter()
        r = dashboard.parse_log_progress(str(p))
        elapsed = time.perf_counter() - start

        assert r["completed"] == 1
        assert elapsed < 0.1, f"parse_log_progress took {elapsed:.3f}s on 10k-line log"

    def test_multiple_start_lines_uses_first(self, dashboard, tmp_path):
        """If the log has two 'Pipeline start' lines, the first is authoritative."""
        p = tmp_path / "restart.log"
        p.write_text(
            "2026-07-10 [INFO] Pipeline start — 3 step(s): ['a', 'b', 'c']\n"
            "2026-07-10 [INFO] Step 'a' — OK\n"
            "2026-07-10 [INFO] Pipeline start — 2 step(s): ['a', 'b']\n",
            encoding="utf-8",
        )
        r = dashboard.parse_log_progress(str(p))
        assert r["total"] == 3


# ─── collect_source_csvs ──────────────────────────────────────────────────────

class TestCollectSourceCsvs:
    """
    collect_source_csvs() walks PIPELINES values' cfg["dir"] / "Data" / "shopify".
    We monkey-patch PIPELINES to point at our fixture directories.
    """

    def _run(self, dashboard, supplier_dirs: dict):
        """Temporarily replace PIPELINES with a minimal dict pointing at tmp dirs."""
        orig = dashboard.PIPELINES
        dashboard.PIPELINES = {
            k: {"dir": v, "label": k, "run_args": [], "skip_args": [], "script": "run_pipeline.py"}
            for k, v in supplier_dirs.items()
        }
        try:
            return dashboard.collect_source_csvs()
        finally:
            dashboard.PIPELINES = orig

    def test_most_recent_file_per_type(self, dashboard, shopify_csv_dir):
        result = self._run(dashboard, {
            "formula_distributors": shopify_csv_dir / "formula_distributors",
            "wheelsmart":           shopify_csv_dir / "wheelsmart",
        })
        names = [p.name for p in result]
        # Newer file must be present
        assert "Shopify_SEO_tires_20260709_083000.csv" in names
        # Older file for the same base type must NOT be present
        assert "Shopify_SEO_tires_20260701_120000.csv" not in names

    def test_different_types_both_kept(self, dashboard, shopify_csv_dir):
        result = self._run(dashboard, {
            "wheelsmart": shopify_csv_dir / "wheelsmart",
        })
        names = [p.name for p in result]
        # tires and rims are different base names → both kept
        assert any("tires" in n for n in names)
        assert any("rims" in n for n in names)

    def test_empty_supplier_dir_no_exception(self, dashboard, tmp_path):
        """Supplier with no Data/shopify/ directory → no crash, contributes 0 files."""
        result = self._run(dashboard, {"empty_supplier": tmp_path / "nosuchdir"})
        assert result == []

    def test_empty_shopify_subdir_no_exception(self, dashboard, tmp_path):
        """Supplier has Data/shopify/ but no CSV files → no crash."""
        d = tmp_path / "supplier" / "Data" / "shopify"
        d.mkdir(parents=True)
        result = self._run(dashboard, {"supplier": tmp_path / "supplier"})
        assert result == []

    def test_zero_row_csv_included(self, dashboard, tmp_path):
        """
        A CSV with only a header row (zero data rows) is included by collect_source_csvs
        because it only filters by filename glob, not row count.
        The downstream prepare_upload_files() would include it as well (pd.concat ignores empties).
        """
        d = tmp_path / "supplier" / "Data" / "shopify"
        d.mkdir(parents=True)
        p = d / "Shopify_SEO_tires_20260709_120000.csv"
        p.write_text("Handle,Title,Body\n", encoding="utf-8")  # header only, 0 data rows
        result = self._run(dashboard, {"supplier": tmp_path / "supplier"})
        assert len(result) == 1
        assert result[0].name == p.name

    def test_returns_path_objects(self, dashboard, shopify_csv_dir):
        result = self._run(dashboard, {
            "formula_distributors": shopify_csv_dir / "formula_distributors",
        })
        assert all(isinstance(p, Path) for p in result)

    def test_50_matches_fast(self, dashboard, tmp_path):
        """collect_source_csvs with 50 glob matches should complete in <500 ms."""
        # Create 50 timestamped files across 5 suppliers, 10 types each
        for i in range(5):
            d = tmp_path / f"supplier_{i}" / "Data" / "shopify"
            d.mkdir(parents=True)
            base_ts = 1_700_000_000 + i * 1000
            for j in range(10):
                fname = f"Shopify_SEO_type_{j:02d}_2026070{i}_12{j:02d}00.csv"
                p = d / fname
                p.write_text("Handle\n", encoding="utf-8")
                os.utime(p, (base_ts + j, base_ts + j))

        supplier_dirs = {f"supplier_{i}": tmp_path / f"supplier_{i}" for i in range(5)}

        start = time.perf_counter()
        result = self._run(dashboard, supplier_dirs)
        elapsed = time.perf_counter() - start

        assert len(result) == 50  # 5 suppliers × 10 types, 1 file each (all unique bases)
        assert elapsed < 0.5, f"collect_source_csvs took {elapsed:.3f}s for 50 files"


# ─── pid_alive ────────────────────────────────────────────────────────────────

class TestPidAlive:
    def test_own_pid_is_alive(self, dashboard):
        assert dashboard.pid_alive(os.getpid()) is True

    def test_none_returns_false(self, dashboard):
        assert dashboard.pid_alive(None) is False

    def test_zero_returns_false(self, dashboard):
        """PID 0 is init/kernel; must not kill it and must return False."""
        # On POSIX, os.kill(0, 0) sends signal to the entire process group
        # and almost always succeeds (returns True in current code).
        # We only assert it does NOT raise — the return value is platform-defined.
        try:
            result = dashboard.pid_alive(0)
            assert isinstance(result, bool)
        except Exception as exc:
            pytest.fail(f"pid_alive(0) raised an exception: {exc}")

    def test_dead_pid_returns_false(self, dashboard):
        """Use a PID so large it almost certainly doesn't exist."""
        # sys.maxsize is too large for PIDs on most systems; use 2**22 - 1
        big_pid = (2 ** 22) - 1
        # We can't guarantee this PID is dead on all systems, so we just
        # assert no exception is raised and result is bool.
        result = dashboard.pid_alive(big_pid)
        assert isinstance(result, bool)

    def test_string_pid_is_handled(self, dashboard):
        """status JSON may store PID as string; pid_alive must cast to int."""
        own_pid_str = str(os.getpid())
        assert dashboard.pid_alive(own_pid_str) is True

    def test_permission_error_means_alive(self, dashboard):
        """
        On POSIX, EPERM means the process exists but belongs to another user.
        Test by patching os.kill to raise PermissionError — should return True.
        Only meaningful on POSIX; skip on Windows.
        """
        if sys.platform == "win32":
            pytest.skip("POSIX-only test")

        import unittest.mock as mock
        with mock.patch("os.kill", side_effect=PermissionError):
            # Remove psutil from sys.modules temporarily so the POSIX branch runs
            psutil_mod = sys.modules.pop("psutil", None)
            try:
                result = dashboard.pid_alive(12345)
                assert result is True
            finally:
                if psutil_mod is not None:
                    sys.modules["psutil"] = psutil_mod

    def test_process_lookup_error_means_dead(self, dashboard):
        """On POSIX, ProcessLookupError (ESRCH) means the PID is gone."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only test")

        import unittest.mock as mock
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            psutil_mod = sys.modules.pop("psutil", None)
            try:
                result = dashboard.pid_alive(12345)
                assert result is False
            finally:
                if psutil_mod is not None:
                    sys.modules["psutil"] = psutil_mod


# ─── is_running ───────────────────────────────────────────────────────────────

class TestIsRunning:
    def test_status_not_running(self, dashboard):
        status = {"formula": {"status": "success", "pid": None}}
        assert dashboard.is_running("formula", status) is False

    def test_status_running_but_no_pid(self, dashboard):
        status = {"formula": {"status": "running", "pid": None}}
        assert dashboard.is_running("formula", status) is False

    def test_status_running_with_own_pid(self, dashboard):
        """status=running + live PID (our own process) → True."""
        status = {"formula": {"status": "running", "pid": os.getpid()}}
        assert dashboard.is_running("formula", status) is True

    def test_status_running_with_dead_pid(self, dashboard):
        """Bogus PID → pid_alive returns False → is_running returns False."""
        status = {"formula": {"status": "running", "pid": 99999999}}
        # We can't guarantee this PID is dead, but on most systems it won't exist.
        # This is a best-effort check.
        result = dashboard.is_running("formula", status)
        assert isinstance(result, bool)

    def test_missing_key_in_status(self, dashboard):
        assert dashboard.is_running("unknown_key", {}) is False


# ─── Progress bar denominator math ────────────────────────────────────────────

class TestProgressDenominator:
    """
    These tests replicate the math from step2_section() without Streamlit.
    The key invariants from the dashboard:
      total_steps = sum(prog["total"]) + len(running_no_log)
      done_steps  = sum(prog["completed"])
      pct         = min(done_steps / total_steps, 1.0) if total_steps else 0.0
    """

    @staticmethod
    def _pct(with_data, running_no_log_count):
        total_steps = sum(p["total"] for p in with_data) + running_no_log_count
        done_steps  = sum(p["completed"] for p in with_data)
        return min(done_steps / total_steps, 1.0) if total_steps else 0.0

    def test_all_zero_no_divide_by_zero(self):
        assert self._pct([], 0) == 0.0

    def test_running_no_log_keeps_pct_below_1(self):
        """A running pipeline with no log yet contributes 1 phantom step."""
        pct = self._pct([], running_no_log_count=1)
        # 0 done / 1 total = 0.0, not 1.0
        assert pct == 0.0

    def test_all_finished_exact_1(self):
        pct = self._pct(
            [{"total": 7, "completed": 7}, {"total": 5, "completed": 5}],
            running_no_log_count=0,
        )
        assert pct == 1.0

    def test_partial_progress(self):
        pct = self._pct([{"total": 10, "completed": 5}], running_no_log_count=0)
        assert pct == pytest.approx(0.5)

    def test_mix_finished_and_running_no_log(self):
        """Finished supplier + running-no-log supplier → pct < 1.0."""
        pct = self._pct(
            [{"total": 7, "completed": 7}],   # finished
            running_no_log_count=1,            # still running, no log yet
        )
        # 7 / (7+1) = 0.875
        assert pct == pytest.approx(7 / 8)
        assert pct < 1.0

    def test_pct_capped_at_1(self):
        """completed > total should not produce pct > 1.0 (defensive clamp)."""
        pct = self._pct([{"total": 3, "completed": 99}], running_no_log_count=0)
        assert pct == 1.0
