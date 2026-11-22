"""Voice tools: move everyone, party shuffle, server mute/deafen, auto-mute on join."""
import asyncio
import random
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import (data, save_data, find_member, find_vc, need_guild, is_owner, has_access,
                   BotError)
from ui import BaseCog, emb, GREEN, RED, YELLOW, BLURPLE

GROUP = "voicetools"
MOVE_DELAY = 0.25      # pause between moves so Discord's rate limits stay happy


# ---------- helpers ----------
def reserved_channel_ids(guild):
    """Channels the party/move tools must leave alone: AFK, join-to-create hub + temp rooms."""
    ids = set()
    if guild.afk_channel:
        ids.add(guild.afk_channel.id)
    jtc = data.get("jtc", {})
    cfg = jtc.get("guilds", {}).get(str(guild.id))
    if cfg:
        ids.add(cfg["hub"])
    for cid, info in jtc.get("rooms", {}).items():
        if info.get("guild") == guild.id:
            ids.add(int(cid))
    for wid, rid in jtc.get("waiting", {}).items():          # waiting rooms belong to a temp room too
        if str(rid) in jtc.get("rooms", {}) and jtc["rooms"][str(rid)].get("guild") == guild.id:
            ids.add(int(wid))
    return ids


def can_move_in(guild, channel):
    p = channel.permissions_for(guild.me)
    return p.view_channel and p.connect and p.move_members


def party_channels(guild):
    reserved = reserved_channel_ids(guild)
    return [c for c in guild.voice_channels if c.id not in reserved and can_move_in(guild, c)]


async def move_members(members, dest):
    moved = failed = 0
    for m in members:
        try:
            await m.move_to(dest)
            moved += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
        await asyncio.sleep(MOVE_DELAY)
    return moved, failed


def plan_party(members, channels, rng=random):
    """Even, random spread: [(member, channel), ...]"""
    members, channels = list(members), list(channels)
    rng.shuffle(members)
    rng.shuffle(channels)
    return [(m, channels[i % len(channels)]) for i, m in enumerate(members)]


# ---------- operations (shared by console, slash/text commands and the AI) ----------
async def op_moveall(guild, src, dst):
    if src.id == dst.id:
        raise BotError("The source and destination are the same channel.")
    members = [m for m in src.members if not m.bot]
    if not members:
        raise BotError(f"Nobody (human) is in **{src.name}**.")
    if not can_move_in(guild, src):
        raise BotError(f"I need View, Connect and Move Members in **{src.name}**.")
    if not can_move_in(guild, dst):
        raise BotError(f"I need View, Connect and Move Members in **{dst.name}**.")
    moved, failed = await move_members(members, dst)
    text = f"🚚 Moved **{moved}** member(s) from **{src.name}** to **{dst.name}**."
    if failed:
        text += f"\n⚠️ {failed} could not be moved."
    return text


async def op_party(guild):
    chans = party_channels(guild)
    if len(chans) < 2:
        raise BotError("I need at least 2 voice channels where I can move people (View + Connect + Move Members).")
    members = [m for c in chans for m in c.members if not m.bot]
    if len(members) < 2:
        raise BotError("At least 2 people need to be in voice for a party shuffle.")
    plan = [(m, c) for m, c in plan_party(members, chans)
            if not (m.voice and m.voice.channel and m.voice.channel.id == c.id)]
    moved = failed = 0
    for m, c in plan:
        mv, fl = await move_members([m], c)
        moved += mv
        failed += fl
    text = f"🎉 **Party shuffle!** Mixed **{len(members)}** people across **{len(chans)}** channels ({moved} moved)."
    if failed:
        text += f"\n⚠️ {failed} could not be moved."
    return text


async def op_voice_state(member, mute=None, deafen=None):
    if not member.voice or not member.voice.channel:
        raise BotError(f"**{member.display_name}** isn't in a voice channel.")
    kw = {}
    if mute is not None:
        kw["mute"] = mute
    if deafen is not None:
        kw["deafen"] = deafen
    try:
        await member.edit(reason="[bot] voice state", **kw)
    except discord.Forbidden:
        raise BotError("I need **Mute Members** / **Deafen Members** (and a role above theirs).")
    bits = []
    if mute is not None:
        bits.append("server-muted 🔇" if mute else "unmuted 🔊")
    if deafen is not None:
        bits.append("server-deafened 🙉" if deafen else "undeafened 👂")
    return f"**{member.display_name}** is now " + " and ".join(bits) + "."


def op_automute(guild, member, mode):
    if member.bot:
        raise BotError("Bots can't be auto-muted.")
    if is_owner(member.id):
        raise BotError("I won't do that to the bot owner.")
    store = data["automute"].setdefault(str(guild.id), {})
    if mode == "off":
        store.pop(str(member.id), None)
        save_data()
        return f"✅ Auto-mute removed for **{member.display_name}**."
    store[str(member.id)] = mode
    save_data()
    what = {"mute": "muted", "deafen": "deafened", "both": "muted **and** deafened"}[mode]
    return f"🔇 **{member.display_name}** will be {what} automatically whenever they join voice."


class MovePicker(discord.ui.View):
    """Dropdown that asks 'where should I move them?'"""

    def __init__(self, user_id, src):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.src = src
        sel = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.voice],
                                       placeholder="Where should I move them?", min_values=1, max_values=1)
        sel.callback = self.picked
        self.add_item(sel)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=emb("This menu isn't yours.", RED), ephemeral=True)
            return False
        return True

    async def picked(self, interaction):
        chosen = self.children[0].values[0]
        dst = interaction.guild.get_channel(chosen.id)
        await interaction.response.edit_message(embed=emb(f"⏳ Moving everyone to **{chosen.name}**…", YELLOW), view=None)
        try:
            if dst is None:
                raise BotError("I can't see that channel.")
            result = emb(await op_moveall(interaction.guild, self.src, dst), GREEN)
        except BotError as e:
            result = emb(str(e), RED)
        await interaction.edit_original_response(embed=result)
        self.stop()


class VoiceTools(BaseCog):
    # ----- mute / deafen / moveall / party: owner + trusted -----
    @commands.hybrid_command(name="moveall", description="Move everyone from a channel to another (asks where if you skip it)")
    @app_commands.describe(from_channel="Channel to empty", to_channel="Where to move them (leave empty to be asked)")
    @commands.check(lambda ctx: ctx.guild is not None and has_access(ctx.author.id))
    async def moveall(self, ctx, from_channel: discord.VoiceChannel, to_channel: Optional[discord.VoiceChannel] = None):
        if to_channel is not None:
            return await ctx.send(embed=emb(await op_moveall(ctx.guild, from_channel, to_channel), GREEN))
        if not [m for m in from_channel.members if not m.bot]:
            raise BotError(f"Nobody (human) is in **{from_channel.name}**.")
        await ctx.send(embed=emb(f"Moving **{len([m for m in from_channel.members if not m.bot])}** member(s) from **{from_channel.name}**.\nPick the destination:", BLURPLE),
                       view=MovePicker(ctx.author.id, from_channel))

    @commands.hybrid_command(name="party", description="Shuffle party: randomly redistribute everyone in voice across channels")
    @commands.check(lambda ctx: ctx.guild is not None and has_access(ctx.author.id))
    async def party(self, ctx):
        async with ctx.typing():
            text = await op_party(ctx.guild)
        await ctx.send(embed=emb(text, GREEN))

    @commands.hybrid_command(name="mute", description="Server-mute a member in voice")
    @app_commands.describe(member="Who")
    @commands.check(lambda ctx: ctx.guild is not None and has_access(ctx.author.id))
    async def mute(self, ctx, member: discord.Member):
        await ctx.send(embed=emb(await op_voice_state(member, mute=True), GREEN))

    @commands.hybrid_command(name="unmute", description="Remove a member's server mute")
    @app_commands.describe(member="Who")
    @commands.check(lambda ctx: ctx.guild is not None and has_access(ctx.author.id))
    async def unmute(self, ctx, member: discord.Member):
        await ctx.send(embed=emb(await op_voice_state(member, mute=False), GREEN))

    @commands.hybrid_command(name="deafen", description="Server-deafen a member in voice")
    @app_commands.describe(member="Who")
    @commands.check(lambda ctx: ctx.guild is not None and has_access(ctx.author.id))
    async def deafen(self, ctx, member: discord.Member):
        await ctx.send(embed=emb(await op_voice_state(member, deafen=True), GREEN))

    @commands.hybrid_command(name="undeafen", description="Remove a member's server deafen")
    @app_commands.describe(member="Who")
    @commands.check(lambda ctx: ctx.guild is not None and has_access(ctx.author.id))
    async def undeafen(self, ctx, member: discord.Member):
        await ctx.send(embed=emb(await op_voice_state(member, deafen=False), GREEN))

    # ----- auto-mute: owner only -----
    @commands.hybrid_command(name="automute", description="Auto mute/deafen a member whenever they join voice")
    @app_commands.describe(member="Who", mode="mute, deafen, both, or off")
    @commands.check(lambda ctx: ctx.guild is not None and is_owner(ctx.author.id))
    async def automute(self, ctx, member: discord.Member, mode: Literal["mute", "deafen", "both", "off"] = "mute"):
        text = op_automute(ctx.guild, member, mode)
        if mode != "off" and member.voice and member.voice.channel:
            try:
                await op_voice_state(member, mute=mode in ("mute", "both") or None,
                                     deafen=True if mode in ("deafen", "both") else None)
            except BotError:
                pass
        await ctx.send(embed=emb(text, GREEN))

    @commands.hybrid_command(name="automutes", description="List auto-muted members")
    @commands.check(lambda ctx: ctx.guild is not None and is_owner(ctx.author.id))
    async def automutes(self, ctx):
        store = data["automute"].get(str(ctx.guild.id), {})
        text = "\n".join(f"<@{uid}> — **{mode}**" for uid, mode in store.items()) or "Nobody is auto-muted."
        await ctx.send(embed=emb(text, BLURPLE, "🔇 Auto-mute list"), allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # only on a fresh join (not when moving between channels)
        if member.bot or after.channel is None or before.channel is not None:
            return
        mode = data["automute"].get(str(member.guild.id), {}).get(str(member.id))
        if not mode:
            return
        kw = {}
        if mode in ("mute", "both"):
            kw["mute"] = True
        if mode in ("deafen", "both"):
            kw["deafen"] = True
        try:
            await member.edit(reason="[bot] auto-mute", **kw)
        except (discord.Forbidden, discord.HTTPException):
            print(f"[voicetools] couldn't auto-mute {member} (need Mute/Deafen Members + role above theirs)")


# ---------------- console ----------------
def _plain(text):
    return text.replace("**", "").replace("`", "")


async def c_moveall(bot, args):
    g = need_guild(bot)
    if not g:
        return
    if not args:
        print("Usage: moveall <from> [to]   (number, ID or name - see 'channels')")
        return
    src = find_vc(g, args[0])
    if not src:
        print("Source channel not found.")
        return
    if len(args) >= 2:
        dst = find_vc(g, " ".join(args[1:]))
    else:
        for i, c in enumerate(g.voice_channels, 1):
            print(f"  {i}. {c.name}")
        answer = (await bot.ask("Move them to which channel? (number/ID/name, blank = cancel): ")).strip()
        if not answer:
            print("Cancelled.")
            return
        dst = find_vc(g, answer)
    if not dst:
        print("Destination channel not found.")
        return
    print(_plain(await op_moveall(g, src, dst)))


async def c_party(bot, args):
    g = need_guild(bot)
    if g:
        print(_plain(await op_party(g)))


def _voice_cmd(mute=None, deafen=None, name="mute"):
    async def handler(bot, args):
        g = need_guild(bot)
        if not g:
            return
        m = find_member(g, " ".join(args)) if args else None
        if not m:
            print(f"Usage: {name} <user>   (member not found)")
            return
        print(_plain(await op_voice_state(m, mute=mute, deafen=deafen)))
    return handler


async def c_automute(bot, args):
    g = need_guild(bot)
    if not g:
        return
    m = find_member(g, args[0]) if args else None
    mode = (args[1].lower() if len(args) > 1 else "mute")
    if not m or mode not in ("mute", "deafen", "both", "off"):
        print("Usage: automute <user> [mute|deafen|both|off]")
        return
    print(_plain(op_automute(g, m, mode)))


async def setup(bot):
    await bot.add_cog(VoiceTools())
    a = bot.add_console
    a("moveall", c_moveall, "moveall <from> [to]", "move everyone; asks where if [to] is missing", group=GROUP)
    a("party", c_party, "party", "shuffle party: randomly mix everyone in voice", group=GROUP)
    a("mute", _voice_cmd(mute=True, name="mute"), "mute <user>", "server-mute", group=GROUP)
    a("unmute", _voice_cmd(mute=False, name="unmute"), "unmute <user>", "remove server-mute", group=GROUP)
    a("deafen", _voice_cmd(deafen=True, name="deafen"), "deafen <user>", "server-deafen", group=GROUP)
    a("undeafen", _voice_cmd(deafen=False, name="undeafen"), "undeafen <user>", "remove server-deafen", group=GROUP)
    a("automute", c_automute, "automute <user> [mode]", "auto mute/deafen on join (mute|deafen|both|off)", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
