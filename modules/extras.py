"""Small extras: /status and /roulette."""
import asyncio
import platform
import random
import time

import discord
from discord.ext import commands

from utils import data, is_owner, has_access, BotError
from ui import BaseCog, emb, BLURPLE, RED, YELLOW

GROUP = "extras"


def _uptime(seconds):
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return " ".join(f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s")) if v) or "0s"


def status_embed(bot):
    music = bot.get_cog("Music")
    playing = sum(1 for p in music.players.values() if p.current) if music else 0
    ai = bot.get_cog("AI")
    if ai is None:
        ai_text = "module off"
    elif not ai.client.configured():
        ai_text = "not configured"
    else:
        ai_text = f"`{ai.client.model}`"
    rooms = len(data["jtc"]["rooms"])
    e.set_footer(text=f"discord.py {discord.__version__} • Python {platform.python_version()}")
    return e


async def op_roulette(guild, channel):
    humans = [m for m in channel.members if not m.bot]
    if len(humans) < 2:
        raise BotError("Roulette needs at least 2 people in the voice channel.")
    if not channel.permissions_for(guild.me).move_members:
        raise BotError("I need **Move Members** in that channel.")
    victim = random.choice(humans)
    try:
        await victim.move_to(None, reason="[bot] voice roulette")
    except (discord.Forbidden, discord.HTTPException):
        raise BotError("I couldn't disconnect the chosen person.")
    return victim


class Extras(BaseCog):
    @commands.hybrid_command(name="status", description="Bot health, uptime and what's running")
    @commands.check(lambda ctx: ctx.guild is not None and has_access(ctx.author.id))
    async def status(self, ctx):
        await ctx.send(embed=status_embed(self.bot_ref(ctx)))

    @commands.hybrid_command(name="roulette", description="Voice roulette: disconnect a random person from your channel")
    @discord.app_commands.describe(channel="Voice channel (default: the one you're in)")
    @commands.check(lambda ctx: ctx.guild is not None and is_owner(ctx.author.id))
    async def roulette(self, ctx, channel: discord.VoiceChannel = None):
        channel = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if channel is None:
            raise BotError("Join a voice channel or pick one.")
        msg = await ctx.send(embed=emb("🔫 Spinning the cylinder…", YELLOW, "Voice Roulette"))
        await asyncio.sleep(2)
        victim = await op_roulette(ctx.guild, channel)
        await msg.edit(embed=emb(f"💥 **{victim.display_name}** got disconnected!", RED, "Voice Roulette"))

    @staticmethod
    def bot_ref(ctx):
        return ctx.bot


async def setup(bot):
    await bot.add_cog(Extras())


async def teardown(bot):
    pass
