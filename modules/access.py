"""Trust system. Only the OWNER can add/remove trusted users.
Trusted users can use the music, voice-tool and AI commands - nothing destructive."""
import discord
from discord import app_commands
from discord.ext import commands

from utils import data, save_data, find_member, need_guild, is_owner, BotError
from ui import BaseCog, emb, GREEN, RED, BLURPLE

GROUP = "access"


class Access(BaseCog):
    async def cog_check(self, ctx):
        return ctx.guild is not None and is_owner(ctx.author.id)

    @commands.hybrid_command(name="trust", description="Let a member use the music, voice and AI commands")
    @app_commands.describe(member="Who to trust")
    async def trust(self, ctx, member: discord.Member):
        if member.bot:
            raise BotError("Bots can't be trusted.")
        if is_owner(member.id):
            raise BotError("You're the owner already.")
        if member.id in data["trusted"]:
            raise BotError(f"{member.mention} is already trusted.")
        data["trusted"].append(member.id)
        save_data()
        await ctx.send(embed=emb(f"✅ {member.mention} is now **trusted**.", GREEN))

    @commands.hybrid_command(name="untrust", description="Remove a member's trusted access")
    @app_commands.describe(member="Who to remove")
    async def untrust(self, ctx, member: discord.Member):
        if member.id not in data["trusted"]:
            raise BotError(f"{member.mention} isn't trusted.")
        data["trusted"].remove(member.id)
        save_data()
        await ctx.send(embed=emb(f"🚫 {member.mention} is no longer trusted.", RED))

    @commands.hybrid_command(name="trusted", description="List trusted members")
    async def trusted(self, ctx):
        ids = data["trusted"]
        text = "\n".join(f"<@{i}>" for i in ids) if ids else "Nobody is trusted yet."
        await ctx.send(embed=emb(text, BLURPLE, "🔑 Trusted users"))


# ---------------- console ----------------
def _resolve(bot, query):
    if query.isdigit():
        return int(query)
    g = need_guild(bot)
    if not g:
        return None
    m = find_member(g, query)
    if not m:
        print("Member not found.")
        return None
    return m.id


async def c_trust(bot, args):
    if not args:
        print("Usage: trust <user ID or name>")
        return
    uid = _resolve(bot, " ".join(args))
    if uid is None:
        return
    if is_owner(uid):
        print("That's you, the owner.")
    elif uid in data["trusted"]:
        print("Already trusted.")
    else:
        data["trusted"].append(uid)
        save_data()
        print(f"Trusted {uid}.")


async def c_untrust(bot, args):
    if not args:
        print("Usage: untrust <user ID or name>")
        return
    uid = _resolve(bot, " ".join(args))
    if uid is None:
        return
    if uid in data["trusted"]:
        data["trusted"].remove(uid)
        save_data()
        print(f"Removed trust from {uid}.")
    else:
        print("That user isn't trusted.")


async def c_trusted(bot, args):
    if not data["trusted"]:
        print("Nobody is trusted yet.")
    for uid in data["trusted"]:
        print(f"  {bot.get_user(uid) or 'unknown'} ({uid})")


async def setup(bot):
    await bot.add_cog(Access())
    a = bot.add_console
    a("trust", c_trust, "trust <user>", "let a user use music/voice/AI commands", group=GROUP)
    a("untrust", c_untrust, "untrust <user>", "remove a user's access", group=GROUP)
    a("trusted", c_trusted, "trusted", "list trusted users", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
