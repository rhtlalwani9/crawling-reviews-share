"""Human-like input for the mint step."""

from __future__ import annotations

import math
import random
import time

from ...core.logging import get_logger

log = get_logger(__name__)


# One compositor frame at 60Hz. Chrome coalesces mousemove delivery to the frame rate, so this is
# the
# interval a real cursor reports at — sampling faster buys nothing, sampling slower is visible.
_FRAME_S = 1 / 60
# No single frame of hand movement covers much ground at 60Hz. Keeps a fast flick from becoming a
# jump.
_MAX_STEP_PX = 8.0


def _ease_in_out(t: float) -> float:
    """Cubic ease. Velocity starts near zero, peaks mid-travel, and settles — like a hand."""
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def _bezier(p0, p1, p2, p3, t):
    """Cubic Bezier point. Two random control points give the path natural curvature."""
    u = 1 - t
    x = (u ** 3) * p0[0] + 3 * (u ** 2) * t * p1[0] + 3 * u * (t ** 2) * p2[0] + (t ** 3) * p3[0]
    y = (u ** 3) * p0[1] + 3 * (u ** 2) * t * p1[1] + 3 * u * (t ** 2) * p2[1] + (t ** 3) * p3[1]
    return x, y


def viewport_of(page) -> tuple[int, int]:
    """Actual inner size. `page.viewport_size` is None when launched with no_viewport."""
    try:
        w, h = page.evaluate("() => [window.innerWidth, window.innerHeight]")
        return int(w or 1280), int(h or 720)
    except Exception:
        return 1280, 720


def move_to(page, x: float, y: float, *, start: tuple[float, float] | None = None,
            duration_s: float | None = None) -> tuple[float, float]:
    """Move the cursor along a curved, eased path with jittered timing."""
    sx, sy = start if start else (random.uniform(0, 200), random.uniform(0, 200))
    distance = math.hypot(x - sx, y - sy)

    # Longer travel takes longer, but sub-linearly, and never instantaneously.
    duration_s = duration_s if duration_s is not None else min(1.2, 0.15 + distance / 1400)

    # Step count is derived from the frame interval, not capped at an arbitrary number.
    steps = max(6, int(duration_s / _FRAME_S))
    # Independently cap the per-event distance: a fast flick still has to be sampled finely, or the
    # path is a series of teleports rather than a movement.
    steps = max(steps, int(distance / _MAX_STEP_PX))

    # Control points pushed off the straight line, on alternating sides, so the arc looks
    # incidental
    # rather than symmetrical.
    spread = max(20.0, distance * 0.25)
    c1 = (sx + (x - sx) * 0.3 + random.uniform(-spread, spread),
          sy + (y - sy) * 0.3 + random.uniform(-spread, spread))
    c2 = (sx + (x - sx) * 0.7 + random.uniform(-spread, spread),
          sy + (y - sy) * 0.7 + random.uniform(-spread, spread))

    # Paced against a fixed timeline rather than by sleeping a constant amount after each call.
    interval = duration_s / steps
    origin = time.perf_counter()

    for i in range(1, steps + 1):
        t = _ease_in_out(i / steps)
        px, py = _bezier((sx, sy), c1, c2, (x, y), t)
        # Sub-pixel tremor: real pointer traces are not perfectly smooth.
        px += random.uniform(-0.6, 0.6)
        py += random.uniform(-0.6, 0.6)
        try:
            page.mouse.move(px, py)
        except Exception:
            break
        # A few percent of jitter, because a device's polling is near-constant but not exact.
        target = origin + i * interval * random.gauss(1.0, 0.04)
        remaining = target - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)

    return x, y


def idle_drift(page, position: tuple[float, float], *, duration_s: float = 0.8) -> tuple[float, float]:
    """Small aimless movement around a point. A resting hand still produces events."""
    x, y = position
    deadline = time.time() + duration_s
    while time.time() < deadline:
        x, y = move_to(page, x + random.uniform(-25, 25), y + random.uniform(-18, 18),
                       start=(x, y), duration_s=random.uniform(0.08, 0.25))
        time.sleep(random.uniform(0.05, 0.3))
    return x, y


# Chrome reports one mouse-wheel notch as a fixed number of pixels — 100 on Linux and Windows.
_WHEEL_NOTCH_PX = 100


def scroll(page, *, amount: int | None = None) -> None:
    """A short scroll, delivered as whole wheel notches."""
    direction = 1 if amount is None or amount >= 0 else -1
    target = abs(amount) if amount is not None else random.randint(200, 600)

    remaining_notches = max(1, round(target / _WHEEL_NOTCH_PX))
    while remaining_notches > 0:
        notches = min(remaining_notches, random.choice([1, 1, 1, 2, 2, 3]))
        try:
            page.mouse.wheel(0, direction * notches * _WHEEL_NOTCH_PX)
        except Exception:
            return
        remaining_notches -= notches
        time.sleep(random.uniform(0.12, 0.4))


def warm_up(page, *, duration_s: float = 3.0) -> None:
    """A few seconds of ordinary-looking activity: land the cursor, wander, scroll, pause."""
    width, height = viewport_of(page)
    started = time.time()

    # Enter from an edge rather than materialising mid-screen.
    pos = (random.uniform(0, width * 0.2), random.uniform(height * 0.5, height))
    try:
        page.mouse.move(*pos)
    except Exception:
        return

    while time.time() - started < duration_s:
        target = (random.uniform(width * 0.15, width * 0.85),
                  random.uniform(height * 0.15, height * 0.85))
        pos = move_to(page, *target, start=pos)
        pos = idle_drift(page, pos, duration_s=random.uniform(0.2, 0.7))
        if random.random() < 0.5:
            scroll(page)

    log.debug("humanised warm-up finished",
              extra_fields={"seconds": round(time.time() - started, 1),
                            "viewport": f"{width}x{height}"})


def click_like_human(page, selector: str, *, start: tuple[float, float] | None = None) -> bool:
    """Click a little off-centre, after moving to it."""
    try:
        box = page.locator(selector).first.bounding_box()
        if not box:
            return False
        # Stay within the middle 60% so the click still lands on the element.
        tx = box["x"] + box["width"] * random.uniform(0.2, 0.8)
        ty = box["y"] + box["height"] * random.uniform(0.2, 0.8)
        # `start` matters: without it move_to invents a random origin and the cursor jumps there
        # first, which is exactly the discontinuity this module exists to avoid.
        move_to(page, tx, ty, start=start)
        time.sleep(random.uniform(0.08, 0.25))   # humans pause before committing
        page.mouse.click(tx, ty)
        return True
    except Exception:
        return False


# --- continuous activity, interleaved with waiting -----------------------------------------------


class Presence:
    """Tracks a plausible cursor across a whole session."""

    def __init__(self, page):
        self.page = page
        width, height = viewport_of(page)
        self.width, self.height = width, height
        # Start somewhere a cursor plausibly already is, rather than dead centre.
        self.pos = (random.uniform(width * 0.1, width * 0.6),
                    random.uniform(height * 0.3, height * 0.9))
        self._scrolled = 0
        try:
            page.mouse.move(*self.pos)
        except Exception:
            pass

    def tick(self) -> None:
        """One short unit of plausible activity, roughly 0.2-1.2s."""
        roll = random.random()
        try:
            if roll < 0.35:
                # Resting hand: micro-movement around the current point.
                self.pos = idle_drift(self.page, self.pos, duration_s=random.uniform(0.15, 0.45))
            elif roll < 0.65:
                # Purposeful move to somewhere on the page.
                target = (random.uniform(self.width * 0.1, self.width * 0.9),
                          random.uniform(self.height * 0.1, self.height * 0.9))
                self.pos = move_to(self.page, *target, start=self.pos)
            elif roll < 0.80:
                # Reading behaviour: scroll a little, but not endlessly in one direction.
                if self._scrolled < 1600:
                    amount = random.randint(100, 300)
                    scroll(self.page, amount=amount)
                    self._scrolled += amount
                else:
                    scroll(self.page, amount=-random.randint(100, 300))
                    self._scrolled = max(0, self._scrolled - 200)
            else:
                # Doing nothing is also human, and matters: uninterrupted activity is its own
                # signal.
                time.sleep(random.uniform(0.2, 0.9))
        except Exception:
            # A navigation mid-tick invalidates the page; that is expected, not an error.
            pass


def reload_like_human(page, presence: "Presence | None" = None) -> None:
    """Reload via a keypress instead of a scripted navigation."""
    if presence:
        presence.tick()          # a beat of hesitation before acting
    time.sleep(random.uniform(0.2, 0.7))
    try:
        page.keyboard.press("F5")
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
    except Exception:
        # Fall back to the scripted navigation rather than failing the mint over the mechanism.
        try:
            page.reload(wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass


def wait_with_presence(page, selector: str, timeout_s: float, *,
                       presence: "Presence | None" = None, humanize_enabled: bool = True):
    """Wait for `selector` while behaving like someone is at the keyboard."""
    # Reuse the caller's Presence when given one. Creating a fresh instance mid-session would pick
    # a
    # new random start position, and the cursor would teleport — undoing the point of the exercise.
    if presence is None and humanize_enabled:
        presence = Presence(page)
    started = time.time()
    deadline = started + timeout_s

    while time.time() < deadline:
        try:
            if page.query_selector(selector):
                return True, time.time() - started
        except Exception:
            pass  # mid-navigation; try again next loop

        if presence:
            presence.tick()
        else:
            time.sleep(0.3)

    return False, time.time() - started
