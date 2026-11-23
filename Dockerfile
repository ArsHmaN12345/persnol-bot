FROM python:3.12-slim

# ffmpeg + libopus = audio engine, deno = JavaScript runtime that yt-dlp needs for YouTube
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# update yt-dlp on every start (YouTube changes often), then run the bot
CMD ["sh", "-c", "pip install -U --no-cache-dir 'yt-dlp[default]' >/dev/null 2>&1; python main.py"]
