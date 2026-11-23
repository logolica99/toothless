# Toothless Phase 07.2.4 — Chromium Lifecycle Diagnostics and Fatal-Error Propagation

## Status

PARTIAL; implementation is deployed and the new owner-approved visible-browser run exercised the headed browser, repeated extraction, lifecycle instrumentation, cleanup, and Telegram delivery. The run ended `incomplete` solely because the agent reached its configured 12-step ceiling; it recorded zero browser-tool failures. The owner confirmed seeing Chrome navigate in Watch Live. This is not a successful task-completion acceptance, so Phase 07.2.4 is not VERIFIED. The historical browser termination mechanism remains UNKNOWN. No historical task was retried, no resource/security limits were changed, and no live fatal crash was provoked.

## Historical execution review

The owner-approved task `task-035183842960882d53525bd727b75aac` has one execution, `opencode-f7666aeb83dc4d76858dcde7a0847d48`, and remains terminal `incomplete`. Its sanitized host record observed one successful headed-browser `navigate`, then a `TargetClosedError` categorized as `browser_context_closed` at extraction, followed by rejected operations against the invalidated session. The run observed 41 events, used all 12 configured steps, recorded 10 browser-tool errors, and had zero provider retries. Telegram received the incomplete explanation as message 759. No second execution was created.

This proves that the browser context/session was unavailable after navigation; it does not prove that Chromium was killed by the OS. The earlier `browser_process_alive` field checked the Playwright driver process, not the Chromium browser process. The returned `TargetClosedError` is consistent with both process failure and application/context closure, so it does not distinguish the mechanism. The report and retained records contain no Chromium exit status/signal or failure-time cgroup counters. No historical cause is asserted.

## Read-only runtime evidence

The protected VM was initially stopped and was brought up only with `ops/worker/start.sh`; the protected Softnet/DNS launcher verified isolation and DNS forwarding. Guest readiness then passed for VM state, Softnet, DNS relay/resolver, OpenCode authentication, execution helper, workspace profile, and browser profile. No task inference was started. The desktop unit was active/running with `NRestarts=0`; at the post-start inspection its current memory use was 383,717,376 bytes, `TasksCurrent=7`, and its configured task maximum was 128. The observed cgroup `memory.events` and `pids.events` counters were zero, and CPU throttling was not observed in that snapshot. These are post-start baseline readings, not measurements from the failed execution. The unit configuration remains `MemoryMax=1G`, `CPUQuota=150%`, `TasksMax=128`, and `LimitNOFILE=256`; no limit was changed.

The desktop service was not restarted between its 21:34 UTC start and the historical failed execution around 21:46 UTC. A later protected VM startup at 22:06 UTC provides only a post-restart baseline and resets current service/cgroup counters. The inspected journal showed normal Xvfb/Openbox startup but no failure-time kernel OOM/AppArmor record or Chromium exit record. AppArmor, sandbox, X11, resource exhaustion, browser crash, and application cleanup therefore remain hypotheses rather than findings for that historical task.

## New live acceptance task

Owner-approved task `task-1c00da3deba5048da58deaf7f76a9482` was approved at `2026-11-21T23:04:28.277328Z` and ran once as `opencode-88e1960f80b24de7b02c52571e2a6a2d`. TaskStore records terminal `incomplete`, result delivery `sent`, Telegram message `764`, and exactly one execution. Sanitized diagnostics record 47 events, zero provider retries, 12 step starts against a 12-step limit, zero browser-tool failures, and browser `navigate`, `extract_text`, and `click_link` tool names. The retained bounded timeline has only 32 entries (8 each of step-start/text/tool-use/step-finish) and is marked truncated. The owner confirmed Watch Live showed the actual headed Chrome window navigating the site.

The broker journal retained 23 lifecycle records between `2026-11-21T23:04:36.124Z` and `2026-11-21T23:05:11.817Z`. It observed browser/context/page creation, successful navigation and extraction lifecycle boundaries, browser connected/context open/page open through the recorded extraction calls, and explicit `close_session` cleanup. It reported no browser tool failures, and `memory.events` OOM/OOM-kill deltas were zero. Browser PID and Playwright-driver PID remained unavailable (`null`), so exact process identities and exit codes were not observed. At peak, the service cgroup reached `pids.current=128` of `pids.max=128`; the `pids.events` `max` delta reached 543, and memory reached 700,878,848 of 1,073,741,824 bytes. This is confirmed task/PID-limit pressure, not evidence that it crashed Chromium or caused the prior task's closure. No limits were changed. At explicit cleanup, browser connection/context/page were closed, broker telemetry reported no browser process in its cgroup snapshot, and a subsequent guest check found no Chrome/Chromium process or task authorization file. TaskStore has zero structured source records for this task; its delivered result text contains the findings/URLs, so structured source persistence was not demonstrated.

The delivered answer reports navigation categories AI, Energy, Robotics, Space, Funding, GTA 6, and Other. It provides category-page evidence for AI, Energy, Robotics, and Space; Funding's navigation target was identified but its content extraction was not completed. GTA 6 and Other URLs were described as unverified guesses. Because the agent used the full step budget, the requested verified category/URL list is partial and the task correctly remains `incomplete`; this task was not retried.

## Local implementation

- The visible broker now samples bounded process/thread and FD counts, the Playwright-driver and Chromium identities/liveness separately, best-effort exit code/signal data, cgroup PID/memory current and maximum values, memory/PID event deltas, and bounded CPU usage/throttling counters at browser lifecycle events. It keeps a 12-entry in-memory event ring and emits safe metadata only; no command lines, page contents, URLs, prompts, profile data, or credentials are recorded. Missing counters remain unknown rather than being reported as zero.
- Lifecycle termination labels distinguish an observed signal/normal exit, process disappearance, transport disconnect, and application cleanup when the relevant state is available. Otherwise the category remains unknown. The earlier process name ambiguity was removed: Chromium process metrics no longer reuse the Playwright-driver liveness field.
- Fatal browser-session errors write a task/execution-bound, mode-0600 signal file in the private per-execution `/run/toothless-browser-*` directory. The fixed guest runner verifies ownership, permissions, path, and identity, terminates the current OpenCode process through its existing process-group/systemd-scope control, cleans up the browser helper/authorization, preserves bounded partial text, and returns `incomplete` without retry. If the browser session remains healthy, ordinary navigation timeout, HTTP error, invalid request, and operation errors remain recoverable and do not invalidate the session.
- The host serializer accepts only bounded lifecycle fields and fixed enumerated values. It excludes private payloads and malformed/unexpected types. `incomplete` remains terminal and separate from Telegram delivery. No schedule, task, or model permission was expanded.
- The existing event-size diagnostics were restored to the common serializer path so event-size metadata remains available independently of fatal-browser diagnostics.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Historical task/execution replay | PASS | Task `task-035183842960882d53525bd727b75aac` remains terminal with one execution; no new execution was made. |
| Browser lifecycle reuse and stale/disconnected state | PASS (deterministic) | Browser helper tests verify the same browser page is retained across navigate/extract calls and a disconnected runtime is closed/rejected instead of silently relaunched. |
| Fatal versus recoverable browser errors | PASS (deterministic) | Context closure invalidates the same execution and rejects the next call; healthy HTTP, timeout, and invalid-request failures remain recoverable. |
| cgroup/process/FD diagnostic parsing | PASS (deterministic) | Tests cover PID/memory limits, event counters, CPU counters, process/thread/browser PID, and unavailable counters remaining unknown. |
| Fatal signal validation and runner abort | PASS (deterministic) | Task-bound signal parsing rejects a mismatched task and strips private fields. A runner test confirms one child termination after the signal, bounded helper cleanup, zero retry, and `incomplete` output. |
| Partial result and host persistence | PASS (deterministic) | Host tests preserve partial answer and sanitized lifecycle metadata while excluding page content; `incomplete` remains terminal. |
| Focused browser/broker/runner/TaskStore tests | PASS | 73 passed after the final focused change. |
| Full `tests/personal_agent` suite | PASS | 241 passed, 7 skipped when run with permission to bind loopback. A sandboxed run failed six existing loopback-binding tests with `Operation not permitted`; the permitted rerun passed. |
| Ruff | PASS | All touched Python modules and tests passed. |
| BasedPyright | PASS | `nanobot/personal_agent/opencode_task.py`: 0 errors, warnings, or notes. |
| Worker static safety checks | PASS | `bash ops/worker/test-static.sh`. |
| Import boundaries and whitespace | PASS | 13 rules across 218 files; `git diff --check` clean for the touched code. |
| Deployment / installed source hashes | PASS | After fresh preflight (worker ready; zero active/unknown tasks, uncertain worker executions, active/uncertain occurrences, unexpired grants, viewer token/listener, guest authorizations, or browser processes), only `toothless-desktop.service` was stopped and restored using `bash ops/worker/install-visible-browser-runtime.sh`. All seven installed broker/helper/runner/unit/config files match their local SHA-256 hashes. The unit is active, `NRestarts=0`, broker socket is group-owned mode `0660`, authorization directory is root-owned/group `toothless-browser` mode `0750`, and it contains no authorizations. Post-deploy worker readiness passes. |
| Service resources / policy | PASS | Post-deploy: `MemoryCurrent=36,065,280` bytes, `TasksCurrent=7`, `NRestarts=0`; actual limits remain `MemoryMax=1,073,741,824`, `CPUQuotaPerSecUSec=1.500000s` (150%), `TasksMax=128`, and `LimitNOFILE=256`. |
| Controller reload / singleton | PASS | After a second clean preflight, the existing LaunchAgent received `SIGTERM` and restarted gracefully: PID 37307 → 86336. Launchd reports `active count = 1`; process inspection found exactly one controller. Tailscale Serve remained tailnet-only with `/memory` and `/desktop` routes unchanged. |
| Same-broker visible browser run / Watch Live | PARTIAL | Fresh owner task `task-1c00da3deba5048da58deaf7f76a9482`, one execution `opencode-88e1960f80b24de7b02c52571e2a6a2d`; actual navigate/extract/click tool names, zero browser errors, successful lifecycle boundaries, and owner-confirmed visible Chrome. The task ended `incomplete` at the 12-step ceiling; it did not verify all requested category URLs. Telegram delivery message 764; browser authorization/process cleanup passed. |
| Browser lifecycle diagnostics at runtime | PARTIAL | Lifecycle journal recorded 23 events; memory/PID OOM deltas were zero. The cgroup reached 128/128 tasks with `pids.events max` delta 543 and peak memory 700,878,848/1,073,741,824 bytes. Browser and Playwright-driver PIDs/exit status were unavailable, so exact process identity remains unknown. |
| Fatal failure under a live model execution | NOT RUN | The exact runner abort path is covered deterministically; no production task was crashed or interrupted. |
| Root cause of the historical browser closure | UNKNOWN | No failure-time Chromium exit code/signal or cgroup evidence exists; the observed closed context does not identify who closed it. |

## Deployment and acceptance gate

The reviewed deployment is installed. Fresh preflight found no active task/execution/occurrence, browser authorization, or viewing session. The existing protected installer restarted only `toothless-desktop.service`; no VM shutdown/reset, Softnet/DNS change, network-policy modification, baseline change, or resource-limit change occurred. Installed file hashes match the local reviewed sources. The controller was gracefully restarted only after a second clean preflight; launchd and process inspection confirm one poller, and the tailnet-only Serve routes are unchanged. The new live run exercised the deployed browser and viewer but ended incomplete at the 12-step ceiling; no retry is authorized or performed. Do not retry `task-035183842960882d53525bd727b75aac`, `task-f7fd7753d7c9fc9115aa9d6fd2a169a6`, `task-ba8552131c4a1693b4aa54f76579a129`, or `task-1c00da3deba5048da58deaf7f76a9482`.

Installed SHA-256 values (local source and guest destination matched):

| File | SHA-256 |
|---|---|
| `visible_browser_broker.py` | `67874047c1f85ebae714d35313dc7df37bb727bc267903e6bbfced7a5ca46e97` |
| `browser_mcp.py` | `439af9627637d929296b10af5900f28ce89b84516404eb8b31541b13caf530b2` |
| `opencode_task_runner.py` | `23a25017bf623a50f03ebe5ce83a2598e5634f96bf47895ededced78f36d5875` |
| `toothless-desktop-session.sh` | `798f21368496eaa0c7fb79262c6bf865f651edbe2030ed9176f4dd6848421180` |
| `toothless-desktop.service` | `a5f91d9dc343d3a60c90936b0c81b4ae09f9d89e51c2c1217613e53450fbb89c` |
| `toothless-browser-authorizations.conf` | `2782e8f543e0188ee8170cda29a33212d15b856912ab407aad884e829acd43f2` |
| `toothless-browser-auth-dir.service` | `33a56a9b1b5aed315d128f7314e09a4151e3d7ac6db6a7fdd3fb51b0493f62d9` |

## Remaining limitations

The historical root cause remains unknown. The new run shows persistent browser connectivity and successful extraction, but also confirmed cgroup PID-limit pressure; neither establishes the cause of the earlier context closure. The task reached its step ceiling before completing all requested verification. PID identities were unavailable despite deployed lifecycle instrumentation, and task source metadata was not structured in TaskStore. Deterministic tests cover fatal-session propagation; live fatal failure was not tested. Phase 07.2.4 remains PARTIAL, not VERIFIED.
