# Personal agent status

## Current phase

Phase 00 is implemented and locally verified. The selected foundation is Nanobot v0.4.1 at commit `3404fe83e22b7b2cb3edae1348ed45dd9b8ad051`. Phase 01 must not be treated as verified until its containment checks run on the deployment Mac and a real disposable worker VM.

## Deployment state

- Unattended operation: blocked until phases 01, 13, and applicable phase 14 checks pass.
- Real model provider: not configured; Phase 00 uses a deterministic offline provider.
- Telegram credentials: not configured and not needed in Phase 00.
- Worker VM: not provisioned.

## Evidence

- Phase report: `reports/phase-00.md`
- Architecture decision: `docs/decisions/0001-adopt-nanobot-v0.4.1.md`
- Reuse map: `docs/upstream-map.md`
