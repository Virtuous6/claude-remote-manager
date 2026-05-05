# Claude Remote Manager — Handoff
**Date:** 2026-04-03
**Session:** PA integration — Steve's working_directory = PA repo, slim CLAUDE.md overlay

---

## TL;DR

Steve is now a thin Telegram overlay on the PA repo — one system, two interfaces. Committed and pushed to `Virtuous6/claude-remote-manager`.

---

## What's Done (This Session)

- Set `working_directory` in `config.json` to `/Users/josephsanchez/Documents/repos/power-assistant-joe` — PA CLAUDE.md loads automatically
- Rewrote `agents/steve-kingsley/CLAUDE.md` from 153 lines to 87 — stripped all duplicated PA content (identity, skills, rules, paths, activity logging), kept only Telegram-specific ops
- Updated cron prompts to use relative paths (`orchestration/workflows/...`) instead of absolute `~/.claude/` paths
- Fixed old `power assistant/` vault paths that would have broken Steve (deleted folder)
- Fixed tag format: `LEVANTAGE`/`NEUSTAC`/`PERSONAL` → `LEV`/`NEU`/`PER`
- Fixed brief path: hardcoded `2026/03/` → dynamic `{YYYY}/{MM}/` with `_CC/` prefix
- Forked repo to `Virtuous6/claude-remote-manager`, set origin, pushed
- Added `.playwright-mcp/` to `.gitignore`

---

## What's Next

### Priority 1: Test Steve with new working_directory
**What:** Enable Steve, verify PA CLAUDE.md loads, crons fire, Telegram works
**Depends on:** MCP auth may need manual approval (attach to tmux)

### Priority 2: Verify cron relative paths work
**What:** Cron prompts now say `orchestration/workflows/agent-work-loop/SKILL.md` (relative). Confirm CRM resolves these from working_directory.
**Depends on:** Priority 1

### Priority 3: Consider consolidating Desktop + Steve crons
**What:** Desktop runs morning brief, guardian, pulse, weekly, monthly, contact builder. Steve runs agent-work-loop, beeper-monitor, inbox-check. Document which runs where and why.
**Depends on:** Nothing

### Priority 4: Steve is disabled
**What:** `config.json` has `"enabled": false`. Need to enable when ready to test.
**Depends on:** Priority 1

---

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| working_directory = PA repo | PA CLAUDE.md loads automatically, all PA changes flow to Steve for free |
| Steve CLAUDE.md = CRM-only overlay | Eliminates dual maintenance — identity, skills, paths all inherited from PA |
| Crons use relative paths | Resolved from working_directory, no hardcoded absolute paths to break |
| Desktop runs file-only tasks, Steve runs Telegram-output tasks | Clean split — Desktop writes briefs, Steve surfaces them to Joe's phone |
| Forked to Virtuous6 | Can't push to grandamenium upstream, need our own remote |

---

## Blockers

| Blocker | Impact | Who/What Unblocks |
|---------|--------|-------------------|
| Steve disabled | Can't test integration | `./enable-agent.sh steve-kingsley` |
| MCP auth on first boot | Steve may lack Gmail/Calendar/Beeper | Attach to tmux, approve prompts |

---

## Open Questions

1. Do CRM cron prompts resolve relative paths from working_directory or from the agent directory?
2. Should Steve also run morning brief / guardian, or keep that split with Desktop?
3. Steve's `scheduled_tasks.lock` — should it be gitignored?

---

## Key Files

| File | Purpose |
|------|---------|
| `agents/steve-kingsley/CLAUDE.md` | Telegram-only overlay (87 lines) |
| `agents/steve-kingsley/config.json` | working_directory + 4 crons |
| `agents/steve-kingsley/.env` | Bot token + chat ID (gitignored) |
| `repos/power-assistant-joe/CLAUDE.md` | PA operating manual (auto-loaded via working_directory) |

---

## Suggested Next Session Flow

1. `/pickup` — read this handoff
2. `./enable-agent.sh steve-kingsley` — start Steve
3. Attach to tmux (`tmux attach -t crm-default-steve-kingsley`), approve MCP auth if prompted
4. Send Steve a test message on Telegram — verify PA identity loads
5. Wait for a cron to fire — verify relative path resolution
6. If working: commit any fixes, update memory
7. If broken: check if cron paths resolve from working_directory or agent directory, fix accordingly
