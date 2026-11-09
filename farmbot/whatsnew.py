"""What's new in each version, in a player's words: shown once after an
update, so a returning player learns what changed without reading the
release page.

The version last shown is kept in ``update.json`` (it survives updates).
"""

from __future__ import annotations

from pathlib import Path

from farmbot import __version__
from farmbot.update import STATE, _load, _save, parse_version

# Newest first. Each change is (kind, headline, detail): the kind is one of
# KINDS, the headline says what the player gets in a few words, and the
# detail (may be empty) explains it in one plain sentence, not how it's done.
KINDS = ["New", "Improved", "Fixed"]

NOTES: dict[str, list[tuple[str, str, str]]] = {
    "1.5.3": [
        ("Improved", "Super Miners are healed where they dig",
         "In Raid Weekend, Healing spells go just ahead of the Super Miners, by where they're heading, every 17 "
         "seconds from 25 seconds in (sooner if one gets low). The view follows them when they dig off screen, "
         "and Lightning waits for them, then hits the buildings in their way."),
        ("New", "Suggested loot amounts for every Town Hall",
         "The loot a base must have now follows new amounts for your Town Hall and rises with it. Switch "
         "'Suggested amounts' off to keep your own, and press 'See all' for a table of every Town Hall."),
        ("Improved", "Every emulator is the same phone",
         "BlueStacks, MuMu Player and LDPlayer are all set up as a OnePlus 10 Pro."),
        ("Fixed", "MuMu Player is muted",
         "MuMu Player's Android 15 kept its sound on; it's now muted like the other emulators."),
        ("New", "Other emulator instances on the Farms page",
         "Instances that aren't farms yet are listed in their own group, ready to switch on."),
        ("Fixed", "Update now shows the download",
         "Pressing Update now opens the Updates box, scrolled down to it, and the update bar no longer shows "
         "an error once it's closed."),
    ],
    "1.5.2": [
        ("New", "A stopped farm starts again by itself",
         "When a farm stops on a problem, it waits 10 minutes (then 20, then 40), restarts the emulator and "
         "carries on, with the time shown on its card and sent to Discord. Press Pause or Stop to cancel. A "
         "farm that isn't logged in, shares its account with another farm, or needs you (a missing emulator, "
         "or one COCFARM mustn't open) stays stopped."),
        ("Improved", "Maintenance breaks are waited out",
         "Instead of stopping, the farm closes the game and looks again every 5 minutes until Supercell's break "
         "is over, then carries on."),
        ("Improved", "Extras never stop a farm",
         "If collecting, the clan, Clan Capital raids, upgrades or (in Auto) the builder base keep failing, "
         "that one is left alone for 2 hours and the farm goes on attacking."),
        ("Fixed", "The Town Hall is upgraded",
         "With 'Upgrade the Town Hall' on, it's now upgraded first among the buildings, right after new "
         "ones. It's under none of the list's Defense, Resource or Army types, so it was only looked at "
         "last, or never with heroes off, after everything else had taken the builders and the gold."),
        ("Fixed", "Fewer taps on Build for free walls",
         "Each free wall closes the list, so it's opened again for the next one. It no longer taps the tab "
         "twice each time: the bot now sees which tab is open (it's lighter) and taps one only when it isn't. "
         "Only 5 walls are put down a round, so a village with many "
         "waiting no longer spends minutes on them between attacks. The rest come in later rounds."),
        ("New", "Leave when the storages are full",
         "A battle ends once the loot taken fills your storages (the ones picked under When these storages are "
         "full, or all of them): the rest of it couldn't be kept. Switch it off under Home village → Attacking → "
         "Ending the battle."),
        ("Improved", "Quicker upgrade rounds",
         "The build list's filter menu is waited on by what shows (open, a button lit, closed) instead of set "
         "pauses. A filter already set isn't set again in the same round, and one that's empty (no hero "
         "upgrades yet, say) isn't opened again for an hour. A round at Town Hall 4 went from 61 s to 35-47 s."),
        ("Improved", "A short build list isn't scrolled",
         "When the whole list is on show (the empty background under its last row), the bot no longer swipes "
         "it looking for more."),
        ("Improved", "No loot cart while the storages are full",
         "The loot cart keeps its loot until it's collected, so it's left for later while every storage is "
         "full, and collectors of a full storage are left too."),
        ("Fixed", "No dark elixir before Town Hall 7",
         "A dark elixir storage size copied over from another farm's settings no longer counts on an account "
         "that can't have dark elixir yet, and research never looks for dark elixir upgrades there."),
        ("Fixed", "No boat search at Town Hall 4",
         "The builder base's boat can only be repaired from Town Hall 5, so a Town Hall 4 no longer drags the "
         "view around looking for it."),
        ("Fixed", "Storages measured again before they're spent",
         "A storage size read before new storages were built made gold look full early, so it was spent every "
         "round before it could reach a big upgrade like the Town Hall. A storage that looks full is now "
         "measured again first (at most once an hour)."),
        ("Fixed", "Windows in French and other languages",
         "Connecting to LDPlayer no longer fails with a 'charmap' codec error on a Windows that isn't in "
         "English: Windows' own commands are read in its console's language, and an emulator's port is "
         "found whatever language Windows is in."),
        ("New", "A game that isn't in English is named",
         "COCFARM reads the game's English buttons. When it recognises nothing and the emulator is in another "
         "language, it now says the game should be set to English, instead of only 'stuck'."),
        ("Fixed", "A Windows user name with accents",
         "COCFARM in a folder like C:\\Users\\Jérôme couldn't load its pictures of the game's buttons, save "
         "screenshots or read the player's name. Pictures now load and save at any path."),
        ("Fixed", "Stuck in the Clan Capital",
         "A game left on the Capital's raid map is taken home instead of being restarted over and over."),
        ("Fixed", "MuMu Player's slow first start",
         "When Android is up but the emulator never answers, the start is given up on after 75 seconds, not 2 "
         "minutes, and tried again. A slow first boot still gets its full time."),
        ("Improved", "Every emulator side by side, no copies",
         "LDPlayer 9, LDPlayer 14, MuMu Player and BlueStacks can farm at the same time. An instance whose "
         "connection port is another farm's isn't offered as a farm, new instances get a port of their own, and "
         "BlueStacks is given a free port before it starts, so no instance has to be copied to get out of the way."),
        ("New", "Delete the old instance after a move",
         "When a farm was copied to a new instance, the notice has a button to delete the old one and free its "
         "disk space."),
        ("New", "Two COCFARM windows don't fight",
         "A farm's emulator instance and its account are kept for it across every COCFARM open on the PC: a "
         "second COCFARM won't start a farm on them, or close an emulator the other is farming with."),
        ("Fixed", "MuMu Player starts after LDPlayer",
         "LDPlayer's background service could stay behind and hang MuMu Player's start. If no LDPlayer instance "
         "is running, COCFARM ends it and starts MuMu Player again."),
        ("Improved", "Disk space checked before a MuMu copy",
         "A new MuMu Player instance isn't started on a drive without room for it, and a failed LDPlayer copy "
         "leaves no empty instances behind."),
        ("Improved", "Builders from 0 and simpler Upgrades",
         "The builders slider starts at 0, and upgrades your Town Hall can't do yet are greyed out, with "
         "settings that can't apply hidden."),
    ],
    "1.5.1": [
        ("New", "Farms move off a shared port by themselves",
         "LDPlayer gives instance N the port 5555 + 2N in every version, so LDPlayer 9 and 14 (and MuMu Player's "
         "first instance, which listens on 5555 too) can want the same one. Instead of stopping, the farm that "
         "started later copies its instance to one with a port of its own (the game and its login go with it) "
         "and plays that from then on. The old instance is kept."),
        ("New", "One account, one farm",
         "Two farms on the same account log each other out. Each farm reads the player's name once its game is "
         "open; if another farm already plays that account, this one closes its game and stops, and says so."),
        ("New", "Stop any farm from the Farms page",
         "Each farm that's playing has its own Stop button: no need to show it in the window first. A second "
         "press stops it straight away."),
        ("New", "Mute switch and builder limits",
         "Advanced → Emulator → Keep the emulator muted (on by default). Upgrades → how many builders may be busy "
         "at once, for the "
         "home village and the builder base; the log says how many are working, free and kept free."),
        ("Improved", "Fewer trips to the Build menu",
         "New buildings are looked for in one list across gold and elixir, a busy Laboratory is seen from its "
         "count without opening its tab, and a building that won't upgrade is passed over for an hour instead of "
         "tapped every round."),
        ("Fixed", "A new Laboratory or Town Hall is seen the same day",
         "The village data was read once a day, so research could stay \"locked\" for hours after the Laboratory "
         "went up. It's read again after the bot builds something new, and every two hours while things are "
         "still locked."),
        ("Improved", "The window opens faster",
         "Pages are built the first time they're shown, with the logo breathing while they load."),
        ("Fixed", "Muting on LDPlayer 14",
         "Android 9's volume command differs from newer Androids': the emulator is muted on both now."),
        ("Improved", "Every emulator is set up the same",
         "LDPlayer, MuMu Player and BlueStacks always get the screen COCFARM is trained on (860 × 672, 240 DPI), "
         "2 CPU cores, 4 GB of memory and 60 FPS, and LDPlayer and MuMu the same phone model. Anything changed in "
         "the emulator is put back when the bot connects, so these are no longer in Settings."),
        ("Fixed", "No more emulator ads over the game",
         "MuMu Player's and LDPlayer's own store apps showed other games' ads over Clash of Clans, and a tap on "
         "one opened the Play Store. COCFARM switches those store apps off when it connects; any ad that still "
         "shows is closed with its X."),
        ("New", "New buildings are built",
         "Buildings your Town Hall has unlocked but you haven't built yet come before upgrades, put down where "
         "the game places them (move them whenever you like). New walls, which are free, are put down too. "
         "Switch it off in Upgrades → Build new buildings."),
        ("Improved", "The Town Hall is upgraded too",
         "When the game says the Town Hall can't be upgraded yet (other buildings first), the bot closes that "
         "and starts the next upgrade, instead of spending nothing. Switch it off in Upgrades → Upgrade the "
         "Town Hall to keep it for yourself."),
        ("Fixed", "An upgrade that won't start no longer stops others",
         "If a building's upgrade doesn't open (locked, say), the bot moves on to the next one instead of giving "
         "up on that resource for the round."),
        ("Fixed", "\"Unable to attack\" is waited out",
         "When the game won't start a battle (one left early still runs), the bot spots the white \"Unable to "
         "attack\" screen even where its words look a little different, presses Return Home, closes the game for "
         "3 to 5 minutes and searches again."),
        ("Fixed", "Ranked Battle is never started",
         "While a battle left midway still runs, the battle menu shows a countdown where Find a Match goes, and "
         "the bot could tap Ranked Battle's Find a Match instead. It now only ever taps the regular one, and "
         "takes the countdown as the sign to wait."),
        ("Fixed", "Attacks below Town Hall 7",
         "A low Town Hall's Find a Match button (with its cost under it) wasn't recognised, so the bot never "
         "started a battle there."),
        ("Fixed", "A game that closes mid-battle is noticed at once",
         "Before, the bot watched an empty screen until the battle's 3 minutes were up and then blamed a missing "
         "result screen; now it restarts the game within seconds."),
        ("Improved", "Searching copes with slow emulators",
         "The bot waits longer for a base to show, measures the screen's size again if Next can't be found, taps "
         "Next again when the same base comes back, and after a battle waits for the result screen (taking any "
         "reward on it) before going home. When something does go wrong, the log says exactly what."),
    ],
    "1.5.0": [
        ("Improved", "Troops go right up to the red line",
         "Troops land just outside the red line (half as far out as before), and where it can't be seen, just "
         "outside the buildings instead of out at the map's edge. Works on any window size; older settings move "
         "up to the line on their own."),
        ("New", "Play like a person",
         "A new page of habits you switch on one by one: taps that land around a button, varied pauses and "
         "swipes, a different battle rhythm each time, a look at each base, and more. It shows how much time "
         "each habit adds."),
        ("New", "Settings that fit your Town Hall",
         "The loot to look for starts at amounts that suit your Town Hall and goes up with it, until you type "
         "your own. Anything your Town Hall hasn't unlocked yet, like dark elixir, spells, heroes or the builder "
         "base, is greyed out with a note saying why."),
        ("New", "Your village data",
         "Once a day the bot reads your village from the game's own Data Export. The Village tab's Show button "
         "lists your Town Hall, buildings and army."),
        ("Improved", "Profiles named after your account",
         "A new profile takes the player name shown in the game instead of staying \"Account 1\"."),
        ("Fixed", "\"Unable to attack Village\" is waited out",
         "After a battle that didn't finish, the game refuses new ones for a while: the bot closes the game for "
         "a few minutes and carries on, instead of failing round after round."),
        ("Improved", "Says why MuMu won't start",
         "When MuMu Player hangs while starting and LDPlayer's background service is still running (it stops "
         "MuMu starting), the message says so and how to end it."),
        ("Improved", "Smoother window","Resizing the COCFARM window no longer stutters."),
    ],
    "1.0.0": [
        ("New", "Farm several accounts at once (beta)",
         "Switch on a farm for each emulator instance on the Farms page (LDPlayer, MuMu Player or BlueStacks, "
         "mixed as you like) and start them all together. Each farm keeps to the account signed in there."),
        ("New", "Make and delete emulator instances",
         "New instance copies one on LDPlayer or MuMu Player, and the bin on a row deletes one, after asking."),
        ("New", "Each farm in the main window or its own",
         "View shows a farm in the main window while the others keep farming; the small button opens it in a "
         "window of its own."),
        ("New", "Stops on Clash of Clans' maintenance break",
         "When the game is down for maintenance, the bot stops and says why instead of retrying."),
        ("Fixed", "Google Play's \"Rate Clash of Clans\" box is closed",
         "It could sit over the game after a battle and stop the bot; COCFARM now answers Never."),
        ("Fixed", "Buttons found on every farm",
         "A farm added from the Farms page could look for buttons at the wrong size and stop with \"find-a-match "
         "button did not appear\"."),
        ("Fixed", "MuMu Player stays connected",
         "A dropped MuMu Player connection no longer loops on \"Lost the connection; reconnecting\"."),
        ("Fixed", "LDPlayer 14 starts with LDPlayer 9 installed",
         "COCFARM no longer mistakes LDPlayer 14 for LDPlayer 9 and stops with \"LDPlayer 9 did not close\"."),
        ("Improved", "Faster tabs and start-up",
         "Switching to the Farms page and opening COCFARM with several farms no longer lag."),
        ("Improved", "Clearer wall messages",
         "When a wall costs more than you have, the log says so instead of \"couldn't read the price\"."),
    ],
    "0.9.9": [
        ("Fixed", "LDPlayer connects every time",
         "LDPlayer's own background helper could take the connection away, so the bot stopped with \"did not "
         "answer over ADB\"."),
        ("New", "A stuck emulator is restarted",
         "If the emulator doesn't answer, COCFARM closes it and starts it again, up to three times, before "
         "stopping."),
        ("Improved", "The game is restarted after a failed round",
         "Something over the game (like an emulator's ad) is cleared by closing the game and opening it again. "
         "After three failed rounds in a row the bot stops and tells you why."),
        ("Fixed", "Messages fit in the window",
         "A long message, like a stop with its reason, no longer runs off the edge of the COCFARM window."),
        ("Improved", "A tidier log", "Technical details only the log file needs no longer show in the window."),
    ],
    "0.9.8": [
        ("Fixed", "Every hero goes out again",
         "The trees behind the troop bar could look like a hero's health bar, so a hero (often the Royal Champion) "
         "was taken as already placed and left out."),
        ("Fixed", "Fewer bases skipped as unreadable",
         "Loot numbers over snow, trees or a bright wall edge are read instead of skipped, and no longer misread "
         "(a gold of 1, or dark elixir taken as none)."),
        ("Fixed", "Requested spells are donated",
         "When a request names the units it wants, the spells it asks for are given along with the troops."),
        ("Fixed", "Donations by Clan Castle level on LDPlayer",
         "The requester's Clan Castle room (like 0/50) is read again, so your armies by Clan Castle level are used."),
        ("Improved", "Commas in amounts as you type", "10000000 shows as 10,000,000, so an extra zero stands out."),
        ("Improved", "Warning for loot minimums no base meets",
         "A minimum like 10,000,000 gold (an extra zero, say) is flagged in Settings, on the Dashboard and in the "
         "log, since it makes the bot skip every base."),
        ("Fixed", "Searching no longer stops with Next on screen",
         "The bot could give up waiting for the Next button while it was showing."),
        ("Fixed", "Battle is tapped again if the menu doesn't open",
         "After an attack, a tap on Battle that the game missed no longer ends the round."),
    ],
    "0.9.7": [
        ("Improved", "Troops are placed exactly",
         "Each card is tapped once per troop left, so spare taps no longer drop the next troop or a hero."),
        ("Fixed", "Troop counts are read correctly", "Counts like x68 and x7 on the cards were sometimes misread."),
        ("Fixed", "Heroes in big armies",
         "When the troop bar scrolls, a hero already out isn't tapped again (which used its ability early), "
         "and abilities still fire for heroes scrolled out of view."),
        ("Fixed", "Pop-up windows",
         "What's new and Accounts no longer stop you moving COCFARM, and What's new closes when you click outside it."),
    ],
    "0.9.6": [
        ("New", "Donations by Clan Castle level",
         "Each request gets troops that fit the requester's Clan Castle. Choose them in Home village → Clan → "
         "Armies by Clan Castle level."),
        ("Improved", "Donations fill what's on screen",
         "Only the requests you can see in chat are filled, and Fill the rest uses up the space left."),
        ("Improved", "The Dashboard explains itself", "It says what Start will do, and why and when the bot stopped."),
        ("Improved", "History shows the newest attacks first", ""),
        ("Improved", "Some settings moved",
         "Phone alerts are under Advanced → Alerts, and rarely used attack settings are folded away on "
         "Home village → Attacking."),
        ("Fixed", "No upgrades during Goblin events",
         "Nothing is upgraded while the Goblin Builder or Goblin Researcher is in the village."),
        ("Fixed", "LDPlayer sign-in and ports",
         "The sign-in screen is noticed, and another emulator using COCFARM's port is closed for you."),
    ],
    "0.9.5": [
        ("New", "Clan Capital raids (beta)", "Raid on its own Raid Weekend tab."),
        ("Fixed", "Zooming out", "The bot no longer zooms out when the view already is."),
    ],
    "0.9.4": [
        ("New", "Super troop boosts", "Boost super troops with dark elixir. Never with a Super Potion or gems."),
        ("New", "Gravestones are cleared", ""),
        ("Improved", "Loot minimums", "Minimums left at their defaults are set from your storage sizes."),
        ("Improved", "Discord stop messages", "They sum up the run."),
    ],
}


def unseen(state_path: Path | None = None, current: str = __version__,
           first_run: bool = False) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """The notes of the versions since the one last shown, newest first, and
    remember this one as shown. A new player (``first_run``) gets none:
    everything is new to them."""
    state_path = state_path or STATE
    state = _load(state_path)
    seen = state.get("seen")
    if seen == current:
        return []
    state["seen"] = current
    _save(state_path, state)
    if first_run:
        return []
    now = parse_version(current)
    if not seen:  # updated from before these notes: only this version's
        return [(version, lines) for version, lines in NOTES.items() if parse_version(version) == now]
    since = parse_version(seen)
    return [(version, lines) for version, lines in NOTES.items() if since < parse_version(version) <= now]


def latest(current: str = __version__) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """The newest notes up to this version (Advanced → Updates → What's new)."""
    now = parse_version(current)
    return [(version, lines) for version, lines in NOTES.items() if parse_version(version) <= now][:1]
