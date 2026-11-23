from utils import need_guild

GROUP = "core"


async def c_help(bot, args):
    seen, groups = set(), {}
    for e in bot.console.values():
        if id(e) in seen:
            continue
        seen.add(id(e))
        groups.setdefault(e["group"], []).append(e)
    for g, items in groups.items():
        print(f"\n[{g}]")
        for e in sorted(items, key=lambda x: x["name"]):
            print(f"  {e['usage']:<36} {e['desc']}")
    print()


async def c_exit(bot, args):
    await bot.close()


async def c_guilds(bot, args):
    for i, s in enumerate(bot.guilds, 1):
        mark = "*" if s == bot.current_guild else " "
        print(f" {mark} {i}. {s.name} ({s.id})")


async def c_guild(bot, args):
    if args and args[0].isdigit() and 1 <= int(args[0]) <= len(bot.guilds):
        bot.current_guild = bot.guilds[int(args[0]) - 1]
        print(f"Selected: {bot.current_guild.name}")
    else:
        print("Usage: guild <number>  (see 'guilds')")


async def c_channels(bot, args):
    g = need_guild(bot)
    if g:
        for i, c in enumerate(g.voice_channels, 1):
            print(f"  {i}. {c.name} ({c.id})  {c.bitrate // 1000}kbps")


async def c_members(bot, args):
    g = need_guild(bot)
    if not g:
        return
    q = " ".join(args).lower()
    found = [m for m in g.members if not m.bot and
             (not q or q in m.name.lower() or q in m.display_name.lower())]
    for m in found[:25]:
        vc = m.voice.channel.name if m.voice and m.voice.channel else "-"
        print(f"  {m.display_name} ({m.name}) {m.id}  voice: {vc}")
    if len(found) > 25:
        print(f"  ...and {len(found) - 25} more, narrow your search")


async def c_sync(bot, args):
    n = await bot.sync_slash()
    print(f"Slash commands synced to {n} server(s).")


async def c_modules(bot, args):
    for name in sorted(bot.extensions):
        print(f"  {name}")


async def _ext(bot, args, action):
    if not args:
        print(f"Usage: {action} <module>   e.g. {action} music")
        return
    name = args[0].lower()
    if name == "core" and action != "reload":
        print("The core module can't be unloaded.")
        return
    path = f"modules.{name}"
    if action == "load":
        await bot.load_extension(path)
    elif action == "unload":
        await bot.unload_extension(path)
    else:
        await bot.reload_extension(path)
    print(f"{action}ed {name}" if action != "unload" else f"unloaded {name}")


async def c_load(bot, args):
    await _ext(bot, args, "load")


async def c_unload(bot, args):
    await _ext(bot, args, "unload")


async def c_reload(bot, args):
    await _ext(bot, args, "reload")


async def setup(bot):
    a = bot.add_console
    a("help", c_help, "help", "show all commands", group=GROUP)
    a("exit", c_exit, "exit", "stop the bot", aliases=("quit",), group=GROUP)
    a("guilds", c_guilds, "guilds", "list servers", group=GROUP)
    a("guild", c_guild, "guild <number>", "select a server", group=GROUP)
    a("channels", c_channels, "channels", "list voice channels", group=GROUP)
    a("members", c_members, "members [text]", "search members", group=GROUP)
    a("sync", c_sync, "sync", "re-sync slash commands to every server", group=GROUP)
    a("modules", c_modules, "modules", "list loaded modules", group=GROUP)
    a("load", c_load, "load <module>", "load a module file", group=GROUP)
    a("unload", c_unload, "unload <module>", "unload a module", group=GROUP)
    a("reload", c_reload, "reload <module>", "reload a module after editing it", group=GROUP)


async def teardown(bot):
    bot.remove_console_group(GROUP)
