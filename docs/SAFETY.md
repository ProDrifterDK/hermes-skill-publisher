# Safety model

The plugin follows these non-negotiable rules:

1. Only an on-disk, portable package with `metadata.skill-publisher-scope: shared` can be published.
2. The canonical root must preexist, be a real directory, and be exactly authorized by active `skills.external_dirs`.
3. No existing unmanaged canonical or adapter object is overwritten, replaced, adopted, repaired, or deleted.
4. Package roots and descendants are inspected with `lstat`; symlinks and non-regular/non-directory entries are rejected.
5. Source state is retained in a hidden backup until canonical state, every adapter, and registry ownership are durable.
6. Journals and registry state are atomically written and fsynced. One deep validator gates recovery, operation barriers, status, and doctor; unreadable or structurally invalid state blocks mutation.
7. An adapter can be repaired or removed only when deeply validated active-profile registry/journal ownership, canonical-derived raw `readlink` payload, and complete parent-lock coverage agree. An out-of-config root additionally requires its preexisting publisher lock marker; recovery never creates that authority.
8. Cleanup acts only on exact journal/registry paths with matching digests or link text. Name prefixes alone never prove ownership.
9. Shared/config/adapter paths are re-resolved for every callback. No active profile path is cached at import.
10. Hermes configuration is read-only to this plugin.
11. Audit/status/doctor never echo raw durable records or unvalidated skill/action strings; invalid records use stable codes and safe identifiers.

## No-replace release gate

Ordinary POSIX rename may replace an empty destination directory. Canonical/local user-package commits therefore use Linux `renameat2(RENAME_NOREPLACE)`. Unsupported kernels or filesystems fail closed. State-file atomic replacement is limited to plugin-owned registry and journal files.

## Trust boundary

A `shared` label is an explicit publication request, not proof that content is safe. The plugin does not inspect skills for secrets or personal data. Operators must review content before classifying it as shared.

Direct human or other-harness edits bypass Hermes middleware. `doctor` reports managed digest/link drift and does not rewrite external changes.
