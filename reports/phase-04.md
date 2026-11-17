# Phase 04 — Conversational task management

Date: 2026-11-16

## Result

Phase 04 is VERIFIED for the requested task-management scope. Real owner-account Telegram task creation, lifecycle commands, task listing/detail, controller restart recovery, cancellation persistence, and existing execution-command compatibility passed. Local tests verify Telegram update deduplication, authorization, and that tasks do not invoke a worker. Task acceptance only stores a queued request; no model call, worker command, or VM startup occurs in this phase.

The prior Phase 03.2 live unauthorized-account test remains NOT RUN because a second Telegram account was unavailable. It remains explicitly outstanding in `docs/status.md` and is not marked passed here. Phase 04's unauthorized create/modify gate was tested locally for other users and group chats as requested.

## Implementation

- Added `nanobot.personal_agent.task_store.TaskStore`, a host-owned SQLite store with unique task IDs, the original request, appended instructions, timestamps, status, progress/error fields, source Telegram update ID, append-only lifecycle/instruction events, and task-to-worker-execution associations. SQLite state uses `synchronous=FULL`, foreign keys, and owner-only database file permissions.
- Task creation commits before the Telegram update is recorded for offset advancement. `source_update_id` is unique in the task table; replay returns the same task instead of inserting a duplicate.
- `/new <description>` and ordinary private owner text create `queued` tasks. The response says AI execution is disabled and work has not started. There is no dispatch path to OpenCode or the worker.
- Added `/tasks`, `/task <task_id>`, `/update <task_id> <instructions>`, `/pause <task_id>`, and `/resume <task_id>`. Task cancellation with `/cancel <task_id>` is recorded as a terminal `cancelled` state. Queued tasks can be paused or cancelled; paused tasks can be resumed or cancelled. Repeated state transitions do not create duplicate events.
- Preserved Phase 03 worker execution controls with `/executions` and `/execution <execution_id>`. `/cancel <execution_id>` retains the existing pinned-key SSH cancellation path and never starts a stopped worker. Task IDs use `task-` plus 32 lowercase hexadecimal characters to distinguish them from prior execution IDs.
- Task state lives in `tasks.sqlite3` next to the Telegram update database by default; `TOOTHLESS_TASK_DB` can select another absolute path. No task data was placed in the repository.

## Files changed

- `nanobot/personal_agent/task_store.py`
- `nanobot/personal_agent/telegram_controller.py`
- `tests/personal_agent/test_task_store.py`
- `tests/personal_agent/test_telegram_controller.py`
- `docs/architecture.md`
- `docs/features.md`
- `docs/status.md`
- `docs/telegram-setup.md`
- `reports/phase-04.md`

## Real Telegram results

The replacement bot token was loaded from the existing protected configuration; no token value was printed or stored in the repository. The first sandboxed startup could not reach Telegram and was stopped. The live run used the approved network-enabled controller, a private temporary task/update/execution database directory, and the same databases across a controller restart. Telegram polling connected on both starts without HTTP 409. The controller was stopped cleanly after verification.

| Live operation | Result |
|---|---|
| `/new Phase 04 acceptance test; do not perform the task` | PASS; created `task-65d32f5184e494a1c6ca2639035f75ba`, state `queued`, and the acknowledgment said AI execution was not enabled and work had not started |
| Natural-language `Research the latest open-source AI coding agents.` | PASS; created `task-ca9e296546fdfc2db0cc78077217d988`, state `queued`, with zero associated execution IDs |
| `/tasks` and `/task` | PASS; list and detail commands were handled by the real controller; details were repeated successfully after restart |
| `/update task-ca9e296546fdfc2db0cc78077217d988 Keep the answer concise and include primary sources.` | PASS; instruction persisted in the record |
| `/pause task-ca9e296546fdfc2db0cc78077217d988` then `/resume task-ca9e296546fdfc2db0cc78077217d988` | PASS; event history records `paused` then `queued`; final status remained queued |
| `/cancel task-65d32f5184e494a1c6ca2639035f75ba` | PASS; state changed from `queued` to `cancelled`; no worker execution was started |
| `/cancel task-65d32f5184e494a1c6ca2639035f75ba` after restart | PASS; existing cancelled state was reported idempotently |
| `/executions` and `/execution phase04-compat-execution` | PASS through real Telegram. These commands read a terminal fake execution fixture (`succeeded`, exit 0) seeded in the private test database; this validates command compatibility, not a new real-worker run |
| Controller restart | PASS; the same task IDs, instructions, statuses, and events were recovered from SQLite; `/tasks` and `/task` worked after reconnection |
| Worker/OpenCode execution and VM startup | NOT RUN by design; task records all have zero execution associations, and no task command invokes either system |

An additional plain-text test message, `cancel task-65d32f5184e494a1c6ca2639035f75ba`, was accepted as a natural-language task (`task-d6804f5bcc851c3c5f611566642ad14f`) because it had no slash prefix. It was then cancelled through `/cancel`; its final state is `cancelled`. This confirms that all ordinary owner text is interpreted as a task request in this checkpoint.

The test database contained 18 processed Telegram updates. Task rows at shutdown were: `task-ca9e296546fdfc2db0cc78077217d988` queued with its instruction update; `task-65d32f5184e494a1c6ca2639035f75ba` cancelled; and `task-d6804f5bcc851c3c5f611566642ad14f` cancelled. All three had no associated worker execution. Task and Telegram SQLite files had mode `0600` under the private test directory. Replies were accepted by Telegram's sendMessage API; the application logged each authorized command only after the API call completed.

## Local verification

| Check | Result |
|---|---|
| `/new` and ordinary text create durable queued tasks without worker dispatch | PASS, deterministic controller tests |
| `/tasks`, `/task`, instruction update, pause, resume, cancel | PASS, deterministic controller/store tests |
| Duplicate Telegram update ID does not create a second task or reply | PASS, deterministic replay test; direct Bot API redelivery was not injected |
| Controller restart returns the same task data and event history | PASS, deterministic test and live Telegram restart |
| Unauthorized user/group cannot create or modify tasks | PASS, deterministic controller test; the earlier direct unauthorized-account Phase 03 check remains NOT RUN |
| Task-to-execution association persistence | PASS, store test; none of the live acceptance tasks were associated with an execution |
| Worker execution commands retained | PASS locally and through live Telegram with the synthetic terminal fixture described above |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync pytest -q tests/personal_agent/test_task_store.py tests/personal_agent/test_telegram_controller.py tests/personal_agent/test_worker_execution_store.py tests/personal_agent/test_worker_interface.py tests/personal_agent/test_worker_ssh_integration.py` | PASS; 37 passed, 7 skipped, exit 0; skipped tests require separate live SSH configuration |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync ruff check nanobot/personal_agent/task_store.py nanobot/personal_agent/telegram_controller.py tests/personal_agent/test_task_store.py tests/personal_agent/test_telegram_controller.py` | PASS, exit 0 |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync basedpyright nanobot/personal_agent/task_store.py nanobot/personal_agent/telegram_controller.py` | PASS; 0 errors, warnings, or notes; exit 0 |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync python -m scripts.check_import_boundaries` | PASS; 13 rules across 218 files, exit 0 |
| `git diff --check` | PASS, exit 0 |

## Limitations and next step

This checkpoint does not include task scheduling, background progress, retries, worker leases/heartbeats, cancellation of an already-running task execution, an outbound notification outbox, attachments, or OpenCode integration. There is no claim that a queued task is being worked on. The legacy `/tasks` execution list and `/task <execution_id>` detail moved to `/executions` and `/execution <execution_id>`; help and setup documentation state this migration.

Phase 03.2's separate live unauthorized-account acceptance is still NOT RUN. Phase 05 was not started. Before enabling execution in a later phase, add worker dispatch ownership, restart/reconciliation rules, and cancellation behavior for an associated active execution; do not interpret this queued-task implementation as autonomous execution readiness.
