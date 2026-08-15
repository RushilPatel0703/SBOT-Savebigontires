"""
Arg construction correctness for all suppliers.

dashboard.py PIPELINES schema:
  always_skip  — steps skipped on EVERY dashboard run (create_* / update_images*
                 steps that write to Shopify; client uploads CSVs manually)
  scrape_steps — scrape steps, skipped when "Skip scraping" is checked
  full_option  — supplier always scrapes fresh (no skip-scraping mode); a
                 "Full scrape" checkbox passes --full to rebuild the
                 zero-terms cache
pipeline_args(key, cfg) assembles the CLI args from the checkbox session state.

Verifies:
1. always_skip must contain every create_*/update_images* step (otherwise a
   dashboard run would push to Shopify unintentionally).

2. pipeline_args must skip all Shopify-write steps in every mode (default,
   skip-scraping, full scrape) and must never skip scrapers by default.

3. NC must always scrape (full_option), and brand_sweep must NOT be in
   always_skip (runs need the gap-fill).

4. wheelsmart scrape_steps must include both scraper_tires and scraper_rims.

5. All step names referenced in always_skip/scrape_steps must exist in that
   supplier's STEPS list (no typos that silently do nothing).

6. run_all.py SUPPLIERS still uses run_args/skip_args — same checks in the
   old schema (separate from dashboard.py PIPELINES).
"""

import sys
import types
from pathlib import Path

import pytest

# ── Streamlit stub (in case conftest hasn't run yet in isolation) ──────────────
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

    _st.cache_data = lambda *a, **kw: (lambda f: f)
    _st.fragment   = _fragment_deco
    _st.__getattr__ = lambda n: _noop   # type: ignore[assignment]
    for _a in ("set_page_config","title","header","caption","divider","button",
               "checkbox","columns","container","expander","progress","info",
               "success","error","warning","code","markdown","write",
               "subheader","spinner","rerun","sidebar"):
        setattr(_st, _a, _noop)
    sys.modules["streamlit"] = _st

sys.argv = ["dashboard.py"]

import importlib.util, os

_PIPELINES_DIR = Path(__file__).parent.parent

def _import(rel_path: str):
    spec = importlib.util.spec_from_file_location("_mod", _PIPELINES_DIR / rel_path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

_dashboard = _import("dashboard.py")
_run_all   = _import("run_all.py")


# ─── Per-supplier STEPS lookup ─────────────────────────────────────────────────
# Maps dashboard.py PIPELINES key → the STEPS list from that supplier's run_pipeline.py

def _load_supplier_steps(supplier_key: str) -> list[str]:
    """Return the STEP_NAMES list from a supplier's run_pipeline.py."""
    cfg      = _dashboard.PIPELINES[supplier_key]
    pipeline = cfg["dir"] / cfg["script"]
    if not pipeline.exists():
        pytest.skip(f"run_pipeline.py not found for {supplier_key}: {pipeline}")
    spec = importlib.util.spec_from_file_location(f"run_pipeline_{supplier_key}", pipeline)
    mod  = importlib.util.module_from_spec(spec)
    # These scripts use relative paths for logging; we just need STEPS / STEP_NAMES
    # Patch os.makedirs to prevent side effects during import
    import unittest.mock as mock
    with mock.patch("os.makedirs"):
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    if hasattr(mod, "STEP_NAMES"):
        return mod.STEP_NAMES
    elif hasattr(mod, "STEPS"):
        return [n for n, *_ in mod.STEPS]
    return []


# ─── Helper: extract --skip argument values ────────────────────────────────────

def _skip_values(args: list) -> set[str]:
    """
    Given a args list like ['--skip', 'create_tires', 'create_wheels'],
    return the set of values that follow '--skip'.
    """
    values = set()
    collect = False
    for tok in args:
        if tok == "--skip":
            collect = True
            continue
        if tok.startswith("--"):
            collect = False
            continue
        if collect:
            values.add(tok)
    return values


def _create_steps_skipped(args: list) -> set[str]:
    """Return the subset of --skip values that start with 'create_'."""
    return {v for v in _skip_values(args) if v.startswith("create_")}


# ─── Helper: run pipeline_args with a controlled checkbox session state ───────

def _args_with_state(key: str, state: dict) -> list:
    """Call dashboard.pipeline_args with a real dict as st.session_state."""
    _st = sys.modules["streamlit"]
    had_attr = "session_state" in vars(_st)
    old = vars(_st).get("session_state")
    _st.session_state = state
    try:
        return _dashboard.pipeline_args(key, _dashboard.PIPELINES[key])
    finally:
        if had_attr:
            _st.session_state = old
        else:
            del _st.session_state


def _shopify_write_steps(step_names: list[str]) -> set[str]:
    """Steps that write to Shopify (product creation and image upload)."""
    return {s for s in step_names if s.startswith(("create_", "update_images"))}


# ─── Tests: dashboard.py PIPELINES ────────────────────────────────────────────

PIPELINE_KEYS = list(_dashboard.PIPELINES.keys())


@pytest.mark.parametrize("key", PIPELINE_KEYS)
class TestDashboardPipelineArgs:

    def test_always_skip_covers_all_shopify_write_steps(self, key):
        """
        always_skip must contain every create_*/update_images* step so no
        Shopify import happens during a pipeline run from the dashboard.
        """
        cfg         = _dashboard.PIPELINES[key]
        step_names  = _load_supplier_steps(key)
        write_steps = _shopify_write_steps(step_names)

        if not write_steps:
            pytest.skip(f"No Shopify-write steps in {key} pipeline")

        missing = write_steps - set(cfg["always_skip"])
        assert not missing, (
            f"[{key}] always_skip does not cover these Shopify-write steps: {missing}. "
            f"This would push to Shopify unintentionally."
        )

    def test_no_step_name_typos_in_always_skip(self, key):
        """Every step name in always_skip must exist in that supplier's STEPS."""
        cfg        = _dashboard.PIPELINES[key]
        step_names = _load_supplier_steps(key)
        if not step_names:
            pytest.skip(f"Could not load STEPS for {key}")
        unknown = set(cfg["always_skip"]) - set(step_names)
        assert not unknown, (
            f"[{key}] always_skip references step(s) not in STEPS: {unknown}\n"
            f"  Valid steps: {step_names}"
        )

    def test_no_step_name_typos_in_scrape_steps(self, key):
        """Every step name in scrape_steps must exist in that supplier's STEPS."""
        cfg        = _dashboard.PIPELINES[key]
        step_names = _load_supplier_steps(key)
        if not step_names:
            pytest.skip(f"Could not load STEPS for {key}")
        unknown = set(cfg["scrape_steps"]) - set(step_names)
        assert not unknown, (
            f"[{key}] scrape_steps references step(s) not in STEPS: {unknown}\n"
            f"  Valid steps: {step_names}"
        )

    def test_default_args_skip_shopify_writes(self, key):
        """With no checkboxes touched, pipeline_args must skip all always_skip steps."""
        skipped = _skip_values(_args_with_state(key, {}))
        cfg     = _dashboard.PIPELINES[key]
        missing = set(cfg["always_skip"]) - skipped
        assert not missing, (
            f"[{key}] default pipeline_args does not skip: {missing}"
        )

    def test_skip_scraping_args_skip_shopify_writes_and_scrapers(self, key):
        """
        'Skip scraping' must skip the scrapers AND still skip all
        Shopify-write steps.
        """
        cfg = _dashboard.PIPELINES[key]
        if cfg.get("full_option"):
            pytest.skip(f"{key} has no skip-scraping mode — it always scrapes")
        skipped = _skip_values(_args_with_state(key, {f"skip_{key}": True}))
        missing = (set(cfg["always_skip"]) | set(cfg["scrape_steps"])) - skipped
        assert not missing, (
            f"[{key}] skip-scraping pipeline_args does not skip: {missing}"
        )

    def test_default_run_scrapes(self, key):
        """With no checkboxes touched, the scrape steps must NOT be skipped."""
        cfg     = _dashboard.PIPELINES[key]
        skipped = _skip_values(_args_with_state(key, {}))
        wrongly_skipped = set(cfg["scrape_steps"]) & skipped
        assert not wrongly_skipped, (
            f"[{key}] default pipeline_args skips scrape steps: {wrongly_skipped}"
        )


class TestNorthCountrySpecific:
    def test_nc_has_full_option(self):
        """NC always scrapes fresh; the checkbox toggles --full, not skip-scraping."""
        assert _dashboard.PIPELINES["northcountry"].get("full_option") is True

    def test_nc_default_run_scrapes_everything(self):
        """A default NC run must run all scrape steps (fresh inventory every run)."""
        cfg     = _dashboard.PIPELINES["northcountry"]
        skipped = _skip_values(_args_with_state("northcountry", {}))
        assert not set(cfg["scrape_steps"]) & skipped, (
            "northcountry default run must not skip any scrape step — "
            "inventory would silently go stale"
        )

    def test_nc_always_skip_does_not_include_brand_sweep(self):
        """
        A full scrape SHOULD run brand_sweep.
        If always_skip contains it, gap-fill data is silently missing every time.
        """
        cfg = _dashboard.PIPELINES["northcountry"]
        assert "brand_sweep" not in cfg["always_skip"], (
            "northcountry always_skip must NOT contain brand_sweep — "
            "it is required during full scrapes"
        )

    def test_nc_full_scrape_passes_full_flag(self):
        """The 'Full scrape' checkbox must add --full so the zero-terms cache is rebuilt."""
        args = _args_with_state("northcountry", {"full_northcountry": True})
        assert "--full" in args

    def test_nc_default_run_has_no_full_flag(self):
        args = _args_with_state("northcountry", {})
        assert "--full" not in args


class TestWheelSmartSpecific:
    def test_wheelsmart_scrape_steps_include_both_scrapers(self):
        cfg = _dashboard.PIPELINES["wheelsmart"]
        assert "scraper_tires" in cfg["scrape_steps"]
        assert "scraper_rims"  in cfg["scrape_steps"]


class TestEmergeTiresSpecific:
    def test_emerge_scrape_steps_include_scraper(self):
        cfg = _dashboard.PIPELINES["emergetires"]
        assert "scraper" in cfg["scrape_steps"], (
            "emergetires scrape_steps must include 'scraper'"
        )


class TestDtTireSpecific:
    def test_dttire_scrape_steps_include_both_scrapers(self):
        cfg = _dashboard.PIPELINES["dttire"]
        assert "scraper_tires"  in cfg["scrape_steps"]
        assert "scraper_wheels" in cfg["scrape_steps"]


# ─── Tests: run_all.py SUPPLIERS ──────────────────────────────────────────────

RUN_ALL_KEYS = list(_run_all.SUPPLIERS.keys())


@pytest.mark.parametrize("key", RUN_ALL_KEYS)
class TestRunAllSupplierArgs:
    """run_all.py SUPPLIERS has the same shape as dashboard.py PIPELINES."""

    def _steps_for_run_all_key(self, key: str) -> list[str]:
        """Map run_all.py supplier key to STEPS in that supplier's run_pipeline.py."""
        # run_all.py key 'nc' → dir is northcountry/
        cfg      = _run_all.SUPPLIERS[key]
        pipeline = cfg["dir"] / "run_pipeline.py"
        if not pipeline.exists():
            pytest.skip(f"run_pipeline.py not found for run_all key '{key}': {pipeline}")
        spec = importlib.util.spec_from_file_location(f"rp_{key}", pipeline)
        mod  = importlib.util.module_from_spec(spec)
        import unittest.mock as mock
        with mock.patch("os.makedirs"):
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        if hasattr(mod, "STEP_NAMES"):
            return mod.STEP_NAMES
        elif hasattr(mod, "STEPS"):
            return [n for n, *_ in mod.STEPS]
        return []

    def test_run_args_skip_all_create_steps(self, key):
        cfg         = _run_all.SUPPLIERS[key]
        step_names  = self._steps_for_run_all_key(key)
        create_steps = {s for s in step_names if s.startswith("create_")}
        if not create_steps:
            pytest.skip(f"No create_* steps in {key} pipeline")
        skipped = _create_steps_skipped(cfg["run_args"])
        missing = create_steps - skipped
        assert not missing, (
            f"[run_all/{key}] run_args does not skip: {missing}"
        )

    def test_skip_args_covers_create_steps(self, key):
        cfg          = _run_all.SUPPLIERS[key]
        run_creates  = _create_steps_skipped(cfg["run_args"])
        skip_creates = _create_steps_skipped(cfg["skip_args"])
        missing      = run_creates - skip_creates
        assert not missing, (
            f"[run_all/{key}] skip_args does not cover create_* from run_args: {missing}"
        )

    def test_no_typos_in_run_args(self, key):
        cfg        = _run_all.SUPPLIERS[key]
        step_names = self._steps_for_run_all_key(key)
        if not step_names:
            pytest.skip(f"Could not load STEPS for run_all/{key}")
        unknown = _skip_values(cfg["run_args"]) - set(step_names)
        assert not unknown, (
            f"[run_all/{key}] run_args typos: {unknown}. Valid: {step_names}"
        )

    def test_no_typos_in_skip_args(self, key):
        cfg        = _run_all.SUPPLIERS[key]
        step_names = self._steps_for_run_all_key(key)
        if not step_names:
            pytest.skip(f"Could not load STEPS for run_all/{key}")
        unknown = _skip_values(cfg["skip_args"]) - set(step_names)
        assert not unknown, (
            f"[run_all/{key}] skip_args typos: {unknown}. Valid: {step_names}"
        )


class TestRunAllNcSpecific:
    def test_nc_skip_args_includes_brand_sweep(self):
        cfg = _run_all.SUPPLIERS["nc"]
        assert "brand_sweep" in _skip_values(cfg["skip_args"]), (
            "run_all nc skip_args must include brand_sweep"
        )
