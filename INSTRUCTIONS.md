# Mac Mini personal agent: OpenCode engine, implementation prompts, and test gates

<!-- Active product specification; supersedes the Codex-engine edition. -->

Updated November 16, 2026 · OpenCode primary-engine edition

## 1. Recommended decision — OpenCode is the primary agent engine

**Primary engine: [OpenCode](https://opencode.ai), using an explicitly selected permitted free model for the first prototype.** Use the official OpenCode CLI `opencode run` for bounded smoke tests and an OpenCode server/SDK only after verifying its current supported interface and security boundaries. OpenCode is free open-source agent software; hosted inference is a separate service. Models marked `Free` may be rate limited, changed, withdrawn, or given new pricing. Do not assume perpetual or unlimited availability.

**Product: Telegram → trusted Mac host controller (tasks, approvals, credentials) → isolated Linux VM running OpenCode and guest tools → validated artifacts returned to host → Telegram.** This places OpenCode's built-in shell, file edits, plugin code, and other agent-executed commands inside the Linux guest, rather than attempting to make a host-launched full agent delegate only some commands. The Mac may retain an interactive OpenCode installation for *developing* this repository, but the *deployed autonomous agent* runs in the worker VM. Keep those roles distinct.

**Important trust limitation:** If an OpenCode account/provider credential is stored in the VM and agent-executable code can read it, the credential is exposed to the worker trust domain. Use a separate low-privilege/free-only account where allowed, minimal account scope, non-readable credentials or a host-owned narrow provider relay if technically supported and verified. Do not claim the VM can protect secrets from its own root-capable agent. No host secrets, personal directory mounts, privileged host RPC or LAN access may be introduced to solve login issues. Host-enforced network rules must be independently tested.

**Cost policy:** Default to *zero authorized paid model spending*. Pick a specific currently free model using the exact provider/model ID shown by the installed OpenCode version, and enforce a controller-level allowlist. Reject unknown IDs, paid models, paid fallback, optional title/summarization models that incur cost, auto-upgrade of plan, and payment/credit purchasing. A selected model disappearing or becoming paid is a hard stop with an operator notification. A `Free` label is not a contract; audit provider/account billing settings and reported model metadata during verification. An OpenCode Console/Zen key or other provider credentials are not automatically free. No silent API key or alternate provider. Consider Codex CLI, Cursor CLI or Claude Code only as separately approved future adapters, not MVP dependencies.

**Framework decision:** Prefer a thin Python host controller plus one OpenCode runtime inside the guest. Nanobot can be selectively reused for Telegram/scheduling after review, but do not nest two autonomous agent loops. Preserve the original 16-phase plan, test gates, optional connectors, and host/worker separation. This is an implementation specification, not evidence that the Mac or provider billing has been verified.

## 2. Engine choices and adoption research

| Engine | Useful capability | Cost limitation | Role in this project |
| --- | --- | --- | --- |
| [OpenCode](https://opencode.ai) | Open-source terminal agent, interactive TUI, scripted run and server, multiple providers/models | The software is free; model hosting can be free, metered or subscription-backed | **Primary deployed engine inside VM** |
| [Codex CLI](https://github.com/openai/codex) | Official ChatGPT-account and agent orchestration integrations | Subscription limits; separate API billing if API key is used | Optional future engine adapter; host development tool can remain installed |
| [Cursor CLI](https://cursor.com/cli) | Multi-model coding agent and headless workflows | Available models and included usage depend on Cursor plan | Optional future adapter |
| [Claude Code](https://code.claude.com/docs) | Agentic software engineering runtime | Subscription and usage conditions apply | Optional future adapter |
| [Nanobot](https://github.com/HKUDS/nanobot) | Telegram and assistant components | Integration/maintenance work; separate agent loop | Optional channel components only |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent), [OpenClaw](https://github.com/openclaw/openclaw) | Mature personal-assistant capabilities | Security placement and integration effort require review | Alternatives, not MVP dependencies |

**Documented OpenCode interfaces:** [CLI](https://opencode.ai/docs/cli/), [server](https://opencode.ai/docs/server/), [configuration](https://opencode.ai/docs/config/), [providers](https://opencode.ai/docs/providers/), [models](https://opencode.ai/docs/models/), [permissions](https://opencode.ai/docs/permissions/). Confirm exact flags, JSON event schemas, versions, model IDs, and defaults against the installed release rather than copying untested syntax. Configuration may merge across system, user, and project; inspect the effective configuration and inherited environment before believing a model/provider is disabled.

**Architectural findings:**
1. OpenCode's tools execute in the environment running the agent. To avoid exposing the Mac host, run the autonomous OpenCode process inside a Linux VM rather than assuming a remote shell plugin moves all tool execution.
2. A virtualized guest and OpenCode's app-level tool permission settings are *not* substitutes for an externally enforced host/LAN firewall. Test guest root, IPv4, IPv6, DNS, mounts, forwarded credentials and alternate interfaces.
3. The built-in OpenCode free models may be temporary. Only an explicit allowlisted free provider/model may run, and billing must fail closed if price or availability changes. The operator must opt into any paid account/provider.
4. Model quality varies. Benchmark real tool-use and data tasks using GLM/MiMo/other currently free options as available, without hard-coding names from a screenshot into a long-lived configuration.
5. Persistence and resumption do not guarantee exactly-once execution of side effects. Maintain host-owned operation IDs, approvals and reconciliation.

Review the actual OpenCode upstream revision, project license, dependency locks and security advisories at Phase 00. Preserve third-party notices, and do not conflate permission prompts with a security boundary.

## 3. Architecture and ownership

```mermaid
flowchart TD
    U["Telegram and later dashboard"] <--> C["Trusted controller on Mac"]
    C <--> S["Host task DB, approvals, artifacts"]
    C --> P["Host policy and VM dispatcher"]
    P <--> W["Linux worker VM: OpenCode agent"]
    W --> T["Guest terminal, code, files, package installation"]
    W --> B["Guest browser and desktop"]
    W <--> F["Explicitly allowlisted free model provider"]
    H["Host watchdog and externally enforced network boundary"] --> W
```

The controller is deterministic: it receives Telegram instructions, owns task state, applies permissions and stops/restarts worker jobs. **OpenCode performs reasoning and execution inside the Linux VM**. Development instructions for coding the project (`instruction.md`) are distinct from deployment agent operating rules (`agent-instructions.md`). Never copy the developer's host home, login material, credentials or unrestricted CLI configuration into the guest. An OpenCode session's own history is not the authoritative task database.

| Component | Location / privilege |
| --- | --- |
| Telegram token, authoritative tasks, schedules, approvals, artifact store | Trusted Mac host |
| Development OpenCode installation and project build work | Interactive host operator workflow, not the deployed agent |
| Deployed OpenCode process, built-in tools, shell, browser, package installation | Linux worker VM only |
| Free model provider configuration and limited access token if required | Dedicated guest identity where possible; **guest credential exposure is an acknowledged risk** |
| Worker VM start/stop/reset, external firewall and policy controls | Fixed host service/operator operations, never general agent-controlled host shell |
| Outputs | Guest export staging → host validation → host storage/Telegram |

### Initial stack

- Python host controller, SQLite transactions and task/operation IDs, with narrow SSH or authenticated guest transport and bounded artifact collection. FastAPI only if needed.
- **OpenCode inside the VM**, first using `opencode run` with a verified explicit `provider/model` argument; consider an authenticated private `opencode serve` interface later. Never expose an unprotected OpenCode server to LAN/internet; avoid binding `0.0.0.0` by default.
- A currently supported Linux ARM64 distribution for Apple Silicon, or an appropriate guest for Intel, using Tart or UTM after hardware and license checks. Do not silently provision global firewall changes.
- Model options configured and allowlisted, never assumed from a screenshot. Always confirm the current label, cost, model availability, and real run behavior in Phase 00/05. Disable any implicit paid `small_model`, retries, model switching, or provider fallback.
- Offline fake OpenCode adapter/event fixtures to test orchestration deterministically. Real free-model checks are separate and may face rate limits or external service changes.
- One worker, one active task, bounded RAM/vCPUs/disk and time/tool/output budgets. Minimal watchdog and log redaction from the beginning.

Suggested starting VM allocation (not a benchmark): a 16 GB Mac can try ~4 GB guest RAM and 2–4 vCPUs; a 24/32 GB Mac can try 6–8 GB. Confirm actual headroom and guest workload first.

## 4. Complete feature map and delivery order

| ID | Features | User outcome | Release |
| --- | --- | --- | --- |
| 00 | OpenCode installation, free-model and billing safety proof of concept; version pin, baseline checks | Verified free-model-first foundation | Foundation |
| 01 | VM, host/LAN isolation, clean image, stop/reset | Own isolated working computer | Foundation |
| 02 | Remote commands, processes, files, guest package installation | Agent can actually do work | Foundation |
| 03 | Private Telegram, attachments, commands, deduplication | Message it from anywhere | MVP |
| 04 | Durable tasks, steering, cancellation, progress, recovery | Long jobs remain controllable | MVP |
| 05 | OpenCode sessions, tool confinement, policy dispatch, free-model and quota limits | Controlled free-model autonomous execution | MVP |
| 06 | CSV analysis, reproducible scripts, validated exports, file delivery | First useful research workflow | MVP |
| 07 | Public research, browser interaction, downloads, source records | Work across websites and files | Useful assistant |
| 08 | Editable memory, project context, reusable versioned workflows | Less repeated instruction | Useful assistant |
| 09 | Schedules, event triggers, quiet hours, deduplicated notifications | Recurring work and proactive updates | Useful assistant |
| 10 | Scoped connectors, approvals and effect reconciliation | Email/calendar or other accounts | Expanded access |
| 11 | Responsive dashboard, task stream, files, approvals and usage | Custom app experience | Expanded interface |
| 12 | Voice notes, optional replies, VM desktop control and handoff | Natural interaction and GUI apps | Optional |
| 13 | Startup services, health, backups, restore, retention and upgrades | Dependable home appliance | Before unattended use |
| 14 | Independent evaluation pack, adversarial inputs, fault injection | Evidence the system works | Every release |
| 15 | Multiple isolated workers, bounded delegation | Parallel work when justified | Later |

Build order: 00 (including OpenCode free-model proof of concept) → 01 → 02 → 03 → 04 → 05 → 06. Add a minimal watchdog and logs during those phases. Finish 13 and the applicable 14 checks before unattended deployment. Then add 07–12 as needed. Phase 15 comes last.

## 5. How to use the prompts

Give the **development assistant on the host** this master prompt once, then one phase prompt at a time. The host's existing Codex or OpenCode installation may develop the project; that is distinct from the deployed OpenCode in the guest. Record actual evidence in `reports/`, `docs/status.md` and an operator checklist. An unrun test is NOT RUN, not PASS.

### Master prompt

```text
You are implementing my personal AI assistant on a Mac Mini. Read instruction.md
and follow its OpenCode-first architecture. Build only the assigned phase.

Deployed runtime: OpenCode inside an isolated Linux worker VM with a specifically
allowlisted free provider/model. Trusted host: Telegram, approvals, task database,
fixed worker lifecycle controls, artifact validation and task scheduling.
Keep development assistant and deployed runtime separate. Avoid two agent loops.

NO paid inference by default. Never silently add API-key billing, credits, paid
model fallbacks, auto-paid small models, or automatic spending. If model
availability/pricing cannot be verified, STOP and ask the operator. Free labels
may change. Document current approved model ID, evidence and quota limits.

No generic agent-accessible host shell, user-home mounts, SSH-agent forwarding,
clipboard, host Docker socket, or personal browser profile. The guest can break
its own installation; enforce boundaries on the host/gateway and actually test.
Guest-owned provider credentials are at risk from guest execution; never claim
otherwise. Use official current OpenCode CLI/server APIs, not invented flags.

Reuse working code and official extension points; keep typed contracts, bounded
outputs, operation IDs, host-held authoritative state, logs and secret redaction.
Run deterministic tests, integration checks and available real guest checks.
Manual browser acceptance rather than Playwright/browser UI test suites unless
I explicitly request those. Do not silently change host firewall/network rules.

End with changed behavior, files, exact commands and PASS/FAIL/NOT RUN results,
blockers, manual demo and reports/phase-XX.md. Never proceed without approval.
```

## 6. Phase-by-phase implementation and test prompts

### 00 — Foundation and OpenCode free-engine proof of concept

Features: inspect existing OpenCode installation, pin version/config, verify models/prices, exercise read-only CLI tasks and capture structured output. **Gate:** one real explicitly free-model task works without any paid API key, and the model selection and billing configuration are reviewed.

**Implementation prompt**

```text
Implement phase 00 ONLY. Read instruction.md and inspect the current repository.
Confirm installed OpenCode version (`opencode --version`), CLI flags via
`opencode run --help`, available models via documented CLI/TUI, effective user and
project config, inherited provider environment, and authentication status without
printing any token. If OpenCode exists on the host as a development tool, do not
copy its credentials into the VM or assume it is the deployed runtime.

Select one *currently* free provider/model from the real installed model catalog;
record its exact ID, official price/availability evidence, date, rate/usage limits
and privacy considerations. Use a separate account if practical. Do not auto-add
billing information or purchase OpenCode Zen credits. A free model is not guaranteed
permanently. Explicitly constrain model selection and secondary/small models;
reject paid or unknown providers at the trusted controller rather than relying
on a prompt. Remove or mask inherited paid-provider credentials for this test.

In a disposable, non-sensitive directory, run one harmless task via the documented
`opencode run` CLI with explicit model ID, preferably JSON events if supported.
Capture event stream/schema, result, exit status, latency, usage when reported,
errors and timeout behavior. No uncontrolled host file/shell test. On first POC,
use a read-only tool profile or no executable tools if supported. A successful
prompt does NOT prove host isolation; Phase 01 is still mandatory.

Write a minimal fake OpenCode execution adapter, inspect the official server and
SDK options for future sessions/streaming/interrupt/approval, record the exact
OpenCode revision, dependency lock, license/notice and upstream risk review.
Keep Nanobot optional for Telegram plumbing only. Create/update docs/architecture.md,
docs/features.md, docs/status.md and reports/phase-00.md. Do not provision the VM.
```

**Test prompt**

```text
Verify phase 00: installed version and supported flags; explicitly selected
free model; actual response and event parser; no provider token leaked; no paid
model selected; no unauthorized files changed. Simulate model removal, rate limit,
missing auth, malformed stream, timeout and network outage. Verify every case
stops or returns a clearly typed error, never falls back to a paid provider.
Distinguish genuine provider observations from fake traces. Record PASS/FAIL/NOT
RUN. Block the next phase if a paid default or unknown billing path remains.
```

**Operator smoke test** (verify flags against installed help before running):

```bash
opencode --version
opencode run --help
# Verify the exact provider/model identifier in your current OpenCode model list.
# In a disposable directory, replace the placeholder with that verified FREE ID:
mkdir -p /tmp/mac-mini-opencode-poc && cd /tmp/mac-mini-opencode-poc
opencode run --model 'PROVIDER/VERIFIED_FREE_MODEL' 'Reply with exactly READY. Do not use tools.'
```

Do not confuse a free CLI binary with free frontier-model inference.

### 01 — Worker VM and externally enforced isolation

Features: dedicated guest, installation freedom, no host files, controlled networking, immutable base image and emergency stop. Gate: real deployment boundary checks pass before agent code runs unrestricted.

**Implementation prompt**

```text
Implement phase 01. Detect chip, RAM, macOS and available disk on the deployment
Mac; if this runtime is not that Mac, generate the discovery command and keep
hardware facts unresolved. Select a supported UTM or Tart path with a native-
architecture Ubuntu guest. Pin images/checksums and review the selected license.
Create idempotent provisioning, bounded CPU/RAM/disk, a clean powered-off baseline,
backup/clone/reset procedure and host-controlled emergency stop.

No host directory sharing, clipboard, personal profile, host SSH-agent forwarding,
host Docker socket or guest credentials authorizing host login. The guest may
have administrator privileges. Therefore guest firewall rules are not containment.
Implement a host/gateway-enforced boundary appropriate to the actual VM backend:
block guest-initiated host/LAN/private/link-local/multicast/metadata access over
IPv4 and IPv6 except narrowly necessary infrastructure. Allow public internet
and the explicitly designed return traffic/control transport. Do not blanket-
allow the host as gateway. Account for DNS and virtual network packet paths.
If the chosen backend cannot enforce this, use a separate trusted egress gateway
or change the backend. Do not claim NAT is sufficient. Supply a reviewed operator
installation and rollback step; avoid blind global firewall changes.
```

**Test prompt**

```text
Verify phase 01 on the actual Mac using disposable fixtures and owned endpoints.
As root inside the worker, read a dummy host-only marker, reach dummy listeners
on host/private LAN addresses, and test IPv6, link-local/metadata-shaped targets,
DNS rebinding/redirect paths and alternate network interfaces. Do not contact
unowned sensitive services. Verify forbidden paths fail and permitted package
downloads succeed. Disable guest firewall rules and repeat: containment must hold.
Inspect mounts, clipboard and forwarding. Install a guest package, change a file,
reset, and verify the clean baseline is restored. Test emergency stop independent
of guest cooperation and check CPU/memory/disk limits. If no real VM is available,
mark isolation NOT RUN and block unattended deployment rather than passing mocks.
```

### 02 — Remote execution and tool contracts

Features: install packages, run commands, manage long processes, read/write files, reliable remote execution IDs. Gate: all executable actions remain in the guest.

**Implementation prompt**

```text
Implement phase 02. Add a worker adapter to the upstream tool registry: execute,
poll, terminate, read, write, list and stat. Use authenticated host-to-worker SSH
with pinned host keys or an equivalently narrow transport. File/command requests
must use structured encoding; do not interpolate filenames or JSON into shells.
Only the explicit shell-command field may contain intentional guest shell syntax.
Persist execution IDs, task ID, start/end times, exit status and bounded stdout/
stderr. Support streamed/paginated output, timeouts, process-group termination,
working-directory selection and binary-safe file transfers. Guest administration
is allowed. No fallback to local host execution if the worker is unavailable.
Keep worker credentials narrowly scoped; disable SSH forwarding and reverse paths.
Document worker-root trust limits: journals are operational aids, not trusted policy.
Add host-owned dispatch records so connection loss can be reconciled by execution ID.
```

**Test prompt**

```text
Verify phase 02 with exit failures, Unicode/spaced filenames, literal shell
metacharacters in path fields, binary files, huge output, a hung process, and a
child-process tree. Test install/use of a small guest package and a long process
across reconnects. A worker outage must return unavailable, never execute locally.
Repeat an execution ID and verify its documented deduplication behavior. Lose the
connection after start and reconcile rather than rerunning blindly. Tamper with
guest journal results and confirm the controller does not grant new capabilities.
Check host/process markers to prove actual execution location. Write evidence.
```

### 03 — Telegram gateway and uploads

Features: one authorized owner, text and attachments, status and cancel commands, secure callbacks, deduplicated inbound events. Gate: unauthorized input never starts work.

**Implementation prompt**

```text
Implement phase 03 by extending upstream Telegram support. Use outbound long
polling initially; no public inbound router port. Authorize exact numeric owner
ID and private-chat ID on messages, uploads, edits and callback queries. Reject
group chats and other users by default. Keep bot token in trusted controller
secret configuration. Add /new, /tasks, /status, /cancel, /pause and /resume.
Persist inbound update IDs with the task creation transaction before advancing
the poll offset; tolerate replay and reconnect. Define how replies target tasks.
Download uploads to controller staging under generated names, enforce configurable
size/type limits, preserve original names only as metadata, then copy selected
bytes into the worker. Do not auto-extract archives. Handle long messages, delivery
rate limits, API errors and meaningful progress updates. Add an outbound outbox
and an explicit uncertain-delivery state; do not promise exactly-once Telegram
delivery after a lost API response. Document BotFather and owner-ID setup.
```

**Test prompt**

```text
Verify phase 03 using recorded synthetic updates and a fake Telegram API:
unauthorized sender, wrong chat, group, forged callback owner, replayed update,
out-of-order updates, restart between persistence and offset advance, edited
message and oversized/path-traversal attachment names. Verify one logical task
per accepted inbound event and no model calls for rejected input. Simulate 429,
timeout and upload failure. Confirm tokens do not appear in logs or request URLs
shown to users. Provide a real private-bot manual smoke test: send a text and CSV,
request status, cancel, and receive a file. Mark it NOT RUN without credentials.
```

### 04 — Durable tasks, steering and cancellation

Features: queued jobs, task history, bounded retries, new instructions during work, interrupted-task recovery. Gate: no silently duplicated work after restarts.

**Implementation prompt**

```text
Implement phase 04 as the authoritative task layer, integrated with existing
conversation state rather than replacing it. Model queued, running, waiting_user,
verifying, completed, failed, cancelling, cancelled and interrupted/unknown states.
Use SQLite migrations, transactions, worker leases/heartbeats, append-only task
events, operation records and an outbound outbox. One active task per worker.
New instructions target an explicit task and take effect at a safe tool boundary;
ambiguous instructions request clarification without silently changing another task.
Cancellation stops new dispatch, terminates the process group and reconciles any
in-flight effects. On restart, reconcile leases and worker execution IDs. A timed-
out external operation is unknown until checked. Persist completed artifacts and
decisions. Add status events with timestamps; avoid spam. Make concurrent update
and cancellation races explicit and recoverable.
```

**Test prompt**

```text
Verify phase 04 by killing the controller before/after task creation, dispatch,
worker start, result persistence and notification delivery. Test duplicate events,
two schedulers competing for one lease, worker crash and cancellation races.
Steer task A while B is queued and prove B is unchanged. Cancel a long process
with grandchildren; verify no further tool calls and report any effects already
completed. Recover a task without rerunning confirmed operations. An uncertain
external effect must remain unknown/waiting for reconciliation. Inspect database
state and operation traces independently; a plausible chat reply is not evidence.
```

### 05 — OpenCode autonomous sessions, policy, tool isolation, and cost controls

Features: OpenCode task sessions, event streaming, steering/cancel, safe tool execution and free-only model control. **Hard gate:** OpenCode agent code cannot run on the Mac host and cannot silently invoke paid inference.

**Implementation prompt**

```text
Implement Phase 05 using the tested OpenCode release and official CLI/server or
SDK interface from Phase 00. Run the DEPLOYED OpenCode in the Linux worker VM.
Do not host-launch an unrestricted agent subprocess merely because its shell
plugin points at the VM. Audit built-in bash, file edits, patches, browser,
plugins, MCP tools, delegated agents, network endpoints and inherited credentials.
Use a narrow host task/operation transport; prohibit host shell or arbitrary host
RPC. Host policy gates approvals, cancellation, export and budgets. Guest agent
permissions add defense in depth but are not the isolation boundary.

Set an explicit permitted model allowlist, enforce it before every task/session,
and validate any effective model and optional small_model. Prohibit automatic
switch to paid/unknown model, alternate provider, paid credits or metered key.
Handle rate limit, disappeared free model, auth expiry, pricing uncertainty and
network failure by pausing and notifying the user, never by switching silently.
If price cannot be independently established, fail closed. Keep guest secrets
minimal and document credential theft risk from arbitrary guest execution.

Support bounded runtime, messages, tool calls, outputs, cancellation, durable
operation IDs, result validation, safe artifact export and redacted logging. Treat
all documents, webpages, Telegram messages and tool output as untrusted data.
No capture of private model reasoning. Add fake OpenCode event fixtures for tests.
```

**Test prompt**

```text
Verify real worker-only command placement, no host file access, no host/LAN
reachability despite guest root; invalid tool requests, injected instructions,
malformed events, cancelled tasks and orphan processes. Simulate free-model
unavailability, price changes, rate limits and an unauthorized paid model ID;
confirm STOP/NOTIFY rather than fallback. Confirm no payment or additional
provider usage occurred. Run a bounded benign multi-step task inside the VM and
check returned artifacts independently. Record actual traces and PASS/FAIL/NOT
RUN; do not count fake tests as real VM isolation.
```

### 06 — First useful workflow: data to checked deliverables

Features: CSV analysis, plotting, reproducible scripts, report and file delivery. Gate: output values and provenance can be checked without trusting the model.

**Implementation prompt**

```text
Implement phase 06: Telegram CSV upload -> analysis inside worker -> verified
PNG/PDF, processed CSV, reproducible script and concise explanation. Provide a
versioned workflow template for time, E11, E22, E33 and G12 case comparisons.
Ask about ambiguous units, tensor-vs-engineering shear, averaging/weighting and
strain definition; never silently infer them. Use supplied metadata when clear.
Treat font size 20 as an editable project preference, not a universal constant.
Never calculate equivalent strain without a declared convention and valid inputs.
Add bounded artifact export: regular-file reads only, no symlink/path escapes,
no device/FIFO/blocking reads, size limits, checksums and controller-owned copies.
Prevent path replacement races with descriptor-based or equivalent safe reads;
do not trust guest manifests. Store input hashes, script hash, dependency versions,
units and method. Validate output before marking the task completed. Analysis
must not treat dataset text cells as instructions or execute uploaded scripts.
```

**Test prompt**

```text
Verify phase 06 using two synthetic case files. Case A: time_us=[0,1,2],
E11=[0,.1,.2], E22=[0,-.05,-.1], E33=[0,0,0], G12=[0,.02,.04]. Case B:
same times; E11=[0,.15,.3], E22=[0,-.1,-.2], E33=[0,0,0], G12=[0,.03,.06].
Declare all strains dimensionless and G12 engineering shear. Independently assert
A peak E11=.2, B=.3 and each time average equals its middle sample for these
linear histories. Re-run the exported script and compare numeric results.
Add missing columns, NaNs, duplicate times, nonmonotonic times, mismatched units,
malicious text and ambiguous shear. Invalid scientific assumptions must surface.
Attack export with symlinks, traversal, FIFO, oversized files and concurrent path
replacement. Manually inspect plots for labels/units/readability and verify real
Telegram delivery when available. Do not claim visual QA from file existence.
```

### 07 — Public research and browser operation

Features: search/fetch, sourced research, dynamic websites, downloads, screenshots and manual browser handoff. Gate: browser and model-written driver code are in the worker.

**Implementation prompt**

```text
Implement phase 07. Add public search/fetch through a selected documented provider
and browser tools for dynamic interaction. Use fetch when sufficient, browser
when interaction is necessary. Place Chromium, profiles, browser-driver code and
downloads inside the worker VM. Do not connect personal host browser profiles or
host CDP endpoints. Use fresh profiles initially. Keep source URL, retrieval time,
title and supporting excerpts for cited claims; report inaccessible sources.
Prevent fetch redirects and DNS changes from reaching denied host/private networks;
the external network boundary must apply to browser traffic too, not just HTTP tools.
Expose navigate/inspect/click/type/download/screenshot with time/action limits.
Add stop-and-handoff for authentication, CAPTCHA and unclear consequential actions.
Do not add account credentials or evasion features. Keep browser action traces and
screenshots task-scoped with retention controls.
```

**Test prompt**

```text
Verify phase 07 using an owned fixture site with delayed content, a download,
redirects, malicious embedded instructions and an unavailable page. Test adapter
contracts and network policy with backend fixtures; provide manual browser
acceptance steps rather than an automated UI suite. Confirm browser process and
driver execution markers exist only in the VM. Check a public research answer
against cited primary pages manually: each central claim needs support. Test
blocked private-address redirects, worker outage and download limits. CAPTCHA/
login must yield a clear handoff. Document fixture exceptions to the network
policy and remove them before deployment.
```

### 08 — Memory, project context and reusable workflows

Features: explicit remember/forget, provenance, project isolation, saved procedures and controlled improvement. Gate: preferences persist and can be corrected/deleted.

**Implementation prompt**

```text
Implement phase 08 using upstream memory where it satisfies the contract. Store
user-confirmed preferences, project-scoped facts and versioned workflow recipes
separately from transient model guesses. Each entry needs source, scope, dates
and revision. Expose list/edit/forget/export and a record of memory actually used
for a task. Begin with ordinary structured/text retrieval; avoid adding a vector
database without demonstrated need. Resolve conflicting instructions by current
user direction and provenance, not whichever result ranks highest.
Saved workflows specify inputs, outputs, required capabilities and verification.
The agent may propose recipe improvements, but cannot rewrite controller policy,
install a host plugin or grant itself permissions. Secrets never become memory.
Document deletion from current stores versus residual backup retention.
```

**Test prompt**

```text
Verify phase 08 by saving font-size preference 20, starting a new task, correcting
it to 18 for one project and forgetting it. Confirm scope, provenance and retrieval
change as intended without leaking another project's context. Restart the service
and repeat. Inject a web page instructing the agent to save a fabricated preference
or secret; it must not become trusted user memory. Version a workflow and run the
previous revision reproducibly. Attempt a workflow edit that adds host access;
policy must deny it regardless of stored instructions. Verify export and retention
semantics, including documented backup limitations.
```

### 09 — Recurring tasks, triggers and notifications

Features: natural-language schedules, timezone, missed-run policy, event deduplication, quiet hours and useful updates. Gate: recurring work is durable and bounded.

**Implementation prompt**

```text
Implement phase 09 around the authoritative task service, reusing upstream cron
only if it supports the requirements or can be adapted. Store IANA timezone,
normalized schedule, next run, capabilities, budget, overlap/misfire policy and
delivery target. Preview interpreted time in natural language. Use America/
Los_Angeles as the initial editable user timezone. Handle daylight saving with
an explicit documented policy. Create/list/update/pause/resume/delete/run-now.
Use transactional occurrence IDs to avoid duplicate task creation after restart.
Add one optional trigger type: controller-managed inbox directory or authenticated
webhook, with dedup keys and no executable payloads. Do not watch arbitrary Mac
folders. Add quiet hours, progress coalescing, rate limits and urgent-blocker rules.
No constant LLM heartbeat when no work is due. Record unknown notification outcomes.
```

**Test prompt**

```text
Verify phase 09 using a fake clock for DST skipped/repeated hours, timezone changes,
restart after missed runs, two scheduler instances, overlap, paused schedules,
deleted schedules and duplicate trigger events. One occurrence must produce one
logical task. Prove task budgets/capabilities still apply. Verify quiet hours and
coalescing without losing completion records; distinguish a queued notification
from confirmed delivery. Run a one-minute real scheduled smoke in a private test
bot only when configured, then remove that schedule. Do not create persistent
live automations merely to satisfy a mock test.
```

### 10 — Protected connectors and approval transactions

Features: optional read-only account access, drafts, exact-action approval and reconciliation. Gate: worker code cannot bypass a denied action.

**Implementation prompt**

```text
Implement phase 10 with a connector interface and fake email/calendar service
first. Real OAuth is an optional operator setup step with minimal scopes. Keep
credentials in the controller and never put equivalent authenticated sessions in
the worker browser. Expose separately scoped read, draft and external-write actions.
For gated effects, store the canonical action payload, owner, capability, expiry,
payload hash and nonce. Approval must authorize exactly that payload once; edits
invalidate it. Record requested/approved/executing/succeeded/failed/unknown states.
Use provider idempotency keys where available and check remote state after uncertain
responses. Never promise exactly-once behavior from a provider lacking the required
semantics. Add revoke/disconnect and a visible account-access inventory. Do not
send real email, book events or spend money as an automatic verification step.
```

**Test prompt**

```text
Verify phase 10 against fake services: wrong owner, expired approval, altered body,
changed recipient, replayed nonce, double-click, revocation and concurrent workers.
No write before authorization; one authorized logical operation afterward. Simulate
remote success followed by lost response: reconcile before retrying. If remote
state cannot be determined, mark unknown and ask rather than resending. From the
root-capable worker, attempt to read connector secrets and invoke hidden write
APIs; both must fail at the trusted boundary. Test logs/redaction and read-only
scope. Provide an optional manual real-account read-only check, not a live send.
```

### 11 — Custom dashboard and app backend

Features: mobile-friendly chat, tasks, event stream, files, screenshots, decisions, usage and controls. Gate: dashboard uses the same policy and task service as Telegram.

**Implementation prompt**

```text
Implement phase 11 as a responsive web app, reusing upstream WebUI where practical
or adding a thin client to our API. Show chat/task navigation, current progress,
tool-event summaries, artifacts, screenshot preview, approvals, schedules, memory,
usage and stop controls. Distinguish queued, running, unknown and completed states.
Use server events/WebSocket with reconnect and event IDs. Keep worker-generated
HTML and files inert by default; downloads/preview isolation must not execute
active content with dashboard authority. Use authenticated sessions, CSRF and
origin checks appropriate to transport; no API keys in the client. Bind locally
initially and document authenticated private remote access. Do not publish a
public dashboard. Keep the backend reusable for a later native phone app.
```

**Test prompt**

```text
Verify phase 11 with API authorization tests: unauthorized session, wrong task,
CSRF, cross-origin socket, replayed approval, reconnect/event ordering and expired
artifact access. Test hostile artifact HTML/filenames and confirm it cannot execute
in the app's origin. Run build/type checks. Provide a manual desktop/mobile checklist
covering task start, steering, cancel, file download, schedule edit and approval.
Inspect empty/error/loading/disconnected states. Do not mark manual checks complete
without observation and do not introduce an automated UI test suite.
```

### 12 — Voice and desktop interaction

Features: Telegram voice notes, optional audio replies, a visible Linux desktop and safe handoff. Gate: no host microphone/camera/desktop authority is added incidentally.

**Implementation prompt**

```text
Implement phase 12 as two independently disabled-by-default capabilities. Voice:
accept owner voice-note files, enforce duration/size limits, transcribe with a
configured provider, show the interpreted instruction when ambiguity matters,
and optionally synthesize replies. Include provider charges and privacy settings.
Audio conversion and model-written media commands run in the worker. Desktop:
add guest-only screenshot/accessibility/mouse/keyboard operations with stable
coordinates and display metadata. Support user takeover, resume and stop; serialize
human and agent control. Never grant host Accessibility/screen/mic/camera access
for a guest desktop feature. Protect remote desktop transport and keep it private.
Voice authorization is the Telegram owner identity, not a voiceprint. Do not let
an uncertain transcript grant new capabilities or approve a different payload.
```

**Test prompt**

```text
Verify phase 12 with synthetic audio, corrupt audio, long files, noisy/ambiguous
commands and text saying to ignore policy. Confirm transcripts do not authorize
new permissions and additional provider spend is counted. Manually operate a
harmless guest editor, capture a screenshot, take over, resume and cancel; verify
host desktop is untouched. Test changed display resolution, stale coordinates,
worker disconnect and simultaneous human input. Run adapter/API checks and provide
manual GUI steps, marking unobserved GUI/provider tests NOT RUN.
```

### 13 — Always-on operations, backup and upgrade

Features: startup, heartbeat, disk monitoring, encrypted backups, restore drills, retention, rollback and emergency stop. Gate: recovery works after a real interruption.

**Implementation prompt**

```text
Implement phase 13. Add operator-installed launchd service definitions and guest
services appropriate to the chosen VM stack. Do not assume a login agent starts
before login; document FileVault/unlock and GUI-runtime constraints. Add fixed host
watchdog controls independent of guest health reports, meaningful health endpoints,
log rotation/redaction, bounded artifact retention and disk-space limits. Back up
SQLite consistently with a supported backup method, plus controller config, task
metadata and artifacts. Encrypt backups; keep encryption keys outside worker and
separate from backup media. Document included/excluded credentials and VM data.
Implement restore to a disposable environment, an upgrade staging process with
pinned versions, migration backups and rollback compatibility. Updating guest
packages is agent freedom; updating controller/policy is an operator deployment.
Add minimal safe mode that refuses new work after policy or storage failure.
```

**Test prompt**

```text
Verify phase 13 with controller crash, guest crash, sleep/wake, internet outage,
disk pressure, corrupted backup and migration failure. Perform a real restore
drill into a disposable environment and verify task/artifact checksums and revoked
credentials behavior. Test emergency stop when guest ignores termination and ensure
watchdog does not restart a deliberately stopped unsafe worker. Check log secrets,
retention, backup exclusion and safe mode. Reboot the actual Mac and document
unlock/login steps and measured readiness; without that environment mark reboot
NOT RUN. Never disable FileVault or weaken host security to make startup pass.
```

### 14 — Independent evaluation and release gates

Features: task benchmark, observable success criteria, security regressions, fault injection and evidence reports. Gate: test results describe actual outcomes.

**Implementation prompt**

```text
Implement phase 14. Build an offline deterministic evaluation harness separate
from model-written task code and an opt-in capped live-model runner. Create 20
personal-assistant scenarios: 5 data/artifact, 4 research/browser, 4 task/recovery,
3 memory/schedule and 4 permissions/untrusted-input scenarios. Use owned fixtures
and fake external effects. Each fixture needs inputs, expected properties,
independent checker, permitted capabilities and failure definition. Borrow the
principle of environment-state checking from OSWorld and attack-plus-utility
measurement from AgentDojo; do not claim official benchmark scores unless running
their actual protocols. Log model/version, source SHA, tool/image versions, cost,
latency, interventions, task success, unsupported claims and blocked unsafe actions.
Include cross-feature failures, not only unit tests mirroring implementation.
```

**Test prompt**

```text
Audit phase 14 as a skeptical reviewer. Confirm the evaluator cannot be modified
by worker code and reads controller-owned output copies. Seed one incorrect output
and prove the checker fails. Run offline scenarios and repeat stochastic live
tasks at least three times if budget and environment allow; report sample counts
and denominators. Set proposed release criteria: all deterministic checks pass,
zero prohibited actions in the defined suite, at least 18/20 task scenarios succeed
per full live run, no silent duplicates in recovery tests, and cancellation stops
dispatch immediately with process termination within a configured 10-second target
or a documented VM-kill fallback. These are project targets, not general safety
guarantees. Report NOT RUN separately. Do not average a policy failure away.
```

### 15 — Optional parallel workers and delegation

Features: separate workers, bounded child tasks, parent tracking, file merge and shared budget. Gate: concurrency improves measured work without violating boundaries.

**Implementation prompt**

```text
Implement phase 15 only after one worker passes release gates. Add a pool of
separately isolated VMs or a VM-per-job mechanism, explicit parent/child tasks,
resource reservations and project-scoped inputs. A folder or separate browser
profile inside a root-controlled shared VM is not a cross-task security boundary.
Allow a parent to delegate bounded subtasks with capabilities no broader than
its own. Reserve child model/worker costs against the parent and global budgets.
Export results through the controller and merge with provenance/conflict handling.
Add cancellation propagation and cleanup. Start with at most two workers after
measuring Mac memory pressure; avoid unbounded recursive delegation.
```

**Test prompt**

```text
Verify phase 15 with two jobs using identical filenames and browser origins;
their files, cookies and secrets must remain separate. Cancel parent and confirm
children stop. Simulate one failed child, budget contention, worker exhaustion,
repeated result messages and conflicting artifacts. A child must not acquire
extra capabilities or escape global spending reservations. Compare the same
independent workload sequentially and concurrently, reporting wall time, cost,
peak memory and success. If parallelism does not improve the target workload,
leave it disabled by default and retain the simpler operating mode.
```

## 7. Verification contract and realistic limitations

Every phase report should contain a table with check, environment, command/manual action, expected outcome, observed outcome, evidence path and PASS/FAIL/NOT RUN. Keep implemented and deployed distinct. Record pre-existing upstream failures. Never weaken an assertion merely to get a green result.

Use deterministic tests for authorization, leases, serialization, scheduling, export and budgets. Use integration checks for real VM boundaries and process control. Use fault injection for restarts and unknown effects. Use manual UI acceptance for visual browser/dashboard/desktop flows. Use a capped, explicitly approved free-model OpenCode evaluation for autonomous task success (subject to availability and rate limits). These layers answer different questions.

The root-capable guest is disposable and untrusted. Host network containment must be validated against the actual virtualization path. Merely denying one HTTP URL, running a guest firewall or testing a file-path guard is insufficient. Conversely, a successful finite boundary test suite is evidence about those tests, not proof of immunity to hypervisor vulnerabilities or arbitrary prompt injection.

The controller can provide at-most-once logical dispatch using records and keys. Delivery systems and external services may still have unknown outcomes; reconciled status and explicit unknown states are more honest than a blanket exactly-once promise. Task cancellation cannot undo external effects already completed.

A free-model usage ledger is not a guarantee of future free service. Track selected provider/model, task counts, session duration, reported usage, rate limits and any cost metadata. If pricing is unknown or changes, STOP rather than risk a charge. No automatic credit purchasing, paid fallback or silent metered charging is permitted.

No paid LLM API dependency, expensive model training, Kubernetes, vector database, native mobile app or multi-agent hierarchy is required for the MVP. Add them only for a demonstrated feature or reliability requirement.

## 8. Practical first demo and milestone sizing

Start with a small synthetic CSV rather than real private research data. Message: “Compare these two cases, plot E11/E22/E33/G12 against time in microseconds, and send the plots, processed CSV and script.” It should clarify unknown scientific conventions, execute inside the guest, show meaningful progress, validate numeric results and deliver usable files. Then interrupt a second run and restart the controller to test recovery.

Planning ranges for one developer using a coding assistant during development, not measured commitments: foundation and isolation 3–7 focused days; Telegram-to-CSV MVP another 5–10; persistence/browser/memory/schedules and operations another 1–3 weeks; accounts, custom app and optional desktop/voice another 2–4+ weeks. Familiarity, Mac constraints and upstream integration gaps can dominate. A usable configured assistant can be much faster than building this maintainable custom architecture. Do not compress the isolation and recovery checks to meet an estimate.

## 9. Research and reference index

**OpenCode sources (check the current revision during implementation):**

1. [OpenCode home](https://opencode.ai/) and [GitHub](https://github.com/anomalyco/opencode) — current software, repository and license.
2. [OpenCode CLI](https://opencode.ai/docs/cli/) — `opencode run`, flags, models and scripted usage.
3. [OpenCode models](https://opencode.ai/docs/models/) — selection, model identifiers and available options.
4. [OpenCode providers](https://opencode.ai/docs/providers/) — authentication/provider choices and pricing distinctions.
5. [OpenCode configuration](https://opencode.ai/docs/config/) — merged settings, default/small models and permissions.
6. [OpenCode server](https://opencode.ai/docs/server/) and [SDK](https://opencode.ai/docs/sdk/) — API/session capability review.
7. [OpenCode Console models](https://opencode.ai/docs/console/models/) — metered console credits and optional accounts, not proof all models are free.
8. [OpenCode permissions](https://opencode.ai/docs/permissions/) — application tool controls, not equivalent to VM isolation.

**Infrastructure and evaluation references:**

9. [Tart](https://github.com/cirruslabs/tart), [UTM](https://github.com/utmapp/UTM), [Apple virtualization](https://developer.apple.com/documentation/virtualization/creating-and-running-a-linux-virtual-machine).
10. [Telegram Bot API](https://core.telegram.org/bots/api).
11. [Nanobot](https://github.com/HKUDS/nanobot), [OpenClaw](https://github.com/openclaw/openclaw), [Hermes Agent](https://github.com/NousResearch/hermes-agent) — optional components/alternatives.
12. [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) — checkpoints vs durable side effects.
13. [AgentDojo](https://github.com/ethz-spylab/agentdojo), [OSWorld](https://github.com/xlang-ai/OSWorld) — evaluation inspiration, not certification.

Research and model catalog observations are not security audits, cost guarantees, or hands-on verification of this user's Mac Mini.

## 10. OpenCode-specific decisions and maintenance notes (2026-11-16)

- **Development vs deployed agent:** Using OpenCode (or Codex) on the Mac to *develop this repository* is different from the deployed OpenCode worker that runs untrusted tasks inside the VM. Never equate the host's development CLI session with runtime sandboxing.
- **Current workflow:** Phase 00 free-model POC → Phase 01 host-enforced Linux VM isolation → Phase 02 authenticated transport → Phase 03 Telegram → Phase 04 task durability → Phase 05 autonomous OpenCode in guest → Phase 06 useful data workflow. Phase 13 and relevant Phase 14 checks before unattended use.
- **Model choice:** Only a model explicitly verified as free at runtime is allowed by default. The free model list shown in a UI can change. No particular GLM, MiMo, Nemotron, Beam or other free label is assumed permanent. Log exact selected model and price evidence, but never provider credentials.
- **Automatic fallback:** Disable default paid model, implicit small-model selection when chargeable, model re-routing, paid provider retry, and credit purchasing. Do not assume settings alone work; test enforcement at the trusted controller and verify effective CLI/server configuration.
- **Privacy:** A hosted free model still sends prompts/data to an external service. Check vendor data policies and limit task data accordingly. Do not send personal secrets or protected account sessions into the guest/provider.
- **Credentials:** If the guest possesses OpenCode provider credentials and the assistant executes arbitrary guest code, those credentials are potentially discoverable. A VM isolates the host from the guest; it does not isolate secret files from root inside the guest. Record and address this risk before unattended operation.
- **Server transport:** Never expose unauthenticated `opencode serve` endpoints publicly or on the LAN. Use authenticated narrow control interfaces and verify how OpenCode actually handles access and tools for the pinned version.
- **Optional engines:** Codex CLI, Claude Code or Cursor CLI may be separately added later via tested engine adapters; none may be a silent billing fallback. The existing host Codex installation does not need removal.
- **Original playbook terminology:** Any remaining references to provider, model request or tool loop must be interpreted through OpenCode's documented session/event/tool interfaces. Do not fabricate a low-level chat-completions tool-call protocol that OpenCode does not expose.
