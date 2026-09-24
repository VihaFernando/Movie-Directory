"""
Entry point for the packaged desktop app (see build_exe.spec).

Not used for normal development - that's still `python run.py` (backend) +
`npm run dev` (frontend), unchanged. This script is what PyInstaller
actually freezes into the .exe: it starts the same FastAPI app used
everywhere else, waits for it to actually accept connections, then opens
the user's default browser to it - so double-clicking the exe looks and
feels like opening a normal desktop app, with no terminal or manual step
visible.
"""
import asyncio
import os
import socket
import sys
import threading
import time
import webbrowser

# Must happen before `import uvicorn`/anything that imports playwright:
# Playwright resolves its browser/driver paths at import time. The dev
# machine's PLAYWRIGHT_BROWSERS_PATH env var (if any) is irrelevant on a
# user's machine, which has neither that variable nor its browser cache -
# build_exe.spec bundles Playwright's driver + Chromium as data instead
# (under ms-playwright/ and playwright/driver/ next to this executable),
# so point Playwright there when running from a frozen build.
if getattr(sys, "frozen", False):
    _bundle_dir = sys._MEIPASS
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(_bundle_dir, "ms-playwright")
    # Playwright's driver looks for node.exe/cli.js relative to its own
    # package location, which collect_all("playwright") already places
    # correctly under the frozen bundle - no override needed for that part.

    # app/config.py's Settings.Config.env_file = ".env" is a relative path,
    # correct for normal dev (run from the project root) and for a hosted
    # deployment (its own .env alongside the code) - neither applies here.
    # A distributed exe ships its own .env bundled as data (see
    # build_exe.spec) since an end user has no Mongo/Clerk/TMDB credentials
    # of their own to supply. pydantic-settings reads real environment
    # variables with equal priority to its .env file, so setting them here
    # from the bundled file achieves the same effect without touching
    # config.py's behavior for the other two deployment modes.
    from dotenv import dotenv_values

    _bundled_env_path = os.path.join(_bundle_dir, ".env")
    if os.path.isfile(_bundled_env_path):
        for _key, _value in dotenv_values(_bundled_env_path).items():
            if _value is not None:
                os.environ.setdefault(_key, _value)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

# Imported directly (not passed to uvicorn.run() as the "app.main:app"
# string form run.py uses) so PyInstaller's static analysis actually
# traces into app/ and bundles it - a string target is resolved by
# uvicorn at runtime via importlib, which a frozen build's import
# machinery can't discover ahead of time the way it does a real import
# statement. Confirmed needed: without this, the frozen exe raised
# "ModuleNotFoundError: No module named 'app'" trying to resolve the
# string form.
from app.main import app as fastapi_app

HOST = "127.0.0.1"
PORT = 8000


def _wait_for_server(host: str, port: int, timeout_s: float = 30.0) -> bool:
    """Poll until something is actually listening on (host, port).

    Opening the browser before uvicorn has bound its socket would show the
    user a connection-refused error for the first instant after launch -
    this closes that race instead of guessing a fixed sleep duration.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _open_browser_when_ready() -> None:
    if _wait_for_server(HOST, PORT):
        webbrowser.open(f"http://{HOST}:{PORT}")
    # If the server never came up, uvicorn.run() below will have already
    # crashed loudly (or is still starting slowly) - nothing useful to do
    # here beyond not opening a browser tab that would just error.


if __name__ == "__main__":
    # Opening the browser has to happen from a second thread: uvicorn.run()
    # below blocks the main thread for the app's entire lifetime, the same
    # way it does in run.py.
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    uvicorn.run(fastapi_app, host=HOST, port=PORT, reload=False)
