# Examples

## Explicit shared skill

```markdown
---
name: release-checklist
description: Use when preparing a small software release.
license: MIT
compatibility: Requires standard shell and Git commands.
metadata:
  skill-publisher-scope: shared
allowed-tools: shell_exec read_file
---

# Release checklist

Verify tests, build artifacts, and release notes before tagging.
```

## Explicit local or private policy

```yaml
metadata:
  skill-publisher-scope: local
```

```yaml
metadata:
  skill-publisher-scope: private
```

Both remain in the active profile. `private` also blocks manual publication until the author deliberately changes metadata.

## Neutral configuration with adapters

```yaml
skills:
  external_dirs:
    - "$HOME/shared-agent-skills"
plugins:
  enabled:
    - hermes-skill-publisher
  entries:
    hermes-skill-publisher:
      require_classification: true
      shared_root: "$HOME/shared-agent-skills"
      adapter_roots:
        harness-a: "$HOME/.harness-a/skills"
```

Create both directories before starting Hermes. The adapter is a relative per-skill symlink, for example:

```text
$HOME/.harness-a/skills/release-checklist -> ../../shared-agent-skills/release-checklist
```

The exact relative payload depends on configured roots.

## JSON diagnostics

```bash
hermes skill-publisher doctor --json
hermes skill-publisher status --json
hermes skill-publisher audit --json --limit 20
```

A missing classification in default mode stays local and adds `skill_publisher.classification_missing` to a successful create result. Required mode returns retryable `skill_publisher.classification_required` without invoking Hermes core.
