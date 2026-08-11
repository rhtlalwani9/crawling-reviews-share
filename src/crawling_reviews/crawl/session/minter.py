"""Session minting — the one expensive operation in the system."""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass

from ...config import config
from ...core.errors import MintError
from ...core.logging import get_logger
from ...profiles.models import Profile
from ...profiles.signatures import Signature, effective_device
from ...profiles.store import clear_stale_singleton_locks
from . import geo, humanize
from .models import Session

log = get_logger(__name__)

CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


@dataclass(frozen=True)
class MintSpec:
    """What a source needs in order to be minted."""

    url: str                       # page to land on; the target itself is usually best
    wait_selector: str             # proof the challenge cleared
    settle_s: float = 2.0          # let trailing cookie writes land
    extract_csrf: bool = True


def build_launch_config(profile: Profile, signature: Signature, *, headless: bool,
                        egress: dict | None = None) -> dict:
    """The exact launch configuration a mint uses."""
    # The display is authoritative for screen size, not the preset — see effective_device.
    device = effective_device(signature)
    # Caller-supplied so the browser flags and the Session record the same language. Resolved here
    # only
    # when nobody supplied one, which keeps the function usable on its own for diagnostics.
    if egress is None:
        egress = geo.egress_profile(fallback_locale=signature.locale)
    # Window geometry, from the device preset.
    screen_w, screen_h = device.screen_width, device.screen_height
    win_w = min(device.viewport_width, screen_w)
    win_h = min(device.viewport_height, screen_h - 74)      # leave room for browser chrome
    # Offset so screenX/screenY are not 0,0 — a window pinned to the exact origin is a tell, and
    # nobody positions their browser there by hand.
    pos_x = max(0, min(60, screen_w - win_w))
    pos_y = max(0, min(40, screen_h - win_h))

    args = [
        "--disable-blink-features=AutomationControlled",
        f"--window-size={win_w},{win_h}",
        f"--window-position={pos_x},{pos_y}",
        # Language via Chrome's own flags rather than Playwright's `locale` option.
        f"--accept-lang={egress['languages']}",
        f"--lang={egress['locale']}",
    ]

    # Container-specific flags.
    if signature.platform == "Linux":
        args += [
            "--no-sandbox",                    # no user namespaces in most container runtimes
            "--disable-dev-shm-usage",         # /dev/shm is small unless shm_size is raised
        ]
        # Which software renderer, and it matters more than it looks.
        if os.getenv("CHROME_GL", "swiftshader").lower() == "mesa":
            args += ["--use-gl=angle", "--use-angle=gl"]
        else:
            args += [
                "--use-gl=swiftshader",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",   # newer Chrome refuses software WebGL without this
            ]

    # Timezone follows the *egress*, not the signature: a UTC clock behind an Indian residential IP
    # is a contradiction, and cross-signal mismatches are what environmental scoring is built to
    # catch.
    launch_kwargs: dict = {
        "user_data_dir": profile.user_data_dir,
        "headless": headless,
        "no_viewport": True,             # patchright guidance: avoid the automation-shaped viewport
        "args": args,
    }
    if egress["timezone"]:
        launch_kwargs["timezone_id"] = egress["timezone"]

    # The locale environment, which is what Chrome's ICU default actually follows — not --lang, and
    # not --accept-lang.
    if egress.get("posix_locale"):
        launch_kwargs["env"] = {
            **os.environ,
            "LANG": egress["posix_locale"],
            "LC_ALL": egress["posix_locale"],
        }
    # Exactly one of these selects the binary; see Signature.
    if signature.executable_path:
        launch_kwargs["executable_path"] = signature.executable_path
    else:
        launch_kwargs["channel"] = signature.channel or "chrome"

    if signature.proxy_url:
        # Egress and cookie jar are one inseparable unit: clearance tokens are IP-bound, so a
        # session minted through this exit is only replayable through the same exit.
        launch_kwargs["proxy"] = {
            "server": signature.proxy_url,
            "username": signature.proxy_username,
            "password": signature.proxy_password,
        }

    return launch_kwargs


class SessionMinter:
    """Turns a leased profile into a Session. Stateless; the browser lives only for the mint."""

    def mint(self, profile: Profile, signature: Signature, spec: MintSpec) -> Session:
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment problem, not logic
            raise MintError(
                "patchright is not installed. Run: pip install patchright && patchright install chromium",
                cause=exc,
            ) from exc

        started = time.time()
        device = signature.device
        log.info(
            "minting session",
            extra_fields={
                "profile_id": profile.id, "signature_id": signature.id,
                "url": spec.url, "headless": config.session.headless,
            },
        )

        egress = geo.egress_profile(fallback_locale=signature.locale)
        launch_kwargs = build_launch_config(
            profile, signature, headless=config.session.headless, egress=egress,
        )

        try:
            with sync_playwright() as pw:
                # A lock from a previous container names a hostname that can never match again, so
                # Chrome would refuse the directory outright. See clear_stale_singleton_locks.
                clear_stale_singleton_locks(profile.user_data_dir)

                ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()

                    # NOT overridden: navigator.hardwareConcurrency.

                    # Establish presence BEFORE navigating.
                    presence = humanize.Presence(page) if config.session.humanize else None
                    if presence:
                        presence.tick()

                    page.goto(spec.url, wait_until="domcontentloaded", timeout=60_000)

                    # Wait for the content while continuously behaving like someone is present.
                    cleared, waited = humanize.wait_with_presence(
                        page,
                        spec.wait_selector,
                        timeout_s=config.session.wait_timeout_s,
                        presence=presence,
                    )

                    # A single reload as a last resort, still under continuous presence.
                    if not cleared and config.session.challenge_attempts > 1:
                        log.info(
                            "content absent, reloading once while staying active",
                            extra_fields={"profile_id": profile.id, "waited_s": round(waited, 1)},
                        )
                        try:
                            # F5 through the input pipeline rather than Page.reload.
                            humanize.reload_like_human(page, presence)
                            cleared, extra = humanize.wait_with_presence(
                                page,
                                spec.wait_selector,
                                timeout_s=config.session.wait_timeout_s,
                                presence=presence,
                            )
                            waited += extra
                        except Exception:
                            pass

                    if cleared:
                        log.info(
                            "challenge cleared",
                            extra_fields={"profile_id": profile.id, "waited_s": round(waited, 1)},
                        )
                    else:
                        log.warning(
                            "expected content never appeared during mint",
                            extra_fields={
                                "profile_id": profile.id,
                                "selector": spec.wait_selector,
                                "waited_s": round(waited, 1),
                            },
                        )
                        if config.session.capture_failures:
                            _capture_failure(page, profile.id)

                    # No extra warm-up here: presence ran throughout the wait above, so the session
                    # already has a continuous activity history rather than a burst at the end.
                    time.sleep(spec.settle_s)

                    html = page.content()
                    cookies = ctx.cookies()
                    user_agent = page.evaluate("() => navigator.userAgent")
                finally:
                    ctx.close()
        except MintError:
            raise
        except Exception as exc:
            raise MintError(f"browser mint failed for {profile.id}: {exc}", cause=exc) from exc

        if not cleared:
            # Deliberately a failure. Returning an uncleared session would send the crawler off to
            # make HTTP requests that are certain to be refused, burning the IP for nothing.
            raise MintError(
                f"mint did not clear the challenge for {spec.url} "
                f"(selector {spec.wait_selector!r} never appeared)"
            )

        csrf = None
        if spec.extract_csrf:
            match = CSRF_META.search(html)
            csrf = match.group(1) if match else None

        session = Session(
            host=_host_of(spec.url),
            profile_id=profile.id,
            cookies=[{"name": c["name"], "value": c["value"]} for c in cookies],
            csrf_token=csrf,
            user_agent=user_agent,
            impersonate_key=signature.impersonate_key,
            sec_ch_ua_platform=signature.sec_ch_ua_platform(),
            # Carried forward so every replayed request matches the browser that earned the
            # cookies.
            accept_language=egress["accept_language"],
            ttl_s=config.session.ttl_s,
        )

        log.info(
            "session minted",
            extra_fields={**session.summary(), "mint_ms": int((time.time() - started) * 1000)},
        )
        return session, html   # the mint page is page 1; returning it avoids re-fetching


def _capture_failure(page, profile_id: str) -> None:
    """Save the page and a screenshot when a mint fails."""
    from datetime import datetime
    from pathlib import Path

    try:
        out_dir = Path(config.profiles.data_dir) / "diagnostics"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = out_dir / f"{profile_id}-{stamp}"

        html = page.content()
        base.with_suffix(".html").write_text(html, encoding="utf-8")
        page.screenshot(path=str(base.with_suffix(".png")), full_page=False)

        title = page.title()
        lower = html.lower()

        # Read the fingerprint signals that differ most between a container and a real desktop, so
        # a
        # failure capture explains itself instead of needing a separate experiment.
        try:
            fp = page.evaluate("""() => {
                const out = {};
                try {
                    const gl = document.createElement('canvas').getContext('webgl');
                    const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
                    out.webgl = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)
                                    : (gl ? 'no-debug-ext' : 'unavailable');
                } catch (e) { out.webgl = 'error'; }
                out.platform = navigator.platform;
                out.cores = navigator.hardwareConcurrency;
                out.screen = screen.width + 'x' + screen.height + '@' + window.devicePixelRatio;
                return out;
            }""")
        except Exception:
            fp = {}

        # Name the vendor and, more importantly, distinguish a *self-resolving* challenge from a
        # *hard block*.
        markers = {
            "vendor": (
                "datadome" if ("datadome" in lower or "captcha-delivery" in lower)
                else "cloudflare" if any(m in lower for m in ("cf-chl", "challenge-platform", "just a moment"))
                else "recaptcha" if "g-recaptcha" in lower
                else "turnstile" if "turnstile" in lower
                else "unknown"
            ),
            # Waiting can help: a JS challenge that resolves itself.
            "self_resolving_challenge": any(m in lower for m in
                                            ("just a moment", "cf-chl", "challenge-platform")),
            # Waiting cannot help: interactive or outright refusal.
            "interactive_captcha": any(m in lower for m in
                                       ("captcha-delivery", "g-recaptcha", "hcaptcha")),
            "hard_block": len(html) < 20_000,
        }
        if markers["hard_block"] or markers["interactive_captcha"]:
            log.warning(
                "this is not a timeout - the egress is refused. Raising SESSION_WAIT_TIMEOUT_S will "
                "not help; it needs different egress (proxy) or a pre-warmed profile.",
                extra_fields={"profile_id": profile_id, "vendor": markers["vendor"]},
            )
        log.warning(
            "mint failure captured",
            extra_fields={
                "profile_id": profile_id, "title": title[:80], "bytes": len(html),
                "saved": str(base.with_suffix(".html")), **markers, **fp,
            },
        )
    except Exception as exc:  # diagnostics must never mask the original failure
        log.debug("could not capture mint failure", extra_fields={"error": str(exc)})


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc
