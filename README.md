# Personal Discord Bot

This bot combines a Python `discord.py` Discord client with an embedded Java **Lavaplayer** audio engine. The Java engine runs in the same process container, bound to loopback only. No Lavalink server or separate Railway audio service is used.

## Music features

- Search and playback with `/play`; queue controls include `/skip`, `/pause`, `/resume`, `/stop`, `/queue`, `/nowplaying`, `/volume`, `/loop`, `/shuffle`, `/remove`, and `/leave`.
- Lavaplayer sources: YouTube, SoundCloud, Bandcamp, Vimeo, Twitch streams, local files, and HTTP URLs. Availability can depend on provider changes and region.
- Supported media includes MP3, FLAC, WAV, Matroska/WebM (AAC, Opus, Vorbis), MP4/M4A (AAC), OGG (Opus, Vorbis, FLAC), AAC, plus M3U/PLS stream playlists.
- Spotify links use Spotify's Web API to read track metadata, then search for playable audio through configured Lavaplayer sources. Spotify audio itself is not streamed.
- `/musicdiag` checks Java IPC, configured search sources, local music directory, and FFmpeg status.
- `AUDIO_BITRATE=256` asks Discord to set the connected voice channel to 256 kbps. Discord caps this to the server's available limit, and the bot needs **Manage Channels** to change it.

## Run locally

Requirements: Python 3.12+, Java 17+, Maven 3.9+, and a Discord bot token. FFmpeg is needed for AI voice-message commands.

1. Set `DISCORD_TOKEN` and `OWNER_ID` in the environment (or copy `.env.example` to `.env` for Docker).
2. Build the engine: `cd java-engine` then `mvn -DskipTests package`.
3. From the repository root, install Python dependencies with `pip install -r requirements.txt`.
4. Run `python main.py`.

Place local media under `storage/music` and use `local:filename.mp3` with `/play`. Relative paths are confined to that folder.

Optional Spotify metadata lookup uses `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`. Optional AI features use `GEMINI_API_KEY`.

## Deploy on Railway

Deploy the repository root as a single service. Railway builds the root `Dockerfile`, which compiles the Lavaplayer JAR and packages it with Python and Java. There is no Lavalink service to create.

Set Railway variables:

- `DISCORD_TOKEN`
- `OWNER_ID`
- `DATA_FILE=/data/data.json`
- `AUDIO_LOCAL_DIR=/data/music`
- Optional: `GEMINI_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`

Add a Railway Volume mounted at `/data` to persist bot data and local music files across redeploys. Use the Railway service logs to confirm Java engine startup and Discord login. Run `/musicdiag` in Discord to check music sources.

### Private GHCR image

The `Publish private GHCR image` workflow builds the root Dockerfile and publishes `ghcr.io/arshman12345/persnol-bot:latest` on updates to `main`. The image is linked to this repository and is intended to remain private. In Railway, connect that image and add a GitHub classic personal access token with only `read:packages` under Registry Credentials. Railway requires a Pro plan for private registry credentials. Never put the token in the repository or Discord.

## Docker Compose

Copy `.env.example` to `.env`, fill in required credentials, then run `docker compose up -d --build`. Compose mounts `./storage` at `/app/storage` for persistent data and local media.

## Credentials and safety

Keep bot tokens and API secrets in environment variables or Railway Variables. Do not commit `.env`. The Java IPC listener binds to `127.0.0.1` and is not exposed publicly. Local-file playback is restricted to `AUDIO_LOCAL_DIR`.

## Other bot features

The bot also includes voice channel management, moderation, persistent 24/7 voice presence, slash and console commands, and optional Gemini-powered chat and voice-message commands. Review the command descriptions in Discord and `config.py` for feature settings.

