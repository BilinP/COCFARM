# COCFARM

<img src="farmbot/assets/cocfarm.png" alt="COCFARM logo" width="96" align="right">

A clean-room Clash of Clans farming bot for Windows. COCFARM drives the game
over ADB on an Android emulator, **BlueStacks 5** or **LDPlayer 9**, and finds
everything it needs on the screen with OpenCV. It comes as a ready-made
`COCFARM.exe` with a Windows 11 style control window: download it, unzip it,
double-click it.

[![windows-exe](https://github.com/BilinP/COCFARM/actions/workflows/windows-exe.yml/badge.svg)](https://github.com/BilinP/COCFARM/actions/workflows/windows-exe.yml)
[![Latest release](https://img.shields.io/github/v/release/BilinP/COCFARM)](https://github.com/BilinP/COCFARM/releases/latest)

Each cycle the bot:

1. Starts the game if needed and works out which screen it's on (home village,
   builder base, battle, results, chest room, reward picker, popup), then gets
   back to the village from any of them.
2. Looks after the village: collects resources and the loot cart, upgrades
   walls with spare loot (if turned on), donates to clan requests, and
   requests Clan Castle troops (when those pictures exist).
3. Unless set to only maintain the village, or your gold is below your limit,
   goes **Battle → Find a Match → Attack!**. It reads each base's available
   gold, elixir and dark elixir and presses **Next** until one meets your minimums.
4. Deploys **everything in the troop bar** (troops, then heroes, then spells;
   a big army that doesn't fit is deployed a screenful at a time, scrolling the
   bar; super and event troops count as troops; siege machines stay out unless
   turned on, as they let the Clan Castle troops out), split into 1–3 waves at the speed
   you set: troops around the outside of the base, then all heroes and siege
   machines in one quick pass, then spells (all on one spot, or spread over the
   base). It fires hero abilities after a delay or once each hero is hurt,
   picks the rightmost event reward when offered, ends the battle once the loot
   stops dropping, and returns home.
5. Optionally plays the **builder base** instead: two-stage builder battles
   and looking after the builder village. Train an army of **Night Witches
   only**: the bot attacks best with an all Night Witch army. **Auto switch** farms the village
   you pick first until the storages you picked are full, then the other
   one, and stops once both are full.

It only plays during the hours you choose, up to a daily attack limit, and can
take random breaks. It can message a Discord channel (through a webhook) when
storage is full, when it stops, when it restarts the emulator, and with a
daily summary.

Every attack is logged to `attacks.csv`, with the base's loot, your resources
before the attack, and what you actually gained (read from your resource bar
once back home, so it includes the star bonus). Open it in Excel or Google
Sheets to see which loot settings pay off. The **History** page shows today's,
the last week's and all-time totals, loot per hour of play, a 14-day chart (hover a bar for the exact amount; all loot,
or gold, elixir or dark elixir on its own) and every attack.

> Automating Clash of Clans breaks Supercell's Terms of Service. Use it on an
> account you are willing to lose.

## Download and install

Nothing but the emulator needs installing: the app bundles everything else.

1. Install an emulator, and Clash of Clans inside it:
   - [BlueStacks 5](https://www.bluestacks.com/), or
   - [LDPlayer 9](https://www.ldplayer.net/) (or newer).
2. Open the [**latest release**](https://github.com/BilinP/COCFARM/releases/latest),
   download **COCFARM-windows.zip**, and unzip it anywhere (for example
   `Documents\COCFARM`).
3. Double-click **COCFARM.exe**. If Windows warns about an unknown publisher,
   choose **More info → Run anyway**.
4. On the **Settings** page pick **BlueStacks** or **LDPlayer** under
   **Connect to**. **What COCFARM found** shows where the emulator is installed
   and its instances. Press **Test connection**.

   COCFARM sets the emulator up by itself the first time it connects: it turns
   on ADB and sets the display to 860 × 672 at 240 DPI (the size its pictures
   are made for), restarting the emulator once to do so. You can turn each of
   these off under **Emulator setup**.
5. Optionally (recommended), open **Pictures** and add the two button pictures
   not shipped, **Return Home** and **Okay**. Get the game to the screen each
   appears on, press **Take screenshot**, drag a box around the button's text,
   and press **Save picture**. The page warns you if the box could be mistaken
   for something else on the screen. Until they're added, the bot leaves each
   battle by closing the game once the loot stops dropping (the attack keeps
   what it has won); that works but costs about 30 seconds per attack.
6. Set your loot minimums (step 3 of **Setup**), then press **Start** (F5).

Before the first Start, check the bot's vision without attacking: in the game,
scout a base (Battle → Find a Match), then press **What the bot sees** under
**Advanced**. It shows the loot it reads, the troop cards it found and the
deploy points, drawn on the screenshot.

Settings (`farmbot.toml`), button pictures (`templates`), `attacks.csv` and
error screenshots (`screens`) are kept in the app's folder; if the window
doesn't open, look in `farmbot.log` there. To update, download the new zip and
unzip it over the old folder; your settings and pictures are kept.

### How the emulator connection works

You never type a port.

**BlueStacks 5**

- **Finding BlueStacks:** the install folder comes from the Windows registry
  (`HKLM\SOFTWARE\BlueStacks_nxt`, or the MSI edition's key).
- **Finding the port:** each instance's ADB port is read from `bluestacks.conf`
  in BlueStacks' data folder, again on every connect, because it can change
  between runs.
- **ADB program:** BlueStacks' own `HD-Adb.exe`, so platform-tools aren't needed.
- **More than one instance:** pick the instance by name in Settings (empty uses
  the main one).

**LDPlayer 9+**

- **Finding LDPlayer:** checks the Windows registry and the usual install
  folders (`C:\LDPlayer`, `D:\LDPlayer`, …); pick a version in Settings if you have more than one
  (empty uses the newest).
- **Instances and setup:** uses LDPlayer's `ldconsole.exe` to list instances,
  set the display, CPU and memory, and launch the instance. ADB is the instance's
  "Local ADB connection" setting, switched on while it is stopped.
- **Connecting:** instance *N* is `emulator-<5554 + 2N>` on LDPlayer's own
  `adb.exe`. Pick the instance number in Settings (0 is the first one in
  LDMultiPlayer).

With **Start the emulator automatically** on, the bot opens the chosen instance
when you press Start and waits up to two minutes for it to answer over ADB.
To keep the PC free, **Use as little of the PC as possible** gives the emulator
only 2 CPU cores and 2048 MB of memory (adjustable).

## Releases (for maintainers)

The [`windows-exe`](.github/workflows/windows-exe.yml) GitHub Actions workflow
builds `COCFARM.exe` from the source on a Windows runner with PyInstaller
([`packaging/build.py`](packaging/build.py)) and starts it once with
`--self-test` to make sure it runs.

- **Every push and pull request:** the build is attached to the workflow run as
  the **COCFARM-windows** artifact (kept 30 days), handy for testing a branch.
- **Every push to `main`** (for example merging a pull request): the workflow
  also publishes a GitHub Release with `COCFARM-windows.zip`, tagged
  `v<version>-<run number>` (for example `v0.1.0-7`, version from
  `pyproject.toml`), and marks it as the latest. Nothing to do by hand.
- **A named version:** push a tag such as `v1.0.0`
  (`git tag v1.0.0 && git push origin v1.0.0`), or run the workflow from the
  **Actions** tab (**Run workflow**) and enter the version.

## The control window


The first time it opens, the window goes to **Setup**: four numbered steps
(pick the emulator, check the connection and the game, choose the loot you
want, Start), each ticked off as it's done. A failed check says what's wrong
in plain words, with a button for the fix.

| Page | What's on it |
|---|---|
| Dashboard | A large status (Running, Attacking, Paused, Waiting until 17:00, Stopped, Problem) with one line on what it's doing and the button that fits (Start, Pause, Resume, or the fix for a problem); which village to farm; the base being looked at, this session's loot, the last attack; the step-by-step log under Details |
| Home village | Tabs: **Bases to attack** (attack on/off, loot minimums, totals, gold floor), **Attacking** (sides laid out like the map, waves, speeds, spells, heroes, Clan Castle, when to leave), **Village** (collecting, loot cart, walls), **Clan** (donations, requests) |
| Builder base | Tabs: **Attacking** (corner, hero, abilities, 4x speed) and **Village** (collecting, elixir cart, walls). Use an all Night Witch army |
| When to play | Playing hours (24-hour grid with presets), random breaks, and when to stop: full storages, attacks per day (a random number in a range you set) |
| History | Totals, loot per hour, the last two weeks as a chart, and every attack |
| Setup | The four first-run steps, and Discord alerts |
| Advanced | Theme (light, dark, follow Windows), emulator tuning (ADB, display, CPU/memory, LDPlayer instance, device address), deploy-point and timing details, **What the bot sees**, and the **Pictures** tool for adding button pictures |

The window is built with [PySide6](https://doc.qt.io/qtforpython-6/) (Qt 6) and
[Fluent Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets), in a Windows 11
(Fluent Design) style, light or dark. It is designed to keep you oriented and stop mistakes:

- **Status is always visible.** The Dashboard and the title bar show the state as a word, a symbol and a colour, plus what the bot is doing; problems come with a fix button, and the technical details are kept apart.
- **Settings are laid out like Windows Settings.** Each one is a card with a short explanation, plus switches, sliders and segmented choices.
- **Problems are caught as you type.** An invalid value replaces the card's explanation with the reason, and Start stays disabled until it is fixed.

- **Settings that don't apply are greyed out**, such as the ability delay when hero abilities are off.
- **Saving is automatic.** Valid changes save to `farmbot.toml` straight away, and a running bot picks them up at its next step.
- **Keyboard:** F5 starts, Esc pauses (press again to stop now), Ctrl+1…7 switch pages, Tab moves between controls and Enter presses the focused button.
- **Updates:** when a newer release is on GitHub, a banner offers the download.

Fluent Widgets is free under GPLv3 for non-commercial use; a commercial project needs its paid licence.

## How it sees the game

| What | How |
|---|---|
| Screen size | Every screenshot is scaled to 920 px tall, so one set of templates and coordinates fits any device. Screenshots are fetched as raw pixels (no PNG compression on the device), falling back to PNG. |
| Buttons | OpenCV template matching against crops in `templates/buttons/`. |
| Loot numbers | White text is split into characters and each is matched against digit shapes learned from screenshots (`farmbot learn-digits`). |
| Troop bar | Cards are found from their outlines. The white "xN" corner and the header colour tell troops, spells, and heroes/siege machines apart. Each card is deployed until it greys out, checked with a screenshot after every batch of taps. |
| Deploy points | 1. The red line, when the game shows it (`farmbot/redline.py`). 2. Otherwise a ring just outside the bright building-area grass, kept clear of buildings and on-screen UI (`farmbot/deployzone.py`). 3. Otherwise fixed lines from `[attack.deploy_lines]`. |

## Commands

These are for development and troubleshooting when working from the source
(`pip install -e .`, then `farmbot …`); normal use only needs `COCFARM.exe`.

| Command | Use |
|---|---|
| `farmbot gui` | Control window |
| `farmbot run [--max-attacks N]` | Farm without the window |
| `farmbot devices` | List emulator instances, their ADB ports, and whether ADB is on |
| `farmbot pictures` | List the button pictures and which are missing |
| `farmbot inspect [--image F]` | Show loot, cards and deploy points on a screen |
| `farmbot screenshot [F]` | Save the device screen, scaled to bot coordinates |
| `farmbot crop F x y w h NAME` | Cut a template out of a screenshot |
| `farmbot locate NAME [--image F]` | Test a template |
| `farmbot learn-digits F x y w h "1 234"` | Learn loot digits from a number on screen |

## Layout

| Path | Purpose |
|---|---|
| `farmbot/device.py` | ADB device: screenshots, taps, swipes, app start/stop |
| `farmbot/bluestacks.py`, `farmbot/ldplayer.py` | Finding, setting up and connecting to BlueStacks 5 and LDPlayer 9+ |
| `farmbot/game.py` | Screen scaling, find/click/wait helpers, template names (`T`) |
| `farmbot/vision.py`, `farmbot/text.py` | Template matching, digit reading |
| `farmbot/catalog.py` | Every button picture: where to find it and what it enables |
| `farmbot/armybar.py` | Troop bar cards |
| `farmbot/redline.py`, `farmbot/deployzone.py` | Deploy point detection |
| `farmbot/tasks/` | Village chores, clan, search, attack, builder base |
| `farmbot/screens.py` | Names the screen on show and gets back to a village from it |
| `farmbot/bot.py` | Main loop with error recovery, the watchdog and status events |
| `farmbot/notify.py` | Discord webhook messages |
| `farmbot/history.py` | The attack log: writing it, totals, loot per hour |
| `farmbot/update.py` | Checks GitHub for a newer release |
| `farmbot/gui.py`, `farmbot/form.py` | Fluent (PySide6) window and its settings model |
| `farmbot/friendly.py` | Plain words for the window: what the bot is doing, and what went wrong with the fix |
| `farmbot/config.py` | TOML settings (`farmbot.example.toml` lists them all) |
| `templates/` | Button, collector and digit pictures the bot matches against |
| `packaging/` | PyInstaller build of `COCFARM.exe` |
| `.github/workflows/windows-exe.yml` | CI: builds, self-tests and releases the exe |

## When something goes wrong

The Dashboard shows what went wrong in plain words, with a button for the fix. The step-by-step log (Dashboard → Details) says exactly what failed, and the same log is kept in
`logs/farmbot.log` (a new file each day, two weeks kept). Each time a round
fails, the bot also saves the screen it was looking at in `screens/errors/`, so
you can see exactly what it saw (and share it when asking for help).

The watchdog: after three failed rounds in a row it restarts the game; if that
doesn't help (or the emulator stops answering), it closes and reopens the
emulator.

## Releases

Push a version tag (`git tag v0.2.0 && git push origin v0.2.0`) and the
`windows-exe` workflow builds COCFARM.exe and publishes it as a GitHub release.

## Not yet done

Supertroop activation, Town Hall level filter, army presets and a per-troop
deploy order (the bot deploys troops, then heroes/siege, then spells), and
dead-base search (only attacking bases with full collectors).
