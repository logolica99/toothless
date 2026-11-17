# Phase 05.1 — First OpenCode-Powered Telegram Task

Date: 2026-11-17 (America/Los_Angeles)

Status: **VERIFIED FOR PHASE 05.1.** The selected model ran inside the protected Ubuntu worker, the answer was stored on the host and delivered in Telegram, controller restart recovery and duplicate-run behavior passed, and a bounded live cancellation was confirmed by the worker. Model tools remain disabled.

## Implementation and security

- Reused the existing Telegram task store, worker interface, pinned-key SSH backend, and host-owned execution database. `/run <task_id>` remains the only task dispatch authorization; queued Phase 04 tasks are not auto-dispatched.
- Persisted dispatch intent and the worker execution ID before SSH dispatch. The task stays `dispatching` until SSH acceptance is recorded, then the same execution ID is polled to terminal. A reconnect or controller restart reconciles the original ID without submitting the task again.
- Added terminal polling to `OpenCodeTaskRunner`. If the worker disconnects after acceptance, the task becomes unknown rather than being resubmitted. A bounded timeout attempts guest termination and reports uncertain state if termination cannot be confirmed.
- The root-owned Ubuntu runner accepts only `opencode-go/glm-5.3` and invokes OpenCode without a shell. Effective global and agent permissions deny all tools; tools are disabled, plugins and MCP are empty, and updates are disabled. Runtime, event, output, and answer sizes are bounded.
- Authentication remains guest-local. Preflight found one active OpenCode credential record (`opencode`) in the guest OpenCode database; the credential value was not read or displayed. No Mac auth directory, host credential, or host filesystem mount was copied to the VM. Guest root can read the guest credential.
- No paid-model/provider fallback is implemented: the guest runner selects and validates only the exact approved model. Account billing/entitlement details were not inspected; provider availability may still be rate-limited.

## Protected worker and OpenCode preflight

`ops/worker/start.sh` had started the VM through the protected Tart/Softnet launcher. The launcher acknowledged the directional host-flow block and DNS relay policy. At verification the worker was running with 4 CPUs, 6144 MB RAM, and a 20 GB disk. Direct Tart launches remain unprotected.

`ops/worker/install-opencode.sh` completed on the running worker. OpenCode CLI **2.3.0** is installed in Ubuntu ARM64. Binary SHA-256: `5c7d89bc2d5fa5d83cc7768b813ca671368e70b8b9eda9d1ca9e6ec6b66d8ce0`. Config SHA-256: `85f4457e7ed330262e3797bceef759b68938042dc757977f764365fa5aa20eff`. The live `--verify-config` preflight passed for the exact model, deny-all permissions, disabled tools, empty plugins/MCP, and disabled updates. No inference was made during preflight.

After login OpenCode reports its provider catalog and empty XDG config directory as additional debug-config sources. The verifier was adjusted to allow only those known non-user-configuration sources while still rejecting any unexpected config source and requiring the pinned `/etc/toothless/opencode.json` policy. The installer was rerun and effective verification passed.

## Live Telegram task and recovery

Owner created task **`task-db9b3defaf116b3e7f10a2a359294adb`** for: “Explain the difference between CPU and GPU in three concise bullet points”. Before approval the host record was `queued`, had no execution ID, and had no approval timestamp. Owner sent `/run task-db9b3defaf116b3e7f10a2a359294adb`; Telegram acknowledged dispatching to Ubuntu.

The host persisted execution ID **`opencode-60d53d9786b04b56846548414a1194a3`** and approval time `2026-11-17T08:33:15.982809+00:00`. Worker acceptance was `2026-11-17T08:33:17.368096+00:00`; guest start was `2026-11-17T08:33:17.398350+00:00`; guest completion was `2026-11-17T08:33:22.356899+00:00`. Worker exit code was 0. The structured event result reported model `opencode-go/glm-5.3`, three events, and 4.841 seconds of model execution. The host task record is now `succeeded`, stores the actual 611-character response, and is associated with that one execution ID.

Actual generated response delivered to the owner Telegram chat:

> - **Architecture**: CPUs have a few powerful, complex cores optimized for sequential tasks; GPUs have thousands of smaller, simpler cores designed to work simultaneously.
> - **Processing model**: CPUs excel at serial processing—handling complex logic, branching, and varied tasks quickly one after another; GPUs excel at parallel processing—performing the same operation on massive amounts of data at once.
> - **Typical use cases**: CPUs run operating systems, applications, and general-purpose computing; GPUs handle graphics rendering, gaming, video processing, and increasingly AI/machine learning workloads.

The first controller implementation treated an accepted `running` snapshot as a failure and wrote `failed` even though the guest process was still active. I inspected the same execution ID in the host and guest journals, confirmed the guest had not completed, and reconciled it over the existing pinned-key SSH backend; I did not resubmit it. I fixed the runner to poll accepted work to terminal, repaired this one task record using a guard on its exact error and associated execution ID, and recorded a reconciliation event. After restarting the controller, startup reconciliation consumed the original succeeded worker record, saved the result, and sent it to Telegram. The follow-up Telegram `/task` response showed `succeeded` and the stored answer. Future requests use the fixed poll-to-terminal path.

Owner live command results:

| Command | Result |
|---|---|
| `/task task-db9b3defaf116b3e7f10a2a359294adb` | PASS — `succeeded`, stored response and original execution ID shown |
| `/tasks` | PASS — completed task listed; another earlier task remained queued |
| `/ping` | PASS — “Toothless is online.” |
| `/worker` | PASS — worker reported running without changing it |
| `/status` | PASS — controller connected, worker running, Phase 05.1 capabilities reported |
| Duplicate `/run` on completed task | PASS — said already succeeded; not submitted again |
| `/cancel` on completed task | PASS — said succeeded; cancellation was not applied |
| Controller restart | PASS — recovered the same execution ID and delivered the stored result; no inference was resubmitted |
| Bounded live cancellation | PASS — task `task-98089e7532aa763b289c68e01028ad93` reached `cancelled` using one accepted execution, `opencode-10c58602cecd44758bab4d90940285ba` |
| Cancellation confirmation deduplication | One duplicate confirmation was observed live. The duplicate-notice fix passes locally but was not re-tested with another model request |

Other previously queued tasks remain queued and unapproved. No automatic task scheduling is enabled.

## Failure handling and cancellation

Local fake-backend tests cover authentication failure, rate/quota error, timeout, worker unavailable, accepted-execution disconnect, cancellation, duplicate run, result-delivery failure, and restart reconciliation without resubmission. No paid fallback is implemented. Provider quota was not intentionally exhausted, credentials were not invalidated, and those provider failures were not induced live.

The owner explicitly ran `/run task-98089e7532aa763b289c68e01028ad93` followed immediately by `/cancel task-98089e7532aa763b289c68e01028ad93`. The host task record reached `cancelled`; the associated execution ID `opencode-10c58602cecd44758bab4d90940285ba` was accepted once and the worker journal reached `cancelled` with one task-to-execution association. Worker acceptance time: `2026-11-17T08:51:54.490514+00:00`; worker completion time: `2026-11-17T08:52:06.631336+00:00`. No second execution was created. Telegram returned the same cancellation confirmation twice because both the `/cancel` handler and background task completion handler sent it. I fixed the notification race so a background completion does not send another cancellation notice after a persisted `/cancel`; a regression test passes. That notification fix was not retested with another live model request.

## Regression checks

| Command | Result |
|---|---|
| `UV_CACHE_DIR=/tmp/toothless-phase05-uv-cache .venv/bin/python -m pytest tests/personal_agent` | PASS — 75 passed, 7 skipped; skipped tests require real worker fixtures. One pytest-asyncio fixture-scope deprecation warning. |
| `.venv/bin/ruff check nanobot/personal_agent nanobot/worker/ssh_backend.py nanobot/worker/execution_store.py ops/worker/opencode_task_runner.py tests/personal_agent` | PASS |
| `.venv/bin/basedpyright nanobot/personal_agent nanobot/worker/ssh_backend.py nanobot/worker/execution_store.py ops/worker/opencode_task_runner.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_telegram_controller.py` | PASS — 0 errors, 0 warnings, 0 notes |
| `bash -n ops/worker/install-opencode.sh ops/worker/guest_install_opencode.sh ops/worker/opencode-auth-login.sh` | PASS |
| `.venv/bin/python -m py_compile nanobot/personal_agent/task_store.py nanobot/personal_agent/opencode_task.py nanobot/personal_agent/telegram_controller.py nanobot/worker/ssh_backend.py ops/worker/opencode_task_runner.py` | PASS |
| `.venv/bin/python -m scripts.check_import_boundaries` | PASS — 13 rules, 218 files |
| `git diff --check` | PASS |
| Live worker OpenCode effective-config verification | PASS — no model inference during config checks |

## Final status and remaining limits

Phase 05.1 is **VERIFIED** for the requested deployment profile: protected Ubuntu execution, exact model, owner approval, persistent dispatch, real inference, host result storage, Telegram delivery, restart recovery, duplicate-run protection, core Telegram commands, and bounded live cancellation passed. Local fake-backend tests cover auth, quota, timeout, disconnect, worker-unavailable, and delivery failures; those provider failures were not induced live, and no quota was intentionally exhausted. OpenCode Go account entitlements and billing were not audited. The cancellation duplicate-notice fix is locally regression-tested but was not repeated live. Unattended scheduling remains disabled, and model tools remain denied.
