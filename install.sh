#!/usr/bin/env bash
set -euo pipefail

REPO="${CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_REPO:-JamesDBartlett3/claude-code-token-usage-dashboard}"
RELEASES_API="${CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_RELEASES_API:-https://api.github.com/repos/${REPO}/releases/latest}"
ASSET_NAME="${CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_ASSET_NAME:-claude-code-token-usage-dashboard.zip}"
ARCHIVE_URL="${CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_ARCHIVE_URL:-}"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/claude-code-token-usage-dashboard-XXXXXX")"
cleanup() {
  rm -rf "$work_dir"
}
trap cleanup EXIT

download() {
  local url="$1"
  local output="$2"
  local local_path=""
  if [[ "$url" == file://* ]]; then
    local_path="${url#file://}"
  elif [[ -f "$url" ]]; then
    local_path="$url"
  fi
  if [[ -n "$local_path" ]]; then
    cp "$local_path" "$output"
    return
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$output"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$output" "$url"
  else
    echo "ERROR: curl or wget is required." >&2
    exit 1
  fi
}

fetch_text() {
  local url="$1"
  local local_path=""
  if [[ "$url" == file://* ]]; then
    local_path="${url#file://}"
  elif [[ -f "$url" ]]; then
    local_path="$url"
  fi
  if [[ -n "$local_path" ]]; then
    cat "$local_path"
    return
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -H "Accept: application/vnd.github+json" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- --header="Accept: application/vnd.github+json" "$url"
  else
    echo "ERROR: curl or wget is required." >&2
    exit 1
  fi
}

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  echo "ERROR: Python 3.10+ is required." >&2
  exit 1
}

python_exe="$(find_python)"

if [[ -n "$ARCHIVE_URL" ]]; then
  archive_url="$ARCHIVE_URL"
else
  echo "Looking up latest release for ${REPO}..."
  release_json="$(fetch_text "$RELEASES_API")"
  archive_url="$(
    ASSET_NAME="$ASSET_NAME" "$python_exe" -c '
import json
import os
import sys

data = json.load(sys.stdin)
asset_name = os.environ["ASSET_NAME"]
assets = data.get("assets") or []
for asset in assets:
    if asset.get("name") == asset_name and asset.get("browser_download_url"):
        print(asset["browser_download_url"])
        raise SystemExit(0)
for asset in assets:
    name = asset.get("name") or ""
    url = asset.get("browser_download_url") or ""
    if name.endswith(".zip") and url:
        print(url)
        raise SystemExit(0)
raise SystemExit(1)
' <<<"$release_json"
  )" || {
    echo "ERROR: No release zip asset was found in ${RELEASES_API}." >&2
    exit 1
  }
fi

echo "Downloading release asset..."
archive_path="$work_dir/$ASSET_NAME"
download "$archive_url" "$archive_path"

echo "Extracting installer..."
"$python_exe" - "$archive_path" "$work_dir" <<'PY'
import sys
import zipfile

archive_path, output_dir = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(archive_path) as archive:
    archive.extractall(output_dir)
PY

installer_path="$(find "$work_dir" -type f -name install.py -print -quit)"
if [[ -z "$installer_path" ]]; then
  echo "ERROR: install.py was not found in the downloaded archive." >&2
  exit 1
fi

echo "Running installer with ${python_exe}..."
"$python_exe" "$installer_path"
