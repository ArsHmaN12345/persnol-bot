"""Change the bot's status: online/idle/dnd/invisible + "Playing / Listening / Watching / Competing / Streaming ..."

The setting is saved, restored on every start, and shared with the music player so a song title
never silently overwrites a status you chose (see 'music' mode below).
"""
import asyncio
import re
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import data, save_data, is_owner, BotError
from ui import BaseCog, GREEN, RED, YELLOW

GROUP = "presence"
STATES = {"online": discord.Status.online, "idle": discord.Status.idle,
          "dnd": discord.Status.dnd, "invisible": discord.Status.invisible}
STATE_LOOK = {"online": ("🟢 Online", GREEN), "idle": ("🌙 Idle", YELLOW),
              "dnd": ("⛔ Do Not Disturb", RED), "invisible": ("⚫ Invisible", 0x747F8D)}
ACTIVITIES = ("playing", "listening", "watching", "competing", "streaming", "none")
VERB = {"playing": "Playing", "listening": "Listening to", "watching": "Watching",
        "competing": "Competing in", "streaming": "Streaming"}
STREAM_URL = re.compile(r"^https?://(www\.)?(twitch\.tv|youtube\.com|youtu\.be)/\S+$", re.I)
MAX_TEXT = 128
REFRESH_SECONDS = 300           # placeholders like {servers} are refreshed this often


def default_cfg():
    return {"state": "online", "type": "none", "text": "", "url": "", "music": None}


def get_cfg():
    cfg = data["settings"].setdefault("presence", {})
    for k, v in default_cfg().items():
        cfg.setdefault(k, v)
    return cfg


def render_text(bot, text):
    """{servers} {members} {ping} are replaced with live numbers."""
    members = sum(g.member_count or 0 for g in bot.guilds)
    ping = round(bot.latency * 1000) if bot.latency == bot.latency else 0     # latency can be NaN before connecting
    return (text.replace("{servers}", str(len(bot.guilds)))
                .replace("{members}", str(members)).replace("{ping}", str(ping)))[:MAX_TEXT]


def build_activity(bot, cfg, song=None):
    """What the bot should display right now (a song may take over, depending on the 'music' mode)."""
    music = cfg["music"]
    if song and (music is True or (music is None and cfg["type"] == "none")):
        return discord.Activity(type=discord.ActivityType.listening, name=song[:MAX_TEXT])
    if cfg["type"] == "none" or not cfg["text"]:
        return None
    text = render_text(bot, cfg["text"])
    t = cfg["type"]
    if t == "playing":
        return discord.Game(name=text)
    if t == "streaming":
        return discord.Streaming(name=text, url=cfg["url"])
    kind = {"listening": discord.ActivityType.listening, "watching": discord.ActivityType.watching,
            "competing": discord.ActivityType.competing}[t]
    return discord.Activity(type=kind, name=text)


def update_cfg(state=None, activity=None, text=None, url=None, music=None, reset=False):
    """Validate + merge a partial change into the saved settings. Returns the new config."""
    cfg = default_cfg() if reset else dict(get_cfg())
    if state is not None:
        if state not in STATES:
            raise BotError("State must be online, idle, dnd or invisible.")
        cfg["state"] = state
    if activity is not None:
        if activity not in ACTIVITIES:
            raise BotError("Activity must be playing, listening, watching, competing, streaming or none.")
        cfg["type"] = activity
        if activity == "none":
            cfg["text"], cfg["url"] = "", ""
    if text is not None:
        text = text.strip()
        if len(text) > MAX_TEXT:
            raise BotError(f"The status text can be at most {MAX_TEXT} characters (yours is {len(text)}).")
        cfg["text"] = text
    if url is not None:
        cfg["url"] = url.strip()
    if music is not None:
        cfg["music"] = music
    if cfg["type"] != "none" and not cfg["text"]:
        raise BotError("Add the status text too, e.g. `text: with the server`.")
    if cfg["type"] == "streaming" and not STREAM_URL.match(cfg["url"] or ""):
        raise BotError("Streaming needs a Twitch or YouTube link in `url` (Discord only accepts those).")
    if cfg["type"] != "streaming":
        cfg["url"] = ""
    return cfg


def describe(cfg):
    if cfg["type"] == "none":
        return "No activity"
    return f"{VERB[cfg['type']]} **{cfg['text']}**"


def music_mode_text(cfg):
    return {None: "Auto (a song shows only while you have no custom activity)",
            True: "Always show the current song while music plays",
            False: "Never - your status always stays"}[cfg["music"]]


def status_embed(cfg, title="🎭 Bot status"):
    label, color = STATE_LOOK[cfg["state"]]
    e = discord.Embed(title=title, color=color)
    e.add_field(name="Status", value=label)
    e.add_field(name="Activity", value=describe(cfg))
    e.add_field(name="🎵 Music mode", value=music_mode_text(cfg), inline=False)
    if cfg["state"] == "invisible":
        e.set_footer(text="Invisible: the bot looks offline but still works normally.")
    elif "{" in cfg["text"]:
        e.set_footer(text="Live placeholders: {servers} {members} {ping} (refreshed every 5 minutes)")
    return e


class Presence(BaseCog):
    def __init__(self, bot):
        self.bot = bot
        self.song = None                      # (guild_id, title) of the song currently playing
        self._task = None

    async def cog_load(self):
        self._task = asyncio.create_task(self._refresh_loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def cog_check(self, ctx):
        return ctx.guild is not None and is_owner(ctx.author.id)

    # ---------- applying ----------
    async def apply(self):
        cfg = get_cfg()
        try:
            # always send the status too: change_presence() would otherwise reset it to "online"
            await self.bot.change_presence(status=STATES[cfg["state"]],
                                           activity=build_activity(self.bot, cfg, self.song[1] if self.song else None))
        except Exception as e:                # presence is cosmetic and must never crash anything
            print(f"[presence] couldn't update: {type(e).__name__}: {e}")

    async def music_update(self, guild_id, title):
        """Called by the music player when a song starts (title) or playback ends (None)."""
        if title:
            self.song = (guild_id, title)
        elif self.song and self.song[0] == guild_id:
            self.song = None
        await self.apply()

    async def set(self, **changes):
        cfg = update_cfg(**changes)
        data["settings"]["presence"] = cfg
        save_data()
        await self.apply()
        return cfg

    async def _refresh_loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.apply()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[presence] refresh error: {e}")
            await asyncio.sleep(REFRESH_SECONDS)

    # ---------- command ----------
    @commands.hybrid_command(name="botstatus", description="Change the bot's status and activity (owner only)")
    @app_commands.describe(
        state="online, idle, dnd or invisible",
        activity="What the bot is doing: playing, listening, watching, competing, streaming or none",
        text="The status text. You can use {servers} {members} {ping}",
        url="Twitch/YouTube link (only for streaming)",
        music="Show the current song as the status while music plays? (leave empty = auto)",
        reset="Back to default (online, no activity)")
    async def botstatus(self, ctx, state: Optional[Literal["online", "idle", "dnd", "invisible"]] = None,
                        activity: Optional[Literal["playing", "listening", "watching", "competing", "streaming", "none"]] = None,
                        text: Optional[str] = None, url: Optional[str] = None,
                        music: Optional[bool] = None, reset: bool = False):
        if not reset and all(v is None for v in (state, activity, text, url, music)):
            return await ctx.send(embed=status_embed(get_cfg(), "🎭 Current bot status"), ephemeral=True)
        cfg = await self.set(state=state, activity=activity, text=text, url=url, music=music, reset=reset)
        await ctx.send(embed=status_embed(cfg, "✅ Bot status updated"))


# ---------------- console ----------------
async def c_botstatus(bot, args):
    cog = bot.get_cog("Presence")
    if not args:
        print("Current:", STATE_LOOK[get_cfg()["state"]][0], "|", describe(get_cfg()).replace("**", ""))
        print("Usage: botstatus [online|idle|dnd|invisible] [playing|listening|watching|competing|streaming|none] [text...]")
        print("       botstatus reset      (add 'url=<twitch/youtube link>' for streaming)")
        return
    if args[0].lower() == "reset":
        await cog.set(reset=True)
        print("Status reset.")
        return
    args = list(args)
    state = activity = url = None
    if args[0].lower() in STATES:
        state = args.pop(0).lower()
    if args and args[0].lower() in ACTIVITIES:
        activity = args.pop(0).lower()
    for a in list(args):
        if a.lower().startswith("url="):
            url = a[4:]
            args.remove(a)
    cfg = await cog.set(state=state, activity=activity, text=" ".join(args) if args else None, url=url)
    print("Status updated:", STATE_LOOK[cfg["state"]][0], "|", describe(cfg).replace("**", ""))


async def setup(bot):
    await bot.add_cog(Presence(bot))
    bot.add_console("botstatus", c_botstatus, "botstatus [state] [activity] [text]",
                    "change the bot's status (e.g. botstatus dnd watching the server)", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
