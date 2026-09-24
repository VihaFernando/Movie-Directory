FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Base image already has Chromium + its OS deps installed for this
# Playwright version, so no separate `playwright install` step is needed.

COPY . .

# Both Hugging Face Spaces and Render inject $PORT and expect the app to
# bind 0.0.0.0 on it - this Dockerfile works unchanged on either.
ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
