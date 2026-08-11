#!/usr/bin/env bash
# Prepare a warmed Chrome profile for shipping/mounting.
#
# Two things must happen before a profile can be handed to another machine or container:
#
#  1. Remove the singleton lock files. A profile copied while Chrome was running keeps a stale
#     SingletonLock/Cookie/Socket, and Chrome refuses to start on it.
#  2. Match ownership to the container user, or Chrome cannot take the lock at all.
#
# What travels and what does not:
#     travels     -> browsing history, localStorage, site settings, accumulated trust
#     does NOT    -> cf_clearance. It is IP-bound, so the recipient still pays one challenge.
# That is still a large improvement: an aged profile clears challenges a fresh one cannot.
#
#   ./docker/prepare-profile.sh src/crawling_reviews/profiles/_data/Linux/chrome-profiles/prof-149-abc123
set -euo pipefail

TARGET="${1:?usage: prepare-profile.sh <profile-dir>}"
UID_GID="${2:-10001:10001}"     # must match APP_UID:APP_GID in base.Dockerfile

[ -d "${TARGET}" ] || { echo "not a directory: ${TARGET}" >&2; exit 1; }

echo "[prepare] stripping singleton locks in ${TARGET}"
find "${TARGET}" -maxdepth 2 \
  \( -name 'SingletonLock' -o -name 'SingletonCookie' -o -name 'SingletonSocket' \) \
  -print -delete 2>/dev/null || true

# Caches are large and worthless to a recipient; trust lives in cookies and history, not in cache.
echo "[prepare] pruning caches"
for d in "Default/Cache" "Default/Code Cache" "Default/GPUCache" "GrCache" "ShaderCache" "GraphiteDawnCache"; do
  rm -rf "${TARGET:?}/${d}" 2>/dev/null || true
done

if command -v sudo >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
  echo "[prepare] chown -> ${UID_GID}"
  chown -R "${UID_GID}" "${TARGET}"
else
  echo "[prepare] NOTE: run as root (or in the container) to chown to ${UID_GID};"
  echo "          otherwise Chrome in the container cannot lock this profile."
fi

echo "[prepare] size: $(du -sh "${TARGET}" | cut -f1)"
echo "[prepare] done"
