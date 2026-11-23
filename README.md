=== RUN ON YOUR WINDOWS PC ===
1. pip install -r requirements.txt
2. Set DISCORD_TOKEN and OWNER_ID in your environment.
3. Run a Lavalink 4.2+ server and set LAVALINK_HOST, LAVALINK_PORT, and LAVALINK_PASSWORD.
4. Optional AI voice messages: install FFmpeg. Optional AI chat: set GEMINI_API_KEY.
5. python main.py     -> type 'help' at the > prompt

Music playback and song search require Lavalink; there is no direct-FFmpeg music fallback.
See README.txt for Railway and Docker Lavalink setup.

=== ACCESS ===
Owner = full control. Trusted users = music, moveall, party, mute/deafen, AI. Everyone else is ignored.
  /trust @user   /untrust @user   /trusted      (CMD: trust / untrust / trusted)
Owner only: drag/autodc/automute, kick/ban/unban/timeout, /jtc setup, roulette, trust.

=== SLASH COMMANDS (also work as !text commands) ===
Music:  /join (stays 24/7) /play /skip /pause /resume /stop /queue /nowplaying /volume /loop /shuffle /remove /leave
Voice:  /moveall (asks where) /party /mute /unmute /deafen /undeafen /automute /automutes
Drag:   /setchannels /drag /autodc /undrag /undragall /targets /dragdelay [seconds]
Mod:    /kick /ban /unban /banlist /timeout /untimeout /timeouts
Rooms:  /jtc setup | settings | status | panel | disable    /voice <name|limit|privacy|waiting|chat|trust|...>
AI:     /ai <anything>   /voicecmd <audio>   /aichannel   /aireset   or  @mention the bot   or  'ai ...' in CMD
Other:  /status  /roulette  /botstatus
Slash commands sync to every server automatically on start (CMD: 'sync' to force it).

=== BOT PERMISSIONS ===
Manage Channels, Manage Roles, Move Members, Mute Members, Deafen Members, Kick Members, Ban Members,
Moderate Members, Connect, Speak, View Channels, Send Messages, Embed Links, Use Slash Commands.
Developer Portal: enable Server Members Intent + Message Content Intent, turn OFF "Public Bot".

=== BOT STATUS ===
/botstatus state:<online|idle|dnd|invisible> activity:<playing|listening|watching|competing|streaming|none> text:<...>
  - no options = show the current status;  reset:true = back to default.  Owner only, saved across restarts.
  - text accepts live placeholders {servers} {members} {ping} (refreshed every 5 minutes).
  - streaming needs a Twitch/YouTube link in url.  Bots can't use Discord's "custom status".
  - music: auto (a song shows only if you set no activity) / true (song always shows while playing) / false (never).
  CMD: botstatus dnd watching over the server      botstatus reset
  AI:  "set your status to idle and watching the server"

=== VOICE ROOMS (join-to-create, TempVoice-style) ===
/jtc setup creates the hub + an animated control panel (or pass your own channels). Members join the hub and
get their own room + a live "room card". The interface has 18 buttons:
  NAME  LIMIT  PRIVACY  WAITING ROOM  CHAT | TRUST  UNTRUST  INVITE  KICK  REGION
  BLOCK  UNBLOCK  CLAIM  TRANSFER  DELETE  | BITRATE  INFO  SAVE
Same things as commands: /voice name | limit | privacy | waiting | chat | bitrate | region | trust | untrust |
  invite | kick | block | unblock | transfer | claim | info | save | reset | delete
- Privacy: lock / unlock / invisible / visible.   Chat: open / close / clear.
- Waiting room: a second channel; when someone joins it the owner gets Let in / Deny / Block buttons.
- Trust / Block lists and SAVE'd settings follow the owner into every future room.
- Invite DMs a join link.  Region picker.  Owner leaves -> room passes to someone inside.
Admin: /jtc setup | settings | status | panel | disable.  config.py: JTC_ALLOW_EVERYONE = False limits rooms
to owner + trusted.  Bot needs Manage Channels, Manage Roles, Move Members, Manage Messages (clear chat).

=== AI ===
Powered by Google Gemini. Set GEMINI_API_KEY. It can run music, voice and (for the owner) moderation
tools. Kick / ban / timeout / trust changes ALWAYS ask for a confirm button first.

=== 24/7 IN VOICE ===
/join [channel]  -> bot joins (your channel by default) and STAYS: never leaves when idle, rejoins after a
restart or disconnect, and moves back if someone drags it away. /join stay:false = normal. /leave turns it off.
CMD: join <channel> [temp]. Saved in data.json (use a Railway Volume so it survives redeploys).

=== VOICE COMMANDS (any language) ===
Record a Discord VOICE MESSAGE (mic button in the text box) and send it:
  - in the CHAT OF THE VOICE CHANNEL the bot is in (open the voice channel's chat panel; no setup needed), or
  - in the AI channel (set with /aichannel #channel) - owner/trusted only, no mention needed, or
  - with /voicecmd (attach audio), or with an @mention + audio file.
The bot converts it with ffmpeg and Gemini understands it directly, then acts like you typed it and replies
in your language. Audio is sent to Google for that one request and is not stored by the bot.
(Live listening inside a voice channel is NOT included: Discord's end-to-end voice encryption blocks it
 with current libraries.)

=== HOST ON RAILWAY ===
1. Put this folder in a PRIVATE GitHub repo (.gitignore keeps .env / data.json out).
2. Deploy the bot service from the repo root and set DISCORD_TOKEN, OWNER_ID, DATA_FILE=/data/data.json,
   LAVALINK_HOST, LAVALINK_PORT=2333, and LAVALINK_PASSWORD.
3. Add a separate Lavalink service from the same repo with Root Directory `/lavalink`.
   Set LAVALINK_SERVER_PASSWORD there to the same value as the bot's LAVALINK_PASSWORD.
4. Add a Volume mounted at /data to the bot service (keeps data across deploys).
5. Optional: set GEMINI_API_KEY and Spotify client credentials. See README.txt for details.

=== LINUX VPS (Docker) ===
cp .env.example .env  (edit)  ->  docker compose up -d --build
