# Mac Mini personal agent: research, features, implementation prompts, and test gates

Prepared for Jubaer Jami · November 15, 2026

## 1. Recommended decision

Build a personal assistant that receives Telegram messages, works autonomously inside a Linux virtual machine on your Mac Mini, and returns useful files and progress. Keep the controller, messaging credentials, model credentials, policies, and authoritative task records outside the worker VM.

For building a product you understand and control, my recommendation is a small, maintained fork of **HKUDS/nanobot**, with a remote-worker adapter and durable task service added around its existing agent and messaging components. This is an engineering judgment from the documented architecture, not the result of a comparative performance benchmark or a source security audit. Inspect the exact selected revision before adopting it. The remote-worker boundary described here is **custom work**, not a claim that Nanobot already supplies this architecture.

If your main goal changes to getting an existing assistant running quickly, evaluate **Hermes Agent** first, with **OpenClaw** as the integration-heavy alternative. Start with configuration and extensions; fork their core only when an extension point cannot meet a concrete need. Do not combine several complete agent runtimes into the first version.

No model pretraining or fine-tuning is necessary. The initial product is an application around an existing tool-calling model. Academic projects are most useful for interface design and evaluation, rather than as the entire personal-assistant product.

## 2. Research comparison

The capabilities below are documented project capabilities. Suitability, implementation effort, and the recommendation are my judgments. Repository contents and documentation can change; phase 00 records exact revisions, dependencies, advisories, and licenses. A feature appearing in documentation does not establish reliability on your hardware.

| Project | Documented starting point | What you still own for this plan | Recommendation |
| --- | --- | --- | --- |
| [Nanobot](https://github.com/HKUDS/nanobot) | Python personal agent, messaging channels, tools, memory, scheduling, WebUI; MIT | Separate remote execution for every relevant tool, durable task semantics, host-enforced VM networking, verification | Default fork for building your own product |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Messaging gateway, memory and skills, cron, multiple terminal backends including SSH; MIT | Audit tool execution locations, isolate browser and credentials, prove recovery and budgets | Strongest ready-made alternative to trial |
| [OpenClaw](https://github.com/openclaw/openclaw) | Personal assistant gateway, Telegram and many other channels, browser and sandbox backends; MIT | Harden configuration; verify every execution surface; add your workflow and operational checks | Strong integration-oriented alternative |
| [Agent Zero](https://github.com/agent0ai/agent-zero) | Container-oriented assistant with browser, Linux desktop, projects and document workflows | Separate controller and credentials if matching this design; avoid optional host-access bridges | Useful if visible desktop coworking is your priority |
| [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) | Software-agent SDK, tools, events, workspaces and remote Agent Server; MIT | Telegram, personal memory, schedules and product experience | Consider later as a specialist coding worker |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) | Orchestration with checkpoints and long-term stores | All the messaging, tools, sandbox and product wiring | Alternative foundation for a custom application; use as a dependency rather than forking it |
| [Browser Use](https://github.com/browser-use/browser-use) | Browser-agent library; MIT | Personal assistant, tasks, VM lifecycle and messaging | Optional browser component; not a complete foundation |
| [AutoGen](https://github.com/microsoft/autogen) / [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) | AutoGen's project recommends Agent Framework for new projects | Your personal-assistant product and worker boundary | Do not start a fresh build on legacy AutoGen tutorials |

### Findings that change implementation choices

1. **Workspace restrictions are not a VM.** Nanobot's configuration distinguishes application-level workspace guards from process isolation. Its documented shell sandboxes do not restrict network access. Use the VM and external networking controls as the boundary. [Configuration](https://github.com/HKUDS/nanobot/blob/main/docs/configuration.md), [security](https://github.com/HKUDS/nanobot/security).
2. **OpenClaw sandboxing is documented as off by default.** Its Gateway remains outside tool sandboxes. Its SSH backend supports shell and file tools, but the capability matrix explicitly does not provide the sandboxed browser on SSH. A remote shell setting is insufficient evidence of browser placement. [Sandboxing](https://docs.openclaw.ai/gateway/sandboxing), [capability matrix](https://docs.openclaw.ai/gateway/sandboxing/supported-capability-matrix), [SSH](https://docs.openclaw.ai/gateway/sandboxing/ssh-backend).
3. **Hermes also requires a per-tool placement review.** Its browser documentation says browser execution runs model-written Python, and browser location/configuration is a separate choice from terminal backends. A remote terminal alone is not a complete containment guarantee. [Tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools), [browser](https://hermes-agent.nousresearch.com/docs/user-guide/features/browser), [trust model](https://github.com/NousResearch/hermes-agent/blob/main/SECURITY.md).
4. **Shared VM networking can reach your Mac.** UTM documents host/guest visibility in shared networking. Configure and test a boundary outside the root-capable guest. [Apple backend](https://docs.getutm.app/settings-apple/devices/network/), [QEMU backend](https://docs.getutm.app/settings-qemu/devices/network/network/).
5. **Pin and review the upstream revision.** Nanobot publishes advisories involving authorization, attachment paths and web fetching. The existence of an advisory does not establish that a selected revision is vulnerable; inspect affected/fixed ranges and the code selected for use. [Advisories](https://github.com/HKUDS/nanobot/security/advisories).
6. **A checkpoint is not exactly-once external execution.** LangGraph offers persistence, but the application still needs operation IDs and reconciliation for side effects. Our design deliberately distinguishes task recovery from replaying an uncertain action. [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence).

### Scratch versus fork versus configuration

| Route | Best use | Tradeoff |
| --- | --- | --- |
| Configure Hermes/OpenClaw | Get a capable personal assistant quickly | You adopt their architecture and must verify that tools and accounts respect your boundary |
| Fork Nanobot | Build your own assistant while reusing Python messaging and agent infrastructure | You maintain a fork and implement the remote execution and recovery gaps |
| New application using existing libraries | Learn the whole system or enforce a highly specific architecture | Most integration and maintenance work; use SDKs rather than rebuilding HTTP clients and database layers |

Choose the Nanobot route for the prompts below. Phase 00 is an evidence gate: if the chosen revision is unsuitable, record why and switch once, before implementation. Preserve MIT notices and inspect bundled/dependency licenses. Do not claim the whole dependency tree is MIT merely because the top-level project is.

## 3. Architecture and ownership

```mermaid
flowchart TD
    U["Telegram and later dashboard"] <--> C["Trusted controller on Mac"]
    C <--> M["Cloud model provider"]
    C <--> S["Task database and artifacts"]
    C --> P["Policy and tool dispatcher"]
    P <--> W["Linux worker VM"]
    W --> T["Terminal and installed apps"]
    W --> B["Browser and desktop"]
    H["Host watchdog and network boundary"] --> W
```

The controller performs fixed application logic and model API calls. It exposes no model-callable general host shell. All model-written code runs in the worker. Even browser-driver snippets belong there. The worker is allowed to administer and break its own guest. It is not trusted to enforce its own containment, tell the truth about its health, or protect secrets stored inside it.

| Component | Location and authority |
| --- | --- |
| Model and Telegram API credentials | Host secret storage; narrowly injected into trusted controller processes |
| Authoritative tasks, approvals, schedules, budgets | Host-owned database outside worker mounts |
| Arbitrary shell, code execution, package installation | Worker VM only |
| Browser profile and screenshots | Worker; disposable profile initially |
| Agent-generated scripts and installed guest apps | Worker; export selected results |
| VM start/stop/reset and network rules | Host operator/watchdog; fixed operations, not arbitrary model command strings |
| Protected connected accounts | Controller connector with narrowly defined operations; no equivalent browser credentials in worker |
| User-facing artifacts | Controller-managed copies of validated worker export bytes |

An internet-enabled worker can transmit data you place in it. Arbitrary code plus an authenticated website session can perform the website actions that session allows. A confirmation dialog elsewhere does not remove that ability. For protected actions, enforce the restriction through a connector whose credentials the worker cannot obtain.

### Initial stack

- Python controller using Nanobot's existing components; preserve the selected upstream Python constraints and dependency tooling.
- SQLite for custom task state, operation records and migrations; separate upstream conversation state only where necessary. Avoid duplicate authoritative records of the same concept.
- FastAPI only where an internal API or later dashboard needs it.
- SSH-based transport or a narrow authenticated RPC to the Linux VM; structured requests, never interpolated shell arguments for file names or JSON.
- Browser automation inside the worker; begin with a small Playwright-based adapter or a proven existing adapter if it satisfies the same contract.
- A cloud model through one tested provider adapter; models configured rather than hard-coded. Offline fake provider for deterministic tests.
- UTM as a broadly applicable VM candidate; Tart as an Apple-silicon automation candidate after license/version review. [UTM](https://github.com/utmapp/UTM), [Tart](https://github.com/cirruslabs/tart).
- ARM64 Ubuntu on Apple silicon, AMD64 Ubuntu on Intel. Apple documents matching Linux guest images to host architecture. [Apple](https://developer.apple.com/documentation/virtualization/creating-and-running-a-linux-virtual-machine).

Suggested starting allocations, not minimum requirements or measured performance: on a 16 GB Mac try a 4 GB worker with 2–4 vCPUs; on a 24/32 GB Mac try 6–8 GB. Use a bounded virtual disk, one worker, and one active task. On an 8 GB Mac trial a headless worker first. Confirm actual free memory/disk and workload before committing resources.

## 4. Complete feature map and delivery order

| ID | Features | User outcome | Release |
| --- | --- | --- | --- |
| 00 | Source selection, version pin, license/advisory review, baseline tests | Reproducible foundation | Foundation |
| 01 | VM, host/LAN isolation, clean image, stop/reset | Own isolated working computer | Foundation |
| 02 | Remote commands, processes, files, guest package installation | Agent can actually do work | Foundation |
| 03 | Private Telegram, attachments, commands, deduplication | Message it from anywhere | MVP |
| 04 | Durable tasks, steering, cancellation, progress, recovery | Long jobs remain controllable | MVP |
| 05 | Model tool loop, policy dispatch, budgets, clarification | General autonomous execution | MVP |
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

Build order: 00 → 01 → 02 → 03 → 04 → 05 → 06. Add a minimal watchdog and logs during those phases. Finish 13 and the applicable 14 checks before unattended deployment. Then add 07–12 as needed. Phase 15 comes last.

## 5. How to use the prompts

Give your coding LLM the master prompt once, then one implementation prompt at a time. Run that section's test prompt after the change. Carry forward the repository, decision records and status files, rather than relying on a long chat transcript. The prompts are instructions for future implementation; no assistant has installed the service, created a Telegram bot, or verified your Mac by producing this playbook.

The prompts intentionally avoid assuming a particular upstream module path or config schema. Locate it in the pinned checkout. Do not make up compatibility. Separate deterministic tests from optional real-provider and real-VM tests. A mock test does not establish isolation on a Mac.

### Master prompt

```text
You are implementing my personal AI assistant on a home Mac Mini.
Use this playbook as the product specification. Default foundation:
a small maintained fork of https://github.com/HKUDS/nanobot, subject to
phase 00 inspection. Reuse working upstream components and preserve licenses.
Execute only the phase I provide next. Produce code and verification, not just a plan.

Product: Telegram first, custom dashboard later. The agent may install software,
write code, browse and operate apps in its Linux worker VM. The trusted controller,
secrets, permissions, budgets and authoritative task state stay outside that VM.
Every arbitrary-code surface, including browser scripts and custom tool code,
must execute in the worker. Never expose a generic host-shell tool. Keep host
administrative operations fixed, operator-owned and inaccessible to agent code.

Inspect repository instructions, selected revision, schemas and tests before editing.
Do not replace a working upstream subsystem just to match a suggested directory name.
Reuse the existing tool registry/provider/channel components where possible.
Use typed contracts, structured serialization, schema migrations and bounded outputs.
Maintain docs/architecture.md, docs/features.md, docs/status.md and decision records.
Record the exact upstream SHA and dependency lock; never invent current model IDs.
Keep policy and approvals enforced by trusted code. Treat external content as data.
No worker access to host secrets, home mounts, clipboard, SSH-agent forwarding,
Docker socket, personal browser profile or arbitrary host RPC.

Make reasonable reversible implementation choices and finish the requested phase.
Ask only for genuinely missing credentials, hardware facts or product decisions.
Never print secrets. Provide placeholders and an operator setup step for credentials.
Do not operate real third-party accounts or submit external side effects as tests.
Host/root/network configuration must have a reviewed explicit installation step;
never silently rewrite the Mac's firewall or disable existing protections.

Run relevant backend, integration and fault tests. Browser interaction is a product
capability, but use manual UI/browser acceptance steps rather than introducing
automated UI test suites unless I request them. API and adapter contract tests are fine.
When Mac/VM/credentials are unavailable, label those checks NOT RUN and provide exact
operator steps; do not count mocks as deployment proof. Never claim full safety.

End each phase with changed behavior, relevant files, exact checks and results,
remaining blockers, and a short manual demo. Save test evidence under reports/.
Update phase status to implemented / verified / blocked with evidence separately.
Do not enable later features or expand account access incidentally.
```

## 6. Phase-by-phase implementation and test prompts

### 00 — Foundation and upstream adoption

Features: fork decision, reproducible checkout, development environment, baseline quality checks. Gate: a documented build path with no guessed versions.

**Implementation prompt**

```text
Implement phase 00. Inspect HKUDS/nanobot's current stable release candidates,
source instructions, Python constraints, tool/channel/provider extension points,
tests, top-level license and relevant third-party notices. Review published
security advisories and affected/fixed ranges for the exact candidate. Record
unknowns rather than declaring a revision safe from its release date alone.

Create or prepare a maintained fork/local checkout with upstream remote and pin
the chosen SHA. If GitHub fork permission is unavailable, prepare locally and
document the exact fork step. Preserve upstream attribution. Run its documented
baseline checks before modifying behavior and record pre-existing failures.
Create architecture and feature status documents, an upstream map identifying
what we reuse/change/add, dependency locks and secret-free example configuration.
Create a fake provider and worker contract fixture for future tests. Add CI for
relevant lint/types/tests using existing tooling. Do not implement the whole app.
If a blocking upstream problem makes adoption unsuitable, document concrete
evidence and select one alternative before continuing.
```

**Test prompt**

```text
Verify phase 00 from a clean temporary checkout. Install using the documented
locked workflow; run baseline checks and a fake-provider conversation. Confirm
the recorded SHA is real and matches the checkout, secret placeholders are not
credentials, ignored local secrets stay untracked, and license notices survive.
Report baseline failures separately from new regressions. Verify that phase 01
can build on explicit extension points instead of an assumed module name.
Deliver reports/phase-00.md with commands, exit codes and unresolved advisories.
```

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

### 05 — General agent loop, policy and spending controls

Features: tool calling, autonomous continuation, clarification, bounded work and configurable model provider. Gate: policy and budget checks survive malformed model output.

**Implementation prompt**

```text
Implement phase 05 using the existing provider/agent loop where possible. Connect
the durable task service and remote tools. Require schema-valid tool arguments,
bounded tool results, cancellation checks and capability checks before dispatch.
Treat websites, attachments and tool output as data that cannot change permissions.
Support final response, clarification and tool-call outcomes; retain model/tool
events without saving private reasoning as a product feature. Add context limits
and compaction with source references for preserved facts. One configured model
initially; no autonomous fallback to a more expensive provider.
Enforce task call/token/time ceilings and a host-owned spend ledger, with atomic
reservations before model calls, bounded output tokens, reconciled actual usage
and versioned price configuration. If pricing/usage is unknown, report unknown
and apply conservative call/token limits. Count related delegated/tool-provider
charges where applicable. Add retry limits/backoff for transient provider failures;
never retry an effect merely because the model request failed. Add fake provider.
```

**Test prompt**

```text
Verify phase 05 with fake-provider traces: malformed JSON, nonexistent tool,
policy bypass instruction, repeated failing tool, provider outage, overlong output,
context compaction and cancelled task. Test budget reservation races, unknown
usage, rejected overspend and no hidden provider fallback. Demonstrate a multi-step
guest task and a request requiring clarification. Attempt to invoke host exec
through every registered tool. Supply an injected document asking to disable
policy; no capability change may occur. Run a separately capped real-provider
smoke only when configured; report its actual usage, model and date.
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

Use deterministic tests for authorization, leases, serialization, scheduling, export and budgets. Use integration checks for real VM boundaries and process control. Use fault injection for restarts and unknown effects. Use manual UI acceptance for visual browser/dashboard/desktop flows. Use a capped real-model evaluation for autonomous task success. These layers answer different questions.

The root-capable guest is disposable and untrusted. Host network containment must be validated against the actual virtualization path. Merely denying one HTTP URL, running a guest firewall or testing a file-path guard is insufficient. Conversely, a successful finite boundary test suite is evidence about those tests, not proof of immunity to hypervisor vulnerabilities or arbitrary prompt injection.

The controller can provide at-most-once logical dispatch using records and keys. Delivery systems and external services may still have unknown outcomes; reconciled status and explicit unknown states are more honest than a blanket exactly-once promise. Task cancellation cannot undo external effects already completed.

A budget ledger bounds admission based on its pricing assumptions and reservations. Provider billing may differ due to delayed usage, separately charged tools or changed prices. Use provider-side limits when available and periodically reconcile bills; display estimated and confirmed amounts separately.

No expensive model training, Kubernetes, vector database, native mobile app or multi-agent hierarchy is required for the MVP. Add them only for a demonstrated feature or reliability requirement.

## 8. Practical first demo and milestone sizing

Start with a small synthetic CSV rather than real private research data. Message: “Compare these two cases, plot E11/E22/E33/G12 against time in microseconds, and send the plots, processed CSV and script.” It should clarify unknown scientific conventions, execute inside the guest, show meaningful progress, validate numeric results and deliver usable files. Then interrupt a second run and restart the controller to test recovery.

Planning ranges for one developer assisted by a coding LLM, not measured commitments: foundation and isolation 3–7 focused days; Telegram-to-CSV MVP another 5–10; persistence/browser/memory/schedules and operations another 1–3 weeks; accounts, custom app and optional desktop/voice another 2–4+ weeks. Familiarity, Mac constraints and upstream integration gaps can dominate. A usable configured assistant can be much faster than building this maintainable custom architecture. Do not compress the isolation and recovery checks to meet an estimate.

## 9. Research and reference index

Official repositories/documentation and original research used in the comparison and design:

1. [Nanobot repository](https://github.com/HKUDS/nanobot) — personal-agent foundation and license.
2. [Nanobot architecture](https://github.com/HKUDS/nanobot/blob/main/docs/architecture.md) — extension points and component responsibilities.
3. [Nanobot configuration](https://github.com/HKUDS/nanobot/blob/main/docs/configuration.md) — workspace and shell isolation distinctions.
4. [Nanobot security](https://github.com/HKUDS/nanobot/security) and [advisories](https://github.com/HKUDS/nanobot/security/advisories) — revision review and known issue classes.
5. [Hermes Agent](https://github.com/NousResearch/hermes-agent) — existing messaging, memory, schedules and terminal backends.
6. [Hermes tool reference](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools) — tool categories and placement review.
7. [Hermes browser documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/browser) — browser execution and deployment modes.
8. [Hermes security policy](https://github.com/NousResearch/hermes-agent/blob/main/SECURITY.md) — containment trust model.
9. [OpenClaw](https://github.com/openclaw/openclaw) — integration-oriented personal assistant.
10. [OpenClaw sandboxing](https://docs.openclaw.ai/gateway/sandboxing) — default behavior and execution boundary.
11. [OpenClaw capability matrix](https://docs.openclaw.ai/gateway/sandboxing/supported-capability-matrix) — backend differences.
12. [OpenClaw SSH sandbox](https://docs.openclaw.ai/gateway/sandboxing/ssh-backend) — remote shell/files utility contract.
13. [OpenClaw Telegram](https://docs.openclaw.ai/channels/telegram) — existing channel implementation.
14. [Agent Zero](https://github.com/agent0ai/agent-zero) — desktop/browser-oriented alternative.
15. [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) and [SDK paper](https://arxiv.org/abs/2511.03690) — reusable software-agent infrastructure.
16. [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) — checkpoint versus durable cross-thread state.
17. [Browser Use](https://github.com/browser-use/browser-use) — optional browser component.
18. [AutoGen](https://github.com/microsoft/autogen) and [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) — current upstream direction for new projects.
19. [Telegram Bot API](https://core.telegram.org/bots/api) — updates, polling, file and callback semantics.
20. [Apple Linux virtualization](https://developer.apple.com/documentation/virtualization/creating-and-running-a-linux-virtual-machine) — matching guest architecture and VM setup.
21. [UTM](https://github.com/utmapp/UTM), [Apple network settings](https://docs.getutm.app/settings-apple/devices/network/), [QEMU network settings](https://docs.getutm.app/settings-qemu/devices/network/network/) — host/guest network behavior.
22. [Tart](https://github.com/cirruslabs/tart) — Apple-silicon VM automation candidate; review current repository/license at adoption.
23. [SWE-agent paper](https://arxiv.org/abs/2405.15793) — evidence that agent-computer interface design matters; not a claim of performance for this assistant.
24. [OSWorld](https://github.com/xlang-ai/OSWorld) — real-environment task evaluation inspiration, not a personal-assistant framework.
25. [AgentDojo](https://github.com/ethz-spylab/agentdojo) — prompt-injection and useful-task evaluation inspiration, not a security certification.

Research scope: reviewed public repositories, documented architecture, trust boundaries and original evaluation projects. This was not a hands-on benchmark of the frameworks, full dependency audit, penetration test or verification of the user's Mac. Exact revision selection and real deployment tests are intentionally phase 00/01 gates.
