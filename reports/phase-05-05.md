# Phase 05.5 — Conversational task steering

Date: 2026-11-17 (America/Los_Angeles)

Status: IMPLEMENTED; LIVE ACCEPTANCE PARTIAL; NOT VERIFIED. The existing timed-out continuation is now independently confirmed from host SQLite records. The current guest runner was deployed through the protected pinned-key installer and both configured OpenCode profiles verified. Real conversational approval and the remaining Telegram acceptance checks are still pending.

## Implementation

The Telegram controller now recognizes bounded conversational status questions, cancellation requests, task follow-ups, exact affirmative replies, and exact negative replies. It uses Telegram reply-to context, explicit task IDs, the persisted most-recent task context, or a unique active task. It asks for clarification when no task can be resolved or multiple tasks are active rather than selecting an arbitrary record. General natural-language requests continue to create queued tasks; they never dispatch by themselves.

TaskStore now records parent-task relationships, a bounded snapshot of the parent result, task-specific Telegram message links, and the latest task context per private chat. A continuation is a new queued task with a new source update ID. It inherits references to the parent's uploaded files and successfully retained generated artifacts from the host's owner-private attachment store. It does not copy or reuse the parent's execution ID. Inherited uploads, generated files, and the saved parent result are marked as untrusted data in the OpenCode prompt; embedded document or page instructions cannot alter the new owner's request or policy. Inherited inputs are capped at 12 files and 4 MiB.

Natural approval is opt-in for continuation proposals only. Toothless persists the chat, exact prompt message ID, expiry (10 minutes), and SHA-256 snapshot of the proposed request, instructions, parent context, and inherited file references. “Yes” starts only when sent as a reply to that precise prompt, while the proposal remains queued, unexpired, unchanged, and unused. Unbound, stale, changed, and replayed approvals are rejected. “No” as a reply to the exact prompt cancels the proposal without dispatch. `/run <task_id>` remains available for any queued task. The existing atomic dispatch claim still persists a new execution intent before worker submission.

Natural status and cancellation responses use host TaskStore and worker records. Status-only progress reports persist elapsed whole minutes and state that no result is available; the controller suppresses further reports after terminal state and also resumes these reports while reconciling a running execution after restart. Timed-out failed executions preserve only bounded structural metadata (complete event count, event types, and tool names), not partial model text or tool arguments. Timeout diagnostics never establish task success. Existing execution ceilings are unchanged: workspace and browser tasks retain their fixed limits, attachment tasks retain the 300-second OpenCode/315-second worker bounds, and no profile was given unlimited runtime.

## Regression verification

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/personal_agent/test_task_store.py tests/personal_agent/test_telegram_controller.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_opencode_guest_policy.py tests/personal_agent/test_telegram_files.py tests/personal_agent/test_worker_interface.py tests/personal_agent/test_worker_vm_contract.py -q` | PASS — 85 passed. Existing pytest-asyncio loop-scope deprecation warning only. |
| `.venv/bin/ruff check nanobot/personal_agent/task_store.py nanobot/personal_agent/telegram_controller.py nanobot/personal_agent/opencode_task.py ops/worker/opencode_task_runner.py tests/personal_agent/test_task_store.py tests/personal_agent/test_telegram_controller.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_opencode_guest_policy.py` | PASS. |
| `.venv/bin/basedpyright nanobot/personal_agent/task_store.py nanobot/personal_agent/telegram_controller.py nanobot/personal_agent/opencode_task.py ops/worker/opencode_task_runner.py tests/personal_agent/test_task_store.py tests/personal_agent/test_telegram_controller.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_opencode_guest_policy.py` | PASS — 0 errors, warnings, or notes. |
| `.venv/bin/python -m scripts.check_import_boundaries` | PASS — 13 rules, 218 files. |
| `ops/worker/test-static.sh` | PASS. |
| `git diff --check` | PASS. |

Focused tests cover continuation persistence and inherited files/artifacts, approval binding and one-time use, changed-action rejection, decline, ambiguous follow-up clarification, natural status and cancellation using a fake worker, one-run dispatch, progress stopping after terminal state, untrusted parent-result prompt framing, and timeout metadata that excludes partial content.

The first live attempt with the requested wording exposed a follow-up-classification gap: “Using the previous data, calculate its mean again and give me a short explanation.” was saved as an ordinary queued task (`task-2e1ebe02ab461d1eab7b66e121ff30f3`) instead of a continuation. The owner cancelled that task before it had any execution or approval. The deterministic follow-up matcher now recognizes “using the previous/same/last data, file, or result”; the existing reply-bound continuation/approval test now uses this exact wording and passes. After restarting the controller, the owner replied to the parent task message and created `task-0a6e04d34146ef62c4a7f9a7a49a1065`. Host records confirm it was queued with parent `task-bc2af8391f32de8ec4d582cfcd9263ac`, inherited `number_1_.csv` (17 bytes), and a pending approval bound to owner chat `1585657305`, Telegram prompt message `385`, and the persisted action hash. The owner replied to that exact prompt; the approval was consumed and exactly one dispatch intent was recorded as execution `opencode-4d1d6f45b413428d83537de702416d31`. The worker acknowledged it. Telegram and host records then showed progress at minutes 1, 2, 4, and 5 (the host also recorded minute 3) with no result, followed by terminal failure at the existing timeout. The task has no result, artifacts, or file deliveries.

The live natural-status check found another wording gap: Telegram supplied a curly apostrophe in “What’s the status of my last task?” and the controller queued it as ordinary task `task-ebd98f37e6b06d2786421e61fd72f7cb`. A subsequent stray plain-text message was recorded as unapproved continuation `task-365d148d65be2b09a5be8286cced0eaa` of that mistaken task. Both were later cancelled without execution. Two earlier queued status-text records, `task-43d350310af3744f5a582b78ce45fe81` and `task-74d7238afa17201e8b0bb365d4bec6b2`, remain queued with no approval or execution. The intended cancellation fixture is separately queued as `task-0d37c05f190eb6b3c5a91d9f2d0948cb`, without approval or execution. The status matcher now accepts straight and curly apostrophes; a focused local regression test confirms the curly-apostrophe status question reads the active task without creating another task. After controller restart, the latest curly-apostrophe message was processed with no task created; the outbound message context record (Telegram message 412) is bound to the current task. The owner did not include the bot's response text, so its wording is not independently confirmed.

## Historical timeout and runner deployment

The historical task `task-743fe8f2b623223b1cfbcfb8132afa09` was read from the owner-private TaskStore database using SQLite read-only/immutable mode; it was not resubmitted. Its stored status is `failed`, error is `timed_out: OpenCode timed out.`, and progress is `OpenCode execution failed.` It has exactly one associated worker execution, `opencode-64ff0cbaacbb48209375071819e8b312`, created at `2026-11-18T06:52:50.153701+00:00`. Its parent is `task-bc2af8391f32de8ec4d582cfcd9263ac`; the saved parent-context snapshot says the parent task timed out after 300 seconds but its CSV summary was independently verified. The child task's own `result` is null and it has no task-artifact or file-delivery rows. The child has one uploaded attachment, `number_1_.csv` (17 bytes). The parent task has delivered `chart.png` (6,640 bytes, Telegram message 323) and `summary.csv` (64 bytes, Telegram message 324); both delivery rows are marked `delivered`. These records support that the parent artifacts reached Telegram, but do not show child artifacts or child result content.

The updated guest runner was installed with `bash ops/worker/install-opencode-runner.sh`; it completed with exit status 0. Installer output reported SHA-256 `a3b3aa5cbe581b4ba5ebe6ba1ee56bc1cdf9ecc211c9c88f3adff86ee85edcbf`, matching the current repository runner. Its protected remote checks passed for OpenCode 2.3.0, the allowlisted `opencode-go/glm-5.3` model, the bounded rootless workspace policy, and the separate browser-only helper-filtered policy. The install script stages over pinned-key SSH and installs the runner as `root:root`, mode `0755`; it does not run model inference. The configured timeout ceilings remain 120 seconds for ordinary tasks and 300 seconds for attachment/browser tasks (315 seconds at the worker boundary). No Softnet policy, VM baseline, host firewall, or macOS permission changes were made by this deployment.

## Live acceptance matrix

| Required check | Result | Evidence |
|---|---|---|
| A. Owner uploads CSV, processes it, then asks for a modification without re-upload | NOT RUN | No new live Telegram upload or continuation task was created in this checkpoint. |
| B. Owner replies “Yes” to an exact continuation proposal; one new worker execution completes | FAIL | Exact reply approval was consumed; one execution `opencode-4d1d6f45b413428d83537de702416d31` was accepted. Task `task-0a6e04d34146ef62c4a7f9a7a49a1065` then timed out after 300 seconds with no result or artifacts. |
| C. Ask for status during an active task | PARTIAL PASS | Host progress events were recorded each minute from 1 through 5; the owner-provided Telegram transcript showed minutes 1, 2, 4, and 5. A separate natural-language status query was NOT RUN. |
| D. Natural-language cancellation reaches active worker | NOT RUN live / PASS deterministically | Fake worker confirms cancellation of the unique active task. |
| E. Restart controller and recover conversation/task context | NOT RUN live / PASS for SQLite persistence | Task parent, artifact references, bot-message mapping, and chat context survive TaskStore reopening. |
| F. Ambiguous and stale approvals cannot start work | NOT RUN live / PASS deterministically | Ambiguous follow-up creates no task; wrong chat/message, altered action, and replay do not dispatch. |
| G. Duplicate updates do not repeat execution or file delivery | NOT RUN live / PASS for local dedupe contracts | Existing update/task idempotency tests pass; no real Telegram redelivery was induced. |
| Live task progress and terminal status | PARTIAL PASS | For continuation `task-0a6e04d34146ef62c4a7f9a7a49a1065`, the owner received progress updates and then a truthful timeout failure. Host records contain progress events at minutes 1–5. No successful result was produced. |
| Guest runner timeout metadata on the real Ubuntu worker | DEPLOYED; naturally timed-out task evidence NOT RUN | The updated runner is installed and its policy checks pass. No new timeout has occurred since deployment; no inference or deliberate wait was used to test timeout metadata. |
| Natural follow-up phrase maps to parent | PASS after fix | First task `task-2e1ebe02ab461d1eab7b66e121ff30f3` was safely cancelled after the unparented queue bug. The retest created task `task-0a6e04d34146ef62c4a7f9a7a49a1065` with the correct parent and inherited CSV. |
| Natural status with curly apostrophe | PARTIAL PASS | Initial attempt mistakenly queued `task-ebd98f37e6b06d2786421e61fd72f7cb`; its stray child `task-365d148d65be2b09a5be8286cced0eaa` was cancelled. After the matcher fix/restart, the query created no task and produced outbound context message 412 bound to the current task. The owner did not provide the text of the bot reply. |
| Harmless active-task cancellation through natural language | PASS | Task `task-0d37c05f190eb6b3c5a91d9f2d0948cb` was owner-approved and worker-acknowledged as execution `opencode-dd01b21551094271969fc63000e39846`, then cancelled via “cancel that task”. Host event records show cancellation intent followed by worker-confirmed terminal cancellation; the owner transcript contains one cancellation confirmation. |
| Cleanup of older unintended status tasks | PASS | Tasks `task-ebd98f37e6b06d2786421e61fd72f7cb`, `task-365d148d65be2b09a5be8286cced0eaa`, `task-43d350310af3744f5a582b78ce45fe81`, and `task-74d7238afa17201e8b0bb365d4bec6b2` were cancelled before execution. |
| Controller restart and terminal task recovery | PASS for stored state | After controller restart, `/task task-0a6e04d34146ef62c4a7f9a7a49a1065` returned the same failed status, parent relationship, timeout metadata, and sole execution ID. |
| Duplicate `/run` on failed task | PASS | `/run task-0a6e04d34146ef62c4a7f9a7a49a1065` returned “failed; it was not submitted again”; host DB still contains exactly one execution. |
| Unbound affirmative approval | PASS | Standalone `Yes` returned “reply directly to its approval message”; no dispatch or execution was added. |
| Expired approval rejection | PASS | Replying to the exact original proposal for cancelled task `task-365d148d65be2b09a5be8286cced0eaa` returned “That approval expired. Nothing was started”; the host approval row was consumed after expiry, with no execution. |
| Literal “duplicate /run …” text | Unexpected task, then cleaned up | Text `duplicate /run task-0a6e04d34146ef62c4a7f9a7a49a1065` created task `task-ead70f74c743240b2fa5f6f53fe12e86`; `/cancel` cancelled it before execution. The actual slash command was tested separately and was idempotent. |

The host record for continuation `task-0a6e04d34146ef62c4a7f9a7a49a1065` shows one approval, one dispatch intent, worker acknowledgement, five persisted minute-progress events, and one terminal timeout. Its error contains 15 complete OpenCode events; the observed event types are `step_start` and `retry`. No tool names, final answer, result, artifacts, or file deliveries are recorded. The sanitized metadata stores a count and distinct event types, not event ordering, so the exact last event cannot be identified; the captured types provide no completion/finalization evidence. This is consistent with the existing 300-second attachment-task deadline, which was not increased. The earlier task `task-743fe8f2b623223b1cfbcfb8132afa09` remains failed with its own single execution and parent `task-bc2af8391f32de8ec4d582cfcd9263ac`; its child result is empty, and its parent's verified summary files were delivered as documented above. No retry was made. Workspace shell and browser-only profiles remain separate, tasks remain owner-approved, and no scheduling or automatic execution was enabled.

## Remaining limitations and next verification

Conversation understanding is deterministic and intentionally bounded to supported status, cancellation, and follow-up patterns; it does not use an extra model call to classify messages. If the text or reply context is ambiguous, the controller requests an explicit task reference. Approval requires replying to the exact Telegram proposal prompt; plain unbound “yes” is deliberately not approval. Telegram delivery of proposal message IDs, database recovery under a real process restart, and live cancellation/progress timing still require operator acceptance.

At the first acceptance checkpoint, exact-message approval and parent/child persistence were live-verified, but continuation `task-0a6e04d34146ef62c4a7f9a7a49a1065` timed out before producing an answer and was not resubmitted. After restart, `/task` recovered that terminal record, duplicate `/run` was rejected, unbound `Yes` was rejected, and replying to the exact expired proposal was rejected as expired. The curly-apostrophe status query created no task after the matcher fix, though the owner did not supply the reply text. The harmless sleep task was cancelled by the worker via natural language, and unintended tasks were cancelled before execution. The first checkpoint remained NOT VERIFIED; the final live continuation and its restart checks are documented below and supersede that intermediate status.

## 2026-11-18 — Retry diagnosis and bounded event capture

### Confirmed findings from the failed execution

The owner-private TaskStore was opened read-only (`mode=ro&immutable=1`). Execution `opencode-4d1d6f45b413428d83537de702416d31` belongs to task `task-0a6e04d34146ef62c4a7f9a7a49a1065`, parent `task-bc2af8391f32de8ec4d582cfcd9263ac`. It has one dispatch intent, one worker acceptance, five persisted minute progress events, and one terminal timeout at 300 seconds. Its request has 180 characters, its continuation instructions 82 characters, its stored parent context 505 characters, and its inherited CSV 17 bytes. It has no saved answer, child artifacts, or file deliveries. The parent CSV task is separately known to have created verified partial artifacts; that does not establish that this continuation read them.

The worker record preserved a guest response summary with 15 complete OpenCode events, distinct types `step_start` and `retry`, no tool names, and no final answer. It did not preserve event order, individual timestamps, retry count, retry attempt, status class, category, backoff, provider response, or finalization evidence. Therefore “15 retries” is not supported: 15 is the total complete event count, and the number of those events that were retries is unknown. There is no recorded HTTP 429, quota message, authentication failure, DNS/TLS error, transport timeout, parse error, or provider endpoint failure. A retry loop before tool activity is observed, but its triggering cause is not.

### Comparison of existing runs

| Run | Profile and input | Model | Observed result |
|---|---|---|---|
| Phase 05.2 success: `opencode-6029197c9fbc4256846de904d9ff63e1` | Workspace; 218-character request, no attachment or inherited context | `opencode-go/glm-5.3` | Succeeded in 11.958 seconds; 13 events; `write` and `shell` tools. |
| Phase 05.3B success: `opencode-05939a1953ca4ec7ac4b7a7f9b29fca2` | Browser-only; 224-character request, no attachment or inherited context | `opencode-go/glm-5.3` | Succeeded in 7.575 seconds; 15 events; browser navigate, extract, and close tools. |
| Phase 05.4 attachment timeout: `opencode-7fcd085254874d77b87cc23d761bf894` | Workspace; 180-character request, one 17-byte CSV; exports existed but no final response | `opencode-go/glm-5.3` | Timed out at the 300-second guest deadline; prior instrumentation discarded event details. |
| Phase 05.5 continuation timeout: `opencode-4d1d6f45b413428d83537de702416d31` | Workspace; same 180-character request, plus 82-character instructions, 505-character parent snapshot, and inherited 17-byte CSV | `opencode-go/glm-5.3` | Timed out at 300 seconds; 15 events total, including event types `step_start` and `retry`; no tools or final response recorded. |

Across these records, the selected model is unchanged. The effective runner policy, CLI version, and model identity were verified by the protected installer for both current profiles; the workspace failures and workspace success use the same configured profile. Successful provider inference in the two earlier runs confirms the route worked at those earlier times, not that provider availability or authentication was healthy during the later failures. No provider connectivity probe was run during the old executions, and the prior event capture cannot establish provider health, prompt transmission, retry causes, or whether the model reached a tool-selection stage. The larger context and attachment co-occur with the failures, but the evidence does not isolate either as causal. Request deadlines and model were not changed.

### Narrow observability change deployed

The fixed guest runner now keeps a bounded ordered timeline (first 32 safe events from at most 1,000 complete records), total event count, retry count, distinct safe event types and tool names, and a truncation flag. For fields present in the CLI event stream it records the event timestamp, retry attempt and scheduled retry time, computes bounded backoff milliseconds only when both event and retry timestamps are present, and records safe error category, HTTP status class, and tool state. Missing timestamps remain absent; sequence numbers reflect stream order and are not presented as wall-clock time. Error categories are allowlisted (`rate_limit`, `authentication`, `tls`, `timeout`, `network`, `response_parse`, `provider_unavailable`, `provider_error`, `unknown`). Raw provider messages, tool arguments, model text, URLs, and attachment content are excluded from diagnostics. The host filters this structure again, caps it at 8 KiB, and saves it as an `execution_diagnostics` TaskStore event alongside the terminal state. The change does not alter OpenCode retry behavior or infer a root cause.

The protected installer exited 0 and installed the final runner SHA-256 `68ac56c2357038ffd4832193a59b8c1e014689589d940caa27a19ca9d5b2777c`. Its effective checks passed for OpenCode 2.3.0, `opencode-go/glm-5.3`, the bounded rootless workspace profile, and the separate browser-only profile. It changed no timeout, model, provider, Softnet policy, or macOS permission and performed no inference.

### Verification and current status

| Check | Result |
|---|---|
| Read-only historical task and worker evidence review | PASS; task not resubmitted. |
| New retry-event parser fixture; sensitive message omitted | PASS. |
| Diagnostics host filtering and TaskStore persistence | PASS. |
| `.venv/bin/python -m pytest tests/personal_agent -q` | PASS — 132 passed, 7 skipped. |
| `.venv/bin/ruff check` on changed runner/controller/store/tests | PASS. |
| `.venv/bin/basedpyright nanobot/personal_agent/opencode_task.py nanobot/personal_agent/task_store.py nanobot/personal_agent/telegram_controller.py ops/worker/opencode_task_runner.py` | PASS — 0 errors, warnings, or notes. |
| `bash ops/worker/test-static.sh` | PASS. |
| `git diff --check` | PASS. |
| New READY inference | FAIL — owner task `task-9e3a8bff67516956576169339ff16431`, execution `opencode-dc40a6ae5de244d3a05234910a7390c8`, was queued and explicitly approved. It reached RUNNING, then timed out with 9 total complete events of types `step_start` and `retry`; no tool, answer, result, or artifacts. |
| Retry reason for READY run | NOT AVAILABLE — TaskStore has no `execution_diagnostics` event. The deployed guest runner returns timeout diagnostics as top-level fields, while the running host parser only read a nested `diagnostics` object. The host therefore retained the event count/type summary but discarded the timeline. A local parser fallback now accepts and sanitizes the runner's top-level fields; it has not yet been loaded by a controller restart or live-verified. |
| Short Python arithmetic task | NOT RUN — stopped after the READY inference repeated the retry/timeout pattern, as requested. |
| New CSV continuation, result delivery, and restart verification | NOT RUN — stopped after the READY failure. |

At the time of this initial report, the confirmed root cause was limited to why diagnostic detail was missing from the host record: the guest/host JSON shape mismatch. The historical provider retry cause was unknown, and no later inference was authorized then. Those failed task IDs remain terminal and were not retried. The subsequent controller restart, read-only diagnostics, guest reauthentication, resolver restoration, and one newly approved READY request are recorded in the dated addenda below.

## 2026-11-18 — Reliability diagnosis and read-only provider health

### Historical READY run: recovered worker-side timeline

Task `task-9e3a8bff67516956576169339ff16431` and execution `opencode-dc40a6ae5de244d3a05234910a7390c8` were inspected from the host-owned SQLite execution record in read-only mode; the task was not resubmitted. The guest runner returned a successful transport response containing a model-run timeout after 121.036 seconds. The bounded event timeline contains 9 events: one `step_start` and 8 `retry` events, with retry attempts numbered 2 through 9. There are no tool calls, final answer, provider response details, HTTP status class, or completion/finalization event. Each retry has safe category `unknown`; therefore the evidence does not identify authentication, quota, service, DNS, TLS, transport, or response parsing as the trigger.

| Sequence | Event | Attempt | UTC timestamp | Safe category |
|---:|---|---:|---|---|
| 1 | `step_start` | — | `2026-11-18T17:22:58.613Z` | — |
| 2 | `retry` | 2 | `2026-11-18T17:22:58.613Z` | `unknown` |
| 3 | `retry` | 3 | `2026-11-18T17:23:00.940Z` | `unknown` |
| 4 | `retry` | 4 | `2026-11-18T17:23:04.446Z` | `unknown` |
| 5 | `retry` | 5 | `2026-11-18T17:23:13.394Z` | `unknown` |
| 6 | `retry` | 6 | `2026-11-18T17:23:31.229Z` | `unknown` |
| 7 | `retry` | 7 | `2026-11-18T17:23:56.178Z` | `unknown` |
| 8 | `retry` | 8 | `2026-11-18T17:24:26.186Z` | `unknown` |
| 9 | `retry` | 9 | `2026-11-18T17:24:55.871Z` | `unknown` |

The host TaskStore did not contain the detailed diagnostic event from this run because the controller version that executed it expected nested `diagnostics`, while the guest runner returned top-level diagnostic fields. The worker database retained the bounded guest result, allowing the timeline above to be recovered. The host parser correction now accepts both shapes and sends either through the same revalidation and size cap before TaskStore persistence. It does not change the guest retry policy.

### Parser and persistence verification

The Telegram controller was stopped and restarted using its documented command; no VM restart or reset occurred. Startup logged that Telegram polling connected. Process inspection found one active controller process group. The restarted process loaded the current parser implementation. A synthetic worker response exercised both nested and top-level shapes through result parsing and `TaskStore.finish_execution`; tests verified that event order, retry attempts/count, safe category, timestamps, and allowed tool metadata survive persistence, while secrets, prompts, raw provider responses, and file contents do not.

Command: `.venv/bin/python -m pytest tests/personal_agent/test_opencode_task.py::test_synthetic_runner_response_persists_sanitized_diagnostics_in_task_store tests/personal_agent/test_opencode_task.py::test_parse_task_result_keeps_sanitized_retry_diagnostics tests/personal_agent/test_opencode_task.py::test_parse_timeout_captures_guest_top_level_metadata -q` — PASS, 3 passed. The historical run itself remains unrecoverable in host TaskStore diagnostics; its timeline is taken from the guest execution record, not reconstructed or inferred.

Post-restart regression commands and results: `.venv/bin/python -m pytest tests/personal_agent -q` — PASS, 134 passed and 7 skipped; `.venv/bin/ruff check nanobot/personal_agent/opencode_task.py nanobot/personal_agent/task_store.py nanobot/personal_agent/telegram_controller.py ops/worker/opencode_task_runner.py tests/personal_agent/test_opencode_guest_policy.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_task_store.py` — PASS; `.venv/bin/basedpyright nanobot/personal_agent/opencode_task.py nanobot/personal_agent/task_store.py nanobot/personal_agent/telegram_controller.py ops/worker/opencode_task_runner.py` — PASS, zero errors/warnings/notes; `bash ops/worker/test-static.sh` — PASS; `.venv/bin/python -m scripts.check_import_boundaries` — PASS, 13 rules/218 files; `git diff --check` — PASS. Pytest emitted only the existing pytest-asyncio fixture-loop-scope deprecation warning.

### Read-only worker checks and observed correction

Protected read-only checks confirmed OpenCode 2.3.0, the explicit `opencode-go/glm-5.3` model, the rootless workspace profile, the separate browser-only profile, and existing bounded execution settings. They also found the guest OpenCode Go authentication file absent, and the non-secret auth listing did not show an OpenCode Go account. No credential was printed, copied, changed, or reconfigured. The runner constructs a filtered environment and does not inherit host credentials.

Guest `systemd-resolved` had no DNS scope/server configured on `enp0s1`, although the approved `toothless-dns-configure.service` was active. The existing already-approved guest helper was rerun with noninteractive sudo; it restored the dynamically discovered Softnet relay at port 55353. This changed only the guest resolver configuration through the existing mechanism. Afterward, both workspace and browser scopes resolved `opencode.ai`; TLS-verified HTTPS GET requests to the public OpenCode Go model catalog returned HTTP 200. Direct public-IP TLS verification was also successful before DNS restoration. These unauthenticated catalog requests verify name resolution and public TLS connectivity only; they do not prove valid account authentication, subscription entitlement, quota availability, or inference health. No model request was made during these checks.

The guest clock reported NTP unsynchronized. Certificate verification succeeded, so no certificate failure was observed; time synchronization remains a diagnostic limitation. Guest disk had approximately 12.6 GiB free (29% used). Process inspection found a pre-existing `admin`-owned `opencode serve --service` process, parented to PID 1 and started Nov 17; it was left untouched. A separate `toothless-agent` service process appeared during the read-only `opencode auth list` check and was stopped by exact PID after ownership and start time were confirmed; the unrelated admin process remained running. Workspace and browser profile public endpoint connectivity matched after DNS restoration.

### Provider evidence and root-cause assessment

The [OpenCode status page](https://opencode.ai/status/) currently reports hosted inference operational and lists a Nov 18 “Slow responses Go” incident from 15:57 to 17:38 UTC. The READY retry timeline from 17:22:58 to 17:24:55 UTC falls within that incident. This is a time correlation and a plausible contributing factor, not proof of the failure cause. The status page notes that requests rejected due to account balance, budget, or usage limits are not counted as service failures, so the incident cannot rule out an account-specific limit. The [OpenCode Go documentation](https://opencode.ai/docs/console/go/) says usage can be checked in Console; account-specific allowance/entitlement details were not available from the guest's unauthenticated read-only checks.

**Confirmed at initial health check:** host parser shape mismatch caused the original omission of the detailed timeline; the historical run had eight retries, each category `unknown`, and no tool/final answer; DNS client configuration was absent during this diagnostic check and was restored by the existing helper; public DNS and TLS-verified catalog HTTPS now work in both profiles; the guest auth file was absent at that initial check; a Go slow-response incident overlapped the failed request.

**Not established:** the auth file's state at the exact historical failure time; account entitlement or quota at that time; whether DNS configuration was missing during that inference; whether the provider incident triggered the retries; and the exact retry error. The later successful READY request verifies one authenticated inference, not a complete account quota or entitlement audit.

**Fixes made:** the host parser correction is loaded and passes synthetic persistence tests; the existing DNS-client helper restored dynamic guest DNS. No provider/model, credentials, billing setting, timeout, guest privilege, Softnet policy, host firewall, or macOS permission was changed. The model remains `opencode-go/glm-5.3`.

### Controlled inference gate and current status

At the time of the initial health check, the guest OpenCode Go auth file was absent and no inference was attempted. The operator subsequently reported 0% Go usage and completed the existing interactive guest authentication flow, which reported “Connected to OpenCode Go.” After DNS was restored with the existing helper, a separately created READY task was explicitly approved and completed. No arithmetic task or CSV continuation was run in this diagnostic step. Do not retry either earlier failed execution. This READY test establishes that a simple request worked at that time; it does not identify the cause of the earlier retry loop or satisfy the Phase 05.5 conversational-continuation acceptance by itself.

| Check | Result |
|---|---|
| Controller restart without VM restart; one poller active | PASS |
| Nested and top-level parser shapes, sanitized TaskStore persistence | PASS; 3 focused tests passed |
| Historical READY timeline recovered from worker DB | PASS; 9 events, 8 retries, all categories `unknown` |
| Exact retry cause | NOT ESTABLISHED |
| Effective OpenCode version/model/profile checks | PASS |
| Guest Go credential presence at initial health check | FAIL at that time; later restored through interactive guest login |
| Existing dynamic DNS restored; DNS and TLS-verified public catalog GET in both profiles | PASS after helper reapplication; does not verify inference |
| Authenticated minimal inference after guest login | PASS for one READY task; this is not a full account/quota audit |
| Fresh minimal READY inference after guest reauthentication and DNS restoration | PASS; see live result below |
| Arithmetic task or CSV continuation | NOT RUN |
| Softnet, host firewall, or macOS permission changes | NONE |

### 2026-11-18 — One controlled READY inference after reauthentication

The owner reported that OpenCode Go Console usage was 0% and ran `bash ops/worker/opencode-auth-login.sh`. The guest prompt reported “Connected to OpenCode Go”; the credential remained guest-local and was not displayed in this report. This is evidence that the login flow accepted a credential, while the owner-provided usage figure is not independently captured from the account console.

A fresh owner Telegram task, `task-50a661d9b46f7066edde1bcd2327a577`, was checked in immutable read-only host SQLite before approval. It was queued with the exact request `Reply with exactly READY. Do not use any tools.`, no approval timestamp, and zero execution IDs. The owner then explicitly ran `/run task-50a661d9b46f7066edde1bcd2327a577` in the authorized Telegram chat. Toothless replied “dispatching to the Ubuntu worker” followed by “completed” and `READY`.

Host TaskStore records show status `succeeded`, no error, a saved result, and exactly one execution: `opencode-d105367e01bf40759ed4fecb604924a8`. The worker journal independently records that execution as accepted and `succeeded`, exit code 0, with no stderr; it completed in approximately 5.2 seconds. Host `execution_diagnostics` contains 3 ordered events with timestamps: `step_start`, `text`, `step_finish`; retry count is zero, tool list is empty, and metadata was not truncated. The result text itself is omitted from this report; Telegram delivery of `READY` is confirmed by the owner-provided transcript.

This is a successful one-request inference after both guest authentication was refreshed and DNS had been restored, so the request did not exhibit the earlier retry pattern. Because both conditions had been addressed before the request, this does not isolate which condition, if either, caused the historical failures. The Nov 18 Go slow-response incident overlapping the earlier failure remains a correlation only. The [OpenCode status page](https://opencode.ai/status/) currently reports hosted inference operational, while noting that account balance/usage-limit denials are not counted as service incidents. No model, timeout, provider, billing, privilege, Softnet policy, host firewall, or macOS permission was changed in this check.

| Controlled READY verification | Result |
|---|---|
| New task queued before approval; exact prompt and no existing execution verified | PASS |
| Explicit owner `/run` approval | PASS |
| Ubuntu worker execution | PASS; `opencode-d105367e01bf40759ed4fecb604924a8`, exit 0 |
| OpenCode model | `opencode-go/glm-5.3` (effective config previously verified; unchanged) |
| Final answer delivered to Telegram | PASS; `READY` |
| Host TaskStore state and execution association | PASS; succeeded, exactly one execution |
| Sanitized diagnostics persisted | PASS; 3 events, 0 retries, 0 tools, `step_finish` observed |
| Duplicate run | NOT RUN for this new task |
| Conversational continuation completion | NOT RUN |

Phase 05.5 remained **NOT VERIFIED** at this point in the chronological record, pending the requested real conversational continuation. Existing task deadlines and isolation remain unchanged.

## 2026-11-18 — Final live continuation acceptance

### Preflight and recovery-path review

Before creating the continuation, read-only checks confirmed the worker was already running through the protected Softnet launcher, the workspace and browser-only effective-config checks passed for OpenCode 2.3.0 and `opencode-go/glm-5.3`, DNS resolved through the approved dynamic relay `172.30.249.129:55353`, and one Telegram controller poller was active. The guest OpenCode database contains an active `opencode-go` credential record; only provider ID and active status were inspected, never credential values. The recent READY inference also confirmed the credential was usable. No old failed task was submitted.

The earlier report that authentication was absent relied on a check for an `auth.json` file and a CLI listing that did not show the account. The installed OpenCode 2.3.0 stores credentials in its guest-local SQLite `opencode.db` `credential` table, not the assumed JSON path. The current database has one active and one inactive `opencode-go` credential record. Because there is no historical snapshot of this table from the earlier check, it cannot be determined whether the account was genuinely absent then or whether that diagnostic was a false negative; the operator did perform the interactive login afterward. No credentials were copied from macOS.

The DNS systemd oneshot journal shows successful relay configuration at worker boot on Nov 17 and again on Nov 18 at 06:23–06:24 UTC. At the earlier health investigation, `systemd-resolved` nevertheless had no DNS scope/server configured; the current preflight now sees the correct relay and resolves `opencode.ai`. The precise cause and time of the lost per-link resolver state are not present in the journal. `toothless-dns-configure.service` is a `RemainAfterExit=yes` oneshot, so its “active” state records that it ran; it does not continuously verify that dynamic per-link resolver state remains installed. `configure_dns_client.sh` discovers the Softnet gateway at runtime and applies `gateway:55353` with `resolvectl`; no gateway is hard-coded.

`ops/worker/start.sh` checks host preflight and delegates to `softnet_launcher.py`, which installs/reads back the Softnet host policy; it does not currently verify DNS after guest network startup. Guest DNS configuration is instead applied by the enabled boot-time oneshot. `ops/worker/reset.sh` replaces the worker with a clone of the sealed base; its post-reset message mentions reinstalling the execution helper, but does not restore later OpenCode installation or guest-local authentication. Reset was not performed, and the baseline was not modified.

**Recommended smallest recovery improvement (not implemented in this checkpoint):** after the protected launcher has started Tart and confirmed Softnet policy, run the existing fixed DNS-client helper and verify the current interface relay plus a DNS lookup before declaring the worker task-ready. Fail closed and report DNS unavailable if either step fails. Separately, add a non-secret pre-dispatch auth check against the guest OpenCode credential database for an active `opencode-go` record; return an actionable instruction to run `bash ops/worker/opencode-auth-login.sh` if missing. Do not transfer host credentials. Update the reset operator checklist to reinstall OpenCode and repeat the interactive guest login after a reset; do not put credentials in the sealed baseline.

### Continuation creation, approval, and guest work

The owner replied to the original task `task-6a7bdc1eb955983728d9522b53086306` upload acknowledgment for `number.csv`. The host record confirms this source task is still failed from its earlier `backend_protocol_error: PermissionError`, has exactly one old execution `opencode-e26f19f5e8aa460c89c61b49117adad1`, and has the original 17-byte CSV attachment. The continuation `task-cbd05c7a88e40fa15f4834d9e64e2667` was created as a new queued child with `parent_task_id=task-6a7bdc1eb955983728d9522b53086306`, inherited `number.csv` (17 bytes), and follow-up instructions “Using the same CSV, calculate the mean and explain it in one sentence”. It had no execution before approval. Proposal message 454 was persisted with a ten-minute expiry and action hash; the owner replied directly “yes” to that message. The proposal was consumed once and one dispatch/execution was recorded.

The worker execution is `opencode-adecb7a01a5a4c209f07db51da92d975`, status `succeeded`, exit code 0, with 43 complete OpenCode events, zero retries, and no error. Sanitized event metadata lists actual `read`, `shell`, and `write` tool calls; the first read tool event is recorded at sequence 2. The guest task path `inputs/task-cbd05c7a88e40fa15f4834d9e64e2667/number.csv` has the same SHA-256 as the host attachment, confirming the exact uploaded bytes were staged. The parent context snapshot contained no saved result, so the CSV was the available source for the values. An independent host-side parse of the original attachment confirmed five values and mean 6.

OpenCode returned the mean and the one-sentence explanation through Telegram, and TaskStore saved the result. The response also recreated a chart and summary CSV because the continuation retains the parent's original full request and adds the follow-up as additional instructions. This source task had failed before generating any artifacts, so this continuation delivered `chart.png` and `summary.csv` once each (Telegram message IDs 459 and 460); these are not duplicate deliveries for this source task. This behavior is broader than the short follow-up alone and is a user-visible limitation of the current continuation semantics. The current acceptance passed; a later product improvement could make continuation requests explicitly additive or replacement actions, with the proposed action visible in the approval text.

### Restart, duplicate, and approval safety

Only the Telegram controller was stopped and restarted; the Ubuntu VM was not restarted. The new controller connected to Telegram polling. After restart, the owner used `/task task-cbd05c7a88e40fa15f4834d9e64e2667`; Toothless returned the same `succeeded` result, parent link, and execution ID. `/run task-cbd05c7a88e40fa15f4834d9e64e2667` returned that it had already succeeded and was not resubmitted. A standalone `Yes` was rejected because it did not reply to a pending approval. Immutable host SQLite checks after those interactions show exactly one child execution, the source parent's status and execution unchanged, each output delivery still recorded once, and the earlier failed task `task-0a6e04d34146ef62c4a7f9a7a49a1065` still failed with its original single execution. No previous failed execution was retried.

Earlier live checks in this phase already confirmed exact-message approval, duplicate approval/replay rejection, stale/expired proposal rejection, and natural cancellation. Focused deterministic tests were rerun: `.venv/bin/python -m pytest tests/personal_agent/test_task_store.py::test_continuation_inherits_files_and_requires_single_reply_bound_approval tests/personal_agent/test_telegram_controller.py::test_conversational_follow_up_is_reply_bound_and_yes_approves_once tests/personal_agent/test_telegram_controller.py::test_ambiguous_follow_up_does_not_guess_or_create_task -q` — PASS, 3 passed. `git diff --check` — PASS. Full personal-agent suite previously passed 134 tests with 7 skipped after the diagnostics changes.

| Final acceptance check | Result |
|---|---|
| Worker auth, DNS, model/workspace policy, single Telegram poller preflight | PASS |
| Correct parent and inherited 17-byte CSV | PASS; matching host/guest SHA-256 |
| Reply-bound approval consumed exactly once | PASS; prompt message 454 |
| OpenCode read/tool evidence | PASS; 43 events, zero retries, `read` at sequence 2 plus `shell`/`write` tools |
| Mean and one-sentence explanation | PASS; mean 6, independently checked against original CSV |
| Result persistence and Telegram delivery | PASS |
| Controller-only restart recovery | PASS |
| Duplicate `/run` and standalone `Yes` | PASS; no new execution or task approval |
| Old failed task/parent mutation or execution resubmission | NONE; records unchanged |
| Artifact delivery after restart | PASS; one record each for chart and summary, no resend |
| Ambiguous/expired approval safeguards | PASS by existing tests and earlier live expiry check |
| Cause of prior DNS resolver loss | NOT ESTABLISHED; oneshot journal shows successful runs, no loss event |
| Why earlier auth diagnostic reported absence | Prior JSON-path check was invalid for this SQLite-backed OpenCode install; historical DB state unavailable |

Phase 05.5 is **VERIFIED** for the implemented conversational continuation and approval flow. The exact historical retry cause remains unproven. DNS startup verification, auth pre-dispatch diagnostics, and reset recovery guidance are recommended follow-ups, not changes made here. No memory, scheduling, or blog automation was started.


## Phase 05.5.1 — Follow-up semantics and worker readiness (2026-11-18)

Status: VERIFIED for the requested follow-up semantics and worker-readiness profile. The prior Phase 05.5 VERIFIED acceptance above is preserved as historical evidence; this checkpoint adds a new live continuation and protected-start verification.

Follow-up tasks now store the latest owner message as `request`, leave `instructions` empty, and retain the parent request, prior instructions, result, and outputs only in a bounded historical-context snapshot explicitly labeled untrusted and non-executable. The OpenCode prompt identifies the current request as the sole action, tells the model not to repeat or resume parent work unless asked, and allows inherited files/results to be reused as data. The Telegram approval proposal names the exact proposed follow-up action and says prior work and outputs will not be repeated unless explicitly requested. Parent task and execution records are unchanged.

Protected startup now waits for the guest command agent, reruns the existing fixed root-owned DNS-client helper after the Softnet host policy and DNS relay are ready, verifies the dynamic relay address and port 55353 on `enp0s1`, then resolves `opencode.ai` to a public IPv4 address. Any helper, resolver, or lookup failure blocks startup and terminates Tart. The relay address is discovered at runtime; no gateway IP is hard-coded. The guest runner checks only `integration_id` and `active` metadata in OpenCode's SQLite credential table. It never selects credential values; if no active `opencode-go` record exists it returns `auth_required` with instructions to run `bash ops/worker/opencode-auth-login.sh` and enter the key only at the Ubuntu prompt. No Mac credentials are copied. Reset instructions now cover reinstalling the execution helper, OpenCode and its runner, plus fresh guest-local authentication. The baseline was not modified or resealed.

The updated guest runner was deployed with `bash ops/worker/install-opencode-runner.sh`; exit status 0, installed SHA-256 `d21bf24dbbb31ff68cff3c0596d2243f6f43d003c36e1962336798769e05bfd9`. Both workspace and browser-only profile verifiers passed. The worker was gracefully powered off through the Tart guest agent after the existing `shutdown.sh` failed host-key verification; no guest shutdown was confirmed by that SSH script. The protected `bash ops/worker/start.sh` then passed host preflight, acknowledged the Softnet policy (`out @host` block, two DNS allow rules), and reported DNS forwarding active at dynamically assigned `172.31.90.225:55353`, guest relay reapplied and resolution verified, direct host port 53 blocked. Read-only guest checks confirmed `systemd-resolved` uses that address, `getent ahostsv4 opencode.ai` resolved public addresses, workspace and browser OpenCode profile checks passed, and the guest SQLite metadata query found one active `opencode-go` credential. Protected Tart/Softnet policy and rootless OpenCode identity were not weakened; no baseline or Mac permission/network settings changed.

Local verification: `.venv/bin/python -m pytest tests/personal_agent/test_worker_vm_contract.py tests/personal_agent/test_opencode_guest_policy.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_task_store.py tests/personal_agent/test_telegram_controller.py tests/personal_agent/test_telegram_files.py tests/personal_agent/test_worker_interface.py -q --tb=short` — PASS, 96 passed. `.venv/bin/ruff check` on the changed Python modules/tests — PASS. `.venv/bin/basedpyright` on changed Python modules/tests — PASS, 0 errors/warnings/notes. `ops/worker/test-static.sh` — PASS. `.venv/bin/python -m scripts.check_import_boundaries` — PASS, 13 rules / 218 files. `git diff --check` — PASS. Tests cover follow-up action separation and approval snapshot, parent file inheritance and store reopen, active/missing guest credential metadata without key reads, DNS reapply/verification and fail-closed startup.

| Phase 05.5.1 check | Result |
|---|---|
| Local follow-up semantics and approval snapshot | PASS |
| Missing DNS fails protected startup closed | PASS in deterministic test |
| Active/missing credential metadata handling | PASS in deterministic test; live guest has one active record |
| Protected startup DNS reapply with dynamic relay | PASS live; `172.31.90.225:55353` |
| OpenCode 2.3.0 model/profile checks | PASS live; workspace and browser-only profiles |
| Reset reinstall/auth guidance | PASS; operator docs and reset message updated; reset not run |
| New owner-approved continuation and tool evidence | PASS; task `task-1ea41cb87047e227475473ac9edc0d31`, execution `opencode-831ed5e9495c48ea92df61223decdb91`; one `read` tool event, succeeded, zero retries |
| Approval/action binding | PASS via the proposal’s explicit `/run <exact task ID>` alternative; prompt message 515 showed the sole follow-up action. A reply-bound `Yes` was not used for this new task; that path was previously live-verified in Phase 05.5 and remains covered locally |
| No repeated artifact generation/delivery | PASS; child has zero artifacts and zero file deliveries |
| Parent/old execution records remain unchanged | PASS; original upload and completed parent task retain their prior statuses and sole original execution IDs |
| Baseline reseal or modification | NOT DONE |

The historical cause of the earlier DNS state loss remains unknown. The previous `RemainAfterExit=yes` DNS oneshot reports that it ran, not that the dynamic link state remains correct; protected startup now independently reapplies and verifies it. The prior missing-auth conclusion was based on an incorrect JSON-path assumption; current detection uses the actual SQLite store, but the historical database state is unavailable. The current controller was restarted from the updated source and reconnected to Telegram polling before this acceptance. The owner replied to the completed parent task context and created child `task-1ea41cb87047e227475473ac9edc0d31` with exact request “Using the same CSV, tell me only its mean in one sentence. Do not create or resend files.” It was stored queued with parent `task-cbd05c7a88e40fa15f4834d9e64e2667`, blank additional instructions, the 17-byte `number.csv`, and inherited `previous-chart.png`/`previous-summary.csv`. Before execution, the proposal for Telegram message 515 stated that this exact request was the only work and prior outputs would not be repeated unless requested. The owner explicitly approved with `/run task-1ea41cb87047e227475473ac9edc0d31`, the alternative shown in the prompt. Exactly one dispatch and worker execution were recorded. Host TaskStore and the worker execution journal agree on `succeeded`, exit code 0, selected model `opencode-go/glm-5.3`, zero retries, and approximately 12.3 seconds. Sanitized events show one completed `read` tool call followed by final text; tool arguments are intentionally not stored. The final Telegram answer was “The mean of the CSV’s five values (2, 4, 6, 8, 10) is 6.0.” The host child has no generated artifact rows and no Telegram file-delivery rows. The parent task remains succeeded with its original execution and two already-delivered artifacts; the original uploaded task remains failed with its original single execution. No prior task was resubmitted. A reply-bound `Yes` was not used on this new child because the owner used the offered explicit `/run` alternative; reply-bound approval was demonstrated in the previous Phase 05.5 live acceptance and is covered by the updated local continuation/approval tests. No direct duplicate `/run` was sent for this child; the record contains exactly one execution. No continuation task or approval remains necessary for this acceptance.
