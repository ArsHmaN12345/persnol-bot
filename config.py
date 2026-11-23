import os

# ---------- REQUIRED ----------
# Paste your (new, reset) bot token here, or set the DISCORD_TOKEN environment variable.
TOKEN = os.getenv("DISCORD_TOKEN", "PUT_YOUR_NEW_BOT_TOKEN_HERE")

# ---------- GENERAL ----------
PREFIX = "!"                                        # prefix for text commands (slash commands always work too)
OWNER_ID = int(os.getenv("OWNER_ID", "958101166896582666"))   # the ONLY person with full control
# Everyone else is ignored unless the owner trusts them:  /trust @user   (or  trust <user>  in CMD)

# ---------- DRAG MODULE ----------
DRAG_DELAY = 1.5            # seconds between moves (keep >= 1)

# ---------- MUSIC MODULE ----------
AUDIO_BITRATE = 256         # kbps Opus target (automatically capped to the voice channel's bitrate)
DEFAULT_VOLUME = 0.5        # 0.0 - 2.0
MAX_QUEUE = 300
IDLE_TIMEOUT = 300          # seconds of empty queue before the bot leaves voice
VC_STATUS = True            # show the current song as the voice channel status
VC_STATUS_TEMPLATE = "🎵 {title}"   # {title} = song name
SET_PRESENCE = True         # show "Listening to <song>" on the bot's profile

# Needed only for Spotify links (free): https://developer.spotify.com/dashboard
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# Music requires a Lavalink server.
MUSIC_BACKEND = os.getenv("MUSIC_BACKEND", "lavalink").strip().lower()
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "")
LAVALINK_SECURE = os.getenv("LAVALINK_SECURE", "false").strip().lower() in ("1", "true", "yes", "on")
LAVALINK_SEARCH = os.getenv("LAVALINK_SEARCH", "ytsearch,scsearch")

# ---------- JOIN-TO-CREATE ----------
JTC_ALLOW_EVERYONE = True   # True = any server member can make/use their own temp room
                            # False = only the owner and trusted users
JTC_MAX_ROOMS = 50          # safety cap per server

# ---------- AI (Google Gemini) ----------
# NEVER write the key in this file. Set it as an environment variable / Railway variable:
#   GEMINI_API_KEY = your key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip().strip('"').strip("'").strip()   # auto-falls back to the newest Flash model
AI_COOLDOWN = 4             # seconds between AI requests per user
AI_MAX_STEPS = 6            # max tool-calling rounds per request
AI_HISTORY_TURNS = 6        # how many past exchanges the AI remembers per user
AI_MENTION = True           # @mention the bot to talk to the AI (owner/trusted only)
