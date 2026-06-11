# Development and Testing Results

## Feature
Implemented network-install bootstrap scripts for Windows PowerShell and Bash so the project can be installed with a single command while still delegating the real work to the existing `install.py` installer.

## Changes Made
- Added `install.ps1`:
  - Downloads a repository archive to a temporary directory.
  - Extracts it and runs `install.py`.
  - Supports the requested `irm <URL> | iex` installation style.
- Added `install.sh`:
  - Downloads a repository archive to a temporary directory.
  - Extracts it and runs `install.py`.
  - Supports the requested `curl ... | bash` installation style for Mac, Linux, and WSL2.
- Updated `README.md`:
  - Documented both one-line install commands.
  - Kept the existing Windows zip-based install flow.
  - Added the new bootstrap scripts to the file list.

## Validation Performed
### Baseline (before changes)
- Ran the existing installer validation flow in an isolated temporary `HOME`:
  - `HOME=$(mktemp -d) python3 install.py`
  - Result: PASS

### Targeted verification (after changes)
1. Bash bootstrap syntax check:
   - `bash -n install.sh`
   - Result: PASS
2. Bash bootstrap end-to-end install using a local archive override:
   - `HOME=<tmp> CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_ARCHIVE_URL=file://<repo.tar.gz> ./install.sh`
   - Result: PASS
3. PowerShell bootstrap syntax parse:
   - `pwsh -NoLogo -NoProfile -Command '[System.Management.Automation.Language.Parser]::ParseFile(...)'`
   - Result: PASS
4. PowerShell bootstrap end-to-end install using a local archive override:
   - `HOME=<tmp> CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_ARCHIVE_URL=<repo.zip> pwsh -NoLogo -NoProfile -File ./install.ps1`
   - Result: PASS

### Security/quality checks
- Secret scan on changed files: PASS
- CodeQL check: no analyzable code changes detected for supported CodeQL languages

## Notes
- The new scripts intentionally keep `install.py` as the single source of truth for copying files, merging Claude hook settings, initializing the database, and running the smoke test.
- Both bootstraps support an archive override environment variable to simplify validation without changing the default GitHub download behavior.
