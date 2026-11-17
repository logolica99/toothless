# Phase 00 final report

Date: 2026-11-16 (America/Los_Angeles)

Status: **VERIFIED** for the Phase 00 host proof of concept. The adapter returned the required READY response, a factual answer, and a parseable JSON response under the effective deny-all tool configuration. The five-step agent limit is the likely explanation for the earlier maximum-steps summary, but the evidence is not a controlled single-variable proof.

## Changes made

- Kept OpenCode as the sole primary Phase 00 engine and kept Nanobot outside this engine call path.
- Added immutable engine configuration separate from controller/process code, with the sole currently authorized model `opencode-go/glm-5.3`, a 120-second default timeout, a 1-MiB total output limit, and a 64-KiB per-event limit.
- Extended the existing adapter into a stable prompt/model/result interface using documented non-interactive `opencode run --format json` arguments without a shell.
- Added a real subprocess executor with a minimal inherited environment, disposable working-directory support, elapsed-time recording, process-group timeout cleanup, cancellation, and output-limit termination.
- Added typed success, quota/rate-limit, unavailable-model, authentication, network, timeout, cancellation, output-limit, malformed-output, and generic-failure results. Results include model, duration, event count, and session identifier; successful final text can be parsed as JSON.
- Raised the tracked OpenCode v2.3.0 `phase00-readonly` agent limit to five steps while retaining recognized deny-all permissions. Unsupported `policy` keys remain omitted because effective-config inspection did not recognize them. Provider/model authorization remains enforced before process creation by the controller.
- Preserved the fake executor for deterministic failure injection. No model/provider retry or fallback path exists.

## Files modified

- `nanobot/providers/opencode_config.py`
- `nanobot/providers/opencode_cli.py`
- `ops/opencode/phase00/opencode.json`
- `tests/personal_agent/test_opencode_cli.py`
- `docs/architecture.md`
- `docs/features.md`
- `docs/status.md`
- `reports/phase-00.md`
- `reports/phase-00-final.md`

Phase 01 implementation was not changed or started by this task. Its existing worker was confirmed stopped.

## Engine and effective restrictions

| Item | Observed value |
| --- | --- |
| CLI | OpenCode v2.3.0 |
| Binary SHA-256 | `9f3147cae4ca323827d3ca39451b0d3431b2906e79a08e9b09d1eb34bb703f52` |
| Provider/model | `opencode-go/glm-5.3` |
| Authentication | OpenCode Console Personal stored connection; credential values were not inspected or printed |
| Model selection | Exact controller allowlist; unknown IDs rejected before executor invocation |
| OpenCode permissions | Deny all at top level and on the five-step `phase00-readonly` agent |
| Invocation | `opencode run --standalone --archived --format json --agent phase00-readonly --model opencode-go/glm-5.3 PROMPT` |
| Paid fallback | None implemented or observed |

The Go model was present in the authenticated OpenCode catalog. The prior one-step adapter run returned a maximum-steps summary. The installed v2.3.0 binary contains the exact max-steps message and documents the `steps` field as the maximum number of agentic iterations before forcing a text-only response. Raising the limit to five was followed by three successful application-adapter responses. This supports the step-limit hypothesis, but because the earlier adapter result and later results were separate model calls, it does not prove the limit alone caused the earlier response.

## Phase 00.5 controlled comparison

| Property | Manual CLI | Python adapter |
| --- | --- | --- |
| Binary | `/Users/jubaerjami/.opencode/bin/opencode` | Same |
| Version | v2.3.0 | Same |
| Model | `opencode-go/glm-5.3` | Same |
| Arguments | `run --standalone --archived --format json --agent phase00-readonly --model opencode-go/glm-5.3` plus READY prompt | Same argument vector |
| Working directory | `/private/tmp/toothless-phase00-real.00wK6D` | Same |
| Effective config | Temp `opencode.json`, exact Go model, five steps, deny-all permissions | Same |
| HOME / XDG | Same HOME; XDG variables unset | Same HOME; XDG variables unset |
| Authentication | `OpenCode Console Personal` stored; values not inspected | Same HOME-based stored auth context; no auth variables filtered |
| Environment | Full host process environment | Allowlist: HOME, PATH, TMPDIR, LANG, LC_ALL, TERM, and XDG config/data/cache variables |
| Flags affecting behavior | `--standalone --archived` | Same |
| Result after five-step setting | Adapter returned `READY`, 1.632 s, 3 events | Adapter returned `READY`, 1.632 s, 3 events |

The later adapter requests, all under the same five-step deny-all configuration, returned `Paris` (2.718 s, 3 events) and `{"ready": true, "count": 3}` (2.571 s, 3 events), which parsed as a JSON object. No 429 occurred, and there was no retry or model switch.

Filtered environment names included `SSH_AUTH_SOCK`, `CODEX_*`, `XPC_*`, `PWD`, locale/terminal, and other host variables. No OpenCode, API-key, token, or auth-named environment variable was filtered. The exact model and config are unchanged; current evidence does not show that `HOME`, XDG, authentication, cwd, flags, or binary explains the different completion.

At most one manual and one adapter request were made, as authorized. Neither returned HTTP 429, so no retry was made.

## Verification results

| Check | Result | Evidence |
| --- | --- | --- |
| Adapter submits prompt through documented CLI | PASS | Exact argument assertion and real application-adapter invocation |
| Manual expected real response `READY` | PASS | Exit 0, 3 events, 3.367 seconds |
| Adapter expected real response `READY` | PASS | 3 events, 1.632 seconds, exact text READY |
| Simple factual real answer | PASS | Paris, 3 events, 2.718 seconds |
| Real structured JSON final answer | PASS | JSON parsed to `{"ready": true, "count": 3}`, 3 events, 2.571 seconds |
| Quota classification | NOT TRIGGERED | No request returned 429 |
| Deterministic structured JSON success parsing | PASS | Fake JSON final response parsed to `{"ready": true}` |
| Invalid model rejected before execution | PASS | Executor received zero calls |
| Simulated rate limit stops cleanly | PASS | Typed `rate_limited`, one call, no fallback |
| Simulated network failure | PASS | Typed `network_error` |
| Timeout cleanup | PASS | Real sleeping subprocess group terminated in under two seconds |
| Cancellation cleanup | PASS | Active real sleeping subprocess terminated and reported cancelled |
| Total output and individual event bounds | PASS | Real output-overflow process terminated; oversized fake event rejected |
| No paid fallback | PASS | One constant allowlist entry and one executor call; no retry/fallback implementation |
| No unexpected disposable-directory changes | PASS | Real requests used the disposable working directory; only its OpenCode config was present |
| No tool execution on Mac | PASS for tested boundary | Effective configuration shows deny-all permissions at top level and agent level; adapter accepts prompts, not arbitrary shell commands |
| Existing personal-agent regressions | PASS | 16 passed |
| Ruff | PASS | Exit 0 |
| BasedPyright | PASS | 0 errors, 0 warnings |
| Import boundaries | PASS | 13 rules, 218 files |
| `git diff --check` | PASS | Exit 0 |
| Phase 01 work | NOT RUN | Explicitly outside task scope |

## Exact verification commands

```text
/Users/jubaerjami/.opencode/bin/opencode --version
/Users/jubaerjami/.opencode/bin/opencode debug config --standalone
/Users/jubaerjami/.opencode/bin/opencode models --standalone
uv run --no-sync python -m pytest tests/personal_agent -q
uv run --no-sync ruff check nanobot/providers/opencode_cli.py nanobot/providers/opencode_config.py tests/personal_agent
uv run --no-sync basedpyright nanobot/providers/opencode_cli.py nanobot/providers/opencode_config.py
python -m scripts.check_import_boundaries
git diff --check
```

The real inference was run through `OpenCodeCLI(SubprocessOpenCodeExecutor(), OpenCodeEngineConfig(...))`, not directly as a terminal-only smoke test.

## Remaining blocker and recommendation

The five-step limit is the likely root cause: the earlier response explicitly said the agent maximum had been reached, and the installed binary documents `steps` as the agentic-iteration limit before forcing a text-only response. With `steps: 5`, READY, factual, and JSON adapter calls all passed. This is strong supporting evidence rather than a controlled A/B test; the model's response can vary between calls. Tool permissions remain deny-all, the model remains `opencode-go/glm-5.3`, and the adapter has no fallback. This verification covers the bounded host Phase 00 proof only; deployed autonomous execution remains VM-only for Phase 05. Do not begin Phase 01 automatically.

Do not begin Phase 01 yet. Once all three real prompts pass and the disposable-directory/config checks remain unchanged, update this report and Phase 00 status to VERIFIED, then request explicit operator approval before any Phase 01 work.
