# Drachometer

![Coin logo](coin.svg)

Drachometer: "drachma" (ancient Greek currency) + "meter" = a token usage meter for Claude Code.

A Claude Code hook that logs every turn and tool call to a local SQLite database, with a browser dashboard for exploring token usage, costs, and cache efficiency. Features model-aware pricing, multi-sort tables, date filtering, live SSE refresh, and rich charts. No API keys or external services.

![Dashboard](https://img.shields.io/badge/dashboard-localhost:9873-c87533)

## One-Line Install (Windows PowerShell)

```powershell
irm https://raw.githubusercontent.com/JamesDBartlett3/drachometer/main/drachometer-install.ps1 | iex
```

## One-Line Install (Mac, Linux, and WSL2)

```bash
curl -fsSL https://raw.githubusercontent.com/JamesDBartlett3/drachometer/main/drachometer-install.sh | bash
```

These bootstrap scripts resolve the latest published release via the GitHub Releases API, download the single packaged zip artifact to a temporary directory, extract it, and then run the existing installer. The installer finds your Python interpreter, copies hook scripts to `~/.claude/hooks/drachometer/`, registers them in `~/.claude/settings.json`, creates the database, and runs a smoke test.

## Customizing the Install

The bootstrap scripts accept environment variables so you can point them at a fork, a private mirror, or a locally built zip:

| Variable                   | Default                                               | Purpose                                                             |
| -------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------- |
| `DRACHOMETER_REPO`         | `JamesDBartlett3/drachometer`                         | GitHub `owner/repo` used to resolve releases                        |
| `DRACHOMETER_RELEASES_API` | `https://api.github.com/repos/<REPO>/releases/latest` | Full URL for the GitHub Releases API call                           |
| `DRACHOMETER_ASSET_NAME`   | `drachometer.zip`                                     | Filename of the release asset to download                           |
| `DRACHOMETER_ARCHIVE_URL`  | _(not set)_                                           | When set, skips the API lookup and downloads from this URL directly |

Both scripts also accept `file://` paths and plain local filesystem paths for `ARCHIVE_URL`, which lets you install completely offline from a locally built zip:

```powershell
# Windows — install from a local zip
$env:DRACHOMETER_ARCHIVE_URL = "file://C:/path/to/drachometer.zip"
irm https://raw.githubusercontent.com/JamesDBartlett3/drachometer/main/drachometer-install.ps1 | iex
```

```bash
# Mac/Linux/WSL2 — install from a local zip
DRACHOMETER_ARCHIVE_URL="file:///path/to/drachometer.zip" \
  curl -fsSL https://raw.githubusercontent.com/JamesDBartlett3/drachometer/main/drachometer-install.sh | bash
```

## Quick Start (Windows zip)

1. Extract the zip
2. Double-click **drachometer-install.bat**
3. Open **[http://localhost:9873/drachometer-dashboard.html](http://localhost:9873/drachometer-dashboard.html)**

That's it. Usage is logged automatically from that point on.

> The installer finds your Python interpreter, copies hook scripts to `~/.claude/hooks/drachometer/`, registers them in `~/.claude/settings.json`, creates the database, and runs a smoke test. Any existing hooks are left in place.

<img width="3679" height="1912" alt="image" src="https://github.com/user-attachments/assets/57220cd7-8097-4d57-ab61-546ab50af504" />

## Dashboard Features

### KPIs

- **Total Cost** with daily average
- **Total Input Context** with cache hit percentage
- **Output Tokens** with uncached input count
- **Turns** with tool call count
- **Sessions** with average turns per session
- **Avg Cost / Session** with per-turn average

### Charts

- **Daily Cost Breakdown** — waterfall chart showing cost by category (uncached input, output, cache read, cache create) with a running total
- **Cost by Day** — line chart with one line per model tier (Opus, Sonnet, Haiku)
- **Top Tools** — horizontal bar chart of most-used tools
- **Cache Hit Rate** — line chart of daily prompt cache efficiency

### Tables

- **Sessions** — cost, tokens, model, directory, and branch per session
- **Recent Turns** — last 50 turns with full token breakdown

### Interactive Features

- **Date range slicer** — preset buttons (All, Today, 7d, 30d, 90d) and a Flatpickr date range picker in the header; selection persists across refreshes
- **Hourly drill-down** — when a single date is selected, time-based charts automatically switch from daily to hourly granularity
- **Multi-sort tables** — click any column header to sort; Ctrl+click to add secondary/tertiary sort columns (▲/▼ indicators with subscript priority)
- **Rich chart tooltips** — hover over any chart element to see cost and full token breakdown (uncached input, output, cache read, cache create)
- **Live refresh** — the dashboard auto-updates via SSE when the database changes (no manual reload needed)
- **Release update notice** — the dashboard checks GitHub Releases and shows a banner when a newer semver release is available
- **Info tooltips** — hover over any card's info icon for an explanation of how to read that visual
- **Local timezone** — all dates and times display in your browser's timezone
- **Model-aware pricing** — costs are calculated per-row using each turn's actual model (Opus, Sonnet, or Haiku pricing)

## What Gets Logged

Each **turn** (one assistant response) records:

- Token counts: uncached input, output, cache read, cache creation
- Model relationship (`model_id`) to the `models` dimension table
- Working directory and git branch
- Stop reason
- Timestamp (UTC)

Each **tool call** records:

- Tool name and input
- Exit code and errors
- Linked back to the parent turn

All data is extracted from Claude Code's transcript files — no API keys or external services required.

Each **model** row in the dimension table stores:

- Model key from transcript data (e.g. `claude-opus-4-20250115`)
- Model name
- Model version
- Model provider
- Token pricing (input, output, cache read, cache creation)

## How It Works

The installer registers two Claude Code hooks:

- **Stop** — fires after each assistant turn. Reads the transcript file to extract token usage, model, and stop reason. Upserts model metadata into the `models` table, then writes the turn row.
- **PostToolUse** — fires after each tool call. Writes a row to the `tool_calls` table.

On first run, the hook also starts a lightweight HTTP server (port 9873) that serves the dashboard and reads directly from the live database.
The installer now detects the installed version and runs migrations automatically before files are copied.
When the installer finds models with missing metadata during migration, it prompts for any missing name/version/provider/pricing values.

## Automatic Migration Mechanism

During install/upgrade, `drachometer-install.py`:

1. Reads the installed app version from `~/.claude/hooks/drachometer/drachometer-version.json` (defaults to `0.0.0` when not present).
2. Applies migration steps for older versions while preserving existing data.
   - Database filename/path migration for known legacy locations.
   - Hook settings migration for HTTP server behavior changes (removes legacy direct `drachometer-serve-dashboard.py` hooks).
   - Database schema migration/backfill via normal DB initialization.
3. Copies the latest files and writes the new version metadata.

The SQL migration file is still available for manual use when needed:

`migrations/001_migrate_to_model_dimension.sql`

That script performs the model-dimension migration in one transaction by:

1. Creates `models` if it does not exist.
2. Adds `turns.model_id` as a foreign key to `models(id)`.
3. Inserts one `models` row per distinct non-empty legacy `turns.model` value.
4. Backfills `turns.model_id` by joining legacy `turns.model` values to `models.model_key`.
5. Creates `idx_turns_model_id` for query performance.

### Full schema diagrams

Before migration (legacy schema):

```text
turns
├─ id                    INTEGER PRIMARY KEY AUTOINCREMENT
├─ session_id            TEXT NOT NULL
├─ turn_id               TEXT NOT NULL
├─ recorded_at           TEXT NOT NULL
├─ stop_reason           TEXT
├─ input_tokens          INTEGER NOT NULL DEFAULT 0
├─ output_tokens         INTEGER NOT NULL DEFAULT 0
├─ cache_read_tokens     INTEGER NOT NULL DEFAULT 0
├─ cache_creation_tokens INTEGER NOT NULL DEFAULT 0
├─ cwd                   TEXT
├─ git_branch            TEXT
└─ model                 TEXT
   UNIQUE(session_id, turn_id)

tool_calls
├─ id          INTEGER PRIMARY KEY AUTOINCREMENT
├─ turn_pk     INTEGER REFERENCES turns(id) ON DELETE CASCADE
├─ session_id  TEXT NOT NULL
├─ turn_id     TEXT NOT NULL
├─ recorded_at TEXT NOT NULL
├─ tool_name   TEXT
├─ tool_input  TEXT
├─ exit_code   INTEGER
└─ error       TEXT
```

After migration (new schema):

```text
models
├─ id                            INTEGER PRIMARY KEY AUTOINCREMENT
├─ model_key                     TEXT NOT NULL UNIQUE
├─ model_name                    TEXT
├─ model_version                 TEXT
├─ model_provider                TEXT
├─ input_price_per_mtok          REAL
├─ output_price_per_mtok         REAL
├─ cache_read_price_per_mtok     REAL
└─ cache_creation_price_per_mtok REAL

turns
├─ id                    INTEGER PRIMARY KEY AUTOINCREMENT
├─ session_id            TEXT NOT NULL
├─ turn_id               TEXT NOT NULL
├─ recorded_at           TEXT NOT NULL
├─ stop_reason           TEXT
├─ input_tokens          INTEGER NOT NULL DEFAULT 0
├─ output_tokens         INTEGER NOT NULL DEFAULT 0
├─ cache_read_tokens     INTEGER NOT NULL DEFAULT 0
├─ cache_creation_tokens INTEGER NOT NULL DEFAULT 0
├─ cwd                   TEXT
├─ git_branch            TEXT
├─ model                 TEXT
└─ model_id              INTEGER REFERENCES models(id)
   UNIQUE(session_id, turn_id)

tool_calls
├─ id          INTEGER PRIMARY KEY AUTOINCREMENT
├─ turn_pk     INTEGER REFERENCES turns(id) ON DELETE CASCADE
├─ session_id  TEXT NOT NULL
├─ turn_id     TEXT NOT NULL
├─ recorded_at TEXT NOT NULL
├─ tool_name   TEXT
├─ tool_input  TEXT
├─ exit_code   INTEGER
└─ error       TEXT

schema_migrations
└─ version TEXT PRIMARY KEY

Relationships
├─ tool_calls.turn_pk -> turns.id
└─ turns.model_id -> models.id
```

Recommended manual upgrade procedure (if you run SQL migration directly):

1. Stop Claude Code so no writes occur during migration.
2. Back up the database:
   - `cp ~/.claude/drachometer.db ~/.claude/drachometer.db.bak`
3. Run the migration script:
   - `sqlite3 ~/.claude/drachometer.db < migrations/001_migrate_to_model_dimension.sql`
4. Verify migration results:
   - `SELECT COUNT(*) FROM turns WHERE model IS NOT NULL AND TRIM(model) <> '' AND model_id IS NULL;` (should be `0`)
   - `SELECT COUNT(*) FROM turns t LEFT JOIN models m ON m.id = t.model_id WHERE t.model_id IS NOT NULL AND m.id IS NULL;` (should be `0`)
5. Start Claude Code again.

## Model Pricing

All per-token pricing lives in one place — [`drachometer-pricing.json`](drachometer-pricing.json) — and is expressed in dollars per million tokens per model tier:

```json
{
  "tiers": {
    "opus": {
      "input": 5,
      "output": 25,
      "cache_read": 0.5,
      "cache_create": 6.25
    },
    "sonnet": {
      "input": 3,
      "output": 15,
      "cache_read": 0.3,
      "cache_create": 3.75
    },
    "haiku": {
      "input": 1,
      "output": 5,
      "cache_read": 0.1,
      "cache_create": 1.25
    }
  }
}
```

The tier is inferred from the model key (e.g. `claude-sonnet-4-6` → Sonnet). The same file is read by all three components, so they never disagree:

- The **dashboard** fetches the latest `drachometer-pricing.json` from the repo (GitHub raw) on load, falling back to the installed copy, then to a built-in table if offline.
- The **hook** reads its installed copy when it first records a model, storing that model's prices in the `models` table.
- The **installer** reads it when creating model rows during install/migration.

A turn's cost uses the model's stored per-row pricing when present (so you can hand-edit a model's prices in the `models` table), otherwise its tier's pricing. A model key that matches no known tier is costed at `$0.00` (an honest "unknown") rather than being silently assumed to be a particular tier.

### Automatic pricing updates

Anthropic does not publish a pricing REST API, so [`.github/workflows/update-pricing.yml`](.github/workflows/update-pricing.yml) runs [`scripts/drachometer-update-pricing.py`](scripts/drachometer-update-pricing.py) on a weekly schedule (and on demand) to scrape the published pricing and commit any changes to `drachometer-pricing.json`.

Every tier is **optional**: if a model disappears from the pricing page (e.g. a withdrawn model), its last-known price is preserved rather than wiped. The scraper is also **fail-loud**: if it can't parse _any_ pricing at all — which means the page format or URL changed — it exits non-zero and writes nothing, so the workflow run fails (notifying maintainers) while the last-good `drachometer-pricing.json` stays in effect.

The `fable` tier is fully wired through the dashboard, hook, and installer, so when Claude Fable pricing is published it is picked up automatically with no code or schema change.

> The autonomous commit needs the repository's Actions token to allow writes — enable **Settings → Actions → General → Workflow permissions → "Read and write permissions"**.

## Data Retention

You can automatically purge old records by setting a retention window (in days):

- Add `"token_usage_retention_days": 30` to `~/.claude/settings.json`, or
- Set `TOKEN_USAGE_RETENTION_DAYS=30` in the environment where Claude Code runs.

When configured, the hook deletes `turns` and `tool_calls` rows older than the retention window each time it runs.

## Mesh Replication (LAN/VM)

> **Status:** Configured from the dashboard. Off by default — single-node users are unaffected.

Mesh replication lets several machines on the same trusted network share one combined view of your Claude Code usage. Each node keeps its own local database and they converge toward the union of everyone's history.

Mesh is now set up **from the dashboard**, not during installation. Open the dashboard and use the **hamburger menu (☰) to the left of the logo → “Configure Local Mesh Network.”**

### Scope and safety

- **LAN/VM networks only.** This is designed for a home/lab network or a set of VMs. It is **out of scope** for the public internet/WAN.
- **Not a security boundary.** There is no authentication and no TLS. The mesh identifier exists to prevent _accidental_ cross-merges between unrelated meshes that happen to share a LAN (coworkers, roommates) — not to keep data private. **Do not expose the mesh port to the internet.** Restrict it with your firewall to the LAN/VM subnet.

### How it works

- Every local write to `turns`/`tool_calls` also appends an immutable event to an append-only `oplog`, keyed by a **content hash** so applying the same event twice is a no-op.
- Nodes gossip over plain HTTP using **pull-based anti-entropy**: a node compares per-origin event counts with each peer and fetches only the events it is missing. No broker and no third-party dependencies.
- Because each Claude Code session runs on a single machine, `session_id` partitions writes by node, so merging is a conflict-free **union**. The rare case of the same turn re-logged is resolved last-writer-wins by timestamp.

### Enabling it (from the dashboard)

Open the dashboard, click the **hamburger menu (☰)** to the left of the logo, and choose **“Configure Local Mesh Network.”** The configuration window:

- **Scans all local subnets on every enabled NIC** for existing mesh networks and lists any it finds, along with adjacent meshes that have at least one active node.
- **Join an existing mesh:** pick a discovered mesh and enter the segment of its name that comes **after the final dash** (e.g. for `home-a1b2c3d4`, enter `a1b2c3d4`). This shared secret keeps unrelated meshes on the same LAN from accidentally merging.
- **Create a new mesh:** give it a name and a new mesh id like `home-a1b2c3d4` is generated.
- **Leave:** stop replicating. History is preserved and your node identity is kept for re-joining later.

Only **one mesh can be joined at a time** — joining or creating a mesh automatically leaves the current one, even if it has no active peers.

### Header status indicator

When you belong to a mesh, a small indicator appears just to the right of **Drachometer** in the header:

- a **gray/green dot** — green when at least one peer is connected, gray otherwise;
- a **sync icon** that animates while a sync round is in progress, plus the usual success/failure icons;
- **`{n} peer(s) connected`.**

Clicking any part of the indicator opens the same configuration window, which also shows mesh details: active peers on the current network, adjacent meshes with at least one active node, current-mesh uptime, and the mean time between a new record and its complete propagation to all active nodes (rolling window of the last 15 records).

### Command line (optional)

The same operations are available from the installed mesh script if you prefer a terminal:

```powershell
python "$HOME/.claude/hooks/drachometer/drachometer_mesh.py" discover        # scan LAN for meshes
python "$HOME/.claude/hooks/drachometer/drachometer_mesh.py" init --name home # create a mesh
python "$HOME/.claude/hooks/drachometer/drachometer_mesh.py" join home-a1b2c3d4 --peer 192.168.1.10:9874
python "$HOME/.claude/hooks/drachometer/drachometer_mesh.py" leave           # leave the current mesh
```

Options: `--mesh-port` (default `9874`), `--advertise HOST` (the address peers use to reach this node; auto-detected if omitted), and repeatable `--peer HOST:PORT` seeds. The mesh server starts automatically with the dashboard server.

Joining a node that already has history is safe: its existing rows are **backfilled** into the oplog and replicate to peers, while peers' history flows back — so two independently-created clients converge to the union.

### Maintenance

```powershell
python "$HOME/.claude/hooks/drachometer/drachometer_mesh.py" status          # config, oplog counts, peer reachability
python "$HOME/.claude/hooks/drachometer/drachometer_mesh.py" import OTHER.db  # merge an offline database file
python "$HOME/.claude/hooks/drachometer/drachometer_mesh.py" compact --dry-run  # preview retention-based oplog compaction
python "$HOME/.claude/hooks/drachometer/drachometer_mesh.py" migrate         # sync mesh schema metadata after upgrades
python "$HOME/.claude/hooks/drachometer/drachometer_mesh.py" disable          # stop replicating (history preserved)
```

### Phase 2 hardening

The mesh config file (`~/.claude/drachometer-mesh.json`) now supports operational tuning such as:

- `log_level` (`debug`, `info`, `warning`, `error`)
- `max_retries` and `retry_backoff_seconds` for transient peer failures
- `retention_days` and `retention_keep_per_origin` for safe oplog compaction
- `compress_payloads` to reduce the size of replication traffic over the LAN

The status command also reports basic health signals (peer reachability, replication lag, dedupe rate, conflict rate, and failed sync attempts) so you can spot unhealthy peers before they drift too far apart.

### Firewall

Allow inbound TCP on the mesh port (default `9874`) **only from your LAN/VM subnet**. Example (Windows PowerShell, adjust the subnet):

```powershell
New-NetFirewallRule -DisplayName "Drachometer mesh" -Direction Inbound -Protocol TCP `
  -LocalPort 9874 -RemoteAddress 192.168.1.0/24 -Action Allow
```

### Recovery

A new or long-offline node converges automatically on the next gossip round — its first anti-entropy pass pulls the full event history (the bootstrap snapshot), and subsequent rounds catch up incrementally. If a node's database is lost, reinstall, re-`join` the mesh with the same mesh id, and it will re-pull everyone's history. Diagnostics are written to `~/.claude/drachometer-mesh.log`.

## Files

```
drachometer-install.bat             # Double-click installer (Windows zip)
drachometer-install.ps1             # Network installer bootstrap (Windows PowerShell)
drachometer-install.py              # Installer script
drachometer-install.sh              # Network installer bootstrap (Mac/Linux/WSL2)
hooks/drachometer-log-usage.py      # Hook script (Stop + PostToolUse events)
drachometer-serve-dashboard.py         # Dashboard server (auto-launched by hook)
drachometer_mesh.py                 # Mesh replication library + CLI (opt-in, LAN/VM)
drachometer-dashboard.html             # Browser dashboard (sql.js + Chart.js)
drachometer-pricing.json            # Per-tier model pricing (single source of truth)
drachometer-version.json # App version + GitHub release metadata
coin.svg                # Logo / favicon artwork
scripts/drachometer-update-pricing.py             # Scrapes Anthropic pricing -> drachometer-pricing.json
.github/workflows/release-package.yml # Publishes the release zip asset
.github/workflows/update-pricing.yml  # Weekly pricing refresh (commits drachometer-pricing.json)
```

## Installed Locations

After install, the source folder can be deleted. Everything runs from:

```
~/.claude/hooks/drachometer/drachometer-log-usage.py    # Hook script
~/.claude/hooks/drachometer/drachometer-serve-dashboard.py # Dashboard server
~/.claude/hooks/drachometer/drachometer_mesh.py         # Mesh replication library + CLI
~/.claude/hooks/drachometer/drachometer-dashboard.html     # Dashboard
~/.claude/hooks/drachometer/drachometer-pricing.json    # Per-tier model pricing
~/.claude/hooks/drachometer/drachometer-version.json    # Installed version metadata
~/.claude/drachometer.db          # SQLite database
~/.claude/drachometer-mesh.json   # Mesh config (only if mesh is enabled)
~/.claude/drachometer-mesh.log    # Mesh diagnostics (only if mesh is enabled)
~/.claude/settings.json           # Hook registrations (merged, not replaced)
```

## Viewing the Dashboard

Open **[http://localhost:9873/drachometer-dashboard.html](http://localhost:9873/drachometer-dashboard.html)** in your browser.

The server starts automatically on your first Claude Code session after install. If the server isn't running, just start any Claude Code session and it will launch.

You can also open `drachometer-dashboard.html` directly in a browser and drag-and-drop the database file (`~/.claude/drachometer.db`) onto it.

## Uninstalling

1. Remove the `Stop` and `PostToolUse` entries that reference `drachometer-log-usage.py` from `~/.claude/settings.json`
2. Delete the installed directory and database:
   ```bash
   # Mac / Linux / WSL2
   rm -rf ~/.claude/hooks/drachometer/
   rm ~/.claude/drachometer.db
   ```
   ```powershell
   # Windows PowerShell
   Remove-Item -Recurse -Force "$HOME/.claude/hooks/drachometer/"
   Remove-Item "$HOME/.claude/drachometer.db"
   ```
3. If you enabled mesh replication, also remove its config and log:
   ```bash
   # Mac / Linux / WSL2
   rm -f ~/.claude/drachometer-mesh.json ~/.claude/drachometer-mesh.log
   ```
   ```powershell
   # Windows PowerShell
   Remove-Item -Force "$HOME/.claude/drachometer-mesh.json", "$HOME/.claude/drachometer-mesh.log" -ErrorAction SilentlyContinue
   ```

## Requirements

- Python 3.10+
- Claude Code
