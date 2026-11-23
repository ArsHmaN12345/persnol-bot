"""Lavalink music backend: a small, dependency-free client for Lavalink v4 (4.2+ is required because Discord now
uses end-to-end encrypted "DAVE" voice, which Lavalink handles for us).

 * Node            - REST + WebSocket connection to the Lavalink server (auto-reconnect).
 * LavalinkVoiceClient - a discord.VoiceProtocol: hands Discord's voice credentials to Lavalink, then plays,
                      pauses, stops and sets the volume through it. It mimics the parts of discord.VoiceClient
                      that the music module uses, so the player, panel, queue, 24/7 mode etc. work unchanged.
 * /musicdiag      - checks everything that can stop music from working and says what to fix.
"""
import asyncio
import json
import shutil

import aiohttp
import discord
from discord.ext import commands

import config
from utils import BotError
from ui import BaseCog, GREEN, RED

GROUP = "lavalink"
NODE = None                     # the active Node (music.py reads this)


class LavalinkError(BotError):
    pass


def search_prefixes():
    return [p.strip() for p in config.LAVALINK_SEARCH.split(",") if p.strip()] or ["ytsearch"]


def parse_track(t):
    info = t.get("info", {})
    stream = bool(info.get("isStream"))
    return {"encoded": t["encoded"], "title": info.get("title") or "Unknown", "author": info.get("author"),
            "length": None if stream else (info.get("length") or 0) / 1000, "uri": info.get("uri"),
            "thumb": info.get("artworkUrl"), "stream": stream}


# ============================================================ node
class Node:
    def __init__(self, bot, host, port, password, secure=False):
        self.bot = bot
        self.host, self.port, self.password = host, port, password
        http, ws = ("https", "wss") if secure else ("http", "ws")
        self.base = f"{http}://{host}:{port}"
        self.ws_url = f"{ws}://{host}:{port}/v4/websocket"
        self.session_id = None
        self.ready_event = asyncio.Event()
        self.listeners = {}              # guild_id -> LavalinkVoiceClient
        self.stats = {}
        self.last_error = None
        self._http = None
        self._ws = None
        self._task = None

    @property
    def ready(self):
        return bool(self.session_id) and self._ws is not None and not self._ws.closed

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def close(self):
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._http is not None and not self._http.closed:
            await self._http.close()

    def _session(self):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    def _headers(self):
        return {"Authorization": self.password}

    # ---------- websocket ----------
    async def _run(self):
        await self.bot.wait_until_ready()
        delay, last_logged = 2, None
        while True:
            try:
                headers = {**self._headers(), "User-Id": str(self.bot.user.id), "Client-Name": "dragbot/1.0"}
                async with self._session().ws_connect(self.ws_url, headers=headers, heartbeat=30) as ws:
                    self._ws, delay, self.last_error = ws, 2, None
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as e:
                self.last_error = "wrong password" if e.status in (401, 403) else f"HTTP {e.status}"
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            finally:
                await self._lost()
            if self.last_error != last_logged:
                print(f"[lavalink] not connected to {self.host}:{self.port} - {self.last_error}")
                last_logged = self.last_error
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _lost(self):
        was_ready = self.session_id is not None
        self.session_id, self._ws = None, None
        self.ready_event.clear()
        if was_ready:
            print("[lavalink] connection lost - reconnecting...")
            for vc in list(self.listeners.values()):
                await vc.on_node_lost()

    async def _handle(self, msg):
        op = msg.get("op")
        if op == "ready":
            self.session_id = msg["sessionId"]
            self.ready_event.set()
            print(f"[lavalink] connected to {self.host}:{self.port}")
            for vc in list(self.listeners.values()):
                asyncio.create_task(vc.on_node_ready())
        elif op == "stats":
            self.stats = msg
        elif op == "playerUpdate":
            vc = self.listeners.get(int(msg["guildId"]))
            if vc:
                vc.position, vc.ping = msg["state"].get("position", 0), msg["state"].get("ping", -1)
        elif op == "event":
            vc = self.listeners.get(int(msg["guildId"]))
            if vc:
                await vc.on_lavalink_event(msg)

    # ---------- REST ----------
    async def wait_ready(self, timeout=10):
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout)
        except asyncio.TimeoutError:
            raise LavalinkError("The Lavalink server isn't connected right now.")

    async def request(self, method, path, params=None, body=None, timeout=20):
        url = f"{self.base}/v4{path}"
        try:
            async with self._session().request(method, url, params=params, json=body, headers=self._headers(),
                                               timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status in (401, 403):
                    raise LavalinkError("Lavalink rejected the password (check LAVALINK_PASSWORD).")
                if r.status == 204:
                    return None
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = None
                if r.status >= 400:
                    msg = data.get("message") if isinstance(data, dict) else None
                    raise LavalinkError(f"Lavalink error: {msg or r.status}")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise LavalinkError(f"Couldn't reach the Lavalink server ({type(e).__name__}).")

    async def info(self):
        return await self.request("GET", "/info")

    async def resolve(self, identifier):
        """A URL or 'ytsearch:text' -> list of parsed tracks ([] if nothing was found)."""
        data = await self.request("GET", "/loadtracks", params={"identifier": identifier})
        kind, d = data["loadType"], data.get("data")
        if kind == "track":
            return [parse_track(d)]
        if kind == "playlist":
            return [parse_track(t) for t in d["tracks"]]
        if kind == "search":
            return [parse_track(t) for t in d]
        if kind == "error":
            raise LavalinkError(str((d or {}).get("message") or "Lavalink couldn't load that."))
        return []

    async def search(self, text, prefixes=None):
        last = None
        for p in (prefixes or search_prefixes()):
            try:
                found = await self.resolve(f"{p}:{text}")
            except LavalinkError as e:
                last = e
                continue
            if found:
                return found
        if last:
            raise last
        return []

    async def update_player(self, guild_id, body, no_replace=False):
        await self.wait_ready()
        return await self.request("PATCH", f"/sessions/{self.session_id}/players/{guild_id}",
                                  params={"noReplace": str(no_replace).lower()}, body=body)

    async def destroy_player(self, guild_id):
        if self.session_id:
            try:
                await self.request("DELETE", f"/sessions/{self.session_id}/players/{guild_id}")
            except LavalinkError:
                pass


async def resolve_track(track, node):
    """Make sure a music.Track has a Lavalink 'encoded' id (searching for it if needed)."""
    if track.encoded:
        return
    q = track.query or track.title
    if q.startswith("ytsearch1:"):
        q = q[len("ytsearch1:"):]
    found = await node.resolve(q) if q.startswith(("http://", "https://")) else await node.search(q)
    if not found:
        raise LavalinkError("No results.")
    t = found[0]
    track.encoded = t["encoded"]
    track.duration = track.duration or t["length"]
    track.page = track.page or t["uri"]
    track.thumb = track.thumb or t["thumb"]


# ============================================================ voice client
class LavalinkVoiceClient(discord.VoiceProtocol):
    """Discord voice-state adapter; Lavalink handles encrypted audio playback."""
    source = None

    def __init__(self, client, channel):
        super().__init__(client, channel)
        self.guild = channel.guild
        self.node = NODE
        self._voice = {}
        self._connected = False
        self._connect_evt = asyncio.Event()
        self._playing = False
        self._paused = False
        self._after = None
        self._encoded = None
        self._stop_requested = False
        self._volume = 100
        self._destroyed = False
        self.position, self.ping, self.last_exception = 0, -1, None

    # ----- discord.VoiceProtocol -----
    async def connect(self, *, timeout, reconnect, self_deaf=False, self_mute=False):
        if self.node is None or not self.node.ready:
            self.cleanup()
            raise LavalinkError("The Lavalink server isn't connected right now.")
        self.node.listeners[self.guild.id] = self
        await self.guild.change_voice_state(channel=self.channel, self_mute=self_mute, self_deaf=self_deaf)
        try:
            await asyncio.wait_for(self._connect_evt.wait(), timeout)
        except asyncio.TimeoutError:
            await self.disconnect(force=True)
            raise

    async def disconnect(self, *, force=False):
        try:
            await self.guild.change_voice_state(channel=None)
        except discord.HTTPException as error:
            print(f"[lavalink] couldn't leave the Discord voice channel: {error}")
        await self._teardown()

    async def _teardown(self):
        """Idempotent: safe to call from disconnect() and again when Discord confirms we left."""
        if self._destroyed:
            return
        self._destroyed = True
        self._connected = False
        self._connect_evt.clear()
        if self.node:
            self.node.listeners.pop(self.guild.id, None)
            await self.node.destroy_player(self.guild.id)
        self._end_track("disconnected")
        self.cleanup()

    async def on_voice_state_update(self, data):
        cid = data.get("channel_id")
        if cid is None:                                   # we got disconnected (kicked / channel deleted / left)
            await self._teardown()
            return
        self._voice["sessionId"] = data["session_id"]
        self._voice["channelId"] = str(cid)
        ch = self.guild.get_channel(int(cid))
        if ch is not None:
            self.channel = ch
        await self._send_voice()

    async def on_voice_server_update(self, data):
        if not data.get("endpoint"):                      # Discord is reallocating the voice server
            return
        self._voice["token"], self._voice["endpoint"] = data["token"], data["endpoint"]
        await self._send_voice()

    async def _send_voice(self):
        if not all(k in self._voice for k in ("token", "endpoint", "sessionId", "channelId")):
            return
        try:
            await self.node.update_player(self.guild.id, {"voice": dict(self._voice)})
        except LavalinkError as e:
            print(f"[lavalink] couldn't send voice state: {e}")
            return
        self._connected = True
        self._connect_evt.set()

    # ----- what the music module expects from a voice client -----
    def is_connected(self):
        return self._connected and self.node is not None and self.node.ready

    def is_playing(self):
        return self._playing and not self._paused

    def is_paused(self):
        return self._playing and self._paused

    async def move_to(self, channel, *, timeout=30.0):
        await self.guild.change_voice_state(channel=channel)
        self.channel = channel

    async def play_track(self, track, volume_percent, after):
        self._after, self._playing, self._paused, self._volume = after, True, False, int(volume_percent)
        self._encoded = track.encoded
        self.last_exception = None
        try:
            await self.node.update_player(self.guild.id, {"track": {"encoded": track.encoded}, "volume": self._volume,
                                                          "paused": False, "position": 0})
        except LavalinkError:
            self._playing = self._paused = False
            self._encoded = None
            after_error = self._after
            self._after = None
            if after_error is not None:
                after_error(LavalinkError("Lavalink couldn't start the track."))
            raise

    def _fire(self, coro):
        async def guard():
            try:
                await coro
            except LavalinkError as e:
                print(f"[lavalink] {e}")
        asyncio.create_task(guard())

    def stop(self):
        if self._stop_requested:
            return
        self._stop_requested = True

        async def stop_track():
            try:
                await self.node.update_player(self.guild.id, {"track": {"encoded": None}})
            except LavalinkError as error:
                print(f"[lavalink] couldn't stop track: {error}")
            finally:
                self._stop_requested = False
                self._end_track("stopped")

        asyncio.create_task(stop_track())

    def pause(self):
        self._paused = True
        self._fire(self.node.update_player(self.guild.id, {"paused": True}))

    def resume(self):
        self._paused = False
        self._fire(self.node.update_player(self.guild.id, {"paused": False}))

    def set_volume_percent(self, percent):
        self._volume = int(percent)
        if self._playing:
            self._fire(self.node.update_player(self.guild.id, {"volume": self._volume}))

    # ----- events from Lavalink -----
    def _end_track(self, reason):
        was = self._playing
        self._playing = self._paused = False
        self._encoded = None
        after, self._after = self._after, None
        if was and after is not None:
            after(None if reason in ("finished", "stopped", "cleanup", "disconnected") else RuntimeError(reason))

    async def on_lavalink_event(self, msg):
        event_track = msg.get("track") or {}
        event_encoded = event_track.get("encoded")
        if event_encoded and self._encoded and event_encoded != self._encoded:
            return

        t = msg.get("type")
        if t == "TrackEndEvent":
            if msg.get("reason") != "replaced" and not self._stop_requested:
                self._end_track(msg.get("reason", "finished"))
        elif t == "TrackExceptionEvent":
            self.last_exception = (msg.get("exception") or {}).get("message", "unknown error")
            print(f"[lavalink] track failed: {self.last_exception}")
            self._end_track("loadFailed")
        elif t == "TrackStuckEvent":
            print("[lavalink] track got stuck - skipping")
            self.stop()
        elif t == "WebSocketClosedEvent":
            print(f"[lavalink] voice websocket closed (code {msg.get('code')}): {msg.get('reason')}")

    async def on_node_ready(self):                         # Lavalink restarted: it forgot our player
        await self._send_voice()

    async def on_node_lost(self):
        self._end_track("lavalink connection lost")


# ============================================================ diagnostics
def _one_line(e, n=170):
    return " ".join(str(e).split())[:n]


async def run_diag(bot):
    """Returns (lines, all_good). ✅ works, ⚠️ optional/unknown, ❌ broken."""
    lines, bad = [], False

    def add(icon, text):
        nonlocal bad
        bad = bad or icon == "❌"
        lines.append(f"{icon} {text}")

    node = NODE
    mode = config.MUSIC_BACKEND
    add("ℹ️", f"Music backend: Lavalink (required; setting is `{mode}`).")
    if mode == "ffmpeg":
        add("❌", "The FFmpeg music backend is no longer supported. Set `MUSIC_BACKEND=lavalink`.")
    lines.append("**Lavalink**")
    if node is None:
        add("❌", "Not configured (set `LAVALINK_HOST`, `LAVALINK_PORT`, and `LAVALINK_PASSWORD`).")
    elif not node.ready:
        add("❌", f"Not connected to `{node.host}:{node.port}` - {node.last_error or 'connecting...'}")
    else:
        try:
            info = await node.info()
            add("✅", f"Connected: Lavalink **{info['version']['semver']}** | sources: {', '.join(info['sourceManagers'])}"
                      + (f" | plugins: {', '.join(p['name'] for p in info['plugins'])}" if info["plugins"] else ""))
            major_minor = tuple(int(x) for x in info["version"]["semver"].split("-")[0].split(".")[:2])
            if major_minor < (4, 2):
                add("❌", "This Lavalink is older than 4.2 - Discord's encrypted voice needs **4.2 or newer**.")
        except Exception as e:
            add("❌", f"Connected but /info failed: {_one_line(e)}")
        for prefix in search_prefixes():
            try:
                found = await asyncio.wait_for(node.resolve(f"{prefix}:never gonna give you up"), 30)
                add("✅" if found else "⚠️", f"`{prefix}:` search -> {len(found)} result(s)"
                    + (f" (e.g. {_one_line(found[0]['title'], 50)})" if found else " - that source is probably disabled or blocked"))
            except Exception as e:
                add("❌", f"`{prefix}:` search failed - {_one_line(e)}")
    lines.append("**FFmpeg (AI voice messages only)**")
    ff = shutil.which("ffmpeg")
    add("✅" if ff else "⚠️", "ffmpeg " + ("found" if ff else "not installed; AI voice-message commands need it"))

    text = "\n".join(lines)
    hints = []
    if "Sign in to confirm" in text or "not a bot" in text.lower():
        hints.append("YouTube is blocking this server's IP. Use the `scsearch` (SoundCloud) source, add YouTube cookies/OAuth, or host the bot at home.")
    return lines, hints, not bad


class LavalinkCog(BaseCog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        if NODE is not None:
            NODE.start()

    async def cog_unload(self):
        if NODE is not None:
            await NODE.close()

    async def cog_check(self, ctx):
        from utils import is_owner
        return ctx.guild is not None and is_owner(ctx.author.id)

    @commands.hybrid_command(name="musicdiag", description="Find out why music might not work (owner only)")
    async def musicdiag(self, ctx):
        async with ctx.typing():
            lines, hints, ok = await run_diag(self.bot)
        e = discord.Embed(title="🩺 Music check", description="\n".join(lines)[:4000], color=GREEN if ok else RED)
        if hints:
            e.add_field(name="💡 Likely fix", value="\n".join(hints)[:1000], inline=False)
        await ctx.send(embed=e)


async def c_musicdiag(bot, args):
    print("checking...")
    lines, hints, ok = await run_diag(bot)
    print("\n" + "\n".join(l.replace("**", "").replace("`", "") for l in lines))
    for h in hints:
        print("\n💡 " + h)
    print("\nAll good." if ok else "\nSomething needs fixing (see the ❌ lines).")


async def setup(bot):
    global NODE
    NODE = None
    if config.LAVALINK_HOST:
        NODE = Node(bot, config.LAVALINK_HOST, config.LAVALINK_PORT, config.LAVALINK_PASSWORD, config.LAVALINK_SECURE)
    await bot.add_cog(LavalinkCog(bot))
    bot.add_console("musicdiag", c_musicdiag, "musicdiag", "check why music might not work", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
