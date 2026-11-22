"""Categorized public help command for the bot."""
import discord
from discord import app_commands
from discord.ext import commands

from ui import BaseCog, RED, BLURPLE, emb

CATEGORIES = {
    "home": ("Bot Help", "Choose a category below."),
    "music": ("Music", "`/spotifyconnect` (owner setup) `/play` `/join` `/leave` `/queue` `/nowplaying`\n`/skip` `/pause` `/resume` `/stop`\n`/volume` `/loop` `/shuffle` `/remove` `/testaudio`\nConnect once to read Spotify playlists you own or collaborate on. Audio is matched from SoundCloud."),
    "voice": ("Voice", "`/moveall` `/party` `/mute` `/unmute` `/deafen` `/undeafen`\n`/automute` `/automutes`\n`/jtc setup` `/jtc settings` `/jtc status` `/jtc panel` `/jtc disable`\n`/voice name|limit|privacy|waiting|chat|trust|untrust|invite|kick|block|unblock|claim|transfer|info|save|reset|delete`"),
    "moderation": ("Moderation", "`/kick` `/ban` `/unban` `/banlist`\n`/timeout` `/untimeout` `/timeouts`"),
    "automation": ("Automation", "`/setchannels` `/drag` `/autodc` `/stopautodc`\n`/undc` `/stopdc` `/undrag` `/undragall` `/targets` `/dragdelay`"),
    "tools": ("Tools", "`/ai` `/voicecmd` `/aichannel` `/aireset`\n`/trust` `/untrust` `/trusted`\n`/status` `/roulette` `/botstatus`"),
}


def make_embed(category, bot=None):
    title, text = CATEGORIES[category]
    embed = discord.Embed(title=f"✨ {title}", description=text, color=BLURPLE)
    if bot and bot.user:
        embed.set_author(name=f"{bot.user.name} • Help Center", icon_url=bot.user.display_avatar.url)
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    if category == "home":
        embed.add_field(name="🚀 Quick start", value="Join a voice channel, then use `/play <song>` or `/help music`.", inline=False)
        embed.add_field(name="🔐 Access", value="Music and voice tools require owner/trusted access. Moderation is owner-only.", inline=False)
    embed.set_footer(text="Click a category below • Use /help <category> for a direct page")
    embed.timestamp = discord.utils.utcnow()
    return embed


class HelpView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=emb("This help menu belongs to the person who opened it.", RED), ephemeral=True)
            return False
        return True

    async def show(self, interaction, category):
        await interaction.response.edit_message(embed=make_embed(category), view=self)

    @discord.ui.button(label="Home", style=discord.ButtonStyle.primary, row=0)
    async def home(self, interaction, button):
        await self.show(interaction, "home")

    @discord.ui.button(label="Music", style=discord.ButtonStyle.success, row=0)
    async def music(self, interaction, button):
        await self.show(interaction, "music")

    @discord.ui.button(label="Voice", style=discord.ButtonStyle.secondary, row=0)
    async def voice(self, interaction, button):
        await self.show(interaction, "voice")

    @discord.ui.button(label="Moderation", style=discord.ButtonStyle.danger, row=1)
    async def moderation(self, interaction, button):
        await self.show(interaction, "moderation")

    @discord.ui.button(label="Automation", style=discord.ButtonStyle.secondary, row=1)
    async def automation(self, interaction, button):
        await self.show(interaction, "automation")

    @discord.ui.button(label="Tools", style=discord.ButtonStyle.secondary, row=1)
    async def tools(self, interaction, button):
        await self.show(interaction, "tools")


class Help(BaseCog):
    async def cog_check(self, ctx):
        return ctx.guild is not None

    @commands.hybrid_command(name="help", aliases=["commands"], description="Show categorized bot help")
    @app_commands.describe(category="home, music, voice, moderation, automation or tools")
    async def help(self, ctx, category: str = "home"):
        category = category.lower()
        if category not in CATEGORIES:
            category = "home"
        await ctx.send(embed=make_embed(category, ctx.bot), view=HelpView(ctx.author.id))


async def setup(bot):
    await bot.add_cog(Help(bot))


async def teardown(bot):
    pass

