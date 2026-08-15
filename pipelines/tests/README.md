# Pipeline Dashboard Test Suite

## Setup

```bash
pip install pytest
# Optional but recommended — needed for pid_alive() cross-platform support:
pip install psutil
```

## Running the tests

Run all tests from the `pipelines/` directory:

```bash
cd pipelines
python -m pytest tests/ -v
```

Run a single file:

```bash
python -m pytest tests/test_dashboard_logic.py -v
```

Run only the concurrency tests:

```bash
python -m pytest tests/test_concurrency.py -v
```

Skip slow tests (not needed — the full suite runs in <10 seconds):

```bash
python -m pytest tests/ -v -m "not slow"
```

## What is tested

| File | Coverage |
|---|---|
| `test_dashboard_logic.py` | `load_status`, `save_status`, `parse_log_progress`, `collect_source_csvs`, `pid_alive`, `is_running`, progress bar math |
| `test_cross_platform.py` | `Path()` operations, glob case-sensitivity, `st_mtime` vs alphabetical sort, encoding round-trips |
| `test_pipeline_args.py` | `run_args`/`skip_args` for all 5 suppliers in both `dashboard.py` and `run_all.py` |
| `test_concurrency.py` | Thread-safety of `_update_status()`, crash/corruption recovery in `load_status()` |

## Notes

- Streamlit is stubbed — no browser or server is launched.
- No network calls, no Shopify API calls, no scraper execution.
- All tests use temporary directories; nothing is written to the project tree.
- The suite is designed to run on Windows, macOS, and Linux.
