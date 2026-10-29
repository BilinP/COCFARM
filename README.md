# COCFARM

> [!NOTE]
> **Not affiliated with, endorsed by, or connected to Supercell.** Clash of Clans is a trademark of Supercell Oy.

<img src="farmbot/assets/cocfarm.png" alt="COCFARM logo" width="96" align="right">

**A Clash of Clans farming bot for Windows.** It plays the game for you on an
Android emulator (BlueStacks, LDPlayer or MuMu Player): it attacks, collects your loot and
looks after your village, in **both the home village and the builder base**.

[![windows-exe](https://github.com/BilinP/COCFARM/actions/workflows/windows-exe.yml/badge.svg)](https://github.com/BilinP/COCFARM/actions/workflows/windows-exe.yml)

**[⬇ Download the latest release](https://github.com/BilinP/COCFARM/releases/latest)** · [All releases](https://github.com/BilinP/COCFARM/releases)

> [!WARNING]
> **Using this bot can get your Clash of Clans account banned.** Botting breaks
> Supercell's Terms of Service, and Supercell bans accounts it catches,
> temporarily or for good. Only use it on an account you're willing to lose.

![The COCFARM Dashboard](readme/dashboard.png)

## What it does

- ⚔️ **Farms the home village.** It finds bases with the loot you want, attacks with your whole army, and comes home.
- 🛠️ **Farms the builder base.** It plays builder battles with your hero and troops (an all **Night Witch** army works best).
- 🔄 **Switches between villages for you.** With **Auto switch** on, it farms one village until its storages are full, then moves to the other.
- 💰 **Looks after your village.** It collects resources and the loot cart, can upgrade walls, and fills clan donation requests.
- ⏰ **Plays when you want.** You pick the hours, a daily attack range and random breaks, so it plays more like a person.
- 📊 **Tracks your loot.** The History page shows every attack and what you gained.
- ⬆️ **Updates itself** from GitHub releases, in one click.
- 🔔 **Sends Discord alerts.** You'll hear when storage is full, when it stops, and get a daily summary.

## Get started

1. **Install an emulator** ([BlueStacks 5](https://www.bluestacks.com/),
   [LDPlayer 9 or 14](https://www.ldplayer.net/), or [MuMu Player](https://www.mumuplayer.com/))
   and sign in to Clash of Clans in it.
2. **Download COCFARM.** Get `COCFARM-windows.zip` from the
   [latest release](https://github.com/BilinP/COCFARM/releases/latest) and unzip it anywhere.
3. **Open `COCFARM.exe`.** If Windows warns you, click **More info → Run anyway**.
4. **Follow the Setup page.** Pick your emulator, press **Check now**, and choose the loot you want.
   COCFARM sets the emulator up by itself.
5. **Press Start** (or F5). That's it.

**Updating:** COCFARM tells you when a new version is out. Press **Update now**, then
**Restart and update** once it's downloaded. Your settings, history and pictures are kept.
You can also check by hand under **Advanced → Settings → Updates**.

### Tested emulators

Home village and builder base attacks were tested live on each of these (COCFARM 0.6.0):

| Emulator | Version | Android |
|---|---|---|
| BlueStacks 5 | 5.22.110.1028 (Pie 64-bit) | 9 |
| LDPlayer 9 | 9.5.25.0 | 9 |
| LDPlayer 14 | 14.0.29.2 | 14 |
| MuMu Player | 6.8.9.0 (COCFARM 0.9.0) | 15 |

Other versions will likely work too. If yours doesn't, please
[report it](https://github.com/BilinP/COCFARM/issues/new/choose) with the emulator and its version.
Run one emulator at a time: BlueStacks and LDPlayer use the same connection port.
COCFARM sets the emulator's screen (860 × 672), CPU cores and memory by itself; you can turn that off under **Advanced**.

## A quick look

| Home village | Builder base |
|---|---|
| ![Home village settings](readme/home_village.png) | ![Builder base settings](readme/builder_base.png) |
| Pick the loot a base must have, how to attack, and village chores. | Pick how builder battles are played. |

| History | When to play |
|---|---|
| ![History page](readme/history.png) | ![When to play page](readme/when_to_play.png) |
| Every attack, daily totals, and a chart you can hover for exact numbers. | Playing hours, breaks, and when to stop. |

![Setup page](readme/setup.png)

## Tips

- **Faster battles:** under **Advanced → Pictures**, add pictures of the
  **Return Home** and **Okay** buttons (the page walks you through it).
  Without them, the bot still works but each attack ends about 30 seconds slower.
- **Builder base army:** train only Night Witches.
- **Spells:** by default it casts only **Bat Spells**, all at once on one spot: the middle of the base,
  or a random spot near the middle each battle. Switch to **All spells** under
  **Home village → Attacking**.
- **Check what the bot sees:** scout a base in the game, then press
  **What the bot sees** under **Advanced**.
- **Keyboard shortcuts:** F5 starts, Esc pauses, and Ctrl+1 to Ctrl+7 switch pages.

## Several accounts at once

COCFARM can farm on more than one emulator instance at the same time, one window each:

1. In your emulator's multi-instance manager, make a second instance and sign in to the other account in it.
2. In COCFARM, open **Advanced → Settings → Several emulators at once** and press **Add instance**.
   Its window opens with a copy of your settings.
3. In the new window's **Setup**, pick the second emulator instance, then press **Start**.

Each instance keeps its own settings, history and logs in `instances\<name>` in the COCFARM folder.
The button pictures and account profiles are shared, so an account keeps its settings whichever
window plays it. Two windows can't play on the same emulator instance. Install updates from the
main window, with the other windows closed. Each emulator needs its own CPU cores and memory, so
check your PC can run two.

## Something went wrong?

The Dashboard tells you what happened in plain words, with a button to fix it.
If the game gets stuck, COCFARM restarts it by itself, and restarts the emulator if needed.
For more detail, check `logs/farmbot.log` and the screenshots in `screens/errors/`
in the COCFARM folder.

<details>
<summary><b>For developers</b></summary>

COCFARM is written in Python. It controls the emulator over ADB, reads the
screen with OpenCV, and has a [PySide6](https://doc.qt.io/qtforpython-6/) +
[Fluent Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets) window.
Every setting is listed in [`farmbot.example.toml`](farmbot.example.toml).

```sh
pip install -e .
farmbot gui         # the window
farmbot run         # farm without the window
farmbot devices     # list emulator instances and their ADB ports
farmbot inspect     # show the loot, troop cards and deploy points it sees
farmbot pictures    # list the button pictures and which are missing
```

**Releases:** every push to `main` makes the
[`windows-exe`](.github/workflows/windows-exe.yml) workflow build
`COCFARM.exe` with PyInstaller, self-test it, and publish a GitHub release
(a version tag like `v1.2.3` publishes under that exact version).

**License:** COCFARM is free software under the [GNU GPL v3 or later](LICENSE).
The libraries it bundles (Qt, Fluent Widgets, OpenCV and others) are listed with
their licenses in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

</details>
