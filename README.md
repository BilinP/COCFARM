# COCFARM

<img src="farmbot/assets/cocfarm.png" alt="COCFARM logo" width="96" align="right">

A clean-room Clash of Clans farming bot (COCFARM, Python package `farmbot`) that drives the game over
ADB on an Android emulator (BlueStacks 5 or LDPlayer) or phone.


Each cycle the bot:

1. Starts the game if needed and dismisses popups.
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

It only plays during the hours you choose, up to a daily attack limit.

Every attack is logged to `attacks.csv`, with the base's loot, your resources
before the attack, and what you actually gained (read from your resource bar
once back home, so it includes the star bonus). Open it in Excel or Google
Sheets to see which loot settings pay off.

> Automating Clash of Clans breaks Supercell's Terms of Service. Use it on an
> account you are willing to lose.

## Setup

### Easiest: the ready-made Windows app

1. On GitHub, open the repository's **Actions** tab, pick **windows-exe**, and
   open the newest run with a green tick for this branch.
2. Under **Artifacts**, download **COCFARM-windows** and unzip it anywhere
   (for example `Documents\COCFARM`).
3. Do steps 1–2 below for BlueStacks, then double-click **COCFARM.exe**. If
   Windows warns about an unknown publisher, choose **More info → Run anyway**.

No Python is needed. Settings, pictures, `attacks.csv` and error screenshots
are kept in that folder; if the window doesn't open, look in `farmbot.log` there.

### From source

1. Install [BlueStacks 5](https://www.bluestacks.com/) and Clash of Clans inside it.
2. In BlueStacks open **Settings → Advanced** and turn on **Android Debug Bridge (ADB)**.
3. Install [Python 3.11+](https://www.python.org/downloads/) (tick **Add python.exe to PATH**),
   then double-click **`start-farmbot.bat`**. The first run installs what the bot
   needs into a `.venv` folder and opens the window; later runs open it straight away.
   (Or install it yourself with `pip install -e .` and run `farmbot gui`.)
4. In the window, open **Device**. **What farmbot found** shows where BlueStacks
   is installed, whether ADB is on, and each instance with its ADB port. Press
   **Test connection**. (From a terminal, `farmbot devices` prints the same.)

5. Optionally (recommended), open **Pictures** and add the two button pictures
   not shipped, **Return Home** and **Okay**. Get the game to the screen each appears
   on, press **Take screenshot**, drag a box around the button's text, and press
   **Save picture**. The page warns you if the box could be mistaken for something
   else on the screen. Until they're added, the bot leaves each battle by closing
   the game once the loot stops dropping (the attack keeps what it has won); that
   works but costs about 30 seconds per attack.

6. Set your loot minimums on **Search**, then press **Start** (F5).

Before the first Start, check the bot's vision without attacking: in the game,
scout a base (Battle → Find a Match), then press **What the bot sees** on the
Settings page. It shows the loot it reads, the troop cards it found and the deploy
points, drawn on the screenshot. (`farmbot inspect` does the same from a terminal.)

### How the BlueStacks connection works

With **Connect to: BlueStacks** (the default), you never type a port:

- **Finding BlueStacks:** the bot reads the install folder from the Windows
  registry (`HKLM\SOFTWARE\BlueStacks_nxt`, or the MSI edition's key).
- **Finding the port:** it reads each instance's ADB port from `bluestacks.conf`
  in BlueStacks' data folder. It re-reads the file on every connect, because the
  port can change between runs.
- **ADB program:** it uses BlueStacks' own `HD-Adb.exe`, so platform-tools aren't needed.
- **Starting BlueStacks:** if BlueStacks isn't running and **Start BlueStacks
  automatically** is on, the bot opens the chosen instance (`HD-Player.exe --instance …`)
  and waits up to two minutes for it to accept ADB.
- **More than one instance:** if you use the Multi-Instance Manager, pick the
  instance by name in Settings.

For another emulator or a phone, choose **Connect to: Address** and enter its
ADB address (for example `127.0.0.1:5555`), or pass `-s ADDRESS` on the command line.

## Run

```sh
farmbot gui                     # control window
farmbot -c farmbot.toml run     # headless, Ctrl+C to stop
```


| Page | What you set |
|---|---|
| Dashboard | Live view of the base being scouted (each resource ticked against your minimum), your resources, loot actually gained this session, colour-coded activity log |
| Search | Minimum loot, all-or-any, dead bases only, sound on match, give-up limit, gold floor, maintain-only mode |
| Attack | Sides (laid out like the map), 1–3 waves, troop and wave speed, Clan Castle troops, hero abilities, ending early when loot stops |
| Village | Collecting resources and the loot cart, wall upgrades |
| Clan | Default troop/siege/spell to donate, troop requests with a message, and which pictures are still missing |
| Schedule | Active hours (click the 24-hour grid or use a preset), daily and per-session attack limits |
| Pictures | Every button picture the bot uses, which are missing or recommended, and a screenshot you drag a box on to add one |
| Device | BlueStacks instance (found automatically) or an ADB address, auto-start, and **Test connection** |

The window is built with [PySide6](https://doc.qt.io/qtforpython-6/) (Qt 6) and
[Fluent Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets), in a dark Windows 11
(Fluent Design) style. It is designed to keep you oriented and stop mistakes:

- **Status is always visible.** The title bar shows a coloured state (Idle, Running, Attacking, Paused, Error), what the bot is doing, and Start/Stop.
- **Settings are laid out like Windows Settings.** Each one is a card with a short explanation, plus switches, sliders and segmented choices.
- **Problems are caught as you type.** An invalid value replaces the card's explanation with the reason, and Start stays disabled until it is fixed.

- **Settings that don't apply are greyed out**, such as the ability delay when hero abilities are off.
- **Saving is automatic.** Valid changes save to `farmbot.toml` straight away, and a running bot picks them up at its next step.
- **Shortcuts:** F5 starts, Esc stops, and Ctrl+1…8 switch pages.

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

| Command | Use |
|---|---|
| `farmbot gui` | Control window |
| `farmbot run [--max-attacks N]` | Farm without the window |
| `farmbot devices` | List BlueStacks instances, their ADB ports, and whether ADB is on |
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
| `farmbot/game.py` | Screen scaling, find/click/wait helpers, template names (`T`) |
| `farmbot/vision.py`, `farmbot/text.py` | Template matching, digit reading |
| `farmbot/catalog.py` | Every button picture: where to find it and what it enables |
| `farmbot/armybar.py` | Troop bar cards |
| `farmbot/redline.py`, `farmbot/deployzone.py` | Deploy point detection |
| `farmbot/tasks/` | Village chores, clan, search, attack |
| `farmbot/bot.py` | Main loop with error recovery and status events |
| `farmbot/gui.py`, `farmbot/form.py` | Fluent (PySide6) window and its settings model |
| `farmbot/config.py` | TOML settings (`farmbot.example.toml` lists them all) |

## When something goes wrong

The Activity log on the Dashboard says what failed. Each time a round fails,
the bot also saves the screen it was looking at in `screens/errors/`, so you
can see exactly what it saw (and share it when asking for help). After three
failed rounds in a row it restarts the game.

## Not yet done

Supertroop activation, Town Hall level filter, Telegram
control, and a per-troop deploy order (the bot deploys troops, then
heroes/siege, then spells).
