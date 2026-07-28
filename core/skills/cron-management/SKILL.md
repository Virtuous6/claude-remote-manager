---
name: cron-management
description: "Manage scheduled tasks (crons). Use when: setting up crons on session start, creating new recurring tasks, or troubleshooting scheduled tasks."
---

# Cron Management

Use the correct scheduler for the destination:

- Buzz Workflow: reliable work that must wake you and publish in Buzz
- local CRM cron: computer-dependent or file-only work

Never move Buzz credentials into the local Claude session.

## Buzz Workflow Schedules

Only create or mutate a schedule when the owner explicitly asks during a Buzz
ACP turn. The managed agent's Buzz response policy must be `Anyone` or an
`Allowlist` containing the pinned community relay so relay-signed workflow
posts reach CRM. Prefer `Allowlist`; retain any people the owner selected.
Buzz still admits the owner and verified same-owner agents implicitly. CRM
mirrors the selected policy and independently verifies attributed relay
workflows.

Supported actions:

```text
upsert · list · pause · resume · delete · run_now
```

Write one operation to:

```text
$CRM_ROOT/state/$CRM_AGENT_NAME-workflow-op.json
```

Then include it in the correlated reply:

```bash
bash "$CRM_TEMPLATE_ROOT/core/bus/send-acp-reply.sh" \
  <adapter> <turn-id> '<reply>' \
  --workflow "$CRM_ROOT/state/$CRM_AGENT_NAME-workflow-op.json"
```

Calendar example:

```json
{
  "action": "upsert",
  "name": "weekday-review",
  "cron": "0 15 * * 1-5",
  "timezone": "UTC",
  "task": "Review the project and post the next useful move."
}
```

Interval example:

```json
{
  "action": "upsert",
  "name": "project-check",
  "interval": "30m",
  "task": "Review the project and post material changes."
}
```

Mutation example:

```json
{"action": "pause", "name": "project-check"}
```

Rules:

- schedule names are lowercase slugs;
- intervals are at least 60 seconds;
- cron expressions are five fields and UTC;
- tasks cannot contain `@` mentions;
- use the local schedule name for mutations;
- never supply raw workflow YAML, a channel UUID, or workflow UUID;
- do not claim success before the adapter confirmation appears in Buzz.

Buzz posts a top-level exact-name mention in the bound channel. That mention
wakes the managed ACP agent; its result replies in the workflow post's thread.
The operation file is consumed after the reply envelope is written.

## Local CRM Crons

## On Session Start

Check if your crons are active. If not, recreate them:

1. Read `config.json` to get your cron definitions
2. For each entry in the `crons` array, create a loop: `/loop {interval} {prompt}`
3. Verify all crons are running

## Default Crons

No crons are defined by default. Users can add any recurring tasks they need to `config.json`.

## Adding a Local Cron

1. Create the `/loop` for immediate use: `/loop {interval} {prompt}`
2. **Persist it** - Add the cron to `config.json` so it survives restarts:
   ```json
   {"name": "descriptive-name", "interval": "5m", "prompt": "What to do each cycle"}
   ```
3. Confirm to the user that the cron is active and persisted

## Removing a Local Cron

1. Cancel the active `/loop`
2. Remove the entry from `config.json`

## Local Cron Expiry

Built-in `/loop` crons expire after 3 days. Since your session restarts via launchd, this isn't an issue - crons are recreated from `config.json` on each fresh start.

## Troubleshooting

- Buzz: list the adapter registry; confirm the policy is `Anyone` or the relay
  remains in `Allowlist`; remember calendar times are UTC.
- Local: check if the loop was created this session.
- Local: if crons are missing after restart, re-read `config.json` and recreate
  them.
