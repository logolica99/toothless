# Toothless — Phase 07.2.2: OpenCode Event Size and Browser Output Reliability

Status: Implementation and protected-worker deployment PASS. The fresh owner-approved visible-browser task completed and delivered a result with one execution and no oversized-event failure. Remote visual confirmation for this specific run and exhaustive enumeration of post tags remain unconfirmed; see the acceptance table. The event-size fix passed live execution, but full research acceptance remains PARTIAL.

## Incident and confirmed boundary

Historical task `task-8aa45ba7d7df7982e0a520f172509e46` / execution `opencode-01c5cc7ef4f34dbeac522fbbd26bd261` remains failed and unchanged with exactly one execution. The persisted host record says only `output_limited: OpenCode event exceeded the configured limit`; the previous runner did not persist event type, observed bytes, or tool name. The owner observed browser navigation before failure, but that observation does not establish which event exceeded the limit. The historical event type and size remain UNKNOWN and were not reconstructed or inferred.

The enforced 65,536-byte limit applies to the raw UTF-8 NDJSON event line, including JSON escaping, before JSON decoding. It is not the total task-output limit (1 MiB across captured OpenCode output), the final answer limit (48,000 UTF-8 bytes in the guest runner and 48,000 host result characters), the rendered browser text source cap (32,000 characters), the browser structured tool-result cap (8,192 UTF-8 bytes), the model context window, or Telegram's message limit. No event, task-output, answer, model-context, or Telegram limit was increased.

## Changes

The guest parser now extracts only a bounded JSON event header from at most the first 4,096 bytes of an oversized line. It records sequence, observed byte count (or a lower bound when the total-output guard interrupted the line), fixed 65,536-byte limit, allowlisted event type and tool name when safely recognizable, the host-provided execution ID, and the fixed `event_size_limit` category. It never parses or saves the oversized event body. When the independent 1 MiB total-output guard stops capture, diagnostics mark an observed lower bound rather than claiming an exact event size.

The host accepts only the explicit diagnostic whitelist, binds the diagnostic execution ID to its own expected execution ID, persists the sanitized record in the existing TaskStore execution-diagnostics event, and displays a concise size/type/tool summary in owner-authorized `/task` details. Prompts, provider messages, event bodies, page text, credentials, cookies, tokens, and browser profiles are excluded. Generic Telegram failure messages remain concise.

Rendered page extraction now uses a fixed 8,192-byte production setting, validated to remain between 4,096 and 16,384 bytes. It preserves title and final URL, clips text on UTF-8 character boundaries, caps source text inspected to 32,000 characters, and returns `truncated`, `returned_characters`, `returned_bytes`, and `limit_bytes`. It also checks the serialized structured result against the 8,192-byte bound, accounting for Unicode and JSON escaping. The visible broker continues to return the same four approved operations and applies its existing message/result bounds; no permissions or network policy changed.

## Deployment and preflight

The stopped VM was started only with `ops/worker/start.sh`. The protected launcher reported Softnet isolation acknowledged and the approved DNS relay re-applied and verified. Full readiness then reported Tart running, Softnet policy verified, relay and guest DNS ready, OpenCode Go authentication metadata ready, execution helper ready, and both workspace and browser profiles ready. No VM reset, baseline reseal, Softnet policy edit, or host permission change occurred.

Before deployment, read-only host-store checks found zero dispatching/running/cancelling tasks, zero running/unknown/uncertain worker executions, zero active schedules, and zero active occurrences. The original historical task remained failed with one execution. The existing protected installer repeated task/execution/schedule/readiness checks, then deployed through the pinned-key SSH path. Installed SHA-256 values matched local reviewed sources: runner `e9a4f63bab67ce9cb60faa2c72309df0830cbba52d8e964ab778626137c907fe`, browser helper `e6e184fdc3b8aec8b70fd9ce37368b18186ddc5c4d64d3701fc5f0c0d58a0477`, and visible broker `3e39c5f8d7828095b8342ef078159c9ca12533d8caff7f7f3030775b0b0ed3f7`. The desktop service was active after installation. The host controller LaunchAgent was restarted; `launchctl print` showed one active controller instance in `running` state.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| Historical failed task not replayed | PASS | `task-8aa45ba7d7df7982e0a520f172509e46` remains failed with one original execution; no inference was made for this task. |
| Event size definition | PASS | Parser checks raw NDJSON line bytes before JSON decoding; the 65,536-byte setting is unchanged. |
| Exact 65,536-byte event | PASS | Deterministic test accepts a valid event exactly at the configured byte limit. |
| 65,537-byte event | PASS | Deterministic test fails safely, records type/tool/sequence/size/limit/execution ID, and excludes a sensitive body marker. |
| UTF-8 and JSON expansion | PASS | Deterministic tests exercise multibyte UTF-8 byte sizing, escaping expansion, and a total-output-interrupted event with a bounded lower-bound size. |
| Browser output bound | PASS | Large rendered text is clipped on UTF-8 boundaries; title/final URL and explicit truncation metadata survive; serialized result remains within 8,192 bytes. |
| Host persistence and owner task detail | PASS | Synthetic runner response persists only whitelisted size metadata and uses the host execution ID; owner task detail formats the safe event summary. |
| Browser authorization profile | PASS | Existing tool-list contract test continues to require only the existing approved browser operations; no new tools or profile combinations were added. |
| Focused tests | PASS | 95 passed, 1 deselected. The deselected controller replay test requires loopback binding, which this local test environment denies (`PermissionError: Operation not permitted`); no source change caused the restriction. |
| Additional regressions | PASS | 18 tests passed across visible-browser broker and TaskStore idempotence. |
| Ruff / BasedPyright | PASS | Ruff clean; BasedPyright 0 errors, warnings, or notes. |
| Import boundaries / worker static checks / diff | PASS | 13 import-boundary rules across 218 files passed; worker static safety checks and `git diff --check` passed. Installer shell syntax passed. |
| Protected worker readiness after deployment | PASS | All required readiness fields were ready; Tart running; desktop service active. |
| Fresh live browser research task | PASS | Owner-created task `task-f7fd7753d7c9fc9115aa9d6fd2a169a6`, explicitly approved with `/run`, reached TaskStore `succeeded` as execution `opencode-e906e45691d24cd29d44b8e72cafe470`; exactly one execution. |
| Actual browser calls and event-limit outcome | PASS | Persisted diagnostics record 44 events, zero retries, and `toothless_browser_navigate`, `toothless_browser_extract_text`, and `toothless_browser_click_link`. No `output_limited` or event-size failure occurred. The call-level text byte count is not logged; the deployed source helper enforces the 8,192-byte structured-result cap. |
| Returned navigation categories and source | PASS | Delivered result lists AI, Energy, Robotics, Space, Funding, GTA 6, and Other as header-navigation categories, citing `https://blogs.jubaerjami.com/`. It also cites the AI and Other tag pages and a post/sitemap. |
| Research exhaustiveness | PARTIAL | While pursuing all site tag pages, the model reported reaching its step limit after finding post tags `Rockstar Games` and `Gaming`; it did not verify all tag pages. The seven header categories are evidenced, but the wider tag taxonomy is not claimed exhaustive. |
| Telegram delivery and host persistence | PASS | TaskStore status `succeeded`; one execution; result delivery `sent`, Telegram message 747; source metadata persisted. |
| Live viewer observation for task `task-f7fd7753d7c9fc9115aa9d6fd2a169a6` | NOT CONFIRMED | The task was dispatched as visible-browser mode and browser calls are recorded, but the owner did not see this run in Watch Live. |
| Historical failed task unchanged | PASS | `task-8aa45ba7d7df7982e0a520f172509e46` remains `failed` with exactly one execution; it was not replayed. |

## Fresh all-tag follow-up attempt

After the owner clarified that the request means every distinct tag/category page and that they did not observe the previous run live, a separate task `task-ba8552131c4a1693b4aa54f76579a129` was queued and explicitly approved. It was not a replay of either earlier task. TaskStore records terminal `succeeded`, exactly one execution `opencode-cba66661f97d4cffb83345c1803800cc`, and Telegram delivery `sent` as message 752. This status means OpenCode returned a final explanatory response; it does not mean the requested browsing succeeded.

Host diagnostics contain 38 events with the `toothless_browser_navigate` and `toothless_browser_close_session` tools, zero recorded extraction tool names, and no event-size failure category. The retained first 32 timeline entries show repeated navigate tool errors and close-session completions; the timeline is marked truncated. The delivered explanation reports an initial Chromium `BrowserContext.new_page: Target crashed` followed by Playwright sync/async-loop errors. The exact exception text is not present in the sanitized host event record, so that detailed cause remains owner-relayed rather than independently verified from host diagnostics. No page text or category list was retrieved. The task was not retried and the VM was not restarted. This run does not pass the research acceptance and does not indicate a recurrence of the oversized-event failure; it exposes a separate visible-browser runtime instability.

## Read-only post-run browser diagnostics

The desktop unit remained `active/running`, `NRestarts=0`, with current `MemoryCurrent=414,855,168` bytes and `TasksCurrent=7` at inspection time; these are post-run values, not failure-time measurements. A bounded journal query for 2026-11-20 04:45–05:10 UTC returned no matching `Target crashed`, Sync-API/asyncio, EAGAIN, SIGSEGV, OOM, AppArmor-denial, or Chromium-crash entries. Thus the owner-relayed target-crash text cannot be corroborated from this journal window; absence of a log entry does not rule out a transient browser-process failure. Current guest process-name counts showed Xvfb 1, Openbox 1, xterm 1, and no Chrome/Chromium/Firefox process. The browser authorization directory was valid and empty (zero task records). No service/VM restart or policy change was performed.

## Remaining limitations

The prior oversized event's type and actual byte size cannot be recovered because that failure record predates the diagnostic change and raw event bodies were intentionally not retained. Task `task-f7fd7753d7c9fc9115aa9d6fd2a169a6` demonstrates a successful end-to-end visible-browser run and delivery without the event-size failure, but it returned only the seven header categories before encountering the step ceiling, and its live viewer activity was not owner-confirmed. The separately approved all-tag task `task-ba8552131c4a1693b4aa54f76579a129` returned an explanatory final answer but retrieved no rendered page text; host state says `succeeded` because a final response was delivered. Therefore the byte-limit fix is live-validated, while the requested exhaustive browser-research acceptance remains FAIL/PARTIAL because of a separate visible-browser crash. Do not retry either terminal task. The existing 300-second browser execution bound and all existing authorization, browser, networking, and output limits remain unchanged.
