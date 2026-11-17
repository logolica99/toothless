# Phase 00 verification report

Historical report for the initial free-model attempt; superseded for current model/configuration by `reports/phase-00-final.md`.

Date: 2026-11-16 (America/Los_Angeles)

Status: implemented; real free-model response gate blocked by provider quota

## Pinned foundation and engine

- Optional Nanobot foundation: upstream `https://github.com/HKUDS/nanobot.git`, tag `v0.4.1`, commit `3404fe83e22b7b2cb3edae1348ed45dd9b8ad051`, retained MIT license and third-party notices.
- Host development OpenCode: `opencode v2.3.0`, binary `/Users/jubaerjami/.opencode/bin/opencode`, SHA-256 `9f3147cae4ca323827d3ca39451b0d3431b2906e79a08e9b09d1eb34bb703f52`.
- Current authorized test model: `opencode-go/glm-5.3`, listed in the authenticated OpenCode Go model catalog.
- The adapter accepts only the exact configured model and has no alternate model, provider, or automatic fallback.

The installed binary version and hash are pinned evidence, not a reproducible source build. Before deployment, Phase 05 must install and verify OpenCode inside the worker VM and review that release's source tag, dependency lock, license, notices, and security advisories. OpenCode tool permissions are application policy, not a sandbox; deployed execution remains VM-only.

## Effective proof configuration

The tracked Phase 00 configuration selects `opencode/glm-5.3-flash-free`, denies every permission, limits the custom `phase00-readonly` agent to one step, and uses standalone archived JSON output. `opencode debug config --standalone` confirmed those settings. It silently omitted experimental `policy` keys, so they were removed and are not claimed as controls. The Python controller is the provider/model allowlist: it accepts only the exact selected ID and has no fallback. `opencode models --standalone` lists the signed-out OpenCode catalog, while only the controller-approved ID can reach execution.

The real command was:

```text
opencode run --standalone --archived --format json --agent phase00-readonly --model opencode/glm-5.3-flash-free "Reply with exactly READY. Do not use tools."
```

The latest application-adapter run emitted a structured start event followed by `provider.free-limit`, HTTP 429, and a reported retry interval of 7,929,000 ms. The adapter recorded two events, a session identifier, and 0.706 seconds before returning typed `rate_limited`. The run did not switch model or provider. Per the specification, quota exhaustion is a hard stop, so no other free model was tried silently.

## Checks

| Check | Result |
| --- | --- |
| OpenCode version and CLI flags | PASS: v2.3.0; explicit model, JSON output, agent, standalone, and archived flags observed |
| Authentication/provider environment review | PASS: no authenticated integration or provider key detected |
| Effective restrictive configuration | PASS: exact model, deny-all permissions, one-step read-only agent; provider allowlist enforced by controller |
| Signed-out model catalog | PASS: exact free IDs enumerated; selected `opencode/glm-5.3-flash-free` |
| Real explicit model selection | PASS: attempted exact selected ID with no fallback |
| Real `READY` response | BLOCKED: provider returned `provider.free-limit` / HTTP 429 |
| OpenCode execution adapter | PASS: typed JSON-lines boundary, exact argv, explicit allowlist, no shell, metadata, bounds, timeout and cancellation |
| Deterministic adapter tests | PASS: 9 passed covering success/JSON, unauthorized model, rate limit, network failure, malformed/oversized event, timeout, cancellation, and total output limit |
| Disposable-directory integrity | PASS: only the copied configuration existed after real adapter runs; its hash remained unchanged |
| Ruff and BasedPyright for adapter | PASS |
| Import boundaries | PASS: 13 rules and 218 files |
| `git diff --check` | PASS |

The earlier Nanobot baseline remains valid: locked dependency synchronization passed; 10,845 upstream tests passed with 100 skipped before six manifest-owned optional-channel dependency failures; after installing those documented channel dependencies, all 20 affected cases passed. The GitHub advisory ranges reviewed for the pinned Nanobot revision list v0.4.1 as patched or newer than their patched ranges. This is not a complete security audit.

## Remaining gate

Phase 00 cannot be marked verified until the same explicit free model returns the harmless expected response after its quota resets, while the restrictive configuration and absence of paid fallback remain in force. A model disappearance, changed price, authentication demand, or continued quota failure remains a hard stop and requires operator review rather than automatic substitution.
