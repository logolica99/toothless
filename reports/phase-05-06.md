# Phase 05.6 — Persistent Memory and Project Context

Date: 2026-11-18

Status: VERIFIED for owner-only memory creation, restart retrieval, task/file references, correction precedence, and forgetting through Telegram. Unauthorized-user, uploaded-content, and duplicate-update protections passed deterministic tests; a live second-account test was not run.

## Implementation

- Added a separate host-controlled SQLite `MemoryStore` beside the existing TaskStore database. It enforces an owner-private parent directory, rejects symlink paths, and creates the database with mode `0600`.
- Added project descriptions; explicit owner facts, preferences, and decisions; task-result references; file/artifact references; provenance IDs and timestamps; Telegram message-to-memory links; and task-to-memory context links.
- Added bounded lexical search and project-context retrieval. No embeddings, vector service, raw conversation transcript, model reasoning, provider credential, or uploaded file content is copied into the memory database.
- Corrections create a newer record and supersede the old one. Search and injected project context return only the current version. Forgetting deletes active content and its references and retains only a content-free tombstone with deletion provenance.
- Added deterministic Telegram memory phrases and commands: `/project`, `/remember`, `/memory`, `/memory-update`, and `/forget`; natural owner phrases cover project facts, preferences, decisions, searches, saving a completed task reference, corrections, and forgetting a memory through a reply to its result.
- Saving a task under a project stores only a short status/reference record and links to the original TaskStore task, its existing upload names, and retained artifact references. Original task/result/file records remain the authoritative source.
- Explicitly requested project context can be attached to a newly queued task using a named or reply-bound project. The snapshot is bounded, records source memory IDs, and is labeled untrusted reference data in the OpenCode prompt. It does not stage files, authorize a run, or change the selected tool profile.
- Memory update rows are keyed by Telegram update ID in the same SQLite transaction as each write/correction/deletion, so redelivery does not duplicate the memory mutation. The existing controller owner/private-chat authorization runs before memory handling; upload messages continue through the file handler and do not enter the memory command path.

## Local verification

| Check | Result |
|---|---|
| Personal-agent regression suite | PASS — `.venv/bin/python -m pytest tests/personal_agent -q --tb=short`; 151 passed, 7 skipped |
| Memory/controller follow-up suite after provenance-display adjustment | PASS — `.venv/bin/python -m pytest tests/personal_agent/test_memory_store.py tests/personal_agent/test_telegram_controller.py -q --tb=short`; 38 passed |
| Ruff on changed Python files | PASS — `.venv/bin/ruff check nanobot/personal_agent/memory_store.py nanobot/personal_agent/task_store.py nanobot/personal_agent/opencode_task.py nanobot/personal_agent/telegram_controller.py tests/personal_agent/test_memory_store.py tests/personal_agent/test_task_store.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_telegram_controller.py` |
| BasedPyright on changed Python files | PASS — `.venv/bin/basedpyright` with the same changed module/test paths; 0 errors, 0 warnings, 0 notes |
| Patch whitespace | PASS — `git diff --check` |
| Controller deployment and restart | PASS — restarted only the identified poller with SIGINT and `uv run --no-sync python -m nanobot.personal_agent.telegram_controller`; polling connected successfully after both restarts |
| Durable project fact and provenance | PASS locally — SQLite reopen preserves owner-authored project memory and search provenance |
| Correction precedence | PASS locally — corrected record supersedes prior value and current retrieval excludes the old value |
| Forget and retrieval exclusion | PASS locally — content and task/file links removed, tombstone retained, repeated forget is idempotent |
| Task result/file reference | PASS locally — task and retained file/artifact references are linked without copying model result text |
| Telegram authorization and upload boundary | PASS locally — unauthorized user/group cannot read or write; upload content/caption does not write trusted memory |
| Duplicate Telegram update | PASS locally — duplicate update ID does not create another project/fact/correction/deletion |
| Project context association | PASS locally — selected memory snapshot is persisted with queued task and does not dispatch it |

## Live Telegram acceptance

| Check | Result |
|---|---|
| Live owner project fact | PASS — owner sent “Remember that project mechanochemistry uses FEM and MD.” Telegram acknowledged memory `memory-23a2bb328dfdebb6ff32c1fd` under project `mechanochemistry`. |
| Host persistence and privacy | PASS — after controller restart, the record remained active with content `Uses FEM and MD`, source `telegram_owner`, Telegram update provenance, and no supersession/deletion; `memory.sqlite3` mode was `0600`. |
| Telegram retrieval after restart | PASS — owner asked “What do you remember about project mechanochemistry?” and received the saved fact with project, memory ID, and source provenance. |
| Saving a task result under a project | PASS — owner replied to completed task `task-cbd05c7a88e40fa15f4834d9e64e2667` with “Save this result under my mechanochemistry project.” Toothless created memory `memory-a30271714f52a7a8b6a11ec1`; original TaskStore result/files remain authoritative. |
| Natural task/file retrieval | PASS — “Find my last CSV analysis” returned memory `memory-a30271714f52a7a8b6a11ec1`, task `task-cbd05c7a88e40fa15f4834d9e64e2667`, and retained files `number.csv`, `chart.png`, and `summary.csv`. |
| Live correction and precedence | PASS — `/memory-update memory-23a2bb328dfdebb6ff32c1fd | Uses FEM and QM/MD` created `memory-1fc102b44a35ee7a96568e0f` and marked the original superseded. A later owner Telegram project query returned `Uses FEM and QM/MD` and did not return the old `Uses FEM and MD` value. |
| Live forget and retrieval exclusion | PASS — owner sent `/forget memory-1fc102b44a35ee7a96568e0f`; Toothless confirmed deletion. A subsequent project query omitted `Uses FEM and QM/MD` while retaining task `task-cbd05c7a88e40fa15f4834d9e64e2667` and its `chart.png`, `summary.csv`, and `number.csv` references. Host SQLite inspection confirmed the forgotten fact is absent from active rows and only a content-free tombstone remains. |
| Direct unauthorized-account live check | NOT RUN — no second owner-controlled Telegram account was used; deterministic authorization tests PASS. |
| Actual Bot API duplicate-update replay | NOT RUN — local update-ID idempotency tests PASS. |

The live core memory lifecycle is complete. Direct unauthorized-account interaction and actual Bot API duplicate-update replay were not performed; deterministic tests verify unauthorized user/group requests cannot read or write memory, uploaded document content/captions do not create trusted memory, and duplicate update IDs do not duplicate mutations.

## Security and limitations

Memory reads are owner-only and do not authorize worker execution. New tasks still require the existing explicit run approval; browser-only and workspace profiles remain separate; memory is never exposed as a model tool. Project context is attached only when the owner explicitly names or reply-binds a project, and is presented as untrusted reference data. The simple token-overlap search is intentionally bounded and may miss paraphrases; ambiguous project matches ask for clarification. Forgetting a task-reference memory removes the memory's links but does not delete the original task or its retained files. The live unauthorized-account check remains NOT RUN because no second owner-controlled Telegram account was used for this phase.

## Final gate

Phase 05.6 is VERIFIED for the documented owner-only deployment profile: real Telegram creation, restart retrieval, task/file association, natural reference retrieval, correction precedence, and deletion/exclusion passed. Unauthorized sender, upload-boundary, and duplicate-update safeguards passed deterministic tests; live second-account and Bot API replay checks remain unperformed. No scheduling, unattended execution, publishing, or memory-driven execution authorization was added.
