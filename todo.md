# Drachometer — Deferred Improvements

Pinned on 2026-09-29: item 6 from the September review sweep ("smaller things
worth a sweep"). Revisit these later.

- [ ] **Dashboard `innerHTML` hardening** — the dashboard renders fetched
      markdown (README, release notes) into `innerHTML` (~10 call sites). It's
      all locally-served content, so risk is low, but sanitizing the markdown
      renderer output or using `textContent` for the dynamic bits would tidy
      it up.
- [ ] **Loopback API origin check** — `/shutdown`, `/mesh/api/*`,
      `/api/preferences`, and `GET /drachometer.db` on `127.0.0.1:9873` do no
      `Origin`/`Host` validation, so a malicious web page could CSRF/
      DNS-rebind a browser into POSTing `/shutdown` or re-pointing the mesh.
      A `Host`-header allowlist (~5 lines) closes this.
- [ ] **`mock-anthropic-api.py` polish** — binds `0.0.0.0:8787` unauthenticated
      (should be `127.0.0.1`); duplicate imports; two dead
      `if tool_name: import subprocess` blocks; stale "Claude 3.5 Sonnet"
      docstring; `run_test()` writes to the real `~/.claude/drachometer.db`.
- [ ] **Dead installer flags/params** — `--no-mesh` (`drachometer-install.py`,
      `parse_args`) is defined but never read; `copy_hooks()` takes an unused
      `python_exe` parameter.
- [ ] **`.video_agent/` at repo root** — tool droppings committed to the repo;
      add to `.gitignore` and remove.
- [ ] **`merge_settings` crash on corrupt settings.json** — the installer's
      `merge_settings()` is the only JSON read without a try/except, so a
      malformed `~/.claude/settings.json` aborts the install halfway through
      (files already copied).
- [ ] **Fresh-install migration stamping foot-gun** — `init_database()` marks
      all `migrations/*.sql` as applied without running them. Correct only
      because current migrations are pure backfill; a future migration with
      load-bearing logic would be silently skipped on fresh installs.
- [ ] **Mesh server binds `0.0.0.0` by default** — mitigated now by the
      dashboard's Listen-interface selector (mesh config → `listen_host`),
      but the *default* is still all-interfaces; consider defaulting to the
      detected LAN IP instead.
- [ ] **Mesh request size caps** — `/mesh/events` and the dashboard JSON body
      readers have no `Content-Length` cap (memory DoS on the LAN listener);
      peer responses in `_get_json`/`_post_json` are buffered without limit.
- [ ] **Actions pinned to tags, not SHAs** — `actions/checkout@v6.0.3` etc.
      are mutable tags; pin to commit SHAs for supply-chain hygiene.

## Also noted (lower priority)

- SSE handler in `drachometer-serve-dashboard.py` has no write timeout; a
  stuck client thread can hang forever (one thread per client).
- `collect_health_metrics` probes every peer synchronously (1s timeout each)
  inside `cmd_status`.
- Pricing scraper (`scripts/drachometer-update-pricing.py`) parse functions
  are pure and untested — table-driven tests would catch page-format changes
  before the Monday cron fails.
