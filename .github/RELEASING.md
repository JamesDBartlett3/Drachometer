# Releasing drachometer

This document explains how to publish a new release and how the GitHub Actions release workflow operates.

## Overview

Releasing is two automated steps, so the version number is never hand-typed into a file and never has a chance to drift from the release tag:

1. **Prepare release**, which you trigger manually from the Actions tab, bumps `drachometer-version.json` on `main`, creates the tag from that same commit, and opens a draft release on that tag, all in one job. The commit, the tag, and the draft are produced by the same script from the same input, so they cannot disagree.
2. **Release package** (fires on publishing that GitHub Release) re-stamps `drachometer-version.json` from the release's tag name as a belt-and-suspenders check, then builds and attaches the zip asset. The one-line installers (`drachometer-install.sh` and `drachometer-install.ps1`) resolve the latest release via the GitHub Releases API and install from that zip.

## Cutting a release

1. Go to **GitHub → Actions → Prepare release → Run workflow**, enter the new version with no leading `v` (e.g. `1.2.0`), and run it. This commits the version bump to `main`, pushes tag `v1.2.0`, and opens a draft release on that tag with auto-generated notes.
2. Go to **GitHub → Releases**, open the `v1.2.0` draft, review/edit the auto-generated notes and title.
3. Click **Publish release** (not _Save draft_).

> **Important:** The Release package workflow only triggers on `published`, not on draft releases. Do not click _Publish_ until the release notes are final.

## What the workflows do

### Prepare release — `.github/workflows/prepare-release.yml`

| Step                | What happens                                                                                              |
| ------------------- | ---------------------------------------------------------------------------------------------------------- |
| Validate version     | Fails fast if the input isn't `X.Y.Z`                                                                      |
| Check tag free       | Fails if `vX.Y.Z` already exists, so a typo can't silently overwrite an existing release's tag             |
| Bump version.json    | Sets `.version` in `drachometer-version.json` to the input                                                 |
| Commit and tag       | Commits the bump to `main` and creates `vX.Y.Z` on that same commit, then pushes both                      |
| Create draft release | Opens a **draft** GitHub Release on tag `vX.Y.Z` with auto-generated notes (`gh release create --draft --generate-notes`) — nothing is public yet |

### Release package — `.github/workflows/release-package.yml`

| Step         | What happens                                                                                                                                                                                                                                                                                                             |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Trigger       | Fires on `release: types: [published]`                                                                                                                                                                                                                                                                                   |
| Permissions   | `contents: write` (needed to upload the release asset)                                                                                                                                                                                                                                                                   |
| Check out     | Checks out the repository at the tagged commit                                                                                                                                                                                                                                                                           |
| Stamp version | Overwrites the `version` field in `drachometer-version.json` with the release's tag name (e.g. `v1.2.0` → `1.2.0`) — redundant when the tag came from Prepare release, but keeps a manually-pushed tag honest too                                                                                                      |
| Build zip     | Creates `dist/drachometer.zip` containing: `README.md`, `drachometer-logo.svg`, `hooks/`, `drachometer-install.bat`, `drachometer-install.ps1`, `drachometer-install.py`, `drachometer-install.sh`, `migrations/`, `drachometer-dashboard.html`, `drachometer-serve-dashboard.py`, `drachometer_mesh.py`, `drachometer-version.json` |
| Upload asset  | Attaches the zip to the published release via `softprops/action-gh-release`                                                                                                                                                                                                                                              |

The zip intentionally omits development-only files (`.github/`, `.git/`, `screenshots/`, etc.) so users receive only what is needed to install and run the dashboard.

## Verifying a release

After the workflow completes (usually under a minute):

1. Open the release page on GitHub and confirm `drachometer.zip` appears under **Assets**.
2. Optionally run the one-line installer against the new release to do an end-to-end smoke test:

   ```bash
   # macOS / Linux / WSL2
   curl -fsSL https://raw.githubusercontent.com/JamesDBartlett3/drachometer/main/drachometer-install.sh | bash
   ```

   ```powershell
   # Windows PowerShell
   irm https://raw.githubusercontent.com/JamesDBartlett3/drachometer/main/drachometer-install.ps1 | iex
   ```

3. Both installers resolve the latest release from `https://api.github.com/repos/JamesDBartlett3/drachometer/releases/latest`, download the zip, extract it, and run `drachometer-install.py`.

## Monitoring the workflow run

- Go to **GitHub → Actions → Release package** to see live logs.
- If the run fails, the zip will not be attached. Fix the issue, delete the broken release/tag, and re-publish.

## Workflow action versions

| Action                        | Pinned version |
| ----------------------------- | -------------- |
| `actions/checkout`            | `v6.0.3`       |
| `softprops/action-gh-release` | `v3.0.0`       |

To update an action version, edit `.github/workflows/release-package.yml` and update the `uses:` line, then commit to `main` before the next release.
