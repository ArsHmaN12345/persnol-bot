"""Shared embeds, confirmation buttons and one error handler for every module."""
import traceback

import discord
from discord.ext import commands

from utils import BotError

BLURPLE, GREEN, YELLOW, RED = 0x5865F2, 0x57F287, 0xFEE75C, 0xED4245
NO_PINGS = discord.AllowedMentions.none()


def emb(text, color=BLURPLE, title=None):
    return discord.Embed(title=title, description=text, color=color)


class ConfirmView(discord.ui.View):
    def __init__(self, user_id, timeout=30):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.value = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=emb("Only the person who asked can confirm this.", RED), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.success)
    async def yes(self, interaction, button):
        self.value = True
        self.stop()
        await interaction.response.edit_message(view=None)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(embed=emb("Cancelled.", RED), view=None)


async def confirm(dest, user_id, text, timeout=30):
    """Ask `user_id` to confirm in `dest` (a Context or a text channel). Returns True/False."""
    view = ConfirmView(user_id, timeout)
    msg = await dest.send(embed=emb(text, YELLOW, "⚠️ Confirm"), view=view, allowed_mentions=NO_PINGS)
    await view.wait()
    if view.value is None:
        try:
            await msg.edit(embed=emb("⌛ Timed out - cancelled.", RED), view=None)
        except discord.HTTPException:
            pass
    return bool(view.value)


def _unwrap(error):
    err = error
    for _ in range(4):
        inner = getattr(err, "original", None) or getattr(err, "__cause__", None)
        if inner is None or inner is err:
            break
        err = inner
    return err


async def handle_error(ctx, error):
    err = _unwrap(error)
    slash = ctx.interaction is not None
    if isinstance(err, commands.CommandNotFound):
        return
    if isinstance(err, BotError):
        text = str(err)
    elif isinstance(error, commands.CheckFailure):
        if not slash:
            return                                   # strangers are silently ignored in text chat
        text = "🔒 Only the owner and trusted users can use this command."
    elif isinstance(error, commands.MissingRequiredArgument):
        text = f"Usage: `{ctx.prefix or '/'}{ctx.command.qualified_name} {ctx.command.signature}`"
    elif isinstance(error, (commands.MemberNotFound, commands.UserNotFound, commands.ChannelNotFound)):
        text = "I couldn't find that. Mention it or use its ID."
    elif isinstance(error, commands.BadArgument) or isinstance(err, commands.BadArgument):
        text = f"That value isn't valid. Usage: `{ctx.prefix or '/'}{ctx.command.qualified_name} {ctx.command.signature}`"
    elif isinstance(err, discord.Forbidden):
        text = "I don't have permission to do that. Check my role/permissions."
    else:
        print(f"[error] /{getattr(ctx.command, 'qualified_name', '?')}: {type(err).__name__}: {err}")
        traceback.print_exception(type(err), err, err.__traceback__)
        text = "❌ Something went wrong."
    try:
        await ctx.send(embed=emb(text, RED), ephemeral=slash, allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass


class BaseCog(commands.Cog):
    async def cog_command_error(self, ctx, error):
        await handle_error(ctx, error)
