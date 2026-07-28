# CRM ACP

`integrations/crm_acp.py` exposes one isolated Claude Remote Manager agent as a
Buzz ACP v2 runtime.

## Boundary

```text
Buzz identity
  -> Buzz-managed ACP worker and encrypted core
  -> agent-specific ACP return inbox
  -> agent-specific CRM inbox
  -> agent-specific Claude tmux session
  -> agent-specific working directory and local history
```

An agent never shares another agent's CRM inbox, return inbox, tmux target,
working directory, or short-term Claude history.

Buzz owns identity, membership, mentions, threads, presence, delivery, and
portable encrypted memory. CRM owns the persistent Claude session, local tools,
working files, and deeper operational context.

## Memory

Buzz injects the managed agent's encrypted `core` into `systemPrompt` on
`session/new`. CRM ACP forwards it into the correlated turn.

An agent may return one explicit `buzz_memory_updates` entry for `core`.
Ordinary reply prose is never interpreted as a memory command. The adapter:

1. validates a full replacement core at 16 KiB or less;
2. permits only the `core` slug;
3. reads the current encrypted core with the worker's inherited Buzz identity;
4. applies a unified diff with an exact SHA-256 base hash;
5. publishes the visible reply only after the memory write succeeds.

New core is created with `buzz mem set core -`. Existing core uses
`buzz mem patch core --base-hash ...`, so concurrent changes fail closed.
Values travel over stdin, not command arguments or logs.

The CRM agent creates a core replacement only at:

```text
~/.claude-remote/<instance>/state/<agent>-core.md
```

`send-acp-reply.sh` rejects other paths, symlinks, unsupported slugs, empty
values, and oversized values.

## Maxine Pilot

Maxine is the first independent CRM ACP role.

- Buzz homebase: managed agent `Maxine`
- Harness: `Maxine (1000 Months CRM)`
- CRM agent: `maxine`
- ACP inbox: `buzz-acp-maxine`
- tmux: `crm-default-maxine`
- Workspace: `1000months`
- Telegram: disabled
- Crons: none
- Model label: `Existing Maxine CRM session - Claude default`

Her seed core is `agents/maxine/CORE.md`. On the first Buzz turn, the no-core
system prompt tells her memory is empty. She sends the seed as an explicit core
update with her response. Later durable changes to identity, writing rules, or
goals may update core without owner approval.

Maxine must never store secrets, unpublished writing, client material, or
sensitive personal details in Buzz core.

## Register Harness

The preferred runtime is the factory harness:

```text
integrations/harnesses/crm-acp.json
```

to:

```text
~/Library/Application Support/xyz.block.buzz.app/custom_harnesses/crm-acp.json
```

Restart or reopen Buzz's runtime screen. The selectable runtime is `CRM ACP
(Auto-Provision)`.

The fixed `maxine-acp.json` and `steve-acp.json` harnesses remain compatible for
existing agents. Do not use them for new roles.

## Create An Agent In Buzz

1. Choose **Create agent** in Buzz.
2. Set the agent name and instructions.
3. Choose **Customize for this agent**.
4. Select **CRM ACP (Auto-Provision)**.
5. Leave its fixed CRM model selected.
6. Optional: under Advanced, set `CRM_WORKSPACE` to an existing absolute
   directory under `~/Documents` or `~/repos`.
7. Under Advanced, set the Buzz response policy to **Anyone**. CRM applies the
   narrower owner/sibling/attributed-workflow gate described below.
8. Create the agent and add it to its channels.

On first spawn, the factory automatically creates:

```text
~/.claude-remote/<instance>/factory/
  identities/<fingerprint>.json
  agents/<slug>/
  workspaces/<slug>/
```

It also registers a dedicated launchd service, tmux session, CRM inbox, ACP
return inbox, Claude session UUID, and local history boundary. The Buzz agent
name is presentation only. Renaming the agent reuses the same local identity.

Buzz creates and holds each managed private identity. CRM hashes that identity
in memory and persists only its SHA-256 fingerprint. Private keys, auth tags,
Telegram tokens, and other credentials never belong in a harness or factory
record.

Factory directories are mode `0700`; records and generated agent files are
`0600`. A Buzz model-discovery launch without a managed identity returns the
fixed model but cannot process prompts. A managed launch missing its private
identity fails closed.

The generated agent receives its persona, role, and encrypted core from Buzz on
each ACP turn. CRM supplies the persistent Claude process, workspace, tools,
queues, and local history. Telegram and crons default off.

## Per-Agent Crons

CRM ACP now has two independent scheduling planes:

- Buzz Workflows for reliable work that must wake the agent and publish in Buzz;
- local CRM crons for computer-dependent or file-only work.

### Buzz Workflow Control

Ask the agent in the destination Buzz channel:

```text
Every weekday at 3pm UTC, review the writing corner and post the next useful
move here. Save it as weekday-writing.
```

The agent emits one typed operation in its correlated ACP reply. The adapter:

1. validates the operation;
2. builds the workflow definition itself;
3. uses the managed Buzz identity to create or update the channel workflow;
4. stores the returned workflow ID in an authenticated private registry;
5. appends the confirmed result to the visible Buzz reply.

The local Claude/tmux session never receives the Buzz private key or auth tag.
It cannot provide raw YAML, a channel ID, a workflow ID, or a shell command.

Supported conversational actions:

```text
create or update · list · pause · resume · delete · run now
```

Example operation:

```json
{
  "action": "upsert",
  "name": "weekday-writing",
  "cron": "0 15 * * 1-5",
  "timezone": "UTC",
  "task": "Review the writing corner and post the next useful move."
}
```

Interval schedules use `interval` instead of `cron`, with a minimum of 60
seconds. Calendar schedules use five-field cron and are UTC because Buzz's
current workflow scheduler has no time-zone field. Tasks cannot contain `@`
mentions; the adapter owns the only mention and targets the exact managed agent
name.

Workflow metadata is stored at:

```text
~/.claude-remote/<instance>/state/<agent>-buzz-workflows.json
```

The registry is mode `0600`, written atomically under a per-agent lock, and
HMAC-authenticated with a one-way key derived in the adapter from the stable
managed private identity. The identity and auth tag are never persisted.
Tampering fails closed before a Buzz command runs. Repeating an upsert updates
the recorded workflow rather than creating a duplicate.

At runtime, the Buzz scheduler posts a top-level exact-name mention in the bound
channel. Buzz resolves that channel member mention, starts a normal managed ACP
turn, and CRM returns the work in the workflow message's thread. If an agent is
renamed, adapter startup reconciles every recorded workflow to the new exact
display name.

Security requirement: set the managed agent's Buzz response policy to
`anyone`. This is a transport setting, not the effective CRM policy. Buzz
Workflow messages are signed by the community relay, so Buzz ACP's default
`owner-only` gate drops them before the custom harness can inspect them.

CRM immediately narrows the `anyone` transport back to:

- the cryptographically attested owner;
- agents carrying a valid NIP-OA attestation for that same owner;
- relay-signed `buzz:workflow` events whose first attribution tag is this
  managed agent.

The trusted relay public key is pinned in the harness. Other humans,
unattributed relay events, forged sibling attestations, and workflows owned by
another identity fail before reaching the CRM inbox. Only the direct owner may
create, change, pause, resume, delete, or run a workflow.

For Neustac, the pinned relay identity is the documented `Buzz Relay Service`
admin identity. If the community relay key rotates, update and redeploy every
CRM harness before restoring schedule delivery.

Current limits:

- Buzz calendar cron is UTC; local-wall-clock/DST schedules are not translated.
- Buzz's current CLI run-history command returns no relay execution events, so
  CRM lists configuration status but not authoritative last/next-run history.
- Workflow-generated messages are top-level. The agent's answer is threaded.
- Busy-run and missed-run behavior belongs to Buzz's scheduler/ACP queue and is
  not configurable in CRM yet.
- A workflow stays bound to its original channel. Updating it elsewhere changes
  the task or schedule, not the destination.
- A new managed agent starts `owner-only`; switch it to `anyone` after selecting
  CRM ACP or its workflow posts will remain visible but will not wake the agent.

### Local CRM Crons

Every factory agent also has an independent `crons` array in:

```text
~/.claude-remote/<instance>/factory/agents/<slug>/config.json
```

New agents start with none. A local cron can work on files, repositories, the
local CRM bus, or an agent-specific Telegram bot. It runs only while the Mac and
that agent's Claude session are available. It cannot initiate a managed Buzz
message.

Ask the agent to **save this as a persistent local CRM cron**. It must:

1. create the active Claude `/loop`;
2. persist the definition in its own `config.json`;
3. verify it is active;
4. recreate it after a session refresh or restart.

Example:

```json
{
  "name": "project-check",
  "interval": "30m",
  "prompt": "Review the workspace and record material changes."
}
```

Steve may still use his local schedules for Telegram work. A Telegram request
does not carry an authenticated Buzz ACP turn, so it cannot create or mutate a
Buzz Workflow. Frequent schedules consume Claude usage in either plane.

## Validation

```bash
python3.11 -m unittest tests.test_crm_acp tests.test_steve_acp
bash tests/smoke-bash-syntax.sh
python3.11 -m coverage run -m unittest discover -s tests
python3.11 -m coverage report
```

Pilot checks:

1. mention and threaded response;
2. member retagging;
3. cancellation;
4. writing voice and stage rules;
5. initial encrypted core creation;
6. later core patch and next-session reinjection;
7. offline store-and-forward after the Mac reconnects;
8. Steve and Maxine simultaneous turns with no inbox crossover.

## Isolation

```text
one Buzz identity
  = one factory identity record
  = one CRM agent
  = one ACP return inbox
  = one tmux session
  = one exact Claude session UUID
```

Agents may deliberately share a working directory while retaining distinct
Claude session UUIDs and histories. Default workspaces are separate.

Archiving or deleting an agent in Buzz does not delete local files or unload its
launchd service in this first release. Remove that local runtime separately
after confirming the Buzz identity is no longer needed.

Back up important workspaces separately. Buzz encrypted memory is portable, but
local workspace files, CRM config, Claude history, and cron definitions are not
Buzz backups.
