"""Join-to-Create voice rooms with a TempVoice-style interface.

Join the hub -> the bot builds you a private room, moves you in and posts a live "room card".
The panel (and /voice commands) give the room owner:
  NAME  LIMIT  PRIVACY  WAITING ROOM  CHAT  |  TRUST  UNTRUST  INVITE  KICK  REGION
  BLOCK  UNBLOCK  CLAIM  TRANSFER  DELETE   |  + BITRATE  INFO  SAVE
Trusted / blocked people and saved settings follow the owner into every future room.
"""
import asyncio
import re
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import data, save_data, has_access, is_owner, BotError
from ui import BaseCog, emb, BLURPLE, GREEN, RED, YELLOW, NO_PINGS

GROUP = "jtc"
VIOLET, NEON, ORANGE, PINK = 0x8B5CF6, 0x22D3EE, 0xF59E0B, 0xEC4899
PERM_MSG = ("I need **Manage Channels** and **Manage Roles** (room permissions), **Move Members**, "
            "**Connect**, **View Channel** and **Send Messages**.")
STATIC_REGIONS = [("Brazil", "brazil"), ("Hong Kong", "hongkong"), ("India", "india"), ("Japan", "japan"),
                  ("Rotterdam", "rotterdam"), ("Russia", "russia"), ("Singapore", "singapore"),
                  ("South Africa", "southafrica"), ("South Korea", "south-korea"), ("Sydney", "sydney"),
                  ("US Central", "us-central"), ("US East", "us-east"), ("US South", "us-south"), ("US West", "us-west")]


# ============================================================ data helpers
def rooms():
    return data["jtc"]["rooms"]


def waits():
    return data["jtc"]["waiting"]


def guild_cfg(guild_id):
    return data["jtc"]["guilds"].get(str(guild_id))


def profile(guild_id, user_id):
    p = data["jtc"]["profiles"].setdefault(f"{guild_id}:{user_id}", {})
    p.setdefault("trusted", [])
    p.setdefault("blocked", [])
    p.setdefault("prefs", {})
    return p


def allowed(user_id):
    return config.JTC_ALLOW_EVERYONE or has_access(user_id)


def clean_name(text):
    return re.sub(r"[\x00-\x1f\x7f]", "", text).strip()[:100]


def room_name(template, member, count):
    name = (template or "{user}'s room").replace("{user}", member.display_name).replace("{count}", str(count))
    return clean_name(name) or "Voice room"


def privacy_of(room):
    ow = room.overwrites_for(room.guild.default_role)
    return {"locked": ow.connect is False, "hidden": ow.view_channel is False, "chat_closed": ow.send_messages is False}


def member_target(guild, uid):
    return guild.get_member(uid) or discord.Object(id=uid, type=discord.Member)


# ============================================================ cog
class JoinToCreate(BaseCog):
    def __init__(self, bot):
        self.bot = bot
        self.cooldown = {}
        self.locks = {}
        self._regions = None
        self._regions_at = 0.0

    async def cog_load(self):
        self.bot.add_view(PanelView(self))          # panel buttons survive restarts

    async def cog_check(self, ctx):                 # /jtc admin commands
        return ctx.guild is not None and is_owner(ctx.author.id)

    # ---------- lookup ----------
    def room_info(self, channel_id):
        return rooms().get(str(channel_id))

    def owned_room(self, member):
        for cid, info in list(rooms().items()):
            if info["owner"] == member.id and info["guild"] == member.guild.id:
                ch = member.guild.get_channel(int(cid))
                if ch is not None:
                    return ch
                rooms().pop(cid, None)
                save_data()
        return None

    def count_rooms(self, guild):
        return sum(1 for i in rooms().values() if i["guild"] == guild.id)

    # ---------- lifecycle ----------
    @commands.Cog.listener()
    async def on_ready(self):
        for cid, info in list(rooms().items()):          # tidy up after a restart
            guild = self.bot.get_guild(info["guild"])
            ch = guild.get_channel(int(cid)) if guild else None
            if ch is None:
                rooms().pop(cid, None)
            elif not [m for m in ch.members if not m.bot]:
                await self.delete_room(ch)
        for wid, rid in list(waits().items()):
            if str(rid) not in rooms():
                waits().pop(wid, None)
        save_data()
        await self.refresh_panel_messages()

    async def refresh_panel_messages(self):
        """Update saved panels after a deploy so the layout stays current."""
        for guild_id, cfg in list(data["jtc"]["guilds"].items()):
            try:
                guild = self.bot.get_guild(int(guild_id))
                channel = guild.get_channel(int(cfg.get("panel_channel") or 0)) if guild else None
                message_id = int(cfg.get("panel_message") or 0)
                if channel is None or not message_id:
                    continue
                message = await channel.fetch_message(message_id)
                await message.edit(embed=self.panel_payload(), view=PanelView(self))
            except discord.NotFound:
                continue
            except (discord.HTTPException, TypeError, ValueError) as e:
                print(f"[jtc] couldn't refresh saved panel: {type(e).__name__}: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        changed = rooms().pop(str(channel.id), None) is not None
        rid = waits().pop(str(channel.id), None)
        if rid is not None:
            changed = True
            info = rooms().get(str(rid))
            if info:
                info["waiting"] = None
        if changed:
            save_data()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        cfg = guild_cfg(member.guild.id)
        if after.channel is not None and not member.bot:
            if cfg and after.channel.id == cfg["hub"]:
                await self.create_room(member, cfg)
            elif str(after.channel.id) in waits():
                await self.knock(member, after.channel)
        if before.channel is not None and str(before.channel.id) in rooms():
            await self.after_leave(before.channel)

    async def create_room(self, member, cfg):
        guild = member.guild
        if not allowed(member.id):
            return
        key = (guild.id, member.id)
        lock = self.locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            existing = self.owned_room(member)
            if existing is not None:                     # already has a room -> go back to it
                try:
                    await member.move_to(existing)
                except discord.HTTPException:
                    pass
                return
            now = time.monotonic()
            if now - self.cooldown.get(key, 0) < 2 or self.count_rooms(guild) >= config.JTC_MAX_ROOMS:
                return
            self.cooldown[key] = now

            prof = profile(guild.id, member.id)
            prefs = prof["prefs"]
            hub = guild.get_channel(cfg["hub"])
            category = guild.get_channel(cfg.get("category") or 0) or (hub.category if hub else None)
            kwargs = dict(name=clean_name(prefs.get("name") or "") or room_name(cfg.get("template"), member, self.count_rooms(guild) + 1),
                          category=category, reason=f"Join-to-create for {member}")
            limit = prefs.get("limit", cfg.get("limit"))
            if limit:
                kwargs["user_limit"] = int(limit)
            bitrate = prefs.get("bitrate", cfg.get("bitrate"))
            if bitrate:
                kwargs["bitrate"] = max(8000, min(int(bitrate) * 1000, int(guild.bitrate_limit)))
            if prefs.get("region"):
                kwargs["rtc_region"] = prefs["region"]

            overwrites = dict(category.overwrites) if category else {}
            everyone = overwrites.get(guild.default_role, discord.PermissionOverwrite())
            if prefs.get("locked"):
                everyone.update(connect=False)
            if prefs.get("hidden"):
                everyone.update(view_channel=False)
            if prefs.get("chat_closed"):
                everyone.update(send_messages=False)
                overwrites[guild.me] = discord.PermissionOverwrite(send_messages=True, embed_links=True)
            if not everyone.is_empty():
                overwrites[guild.default_role] = everyone
            for uid in prof["trusted"]:
                overwrites[member_target(guild, uid)] = discord.PermissionOverwrite(connect=True, view_channel=True, send_messages=True)
            for uid in prof["blocked"]:
                overwrites[member_target(guild, uid)] = discord.PermissionOverwrite(connect=False, view_channel=False, send_messages=False)
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True, send_messages=True)

            try:
                try:
                    ch = await guild.create_voice_channel(overwrites=overwrites, **kwargs)
                except discord.Forbidden:                # no Manage Roles -> create it without overrides
                    kwargs.pop("rtc_region", None)
                    ch = await guild.create_voice_channel(**kwargs)
            except discord.HTTPException as e:
                print(f"[jtc] couldn't create a room in {guild.name}: {e}")
                return
            rooms()[str(ch.id)] = {"guild": guild.id, "owner": member.id, "waiting": None, "card": None}
            save_data()
            try:
                await member.move_to(ch)
            except discord.HTTPException:                # they left the hub in the meantime
                await self.delete_room(ch)
                return
            await self.refresh_card(ch)

    async def after_leave(self, channel):
        info = self.room_info(channel.id)
        if not info:
            return
        humans = [m for m in channel.members if not m.bot]
        if not humans:
            await self.delete_room(channel)
            return
        if info["owner"] not in [m.id for m in humans]:   # owner left -> hand the room to someone inside
            new_owner = humans[0]
            old = info["owner"]
            info["owner"] = new_owner.id
            save_data()
            try:
                await channel.set_permissions(new_owner, connect=True, view_channel=True, send_messages=True, reason="JTC new owner")
            except discord.HTTPException:
                pass
            await self.refresh_card(channel)
            try:
                await channel.send(embed=emb(f"👑 {new_owner.mention} is now the owner of this room (<@{old}> left).", VIOLET),
                                   allowed_mentions=NO_PINGS)
            except (discord.HTTPException, AttributeError):
                pass

    async def delete_room(self, channel):
        info = rooms().pop(str(channel.id), None)
        if info and info.get("waiting"):
            await self._delete_waiting_channel(channel.guild, info["waiting"])
        save_data()
        try:
            await channel.delete(reason="Join-to-create: room is empty or deleted")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _delete_waiting_channel(self, guild, wid):
        waits().pop(str(wid), None)
        wch = guild.get_channel(wid)
        if wch is not None:
            try:
                await wch.delete(reason="Join-to-create: waiting room closed")
            except discord.HTTPException:
                pass

    # ---------- permission helpers ----------
    async def _perm(self, room, target, reason="JTC", **perms):
        try:
            await room.set_permissions(target, reason=reason, **perms)
        except discord.Forbidden:
            raise BotError(PERM_MSG)

    async def _drop_perm(self, room, uid):
        member = room.guild.get_member(uid)
        try:
            if member is not None:
                await room.set_permissions(member, overwrite=None, reason="JTC")
            else:                                         # person isn't cached/in the server: delete by raw ID
                await self.bot.http.delete_channel_permissions(room.id, uid, reason="JTC")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            raise BotError(PERM_MSG)

    def _owner_id(self, room):
        return self.room_info(room.id)["owner"]

    def _name_of(self, guild, uid):
        m = guild.get_member(uid)
        return m.display_name if m else f"User {uid}"

    # ---------- room actions (each returns text) ----------
    async def rename(self, room, name):
        name = clean_name(name)
        if not name:
            raise BotError("That name is empty.")
        try:
            await asyncio.wait_for(room.edit(name=name, reason="JTC rename"), timeout=8)
        except asyncio.TimeoutError:
            raise BotError("Discord rate-limits channel renames (about 2 per 10 minutes). Try again later.")
        except discord.Forbidden:
            raise BotError(PERM_MSG)
        return f"📝 Room renamed to **{name}**."

    async def set_limit(self, room, text):
        text = str(text).strip()
        if not text.isdigit() or not 0 <= int(text) <= 99:
            raise BotError("Enter a number from 0 to 99 (0 = no limit).")
        try:
            await room.edit(user_limit=int(text), reason="JTC limit")
        except discord.Forbidden:
            raise BotError(PERM_MSG)
        return f"👥 User limit: **{int(text) or 'none'}**."

    async def set_bitrate(self, room, text):
        top = int(room.guild.bitrate_limit) // 1000
        text = str(text).strip()
        if not text.isdigit() or not 8 <= int(text) <= top:
            raise BotError(f"Enter a bitrate from 8 to {top} kbps (your server's boost level sets the max).")
        try:
            await room.edit(bitrate=int(text) * 1000, reason="JTC bitrate")
        except discord.Forbidden:
            raise BotError(PERM_MSG)
        return f"🎚️ Audio quality: **{int(text)} kbps**."

    async def set_privacy(self, room, mode):
        everyone = room.guild.default_role
        owner = room.guild.get_member(self._owner_id(room))
        if owner:
            await self._perm(room, owner, "JTC privacy", connect=True, view_channel=True, send_messages=True)
        if mode == "lock":
            await self._perm(room, everyone, "JTC lock", connect=False)
            return "🔒 Room **locked** - only trusted or invited people can join."
        if mode == "unlock":
            await self._perm(room, everyone, "JTC unlock", connect=None)
            return "🔓 Room **unlocked** - anyone can join."
        if mode == "invisible":
            await self._perm(room, everyone, "JTC hide", view_channel=False)
            return "👻 Room is now **invisible** to everyone except you and trusted people."
        if mode == "visible":
            await self._perm(room, everyone, "JTC show", view_channel=None)
            return "👁️ Room is **visible** again."
        raise BotError("Unknown privacy mode.")

    async def set_chat(self, room, mode):
        everyone = room.guild.default_role
        if mode == "open":
            await self._perm(room, everyone, "JTC chat", send_messages=None)
            return "💬 Voice chat is **open** for everyone."
        if mode == "close":
            await self._perm(room, room.guild.me, "JTC chat", send_messages=True, embed_links=True)
            await self._perm(room, everyone, "JTC chat", send_messages=False)
            return "🔇 Voice chat is **closed** - only you and trusted people can write."
        if mode == "clear":
            try:
                deleted = await room.purge(limit=200)
            except discord.Forbidden:
                raise BotError("I need **Manage Messages** to clear the chat.")
            except discord.HTTPException:
                raise BotError("I couldn't clear the chat.")
            return f"🧹 Cleared **{len(deleted)}** message(s)."
        raise BotError("Unknown chat option.")

    async def toggle_waiting(self, room):
        info = self.room_info(room.id)
        wid = info.get("waiting")
        if wid and room.guild.get_channel(wid) is not None:
            await self._delete_waiting_channel(room.guild, wid)
            info["waiting"] = None
            save_data()
            return "⏳ Waiting room **closed**."
        try:
            wch = await room.guild.create_voice_channel(
                name=clean_name(f"⏳ Waiting · {room.name}"), category=room.category, reason="JTC waiting room")
        except discord.Forbidden:
            raise BotError(PERM_MSG)
        info["waiting"] = wch.id
        waits()[str(wch.id)] = room.id
        save_data()
        extra = ""
        if not privacy_of(room)["locked"]:
            await self.set_privacy(room, "lock")
            extra = " Your room is now **locked**."
        return (f"⏳ Waiting room **opened** (**{wch.name}**).{extra}\n"
                "When someone joins it, you get a prompt in this room's chat to let them in.")

    async def trust(self, room, member):
        owner = self._owner_id(room)
        if member.bot or member.id == owner:
            raise BotError("Pick another person.")
        p = profile(room.guild.id, owner)
        if member.id in p["blocked"]:
            p["blocked"].remove(member.id)
        if member.id not in p["trusted"]:
            p["trusted"].append(member.id)
        save_data()
        await self._perm(room, member, "JTC trust", connect=True, view_channel=True, send_messages=True)
        return f"🤝 **{member.display_name}** is trusted - they can always join your rooms, even locked or hidden."

    async def untrust(self, room, uid):
        uid = int(uid)
        p = profile(room.guild.id, self._owner_id(room))
        if uid not in p["trusted"]:
            raise BotError("That person isn't on your trusted list.")
        p["trusted"].remove(uid)
        save_data()
        await self._drop_perm(room, uid)
        return f"🙅 **{self._name_of(room.guild, uid)}** is no longer trusted."

    async def invite(self, room, member):
        if member.bot:
            raise BotError("You can't invite a bot.")
        await self._perm(room, member, "JTC invite", connect=True, view_channel=True)
        link = room.jump_url
        embed = discord.Embed(title="📨 You're invited!", color=VIOLET,
                              description=f"**{self._name_of(room.guild, self._owner_id(room))}** invited you to **{room.name}** in **{room.guild.name}**.")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Join the room", style=discord.ButtonStyle.link, url=link))
        try:
            await member.send(embed=embed, view=view)
            return f"📨 Invitation sent to **{member.display_name}** (they can join even if the room is locked)."
        except discord.HTTPException:
            try:
                await room.send(content=member.mention, embed=embed, view=view,
                                allowed_mentions=discord.AllowedMentions(users=[member]))
            except (discord.HTTPException, AttributeError):
                pass
            return f"📨 **{member.display_name}** has DMs closed, so I pinged them in the room chat."

    async def kick(self, room, member):
        if member.id == self._owner_id(room):
            raise BotError("You can't kick yourself out of your own room.")
        if member.voice is None or member.voice.channel != room:
            raise BotError(f"**{member.display_name}** isn't in your room.")
        await member.move_to(None, reason="JTC kick")
        return f"👢 **{member.display_name}** was removed from the room."

    async def block(self, room, member):
        owner = self._owner_id(room)
        if member.id == owner or member.bot:
            raise BotError("You can't block that account.")
        p = profile(room.guild.id, owner)
        if member.id in p["trusted"]:
            p["trusted"].remove(member.id)
        if member.id not in p["blocked"]:
            p["blocked"].append(member.id)
        save_data()
        await self._perm(room, member, "JTC block", connect=False, view_channel=False, send_messages=False)
        if member.voice and member.voice.channel is not None:
            wid = self.room_info(room.id).get("waiting")
            if member.voice.channel == room or member.voice.channel.id == wid:
                await member.move_to(None, reason="JTC block")
        return f"⛔ **{member.display_name}** is blocked from your rooms (now and in future rooms)."

    async def unblock(self, room, uid):
        uid = int(uid)
        p = profile(room.guild.id, self._owner_id(room))
        if uid not in p["blocked"]:
            raise BotError("That person isn't on your block list.")
        p["blocked"].remove(uid)
        save_data()
        await self._drop_perm(room, uid)
        return f"✅ **{self._name_of(room.guild, uid)}** is unblocked."

    async def regions(self):
        if self._regions and time.monotonic() - self._regions_at < 3600:
            return self._regions
        try:
            raw = await asyncio.wait_for(self.bot.http.request(discord.http.Route("GET", "/voice/regions")), 5)
            regs = sorted((r["name"], r["id"]) for r in raw if not r.get("deprecated") and not r.get("custom"))
        except Exception:
            regs = []
        self._regions = regs or STATIC_REGIONS
        self._regions_at = time.monotonic()
        return self._regions

    async def set_region(self, room, region):
        region = None if region in (None, "auto", "") else region
        try:
            await room.edit(rtc_region=region, reason="JTC region")
        except discord.Forbidden:
            raise BotError(PERM_MSG)
        except discord.HTTPException:
            raise BotError("Discord didn't accept that region.")
        return f"🌍 Region: **{region or 'Automatic'}** (people may reconnect for a second)."

    async def claim(self, user):
        room = user.voice.channel if user.voice else None
        info = self.room_info(room.id) if room else None
        if not info:
            raise BotError("Join the temporary room you want to claim first.")
        if info["owner"] in [m.id for m in room.members]:
            raise BotError("The owner is still in the room.")
        if self.owned_room(user) is not None:
            raise BotError("You already own another room.")
        info["owner"] = user.id
        save_data()
        await self._perm(room, user, "JTC claim", connect=True, view_channel=True, send_messages=True)
        return room, f"👑 You are now the owner of **{room.name}**."

    async def transfer(self, room, member):
        info = self.room_info(room.id)
        if member.bot or member.id == info["owner"]:
            raise BotError("Pick another person.")
        if member.voice is None or member.voice.channel != room:
            raise BotError(f"**{member.display_name}** has to be in the room first.")
        old = info["owner"]
        info["owner"] = member.id
        save_data()
        await self._perm(room, member, "JTC transfer", connect=True, view_channel=True, send_messages=True)
        try:
            await self._drop_perm(room, old)
        except BotError:
            pass
        return f"🔁 **{member.display_name}** is now the room owner."

    async def save_prefs(self, room):
        p = profile(room.guild.id, self._owner_id(room))
        pv = privacy_of(room)
        p["prefs"] = {"name": room.name, "limit": room.user_limit, "bitrate": room.bitrate // 1000,
                      "region": room.rtc_region, "locked": pv["locked"], "hidden": pv["hidden"],
                      "chat_closed": pv["chat_closed"]}
        save_data()
        return "💾 Saved! Your next room will start with these settings, trusted list and block list."

    async def reset_prefs(self, room):
        profile(room.guild.id, self._owner_id(room))["prefs"] = {}
        save_data()
        return "♻️ Saved room settings cleared (your trusted and block lists are kept)."

    # ---------- knock (waiting room) ----------
    async def knock(self, member, wait_ch):
        guild = member.guild
        room_id = waits().get(str(wait_ch.id))
        info = rooms().get(str(room_id)) if room_id else None
        room = guild.get_channel(room_id) if room_id else None
        if not info or room is None:                      # stale waiting channel
            await self._delete_waiting_channel(guild, wait_ch.id)
            return
        p = profile(guild.id, info["owner"])
        if member.id == info["owner"] or member.id in p["trusted"]:
            try:
                await member.move_to(room)
            except discord.HTTPException:
                pass
            return
        if member.id in p["blocked"]:
            try:
                await member.move_to(None)
            except discord.HTTPException:
                pass
            return
        embed = discord.Embed(title="🚪 Someone is knocking", color=ORANGE,
                              description=f"{member.mention} is waiting to join **{room.name}**.")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Only the room owner can answer.")
        try:
            await room.send(content=f"<@{info['owner']}>", embed=embed, view=KnockView(self, room.id, member.id),
                            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=info["owner"])]))
        except (discord.HTTPException, AttributeError):
            pass

    async def let_in(self, room, member):
        await self._perm(room, member, "JTC let in", connect=True, view_channel=True)
        if member.voice and member.voice.channel is not None:
            await member.move_to(room)
        return f"✅ **{member.display_name}** was let in."

    # ---------- cards ----------
    def room_card(self, room):
        info = self.room_info(room.id) or {}
        owner = room.guild.get_member(info.get("owner", 0))
        prefs = profile(room.guild.id, info.get("owner", 0))
        pv = privacy_of(room)
        color = PINK if pv["hidden"] else ORANGE if pv["locked"] else NEON
        privacy = "👻 Hidden" if pv["hidden"] else "🔒 Locked" if pv["locked"] else "🔓 Public"
        members = len([m for m in room.members if not m.bot])
        limit = str(room.user_limit) if room.user_limit else "∞"
        region = str(room.rtc_region or "Automatic").replace("_", " ").title()
        chat = "Closed" if pv["chat_closed"] else "Open"
        waiting = "On" if info.get("waiting") else "Off"

        e = discord.Embed(
            title=f"🎙️ {room.name}",
            description=(
                f"👑 **Owner:** {owner.mention if owner else 'Unknown'}\n"
                f"{privacy}  ·  👥 **{members}/{limit}** members\n"
                f"🎚️ **{room.bitrate // 1000} kbps**  ·  🌍 **{region}**\n"
                f"💬 **Chat {chat}**  ·  ⏳ **Waiting {waiting}**"
            ),
            color=color,
        )
        e.add_field(
            name="Room access",
            value=f"🤝 **{len(prefs['trusted'])} trusted**  ·  ⛔ **{len(prefs['blocked'])} blocked**",
            inline=False,
        )
        if owner:
            e.set_thumbnail(url=owner.display_avatar.url)
        e.set_footer(text="Use the controls above or /voice")
        return e

    async def refresh_card(self, room):
        info = self.room_info(room.id)
        if not info:
            return
        embed = self.room_card(room)
        try:
            mid = info.get("card")
            if mid:
                try:
                    await (await room.fetch_message(mid)).edit(embed=embed)
                    return
                except discord.NotFound:
                    pass
            msg = await room.send(embed=embed)
            info["card"] = msg.id
            save_data()
        except (discord.HTTPException, AttributeError):
            pass

    # ---------- admin: setup / settings ----------
    def panel_payload(self):
        e = discord.Embed(
            title="🎙️ Temp Voice Controls",
            color=VIOLET,
            description="Join **➕ Join to Create** to open your room, then choose an icon below.\n"
                        "Use **`/voice`** for the full command list.",
        )
        e.add_field(
            name="🏠 Room",
            value="📝 **Name** · 👥 **Limit** · 🔐 **Privacy** · ⏳ **Waiting** · 💬 **Chat** · 🎚️ **Bitrate** · 🌍 **Region**",
            inline=False,
        )
        e.add_field(
            name="👥 Members",
            value="🤝 **Trust** · 🙅 **Untrust** · 📨 **Invite** · 👢 **Kick** · ⛔ **Block** · ✅ **Unblock**",
            inline=False,
        )
        e.add_field(
            name="👑 Owner",
            value="👑 **Claim** · 🔁 **Transfer** · ℹ️ **Info** · 💾 **Save** · 🗑️ **Delete**",
            inline=False,
        )
        if self.bot.user:
            e.set_thumbnail(url=self.bot.user.display_avatar.url)
        e.set_footer(text="The icons below match this guide")
        return e

    async def post_panel(self, guild, channel, cfg):
        embed = self.panel_payload()
        old_id = cfg.get("panel_message")
        old_ch = guild.get_channel(cfg.get("panel_channel") or 0)
        if old_id and old_ch:
            try:
                old_msg = await old_ch.fetch_message(old_id)
                if old_ch.id == channel.id:
                    await old_msg.edit(embed=embed, view=PanelView(self))
                    msg = old_msg
                else:
                    await old_msg.delete()
                    msg = await channel.send(embed=embed, view=PanelView(self))
            except discord.NotFound:
                msg = await channel.send(embed=embed, view=PanelView(self))
            except discord.HTTPException:
                msg = await channel.send(embed=embed, view=PanelView(self))
        else:
            msg = await channel.send(embed=embed, view=PanelView(self))
        cfg["panel_channel"], cfg["panel_message"] = channel.id, msg.id
        save_data()
        return msg

    def status_embed(self, guild):
        cfg = guild_cfg(guild.id)
        if not cfg:
            return emb("Join-to-create isn't set up here. Run `/jtc setup`.", YELLOW, "🎙️ Join-to-Create")
        template = cfg.get("template") or "{user}'s room"
        e = discord.Embed(title="🎙️ Join-to-Create", color=VIOLET)
        e.add_field(name="Hub", value=f"<#{cfg['hub']}>")
        e.add_field(name="Panel", value=f"<#{cfg['panel_channel']}>" if cfg.get("panel_channel") else "—")
        e.add_field(name="Live rooms", value=str(self.count_rooms(guild)))
        e.add_field(name="Name template", value=f"`{template}`")
        e.add_field(name="Default limit", value=str(cfg.get("limit") or "none"))
        e.add_field(name="Default bitrate", value=f"{cfg['bitrate']} kbps" if cfg.get("bitrate") else "channel default")
        e.set_footer(text="Who can create rooms: " + ("everyone" if config.JTC_ALLOW_EVERYONE else "owner + trusted only"))
        return e

    @commands.hybrid_group(name="jtc", description="Join-to-create voice rooms (admin)")
    async def jtc(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=emb("`/jtc setup` • `/jtc settings` • `/jtc status` • `/jtc panel` • `/jtc disable`"))

    @jtc.command(name="setup", description="Create (or connect) the hub channel and post the control panel")
    @app_commands.describe(hub="Existing hub voice channel (leave empty to create one)",
                           category="Category for hub + rooms (leave empty to create one)",
                           panel_channel="Text channel for the panel (leave empty to create one)")
    async def setup_cmd(self, ctx, hub: discord.VoiceChannel = None, category: discord.CategoryChannel = None,
                        panel_channel: discord.TextChannel = None):
        guild = ctx.guild
        me = guild.me.guild_permissions
        if not (me.manage_channels and me.move_members):
            raise BotError(PERM_MSG)
        async with ctx.typing():
            try:
                category = category or (hub.category if hub and hub.category else None) \
                    or await guild.create_category("🎙️ Voice Hub", reason="JTC setup")
                hub = hub or await guild.create_voice_channel("➕ Join to Create", category=category, reason="JTC setup")
                if panel_channel is None:
                    panel_channel = await guild.create_text_channel(
                        "🎛️-voice-controls", category=category, reason="JTC setup",
                        overwrites={guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False)})
            except discord.Forbidden:
                raise BotError(PERM_MSG)
            cfg = guild_cfg(guild.id) or {}
            cfg.update(hub=hub.id, category=category.id, template=cfg.get("template") or "{user}'s room",
                       limit=cfg.get("limit", 0), bitrate=cfg.get("bitrate", 0))
            data["jtc"]["guilds"][str(guild.id)] = cfg
            await self.post_panel(guild, panel_channel, cfg)
        await ctx.send(embed=emb(f"✅ Ready! Join {hub.mention} to create a room. Panel: {panel_channel.mention}\n"
                                 "Use the buttons below to tweak the defaults.", GREEN, "🎙️ Join-to-Create"),
                       view=SettingsView(self, guild.id, ctx.author.id))

    @jtc.command(name="settings", description="Open the settings interface (name template, limit, bitrate...)")
    async def settings_cmd(self, ctx):
        if not guild_cfg(ctx.guild.id):
            raise BotError("Run `/jtc setup` first.")
        await ctx.send(embed=self.status_embed(ctx.guild), view=SettingsView(self, ctx.guild.id, ctx.author.id))

    @jtc.command(name="status", description="Show the join-to-create configuration")
    async def status_cmd(self, ctx):
        await ctx.send(embed=self.status_embed(ctx.guild))

    @jtc.command(name="panel", description="Re-post the control panel")
    @app_commands.describe(channel="Where to post it (default: where it was before)")
    async def panel_cmd(self, ctx, channel: discord.TextChannel = None):
        cfg = guild_cfg(ctx.guild.id)
        if not cfg:
            raise BotError("Run `/jtc setup` first.")
        channel = channel or ctx.guild.get_channel(cfg.get("panel_channel") or 0) or ctx.channel
        msg = await self.post_panel(ctx.guild, channel, cfg)
        await ctx.send(embed=emb(f"✅ Panel posted: {msg.jump_url}", GREEN))

    @jtc.command(name="disable", description="Turn join-to-create off for this server")
    @app_commands.describe(delete_hub="Also delete the hub channel")
    async def disable_cmd(self, ctx, delete_hub: bool = False):
        cfg = data["jtc"]["guilds"].pop(str(ctx.guild.id), None)
        if not cfg:
            raise BotError("It isn't set up here.")
        save_data()
        if delete_hub:
            ch = ctx.guild.get_channel(cfg["hub"])
            if ch:
                try:
                    await ch.delete(reason="JTC disabled")
                except discord.HTTPException:
                    pass
        await ctx.send(embed=emb("🛑 Join-to-create disabled. Existing rooms stay until they're empty.", YELLOW))


# ============================================================ /voice commands (room owners)
class VoiceCommands(BaseCog):
    """Everything the panel does, as slash/text commands. Acts on the caller's own room."""

    async def cog_check(self, ctx):
        return ctx.guild is not None and allowed(ctx.author.id)

    def _jtc(self):
        cog = self.bot.get_cog("JoinToCreate")
        if cog is None:
            raise BotError("Voice rooms aren't loaded.")
        return cog

    def _room(self, ctx):
        room = self._jtc().owned_room(ctx.author)
        if room is None:
            raise BotError("You don't own a room right now. Join **➕ Join to Create** first.")
        return room

    async def _done(self, ctx, room, text, color=GREEN):
        await self._jtc().refresh_card(room)
        await ctx.send(embed=emb(text, color), ephemeral=True)

    @commands.hybrid_group(name="voice", description="Manage your temporary voice room")
    async def voice(self, ctx):
        if ctx.invoked_subcommand is None:
            e = discord.Embed(title="🎙️ /voice commands", color=VIOLET,
                              description="Use these in your own temporary room:")
            e.add_field(name="Look & access", inline=False,
                        value="`name` `limit` `privacy` `waiting` `chat` `bitrate` `region`")
            e.add_field(name="People", inline=False,
                        value="`trust` `untrust` `invite` `kick` `block` `unblock` `transfer` `claim`")
            e.add_field(name="Other", inline=False, value="`info` `save` `reset` `delete`")
            await ctx.send(embed=e, ephemeral=True)

    @voice.command(name="name", description="Rename your room")
    @app_commands.describe(name="New room name")
    async def v_name(self, ctx, *, name: str):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().rename(room, name))

    @voice.command(name="limit", description="Set the user limit (0 = no limit)")
    @app_commands.describe(users="0-99")
    async def v_limit(self, ctx, users: int):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().set_limit(room, users))

    @voice.command(name="privacy", description="Lock, unlock, hide or show your room")
    @app_commands.describe(mode="What to do")
    async def v_privacy(self, ctx, mode: Literal["lock", "unlock", "invisible", "visible"]):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().set_privacy(room, mode))

    @voice.command(name="waiting", description="Turn the waiting room on or off")
    async def v_waiting(self, ctx):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().toggle_waiting(room))

    @voice.command(name="chat", description="Open, close or clear the room's text chat")
    @app_commands.describe(mode="What to do")
    async def v_chat(self, ctx, mode: Literal["open", "close", "clear"]):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().set_chat(room, mode))

    @voice.command(name="bitrate", description="Set the audio quality in kbps")
    @app_commands.describe(kbps="8 up to your server's maximum")
    async def v_bitrate(self, ctx, kbps: int):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().set_bitrate(room, kbps))

    @voice.command(name="region", description="Set the voice region (or 'auto')")
    @app_commands.describe(region="Region name, or auto")
    async def v_region(self, ctx, region: str = "auto"):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().set_region(room, region))

    @v_region.autocomplete("region")
    async def v_region_auto(self, interaction, current: str):
        regs = [("Automatic", "auto")] + list(await self._jtc().regions())
        cur = current.lower()
        return [app_commands.Choice(name=n, value=v) for n, v in regs if cur in n.lower() or cur in v.lower()][:25]

    @voice.command(name="trust", description="Always let someone in (even when locked or hidden)")
    @app_commands.describe(user="Who to trust")
    async def v_trust(self, ctx, user: discord.Member):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().trust(room, user))

    @voice.command(name="untrust", description="Remove someone from your trusted list")
    @app_commands.describe(user="Who to remove")
    async def v_untrust(self, ctx, user: discord.User):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().untrust(room, user.id))

    @voice.command(name="invite", description="Invite someone to your room (sends them a DM)")
    @app_commands.describe(user="Who to invite")
    async def v_invite(self, ctx, user: discord.Member):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().invite(room, user))

    @voice.command(name="kick", description="Remove someone from your room")
    @app_commands.describe(user="Who to remove")
    async def v_kick(self, ctx, user: discord.Member):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().kick(room, user))

    @voice.command(name="block", description="Block someone from your rooms")
    @app_commands.describe(user="Who to block")
    async def v_block(self, ctx, user: discord.Member):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().block(room, user))

    @voice.command(name="unblock", description="Unblock someone")
    @app_commands.describe(user="Who to unblock")
    async def v_unblock(self, ctx, user: discord.User):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().unblock(room, user.id))

    @voice.command(name="transfer", description="Give your room to someone inside it")
    @app_commands.describe(user="The new owner")
    async def v_transfer(self, ctx, user: discord.Member):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().transfer(room, user))

    @voice.command(name="claim", description="Claim the room you're in when its owner has left")
    async def v_claim(self, ctx):
        room, text = await self._jtc().claim(ctx.author)
        await self._done(ctx, room, text)

    @voice.command(name="info", description="Show your room's card")
    async def v_info(self, ctx):
        room = self._room(ctx)
        await ctx.send(embed=self._jtc().room_card(room), ephemeral=True)

    @voice.command(name="save", description="Save your room settings for next time")
    async def v_save(self, ctx):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().save_prefs(room))

    @voice.command(name="reset", description="Forget your saved room settings")
    async def v_reset(self, ctx):
        room = self._room(ctx)
        await self._done(ctx, room, await self._jtc().reset_prefs(room))

    @voice.command(name="delete", description="Delete your room")
    async def v_delete(self, ctx):
        room = self._room(ctx)
        await self._jtc().delete_room(room)
        await ctx.send(embed=emb("🗑️ Room deleted.", GREEN), ephemeral=True)


# ============================================================ interface UI
class ValueModal(discord.ui.Modal):
    def __init__(self, title, label, callback, default="", placeholder=""):
        super().__init__(title=title)
        self.field = discord.ui.TextInput(label=label, default=default or None, placeholder=placeholder or None,
                                          max_length=100, required=True)
        self.add_item(self.field)
        self._cb = callback

    async def on_submit(self, interaction):
        try:
            embed = emb(await self._cb(interaction, self.field.value), GREEN)
        except BotError as e:
            embed = emb(str(e), RED)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction, error):
        print(f"[jtc] modal error: {type(error).__name__}: {error}")
        try:
            await interaction.response.send_message(embed=emb("❌ Something went wrong.", RED), ephemeral=True)
        except discord.HTTPException:
            pass


class _OwnerView(discord.ui.View):
    """Ephemeral helper views: only the room owner can use them."""

    def __init__(self, cog, owner_id, room_id):
        super().__init__(timeout=90)
        self.cog, self.owner_id, self.room_id = cog, owner_id, room_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(embed=emb("This menu isn't yours.", RED), ephemeral=True)
            return False
        return True

    async def _apply(self, interaction, fn):
        room = interaction.guild.get_channel(self.room_id)
        try:
            info = self.cog.room_info(self.room_id)
            if room is None or info is None or info["owner"] != interaction.user.id:
                raise BotError("You no longer own that room.")
            embed = emb(await fn(room), GREEN)
        except BotError as e:
            embed = emb(str(e), RED)
        except discord.HTTPException:
            embed = emb("Discord refused that action.", RED)
        await interaction.response.edit_message(embed=embed, view=None)
        if room is not None:
            await self.cog.refresh_card(room)
        self.stop()


class UserPick(_OwnerView):
    """'Pick a person' dropdown (trust / invite / kick / block / transfer)."""

    def __init__(self, cog, owner_id, room_id, action):
        super().__init__(cog, owner_id, room_id)
        self.action = action
        sel = discord.ui.UserSelect(placeholder="Pick a person…", min_values=1, max_values=1)
        sel.callback = self.picked
        self.add_item(sel)

    async def picked(self, interaction):
        member = interaction.guild.get_member(self.children[0].values[0].id)

        async def go(room):
            if member is None:
                raise BotError("That person isn't in this server.")
            return await self.action(room, member)
        await self._apply(interaction, go)


class ChoiceView(_OwnerView):
    """Dropdown of fixed choices (privacy / chat / region / saved lists)."""

    def __init__(self, cog, owner_id, room_id, options, action, placeholder):
        super().__init__(cog, owner_id, room_id)
        self.action = action
        sel = discord.ui.Select(
            placeholder=placeholder, min_values=1, max_values=1,
            options=[discord.SelectOption(label=l[:100], value=v, emoji=e, description=(d or None)) for l, v, e, d in options[:25]])
        sel.callback = self.picked
        self.add_item(sel)

    async def picked(self, interaction):
        value = self.children[0].values[0]
        await self._apply(interaction, lambda room: self.action(room, value))


class ConfirmDelete(_OwnerView):
    @discord.ui.button(label="Yes, delete it", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def yes(self, interaction, button):
        async def go(room):
            await self.cog.delete_room(room)
            return "🗑️ Room deleted."
        await self._apply(interaction, go)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        await interaction.response.edit_message(embed=emb("Cancelled.", RED), view=None)
        self.stop()


class KnockView(discord.ui.View):
    """Shown in the room's chat when someone joins the waiting room."""

    def __init__(self, cog, room_id, member_id):
        super().__init__(timeout=300)
        self.cog, self.room_id, self.member_id = cog, room_id, member_id

    async def interaction_check(self, interaction):
        info = self.cog.room_info(self.room_id)
        if not info or info["owner"] != interaction.user.id:
            await interaction.response.send_message(embed=emb("Only the room owner can answer.", RED), ephemeral=True)
            return False
        return True

    async def _answer(self, interaction, fn, title, color):
        room = interaction.guild.get_channel(self.room_id)
        member = interaction.guild.get_member(self.member_id)
        try:
            if room is None or member is None:
                raise BotError("They already left.")
            text = await fn(room, member)
            embed = emb(text, color, title)
        except BotError as e:
            embed = emb(str(e), RED)
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    @discord.ui.button(label="Let in", emoji="✅", style=discord.ButtonStyle.success)
    async def b_in(self, interaction, button):
        await self._answer(interaction, self.cog.let_in, "🚪 Door opened", GREEN)

    @discord.ui.button(label="Deny", emoji="❌", style=discord.ButtonStyle.secondary)
    async def b_deny(self, interaction, button):
        async def deny(room, member):
            if member.voice and member.voice.channel is not None:
                await member.move_to(None)
            return f"❌ **{member.display_name}** was turned away."
        await self._answer(interaction, deny, "🚪 Door closed", ORANGE)

    @discord.ui.button(label="Block", emoji="⛔", style=discord.ButtonStyle.danger)
    async def b_block(self, interaction, button):
        await self._answer(interaction, self.cog.block, "⛔ Blocked", RED)


class PanelView(discord.ui.View):
    """The persistent TempVoice-style interface. Every button acts on the clicker's own room."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction):
        if interaction.guild is None or not allowed(interaction.user.id):
            await interaction.response.send_message(
                embed=emb("🔒 Voice rooms are limited to the owner and trusted users.", RED), ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        print(f"[jtc] panel error: {type(error).__name__}: {error}")
        try:
            msg = emb("❌ Something went wrong.", RED)
            if interaction.response.is_done():
                await interaction.followup.send(embed=msg, ephemeral=True)
            else:
                await interaction.response.send_message(embed=msg, ephemeral=True)
        except discord.HTTPException:
            pass

    # ----- helpers -----
    async def _room(self, interaction):
        room = self.cog.owned_room(interaction.user)
        if room is None:
            await interaction.response.send_message(
                embed=emb("You don't own a room right now. Join **➕ Join to Create** first.", YELLOW), ephemeral=True)
        return room

    async def _quick(self, interaction, fn):
        room = await self._room(interaction)
        if room is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            embed = emb(await fn(room), GREEN)
        except BotError as e:
            embed = emb(str(e), RED)
        except discord.HTTPException:
            embed = emb("Discord refused that action.", RED)
        await interaction.followup.send(embed=embed, ephemeral=True)
        await self.cog.refresh_card(room)

    async def _pick_user(self, interaction, action, text):
        room = await self._room(interaction)
        if room is not None:
            await interaction.response.send_message(embed=emb(text, BLURPLE), ephemeral=True,
                                                    view=UserPick(self.cog, interaction.user.id, room.id, action))

    async def _choice(self, interaction, options, action, placeholder, text):
        room = await self._room(interaction)
        if room is not None:
            await interaction.response.send_message(
                embed=emb(text, BLURPLE), ephemeral=True,
                view=ChoiceView(self.cog, interaction.user.id, room.id, options, action, placeholder))

    async def _saved_list(self, interaction, key, action, empty, text):
        room = await self._room(interaction)
        if room is None:
            return
        ids = profile(room.guild.id, self.cog._owner_id(room))[key]
        if not ids:
            return await interaction.response.send_message(embed=emb(empty, YELLOW), ephemeral=True)
        opts = [(self.cog._name_of(room.guild, uid), str(uid), None, f"ID {uid}") for uid in ids]
        await interaction.response.send_message(
            embed=emb(text, BLURPLE), ephemeral=True,
            view=ChoiceView(self.cog, interaction.user.id, room.id, opts, action, "Pick someone…"))

    async def _modal(self, interaction, title, label, apply, default=""):
        room = await self._room(interaction)
        if room is None:
            return

        async def cb(inter, value):
            current = self.cog.owned_room(inter.user)
            if current is None:
                raise BotError("You no longer own a room.")
            text = await apply(current, value)
            await self.cog.refresh_card(current)
            return text
        await interaction.response.send_modal(ValueModal(title, label, cb, default=default))

    # ----- row 1 -----
    @discord.ui.button(emoji="📝", style=discord.ButtonStyle.primary, custom_id="jtc:name", row=0)
    async def b_name(self, interaction, button):
        room = self.cog.owned_room(interaction.user)
        await self._modal(interaction, "Rename your room", "New name", self.cog.rename, default=room.name if room else "")

    @discord.ui.button(emoji="👥", style=discord.ButtonStyle.primary, custom_id="jtc:limit", row=0)
    async def b_limit(self, interaction, button):
        await self._modal(interaction, "User limit", "Max people (0 = unlimited)", self.cog.set_limit)

    @discord.ui.button(emoji="🔐", style=discord.ButtonStyle.primary, custom_id="jtc:privacy", row=0)
    async def b_privacy(self, interaction, button):
        opts = [("Unlock", "unlock", "🔓", "Anyone can join"), ("Lock", "lock", "🔒", "Only trusted or invited people can join"),
                ("Invisible", "invisible", "👻", "Hide the room from everyone else"), ("Visible", "visible", "👁️", "Show the room again")]
        await self._choice(interaction, opts, self.cog.set_privacy, "Choose privacy…", "🔐 **Room privacy**")

    @discord.ui.button(emoji="⏳", style=discord.ButtonStyle.primary, custom_id="jtc:waiting", row=0)
    async def b_waiting(self, interaction, button):
        await self._quick(interaction, self.cog.toggle_waiting)

    @discord.ui.button(emoji="💬", style=discord.ButtonStyle.primary, custom_id="jtc:chat", row=0)
    async def b_chat(self, interaction, button):
        opts = [("Open chat", "open", "💬", "Everyone can write"), ("Close chat", "close", "🔇", "Only you and trusted people"),
                ("Clear chat", "clear", "🧹", "Delete the latest messages")]
        await self._choice(interaction, opts, self.cog.set_chat, "Choose a chat option…", "💬 **Voice chat**")

    # ----- row 2 -----
    @discord.ui.button(emoji="🤝", style=discord.ButtonStyle.success, custom_id="jtc:trust", row=1)
    async def b_trust(self, interaction, button):
        await self._pick_user(interaction, self.cog.trust, "🤝 **Who do you trust?** They can always join your rooms.")

    @discord.ui.button(emoji="🙅", style=discord.ButtonStyle.secondary, custom_id="jtc:untrust", row=1)
    async def b_untrust(self, interaction, button):
        await self._saved_list(interaction, "trusted", self.cog.untrust, "Your trusted list is empty.", "🙅 **Remove someone from your trusted list**")

    @discord.ui.button(emoji="📨", style=discord.ButtonStyle.success, custom_id="jtc:invite", row=1)
    async def b_invite(self, interaction, button):
        await self._pick_user(interaction, self.cog.invite, "📨 **Who do you want to invite?** I'll send them a DM.")

    @discord.ui.button(emoji="👢", style=discord.ButtonStyle.danger, custom_id="jtc:kick", row=1)
    async def b_kick(self, interaction, button):
        await self._pick_user(interaction, self.cog.kick, "👢 **Who should I remove from your room?**")

    @discord.ui.button(emoji="🌍", style=discord.ButtonStyle.primary, custom_id="jtc:region", row=1)
    async def b_region(self, interaction, button):
        regs = [("Automatic", "auto", "🌐", "Let Discord choose the best server")]
        regs += [(n, v, "🌍", None) for n, v in await self.cog.regions()][:24]
        await self._choice(interaction, regs, self.cog.set_region, "Choose a region…", "🌍 **Voice region**")

    # ----- row 3 -----
    @discord.ui.button(emoji="⛔", style=discord.ButtonStyle.danger, custom_id="jtc:block", row=2)
    async def b_block(self, interaction, button):
        await self._pick_user(interaction, self.cog.block, "⛔ **Who should be blocked?** They can't join your rooms.")

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.secondary, custom_id="jtc:unblock", row=2)
    async def b_unblock(self, interaction, button):
        await self._saved_list(interaction, "blocked", self.cog.unblock, "Your block list is empty.", "✅ **Unblock someone**")

    @discord.ui.button(emoji="👑", style=discord.ButtonStyle.success, custom_id="jtc:claim", row=2)
    async def b_claim(self, interaction, button):
        try:
            room, text = await self.cog.claim(interaction.user)
            embed = emb(text, GREEN)
        except BotError as e:
            room, embed = None, emb(str(e), RED)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        if room is not None:
            await self.cog.refresh_card(room)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.primary, custom_id="jtc:transfer", row=2)
    async def b_transfer(self, interaction, button):
        await self._pick_user(interaction, self.cog.transfer, "🔁 **Who becomes the new owner?** They must be in your room.")

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="jtc:delete", row=2)
    async def b_delete(self, interaction, button):
        room = await self._room(interaction)
        if room is not None:
            await interaction.response.send_message(
                embed=emb(f"Delete **{room.name}**? Everyone inside will be disconnected.", YELLOW, "⚠️ Are you sure?"),
                ephemeral=True, view=ConfirmDelete(self.cog, interaction.user.id, room.id))

    # ----- row 4 (extras) -----
    @discord.ui.button(emoji="🎚️", style=discord.ButtonStyle.secondary, custom_id="jtc:bitrate", row=3)
    async def b_bitrate(self, interaction, button):
        await self._modal(interaction, "Audio quality", "Bitrate in kbps (e.g. 64, 96, 128)", self.cog.set_bitrate)

    @discord.ui.button(emoji="ℹ️", style=discord.ButtonStyle.secondary, custom_id="jtc:info", row=3)
    async def b_info(self, interaction, button):
        room = await self._room(interaction)
        if room is not None:
            await interaction.response.send_message(embed=self.cog.room_card(room), ephemeral=True)

    @discord.ui.button(emoji="💾", style=discord.ButtonStyle.secondary, custom_id="jtc:save", row=3)
    async def b_save(self, interaction, button):
        await self._quick(interaction, self.cog.save_prefs)


class SettingsView(discord.ui.View):
    """The admin setup interface."""

    def __init__(self, cog, guild_id, owner_id):
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.owner_id = cog, guild_id, owner_id

    async def interaction_check(self, interaction):
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(embed=emb("Only the bot owner can change this.", RED), ephemeral=True)
            return False
        return True

    def _cfg(self):
        cfg = guild_cfg(self.guild_id)
        if not cfg:
            raise BotError("Join-to-create is disabled. Run `/jtc setup` again.")
        return cfg

    @discord.ui.button(label="Name template", emoji="📝", row=0)
    async def b_template(self, interaction, button):
        async def apply(inter, value):
            cfg = self._cfg()
            cfg["template"] = value.strip() or "{user}'s room"
            save_data()
            return f"📝 Template: `{cfg['template']}`  (use `{{user}}` and `{{count}}`)"
        await interaction.response.send_modal(ValueModal("Room name template", "Template", apply,
                                                         default=(guild_cfg(self.guild_id) or {}).get("template", ""),
                                                         placeholder="{user}'s room"))

    @discord.ui.button(label="Default limit", emoji="👥", row=0)
    async def b_limit(self, interaction, button):
        async def apply(inter, value):
            if not value.strip().isdigit() or not 0 <= int(value) <= 99:
                raise BotError("Enter a number from 0 to 99 (0 = no limit).")
            self._cfg()["limit"] = int(value)
            save_data()
            return f"👥 New rooms get a limit of **{int(value) or 'none'}**."
        await interaction.response.send_modal(ValueModal("Default user limit", "Limit (0 = none)", apply))

    @discord.ui.button(label="Default bitrate", emoji="🎚️", row=0)
    async def b_bitrate(self, interaction, button):
        async def apply(inter, value):
            top = int(inter.guild.bitrate_limit) // 1000
            if not value.strip().isdigit() or not 0 <= int(value) <= top:
                raise BotError(f"Enter 0 (default) or 8-{top} kbps.")
            self._cfg()["bitrate"] = int(value)
            save_data()
            return f"🎚️ New rooms use **{int(value) or 'the default'}** kbps."
        await interaction.response.send_modal(ValueModal("Default bitrate", "kbps (0 = default)", apply))

    @discord.ui.button(label="Re-post panel", emoji="🔄", row=1)
    async def b_repost(self, interaction, button):
        try:
            cfg = self._cfg()
            ch = interaction.guild.get_channel(cfg.get("panel_channel") or 0) or interaction.channel
            msg = await self.cog.post_panel(interaction.guild, ch, cfg)
            embed = emb(f"✅ Panel posted: {msg.jump_url}", GREEN)
        except BotError as e:
            embed = emb(str(e), RED)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Show status", emoji="📊", row=1)
    async def b_status(self, interaction, button):
        await interaction.response.send_message(embed=self.cog.status_embed(interaction.guild), ephemeral=True)


# ============================================================ console
async def c_jtc(bot, args):
    g = bot.current_guild
    if g is None:
        print("No server selected.")
        return
    cfg = guild_cfg(g.id)
    print("Not set up here (use /jtc setup in Discord)." if not cfg else
          f"Hub: {g.get_channel(cfg['hub'])}  | template: {cfg.get('template')}  | limit: {cfg.get('limit')}  | bitrate: {cfg.get('bitrate')}")
    for cid, info in rooms().items():
        if info["guild"] == g.id:
            ch = g.get_channel(int(cid))
            print(f"  room {ch.name if ch else cid} - owner {info['owner']}")


async def setup(bot):
    await bot.add_cog(JoinToCreate(bot))
    await bot.add_cog(VoiceCommands(bot))
    bot.add_console("jtc", c_jtc, "jtc", "show join-to-create status + live rooms", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
