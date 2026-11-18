# Phase 05.3 — Guest Browser and Website Interaction

Date: 2026-11-17

Status: BLOCKED before browser installation or tool enablement. The inspected Ubuntu ARM64 platform is compatible in principle with a headless Chromium/Playwright setup, but current isolation does not provide the required browser-specific containment for guest loopback, private destinations, redirects, or DNS rebinding. No policy was weakened and no browser package was installed.

## Findings

- The current worker is Ubuntu 24.04 ARM64 (`aarch64`), with Python 3.12.3, approximately 6.2 GB total RAM, approximately 5.4 GB available at inspection, and approximately 13.7 GB free on the 20 GB guest disk.
- No Node.js, npm, Playwright, Chromium, or browser automation runtime was present. The `nftables` package was present at `1.0.9-1ubuntu0.1`; the observed restricted SSH command path did not resolve `nft`, and no browser rules were installed or changed.
- The guest resolver uses the dynamically assigned Softnet gateway on port 55353. No DNS or Softnet configuration was changed.
- Current Phase 01 evidence verifies the documented host-gateway/DNS-relay behavior, public access, key-only management SSH, and owned endpoint tests listed in the Phase 01 report. It does not establish per-process denial of guest loopback or all private destinations for a browser, nor does it prove redirect- and DNS-rebinding-safe navigation.
- OpenCode's `shell` tool runs as the same `toothless-agent` identity as any browser process would. A Playwright origin list or shell command scanner is not an OS/network boundary. Exposing browser and shell together while reading untrusted pages would leave the browser-target and hostile-content risks unresolved.
- Playwright documentation lists Ubuntu 24.04 ARM64 support and Chromium. Playwright MCP documents that it is not a security boundary and that origin checks do not restrict redirect destinations. References: [Playwright installation](https://playwright.dev/docs/intro), [Playwright MCP security notes](https://github.com/microsoft/playwright-mcp).

## Proposed dependency transaction, not approved or installed

The simulated candidate direct apt package set was `python3.12-venv libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2`. Apt would also install transitive packages and upgrade some existing shared libraries, including GLib and NSS. The package versions and complete transaction are mutable and must be freshly simulated and reviewed before any future install. Separately, a rootless Python environment would need a pinned Playwright release and its ARM64 Chromium headless shell download; their final version and disk footprint were not installed or measured. `toothless-agent` has no sudo or package-manager privilege.

Installation is deferred until the network boundary is designed. Browser packages alone do not meet the safety requirements. No installation approval was requested or assumed.

## Verification matrix

| Check | Result | Evidence / limitation |
| --- | --- | --- |
| Ubuntu version and architecture | PASS | Read-only authenticated worker inventory: Ubuntu 24.04 ARM64 (`aarch64`) |
| Runtime and capacity inventory | PASS | Python 3.12.3; approximately 6.2 GB RAM and 13.7 GB guest disk free at inspection |
| Existing browser/runtime inventory | PASS | Node, npm, Playwright, and Chromium absent |
| Existing enforcement inventory | PARTIAL | `nftables` package present; no browser-specific rules; `nft` not found in restricted SSH PATH |
| Candidate package simulation | PASS WITH LIMITATION | Apt simulation identified direct package set and library upgrades; exact transaction must be refreshed before approval |
| Browser binary/version on VM | NOT RUN | No browser installed |
| Public URL load/render and official Python release extraction | NOT RUN | No browser tool; no website query made |
| JavaScript-rendered page, click, screenshot, download | NOT RUN | No browser tool or fixture executed |
| Crash, timeout, cancellation cleanup | NOT RUN | No browser integration exists |
| Private/loopback/link-local/metadata denial | NOT RUN | No browser; current per-process protection is unproven. No real metadata endpoint contacted |
| Redirect and DNS-rebinding enforcement | NOT RUN | No browser-specific destination policy exists |
| Duplicate `/run`, controller restart, Telegram browser workflow | NOT RUN | No browser task submitted |
| OpenCode/worker regression checks | NOT RUN | No implementation was added; no test suite, lint, or type checker was run for this blocked checkpoint |

## Required next control

Design and verify an OS-enforced browser-only egress boundary that blocks guest loopback and prohibited IPv4/IPv6 private, link-local, and metadata ranges after resolution and across redirects, fails closed, and cannot be bypassed by the browser process. Use owned fixtures. Keep the existing Softnet policy, rootless identity, task approval, pinned SSH, and no-host-fallback guarantees. Separate access to browser tools from arbitrary shell while processing untrusted page content. Only after that design passes review should the exact apt transaction and browser download be presented for explicit operator approval.

Direct Tart launches remain unprotected; all worker launches must continue through `ops/worker/start.sh`. No Phase 05.3 demo was performed, no official Python release was asserted, and Phase 05.4 or unattended execution was not started.
