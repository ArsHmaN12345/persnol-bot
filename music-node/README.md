# Node music runtime

This is the single Discord client configured by the root Dockerfile. It uses `@discordjs/voice`, yt-dlp for source resolution, FFmpeg for PCM decoding, and `@discordjs/opus` for Discord voice encoding. No Java music engine or Lavalink service is started.

## Railway variables

- `DISCORD_TOKEN` — bot token
- `OWNER_ID` — retained for the broader bot migration; music currently follows the existing server command access policy
- `AUDIO_LOCAL_DIR` — optional local audio directory (defaults to `/data/music`)
- `DEFAULT_VOLUME` — initial volume from 0 to 2 (defaults to `0.5`)
- `IDLE_TIMEOUT` — seconds to wait before leaving an empty queue (defaults to `300`)

Mount a Railway volume at `/data` to retain local audio. Grant the bot View Channel, Connect, Speak, and Send Messages permissions. Enable the Server Members and Voice States intents only if later Node feature ports require them; this music runtime currently uses guild and voice state data.

## Current commands

`/play`, `/pause`, `/resume`, `/skip`, `/stop`, `/leave`, `/queue`, `/nowplaying`, `/shuffle`, `/clear`, `/volume`, `/loop`, `/remove`, and `/musicdiag`.

Spotify metadata matching and full parity with the Python command set are not implemented yet. yt-dlp provider support changes over time; actual source playback must be checked in a Discord voice channel after deploy.
