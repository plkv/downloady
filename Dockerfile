FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps that yt-dlp may rely on (ffmpeg is optional for direct URLs but helpful)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8080

# Use proper module execution (no fallback to hide errors)
CMD ["python", "-m", "app.bot"]
