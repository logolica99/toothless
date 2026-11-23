# Phase 07.2.6 — Browser PID exhaustion diagnosis

Status: PARTIAL; the bounded `TasksMax=160` correction and diagnostic attribution update are deployed, while the historical Chromium crash cause remains unproven and live browser smoke is awaiting a separate owner-approved task. The protected worker passed before and after deployment. No VM, Softnet setting, DNS configuration, browser sandbox policy, or network policy was changed.

## Historical incident

The incident is task `task-af45475e355baea655f4eff2113686f4`, execution `opencode-e99f3fcc81a449f4a0c7fa7ce472829a`. The owner-approved task has exactly one execution and one Telegram result delivery (message 771, status `sent`); TaskStore remains `incomplete`. There is no recorded retry, page load, text extraction, or source row. The first navigate failed after 1,032 ms as `browser_target_crashed`; the browser connection was invalidated. The saved termination is `unknown_exit`, not an inferred signal. No historical task was resubmitted.

The failure sample measured `pids.current=112` with `pids.max=128`, and `pids.events max` increased by 68. The peak sample was 112 rather than 128, but the event counter demonstrates that task creation was denied by a PID controller during the run. This is strong evidence of PID pressure, not proof that PID denial caused Chromium’s target crash. Browser and Playwright identities were absent at the failure snapshot. Memory peaked at 272,719,872 of 1,073,741,824 bytes, with zero memory max/OOM/OOM-kill deltas. The bounded journal-category scan for the incident minute found no EAGAIN, SIGSEGV, AppArmor denial, sandbox error, X11 error, or OOM entry. No exit signal was retained.

The twelve historical processes classified only as `other` (107 threads) cannot be identified after the fact: their individual PID, PPID, `comm`, thread count, and cgroup membership were not persisted. They must not be assumed unrelated to Chromium. The classifier counted known roles and collapsed all remaining processes into `other`.

## Pre-deployment read-only preflight and capacity

| Check | Result | Evidence |
|---|---|---|
| Tart state | PASS | Configured `TART_HOME=/Volumes/toothless/Tools/Tart`; Tart reports `toothless-worker` running. No direct Tart start, reset, or shutdown occurred. |
| Protected worker readiness | PASS | Existing bounded readiness probe reports VM running, Softnet policy verified, DNS relay and guest resolver ready, OpenCode auth metadata ready, execution agent ready, and both approved profiles ready. |
| Task and execution ledger | PASS | Immutable TaskStore query: 14 queued historical tasks remain untouched; no dispatching/running/cancelling/unknown tasks. Host execution store has no unresolved execution. Target task remains incomplete with one execution. |
| Schedules | PASS | Three schedules are cancelled and three completed; all four recorded occurrences are succeeded; no unresolved/due occurrence exists. |
| Viewer/browser authorization and processes | PASS | Live read-only session-store query reports zero unexpired/unconsumed grants (epoch 29); no host viewer listener on 8768/8769, no guest browser authorizations, and no Chrome/Chromium/Playwright process. Desktop service is active and its fixed `desktop_status` operation returns available. |
| Deployed desktop service unit | PASS | Installed and reviewed source SHA-256 both equal `a5f91d9dc343d3a60c90936b0c81b4ae09f9d89e51c2c1217613e53450fbb89c`. Effective values: `TasksMax=128`, `MemoryMax=1G`, `CPUQuota=150%`, `LimitNOFILE=256`. |
| Service and parent cgroup | PASS | Current service `pids.current=7`, `pids.max=128`, `pids.events max=68`, `MemoryCurrent=46,276,608`, `MemoryMax=1,073,741,824`. Immediate parent `system.slice`: `pids.current=56`, `pids.max=max` (unlimited), `pids.events max=611`. The guest cgroup root does not expose a `pids.current` controller file; `system.slice` is unbounded. |
| Guest-wide task/memory capacity | PASS (bounded) | Guest reports 6,043,416 KiB RAM, 5,644,992 KiB available, no swap, 121 visible processes / 152 threads, kernel `threads-max=42,178`, and `pid_max=4,194,304`. This shows no guest-wide task pressure in the present idle sample; it does not prove future browser workload demand. |
| Current desktop process identities | PASS (current sample only) | Six cgroup members: bash (PID 20460, PPID 1), Xvfb (20468/20460), Openbox (20473/20460), Python (20478/20460), xterm (20479/20460), and bash (20480/20479); 7 systemd tasks total. Browser is not active. This cannot reconstruct the twelve processes from the historical crash. |

The worker was already running at preflight and passed protected readiness; no VM startup, reset, or shutdown was performed. All diagnostics used fixed read-only Tart/systemd/proc/cgroup operations or host SQLite read-only queries; no command lines, environments, credentials, browser profiles, webpage content, or tokens were read or stored. After fresh quiescence checks and owner approval, only `toothless-desktop.service` was stopped and restarted through the protected installer; the Telegram LaunchAgent was then gracefully reloaded to load the host parser.

## Resource-limit assessment

Before deployment, the service-only cap was 128. A previous run reached 128/128 and recorded 543 PID-limit events; the target run recorded a 68-event increase while sampling at 112/128. The same target run’s first browser operation failed before a Chrome or Playwright PID was observed. Across those observations, service-level PID pressure is confirmed and is a plausible contributor to browser startup failure. The causal link remains unconfirmed because the startup failure did not retain `EAGAIN`, a Chrome/driver exit code or signal, or a failure-time process inventory. The historical logs did not show OOM, AppArmor, sandbox, or X11 errors.

`TasksMax=160` adds 32 task slots (25%) to the desktop service only. It is the smallest candidate requested for evaluation and adds headroom beyond the prior 128-task saturation. The immediate parent is unlimited, guest kernel limits are high, current guest usage is low, and the service memory/CPU/FD limits remain unchanged. This supports a bounded 160 trial but does not establish that 160 will be sufficient or eliminate all crash causes. `TasksMax=192` is not justified by the available evidence.

The owner explicitly approved `TasksMax=160` and diagnostic deployment. Immediately before deployment, fresh checks found no viewer, browser process, authorization record, active task, unresolved execution, or schedule occurrence to interrupt. `ops/worker/install-visible-browser-runtime.sh` deployed the reviewed guest file set after stopping only `toothless-desktop.service`; six transferred files were unchanged by SHA-256, and only the unit and broker changed. Post-deployment source/guest SHA-256 values match: service unit `f9af9eaf969f66fd590ef52a5942009306ba3d943208acf5a1995c38ec023b75`; broker `f71f2f46fd544ae98d8bc8c00b26b27aa3a0d78a4a78f542db67a996637071cc`. The service is active/running with `NRestarts=0`, `TasksCurrent=7`, `TasksMax=160`; MemoryCurrent is 37,416,960 bytes and memory OOM counters are zero. The broker socket is group-owned mode `0660`; the empty browser authorization directory is root-owned mode `0750`. Systemd confirms `NoNewPrivileges=yes`, `PrivateTmp=yes`, `PrivateDevices=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, all inspected kernel/capability protections, and the existing private/link-local/metadata network denials. MemoryMax=1G, CPUQuota=150%, and LimitNOFILE=256 are unchanged. Rollback is to restore only `TasksMax=128`, daemon-reload, and restart the desktop service after fresh quiescence checks.

## Diagnostic attribution improvement

The deployed broker captures, only on browser startup-failure/disconnect lifecycle events, a bounded inventory of at most 16 processes from the desktop service cgroup: PID, PPID, sanitized process `comm`, thread count, and cgroup path relative to the desktop service. It excludes command-line arguments, environments, file paths outside that cgroup, and browser/page contents. The host diagnostic serializer independently validates the same bounded fields. This can help attribute short-lived Chromium children in a future approved run; it cannot identify the vanished historical processes. The host parser was loaded by one graceful Telegram LaunchAgent restart (PID 58651 → 96726); launchd reports one controller and the owner-private log records polling reconnected. The 39 focused broker/parser tests pass; Ruff and `git diff --check` pass. BasedPyright passes for the changed host parser file (0 errors); checking the broker reports 14 existing errors at unchanged private-API/type-narrowing/cast sites, so the broker has no type-check pass claim.

## Acceptance and remaining work

| Requirement | Result |
|---|---|
| Protected worker, current service limits, cgroup parent, guest RAM/task capacity | PASS |
| Historical PID-controller denials confirmed | PASS |
| Historical `other` process identities recoverable | FAIL / unavailable; not persisted and processes are gone |
| PID denials proven to cause Chromium crash | PARTIAL; plausible contributor, causal proof absent |
| Process-inventory telemetry change | PASS; deployed and host parser reloaded |
| `TasksMax=160` deployed | PASS; owner approved exact value and restart scope |
| Authorized visible-browser smoke | PASS | Owner approved task `task-8feb19c782c10c89da92cf431639e375`; one execution `opencode-a8a9085e01ab4d4c983990123d75eab2` completed with worker exit 0 in 7.90 seconds. Host diagnostics recorded successful `toothless_browser_navigate` and `toothless_browser_extract_text`, zero retries, and zero browser tool failures. The delivered result reported HTTP 200, title `Example Domain`, final URL `https://example.com/`, and the rendered first sentence. |
| TaskStore and Telegram delivery | PASS | Task is `succeeded`, has exactly one execution, and exactly one result delivery (message 776, status `sent`). No duplicate `/run` or retry was performed. |
| PID event delta during smoke | PASS | Service `pids.events max` was zero after deployment and remained zero immediately after the task. |
| Peak PID/thread count during smoke | PARTIAL | No failure occurred and post-run desktop state is 7 tasks with Xvfb/Openbox/xterm/broker only; peak in-run `pids.current` and thread count were not emitted by the successful task’s persisted diagnostic record. |
| Browser and authorization cleanup | PASS | Post-run guest check found no Chrome/Playwright processes and zero browser authorization records; service returned to 7/160 tasks. Memory OOM counters remain zero. |
| Owner Watch Live observation | NOT RUN | Owner confirmed they did not open Watch Live for the smoke task. Browser use is evidenced by the recorded tool calls, not by direct owner observation. |
| Blog homepage Telegram research acceptance | NOT RUN | Still requires a fresh task, explicit `/run`, structured source verification, one delivery, cleanup, and owner observation. |

The fixed visible-browser smoke now passes through Telegram after the approved 160-task change. Phase 07.2.6 remains PARTIAL until a separately approved bounded blog-homepage research task completes with structured source evidence, correct Telegram delivery, cleanup, and live owner observation. Peak in-run task/thread utilization was not captured for this successful smoke; future failure-time inventory is deployed, and the historical crash root cause remains unproven.
