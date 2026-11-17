# Phase 03.2 — Telegram task controls

Date: 2026-11-16

## Result

Implementation and local verification PASS. Live owner-account Telegram workflows were exercised with the replacement bot token, including task listing and details before and after a controller restart, cancellation of a running guest process, and the stopped-worker case. Phase 03.2 remains PARTIALLY VERIFIED because the operator had no second Telegram account available, so a direct live unauthorized-account test was NOT RUN. Deterministic local tests for unauthorized users and group chats pass. Autonomous task submission and OpenCode tools remain disabled.

## Changes

- Added `SqliteExecutionStore.list_latest(limit)` for bounded newest-first record queries; `/tasks` and `/task` use the existing host execution store, not a second task database.
- Added `/tasks`, `/task <execution_id>`, and `/cancel <execution_id>`. `/task` labels saved host state separately from fresh reconciliation and avoids guest requests for terminal records. `/cancel` validates the ID, confirms an existing record and nonterminal state, persists cancellation intent, then calls existing `WorkerInterface.terminate` through the pinned-key SSH backend.
- The SSH adapter checks that the fixed VM is already running and never starts it. It obtains the guest IPv4 address and Ed25519 public key from Tart's trusted guest agent, creates a temporary mode-0600 host-key pin file, uses strict SSH verification, and removes the pin file afterward. Tart child processes receive a minimal environment so Telegram credentials are not inherited.
- Telegram output is plain text, control characters are removed, each output preview is capped at 700 characters, and complete replies stay below Telegram's message limit. Logs omit IDs, output, message text, API URLs, and credential values.
- Update-store setup refuses shared or non-owner directories instead of changing permissions on an arbitrary parent. A failed attempt to use shared `/private/tmp` directly did not change its existing permissions; the live run used a private temporary subdirectory.

## Files changed

- `nanobot/worker/execution_store.py`
- `nanobot/personal_agent/telegram_controller.py`
- `tests/personal_agent/test_worker_execution_store.py`
- `tests/personal_agent/test_telegram_controller.py`
- `docs/features.md`
- `docs/status.md`
- `docs/telegram-setup.md`
- `reports/phase-03-02.md`

## Live Telegram and worker verification

| Check | Result |
|---|---|
| Replacement bot configuration loaded without displaying values | PASS |
| Read-only `getWebhookInfo` | PASS; no webhook configured and zero pending updates; no webhook mutation was needed |
| Single long-poll controller / `getUpdates` | PASS; polling started and processed owner updates without HTTP 409; pending updates were not discarded |
| HTTPX/HTTP Core URL logging | PASS after token rotation; effective log threshold was WARNING or higher; captured startup/command logs contained no token or Telegram API URL |
| Owner `/ping`, `/worker`, `/status`, `/tasks` | PASS; authorized updates were handled; `/worker` and `/status` did not start or change the VM |
| Owner `/task` completed, active, invalid/nonexistent IDs | PASS; handler accepted the requests; invalid IDs did not create execution records |
| Owner `/cancel` active execution | PASS; execution `2c580e442a0a485282dba63735a9b05c` transitioned from running to `cancelled`; cancellation timestamp persisted, with no error |
| Owner `/cancel` already-completed and invalid IDs | PASS; completed execution `1f40a0236fe04579949d8292a959ac88` remained `succeeded` with exit 0; invalid ID did not create or alter a record |
| Owner `/cancel` when worker stopped | PASS; execution `24107f09d511488c9556dd4421ee1d53` remained stored as `running`, cancellation intent persisted, and `worker_unavailable` was recorded; VM remained stopped |
| Controller restart and history recovery | PASS; restarted against the same host SQLite databases; `/tasks` and `/task` for the completed and stopped-worker records were handled; database retained all 3 execution records and 20 processed Telegram updates |
| Unauthorized account over real Telegram | NOT RUN; operator did not have access to a second account |
| Worker final state | PASS; graceful shutdown completed before stopped-worker checks; no status command auto-started it |

The worker execution IDs and current stored states are: `1f40a0236fe04579949d8292a959ac88` (`succeeded`, exit 0), `2c580e442a0a485282dba63735a9b05c` (`cancelled`, cancellation timestamp recorded), and `24107f09d511488c9556dd4421ee1d53` (`running` in the host store, cancellation requested, latest structured error `worker_unavailable`). The last state is intentionally not reported as cancelled or successful because the stopped guest could not confirm termination. After controller restart, querying that record showed stored and freshly reconciled status separately; no command was resubmitted.

One preliminary live startup had emitted an HTTPX INFO request log containing the then-current bot API URL. The operator revoked that token through BotFather and configured a replacement before the live command run. Subsequent polling and command logs were checked and contained no URL or token. The replacement credential was never displayed or written into the repository or execution records.

An initial helper check found the guest execution helper empty; the reviewed install script restored it before the live commands. The original uncertain operation `276e338a9c8346f4964595766157ab92` was reconciled to `unknown/history_missing` without resubmission. The normal worker shutdown script had a stale pinned host-key entry; shutdown was completed with strict verification against the current Ed25519 key obtained from Tart's trusted guest-agent channel. Host-key verification was not disabled.

## Local verification

| Check | Result |
|---|---|
| `/tasks` empty state, latest 10 records, status categories | PASS, deterministic tests |
| `/task` terminal/active, bounded previews, invalid/missing IDs | PASS, deterministic tests |
| `/cancel` active, terminal, missing/invalid ID, unavailable persistence | PASS, deterministic tests |
| Unauthorized private user and group chat cannot retrieve/cancel tasks | PASS, deterministic tests; live unauthorized-account check remains NOT RUN |
| Controller recreation sees existing records in same SQLite database | PASS, deterministic test and live restart |
| Duplicate Telegram update handling | PASS, local persistence tests; no separate live duplicate-delivery injection |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync pytest -q tests/personal_agent/test_telegram_controller.py tests/personal_agent/test_worker_execution_store.py tests/personal_agent/test_worker_interface.py tests/personal_agent/test_worker_ssh_integration.py` | PASS; 33 passed, 7 skipped, exit 0; skipped tests require separate live SSH test configuration |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync ruff check nanobot/personal_agent/telegram_controller.py nanobot/worker/execution_store.py tests/personal_agent/test_telegram_controller.py tests/personal_agent/test_worker_execution_store.py` | PASS, exit 0 |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync basedpyright nanobot/personal_agent/telegram_controller.py nanobot/worker/execution_store.py` | PASS; 0 errors, warnings, or notes; exit 0 |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync python -m scripts.check_import_boundaries` | PASS; 13 rules across 218 files; exit 0 |
| `git diff --check` | PASS, exit 0 |

## Remaining limits

Direct Telegram rejection of a second account remains unverified live, although numeric-owner/private-chat enforcement is covered by local tests. The stopped-worker cancellation record remains uncertain (`running` plus cancellation intent and `worker_unavailable`) until the guest can be queried; it was not executed locally or automatically retried. Live duplicate update replay was not injected. These gaps keep full Phase 03.2 live acceptance PARTIALLY VERIFIED. The controller was stopped after the checks, the worker was left stopped, and the temporary test databases were used only for this run. No Phase 04 work or autonomous execution was started.
