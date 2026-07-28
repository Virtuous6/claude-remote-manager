# Steve ACP

`integrations/steve_acp.py` exposes the existing CRM Steve session as an ACP v2
stdio runtime for Buzz.

## Data Flow

```text
Buzz Desktop → buzz-acp → steve_acp.py → CRM inbox → Steve
                                             ↑         │
                                             └─ reply ─┘
                              steve_acp.py → Buzz CLI
```

Telegram continues to enter the same Steve session through CRM.

## Buzz Custom Harness

- Name: `Steve Kingsley (CRM)`
- ID: `steve-acp`
- Command: `/opt/homebrew/bin/python3.11`
- Argument:
  `/Users/josephsanchez/repos/claude-remote-manager/integrations/steve_acp.py`
- Environment: `CRM_INSTANCE_ID=default`

Buzz stores the local definition at:

```text
~/Library/Application Support/xyz.block.buzz.app/custom_harnesses/steve-acp.json
```

## Validation

```bash
python3.11 -m unittest discover -s tests
python3.11 -m ruff check integrations/steve_acp.py tests/test_steve_acp.py
```

## ACP-only Trial

Started 2026-07-28 after confirming:

- private-channel mention and threaded reply;
- member retagging;
- mid-turn `!cancel`;
- Steve personality and local memory;
- Telegram coexistence;
- Buzz restart recovery;
- offline store-and-forward after the Mac reconnects.

The legacy `com.neustac.steve-buzz-bridge` LaunchAgent is disabled and unloaded.
The legacy Steve identity was removed from the community after ACP parity was
confirmed. Its configuration snapshot is at:

```text
~/.config/buzz/steve-kingsley/backups/20260728-1221-acp-cutover
```

No legacy code, identity, or LaunchAgent file has been deleted.

Rollback requires inviting the legacy identity back to the community before
restarting its bridge:

```bash
launchctl enable gui/$(id -u)/com.neustac.steve-buzz-bridge
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.neustac.steve-buzz-bridge.plist
```

Do not run both Steve identities in the same production channels; duplicate
mentions can produce conflicting replies.

## Constraints

- CRM emits a final response, so ACP receives one message chunk rather than
  token-by-token streaming.
- Buzz-provided MCP servers cannot be attached dynamically to the existing
  Claude process. The adapter publishes through the inherited Buzz CLI identity.
- Buzz's managed system prompt is included with each correlated CRM turn so
  native mention, threading, and collaboration policies still reach Steve.
- ACP cancellation sends `Ctrl-C` only when an ACP turn owns the shared Steve
  session. This can still interrupt concurrent Telegram work.

## One-Brain Boundary

This harness is a doorway to the existing `steve-kingsley` CRM agent. It is not
an agent factory.

Creating another Buzz identity such as `Fizz` with this harness would still
route its work into Steve's Claude session. The identities would share:

- short-term context and device memory;
- Claude model, subscription, tools, and Telegram access;
- one processing queue and cancellation boundary;
- restart and context-rotation lifecycle.

That can be intentional for clearly labeled Steve roles, but it is unsafe for
independent personas because instructions, context, permissions, and replies
could cross boundaries.

The generic `crm-acp` adapter now maps each Buzz agent to a distinct CRM agent:

```text
Buzz Steve  → CRM steve-kingsley → Claude session A
Buzz Fizz   → CRM fizz           → Claude session B
Buzz Rachel → CRM rachel         → Claude session C
```

Each mapped agent must have its own identity, OS/CRM agent, Claude session,
memory, model configuration, permissions, queue, and cancellation scope.

See `docs/CRM_ACP.md`. Maxine is the first isolated pilot.

## Registering Steve in Buzz

Create one Buzz managed agent named `Steve Kingsley` and select
`Steve Kingsley (CRM)` as its harness. This makes Steve visible in Buzz's Agents
tab while computation remains in the existing CRM Steve session.

The harness uses Claude's active CRM/CLI default model and advertises one fixed
Buzz model choice: `Existing Steve CRM session — Claude default`. The label is
informational; selecting it does not start or switch a Claude session.

Buzz will create a managed Buzz identity for the new agent. Treat it as a
migration from the current bridge identity:

1. Create the managed Steve without adding both identities to the same channels.
2. Verify DM, mention, thread, retagging, cancellation, and Telegram behavior.
3. Disable the old bridge temporarily.
4. Add the managed identity to Steve's production channels.
5. Run ACP-only before deleting the old bridge service.

Do not leave both Steve identities subscribed to the same production mentions;
that can produce duplicate or conflicting replies.
