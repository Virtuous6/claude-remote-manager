# Claude Remote Manager — Handoff
**Date:** 2026-07-06
**Session:** Steve 52h outage — TCC/FDA root cause, dead-pane guard, backlog committed

---

## TL;DR

Steve was dead Jul 4 06:00 → Jul 6 10:01. The 71h session-refresh relaunch died instantly with `error: An internal error occurred (EPERM)`; pane fell to bash and fast-checker pasted Joe's Telegram messages INTO bash (executed as shell commands). Root cause: tmux had an explicit Full Disk Access DENY in TCC — any claude with cwd under `~/Documents` EPERMs at startup in the tmux-server context. Fixed by granting tmux FDA (System Settings) + hard-restart. Dead-pane guard added to fast-checker.

---

## What's Done (This Session)

- Root-caused the EPERM: reproduced in a scratch window of the live server (`ls ~/Documents` fails; claude with cwd `~/repos` boots, cwd `~/Documents/...` dies — any version).
- Joe granted `/opt/homebrew/Cellar/tmux/3.6a/bin/tmux` FDA 10:00; hard-restart 10:01; verified DOCS_OK, clean boot, Telegram commands registered.
- `inject_messages()` dead-pane guard: refuses to paste when `pane_current_command` is a shell; does NOT commit Telegram offset (message retries after recovery). Deny-lists shells — claude shows as version basename (`2.1.201`), allow-list would false-refuse.
- Committed the dirty worktree (was uncommitted since ~May): is_agent_idle spinner fix, dead-pane guard, sanitize-claude-history + test, live steve config (incl. rachel-daily-fact cron), gitignore for telegram-docs/ + config backups.

---

## What's Next

### Priority 1: Watch Thu Jul 9 ~10am session refresh
**What:** First natural exercise of the timer-continue path since the fix. If Steve survives, case closed.
**Files:** `~/.claude-remote/default/logs/steve-kingsley/crashes.log`, `activity.log`

### Priority 2: Relaunch retry in agent-wrapper
**What:** Timer-refresh relaunch is one-shot (`agent-wrapper.sh` ~line 376). One transient failure = dead until noticed. Add retry + fall back to fresh + Telegram alert on final failure.
**Files:** `core/scripts/agent-wrapper.sh`

### Priority 3: Homebrew tmux upgrade will break the FDA grant
**What:** Grant is keyed to the Cellar path/binary. `brew upgrade tmux` → new path → grant dead → same outage. Re-grant after upgrades. Check: `sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" "SELECT client,auth_value FROM access WHERE service='kTCCServiceSystemPolicyAllFiles' AND client LIKE '%tmux%';"` (2=allowed).

---

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| Grant FDA to tmux binary | Server context needs Documents (Steve cwd + vault). Wrapper-side avoidance (c1776b9) can't cover in-pane claude |
| Dead-pane guard deny-lists shells | Fails closed on the actual hazard; claude's pane command name is version-dependent |
| Guard runs before dedup write | Otherwise a refused message would be dedup-skipped on retry |
| Commit live config.json | Agents-are-files: board truth in git; private repo |

## Open Questions

1. Why did Documents access work Jun 30→Jul 4 then stop? No OS update, no TCC row change in window. Unresolved — fix holds regardless.
2. Merge `joe/canonical-crm-symlink` → main done locally this session; delete branch after Thu proves stable?

---

## Key Files

| File | Purpose |
|------|---------|
| `core/scripts/fast-checker.sh` | Telegram poller, watchdog, dead-pane guard |
| `core/scripts/agent-wrapper.sh` | launchd wrapper, tmux lifecycle, 71h refresh (one-shot relaunch — P2) |
| `agents/steve-kingsley/config.json` | Steve thresholds/crons/working directory |
| `~/.claude-remote/default/logs/steve-kingsley/` | crashes.log, restarts.log, activity.log, fast-checker.log |
