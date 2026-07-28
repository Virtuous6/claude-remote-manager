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
7. Create the agent and add it to its channels.

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

Every factory agent has an independent `crons` array in:

```text
~/.claude-remote/<instance>/factory/agents/<slug>/config.json
```

New agents start with no schedules. The owner may tell an agent to create,
change, pause, or remove one. Until cron management is standardized in the
factory instructions, say **save this as a persistent CRM cron**. The agent
must:

1. create the active Claude schedule;
2. write the definition to its own `config.json`;
3. verify it is active;
4. recreate it from config after session refreshes or restarts.

A basic interval definition is:

```json
{
  "name": "project-check",
  "interval": "30m",
  "prompt": "Review the workspace and record material changes."
}
```

Existing agents may also use a `cron` expression for calendar schedules. Store
an explicit time zone with the task instructions until the control layer has a
first-class `timezone` field.

### Delivery Boundary

CRM crons run inside the local Claude session. They can work on local files,
repositories, and the CRM agent bus. They run only while the Mac and that
agent's session are available.

A factory agent cannot currently initiate a new Buzz message from a local cron.
ACP is turn-based: Buzz retains the managed private key and gives CRM a
correlated reply path only after a Buzz event starts a turn. The local tmux
session intentionally does not receive that private key.

Use:

- a Buzz Workflow to trigger scheduled work that must publish in Buzz;
- a CRM cron for local or computer-dependent work;
- Telegram only after an agent receives its own optional token;
- the workspace to hold cron output for the next Buzz turn.

Steve's existing proactive Buzz helper is a fixed, Steve-specific compatibility
path. It is not inherited by factory agents and should not become the generic
credential model.

### Planned Cron Control Layer

The intended conversational interface is:

```text
create · list · pause · resume · delete · run now
```

Each schedule should eventually record:

```text
name
schedule
timezone
task
destination
missed-run policy
busy-run policy
enabled status
last run
next run
last error
```

Required behavior:

- owner or allowlist authorization for schedule mutations;
- atomic config updates and idempotent reconciliation after restart;
- no duplicate loops;
- explicit `skip`, `run-on-wake`, or `backfill` behavior after downtime;
- explicit `queue`, `skip`, or `overlap` behavior when the agent is busy;
- visible run status and errors;
- awareness that frequent schedules consume Claude usage.

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
