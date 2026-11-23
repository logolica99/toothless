# Phase 08.0 — Experimental blog provisioning

Status: IMPLEMENTED LOCALLY; NOT DEPLOYED; NOT VERIFIED. No provider credentials were supplied, no Keychain entries were created, and no repository, Vercel project, deployment, or production setting was changed.

## Implemented

- `/blog setup`, `/blog status`, and `/blog cancel` provide persistent Telegram status and owner-bound inline actions. Callback records are single-use, bound to the owner, private chat, and Telegram message; approval proposals expire after ten minutes and bind the exact fixed target, stage, source baseline commit, and target repository ID where applicable.
- A private credential page is mounted at `/memory/blog-setup/` under the existing Tailscale Serve/Memory Explorer service. A one-use code is issued only after the owner presses a Telegram callback. The code exchange requires the configured HTTPS origin and creates a ten-minute strict-samesite session, HttpOnly session cookie, CSRF cookie/header, no-store responses, bounded JSON request size, and the existing controller restart epoch. No public listener or new network route was added.
- GitHub and Vercel credentials are validated by host-side provider requests and stored in the macOS login Keychain through Security.framework. The setup UI never asks the owner to paste credentials into Telegram. Credential values are not put in SQLite, logs, model prompts, environment variables, or process arguments. Local removal clears the Keychain item; the owner must separately revoke it at the provider. Git operations use a private temporary directory and a fixed askpass bridge that reads the target-only publishing token from Keychain when Git requests it.
- Repository provisioning is fixed to source `logolica99/tech-pulse` and private target `logolica99/tech-pulse-toothless`; the Vercel project is fixed to `tech-pulse-toothless`. Repository creation and copy/deployment are distinct approval stages. The copy stage refuses an unrecorded target, changed source baseline, different target repository ID, mismatching target commit, existing unrecorded Vercel project, unexpected linked repo/domain, non-READY deployment, or homepage response other than HTTP 200. Git writes target only; the original source is cloned read-only and never used as a push remote.
- State and a capped sanitized audit history live in owner-private `blog-setup.sqlite3`; provider secrets do not. `/blog cancel` expires pending proposals and does not delete external resources or revoke provider tokens.

## Verification

- `tests/personal_agent/test_blog_setup.py`: 5 passed. Coverage includes fixed-destination/message-bound one-use proposals, staged mock GitHub/Vercel provisioning through READY/HTTP 200, account and repository identity checks, private one-use credential enrollment, CSRF/cross-origin rejection, credential validation/storage/removal, and Telegram callback issuance. The orchestration test replaces the actual Git clone/push boundary with a recorder; it does not verify a live or local-network Git history transfer.
- Related suites: `tests/personal_agent/test_blog_setup.py tests/personal_agent/test_memory_explorer.py tests/personal_agent/test_remote_sessions.py tests/personal_agent/test_telegram_controller.py` — 61 passed, 2 deselected because the managed sandbox forbids the loopback socket binds used by those two existing live-server tests. This is not evidence that those listener tests passed.
- Ruff, BasedPyright (0 errors), Python compileall, and `bash ops/worker/test-static.sh` passed on the touched implementation and test files/current worker policy.
- Fake-provider tests do not prove GitHub/Vercel behavior, provider permissions, pricing, GitHub App authorization, Keychain runtime behavior, HTTPS/Tailscale delivery, or actual deployment.

## Not done / owner actions

- The implementation is not deployed to the running controller or Tailscale Serve process. `/blog setup` is therefore not yet available from the live bot. Deployment requires a separate review/approval under the existing controller deployment procedure.
- No GitHub or Vercel credential was provided or stored. After deployment, the owner must use the private setup page, enroll the separate minimum-permission GitHub credentials and Vercel token, and complete any GitHub App authorization directly with Vercel.
- No provider resource was created. Each external write still requires a new explicit approval from its exact Telegram proposal. Before approving project creation/deployment, the owner must review the current Vercel plan and any applicable usage charges; the flow does not upgrade a plan, purchase a domain, or touch the production project.
- Live Keychain, GitHub repository creation/history transfer, Vercel project connection, deployment READY/HTTP 200, resume/recovery after an uncertain provider response, and the final returned website URL are NOT RUN.
- First live publishing and autonomous publishing schedules remain out of scope and require separate future approval.

No Phase 08.0 external provisioning acceptance is claimed. See `docs/telegram-setup.md`, `docs/architecture.md`, `docs/features.md`, and `docs/status.md` for operator and boundary notes.
