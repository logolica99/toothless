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
   TOOTHLESS_REMOTE_ORIGIN=https://your-mac-mini.your-tailnet.ts.net
   ```

`TOOTHLESS_REMOTE_ORIGIN` is required for remote Memory Explorer and Desktop Viewer links. Set it to the HTTPS Tailscale Serve origin only, with no path or trailing slash; the controller accepts only HTTPS `*.ts.net` hosts. Optionally add `TOOTHLESS_TIME_ZONE=America/Los_Angeles` using a valid IANA time-zone name. If omitted, the controller uses the Mac's configured IANA zone when available. It displays the resolved time and zone in each schedule proposal; do not approve if the displayed local time is not what you intended.

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

Phase 06 schedule support adds `/schedules`, `/schedule show <id>`, `/schedule latest <id>`, `/schedule approve <id>`, `/schedule pause <id>`, `/schedule resume <id>`, and `/schedule cancel <id>`. Natural scheduling currently accepts one-time reminders such as “Remind me tomorrow at 9 AM to review my results” or “Remind me in 2 minutes to review my results,” and daily research such as “Every morning at 8, research the latest AI agent developments and send me a summary.” Toothless shows the exact action, recurrence, local time, zone, profile, and limits before asking for the separate `/schedule approve <id>` activation. Proposals remain inactive unless explicitly approved by the owner. Reminder delivery uses no model. Daily research is limited to the existing browser-only profile and public GET/HEAD requests; it cannot use workspace shell/file tools or perform logins, submissions, purchases, publishing, or account changes. Startup-discovered missed occurrences are recorded and notified, never replayed. Failed daily research pauses the schedule. The database is `schedules.sqlite3` beside TaskStore and contains owner/chat IDs, approval snapshot/digest, occurrence/task/execution references, result/error, and Telegram delivery status. Pause or cancel stops future occurrences; a currently dispatched occurrence is allowed to finish under its existing approved scope.

Worker execution controls retain explicit migration aliases: `/executions` lists existing host-owned worker execution records and `/execution <execution_id>` shows/reconciles a worker execution. `/cancel <execution_id>` still cancels an existing worker execution; `/cancel <task_id>` cancels a queued task before execution. Task IDs use the `task-` prefix. Telegram operations are not registered as OpenCode tools.

Task detail and cancellation use the existing SSH backend. When needed, the controller checks that the fixed worker is already running, obtains its current IPv4 address and Ed25519 public host key from the trusted Tart guest-agent channel, writes a temporary mode-0600 known-hosts file, and deletes that file after the SSH request. SSH remains key-only with `~/.ssh/id_ed25519`, strict host-key verification, agent forwarding disabled, and the fixed guest helper protocol. A stopped worker is reported unavailable; there is no Tart start fallback. `/tasks` uses the same host-owned SQLite execution store as the worker interface and returns only the 10 most recent records.

Task details show the original request, additional instructions, status, progress, errors, timestamps, associated execution IDs, and a bounded result preview when available. Worker execution stdout/stderr previews are bounded and control characters are removed. Outgoing messages use Telegram HTML formatting: common Markdown bold, italic, strikethrough, headings, bullets, inline code, and fenced code are rendered as rich text after escaping plain content. Stored task results retain their original text. `/execution` labels the saved host status separately from fresh worker reconciliation; terminal records are not polled again. Task `/cancel` persists cancellation intent before contacting the worker and targets only the execution ID associated with that stored task. Worker `/cancel` accepts only an execution ID that already exists in the host store. Neither command starts a stopped VM. Unknown execution history remains unknown and is never resubmitted.

## Phase 08.0 experimental blog provisioning

The local implementation adds owner-only `/blog setup`, `/blog status`, and `/blog cancel`. Setup state is stored in `blog-setup.sqlite3` beside the task database; provider credentials are stored only in the Mac login Keychain. Use the inline connection controls to issue a one-use code, then enter that code and the provider credential only in the private HTTPS setup page. Never paste credentials into Telegram or a model prompt. The GitHub flow uses separate repository-creation and target-only publishing credentials; create a GitHub fine-grained token for account `logolica99` with repository creation permission, then a second token scoped only to `logolica99/tech-pulse-toothless` with Contents read/write. Connect a Vercel token for personal account `logolica99` with the shortest practical expiry. If Vercel requires GitHub App authorization, complete that action directly with Vercel before retrying the setup review.

`Review private repository setup` only prepares a proposal. Inspect its exact source baseline and private target, then use the Telegram approval button to create that repository. Copy/deployment requires a separate proposal and approval bound to that repository ID and the same source baseline. Review your current Vercel billing plan before approving because usage may be billable; this workflow does not upgrade a plan, purchase a domain, or modify the production repository/project. `/blog cancel` cancels pending proposals; it does not delete provider resources or revoke provider tokens. Remove a local credential from the setup page and revoke/rotate it separately at GitHub or Vercel. The Phase 08.0 code is not deployed until the report says otherwise.

## Phase 05.6 project memory

The controller stores project descriptions, explicit facts/preferences/decisions, and task-result references in `~/.local/share/toothless/telegram/memory.sqlite3` by default, beside the task database. The parent directory must be owner-only, and the database is created mode `0600`. Use `/project <name> | <description>`, `/remember [project |] <fact>`, or the natural phrase “Remember that project <name> uses <methods>.” Use `preference:` or `decision:` with `/remember`, or say “Remember that I prefer …” / “Remember we decided …”. Query with `/memory <query>` or “What do you remember about project <name>?”; search replies include memory IDs, timestamps, provenance, and task/file references. Correct with `/memory-update <memory_id> | <replacement>` and delete with `/forget <memory_id>`. “Forget that preference/fact/decision” requires replying to one matching memory result. Save a successful task reference under a project by replying to its result with “Save this result under my <project> project.”

Memory updates do not start tasks. To attach project context, submit a new task that explicitly says “Use my <project> project context for this new task: <request>” or reply to an unambiguous project-memory result with “Use the same project context for this new task: <request>”. Review the queued task with `/task` and explicitly approve it using `/run` or the exact reply-bound approval. Context is bounded and marked untrusted; uploaded document text, webpage content, and model output never become trusted memory automatically. Memory writes are deduplicated by Telegram update ID.

The Phase 05.6.1 knowledge graph is a bounded index in the same host SQLite memory database. Owner queries include “Which files belong to my <project> project?”, “What tasks used <filename>?”, “Find the results produced by my last CSV analysis.”, and “Which memory replaced the earlier project fact?” Replies contain task, file, artifact, memory, and provenance references; they do not expose file contents or authorize task execution. Its lexical candidate set is limited to 500 recent graph entities and 500 active memory records, with at most eight seeds and bounded graph traversal. Project/file and filename/task lookups passed live after controller restart; see `reports/phase-05-06-1.md` for limits and verification status.

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

Before a workspace task is dispatched, the fixed guest runner checks OpenCode's SQLite credential metadata for an active `opencode-go` credential without selecting or printing credential values. If it is absent, Toothless returns an actionable `auth_required` result; authenticate only inside the Ubuntu guest with `bash ops/worker/opencode-auth-login.sh`. Protected worker startup re-runs the existing guest DNS helper and verifies the dynamic Softnet relay on port 55353 plus a DNS lookup. Startup fails closed if that state cannot be established. A VM reset removes guest-local authentication; install OpenCode and its reviewed runner, then authenticate again. Never copy credentials from the Mac or preserve them by resealing the baseline.

Phase 05.4 adds private file uploads and output delivery for explicitly approved workspace tasks. Send a supported document or photo in the owner's private chat with a task request as its caption; Toothless stores it as queued and does not run until `/run <task_id>`. Inputs are limited to PDF, TXT, CSV, JSON, PNG/JPG, or small ZIP files, each at most 512 KiB. ZIP extraction is bounded and rejects traversal, links, encrypted/nested archives, excessive expansion, and unsupported members. Generated files are sent only from the task-specific `opencode-task-workspace/exports/<task_id>/` guest directory; the host records each Telegram message ID and never intentionally retries an uncertain file send. Attached tasks use the workspace profile. Tasks with attachments cannot select the browser-only profile, whose tool permissions remain separate. See `docs/file-security.md` and `reports/phase-05-04.md` for limits and live verification status.

## Telegram inline controls

Task, research, memory, and schedule responses may include inline buttons. Buttons are bound to the owner, private chat, originating message, and a one-use server-side callback record. Approval buttons consume the same exact-action proposal used by reply-bound `Yes` and `/run`; they do not grant broader permissions. Stale or repeated buttons are acknowledged without repeating an action. Keep `/run`, `/task`, `/cancel`, and `/schedule ...` text commands as fallbacks. Research source controls expose only bounded validated public HTTP(S) URLs and titles; they do not expose raw page contents.

## Local Memory Explorer

From the repository root, run `uv run --no-sync python -m nanobot.personal_agent.memory_explorer` in a dedicated Terminal. The process binds to `127.0.0.1:8768` by default and prints a temporary random bearer token once after startup. Open the displayed localhost address in a browser and enter that token; do not paste it into Telegram or save it in a shared file. Stop the process with `Ctrl+C`. The dashboard opens the existing TaskStore and MemoryStore read-only and does not start the VM or dispatch tasks. To choose another port, use `--port <1024-65535>` and open the corresponding localhost address. Dashboard access is limited to the local host; do not proxy or forward this service onto a LAN or public network.

## Phase 07.2 visible browser tasks

Visible browsing is an opt-in mode of the existing browser-only profile. To request it, say that you want to watch the browser in the Ubuntu desktop and describe a bounded public read-only task. Review the queued task card/details to confirm the visible-browser mode and exact action, then approve only that task with `/run <task_id>` or its exact reply-bound approval. Do not use the workspace shell profile for the same task. The broker exposes navigation, rendered-text extraction, link clicking, and session close only; the existing public-network destination restrictions and execution limits still apply. The `/desktop` viewer remains image-only: it provides no keyboard, pointer, clipboard, upload, or arbitrary command input. Browser installation and broker deployment do not run model inference by themselves. The bounded Example Domain visible-browser workflow is verified; active visible-task cancellation and controller-restart recovery remain untested. See `reports/phase-07-2.md`.

## Start Telegram automatically at owner login

The optional per-user LaunchAgent starts the controller in the logged-in macOS Aqua session and restarts unexpected exits with a 30-second launchd throttle. It uses `.venv/bin/python` from this repository and reads the existing protected `~/.config/toothless/telegram.env`; the token is not placed in the plist or process arguments. Install it with `ops/macos/install-controller-launch-agent.sh` from the repository root. Inspect it with `launchctl print gui/$(id -u)/com.toothless.telegram-controller`; logs are kept under `~/.local/share/toothless/telegram/logs/` with owner-only permissions. Remove it with `ops/macos/uninstall-controller-launch-agent.sh`; this retains the databases and logs. A controller singleton lock prevents a second poller. If launchd collides with a manually started controller, it retries after the throttle until the lock becomes available. A real reboot/login test must be performed separately; installing or restarting the service is not evidence of reboot recovery.

The `/status` card has Refresh, Tasks, Schedules, and Diagnostics controls. Diagnostics reports only component readiness. A running Tart VM is not considered protected unless the current `ops/worker/start.sh` Softnet/DNS launcher heartbeat is fresh and its policy was verified. If readiness reports DNS or guest OpenCode authentication unavailable, do not reset the VM or bypass the launcher. Reapply the documented worker startup and, if authentication is absent, run `bash ops/worker/opencode-auth-login.sh` and enter the credential only in the Ubuntu guest prompt. Launchd does not start or reset the worker. Direct Tart launches remain unprotected.

## Guest desktop viewing (Phase 07.1, local read-only viewer verified)

After the guest desktop dependencies and fixed helper are installed, send `/desktop` in the owner-only private chat. **Screenshot** and **Refresh** request a new PNG through the existing protected SSH worker interface and send it as a Telegram photo. **Open Desktop** starts only the fixed dedicated Ubuntu desktop service after current full worker readiness; it does not start the VM. On the Mac Mini, run `ops/macos/open-desktop-viewer.sh` for the local URL, or use the private Tailscale Serve URL described below and its separately issued one-time code. Both viewers display images only. **Close Viewer** revokes its code/session and requests the fixed guest desktop service stop. A controller restart invalidates prior desktop grants. The read-only viewer passed screenshot, automatic refresh, expiry, restart invalidation, and exact callback replay checks; see `reports/phase-07-1.md`. `toothless-agent` receives no sudo or apt privileges.

## Private remote access — Phase 07.1.1 (verified for owner devices)

The approved Tailscale Serve routes use the private origin configured as `TOOTHLESS_REMOTE_ORIGIN` in `~/.config/toothless/telegram.env`, with `/desktop/` and `/memory/` paths; both proxy to localhost-only applications and are tailnet-only. Set the value to the HTTPS Tailscale Serve origin only, for example `https://<mac-mini>.<tailnet>.ts.net`, with no path or trailing slash. The controller validates that it is an HTTPS `*.ts.net` hostname. The owner chose to keep the broad tailnet rule for all devices/users. In the owner-only private Telegram chat, send `/remote` to receive a one-use Memory Explorer sign-in code and URL. For desktop, send `/desktop` and use its one-use Open Desktop callback; open the returned URL and enter its one-time code locally. Never paste these codes into chats or logs. Memory Explorer is read-only; desktop is image-only with no input. Use `/remote revoke` to invalidate remote codes/sessions and close the viewer. Controller restart also invalidates prior codes/sessions. Owner-confirmed acceptance passed on MacBook Air and iPhone over mobile data with Tailscale connected. A live negative check using another tailnet identity remains unrun. See [`remote-access.md`](remote-access.md). Do not enable Funnel, port forwarding, public proxies, or direct LAN access to ports 8768/8769.
