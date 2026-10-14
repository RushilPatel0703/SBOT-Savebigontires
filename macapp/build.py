#!/usr/bin/env python3
"""Build the client's Mac app: "Save Big On Tires.app", zipped with its install guide.

Runs on macOS — PyInstaller only builds for the OS it runs on. CI does it on
GitHub's Apple Silicon runner (.github/workflows/build-mac.yml); any Mac with
this repo's requirements installed can too:

    pip install -r requirements.txt -r macapp/requirements-build.txt
    python macapp/build.py

Steps, each of which stops the build on failure:

  1. stage     pipelines/ -> macapp/build/payload/, every .py compiled to a
               sourceless .pyc (the client gets no source), plus the list of
               modules the pipelines import, which PyInstaller cannot see
               because it never analyses the payload
  2. freeze    PyInstaller, from macapp/SaveBigOnTires.spec
  3. selftest  the frozen app checks its own imports, script dispatch and
               Playwright (sbot_app.selftest)
  4. e2e       a real DT Tire clean -> enrich -> augment on a small fixture,
               run through the frozen app, offline from the supplier portals
  5. ui        the dashboard served by the frozen app, driven in a browser:
               no errors, no Sync tab, and "Prepare upload files" writes an
               import file whose rows carry the supplier tag
  6. package   install guide PDF + app -> dist/SaveBigOnTires-mac.zip

`--stage-only` runs step 1 alone, on any OS, to inspect the payload.
"""

import argparse
import ast
import importlib.util
import json
import os
import plistlib
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / "build"
PAYLOAD = BUILD / "payload"
DIST = HERE / "dist"
APP = DIST / "Save Big On Tires.app"
EXE = APP / "Contents" / "MacOS" / "SaveBigOnTires"

# Not part of what the client runs.
EXCLUDE = (
    re.compile(r"^pipelines/tests/"),
    re.compile(r"\.md$"),
)

DASHBOARD_STUB = '''\
# Save Big On Tires dashboard. The dashboard ships compiled, as dashboard_app.pyc
# beside this file; Streamlit needs a .py to run, so this runs that on every rerun
# exactly as the source would run.
import marshal as _marshal, pathlib as _pathlib
exec(_marshal.loads(_pathlib.Path(__file__).with_name("dashboard_app.pyc").read_bytes()[16:]), globals())
'''


def sh(*cmd, **kwargs):
    print("$", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def pipeline_files():
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "pipelines"],
        cwd=ROOT, check=True, capture_output=True).stdout.decode()
    files = sorted(f for f in out.split("\0") if f)
    return [f for f in files if not any(p.search(f) for p in EXCLUDE) and (ROOT / f).is_file()]


def imported_modules(py_files):
    """Every absolute import in the pipelines that resolves to an installed module
    that is not one of the pipelines' own files."""
    local = {Path(f).stem for f in py_files}
    candidates = set()
    for f in py_files:
        tree = ast.parse((ROOT / f).read_bytes(), filename=f)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                candidates.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                candidates.add(node.module)
                candidates.update(f"{node.module}.{a.name}" for a in node.names)
    found = set()
    for name in sorted(candidates):
        if name.split(".")[0] in local or name.startswith("_sbot"):
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                found.add(name)
        except (ImportError, ValueError, AttributeError):
            pass
    return sorted(found)


def stage():
    shutil.rmtree(PAYLOAD, ignore_errors=True)
    files = pipeline_files()
    py_files = [f for f in files if f.endswith(".py")]
    for f in files:
        src = ROOT / f
        if f.endswith(".py"):
            rel = "pipelines/dashboard_app.pyc" if f == "pipelines/dashboard.py" else f[:-3] + ".pyc"
            dst = PAYLOAD / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            py_compile.compile(str(src), cfile=str(dst), dfile=f, doraise=True,
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        else:
            dst = PAYLOAD / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    (PAYLOAD / "pipelines" / "dashboard.py").write_text(DASHBOARD_STUB, encoding="utf-8")

    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip() or "nogit"
    version = f"{datetime.now(timezone.utc):%Y.%m.%d.%H%M}-{sha}"
    (PAYLOAD / "VERSION").write_text(version, encoding="utf-8")
    imports = imported_modules(py_files)
    (PAYLOAD / "imports.json").write_text(json.dumps(imports, indent=1), encoding="utf-8")

    leaked = [p for p in PAYLOAD.rglob("*.py") if p.name != "dashboard.py"]
    assert not leaked, f"source in payload: {leaked}"
    print(f"staged {len(files)} files ({len(py_files)} compiled), "
          f"{len(imports)} imports, version {version}")
    return version


def freeze():
    shutil.rmtree(DIST, ignore_errors=True)
    sh(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
       "--distpath", DIST, "--workpath", BUILD / "pyinstaller",
       HERE / "SaveBigOnTires.spec")


def min_macos():
    """Highest minimum-OS any bundled binary declares — the app can't run below it."""
    best = (11, 0)
    for p in APP.rglob("*"):
        if not p.is_file() or p.is_symlink() or p.stat().st_size < 4096:
            continue
        with open(p, "rb") as fh:
            if fh.read(4) not in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
                continue
        out = subprocess.run(["otool", "-l", str(p)], capture_output=True, text=True).stdout
        for m in re.finditer(r"minos (\d+)\.(\d+)", out):
            best = max(best, (int(m.group(1)), int(m.group(2))))
    return f"{best[0]}.{best[1]}"


def finalize_bundle(version):
    minos = min_macos()
    info_path = APP / "Contents" / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["LSMinimumSystemVersion"] = minos
    info["CFBundleShortVersionString"] = version.split("-")[0]
    info["CFBundleVersion"] = version
    info_path.write_bytes(plistlib.dumps(info))
    # Editing Info.plist invalidates the signature; Apple Silicon refuses to run
    # unsigned code, so re-apply the ad-hoc signature over the whole bundle.
    sh("codesign", "--force", "--deep", "--sign", "-", APP)
    sh("codesign", "--verify", "--deep", "--strict", APP)
    print(f"minimum macOS: {minos}")
    return minos


def selftest(home):
    # An empty browser folder, so the self-test does the real first-launch
    # download through the frozen driver rather than finding CI's own Chromium.
    env = {**os.environ, "SBOT_HOME": str(home), "SBOT_NO_UI": "1",
           "PLAYWRIGHT_BROWSERS_PATH": str(home.parent / "ms-playwright")}
    sh(EXE, "--sbot-selftest", env=env, cwd=home)


def e2e(home):
    """DT Tire clean -> enrich (cache only) -> augment through the frozen app."""
    env = {**os.environ, "SBOT_HOME": str(home), "SBOT_NO_UI": "1"}
    engine = Path(subprocess.run([str(EXE), "--sbot-sync"], env=env, check=True,
                                 capture_output=True, text=True).stdout.strip().splitlines()[-1])
    dt = engine / "pipelines" / "dttire"
    raw = dt / "Data" / "raw" / f"dttire_tires_{datetime.now():%Y%m%d_%H%M%S}.csv"
    shutil.copy2(HERE / "fixtures" / "dttire_tires_fixture.csv", raw)
    sh(EXE, "run_pipeline.py", "--skip", "scraper_tires", "create_tires", "scraper_wheels",
       "clean_wheels", "enrich_wheels", "augment_wheels", "create_wheels", env=env, cwd=dt)
    outputs = sorted((dt / "Data" / "shopify").glob("Shopify_SEO_dttire_tires_*.csv"))
    assert outputs, "augment produced no Shopify CSV"
    text = outputs[-1].read_text(encoding="utf-8")
    assert "Supplier_Dttire" in text, "augment output lost the supplier tag"
    print(f"e2e: {outputs[-1].name} written, {text.count(chr(10)) - 1} row(s)")


def ui(home):
    """Serve the dashboard from the frozen app and drive it in a browser."""
    env = {**os.environ, "SBOT_HOME": str(home), "SBOT_NO_UI": "1"}
    shots = DIST / "screens"
    shots.mkdir(parents=True, exist_ok=True)
    server = subprocess.Popen([str(EXE), "--sbot-serve"], env=env, cwd=home,
                              stdout=open(shots / "server.log", "w"), stderr=subprocess.STDOUT)
    try:
        sh(sys.executable, HERE / "ui_smoke.py", "--home", home, "--shots", shots)
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()


def package(version, minos):
    stage_dir = BUILD / "zip" / "Save Big On Tires"
    shutil.rmtree(stage_dir.parent, ignore_errors=True)
    stage_dir.mkdir(parents=True)
    sh("ditto", APP, stage_dir / APP.name)
    sh(sys.executable, HERE / "make_instructions_pdf.py",
       "--out", stage_dir / "Install Instructions.pdf", "--version", version, "--min-macos", minos)
    zip_path = DIST / "SaveBigOnTires-mac.zip"
    # ditto, not zipfile: it keeps the bundle's symlinks, permissions and signature.
    sh("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", stage_dir, zip_path)
    print(f"package: {zip_path} ({zip_path.stat().st_size / 1e6:.0f} MB)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage-only", action="store_true")
    ap.add_argument("--skip-ui", action="store_true")
    args = ap.parse_args()

    version = stage()
    if args.stage_only:
        return
    if sys.platform != "darwin":
        sys.exit("build.py builds a Mac app and must run on macOS (use --stage-only elsewhere).")

    freeze()
    minos = finalize_bundle(version)
    with tempfile.TemporaryDirectory(prefix="sbot-test-") as tmp:
        home = Path(tmp) / "SaveBigOnTires"
        home.mkdir()            # the app makes its own folders inside; cwd must exist first
        selftest(home)
        e2e(home)
        if not args.skip_ui:
            ui(home)
    package(version, minos)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"done in {time.time() - t0:.0f}s")
