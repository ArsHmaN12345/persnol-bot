FROM node:22-bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/ArsHmaN12345/persnol-bot"
LABEL org.opencontainers.image.description="Persnol Discord bot with a local Lavaplayer music engine"

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg yt-dlp ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY music-node/package.json /app/music-node/package.json
RUN cd /app/music-node && npm install --omit=dev
COPY . .
ENV AUDIO_LOCAL_DIR=/data/music
CMD ["node", "music-node/src/index.js"]

