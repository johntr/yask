# yask

**Yet Another Steam KRunner.** Find and launch your installed Steam games straight
from KRunner.

Press **Alt+Space**, type a few letters of a game, hit **Enter**. No Steam window, no
library scrolling, no `.desktop` shortcuts to maintain.

Built for **KDE Plasma 6** as a D-Bus runner, so there is nothing to compile and
nothing to rebuild when Plasma updates.

---

## Features

**Finds every game, everywhere**

- Reads `libraryfolders.vdf` and scans `appmanifest_*.acf` across *all* your Steam
  libraries — internal drives, secondary SSDs, external disks.
- Libraries on unmounted drives are skipped silently instead of erroring or
  listing games you cannot launch.
- Works with both native Steam and the Flatpak build, detected at launch time.
- Filters junk using the real app type from `appinfo.vdf` rather than guessing from
  names, so Proton builds, Steam runtimes, redistributables, DLC records and Steam's
  tooling all stay out of your results.

**Search that matches how you actually type**

Results are ranked across several tiers rather than plain substring matching:

| You type | You get | Why |
| --- | --- | --- |
| `terraria` | Terraria | exact title |
| `terr` | Terraria | prefix |
| `dead` | The Walking Dead | start of a word |
| `hl2` | Half-Life 2 | initials |
| `ksp` | Kerbal Space Program | initials |
| `tf2` | Team Fortress 2 | initials |

Exact → prefix → word-start → substring → all-words → subsequence, with a bonus for
characters landing on word boundaries. Recently played games get a small nudge
upward when scores are close, so your current game tends to surface first.

**Games you haven't installed**

Games you own but do not currently have on disk are searchable too, in their own
`Steam (not installed)` group below the installed ones. Enter — or the **Install**
action — hands the app to Steam's install dialog; nothing downloads unprompted.

Names for these come from Steam's binary `appinfo.vdf`, so no network request and
no API key is involved.

**Real game icons**

Each result shows the game's actual Steam icon, pulled from Steam's own
`appcache/librarycache` (the square client icon, falling back to logo, header, or
portrait art). Games with no cached art fall back to the Steam icon.

Games you actually play drift upward when text scores are close: recency and
playtime both feed the ranking, with playtime log-scaled so a 700-hour favourite
can never bury an exact title match on something you just installed.

**Details where you need them**

Highlighting a result shows what you'd want to know before launching:

```
Transistor        Update pending · 4h played · played 4 years ago
Deep Rock Galactic    115h played · played 9 days ago
Total War: WARHAMMER III   3h played · played 8 days ago · Proton cachyos-slr
```

Playtime and last-played come from Steam's own `localconfig.vdf`; the Proton
version is read from `CompatToolMapping` and only shown when you have explicitly
pinned one to that game. Pending updates are detected from the manifest's
`StateFlags`.

**Extra actions**

Press **Tab** or right-click a result:

- **Show in Steam library** — open the game's library page instead of launching
- **Open store page** — the game's Steam store page in your browser
- **Open install folder** — jump straight to the files
- **Update now** — offered only on games with a pending update

Results can also be dragged into a file manager, which drops the install folder.

**Steam's own screens**

Type `steam downloads`, `steam friends`, `steam big picture`, `steam console`,
`steam settings`, `steam screenshots`, `steam store`, or `steam library`. These
appear only behind the `steam`/`play`/`game` prefix, so searching for a game never
turns up "Steam Settings".

**Stays out of your way**

- Starts on demand via D-Bus activation and exits after 30 minutes idle — no
  daemon running all day.
- Queries are answered from a cache in ~4 ms. A cold start is ~150 ms.
- Installing or uninstalling a game is picked up automatically: the game list
  refreshes in the background when a `steamapps` directory changes. No restart.
- Launching never blocks KRunner — the runner replies immediately and spawns Steam
  on the next idle tick, so the KRunner window closes instantly.
- Passes through KRunner's XDG activation token, so on Wayland the launched game is
  allowed to raise and focus itself.

**List everything**

Type `steam` (or `steam <text>`, `play <text>`, `game <text>`) to browse the whole
library rather than searching for one title.

---

## Requirements

- KDE Plasma 6
- Python 3
- `dbus-python` and `PyGObject`

| Distro | Install dependencies |
| --- | --- |
| Arch | `sudo pacman -S python-dbus python-gobject` |
| Fedora | `sudo dnf install python3-dbus python3-gobject` |
| Debian/Ubuntu | `sudo apt install python3-dbus python3-gi` |

---

## Install

```sh
git clone https://github.com/johntr/yask.git
cd yask
./install.sh
```

The installer checks dependencies, copies three files into your home directory,
reloads the session bus, prints how many games it found, and restarts KRunner. No
root, nothing outside `~/.local/share`.

Then open KRunner with **Alt+Space** and type a game name.

### Uninstall

```sh
./uninstall.sh
```

---

## What gets installed

| Path | Purpose |
| --- | --- |
| `~/.local/share/yask/yask.py` | the runner |
| `~/.local/share/krunner/dbusplugins/plasma-runner-yask.desktop` | registers it with KRunner |
| `~/.local/share/dbus-1/services/io.github.johntr.yask.service` | D-Bus activation |

---

## How it works

KRunner can load runners over D-Bus instead of as compiled C++ plugins. This one
registers as `io.github.johntr.yask` on the session bus and implements
`org.kde.krunner1`:

| Method | Role |
| --- | --- |
| `Match` | scores the query against the cached game list |
| `Run` | launches `steam://rungameid/<appid>` (or a chosen action) |
| `Actions` | the three secondary actions |
| `SetActivationToken` | receives the Wayland activation token sent just before `Run` |
| `Config` | sets a 2-character minimum query length |
| `Teardown` | end of a match session |

The desktop file declares `X-Plasma-API=DBus2`, which is what makes KRunner call the
`Config` and `Teardown` lifecycle methods at all.

Steam data is read directly from disk — no API key, no network, no Steam Web API:

| File | Used for |
| --- | --- |
| `steamapps/libraryfolders.vdf` | where the libraries are |
| `steamapps/appmanifest_*.acf` | installed games, install dirs, `StateFlags` |
| `userdata/*/config/localconfig.vdf` | playtime and last-played |
| `config/config.vdf` | per-game Proton pins |
| `appcache/librarycache/` | icons, and which games you own |
| `appcache/appinfo.vdf` | app names and types (binary format) |

`appinfo.vdf` is an undocumented binary format, so every failure path there is
non-fatal: if Valve changes the version, yask falls back to filtering on name
patterns and simply stops offering uninstalled games. Installed games keep working.

One wrinkle worth knowing if you touch that code: Steam types Half-Life 2's
episodes as `Tool`, exactly like Proton and the Steam runtimes. So `Tool` alone is
not grounds for hiding something — it falls back to the name patterns, while
`Config`/`Music`/`DLC`/`Video` are excluded outright.

### A note for anyone forking this

KRunner's `Config` method accepts `TriggerWords`, and it looks like the right way
to implement the `steam ` prefix. It is not. `AbstractRunner::setTriggerWords()`
compiles the words into a `^(word|word)` match regex, so the runner is then *only*
queried when the query starts with one — plain `terraria` would stop matching
entirely. The prefix handling here is deliberately done inside `Match`.

---

## Troubleshooting

**No games appear.** Check what the runner sees:

```sh
~/.local/share/yask/yask.py --list
```

This prints every detected game with its appid and icon path. If it is empty, your
Steam root is somewhere unexpected — add it to `STEAM_ROOTS` at the top of the
script.

**Games on a second drive are missing.** They are only listed when the drive is
mounted. Mount it and search again; no restart needed.

**The KRunner window stays open after pressing Enter.** That is KRunner's "Keep
Open" pin, not this runner — it affects every runner. Click the 📌 button in
KRunner's top-right, or:

```sh
sed -i 's/^Pinned=true$/Pinned=false/' ~/.local/share/krunnerstaterc
kquitapp6 krunner
```

**Check the runner is registered:**

```sh
gdbus call --session --dest io.github.johntr.yask \
  --object-path /runner --method org.kde.krunner1.Match "portal"
```

---

## Prior art

[xTibor/krunner-steam](https://github.com/xTibor/krunner-steam) is an earlier Steam
runner using the same D-Bus approach. It was archived in June 2026 and does plain
substring matching, with library reloading left as a TODO.

## License

MIT — see [LICENSE](LICENSE).
