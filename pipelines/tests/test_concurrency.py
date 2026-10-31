"""
Thread-safety tests for status file writes.

Covers:
- 5 threads calling _update_status() simultaneously → no data loss
- 2 threads calling _update_status() at the same moment → JSON integrity
- Half-written file (simulate crash mid-write) → load_status() recovers
- Sequential interleaving of load→modify→save under the lock

Design note on STATUS_FILE redirection
---------------------------------------
dashboard.STATUS_FILE is a module-level Path that _update_status() reads at
call time (not at function-definition time).  Redirecting it is safe ONLY
when done before any threads are spawned and restored after all threads join.
We use a pytest fixture pattern to achieve this.  Per-thread context managers
that mutate a shared global are NOT safe.
"""

import json
import sys
import threading
import time
import types
import contextlib
from pathlib import Path

import pytest

# ── Streamlit stub ────────────────────────────────────────────────────────────
if "streamlit" not in sys.modules:
    _st = types.ModuleType("streamlit")

    class _Noop:
        def __getattr__(self, n): return _Noop()
        def __call__(self, *a, **kw):
            if a and isinstance(a[0], (list, tuple)):
                return tuple(_Noop() for _ in a[0])
            if a and isinstance(a[0], int):
                return tuple(_Noop() for _ in range(a[0]))
            return _Noop()
        def __enter__(self): return _Noop()
        def __exit__(self, *a): return False
        def __iter__(self): return iter([])
        def __bool__(self): return False

    _noop = _Noop()

    def _fragment_deco(*a, **kw):
        def _wrap(fn):
            try: fn()
            except Exception: pass
            return fn
        return _wrap

    _RESOURCE_CACHE = {}

    def _make_cache_deco(memoize):
        # See conftest._make_cache_decorator - the lock these tests exercise now
        # comes from @st.cache_resource, so this stub MUST return the same
        # object on every call, and across a re-exec of the module, or the
        # tests below prove nothing.
        def _wrap(fn):
            if not memoize:
                return fn
            key = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))

            def _cached(*a, **kw):
                if key not in _RESOURCE_CACHE:
                    _RESOURCE_CACHE[key] = fn(*a, **kw)
                return _RESOURCE_CACHE[key]
            return _cached

        def _deco(*a, **kw):
            if len(a) == 1 and callable(a[0]) and not kw:
                return _wrap(a[0])
            return _wrap
        return _deco

    _st.cache_data     = _make_cache_deco(memoize=False)
    _st.cache_resource = _make_cache_deco(memoize=True)
    _st.runtime        = types.SimpleNamespace(exists=lambda: False)
    _st.fragment       = _fragment_deco
    _st.__getattr__ = lambda n: _noop   # type: ignore[assignment]
    for _a in ("set_page_config","title","header","caption","divider","button",
               "checkbox","columns","container","expander","progress","info",
               "success","error","warning","code","markdown","write",
               "subheader","spinner","rerun","sidebar"):
        setattr(_st, _a, _noop)
    sys.modules["streamlit"] = _st

sys.argv = ["dashboard.py"]

import importlib.util
_PIPELINES_DIR = Path(__file__).parent.parent

def _import_dashboard():
    spec = importlib.util.spec_from_file_location(
        "dashboard_conc", _PIPELINES_DIR / "dashboard.py"
    )
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

_dashboard = _import_dashboard()


# ─── Helpers ──────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _use_status_file(new_path: Path):
    """
    Redirect _dashboard.STATUS_FILE for the duration of a single-threaded block.
    MUST NOT be used across thread boundaries — set the path before spawning
    threads and restore after joining them.
    """
    orig = _dashboard.STATUS_FILE
    _dashboard.STATUS_FILE = new_path
    try:
        yield
    finally:
        _dashboard.STATUS_FILE = orig


def _run_threads(workers, timeout=15):
    """Start and join a list of (Thread,) objects; return the thread list."""
    threads = [threading.Thread(target=w) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    return threads


# ─── Test cases ───────────────────────────────────────────────────────────────

class TestConcurrentStatusWrites:

    def test_five_threads_no_data_loss(self, tmp_path):
        """
        5 threads each call _update_status() for a different supplier key.
        After all threads finish, all 5 keys must be present with correct values.
        STATUS_FILE is redirected once (single-threaded) before threads start.
        """
        status_file = tmp_path / ".dashboard_status.json"
        keys = ["formula", "northcountry", "wheelsmart", "emergetires", "dttire"]
        errors = []

        def worker(key):
            try:
                _dashboard._update_status(key, {"status": "success", "pid": None, "tag": key})
            except Exception as exc:
                errors.append(f"{key}: {exc}")

        with _use_status_file(status_file):
            _run_threads([lambda k=k: worker(k) for k in keys])
            assert not errors, f"Threads raised exceptions: {errors}"
            final = _dashboard.load_status()

        for key in keys:
            assert key in final, f"Key '{key}' missing after concurrent writes"
            assert final[key]["status"] == "success"
            assert final[key]["tag"] == key

    def test_two_threads_simultaneous_no_corruption(self, tmp_path):
        """
        Two threads call _update_status() at exactly the same time.
        The resulting JSON must be valid (not corrupted/truncated).
        """
        status_file = tmp_path / ".dashboard_status.json"
        barrier     = threading.Barrier(2)
        errors      = []

        def worker(key):
            try:
                barrier.wait()   # start at the same time
                _dashboard._update_status(key, {"status": "running", "pid": 12345})
            except Exception as exc:
                errors.append(f"{key}: {exc}")

        with _use_status_file(status_file):
            _run_threads([lambda k="formula": worker(k), lambda k="wheelsmart": worker(k)])
            assert not errors, f"Threads raised: {errors}"
            raw = status_file.read_text(encoding="utf-8")

        parsed = json.loads(raw)   # JSONDecodeError if corrupted
        assert isinstance(parsed, dict)

    def test_lock_enforces_sequential_load_modify_save(self, tmp_path):
        """
        Each thread increments a shared counter inside _STATUS_LOCK.
        Because load_status + save_status both run inside _update_status which
        holds the lock, the counter must reach exactly N increments.

        NOTE: This test exercises the lock semantics, not the full
        read-modify-write atomicity (which would require atomic CAS).
        We use _update_status to write a fixed value per thread, then count
        how many unique keys appeared — simpler and deterministic.
        """
        status_file = tmp_path / ".dashboard_status.json"
        n_threads   = 10
        errors      = []

        def worker(i):
            try:
                _dashboard._update_status(f"key_{i}", {"value": i})
            except Exception as exc:
                errors.append(str(exc))

        with _use_status_file(status_file):
            _run_threads([lambda i=i: worker(i) for i in range(n_threads)])
            assert not errors, f"Threads raised: {errors}"
            final = _dashboard.load_status()

        assert len(final) == n_threads, (
            f"Expected {n_threads} keys, got {len(final)}: {list(final)}"
        )
        for i in range(n_threads):
            assert f"key_{i}" in final
            assert final[f"key_{i}"]["value"] == i

    def test_update_status_on_nonexistent_file(self, tmp_path):
        """
        _update_status() on a file that doesn't exist yet must create it cleanly.
        """
        status_file = tmp_path / "new_status.json"
        assert not status_file.exists()

        with _use_status_file(status_file):
            _dashboard._update_status("formula", {"status": "running", "pid": 99})

        assert status_file.exists()
        data = json.loads(status_file.read_text(encoding="utf-8"))
        assert data["formula"]["status"] == "running"

    def test_update_merges_not_replaces(self, tmp_path):
        """
        _update_status() should merge the update dict into the existing entry,
        not overwrite the entire entry.
        """
        status_file = tmp_path / "status.json"
        initial = {"formula": {"status": "running", "pid": 123, "started": "2026-07-10T08:00:00"}}
        status_file.write_text(json.dumps(initial), encoding="utf-8")

        with _use_status_file(status_file):
            _dashboard._update_status("formula", {"status": "success", "finished": "2026-07-10T10:00:00"})
            result = _dashboard.load_status()

        # 'started' and 'pid' must still be there (merge, not replace)
        assert result["formula"]["started"] == "2026-07-10T08:00:00"
        assert result["formula"]["status"]  == "success"
        assert result["formula"]["pid"]     == 123

    def test_twenty_threads_all_keys_present(self, tmp_path):
        """
        20 threads writing 20 different keys — all must survive to the final file.
        Tests that the lock prevents write-then-overwrite data loss.
        """
        status_file = tmp_path / ".dashboard_status.json"
        errors      = []

        def worker(i):
            try:
                _dashboard._update_status(f"pipeline_{i}", {"value": i})
            except Exception as exc:
                errors.append(str(exc))

        with _use_status_file(status_file):
            _run_threads([lambda i=i: worker(i) for i in range(20)])
            assert not errors, f"Exceptions: {errors}"
            raw = status_file.read_text(encoding="utf-8")
            data = json.loads(raw)

        assert len(data) == 20, (
            f"Expected 20 keys, got {len(data)}: {list(data.keys())}"
        )

    def test_five_threads_json_is_always_valid(self, tmp_path):
        """
        After concurrent writes, the file must be parseable JSON.
        Catches any scenario where write_text() is not atomic at the OS level.
        """
        status_file = tmp_path / ".dashboard_status.json"
        keys = ["formula", "northcountry", "wheelsmart", "emergetires", "dttire"]
        errors = []

        def worker(key):
            try:
                _dashboard._update_status(key, {"status": "done"})
            except Exception as exc:
                errors.append(str(exc))

        with _use_status_file(status_file):
            _run_threads([lambda k=k: worker(k) for k in keys])

        assert not errors
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            pytest.fail(f"Status file is not valid JSON after concurrent writes: {e}")
        assert isinstance(data, dict)


class TestLockSurvivesRerun:
    """The status lock must be the same object across a full Streamlit rerun.

    Regression test for the original bug: `_STATUS_LOCK = threading.Lock()` at
    module level. Streamlit exec()s the main script into a fresh module on every
    full rerun, so that handed out a NEW lock each time while the watcher threads
    started by earlier runs still held the old one. Two writers holding different
    locks is the same as no lock, and _update_status is a read-modify-write on a
    shared JSON file - the loser's write vanished. When that write was a pid, the
    supplier card fell back to "Not yet run", the Run button re-enabled, and a
    second scrape of the same supplier could be launched over the first one.

    The single-import test suite could never catch this: it only ever exec'd the
    module once, which is exactly the case where the old code looked correct.
    """

    def test_lock_identical_across_two_execs(self):
        second = _import_dashboard()
        assert _dashboard._status_lock() is second._status_lock(), (
            "status lock differs between two exec()s of dashboard.py - "
            "a rerun would leave watcher threads locking against nothing"
        )

    def test_update_status_uses_that_lock(self, tmp_path):
        """A second exec's writes must serialise against the first exec's."""
        second = _import_dashboard()
        status_file = tmp_path / ".dashboard_status.json"
        errors = []

        def worker(mod, key):
            try:
                mod._update_status(key, {"value": key})
            except Exception as exc:
                errors.append(str(exc))

        orig_a, orig_b = _dashboard.STATUS_FILE, second.STATUS_FILE
        _dashboard.STATUS_FILE = second.STATUS_FILE = status_file
        try:
            _run_threads(
                [lambda i=i: worker(_dashboard if i % 2 else second, f"k_{i}")
                 for i in range(20)]
            )
            assert not errors, f"Threads raised: {errors}"
            final = _dashboard.load_status()
        finally:
            _dashboard.STATUS_FILE, second.STATUS_FILE = orig_a, orig_b

        assert len(final) == 20, (
            f"Expected 20 keys, got {len(final)}: {sorted(final)}"
        )


class TestCrashRecovery:

    def test_half_written_json_returns_empty(self, tmp_path):
        """
        Simulates a crash mid-write: load_status() on a truncated JSON file
        must return {} (not raise an exception).
        """
        status_file = tmp_path / ".dashboard_status.json"
        truncated_inputs = [
            b'',
            b'{',
            b'{"formula":',
            b'{"formula": {"status": "runn',
            b'{"formula": {"status": "success"}, "nc":',
        ]
        with _use_status_file(status_file):
            for truncated in truncated_inputs:
                status_file.write_bytes(truncated)
                result = _dashboard.load_status()
                assert result == {}, (
                    f"Expected empty dict for truncated input {truncated!r}, got {result!r}"
                )

    def test_full_zero_bytes_returns_empty(self, tmp_path):
        """A file of all null bytes is not valid JSON → must return {}."""
        status_file = tmp_path / ".dashboard_status.json"
        status_file.write_bytes(b"\x00" * 512)
        with _use_status_file(status_file):
            result = _dashboard.load_status()
        assert result == {}

    def test_valid_json_after_crash_recovery(self, tmp_path):
        """
        After a corrupted file, a subsequent successful write must produce valid JSON.
        """
        status_file = tmp_path / ".dashboard_status.json"
        status_file.write_bytes(b"garbage not json")

        with _use_status_file(status_file):
            s = _dashboard.load_status()
            assert s == {}
            _dashboard._update_status("formula", {"status": "success"})
            result = _dashboard.load_status()

        assert result["formula"]["status"] == "success"

    def test_binary_garbage_returns_empty(self, tmp_path):
        """Binary garbage (non-UTF-8 bytes) must return {} without raising."""
        status_file = tmp_path / ".dashboard_status.json"
        status_file.write_bytes(bytes(range(256)))
        with _use_status_file(status_file):
            result = _dashboard.load_status()
        assert result == {}
