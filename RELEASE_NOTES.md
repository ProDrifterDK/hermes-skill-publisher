# 0.1.0 beta release notes

This first beta adds policy-governed promotion of explicitly shared Hermes skills into a canonical Agent Skills root. Normal publication waits for a successful, non-interrupted turn boundary. Durable journals recover interrupted promotion/unpublish work without adopting or replacing external objects.

## Required release gates

- Linux/WSL with `renameat2(RENAME_NOREPLACE)` verified on the destination filesystem.
- Hermes `tool_execution` middleware capability plus non-zero plugin CLI exit-code propagation; v2026.7.30 is the documented minimum compatible release line.
- `skills.write_approval` normalized off (the default); boolean true and host-truthy strings are hard incompatibilities. The host `_skill_gate_bypass` capability must also be present and intact.
- Canonical root precreated and exactly listed in active `skills.external_dirs`.
- Full tests, compile check, public hygiene scan, isolated real-Hermes E2E, and diff hygiene passing.

## Important boundaries

This beta does not inspect content for secrets, synchronize over a network, scan other profiles, support project scope, or promise tool/instruction compatibility in arbitrary harnesses. End-hook errors are asynchronous to the completed create result. Crash recovery validates the durable snapshot but cannot prove that no further support files were intended. On current Hermes, `pre_llm_call` recovery completes before the API call but after system-prompt restoration/build, so same-turn prompt discovery is not guaranteed.
