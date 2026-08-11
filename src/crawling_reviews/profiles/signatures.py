"""Browser signatures — immutable fingerprint templates."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass, field, replace
from typing import Literal

log = logging.getLogger(__name__)

Platform = Literal["macOS", "Linux", "Windows"]


def detect_platform() -> Platform:
    """The OS we are actually running on."""
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("win"):
        return "Windows"
    return "Linux"


@dataclass(frozen=True)
class DescribedHardware:
    """Hardware a signature is meant to represent, recorded but never presented."""

    hardware_concurrency: int
    device_memory_gb: int


@dataclass(frozen=True)
class DevicePreset:
    """A coherent device description."""

    # APPLIED: these reach the browser. viewport_* becomes --window-size, screen_* is checked
    # against
    # the real display, device_scale_factor documents the expected dpr for that pairing.
    viewport_width: int
    viewport_height: int
    screen_width: int
    screen_height: int
    device_scale_factor: int

    # DESCRIBED, never spoofed.
    described: DescribedHardware


@dataclass(frozen=True)
class Signature:
    """One fingerprint template. Frozen: signatures are configuration, not state."""

    id: str
    chrome_version: int
    impersonate_key: str          # curl_cffi profile used to replay this signature's cookies
    device: DevicePreset
    #: The OS this signature is coherent with. Enforced by active_signatures(), and echoed to the
    #: HTTP tier as sec-ch-ua-platform so the client hints never contradict the User-Agent.
    platform: Platform = "macOS"
    # Only a last-resort fallback for when the egress cannot be resolved at all; the real values
    # come from geo, because language has to follow the exit and not the signature.
    locale: str = "en-US"
    accept_language: str = "en-US,en;q=0.9"

    # Exactly one of these selects the browser binary.
    channel: str | None = None
    executable_path: str | None = None

    # Optional egress.
    proxy_url: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None

    # Notes surfaced in diagnostics.
    tags: tuple[str, ...] = field(default_factory=tuple)

    def is_available(self) -> bool:
        """Can this signature actually be launched on this machine?"""
        if self.platform != detect_platform():
            return False
        if self.executable_path:
            return shutil.which(self.executable_path) is not None or _file_exists(self.executable_path)
        return True  # channel="chrome" is resolved by Playwright at launch

    def sec_ch_ua_platform(self) -> str:
        """Value for the sec-ch-ua-platform header, quoted as the spec requires."""
        return f'"{self.platform}"'


def _file_exists(path: str) -> bool:
    from pathlib import Path

    return Path(path).exists()


def display_geometry() -> tuple[int, int] | None:
    """The real display size, when something authoritative says what it is."""
    raw = os.getenv("SCREEN_GEOMETRY", "")
    if "x" in raw:
        try:
            width, height = raw.split("x")[:2]
            return int(width), int(height)
        except ValueError:
            pass
    return None


def effective_device(signature: "Signature") -> DevicePreset:
    """The signature's device with screen size corrected to the real display."""
    display = display_geometry()
    device = signature.device
    if display is None or (device.screen_width, device.screen_height) == display:
        return device

    width, height = display
    log.warning(
        "signature %s declares a %dx%d screen but the display is %dx%d; using the display, since "
        "screen.width/height comes from it. Update the preset to match.",
        signature.id, device.screen_width, device.screen_height, width, height,
    )
    return replace(
        device,
        screen_width=width, screen_height=height,
        viewport_width=min(device.viewport_width, width),
        viewport_height=min(device.viewport_height, height),
    )


# --- device presets ----------------------------------------------------------------------------

MBP_14 = DevicePreset(
    viewport_width=1440, viewport_height=900,
    screen_width=1440, screen_height=900,
    device_scale_factor=2,
    described=DescribedHardware(hardware_concurrency=8, device_memory_gb=8),
)

MBP_16 = DevicePreset(
    viewport_width=1512, viewport_height=982,
    screen_width=1512, screen_height=982,
    device_scale_factor=2,
    described=DescribedHardware(hardware_concurrency=12, device_memory_gb=16),
)

# Linux/container shapes.
LINUX_1080P = DevicePreset(
    viewport_width=1600, viewport_height=900,
    screen_width=1920, screen_height=1080,
    device_scale_factor=1,
    described=DescribedHardware(hardware_concurrency=4, device_memory_gb=8),
)

LINUX_1440 = DevicePreset(
    viewport_width=1366, viewport_height=768,
    screen_width=1920, screen_height=1080,
    device_scale_factor=1,
    described=DescribedHardware(hardware_concurrency=8, device_memory_gb=8),
)

MAC_WINDOWED = DevicePreset(
    viewport_width=1280, viewport_height=720,
    screen_width=1440, screen_height=900,
    device_scale_factor=2,
    described=DescribedHardware(hardware_concurrency=8, device_memory_gb=8),
)


# --- container Chrome paths ---------------------------------------------------------------------

LINUX_CHROME_PRIMARY = "/opt/chrome/151/chrome"
LINUX_CHROME_SECONDARY = "/opt/chrome/150/chrome"


# --- signatures ---------------------------------------------------------------------------------

SIGNATURES: list[Signature] = [
    Signature(
        id="sig-chrome-system-mbp14",
        chrome_version=149,
        impersonate_key="chrome146",   # newest in curl_cffi 0.16; a 3-version gap is tolerated
        device=MBP_14,
        platform="macOS",
        channel="chrome",
        tags=("system-chrome", "default"),
    ),
    Signature(
        id="sig-chrome-system-mbp16",
        chrome_version=149,
        impersonate_key="chrome146",
        device=MBP_16,
        platform="macOS",
        locale="en-GB",
        accept_language="en-GB,en;q=0.9",
        channel="chrome",
        tags=("system-chrome",),
    ),
]


# --- Linux / container signatures ---------------------------------------------------------------

SIGNATURES += [
    Signature(
        id="sig-linux-chrome151-1080p",
        chrome_version=149,
        impersonate_key="chrome146",
        device=LINUX_1080P,
        platform="Linux",
        executable_path=LINUX_CHROME_PRIMARY,
        tags=("container", "chrome-for-testing"),
    ),
    Signature(
        id="sig-linux-chrome150-1440",
        chrome_version=146,
        impersonate_key="chrome146",   # exact match: this build and this profile are the same version
        device=LINUX_1440,
        platform="Linux",
        executable_path=LINUX_CHROME_SECONDARY,
        tags=("container", "chrome-for-testing"),
    ),
]


# --- additional signatures, ready to enable -----------------------------------------------------


def active_signatures() -> list[Signature]:
    """Signatures that can actually be launched here. Unavailable binaries are skipped, not fatal."""
    return [s for s in SIGNATURES if s.is_available()]


def get_signature(signature_id: str) -> Signature:
    for sig in SIGNATURES:
        if sig.id == signature_id:
            return sig
    raise KeyError(f"unknown signature: {signature_id}")
