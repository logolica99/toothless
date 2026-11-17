# Phase 03.1 — Telegram integration

Date: 2026-11-16

## Outcome

The limited Telegram controller is implemented with deterministic backend tests passing. Direct Telegram acceptance is NOT RUN because no protected Telegram configuration file or Telegram credential environment variables are present in this workspace. This checkpoint is not fully verified until the operator configures the bot and completes the phone-based acceptance steps.

## Implementation

- Added `nanobot/personal_agent/telegram_controller.py`, a separate outbound HTTPS long-poll process using the existing `httpx` dependency. The existing Nanobot Telegram channel was not reused because it routes inbound messages into the general agent command/session path; the Toothless boundary requires this checkpoint to expose only fixed responses and a read-only Tart status query.
- Enforced one numeric user ID and private-chat-only message handling. The poll requests only `message` updates; callbacks are not requested or acted on. Unauthorized users, groups, channels, and non-text updates do not trigger commands.
- Implemented `/start`, `/ping`, `/help`, `/worker`, and `/status`. Arbitrary text receives a notice that autonomous tasks are disabled. No message reaches a shell, worker operation, or OpenCode adapter.
- `/worker` and `/status` use the fixed `tart get toothless-worker` query with the documented Tart storage path. They do not start or mutate the VM. Status-query failure is returned as unavailable or unknown rather than guessed.
- Added host-owned SQLite update ID deduplication with owner-only file permissions, separate from the worker execution database. Accepted update IDs are persisted before handling, and the stored offset survives controller restart.
- Credentials are read from `~/.config/toothless/telegram.env` (or `TOOTHLESS_TELEGRAM_ENV_FILE`) and the file must be mode `0600`. The token is never written to the update database and log messages omit request URLs, response bodies, and exception text. No inbound server or port forwarding is used.
- Bounded Telegram 429 handling honors `retry_after` for up to four requests, capped at 60 seconds per delay. Polling/network errors produce sanitized logs and a short retry delay.

## Files changed

- `nanobot/personal_agent/__init__.py`
- `nanobot/personal_agent/telegram_controller.py`
- `tests/personal_agent/test_telegram_controller.py`
- `docs/telegram-setup.md`
- `docs/features.md`
- `docs/status.md`
- `reports/phase-03-01.md`

## Tests

| Check | Result |
|---|---|
| Limited command replies and unknown text behavior | PASS |
| Exact owner accepted; unauthorized user rejected | PASS |
| Group chat rejected | PASS |
| Callback update ignored | PASS |
| Duplicate update ID suppressed | PASS |
| Process-reopen update offset and deduplication persistence | PASS |
| Mode-0600 configuration and numeric owner validation | PASS |
| HTTP 429 `retry_after` honored with bounded retry | PASS |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync pytest -q tests/personal_agent/test_telegram_controller.py` | PASS, 4 passed, exit 0 |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync ruff check nanobot/personal_agent/telegram_controller.py tests/personal_agent/test_telegram_controller.py` | PASS, exit 0 |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync basedpyright nanobot/personal_agent/telegram_controller.py` | PASS, 0 errors/warnings/notes, exit 0 |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync python -m scripts.check_import_boundaries` | PASS, 13 rules across 218 files, exit 0 |
| `git diff --check` | PASS, exit 0 |
| Direct iPhone `/ping`, `/start`, `/help`, `/worker`, `/status`, unknown command | NOT RUN; operator credentials are not configured |
| Live unauthorized account and repeat-update tests | NOT RUN; require direct Telegram interaction |
| Live worker state result via Telegram | NOT RUN; requires the Telegram controller to be configured and running |

The initial `uv` command using its default cache failed because the session could not write under `~/.cache/uv` (exit 2). Re-running with the temporary `UV_CACHE_DIR` shown above passed. Initial test iterations exposed incorrect test expectations and import ordering, which were corrected; the final run passed.

## Operator acceptance

Follow [`docs/telegram-setup.md`](../docs/telegram-setup.md): create a bot through `@BotFather`, identify the owner's numeric Telegram ID, create the protected env file without placing the token in shell history, start the controller with `uv run --no-sync python -m nanobot.personal_agent.telegram_controller`, and send `/start`, `/ping`, `/help`, `/worker`, `/status`, and an unknown command from the configured private account. Verify unauthorized and group messages receive no operation or response. Do not share the bot token in chat or commit it. Record the actual Telegram responses and worker state before marking Phase 03.1 VERIFIED.

## Remaining issues

- Live Telegram API connectivity and the phone-based acceptance list remain unverified until operator credentials and interactions are supplied.
- A pre-existing Telegram webhook is not deleted automatically; it must be removed for this bot before `getUpdates` polling can work.
- Deduplication prevents reprocessing after restart. If the process crashes after claiming an update but before Telegram accepts the reply, that reply can be lost; the controller does not risk repeating an uncertain send.
- Later Phase 03 features such as attachments, owner approvals, task intake, callbacks, and delivery reconciliation are not implemented. No autonomous execution is enabled.
