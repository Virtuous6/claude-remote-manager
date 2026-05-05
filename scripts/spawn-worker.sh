#!/bin/bash
# spawn-worker.sh — clone worker-template, customize, register as a new CRM agent
# Usage: bash spawn-worker.sh <worker-name> <parent-agent> [skill-pack...]
# Example: bash spawn-worker.sh scribe helm writing brand-voice content

set -e

WORKER_NAME="$1"
PARENT="$2"
shift 2 || true
SKILL_PACK="$@"

if [ -z "$WORKER_NAME" ] || [ -z "$PARENT" ]; then
    echo "Usage: spawn-worker.sh <worker-name> <parent-agent> [skill-pack...]"
    echo "  worker-name: e.g. scribe, lens, forge, sage"
    echo "  parent-agent: usually 'helm' (the Operator)"
    echo "  skill-pack: space-separated list of skills to install"
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO_ROOT/agents/worker-template"
TARGET="$REPO_ROOT/agents/$WORKER_NAME"

if [ -d "$TARGET" ]; then
    echo "Worker '$WORKER_NAME' already exists at $TARGET"
    exit 1
fi

if [ ! -d "$TEMPLATE" ]; then
    echo "Template missing: $TEMPLATE"
    exit 1
fi

# Clone template
cp -r "$TEMPLATE" "$TARGET"

# Update config.json with worker name, parent, and skill pack
python3 -c "
import json, sys
with open('$TARGET/config.json') as f: cfg = json.load(f)
cfg['agent_name'] = '$WORKER_NAME'
cfg['parent'] = '$PARENT'
cfg['skill_pack'] = '$SKILL_PACK'.split() if '$SKILL_PACK' else []
with open('$TARGET/config.json','w') as f: json.dump(cfg, f, indent=2)
print(f'  config updated: parent={cfg[\"parent\"]}, skill_pack={cfg[\"skill_pack\"]}')"

# .env stub — user must supply BOT_TOKEN (workers use agent-bus only, no Telegram by default)
cat > "$TARGET/.env" << EOF
# Worker agents don't need a Telegram bot by default — they communicate via the agent bus.
# If you want this worker to also receive Telegram messages, add:
# BOT_TOKEN=<token>
# CHAT_ID=<chat_id>
# ALLOWED_USER=<user_id>
EOF

echo "Worker spawned: $WORKER_NAME"
echo "  Path: $TARGET"
echo "  Parent: $PARENT"
echo "  Skills: $SKILL_PACK"
echo ""
echo "Next steps:"
echo "  1. Install skill pack into $TARGET/skills/ (copy from ~/.claude/skills/)"
echo "  2. Enable: bash $REPO_ROOT/enable-agent.sh $WORKER_NAME"
echo "  3. Register in funnel-map workspace.json as agents[] entry with role='worker', parent='$PARENT'"
