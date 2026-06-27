# TODO List


- [x] Generate 5 possible names for this solution. Should be clever, catchy, and relevant to the functionality of the solution. After renaming, update all references in the codebase, documentation, and configuration files to reflect the new name. _(Named **Drachometer** — vetted collision-free; renamed all files + the GitHub repo.)_
- [x] Fix naming inconsistencies in the codebase, ensuring that all variables, functions, and files follow a consistent naming convention that is clear and descriptive.
- [x] Mesh rollout phase 1 (MVP replication on LAN/VM networks) _(event-sourced `oplog` + pull-based HTTP anti-entropy; opt-in, no deps. See [`drachometer_mesh.py`](drachometer_mesh.py) and the [Mesh Replication](README.md#mesh-replication-lanvm) docs.)_
    - [x] During installation, configure each instance as a mesh node (node ID, listen address, peer seed list, startup registration). _(installer `--enable-mesh`/`--join-mesh` + interactive opt-in; `/mesh/announce` startup registration.)_
    - [x] Generate and persist a mesh network identifier (user-specified mesh name plus auto-generated 8-character GUID suffix) during initial setup. _(`make_mesh_id` → `<name>-<8 hex>`, persisted to `drachometer-mesh.json`.)_
    - [x] Require exact mesh network identifier match during peer handshake before any replication begins. _(`/mesh/hello` checked in `sync_with_peer` before any pull.)_
    - [x] Allow the user to specify a custom replication port and advertise address. _(`--mesh-port` / `--advertise`; auto-detected LAN IP fallback.)_
    - [x] Restrict supported deployment scope to LAN/VM networks only (explicitly out of scope: public internet/WAN). _(report server stays loopback; docs cover firewall + no-WAN; no auth/TLS by design.)_
    - [x] Implement concurrent peer synchronization so each node can communicate with multiple peers and process updates safely without local DB conflicts. _(threaded gossip + WAL + busy_timeout + in-process apply lock.)_
    - [x] Define replication identity and idempotency rules so every change has a globally unique event ID and can be safely applied multiple times. _(content-hash `event_id`; `INSERT OR IGNORE`; `tool_calls.uid`.)_
    - [x] Implement anti-entropy reconciliation so nodes periodically compare state summaries and request missing history until convergence. _(per-origin digest → id-diff → fetch-missing loop.)_
    - [x] Add bootstrap and recovery workflows so new or stale nodes can perform full snapshot sync followed by incremental catch-up. _(empty-oplog pull = full snapshot; backfill of pre-mesh history; subsequent rounds catch up.)_
- [x] Mesh rollout phase 2 (safety, compatibility, and operations)
    - [x] Implement a logging mechanism for mesh replication to log outgoing/incoming sync batches, conflicts, dedupe actions, retries, errors, and replication latency.
    - [x] Implement a configuration file for mesh nodes, allowing users to customize settings such as node ID, listen address, peer list, replication interval, retry/backoff policy, and logging level.
    - [x] Implement schema versioning and migration support for mesh replication. Nodes should reject incompatible replication payloads, advertise schema version during handshake, and provide a migration workflow to rejoin healthy sync state.
    - [x] Define conflict resolution semantics (for example, per-table merge rules, deterministic tie-breakers, and tombstone handling) and document behavior.
    - [x] Add retention and compaction policies for replicated history and tombstones, including safeguards that prevent premature deletion before cluster convergence.
    - [x] Add mesh health metrics and alerts (peer reachability, replication lag, dedupe rate, conflict rate, and failed sync attempts) with operational runbooks.
- [x] Mesh rollout phase 3 (performance tuning)
    - [x] Implement replication performance optimization (batching, compression, checkpoints/high-water marks, and optional query/result caching where applicable).
- [x] Update documentation for phased mesh installation and usage, including LAN/VM networking requirements, firewall setup, recovery procedures, and troubleshooting.
    - [x] Document mesh network identifier behavior as accidental-cross-merge prevention for shared LANs (coworkers/roommates), not as cryptographic security.