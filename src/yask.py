#!/usr/bin/env python3
"""yask — Yet Another Steam KRunner.

A KRunner D-Bus runner that finds and launches installed Steam games.
"""

import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

OBJPATH = "/runner"
IFACE = "org.kde.krunner1"
SERVICE = "io.github.johntr.yask"

HOME = os.path.expanduser("~")
STEAM_ROOTS = [
    os.path.join(HOME, ".steam", "steam"),
    os.path.join(HOME, ".local", "share", "Steam"),
    os.path.join(HOME, ".steam", "root"),
    os.path.join(HOME, ".var", "app", "com.valvesoftware.Steam", "data", "Steam"),
]

# Runtimes, redistributables and other non-game entries Steam keeps as "apps".
SKIP_PATTERNS = [
    re.compile(r"^Proton\b", re.I),
    re.compile(r"^Steam Linux Runtime", re.I),
    re.compile(r"^Steamworks Common Redistributables$", re.I),
    re.compile(r"^Steam Runtime", re.I),
    re.compile(r"redistributable", re.I),
    re.compile(r"^SteamVR", re.I),
]

NAME_RE = re.compile(r'^\s*"name"\s*"(.*)"\s*$')
APPID_RE = re.compile(r'^\s*"appid"\s*"(\d+)"\s*$')
INSTALLDIR_RE = re.compile(r'^\s*"installdir"\s*"(.*)"\s*$')
LASTPLAYED_RE = re.compile(r'^\s*"LastPlayed"\s*"(\d+)"\s*$')
STATEFLAGS_RE = re.compile(r'^\s*"StateFlags"\s*"(\d+)"\s*$')
LIBPATH_RE = re.compile(r'^\s*"path"\s*"(.*)"\s*$')
HASH_ICON_RE = re.compile(r"^[0-9a-f]{40}\.(jpg|png)$")

STATE_FULLY_INSTALLED = 4

APPINFO_MAGICS = (0x07564428, 0x07564429)

# Steam calls everything an "app". These types are never something you launch.
EXCLUDED_TYPES = {"config", "music", "dlc", "video", "series", "hardware"}

# "Tool" is not decisive on its own: Proton and the Steam runtimes are tools, but
# so are Half-Life 2's episodes. Tools fall back to the name patterns below.
AMBIGUOUS_TYPES = {"tool"}

# Things Steam itself can open, surfaced only behind a "steam "/"play " prefix so
# generic words like "friends" or "settings" never pollute ordinary searches.
STEAM_COMMANDS = [
    ("downloads", "Steam Downloads", "steam://open/downloads", "download"),
    ("library", "Steam Library", "steam://open/games", "applications-games"),
    ("friends", "Steam Friends", "steam://open/friends", "system-users"),
    ("big picture", "Steam Big Picture", "steam://open/bigpicture", "video-display"),
    ("console", "Steam Console", "steam://open/console", "utilities-terminal"),
    ("settings", "Steam Settings", "steam://open/settings", "configure"),
    ("screenshots", "Steam Screenshots", "steam://open/screenshots", "image-x-generic"),
    ("store", "Steam Store", "steam://store", "internet-web-browser"),
]

VDF_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text):
    """Minimal parser for Steam's text VDF format, returning nested dicts.

    Keys are lower-cased because Steam is inconsistent about their casing
    between files and versions.
    """
    root = {}
    stack = [root]
    key = None
    for match in VDF_TOKEN_RE.finditer(text):
        token, brace = match.group(1), match.group(2)
        if brace == "{":
            child = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = token.lower()
        else:
            stack[-1][key] = token.replace(r"\\", "\\")
            key = None
    return root


def vdf_get(node, *keys):
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def read_vdf(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_vdf(fh.read())
    except OSError:
        return {}


def user_config_path(root):
    """The localconfig.vdf of the most recently used Steam account."""
    userdata = os.path.join(root, "userdata")
    best, best_mtime = None, -1
    try:
        for entry in os.listdir(userdata):
            cand = os.path.join(userdata, entry, "config", "localconfig.vdf")
            try:
                mtime = os.stat(cand).st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = cand, mtime
    except OSError:
        return None
    return best


def read_playtime(root):
    """appid -> (minutes played, unix timestamp last played)."""
    path = user_config_path(root)
    if not path:
        return {}
    apps = vdf_get(read_vdf(path), "userlocalconfigstore", "software",
                   "valve", "steam", "apps") or {}
    stats = {}
    for appid, data in apps.items():
        if not isinstance(data, dict):
            continue
        try:
            minutes = int(data.get("playtime", 0) or 0)
            played = int(data.get("lastplayed", 0) or 0)
        except ValueError:
            continue
        if minutes or played:
            stats[appid] = (minutes, played)
    return stats


def read_compat_tools(root):
    """appid -> Proton/compat tool the user pinned to that game.

    Only explicit per-game entries are returned. Steam stores its global default
    under key "0", but that says nothing about whether a given game is actually
    run through Proton, so reporting it would be a guess.
    """
    mapping = vdf_get(read_vdf(os.path.join(root, "config", "config.vdf")),
                      "installconfigstore", "software", "valve", "steam",
                      "compattoolmapping") or {}
    tools = {}
    for appid, data in mapping.items():
        if appid == "0" or not isinstance(data, dict):
            continue
        name = (data.get("name") or "").strip()
        if name:
            tools[appid] = name
    return tools


def pretty_tool(name):
    """Shorten e.g. proton-cachyos-11.0-20260703-slr-x86_64 to something legible."""
    label = re.sub(r"^proton[-_ ]*", "", name, flags=re.I).replace("_", " ")
    label = re.sub(r"[-_]x86[-_]64$", "", label, flags=re.I)
    if len(label) > 22:
        label = label[:21].rstrip("-. ") + "\u2026"
    return "Proton " + label if label else "Proton"


def human_playtime(minutes):
    if minutes >= 60:
        return f"{minutes // 60}h played"
    return f"{minutes}m played" if minutes else ""


# Steam predates none of these timestamps; anything older is a placeholder value
# rather than a date, and rendering it gives nonsense like "played 56 years ago".
STEAM_EPOCH = 1041379200  # 2003-01-01, the year Steam shipped


def human_last_played(timestamp, now=None):
    if not timestamp or timestamp < STEAM_EPOCH:
        return ""
    days = int(((now or time.time()) - timestamp) // 86400)
    if days <= 0:
        return "played today"
    if days == 1:
        return "played yesterday"
    if days < 14:
        return f"played {days} days ago"
    if days < 60:
        return f"played {days // 7} weeks ago"
    if days < 730:
        return f"played {max(1, days // 30)} months ago"
    return f"played {days // 365} years ago"


def read_appinfo(root):
    """appid -> {"name", "type"} from Steam's binary appinfo.vdf.

    This is an undocumented format that Valve has revised before, so every
    failure mode here is non-fatal: callers fall back to name-pattern filtering
    and simply do not offer uninstalled games.
    """
    path = os.path.join(root, "appcache", "appinfo.vdf")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return _parse_appinfo(data)
    except (OSError, ValueError, struct.error, IndexError, UnicodeDecodeError):
        return {}


def _parse_appinfo(data):
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic not in APPINFO_MAGICS:
        raise ValueError(f"unsupported appinfo version 0x{magic:08x}")

    keys = None
    pos = 8
    if magic == 0x07564429:
        # v29 moved all key names into one table at the end of the file and
        # refers to them by index from within each app's key/value blob.
        table_offset = struct.unpack_from("<q", data, 8)[0]
        pos = 16
        count = struct.unpack_from("<I", data, table_offset)[0]
        keys = []
        cursor = table_offset + 4
        for _ in range(count):
            end = data.index(b"\0", cursor)
            keys.append(data[cursor:end].decode("utf-8", "replace"))
            cursor = end + 1

    apps = {}
    while pos < len(data) - 4:
        appid = struct.unpack_from("<I", data, pos)[0]
        if appid == 0:
            break
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        # infoState, lastUpdated, picsToken, sha1(text), changeNumber, sha1(binary)
        info = _read_appinfo_kv(data, body + 4 + 4 + 8 + 20 + 4 + 20, keys)
        if info:
            apps[str(appid)] = info
        pos = body + size
    return apps


def _read_appinfo_kv(data, pos, keys):
    """Walk one app's binary KV blob, keeping only common/name and common/type."""
    stack = []
    found = {}
    while pos < len(data):
        node = data[pos]
        pos += 1
        if node == 0x08:  # end of the current dict
            if not stack:
                break
            stack.pop()
            continue
        if keys is not None:
            index = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            key = keys[index] if index < len(keys) else ""
        else:
            end = data.index(b"\0", pos)
            key = data[pos:end].decode("utf-8", "replace")
            pos = end + 1
        if node == 0x00:  # nested dict
            stack.append(key)
        elif node == 0x01:  # string
            end = data.index(b"\0", pos)
            value = data[pos:end].decode("utf-8", "replace")
            pos = end + 1
            if stack and stack[-1] == "common" and key in ("name", "type"):
                found[key] = value
                if len(found) == 2:
                    return found
        elif node in (0x02, 0x03, 0x04, 0x06):
            pos += 4
        elif node in (0x07, 0x0A):
            pos += 8
        elif node == 0x05:  # wide string
            pos = data.index(b"\0\0", pos) + 2
        else:
            break
    return found


def owned_appids(root):
    """Apps this account has touched, whether or not they are installed now.

    Two sources, because neither is complete on its own: per-app entries in
    localconfig.vdf, and the artwork Steam caches for library entries.
    """
    ids = set(read_playtime(root))
    try:
        ids.update(os.listdir(os.path.join(root, "appcache", "librarycache")))
    except OSError:
        pass
    return {i for i in ids if i.isdigit()}


def steam_root():
    for root in STEAM_ROOTS:
        if os.path.isdir(os.path.join(root, "steamapps")):
            return os.path.realpath(root)
    return None


def library_paths(root):
    """All Steam library folders, main one first, skipping unmounted drives."""
    paths = [root]
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = LIBPATH_RE.match(line)
                if m:
                    paths.append(m.group(1).replace("\\\\", "\\"))
    except OSError:
        pass
    seen, out = set(), []
    for p in paths:
        real = os.path.realpath(p)
        if real in seen or not os.path.isdir(os.path.join(real, "steamapps")):
            continue
        seen.add(real)
        out.append(real)
    return out


def parse_manifest(path):
    """Pull appid/name/installdir out of an appmanifest_*.acf (first hit wins:
    the AppState block comes before the UserConfig block that repeats 'name')."""
    info = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                for key, rx in (("appid", APPID_RE), ("name", NAME_RE),
                                ("installdir", INSTALLDIR_RE),
                                ("lastplayed", LASTPLAYED_RE),
                                ("stateflags", STATEFLAGS_RE)):
                    if key not in info:
                        m = rx.match(line)
                        if m:
                            info[key] = m.group(1)
                if len(info) == 5:
                    break
    except OSError:
        return None
    if "appid" not in info or not info.get("name"):
        return None
    return info


def find_icon(root, appid):
    cache = os.path.join(root, "appcache", "librarycache")
    perapp = os.path.join(cache, appid)
    if os.path.isdir(perapp):
        try:
            for entry in sorted(os.listdir(perapp)):
                if HASH_ICON_RE.match(entry):  # square client icon
                    return os.path.join(perapp, entry)
        except OSError:
            pass
        for name in ("logo.png", "header.jpg", "library_600x900.jpg"):
            cand = os.path.join(perapp, name)
            if os.path.exists(cand):
                return cand
    for suffix in ("_icon.jpg", "_icon.png", "_logo.png", "_header.jpg"):
        cand = os.path.join(cache, appid + suffix)
        if os.path.exists(cand):
            return cand
    return "steam"


def build_subtext(game):
    """Detail line for the highlighted result. KRunner only renders subtext on
    the selected row, so this can be verbose without adding visual noise."""
    parts = []
    if not game["installed"]:
        parts.append("Not installed")
    elif game["pending_update"]:
        parts.append("Update pending")
    played = human_playtime(game["playtime"])
    if played:
        parts.append(played)
    last = human_last_played(game["lastplayed"])
    if last:
        parts.append(last)
    if game["compat_tool"]:
        parts.append(pretty_tool(game["compat_tool"]))
    return " \u00b7 ".join(parts) if parts else "Steam game"


def scan_games():
    root = steam_root()
    if not root:
        return {}
    playtime = read_playtime(root)
    compat_tools = read_compat_tools(root)
    appinfo = read_appinfo(root)
    games = {}
    for lib in library_paths(root):
        appsdir = os.path.join(lib, "steamapps")
        try:
            entries = os.listdir(appsdir)
        except OSError:
            continue
        for entry in entries:
            if not (entry.startswith("appmanifest_") and entry.endswith(".acf")):
                continue
            info = parse_manifest(os.path.join(appsdir, entry))
            if not info:
                continue
            name = info["name"]
            appid = info["appid"]
            apptype = appinfo.get(appid, {}).get("type", "").lower()
            if apptype in EXCLUDED_TYPES:
                continue
            if (apptype in AMBIGUOUS_TYPES or not apptype) and \
                    any(rx.search(name) for rx in SKIP_PATTERNS):
                continue
            minutes, played = playtime.get(appid, (0, 0))
            try:
                state = int(info.get("stateflags") or STATE_FULLY_INSTALLED)
            except ValueError:
                state = STATE_FULLY_INSTALLED
            game = {
                "appid": appid,
                "name": name,
                "icon": find_icon(root, appid),
                # the manifest and localconfig can disagree; trust the newer one
                "lastplayed": max(int(info.get("lastplayed") or 0), played),
                "playtime": minutes,
                "compat_tool": compat_tools.get(appid),
                "installed": True,
                "pending_update": state != STATE_FULLY_INSTALLED,
                "path": os.path.join(appsdir, "common", info.get("installdir", "")),
                "library": lib,
            }
            game["subtext"] = build_subtext(game)
            games[appid] = game

    add_uninstalled(games, root, appinfo, playtime)
    return games


def add_uninstalled(games, root, appinfo, playtime):
    """Owned games that are not installed, so they can be found and installed.

    Deliberately stricter than the installed path: only entries appinfo calls a
    game, so 500-odd DLC records and Steam's tooling never show up here.
    """
    if not appinfo:
        return
    for appid in owned_appids(root) - set(games):
        entry = appinfo.get(appid)
        if not entry or entry.get("type", "").lower() != "game":
            continue
        name = entry.get("name")
        if not name:
            continue
        minutes, played = playtime.get(appid, (0, 0))
        game = {
            "appid": appid,
            "name": name,
            "icon": find_icon(root, appid),
            "lastplayed": played,
            "playtime": minutes,
            "compat_tool": None,
            "installed": False,
            "pending_update": False,
            "path": None,
            "library": None,
        }
        game["subtext"] = build_subtext(game)
        games[appid] = game


def library_signature():
    """Cheap fingerprint of the steamapps dirs, so the cache refreshes on
    install/uninstall without re-reading every manifest on each keystroke."""
    root = steam_root()
    if not root:
        return ()
    sig = []
    watched = [os.path.join(lib, "steamapps") for lib in library_paths(root)]
    # playtime and Proton pins live outside the libraries but feed the subtext
    watched.append(os.path.join(root, "config", "config.vdf"))
    watched.append(os.path.join(root, "appcache", "appinfo.vdf"))
    user_config = user_config_path(root)
    if user_config:
        watched.append(user_config)
    for target in watched:
        try:
            sig.append((target, os.stat(target).st_mtime_ns))
        except OSError:
            sig.append((target, -1))
    return tuple(sig)


def subsequence_score(query, name):
    """Loose match: every query char appears in order. Rewards word-initial hits
    so 'hl2' finds 'Half-Life 2' and 'ksp' finds 'Kerbal Space Program'."""
    qi, hits, at_start = 0, 0, 0
    prev_sep = True
    for ch in name:
        sep = not ch.isalnum()
        if qi < len(query) and ch == query[qi]:
            qi += 1
            hits += 1
            if prev_sep:
                at_start += 1
        prev_sep = sep
    if qi < len(query):
        return 0.0
    return 0.35 + 0.25 * (at_start / len(query))


def score(query, name):
    q, n = query.lower().strip(), name.lower()
    if not q:
        return 0.6
    if q == n:
        return 1.0
    if n.startswith(q):
        return 0.95
    idx = n.find(q)
    if idx >= 0:
        # matching the start of a word beats matching mid-word
        base = 0.85 if idx == 0 or not n[idx - 1].isalnum() else 0.7
        return base - min(0.1, idx / 400.0)
    if all(part in n for part in q.split()):
        return 0.65
    return subsequence_score(q, n)


def steam_command():
    if shutil.which("steam"):
        return ["steam"]
    if shutil.which("flatpak"):
        return ["flatpak", "run", "com.valvesoftware.Steam"]
    return None


def spawn(argv, token=None):
    env = os.environ.copy()
    if token:
        # lets Steam/the game raise itself on Wayland instead of being demoted
        env["XDG_ACTIVATION_TOKEN"] = token
        env["DESKTOP_STARTUP_ID"] = token
    else:
        env.pop("XDG_ACTIVATION_TOKEN", None)
        env.pop("DESKTOP_STARTUP_ID", None)
    subprocess.Popen(argv, start_new_session=True, env=env,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


class SteamRunner(dbus.service.Object):
    IDLE_EXIT = 1800  # seconds without a query before the helper quits

    def __init__(self, loop):
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        name = dbus.service.BusName(SERVICE, bus)
        super().__init__(bus, OBJPATH, name)
        self.loop = loop
        self.games = {}
        self.signature = None
        self.rescan_queued = False
        self.activation_token = None
        self.timer = None
        self.touch()

    def touch(self):
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add_seconds(self.IDLE_EXIT, self.quit)

    def quit(self):
        self.loop.quit()
        return False

    def catalog(self):
        """Return cached games immediately. A changed library only schedules a
        rescan for the next idle tick, so no query ever waits on disk I/O."""
        if not self.games:
            self.games = scan_games()
            self.signature = library_signature()
            return self.games
        if not self.rescan_queued and library_signature() != self.signature:
            self.rescan_queued = True
            GLib.idle_add(self.rescan)
        return self.games

    def rescan(self):
        self.games = scan_games()
        self.signature = library_signature()
        self.rescan_queued = False
        return False

    @dbus.service.method(IFACE, out_signature="a{sv}")
    def Config(self):
        return dbus.Dictionary({"MinLetterCount": dbus.Int32(2)}, signature="sv")

    @dbus.service.method(IFACE, out_signature="a(sss)")
    def Actions(self):
        return [
            ("library", "Show in Steam library", "steam"),
            ("store", "Open store page", "internet-web-browser"),
            ("folder", "Open install folder", "folder-open"),
            ("update", "Update now", "system-software-update"),
            ("install", "Install", "install"),
        ]

    @dbus.service.method(IFACE, in_signature="s", out_signature="a(sssida{sv})")
    def Match(self, query):
        self.touch()
        query = query.strip()
        listing_all = False
        low = query.lower()
        for word in ("steam ", "game ", "play "):
            if low.startswith(word):
                query = query[len(word):].strip()
                listing_all = True
                break
        if low in ("steam", "games"):
            query = ""
            listing_all = True
        if not query and not listing_all:
            return []

        scored = []
        for game in self.catalog().values():
            s = score(query, game["name"])
            if s <= 0:
                continue
            scored.append((s, game))

        matches = []
        if scored:
            # Nudge games you actually play upwards when text scores are close.
            # Playtime is log-scaled so a 700h favourite cannot bury an exact
            # title match on a game you have barely touched.
            newest = max((g["lastplayed"] for _, g in scored), default=0) or 1
            longest = math.log1p(max((g["playtime"] for _, g in scored), default=0)) or 1

            def weighted(item):
                s, game = item
                boost = (0.04 * (game["lastplayed"] / newest)
                         + 0.03 * (math.log1p(game["playtime"]) / longest))
                # uninstalled stays findable, but never outranks something you
                # can play right now
                return (s + boost) if game["installed"] else 0.6 * (s + boost)

            scored = [(weighted(item), item[1]) for item in scored]
            scored.sort(key=lambda t: (t[0], t[1]["lastplayed"]), reverse=True)

            for rank, (s, game) in enumerate(scored[:20]):
                if game["installed"]:
                    actions = ["library", "store", "folder"]
                    if game["pending_update"]:
                        actions.append("update")
                else:
                    actions = ["install", "store"]
                props = {
                    "subtext": dbus.String(game["subtext"]),
                    "category": dbus.String("Steam" if game["installed"]
                                            else "Steam (not installed)"),
                    "actions": dbus.Array(actions, signature="s"),
                }
                if game["path"] and os.path.isdir(game["path"]):
                    # lets the result be dragged into a file manager
                    props["urls"] = dbus.Array(
                        ["file://" + urllib.parse.quote(game["path"])], signature="s")
                matches.append((
                    dbus.String(game["appid"]),
                    dbus.String(game["name"]),
                    dbus.String(game["icon"]),
                    # CategoryRelevance (KF6 field, not match type) — keeps the
                    # uninstalled group sorted below the installed one
                    dbus.Int32(70 if game["installed"] else 40),
                    dbus.Double(min(1.0, max(0.05, s - rank * 0.001))),
                    dbus.Dictionary(props, signature="sv"),
                ))

        matches.extend(self.command_matches(query, listing_all))
        return matches

    def command_matches(self, query, listing_all):
        """Steam's own screens, e.g. "steam downloads". Only offered behind a
        trigger word, so searching for a game never turns up "Steam Settings"."""
        if not listing_all or not query:
            return []
        out = []
        for keyword, title, url, icon in STEAM_COMMANDS:
            s = max(score(query, keyword), score(query, title))
            if s < 0.6:
                continue
            out.append((
                dbus.String("cmd:" + url),
                dbus.String(title),
                dbus.String(icon),
                dbus.Int32(50),
                dbus.Double(min(0.99, s)),
                dbus.Dictionary({
                    "subtext": dbus.String("Steam"),
                    "category": dbus.String("Steam Commands"),
                    "actions": dbus.Array([], signature="s"),
                }, signature="sv"),
            ))
        return out

    @dbus.service.method(IFACE, in_signature="s")
    def SetActivationToken(self, token):
        """KRunner hands us an XDG activation token immediately before Run so
        the process we spawn is allowed to focus itself on Wayland."""
        self.activation_token = str(token)

    @dbus.service.method(IFACE, in_signature="ss")
    def Run(self, match_id, action_id):
        """Reply straight away and launch on the next idle tick, so KRunner is
        never left waiting on us to fork a process before it closes."""
        self.touch()
        if match_id.startswith("cmd:"):
            GLib.idle_add(self.open_url, match_id[4:])
            return
        game = self.catalog().get(match_id)
        if game:
            GLib.idle_add(self.launch, game, action_id)

    def open_url(self, url):
        token, self.activation_token = self.activation_token, None
        cmd = steam_command()
        if cmd:
            spawn(cmd + [url], token)
        return False

    def launch(self, game, action_id):
        appid = game["appid"]
        token, self.activation_token = self.activation_token, None
        if action_id == "store":
            spawn(["xdg-open", f"https://store.steampowered.com/app/{appid}/"], token)
        elif action_id == "folder" and game["path"]:
            spawn(["xdg-open", game["path"]], token)
        else:
            cmd = steam_command()
            if not cmd:
                return False
            if action_id == "install" or not game["installed"]:
                # Steam opens its install dialog; nothing downloads unprompted
                target = f"steam://install/{appid}"
            elif action_id == "library":
                target = f"steam://nav/games/details/{appid}"
            elif action_id == "update":
                target = f"steam://install/{appid}"  # queues the pending update
            else:
                target = f"steam://rungameid/{appid}"
            spawn(cmd + [target], token)
        return False

    @dbus.service.method(IFACE)
    def Teardown(self):
        self.touch()


def main():
    if "--list" in sys.argv:
        games = scan_games()
        for g in sorted(games.values(), key=lambda x: (not x["installed"], x["name"].lower())):
            print(f"{g['appid']:>8}  {g['name']}\n          {g['subtext']}")
        print(f"\n{len(games)} game(s) found.")
        return
    loop = GLib.MainLoop()
    SteamRunner(loop)
    loop.run()


if __name__ == "__main__":
    main()
