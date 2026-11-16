# ADR 0001: Adopt Nanobot v0.4.1

Status: accepted for Phase 00

## Decision

Use HKUDS/nanobot v0.4.1 at commit `3404fe83e22b7b2cb3edae1348ed45dd9b8ad051` as the fork base. Keep the upstream remote at `https://github.com/HKUDS/nanobot.git`. The tag is annotated but is not cryptographically signed, so the full commit SHA is the authoritative pin.

## Evidence

The source declares Python 3.11 or newer, MIT licensing, bundled third-party notices, pytest, Ruff, BasedPyright, and edge extension points for providers, tools, and channels. The upstream tag and source commit were fetched directly from the official repository.

Eight published advisories were reviewed on 2026-11-15. v0.4.1 is the listed patched version for GHSA-mxwj-93g9-cc5r, GHSA-hg87-wf6q-3m38, and GHSA-5v87-m222-qr94. It is newer than the patched ranges for GHSA-3f63-vcp3-hvqr, GHSA-v5j3-4q66-58cf, GHSA-g6cf-xjwr-pm77, GHSA-4gmr-2vc8-7qh3, and GHSA-vc5v-6vwm-wf9m. This range review does not prove the revision is free of other vulnerabilities.

## Consequences

Preserve `LICENSE` and `THIRD_PARTY_NOTICES.md`, keep the exact SHA visible in status and evidence, and review new upstream releases and advisories before rebasing. Phase 01 must provide a VM boundary because upstream workspace and shell restrictions are not the product's containment boundary.

