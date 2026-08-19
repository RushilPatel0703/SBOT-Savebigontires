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

# ── Constants ──────────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
STATUS_FILE = BASE / ".dashboard_status.json"
LOGS_DIR    = BASE / "dashboard_logs"
LOGS_DIR.mkdir(exist_ok=True)

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

    if sys.platform == "darwin":
        # Same idle-sleep prevention as Windows, via caffeinate. -w ties its
        # lifetime to this process so it exits on its own when the dashboard closes.
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
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

# ── UI helpers ─────────────────────────────────────────────────────────────────
def status_badge(s: dict, running: bool):
    if running:
        st.info("⏳ Running…")
    elif s.get("status") == "success":
        ts = s.get("finished", "")[:16].replace("T", " ")
        st.success(f"✅  Done  {ts}")
    elif s.get("status") == "failed":
        ts = s.get("finished", "")[:16].replace("T", " ")
        st.error(f"❌  Failed  {ts}")
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

# ── Page ───────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Tire Inventory Pipeline", page_icon="🔄", layout="wide")
st.title("🔄  Save Big On Tires — Inventory Pipeline")
st.caption("Scrape, clean, and sync tire & wheel inventory from all 5 suppliers to Shopify.")

# Per-supplier card styling — rgba() washes rather than fixed hex so the tint
# blends correctly against both light and dark theme backgrounds automatically.
_CARD_RGB = {
    "blue":   (28, 131, 225),
    "green":  (33, 195, 84),
    "orange": (255, 140, 0),
    "violet": (138, 63, 252),
    "red":    (229, 72, 77),
}
_TINT_RGB = _CARD_RGB["blue"]  # same wash on every card, regardless of accent color
_card_css = "\n".join(
    f""".st-key-card_{key} {{
        border: 1px solid rgba({_TINT_RGB[0]},{_TINT_RGB[1]},{_TINT_RGB[2]},0.35) !important;
        border-left: 5px solid rgba({_TINT_RGB[0]},{_TINT_RGB[1]},{_TINT_RGB[2]},0.9) !important;
        background: rgba({_TINT_RGB[0]},{_TINT_RGB[1]},{_TINT_RGB[2]},0.06);
        border-radius: 10px;
        transition: box-shadow .15s ease, transform .15s ease;
    }}
    .st-key-card_{key}:hover {{
        box-shadow: 0 6px 16px rgba({_TINT_RGB[0]},{_TINT_RGB[1]},{_TINT_RGB[2]},0.25);
        transform: translateY(-2px);
    }}"""
    for key in PIPELINES
)
# Progress bars only — scoped to stProgress so the primary-button color elsewhere
# on the page (Run, Cancel, etc.) is untouched.
_gr = _CARD_RGB["green"]
_progress_css = f"""
[data-testid="stProgressBarTrack"] > div {{ background-color: rgb({_gr[0]},{_gr[1]},{_gr[2]}) !important; }}
"""
st.markdown(f"<style>{_card_css}\n{_progress_css}</style>", unsafe_allow_html=True)

# ─── Sidebar: at-a-glance status ────────────────────────────────────────────────
@st.fragment(run_every=2)
def sidebar_status():
    status = load_status()
    st.sidebar.subheader("At a glance")

    def _line(label: str, s: dict, running: bool):
        if running:
            st.sidebar.markdown(f"⏳ **{label}** — running")
        elif s.get("status") == "success":
            st.sidebar.markdown(f"✅ **{label}** — done")
        elif s.get("status") == "failed":
            st.sidebar.markdown(f"❌ **{label}** — failed")
        else:
            st.sidebar.markdown(f"⚪ **{label}** — not run")

    _line("Cookies", status.get("cookies", {}), is_running("cookies", status))
    st.sidebar.divider()
    for key, cfg in PIPELINES.items():
        _line(cfg["label"], status.get(key, {}), is_running(key, status))

sidebar_status()

# ─── Step 1: Cookies ───────────────────────────────────────────────────────────
st.header("Step 1 — Refresh Cookies")
st.caption("Run before every pipeline run.")

@st.fragment(run_every=3)
def step1_section():
    status  = load_status()
    s       = status.get("cookies", {})
    running = is_running("cookies", status)
    c1, c2  = st.columns([2, 4])
    with c1:
        if st.button("🔑  Refresh All Cookies", disabled=running, use_container_width=True, type="primary"):
            launch("cookies", [sys.executable, "refresh_cookies.py"], BASE)
    with c2:
        status_badge(s, running)
    show_log(s, expanded=running)

step1_section()
st.divider()

# ─── Step 2: Supplier Pipelines ────────────────────────────────────────────────
st.header("Step 2 — Run Supplier Pipelines")
st.caption("Run each supplier. Pipelines run in the background — you can close this browser tab if you'd like.")
st.warning("⚠️  Do not turn off or restart the computer while pipelines are running — this kills any scrape in progress.")

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
    done_count     = sum(1 for k in PIPELINES if status.get(k, {}).get("status") == "success")
    if started or running_no_log:
        # running pipelines with no log yet contribute 1 unfinished step so bar never falsely hits 100%
        total_steps = sum(p["total"]     for p in with_data.values()) + len(running_no_log)
        done_steps  = sum(p["completed"] for p in with_data.values())
        pct_all     = min(done_steps / total_steps, 1.0) if total_steps else 0.0
        running_now = [PIPELINES[k]["label"] for k in PIPELINES if is_running(k, status)]

        m1, m2, m3 = st.columns(3)
        m1.metric("Steps complete", f"{done_steps} / {total_steps}")
        m2.metric("Suppliers complete", f"{done_count} / {len(PIPELINES)}")
        m3.metric("Running now", len(running_now) if running_now else "—")
        st.progress(pct_all, text=", ".join(running_now) if running_now else "No pipelines currently running")
        st.write("")

    for key, cfg in PIPELINES.items():
        s       = status.get(key, {})
        running = is_running(key, status)
        prog    = all_prog.get(key, {})

        with st.container(border=True, key=f"card_{key}"):
            cols = st.columns([3, 2, 2, 4])
            with cols[0]:
                logo = _find_logo(key)
                if logo:
                    st.markdown(
                        '<div style="display:flex;align-items:center;gap:12px;margin-top:4px;">'
                        f'<img src="{_logo_data_uri(logo)}" style="height:44px;width:auto;max-width:140px;object-fit:contain;">'
                        f'<span style="font-size:1.5rem;font-weight:600;line-height:1;">{cfg["label"]}</span>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.subheader(f"{cfg['icon']} :{cfg['color']}[{cfg['label']}]")
            with cols[1]:
                if cfg.get("full_option"):
                    st.checkbox(
                        ":orange[Full scrape]",
                        key=f"full_{key}",
                        disabled=running,
                        help="Also re-check search terms that returned nothing on previous "
                             "scrapes (rebuilds the zero-terms cache — slowest). Normal runs "
                             "scrape fresh inventory but skip those known-empty terms.",
                    )
                    st.caption("Run with full scrape every alternate week")
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
                    if st.button("⏹  Cancel", key=f"cancel_{key}", use_container_width=True, type="primary"):
                        _kill_pipeline(key, status)
                        st.rerun()
                else:
                    if st.button("▶  Run", key=f"btn_{key}", use_container_width=True, type="primary"):
                        launch(key, [sys.executable, cfg["script"]] + extra, cfg["dir"])
            with cols[3]:
                status_badge(s, running)

        # Progress bar — outside container so it always renders
        pipe_status = s.get("status")
        if not pipe_status:
            st.progress(0.0, text=f"{cfg['label']}: not yet started")
        elif prog.get("total"):
            completed = prog["completed"]
            total     = prog["total"]
            pct       = min(completed / total, 1.0)
            if running and prog.get("current"):
                bar_txt = f"{cfg['label']}: step {completed} / {total} — currently: {prog['current']}"
            elif prog.get("complete"):
                bar_txt = f"{cfg['label']}: all {total} steps complete ✓"
            elif prog.get("failed"):
                bar_txt = f"{cfg['label']}: failed at step {completed + 1} / {total} (last: {prog.get('current', '?')})"
            else:
                bar_txt = f"{cfg['label']}: step {completed} / {total}"
            st.progress(pct, text=bar_txt)
        elif running:
            st.progress(0.0, text=f"{cfg['label']}: starting…")
        elif pipe_status == "failed":
            st.progress(0.0, text=f"{cfg['label']}: failed — check log for details")
        elif pipe_status == "success":
            st.progress(1.0, text=f"{cfg['label']}: complete ✓")

        show_log(s, label=f"View log — {cfg['label']}", expanded=running)

step2_section()
st.divider()

# ─── CSV Upload helper ─────────────────────────────────────────────────────────
st.header("Step 3 — Upload New Products to Shopify (when needed)")
st.caption(
    "Only required when suppliers have new SKUs not yet in Shopify. "
    "For regular price/inventory syncs, no upload is needed."
)

CSV_LIMIT_MB  = 15.0   # Shopify hard limit per import file
CSV_TARGET_MB = 14.5   # split target — safety margin under the hard limit
UPLOAD_READY  = BASE.parent / "upload_ready"   # project root — easy for the client to find

sys.path.append(str(BASE / "shared"))
import upload_bundle as _upload_bundle

def collect_source_csvs() -> list[Path]:
    """Return the most recent Shopify_SEO_*.csv per product-type per supplier."""
    return _upload_bundle.collect_source_csvs([cfg["dir"] for cfg in PIPELINES.values()])

def prepare_upload_files() -> tuple[list[Path], str]:
    """Merge all supplier CSVs, split at 15 MB, write to a dated upload_ready/ folder.

    Delegates to pipelines/shared/upload_bundle.py so the button here and the
    command-line build stay identical. That module also writes a manifest.txt
    recording which source CSV each bundle was built from — without it, a
    bundle's folder timestamp is the only clue to its contents, and that lies
    whenever a supplier CSV is regenerated moments after a build.
    """
    return _upload_bundle.prepare_upload_files(
        [cfg["dir"] for cfg in PIPELINES.values()])


# Show existing upload_ready folders — sort by actual mtime, not folder name (names like "9 July" sort after "10 July" alphabetically)
existing_runs = sorted(UPLOAD_READY.glob("*/"), key=lambda p: p.stat().st_mtime, reverse=True) if UPLOAD_READY.exists() else []

col_btn, col_info = st.columns([2, 5])
with col_btn:
    if st.button("📦  Prepare Upload Files", use_container_width=True, type="primary"):
        with st.spinner("Merging CSVs…"):
            files, err = prepare_upload_files()
        if err:
            st.error(err)
        else:
            st.success(f"Ready — {len(files)} file(s) generated.")
        st.rerun()
with col_info:
    source_count = len(collect_source_csvs())
    if source_count:
        st.caption(f"{source_count} supplier CSV(s) found and ready to merge.")
    else:
        st.caption("No supplier CSVs found yet — run Step 2 first.")

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
        c1, c2, c3 = st.columns([1, 5, 2])
        with c1:
            st.write(f"File {i} of {len(files)}" if len(files) > 1 else "File")
        with c2:
            st.code(str(f.relative_to(UPLOAD_READY.parent)), language="")
        with c3:
            st.success(f"✅ {size_mb:.1f} MB")
    if len(files) > 1:
        st.info(f"Upload these {len(files)} files **in order** (1 → {len(files)}). Each is under {CSV_LIMIT_MB:.0f} MB.")

if existing_runs:
    col_hdr, col_open = st.columns([5, 2])
    with col_hdr:
        st.markdown("**Upload-ready files** — upload to Shopify Admin → Products → Import:")
    with col_open:
        if st.button("📂  Open Latest Folder", use_container_width=True, type="primary"):
            _open_folder(existing_runs[0])
    _render_run_files(existing_runs[0])

    if len(existing_runs) > 1:
        with st.expander(f"Previous runs ({len(existing_runs) - 1} older)"):
            for run in existing_runs[1:]:
                _render_run_files(run, collapsed=True)
                st.divider()
else:
    st.caption("No upload-ready files yet — click **Prepare Upload Files** above once Step 2 has produced new products.")

st.divider()

# ─── Reset stuck state ─────────────────────────────────────────────────────────
@st.fragment(run_every=5)
def reset_section():
    with st.expander("⚙️  Advanced — reset stuck status"):
        st.caption(
            "If a pipeline shows 'Running' but the terminal was closed or the process crashed, "
            "click the button below to clear its stale status."
        )
        _reset_status = load_status()
        _all_keys     = ["cookies"] + list(PIPELINES)
        stuck = [k for k in _all_keys if _reset_status.get(k, {}).get("status") == "running"
                 and not pid_alive(_reset_status[k].get("pid"))]
        if not stuck:
            st.success("All pipelines are in a clean state — nothing to reset.")
        for key in stuck:
            label = PIPELINES[key]["label"] if key in PIPELINES else key.title()
            if st.button(f"Clear '{label}' stuck state", key=f"reset_{key}", type="primary"):
                _update_status(key, {"status": "failed", "pid": None})
                st.rerun()

reset_section()
