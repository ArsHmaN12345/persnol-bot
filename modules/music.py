"""Lavalink-backed music playback, queue controls, and voice presence."""
import asyncio
import base64
import collections
import json
import random
import re
import time
import urllib.error
import urllib.request
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import data, save_data, need_guild, find_vc, has_access, BotError
from ui import BaseCog

GROUP = "music"
BLURPLE, GREEN, YELLOW, RED = 0x5865F2, 0x57F287, 0xFEE75C, 0xED4245
STAY_GRACE = 45
STAY_INTERVAL = 15
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)")
_spotify = {"token": None, "expires": 0}


class MusicError(BotError):
    pass


def _lavalink():
    from modules import lavalink

    node = lavalink.NODE
    if node is None:
        raise MusicError("Music needs Lavalink. Configure LAVALINK_HOST, LAVALINK_PORT, and LAVALINK_PASSWORD.")
    if not node.ready:
        detail = f" ({node.last_error})" if node.last_error else ""
        raise MusicError(f"Lavalink isn't connected yet{detail}. Check the Lavalink Railway service.")
    return lavalink


def fmt(seconds):
    if seconds is None:
        return "Live"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def safe(text):
    return discord.utils.escape_markdown(str(text)).replace("[", "(").replace("]", ")")


def bar(volume, width=12):
    filled = round(min(max(volume, 0), 2.0) / 2.0 * width)
    return "▰" * filled + "▱" * (width - filled)


def info_embed(text, color=BLURPLE):
    return discord.Embed(description=text, color=color)


class Track:
    __slots__ = ("title", "requester", "encoded", "page", "thumb", "duration", "query")

    def __init__(self, title, requester, *, encoded=None, page=None, thumb=None, duration=None, query=None):
        self.title = title or "Unknown"
        self.requester = requester
        self.encoded = encoded
        self.page = page
        self.thumb = thumb
        self.duration = duration
        self.query = query


def track_from_lavalink(item, requester):
    return Track(
        item.get("title"),
        requester,
        encoded=item.get("encoded"),
        page=item.get("uri"),
        thumb=item.get("thumb"),
        duration=item.get("length"),
    )


def _spotify_token():
    if _spotify["token"] and time.time() < _spotify["expires"] - 30:
        return _spotify["token"]
    if not (config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET):
        raise MusicError("Spotify links need SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")
    auth = base64.b64encode(
        f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()
    request = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise MusicError(f"Spotify rejected its credentials (HTTP {error.code}).")
    except urllib.error.URLError:
        raise MusicError("Couldn't reach Spotify. Try again shortly.")
    _spotify["token"] = result["access_token"]
    _spotify["expires"] = time.time() + result.get("expires_in", 3600)
    return _spotify["token"]


def _spotify_get(url):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {_spotify_token()}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise MusicError("Spotify refused the request. Check the Spotify API credentials.")
        raise MusicError(f"Spotify returned HTTP {error.code}.")
    except urllib.error.URLError:
        raise MusicError("Couldn't reach Spotify. Try again shortly.")


def spotify_tracks(kind, identifier):
    tracks = []
    if kind == "track":
        item = _spotify_get(f"https://api.spotify.com/v1/tracks/{identifier}")
        tracks.append((item["name"], item["artists"][0]["name"]))
    elif kind == "album":
        page = _spotify_get(f"https://api.spotify.com/v1/albums/{identifier}/tracks?limit=50")
        while page:
            tracks.extend((item["name"], item["artists"][0]["name"]) for item in page["items"])
            page = _spotify_get(page["next"]) if page.get("next") and len(tracks) < config.MAX_QUEUE else None
    else:
        page = _spotify_get(f"https://api.spotify.com/v1/playlists/{identifier}/items?limit=100")
        while page:
            for entry in page["items"]:
                item = entry.get("item") or entry.get("track")
                if item and item.get("type", "track") == "track" and item.get("artists"):
                    tracks.append((item["name"], item["artists"][0]["name"]))
            page = _spotify_get(page["next"]) if page.get("next") and len(tracks) < config.MAX_QUEUE else None
    if not tracks:
        raise MusicError("Couldn't find any tracks in that Spotify link.")
    return tracks[:config.MAX_QUEUE]


async def expand(query, requester):
    """Resolve a song search, supported link, or Spotify playlist through Lavalink."""
    query = query.strip().strip("<>")
    if not query:
        raise MusicError("Enter a song name or a supported music link.")

    lv = _lavalink()
    match = SPOTIFY_RE.search(query)
    if match:
        loop = asyncio.get_running_loop()
        pairs = await loop.run_in_executor(None, spotify_tracks, *match.groups())
        semaphore = asyncio.Semaphore(5)

        async def resolve_spotify(title, artist):
            async with semaphore:
                results = await lv.NODE.search(f"{title} {artist}")
            return track_from_lavalink(results[0], requester) if results else None

        resolved = await asyncio.gather(
            *(resolve_spotify(title, artist) for title, artist in pairs),
            return_exceptions=True,
        )
        tracks = [item for item in resolved if isinstance(item, Track)]
        if not tracks:
            error = next((item for item in resolved if isinstance(item, Exception)), None)
            if error:
                raise MusicError(str(error))
            raise MusicError("Lavalink couldn't find playable matches for that Spotify link.")
        return tracks[:config.MAX_QUEUE]

    if query.startswith(("http://", "https://")):
        results = await lv.NODE.resolve(query)
    else:
        results = await lv.NODE.search(query)
        results = results[:1]
    if not results:
        raise MusicError("No playable results found. Try another search or link.")
    return [track_from_lavalink(item, requester) for item in results[:config.MAX_QUEUE]]


def volume_embed(player):
    percent = int(player.volume * 100)
    icon = "🔇" if percent == 0 else "🔉" if percent < 50 else "🔊"
    embed = discord.Embed(
        title=f"{icon} Volume",
        description=f"`{bar(player.volume)}`  **{percent}%**",
        color=BLURPLE,
    )
    embed.set_footer(text="0%–200% • 100% is original loudness")
    return embed


def queue_embed(player):
    return discord.Embed(title="📜 Queue", description=player.queue_text(), color=BLURPLE)


def np_embed(player, track):
    title = safe(track.title)
    description = f"**[{title}]({track.page})**" if track.page else f"**{title}**"
    voice = player.guild.voice_client
    paused = bool(voice and voice.is_paused())
    embed = discord.Embed(
        title="⏸️ Paused" if paused else "🎶 Now Playing",
        description=description,
        color=YELLOW if paused else BLURPLE,
    )
    if track.thumb:
        embed.set_thumbnail(url=track.thumb)
    embed.add_field(name="Duration", value=fmt(track.duration), inline=True)
    embed.add_field(name="Requested by", value=safe(track.requester), inline=True)
    embed.add_field(name="In queue", value=str(len(player.queue)), inline=True)
    embed.add_field(name="Volume", value=f"`{bar(player.volume)}`  **{int(player.volume * 100)}%**")
    loop = {"off": "off", "track": "🔂 track", "queue": "🔁 queue"}[player.loop_mode]
    embed.set_footer(text=f"Loop: {loop} • Lavalink")
    return embed


class GuildPlayer:
    def __init__(self, cog, guild):
        self.cog = cog
        self.guild = guild
        self.queue = collections.deque()
        self.current = None
        self.volume = config.DEFAULT_VOLUME
        self.loop_mode = "off"
        self.skipped = False
        self.text_channel = None
        self.panel = None
        self.has_tracks = asyncio.Event()
        self.next_event = asyncio.Event()
        self.next_error = None
        self.task = asyncio.create_task(self.run())

    async def say(self, text):
        print(f"[music] {text}")
        if self.text_channel:
            try:
                await self.text_channel.send(text)
            except discord.HTTPException as error:
                print(f"[music] couldn't send message: {error}")

    async def set_status(self, text):
        if not config.VC_STATUS:
            return
        voice = self.guild.voice_client
        if not voice or not voice.channel:
            return
        try:
            await voice.channel.edit(status=text[:120] if text else None)
        except discord.Forbidden:
            print("[music] Can't set voice-channel status; grant Set Voice Channel Status permission.")
        except discord.HTTPException as error:
            print(f"[music] couldn't update voice-channel status: {error}")

    async def set_presence(self, title):
        if not config.SET_PRESENCE:
            return
        try:
            presence = self.cog.bot.get_cog("Presence")
            if presence:
                await presence.music_update(self.guild.id, title)
            else:
                activity = discord.Activity(type=discord.ActivityType.listening, name=title[:100]) if title else None
                await self.cog.bot.change_presence(activity=activity)
        except (discord.HTTPException, AttributeError) as error:
            print(f"[music] couldn't update bot presence: {error}")

    async def end_panel(self):
        if self.panel:
            try:
                await self.panel.edit(view=None)
            except discord.HTTPException as error:
                print(f"[music] couldn't close player controls: {error}")
            self.panel = None

    async def announce(self, track):
        print(f"[music] now playing: {track.title} [{fmt(track.duration)}] - {track.requester}")
        await self.end_panel()
        await self.set_presence(track.title)
        if self.text_channel:
            try:
                self.panel = await self.text_channel.send(embed=np_embed(self, track), view=PlayerView(self))
            except discord.HTTPException as error:
                self.panel = None
                print(f"[music] couldn't post player controls: {error}")

    def add(self, tracks):
        room = max(config.MAX_QUEUE - len(self.queue), 0)
        accepted = tracks[:room]
        self.queue.extend(accepted)
        if accepted:
            self.has_tracks.set()
        return len(accepted)

    def _vc(self):
        voice = self.guild.voice_client
        if not voice or not voice.is_connected():
            raise MusicError("I'm not connected to a voice channel.")
        return voice

    def skip(self):
        voice = self._vc()
        if not (voice.is_playing() or voice.is_paused()):
            raise MusicError("Nothing is playing.")
        self.skipped = True
        voice.stop()

    def pause(self):
        voice = self._vc()
        if not voice.is_playing():
            raise MusicError("Nothing is playing.")
        voice.pause()

    def resume(self):
        voice = self._vc()
        if not voice.is_paused():
            raise MusicError("Nothing is paused.")
        voice.resume()

    def toggle_pause(self):
        voice = self._vc()
        if voice.is_paused():
            voice.resume()
        elif voice.is_playing():
            voice.pause()
        else:
            raise MusicError("Nothing is playing.")

    def stop(self):
        self.queue.clear()
        self.loop_mode = "off"
        self.skipped = True
        voice = self.guild.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

    def set_volume(self, percent):
        if not 0 <= percent <= 200:
            raise MusicError("Volume must be between 0 and 200.")
        self.volume = percent / 100
        voice = self.guild.voice_client
        if voice and hasattr(voice, "set_volume_percent"):
            voice.set_volume_percent(percent)

    def bump_volume(self, delta):
        self.set_volume(max(0, min(200, round(self.volume * 100) + delta)))

    def set_loop(self, mode):
        if mode not in ("off", "track", "queue"):
            raise MusicError("Loop mode must be off, track, or queue.")
        self.loop_mode = mode

    def cycle_loop(self):
        modes = ("off", "track", "queue")
        self.loop_mode = modes[(modes.index(self.loop_mode) + 1) % len(modes)]

    def shuffle(self):
        tracks = list(self.queue)
        random.shuffle(tracks)
        self.queue = collections.deque(tracks)

    def remove(self, position):
        if not 1 <= position <= len(self.queue):
            raise MusicError("There is no queued track at that position.")
        track = self.queue[position - 1]
        del self.queue[position - 1]
        return track

    def queue_text(self):
        lines = []
        if self.current:
            lines.append(f"▶️ **{safe(self.current.title)}** `[{fmt(self.current.duration)}]`")
        lines.extend(
            f"`{index}.` {safe(track.title)}"
            for index, track in enumerate(list(self.queue)[:10], 1)
        )
        if len(self.queue) > 10:
            lines.append(f"...and {len(self.queue) - 10} more")
        lines.append(f"Loop: **{self.loop_mode}** | Volume: **{int(self.volume * 100)}%**")
        return "\n".join(lines) if self.current or self.queue else "The queue is empty."

    async def run(self):
        loop = asyncio.get_running_loop()
        try:
            while True:
                if not self.queue:
                    await self.set_status(None)
                    await self.set_presence(None)
                while not self.queue:
                    self.has_tracks.clear()
                    if self.queue:
                        break
                    try:
                        await asyncio.wait_for(self.has_tracks.wait(), config.IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        if str(self.guild.id) in data["stay"]:
                            continue
                        await self.say("💤 Idle for a while; leaving the voice channel.")
                        await self.destroy()
                        return

                from modules import lavalink

                if lavalink.NODE is None or not lavalink.NODE.ready:
                    await asyncio.sleep(5)
                    continue

                track = self.queue.popleft()
                voice = self.guild.voice_client
                if not voice or not voice.is_connected():
                    if str(self.guild.id) not in data["stay"]:
                        await self.say("❌ Disconnected from voice; stopping the music queue.")
                        await self.destroy()
                        return
                    self.queue.appendleft(track)
                    await asyncio.sleep(5)
                    continue

                self.current = track
                self.skipped = False
                self.next_event.clear()
                self.next_error = None

                def after(error, event=self.next_event):
                    if error:
                        print(f"[music] playback ended with an error: {error}")
                        self.next_error = error
                    loop.call_soon_threadsafe(event.set)

                try:
                    lv = _lavalink()
                    await lv.resolve_track(track, voice.node)
                    await voice.play_track(track, int(self.volume * 100), after)
                except BotError as error:
                    self.current = None
                    if lavalink.NODE is None or not lavalink.NODE.ready:
                        self.queue.appendleft(track)
                        await asyncio.sleep(5)
                        continue
                    await self.say(f"❌ Couldn't play **{safe(track.title)}**: {error}")
                    continue
                except (discord.HTTPException, asyncio.TimeoutError) as error:
                    self.current = None
                    await self.say(f"❌ Playback failed for **{safe(track.title)}**.")
                    print(f"[music] playback request failed: {type(error).__name__}: {error}")
                    continue

                await self.set_status(config.VC_STATUS_TEMPLATE.format(title=track.title))
                await self.announce(track)
                while True:
                    try:
                        await asyncio.wait_for(self.next_event.wait(), 5)
                        break
                    except asyncio.TimeoutError:
                        voice = self.guild.voice_client
                        if voice is None or not voice.is_connected():
                            if str(self.guild.id) not in data["stay"]:
                                self.queue.clear()
                            break

                await self.end_panel()
                self.current = None
                voice = self.guild.voice_client
                if self.next_error and str(self.next_error) == "lavalink connection lost":
                    self.queue.appendleft(track)
                elif (
                    not self.skipped
                    and str(self.guild.id) in data["stay"]
                    and (voice is None or not voice.is_connected())
                ):
                    self.queue.appendleft(track)
                elif self.loop_mode == "track" and not self.skipped:
                    self.queue.appendleft(track)
                elif self.loop_mode == "queue":
                    self.queue.append(track)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[music] player crashed in {self.guild.id}: {type(error).__name__}: {error}")
            await self.say("❌ The music player stopped unexpectedly. Check the bot logs.")
            await self.destroy()

    async def destroy(self):
        self.queue.clear()
        self.has_tracks.set()
        await self.end_panel()
        await self.set_status(None)
        await self.set_presence(None)
        self.cog.players.pop(self.guild.id, None)
        voice = self.guild.voice_client
        if voice:
            try:
                await voice.disconnect()
            except (discord.HTTPException, asyncio.TimeoutError) as error:
                print(f"[music] couldn't disconnect from voice: {error}")
        if asyncio.current_task() is not self.task:
            self.task.cancel()


class PlayerView(discord.ui.View):
    def __init__(self, player):
        super().__init__(timeout=None)
        self.player = player

    async def interaction_check(self, interaction):
        if not has_access(interaction.user.id):
            await interaction.response.send_message(
                embed=info_embed("🔒 Only the owner and trusted users can use these controls.", RED),
                ephemeral=True,
            )
            return False
        voice = self.player.guild.voice_client
        member_voice = getattr(interaction.user, "voice", None)
        if not voice or not member_voice or member_voice.channel != voice.channel:
            await interaction.response.send_message(
                embed=info_embed("Join my voice channel to use the controls.", YELLOW),
                ephemeral=True,
            )
            return False
        return True

    async def _do(self, interaction, action, refresh=True):
        try:
            action()
        except MusicError as error:
            await interaction.response.send_message(embed=info_embed(str(error), RED), ephemeral=True)
            return
        if refresh and self.player.current:
            await interaction.response.edit_message(
                embed=np_embed(self.player, self.player.current),
                view=self,
            )
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=0)
    async def volume_down(self, interaction, button):
        await self._do(interaction, lambda: self.player.bump_volume(-10))

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, row=0)
    async def pause_button(self, interaction, button):
        await self._do(interaction, self.player.toggle_pause)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction, button):
        await self._do(interaction, self.player.skip, refresh=False)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(self, interaction, button):
        await self._do(interaction, self.player.stop, refresh=False)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=0)
    async def volume_up(self, interaction, button):
        await self._do(interaction, lambda: self.player.bump_volume(10))

    @discord.ui.button(emoji="🔁", label="Loop", style=discord.ButtonStyle.secondary, row=1)
    async def loop_button(self, interaction, button):
        await self._do(interaction, self.player.cycle_loop)

    @discord.ui.button(emoji="🔀", label="Shuffle", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_button(self, interaction, button):
        await self._do(interaction, self.player.shuffle)

    @discord.ui.button(emoji="📜", label="Queue", style=discord.ButtonStyle.secondary, row=1)
    async def queue_button(self, interaction, button):
        await interaction.response.send_message(embed=queue_embed(self.player), ephemeral=True)


class Music(BaseCog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cog_load(self):
        self.down_since = {}
        self.next_try = {}
        self.fails = {}
        self._stay_task = asyncio.create_task(self._stay_loop())

    async def cog_unload(self):
        self._stay_task.cancel()
        await asyncio.gather(self._stay_task, return_exceptions=True)
        players = list(self.players.values())
        for player in players:
            player.task.cancel()
            await player.end_panel()
            voice = player.guild.voice_client
            if voice:
                try:
                    await voice.disconnect()
                except discord.HTTPException as error:
                    print(f"[music] couldn't disconnect during unload: {error}")
        await asyncio.gather(*(player.task for player in players), return_exceptions=True)
        self.players.clear()

    def player(self, guild):
        player = self.players.get(guild.id)
        if player is None:
            player = self.players[guild.id] = GuildPlayer(self, guild)
        return player

    async def connect(self, guild, channel):
        lv = _lavalink()
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.connect):
            raise MusicError(f"I need View Channel and Connect permission for {channel.name}.")
        if not permissions.speak:
            raise MusicError(f"I need Speak permission in {channel.name}.")
        voice = guild.voice_client
        already_connected = voice and voice.is_connected()
        if (not already_connected or voice.channel.id != channel.id) and channel.user_limit:
            if len(channel.members) >= channel.user_limit and not permissions.move_members:
                raise MusicError(f"{channel.name} is full.")
        if voice:
            if voice.is_connected():
                if voice.channel.id != channel.id:
                    await voice.move_to(channel)
                return
            try:
                await voice.disconnect(force=True)
            except (discord.HTTPException, asyncio.TimeoutError) as error:
                raise MusicError(f"Couldn't clear the stale voice connection ({type(error).__name__}).")
        try:
            await channel.connect(
                cls=lv.LavalinkVoiceClient,
                self_deaf=True,
                timeout=20,
            )
        except lv.LavalinkError as error:
            raise MusicError(str(error))
        except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError) as error:
            raise MusicError(f"Couldn't join that voice channel ({type(error).__name__}).")

    async def stay_join(self, guild, channel, stay=True):
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.connect):
            raise MusicError(f"I need View Channel and Connect permission for {channel.name}.")
        await self.connect(guild, channel)
        if stay:
            data["stay"][str(guild.id)] = channel.id
        else:
            data["stay"].pop(str(guild.id), None)
        save_data()
        self.down_since.pop(guild.id, None)
        self.fails.pop(guild.id, None)
        if stay:
            return f"📌 Joined **{channel.name}** and will stay 24/7. Use `/leave` to stop."
        return f"✅ Joined **{channel.name}**; I'll leave when idle."

    async def leave_guild(self, guild):
        was_staying = data["stay"].pop(str(guild.id), None) is not None
        save_data()
        player = self.players.get(guild.id)
        if player:
            await player.destroy()
        elif guild.voice_client:
            await guild.voice_client.disconnect()
        elif not was_staying:
            raise MusicError("I'm not in a voice channel.")

    async def _stay_check(self, guild_id, channel_id, now):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            data["stay"].pop(str(guild_id), None)
            save_data()
            return
        voice = guild.voice_client
        if voice and voice.is_connected():
            self.down_since.pop(guild_id, None)
            self.fails.pop(guild_id, None)
            if voice.channel.id != channel.id:
                try:
                    await voice.move_to(channel)
                except (discord.HTTPException, asyncio.TimeoutError) as error:
                    print(f"[24/7] couldn't move back to {channel.name}: {error}")
            return
        down = self.down_since.setdefault(guild_id, now)
        if now - down < STAY_GRACE or now < self.next_try.get(guild_id, 0):
            return
        try:
            await self.connect(guild, channel)
            self.down_since.pop(guild_id, None)
            self.fails.pop(guild_id, None)
            print(f"[24/7] rejoined {channel.name} in {guild.name}")
        except (MusicError, discord.HTTPException, asyncio.TimeoutError) as error:
            count = self.fails[guild_id] = self.fails.get(guild_id, 0) + 1
            self.next_try[guild_id] = now + min(300, 15 * 2 ** count)
            if count == 1:
                print(f"[24/7] couldn't rejoin {channel.name} in {guild.name}: {error}")

    async def _stay_loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                now = time.monotonic()
                for guild_id, channel_id in list(data["stay"].items()):
                    await self._stay_check(int(guild_id), channel_id, now)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[24/7] watchdog error: {type(error).__name__}: {error}")
            await asyncio.sleep(STAY_INTERVAL)

    async def cog_check(self, ctx):
        return ctx.guild is not None and has_access(ctx.author.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if self.bot.user and member.id == self.bot.user.id and after.channel is None:
            if str(member.guild.id) in data["stay"]:
                return
            player = self.players.pop(member.guild.id, None)
            if player:
                player.queue.clear()
                await player.end_panel()
                await player.set_status(None)
                await player.set_presence(None)
                player.task.cancel()

    @commands.hybrid_command(
        name="play",
        aliases=["p"],
        description="Play a song, playlist, or supported music link",
    )
    @app_commands.describe(query="Song name or YouTube, Spotify, SoundCloud, or other supported link")
    async def play(self, ctx, *, query: str):
        await ctx.defer()
        member_voice = ctx.author.voice
        if not member_voice or not member_voice.channel:
            raise MusicError("Join a voice channel first.")
        await self.connect(ctx.guild, member_voice.channel)
        player = self.player(ctx.guild)
        player.text_channel = ctx.channel
        try:
            tracks = await asyncio.wait_for(expand(query, ctx.author.display_name), timeout=60)
        except asyncio.TimeoutError:
            raise MusicError("Music search timed out. Check the Lavalink service and try again.")
        added = player.add(tracks)
        if added == 0:
            raise MusicError("The queue is full.")
        track = tracks[0]
        embed = discord.Embed(
            title="➕ Added to queue",
            description=f"**{safe(track.title)}**" if added == 1 else f"**{added}** tracks added.",
            color=GREEN,
        )
        if track.thumb and added == 1:
            embed.set_thumbnail(url=track.thumb)
        if added == 1:
            embed.add_field(name="Duration", value=fmt(track.duration))
            embed.add_field(name="Position", value=f"#{len(player.queue)}" if player.current else "Up next")
            embed.set_footer(text=f"Requested by {track.requester}")
        if added < len(tracks):
            embed.set_footer(text=f"Added {added} of {len(tracks)} tracks; queue limit reached.")
        await ctx.send(embed=embed)

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

    @commands.hybrid_command(name="queue", aliases=["q"], description="Show the music queue")
    async def queue(self, ctx):
        await ctx.send(embed=queue_embed(self.player(ctx.guild)))

    @commands.hybrid_command(name="nowplaying", aliases=["np", "panel"], description="Show music controls")
    async def nowplaying(self, ctx):
        player = self.player(ctx.guild)
        if not player.current:
            raise MusicError("Nothing is playing.")
        player.text_channel = ctx.channel
        await player.end_panel()
        player.panel = await ctx.send(embed=np_embed(player, player.current), view=PlayerView(player))

    @commands.hybrid_command(name="volume", aliases=["vol", "v"], description="Show or set volume (0-200%)")
    @app_commands.describe(percent="0-200; leave empty to show the current volume")
    async def volume(self, ctx, percent: Optional[int] = None):
        player = self.player(ctx.guild)
        if percent is not None:
            player.set_volume(percent)
        await ctx.send(embed=volume_embed(player))

    @commands.hybrid_command(name="loop", description="Set music loop mode")
    @app_commands.describe(mode="off, track, or queue")
    async def loop(self, ctx, mode: Literal["off", "track", "queue"] = "track"):
        player = self.player(ctx.guild)
        player.set_loop(mode)
        message = {
            "off": "➡️ Loop off",
            "track": "🔂 Looping the current track",
            "queue": "🔁 Looping the queue",
        }[mode]
        await ctx.send(embed=info_embed(message))

    @commands.hybrid_command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, ctx):
        self.player(ctx.guild).shuffle()
        await ctx.send(embed=info_embed("🔀 Queue shuffled."))

    @commands.hybrid_command(name="remove", description="Remove a queued song")
    @app_commands.describe(position="Queue position")
    async def remove(self, ctx, position: int):
        track = self.player(ctx.guild).remove(position)
        await ctx.send(embed=info_embed(f"🗑️ Removed **{safe(track.title)}**"))

    @commands.hybrid_command(name="join", description="Join a voice channel and optionally stay 24/7")
    @app_commands.describe(channel="Voice channel (defaults to yours)", stay="Stay 24/7 (default: yes)")
    async def join(self, ctx, channel: Optional[discord.VoiceChannel] = None, stay: bool = True):
        await ctx.defer()
        if channel is None:
            channel = ctx.author.voice.channel if ctx.author.voice else None
        if channel is None:
            raise MusicError("Join a voice channel or choose one with `/join channel:#General`.")
        text = await self.stay_join(ctx.guild, channel, stay)
        await ctx.send(embed=info_embed(text, GREEN))

    @commands.hybrid_command(name="leave", aliases=["disconnect", "dc"], description="Leave voice and disable 24/7")
    async def leave(self, ctx):
        await self.leave_guild(ctx.guild)
        await ctx.send(embed=info_embed("👋 Left the voice channel. 24/7 mode is off."))

    @commands.hybrid_command(name="help", description="Show music commands")
    async def help(self, ctx):
        prefix = config.PREFIX
        embed = discord.Embed(
            title="🎵 Music commands",
            color=BLURPLE,
            description="Search and play music through the configured Lavalink server.",
        )
        embed.add_field(name="Play", value=f"`{prefix}play <name or link>`\n`{prefix}queue`  `{prefix}nowplaying`", inline=False)
        embed.add_field(name="Control", value=f"`{prefix}skip`  `{prefix}pause`  `{prefix}resume`  `{prefix}stop`  `{prefix}leave`", inline=False)
        embed.add_field(name="Tune", value=f"`{prefix}volume <0-200>`  `{prefix}loop off|track|queue`\n`{prefix}shuffle`  `{prefix}remove <number>`", inline=False)
        embed.add_field(name="Setup", value="`/musicdiag` checks the Lavalink connection.", inline=False)
        embed.set_footer(text="Use the buttons under Now Playing for quick controls.")
        await ctx.send(embed=embed)


def _cog(bot):
    cog = bot.get_cog("Music")
    if cog is None:
        raise MusicError("Music module isn't loaded.")
    return cog


def _player(bot):
    guild = need_guild(bot)
    return _cog(bot).player(guild) if guild else None


async def c_join(bot, args):
    guild = need_guild(bot)
    if not guild:
        return
    stay = not (args and args[-1].lower() == "temp")
    name = " ".join(args[:-1] if not stay else args)
    channel = find_vc(guild, name) if name else None
    if not channel:
        print("Usage: join <channel> [temp]   (number, ID or name - see 'channels')")
        return
    print((await _cog(bot).stay_join(guild, channel, stay)).replace("**", "").replace("`", ""))


async def c_leave(bot, args):
    guild = need_guild(bot)
    if guild:
        await _cog(bot).leave_guild(guild)
        print("Left the voice channel. 24/7 mode is off.")


async def c_play(bot, args):
    guild = need_guild(bot)
    if not guild:
        return
    if not args:
        print("Usage: play <song name or link>")
        return
    if not guild.voice_client or not guild.voice_client.is_connected():
        print("I'm not in a voice channel. Use: join <channel>")
        return
    player = _cog(bot).player(guild)
    print("Searching...")
    tracks = await expand(" ".join(args), "console")
    added = player.add(tracks)
    print(f"Queued {added} track(s)." if added else "The queue is full.")


async def c_skip(bot, args):
    player = _player(bot)
    if player:
        player.skip()
        print("Skipped.")


async def c_pause(bot, args):
    player = _player(bot)
    if player:
        player.pause()
        print("Paused.")


async def c_resume(bot, args):
    player = _player(bot)
    if player:
        player.resume()
        print("Resumed.")


async def c_mstop(bot, args):
    player = _player(bot)
    if player:
        player.stop()
        print("Stopped and cleared the queue.")


async def c_queue(bot, args):
    player = _player(bot)
    if player:
        print(player.queue_text().replace("**", "").replace("`", ""))


async def c_np(bot, args):
    player = _player(bot)
    if player:
        track = player.current
        print(f"Now playing: {track.title} [{fmt(track.duration)}]" if track else "Nothing is playing.")


async def c_volume(bot, args):
    player = _player(bot)
    if not player:
        return
    try:
        player.set_volume(int(args[0]))
        print(f"Volume: {args[0]}%")
    except (IndexError, ValueError):
        print("Usage: volume <0-200>")


async def c_loop(bot, args):
    player = _player(bot)
    if player:
        player.set_loop((args[0] if args else "track").lower())
        print(f"Loop: {player.loop_mode}")


async def c_shuffle(bot, args):
    player = _player(bot)
    if player:
        player.shuffle()
        print("Queue shuffled.")


async def c_remove(bot, args):
    player = _player(bot)
    if not player:
        return
    try:
        track = player.remove(int(args[0]))
        print(f"Removed {track.title}")
    except (IndexError, ValueError):
        print("Usage: remove <position>")


async def c_hq(bot, args):
    guild = need_guild(bot)
    if not guild:
        return
    voice = guild.voice_client
    if not voice or not voice.is_connected():
        print("Join a voice channel first.")
        return
    try:
        bitrate = int(guild.bitrate_limit)
        await voice.channel.edit(bitrate=bitrate)
        print(f"Channel bitrate set to {bitrate // 1000} kbps.")
    except discord.Forbidden:
        print("[!] The bot needs Manage Channels permission to change the bitrate.")


async def setup(bot):
    await bot.add_cog(Music(bot))
    add = bot.add_console
    add("join", c_join, "join <channel> [temp]", "join a voice channel and stay 24/7", group=GROUP)
    add("leave", c_leave, "leave", "bot leaves voice", group=GROUP)
    add("play", c_play, "play <name or link>", "search and play through Lavalink", aliases=("p",), group=GROUP)
    add("skip", c_skip, "skip", "skip the current track", group=GROUP)
    add("pause", c_pause, "pause", "pause playback", group=GROUP)
    add("resume", c_resume, "resume", "resume playback", group=GROUP)
    add("mstop", c_mstop, "mstop", "stop music and clear the queue", group=GROUP)
    add("queue", c_queue, "queue", "show the queue", aliases=("q",), group=GROUP)
    add("np", c_np, "np", "show the current track", group=GROUP)
    add("volume", c_volume, "volume <0-200>", "set volume", aliases=("vol",), group=GROUP)
    add("loop", c_loop, "loop off|track|queue", "set loop mode", group=GROUP)
    add("shuffle", c_shuffle, "shuffle", "shuffle the queue", group=GROUP)
    add("remove", c_remove, "remove <position>", "remove a queued track", group=GROUP)
    add("hq", c_hq, "hq", "set the voice-channel bitrate to the server maximum", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
