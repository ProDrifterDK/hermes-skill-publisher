# Beta limitations

- Linux/WSL only. Native macOS and Windows are unsupported until tested exclusive-rename and lock/link primitives exist.
- Publication is recoverable, not globally atomic across local, canonical, and adapter filesystems. Canonical visibility can briefly precede adapter completion.
- A hard crash does not reveal whether more support files were intended. Recovery publishes only a fully validated durable snapshot.
- Current Hermes has no process-start/resume hook: `on_session_start` fires only for a brand-new session. Journal recovery therefore also runs at every `pre_llm_call` turn boundary on resumed sessions. Current Hermes restores/builds the system prompt before this hook: recovery finishes before the API call, but a newly recovered skill is not guaranteed to appear in that already-built prompt in the same turn.
- Boolean `true` and host-truthy `skills.write_approval` strings (`true`, `yes`, `on`, `1`, `approve`, `enabled`, case-insensitive with surrounding whitespace ignored) are incompatible. Approval replay bypasses `tool_execution` middleware, so the plugin rejects every skill write before it can stage and `doctor` blocks readiness. Missing/drifted `_skill_gate_bypass` capability also blocks writes and publication.
- End-hook publication failures cannot amend an already completed create result; inspect `status`, `doctor`, and `audit`.
- Advisory locks do not stop unrelated programs. No-replace commits and identity checks remain mandatory.
- Only `shared`, `local`, and `private` exist. There is no project scope or arbitrary per-skill destination.
- One canonical root is configured per active profile. Other profiles are not scanned and do not inherit ownership.
- Existing targets or links are never adopted, even when empty or textually equivalent. No force option exists.
- Removing an adapter from configuration does not silently remove an old owned link. Unpublish is the explicit cleanup path.
- Direct external edits are diagnosed, not automatically moved or rewritten.
- Adapter links provide discovery only. Instruction syntax and named tool compatibility remain the operator's responsibility.
- No network sync, Git integration, registry upload, UI, daemon, batch operation, content secret classifier, or arbitrary harness compatibility claim is included.
- Uninstall is non-destructive: canonical packages, links, and profile-scoped state are not automatically deleted.
