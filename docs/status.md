# Personal agent status

## Current phase

The OpenCode-engine specification is authoritative. Phase 00 host proof is VERIFIED with `opencode-go/glm-5.3`: the adapter returned READY, `Paris`, and a parsed JSON response under deny-all tools, with no fallback. The `steps: 5` setting likely resolved the earlier max-steps summary; this is supported by the message and subsequent runs, but not proven by a controlled A/B request. Phase 01 is VERIFIED FOR THE CURRENT DEPLOYMENT PROFILE: prior owned endpoint isolation tests passed, Phase 01.7's trusted DNS relay was sealed into the baseline, the fixed worker reset successfully, and post-reset policy, dynamic resolver, DNS, HTTPS, port 53 denial, and key-only SSH checks passed. One initial post-reset HTTPS lookup timed out; resolver diagnostics and a follow-up returned HTTP 200. DNS-based exfiltration/direct public DoH/DoT and future IPv6 routes remain limitations. Phases 02.1–02.3 now provide a typed worker interface, strict SSH transport, and host-owned SQLite execution recovery; deterministic and real reset/reconnect tests passed. Worker operations remain unregistered as model tools; no autonomous execution is enabled. Phase 03 has not started.

## Deployment state

- Unattended operation: blocked until phases 01, 13, and applicable phase 14 checks pass.
- OpenCode runtime engine: Phase 00 host adapter verification passed with `opencode-go/glm-5.3`, five agent steps, deny-all tools, bounded output/time, and no model fallback. Deployed VM execution remains a later phase.
- Optional Nanobot foundation: pinned and locally verified for possible channel/service reuse.
- Telegram credentials: not configured and not needed in Phase 00.
- Worker VM: new DNS-capable baseline sealed; the pre-DNS baseline remains as `toothless-ubuntu-24.04-base-rollback-20261116`. Reset persistence and all six checks passed after protected launch, with one transient DNS timeout before HTTPS later returned 200. The worker, baseline, and rollback are stopped. Direct Tart launches remain unprotected. Unattended execution remains disabled. See `reports/phase-01-final.md` and `reports/phase-01.7.md`.
- Worker execution interface: `nanobot/worker/` contains typed operations, a deterministic fake backend, pinned-key SSH transport, and host-owned SQLite records under the host runtime `worker/executions.sqlite3` path. Intent is committed before dispatch; acceptance, snapshots, cancellation requests, and errors are persisted without storing command text or SSH credentials. Restart and accepted-SSH interruption reconciled by the original ID without resubmitting; reset history returned `unknown`. `ops/worker/reset.sh` documents the protected-start and helper-install provisioning step. The interface is not registered as a model tool. See `reports/phase-02-03.md`.

## Evidence

- Phase report: `reports/phase-00.md`
- Final Phase 00 report: `reports/phase-00-final.md`
- Final Phase 01 report: `reports/phase-01-final.md`
- Architecture decision: `docs/decisions/0001-adopt-nanobot-v0.4.1.md`
- Reuse map: `docs/upstream-map.md`
- Active specification: `INSTRUCTIONS.md` (OpenCode primary-engine edition, November 16, 2026)
- Feature/task inventory: `docs/features.md`
