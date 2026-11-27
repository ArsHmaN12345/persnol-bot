"""Run the local Java Lavaplayer engine and expose a small loopback IPC client."""
import asyncio
import base64
import json
import os
import socket
import threading

import discord
from discord.ext import commands

import config
from ui import BaseCog, GREEN, RED
from utils import BotError

GROUP = "lavaplayer"


class LavaPlayerError(BotError):
    pass


class Engine:
    def __init__(self):
        self.host = "127.0.0.1"
        self.port = config.LAVAPLAYER_ENGINE_PORT
        self.process = None
        self.last_error = None
        self.ready = False
        self._socket = None
        self._reader = None
        self._writer = None
        self._lock = threading.RLock()

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        jar = config.LAVAPLAYER_ENGINE_JAR
        try:
            self.process = await asyncio.create_subprocess_exec(
                "java", "-jar", jar,
                stdout=None,
                stderr=None,
            )
        except FileNotFoundError:
            self.last_error = "Java 17+ is missing. Install Java or run the Docker image."
            print(f"[lavaplayer] {self.last_error}")
            return
        except OSError as error:
            self.last_error = f"Could not start Java engine: {type(error).__name__}: {error}"
            print(f"[lavaplayer] {self.last_error}")
            return

        for _ in range(60):
            if self.process.returncode is not None:
                self.last_error = f"Java engine exited with code {self.process.returncode}."
                break
            try:
                response = await self.request("PING")
                if response.startswith("OK\t"):
                    self.ready = True
                    self.last_error = None
                    print("[lavaplayer] Java engine connected (direct Lavaplayer)")
                    return
            except LavaPlayerError as error:
                self.last_error = str(error)
            await asyncio.sleep(0.5)
        print(f"[lavaplayer] engine unavailable: {self.last_error or 'startup timed out'}")

    def _connect_locked(self):
        if self._socket is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), timeout=3)
            sock.settimeout(15)
            self._socket = sock
            self._reader = sock.makefile("r", encoding="utf-8", newline="\n")
            self._writer = sock.makefile("w", encoding="utf-8", newline="\n")
        except OSError as error:
            self._drop_locked()
            raise LavaPlayerError(f"Can't reach the local Java music engine ({type(error).__name__}).") from error

    def _drop_locked(self):
        for stream in (self._reader, self._writer):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._reader = self._writer = self._socket = None
        self.ready = False

    def request_sync(self, line):
        with self._lock:
            self._connect_locked()
            try:
                self._writer.write(line + "\n")
                self._writer.flush()
                response = self._reader.readline()
                if not response:
                    raise OSError("Java engine closed the IPC connection")
            except OSError as error:
                self._drop_locked()
                self.last_error = str(error)
                raise LavaPlayerError("The Java music engine connection was interrupted.") from error
            response = response.rstrip("\r\n")
            if response.startswith("ERR\t"):
                try:
                    message = base64.b64decode(response[4:]).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    message = "Lavaplayer couldn't load that request."
                raise LavaPlayerError(message)
            return response

    async def request(self, line):
        return await asyncio.to_thread(self.request_sync, line)

    @staticmethod
    def _b64(value):
        return base64.b64encode(str(value).encode("utf-8")).decode("ascii")

    async def _tracks(self, operation, query, prefix=""):
        response = await self.request(f"{operation}\t{self._b64(query)}\t{self._b64(prefix)}")
        if not response.startswith("TRACKS\t"):
            raise LavaPlayerError("The Java engine returned an invalid track list.")
        try:
            return json.loads(base64.b64decode(response[7:]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LavaPlayerError("The Java engine returned unreadable track data.") from error

    async def resolve(self, query):
        return await self._tracks("RESOLVE", query)

    async def search(self, query, prefixes=None):
        choices = prefixes or config.LAVAPLAYER_SEARCH
        if isinstance(choices, str):
            choices = [item.strip() for item in choices.split(",") if item.strip()]
        errors = []
        for prefix in choices:
            try:
                tracks = await self._tracks("SEARCH", query, prefix)
                if tracks:
                    return tracks
            except LavaPlayerError as error:
                errors.append(error)
        if errors and len(errors) == len(choices):
            raise errors[-1]
        return []

    async def play(self, guild_id, track_id, volume):
        response = await self.request(f"PLAY\t{guild_id}\t{track_id}\t{int(volume)}")
        if response != "OK":
            raise LavaPlayerError("The Java engine couldn't start that track.")

    async def stop(self, guild_id):
        await self.request(f"STOP\t{guild_id}")

    async def pause(self, guild_id, paused):
        await self.request(f"PAUSE\t{guild_id}\t{str(bool(paused)).lower()}")

    async def volume(self, guild_id, value):
        await self.request(f"VOLUME\t{guild_id}\t{int(value)}")

    async def destroy_player(self, guild_id):
        await self.request(f"DESTROY\t{guild_id}")

    def next_frame(self, guild_id):
        response = self.request_sync(f"FRAME\t{guild_id}")
        if response == "IDLE":
            return b""
        if response == "WAIT":
            return b"\xf8\xff\xfe"
        if response.startswith("FRAME\t"):
            try:
                return base64.b64decode(response[6:])
            except ValueError as error:
                raise LavaPlayerError("The Java engine returned an invalid audio frame.") from error
        raise LavaPlayerError("The Java engine returned an invalid audio response.")

    async def close(self):
        if self.process and self.process.returncode is None:
            try:
                await self.request("SHUTDOWN")
            except LavaPlayerError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.terminate()
                await self.process.wait()
        with self._lock:
            self._drop_locked()
        self.process = None


ENGINE = Engine()
NODE = ENGINE  # Music's existing player path uses NODE for resolution and readiness.


class LavaPlayerAudioSource(discord.AudioSource):
    """Pull 20 ms Opus frames from Java; discord.py keeps the Discord voice connection."""
    def __init__(self, engine, guild_id):
        self.engine = engine
        self.guild_id = guild_id
        self.closed = False
        self.opus_frames = 0
        self.silence_frames = 0
        self.end_frames = 0
        self.read_errors = 0

    def read(self):
        if self.closed:
            return b""
        try:
            frame = self.engine.next_frame(self.guild_id)
            if frame == b"":
                self.end_frames += 1
            elif frame == b"\xf8\xff\xfe":
                self.silence_frames += 1
            else:
                self.opus_frames += 1
            return frame
        except LavaPlayerError as error:
            self.read_errors += 1
            if self.read_errors == 1 or self.read_errors % 50 == 0:
                print(f"[lavaplayer] frame read failed ({self.read_errors}): {error}")
            return b""

    def stats(self):
        return (f"Opus frames: {self.opus_frames}; startup-silence frames: {self.silence_frames}; "
                f"end-of-stream frames: {self.end_frames}; IPC read errors: {self.read_errors}")

    def is_opus(self):
        return True

    def cleanup(self):
        self.closed = True


async def run_diag(bot):
    lines, bad = [], False

    def add(icon, text):
        nonlocal bad
        bad = bad or icon == "❌"
        lines.append(f"{icon} {text}")

    add("ℹ️", "Music backend: direct Lavaplayer in a local Java process; Python handles Discord voice.")
    if ENGINE.ready:
        try:
            version = await ENGINE.request("PING")
            add("✅", f"{version[3:]}; loopback IPC at {ENGINE.host}:{ENGINE.port}.")
            for prefix in config.LAVAPLAYER_SEARCH:
                try:
                    found = await asyncio.wait_for(ENGINE.search("never gonna give you up", [prefix]), 30)
                    add("✅" if found else "⚠️", f"`{prefix}:` returned {len(found)} result(s).")
                except Exception as error:
                    add("❌", f"`{prefix}:` search failed: {' '.join(str(error).split())[:150]}")
        except Exception as error:
            add("❌", f"Java engine check failed: {' '.join(str(error).split())[:150]}")
    else:
        add("❌", f"Java engine unavailable: {ENGINE.last_error or 'still starting'}")
    add("✅" if os.path.isdir(config.AUDIO_LOCAL_DIR) else "⚠️",
        f"Local music folder: `{config.AUDIO_LOCAL_DIR}`")
    import shutil
    ffmpeg = shutil.which("ffmpeg")
    add("✅" if ffmpeg else "⚠️", "ffmpeg found" if ffmpeg else "ffmpeg missing; AI voice-message commands need it")
    return lines, [], not bad


class LavaPlayerCog(BaseCog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await ENGINE.start()

    async def cog_unload(self):
        await ENGINE.close()

    async def cog_check(self, ctx):
        from utils import is_owner
        return ctx.guild is not None and is_owner(ctx.author.id)

    @commands.hybrid_command(name="musicdiag", description="Check the Lavaplayer music engine (owner only)")
    async def musicdiag(self, ctx):
        async with ctx.typing():
            lines, _, ok = await run_diag(ctx.bot)
        voice = ctx.guild.voice_client
        lines.append(
            f"{'✅' if voice and voice.is_connected() else '⚠️'} Discord voice: "
            f"{'connected' if voice and voice.is_connected() else 'not connected'}; "
            f"audio player: {'playing' if voice and voice.is_playing() else 'not playing'}."
        )
        cog = ctx.bot.get_cog("Music")
        player = cog.players.get(ctx.guild.id) if cog else None
        source = player.source if player else None
        if source:
            lines.append(f"🎧 Audio source: {source.stats()}.")
        elif voice and voice.is_playing():
            lines.append("⚠️ Discord reports playing audio, but no Lavaplayer source is tracked.")
        await ctx.send(embed=discord.Embed(
            title="🩺 Music check",
            description="\n".join(lines)[:4000],
            color=GREEN if ok else RED,
        ))


async def c_musicdiag(bot, args):
    lines, _, _ = await run_diag(bot)
    print("\n" + "\n".join(line.replace("**", "").replace("`", "") for line in lines))


async def setup(bot):
    await bot.add_cog(LavaPlayerCog(bot))
    bot.add_console("musicdiag", c_musicdiag, "musicdiag", "check the Lavaplayer music engine", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)

