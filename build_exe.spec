# PyInstaller spec for the packaged desktop app.
#
# Build with:
#   .venv\Scripts\pyinstaller build_exe.spec
#
# Produces dist/MovieDirectory/MovieDirectory.exe (Windows) - a one-folder
# build rather than --onefile: Playwright's bundled Chromium is large, and
# --onefile would re-extract it to a temp dir on every single launch, which
# is slow and wastes disk. One-folder unpacks once at build time instead.
#
# Not used by normal development - `python run.py` + `npm run dev` are
# unchanged. This is only for producing a distributable exe.
import os
import sys

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# --- Frontend static build ---
# Must exist before running this spec: `cd frontend && npm run build`.
# Bundled as data so app/main.py's StaticFiles mount (see its sys._MEIPASS
# handling) finds it at frontend/dist inside the frozen app.
FRONTEND_DIST = os.path.join("frontend", "dist")
if not os.path.isdir(FRONTEND_DIST):
    raise SystemExit(
        "frontend/dist not found - run `npm run build` in frontend/ first."
    )

# --- Playwright's Chromium browser ---
# Playwright's Python package only contains the driver (a Node.js CLI) -
# the actual browser binary lives wherever PLAYWRIGHT_BROWSERS_PATH points
# (or Playwright's own default cache dir if that's unset). A machine that
# just installs this exe has neither, so the whole browser directory has to
# ship as bundled data, found dynamically here rather than hardcoded to
# this dev machine's path.
from playwright.sync_api import sync_playwright

with sync_playwright() as _pw:
    _chromium_exe = _pw.chromium.executable_path

# .../chromium-1140/chrome-win/chrome.exe -> bundle the whole
# "chromium-1140" version directory, not just the exe: Chromium needs its
# adjacent DLLs/resources/locales to run at all.
_chromium_version_dir = os.path.dirname(os.path.dirname(_chromium_exe))
_chromium_dirname = os.path.basename(_chromium_version_dir)

# --- Playwright's Node.js driver ---
from playwright._impl._driver import compute_driver_executable

_node_exe, _cli_js = compute_driver_executable()
_driver_dir = os.path.dirname(_node_exe)

# Bundled as data so a distributed exe has working Mongo/Clerk/TMDB/target-
# site credentials without the end user needing (or being able) to create
# their own - see desktop_launcher.py's frozen-mode env loading. This
# script must be run with a real, filled-in .env present at the project
# root (the same one used for normal dev/HF deployment).
ENV_FILE = ".env"
if not os.path.isfile(ENV_FILE):
    raise SystemExit(
        f"{ENV_FILE} not found - the desktop build needs one to bundle "
        "real credentials for end users, who have none of their own."
    )

datas = [
    (FRONTEND_DIST, FRONTEND_DIST),
    (_driver_dir, os.path.join("playwright", "driver")),
    (_chromium_version_dir, os.path.join("ms-playwright", _chromium_dirname)),
    (ENV_FILE, "."),
]

# Playwright's own pyinstaller hook (playwright._impl.__pyinstaller) adds
# its Python-side hidden imports; collect_all here picks up its package
# data too, in case the hook alone misses something on this version.
_pw_datas, _pw_binaries, _pw_hidden = collect_all("playwright")
datas += _pw_datas

a = Analysis(
    ["desktop_launcher.py"],
    pathex=[],
    binaries=_pw_binaries,
    datas=datas,
    hiddenimports=_pw_hidden + [
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MovieDirectory",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # keep a console window for now - shows real errors if something goes wrong; can be hidden later once this is proven stable
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MovieDirectory",
)
