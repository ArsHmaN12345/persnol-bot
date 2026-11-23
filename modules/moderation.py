"""Kick, ban/unban and timeout (owner only). Destructive ones ask for confirmation."""
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils import (data, save_data, find_member, need_guild, is_owner, parse_duration,
                   fmt_delta, BotError)
from ui import BaseCog, emb, confirm, GREEN, YELLOW, BLURPLE

GROUP = "moderation"
MAX_TIMEOUT = timedelta(days=28)


def _reason(text):
    return f"[bot] {text or 'no reason given'}"[:512]


def check_target(guild, member):
    """Refuse targets the bot must never (or can never) act on."""
    if member.id == guild.me.id:
        raise BotError("I can't do that to myself.")
    if is_owner(member.id):
        raise BotError("I won't do that to the bot owner.")
    if member.id == guild.owner_id:
        raise BotError("I can't act on the server owner.")
    if guild.me.top_role <= member.top_role:
        raise BotError("My role must be above that member's top role (Server Settings → Roles).")


# ---------- operations (shared by console, slash/text commands and the AI) ----------
async def op_kick(guild, member, reason=None):
    check_target(guild, member)
    try:
        await member.kick(reason=_reason(reason))
    except discord.Forbidden:
        raise BotError("I need the **Kick Members** permission.")
    return f"👢 Kicked **{member}**."


async def op_ban(guild, target, reason=None):
    if isinstance(target, discord.Member):
        check_target(guild, target)
    elif is_owner(target.id) or target.id == guild.me.id:
        raise BotError("I won't ban that account.")
    try:
        await guild.ban(target, reason=_reason(reason))
    except discord.Forbidden:
        raise BotError("I need the **Ban Members** permission (and a role above theirs).")
    data["targets"].get(str(guild.id), {}).pop(str(target.id), None)
    save_data()
    return f"🔨 Banned **{target}**."


async def op_unban(guild, query):
    query = query.strip()
    try:
        async for entry in guild.bans(limit=None):
            u = entry.user
            if (query.isdigit() and u.id == int(query)) or query.lower() in (u.name.lower(), str(u).lower()):
                await guild.unban(u, reason=_reason("unban"))
                return f"✅ Unbanned **{u}**."
    except discord.Forbidden:
        raise BotError("I need the **Ban Members** permission.")
    raise BotError("Not found in the ban list.")


async def op_timeout(guild, member, delta, reason=None):
    check_target(guild, member)
    if delta <= timedelta(0):
        raise BotError("The duration must be longer than 0.")
    if delta > MAX_TIMEOUT:
        raise BotError("Discord's maximum timeout is 28 days.")
    try:
        await member.timeout(delta, reason=_reason(reason))
    except discord.Forbidden:
        raise BotError("I need the **Moderate Members** permission (and I can't time out admins).")
    return f"🔇 **{member}** timed out for **{fmt_delta(delta)}**."


async def op_untimeout(guild, member):
    if not member.is_timed_out():
        raise BotError(f"**{member}** isn't timed out.")
    try:
        await member.timeout(None, reason=_reason("timeout removed"))
    except discord.Forbidden:
        raise BotError("I need the **Moderate Members** permission.")
    return f"🔊 Timeout removed for **{member}**."


def timeouts_text(guild):
    now = discord.utils.utcnow()
    lines = []
    for m in guild.members:
        until = m.timed_out_until
        if until and until > now:
            lines.append(f"{m.mention} — {fmt_delta(until - now)} left")
    return "\n".join(lines) or "Nobody is timed out."


async def banlist_text(guild):
    out = []
    try:
        async for entry in guild.bans(limit=50):
            out.append(f"{entry.user} (`{entry.user.id}`) — {entry.reason or '-'}")
    except discord.Forbidden:
        raise BotError("I need the **Ban Members** permission.")
    return "\n".join(out) or "No bans."


class Moderation(BaseCog):
    async def cog_check(self, ctx):
        return ctx.guild is not None and is_owner(ctx.author.id)

    @commands.hybrid_command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Who to kick", reason="Why")
    async def kick(self, ctx, member: discord.Member, *, reason: str = None):
        check_target(ctx.guild, member)
        if not await confirm(ctx, ctx.author.id, f"Kick **{member}** from the server?"):
            return
        await ctx.send(embed=emb(await op_kick(ctx.guild, member, reason), GREEN))

    @commands.hybrid_command(name="ban", description="Ban a user (works on people who already left too)")
    @app_commands.describe(user="Who to ban", reason="Why")
    async def ban(self, ctx, user: discord.User, *, reason: str = None):
        target = ctx.guild.get_member(user.id) or user
        if isinstance(target, discord.Member):
            check_target(ctx.guild, target)
        if not await confirm(ctx, ctx.author.id, f"Ban **{user}** from the server?"):
            return
        await ctx.send(embed=emb(await op_ban(ctx.guild, target, reason), GREEN))

    @commands.hybrid_command(name="unban", description="Unban a user by ID or username")
    @app_commands.describe(user="User ID or exact username")
    async def unban(self, ctx, user: str):
        await ctx.send(embed=emb(await op_unban(ctx.guild, user), GREEN))

    @commands.hybrid_command(name="banlist", description="Show banned users")
    async def banlist(self, ctx):
        await ctx.send(embed=emb(await banlist_text(ctx.guild), BLURPLE, "🔨 Bans"))

    @commands.hybrid_command(name="timeout", description="Time a member out (e.g. 10m, 2h, 1d)")
    @app_commands.describe(member="Who", duration="How long: 30s, 10m, 2h, 1d, 1h30m", reason="Why")
    async def timeout(self, ctx, member: discord.Member, duration: str, *, reason: str = None):
        await ctx.send(embed=emb(await op_timeout(ctx.guild, member, parse_duration(duration), reason), YELLOW))

    @commands.hybrid_command(name="untimeout", description="Remove a member's timeout")
    @app_commands.describe(member="Who")
    async def untimeout(self, ctx, member: discord.Member):
        await ctx.send(embed=emb(await op_untimeout(ctx.guild, member), GREEN))

    @commands.hybrid_command(name="timeouts", description="List members who are currently timed out")
    async def timeouts(self, ctx):
        await ctx.send(embed=emb(timeouts_text(ctx.guild), BLURPLE, "🔇 Timeouts"), allowed_mentions=discord.AllowedMentions.none())


# ---------------- console ----------------
def _plain(text):
    return text.replace("**", "").replace("`", "")


async def _member(bot, args, usage):
    g = need_guild(bot)
    if not g:
        return None, None
    if not args:
        print(f"Usage: {usage}")
        return g, None
    m = find_member(g, args[0])
    if not m:
        print("Member not found.")
    return g, m


async def c_kick(bot, args):
    g, m = await _member(bot, args, "kick <user> [reason]")
    if not m:
        return
    check_target(g, m)
    if (await bot.ask(f"Kick {m}? (y/n) ")).strip().lower() != "y":
        print("Cancelled.")
        return
    print(_plain(await op_kick(g, m, " ".join(args[1:]) or None)))


async def c_ban(bot, args):
    g = need_guild(bot)
    if not g:
        return
    if not args:
        print("Usage: ban <user ID or name> [reason]")
        return
    reason = " ".join(args[1:]) or None
    if args[0].isdigit():
        uid = int(args[0])
        target = g.get_member(uid)
        if target is None:
            try:
                target = await bot.fetch_user(uid)
            except discord.NotFound:
                print("No such user.")
                return
    else:
        target = find_member(g, args[0])
        if not target:
            print("Member not found.")
            return
    if isinstance(target, discord.Member):
        check_target(g, target)
    if (await bot.ask(f"Ban {target}? (y/n) ")).strip().lower() != "y":
        print("Cancelled.")
        return
    print(_plain(await op_ban(g, target, reason)))


async def c_unban(bot, args):
    g = need_guild(bot)
    if g:
        print(_plain(await op_unban(g, " ".join(args))) if args else "Usage: unban <user ID or name>")


async def c_banlist(bot, args):
    g = need_guild(bot)
    if g:
        print(_plain(await banlist_text(g)))


async def c_timeout(bot, args):
    g, m = await _member(bot, args, "timeout <user> <duration e.g. 10m> [reason]")
    if not m:
        return
    if len(args) < 2:
        print("Usage: timeout <user> <duration e.g. 10m> [reason]")
        return
    print(_plain(await op_timeout(g, m, parse_duration(args[1]), " ".join(args[2:]) or None)))


async def c_untimeout(bot, args):
    g, m = await _member(bot, args, "untimeout <user>")
    if m:
        print(_plain(await op_untimeout(g, m)))


async def c_timeouts(bot, args):
    g = need_guild(bot)
    if g:
        print(_plain(timeouts_text(g)))


async def setup(bot):
    await bot.add_cog(Moderation())
    a = bot.add_console
    a("kick", c_kick, "kick <user> [reason]", "kick a member (asks to confirm)", group=GROUP)
    a("ban", c_ban, "ban <user> [reason]", "ban by ID or name (asks to confirm)", group=GROUP)
    a("unban", c_unban, "unban <user ID or name>", "unban someone", group=GROUP)
    a("banlist", c_banlist, "banlist", "show banned users", group=GROUP)
    a("timeout", c_timeout, "timeout <user> <10m> [reason]", "time a member out", group=GROUP)
    a("untimeout", c_untimeout, "untimeout <user>", "remove a timeout", group=GROUP)
    a("timeouts", c_timeouts, "timeouts", "list active timeouts", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
