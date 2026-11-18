# Personal agent feature and task map

This repository follows the delivery order in `INSTRUCTIONS.md`. A feature is implemented when its code and deterministic checks exist. It is verified only after its required real-environment gate passes; mocks and static checks do not prove VM isolation, provider pricing, or third-party behavior.

## Phase summary

| Phase | Feature outcome | Implementation status | Verification status |
| --- | --- | --- | --- |
| 00 | OpenCode installation, explicit model selection, billing safety, structured CLI proof | Implemented: restrictive POC and fail-closed adapter | VERIFIED: adapter returned READY, factual answer, and parsed JSON under deny-all tools |
| 01 | Isolated Linux worker VM, clean baseline, lifecycle, host/LAN isolation | Prior owned IPv4 isolation checks passed; validated rate-limited DNS-over-TLS relay was sealed into the baseline and survived reset; policy, DNS, HTTPS, port 53 denial, and key-only SSH passed after reset | VERIFIED FOR CURRENT DEPLOYMENT PROFILE; DNS exfiltration, public encrypted DNS egress, future IPv6 profiles, and unattended execution remain unverified |
| 02 | Remote execution, processes, files, package installation, execution IDs | Phase 02.1 typed interface/fake backend, Phase 02.2 pinned-key SSH transport, and Phase 02.3 host-owned SQLite execution recovery implemented | 02.1 deterministic, 02.2 real-worker, and 02.3 live recovery checks VERIFIED; no model tool registration or autonomous execution |
| 03 | Owner-only Telegram, attachments, commands, callbacks, deduplication | Phases 03.1–03.2 implement owner-only polling, fixed controller commands, and access to existing worker records/cancellation; attachments, callbacks, and task submission are not implemented | Local checks and owner-account live flows PASS; full acceptance PARTIALLY VERIFIED because live unauthorized-account testing was unavailable (local authorization tests PASS) |
| 04 | Durable tasks, steering, cancellation, progress, restart recovery | Implemented host-owned queued task layer with SQLite state, event history, Telegram update dedupe, pause/resume/instruction changes, and worker execution associations; no task dispatch | VERIFIED for queued task management; no AI/worker execution |
| 05.1 | Explicit Telegram `/run` task dispatch to guest-only OpenCode | Implemented: task leasing, persistent intent, fixed guest runner, exact model allowlist, effective deny-all verification, poll-to-terminal, cancellation/reconciliation, and result persistence; guest CLI is installed | VERIFIED: real Telegram task, result, restart recovery, duplicate `/run`, core commands, and bounded live cancellation passed. Auth/quota failure injection was covered locally, not live. See `reports/phase-05-01.md` |
| 05.2 | Owner-approved OpenCode tools in the Ubuntu workspace | Implemented: rootless guest identity, file tools, bounded shell policy, fixed model/provider policy, and existing SSH artifact path; fixed root-owned runner is invoked through the existing noninteractive guest sudo path; package installation needs a separately approved guest bootstrap | VERIFIED for scoped workspace tools: real task and artifact checks PASS; duplicate `/run` did not create a second execution; controller restart preserved the result; live cancellation terminated the guest execution and produced one cancellation reply. Package installation remains unsupported pending a separately approved guest bootstrap. See `reports/phase-05-02.md` |
| 05.3 | Guest browser and public website interaction | Implemented: rootless guest Playwright/Chromium MCP tools, separate no-shell OpenCode profile, pinned systemd cgroup IPv4 destination denials and all-IPv6 denial, bounded downloads/screenshots/output, and task-scoped cleanup | VERIFIED for the current browser-only task profile: real Example Domain and official Python research tasks used browser tools, returned source-backed results to Telegram, and persisted on the host; controller restart and duplicate `/run` passed; cancellation stopped one active browser-profile run before its first browser tool call. In-flight Chromium navigation cancellation remains unverified. Public egress/DNS exfiltration remain possible. See `reports/phase-05-03b.md` and `docs/browser-security.md` |
| 05.4+ | Broader OpenCode sessions, privileged installs, and export | Not started; root/system package manager and unattended execution remain disabled | NOT RUN |
| 06 | CSV analysis, reproducible scripts, checked artifacts, Telegram delivery | Not started | NOT RUN |
| 07 | Public research, browser interaction, downloads, citations | Not started | NOT RUN |
| 08 | Editable memory, project context, versioned workflows | Not started | NOT RUN |
| 09 | Schedules, triggers, quiet hours, deduplicated notifications | Not started | NOT RUN |
| 10 | Scoped connectors, exact-action approvals, effect reconciliation | Not started | NOT RUN |
| 11 | Responsive dashboard, task stream, files, approvals, usage | Not started | NOT RUN |
| 12 | Voice notes, optional replies, guest desktop control and handoff | Not started | NOT RUN |
| 13 | Startup services, watchdog, health, backups, restore, retention, upgrades | Not started | NOT RUN |
| 14 | Independent evaluation pack, adversarial cases, fault injection | Not started | NOT RUN |
| 15 | Multiple isolated workers and bounded delegation | Not started | NOT RUN |

## Implemented features

| Area | Implemented behavior | Evidence |
| --- | --- | --- |
| Upstream foundation | Nanobot v0.4.1 pinned at commit `3404fe83e22b7b2cb3edae1348ed45dd9b8ad051`; licenses/notices retained; dependency lock and advisory-range review recorded | `reports/phase-00.md`, `docs/decisions/0001-adopt-nanobot-v0.4.1.md` |
| Foundation checks | Clean dependency sync, import-boundary checks, deterministic fake-provider contract, stable worker operation identity, placeholder-only secrets | `reports/phase-00.md`, `tests/personal_agent/test_foundation.py` |
| OpenCode Phase 00 boundary | Separate immutable configuration, exact free-model allowlist, shell-free bounded CLI execution, typed JSON events/errors, duration/session metadata, JSON final-response parsing, timeout/cancellation cleanup, and total/event output limits | `nanobot/providers/opencode_config.py`, `nanobot/providers/opencode_cli.py`, `tests/personal_agent/test_opencode_cli.py`, `reports/phase-00-final.md` |
| Mac discovery | Apple silicon, memory, CPU, macOS, APFS storage, and free-space preflight without recording stable hardware identifiers | `ops/worker/discover.sh`, `ops/worker/preflight.sh` |
| Dedicated storage | Tart VM/cache data fixed to `/Volumes/toothless/Tools/Tart`; no internal-disk fallback | `ops/worker/config.sh`, `docs/worker-vm.md` |
| Pinned virtualization stack | Tart 2.41.1, Softnet 0.25.0, exact Ubuntu OCI digest, reviewed checksums/signature, and fixed Softnet privileged path/hash/mode | `ops/worker/config.sh`, `reports/phase-01.md` |
| Bounded VM | Idempotent base/worker provisioning with 4 vCPUs, 6144 MB RAM, and a 20 GB disk | `ops/worker/provision.sh`, `reports/phase-01.md` |
| Restricted launch | Softnet-only VM launch with no host directory, clipboard, USB, exposed-port, bridge, or private-network allowlist; launcher verifies the directional host-gateway block and exposes only a strict host-side DNS relay on port 55353 | `ops/worker/softnet_launcher.py`, `ops/worker/dns_forwarder.py`, `ops/worker/start.sh`, `ops/worker/test-static.sh`, `reports/phase-01.7.md` |
| SSH bootstrap and hardening | Selected Ed25519 key, pinned observed host key, persistent root-owned guest key path, password/root/keyboard-interactive login disabled, forwarding disabled, vendor password locked only after a fresh key-only session succeeds | `ops/worker/harden.sh`, `reports/phase-01.md` |
| Clean lifecycle | Guest sync and graceful shutdown separated from emergency hard stop; baseline sealing refuses a running VM | `ops/worker/shutdown.sh`, `ops/worker/stop.sh`, `ops/worker/seal-baseline.sh` |
| Baseline persistence | Hardened baseline sealed after clean shutdown; recreated worker retained nonempty SSH configuration, key-only login, and locked vendor password | `reports/phase-01.md` |
| Fixed-target recovery | Confirmation-gated worker reset and fixed-name emergency stop | `ops/worker/reset.sh`, `ops/worker/stop.sh` |
| Worker contracts | Static safety checks and four focused VM lifecycle/hardening contract tests | `ops/worker/test-static.sh`, `tests/personal_agent/test_worker_vm_contract.py` |
| Live worker recovery | Public HTTPS/package download, marker/package reset, CPU-bound emergency stop, and host/guest CPU/RAM/disk limits | `reports/phase-01.md` |
| Phase 02.1 worker contract | Typed execute/poll/terminate and binary file operations, execution IDs, structured errors, timeouts, cancellation, bounded output, relative path validation, and fake in-memory backend | `nanobot/worker/`, `tests/personal_agent/test_worker_interface.py`, `reports/phase-02-01.md` |
| Phase 02.2 SSH worker | Strict Ed25519 host-key pinning, public-key-only SSH, no agent/port forwarding, JSON stdin protocol to a fixed guest helper, execution IDs, poll/cancel, bounded output, binary-safe guest workspace files, explicit unavailable/unknown errors, and no host fallback | `nanobot/worker/ssh_backend.py`, `ops/worker/guest_worker_agent.py`, `ops/worker/install-execution-agent.sh`, `tests/personal_agent/test_worker_ssh_integration.py`, `reports/phase-02-02.md` |
| Phase 02.3 persistent executions | Host-only SQLite intent/acceptance/result/error records, deduplication across controller recreation, query/reconcile by ID, retryable cancellation, and honest unknown state after guest reset; command text and SSH credentials are not recorded | `nanobot/worker/execution_store.py`, `nanobot/worker/interface.py`, `tests/personal_agent/test_worker_execution_store.py`, `reports/phase-02-03.md` |
| Phase 03.1 Telegram controller | Separate outbound long-poll runtime; exact numeric owner ID plus private-chat check; `/start`, `/ping`, `/help`, `/worker`, and `/status`; durable host-side update deduplication; arbitrary text is never sent to shell, worker, or OpenCode | `nanobot/personal_agent/telegram_controller.py`, `docs/telegram-setup.md`, `tests/personal_agent/test_telegram_controller.py`, `reports/phase-03-01.md` |
| Phase 03.2 Telegram task controls | `/tasks` lists the latest 10 host records; `/task` shows stored details and reconciles nonterminal records; `/cancel` persists intent and uses the existing pinned-key worker interface without starting a stopped VM; outgoing messages use bounded, escaped Telegram HTML for common Markdown formatting | `nanobot/worker/execution_store.py`, `nanobot/personal_agent/telegram_controller.py`, `tests/personal_agent/test_telegram_controller.py`, `reports/phase-03-02.md`; local tests and owner-account live workflows PASS; direct unauthorized-account live check NOT RUN |
| Phase 04 conversational task management | `/new` and ordinary text create durable queued tasks; `/tasks`, `/task`, `/update`, `/pause`, `/resume`, and `/cancel` manage task records; TaskStore persists event history, Telegram source update IDs, progress/error fields, and execution associations; queued tasks are never dispatched | `nanobot/personal_agent/task_store.py`, `nanobot/personal_agent/telegram_controller.py`, `tests/personal_agent/test_task_store.py`, `tests/personal_agent/test_telegram_controller.py`, `reports/phase-04.md`; local checks and live owner workflow PASS |
| Phase 05.1 explicit OpenCode task dispatch | `/run <task_id>` atomically claims one task and records a guest execution ID before dispatch. Task status becomes running only after SSH acceptance; the same ID is polled to terminal and reconciled after restart without resubmission. OpenCode v2.3.0 ran as Ubuntu `admin` with `opencode-go/glm-5.3`, tools disabled, empty plugins/MCP, bounded runtime/output, guest-local auth, result persistence, and Telegram delivery. Queued tasks were not auto-dispatched. | `nanobot/personal_agent/opencode_task.py`, `nanobot/personal_agent/task_store.py`, `nanobot/personal_agent/telegram_controller.py`, `ops/worker/opencode_task_runner.py`, `ops/worker/guest_install_opencode.sh`, `ops/worker/install-opencode.sh`, `ops/worker/opencode-auth-login.sh`, `ops/opencode/phase05/opencode.json`, focused tests, `reports/phase-05-01.md`; real task, restart recovery, duplicate `/run`, core Telegram commands, and bounded cancellation PASS |
| Phase 05.2 rootless guest tools | After owner `/run`, OpenCode gets read/edit/glob/grep/shell tools in the guest workspace, under a dedicated non-sudo identity. System package managers and elevation are denied. The Ubuntu image lacks `python3-venv`/pip provisioning, so additional Python packages are unavailable until a one-time guest package bootstrap is separately approved. The guest-local provider credential remains readable to commands running as the same user; this is a guest trust limitation. Artifact files remain available through authenticated worker file operations under `opencode-task-workspace/`; Telegram document delivery is not implemented. | `ops/opencode/phase05/opencode.json`, `ops/worker/opencode_task_runner.py`, `ops/worker/guest_install_opencode.sh`, `ops/worker/guest_worker_agent.py`, `nanobot/personal_agent/opencode_task.py`, `tests/personal_agent/test_opencode_guest_policy.py`, `tests/personal_agent/test_guest_worker_workspace_alias.py`, `reports/phase-05-02.md`; effective config, guest authentication, and rootless file-interface probe PASS; task `task-c6e9bf3a36f5ff2f52c58ab6e2d40b21` succeeded with execution `opencode-6029197c9fbc4256846de904d9ff63e1`, invoked `write` and `shell`, generated `random_stats.py` and `random_stats_results.csv`, and passed independent seeded-value/statistics verification. Duplicate-run, controller restart, and active cancellation with one Telegram reply PASS |

## Outstanding task list

### Finish Phase 00 — external free-model gate

- [x] Inspect OpenCode v2.3.0 flags, effective configuration, authentication state, model catalog, and provider environment without exposing tokens.
- [x] Select and enforce `opencode-go/glm-5.3`; reject every other model before execution.
- [x] Compare manual and adapter contexts; raise the agent limit to five while preserving deny-all tool permissions; adapter READY, factual, and JSON checks pass.
- [x] Add deterministic success/JSON parsing, rejection, quota-stop, network failure, malformed/oversized event, timeout cleanup, cancellation, and total-output-limit tests.
- [x] Explain the earlier maximum-steps result as likely related to the one-step cap, while recording that this was not proven by a controlled A/B request.

### Finish Phase 01 — isolation release gate

- [x] Block owned host-gateway TCP/UDP fixtures and owned Wi-Fi/VPN host listeners with UFW inactive; key-only SSH management remained usable.
- [x] Test owned IPv4 link-local and metadata-shaped listeners twice, including after protected worker restart; fixtures were removed and reset left the worker stopped.
- [x] Test an owned scoped IPv4 multicast fixture and IPv4-mapped IPv6 to an owned Wi-Fi endpoint; no multicast packet was received and mapped IPv6 timed out.
- [x] Exercise controlled local DNS rebinding and HTTP redirects to owned Mac endpoints; the local simulation passed, but public owned-domain and application-layer loopback protections remain unverified.
- [ ] Test IPv6 ULA/global/private paths when a guest route exists; current guest has no ULA/global IPv6 route.
- [x] Inspect runtime mounts, forwarded SSH agent, host Docker socket, and host resource limits; no shared host mount, agent socket, or Docker socket was present.
- [x] Download public package metadata/package content, modify a marker, install a package, reset, and prove both changes disappear.
- [x] Stop a CPU-bound guest in under one second and restore the worker from the clean baseline; confirm 4 CPU, 6144 MB, and 20-GB bounds from host and guest views.
- [x] Apply and reapply the directional Softnet host-gateway block, remove the TCP/UDP port 53 exceptions, expose DNS only through the validated port 55353 relay, and verify baseline reset persistence.

### Foundation and MVP sequence

- [x] Phase 02.1: typed worker interface and deterministic fake backend; no host execution fallback, model-tool registration, or real worker transport.
- [x] Phase 02.2: authenticated pinned-key SSH worker transport with bounded guest process/output lifecycle, binary-safe workspace files, acceptance acknowledgement for uncertain dispatch, and explicit no-fallback outage behavior; the fixed helper must be installed again after a VM reset.
- [x] Phase 02.3: durable host-owned execution intent, acceptance, result, cancellation, and error records; controller restart/SSH interruption reconciliation; missing guest history remains unknown; arbitrary OpenCode execution remains disabled.
- [x] Phase 03.1: owner-only outbound Telegram polling and fixed status/help responses; local backend checks pass.
- [x] Phase 03.2 implementation, local checks, and owner-account live task workflows: list, inspect, reconcile, and cancel only existing host-recorded worker executions; no command text dispatch.
- [ ] Complete direct unauthorized-account Telegram acceptance; no second account was available during the live run, while local owner/private-chat authorization tests pass.
- [ ] Phase 03: owner-only Telegram long polling, secure callbacks, uploads, commands, inbound deduplication, and outbound uncertain-delivery handling.
- [x] Phase 04 task records and Telegram lifecycle controls: queued tasks, update-ID deduplication, audit events, pause/resume, instructions, cancellation, task-to-execution links, and restart persistence; no worker/model dispatch.
- [x] Phase 04 live Telegram acceptance: create via `/new` and natural text, lifecycle commands, and restart recovery; duplicate update replay is covered by deterministic tests; no worker execution starts.
- [x] Phase 05.1 implementation: explicit `/run` authorization, single task lease, worker acceptance state, fixed guest-only OpenCode v2.3.0 entry point, exact model allowlist, deny-all effective configuration, bounded runtime/output, persisted result/error, cancellation/restart reconciliation, and no resubmission of uncertain work.
- [x] Phase 05.1 live gate: actual OpenCode task, result delivery, restart recovery, duplicate `/run`, core commands, and bounded live cancellation PASS; no paid fallback or automatic queued execution.
- [x] Phase 05.2 live acceptance: actual OpenCode tool-use task, verified CSV/script, artifact retrieval, duplicate `/run`, controller restart, and live cancellation with one Telegram notice PASS. Separately approve a package bootstrap before claiming package installation support.
- [x] Phase 05.3 live Telegram acceptance: real Example Domain task `task-ecadc5587237f9894b0267b00b7cc87b` and official Python research task `task-1763475eb0cbe5d319e8e36fed5951fb` passed with actual browser-tool events, source evidence, host persistence, and Telegram delivery. Controller restart and duplicate `/run` passed. Harmless cancellation task `task-9bd9eb79820795a32e10a10e7525e3a3` was cancelled by the worker with one Telegram notice and clean scope/process cleanup; cancellation occurred before the first browser tool call, so in-flight Chromium cancellation remains unverified. Public egress/DNS exfiltration are not contained. See `reports/phase-05-03b.md` and `docs/browser-security.md`.
- [ ] Later Phase 05: broader OpenCode sessions, additional policy/budget controls, privileged package-manager approval, and safe artifact export.
- [ ] Phase 06: synthetic CSV-to-checked-plots workflow with reproducible scripts, provenance, safe artifact collection, and Telegram delivery.

### Useful assistant and expanded access

- [ ] Phase 07: broader worker-only research workflows, additional source validation, and manual handoff beyond the Phase 05.3 scoped browser tools.
- [ ] Phase 08: scoped editable memory and versioned workflows with provenance and forget/export behavior.
- [ ] Phase 09: durable schedules/triggers, timezone/DST rules, quiet hours, deduplication, and notification outcomes.
- [ ] Phase 10: scoped account connectors, exact-payload approvals, idempotency, reconciliation, revoke/disconnect, and access inventory.
- [ ] Phase 11: authenticated responsive dashboard reusing the task/policy service with inert artifact handling.
- [ ] Phase 12: independently optional voice and guest-desktop features without host microphone/camera/desktop authority.

### Before unattended use and later

- [ ] Phase 13: launchd/guest services, watchdog, health, safe mode, encrypted backup/restore drill, retention, upgrade staging, and rollback.
- [ ] Phase 14: independent 20-scenario evaluation pack, adversarial inputs, fault injection, release thresholds, and evidence reports.
- [ ] Phase 15: only after release gates, add at most two separately isolated workers with bounded delegation, shared budgets, provenance, cancellation, and measured benefit.

## Deployment blockers

- Unattended execution remains blocked until the real Phase 01 containment gate, Phase 13 operational recovery, and applicable Phase 14 release checks pass.
- OpenCode Phase 00 host proof and Phase 05.1 live inference are verified. Phase 05.2's authenticated rootless guest tool path passed a real Telegram task: OpenCode wrote and ran the fixed-seed script, and the resulting CSV passed independent value/statistics verification via SSH. Duplicate `/run`, controller restart recovery, and active cancellation with exactly one Telegram cancellation reply also passed live. The Ubuntu image lacks `python3-venv`/pip; system package-manager elevation remains disabled pending separate operator approval. The guest shell can read its own provider credential and has public egress; no host credential or host mount is available. Unattended task execution remains disabled.
- Phase 05.3B's separate browser profile and transient systemd cgroup IP filter are installed on the running guest. Owned IPv4 loopback/private/CGNAT/link-local/metadata and IPv6 ULA/link-local/mapped fixtures, controlled metadata-shaped DNS rebinding, public DNS/HTTPS, actual Playwright render/JS/link/screenshot/redirect checks, and cleanup passed. Real Telegram browser research, restart recovery, duplicate-run protection, and worker cancellation passed for the current profile. Cancellation happened before browser-tool invocation; in-flight Chromium cancellation remains unverified. Public internet egress and DNS exfiltration remain possible; this is not general egress isolation. See `reports/phase-05-03b.md` and `docs/browser-security.md`.
- The Telegram bot token is configured in its protected file outside the repository. No model credentials, protected connectors, or public dashboard are configured.
