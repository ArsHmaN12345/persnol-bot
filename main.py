import asyncio
import os
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import utils
from ui import emb, handle_error, RED

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True


class DragBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=config.PREFIX, intents=intents, help_command=None)
        self.console = {}            # console command registry
        self.current_guild = None
        self.started_at = time.time()
        self._synced = False

    # ----- console command registry (used by modules) -----
    def add_console(self, name, func, usage="", desc="", aliases=(), group="core"):
        entry = {"name": name, "func": func, "usage": usage or name, "desc": desc, "group": group}
        for n in (name, *aliases):
            self.console[n] = entry

    def remove_console_group(self, group):
        for k in [k for k, e in self.console.items() if e["group"] == group]:
            del self.console[k]

    async def ask(self, prompt):
        return await asyncio.get_running_loop().run_in_executor(None, input, prompt)

    # ----- slash commands -----
    async def sync_slash(self, guilds=None):
        """Slash commands are synced per server, so they show up instantly."""
        ok = 0
        for g in (guilds or self.guilds):
            try:
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                ok += 1
            except discord.HTTPException as e:
                print(f"[slash] sync failed in {g.name}: {e}")
        return ok

    async def on_ready(self):
        if not self._synced:
            self._synced = True
            n = await self.sync_slash()
            print(f"[slash] synced to {n} server(s)")

    async def on_guild_join(self, guild):
        await self.sync_slash([guild])

    async def on_tree_error(self, interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            text = "🔒 Only the owner and trusted users can use this command."
        else:
            print(f"[slash] {type(error).__name__}: {error}")
            text = "❌ Something went wrong."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=emb(text, RED), ephemeral=True)
            else:
                await interaction.response.send_message(embed=emb(text, RED), ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_command_error(self, ctx, error):
        # commands inside cogs are handled by their cog (ui.BaseCog); this is the fallback
        if ctx.command is None or ctx.command.cog is None:
            await handle_error(ctx, error)

    # ----- startup -----
    async def setup_hook(self):
        utils.load_data()
        self.tree.error(self.on_tree_error)
        for f in sorted(os.listdir("modules")):
            if f.endswith(".py") and not f.startswith("_"):
                # Music is served by the parallel Node.js player in music-node.
                if f in {"music.py", "lavaplayer.py"}:
                    print(f"[module] skipped {f[:-3]} (Node music runtime owns playback)")
                    continue
                try:
                    await self.load_extension(f"modules.{f[:-3]}")
                    print(f"[module] loaded {f[:-3]}")
                except Exception as e:
                    print(f"[module] FAILED {f[:-3]}: {type(e).__name__}: {e}")
        self._console_task = asyncio.create_task(self.console_loop())

    async def console_loop(self):
        await self.wait_until_ready()
        if self.guilds:
            self.current_guild = self.guilds[0]
        print(f"\nLogged in as {self.user}")
        if self.current_guild:
            print(f"Selected server: {self.current_guild.name}")

        if not sys.stdin or not sys.stdin.isatty():
            print("[console] No terminal attached - running headless. "
                  "Control the bot from Discord (slash commands like /drag, /play, /ai ...).")
            return
        print("Type 'help' for commands.\n")

        loop = asyncio.get_running_loop()
        while not self.is_closed():
            try:
                line = await loop.run_in_executor(None, input, "> ")
            except EOFError:
                print("[console] Console closed - the bot keeps running. Use the Discord commands.")
                return
            except KeyboardInterrupt:
                await self.close()
                return
            parts = line.strip().split()
            if not parts:
                continue
            entry = self.console.get(parts[0].lower())
            if entry is None:
                print("Unknown command. Type 'help'.")
                continue
            try:
                await entry["func"](self, parts[1:])
            except Exception as e:
                print(f"[!] {e or type(e).__name__}")


if __name__ == "__main__":
    if config.TOKEN.startswith("PUT_"):
        raise SystemExit("Open config.py and paste your bot token first (or set DISCORD_TOKEN).")
    if config.OWNER_ID == 0:
        raise SystemExit("Set OWNER_ID in config.py (your Discord user ID).")
    DragBot().run(config.TOKEN, log_handler=None)
