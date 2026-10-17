# Templates

The bot recognises buttons by matching small PNG crops, and reads numbers
with digit shapes learned from screenshots. All of them are taken from
screens scaled to **920 px tall**; `farmbot screenshot` saves screens at that
size, so crop coordinates from its output can be used directly.

## Already included

Taken from the screenshots in `tests/fixtures/`:

| Name | From |
|---|---|
| `buttons/battle` | "Battle" button, home village |
| `buttons/find_match` | "Find a Match" text (matches both buttons; the bot taps the left, regular one) |
| `buttons/attack` | "Attack!" on the army screen |
| `buttons/next` | "Next" while scouting |
| `buttons/end_battle` | "End Battle" while scouting/attacking |
| `fonts/loot/0.png` … `9.png` | Digits from the "Available Loot" numbers |

## Recommended

Without these two the bot leaves each battle by closing the game (slower, but it works):

| Name | What to crop |
|---|---|
| `buttons/return_home` | "Return Home" on the battle result screen |
| `buttons/okay` | "Okay" in the End Battle / surrender confirmation |

Optional (the GUI lists what is missing on the Army and Clan pages):

| Name | Enables |
|---|---|
| `collectors/*.png` | Tapping full gold, elixir and dark collector bubbles |
| `buttons/close` | Closing windows (walls, chests) |
| `deadbase/*.png` | Dead-base search: pictures of **full** collectors on enemy bases |
| `cards/clan_castle.png` | Leaving Clan Castle troops out (the CC icon on its troop-bar card) |
| `buttons/open_chat`, `buttons/close_chat`, `buttons/donate` | Donations |
| `donate/<name>.png` | Each unit you donate, as it looks in the donate window (e.g. `donate/yeti.png`); the name is what you pick in the GUI |
| `buttons/open_chat`, `buttons/close_chat`, `buttons/no_clan`, `buttons/request`, `buttons/request_send` | Clan Castle requests from the clan chat |
| `buttons/try_again`, `buttons/reload` | Recovering from disconnect popups |

## Making one

The easiest way is the GUI's **Pictures** page: pick the picture, press
**Take screenshot** while its screen is showing (or open a saved screenshot),
drag a box around the button's text, and press **Save picture**.

From the command line (`farmbot pictures` lists what is missing):

```sh
farmbot screenshot screens/result.png
# open it in an image editor, note x, y, width, height of the button
farmbot crop screens/result.png 880 760 240 70 buttons/return_home
farmbot locate buttons/return_home --image screens/result.png   # aim for a score above 0.9
```

Crop tightly around the button's text. Leave out timers, costs and anything animated.

## Re-learning digits

If loot numbers read wrong (for example after a game update changes the font),
learn them again from a scouting screenshot. Give the region around one number
and the number shown:

```sh
farmbot learn-digits screens/scout.png 195 92 150 34 "1 582 869" --replace
farmbot inspect --image screens/scout.png
```
