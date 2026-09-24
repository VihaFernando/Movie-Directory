"""
Dev entrypoint for Windows.

uvicorn's --reload mode force-sets WindowsSelectorEventLoopPolicy on Windows
(see uvicorn/loops/asyncio.py: use_subprocess=True path), which cannot spawn
subprocesses - breaking Playwright, which launches Chromium as one. Running
uvicorn programmatically without reload avoids that override.

Run with:
    python run.py
"""
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
