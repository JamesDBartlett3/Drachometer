# Claude Code Token Usage Dashboard

![Coin logo](coin.svg)

A Claude Code hook that logs every turn and tool call to a local SQLite database, with a browser dashboard for exploring token usage, costs, and cache efficiency. Features model-aware pricing, multi-sort tables, date filtering, live SSE refresh, and rich charts. No API keys or external services. 

![Dashboard](https://img.shields.io/badge/dashboard-localhost:9873-c87533)

## Quick Start (Windows)

1. Extract the zip
2. Double-click **install.bat**
3. Open **[http://localhost:9873/report.html](http://localhost:9873/report.html)**

That's it. Usage is logged automatically from that point on.

> The installer finds your Python interpreter, copies hook scripts to `~/.claude/hooks/`, registers them in `~/.claude/settings.json`, creates the database, and runs a smoke test. Any existing hooks are left in place.

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
When the installer finds models with missing metadata during migration, it prompts for any missing name/version/provider/pricing values.

## Future Upgrade: Lossless Schema Migration

If a future release needs to migrate older databases to the model-dimension schema (`turns.model_id -> models.id`), run:

`migrations/001_migrate_to_model_dimension.sql`

That script performs the full lossless migration by doing the following in one transaction:

1. Creates `models` if it does not exist.
2. Adds `turns.model_id` as a foreign key to `models(id)`.
3. Inserts one `models` row per distinct non-empty legacy `turns.model` value.
4. Backfills `turns.model_id` by joining legacy `turns.model` values to `models.model_key`.
5. Creates `idx_turns_model_id` for query performance.

Recommended upgrade procedure:

1. Stop Claude Code so no writes occur during migration.
2. Back up the database:
   - `cp ~/.claude/token_usage.db ~/.claude/token_usage.db.bak`
3. Run the migration script:
   - `sqlite3 ~/.claude/token_usage.db < migrations/001_migrate_to_model_dimension.sql`
4. Verify migration results:
   - `SELECT COUNT(*) FROM turns WHERE model IS NOT NULL AND TRIM(model) <> '' AND model_id IS NULL;` (should be `0`)
   - `SELECT COUNT(*) FROM turns t LEFT JOIN models m ON m.id = t.model_id WHERE t.model_id IS NOT NULL AND m.id IS NULL;` (should be `0`)
5. Start Claude Code again.

## Data Retention

You can automatically purge old records by setting a retention window (in days):

- Add `"token_usage_retention_days": 30` to `~/.claude/settings.json`, or
- Set `TOKEN_USAGE_RETENTION_DAYS=30` in the environment where Claude Code runs.

When configured, the hook deletes `turns` and `tool_calls` rows older than the retention window each time it runs.

## Files

```
install.bat             # Double-click installer (Windows)
install.py              # Installer script
hooks/log_usage.py      # Hook script (Stop + PostToolUse events)
serve_report.py         # Dashboard server (auto-launched by hook)
report.html             # Browser dashboard (sql.js + Chart.js)
coin.svg                # Logo / favicon artwork
```

## Installed Locations

After install, the source folder can be deleted. Everything runs from:

```
~/.claude/hooks/log_usage.py      # Hook script
~/.claude/hooks/serve_report.py   # Dashboard server
~/.claude/hooks/report.html       # Dashboard
~/.claude/token_usage.db          # SQLite database
~/.claude/settings.json           # Hook registrations (merged, not replaced)
```

## Viewing the Dashboard

Open **[http://localhost:9873/report.html](http://localhost:9873/report.html)** in your browser.

The server starts automatically on your first Claude Code session after install. If the server isn't running, just start any Claude Code session and it will launch.

You can also open `report.html` directly in a browser and drag-and-drop the database file (`~/.claude/token_usage.db`) onto it.

## Uninstalling

1. Remove the `Stop` and `PostToolUse` entries that reference `log_usage.py` from `~/.claude/settings.json`
2. Delete the installed files:
   ```
   rm ~/.claude/hooks/log_usage.py
   rm ~/.claude/hooks/serve_report.py
   rm ~/.claude/hooks/report.html
   rm ~/.claude/token_usage.db
   ```

## Requirements

- Python 3.10+
- Claude Code
