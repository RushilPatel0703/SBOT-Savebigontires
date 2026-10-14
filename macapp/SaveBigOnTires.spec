# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for "Save Big On Tires.app". Run through macapp/build.py,
# which stages build/payload/ (the compiled pipelines) first.
import json
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

HERE = Path(SPECPATH)
PAYLOAD = HERE / "build" / "payload"

# PyInstaller analyses sbot_app.py only; the pipelines it runs are data to it.
# Everything they import is listed by build.py and pulled in here, along with
# what pandas loads lazily (openpyxl) and isn't visible as an import anywhere.
hiddenimports = json.loads((PAYLOAD / "imports.json").read_text(encoding="utf-8"))
hiddenimports += ["openpyxl", "psutil"]

datas = [(str(PAYLOAD), "payload")]
binaries = []
# Streamlit serves its frontend from package data and reads its own metadata;
# Playwright runs a bundled Node driver. Both need their whole package.
for package in ("streamlit", "playwright"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(HERE / "sbot_app.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # production/ (the Shopify sync) is not shipped, so neither is its database.
    excludes=["tkinter", "duckdb", "pytest", "IPython", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    # Every pipeline step runs as a child of this same executable, and
    # PYTHONUNBUFFERED / PYTHONUTF8 from the environment do not reach a frozen
    # interpreter — so set both here: live log tails, UTF-8 files everywhere.
    [("u", None, "OPTION"), ("X utf8_mode=1", None, "OPTION")],
    exclude_binaries=True,
    name="SaveBigOnTires",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SaveBigOnTires")
app = BUNDLE(
    coll,
    name="Save Big On Tires.app",
    icon=None,
    bundle_identifier="com.savebigontires.inventory",
    info_plist={
        "CFBundleName": "Save Big On Tires",
        "CFBundleDisplayName": "Save Big On Tires",
        # A background app: the dashboard opens in the browser, so no Dock icon
        # that would bounce and then sit there doing nothing.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
