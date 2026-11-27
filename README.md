# Personal Bot

The Railway image runs one Discord client: a Java 25 bot built with JDA 6 and Lavaplayer. The Python and previous Node implementations remain in the repository as inactive source for later work; the Docker image does not start them, so they cannot create a second Discord session.

## Music features

- `/play` accepts a search, supported source URL, playlist, stream, or `local:filename`.
- `/pause`, `/resume`, `/skip`, `/stop`, `/leave`, `/queue`, `/nowplaying`, `/shuffle`, `/clear`, `/remove`, `/seek`, `/volume`, `/loop`, and `/musicdiag`.
- Lavaplayer sources include YouTube, SoundCloud, Bandcamp, Vimeo, Twitch, local files, and HTTP streams. Supported formats include MP3, FLAC, WAV, WebM/Matroska, MP4/M4A, OGG, AAC, M3U, and PLS.
- Track announcements are posted only after Lavaplayer reports playback started. Load errors, track errors, stuck tracks, voice connection timeouts, and playback state are logged.
- YouTube search works without an API key. Spotify track links resolve title and artist through Spotify's public oEmbed endpoint, then search a playable source; Spotify audio is not streamed. Spotify playlists/albums are not supported without a metadata API.

## Railway setup

Build the repository root with the supplied `Dockerfile`. Set these Railway variables:

- `DISCORD_TOKEN` — the existing Discord bot token.
- `DISCORD_GUILD_ID` — optional; syncs slash commands immediately to that server. If omitted, commands sync globally and may take time to appear.
- `AUDIO_LOCAL_DIR=/data/music` — local media directory.

Mount a Railway volume at `/data` if you want uploaded local files to persist. Enable the bot's **Connect**, **Speak**, **View Channel**, and **Send Messages** permissions. Enable the **Server Members Intent** only if other application features need it; the Java music bot requests Guild Voice States. For 256 kbps audio, set the Discord voice channel bitrate to 256 kbps in the server settings and ensure the server tier permits it. The bot does not silently alter channel settings.

The Java image includes JDA's DAVE implementation and Linux x86-64 native library, and is built on Java 25. Railway logs should show `[startup]`, `[commands]`, `[voice]`, and `[playback]` events. `/musicdiag` reports the current voice connection, player state, and queue depth.

## Local run

Requirements: Java 25 and Maven 3.9+. Set `DISCORD_TOKEN` (and optionally `DISCORD_GUILD_ID`) then run:

```powershell
cd java-bot
mvn -DskipTests package
java --enable-native-access=ALL-UNNAMED -jar target/personal-music-bot.jar
```

Put local tracks in `storage/music` and request them as `local:filename.mp3`.

## Image publishing

`.github/workflows/publish-ghcr.yml` builds and publishes `ghcr.io/arshman12345/persnol-bot:latest` from `main`. Railway can deploy the repository Dockerfile or the published image. Keep `DISCORD_TOKEN` in Railway Variables; never commit it.
