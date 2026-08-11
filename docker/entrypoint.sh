#!/usr/bin/env bash
# Start a virtual display, then the app.
#
# headless=True is detected on these targets (verified: the challenge does not clear), so the browser
# runs headful. Xvfb provides the display it needs without a real one.
set -euo pipefail

: "${DISPLAY:=:99}"
export DISPLAY

# The unix socket must exist and be writable: local X clients connect via /tmp/.X11-unix/X<n>,
# and the container runs unprivileged.
mkdir -p /tmp/.X11-unix 2>/dev/null || true
chmod 1777 /tmp/.X11-unix 2>/dev/null || true

# 1920x1080x24 matches the LINUX_1080P device preset. A mismatch between the framebuffer and the
# advertised screen size would be one more incoherent signal.
#
# Only TCP listening is disabled. Passing `-nolisten unix` as well would leave Xvfb running with no
# usable transport at all, and Chrome then fails with "Missing X server or $DISPLAY" — which looks
# like a Chrome problem and is not.
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1920x1080}"
export SCREEN_GEOMETRY                     # read back by the minter to size the browser window
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}x24" -nolisten tcp &
XVFB_PID=$!

# Verify, and fail loudly. Continuing without a display just defers the error into a confusing
# browser crash several seconds later.
ready=0
for _ in $(seq 1 100); do
  if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then ready=1; break; fi
  kill -0 "${XVFB_PID}" 2>/dev/null || { echo "[entrypoint] FATAL: Xvfb exited during startup" >&2; exit 1; }
  sleep 0.1
done
if [ "${ready}" -ne 1 ]; then
  echo "[entrypoint] FATAL: Xvfb did not become ready on ${DISPLAY} after 10s" >&2
  echo "[entrypoint] check that the xvfb package is installed and /tmp/.X11-unix is writable" >&2
  exit 1
fi
echo "[entrypoint] Xvfb ready on ${DISPLAY} (pid ${XVFB_PID}, $(xdpyinfo -display "${DISPLAY}" | awk '/dimensions:/{print $2}'))" >&2
# Optional live view of the virtual display.
#
# DEV ONLY, and off by default: it exposes an unauthenticated view of the browser. Two uses, both
# valuable during development:
#   1. watch what the mint actually does, instead of inferring it from a screenshot after the fact
#   2. solve a challenge BY HAND once — the profile is then warm, and the volume mount keeps it that
#      way, which is the cheapest route to a working container demo
#
# A window manager, because its absence is itself a signal. With no WM there is nothing to map,
# focus, or decorate windows: outerWidth/outerHeight stop bracketing innerWidth/innerHeight the way a
# real browser's do, screenX/screenY sit at the origin, and focus events never fire normally. openbox
# is ~1MB and makes all of that behave like an ordinary Linux desktop.
if command -v openbox >/dev/null 2>&1; then
  openbox --sm-disable >/dev/null 2>&1 &
  echo "[entrypoint] openbox started (window manager)" >&2
else
  echo "[entrypoint] WARNING: no window manager installed; window geometry will look abnormal" >&2
fi

# The panel. Started after the window manager because it needs one to negotiate its strut with; that
# strut is what shrinks _NET_WORKAREA and therefore screen.availHeight.
if command -v tint2 >/dev/null 2>&1; then
  tint2 -c /etc/tint2rc >/dev/null 2>&1 &
  # Give it a moment to map and claim its space before anything reads the work area.
  sleep 1
  echo "[entrypoint] tint2 panel started (reserves work area so availHeight < height)" >&2
else
  echo "[entrypoint] WARNING: tint2 not installed; screen.availHeight will equal screen.height" >&2
fi

# dbus. Chrome looks for the SYSTEM bus at /run/dbus/system_bus_socket and logs a wall of errors
# when it is missing. Starting it needs root, so this is best-effort: the errors are noisy rather
# than fatal, and the container otherwise runs unprivileged.
if command -v dbus-daemon >/dev/null 2>&1; then
  mkdir -p /tmp/dbus
  dbus-daemon --session --fork --print-address > /tmp/dbus/address 2>/dev/null || true
  export DBUS_SESSION_BUS_ADDRESS="$(cat /tmp/dbus/address 2>/dev/null || true)"
  if [ -w /run ] 2>/dev/null; then
    mkdir -p /run/dbus && dbus-daemon --system --fork 2>/dev/null || true
  fi
fi

# Honour an explicit command, e.g. `docker run ... bash`, instead of always starting the API.
# Without this the image discards its arguments and always starts the API, which silently turns every
# diagnostic invocation into "start the server" — the command appears to run and never does.
#
# exec, so the given process becomes PID 1 and receives signals directly. The graceful-shutdown
# handling below is specific to the long-running service and is not wanted for a one-off command.
if [ "$#" -gt 0 ]; then
  echo "[entrypoint] running explicit command: $*" >&2
  exec "$@"
fi

# Forward signals so shutdown stays graceful: the app closes its transports and stops the reaper
# rather than leaving an orphaned Chromium behind.
term() {
  echo "[entrypoint] shutting down" >&2
  kill -TERM "${APP_PID}" 2>/dev/null || true
  wait "${APP_PID}" 2>/dev/null || true
  kill -TERM "${XVFB_PID}" 2>/dev/null || true
}
trap term TERM INT

exec_app() { python -m crawling_reviews.api & APP_PID=$!; }
exec_app
wait "${APP_PID}"
