# Phase 06 — Scheduling and Proactive Tasks

Date: 2026-11-18 to 2026-11-19 (America/Los_Angeles)

Status: VERIFIED for the implemented one-time reminder and daily read-only research profile. Owner-approved reminder delivery, recurring browser research, Telegram delivery, schedule persistence across a controller restart, live pause/resume/cancel, and occurrence idempotence passed. The temporary daily research schedule was cancelled after its first occurrence. Live duplicate-wakeup concurrency and injected provider/DNS failure checks were not performed; deterministic tests cover those states.

## Implementation

`ScheduleStore` keeps owner/chat-scoped proposals, immutable approved specifications, next-run state, occurrence history, and Telegram delivery status in a separate owner-private `schedules.sqlite3`. Telegram update IDs deduplicate schedule creation. Approval binds the owner, chat, action, local and resolved time, time zone, recurrence, profile, authorization policy, DST policy, and fixed resource limits. A pending proposal cannot execute.

The first supported actions are one-time reminders and daily public research. Reminders are composed and delivered by the host without inference. Daily research uses the existing `opencode-go/glm-5.3` browser-only profile, public HTTP(S) GET/HEAD browsing, bounded output and runtime, source URLs, and owner Telegram delivery. Shell/file access, login, forms, publishing, purchases, and account changes are outside the approved scope.

Each occurrence has a deterministic ID unique to its schedule and scheduled instant. Research task and execution intent are persisted before worker dispatch and linked back to the approving schedule. SQLite transactions make repeated scheduler ticks idempotent. One worker occurrence is allowed at a time. Missed occurrences are recorded and never replayed; failures pause recurring research rather than retrying it. Uncertain Telegram delivery is recorded and not automatically resent. The scheduler never starts or resets the VM.

Daily schedules use the configured IANA time zone and local calendar dates. Ambiguous daylight-saving times choose the first fold; nonexistent times shift forward to the first valid local minute. On controller startup, already-due occurrences are marked missed. The scheduler runs only while the Telegram controller is running; it is not installed as a launchd service.

## Live Telegram acceptance

| Check | Result | Evidence |
| --- | --- | --- |
| A. Create and separately approve a one-time reminder | PASS | Schedule `schedule-507aaab53435ad2fe24c8b97`; exact action: “review my results”; recurrence `once`; America/Los_Angeles. Owner activated it with `/schedule approve`. |
| B. Actual scheduled reminder delivery | PASS | Occurrence `occ-9c8d095bac7034fec50df5a6` arrived with “Reminder: review my results”; `/schedule latest` showed `succeeded` and Telegram delivery `sent`. |
| C. Create and approve recurring browser research | PASS | Schedule `schedule-0dd0b2e3d6d0d7be76828250`; daily at 6:24 PM America/Los_Angeles; browser-only public HTTPS GET/HEAD and Telegram delivery. Approved snapshot hash starts `fb683641245483ad`. |
| D. Real scheduled browser research and Telegram result | PASS | Occurrence `occ-b885480782a7925347fe3bc6`, task `task-5ed7304066aaddeb31f259006ca5ebea`, execution `opencode-8ab7bfcd9215423eafac67536052e7d5`. Host records show task and occurrence `succeeded`, notification `sent`; sanitized OpenCode diagnostics record 23 events, zero retries, including `toothless_browser_navigate`, `toothless_browser_extract_text`, and `toothless_browser_close_session`. The returned result cited `https://www.python.org/downloads/` and `https://www.python.org/downloads/release/python-3152/` and reported Python 3.15.2. Browser tool arguments and page text are not retained in host diagnostics, so those URLs are the returned answer, not an independently archived page capture. |
| E. Controller restart recovery | PASS | After activation and before the first occurrence, the controller alone was restarted. Owner `/schedules` and `/schedule show` showed the same daily schedule active with the same immutable approval snapshot. The VM was not reset or restarted. |
| F. Duplicate scheduler wakeup prevention | PASS locally; live duplicate-concurrency test NOT RUN | Deterministic SQLite uniqueness/claim tests passed; the one live occurrence has a single schedule time, occurrence ID, task ID, and execution ID. No second poller or forced simultaneous scheduler wakeup was created. |
| G. Pause/resume | PASS live and locally | Owner approved one-time schedule `schedule-f042942e67b38f06874ab80c` (“verify schedule controls” at 6:47 PM America/Los_Angeles), paused it, resumed it, then cancelled it before due time. SQLite confirms final state `cancelled`, zero occurrences, and no linked worker task. |
| H. Unapproved/unauthorized schedule execution | PASS locally; live second-account attempt NOT RUN | Tests verify pending schedules stay inactive and approval is owner/chat bound. No second Telegram account was used. |
| I. DNS/auth failure blocks execution without unattended retries | PASS in deterministic tests; live fault injection NOT RUN | Synthetic `auth_required` occurrence was recorded once and paused the daily schedule. Existing protected worker tests verify DNS startup fails closed and guest auth preflight does not reveal credentials. |
| J. Existing task and memory records unchanged | PASS by storage boundary review; direct before/after content comparison NOT RUN | Schedules use a separate database; implementation does not write TaskStore or MemoryStore except to create the linked scheduled task and its execution lifecycle. |

The research response included an inaccurate model-generated caveat saying it could not deliver to Telegram. The host’s independently persisted notification status is `sent`, and the owner received the result through the bot. The answer also included intermediate research narration and more detail than the approved concise summary requested. This is a response-quality limitation; it did not alter the schedule scope or delivery state.

After the first research occurrence, the owner cancelled `schedule-0dd0b2e3d6d0d7be76828250`. The controller acknowledged: “No in-flight occurrence was replayed or cancelled by this state change.” The host row is `cancelled`; the completed occurrence remains `succeeded` and `sent`, and no future daily run is authorized. The separate live pause/resume check was also cancelled before its one-time due time, with no occurrence or worker task created.

## Local verification

| Command | Result |
| --- | --- |
| `UV_CACHE_DIR=/tmp/toothless-phase06-uv-cache uv run --no-sync pytest -q tests/personal_agent` | PASS: 171 passed, 7 skipped (after final tool-evidence display test) |
| `UV_CACHE_DIR=/tmp/toothless-phase06-uv-cache uv run --no-sync ruff check nanobot/personal_agent tests/personal_agent` | PASS |
| Targeted BasedPyright for `schedule_parser.py`, `schedule_store.py`, `task_store.py`, and `telegram_controller.py` | PASS: 0 errors |
| `UV_CACHE_DIR=/tmp/toothless-phase06-uv-cache uv run --no-sync python -m scripts.check_import_boundaries` | PASS: 13 rules, 218 files |
| `ops/worker/test-static.sh` | PASS |
| `git diff --check` | PASS |
| `UV_CACHE_DIR=/tmp/toothless-phase06-uv-cache uv run --no-sync basedpyright` | BLOCKED by missing optional channel/provider development dependencies and resulting pre-existing diagnostics in unrelated modules; no touched Phase 06 module errors in the targeted run |

The `/schedule latest` response now displays only bounded, sanitized tool names from the host-persisted `execution_diagnostics` event. It does not expose tool arguments, prompt text, page content, credentials, or raw provider events. A local regression test confirms malformed tool names and unrelated JSON fields are excluded.

## Security and remaining limitations

The scheduler runs only as long as its controller process is available. Downtime at a due time causes a missed occurrence, not catch-up execution. Public internet egress and DNS-name exfiltration remain possible under the existing guest profile; the browser boundary is not general egress isolation. A Telegram send interrupted after its durable send intent remains uncertain and is not retried automatically. The live research result’s prose was not checked against an archived page capture, even though the host recorded actual browser tool names and event types.

No VM baseline, Tart/Softnet policy, host network setting, credential, or package was changed for Phase 06. Phase 07 publishing, arbitrary workspace automation, and broader unattended computer control remain out of scope.
