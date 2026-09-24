FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Base image already has Chromium + its OS deps installed for this
# Playwright version, so no separate `playwright install` step is needed.
# Camoufox ships its own patched Firefox build, fetched separately - see
# app/camoufox_pool.py for why embed capture uses it instead of Chromium.
# Deliberately no [geoip] extra / geoip=True at runtime - see that file's
# module docstring for why: it's ~45MB of MaxMind DB downloads with no
# bearing on whether this fixes the actual IP-reputation block, and a
# heavier build was the likely cause of a prior HF Space getting stuck in a
# broken platform state.
RUN python -m camoufox fetch

COPY . .

# HF Spaces injects $PORT and expects the app to bind 0.0.0.0 on it.
ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
