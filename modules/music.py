"""Music module for online sources, encoded to Discord-ready Opus by FFmpeg."""
import asyncio
import base64
import collections
import json
import random
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request

import discord
from discord.ext import commands
import yt_dlp

import config
from discord import app_commands
from typing import Literal, Optional
from utils import data, save_data, need_guild, find_vc, has_access, BotError
from ui import BaseCog

GROUP = "music"


class MusicError(BotError):
    pass


YTDL_OPTS = {
    "format": "bestaudio[protocol=https][acodec=opus]/bestaudio[protocol=https]/bestaudio[acodec=opus]/bestaudio/best",   # prefer direct HTTPS audio over HLS
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 20,
    "retries": 2,
    "fragment_retries": 2,
    "extractor_retries": 2,
    "playlistend": config.MAX_QUEUE,
}
STAY_GRACE = 45        # seconds a connection may stay down before the 24/7 watchdog steps in
STAY_INTERVAL = 15     # how often the watchdog checks
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def fmt(sec):
    if not sec:
        return "?"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ======================= embeds =======================
BLURPLE, GREEN, YELLOW, RED = 0x5865F2, 0x57F287, 0xFEE75C, 0xED4245


def safe(text):
    return discord.utils.escape_markdown(text).replace("[", "(").replace("]", ")")


def bar(volume, width=12):
    filled = round(min(max(volume, 0), 2.0) / 2.0 * width)
    return "▰" * filled + "▱" * (width - filled)


def info_embed(text, color=BLURPLE):
    return discord.Embed(description=text, color=color)


def volume_embed(p):
    pct = int(p.volume * 100)
    icon = "🔇" if pct == 0 else "🔉" if pct < 50 else "🔊"
    e = discord.Embed(title=f"{icon} Volume", description=f"`{bar(p.volume)}`  **{pct}%**", color=BLURPLE)
    e.set_footer(text="0% – 200%  •  100% is the original loudness")
    return e


def queue_embed(p):
    return discord.Embed(title="📜 Queue", description=p.queue_text(), color=BLURPLE)


def np_embed(p, track):
    title = safe(track.title)
    desc = f"**[{title}]({track.page})**" if track.page else f"**{title}**"
    paused = bool(p.guild.voice_client and p.guild.voice_client.is_paused())
    e = discord.Embed(title="⏸️ Paused" if paused else "🎶 Now Playing", description=desc,
                      color=YELLOW if paused else BLURPLE)
    if track.thumb:
        e.set_thumbnail(url=track.thumb)
    e.add_field(name="Duration", value=fmt(track.duration), inline=True)
    e.add_field(name="Requested by", value=safe(str(track.requester)), inline=True)
    e.add_field(name="In queue", value=str(len(p.queue)), inline=True)
    e.add_field(name="Volume", value=f"`{bar(p.volume)}`  **{int(p.volume * 100)}%**", inline=False)
    loop_icon = {"off": "off", "track": "🔂 track", "queue": "🔁 queue"}[p.loop_mode]
    e.set_footer(text=f"Loop: {loop_icon}  •  Opus {p.kbps} kbps")
    return e


# ======================= sources =======================
def _host(query):
    try:
        return (urllib.parse.urlsplit(query).hostname or "").lower()
    except ValueError:
        return ""


def _is_youtube_url(query):
    host = _host(query)
    return (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
            or host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com"))


def _is_soundcloud_url(query):
    host = _host(query)
    return host == "soundcloud.com" or host.endswith(".soundcloud.com")


def extract(query, flat=False):
    """Extract track metadata and streams without using YouTube or browser cookies."""
    base = dict(YTDL_OPTS)
    # The SoundCloud extractor chooses its own supported browser target when
    # curl-cffi is installed; raw strings are invalid in the Python API here.
    if flat:
        base.update(extract_flat="in_playlist", noplaylist=False)

    try:
        with yt_dlp.YoutubeDL(base) as ydl:
            return ydl.extract_info(query, download=False)
    except (OSError, yt_dlp.utils.DownloadError) as e:
        msg = str(e).replace("ERROR: ", "").splitlines()[0].strip() or type(e).__name__
        low = msg.lower()
        soundcloud = query.lower().startswith("scsearch") or _is_soundcloud_url(query)
        if "403" in low or "forbidden" in low:
            if soundcloud:
                raise MusicError(
                    "SoundCloud refused Railway's request (HTTP 403). Try a public SoundCloud track link "
                    "or another search phrase. YouTube is disabled."
                ) from e
            raise MusicError(f"The audio source refused Railway's request (HTTP 403): {msg[:150]}") from e
        if "unsupported url" in low or "no suitable extractor" in low:
            raise MusicError(
                "I can't play that link. Use a Spotify link, a public SoundCloud link, or a direct audio URL."
            ) from e
        raise MusicError(msg[:220]) from e


# ---- Spotify metadata (track audio is matched on SoundCloud) ----
_sp = {"token": None, "exp": 0}
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)")


def _spotify_token():
    if _sp["token"] and time.time() < _sp["exp"] - 30:
        return _sp["token"]
    if not (config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET):
        raise MusicError("Spotify links need SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in config.py.")
    auth = base64.b64encode(f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.load(r)
    except urllib.error.HTTPError:
        raise MusicError("Spotify rejected your client ID/secret. Check them in config.py.")
    _sp["token"], _sp["exp"] = j["access_token"], time.time() + j.get("expires_in", 3600)
    return _sp["token"]


def _spotify_get(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_spotify_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise MusicError("Spotify refused this request (403). Dev-mode apps need the owner on Premium, "
                             "and playlists can be restricted. Try a track/album link instead.")
        raise MusicError(f"Spotify error {e.code}.")


def spotify_tracks(kind, ident):
    out = []
    if kind == "track":
        t = _spotify_get(f"https://api.spotify.com/v1/tracks/{ident}")
        out.append((t["name"], t["artists"][0]["name"]))
    elif kind == "album":
        alb = _spotify_get(f"https://api.spotify.com/v1/albums/{ident}")
        page = alb["tracks"]
        while page:
            for t in page["items"]:
                out.append((t["name"], t["artists"][0]["name"]))
            page = _spotify_get(page["next"]) if page.get("next") and len(out) < config.MAX_QUEUE else None
    else:  # playlist
        page = _spotify_get(f"https://api.spotify.com/v1/playlists/{ident}/items?limit=100")
        while page:
            for it in page["items"]:
                t = it.get("item") or it.get("track")
                if t and t.get("type", "track") == "track" and t.get("artists"):
                    out.append((t["name"], t["artists"][0]["name"]))
            page = _spotify_get(page["next"]) if page.get("next") and len(out) < config.MAX_QUEUE else None
    if not out:
        raise MusicError("Couldn't read any tracks from that Spotify link.")
    return out[:config.MAX_QUEUE]


# ---- Apple Music metadata (audio matched on SoundCloud) ----
def apple_tracks(url):
    m = re.search(r"music\.apple\.com/([a-z]{2})/(album|song|playlist)/[^/?]+/([\w.\-]+)", url)
    if not m:
        raise MusicError("Unsupported Apple Music link. Use a song or album link.")
    cc, kind, ident = m.groups()
    if kind == "playlist":
        raise MusicError("Apple Music playlists can't be read without an Apple developer account. "
                         "Use a song or album link instead.")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    extra = ""
    if "i" in q:
        ident = q["i"][0]
    elif kind == "album":
        extra = "&entity=song"
    api = f"https://itunes.apple.com/lookup?id={ident}&country={cc}{extra}"
    with urllib.request.urlopen(api, timeout=15) as r:
        res = json.load(r).get("results", [])
    out = [(x["trackName"], x["artistName"]) for x in res if x.get("kind") == "song"]
    if not out:
        raise MusicError("Couldn't read that Apple Music link.")
    return out[:config.MAX_QUEUE]


# ======================= tracks =======================
class Track:
    def __init__(self, title, query, requester, stream_url=None, duration=None, page=None, thumb=None, http_headers=None):
        self.title = title
        self.thumb = thumb
        self.query = query
        self.requester = requester
        self.stream_url = stream_url
        self.http_headers = http_headers or {}
        self.duration = duration
        self.page = page
        self.resolved_at = time.time() if stream_url else 0

    @classmethod
    def from_info(cls, info, requester):
        page = info.get("webpage_url") or info.get("original_url")
        return cls(info.get("title") or "Unknown", page or info.get("url"), requester,
                   stream_url=info.get("url"), duration=info.get("duration"), page=page,
                   thumb=info.get("thumbnail"), http_headers=info.get("http_headers"))

    async def ensure_stream(self):
        if self.stream_url and time.time() - self.resolved_at < 1200:
            return
        info = await asyncio.get_running_loop().run_in_executor(None, extract, self.query)
        if info.get("entries"):
            info = next((e for e in info["entries"] if e), None)
            if info is None:
                raise MusicError("No results.")
        if not info.get("url"):
            raise MusicError("Could not get an audio stream.")
        self.stream_url = info["url"]
        self.http_headers = info.get("http_headers") or {}
        self.resolved_at = time.time()
        self.duration = self.duration or info.get("duration")
        self.page = self.page or info.get("webpage_url")
        self.thumb = self.thumb or info.get("thumbnail")


async def expand(query, requester):
    """Expand Spotify metadata and supported links into SoundCloud or direct streams."""
    loop = asyncio.get_running_loop()
    query = query.strip()
    if not query:
        raise MusicError("Enter a song name, Spotify link, SoundCloud link, or direct audio URL.")

    if "icloud.com" in query.lower():
        raise MusicError("iCloud library files are private/DRM-protected and can't be streamed by a bot.")

    spotify_uri = re.fullmatch(r"spotify:(track|album|playlist):([A-Za-z0-9]+)", query)
    m = SPOTIFY_RE.search(query) or spotify_uri
    if m:
        kind, ident = m.groups()
        pairs = await loop.run_in_executor(None, spotify_tracks, kind, ident)
        spotify_page = f"https://open.spotify.com/{kind}/{ident}"
        return [
            Track(f"{artist} - {title}", f"scsearch1:{artist} - {title} audio", requester, page=spotify_page)
            for title, artist in pairs
        ]

    if "music.apple.com" in query.lower():
        pairs = await loop.run_in_executor(None, apple_tracks, query)
        return [
            Track(f"{artist} - {title}", f"scsearch1:{artist} - {title} audio", requester, page=query)
            for title, artist in pairs
        ]

    if query.startswith(("http://", "https://")):
        if _is_youtube_url(query):
            raise MusicError("YouTube links are disabled. Use a Spotify link, SoundCloud link, or direct audio URL.")
        parsed = urllib.parse.urlsplit(query)
        is_playlist = "/sets/" in parsed.path.lower()
        info = await loop.run_in_executor(None, extract, query, is_playlist)
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if entries:
            tracks = [Track.from_info(entry, requester) for entry in entries[:config.MAX_QUEUE]]
            if not tracks:
                raise MusicError("That playlist is empty or private.")
            return tracks
        return [Track.from_info(info, requester)]

    info = await loop.run_in_executor(None, extract, f"scsearch1:{query}")
    entries = [entry for entry in (info.get("entries") or []) if entry]
    if not entries:
        raise MusicError("No SoundCloud results. Try a different song name or paste a public SoundCloud track link.")
    return [Track.from_info(entries[0], requester)]


# ======================= player =======================
class ProbedAudioSource(discord.AudioSource):
    """Report when FFmpeg produces the first non-empty audio frame."""

    def __init__(self, original, on_audio):
        self.original = original
        self._on_audio = on_audio
        self._reported_audio = False

    def read(self):
        packet = self.original.read()
        if packet and not self._reported_audio:
            self._reported_audio = True
            self._on_audio()
        return packet

    def is_opus(self):
        return self.original.is_opus()

    def cleanup(self):
        self.original.cleanup()


class GuildPlayer:
    def __init__(self, cog, guild):
        self.cog = cog
        self.guild = guild
        self.queue = collections.deque()
        self.current = None
        self.volume = config.DEFAULT_VOLUME
        self.loop_mode = "off"          # off | track | queue
        self.skipped = False
        self.text_channel = None
        self._status_off = False
        self.panel = None
        self.kbps = config.AUDIO_BITRATE
        self.restart_requested = False
        self.restart_paused = False
        self.has_tracks = asyncio.Event()
        self.next_event = asyncio.Event()
        self.task = asyncio.create_task(self.run())

    async def say(self, text):
        print(f"[music] {text}")
        if self.text_channel:
            try:
                await self.text_channel.send(text)
            except discord.HTTPException:
                pass

    async def set_status(self, text):
        """Set (or clear with None) the voice channel status shown under the channel name."""
        if not config.VC_STATUS or self._status_off:
            return
        vc = self.guild.voice_client
        if not vc or not vc.channel:
            return
        if text:
            text = text[:120]
        try:
            await vc.channel.edit(status=text)
        except discord.Forbidden:
            self._status_off = True
            print("[music] No permission to set the voice channel status. "
                  "Give the bot 'Set Voice Channel Status' (or Manage Channels).")
        except Exception:                 # a cosmetic status update must never kill the player
            pass

    async def announce(self, track):
        print(f"[music] Now playing: {track.title} [{fmt(track.duration)}] ({self.kbps}kbps Opus) - {track.requester}")
        await self.end_panel()
        await self.set_presence(track.title)
        if self.text_channel:
            try:
                self.panel = await self.text_channel.send(embed=np_embed(self, track), view=PlayerView(self))
            except discord.HTTPException:
                self.panel = None

    async def set_presence(self, title):
        if not config.SET_PRESENCE:
            return
        try:
            pres = self.cog.bot.get_cog("Presence")
            if pres is not None:                 # the presence module keeps the owner's chosen status intact
                await pres.music_update(self.guild.id, title)
                return
            activity = discord.Activity(type=discord.ActivityType.listening, name=title[:100]) if title else None
            await self.cog.bot.change_presence(activity=activity)
        except Exception:
            pass

    async def end_panel(self):
        if self.panel:
            try:
                await self.panel.edit(view=None)
            except discord.HTTPException:
                pass
            self.panel = None

    def toggle_pause(self):
        vc = self._vc()
        if vc.is_paused():
            vc.resume()
        elif vc.is_playing():
            vc.pause()
        else:
            raise MusicError("Nothing is playing.")

    def bump_volume(self, delta):
        self.set_volume(max(0, min(200, round(self.volume * 100) + delta)))

    def cycle_loop(self):
        modes = ["off", "track", "queue"]
        self.loop_mode = modes[(modes.index(self.loop_mode) + 1) % 3]

    def add(self, tracks):
        room = max(config.MAX_QUEUE - len(self.queue), 0)
        tracks = tracks[:room]
        self.queue.extend(tracks)
        if tracks:
            self.has_tracks.set()
        return len(tracks)

    async def run(self):
        loop = asyncio.get_running_loop()
        try:
            while True:
                if not self.queue:
                    await self.set_status(None)
                    await self.set_presence(None)
                while not self.queue:
                    self.has_tracks.clear()
                    try:
                        await asyncio.wait_for(self.has_tracks.wait(), config.IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        if str(self.guild.id) in data["stay"]:
                            continue                      # 24/7 mode: stay in the channel
                        await self.say("💤 Idle for a while, leaving the voice channel.")
                        await self.destroy()
                        return

                track = self.queue.popleft()
                vc = self.guild.voice_client
                if vc is None or not vc.is_connected():
                    self.queue.clear()
                    continue
                try:
                    await track.ensure_stream()
                except MusicError as e:
                    await self.say(f"❌ Skipping **{track.title}**: {e}")
                    continue

                self.current = track
                self.skipped = False
                self.next_event.clear()
                try:
                    header_blob = "\r\n".join(
                        f"{key}: {value}" for key, value in track.http_headers.items()
                        if key and value
                    )
                    before_options = FFMPEG_BEFORE
                    if header_blob:
                        header_blob += "\r\n"
                        before_options += f" -headers {shlex.quote(header_blob)}"
                    first_audio = asyncio.Event()
                    kbps = max(16, min(config.AUDIO_BITRATE, vc.channel.bitrate // 1000))
                    self.kbps = kbps
                    source = ProbedAudioSource(
                        discord.FFmpegOpusAudio(
                            track.stream_url,
                            bitrate=kbps,
                            # Leave codec unset: discord.py treats explicit "libopus" as stream copy.\n                            before_options=before_options,
                            options=f"-vn -af volume={self.volume}",
                        ),
                        on_audio=lambda: loop.call_soon_threadsafe(first_audio.set),
                    )
                except (discord.ClientException, OSError) as e:
                    await self.say(f"Playback could not start for {track.title}: {e}")
                    self.current = None
                    continue

                player_errors = []

                def after(err, ev=self.next_event):
                    if err:
                        player_errors.append(type(err).__name__)
                        print(f"[music] player error: {err}")
                    loop.call_soon_threadsafe(ev.set)

                pause_after_start = self.restart_paused
                self.restart_paused = False
                try:
                    # FFmpegOpusAudio already encoded the stream; send its
                    # Opus packets without a second encoder pass.
                    vc.play(source, after=after)
                except Exception as e:
                    await self.say(f"❌ Playback failed: {e}")
                    self.current = None
                    continue

                await self.set_status(config.VC_STATUS_TEMPLATE.format(title=track.title))
                await self.announce(track)

                audio_wait = asyncio.create_task(first_audio.wait())
                finish_wait = asyncio.create_task(self.next_event.wait())
                done, pending = await asyncio.wait(
                    {audio_wait, finish_wait},
                    timeout=8,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if first_audio.is_set() and pause_after_start and vc.is_playing():
                    vc.pause()
                if not first_audio.is_set():
                    detail = f" ({player_errors[-1]})" if player_errors else ""
                    await self.say(
                        f"🔇 **{track.title}** started but FFmpeg produced no audio{detail}. "
                        "Try a public SoundCloud track link and check the Railway runtime logs."
                    )
                    if vc.is_playing():
                        vc.stop()

                while True:
                    try:
                        await asyncio.wait_for(self.next_event.wait(), 5)
                        break
                    except asyncio.TimeoutError:
                        v = self.guild.voice_client
                        if v is None or not v.is_connected():
                            self.queue.clear()
                            break
                await self.end_panel()
                if self.restart_requested:
                    self.restart_requested = False
                    self.current = None
                    self.queue.appendleft(track)
                    continue
                self.current = None
                if self.loop_mode == "track" and not self.skipped:
                    self.queue.appendleft(track)
                elif self.loop_mode == "queue":
                    self.queue.append(track)
        except asyncio.CancelledError:
            pass

    # ---- controls ----
    def _vc(self):
        vc = self.guild.voice_client
        if not vc or not vc.is_connected():
            raise MusicError("I'm not in a voice channel.")
        return vc

    def skip(self):
        vc = self._vc()
        if not (vc.is_playing() or vc.is_paused()):
            raise MusicError("Nothing is playing.")
        self.skipped = True
        vc.stop()

    def pause(self):
        vc = self._vc()
        if not vc.is_playing():
            raise MusicError("Nothing is playing.")
        vc.pause()

    def resume(self):
        vc = self._vc()
        if not vc.is_paused():
            raise MusicError("Nothing is paused.")
        vc.resume()

    def stop(self):
        self.queue.clear()
        self.loop_mode = "off"
        self.skipped = True
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    def set_volume(self, percent):
        if not 0 <= percent <= 200:
            raise MusicError("Volume must be 0-200.")
        self.volume = percent / 100
        vc = self.guild.voice_client
        if vc and self.current and (vc.is_playing() or vc.is_paused()):
            # FFmpeg encodes directly to Opus; apply volume in its filter and
            # restart the current track so the new setting takes effect.
            self.restart_paused = vc.is_paused()
            self.restart_requested = True
            vc.stop()

    def set_loop(self, mode):
        if mode not in ("off", "track", "queue"):
            raise MusicError("Loop mode: off, track or queue.")
        self.loop_mode = mode

    def shuffle(self):
        items = list(self.queue)
        random.shuffle(items)
        self.queue = collections.deque(items)

    def remove(self, index):
        if not 1 <= index <= len(self.queue):
            raise MusicError("No track at that position.")
        t = self.queue[index - 1]
        del self.queue[index - 1]
        return t

    def queue_text(self):
        lines = []
        if self.current:
            lines.append(f"▶️ **{self.current.title}** `[{fmt(self.current.duration)}]`")
        for i, t in enumerate(list(self.queue)[:10], 1):
            lines.append(f"`{i}.` {t.title}")
        if len(self.queue) > 10:
            lines.append(f"...and {len(self.queue) - 10} more")
        lines.append(f"Loop: **{self.loop_mode}** | Volume: **{int(self.volume * 100)}%**")
        return "\n".join(lines) if (self.current or self.queue) else "The queue is empty."

    async def destroy(self):
        self.queue.clear()
        await self.end_panel()
        await self.set_status(None)
        await self.set_presence(None)
        self.cog.players.pop(self.guild.id, None)
        vc = self.guild.voice_client
        if vc:
            await vc.disconnect()
        if asyncio.current_task() is not self.task:
            self.task.cancel()


class PlayerView(discord.ui.View):
    """Buttons under the Now Playing embed."""

    def __init__(self, player):
        super().__init__(timeout=None)
        self.p = player

    async def interaction_check(self, interaction):
        if not has_access(interaction.user.id):
            await interaction.response.send_message(embed=info_embed("🔒 Only the owner and trusted users can use these controls.", RED), ephemeral=True)
            return False
        vc = self.p.guild.voice_client
        voice = getattr(interaction.user, "voice", None)
        if not vc or not voice or voice.channel != vc.channel:
            await interaction.response.send_message(embed=info_embed("Join my voice channel to use the controls.", YELLOW), ephemeral=True)
            return False
        return True

    async def _do(self, interaction, fn, refresh=True):
        try:
            fn()
        except MusicError as e:
            return await interaction.response.send_message(embed=info_embed(str(e), RED), ephemeral=True)
        if refresh and self.p.current:
            await interaction.response.edit_message(embed=np_embed(self.p, self.p.current), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=0)
    async def vol_down(self, interaction, button):
        await self._do(interaction, lambda: self.p.bump_volume(-10))

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, row=0)
    async def pause_btn(self, interaction, button):
        await self._do(interaction, self.p.toggle_pause)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction, button):
        await self._do(interaction, self.p.skip, refresh=False)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction, button):
        await self._do(interaction, self.p.stop, refresh=False)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=0)
    async def vol_up(self, interaction, button):
        await self._do(interaction, lambda: self.p.bump_volume(10))

    @discord.ui.button(emoji="🔁", label="Loop", style=discord.ButtonStyle.secondary, row=1)
    async def loop_btn(self, interaction, button):
        await self._do(interaction, self.p.cycle_loop)

    @discord.ui.button(emoji="🔀", label="Shuffle", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction, button):
        await self._do(interaction, self.p.shuffle)

    @discord.ui.button(emoji="📜", label="Queue", style=discord.ButtonStyle.secondary, row=1)
    async def queue_btn(self, interaction, button):
        await interaction.response.send_message(embed=queue_embed(self.p), ephemeral=True)


# ======================= cog (Discord commands) =======================
class Music(BaseCog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cog_unload(self):
        self._stay_task.cancel()
        for p in list(self.players.values()):
            p.task.cancel()
            vc = p.guild.voice_client
            if vc:
                try:
                    await vc.disconnect()
                except Exception:
                    pass
        self.players.clear()

    # ---------- 24/7 voice presence ----------
    async def cog_load(self):
        self.down_since = {}
        self.next_try = {}
        self.fails = {}
        self._stay_task = asyncio.create_task(self._stay_loop())

    async def stay_join(self, guild, channel, stay=True):
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.connect):
            raise MusicError(f"I need **View Channel** and **Connect** for **{channel.name}**.")
        await self.connect(guild, channel)
        if stay:
            data["stay"][str(guild.id)] = channel.id
        else:
            data["stay"].pop(str(guild.id), None)
        save_data()
        self.down_since.pop(guild.id, None)
        self.fails.pop(guild.id, None)
        return (f"📌 Joined **{channel.name}** and I'll stay here 24/7. Use `/leave` to stop."
                if stay else f"✅ Joined **{channel.name}** (I'll leave when idle).")

    async def leave_guild(self, guild):
        was_stay = data["stay"].pop(str(guild.id), None) is not None
        save_data()
        p = self.players.get(guild.id)
        if p is not None:
            await p.destroy()
        elif guild.voice_client:
            await guild.voice_client.disconnect()
        elif not was_stay:
            raise MusicError("I'm not in a voice channel.")

    async def _stay_check(self, guild_id, channel_id, now):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:                                   # the channel was deleted
            data["stay"].pop(str(guild_id), None)
            save_data()
            return
        vc = guild.voice_client
        if vc is not None and vc.is_connected():
            self.down_since.pop(guild_id, None)
            self.fails.pop(guild_id, None)
            if vc.channel.id != channel_id:                   # someone dragged me away -> go back
                try:
                    await vc.move_to(channel)
                except (discord.HTTPException, asyncio.TimeoutError):
                    pass
            return
        down = self.down_since.setdefault(guild_id, now)
        if now - down < STAY_GRACE or now < self.next_try.get(guild_id, 0):
            return                                            # give discord.py's own auto-reconnect a chance first
        try:
            await self.connect(guild, channel)
            self.down_since.pop(guild_id, None)
            self.fails.pop(guild_id, None)
            print(f"[24/7] back in '{channel.name}' ({guild.name})")
        except Exception as e:
            n = self.fails[guild_id] = self.fails.get(guild_id, 0) + 1
            self.next_try[guild_id] = now + min(300, 15 * 2 ** n)
            if n == 1:
                print(f"[24/7] can't rejoin '{channel.name}' in {guild.name}: {e}")

    async def _stay_loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                now = time.monotonic()
                for gid, cid in list(data["stay"].items()):
                    await self._stay_check(int(gid), cid, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[24/7] watchdog error: {type(e).__name__}: {e}")
            await asyncio.sleep(STAY_INTERVAL)

    def player(self, guild):
        p = self.players.get(guild.id)
        if p is None:
            p = self.players[guild.id] = GuildPlayer(self, guild)
        return p

    async def connect(self, guild, channel):
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
        else:
            try:
                await channel.connect(self_deaf=True)
            except (asyncio.TimeoutError, discord.ClientException):
                raise MusicError("I couldn't join that voice channel. Check my Connect permission and try again.")

    async def cog_check(self, ctx):
        return ctx.guild is not None and has_access(ctx.author.id)   # owner + trusted only

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id and after.channel is None:
            p = self.players.pop(member.guild.id, None)
            if p:
                p.queue.clear()
                p.task.cancel()

    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song, playlist or supported audio link")
    @app_commands.describe(query="Song name, Spotify link, SoundCloud link, or direct audio URL")
    async def play(self, ctx, *, query: str):
        """!play <song name | Spotify or SoundCloud link>"""
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.interaction.response.defer(thinking=True)
        voice = ctx.author.voice
        if not voice or not voice.channel:
            raise MusicError("Join a voice channel first.")
        await self.connect(ctx.guild, voice.channel)
        permissions = voice.channel.permissions_for(ctx.guild.me)
        if not permissions.speak:
            raise MusicError(f"I can join **{voice.channel.name}**, but I need the **Speak** permission there.")
        p = self.player(ctx.guild)
        p.text_channel = ctx.channel
        tracks = await expand(query, ctx.author.display_name)
        n = p.add(tracks)
        if n == 0:
            raise MusicError("The queue is full.")
        if n == 1:
            t = tracks[0]
            e = discord.Embed(title="➕ Added to queue", description=f"**{safe(t.title)}**", color=GREEN)
            if t.thumb:
                e.set_thumbnail(url=t.thumb)
            e.add_field(name="Duration", value=fmt(t.duration))
            e.add_field(name="Position", value=f"#{len(p.queue)}" if p.current else "Up next")
            e.set_footer(text=f"Requested by {t.requester}")
        else:
            e = discord.Embed(title="➕ Added to queue", description=f"**{n}** tracks added.", color=GREEN)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="testaudio", aliases=["audiotest"], description="Play a short tone to test Discord audio")
    async def testaudio(self, ctx):
        """Play a local tone to isolate Discord voice output from online sources."""
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.interaction.response.defer(ephemeral=True, thinking=True)
        voice = getattr(ctx.author, "voice", None)
        if not voice or not voice.channel:
            raise MusicError("Join a voice channel first.")
        await self.connect(ctx.guild, voice.channel)
        if not voice.channel.permissions_for(ctx.guild.me).speak:
            raise MusicError(f"I can join **{voice.channel.name}**, but I need the **Speak** permission there.")
        vc = ctx.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            raise MusicError("Stop the current audio first, then run the sound test.")

        first_audio = asyncio.Event()
        test_loop = asyncio.get_running_loop()
        source = ProbedAudioSource(
            discord.FFmpegPCMAudio(
                "sine=frequency=880:duration=4",
                before_options="-f lavfi",
                options="-af volume=0.25",
            ),
            on_audio=lambda: test_loop.call_soon_threadsafe(first_audio.set),
        )
        try:
            vc.play(source)
        except (discord.DiscordException, OSError, RuntimeError) as e:
            raise MusicError(f"Discord audio test could not start: {e}") from e

        try:
            await asyncio.wait_for(first_audio.wait(), timeout=4)
        except asyncio.TimeoutError:
            if vc.is_playing():
                vc.stop()
            await ctx.send(
                embed=info_embed(
                    "FFmpeg did not produce PCM audio frames. Check the Railway FFmpeg runtime logs.",
                    YELLOW,
                ),
                ephemeral=ctx.interaction is not None,
            )
            return
        await ctx.send(
            embed=info_embed(
                "FFmpeg produced audio frames and Discord voice playback started. If you still hear nothing, check the voice output device and whether the bot is server-muted.",
                GREEN,
            ),
            ephemeral=ctx.interaction is not None,
        )

    @commands.hybrid_command(name="skip", aliases=["s"], description="Skip the current song")
    async def skip(self, ctx):
        self.player(ctx.guild).skip()
        await ctx.send(embed=info_embed("⏭️ Skipped."))

    @commands.hybrid_command(name="pause", description="Pause the music")
    async def pause(self, ctx):
        self.player(ctx.guild).pause()
        await ctx.send(embed=info_embed("⏸️ Paused.", YELLOW))

    @commands.hybrid_command(name="resume", description="Resume the music")
    async def resume(self, ctx):
        self.player(ctx.guild).resume()
        await ctx.send(embed=info_embed("▶️ Resumed.", GREEN))

    @commands.hybrid_command(name="stop", description="Stop the music and clear the queue")
    async def stop(self, ctx):
        self.player(ctx.guild).stop()
        await ctx.send(embed=info_embed("⏹️ Stopped and cleared the queue.", RED))

    @commands.hybrid_command(name="queue", aliases=["q"], description="Show the queue")
    async def queue(self, ctx):
        await ctx.send(embed=queue_embed(self.player(ctx.guild)))

    @commands.hybrid_command(name="nowplaying", aliases=["np", "panel"], description="Show the Now Playing card with control buttons")
    async def nowplaying(self, ctx):
        """Shows the Now Playing card with the control buttons."""
        p = self.player(ctx.guild)
        if not p.current:
            raise MusicError("Nothing is playing.")
        p.text_channel = ctx.channel
        await p.end_panel()
        p.panel = await ctx.send(embed=np_embed(p, p.current), view=PlayerView(p))

    @commands.hybrid_command(name="volume", aliases=["vol", "v"], description="Show or set the volume (0-200%)")
    @app_commands.describe(percent="0-200 (leave empty to just show it)")
    async def volume(self, ctx, percent: Optional[int] = None):
        """!volume  (show)  |  !volume 80  (set, 0-200)"""
        p = self.player(ctx.guild)
        if percent is not None:
            p.set_volume(percent)
        await ctx.send(embed=volume_embed(p))

    @commands.hybrid_command(name="loop", description="Set loop mode")
    @app_commands.describe(mode="off, track or queue")
    async def loop(self, ctx, mode: Literal["off", "track", "queue"] = "track"):
        """!loop off | track | queue"""
        p = self.player(ctx.guild)
        p.set_loop(mode)
        icon = {"off": "➡️ Loop off", "track": "🔂 Looping the current track", "queue": "🔁 Looping the queue"}[p.loop_mode]
        await ctx.send(embed=info_embed(icon))

    @commands.hybrid_command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, ctx):
        self.player(ctx.guild).shuffle()
        await ctx.send(embed=info_embed("🔀 Queue shuffled."))

    @commands.hybrid_command(name="remove", description="Remove a song from the queue")
    @app_commands.describe(position="Queue position (see /queue)")
    async def remove(self, ctx, position: int):
        t = self.player(ctx.guild).remove(position)
        await ctx.send(embed=info_embed(f"🗑️ Removed **{safe(t.title)}**"))

    @commands.hybrid_command(name="join", description="Join a voice channel and stay there 24/7")
    @app_commands.describe(channel="Voice channel (default: the one you're in)", stay="Stay 24/7 (default: yes)")
    async def join(self, ctx, channel: Optional[discord.VoiceChannel] = None, stay: bool = True):
        if channel is None:
            channel = ctx.author.voice.channel if ctx.author.voice else None
        if channel is None:
            raise MusicError("Join a voice channel or pick one: `/join channel:#General`.")
        async with ctx.typing():
            text = await self.stay_join(ctx.guild, channel, stay)
        await ctx.send(embed=info_embed(text, GREEN))

    @commands.hybrid_command(name="leave", aliases=["disconnect", "dc"], description="Leave the voice channel (and stop 24/7 mode)")
    async def leave(self, ctx):
        await self.leave_guild(ctx.guild)
        await ctx.send(embed=info_embed("👋 Left the voice channel. 24/7 mode is off."))


# ======================= console commands =======================
def _cog(bot):
    cog = bot.get_cog("Music")
    if cog is None:
        raise MusicError("Music module isn't loaded.")
    return cog


def _player(bot):
    g = need_guild(bot)
    return _cog(bot).player(g) if g else None


async def c_join(bot, args):
    g = need_guild(bot)
    if not g:
        return
    stay = not (args and args[-1].lower() == "temp")
    name = " ".join(args[:-1] if not stay else args)
    ch = find_vc(g, name) if name else None
    if not ch:
        print("Usage: join <channel> [temp]   (number, ID or name - see 'channels'; 'temp' = don't stay 24/7)")
        return
    print((await _cog(bot).stay_join(g, ch, stay)).replace("**", "").replace("`", ""))


async def c_leave(bot, args):
    g = need_guild(bot)
    if g:
        await _cog(bot).leave_guild(g)
        print("Left the voice channel. 24/7 mode is off.")


async def c_play(bot, args):
    g = need_guild(bot)
    if not g:
        return
    if not args:
        print("Usage: play <song name or link>")
        return
    if not (g.voice_client and g.voice_client.is_connected()):
        print("I'm not in a voice channel. Use: join <channel>")
        return
    p = _cog(bot).player(g)
    print("Searching...")
    tracks = await expand(" ".join(args), "console")
    n = p.add(tracks)
    print(f"Queued {n} track(s)." if n else "The queue is full.")


async def c_skip(bot, args):
    p = _player(bot)
    if p:
        p.skip()
        print("Skipped.")


async def c_pause(bot, args):
    p = _player(bot)
    if p:
        p.pause()
        print("Paused.")


async def c_resume(bot, args):
    p = _player(bot)
    if p:
        p.resume()
        print("Resumed.")


async def c_mstop(bot, args):
    p = _player(bot)
    if p:
        p.stop()
        print("Stopped and cleared the queue.")


async def c_queue(bot, args):
    p = _player(bot)
    if p:
        print(p.queue_text().replace("**", "").replace("`", ""))


async def c_np(bot, args):
    p = _player(bot)
    if p:
        t = p.current
        print(f"Now playing: {t.title} [{fmt(t.duration)}]" if t else "Nothing is playing.")


async def c_volume(bot, args):
    p = _player(bot)
    if not p:
        return
    try:
        p.set_volume(int(args[0]))
        print(f"Volume: {args[0]}%")
    except (IndexError, ValueError):
        print("Usage: volume <0-200>")


async def c_loop(bot, args):
    p = _player(bot)
    if p:
        p.set_loop((args[0] if args else "track").lower())
        print(f"Loop: {p.loop_mode}")


async def c_shuffle(bot, args):
    p = _player(bot)
    if p:
        p.shuffle()
        print("Queue shuffled.")


async def c_remove(bot, args):
    p = _player(bot)
    if not p:
        return
    try:
        t = p.remove(int(args[0]))
        print(f"Removed {t.title}")
    except (IndexError, ValueError):
        print("Usage: remove <position>")


async def c_hq(bot, args):
    g = need_guild(bot)
    if not g:
        return
    vc = g.voice_client
    if not vc or not vc.is_connected():
        print("Join a voice channel first.")
        return
    limit = int(g.bitrate_limit)
    try:
        await vc.channel.edit(bitrate=limit)
        print(f"Channel bitrate set to {limit // 1000} kbps (the max for this server's boost level).")
    except discord.Forbidden:
        print("[!] The bot needs the Manage Channels permission to change the bitrate.")


async def setup(bot):
    await bot.add_cog(Music(bot))
    a = bot.add_console
    a("join", c_join, "join <channel> [temp]", "join a voice channel and stay 24/7", group=GROUP)
    a("leave", c_leave, "leave", "bot leaves voice", group=GROUP)
    a("play", c_play, "play <name or link>", "Spotify metadata + SoundCloud/direct audio", aliases=("p",), group=GROUP)
    a("skip", c_skip, "skip", "skip current track", group=GROUP)
    a("pause", c_pause, "pause", "pause", group=GROUP)
    a("resume", c_resume, "resume", "resume", group=GROUP)
    a("mstop", c_mstop, "mstop", "stop music and clear queue", group=GROUP)
    a("queue", c_queue, "queue", "show the queue", aliases=("q",), group=GROUP)
    a("np", c_np, "np", "now playing", group=GROUP)
    a("volume", c_volume, "volume <0-200>", "set volume", aliases=("vol",), group=GROUP)
    a("loop", c_loop, "loop off|track|queue", "loop mode", group=GROUP)
    a("shuffle", c_shuffle, "shuffle", "shuffle the queue", group=GROUP)
    a("remove", c_remove, "remove <position>", "remove a queued track", group=GROUP)
    a("hq", c_hq, "hq", "set channel bitrate to the server max", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
