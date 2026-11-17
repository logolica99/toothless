<p align="center"><img src="images/toothless-logo.png" alt="Toothless" width="360"></p>

<h1 align="center">Toothless</h1>

<p align="center">A security-first personal agent project with a separately isolated Linux worker.</p>

<p align="center"><strong>Operator-driven today. Autonomous execution remains disabled.</strong></p>

## What Toothless is

Toothless is a personal-agent system built around a trusted Mac controller and a bounded Ubuntu worker VM. The controller owns execution policy, SSH identity, durable operation records, and fixed VM lifecycle actions. OpenCode is the selected AI engine; its verified Phase 00 host adapter has no tools and cannot run model-generated commands. The worker interface can execute explicitly submitted operations inside Ubuntu, but it is not exposed to the model.

This repository reuses selected foundations from [Nanobot](https://github.com/HKUDS/nanobot), including its Python gateway and project structure. Nanobot's broader channels, WebUI, automations, and agent features are not automatically Toothless features. See [the reuse decision](docs/decisions/0001-adopt-nanobot-v0.4.1.md), [upstream map](docs/upstream-map.md), [LICENSE](LICENSE), and [third-party notices](THIRD_PARTY_NOTICES.md).

## Implemented and verified

| Area | Current capability | Evidence |
|---|---|---|
| OpenCode boundary | Bounded CLI adapter for the explicit model `opencode-go/glm-5.3`; structured results and errors, timeout/cancellation, output limits, deny-all tools, and no paid-model fallback. | [Phase 00 report](reports/phase-00-final.md) |
| Isolated worker | Pinned Tart and Softnet setup for the documented Apple silicon Mac profile; bounded Ubuntu 24.04 ARM64 VM; protected launcher applies and verifies host-gateway isolation. | [Worker operator guide](docs/worker-vm.md), [Phase 01 report](reports/phase-01-final.md) |
| DNS and network controls | Validated, rate-limited host DNS relay on the dynamically assigned Softnet gateway and port 55353; public DNS/HTTPS, key-only SSH, and owned endpoint isolation passed for the current deployment profile. | [Phase 01.7 report](reports/phase-01.7.md), [Phase 01 final report](reports/phase-01-final.md) |
| Worker operations | Typed execute, poll, terminate, and workspace file operations through key-only SSH with a pinned guest host key, bounded output, timeouts, and explicit unavailable/unknown outcomes. No local execution fallback. | [Phase 02.1 report](reports/phase-02-01.md), [Phase 02.2 report](reports/phase-02-02.md) |
| Recovery | Host-owned SQLite records persist execution intent and outcomes; reconnects reconcile by the original execution ID without resubmission. Missing guest history after reset stays unknown. | [Phase 02.3 report](reports/phase-02-03.md) |

Phases 03 and later are not implemented. See the [feature and task map](docs/features.md) for the full phase list and [status](docs/status.md) for current deployment state.

## Security boundary and current limits

- Start the VM only through `ops/worker/start.sh`. Direct Tart launches bypass the repository's protected policy setup and are unprotected.
- The VM execution helper is installed with `ops/worker/install-execution-agent.sh` after protected startup. Repeat this provisioning step after every worker reset.
- The current network profile is verified; this does not prove isolation for future network or IPv6 configurations.
- DNS queries pass through a restricted relay, but DNS names can still carry data. General public egress can also reach public DoH/DoT services.
- Worker operations are not OpenCode tools. No unattended or general autonomous command execution is enabled.

## Reproduce the development checks

The VM deployment is specific to the reviewed Mac, Tart/Softnet versions, dedicated SSD, and operator-managed SSH key. Review [the worker guide](docs/worker-vm.md) before preparing another host; do not treat these commands as a generic one-click installer.

Install the locked Python development dependencies from the repository root:

```bash
uv sync --locked --extra dev
```

Run the deterministic worker, DNS relay, and VM contract checks:

```bash
uv run --no-sync pytest -q \
  tests/personal_agent/test_worker_interface.py \
  tests/personal_agent/test_worker_execution_store.py \
  tests/personal_agent/test_worker_dns_forwarder.py \
  tests/personal_agent/test_worker_vm_contract.py
```

Run the static worker safety checks and import-boundary check:

```bash
ops/worker/test-static.sh
uv run --no-sync python -m scripts.check_import_boundaries
```

The live SSH tests require an already hardened worker, a verified pinned host key, and operator-provided SSH identity paths. Their exact setup, environment variables, test selection, and observed results are documented in [the Phase 02.3 report](reports/phase-02-03.md). Live networking tests require the reviewed deployment and owned fixtures; do not substitute unowned endpoints or real cloud metadata services.

## Project map

- [`INSTRUCTIONS.md`](INSTRUCTIONS.md): authoritative product plan and phase gates.
- [`docs/architecture.md`](docs/architecture.md): controller, model, worker, and persistence boundaries.
- [`docs/features.md`](docs/features.md): implemented features and remaining work.
- [`docs/status.md`](docs/status.md): current verification and deployment status.
- [`docs/worker-vm.md`](docs/worker-vm.md): installation, protected startup, recovery, reset, and isolation guidance.
- [`reports/`](reports/): phase-specific implementation and verification evidence.

## Nanobot credit

Toothless builds on the open-source [Nanobot](https://github.com/HKUDS/nanobot) project by HKUDS and its contributors. We retain the applicable upstream license and notices in this repository. The Toothless-specific worker isolation, OpenCode boundary, SSH execution interface, and recovery work are documented here and in the linked reports.
