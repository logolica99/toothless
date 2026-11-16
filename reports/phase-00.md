# Phase 00 verification report

Date: 2026-11-15 (America/Los_Angeles)

Status: implemented and locally verified

## Pinned source

- Official upstream: `https://github.com/HKUDS/nanobot.git`
- Tag: `v0.4.1`
- Commit: `3404fe83e22b7b2cb3edae1348ed45dd9b8ad051`
- Tag type: annotated, not cryptographically signed
- Python constraint: 3.11 or newer
- Test interpreter: CPython 3.14.5 on macOS
- License files retained: `LICENSE` and `THIRD_PARTY_NOTICES.md`

The GitHub advisory ranges were reviewed for GHSA-mxwj-93g9-cc5r, GHSA-hg87-wf6q-3m38, GHSA-5v87-m222-qr94, GHSA-3f63-vcp3-hvqr, GHSA-v5j3-4q66-58cf, GHSA-g6cf-xjwr-pm77, GHSA-4gmr-2vc8-7qh3, and GHSA-vc5v-6vwm-wf9m. All list v0.4.1 as patched or newer than the patched range. This is a range check, not a complete security audit.

## Checks

| Check | Result |
| --- | --- |
| `uv lock` | Exit 0; 173 packages resolved |
| `uv sync --locked --all-extras --dev` | Exit 0; 162 packages installed |
| `python -m pytest tests/personal_agent/test_foundation.py -q` | Exit 0; 3 passed |
| `ruff check tests/personal_agent` | Exit 0 |
| `python -m scripts.check_import_boundaries` | Exit 0; 13 rules and 218 files |
| `uv pip check` | Exit 0; 162 packages compatible before optional channel installation |
| Upstream Python baseline without manifest-owned channel dependencies | Exit 1; 10,845 passed, 100 skipped, 6 failed |
| Install manifest-owned channel dependencies | Exit 0 |
| Rerun the affected channel cases | Exit 0; 20 passed |
| Clean temporary-copy `uv sync --locked --all-extras --dev` | Exit 0 |
| Clean temporary-copy foundation tests | Exit 0; 3 passed |

The six initial baseline failures were missing optional Telegram, Slack, Matrix, and Feishu dependencies. Upstream intentionally keeps channel dependencies in manifests outside `uv.lock`; after running its documented installer, every affected case passed. No product behavior was changed to mask those failures.

The repository's existing GitHub Actions workflow already discovers all tests under `tests/`, so the new Phase 00 contract tests enter CI without a duplicate workflow. The committed `uv.lock` additionally pins the fork's core and extras; manifest-owned channel packages remain a documented upstream exception.

## Deterministic demo

Run:

```bash
uv sync --locked --all-extras --dev
uv run --no-sync python -m pytest tests/personal_agent/test_foundation.py -q
```

Expected result: `3 passed`. The test exercises a credential-free provider response, stable worker operation identity, and placeholder-only example secrets.

## Unresolved and deliberately not run

- No real model or messaging credentials were used.
- No worker VM exists yet; isolation tests are not run and unattended deployment remains blocked.
- The upstream tag is not cryptographically signed.
- WebUI and TUI suites were not run because Phase 00 added no client behavior.
- Published advisory review cannot establish that the selected revision has no undisclosed vulnerabilities.
