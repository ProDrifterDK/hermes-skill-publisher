# Architecture

## Boundary

The plugin registers one `tool_execution` middleware around `skill_manage`, passive `post_tool_call`, `on_session_start`, `on_session_end`, and `pre_llm_call` hooks, and one operator CLI command. It does not register an LLM-facing tool or patch Hermes core.

All profile paths are resolved at callback time. Runtime state lives below the active `$HERMES_HOME/plugin-data/hermes-skill-publisher/` and contains a lock, registry, bounded JSONL audit, and fsynced transaction journals.

## Data flow

1. Middleware classifies `skill_manage(create)` from supplied `content` and calls Hermes core at most once. The entire classification/policy surface sits inside a fail-closed boundary: the host falls through to the core tool if a middleware callback raises before `next_call`, so parser or policy faults return a retryable JSON blocker instead.
2. Core creates only in the active local skills root.
3. A successful, non-interrupted end boundary scans disk for explicit `shared` candidates.
4. Publication validates, stages, parks local state, rehashes the parked backup (a concurrent late write is restored and restaged, never stale-committed), commits canonical state, creates adapters, commits registry ownership, and removes the local backup.
5. New-session (`on_session_start`), every-turn (`pre_llm_call`), and later successful end boundaries recover journals and reconcile exact owned adapters before scanning candidates. Unreadable or parseable-but-structurally-invalid journals are global mutation barriers: recovery, reconcile, publishing, and manual mutations suspend until operator resolution. Current Hermes builds/restores the system prompt before `pre_llm_call`, so pre-API recovery does not guarantee same-turn prompt discovery.

Disk, journals, and the registry—not an in-memory pending list—are authoritative.

## Promotion state machine

```text
planned -> staged -> source_parked -> target_committed
        -> adapters_committed -> registry_committed -> committed
```

A safe copy is created within the shared filesystem. Package digest includes sorted relative paths, entry kind, the exact fixed-width `st_mode & 0o111` mask for files and directories, and file bytes; it excludes absolute paths and mtimes. Canonical and local commits use Linux `renameat2(RENAME_NOREPLACE)` with no unsafe fallback.

Rollback runs a full-state preflight before its first mutation: duplicate source/target or source/backup states, digest drift, and durable registry ownership (the point of no return) all stop rollback with every artifact and the journal preserved. This holds even when shared-root authorization was removed mid-transaction. Rollback removes only journal-created links whose current raw payload still matches, moves a matching target back to its journal stage, restores the matching local backup without replacement, and then removes the owned stage.

## Managed mutation and delete

Before any managed `edit`/`patch`/`write_file`/`remove_file`/`delete`, the middleware holds the state/resource locks and verifies that current Hermes local-first resolution would act on the recorded canonical path; a local or external same-name shadow blocks the call. Managed delete keeps those locks continuously through adapter preflight, exact pre-lock/in-lock registry identity comparison, downstream core deletion, and ownership cleanup, so a concurrent unpublish/republication ABA cannot delete a replacement. Canonical-root fsync follows every confirmed canonical deletion before adapter or registry ownership is removed. While `skills.write_approval` is enabled, every skill write is rejected before it can stage, because approved replay bypasses this middleware; the host `_skill_gate_bypass` ContextVar is a required fail-closed capability.

## Unpublish

Unpublish is an inverse transaction. It writes and fsyncs a planned journal first, then copies the managed canonical package to a local stage at a safe rederived relative destination (symlinked or missing ancestors fall back to the flat local root), rewrites only YAML frontmatter scope to `local` or `private`, parks canonical state, exclusively commits local state, removes exact owned adapters, updates the registry, and removes the canonical backup. YAML formatting can be normalized; Markdown body bytes are preserved. Recovery verifies target, backup, stage, and local digests plus staged name/scope before any rename, and keeps every artifact on mismatch.

## Locks and limits

The active profile state lock is acquired first. Shared and every deeply validated recorded adapter-parent lock follow in sorted resolved-path order with a ten-second timeout; journal/registry state is then re-read and revalidated before mutation. An adapter root removed from config remains eligible for explicit unpublish cleanup only when its record derives from the canonical target and its persistent publisher lock file proves the root was previously managed. Recovery never creates that proof. Advisory locks serialize cooperating plugin processes; every commit still revalidates and uses no-replace primitives.

Packages are bounded to 1,024 regular files and 64 MiB. Symlink roots, symlink descendants, and special files are rejected.

## Missing middleware

CLI and hooks remain registered, but all mutation is disabled. `doctor`, `publish`, and `unpublish` return a compatibility blocker. An old host without behavior-changing middleware cannot enforce required classification; the honest remedy is a host upgrade.
