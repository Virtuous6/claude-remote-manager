# Research Brief: Steve ACP

**Date:** 2026-07-28
**Verdict:** PROCEED

## API Feasibility

| Surface | Viable? | Capabilities | Limitations |
|---|---|---|---|
| Buzz BYOH | Yes | Launches any ACP stdio command; supplies Buzz identity and relay environment | Local runtime sleeps with the Mac |
| ACP v2 | Yes | `initialize`, `session/new`, `session/prompt`, `session/cancel`, updates | CRM provides final replies, not token streaming |
| CRM agent bus | Yes | Routes Buzz and Telegram into one Steve session | One global Steve turn at a time |
| Buzz MCP injection | Limited | Buzz supplies MCP servers during `session/new` | Existing Claude session cannot attach MCP servers dynamically |

## Technology

- Build a dependency-free Python ACP stdio adapter.
- Translate ACP prompts into CRM inbox envelopes.
- Wait for Steve's correlated agent-bus reply.
- Publish the reply with the Buzz CLI using the channel and reply anchor from
  Buzz's prompt.
- Emit Steve's final response as an ACP `agent_message_chunk`.
- Keep the existing Buzz bridge live during validation.

## Credentials

No new credentials. Buzz injects its managed identity environment. CRM and
Telegram keep their current local configuration.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Duplicate replies during side-by-side test | High | Do not assign both runtimes the same live identity/channel |
| Buzz cancellation interrupts Telegram work | Medium | Cancel only the active ACP-correlated Steve turn |
| Adapter exits before reply | Medium | Atomic bus files, correlated IDs, bounded wait |
| Dynamic Buzz MCP unavailable to Steve | Medium | Adapter owns Buzz publish; retain validated read-only CRM Buzz tools |
