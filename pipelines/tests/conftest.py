"""
Shared fixtures for the pipeline dashboard test suite.

Streamlit is stubbed out at import time so dashboard.py can be imported
without launching a server. Only the pure functions are exercised.
"""

import json
import sys
import time
import types
from pathlib import Path

import pytest

# ── Streamlit stub — must happen before any import of dashboard ───────────────
# Keyed on __qualname__ and held OUTSIDE the decorator, so the cache survives
# the module being exec'd again. That is the behaviour that matters: Streamlit
# re-executes the main script into a fresh module on every full rerun, and the
# real st.cache_resource keeps returning the same object across those runs. A
# stub that cached in a closure cell would hand each re-exec its own object and
# quietly reintroduce the exact bug dashboard.py uses cache_resource to avoid.
_RESOURCE_CACHE = {}


def _make_cache_decorator(memoize):
    """Stand-in for st.cache_data / st.cache_resource.

    Accepts both `@st.cache_X` and `@st.cache_X(ttl=..., ...)`.

    memoize=True matters for correctness, not just speed: dashboard.py gets its
    status-file lock from an @st.cache_resource function, and the whole point of
    that decorator there is that every caller receives the SAME Lock - across
    reruns and across browser sessions. A stub that handed back a fresh object
    per call would let the concurrency tests pass against a lock that protects
    nothing.
    """
    def _wrap(fn):
        if not memoize:
            return fn
        key = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))

        def _cached(*args, **kwargs):
            if key not in _RESOURCE_CACHE:
                _RESOURCE_CACHE[key] = fn(*args, **kwargs)
            return _RESOURCE_CACHE[key]

        _cached.__name__ = getattr(fn, "__name__", "_cached")
        _cached.clear = lambda: _RESOURCE_CACHE.pop(key, None)
        return _cached

    def _decorator(*a, **kw):
        if len(a) == 1 and callable(a[0]) and not kw:
            return _wrap(a[0])          # bare @st.cache_X
        return _wrap                     # @st.cache_X(...)
    return _decorator


def _make_st_stub():
    """
    Build a minimal streamlit module stub that absorbs all attribute access.

    Key requirement: st.columns([2, 4]) is called at module level inside
    @st.fragment-decorated functions that execute immediately.  The return value
    must be unpackable into exactly N elements (len of the spec list or tuple).
    """

    class _Noop:
        """Absorbs any attribute access, any call, and context-manager protocol."""
        def __getattr__(self, name):
            return _Noop()
        def __call__(self, *a, **kw):
            # If the first arg looks like a column-spec (list/tuple of widths),
            # return a tuple of _Noop() objects of the right length so that
            # code like  c1, c2 = st.columns([2, 4])  can unpack successfully.
            if a and isinstance(a[0], (list, tuple)):
                return tuple(_Noop() for _ in a[0])
            # For integer column count: st.columns(3) → 3-tuple
            if a and isinstance(a[0], int):
                return tuple(_Noop() for _ in range(a[0]))
            return _Noop()
        def __enter__(self):
            return _Noop()
        def __exit__(self, *a):
            return False
        def __iter__(self):
            # fallback: yield nothing (shouldn't be reached for columns())
            return iter([])
        def __bool__(self):
            return False

    st = types.ModuleType("streamlit")
    noop = _Noop()

    # Decorators used at module level in dashboard.py:
    # @st.fragment(run_every=N) must return a decorator that returns the function unchanged,
    # AND the decorated function gets called immediately at module level (step1_section(), etc.)
    # so the decorator must also invoke the function so the module-level call succeeds.
    def _fragment_decorator(*a, **kw):
        def _wrap(fn):
            # Call the function immediately so the module-level invocation works
            try:
                fn()
            except Exception:
                pass  # absorb any st.* errors inside fragment bodies
            return fn
        return _wrap

    st.cache_data     = _make_cache_decorator(memoize=False)
    st.cache_resource = _make_cache_decorator(memoize=True)
    st.fragment       = _fragment_decorator

    # A real dict, not a _Noop: dashboard.py both reads and WRITES session state
    # (e.g. the enrichment auto-scan guard), and _Noop has no __setitem__.
    st.session_state = {}

    # Everything else: delegate to the noop absorber via __getattr__
    st.__getattr__ = lambda name: noop  # type: ignore[assignment]

    # st.runtime.exists() must be falsy on a bare import: it is what stops the
    # keep-awake thread from starting and the enrichment auto-scan from
    # spawning a subprocess during a test run.
    st.runtime = types.SimpleNamespace(exists=lambda: False)

    # Explicit attrs so they resolve before __getattr__ is consulted
    for attr in (
        "set_page_config", "title", "header", "caption", "divider",
        "button", "checkbox", "columns", "container", "expander",
        "progress", "info", "success", "error", "warning", "code",
        "markdown", "write", "subheader", "spinner", "rerun",
        "sidebar",
    ):
        setattr(st, attr, noop)

    return st


if "streamlit" not in sys.modules:
    sys.modules["streamlit"] = _make_st_stub()

# ── Import the pure functions we want to test ─────────────────────────────────
# We import selectively to avoid executing the module-level Streamlit calls
# (set_page_config, st.title, etc.) in dashboard.py.
# Strategy: patch sys.argv so argparse inside any imported sub-module does not
# trip, then do a full import; the st.* calls hit our stub and do nothing.
sys.argv = ["dashboard.py"]

import importlib
import importlib.util
import os

_PIPELINES_DIR = Path(__file__).parent.parent


def _import_dashboard():
    """Import dashboard.py with Streamlit stubbed.  Returns the module."""
    spec = importlib.util.spec_from_file_location(
        "dashboard", _PIPELINES_DIR / "dashboard.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Give it a clean copy so LOGS_DIR.mkdir() etc. run in the real FS
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="session")
def dashboard():
    """Imported dashboard module (Streamlit stubbed).  Shared across all tests."""
    return _import_dashboard()


@pytest.fixture(scope="session")
def dashboard_reexec():
    """A SECOND exec of dashboard.py, as Streamlit does on every full rerun.

    Streamlit exec()s the main script into a brand-new __main__ module each full
    rerun, so anything bound at module level is rebuilt. Tests that care whether
    state survives a rerun need two separately-exec'd modules to say anything.
    """
    return _import_dashboard()


# ── SerpAPI ledger isolation ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_serp_ledger(tmp_path, monkeypatch):
    """Never let a test append to the real spend ledger.

    Autouse and unconditional on purpose. Several tests in test_enrich_runner.py
    drive the REAL enrichment.run_live() with a faked GoogleSearch, so they walk
    straight through serp_ledger.billed_call() and would otherwise write phantom
    calls into pipelines/shared/Data/serp_ledger.jsonl — the one file whose whole
    job is to reconcile against the client's SerpAPI bill. A test run must never
    be able to make that number wrong.
    """
    sys.path.insert(0, str(_PIPELINES_DIR / "shared"))
    import serp_ledger

    monkeypatch.setattr(serp_ledger, "LEDGER_FILE",
                        str(tmp_path / "ledger" / "serp_ledger.jsonl"))


# ── Temporary directory helpers ───────────────────────────────────────────────

@pytest.fixture()
def tmp_status_file(tmp_path):
    """Returns a Path pointing to a fresh .dashboard_status.json in tmp_path."""
    return tmp_path / ".dashboard_status.json"


@pytest.fixture()
def sample_log_good(tmp_path):
    """
    A well-formed pipeline log with 3 steps, 2 completed.
    Matches the format produced by formula_distributors/run_pipeline.py.
    """
    log_path = tmp_path / "good.log"
    log_path.write_text(
        "2026-07-10 08:00:00 [INFO] ===================================================\n"
        "2026-07-10 08:00:00 [INFO] Pipeline start — 3 step(s): ['scraper', 'clean_tires', 'augment']\n"
        "2026-07-10 08:00:01 [INFO] STEP: scraper\n"
        "2026-07-10 08:01:00 [INFO] Step 'scraper' — OK\n"
        "\n"
        "2026-07-10 08:01:01 [INFO] STEP: clean_tires\n"
        "2026-07-10 08:02:00 [INFO] Step 'clean_tires' — OK\n"
        "\n"
        "2026-07-10 08:02:01 [INFO] STEP: augment\n",
        encoding="utf-8",
    )
    return log_path


@pytest.fixture()
def sample_log_complete(tmp_path):
    """A log where all 3 steps finished successfully."""
    log_path = tmp_path / "complete.log"
    log_path.write_text(
        "2026-07-10 08:00:00 [INFO] Pipeline start — 3 step(s): ['scraper', 'clean_tires', 'augment']\n"
        "2026-07-10 08:00:01 [INFO] STEP: scraper\n"
        "2026-07-10 08:01:00 [INFO] Step 'scraper' — OK\n"
        "2026-07-10 08:01:01 [INFO] STEP: clean_tires\n"
        "2026-07-10 08:02:00 [INFO] Step 'clean_tires' — OK\n"
        "2026-07-10 08:02:01 [INFO] STEP: augment\n"
        "2026-07-10 08:03:00 [INFO] Step 'augment' — OK\n"
        "2026-07-10 08:03:01 [INFO] ===================================================\n"
        "2026-07-10 08:03:01 [INFO] Pipeline complete.\n"
        "2026-07-10 08:03:01 [INFO] ===================================================\n",
        encoding="utf-8",
    )
    return log_path


@pytest.fixture()
def sample_log_failed(tmp_path):
    """A log where step 2 failed."""
    log_path = tmp_path / "failed.log"
    log_path.write_text(
        "2026-07-10 08:00:00 [INFO] Pipeline start — 3 step(s): ['scraper', 'clean_tires', 'augment']\n"
        "2026-07-10 08:00:01 [INFO] STEP: scraper\n"
        "2026-07-10 08:01:00 [INFO] Step 'scraper' — OK\n"
        "2026-07-10 08:01:01 [INFO] STEP: clean_tires\n"
        "2026-07-10 08:02:00 [ERROR] Step 'clean_tires' failed (exit 1). Pipeline stopped.\n",
        encoding="utf-8",
    )
    return log_path


@pytest.fixture()
def status_json(tmp_path):
    """
    A sample .dashboard_status.json with one running entry, one success entry.
    The 'pid' values are deliberately bogus (99999999) so pid_alive() returns False.
    """
    data = {
        "formula": {
            "pid":      None,
            "status":   "success",
            "started":  "2026-07-10T08:00:00",
            "finished": "2026-07-10T10:30:00",
            "log":      "/tmp/formula_20260710_080000.log",
        },
        "northcountry": {
            "pid":      99999999,
            "status":   "running",
            "started":  "2026-07-10T09:00:00",
            "finished": None,
            "log":      "/tmp/northcountry_20260710_090000.log",
        },
    }
    p = tmp_path / ".dashboard_status.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


@pytest.fixture()
def shopify_csv_dir(tmp_path):
    """
    Creates a fake supplier Data/shopify/ tree with timestamped Shopify_SEO_*.csv files.

    Layout (three suppliers, two timestamps each):
      formula_distributors/Data/shopify/Shopify_SEO_tires_20260701_120000.csv  (older)
      formula_distributors/Data/shopify/Shopify_SEO_tires_20260709_083000.csv  (newest)
      wheelsmart/Data/shopify/Shopify_SEO_tires_20260708_100000.csv
      wheelsmart/Data/shopify/Shopify_SEO_rims_20260708_100100.csv
    """
    rows_header = "Handle,Title,Body (HTML),Vendor,Type,Tags,Published,Option1 Name\n"

    for supplier, files in {
        "formula_distributors": [
            ("Shopify_SEO_tires_20260701_120000.csv", 1),   # older — should be excluded
            ("Shopify_SEO_tires_20260709_083000.csv", 2),   # newest — should be kept
        ],
        "wheelsmart": [
            ("Shopify_SEO_tires_20260708_100000.csv", 3),
            ("Shopify_SEO_rims_20260708_100100.csv",  4),
        ],
    }.items():
        d = tmp_path / supplier / "Data" / "shopify"
        d.mkdir(parents=True)
        t = 1_700_000_000  # base epoch
        for fname, offset in files:
            p = d / fname
            p.write_text(rows_header + f"handle-{offset},Title {offset},,Vendor,,,,Size\n",
                         encoding="utf-8")
            # ensure deterministic mtime ordering: later offset → later mtime
            os.utime(p, (t + offset, t + offset))

    return tmp_path
