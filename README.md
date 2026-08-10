# Hermes Skill Publisher

`hermes-skill-publisher` is a standalone Hermes Agent plugin that safely promotes explicitly shared skills from the active Hermes profile into a canonical Agent Skills directory. Publication is deferred until a successful, non-interrupted turn boundary so support files written later in the same tool loop are included. Journal recovery runs at every `pre_llm_call` turn boundary (including resumed sessions, for which `on_session_start` never fires) and at lifecycle start/end boundaries. On current Hermes, `pre_llm_call` runs after system-prompt restoration/build: recovery finishes before the API call, but a newly recovered skill is not guaranteed to enter that already-built prompt in the same turn.

> **Beta warning:** publication failures happen after the original `skill_manage(create)` result and therefore appear in `status`, `doctor`, and the audit log—not in that completed tool result. After a hard mid-turn crash, recovery can validate and publish only the durable snapshot; it cannot know whether another support-file write was intended.

## Requirements

- Linux or WSL with working `renameat2(RENAME_NOREPLACE)` on every publication filesystem.
- Python 3.11–3.13 and PyYAML 6.x.
- Hermes v2026.7.30 or later, or a build containing both `tool_execution` middleware and non-zero plugin CLI exit-code propagation. Runtime capability detection is authoritative.
- A preexisting real shared directory already listed in the active profile's `skills.external_dirs`.
- `skills.write_approval` must normalize to off (the default). Boolean `true` and the host-truthy strings `true`, `yes`, `on`, `1`, `approve`, and `enabled` (case-insensitive, surrounding whitespace ignored) block publication. The host `_skill_gate_bypass` capability is required; host drift blocks writes/publication and makes `doctor` exit non-zero.

## Install

```bash
hermes plugins install OWNER/hermes-skill-publisher --enable
hermes config edit
# Create shared/adapter directories explicitly and configure them as shown below.
hermes skill-publisher doctor
```

Replace `OWNER` with the public repository owner. Plugin or configuration changes require a new Hermes process/session; restart a long-lived gateway. Do not consider publication ready until `doctor` exits `0`.

## Classification

Every publishable `SKILL.md` needs flat Agent Skills metadata:

```yaml
---
name: portable-example
description: Use when a repeatable workflow needs this procedure.
metadata:
  skill-publisher-scope: shared
---
```

Exact beta values:

- `shared`: eligible for deferred or manual publication.
- `local`: stays under the active profile.
- `private`: stays local and manual publication refuses it.

`project`, missing values, nested values, and malformed values are invalid. By default, their create call remains local and receives a warning. Set `require_classification: true` to reject such creates before Hermes core runs.

## Configuration

```yaml
skills:
  external_dirs:
    - "~/.agents/skills"

plugins:
  enabled:
    - hermes-skill-publisher
  entries:
    hermes-skill-publisher:
      require_classification: false
      shared_root: "~/.agents/skills"
      adapter_roots: {}
```

To enable adapters, replace the empty mapping with a nested mapping:

```yaml
plugins:
  entries:
    hermes-skill-publisher:
      require_classification: false
      shared_root: "~/.agents/skills"
      adapter_roots:
        claude: "~/.claude/skills"
        codex: "~/.codex/skills"
```

The plugin never creates configured roots and never edits Hermes configuration. Roots cannot be symlinks or overlap. Any existing canonical or adapter object—including an empty directory, broken symlink, or same-target unowned symlink—is a conflict. There is no force/adopt option.

## Operator CLI

```text
hermes skill-publisher status [--json]
hermes skill-publisher audit [--json] [--limit N]
hermes skill-publisher doctor [--json]
hermes skill-publisher publish NAME [--json]
hermes skill-publisher unpublish NAME --scope {local,private} [--json]
```

Exit codes are `0` for success, `1` for unexpected internal/I/O failure, and `2` for a configuration, compatibility, classification, ownership, collision, or safety blocker. Human and JSON output never include skill contents.

## Compatibility boundary

| Layer | Beta claim |
|---|---|
| Package/frontmatter | Strict Agent Skills-compatible frontmatter for published packages. |
| Discovery | Native `~/.agents/skills` consumers can discover canonical packages; configured per-skill adapters provide discovery in other roots. |
| Instructions/tools | No general claim. A skill may still name Hermes-only commands, tools, hooks, or plugins. |

No network/Git synchronization, cross-profile scan, content secret classifier, project scope, or model-facing publication tool is included.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Safety model](docs/SAFETY.md)
- [Limitations](docs/LIMITATIONS.md)
- [Operations](docs/OPERATIONS.md)
- [Examples](docs/EXAMPLES.md)

## Development

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m compileall -q __init__.py hermes_skill_publisher tests scripts
python scripts/check_public_repo.py
```

Tests must use isolated temporary `HOME` and `HERMES_HOME` values. Set `HERMES_AGENT_SOURCE` to a compatible local checkout to run the real-Hermes E2E test.

## License

[MIT](LICENSE)
