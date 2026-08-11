#!/usr/bin/env bash
# Install one Chrome build at /opt/chrome/<major>/chrome.
#
#   install-browsers.sh <major>          e.g. install-browsers.sh 149
#
# The exact patch version is resolved from Chrome for Testing's manifest at build time rather than
# hardcoded, because patch releases roll constantly and a pinned patch number silently rots into a
# failing build. The MAJOR is what matters: it is what must stay aligned with the curl_cffi
# impersonation profile in signatures.py.
#
# amd64 -> Chrome for Testing: genuine Chrome, pinned, no auto-update.
# arm64 -> Google publishes NO Chrome build for Linux/ARM (verified against the CfT manifest:
#          linux-arm64 does not exist for chrome). Falls back to Playwright's Chromium via a wrapper
#          so the slot still resolves; weaker fingerprint, but native rather than emulated.
set -euo pipefail

MAJOR="${1:?usage: install-browsers.sh <major-version>}"
DEST="/opt/chrome/${MAJOR}"
ARCH="$(dpkg --print-architecture)"
MANIFEST="https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"

mkdir -p "${DEST}"

install_chrome_for_testing() {
  echo "[browsers] resolving newest ${MAJOR}.x linux64 build from Chrome for Testing"
  local url
  url="$(curl -fsSL "${MANIFEST}" | jq -r --arg major "${MAJOR}" '
      [ .versions[]
        | select(.version | startswith($major + "."))
        | . as $v
        | $v.downloads.chrome // []
        | map(select(.platform == "linux64"))
        | select(length > 0)
        | { version: $v.version, url: .[0].url }
      ] | last | .url // empty
  ')"

  if [ -z "${url}" ]; then
    echo "[browsers] no linux64 build found for major ${MAJOR}"
    return 1
  fi

  echo "[browsers] downloading ${url}"
  curl -fsSL -o /tmp/chrome.zip "${url}"
  unzip -q /tmp/chrome.zip -d /tmp/chrome-extract
  mv /tmp/chrome-extract/chrome-linux64/* "${DEST}/"
  rm -rf /tmp/chrome.zip /tmp/chrome-extract
  chmod +x "${DEST}/chrome"
  echo "[browsers] installed -> ${DEST}/chrome ($("${DEST}/chrome" --version 2>/dev/null || echo 'version check deferred'))"
}

install_chromium_wrapper() {
  echo "[browsers] ${ARCH}: no Google Chrome for this architecture; wrapping Playwright Chromium"
  # A wrapper rather than a symlink: Chromium resolves its resource paths relative to argv[0], and
  # the exact revision directory is not known until patchright has installed.
  cat > "${DEST}/chrome" <<'WRAPPER'
#!/usr/bin/env bash
ROOT="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
for candidate in \
  "${ROOT}"/chromium-*/chrome-linux/chrome \
  "${ROOT}"/chromium_headless_shell-*/chrome-linux/headless_shell ; do
  [ -x "${candidate}" ] && exec "${candidate}" "$@"
done
echo "no chromium build found under ${ROOT}" >&2
exit 127
WRAPPER
  chmod +x "${DEST}/chrome"
}

if [ "${ARCH}" = "amd64" ]; then
  install_chrome_for_testing || install_chromium_wrapper
else
  install_chromium_wrapper
fi
