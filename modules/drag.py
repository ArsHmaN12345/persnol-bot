import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import data, save_data, find_member, find_vc, need_guild, is_owner, BotError
from ui import BaseCog, emb, GREEN, BLURPLE

GROUP = "drag"


def get_mode(guild_id, user_id):
    return data["targets"].get(str(guild_id), {}).get(str(user_id))


def get_pair(guild):
    ids = data["channels"].get(str(guild.id))
    if not ids or len(ids) != 2:
        return None
    a, b = guild.get_channel(ids[0]), guild.get_channel(ids[1])
    return (a, b) if a and b else None


# ---------- operations (shared by console, slash/text commands and the AI) ----------
def op_set_channels(guild, vc1, vc2):
    if vc1.id == vc2.id:
        raise BotError("Pick 2 different voice channels.")
    data["channels"][str(guild.id)] = [vc1.id, vc2.id]
    save_data()
    return f"🎯 Drag channels: **{vc1.name}** ⇄ **{vc2.name}**"


def op_drag(cog, guild, member):
    if member.bot:
        raise BotError("I won't drag bots.")
    if is_owner(member.id):
        raise BotError("I won't do that to the bot owner.")
    if get_pair(guild) is None:
        raise BotError("Set the 2 drag channels first (`/setchannels`).")
    data["targets"].setdefault(str(guild.id), {})[str(member.id)] = "drag"
    save_data()
    if member.voice and member.voice.channel:
        cog.start(guild, member.id)
    return f"🌀 Auto-drag set on **{member.display_name}**."


async def op_autodc(cog, guild, member):
    if member.bot:
        raise BotError("I won't do that to bots.")
    if is_owner(member.id):
        raise BotError("I won't do that to the bot owner.")
    cog.stop(guild.id, member.id)
    data["targets"].setdefault(str(guild.id), {})[str(member.id)] = "disconnect"
    save_data()
    if member.voice and member.voice.channel:
        try:
            await member.move_to(None)
        except (discord.Forbidden, discord.HTTPException):
            pass
    return f"⛔ Auto-disconnect set on **{member.display_name}**."


def op_undrag(cog, guild, member):
    data["targets"].get(str(guild.id), {}).pop(str(member.id), None)
    cog.stop(guild.id, member.id)
    save_data()
    return f"✅ Removed **{member.display_name}**."


def op_undragall(cog, guild):
    for uid in list(data["targets"].get(str(guild.id), {})):
        cog.stop(guild.id, int(uid))
    data["targets"][str(guild.id)] = {}
    save_data()
    return "✅ Cleared all drag/disconnect targets."


MIN_DELAY = 1.0


def set_drag_delay(seconds):
    """Change the drag speed and remember it across restarts."""
    config.DRAG_DELAY = max(MIN_DELAY, float(seconds))
    data["settings"]["drag_delay"] = config.DRAG_DELAY
    save_data()
    return config.DRAG_DELAY


def targets_text(guild):
    pair = get_pair(guild)
    lines = [f"**Channels:** {pair[0].mention} ⇄ {pair[1].mention}" if pair else "**Channels:** not set"]
    tg = data["targets"].get(str(guild.id), {})
    lines += [f"<@{uid}> — **{mode}**" for uid, mode in tg.items()] or ["No targets."]
    return "\n".join(lines)


class Drag(BaseCog):
    def __init__(self, bot):
        self.bot = bot
        self.tasks = {}

    def cog_unload(self):
        for t in self.tasks.values():
            t.cancel()
        self.tasks.clear()

    async def cog_check(self, ctx):
        return ctx.guild is not None and is_owner(ctx.author.id)

    async def _loop(self, guild, user_id):
        key = (guild.id, user_id)
        try:
            while True:
                member = guild.get_member(user_id)
                if not member or not member.voice or not member.voice.channel:
                    break
                if get_mode(guild.id, user_id) != "drag":
                    break
                pair = get_pair(guild)
                if not pair:
                    break
                a, b = pair
                dest = b if member.voice.channel.id == a.id else a
                try:
                    await member.move_to(dest)
                except discord.Forbidden:
                    print(f"[drag] No permission to move {member}. Need Move Members + role above theirs.")
                    break
                except discord.HTTPException:
                    await asyncio.sleep(3)
                    continue
                await asyncio.sleep(max(1.0, config.DRAG_DELAY))
        finally:
            if self.tasks.get(key) is asyncio.current_task():
                self.tasks.pop(key, None)

    def start(self, guild, user_id):
        key = (guild.id, user_id)
        t = self.tasks.get(key)
        if t and not t.done():
            return
        self.tasks[key] = asyncio.create_task(self._loop(guild, user_id))

    def stop(self, guild_id, user_id):
        t = self.tasks.pop((guild_id, user_id), None)
        if t:
            t.cancel()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        mode = get_mode(member.guild.id, member.id)
        if mode is None or after.channel is None:
            return
        if mode == "disconnect":
            if before.channel != after.channel:
                try:
                    await member.move_to(None)
                except (discord.Forbidden, discord.HTTPException):
                    pass
        elif mode == "drag":
            self.start(member.guild, member.id)

    # ---------- slash + text commands (owner only) ----------
    @commands.hybrid_command(name="setchannels", aliases=["setvc"], description="Choose the 2 voice channels used for dragging")
    @app_commands.describe(vc1="First voice channel", vc2="Second voice channel")
    async def setchannels(self, ctx, vc1: discord.VoiceChannel, vc2: discord.VoiceChannel):
        await ctx.send(embed=emb(op_set_channels(ctx.guild, vc1, vc2), GREEN))

    @commands.hybrid_command(name="drag", description="Drag a member between the 2 channels every time they join voice")
    @app_commands.describe(member="Who to drag")
    async def drag(self, ctx, member: discord.Member):
        await ctx.send(embed=emb(op_drag(self, ctx.guild, member), GREEN))

    @commands.hybrid_command(name="autodc", description="Auto-disconnect a member whenever they join voice")
    @app_commands.describe(member="Who to disconnect")
    async def autodc(self, ctx, member: discord.Member):
        await ctx.send(embed=emb(await op_autodc(self, ctx.guild, member), GREEN))

    @commands.hybrid_command(name="undrag", aliases=["untarget"], description="Remove a member from the drag/disconnect list")
    @app_commands.describe(member="Who to remove")
    async def undrag(self, ctx, member: discord.Member):
        await ctx.send(embed=emb(op_undrag(self, ctx.guild, member), GREEN))

    @commands.hybrid_command(name="undragall", description="Clear every drag/disconnect target")
    async def undragall(self, ctx):
        await ctx.send(embed=emb(op_undragall(self, ctx.guild), GREEN))

    @commands.hybrid_command(name="targets", description="Show the drag channels and targets")
    async def targets(self, ctx):
        await ctx.send(embed=emb(targets_text(ctx.guild), BLURPLE))

    @commands.hybrid_command(name="dragdelay", description="Show or change the drag speed (seconds between moves)")
    @app_commands.describe(seconds="New delay in seconds, minimum 1 (leave empty to just show it)")
    async def dragdelay(self, ctx, seconds: Optional[float] = None):
        if seconds is None:
            return await ctx.send(embed=emb(f"⏱️ Drag delay is **{config.DRAG_DELAY}s** between moves.\n"
                                            f"Change it with `/dragdelay 2` (minimum {MIN_DELAY}s).", BLURPLE))
        note = f"\n⚠️ Minimum is {MIN_DELAY}s, so I used that." if seconds < MIN_DELAY else ""
        await ctx.send(embed=emb(f"⏱️ Drag delay set to **{set_drag_delay(seconds)}s** (saved).{note}", GREEN))


# ---------- console ----------
def _cog(bot):
    return bot.get_cog("Drag")


async def c_setchannels(bot, args):
    g = need_guild(bot)
    if not g:
        return
    if len(args) < 2:
        print("Usage: setchannels <vc1> <vc2>   (number, ID or name)")
        return
    v1, v2 = find_vc(g, args[0]), find_vc(g, args[1])
    if not v1 or not v2:
        print("Voice channel not found. Use 'channels' to see the list.")
        return
    print(op_set_channels(g, v1, v2).replace("**", ""))


async def c_drag(bot, args):
    g = need_guild(bot)
    if not g:
        return
    m = find_member(g, " ".join(args)) if args else None
    if not m:
        print("Usage: drag <user>   (member not found)")
        return
    print(op_drag(_cog(bot), g, m).replace("**", ""))


async def c_disconnect(bot, args):
    g = need_guild(bot)
    if not g:
        return
    m = find_member(g, " ".join(args)) if args else None
    if not m:
        print("Usage: disconnect <user>   (member not found)")
        return
    print((await op_autodc(_cog(bot), g, m)).replace("**", ""))


async def c_stop(bot, args):
    g = need_guild(bot)
    if not g:
        return
    m = find_member(g, " ".join(args)) if args else None
    if not m:
        print("Usage: stop <user>   (member not found)")
        return
    print(op_undrag(_cog(bot), g, m).replace("**", ""))


async def c_stopall(bot, args):
    g = need_guild(bot)
    if g:
        print(op_undragall(_cog(bot), g))


async def c_list(bot, args):
    g = need_guild(bot)
    if not g:
        return
    pair = get_pair(g)
    print("Drag channels:", f"{pair[0].name} <-> {pair[1].name}" if pair else "not set")
    tg = data["targets"].get(str(g.id), {})
    if not tg:
        print("No targets set.")
    for uid, mode in tg.items():
        print(f"  {g.get_member(int(uid)) or uid} - {mode}")


async def c_delay(bot, args):
    try:
        print(f"Drag delay set to {set_drag_delay(args[0])}s (saved).")
    except (IndexError, ValueError):
        print(f"Usage: delay <seconds>   (now {config.DRAG_DELAY}s, minimum {MIN_DELAY})")


async def setup(bot):
    saved = data["settings"].get("drag_delay")
    if saved:
        config.DRAG_DELAY = max(MIN_DELAY, float(saved))
    await bot.add_cog(Drag(bot))
    a = bot.add_console
    a("setchannels", c_setchannels, "setchannels <vc1> <vc2>", "choose the 2 drag channels", aliases=("setvc",), group=GROUP)
    a("drag", c_drag, "drag <user>", "auto drag between the 2 channels", group=GROUP)
    a("disconnect", c_disconnect, "disconnect <user>", "auto disconnect on voice join", aliases=("dc",), group=GROUP)
    a("stop", c_stop, "stop <user>", "remove a drag/disconnect target", aliases=("remove",), group=GROUP)
    a("stopall", c_stopall, "stopall", "clear all targets", group=GROUP)
    a("list", c_list, "list", "show drag channels + targets", group=GROUP)
    a("delay", c_delay, "delay <seconds>", "change drag speed live", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
