# Operations

## Readiness

Precreate every configured shared and adapter root as a real directory (not a symlink), add the canonical directory to the active profile's `skills.external_dirs`, enable the plugin, start a new Hermes process, then run:

```bash
hermes skill-publisher doctor
```

Do not publish until it exits `0`. A gateway must be restarted after plugin/config changes. Harness discovery behavior can change by version; verify each installed harness before relying on a native root or configuring an adapter. Discovery does not establish instruction or tool compatibility.

## Inspect

```bash
hermes skill-publisher status --json
hermes skill-publisher audit --json --limit 50
```

Status lists sanitized registry-owned publication summaries, local classified/unclassified candidates, and deeply validated durable transaction phases. Invalid state is represented only by stable codes and safe record identifiers. Audit events contain validated names/enums and stable error placeholders, never skill bodies, raw durable records, or full tool arguments.

## Manual publication

```bash
hermes skill-publisher publish portable-example
```

The package must be local, unique by name, portable, and explicitly `shared`. Manual publication uses the same locks, root authorization, limits, journal, and no-overwrite transaction as lifecycle publication. There is no force flag.

## Unpublish

```bash
hermes skill-publisher unpublish portable-example --scope local
hermes skill-publisher unpublish portable-example --scope private
```

Only active-profile registry-owned publications are accepted. The recorded local category is reused when its parent still exists; otherwise the package returns directly under the local skills root. Frontmatter YAML can be normalized while the Markdown body remains byte-for-byte unchanged.

## Recovery

New-session (`on_session_start`), every-turn (`pre_llm_call`), and each later successful end boundary process durable journals before scanning new candidates. Current Hermes fires `on_session_start` only for brand-new sessions, so the every-turn boundary provides recovery after a process restart that resumes a session. It runs after current Hermes restores/builds the system prompt: recovery completes before the API call, but a newly recovered skill is not guaranteed to enter that turn's already-built prompt. If recovery reports an unreadable or structurally invalid journal, digest mismatch, duplicate source/target, changed adapter, or unknown state, every mutation is suspended: resolve ownership externally only after backing up all involved paths, then rerun `doctor`. The plugin intentionally does not guess or broadly delete hidden paths.

## Delete

A managed delete holds all publication locks continuously across adapter preflight, exact host-target verification (a local or external same-name shadow blocks the delete), the downstream Hermes core delete, and ownership cleanup. Hermes core performs the canonical deletion; the plugin then fsyncs the canonical root, removes only exact owned adapter links, and updates registry state. Reconciliation finishes cleanup when a successful core delete was interrupted afterward.

## Write approval incompatibility

Boolean `true` and the host-truthy strings `true`, `yes`, `on`, `1`, `approve`, and `enabled` (case-insensitive, surrounding whitespace ignored) stage skill writes for later approval, and approval replay bypasses `tool_execution` middleware. The plugin therefore fails closed while the gate is enabled: every `skill_manage` write is rejected with `skill_publisher.write_approval_incompatible` before it can stage, and `doctor` exits non-zero. The host `_skill_gate_bypass` ContextVar is also required; if absent or changed, writes/publication block. Disable the gate or the plugin; there is no partial mode.

## Uninstall

1. Run `status`.
2. Unpublish each managed skill that should return to the profile.
3. Resolve reported adapter conflicts.
4. Disable and remove the plugin through Hermes.
5. Remove external-dir or adapter configuration only if no longer wanted.

Uninstall never deletes canonical packages, links, registry, or audit automatically.
