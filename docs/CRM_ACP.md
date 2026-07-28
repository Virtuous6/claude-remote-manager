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

Copy:

```text
integrations/harnesses/maxine-acp.json
```

to:

```text
~/Library/Application Support/xyz.block.buzz.app/custom_harnesses/maxine-acp.json
```

Restart or reopen Buzz's runtime screen. Create a managed agent named `Maxine`,
select the harness, then add her only to a private 1000 Months pilot channel.

Buzz creates and holds Maxine's managed identity. Do not put a Buzz private key,
auth tag, Telegram token, or other credential in the harness JSON.

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

## Add Another Role

Create an agent directory and a harness definition using the generic adapter:

```text
crm_acp.py --agent <slug> --display-name <name>
```

Keep the one-to-one mapping:

```text
one Buzz identity
  = one harness definition
  = one CRM agent
  = one ACP return inbox
  = one tmux session
  = one working directory
```

Telegram remains optional per CRM agent with `telegram_enabled`.
