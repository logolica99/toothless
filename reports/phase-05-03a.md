# Phase 05.3A — Real Guest Browser Installation and Smoke Test

Date: 2026-11-17

Status: VERIFIED FOR OPERATOR-DRIVEN GUEST BROWSER SMOKE TEST. The approved 40 additions, 7 upgrades, and separately approved `libatomic1` package were installed after exact transaction simulations. Guest-local Playwright 1.65.2 and Chromium 159.0.8154.6 ARM64 headless shell successfully rendered the public test page and the guest-loopback JavaScript/link fixture, saved a verified PNG screenshot, and cleaned up browser processes and temporary profiles. Browser-specific private-network containment remains unverified, and OpenCode browser tools remain disabled.

## Compatibility and selection

The current VM is Ubuntu 24.04 ARM64 (`aarch64`) with Python 3.12.3. Official Playwright Python documentation supports Ubuntu 24.04 ARM64, and the official PyPI release `playwright==1.65.2` supports Python 3.10+ and publishes an ARM64 Linux wheel. Playwright 1.65.2 maps to Chromium 159.0.8154.6; its Linux ARM64 headless shell is available as the Chrome for Testing artifact. Playwright documents `--only-shell` for headless-only installation. Sources: [Playwright Python installation](https://playwright.dev/python/docs/intro), [Playwright browser installation](https://playwright.dev/docs/browsers), [Playwright 1.65.2 on PyPI](https://pypi.org/project/playwright/1.65.2/), and [official Chrome for Testing ARM64 archive](https://storage.googleapis.com/chrome-for-testing-public/159.0.8154.6/linux-arm64/chrome-headless-shell-linux-arm64.zip).

The proposed library is the pinned Python Playwright 1.65.2 package in a guest-local virtual environment. The browser is the matching Chromium headless shell 159.0.8154.6. The guest binary, venv, browser cache, temporary profile, test fixture, and screenshot remain inside Ubuntu. No Mac browser or profile is involved.

## Live preflight evidence

The running VM state was read with `tart get toothless-worker`; the host command line confirmed the live process is `ops/worker/softnet_launcher.py headless`. No direct Tart launch was used. Authenticated host-key-pinned SSH commands ran through the existing worker interface. Current guest inventory: 18,253,459,456 bytes total on `/`, 13,719,068,672 bytes free, 6,188,457,984 bytes RAM total, and 5,417,820,160 bytes RAM available. The guest currently lacks `python3.12-venv`; Playwright and Chromium are absent.

The package request was simulated with `sudo -n apt-get -s --no-install-recommends install` using the packages below. A second `sudo -n apt-get --assume-no --no-install-recommends install` resolved the package plan, reported archive and disk totals, then received `N` and printed `Abort`; no install occurred. Apt reported 48.8 MB to download (50.9 MB of archives in total, including already cached data) and 200 MB additional installed disk. It would make 7 upgrades, install 40 new packages, remove none, and leave 90 packages not upgraded.

The matching browser archive was checked with a headers-only HTTPS request from the guest, then its ZIP central directory was read from a bounded 1 MiB HTTP range in memory. It is 130,286,843 bytes compressed and 295,295,722 bytes expanded across 287 entries. The PyPI ARM64 wheel for Playwright 1.65.2 is 53,146,329 bytes; the exact venv footprint was not measured. The current 13.72 GB pre-install free space was ample for the apt estimate, browser archive, and Playwright wheel.

Read-only command results:

| Operation | Result |
| --- | --- |
| `tart get toothless-worker` | PASS; worker is running |
| Host launcher process inspection | PASS; protected `softnet_launcher.py headless` owns the running Tart process |
| Guest OS, architecture, Python, RAM, and disk inventory through pinned-key `WorkerInterface` | PASS; Ubuntu 24.04 ARM64, Python 3.12.3, 13,719,068,672 bytes free |
| `sudo -n apt-get -s --no-install-recommends install <candidate packages>` | PASS; resolved transaction with 7 upgrades, 40 new packages, no removals |
| First package action/version comparison against the approved report | FAIL; current `libxdamage1` candidate was `1:1.1.6-1build1`, while the report said `1:1.1.6-1build3`; the operator approved that correction |
| Revised exact version-pinned package action comparison | PASS; execution `e1e2dc60e1c0441cb8d3bada606fbf58` resolved exactly 47 actions, 40 new and 7 upgraded, with 0 removals and 90 not upgraded; all changed package names and versions match the corrected report |
| Unchanged dependency presence and compatibility | PASS; execution `b5a6cff6058b47548062bf52c3a70eb5` confirmed all six already-installed direct dependencies remain installed and candidate-aligned, including `libxext6` `2:1.3.4-1build2`; the apt simulation completed dependency resolution |
| First version-pinned install attempt | FAIL BEFORE CHANGES; execution `3f50fa04c9f445d6a53171d7e30702a1` exited 100 because my manually typed `libxrandr2=1:1.5.2-2build1` did not match the approved/current `2:1.5.2-2build1`; apt stopped before package installation |
| Post-abort package-state check | PASS; execution `b5a6cff6058b47548062bf52c3a70eb5` showed all 40 proposed new packages still not installed and all seven upgrade targets still at their original versions; no partial transaction occurred |
| `sudo -n apt-get --assume-no --no-install-recommends install <candidate packages>` | PASS WITH EXPECTED ABORT; apt estimated 48.8 MB network download and 200 MB additional installed space, then received `N`; no package changed |
| HEAD request for official ARM64 browser artifact | PASS; HTTP 200 and 130,286,843-byte archive |
| Bounded ZIP directory range inspection | PASS; 295,295,722-byte expanded archive size; no archive saved |

## Exact apt transaction for approval

The package table below is the reviewed transaction, amended after operator approval to change `libxdamage1` from `1:1.1.6-1build3` to `1:1.1.6-1build1` and to correct the recorded unchanged `libxext6` version from `1:1.3.4-1build2` to `2:1.3.4-1build2`. The exact version-pinned fresh simulation (execution `e1e2dc60e1c0441cb8d3bada606fbf58`) matched all 47 changed actions, versions, and counts, with no removals or downgrades. A separate read-only inventory confirmed all unchanged direct dependencies are installed and candidate-aligned. The first actual apt invocation failed before changes because its manually entered `libxrandr2` pin used `1:` instead of the approved `2:`; the post-abort inventory confirmed no package changed. The retry will build all version pins directly from this table to avoid another transcription error.

The following 40 packages would be newly installed at the versions resolved by the live simulation:

| Package | Version |
| --- | --- |
| `at-spi2-common` | `2.52.0-1build1` |
| `fontconfig` | `2.15.0-1.1ubuntu2` |
| `libasound2-data` | `1.2.11-1ubuntu0.3` |
| `libasound2t64` | `1.2.11-1ubuntu0.3` |
| `libatk-bridge2.0-0t64` | `2.52.0-1build1` |
| `libatk1.0-0t64` | `2.52.0-1build1` |
| `libatspi2.0-0t64` | `2.52.0-1build1` |
| `libavahi-client3` | `0.8-13ubuntu6.2` |
| `libavahi-common-data` | `0.8-13ubuntu6.2` |
| `libavahi-common3` | `0.8-13ubuntu6.2` |
| `libcairo2` | `1.18.0-3build1` |
| `libcups2t64` | `2.4.7-1.2ubuntu7.14` |
| `libdatrie1` | `0.2.13-3build1` |
| `libgbm1` | `25.2.8-0ubuntu0.24.04.4` |
| `libgraphite2-3` | `1.3.14-2ubuntu0.24.04.1` |
| `libharfbuzz0b` | `8.3.0-2build2` |
| `libllvm20` | `1:20.1.2-0ubuntu1~24.04.3` |
| `libpango-1.0-0` | `1.52.1+ds-1build1` |
| `libpixman-1-0` | `0.42.2-1build1` |
| `libthai-data` | `0.1.29-2build1` |
| `libthai0` | `0.1.29-2build1` |
| `libx11-xcb1` | `2:1.8.7-1ubuntu0.1` |
| `libxcb-dri3-0` | `1.15-1ubuntu2` |
| `libxcb-present0` | `1.15-1ubuntu2` |
| `libxcb-randr0` | `1.15-1ubuntu2` |
| `libxcb-render0` | `1.15-1ubuntu2` |
| `libxcb-shm0` | `1.15-1ubuntu2` |
| `libxcb-sync1` | `1.15-1ubuntu2` |
| `libxcb-xfixes0` | `1.15-1ubuntu2` |
| `libxcomposite1` | `1:0.4.5-1build3` |
| `libxdamage1` | `1:1.1.6-1build1` |
| `libxfixes3` | `1:6.0.0-2build1` |
| `libxi6` | `2:1.8.1-1build1` |
| `libxrandr2` | `2:1.5.2-2build1` |
| `libxrender1` | `1:0.9.10-1.1build1` |
| `libxshmfence1` | `1.3-1build5` |
| `mesa-libgallium` | `25.2.8-0ubuntu0.24.04.4` |
| `python3-pip-whl` | `24.0+dfsg-1ubuntu1.3` |
| `python3-setuptools-whl` | `68.1.2-2ubuntu1.2` |
| `python3.12-venv` | `3.12.3-1ubuntu0.17` |

These 7 already installed packages would be upgraded:

| Package | Current version | Proposed version |
| --- | --- | --- |
| `gir1.2-glib-2.0` | `2.80.0-6ubuntu3.9` | `2.80.0-6ubuntu3.10` |
| `libglib2.0-0t64` | `2.80.0-6ubuntu3.9` | `2.80.0-6ubuntu3.10` |
| `libglib2.0-bin` | `2.80.0-6ubuntu3.9` | `2.80.0-6ubuntu3.10` |
| `libglib2.0-data` | `2.80.0-6ubuntu3.9` | `2.80.0-6ubuntu3.10` |
| `libnss3` | `2:3.98-1ubuntu0.2` | `2:3.98-1ubuntu0.3` |
| `libx11-6` | `2:1.8.7-1build1` | `2:1.8.7-1ubuntu0.1` |
| `libx11-data` | `2:1.8.7-1build1` | `2:1.8.7-1ubuntu0.1` |

These direct dependencies should be present already and remain unchanged: `libdbus-1-3` (`1.14.10-4ubuntu4.1`), `libdrm2` (`2.4.125-1ubuntu0.1~24.04.2`), `libnspr4` (`2:4.35-1.1build1`), `libxcb1` (`1.15-1ubuntu2`), `libxext6` (`2:1.3.4-1build2`), and `libxkbcommon0` (`1.6.0-1build1`). Verify each is installed and apt resolves it without an action; exact versions are recorded for evidence but are not treated as authorized package changes. Apt's simulation marks these already-installed dependencies as manually installed in package selection metadata.

### Impact and rollback

The 7 upgrades affect shared system libraries (GLib, NSS, and X11) in the guest; apt may restart no services automatically, but any process already using those libraries continues with its mapped copy until restarted. The Mac and host network configuration are not touched. The `toothless-agent` account receives no sudo or apt access. The installation will modify only the running VM and will not seal or replace a baseline. A worker reset would remove these changes unless a later operator-reviewed provisioning/baseline step is approved. Newly installed packages can be removed with apt, but returning upgraded packages to prior versions depends on package availability/cache and is not guaranteed; preserving the VM's clean baseline remains the recovery option.

## Approval boundary (before operator approval)

At the time this approval boundary was written, no install had been performed. The requested transaction was 40 new packages and 7 upgrades at the versions listed above, with no removals; apt estimated 48.8 MB network transfer and 200 MB additional installed space; plus a guest-local Playwright 1.65.2 environment and Chromium 159.0.8154.6 headless-shell download (130,286,843 compressed bytes, 295,295,722 expanded bytes). The approval required an immediate fresh exact-action/version simulation before installation. All privileged package commands were invoked through the operator-side pinned-key guest path; `toothless-agent` did not receive sudo or apt permission.

## Smoke-test plan prepared before approval

An operator-controlled script will run as the rootless `toothless-agent` account. It will open `https://example.com`, check its observed title and rendered text, open a small JavaScript fixture served only on guest loopback, click a button and verify the resulting DOM text, and save a screenshot under `/home/toothless-agent/workspace/browser-smoke-example.png`. It will use a fresh ephemeral browser context, no persistent profile or host cookies, no remote debugging server, and will close the context/browser and local fixture server in `finally` cleanup. It will record guest browser process memory before and after close and fail if browser child processes remain. The loopback fixture is for rendering/click smoke only and is not evidence of browser SSRF or private-network containment.

## Installation and smoke-test results

The final fresh package simulation was generated from the approved package table and compared by exact package name, version, and action. Execution `5828e2612c064ce6ab5788f1c120ead5` passed with exactly 40 new packages and 7 upgrades, no removals or downgrades, and no extra package actions. The six unchanged direct dependencies were separately checked for installed status and dependency resolution; `libxext6` was installed at `2:1.3.4-1build2`, and `libxdamage1` was added at the approved `1:1.1.6-1build1` version. The prior manually mistyped `libxrandr2` pin was safely rejected before any package changes; the corrected transaction was generated from this report and installed successfully as execution `bd1971bd78a044a0ba5bd2f67e5e1c2d`. Post-install verification `a8f016b9fa804b379ae5c807d733967b` confirmed the approved versions and actions. Apt restarted `fwupd`, `polkit`, and `udisks2`; it deferred `ModemManager` and `unattended-upgrades`. No package was removed.

Playwright Python 1.65.2 installed into `/home/toothless-agent/.local/share/toothless/browser-smoke/venv` as execution `fd0d441de385449cb06dcbeaaa51594a`. The official ARM64 archive was downloaded and its SHA-256 verified as `e1262e9f4f244c441dcb54aa0e9abe4cf7b266ab02dc666fd975bd55aba9b3dc`; the archive's executable entry was 204,672,400 bytes with archived mode 0755. The extracted binary is `Google Chrome for Testing 159.0.8154.6`, SHA-256 `3e818d32e2fe5749b54c65179d282d14d0af39010ed0fbdd24122af1aca22046`, and mode 0755. The version was checked as `toothless-agent` in execution `ca610453d8964c34a2810495bd4f0933`; the resulting browser permission was restored to the exact 0755 mode recorded by the approved archive.

The first smoke attempt, execution `d6194fd64b154724b9bd5bf20124fc94`, stopped before launching Chromium because its bundled Node driver could not load `libatomic.so.1`. A read-only guest package check confirmed `libatomic1` was absent. After the operator separately approved `libatomic1=14.2.0-4ubuntu2~24.04.1`, exact simulation `415699b30ff3428b80ed41e5eba8401d` showed exactly one new package, zero upgrades, and zero removals; installation `9b32a398a4c248bdbe17f85f30229393` succeeded with that exact action/version, fetching 11.6 kB and adding 82.9 kB of installed disk use; no service restart was required, and the `unattended-upgrades` restart was deferred. An initial post-install smoke attempt exposed that `example.com` now has localized body text while keeping its expected title; the diagnostic run confirmed a successful HTTP response, and the smoke assertion was updated to check the observed English documentation text rather than requiring the title in the body.

The final operator-controlled smoke run, execution `5e8a96b190814cdbab6ff0cd6fb06995`, passed as the unprivileged `toothless-agent` account. It reported HTTP 200 from `https://example.com/`, title `Example Domain`, and rendered documentation text beginning “This domain is for use in documentation examples”. The guest-local button click returned `JS_RENDERED_OK`; clicking the fixture link reached `http://127.0.0.1:44249/clicked` and rendered `LINK_TARGET_OK`. The screenshot was saved to `/home/toothless-agent/workspace/browser-smoke-example.png` (8,796 bytes); a separate authenticated file-interface check confirmed the PNG signature. During the run, seven Chromium processes accounted for 520,284 KiB aggregate RSS; the largest individual process reported 103,692 KiB RSS. No remote debugging TCP port was requested. The script reported zero browser processes after close and no forced process termination. Follow-up cleanup execution `75bc796613b04271ad2f6f2c3df0a610` independently confirmed zero remaining Chromium processes, zero temporary profiles, and removal of the temporary smoke script while preserving the screenshot.

| Check | Result |
| --- | --- |
| Exact package action/version simulation | PASS — 40 new, 7 upgraded, 0 removed/downgraded, no additional actions |
| Approved apt transaction installation | PASS — execution `bd1971bd78a044a0ba5bd2f67e5e1c2d` |
| Post-install package/version inventory | PASS — all approved changes and unchanged direct dependencies verified |
| Playwright Python version | PASS — 1.65.2 in the guest-local venv |
| Chromium archive and extracted binary | PASS — ARM64 159.0.8154.6, archive hash and executable version verified |
| Chromium public HTTPS rendering | PASS — HTTP 200, title `Example Domain`, expected rendered documentation text |
| JavaScript rendering and button interaction | PASS — guest-loopback fixture returned `JS_RENDERED_OK` |
| Fixture link navigation | PASS — click reached `/clicked` and rendered `LINK_TARGET_OK` |
| Screenshot creation | PASS — `/home/toothless-agent/workspace/browser-smoke-example.png`, 8,796 bytes, PNG signature verified through SSH file interface |
| Chromium memory use | PASS — 7 browser processes; 520,284 KiB aggregate RSS, 103,692 KiB peak individual RSS |
| Browser process/profile cleanup | PASS — zero Chromium processes after close, no forced termination, no temporary profile or smoke script left |
| OpenCode browser-tool access | PASS — remains disabled; no model permissions or OpenCode configuration were changed |
| Softnet, baseline, host network, and macOS permissions | PASS — unchanged |

## Regression checks

| Command | Result |
| --- | --- |
| `python3 -m py_compile ops/worker/browser_smoke.py` | PASS, exit 0 |
| `.venv/bin/ruff check ops/worker/browser_smoke.py` | PASS, exit 0 |
| `.venv/bin/python -m pytest tests/personal_agent/test_worker_interface.py tests/personal_agent/test_worker_vm_contract.py tests/personal_agent/test_guest_worker_workspace_alias.py` | PASS, exit 0; 22 tests passed |
| `.venv/bin/basedpyright ops/worker/browser_smoke.py` | EXIT 1 — the host development environment does not contain Playwright because it is installed only in the guest; BasedPyright cannot resolve `playwright.sync_api` and reports cascading unknown Playwright API types |
| `git diff --check` | PASS, exit 0 |

The guest retains the approved apt dependencies, `libatomic1`, guest-local Playwright environment, and extracted headless-shell binary, plus the verified screenshot in the guest workspace. These are changes to the running worker only and are not present in a sealed baseline; a future worker reset can discard them. The VM remains under the existing protected launcher. This checkpoint proves an operator-controlled browser smoke path, not browser-specific SSRF or private-network containment. OpenCode browser tools remain disabled, no browser capability was registered, and Phase 05.3B has not started.
