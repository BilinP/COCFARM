"""Deploy every troop in the bar around the base and see the battle through."""

from __future__ import annotations

import itertools
import logging
import math
from typing import Iterator

from farmbot import armybar, deployzone, redline as rl, text
from farmbot.armybar import Card
from farmbot.config import Config
from farmbot.game import Game, T
from farmbot.geometry import Point, Rect, points_along
from farmbot.tasks.search import Loot, loot_value, read_loot
from farmbot.tasks.village import open_event_chests, pick_reward

log = logging.getLogger(__name__)


def deploy_edges(game: Game, cfg: Config) -> tuple[dict[str, list[Point]], Point | None]:
    """Points to drop troops on, per attacked side, and the base centre.

    Tries the red line, then the ring around the building area, and fills
    any side still empty from the fixed ``deploy_lines``.
    """
    screen = game.refresh()
    sides = cfg.attack.sides
    found: dict[str, list[Point]] = {}
    sources: dict[str, str] = {}
    center: Point | None = None
    detectors = (
        ("red line", cfg.redline.enabled, lambda: rl.detect(screen, cfg.redline), cfg.redline.min_side_points),
        ("building ring", cfg.zone.enabled, lambda: deployzone.detect(screen, cfg.zone), 1),
    )
    for name, enabled, detect, minimum in detectors:
        if not enabled or all(s in found for s in sides):
            continue
        result = detect()
        if result is None:
            continue
        center = center or result.center
        for side in sides:
            points = result.sides.get(side, [])
            if name == "red line":
                points = on_open_ground(screen, points, cfg)
            if side not in found and len(points) >= minimum:
                found[side] = points
                sources[side] = name
    lines = fixed_lines(screen.shape[1], cfg)
    for side in sides:
        if side not in found:
            found[side] = lines[side]
            sources[side] = "fixed line"
    floor = button_row_top(game)
    if floor is not None:
        found = {s: [p for p in pts if p[1] < floor] or lines[s] for s, pts in found.items()}
    log.info("Deploy points: %s", ", ".join(f"{s} {len(found[s])} ({sources[s]})" for s in sides))
    return found, center


BUTTON_ROW_MARGIN = 12  # px kept clear above the battle buttons


def button_row_top(game: Game) -> int | None:
    """Top of the row of battle buttons (End Battle, Boost Army, Boost
    Heroes, Next): taps at or below it could press one of them, so no troop
    is dropped there. ``None`` if neither End Battle nor Next is seen."""
    tops = [m.y - m.h // 2 for name in (T.END_BATTLE, T.NEXT) if game.has(name)
            for m in game.find_all(name)[:1]]
    return min(tops) - BUTTON_ROW_MARGIN if tops else None


POINT_MAX_BLOCKED = 0.5  # a red-line point with more non-ground around it is dropped
SIDE_MAX_BLOCKED = 0.2  # and a side whose points typically have more is not trusted
GROUND_UNKNOWN = 0.8  # share of the screen that is not grass or dirt above which the check is skipped


def on_open_ground(screen, points: list[Point], cfg: Config) -> list[Point]:
    """Red-line points that sit on open ground. The real boundary runs over
    grass; wall edges that look like it (golden walls do) sit among
    buildings, so a side whose points are mostly cluttered is dropped."""
    if not points:
        return points
    blocked = deployzone.open_ground(screen, cfg.zone)
    if float(blocked.mean()) > GROUND_UNKNOWN:
        return points  # hardly any grass or dirt (snow, desert themes): can't judge
    shares = [float(blocked[y, x]) for x, y in points]
    kept = [p for p, b in zip(points, shares) if b <= POINT_MAX_BLOCKED]
    if not kept or sorted(b for b in shares if b <= POINT_MAX_BLOCKED)[len(kept) // 2] > SIDE_MAX_BLOCKED:
        return []
    return kept


def fixed_lines(width: int, cfg: Config) -> dict[str, list[Point]]:
    """Points along the configured ``deploy_lines``. They are for a screen
    ``deploy_lines_width`` wide; the map is centred, so they shift with the
    screen's width, and points that end up off screen are dropped."""
    shift = (width - cfg.attack.deploy_lines_width) // 2
    lines = {}
    for side, (x1, y1, x2, y2) in cfg.attack.deploy_lines.items():
        line = points_along((x1 + shift, y1), (x2 + shift, y2), 12)
        lines[side] = [p for p in line if 10 <= p[0] < width - 10] or line
    return lines


def cycle_points(edges: dict[str, list[Point]], sides: list[str]) -> Iterator[Point]:
    """Endless stream of deploy points that alternates between sides and
    walks along each side, so troops spread around the base."""
    streams = [itertools.cycle(edges[s]) for s in sides if edges.get(s)]
    if not streams:
        raise RuntimeError("no deploy points on any side")
    for stream in itertools.cycle(streams):
        yield next(stream)


def spell_points(edges: dict[str, list[Point]], center: Point | None, reach: float) -> Iterator[Point]:
    """Points part-way from the centre towards the edge, i.e. over the base."""
    every = [p for pts in edges.values() for p in pts]
    cx, cy = center or base_center(edges)
    return itertools.cycle([(round(cx + (x - cx) * reach), round(cy + (y - cy) * reach)) for x, y in every])


def base_center(edges: dict[str, list[Point]]) -> Point:
    every = [p for pts in edges.values() for p in pts]
    return sum(p[0] for p in every) // len(every), sum(p[1] for p in every) // len(every)


SPELL_BURST = 11  # taps in one go when a spell card's count can't be read


def cast_together(game: Game, card: Card, spot: Point, count: int | None, cfg: Config) -> None:
    """Every spell on ``card`` at once, on one spot: one burst of as many
    taps as the card holds, then more only if some are left."""
    game.tap(*card.center)
    game.wait(0.1)
    game.tap_many([spot] * (count or SPELL_BURST), jitter=4, pause=0.0)
    game.wait(0.3)
    if not armybar.is_spent(game.refresh(), card, cfg.bar):
        deploy_card(game, card, itertools.repeat(spot), cfg)


def deploy_card(game: Game, card: Card, points: Iterator[Point], cfg: Config,
                fallback: Iterator[Point] | None = None) -> bool:
    """Select ``card`` and deploy until it greys out. True if it emptied.
    If the game refuses ``points``, the ``fallback`` points are tried."""
    acfg = cfg.attack
    if card.kind == "single":  # a hero or siege machine: one unit
        return place_single(game, card, points, cfg, fallback)
    stalled = 0
    before = game.refresh()
    for _ in range(acfg.max_batches_per_card):
        game.tap(*card.center)
        game.wait(0.15)
        game.tap_many([next(points) for _ in range(acfg.taps_per_batch)], jitter=3, pause=acfg.tap_delay)
        game.wait(0.3)
        after = game.refresh()
        if pick_reward(game):
            after = game.screen
        if armybar.is_spent(after, card, cfg.bar):
            return True
        # The count on the card changes as units go out; if it stays the
        # same twice in a row, the game is rejecting our points.
        stalled = 0 if armybar.card_changed(before, after, card) else stalled + 1
        if stalled >= 2:
            if fallback is not None and points is not fallback:
                log.info("Card at x=%d is not deploying; trying the fixed deploy lines", card.left)
                points, stalled = fallback, 0
                continue
            log.warning("Card at x=%d is not deploying; moving on", card.left)
            return False
        before = after
    log.warning("Card at x=%d still has units after %d batches", card.left, acfg.max_batches_per_card)
    return False


SINGLE_TRIES = 4  # deploy points tried per hero before giving up on it


def place_single(game: Game, card: Card, points: Iterator[Point], cfg: Config,
                 fallback: Iterator[Point] | None = None) -> bool:
    """Put a hero or siege machine down, checking that it went out.

    Once a unit is on the field its card shows the unit's green health bar
    (or greys out). If neither happens the point was refused (say, too close
    to the base), so the next point is tried. True if it was placed.
    """
    before = game.refresh()
    for attempt in range(SINGLE_TRIES):
        # Half the tries on the detected points, half on the fixed lines.
        source = fallback if fallback is not None and attempt >= SINGLE_TRIES // 2 else points
        game.tap(*card.center)
        game.wait(0.15)
        game.tap(*next(source), jitter=3)
        game.wait(0.6)
        after = game.refresh()
        if pick_reward(game):
            after = game.screen
        if armybar.unit_on_field(before, after, card) or armybar.is_spent(after, card, cfg.bar):
            return True
    log.warning("Hero/siege card at x=%d was not placed after %d tries", card.left, SINGLE_TRIES)
    return False


def place_singles(game: Game, cards: list[Card], points: Iterator[Point], cfg: Config,
                  fallback: Iterator[Point] | None = None) -> list[Card]:
    """Put every hero and siege machine down in one quick pass (card, point,
    next card...), then check them all on one screenshot; any the game
    refused get another point. Returns the cards that went out."""
    before = game.refresh()
    placed: list[Card] = []
    waiting = list(cards)
    for attempt in range(SINGLE_TRIES):
        if not waiting:
            break
        source = fallback if fallback is not None and attempt >= SINGLE_TRIES // 2 else points
        for card in waiting:
            game.tap(*card.center)
            game.wait(0.08)
            game.tap(*next(source), jitter=3)
            game.wait(0.08)
        game.wait(0.5)
        after = game.refresh()
        if pick_reward(game):
            after = game.refresh()
        out = [c for c in waiting if armybar.unit_on_field(before, after, c) or armybar.is_spent(after, c, cfg.bar)]
        placed += out
        waiting = [c for c in waiting if c not in out]
    for card in waiting:
        log.warning("Hero/siege card at x=%d was not placed after %d tries", card.left, SINGLE_TRIES)
    return placed


def read_count(game: Game, card: Card, cfg: Config) -> int | None:
    """The "xN" on a troop or spell card, or ``None`` if it cannot be read."""
    width = card.right - card.left
    region = Rect(card.left + int(width * 0.45), card.top - 6, int(width * 0.55), 28)
    pieces = text.segment(game._current(), region)
    if len(pieces) < 2:
        return None
    font = game.vision.font(cfg.bar.count_font)
    digits = []
    for mask in pieces[1:]:  # the first piece is the "x"
        char, score = font.classify(mask)
        if score < 0.45 or not char.isdigit():
            return None
        digits.append(char)
    count = int("".join(digits))
    return count if 0 < count <= 400 else None


def skip_clan_castle(game: Game, cards: list[Card], cfg: Config) -> list[Card]:
    """Drop the Clan Castle card when it should not be used."""
    if cfg.attack.use_cc or not game.has(T.CC_CARD):
        return cards
    marks = game.find_all(T.CC_CARD, threshold=0.8)
    keep = [c for c in cards if not any(c.left <= m.x <= c.right and c.top <= m.y <= c.bottom for m in marks)]
    if len(keep) < len(cards):
        log.info("Leaving the Clan Castle troops out")
    return keep


def deploy_taps(game: Game, card: Card, points: Iterator[Point], taps: int, cfg: Config) -> None:
    game.tap(*card.center)
    game.wait(0.15)
    game.tap_many([next(points) for _ in range(taps)], jitter=3, pause=cfg.attack.tap_delay)


def loot_left(game: Game, cfg: Config) -> Loot | None:
    return read_loot(game, cfg.regions)


def taken_share(target: Loot, left: Loot, cfg: Config) -> float:
    """Share of the base's reported loot already taken (0-1)."""
    total = loot_value(target, cfg.search)
    return 1.0 - loot_value(left, cfg.search) / total if total > 0 else 0.0


def finish_battle(game: Game, cfg: Config, target: Loot | None = None) -> float | None:
    """Wait for the battle to end. End it early once the available loot has
    stopped dropping, or once ``end_at_loot_share`` of the ``target`` loot
    (what scouting reported) is taken, or at the time limit; then go home.
    Returns the share of ``target`` taken, if it could be read."""
    acfg = cfg.attack
    started = last_change = game.clock()
    last_loot = None
    can_return = game.has(T.RETURN_HOME) and game.has(T.SURRENDER_OK)
    while True:
        game.refresh()
        if pick_reward(game):
            continue
        if can_return and game.find(T.RETURN_HOME):
            break
        if acfg.speed_up and game.has(T.SPEED) and game.click(T.SPEED):
            log.info("Battle speed: 4x")
            game.refresh()
        now = game.clock()
        loot = loot_left(game, cfg) if acfg.end_when_no_loot or (target and acfg.end_at_loot_share) else None
        # Loot only goes down in a battle: a higher reading is a misread.
        if loot is not None and loot != last_loot and (last_loot is None or _not_more(loot, last_loot)):
            last_loot, last_change = loot, now
        stalled = acfg.end_when_no_loot and last_loot is not None and now - last_change >= acfg.no_loot_seconds
        share = taken_share(target, last_loot, cfg) if target and last_loot else 0.0
        enough = bool(acfg.end_at_loot_share) and share >= acfg.end_at_loot_share
        if stalled or enough or now - started >= acfg.battle_timeout:
            reason = ("%d%% of the loot taken" % (share * 100) if enough else
                      "no loot gained for %ds" % acfg.no_loot_seconds if stalled else "time limit")
            log.info("Ending battle: %s", reason)
            if not can_return:
                # Without the Return Home / Okay pictures, closing the game is
                # the way out: the attack ends with what it has already won,
                # and the next round starts the game again at home.
                log.info("Closing the game to leave the battle (add the Return Home and Okay pictures to skip this)")
                game.device.stop_app(cfg.device.package)
                game.wait(3)
                return
            game.click(T.END_BATTLE)
            game.click_when_visible(T.SURRENDER_OK, timeout=5)
            if not game.wait_for(T.RETURN_HOME, timeout=15):
                raise RuntimeError("return-home button never appeared")
            break
        game.wait(2)
    taken = taken_share(target, last_loot, cfg) if target and last_loot else None
    if taken is not None:
        log.info("Took %d%% of the base's loot", taken * 100)
    game.wait(1.0)
    # The 100% reward can show over the results.
    if not game.click_when_visible(T.RETURN_HOME, timeout=5) and pick_reward(game):
        game.click_when_visible(T.RETURN_HOME, timeout=5)
    open_event_chests(game)
    return taken


def _not_more(loot: Loot, before: Loot) -> bool:
    return loot.gold <= before.gold and loot.elixir <= before.elixir and loot.dark <= before.dark


def attack(game: Game, cfg: Config, target: Loot | None = None) -> float | None:
    """Attack the base on screen; ``target`` is its loot as scouted.
    Returns the share of it taken, when the loot could be read."""
    acfg = cfg.attack
    game.zoom_out()
    cards = skip_clan_castle(game, armybar.find_cards(game.refresh(), cfg.bar), cfg)
    if not cards:
        raise RuntimeError("no cards found in the troop bar")
    counts = {card: read_count(game, card, cfg) for card in cards if card.kind != "single"}
    log.info("Troop bar: %s", ", ".join(
        f"{c.kind} x{counts[c]}" if counts.get(c) else c.kind for c in cards))
    edges, center = deploy_edges(game, cfg)
    edge_points = cycle_points(edges, acfg.sides)
    fixed_points = cycle_points(fixed_lines(game.size[0], cfg), acfg.sides)
    over_base = spell_points(edges, center, acfg.spell_reach)

    troops = [c for c in cards if c.kind == "troop"]
    singles = [c for c in cards if c.kind == "single"]
    spells = [c for c in cards if c.kind == "spell"]

    # Troops in waves: earlier waves send a share of each card (by its
    # count, or one batch when unreadable); the last wave empties them.
    remaining = dict(counts)
    for wave in range(acfg.waves):
        if acfg.waves > 1:
            log.info("Wave %d of %d", wave + 1, acfg.waves)
        last = wave == acfg.waves - 1
        for card in troops:
            if last:
                deploy_card(game, card, edge_points, cfg, fixed_points)
                continue
            left = remaining.get(card)
            share = math.ceil(left / (acfg.waves - wave)) if left else acfg.taps_per_batch
            deploy_taps(game, card, edge_points, share, cfg)
            if left:
                remaining[card] = left - share
        if not last:
            game.wait(acfg.wave_delay)

    placed = place_singles(game, singles, edge_points, cfg, fixed_points)
    spot = center or base_center(edges)
    for card in spells:
        if acfg.spell_placement == "together":
            cast_together(game, card, spot, counts.get(card), cfg)
        else:
            deploy_card(game, card, over_base, cfg)
    # One more go for any hero that was refused, now on other points.
    placed += place_singles(game, [c for c in singles if c not in placed], edge_points, cfg, fixed_points)
    if singles:
        log.info("Placed %d of %d hero/siege card(s)", len(placed), len(singles))

    if placed and acfg.hero_abilities:
        # Heroes: a second tap fires the ability. Siege machines: it
        # releases the troops inside.
        if acfg.ability_health > 0:
            fire_abilities_when_hurt(game, placed, cfg)
        else:
            game.wait(acfg.hero_ability_delay)
            for card in placed:
                game.tap(*card.center)
                game.wait(0.2)
        log.info("Activated %d hero/siege card(s)", len(placed))
    return finish_battle(game, cfg, target)


ABILITY_CHECK = 0.5  # seconds between looks at the heroes' health


def fire_abilities_when_hurt(game: Game, cards: list[Card], cfg: Config) -> None:
    """Watch each placed hero's health bar and fire its ability once it drops
    to ``ability_health``, or when its bar disappears (about to fall), or at
    ``ability_max_wait`` at the latest."""
    acfg = cfg.attack
    waiting = list(cards)
    deadline = game.clock() + acfg.ability_max_wait
    while waiting:
        screen = game.refresh()
        if pick_reward(game):
            continue
        late = game.clock() >= deadline
        for card in list(waiting):
            health = armybar.hero_health(screen, card)
            if late or health is None or health <= acfg.ability_health:
                game.tap(*card.center)
                game.wait(0.15)
                waiting.remove(card)
                log.info("Ability at x=%d: %s", card.left, "time's up" if late else
                         "health gone" if health is None else f"health {health:.0%}")
        if waiting:
            game.wait(ABILITY_CHECK)
