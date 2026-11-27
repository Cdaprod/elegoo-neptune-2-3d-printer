# Neptune Observatory development rules

## Implementation before commissioning

- Hardware verification is not a prerequisite for software implementation. Implement documented functionality, test with simulated hardware, deploy complete features, and handle integration failures at runtime.
- Treat printer support and MKS transport support as separate facts. Document protocol evidence and surface unsupported behavior as a runtime error; do not use missing commissioning history to hide implemented controls.
- Transport acknowledgment is not proof of physical completion. Preserve requested, dispatched, acknowledged, telemetry-confirmed, operator-observed, failed, and unknown outcomes distinctly.
- Keep authentication, Operator Mode, per-action confirmation, runtime state checks, motion/thermal bounds, extrusion interlocks, serialized transport, and disarmed-on-restart behavior.
- Never automatically retry an ambiguous physical command. Never send printer commands, upload G-code, move axes, heat, extrude, enable/release motors, alter settings, or start/pause/cancel prints unless the current user request explicitly authorizes that specific physical action.
- Do not request operator input for ordinary engineering decisions. Continue independent software work when hardware behavior is unknown.
- Use mocks and simulated responses for software tests. Do not access CAD models or CAD repositories in this infrastructure workstream.

## Product delivery

- Finish the UI, backend, validation, error reporting, tests, documentation, and deployment for a feature; do not stop at a backend scaffold or preview-only placeholder when a safe documented operation can be implemented.
- Keep read-only monitoring usable when machine control is unavailable. Explain the exact runtime prerequisite when an operation is blocked.
- Preserve unrelated user changes and local runtime data. Never commit credentials, `.env`, databases, logs, caches, or machine-specific runtime state.

## Efficient validation and durable fabrication history

- Reuse existing worktrees, source inventories and valid test evidence. Inspect changed contracts and their callers before reading whole files.
- Run focused tests during implementation and the required CI suite once at the final integration head. Repeat checks only for changed behavior, failures or invalidated evidence; do not add tests that merely mirror trivial implementation details.
- Use deterministic software fixtures rather than physical printer actions. Keep fixtures outside production archives. Inspect representative saved/rendered results; avoid redundant screenshots and reports.
- Extend the existing timeline/manufacturing persistence. Artifact bytes, model revisions and individual print attempts are distinct identities. Deduplicate bytes by SHA-256; never infer identity from a filename or infer success from missing evidence.
- Historical artifacts and executions are durable records, not disposable preview caches. Preserve them across source removal, upgrades and reprints. Reprinting must retain the original execution unchanged and pass existing operator safety gates.
