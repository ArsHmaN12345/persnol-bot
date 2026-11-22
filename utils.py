import json
import os
import re
from datetime import timedelta

import discord
from discord.ext import commands

import config

DATA_FILE = os.getenv("DATA_FILE", "data.json")

data = {}


class BotError(Exception):
    """A friendly error that is shown to the user as-is."""


def _defaults():
    data.setdefault("targets", {})      # {guild_id: {user_id: "drag"|"disconnect"}}
    data.setdefault("channels", {})     # {guild_id: [vc1, vc2]}
    data.setdefault("trusted", [])      # [user_id, ...]
    data.setdefault("settings", {})     # small saved settings, e.g. drag_delay
    data.setdefault("stay", {})         # {guild_id: channel_id} - 24/7 voice channel
    data.setdefault("ai_channels", {})  # {guild_id: channel_id} - messages here go straight to the AI
    data.setdefault("automute", {})     # {guild_id: {user_id: "mute"|"deafen"|"both"}}
    jtc = data.setdefault("jtc", {})
    jtc.setdefault("guilds", {})        # {guild_id: {hub, category, panel_channel, panel_message, template, limit, bitrate}}
    jtc.setdefault("rooms", {})         # {channel_id: {guild, owner, waiting, card}}
    jtc.setdefault("waiting", {})       # {waiting_channel_id: room_channel_id}
    jtc.setdefault("profiles", {})      # {"guild:user": {trusted, blocked, prefs}} - a room owner's saved lists
    trusted = data.get("trusted", [])
    if not isinstance(trusted, list):
        trusted = []
    data["trusted"] = list(dict.fromkeys(int(uid) for uid in trusted if str(uid).strip().isdigit()))


def load_data():
    loaded = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("not a JSON object")
        except (ValueError, OSError) as e:
            backup = DATA_FILE + ".broken"
            try:
                os.replace(DATA_FILE, backup)
            except OSError:
                pass
            print(f"[data] Could not read {DATA_FILE} ({e}); moved it to {backup} and started fresh.")
            loaded = {}
    data.clear()
    data.update(loaded)
    _defaults()


def save_data():
    """Atomic write, so a crash/restart can never leave a half-written file."""
    folder = os.path.dirname(DATA_FILE) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)


_defaults()


# ---------- access control ----------
def is_owner(user_id):
    return user_id == config.OWNER_ID


def is_trusted(user_id):
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False
    return user_id in data["trusted"]


def has_access(user_id):
    """Owner or trusted users."""
    return is_owner(user_id) or is_trusted(user_id)


def owner_only():
    async def predicate(ctx):
        return ctx.guild is not None and is_owner(ctx.author.id)
    return commands.check(predicate)


def trusted_only():
    async def predicate(ctx):
        return ctx.guild is not None and has_access(ctx.author.id)
    return commands.check(predicate)


# ---------- lookups ----------
def match_members(guild, query):
    """All members matching an ID / username / nickname (exact matches first)."""
    query = query.strip()
    if query.isdigit():
        m = guild.get_member(int(query))
        if m:
            return [m]
    q = query.lower().lstrip("@")
    if not q:
        return []
    exact = [m for m in guild.members
             if q in (m.name.lower(), m.display_name.lower(), (m.global_name or "").lower())]
    if exact:
        return exact
    return [m for m in guild.members if q in m.name.lower() or q in m.display_name.lower()]


def find_member(guild, query, quiet=False):
    found = match_members(guild, query)
    if len(found) == 1:
        return found[0]
    if len(found) > 1 and not quiet:
        print("Multiple matches, be more specific or use the ID:")
        for m in found[:10]:
            print(f"  {m}  ({m.id})")
    return None


def match_vcs(guild, text):
    text = text.strip()
    vcs = guild.voice_channels
    if text.isdigit() and len(text) >= 15:
        ch = discord.utils.get(vcs, id=int(text))
        return [ch] if ch else []
    t = text.lower()
    exact = [c for c in vcs if c.name.lower() == t]
    if exact:
        return exact
    starts = [c for c in vcs if c.name.lower().startswith(t)]
    return starts or [c for c in vcs if t in c.name.lower()]


def find_vc(guild, arg):
    """Console helper: list number (see 'channels'), channel ID, or name."""
    arg = arg.strip()
    vcs = guild.voice_channels
    if arg.isdigit() and len(arg) < 15:
        n = int(arg)
        return vcs[n - 1] if 1 <= n <= len(vcs) else None
    found = match_vcs(guild, arg)
    return found[0] if len(found) == 1 else None


def need_guild(bot):
    if bot.current_guild is None:
        print("No server selected. Use: guilds  then  guild <number>")
        return None
    return bot.current_guild


def humans_in(channel):
    return [m for m in channel.members if not m.bot]


# ---------- durations ----------
_DUR = re.compile(r"(\d+)\s*([smhdw])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_duration(text):
    """'30s', '10m', '2h', '1d', '1h30m' (a bare number = minutes) -> timedelta"""
    text = text.strip().lower()
    if text.isdigit():
        return timedelta(minutes=int(text))
    parts = _DUR.findall(text)
    if not parts or _DUR.sub("", text).strip():
        raise BotError("Use a duration like `30s`, `10m`, `2h`, `1d` or `1h30m`.")
    total = timedelta()
    for n, u in parts:
        total += timedelta(**{_UNITS[u]: int(n)})
    return total


def fmt_delta(delta):
    secs = int(delta.total_seconds())
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    out = [f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s")) if v]
    return " ".join(out) or "0s"
