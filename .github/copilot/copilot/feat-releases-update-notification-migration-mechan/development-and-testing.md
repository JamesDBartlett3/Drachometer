# Development and Testing Results

## Feature
Implemented release/version metadata, dashboard update notification via GitHub Releases API, and installer-driven migrations that detect installed version and preserve existing data during upgrades.

## Changes Made
- Added `version.json`:
  - Stores app version (`1.0.0`) and release metadata URLs.
  - Copied to `~/.claude/hooks/version.json` during install for runtime version detection and dashboard checks.
- Updated `install.py`:
  - Added installed-version detection from `~/.claude/hooks/version.json`.
  - Added semver comparison helper.
  - Added automatic migration runner for older installs.
  - Added migration steps for:
    - Legacy database filename/path migration to `~/.claude/token_usage.db`.
    - Hook settings migration removing legacy direct `serve_report.py` command hooks (HTTP server behavior migration).
  - Added version output and `version.json` copy in installer flow.
- Updated `report.html`:
  - Added update banner UI.
  - Added release-check logic:
    - fetch local `version.json`
    - query GitHub Releases API (`releases/latest`)
    - compare semver
    - show notification with link to latest release when newer version exists.
- Updated `README.md`:
  - Documented release update notification.
  - Replaced manual “future migration” wording with automatic migration mechanism behavior.
  - Documented installed `version.json` metadata file.

## Validation Performed
### Baseline (before changes)
- Ran installer validation flow:
  - `python3 install.py`
  - Result: PASS

### Targeted verification (after changes)
1. Python syntax validation:
   - `python3 -m py_compile install.py hooks/log_usage.py serve_report.py`
   - Result: PASS
2. Installer validation flow:
   - `python3 install.py`
   - Result: PASS
3. Migration behavior test for older install:
   - Seeded `~/.claude/hooks/version.json` with `0.0.0`.
   - Seeded legacy DB path `~/.claude/claude_token_usage.db`.
   - Seeded settings with direct `serve_report.py` hook command.
   - Ran `python3 install.py`.
   - Verified:
     - DB moved to `~/.claude/token_usage.db`.
     - Legacy DB path removed.
     - Legacy `serve_report.py` hook command removed.
     - Installed version updated to `1.0.0`.

### Security/quality checks
- Secret scan on changed files: pending (run before finalization)
- CodeQL check after final changes: pending (run before finalization)
