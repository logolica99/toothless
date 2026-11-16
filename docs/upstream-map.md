# Upstream reuse map

## Reuse unchanged

| Capability | Upstream owner |
| --- | --- |
| Agent/model iteration | `nanobot/agent/runner.py` and `nanobot/agent/loop.py` |
| Tool extension contract | `nanobot/agent/tools/base.py` and `nanobot/agent/tools/registry.py` |
| Provider contract and routing | `nanobot/providers/base.py`, `factory.py`, and `routing.py` |
| Telegram channel package | `nanobot/channels/telegram/` |
| Session persistence | `nanobot/session/` |
| Configuration loading | `nanobot/config/` |

## Change deliberately

- Route every executable tool through a typed remote-worker adapter beginning in Phase 02.
- Keep authoritative task, approval, budget, and operation records in host-owned storage beginning in Phase 04.
- Add trusted policy dispatch ahead of worker calls beginning in Phase 05.

## Add outside upstream ownership

- Host-owned worker lifecycle and network-boundary operations.
- Durable personal-agent task and operation records where upstream session state is not authoritative.
- Validated artifact import from the worker.

The worker adapter will register at the existing tool-registry edge. It must not add worker transport or host administration to the core runner. The provider fixture uses the existing `LLMProvider` contract. These are observed extension points in the pinned source, not assumed module names.

