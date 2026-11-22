FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# FFmpeg handles PCM input; libopus encodes Discord voice packets.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . .

# Install dependencies at build time; do not mutate the running image on each restart.
CMD ["python", "-u", "main.py"]
