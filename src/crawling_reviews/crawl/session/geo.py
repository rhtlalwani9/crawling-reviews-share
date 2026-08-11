"""Egress-derived locale and timezone."""

from __future__ import annotations

import json
import os
import pathlib
import ssl
import threading
import urllib.request

from ...core.logging import get_logger

log = get_logger(__name__)

# Country code -> locale to claim.
_LOCALE_BY_COUNTRY = {
    # en-GB for the Commonwealth and for regions whose English users overwhelmingly run en-GB.
    "IN": "en-GB", "GB": "en-GB", "AU": "en-GB", "NZ": "en-GB", "IE": "en-GB",
    "SG": "en-GB", "ZA": "en-GB", "AE": "en-GB", "MY": "en-GB", "HK": "en-GB",
    "DE": "en-GB", "FR": "en-GB", "NL": "en-GB", "PL": "en-GB",
    # en-US for the Americas.
    "US": "en-US", "CA": "en-US", "MX": "en-US", "BR": "en-US",
}

# The only locales Chrome can resolve consistently across navigator.language and Intl. A value
# outside
# this set guarantees the two-signal mismatch described above.
_CHROME_APP_LOCALES = frozenset({"en-GB", "en-US"})

# Several providers, tried in order.
_PROVIDERS = (
    ("ipapi.co", "https://ipapi.co/json/", lambda d: (d.get("country_code"), d.get("timezone"), d.get("ip"))),
    ("ipinfo.io", "https://ipinfo.io/json", lambda d: (d.get("country"), d.get("timezone"), d.get("ip"))),
    ("ip-api.com", "http://ip-api.com/json/?fields=status,countryCode,timezone,query",
     lambda d: (d.get("countryCode"), d.get("timezone"), d.get("query"))),
)

_lock = threading.Lock()
_cached: dict | None = None


def _cache_path() -> pathlib.Path:
    """Where the last successful resolution is kept."""
    from ...config import config

    return pathlib.Path(config.profiles.data_dir) / "egress-cache.json"


def _read_disk_cache() -> dict | None:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("timezone") and data.get("locale"):
        data["source"] = f"disk-cache({data.get('source', '?')})"
        return data
    return None


def _write_disk_cache(resolved: dict) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(resolved), encoding="utf-8")
    except Exception as exc:            # a cache that cannot be written is not a reason to fail
        log.debug("could not persist the egress cache: %s", exc)


def _ssl_context() -> "ssl.SSLContext | None":
    """A context with a CA bundle that actually exists."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None            # let urllib use its own default rather than refusing to try


def _lookup() -> dict | None:
    """First provider that answers with a usable country and timezone."""
    context = _ssl_context()
    for name, url, parse in _PROVIDERS:
        try:
            request = urllib.request.Request(url, headers={"accept": "application/json"})
            kwargs = {"timeout": 5}
            if url.startswith("https") and context is not None:
                kwargs["context"] = context
            with urllib.request.urlopen(request, **kwargs) as response:
                data = json.loads(response.read().decode())
            country, timezone, ip = parse(data)
            if not (country and timezone):
                log.debug("%s answered without country/timezone", name)
                continue
            locale = _LOCALE_BY_COUNTRY.get(country.upper(), "en-US")
            if locale not in _CHROME_APP_LOCALES:
                # A guard, not a theoretical one: this is exactly how en-IN got in.
                log.warning(
                    "locale %r is not one Chrome ships an application locale for, so Intl would "
                    "resolve to something else and the two would disagree. Using en-US.", locale,
                )
                locale = "en-US"
            resolved = {
                "timezone": timezone,
                "locale": locale,
                "accept_language": _accept_language(locale),
                "languages": _language_list(locale),
                "posix_locale": _posix_locale(locale),
                "source": name,
            }
            log.info(
                "egress profile resolved",
                extra_fields={"provider": name, "ip": ip, "country": country,
                              "timezone": timezone, "locale": locale},
            )
            return resolved
        except Exception as exc:
            log.debug("%s lookup failed: %s", name, exc)
    return None


def egress_profile(fallback_locale: str = "en-US") -> dict:
    """Return {timezone, locale, accept_language, languages, posix_locale, source}."""
    global _cached

    env_tz = os.getenv("EGRESS_TIMEZONE")
    env_locale = os.getenv("EGRESS_LOCALE")
    if env_tz or env_locale:
        locale = env_locale or fallback_locale
        return {
            "timezone": env_tz or "UTC",
            "locale": locale,
            "accept_language": _accept_language(locale),
            "languages": _language_list(locale),
            "posix_locale": _posix_locale(locale),
            "source": "env",
        }

    with _lock:
        if _cached is not None:
            return _cached

        resolved = _lookup()
        if resolved is not None:
            _write_disk_cache(resolved)
        else:
            resolved = _read_disk_cache()
            if resolved is not None:
                log.warning(
                    "every egress lookup failed; reusing the last known result from disk",
                    extra_fields={"timezone": resolved["timezone"], "locale": resolved["locale"]},
                )

        if resolved is None:
            # Nothing known.
            resolved = {
                "timezone": None,
                "locale": fallback_locale,
                "accept_language": _accept_language(fallback_locale),
                "languages": _language_list(fallback_locale),
                "posix_locale": _posix_locale(fallback_locale),
                "source": "fallback",
            }
            log.warning(
                "egress could not be resolved by any provider and no cache exists, so the browser will "
                "claim %s with the container's own timezone. If the exit is not in that locale this is "
                "an incoherence a detector can see - set EGRESS_TIMEZONE and EGRESS_LOCALE explicitly.",
                fallback_locale,
            )

        _cached = resolved
        return _cached


def _posix_locale(locale: str) -> str:
    """The LANG/LC_ALL value for a BCP-47 locale: en-IN -> en_IN.UTF-8."""
    base, _, region = locale.partition("-")
    return f"{base}_{region.upper()}.UTF-8" if region else f"{base}.UTF-8"


def _language_list(locale: str) -> str:
    """The value for Chrome's `--accept-lang`, e.g."""
    base = locale.split("-")[0]
    return locale if base == locale else f"{locale},{base}"


def _accept_language(locale: str) -> str:
    """Build a plausible Accept-Language from a locale."""
    base = locale.split("-")[0]
    if base == locale:
        return f"{locale},en;q=0.9" if locale != "en" else "en;q=0.9"
    return f"{locale},{base};q=0.9"
