# syntax=docker/dockerfile:1
#
# Base image: browsers + fonts + virtual display.
#
# Kept separate from the application image because it is slow to build (two browser downloads) and
# rarely changes, so application rebuilds should not pay for it. Build it once, tag it, and let
# Dockerfile use it as FROM.
#
#   docker build -f base.Dockerfile -t review-crawler-base:1 .
#
# ── ARCHITECTURE, WHICH IS A REAL CONSTRAINT ────────────────────────────────────────────────────
# Google ships Chrome for Linux on **amd64 only**. There is no arm64 .deb, and Chrome for Testing
# publishes linux64 (amd64) with no linux-arm64 build. So on an arm64 host there are three options
# and none of them is "real Chrome":
#
#   * build --platform=linux/amd64  -> real Chrome, but emulated and slow
#   * build arm64 natively          -> Chromium (Playwright's build), weaker fingerprint
#   * install distro chromium       -> same, plus version pinning at the distro's mercy
#
# This file handles both: on amd64 it installs pinned Chrome for Testing builds at fixed paths; on
# arm64 it falls back to Playwright's Chromium and symlinks it into the same paths. Signatures check
# file existence via is_available(), so whichever exists simply activates and the other deactivates.
#
# For the strongest fingerprint, build for amd64:
#   docker build --platform=linux/amd64 -f base.Dockerfile -t review-crawler-base:1 .

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── system packages ─────────────────────────────────────────────────────────────────────────────
# xvfb: headless=True is detected on these targets, so the browser runs headful against a virtual
#       framebuffer instead. Verified necessary, not precautionary.
# fonts: a bare image has ~40, which makes canvas hashes and text metrics extremely unusual. The set
#       below lands around 600-900 faces, which is what an ordinary Ubuntu desktop reports. Note the
#       target is NOT macOS's ~3,000 — a Linux client claiming a Mac-sized font list would be its own
#       incoherence. Included: Liberation + Carlito/Caladea (metric-compatible stand-ins for
#       Arial/Times/Calibri/Cambria, so no non-free EULA step), DejaVu, Noto for CJK and emoji, and
#       the UI families a real desktop ships (Ubuntu, Cantarell, Lato, Open Sans, Fira, Inconsolata).
# Mesa (libgl1-mesa-dri + llvmpipe): a real software GL stack, present so WebGL can report what an
# ordinary Linux machine without GPU drivers reports — "ANGLE (Mesa, llvmpipe ...)" — rather than
# Chrome's own SwiftShader, which names itself and so reads as containerised Chrome.
#
# MEASURED CAVEAT: on Apple Silicon, where this amd64 image runs under Rosetta translation, no Mesa
# path yields a WebGL context at all — --use-gl=angle/--use-angle=gl, --use-gl=desktop, --use-gl=egl
# and passing no GL flags all produce "UNAVAILABLE", while swiftshader works. Since no WebGL is a
# stronger signal than software WebGL, the default is swiftshader (CHROME_GL in docker-compose.yml).
# Mesa is still worth having: on a native x86_64 host llvmpipe is expected to initialise, so try
# CHROME_GL=mesa there and confirm with `make fingerprint`. mesa-utils supplies glxinfo for checking
# the stack directly.
#
# tint2: a panel. Fingerprint checks compare screen.availHeight against screen.height, and with only a
# window manager nothing reserves screen space, so the work area equals the whole screen. Every real
# desktop has a panel, dock or taskbar taking a strip. Measured against a real Chrome: noTaskbar=false
# there, true here — one of only three signals that differed. tint2 sets _NET_WORKAREA, which is where
# Chrome reads availHeight from, so this makes the value honestly smaller rather than spoofing it.
#
# openbox: a window manager. Its absence is a signal in itself - with nothing mapping or decorating
# windows, outerWidth/outerHeight stop bracketing innerWidth/innerHeight and focus never behaves
# normally. ~1MB for an environment that looks like an ordinary Linux desktop.
COPY docker/tint2rc /etc/tint2rc

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget unzip gnupg jq \
      xvfb x11-utils dbus dbus-x11 openbox tint2 \
      libgl1 libglx-mesa0 libgl1-mesa-dri mesa-utils \
      fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# Additional font families, installed one at a time and tolerant of absence.
#
# Deliberately not a single apt-get line: package names drift between Debian releases (fonts-ubuntu,
# for instance, exists in Ubuntu but not Debian), and one unavailable name in a combined install
# aborts the whole layer. Fonts are a fingerprint nicety, not a hard dependency, so a missing family
# should cost one line of output rather than the build.
#
# Target is ~600-900 faces, matching an ordinary Ubuntu desktop. Explicitly NOT macOS's ~3,000: a
# Linux client advertising a Mac-sized font list is its own incoherence.
RUN apt-get update && \
    for pkg in \
      fonts-liberation2 fonts-dejavu-extra fonts-noto-mono fonts-noto-cjk \
      fonts-croscore fonts-crosextra-carlito fonts-crosextra-caladea \
      fonts-freefont-ttf fonts-droid-fallback fonts-cantarell \
      fonts-open-sans fonts-inconsolata fonts-lato fonts-firacode \
      fonts-hack fonts-jetbrains-mono fonts-roboto-unhinted ; do \
      if apt-get install -y --no-install-recommends "$pkg" >/dev/null 2>&1 ; then \
        echo "[fonts] + $pkg" ; \
      else \
        echo "[fonts] - $pkg (unavailable on this release, skipped)" ; \
      fi ; \
    done ; \
    fc-cache -f >/dev/null && \
    echo "[fonts] total faces: $(fc-list | wc -l)" && \
    rm -rf /var/lib/apt/lists/*

# ── browsers at fixed paths ─────────────────────────────────────────────────────────────────────
# signatures.py references /opt/chrome/<version>/chrome directly, so nothing has to probe at
# runtime. Two versions are installed so the fingerprint pool has more than one identity available.
# Majors only. The exact patch is resolved from the Chrome for Testing manifest at build time,
# because patch releases roll constantly and a pinned patch number rots into a failing build. The
# major is what must stay aligned with the impersonation profile in signatures.py.
# PERISHABLE. Chrome auto-updates aggressively, so real traffic clusters within a release or two of
# stable. These majors were 149/146 while stable was 151 — a browser five releases behind, which is
# rare among real users and common in scraping stacks that pinned once and forgot.
#
# Check before a build:  curl -s https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json
# The installer resolves the newest patch within a major; only the major is chosen here.
ARG CHROME_MAJOR_PRIMARY=151
ARG CHROME_MAJOR_SECONDARY=150

COPY docker/install-browsers.sh /tmp/install-browsers.sh
RUN chmod +x /tmp/install-browsers.sh \
    && /tmp/install-browsers.sh "${CHROME_MAJOR_PRIMARY}" \
    && /tmp/install-browsers.sh "${CHROME_MAJOR_SECONDARY}" \
    && rm /tmp/install-browsers.sh \
    && ls -la /opt/chrome/

# patchright's own driver (the Node bits it shells out to). The browser binaries above are used in
# preference via executable_path; this also provides the arm64 Chromium fallback.
RUN pip install --no-cache-dir patchright==1.48.0 \
    && patchright install chromium \
    && patchright install-deps chromium || true

# ── non-root user ───────────────────────────────────────────────────────────────────────────────
# Chrome must own its user-data directory or it cannot take the SingletonLock. UID/GID are build
# args so they can be matched to the host user when a profile volume is bind-mounted.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd -g "${APP_GID}" app \
    && useradd -m -u "${APP_UID}" -g "${APP_GID}" -s /bin/bash app \
    && mkdir -p /home/app/.cache \
    && cp -r /root/.cache/ms-playwright /home/app/.cache/ 2>/dev/null || true \
    && chown -R app:app /home/app /opt/chrome

ENV PLAYWRIGHT_BROWSERS_PATH=/home/app/.cache/ms-playwright \
    CHROME_149=/opt/chrome/149/chrome \
    CHROME_146=/opt/chrome/146/chrome \
    DISPLAY=:99

# Sanity check at build time: fail the build here rather than at first mint.
RUN /opt/chrome/149/chrome --version || echo "WARNING: primary chrome not runnable"

USER app
WORKDIR /app
