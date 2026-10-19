#!/usr/bin/env python3
"""
Save Big On Tires — Inventory Pipeline Dashboard
Launch: streamlit run dashboard.py  (from the pipelines/ directory)
"""

import base64
import json
import mimetypes
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import streamlit as st

_STATUS_LOCK = threading.Lock()

# Per-supplier markup lives in shared/markup.py — the same module the supplier
# pipelines and the production run price from, so what the client saves here is
# exactly what both of them use. Loaded by path: shared/ is not on sys.path.
import importlib.util as _ilu

_markup_spec = _ilu.spec_from_file_location(
    "_sbot_markup", str(Path(__file__).parent / "shared" / "markup.py"))
markup = _ilu.module_from_spec(_markup_spec)
_markup_spec.loader.exec_module(markup)

# ── Constants ──────────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
STATUS_FILE = BASE / ".dashboard_status.json"
LOGS_DIR    = BASE / "dashboard_logs"
LOGS_DIR.mkdir(exist_ok=True)

# The packaged Mac app (macapp/) sets this. That copy only produces CSVs: it has
# no production/ directory and no store credentials, so the Sync tab and the
# store-comparing "new products only" bundle are left out rather than shown and
# left to fail.
CLIENT_BUILD = os.environ.get("SBOT_CLIENT_BUILD") == "1"

# Drop a logo file named "<supplier_key>.png" (or .jpg/.jpeg/.svg/.webp) in here
# to have it prefixed on that supplier's card — falls back to the colored dot
# icon (see PIPELINES) until a matching file shows up.
LOGOS_DIR = BASE / "assets" / "logos"

def _find_logo(key: str) -> Path | None:
    if not LOGOS_DIR.exists():
        return None
    for ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
        p = LOGOS_DIR / f"{key}{ext}"
        if p.exists():
            return p
    return None

def _logo_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    b64  = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"

# Pipeline subprocesses must survive the dashboard's own terminal/console dying
# (tab closed, VS Code window reload, etc.) — otherwise a multi-hour scrape gets
# silently killed along with the dashboard.
#   Windows: CREATE_NEW_PROCESS_GROUP detaches from the parent's Ctrl+C/console
#   signals; CREATE_BREAKAWAY_FROM_JOB detaches from the parent's Job Object,
#   which is what modern terminals use to kill an entire child tree on close.
#   macOS/Linux: start_new_session=True (os.setsid) detaches into its own
#   session so the terminal closing (SIGHUP) doesn't propagate to the child.
if sys.platform == "win32":
    _POPEN_FLAGS        = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
    _START_NEW_SESSION   = False  # POSIX-only kwarg, irrelevant here

    import ctypes
    ES_CONTINUOUS       = 0x80000000
    ES_SYSTEM_REQUIRED  = 0x00000001
    # Keep the machine from idle-sleeping for as long as the dashboard is open —
    # scrapes run unattended for hours and idle sleep would kill them mid-run.
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
else:
    _POPEN_FLAGS       = 0
    _START_NEW_SESSION = True

    if sys.platform == "darwin" and not os.environ.get("_SBOT_CAFFEINATED"):
        # Same idle-sleep prevention as Windows, via caffeinate. -w ties its
        # lifetime to this process so it exits on its own when the dashboard closes.
        # This module re-runs on every interaction; the env flag keeps it to one
        # caffeinate per server rather than one per click.
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            os.environ["_SBOT_CAFFEINATED"] = "1"
        except FileNotFoundError:
            pass

PIPELINES = {
    # always_skip: applied on every run — create/image steps (client uploads CSV manually)
    # scrape_steps: skipped when "Skip scraping" is checked — reuses raw CSVs from Data/raw/
    # full_option: no skip-scraping; "Full scrape" checkbox passes --full instead
    "formula": {
        "label":        "Formula Tires",
        "dir":          BASE / "formula_distributors",
        "script":       "run_pipeline.py",
        "always_skip":  ["create_tires", "create_wheels"],
        "scrape_steps": ["scraper", "scraper_wheels"],
        "color":        "blue",
        "icon":         "🔵",
    },
    "northcountry": {
        "label":        "North Country",
        "dir":          BASE / "northcountry",
        "script":       "run_pipeline.py",
        "always_skip":  ["create_products", "update_images", "create_wheels", "update_images_wheels"],
        "scrape_steps": ["scraper_tires", "brand_sweep", "scraper_wheels"],
        # always scrapes fresh (zero-terms cache trims dead search terms);
        # "Full scrape" checkbox adds --full to re-check them all
        "full_option":  True,
        "color":        "green",
        "icon":         "🟢",
    },
    "wheelsmart": {
        "label":        "WheelMart Canada",
        "dir":          BASE / "wheelsmart",
        "script":       "run_pipeline.py",
        "always_skip":  ["create_tires", "create_rims"],
        "scrape_steps": ["scraper_tires", "scraper_rims"],
        "color":        "orange",
        "icon":         "🟠",
    },
    "emergetires": {
        "label":        "Emerge Tires",
        "dir":          BASE / "emergetires",
        "script":       "run_pipeline.py",
        # this pipeline's directory is "emergetires" but every pricing layer
        # keys the supplier as "emerge" — see markup_key_for()
        "markup_key":   "emerge",
        "always_skip":  ["create_tires"],
        "scrape_steps": ["scraper"],
        "color":        "violet",
        "icon":         "🟣",
    },
    "dttire": {
        "label":        "DT Tire",
        "dir":          BASE / "dttire",
        "script":       "run_pipeline.py",
        "always_skip":  ["create_tires", "create_wheels"],
        "scrape_steps": ["scraper_tires", "scraper_wheels"],
        "color":        "red",
        "icon":         "🔴",
    },
}

# ── Status helpers ─────────────────────────────────────────────────────────────
def load_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8")) if STATUS_FILE.exists() else {}
    except Exception:
        return {}

def save_status(status: dict):
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS_FILE)

def _update_status(key: str, update: dict):
    with _STATUS_LOCK:
        s = load_status()
        s.setdefault(key, {}).update(update)
        save_status(s)

def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        import psutil  # noqa: PLC0415
        return psutil.pid_exists(int(pid))
    except ImportError:
        if sys.platform == "win32":
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True, text=True, timeout=5,
                )
                return str(pid) in result.stdout
            except Exception:
                return False
        else:
            # macOS / Linux: signal 0 checks existence without sending a signal
            try:
                os.kill(int(pid), 0)
                return True
            except ProcessLookupError:
                return False    # ESRCH — no such process
            except PermissionError:
                return True     # EPERM — process exists but not ours to signal
            except Exception:
                return False

def is_running(key: str, status: dict) -> bool:
    s = status.get(key, {})
    return s.get("status") == "running" and pid_alive(s.get("pid"))

# ── Process runner ─────────────────────────────────────────────────────────────
def _wait_and_finish(key: str, proc: subprocess.Popen, f):
    proc.wait()
    f.close()
    _update_status(key, {
        "pid":      None,
        "status":   "success" if proc.returncode == 0 else "failed",
        "finished": datetime.now().isoformat(),
    })

def _kill_pipeline(key: str, status: dict):
    """Kill a running pipeline and its child processes, then mark as failed."""
    pid = status.get(key, {}).get("pid")
    if pid:
        try:
            import psutil
            parent = psutil.Process(int(pid))
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            parent.kill()
        except ImportError:
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, timeout=5,
                    )
                else:
                    os.kill(int(pid), 9)
            except Exception:
                pass
        except Exception:
            pass
    _update_status(key, {
        "pid":      None,
        "status":   "failed",
        "finished": datetime.now().isoformat(),
    })

def launch(key: str, cmd: list, cwd: Path):
    # Popen + the initial status write happen synchronously (on the button-click's
    # own script run) so the very next rerun already shows "Running" — spawning the
    # process from inside a background thread meant the click could appear to do
    # nothing until some unrelated widget interaction forced a re-render.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"{key}_{ts}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    # Unbuffered so scraper print() output streams to the log live instead of
    # in 8 KB bursts (the View log panel looks frozen otherwise).
    env["PYTHONUNBUFFERED"] = "1"
    f = open(log_path, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(cwd), env=env,
            creationflags=_POPEN_FLAGS, start_new_session=_START_NEW_SESSION,
        )
    except OSError:
        # Current console's Job Object may disallow breakaway — fall back to a
        # plain new process group so the pipeline can still launch.
        proc = subprocess.Popen(
            cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(cwd), env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            start_new_session=_START_NEW_SESSION,
        )
    _update_status(key, {
        "pid":      proc.pid,
        "status":   "running",
        "started":  datetime.now().isoformat(),
        "log":      str(log_path),
        "finished": None,
    })
    threading.Thread(target=_wait_and_finish, args=(key, proc, f), daemon=False).start()

PRODUCTION = BASE.parent / "production"

# production/ is driven as a subprocess rather than imported. Both this project
# and production/ have a module named `config` (pipelines/shared/config.py is
# already on sys.path above, for upload_bundle), so importing production's
# modules here would let `import config` inside them bind to the wrong file
# depending on sys.path order — and the wrong one still *works*, silently, with
# different markup and different store credentials. A subprocess gets
# production/ as its own cwd and its own resolution.
def _production_json(script: str, args: list[str], timeout: int = 180):
    """Run a production/ helper with --json and parse its stdout.

    Returns (payload, error_message) — exactly one is None.
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, script, *args, "--json"],
            cwd=str(PRODUCTION), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return None, f"{script} timed out after {timeout}s."
    except OSError as e:
        return None, f"Could not run {script}: {e}"
    # preflight exits 1 when a check fails, which is a result, not a crash —
    # so parse stdout first and only treat unparseable output as an error.
    try:
        return json.loads(proc.stdout), None
    except ValueError:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, f"{script} produced no usable output:\n{detail[-1500:]}"


# ── UI helpers ─────────────────────────────────────────────────────────────────
def status_badge(s: dict, running: bool):
    """One line of state. Deliberately not an alert panel.

    These used to be st.info/st.success/st.error, which put a tinted panel on
    the page for every step whether or not anything had happened — so a panel
    stopped meaning "read this". Coloured text carries the same state at a
    fraction of the weight, and the emoji are gone because an alert box already
    draws its own icon and a plain line does not need one.
    """
    ts = (s.get("finished") or "")[:16].replace("T", " ")
    if running:
        st.markdown(":blue[**Running…**]")
    elif s.get("status") == "success":
        st.markdown(f":green[**Done**] · {ts}")
    elif s.get("status") == "failed":
        st.markdown(f":red[**Failed**] · {ts}")
    else:
        st.caption("Not yet run")

def show_log(s: dict, label: str = "View log", expanded: bool = False):
    log_p = s.get("log")
    if not log_p:
        return
    with st.expander(label, expanded=expanded):
        try:
            content = Path(log_p).read_text(encoding="utf-8", errors="replace")
            lines   = content.strip().split("\n")
            st.code("\n".join(lines[-150:]), language="")
            if len(lines) > 150:
                st.caption(f"Showing last 150 of {len(lines)} lines — full log: {log_p}")
        except FileNotFoundError:
            st.caption("Log file not found.")

def parse_log_progress(log_path: str | None) -> dict:
    """Parse a pipeline log file and return step progress info."""
    import re
    if not log_path:
        return {}
    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    lines = content.split("\n")

    total, step_list = None, []
    for line in lines:
        m = re.search(r"Pipeline start — (\d+) step\(s\): \[(.+?)\]", line)
        if m:
            total     = int(m.group(1))
            step_list = [s.strip().strip("'\"") for s in m.group(2).split(",")]
            break

    completed_names = [
        re.search(r"Step '(.+?)' — OK", ln).group(1)
        for ln in lines if re.search(r"Step '(.+?)' — OK", ln)
    ]

    current = None
    for line in reversed(lines):
        m = re.search(r"STEP: (\S+)", line)
        if m:
            current = m.group(1)
            break

    return {
        "total":      total or len(step_list),
        "completed":  len(completed_names),
        "done_names": completed_names,
        "current":    current,
        "complete":   any("Pipeline complete." in ln for ln in lines),
        "failed":     any("failed (exit" in ln for ln in lines),
        "step_list":  step_list,
    }

# ── Page config + theme ────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Save Big On Tires — Inventory Pipeline",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Colors, fonts and the button accent live in .streamlit/config.toml. Only what
# the theme cannot reach is set here, and it stays in rgba() so the same rule
# works against either theme background.
_ACCENT = (28, 131, 225)
_a = _ACCENT
_CSS = f"""
/* Supplier rows: one quiet left rule each. The previous tinted, hover-lifting
   panels turned five stacked cards into five competing surfaces. */
{", ".join(f".st-key-card_{k}" for k in PIPELINES)} {{
    border: 1px solid rgba({_a[0]},{_a[1]},{_a[2]},0.16) !important;
    border-left: 3px solid rgba({_a[0]},{_a[1]},{_a[2]},0.7) !important;
    border-radius: 6px;
    background: transparent !important;
}}
/* Progress bars only — scoped to stProgress so buttons keep the theme accent. */
[data-testid="stProgressBarTrack"] > div {{
    background-color: rgba({_a[0]},{_a[1]},{_a[2]},0.85) !important;
}}
/* The tab strip is now the page's primary navigation, so give it some weight. */
[data-baseweb="tab-list"] {{ gap: 2px; }}
button[data-baseweb="tab"] {{ font-size: 0.95rem; font-weight: 500; }}
/* Headers sit closer to the copy that explains them. */
h2, h3 {{ padding-top: 0.2rem; padding-bottom: 0.2rem; }}
"""
st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)

# ─── Sidebar: the one overall status summary ────────────────────────────────────
# Each tab shows only its own step, so this is where "where is the run up to?"
# gets answered — and the only place it is answered, which is why the supplier
# cards no longer carry a status badge of their own.
@st.fragment(run_every=2)
def sidebar_status():
    status = load_status()
    st.sidebar.markdown("#### Progress")

    def _line(label: str, s: dict, running: bool):
        if running:
            dot, tail = ":blue[●]", " — running"
        elif s.get("status") == "success":
            dot, tail = ":green[●]", " — done"
        elif s.get("status") == "failed":
            dot, tail = ":red[●]", " — failed"
        else:
            dot, tail = ":gray[○]", ""
        st.sidebar.markdown(f"{dot} {label}{tail}")

    st.sidebar.caption("STEP 1 — COOKIES")
    _line("Cookies", status.get("cookies", {}), is_running("cookies", status))

    st.sidebar.caption("STEP 2 — SUPPLIERS")
    for key, cfg in PIPELINES.items():
        _line(cfg["label"], status.get(key, {}), is_running(key, status))
    done = sum(1 for k in PIPELINES if status.get(k, {}).get("status") == "success")
    st.sidebar.progress(done / len(PIPELINES),
                        text=f"{done} of {len(PIPELINES)} suppliers complete")

    if status.get("enrich", {}).get("status") or is_running("enrich", status):
        st.sidebar.caption("STEP 3 — ENRICHMENT")
        _line("Enrichment", status.get("enrich", {}), is_running("enrich", status))
    if status.get("sync", {}).get("status") or is_running("sync", status):
        st.sidebar.caption("STEP 5 — SHOPIFY SYNC")
        _line("Shopify sync", status.get("sync", {}), is_running("sync", status))

# ─── Step 1: Cookies ───────────────────────────────────────────────────────────
@st.fragment(run_every=3)
def step1_section():
    status  = load_status()
    s       = status.get("cookies", {})
    running = is_running("cookies", status)
    c1, c2  = st.columns([2, 4])
    with c1:
        if st.button("Refresh all cookies", disabled=running, width="stretch", type="primary"):
            launch("cookies", [sys.executable, "refresh_cookies.py"], BASE)
    with c2:
        status_badge(s, running)
    show_log(s, expanded=running)


# ─── Step 2: Supplier pipelines ────────────────────────────────────────────────
def markup_key_for(key: str) -> str:
    """Dashboard pipeline key → the supplier key the pricing layers use."""
    return PIPELINES[key].get("markup_key", key)


# Deliberately NOT an st.fragment(run_every=...): the surrounding sections
# auto-refresh every 2s, which would reset these inputs mid-edit.
def _markup_summary(rules: dict) -> str:
    """"Formula $12.00 · North Country 8%" — the whole table on one line.

    Lets the form below stay closed by default without hiding the numbers it
    holds. Markup is set once and then left alone for weeks, so an always-open
    five-row form sat above the Run buttons competing with the thing the client
    actually opened this tab to press.
    """
    parts = []
    for key, cfg in PIPELINES.items():
        rule = rules.get(markup_key_for(key), {"mode": markup.DOLLAR, "value": 0.0})
        # Escaped: Streamlit labels are Markdown, and two bare $ signs make
        # everything between them a LaTeX formula.
        amount = (f"{rule['value']:.0f}%" if rule["mode"] == markup.PERCENT
                  else f"\\${rule['value']:.2f}")
        parts.append(f"{cfg['label']} {amount}")
    return " · ".join(parts)


def markup_section():
    try:
        rules = markup.load()
    except markup.MarkupConfigError as e:
        st.error(f"Markup settings could not be read, so nothing here is safe to run: {e}")
        st.caption(f"Fix or delete `{markup.OVERRIDES_FILE}` and reload this page.")
        return

    status  = load_status()
    running = [PIPELINES[k]["label"] for k in PIPELINES if is_running(k, status)]

    with st.expander(f"Markup — {_markup_summary(rules)}"):
        st.caption(
            "Added to each supplier's cost price to get the selling price. Prices are "
            "calculated while a pipeline runs, so set this **before** running one below "
            "— a change made afterwards takes effect on the next run, not this one."
        )
        if running:
            # A markup edit mid-run would apply to whichever pipelines hadn't reached
            # their pricing step yet, silently splitting one run across two tables.
            st.warning(f"Locked while a pipeline is running ({', '.join(running)}).")
        _markup_form(rules, running)


def _markup_form(rules: dict, running: list):
    with st.form("markup_form"):
        head = st.columns([3, 3, 2])
        head[1].markdown("**Type**")
        head[2].markdown("**Amount**")

        chosen = {}
        for key, cfg in PIPELINES.items():
            mk   = markup_key_for(key)
            rule = rules.get(mk, {"mode": markup.DOLLAR, "value": 0.0})
            cols = st.columns([3, 3, 2])

            cols[0].markdown(f"**{cfg['label']}**")
            mode = cols[1].radio(
                f"{cfg['label']} markup type",
                [markup.DOLLAR, markup.PERCENT],
                index=0 if rule["mode"] == markup.DOLLAR else 1,
                format_func=lambda m: "$ per tire" if m == markup.DOLLAR else "% of cost",
                key=f"markup_mode_{mk}",
                horizontal=True,
                label_visibility="collapsed",
                disabled=bool(running),
            )
            value = cols[2].number_input(
                f"{cfg['label']} markup amount",
                min_value=0.0,
                max_value=float(markup.MAX_PERCENT if mode == markup.PERCENT else markup.MAX_DOLLAR),
                value=float(rule["value"]),
                step=0.50,
                format="%.2f",
                key=f"markup_value_{mk}",
                label_visibility="collapsed",
                disabled=bool(running),
            )
            # Left uncoerced on purpose: keyed widgets need session state, so on a
            # bare `import dashboard` (the tests do this) they hand back a sentinel
            # rather than a number. Coerced below, where a real run is guaranteed.
            chosen[mk] = {"mode": mode, "value": value}

        # Secondary: this tab exists to press Run. Saving markup supports that,
        # so it should not carry the same weight as the action it supports.
        saved = st.form_submit_button("Save markup", disabled=bool(running))

    if saved:
        merged = dict(rules)          # keep suppliers that have no dashboard card
        merged.update({mk: {"mode": str(r["mode"]), "value": float(r["value"])}
                       for mk, r in chosen.items()})
        try:
            markup.save(merged)
        except markup.MarkupConfigError as e:
            st.error(f"Not saved — {e}")
        else:
            # A toast rather than st.success: the st.rerun() on the next line
            # throws the page away, so the success panel was drawn and discarded
            # in the same instant and the client never saw a confirmation. The
            # rerun is needed — it is what refreshes the summary in the expander
            # label — and a toast is the one thing that survives it.
            st.toast("Markup saved — applies to the next pipeline run.", icon="✅")
            st.rerun()


def pipeline_args(key: str, cfg: dict) -> list:
    """Build run_pipeline.py args for a supplier from its checkbox state."""
    if cfg.get("full_option"):
        # Always scrapes; the checkbox only controls --full (exhaustive term sweep)
        full = st.session_state.get(f"full_{key}", False)
        skip = False
    else:
        skip = st.session_state.get(f"skip_{key}", False)
        full = False
    skip_steps = cfg["always_skip"] + (cfg["scrape_steps"] if skip else [])
    return (["--skip"] + skip_steps if skip_steps else []) \
         + (["--full"] if full else [])

@st.fragment(run_every=2)
def step2_section():
    status = load_status()

    # Overall progress bar
    all_prog       = {k: parse_log_progress(status.get(k, {}).get("log")) for k in PIPELINES}
    started        = [k for k in PIPELINES if status.get(k, {}).get("status")]
    with_data      = {k: all_prog[k] for k in started if all_prog[k].get("total")}
    running_no_log = [k for k in PIPELINES if is_running(k, status) and k not in with_data]
    if started or running_no_log:
        # running pipelines with no log yet contribute 1 unfinished step so bar never falsely hits 100%
        total_steps = sum(p["total"]     for p in with_data.values()) + len(running_no_log)
        done_steps  = sum(p["completed"] for p in with_data.values())
        pct_all     = min(done_steps / total_steps, 1.0) if total_steps else 0.0
        running_now = [PIPELINES[k]["label"] for k in PIPELINES if is_running(k, status)]

        # One bar, not a three-metric row above it. "Steps complete 41 / 63",
        # "Suppliers complete 2 / 5" and "Running now 1" were three restatements
        # of what the bar and its own caption already say, and the sidebar says
        # the supplier count a third time.
        bar_text = (f"{', '.join(running_now)} — step {done_steps} of {total_steps}"
                    if running_now else
                    f"{done_steps} of {total_steps} steps complete · nothing running")
        st.progress(pct_all, text=bar_text)

    for key, cfg in PIPELINES.items():
        s       = status.get(key, {})
        running = is_running(key, status)
        prog    = all_prog.get(key, {})

        # Name · option · button. The fourth column used to hold a status badge,
        # which said what the progress bar directly below it already says and the
        # sidebar says a third time — so five cards carried fifteen renderings of
        # the same five states.
        with st.container(border=True, key=f"card_{key}"):
            cols = st.columns([4, 4, 2], vertical_alignment="center")
            with cols[0]:
                logo = _find_logo(key)
                if logo:
                    st.markdown(
                        '<div style="display:flex;align-items:center;gap:12px;">'
                        f'<img src="{_logo_data_uri(logo)}" style="height:32px;width:auto;max-width:110px;object-fit:contain;">'
                        f'<span style="font-size:1.05rem;font-weight:600;line-height:1;">{cfg["label"]}</span>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(f"**{cfg['label']}**")
            with cols[1]:
                if cfg.get("full_option"):
                    st.checkbox(
                        "Full scrape",
                        key=f"full_{key}",
                        disabled=running,
                        help="Also re-check search terms that returned nothing on previous "
                             "scrapes (rebuilds the zero-terms cache — slowest). Normal runs "
                             "scrape fresh inventory but skip those known-empty terms. "
                             "Worth doing every alternate week.",
                    )
                else:
                    st.checkbox(
                        "Skip scraping",
                        key=f"skip_{key}",
                        disabled=running,
                        help="Reuse the existing raw CSV from the last scrape instead of re-downloading",
                    )
            with cols[2]:
                extra = pipeline_args(key, cfg)
                if running:
                    # Secondary: stopping a run is a correction, not the action
                    # this tab is for.
                    if st.button("Cancel", key=f"cancel_{key}", width="stretch"):
                        _kill_pipeline(key, status)
                        st.rerun()
                else:
                    if st.button("Run", key=f"btn_{key}", width="stretch", type="primary"):
                        launch(key, [sys.executable, cfg["script"]] + extra, cfg["dir"])

        # Progress bar — outside the container so it always renders. Its text no
        # longer repeats the supplier name: the card immediately above it is the
        # only thing this bar can belong to, and five bars each re-announcing
        # their own supplier was five lines of text saying nothing new.
        pipe_status = s.get("status")
        if not pipe_status:
            st.progress(0.0, text="Not yet started")
        elif prog.get("total"):
            completed = prog["completed"]
            total     = prog["total"]
            pct       = min(completed / total, 1.0)
            if running and prog.get("current"):
                bar_txt = f"Step {completed} of {total} — {prog['current']}"
            elif prog.get("complete"):
                bar_txt = f"All {total} steps complete"
            elif prog.get("failed"):
                bar_txt = f"Failed at step {completed + 1} of {total} (last: {prog.get('current', '?')})"
            else:
                bar_txt = f"Step {completed} of {total}"
            st.progress(pct, text=bar_txt)
        elif running:
            st.progress(0.0, text="Starting…")
        elif pipe_status == "failed":
            st.progress(0.0, text="Failed — see the log below")
        elif pipe_status == "success":
            st.progress(1.0, text="Complete")

        show_log(s, label=f"View log — {cfg['label']}", expanded=running)


# ─── Step 3: Data enrichment ───────────────────────────────────────────────────
ENRICH_RUNNER    = "shared/enrich_runner.py"
ENRICH_SCAN_FILE = BASE / ".enrich_scan.json"

def load_enrich_scan() -> dict:
    try:
        return json.loads(ENRICH_SCAN_FILE.read_text(encoding="utf-8")) if ENRICH_SCAN_FILE.exists() else {}
    except Exception:
        return {}

def _pipelines_signature(status: dict) -> str | None:
    """A fingerprint of "all five suppliers finished successfully", or None.

    The finish timestamps are part of it so that re-running any supplier
    produces a new signature and re-triggers the scan — the counts are read off
    those suppliers' CSVs, so a fresh pipeline run invalidates them.
    """
    stamps = []
    for key in PIPELINES:
        s = status.get(key, {})
        if s.get("status") != "success":
            return None
        stamps.append(s.get("finished") or "")
    return "|".join(stamps)

@st.fragment(run_every=3)
def step3_section():
    status    = load_status()
    scanning  = is_running("enrich_scan", status)
    enriching = is_running("enrich", status)
    busy      = scanning or enriching

    # Auto-scan, once, as soon as every supplier is green. The scan is
    # read-only and makes no API call, so firing it unprompted costs nothing.
    # st.runtime.exists() keeps it to real browser sessions: the unit suite
    # imports this module with a stubbed streamlit that executes fragment
    # bodies directly, and an import should never spawn a subprocess.
    sig = _pipelines_signature(status)
    if sig and not busy and st.runtime.exists() \
            and st.session_state.get("_enrich_scanned_for") != sig:
        st.session_state["_enrich_scanned_for"] = sig
        launch("enrich_scan", [sys.executable, ENRICH_RUNNER, "--scan"], BASE)
        scanning = busy = True

    done_count = sum(1 for k in PIPELINES if status.get(k, {}).get("status") == "success")
    scan       = load_enrich_scan()

    c1, c2 = st.columns([2, 5])
    with c1:
        if st.button("Recheck counts", disabled=busy, width="stretch"):
            st.session_state["_enrich_scanned_for"] = sig
            launch("enrich_scan", [sys.executable, ENRICH_RUNNER, "--scan"], BASE)
    with c2:
        if scanning:
            st.markdown(":blue[**Counting what needs enrichment…**]")
            st.caption("Free — the scan makes no searches.")
        elif done_count < len(PIPELINES):
            st.caption(
                f"{done_count} / {len(PIPELINES)} supplier pipelines complete. Counts appear "
                f"automatically once all of them finish — or click Recheck for a partial count now."
            )
        elif scan:
            st.caption(f"Counted {scan.get('generated', '')[:16].replace('T', ' ')} "
                       f"from the newest CSV of each supplier.")

    scan_s = status.get("enrich_scan", {})
    if scan_s.get("status") == "failed":
        st.error("The enrichment scan failed — nothing was spent. See the log for why.")
        show_log(scan_s, label="View log — enrichment scan")

    if not scan:
        if not scanning:
            st.caption("No counts yet — run Step 2, or click Recheck above.")
        return

    if scan.get("kill_switch"):
        st.error(
            "SERP kill switch is active (`monitoring/SERP_KILL_SWITCH`). Paid searches are "
            "blocked until that file is deleted. Free cache fixes below still work."
        )

    fields    = scan.get("fields", {})
    img       = fields.get("image", {})
    des       = fields.get("designation", {})
    specs     = fields.get("specs", {})
    img_need  = img.get("needs_paid", 0)
    des_need  = des.get("needs_paid", 0)
    spec_need = specs.get("needs_paid", 0)
    # *_need counts ROWS the storefront is missing; *_buy counts the SEARCHES
    # that fill them. One search answers every row sharing its cache key, and
    # an image key covers a model rather than a single size — so these differ
    # by ~8x on images. The budget is spend, so it is denominated in searches.
    img_buy   = img.get("searches", img_need)
    des_buy   = des.get("searches", des_need)
    spec_buy  = specs.get("searches", spec_need)
    # Two kinds of free: results already paid for in an earlier run, and
    # photos this catalogue already owns for the same tire model (see
    # enrichment.build_peer_image_index). Neither costs a search.
    free_now  = scan.get("total_cache_hit", 0) + scan.get("total_peer_fixed", 0)

    spec_dead_end = specs.get("dead_end", 0)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Images needing a search", img_need)
    m2.metric("Designations needing a search", des_need)
    m3.metric("Specs needing a search", spec_need,
              help="Load index, speed rating, load range, section width, aspect ratio, "
                   "rim diameter — bundled because one search commonly answers several "
                   "of these at once for the same row."
                   + (f" {spec_dead_end} more were already searched and came back with no "
                      f"match — those are excluded here and won't be bought again."
                      if spec_dead_end else ""))
    m4.metric("Free fixes ready", free_now,
              help="Results already paid for on an earlier run, plus product photos this "
                   "catalogue already carries for the same tire model at another size or "
                   "supplier. Applying them makes no network call and costs nothing.")
    # ── Completeness of the nine spec fields the storefront shows ──────────
    comp = scan.get("completeness", {})
    if comp.get("tire_rows_scanned"):
        totals   = comp.get("totals", {})
        scanned  = comp["tire_rows_scanned"]
        gaps = {f: n for f, n in totals.items() if n}
        # Name the biggest gaps rather than counting how many fields have any.
        # Seven of nine having a handful of blank rows is true but tells you
        # nothing; "Load range 3,302" is the number worth acting on.
        top = sorted(gaps.items(), key=lambda kv: -kv[1])[:3]
        headline = ("Product spec fields — " + ", ".join(f"{f} {n:,}" for f, n in top)
                    + (" and smaller gaps" if len(gaps) > len(top) else "")
                    if gaps else f"Product spec fields — all {len(totals)} complete ✓")
        with st.expander(headline):
            st.caption(
                f"Read back out of the finished Shopify CSVs ({scanned:,} tire rows) — the spec "
                f"block a customer sees on the product page. Wheels and rims carry a different "
                f"block (Wheel Diameter, Bolt Pattern, Offset…), so tire specs don't apply to "
                f"them and they aren't counted here."
            )
            st.dataframe(
                [{"Field": f,
                  "Blank": totals.get(f, 0),
                  "% of rows": f"{totals.get(f, 0) / scanned * 100:.1f}%" if scanned else "—",
                  "Status": "✅ complete" if not totals.get(f, 0) else "⚠️ gaps"}
                 for f in totals],
                hide_index=True, width="stretch",
            )
            st.caption(
                "Blank counts a value that is absent **or** a `0.0000` placeholder — on a "
                "product page the placeholder reads as a real measurement."
            )
            per_file = [r for r in comp.get("rows", []) if r.get("kind") == "tires"]
            if per_file:
                st.markdown("**By file**")
                st.dataframe(
                    [dict(File=r["file"], Rows=r["total"],
                          **{f: r["missing"].get(f, 0) for f in totals})
                     for r in per_file],
                    hide_index=True, width="stretch",
                )
            st.caption(
                "Brand, Style, Section width, Aspect ratio, Rim diameter, Designation and Image "
                "are filled in for free by parsing the supplier feed or applying a cached search — "
                "a gap in those is a real parsing shortfall worth reporting, not something more "
                "spend fixes. Load index, Speed rating and Load range are what the **Specs** "
                "budget below searches for once free parsing comes up empty."
            )

    with st.expander("Breakdown by supplier"):
        for label, f in (("Image", img), ("Designation", des), ("Specs", specs)):
            rows = f.get("rows", [])
            if not rows:
                continue
            st.markdown(f"**{label}**")
            has_dead_end = any(r.get("dead_end") for r in rows)
            st.dataframe(
                # "Rows to fill" and "Searches to buy" are different numbers:
                # one search answers every row sharing its cache key, and an
                # image key covers a whole model rather than a single size.
                [{"Supplier": r["supplier"], "Type": r["type"], "Missing": r["missing"],
                  "Free from cache": r["cache_fixed"],
                  **({"Free from a photo we own": r.get("peer_fixed", 0)}
                     if any(x.get("peer_fixed") for x in rows) else {}),
                  "Rows to fill": r["needs_search"],
                  "Searches to buy": r.get("searches", r["needs_search"]),
                  **({"Already tried, no match": r.get("dead_end", 0)} if has_dead_end else {}),
                  "Can be written": "yes" if r["writable"] else "report-only"}
                 for r in rows],
                hide_index=True, width="stretch",
            )
            if f.get("report_only"):
                st.caption(
                    f"{f['report_only']} row(s) sit in a file with no column to store the answer, "
                    f"so no search is ever bought for them. They are listed for honesty, not action."
                )
        st.caption(
            '"Missing" counts only rows that would actually ship to Shopify — low-quantity rows '
            "are already excluded, matching each supplier's own augment filter. For Specs, one row "
            "can need more than one of the six sub-fields — each row still counts once."
        )

    # ── Free path: apply what has already been paid for ────────────────────
    if free_now:
        peer_now = scan.get("total_peer_fixed", 0)
        st.caption(
            f"{scan.get('total_cache_hit', 0)} from results already paid for on an earlier run"
            + (f", {peer_now} from a product photo this catalogue already carries for the "
               f"same tire model at another size or supplier" if peer_now else "")
            + ". Applying them makes no network call and costs nothing."
        )
        if st.button(f"Apply {free_now} free fix(es)", disabled=busy, type="primary"):
            launch("enrich", [sys.executable, ENRICH_RUNNER, "--apply-only"], BASE)

    # ── Paid path: explicit, budgeted, confirmed ───────────────────────────
    if not (img_need or des_need or spec_need):
        st.success("Nothing needs a paid search — enrichment is up to date for this catalog.")
    else:
        st.warning(
            "Everything below spends real searches on the client's **paid** SerpAPI key. "
            "Review the numbers, set a cap, and approve only what you mean to buy."
        )
        col_i, col_d, col_s = st.columns(3)
        with col_i:
            want_img = st.checkbox(
                f"Images — {img_buy} search(es) fill {img_need} row(s)",
                value=bool(img_buy),
                disabled=not img_buy or busy, key="enrich_want_image",
                help="The one field with no free alternative — no supplier feed and no "
                     "amount of parsing produces a product photo. One search covers a whole "
                     "tire model rather than a single size, which is why the search count is "
                     "far below the row count.",
            )
            st.caption("No free alternative exists for this field.")
            img_budget = st.number_input(
                "Cap on image searches", min_value=0, max_value=max(img_buy, 0),
                value=img_buy, step=1, disabled=not want_img or busy,
                key="enrich_budget_image",
            )
        with col_d:
            want_des = st.checkbox(
                f"Designations — {des_buy} search(es) fill {des_need} row(s)",
                value=False,
                disabled=not des_buy or busy, key="enrich_want_designation",
                help="Historically 4 useful results out of 253 paid searches (1.6%), so these "
                     "few rows are usually cheaper to fix by hand than to search for. Off by "
                     "default for that reason.",
            )
            st.caption("Poor hit rate (1.6%) — off by default.")
            des_budget = st.number_input(
                "Cap on designation searches", min_value=0, max_value=max(des_buy, 0),
                value=des_buy, step=1, disabled=not want_des or busy,
                key="enrich_budget_designation",
            )
        with col_s:
            want_specs = st.checkbox(
                f"Specs — {spec_buy} search(es) fill {spec_need} row(s)",
                value=bool(spec_buy),
                disabled=not spec_buy or busy, key="enrich_want_specs",
                help="Load index, speed rating, load range, section width, aspect ratio and "
                     "rim diameter. One search commonly fills several of these for the same "
                     "row, so this is rarely as expensive as the row count suggests.",
            )
            st.caption("Six fields, often several per search.")
            specs_budget = st.number_input(
                "Cap on specs searches", min_value=0, max_value=max(spec_buy, 0),
                value=spec_buy, step=1, disabled=not want_specs or busy,
                key="enrich_budget_specs",
            )

        approve_img   = int(img_budget) if want_img else 0
        approve_des   = int(des_budget) if want_des else 0
        approve_specs = int(specs_budget) if want_specs else 0
        total         = approve_img + approve_des + approve_specs

        st.markdown(f"**Total to approve: {total} search(es)** "
                    f"({approve_img} image + {approve_des} designation + {approve_specs} specs)")

        # Keyed on the scan timestamp so a fresh count always clears the box —
        # a money button must never arrive pre-confirmed from a previous run.
        typed = st.text_input(
            f"Type ENRICH to approve {total} search(es)",
            key=f"enrich_confirm_{scan.get('generated', '')}",
            disabled=busy or total <= 0, placeholder="ENRICH",
        )
        confirmed = (typed or "").strip().upper() == "ENRICH" \
            and total > 0 and not scan.get("kill_switch")

        if st.button("Run enrichment", disabled=busy or not confirmed):
            launch("enrich", [sys.executable, ENRICH_RUNNER, "--run",
                              "--image-budget", str(approve_img),
                              "--designation-budget", str(approve_des),
                              "--specs-budget", str(approve_specs)], BASE)
        if not confirmed and total > 0 and not busy:
            st.caption("The button unlocks once ENRICH is typed above.")

    # ── Run status ─────────────────────────────────────────────────────────
    enrich_s = status.get("enrich", {})
    if enrich_s.get("status"):
        st.divider()
        status_badge(enrich_s, enriching)
        if enrich_s.get("status") == "success" and not enriching:
            st.caption("Enrichment applied and the affected Shopify CSVs were rebuilt — "
                       "continue to Step 4 below.")
        show_log(enrich_s, label="View log — enrichment", expanded=enriching)


# ─── Step 4: CSV upload bundle ─────────────────────────────────────────────────

CSV_LIMIT_MB  = 15.0   # Shopify hard limit per import file
CSV_TARGET_MB = 14.5   # split target — safety margin under the hard limit
sys.path.append(str(BASE / "shared"))
import upload_bundle as _upload_bundle

# Project root, easy for the client to find; the Mac app moves it to a visible
# folder via SBOT_UPLOAD_READY. One definition, shared with the bundle build.
UPLOAD_READY  = _upload_bundle.UPLOAD_READY

def collect_source_csvs() -> list[Path]:
    """Return the most recent Shopify_SEO_*.csv per product-type per supplier."""
    return _upload_bundle.collect_source_csvs([cfg["dir"] for cfg in PIPELINES.values()])

def prepare_upload_files(exclude_skus=None) -> tuple[list[Path], str]:
    """Merge all supplier CSVs, split at 15 MB, write to a dated upload_ready/ folder.

    Delegates to pipelines/shared/upload_bundle.py so the button here and the
    command-line build stay identical. That module also writes a manifest.txt
    recording which source CSV each bundle was built from — without it, a
    bundle's folder timestamp is the only clue to its contents, and that lies
    whenever a supplier CSV is regenerated moments after a build.

    exclude_skus: when given, products already in the store are left out and the
    bundle contains only what the import needs to create.
    """
    return _upload_bundle.prepare_upload_files(
        [cfg["dir"] for cfg in PIPELINES.values()], exclude_skus=exclude_skus)

def _store_sku_set() -> tuple[set, dict | None, str | None]:
    """SKUs already in the store, read from the catalog mirror via production/.

    The SKU list is written to a file rather than returned on stdout — 13k SKUs
    is a few hundred KB, and piping that through a pipe just to parse it back
    is needless.
    """
    import tempfile  # noqa: PLC0415
    tmp = Path(tempfile.gettempdir()) / "sbot_store_skus.json"
    meta, err = _production_json("store_skus.py", ["--out", str(tmp)], timeout=180)
    if err:
        return set(), None, err
    try:
        doc = json.loads(tmp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return set(), meta, f"Could not read the store SKU list: {e}"
    return set(doc.get("skus", [])), meta, None

def _open_folder(path: Path):
    """Open a folder in the OS file browser — dashboard runs on the same machine as the browser."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])

def _render_run_files(run_dir: Path, collapsed: bool = False):
    """Render the file list for one upload_ready run folder."""
    files = sorted(run_dir.glob("*.csv"))
    if not files:
        st.caption("No files in this folder.")
        return
    label = f"**{run_dir.name}**" if collapsed else f"**Latest — {run_dir.name}**"
    st.markdown(label)
    for i, f in enumerate(files, 1):
        size_mb = f.stat().st_size / (1024 * 1024)
        c1, c2, c3 = st.columns([1, 5, 2], vertical_alignment="center")
        with c1:
            st.caption(f"File {i} of {len(files)}" if len(files) > 1 else "File")
        with c2:
            st.code(str(f.relative_to(UPLOAD_READY.parent)), language="")
        with c3:
            st.caption(f"{size_mb:.1f} MB")
    if len(files) > 1:
        # Order matters, so this one stays a panel.
        st.info(f"Upload these {len(files)} files **in order** (1 → {len(files)}). "
                f"Each is under {CSV_LIMIT_MB:.0f} MB.")

def step4_section():

    # Show existing upload_ready folders — sort by actual mtime, not folder name (names like "9 July" sort after "10 July" alphabetically)
    existing_runs = sorted(UPLOAD_READY.glob("*/"), key=lambda p: p.stat().st_mtime, reverse=True) if UPLOAD_READY.exists() else []

    # Which products go in the bundle. "New only" is the default because an import
    # exists to CREATE products: re-uploading the ~15.6k that already exist changes
    # nothing in the store (price and quantity are the Sync button's job) and turns
    # a single 4 MB file into several 15 MB ones that take far longer to import.
    # The client build has no catalog mirror to compare against (it has no Sync),
    # so it always bundles every product.
    if CLIENT_BUILD:
        new_only = False
    else:
        bundle_mode = st.radio(
            "What to include",
            ["New products only (not yet in the store)", "All products"],
            index=0, horizontal=True, key="bundle_mode",
            help="New-only compares the supplier CSVs against the store's SKUs, using the "
                 "catalog mirror that the Sync button refreshes on every run.",
        )
        new_only = bundle_mode.startswith("New")

    col_btn, col_info = st.columns([2, 5])
    with col_btn:
        if st.button("Prepare upload files", width="stretch", type="primary"):
            exclude, meta, sku_err = (set(), None, None)
            if new_only:
                with st.spinner("Reading the SKUs already in the store…"):
                    exclude, meta, sku_err = _store_sku_set()
            if sku_err:
                st.error(sku_err)
            elif new_only and not exclude:
                st.error(
                    "The catalog mirror is empty, so every product would look new. "
                    "Run Preview on the Sync tab once (it refreshes the mirror from Shopify), "
                    "then try again."
                )
            else:
                with st.spinner("Merging CSVs…"):
                    files, err = prepare_upload_files(
                        exclude_skus=exclude if new_only else None)
                if err:
                    st.error(err)
                else:
                    kind = "new product" if new_only else "product"
                    st.success(f"Ready — {len(files)} {kind} file(s) generated.")
                st.rerun()
        if new_only:
            st.caption(
                "Compares against the store, so only products Shopify doesn't have "
                "are included."
            )
    with col_info:
        _sources = collect_source_csvs()
        source_count = len(_sources)
        if source_count:
            st.caption(f"{source_count} supplier CSV(s) found and ready to merge.")
            # Say this BEFORE the click, not as an error after it. A supplier CSV
            # older than the cleaned file behind it means a fix never reached the
            # output, and the finished bundle cannot show that — it is newer than
            # everything in it either way.
            _stale = _upload_bundle.stale_sources(_sources)
            if _stale:
                _nl = chr(10)
                _lines = [f"- `{o.name}` <- `{i.name}`" for o, i in _stale]
                st.error(
                    f"{len(_stale)} supplier CSV(s) are older than the data they were "
                    f"built from, so bundling is blocked until their augment step is "
                    f"re-run:" + _nl + _nl + _nl.join(_lines)
                )
            # Bundling before enriching produces a valid upload that is simply
            # missing images it could have had — worth a nudge, not a block.
            _pending = load_enrich_scan().get("total_needs_paid", 0)
            if _pending:
                st.caption(f"{_pending} row(s) could still be enriched on the Enrichment tab. "
                           f"Bundling now ships them without an image.")

            # How current the new-vs-existing comparison is. The mirror is refreshed
            # by Step 5's catalog export, so a client who has not synced in a while
            # would re-import products that already exist — harmless (Shopify matches
            # on Handle and updates) but slow and confusing. Say it up front.
            if new_only:
                _mirror = st.session_state.get("bundle_mirror_meta")
                if _mirror is None:
                    _mirror, _ = _production_json("store_skus.py", [], timeout=120)
                    st.session_state["bundle_mirror_meta"] = _mirror or {}
                _age = (_mirror or {}).get("age_hours")
                _count = (_mirror or {}).get("count")
                if _count:
                    _line = f"Comparing against {_count:,} SKUs in `{_mirror.get('shop','the store')}`"
                    if _age is not None:
                        _line += f", last refreshed {_age} h ago."
                    if _age is not None and _age > 24:
                        st.warning(_line + " Run Preview on the Sync tab to refresh it, or "
                                           "products created since will be bundled as new again.")
                    else:
                        st.caption(_line)
        else:
            st.caption("No supplier CSVs found yet — run the Suppliers tab first.")

    if existing_runs:
        col_hdr, col_open = st.columns([5, 2])
        with col_hdr:
            st.markdown("**Upload-ready files** — Shopify Admin → Products → Import")
        with col_open:
            if st.button("Open latest folder", width="stretch", type="tertiary"):
                _open_folder(existing_runs[0])
        _render_run_files(existing_runs[0])

        if len(existing_runs) > 1:
            with st.expander(f"Previous runs ({len(existing_runs) - 1} older)"):
                for run in existing_runs[1:]:
                    _render_run_files(run, collapsed=True)
                    st.divider()
    else:
        st.caption("No upload-ready files yet — press **Prepare upload files** above once "
                   "the supplier pipelines have produced new products.")


# ─── Step 5: Sync prices + inventory ───────────────────────────────────────────
_SYNC_KEY = "sync"

def sync_section():
    status  = load_status()
    running = is_running(_SYNC_KEY, status)
    s       = status.get(_SYNC_KEY, {})

    # ── what this would write to ───────────────────────────────────────────────
    # Everything shown here comes from the readiness check, which is the same
    # preflight module the run itself uses — so this panel cannot disagree with
    # where the write actually lands. Deliberately NOT recomputed on render:
    # Streamlit re-runs this script on every widget interaction, and spawning a
    # subprocess each time made the whole page sluggish for a line of text.
    preflight = st.session_state.get("sync_preflight") or {}
    def _row(name):
        return next((c for c in preflight.get("checks", []) if c["check"] == name), None)

    shop_row  = _row("shopify_reachable")
    warehouse = _row("warehouse")
    is_dev    = bool(shop_row.get("is_dev")) if shop_row else None
    shop_name = shop_row.get("shop") if shop_row else None

    c1, c2 = st.columns([3, 2])
    with c1:
        if shop_name:
            badge = ":blue[**DEV store**]" if is_dev else ":red[**LIVE store**]"
            st.markdown(f"Target store   `{shop_name}`   {badge}")
        else:
            st.caption("Target store — run the readiness check below to confirm.")
    with c2:
        if warehouse:
            st.caption(f"Run history — {warehouse['detail']}")

    # ── readiness ──────────────────────────────────────────────────────────────
    st.markdown("##### 1 · Check readiness")
    st.caption(
        "Confirms the store answers, the token can write prices and inventory, the "
        "primary location is unambiguous, and the supplier CSVs are fresh enough."
    )
    if st.button("Check readiness", disabled=running):
        with st.spinner("Checking store, token, warehouse and supplier feeds…"):
            payload, err = _production_json("preflight.py", [], timeout=120)
        st.session_state["sync_preflight"] = payload
        st.session_state["sync_preflight_err"] = err
        st.rerun()

    if st.session_state.get("sync_preflight_err"):
        st.error(st.session_state["sync_preflight_err"])
    checks = (st.session_state.get("sync_preflight") or {}).get("checks")
    if checks:
        icon = {"ok": "✅", "warning": "⚠️", "failed": "❌"}
        blocking = [c for c in checks if c["status"] == "failed"]
        stale    = [c for c in checks if c["check"].startswith("feed:") and c["status"] != "ok"]
        with st.expander(
                f"Readiness — {len(blocking)} blocking, {len(stale)} feed warning(s)",
                expanded=bool(blocking)):
            for c in checks:
                st.markdown(f"{icon[c['status']]}  **{c['check']}** — {c['detail']}")
        if stale and not any(c["check"].startswith("feed:") is False and c["status"] == "failed"
                             for c in checks):
            st.warning(
                "Some supplier CSVs are older than the freshness limit. Syncing now would "
                "push prices and quantities from those older files — re-run Step 2 for "
                "those suppliers first, or tick the override below if that is intended."
            )

    # ── options ────────────────────────────────────────────────────────────────
    st.markdown("##### 2 · Choose what to sync")
    opt1, opt2 = st.columns([2, 3])
    with opt1:
        types = st.multiselect("Product types", ["tires", "wheels"],
                               default=["tires", "wheels"], disabled=running,
                               key="sync_types")
    with opt2:
        force = st.checkbox(
            "Sync even if the safety gates fail (stale or short feeds)",
            value=False, disabled=running, key="sync_force",
            help="The gates stop a broken or half-finished scrape from repricing or "
                 "zeroing the store. Only override when you know the feeds are correct.")

    # ── run ────────────────────────────────────────────────────────────────────
    st.markdown("##### 3 · Run")
    if not types:
        st.info("Pick at least one product type above.")
        return

    def _cmd(dry: bool) -> list:
        # create_products is skipped: new products still go through Step 4's CSV
        # upload. This button changes prices and quantities on products that
        # already exist in the store, and nothing else.
        cmd = [sys.executable, "run_weekly.py", "--skip", "create_products",
               "--type", *types]
        if dry:
            cmd.append("--dry-run")
        if force:
            cmd.append("--force")
        return cmd

    run1, run2 = st.columns(2)
    with run1:
        if st.button("Preview changes (writes nothing)", disabled=running,
                     width="stretch", type="primary"):
            launch(_SYNC_KEY, _cmd(dry=True), PRODUCTION)
            st.rerun()
        st.caption("Runs the whole sync and records every change it *would* make.")

    with run2:
        # A preview is cheap and reversible; an apply is neither. Require an
        # explicit tick, and a second one when the target is not the dev store.
        confirm = st.checkbox("I want to write these changes to the store",
                              disabled=running, key="sync_confirm")
        live_ok = True
        if is_dev is False:
            live_ok = st.checkbox(
                f"Yes — write to the LIVE store ({shop_name})",
                disabled=running, key="sync_confirm_live")
        elif is_dev is None:
            live_ok = False

        if st.button("Apply to store", disabled=running or not (confirm and live_ok),
                     width="stretch"):
            launch(_SYNC_KEY, _cmd(dry=False), PRODUCTION)
            st.rerun()
        if is_dev is None:
            st.caption("Run the readiness check first — it confirms which store this writes to.")
        else:
            st.caption("Sends the changes for real. Preview first.")

    # ── progress + result ──────────────────────────────────────────────────────
    st.divider()
    status_badge(s, running)
    if running:
        st.caption(
            "Running in the background — you can close this tab. The catalog export "
            "alone takes a few minutes on a 10,000-product store."
        )
    show_log(s, label="View sync log", expanded=running)

    if not running and s.get("status") in ("success", "failed"):
        # Cached against the run's finish time for the same reason as the target
        # panel above: the summary only changes when a run ends, but this script
        # re-runs on every click.
        cache_key = f"sync_summary::{s.get('finished')}"
        if cache_key not in st.session_state:
            st.session_state[cache_key] = _production_json("report.py", [], timeout=90)
        summary, err = st.session_state[cache_key]
        if err:
            st.caption("Could not read the run summary.")
            return
        run = (summary or {}).get("run") or {}
        if not run:
            return
        mode_label = "PREVIEW — nothing was written" if run.get("mode") == "dry_run" \
                     else "APPLIED to the store"
        st.markdown(f"**Last run #{run['run_id']}** — {run['status']} · {mode_label}")

        counts = {}
        for a in summary.get("actions", []):
            counts[a["action"]] = counts.get(a["action"], 0) + a["n"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Price changes",   counts.get("price_update", 0))
        m2.metric("Quantity updates", counts.get("inventory_set", 0))
        m3.metric("Zeroed (no longer supplied)", counts.get("inventory_zero", 0))

        failed = sum(a["n"] for a in summary.get("actions", []) if a["status"] == "failed")
        if failed:
            st.error(f"{failed} change(s) failed — see the log above.")

        gates = summary.get("failed_gates") or []
        if gates:
            with st.expander(f"{len(gates)} validation gate(s) did not pass"):
                for g in gates:
                    st.markdown(f"- **{g['check_name']}** ({g['severity']}) — `{g['details']}`")


# ─── Advanced: reset stuck state ───────────────────────────────────────────────
@st.fragment(run_every=5)
def reset_section():
    st.markdown("##### Reset stuck status")
    st.caption(
        "If a pipeline shows 'Running' but the terminal was closed or the process crashed, "
        "clear its stale status here."
    )
    _reset_status = load_status()
    _all_keys     = ["cookies"] + list(PIPELINES) + ["enrich_scan", "enrich"] \
                    + ([] if CLIENT_BUILD else ["sync"])
    _extra_labels = {"enrich_scan": "Enrichment scan", "enrich": "Enrichment",
                     "sync": "Shopify sync"}
    stuck = [k for k in _all_keys if _reset_status.get(k, {}).get("status") == "running"
             and not pid_alive(_reset_status[k].get("pid"))]
    if not stuck:
        st.markdown(":green[All pipelines are in a clean state — nothing to reset.]")
    for key in stuck:
        label = PIPELINES[key]["label"] if key in PIPELINES else _extra_labels.get(key, key.title())
        if st.button(f"Clear '{label}' stuck state", key=f"reset_{key}"):
            _update_status(key, {"status": "failed", "pid": None})
            st.rerun()


# ── Render ─────────────────────────────────────────────────────────────────────
# Everything above is a definition. The page itself is assembled once, here: one
# tab per step, so a client on Step 1 is not also looking at a LIVE-store apply
# button and a SerpAPI budget form.
st.title("Save Big On Tires — Inventory Pipeline")
st.caption("Scrape and clean tire & wheel inventory from all five suppliers into Shopify "
           "import files." if CLIENT_BUILD else
           "Scrape, clean and sync tire & wheel inventory from all five suppliers to Shopify.")

sidebar_status()

_tab_names = ["1 · Cookies", "2 · Suppliers", "3 · Enrichment", "4 · Upload"]
if not CLIENT_BUILD:
    _tab_names.append("5 · Sync")
_tab_names.append("Advanced")
_tabs = dict(zip(_tab_names, st.tabs(_tab_names)))

with _tabs["1 · Cookies"]:
    st.subheader("Refresh cookies")
    st.caption("Run this before every pipeline run — supplier sessions expire.")
    step1_section()

with _tabs["2 · Suppliers"]:
    st.subheader("Run supplier pipelines")
    st.caption(
        "Pipelines run in the background, so this browser tab can be closed. Leave the "
        "computer on — a shutdown or restart kills any scrape in progress."
    )
    markup_section()
    step2_section()

with _tabs["3 · Enrichment"]:
    st.subheader("Data enrichment")
    st.caption(
        "Fills in product images, designations and tire specs that no supplier feed "
        "provides. Cached results are free; anything new spends a search on the "
        "client's SerpAPI key and needs approval below."
    )
    step3_section()

with _tabs["4 · Upload"]:
    if CLIENT_BUILD:
        st.subheader("Build Shopify import files")
        st.caption(
            "Merges every supplier's latest output into Shopify import CSVs (Shopify "
            "Admin → Products → Import). Each product is tagged with the supplier it "
            "came from."
        )
    else:
        st.subheader("Upload new products to Shopify")
        st.caption(
            "Only needed when suppliers have SKUs the store does not carry yet. Regular "
            "price and inventory syncs need no upload — that is Step 5."
        )
    step4_section()

if not CLIENT_BUILD:
    with _tabs["5 · Sync"]:
        st.subheader("Sync prices & inventory")
        st.caption(
            "Pushes merged supplier prices and quantities straight to the store over the "
            "Shopify API — no CSV, no manual import. Only what changed is sent."
        )
        sync_section()

with _tabs["Advanced"]:
    st.subheader("Advanced")
    reset_section()
    if CLIENT_BUILD:
        # The Mac app runs this server in the background with no window of its
        # own, so this is the way to stop it. Opening the app again restarts it.
        st.markdown("##### Close the app")
        st.caption(
            "Stops the dashboard. Pipelines that are already running keep going in the "
            "background; open the app again to see their progress."
        )
        if st.button("Close the app"):
            import signal  # noqa: PLC0415
            st.success("Closed. You can close this browser tab.")
            threading.Timer(1.0, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
