# Changelog

All notable changes are documented here.

## [0.1.1] - 2026-08-10

### Fixed

- Local candidate discovery excludes all dot-prefixed directory trees while preserving visible nested categories and transaction-artifact doctor scans.

## [0.1.0] - 2026-08-10

### Added

- Deterministic `skill_manage(create)` classification middleware with a fail-closed top-level boundary (parser/policy faults never reach the core tool).
- Deferred successful-turn publication and lifecycle recovery at new-session, every-turn (`pre_llm_call`), and successful end boundaries.
- Linux no-replace, safe-copy, digest, lock, journal, registry, and audit primitives.
- Exact owned relative adapter links.
- Transactional local/private unpublish with byte-exact Markdown body preservation.
- Operator status, audit, doctor, publish, and unpublish commands.
- Linux Python 3.11–3.13 tests, real-Hermes E2E smoke, and public hygiene checks.

### Fixed

- Deauthorized recovery no longer deletes the only package copy or resolves duplicate source/target states; durable registry ownership is the point of no return.
- Promotion rehashes the parked backup and restages on concurrent late local writes instead of committing a stale stage.
- Managed delete holds all locks continuously across preflight, host-target verification, core delete, and cleanup; same-name local/external shadows block managed mutations.
- Boolean and host-truthy string `skills.write_approval` values are fail-closed incompatibilities; the required host gate-bypass capability is checked by middleware, lifecycle, CLI mutation, and doctor.
- Unpublish journals before staging, rederives safe relative destinations, verifies every digest/scope/name before recovery renames, and preserves CRLF/non-ASCII body bytes.
- Managed delete compares exact pre-lock/in-lock publication identity, blocking same-name unpublish/republication ABA replacements before core runs.
- Registry and journal authority is deep-validated; recovery locks every validated recorded adapter parent, revalidates under lock, and requires a prior-root marker for explicit stale-adapter cleanup.
- Unreadable or structurally invalid journals are global mutation/readiness barriers; reconcile defers names with valid live journals.
- Adapter cleanup fsync failures preserve the durable journal, including an absent-link retry fsync before proof removal; canonical-root fsync precedes ownership removal after deletion.
- Package identity hashes the exact execute mask for files/directories while remaining mtime-independent; safe copy restores directory modes despite umask.
- Audit/status/doctor diagnostics expose only schema-validated summaries, safe enums/names, stable codes, and withheld error details; `audit --limit 0` returns no events.
- GitHub Actions, minimum Hermes, and build/test requirements are pinned and enforced by public hygiene; the current-main compatibility lane alone floats intentionally.
