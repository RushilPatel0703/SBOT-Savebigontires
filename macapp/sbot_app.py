#!/usr/bin/env python3
"""Save Big On Tires — entry point of the packaged Mac app.

The app is a PyInstaller bundle of this file. It never ships the pipeline
source: macapp/build.py compiles pipelines/ to sourceless .pyc files and puts
them in the bundle as `payload/`. On launch the payload is copied into a
writable workspace in the client's home folder, because the pipelines write
their Data/ and logs/ next to their own code:

    ~/SaveBigOnTires/
        Uploads/        Shopify import files the dashboard builds (visible)
        .engine/        compiled pipelines + their Data/ (hidden from Finder)

The one executable plays four roles, picked by its first argument:

    (none)                  the double-click: sync the workspace, start the
                            dashboard server if it is not running, open it
    --sbot-serve            the Streamlit dashboard server itself
    <something>.py [args]   a pipeline script. The dashboard and the run_pipeline
                            chains launch every step as [sys.executable, script],
                            and in a frozen app sys.executable is this binary, so
                            it stands in for `python script.py` and runs the
                            compiled script.pyc beside the named path instead
    --sbot-selftest         build-time checks (macapp/build.py runs them in CI)

Nothing here is specific to Save Big On Tires' pipelines beyond the paths; the
pipelines themselves run unmodified, exactly as they do under a normal python.
"""

import builtins
import importlib.machinery
import importlib.util
import json
import marshal
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

APP_NAME = "Save Big On Tires"
PORT = 8642          # not Streamlit's default 8501, so another Streamlit app can't collide
URL = f"http://localhost:{PORT}"

HOME = Path(os.environ.get("SBOT_HOME") or Path.home() / "SaveBigOnTires")
ENGINE = HOME / ".engine"
UPLOADS = HOME / "Uploads"
APP_LOGS = ENGINE / "app_logs"

PAYLOAD = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent / "build")) / "payload"

# Under any Data/ directory a file is seeded once and then belongs to the
# client's machine: caches that grow with every run, the markup the client sets
# in the dashboard. Updating the app must not reset them. These few are the
# exception, maintained by the developer and replaced by each new version.
ALWAYS_UPDATE = {
    "pipelines/shared/Data/product_denylist.json",
}

# Modules some pipeline code imports only on Windows. Missing from a Mac build
# by design, so the self-test does not count them.
WINDOWS_ONLY = {"msvcrt", "winreg", "winsound", "_winapi", "ctypes.wintypes", "wmi"}

NO_UI = os.environ.get("SBOT_NO_UI") == "1"     # CI: never block on a dialog


# ── interpreter shims ─────────────────────────────────────────────────────────
def _install_import_shims():
    """Let the compiled pipelines import each other the way their source does.

    Two gaps open when .py files become sourceless .pyc:
      * Several modules load a sibling by path — spec_from_file_location(...,
        "shared/markup.py") — and that path no longer exists. Redirect a
        missing foo.py to foo.pyc beside it; importlib then picks the
        sourceless loader by suffix.
      * Plain `import config` from a directory on sys.path needs the standard
        path finder, which a frozen interpreter may not register. Make sure it
        is there so .pyc files on sys.path import normally.
    """
    original = importlib.util.spec_from_file_location

    def spec_from_file_location(name, location=None, *args, **kwargs):
        if location is not None:
            loc = os.fspath(location)
            if loc.endswith(".py") and not os.path.exists(loc) and os.path.exists(loc + "c"):
                location = loc + "c"
        return original(name, location, *args, **kwargs)

    importlib.util.spec_from_file_location = spec_from_file_location

    if not any(getattr(h, "__name__", "") == "path_hook_for_FileFinder" for h in sys.path_hooks):
        loaders = [
            (importlib.machinery.ExtensionFileLoader, importlib.machinery.EXTENSION_SUFFIXES),
            (importlib.machinery.SourceFileLoader, importlib.machinery.SOURCE_SUFFIXES),
            (importlib.machinery.SourcelessFileLoader, importlib.machinery.BYTECODE_SUFFIXES),
        ]
        sys.path_hooks.append(importlib.machinery.FileFinder.path_hook(*loaders))
        sys.path_importer_cache.clear()


def _load_code(path: Path):
    """Code object for a .py (compiled now) or a sourceless .pyc (16-byte header)."""
    if path.suffix == ".pyc":
        return marshal.loads(path.read_bytes()[16:])
    return compile(path.read_bytes(), str(path), "exec")


def run_script(script: str, args: list[str]) -> int:
    """`python script.py args` — for a script that ships as script.pyc."""
    path = Path(script)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.normpath(path))
    target = path if path.exists() else path.with_suffix(".pyc")
    if not target.exists():
        print(f"{APP_NAME}: can't open file '{script}': no such script", file=sys.stderr)
        return 2
    sys.argv = [str(path), *args]
    sys.path.insert(0, str(path.parent))
    globs = {"__name__": "__main__", "__file__": str(path), "__builtins__": builtins,
             "__cached__": None, "__package__": None, "__spec__": None, "__loader__": None}
    try:
        exec(_load_code(target), globs)
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BaseException:                                   # noqa: BLE001
        traceback.print_exc()
        return 1
    return 0


# ── workspace ─────────────────────────────────────────────────────────────────
def payload_version() -> str:
    try:
        return (PAYLOAD / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def sync_workspace() -> None:
    """Copy the bundled pipelines into ~/SaveBigOnTires/.engine.

    Code is replaced on every new version; Data/ files are only seeded (see
    ALWAYS_UPDATE). Compiled modules a newer version no longer ships are
    removed, so a deleted module can't linger and shadow anything.
    """
    version = payload_version()
    stamp = ENGINE / ".version"
    UPLOADS.mkdir(parents=True, exist_ok=True)
    APP_LOGS.mkdir(parents=True, exist_ok=True)
    try:
        if stamp.read_text(encoding="utf-8").strip() == version and version != "unknown":
            return
    except OSError:
        pass

    shipped = set()
    # A .app keeps data under Contents/Resources and symlinks it into
    # Frameworks/, where sys._MEIPASS points — so resolve, and follow links.
    payload = PAYLOAD.resolve()
    sources = [Path(d) / f for d, _, files in os.walk(payload, followlinks=True) for f in files]
    for src in sources:
        rel = src.relative_to(payload).as_posix()
        if rel == "VERSION" or rel == "imports.json":
            continue
        shipped.add(rel)
        dst = ENGINE / rel
        is_data = "Data" in Path(rel).parts[:-1]
        if is_data and rel not in ALWAYS_UPDATE and dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    code_root = ENGINE / "pipelines"
    for old in code_root.rglob("*.pyc"):
        rel = old.relative_to(ENGINE).as_posix()
        if "Data" in Path(rel).parts or rel in shipped:
            continue
        old.unlink(missing_ok=True)

    # Each supplier writes Data/{raw,cleaned,shopify} and logs/ relative to its
    # own directory; most scripts create them, but not all, and not first.
    for supplier in ("formula_distributors", "northcountry", "dttire", "wheelsmart", "emergetires"):
        for sub in ("Data/raw", "Data/cleaned", "Data/shopify", "logs"):
            (code_root / supplier / sub).mkdir(parents=True, exist_ok=True)

    stamp.write_text(version, encoding="utf-8")


# ── macOS niceties ────────────────────────────────────────────────────────────
def _osascript(script: str, wait: bool) -> None:
    if NO_UI or sys.platform != "darwin":
        return
    try:
        proc = subprocess.Popen(["osascript", "-e", script],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait:
            proc.wait()
    except OSError:
        pass


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(message: str) -> None:
    _osascript(f"display notification {_quote(message)} with title {_quote(APP_NAME)}", wait=False)


def alert(message: str) -> None:
    _osascript(f"display dialog {_quote(message)} with title {_quote(APP_NAME)} "
               f'buttons {{"OK"}} default button "OK" with icon caution', wait=True)


def open_browser() -> None:
    if NO_UI:
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, URL])
    except OSError:
        pass


# ── Playwright's browser (cookie refresh logs in through it) ──────────────────
def _driver_command():
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    exe = compute_driver_executable()
    cmd = [str(p) for p in exe] if isinstance(exe, (tuple, list)) else [str(exe)]
    return cmd, get_driver_env()


def browser_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:                                       # noqa: BLE001
        return False


def ensure_browser(log=print) -> bool:
    """Download Playwright's Chromium (~150 MB, once) into ~/Library/Caches."""
    if browser_installed():
        return True
    notify("First-time setup: downloading a browser component (about 150 MB). "
           "The dashboard opens when it's done.")
    cmd, env = _driver_command()
    log(f"Installing Playwright Chromium: {cmd + ['install', 'chromium']}")
    proc = subprocess.run([*cmd, "install", "chromium"], env=env,
                          capture_output=True, text=True)
    log(proc.stdout[-4000:])
    log(proc.stderr[-4000:])
    return proc.returncode == 0 and browser_installed()


# ── the dashboard server ──────────────────────────────────────────────────────
def server_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/_stcore/health", timeout=2) as r:
            return r.status == 200
    except Exception:                                       # noqa: BLE001
        return False


def _server_pid():
    try:
        return int((ENGINE / "server.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def stop_server() -> None:
    pid = _server_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(40):
        if not server_healthy():
            return
        time.sleep(0.25)


def serve() -> int:
    """Run the Streamlit dashboard in the client-build configuration."""
    sync_workspace()
    os.environ["SBOT_CLIENT_BUILD"] = "1"
    os.environ["SBOT_UPLOAD_READY"] = str(UPLOADS)
    (ENGINE / "server.pid").write_text(str(os.getpid()), encoding="utf-8")
    (ENGINE / "server.version").write_text(payload_version(), encoding="utf-8")
    os.chdir(ENGINE / "pipelines")          # .streamlit/config.toml is read from the cwd
    from streamlit.web import cli as stcli
    sys.argv = [
        "streamlit", "run", "dashboard.py",
        f"--server.port={PORT}",
        "--server.address=localhost",       # local only: no firewall prompt, nothing exposed
        "--server.headless=true",
        "--global.developmentMode=false",   # frozen paths look like a dev checkout otherwise
        "--browser.gatherUsageStats=false",
        "--client.toolbarMode=minimal",     # no "Deploy" button or developer menu
    ]
    return stcli.main()


def launch() -> int:
    """The double-click."""
    sync_workspace()
    log_path = APP_LOGS / "launcher.log"
    with open(log_path, "a", encoding="utf-8") as log_file:
        def log(msg):
            log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            log_file.flush()

        version = payload_version()
        log(f"launch, version {version}")
        if server_healthy():
            try:
                running = (ENGINE / "server.version").read_text(encoding="utf-8").strip()
            except OSError:
                running = ""
            if running == version:
                log("server already running; opening browser")
                open_browser()
                return 0
            log(f"server running old version {running!r}; restarting")
            stop_server()

        if not ensure_browser(log):
            log("browser install failed")
            alert("The browser component could not be downloaded. Check the internet "
                  "connection and open the app again.\n\nDetails: " + str(log_path))
            # The dashboard still works for everything except refreshing cookies.

        server_log = open(APP_LOGS / "server.log", "a", encoding="utf-8")
        subprocess.Popen([sys.executable, "--sbot-serve"], stdout=server_log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(HOME))
        for _ in range(240):
            if server_healthy():
                log("server up; opening browser")
                open_browser()
                return 0
            time.sleep(0.5)
        log("server did not come up")
        alert("The dashboard did not start. Details are in:\n" + str(APP_LOGS / "server.log"))
        return 1


# ── build-time self-test ──────────────────────────────────────────────────────
def selftest(args: list[str]) -> int:
    """Checks that only a frozen build can answer. Run by macapp/build.py.

    1. Every module the pipelines import is inside the bundle.
    2. A compiled script runs through this dispatcher, imports its compiled
       siblings, and launches further steps through sys.executable.
    3. Playwright's bundled driver can install and start Chromium.
    """
    failures = []
    sync_workspace()

    imports = json.loads((PAYLOAD / "imports.json").read_text(encoding="utf-8"))
    for name in imports:
        if name in WINDOWS_ONLY:
            continue
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:
            failures.append(f"import {name}: {e}")
        except Exception as e:                              # noqa: BLE001
            # Importable but fails on import (e.g. needs a display) — present, which is what this checks.
            print(f"  note: import {name} raised {type(e).__name__}: {e}")
    print(f"[selftest] {len(imports)} third-party/stdlib imports checked")

    def run(cwd, *cmd):
        proc = subprocess.run([sys.executable, *cmd], cwd=str(cwd),
                              capture_output=True, text=True, timeout=900)
        return proc.returncode, proc.stdout + proc.stderr

    code_root = ENGINE / "pipelines"
    rc, out = run(code_root / "shared", "upload_bundle.py", "--dry-run")
    if "Traceback" in out:
        failures.append("upload_bundle.py --dry-run raised:\n" + out[-3000:])
    print(f"[selftest] upload_bundle.py --dry-run exit {rc}")

    rc, out = run(code_root / "dttire", "run_pipeline.py", "--only", "clean_tires")
    if "STEP: clean_tires" not in out or "Traceback" in out:
        failures.append("nested dispatch through run_pipeline.py failed:\n" + out[-3000:])
    print(f"[selftest] dttire run_pipeline.py --only clean_tires exit {rc}")

    if "--skip-browser" not in args:
        if not ensure_browser():
            failures.append("Playwright could not install Chromium")
        else:
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.set_content("<title>ok</title>")
                    assert page.title() == "ok"
                    browser.close()
                print("[selftest] Playwright Chromium launched")
            except Exception as e:                          # noqa: BLE001
                failures.append(f"Playwright launch: {e}")

    for f in failures:
        print("FAIL:", f)
    print("[selftest] " + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


def main() -> int:
    _install_import_shims()
    # Finder adds -psn_0_NNN on older macOS; it is not ours.
    args = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
    if not args:
        return launch()
    cmd, rest = args[0], args[1:]
    if cmd == "--sbot-serve":
        return serve()
    if cmd == "--sbot-selftest":
        return selftest(rest)
    if cmd == "--sbot-sync":
        sync_workspace()
        print(ENGINE)
        return 0
    if cmd.endswith(".py"):
        return run_script(cmd, rest)
    print(f"{APP_NAME}: unexpected arguments {args!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
