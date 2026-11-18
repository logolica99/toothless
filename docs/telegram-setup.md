# Toothless Telegram setup

The Telegram controller uses outbound-only long polling and accepts messages only from one configured numeric Telegram user ID in a private chat. It has no webhook listener and does not forward arbitrary messages to a model or command runner.

## Create and configure a bot

1. In Telegram, open the verified `@BotFather` account, create a bot with `/newbot`, and keep the returned token private. Do not paste the token into this repository, a chat, an issue, or a terminal command that is saved in shell history.
2. Obtain your numeric Telegram user ID from a trusted source. Do not use a username as the access control value; usernames can change.
3. Create a private configuration file outside the repository:

   ```bash
   mkdir -p "$HOME/.config/toothless"
   chmod 700 "$HOME/.config/toothless"
   touch "$HOME/.config/toothless/telegram.env"
   chmod 600 "$HOME/.config/toothless/telegram.env"
   ${EDITOR:-nano} "$HOME/.config/toothless/telegram.env"
   chmod 600 "$HOME/.config/toothless/telegram.env"
   ```

4. Enter these two lines in that editor, replacing the placeholders locally. Never include quotes or spaces around the values:

   ```text
   TOOTHLESS_TELEGRAM_BOT_TOKEN=your_private_botfather_token
   TOOTHLESS_TELEGRAM_OWNER_ID=your_numeric_telegram_user_id
   ```

The controller refuses a configuration file readable by other users. The default update database is `~/.local/share/toothless/telegram/updates.sqlite3`, separate from the worker execution database, with owner-only directory and file permissions. The bot token is read from the protected environment file and is never written into either database or logs. An alternate env file can be selected with `TOOTHLESS_TELEGRAM_ENV_FILE`.

For an isolated end-to-end test, use a private temporary directory for both databases so the test does not add records to the normal worker history:

```bash
umask 077
TOOTHLESS_TEST_DIR="$(mktemp -d /private/tmp/toothless-telegram-test.XXXXXX)"
export TOOTHLESS_TELEGRAM_DB="$TOOTHLESS_TEST_DIR/telegram.sqlite3"
export TOOTHLESS_EXECUTION_DB="$TOOTHLESS_TEST_DIR/executions.sqlite3"
```

## Run the controller

From the repository root, start it in a dedicated Terminal window:

```bash
uv run --no-sync python -m nanobot.personal_agent.telegram_controller
```

Keep that process running while testing from your phone. Stop it with `Ctrl+C`. It uses Telegram's outbound HTTPS API and `getUpdates` long polling; no inbound server, webhook, port forwarding, or SSH access is configured.

Commands include `/start`, `/ping`, `/help`, `/worker`, `/status`, `/new <request>`, `/run <task_id>`, `/tasks`, `/task <task_id>`, `/update <task_id> <instructions>`, `/pause <task_id>`, `/resume <task_id>`, and `/cancel <task_id|execution_id>`. Ordinary text from the authorized private owner also creates a queued task. Task records are stored in `tasks.sqlite3` next to the update database, unless `TOOTHLESS_TASK_DB` selects another absolute path. Each accepted task records its Telegram update ID and is created before the update offset advances, making redelivery idempotent. A task stays queued until the owner explicitly sends `/run <task_id>`; queued tasks are never dispatched automatically. `/run` does not start a stopped VM.

Worker execution controls retain explicit migration aliases: `/executions` lists existing host-owned worker execution records and `/execution <execution_id>` shows/reconciles a worker execution. `/cancel <execution_id>` still cancels an existing worker execution; `/cancel <task_id>` cancels a queued task before execution. Task IDs use the `task-` prefix. Telegram operations are not registered as OpenCode tools.

Task detail and cancellation use the existing SSH backend. When needed, the controller checks that the fixed worker is already running, obtains its current IPv4 address and Ed25519 public host key from the trusted Tart guest-agent channel, writes a temporary mode-0600 known-hosts file, and deletes that file after the SSH request. SSH remains key-only with `~/.ssh/id_ed25519`, strict host-key verification, agent forwarding disabled, and the fixed guest helper protocol. A stopped worker is reported unavailable; there is no Tart start fallback. `/tasks` uses the same host-owned SQLite execution store as the worker interface and returns only the 10 most recent records.

Task details show the original request, additional instructions, status, progress, errors, timestamps, associated execution IDs, and a bounded result preview when available. Worker execution stdout/stderr previews are bounded and control characters are removed. Outgoing messages use Telegram HTML formatting: common Markdown bold, italic, strikethrough, headings, bullets, inline code, and fenced code are rendered as rich text after escaping plain content. Stored task results retain their original text. `/execution` labels the saved host status separately from fresh worker reconciliation; terminal records are not polled again. Task `/cancel` persists cancellation intent before contacting the worker and targets only the execution ID associated with that stored task. Worker `/cancel` accepts only an execution ID that already exists in the host store. Neither command starts a stopped VM. Unknown execution history remains unknown and is never resubmitted.

## Troubleshooting

- Missing or invalid token/owner settings stop startup with a credential-free error. If Telegram rejects the token, create a new one in BotFather and update the protected file; do not share the old or new token.
- Network failures and Telegram rate limits are logged without request URLs, message text, or API response bodies, then polling retries after a short delay.
- An HTTP 409 from `getUpdates` usually means this bot already has a webhook or another active poller. Stop the other poller or remove the webhook in the bot's owner-managed setup; this controller does not automatically delete or replace it.
- If a token is exposed in a log or other output, revoke it through `@BotFather`, replace it in the protected file, and restart only after verifying the new file remains mode `0600`.
- Live acceptance needs the operator to start the controller and send the commands from the authorized private Telegram account. The test suite does not fake these live results.

## Phase 03.2 live verification state

Owner-account task commands, worker-unavailable behavior, and controller restart persistence were exercised with the real bot and worker. The replacement token was used after the earlier exposed token was revoked; subsequent logs contained no Telegram API URL or credential. Direct rejection of a second Telegram account remains unverified because a second account was unavailable. Local tests cover unauthorized users and group chats. See `reports/phase-03-02.md` for exact execution IDs and results.

Phase 04's queued-task commands were exercised through the real bot. Live owner task creation, instruction update, pause/resume, cancellation, worker execution command aliases, and restart recovery passed. Duplicate-update replay and unauthorized task creation/modification were verified locally, not by injecting duplicate Bot API updates or using a second live account. See `reports/phase-04.md`.

## Phase 05 OpenCode tasks

`/run <task_id>` is an explicit owner approval to run that one queued task through the pinned OpenCode CLI inside Ubuntu. The controller persists the task claim and worker execution ID before SSH. The task reports `dispatching` until the worker acknowledges it, then polls the same ID to terminal and reconciles it after restart without resubmission. Phase 05.1's first owner-approved CPU/GPU task and result recovery passed live. Phase 05.2 allows OpenCode `read`, `edit/write`, `glob`, `grep`, and `shell` tools only after `/run`; those tools run as the non-sudo Ubuntu account `toothless-agent` in its guest workspace. Apt/dpkg, sudo, plugins, MCP, subagents, and unattended task dispatch remain disabled. Do not send any model or Telegram credential in chat.

The Phase 05.2 guest account requires its own OpenCode Go authentication; the Phase 05.1 admin-account login is not copied. The helper explicitly selects the `opencode-go` provider and key method and starts from the agent workspace. Earlier attempts inherited `/home/admin`, which the guest account cannot access; a guest log recorded `EACCES` while OpenCode watched that directory. The corrected helper succeeded, and credential presence was confirmed without printing its value. A shell command invoked by the model runs under that same guest identity and can read its guest-local OpenCode credential; host credentials and files are not mounted into Ubuntu. General public egress also remains available, so guest data exfiltration is possible. The current Ubuntu image lacks pip and `python3-venv`; system package installation requires separate operator approval and is not exposed to the model in Phase 05.2.

The generated guest files can be retrieved using the existing authenticated worker file API, for example with paths under `opencode-task-workspace/`. The Telegram controller does not send documents as attachments; results are text-only. Phase 05.2's scoped workspace tool acceptance is verified. See `reports/phase-05-02.md` for evidence and remaining limitations.
