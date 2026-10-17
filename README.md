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

1. Starts the emulator and the game if needed, and dismisses popups.
2. Looks after the village: collects resources and the loot cart, upgrades
   walls with spare loot (if turned on), donates to clan requests, and
   requests Clan Castle troops (when those pictures exist).
3. Unless set to only maintain the village, or your gold is below your limit,
   goes **Battle → Find a Match → Attack!**. It reads each base's available
   gold, elixir and dark elixir (optionally requiring full collectors, i.e. a
   dead base) and presses **Next** until one meets your minimums.
4. Deploys **everything in the troop bar**, split into 1–3 waves at the speed
   you set: troops around the outside of the base, then heroes and siege
   machines, then spells over the base. It fires hero abilities after a delay,
   ends the battle once the loot stops dropping, and returns home.
5. Optionally plays the **builder base** too: two-stage builder battles and
   looking after the builder village.

It only plays during the hours you choose, up to a daily attack limit.

Every attack is logged to `attacks.csv`, with the base's loot, your resources
before the attack, and what you actually gained (read from your resource bar
once back home, so it includes the star bonus). Open it in Excel or Google
Sheets to see which loot settings pay off.

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
6. Set your loot minimums on **Search**, then press **Start** (F5).

Before the first Start, check the bot's vision without attacking: in the game,
scout a base (Battle → Find a Match), then press **What the bot sees** on the
Settings page. It shows the loot it reads, the troop cards it found and the
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
- **Publishing a release:** push a version tag, and the workflow creates a
  GitHub Release with `COCFARM-windows.zip` and generated release notes:

  ```sh
  git tag v1.0.0
  git push origin v1.0.0
  ```

  Or run the workflow from the **Actions** tab (**Run workflow**) and enter a
  version such as `v1.0.0`.

## The control window


| Page | What you set |
|---|---|
| Dashboard | Live view of the base being scouted (each resource ticked against your minimum), your resources, loot actually gained this session, colour-coded activity log |
| Search | Minimum loot, all-or-any, dead bases only, sound on match, give-up limit, gold floor, maintain-only mode |
| Attack | Sides (laid out like the map), 1–3 waves, troop and wave speed, Clan Castle troops, hero abilities, ending early when loot stops |
| Village | Collecting resources and the loot cart, wall upgrades |
| Clan | Default troop/siege/spell to donate, troop requests with a message, and which pictures are still missing |
| Schedule | Active hours (click the 24-hour grid or use a preset), daily and per-session attack limits |
| Pictures | Every button picture the bot uses, which are missing or recommended, and a screenshot you drag a box on to add one |
| Builder base attack / village | Builder base battles and chores |
| Settings | BlueStacks or LDPlayer (found automatically), which instance, emulator setup (ADB, display, CPU/memory), auto-start, **Test connection** and **What the bot sees** |

The window is built with [PySide6](https://doc.qt.io/qtforpython-6/) (Qt 6) and
[Fluent Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets), in a dark Windows 11
(Fluent Design) style. It is designed to keep you oriented and stop mistakes:

- **Status is always visible.** The title bar shows a coloured state (Idle, Running, Attacking, Paused, Error), what the bot is doing, and Start/Stop.
- **Settings are laid out like Windows Settings.** Each one is a card with a short explanation, plus switches, sliders and segmented choices.
- **Problems are caught as you type.** An invalid value replaces the card's explanation with the reason, and Start stays disabled until it is fixed.

- **Settings that don't apply are greyed out**, such as the ability delay when hero abilities are off.
- **Saving is automatic.** Valid changes save to `farmbot.toml` straight away, and a running bot picks them up at its next step.
- **Shortcuts:** F5 starts, Esc stops, and Ctrl+1…9 switch pages.

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
| `farmbot/bot.py` | Main loop with error recovery and status events |
| `farmbot/gui.py`, `farmbot/form.py` | Fluent (PySide6) window and its settings model |
| `farmbot/config.py` | TOML settings (`farmbot.example.toml` lists them all) |
| `templates/` | Button, collector and digit pictures the bot matches against |
| `packaging/` | PyInstaller build of `COCFARM.exe` |
| `.github/workflows/windows-exe.yml` | CI: builds, self-tests and releases the exe |

## When something goes wrong

The Activity log on the Dashboard says what failed. Each time a round fails,
the bot also saves the screen it was looking at in `screens/errors/`, so you
can see exactly what it saw (and share it when asking for help). After three
failed rounds in a row it restarts the game.

## Not yet done

Supertroop activation, Town Hall level filter, Telegram
control, and a per-troop deploy order (the bot deploys troops, then
heroes/siege, then spells).
