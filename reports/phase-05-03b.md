# Phase 05.3B — Secure OpenCode Browser Integration

Date: 2026-11-17 (America/Los_Angeles)

Status: VERIFIED for the reviewed Phase 05.3B browser task profile and current deployment. The real Example Domain and official Python tasks completed through owner-approved Telegram runs with actual browser tools, stored host results, and Telegram delivery. Restart recovery, duplicate-run protection, process cleanup, and worker cancellation passed. Cancellation stopped an active browser-profile run before its first browser tool call; in-flight Chromium navigation cancellation remains unverified. The browser is still restricted to public HTTP(S) GET/HEAD workflows. Public egress and DNS-name exfiltration remain possible; this is not general egress isolation.

## Implementation

The guest now has a separate OpenCode browser profile using the exact model `opencode-go/glm-5.3` and only the reviewed `toothless_browser_*` tools. It has no shell, file-editing, plugin, skill, subagent, or unrelated MCP tools. Browser requests run as the rootless `toothless-agent` identity inside a transient systemd scope with a 1536 MiB memory cap, 128-task cap, and 200% CPU quota. The root-owned scope policy allows only the current guest DNS stubs and denies prohibited IPv4 ranges and all IPv6 for the OpenCode process tree, including Playwright and Chromium. The helper uses Playwright 1.65.2 and Chromium 159.0.8154.6 ARM64.

The stdio helper supports public HTTP(S) navigation, rendered text/title/final URL, links, standalone non-form buttons, screenshots, bounded public downloads, and session close. It rejects non-HTTP(S), non-public destinations, and non-GET/HEAD requests; checks redirects and routed requests; limits page text to 32 KiB, screenshots to 2 MiB, downloads to 10 MiB, and page navigation to 20 seconds. Chromium downloads and remote debugging are disabled. Contexts close on helper exit, and runner cancellation/timeout stops the transient scope.

The host classifies browser tasks and selects the browser-only profile. The existing explicit Telegram `/run` approval, task records, worker dispatch, cancellation, and reconciliation path remain in use. The guest install script verifies the effective profiles and configuration hashes. The final authorized installer run exited 0 and verified the model and bounded rootless tool policy for both the existing workspace profile and the browser-only profile. No worker baseline, Softnet policy, Mac firewall, host credentials, or host mounts were changed.

## Security and browser tests

| Check | Result | Evidence |
| --- | --- | --- |
| Effective model and browser-only tool allowlist | PASS | Protected guest installer verified OpenCode CLI 2.3.0, model, and effective profile configuration. |
| Browser identity and no model sudo | PASS | Guest check reported UID 999, only the `toothless-agent` primary group, no sudo, and no access to the root-owned network policy or cgroup controls. |
| IPv4 loopback/private/CGNAT/LAN/link-local/metadata/multicast and IPv6 ULA/link-local/mapped IPv4 denial | PASS | Owned listeners were independently reachable before filtering and timed out inside the production-equivalent systemd scope. Fixture addresses/listeners were removed; guest loopback returned to `127.0.0.1/8` and `::1/128`. |
| Public DNS and HTTPS under the same network filter | PASS | Public DNS and `https://example.com/` succeeded inside the filtered rootless scope. |
| DNS rebinding simulation | PASS | Browser resolver mapping targeted an owned `169.254.169.254` listener after the helper's public-answer validation; filtered navigation timed out and the listener received no request. The fixture was cleaned up. |
| Redirect to owned metadata-shaped destination | PASS | A controlled owned redirect was observed; the filtered browser request timed out. No real metadata service was contacted. |
| Real public browser render | PASS | Chromium rendered `https://example.com/`; title `Example Domain` and expected body text were observed. |
| JavaScript and click, public link click | PASS | In-memory owned page changed text after a button click; a normal public link click loaded Example Domain. |
| Screenshot | PASS | Valid PNG saved at `/home/toothless-agent/workspace/browser-artifacts/phase053b-runtime.png` (74,622 bytes) and retrieved via the authenticated worker file interface. |
| Browser process cleanup | PASS | Helper close left no matching Chromium process; task scope cleanup succeeded. |
| OpenCode cancellation during an active browser task | NOT RUN | No live Telegram browser task has been approved/dispatched yet. Scope cancellation is implemented and runner cancellation tests are covered locally. |
| Browser crash recovery | NOT RUN | No dedicated real-browser crash injection was run. |
| Host/private destination tests from browser helper | PASS | Direct private URL rejection and filtered redirect/rebinding fixtures passed. |
| First official Python research task through Telegram | FAIL | Task `task-f8ad70ecd4b04c3f7ced9c0c9b46401e` was explicitly run as execution `opencode-7ed4d406e7684b18b06a091252d3fd6e`. The guest runner returned `{"status":"timed_out","error":"OpenCode timed out."}` at 120 seconds. Host state is `failed`; no answer or source URL was produced. |
| Second official Python research task through Telegram | FAIL | Task `task-82cb36bd442b5a98884c6362348ceb8a` was verified queued, then explicitly run as execution `opencode-3f542d406c604a05add77663a72479a1`. The guest returned `{"status":"failed","text":"","error":"Transport: ETIMEDOUT: Unable to connect. Is the computer able to access the url?","event_count":1,"tools":[],"model":"opencode-go/glm-5.3","duration_seconds":135.145}`. The host task is `failed`; zero browser tools were recorded, so the official site was not verified as opened. |
| Restart recovery, duplicate `/run`, Telegram answer/source verification | NOT RUN | Both attempts failed without a successful result to recover. No duplicate run was attempted. |
| Browser task timeout adjustment | PASS | Browser-only runs have a 300-second guest bound and a 315-second worker request/reconciliation bound. Non-browser tasks retain the existing 120-second guest/150-second host bounds. Unit regression covers the selected browser timeout. The updated runner was installed and effective browser/worker profiles reverified. The second failure occurred before the 300-second deadline and had no browser tool event; increasing the limit did not resolve the transport failure. |
| Browser process cleanup after second failure | PASS with note | No `chrome-headless-shell` process or `toothless-browser-*` transient scope remained. A separate `/usr/local/bin/opencode serve --service` process was found in the guest `admin` login scope, outside the task's `toothless-agent` browser scope; it was not modified because it was not owned by this task. |

The live OS enforcement is destination-based for processes in the isolated scope, so model-controlled tools cannot change the root-owned filter or route to private destinations by using another protocol, redirect, or changed DNS answer. This is scoped browser-profile isolation, not general egress isolation: public IPv4 egress remains available to OpenCode and Chromium, public HTTP/DNS can exfiltrate task data, and DNS-name exfiltration remains possible. The DNS stub IP exception is IP-based rather than port-based. The guest-local OpenCode Go credential remains readable to processes running as the same guest identity. GET requests can have server-side effects; public research must avoid actions. No logins, form submissions, purchases, publishing, or destructive actions are supported.

The installed helper and config are on the active worker and have not been sealed into a new baseline. A reset may remove them; start only through `ops/worker/start.sh`, then reapply the reviewed install procedure if required. Direct Tart startup remains unprotected. Telegram document delivery is not implemented; artifacts remain available through the authenticated worker file interface.

## Regression checks

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest tests/personal_agent/test_opencode_task.py tests/personal_agent/test_opencode_guest_policy.py tests/personal_agent/test_browser_mcp.py -q` | PASS — 20 passed, including the browser-specific timeout. |
| `.venv/bin/ruff check nanobot/personal_agent/opencode_task.py ops/worker/opencode_task_runner.py ops/worker/browser_mcp.py ops/worker/browser_boundary_probe.py ops/worker/browser_runtime_probe.py ops/worker/browser_rebinding_probe.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_browser_mcp.py` | PASS. |
| `.venv/bin/basedpyright nanobot/personal_agent/opencode_task.py ops/worker/opencode_task_runner.py ops/worker/browser_mcp.py ops/worker/browser_boundary_probe.py ops/worker/browser_runtime_probe.py ops/worker/browser_rebinding_probe.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_browser_mcp.py` | PASS — 0 errors, 0 warnings. |
| `.venv/bin/python -m scripts.check_import_boundaries` | PASS — 13 rules, 218 files. |
| `ops/worker/test-static.sh` | PASS. |
| `bash -n ops/worker/install-opencode.sh ops/worker/guest_install_opencode.sh` | PASS. |
| `git diff --check` | PASS. |

Pytest emitted the existing `pytest-asyncio` unset loop-scope deprecation warning; tests passed.

## Phase 05.3B.1 transport diagnosis (2026-11-17)

Status: ROOT CAUSE CONFIRMED; OPERATOR-APPROVED FIXES DEPLOYED; FRESH END-TO-END BROWSER TASK PENDING. Failed executions were not resubmitted.

The first failed task (`task-f8ad70ecd4b04c3f7ced9c0c9b46401e`, execution `opencode-7ed4d406e7684b18b06a091252d3fd6e`) ended at the 120-second guest deadline. The second (`task-82cb36bd442b5a98884c6362348ceb8a`, execution `opencode-3f542d406c604a05add77663a72479a1`) returned one sanitized `error` event classified as `Transport`, with `ETIMEDOUT`, no response text, and zero tool events after 135.145 seconds. The prior runner did not retain raw event payloads; its sanitized summary is the available event evidence. A parser improvement now preserves safe event type names and classifies transport timeouts as `network_error` for future runs.

The failure stage is **A: CLI startup/local control transport**, before provider authentication, provider HTTPS, model inference, MCP discovery, or browser invocation. With the exact production cgroup network policy and resource limits, a read-only standalone OpenCode command timed out. Its debug/strace evidence showed the CLI launching its private `opencode serve --stdio --port 0` child and attempting a connection to `127.0.0.1:<ephemeral-port>`; the existing browser scope denies loopback, so that client-to-child connection never completed. The same read-only command succeeded outside the filtered scope. This establishes the denied local OpenCode control connection as the cause; the `Transport` message itself did not identify the endpoint.

The remaining profile comparisons found no model or provider mismatch: both profiles resolve to `opencode-go/glm-5.3`; the browser profile registers only the seven `toothless_browser_*` tools, with plugins and other MCP servers absent. Both effective configurations passed the guest verifier. The rootless runner uses its own guest identity and HOME/XDG state. The Go catalog endpoint resolved and completed verified HTTPS with HTTP 200 inside the unchanged strict destination filter, so DNS and public HTTPS to the documented provider host are available. The browser MCP `tools/list` handshake and Chromium rendering of Example Domain also succeeded under the helper's unchanged strict filter. Therefore provider reachability and helper startup are not implicated in the observed pre-tool failure; authenticated model inference has not yet been tested after the diagnosis.

The unexpected `opencode serve --service` process was PID 5776, owned by guest `admin` (UID 1000), started in that user's login session and listening on `127.0.0.1:49374` within `session-91.scope`. It is separate from the rootless task runner's UID 999, private HOME/XDG, `--standalone` process and its ephemeral child-server port. It was not stopped or modified. No browser or task scope remained after the failed run; no matching Chromium process remained.

The narrow fix separates the OpenCode coordinator into its own bounded transient scope with the same private/LAN/link-local/metadata/IPv6 denies and one additional `127.0.0.1/32` allowance for its private local control connection. The systemd address filter is IP-based, not port-based, so that coordinator allowance covers loopback protocols/ports while the process runs. The Playwright/Chromium helper remains in a separate sibling scope using the original strict policy, which still denies all `127.0.0.0/8`; the fixed stdio-to-AF_UNIX bridge carries only bounded MCP messages between them. The operator approved this policy change and it is deployed in the current guest. No broad private-network access, Softnet change, host networking change, paid fallback, or credential transfer was introduced. The deployed guest files came from `ops/opencode/phase05/browser-control-network-policy.json`, `ops/opencode/phase05/browser.json`, `ops/worker/opencode_task_runner.py`, `ops/worker/browser_mcp.py`, `ops/worker/browser_mcp_bridge.py`, and the guest installer scripts.

Local verification of the candidate: 23 focused tests passed; Ruff passed; BasedPyright reported 0 errors/warnings; import boundaries passed (13 rules, 218 files); `bash -n` passed for both guest installer scripts; static worker safety checks passed; `git diff --check` passed. The same suite was rerun after deployment. These local checks do not prove guest deployment or live model inference.

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest tests/personal_agent/test_opencode_guest_policy.py tests/personal_agent/test_opencode_task.py tests/personal_agent/test_browser_mcp.py tests/personal_agent/test_browser_mcp_bridge.py -q` | PASS — 23 passed. Existing pytest-asyncio loop-scope deprecation warning only. |
| `.venv/bin/ruff check ops/worker/opencode_task_runner.py ops/worker/browser_mcp.py ops/worker/browser_mcp_bridge.py tests/personal_agent/test_opencode_guest_policy.py tests/personal_agent/test_browser_mcp_bridge.py` | PASS. |
| `.venv/bin/basedpyright ops/worker/opencode_task_runner.py ops/worker/browser_mcp.py ops/worker/browser_mcp_bridge.py tests/personal_agent/test_opencode_guest_policy.py tests/personal_agent/test_browser_mcp_bridge.py` | PASS — 0 errors, 0 warnings. |
| `.venv/bin/python -m scripts.check_import_boundaries` | PASS — 13 rules, 218 files. |
| `bash -n ops/worker/install-opencode.sh ops/worker/guest_install_opencode.sh` | PASS. |
| `ops/worker/test-static.sh` | PASS. |
| `git diff --check` | PASS. |

After the operator approved guest deployment, `ops/worker/install-opencode.sh` exited 0. It rechecked OpenCode 2.3.0 and both effective profiles; browser profile verification also checked the pinned strict helper policy and the separate coordinator policy. The candidate files were transferred by the existing installer over pinned-key SSH. No model inference was performed during installation.

Post-deployment checks through the host-owned worker interface and pinned-key SSH:

| Check | Result | Evidence |
| --- | --- | --- |
| OpenCode Go endpoint connectivity under coordinator scope | PASS | Execution `1c9f01b2c1fb42afb6d2df59c5217396`, exit 0: catalog endpoint returned HTTP 200, remote `104.18.10.66`, TLS verify result 0; response body discarded. No auth header or credential was used. |
| OpenCode CLI private-server handshake under coordinator scope | PASS | Same execution: `opencode session list --standalone --format json` completed with exit 0 and returned stored session metadata. This exercised the CLI-to-private-child connection with the deployed loopback allowance; it did not call the model. |
| MCP `initialize` and `tools/list` with no model request | PASS | Execution `d07637bbbdf34ce2b5d2f131e331cc17` completed the handshake and asserted exactly seven `toothless_browser_*` tools. The diagnostic then hit a bug in its final cleanup assertion (it referenced an attribute on the runner module rather than the browser module), so that execution exit code was 1 despite successful MCP/browser assertions. Its `finally` block did stop the helper. |
| Public browser page under the production helper/Chromium scope | PASS | Same execution: browser MCP `navigate` and `extract_text` returned title `Example Domain`, final URL `https://example.com/`, and the expected documentation-example text. `close_session` returned “Browser session closed.” The helper and bridge retained the original strict filter, including loopback denial. |
| Browser cleanup | PASS | Read-only cleanup execution `148b1ef14ae84805a210d7160fadd4a0`, exit 0: no Chromium process, temporary socket directory, or phase 05.3B.1 browser/bridge scope remained. An earlier scan accidentally matched its own diagnostic text; the final scan used a nonliteral process signature and passed. |
| Model inference with browser profile, no navigation | PASS | Owner approved `/run` for task `task-89b042938b9fd00c81e9838e2987a6a1`. Execution `opencode-2bc87e1a19d8485a959e96abcc1350ca` completed with exit 0 in 2.146 seconds using `opencode-go/glm-5.3`; structured events were `step_start`, `text`, `step_finish`, `tools` was empty, and response was exactly `READY`. Host task store persisted `succeeded`, approval time, execution association, and result; Telegram delivered `READY`. The prompt forbade any URL navigation and browser calls. |
| Cleanup after approved inference | PASS | Read-only check execution `2976c4222a69488884071bba0cbd78b4` found no browser helper/Chromium process, temporary browser runtime directory, or transient browser scope. |

Two diagnostic harness mistakes were corrected without changing guest policy: an initial connectivity command had malformed shell quoting and exited before the checks; an initial MCP harness import did not support the installed runner's extensionless filename and failed before launching the helper. The corrected connectivity and MCP runs above passed. No failed research task was resubmitted, no provider fallback was attempted, and no systemd policy or admin-owned service outside the approved candidate was changed.

## Live acceptance state before the corrected timeout task (historical)

At this point in the record, the direct helper and no-model MCP path had passed, while model-initiated Telegram browser execution remained outstanding. The later corrected-timeout acceptance below supersedes this interim state. Neither earlier failed research task was resubmitted.

## First model-initiated browser attempt and timeout correction

Task `task-c269652a7bf40b5a475fc287c3698a5c` was queued before approval and explicitly run once. Its saved request contained an inserted space in `https:// example.com.` and no correction instruction was recorded. It is terminal and was not resubmitted. The earlier run for that task, execution `opencode-09531d02235a4493808dbb20816900e6`, used only the `skill` tool; it did not invoke the browser.

A fresh task `task-c498b664f96ee3e275c4fc8a071c9473` was queued, verified unapproved, then explicitly approved. Execution `opencode-5312a7735e494449b1488180edfb367c` completed with model `opencode-go/glm-5.3` and recorded actual calls to `toothless_browser_navigate`, `toothless_browser_extract_text`, and `toothless_browser_close_session`. It did not return a successful task answer: OpenCode timed out each MCP tool request and the model returned an explanation without the requested facts. Therefore Telegram delivery of a failure explanation is not counted as acceptance.

Sanitized OpenCode log evidence at `2026-11-18T02:26:30Z` showed the `toothless_browser` server connected. Subsequent warnings were `failed to call MCP tool` / `Request timed out` followed by `mcp transport error` / `Received a response for an unknown message ID`. Those late responses contain actual helper results: `Example Domain`, final URL `https://example.com/`, and rendered first sentence `This domain is for use in documentation examples without needing permission.` This proves the helper and Chromium navigated and extracted content, but the OpenCode caller had already timed out and discarded the results. The task is not accepted because OpenCode did not receive and return those facts to Telegram.

Root cause confirmed: the browser MCP server timeout values in the effective configuration were `20`. OpenCode interprets these values in milliseconds; the official configuration examples use values such as `45000` for a 45-second startup bound and document defaults in seconds ([OpenCode MCP server timeouts](https://opencode.ai/docs/mcp-servers/)). Thus the runner had effectively configured 20 ms per startup, catalog, and execution request. The late matching responses explain the observed unknown-message-ID transport errors. Separately, the helper had returned names already prefixed with `toothless_browser_`, while OpenCode applies the MCP server prefix itself; this caused the earlier empty tool catalog and `skill`-only call. The helper now returns raw MCP names (`navigate`, `extract_text`, etc.), which OpenCode exposes as the permitted `toothless_browser_*` tools. Both issues are corrected without weakening policy.

The effective browser profile now sets each MCP timeout to 20,000 ms, preserving the already intended 20-second bound. The selected model, task deadlines, cgroup destination filters, Softnet policy, and browser navigation limits were not changed. `ops/worker/install-opencode.sh` exited 0 and the guest verifier passed after deployment. The strict-scope no-model MCP test passed using the corrected raw tool names, real Example Domain navigation and extraction, `close_session`, and cleanup. Regression: 25 focused tests passed; Ruff and BasedPyright passed; `git diff --check` passed.

| First corrected-profile Telegram attempt | Result |
| --- | --- |
| Queue-before-approval on fresh task | PASS — `task-c498b664f96ee3e275c4fc8a071c9473` was `queued`, had no approval timestamp and no execution ID. |
| Model-initiated browser tool calls | PASS — event tool list includes `toothless_browser_navigate`, `toothless_browser_extract_text`, and `toothless_browser_close_session`. |
| MCP bridge and browser navigation/extraction | PASS for execution path — late MCP responses include actual Example Domain title, URL, and first rendered sentence. |
| OpenCode received tool results and returned facts to Telegram | FAIL — MCP requests timed out at 20 ms and late responses were discarded. |
| Host task result and execution association | PASS as records; task result is a failure explanation, so content acceptance is FAIL. |
| Browser/helper cleanup | PASS — post-run guest audit found no Chromium process, runtime socket directory, or browser scope. |
| Controller restart and duplicate `/run` check | NOT RUN. |
| Live cancellation of a separate approved browser task | NOT RUN. |
| Official Python research task | NOT RUN; wait for a successful simple browser task. |

## Corrected MCP timeout live acceptance

After deploying the corrected raw MCP tool names and 20,000 ms MCP timeouts, owner-approved task `task-ecadc5587237f9894b0267b00b7cc87b` was verified in the host TaskStore as `queued`, with no approval timestamp or execution association, before the owner sent `/run`. It then reached `succeeded` as execution `opencode-05939a1953ca4ec7ac4b7a7f9b29fca2`; the host execution record reports exit code 0, duration 7.575 seconds, and model `opencode-go/glm-5.3`.

OpenCode recorded 15 structured events including three `tool_use` events and the actual tool list `toothless_browser_navigate`, `toothless_browser_extract_text`, and `toothless_browser_close_session`. The persisted model result reports page title `Example Domain`, final URL `https://example.com/`, and the first rendered sentence `This domain is for use in documentation examples without needing permission.` The owner confirmed Telegram delivered this result. This establishes successful model-initiated MCP navigation, content extraction, result persistence, and Telegram delivery; the earlier failed task was not retried.

A read-only cleanup audit executed through the existing pinned-key SSH worker interface as execution `dab1e2bd816948ac999c9ba7390ba87d` and exited 0. It reported empty lists for Chromium/helper processes, `toothless-browser-*` scopes, and `/run/toothless-browser-*` runtime directories. The browser result's explicit `close_session` event and the independent cleanup audit both passed.

| Interim acceptance item before the final live run (superseded) | Result |
| --- | --- |
| Queue-before-approval | PASS — task was queued, unapproved, and had no execution ID before `/run`. |
| Real model-initiated browser tools | PASS — `navigate`, `extract_text`, and `close_session` recorded under the exact approved model. |
| Browser rendered-page evidence | PASS — task result contains the observed title, final URL, and first sentence; user confirmed the same answer arrived in Telegram. |
| Host persistence and terminal state | PASS — host TaskStore is `succeeded` and references exactly one worker execution. |
| Browser process/scope cleanup | PASS — close event plus independent pinned-key guest audit found no process, scope, or runtime directory. |
| Controller restart recovery and duplicate `/run` | NOT RUN at this interim point; completed in the final acceptance below. |
| Bounded cancellation of a separate browser task | NOT RUN. |
| Official Python website research | NOT RUN at this interim point; completed in the final acceptance below. |

The later live acceptance below completed restart recovery, duplicate-run protection, official-site research, and cancellation checks. Neither failed execution was resubmitted.

## Final live acceptance (2026-11-18)

The controller was restarted without restarting or resetting the Ubuntu VM. The documented `uv run --no-sync python -m nanobot.personal_agent.telegram_controller` attempt could not access the host uv cache under the app sandbox, so the same project virtual-environment interpreter was launched directly with `.venv/bin/python -m nanobot.personal_agent.telegram_controller`. Its log confirmed `Telegram polling is connected`. The owner commands `/task task-ecadc5587237f9894b0267b00b7cc87b`, `/tasks`, `/ping`, and duplicate `/run` were handled; the owner confirmed the duplicate reply was `Task task-ecadc5587237f9894b0267b00b7cc87b is succeeded; it was not submitted again.` The TaskStore still contains the original result and exactly one execution ID, `opencode-05939a1953ca4ec7ac4b7a7f9b29fca2`.

The required new official Python task `task-1763475eb0cbe5d319e8e36fed5951fb` was confirmed queued, unapproved, and without an execution ID before `/run`. After owner approval, it succeeded as execution `opencode-162327ad302945ed94c7194624ac35cb`, exit 0, using `opencode-go/glm-5.3` in 23.166 seconds. Its structured result recorded 28 events, including actual `toothless_browser_navigate`, `toothless_browser_extract_text`, and `toothless_browser_close_session` tool calls. OpenCode reported visiting the Python homepage, downloads page, and release-notes page; the host persisted that answer and execution association, and the owner supplied the Telegram-delivered result.

The structured worker result persists distinct tool names and event types, not each raw tool argument. The exact URLs are present in OpenCode's saved answer and were independently checked against the official pages; the browser-only profile exposes no shell or fetch tool that could substitute for browser calls.

The browser result identified Python 3.15.2 as latest stable, dated October 25, 2026, and summarized the compromised eMudhra intermediate CA keys blocked by `ssl` and `tarfile.tar_filter` rejecting links that replace the destination path. Independent reads of the official [Python homepage](https://www.python.org/), [downloads page](https://www.python.org/downloads/), and [Python 3.15.2 release notes](https://www.python.org/downloads/release/python-3152/) confirmed the version, date, and both security changes. The release notes identify CVE-2026-115557 / gh-160040 for the certificate change and CVE-2026-114583 / gh-160097 for the tarfile change. These same three official URLs were present in the Telegram result.

A separate earlier owner task `task-6328cde8dbc998609e797c26e7851e2a` was not counted as browser acceptance: its execution `opencode-1e22027b7569488cb9f63b224c8f91a9` recorded only the `shell` tool, not browser tools. The specifically worded browser-only task above is the accepted research run.

Successful-run cleanup audit `e96ac9934e6a4241a896cad7fca1c77d`, executed via the existing pinned-key worker interface, exited 0 and returned empty lists for Chromium/helper processes, browser transient scopes, and `/run/toothless-browser-*` runtime directories.

The separate harmless cancellation task `task-9bd9eb79820795a32e10a10e7525e3a3` was verified queued and unapproved before dispatch. Its owner-approved execution was `opencode-0bcf463c15b64039a98883b93eaf20e5`; the owner immediately sent `/cancel`, received one confirmation (`Task ... was cancelled by the worker.`), and the host task and execution stores both record `cancelled` with exactly one execution association. The OpenCode process was terminated before it emitted a browser tool event (`event_count: 0`), so this verifies cancellation of an active browser-profile run and its helper scope, but not cancellation during Chromium navigation. Cleanup audit `8c9dea1f820748a6991f2c5aa24d70d5` exited 0 with no Chromium process, helper process, browser scope, or runtime directory. A separate process audit `5b48591210dc4e2d810d36251d9550af` found no remaining `toothless-agent` private `opencode serve --stdio` child. Cancellation command logging showed one `/cancel` handler; the owner reported one cancellation confirmation.

| Final acceptance item | Result |
| --- | --- |
| Controller restart and polling | PASS — controller restarted via project virtual-environment Python; Telegram polling connected; VM was not restarted. |
| `/ping`, `/task`, `/tasks` after restart | PASS — authorized commands were handled; host retained the succeeded task and original result. |
| Duplicate `/run` after restart | PASS — bot explicitly returned the existing succeeded state; the host retained exactly one execution ID. |
| Official Python research queued before approval | PASS — task `task-1763475eb0cbe5d319e8e36fed5951fb` had no approval or execution before `/run`. |
| Real official-site browser evidence | PASS — successful model result records navigation, rendered-text extraction, and close calls; official release sources independently confirm the cited version and changes. |
| Host persistence and Telegram delivery | PASS — one execution `opencode-162327ad302945ed94c7194624ac35cb`, succeeded; owner supplied the Telegram-delivered answer. |
| Successful-run browser cleanup | PASS — cleanup audit `e96ac9934e6a4241a896cad7fca1c77d` found no browser processes, scopes, or temporary runtime directories. |
| Live cancellation | PASS with limitation — task `task-9bd9eb79820795a32e10a10e7525e3a3`, execution `opencode-0bcf463c15b64039a98883b93eaf20e5`; worker confirmed cancellation and sent one notice. Cancel occurred before the first browser tool call, so in-flight Chromium navigation cancellation is NOT RUN. |
| Cancellation cleanup | PASS — audits found no browser/helper processes or scopes and no task-owned OpenCode stdio child. |
| Duplicate execution prevention for cancelled task | PASS — task has one associated execution ID; no resubmission occurred. |

Phase 05.3B is VERIFIED for this browser-only task profile and current deployment. It does not verify general network egress isolation, in-flight browser-navigation cancellation, account login, external effects, unattended scheduling, or future network configurations. Phase 05.4 was not started.
