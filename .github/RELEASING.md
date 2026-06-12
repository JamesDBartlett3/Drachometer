# Releasing claude-code-token-usage-dashboard

This document explains how to publish a new release and how the GitHub Actions release workflow operates.

## Overview

When you publish a GitHub Release, the **Release package** workflow automatically builds and attaches a zip asset to that release. The one-line installers (`install.sh` and `install.ps1`) then resolve the latest release via the GitHub Releases API and install from that zip.

## Pre-release checklist

1. Update `version.json` with the new semver version string (e.g. `"version": "1.2.0"`).
2. Commit and push all changes to the default branch (`main`).
3. Confirm the `workflows/release-package.yml` workflow file is present on `main` — GitHub Actions only executes workflows that exist on the default branch.

## Publishing a release

1. Go to **GitHub → Releases → Draft a new release**.
2. Click **Choose a tag** and type the new version prefixed with `v` (e.g. `v1.2.0`), then select **Create new tag on publish**.
3. Set the target to `main` (or the commit you want to release).
4. Fill in the release title and release notes.
5. Click **Publish release** (not *Save draft*).

> **Important:** The workflow only triggers on `published`, not on draft releases. Do not click *Publish* until the release notes and tag are final.

## What the workflow does

File: `.github/workflows/release-package.yml`

| Step | What happens |
|------|-------------|
| Trigger | Fires on `release: types: [published]` |
| Permissions | `contents: write` (needed to upload the release asset) |
| Check out | Checks out the repository at the tagged commit |
| Build zip | Creates `dist/claude-code-token-usage-dashboard.zip` containing: `README.md`, `coin.svg`, `hooks/`, `install.bat`, `install.ps1`, `install.py`, `install.sh`, `migrations/`, `report.html`, `serve_report.py`, `version.json` |
| Upload asset | Attaches the zip to the published release via `softprops/action-gh-release` |

The zip intentionally omits development-only files (`.github/`, `.git/`, `screenshots/`, etc.) so users receive only what is needed to install and run the dashboard.

## Verifying a release

After the workflow completes (usually under a minute):

1. Open the release page on GitHub and confirm `claude-code-token-usage-dashboard.zip` appears under **Assets**.
2. Optionally run the one-line installer against the new release to do an end-to-end smoke test:

   ```bash
   # macOS / Linux / WSL2
   curl -fsSL https://raw.githubusercontent.com/JamesDBartlett3/claude-code-token-usage-dashboard/main/install.sh | bash
   ```

   ```powershell
   # Windows PowerShell
   irm https://raw.githubusercontent.com/JamesDBartlett3/claude-code-token-usage-dashboard/main/install.ps1 | iex
   ```

3. Both installers resolve the latest release from `https://api.github.com/repos/JamesDBartlett3/claude-code-token-usage-dashboard/releases/latest`, download the zip, extract it, and run `install.py`.

## Monitoring the workflow run

- Go to **GitHub → Actions → Release package** to see live logs.
- If the run fails, the zip will not be attached. Fix the issue, delete the broken release/tag, and re-publish.

## Workflow action versions

| Action | Pinned version |
|--------|---------------|
| `actions/checkout` | `v6.0.3` |
| `softprops/action-gh-release` | `v3.0.0` |

To update an action version, edit `.github/workflows/release-package.yml` and update the `uses:` line, then commit to `main` before the next release.
