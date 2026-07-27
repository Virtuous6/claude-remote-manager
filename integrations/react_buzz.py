#!/usr/bin/env python3
"""React as Steve to a Buzz event."""

from __future__ import annotations

import argparse
from pathlib import Path

from integrations.buzz_bridge import BuzzBridge


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("event")
    parser.add_argument("emoji")
    arguments = parser.parse_args()
    bridge = BuzzBridge(
        Path.home() / ".config/buzz/steve-kingsley/identity.json",
        Path.home() / ".claude-remote/default",
        Path.home() / ".config/buzz/steve-kingsley/bridge-state.json",
        policy_path=Path.home() / ".config/buzz/steve-kingsley/policy.json",
    )
    print(
        bridge.buzz(
            "reactions",
            "add",
            "--event",
            arguments.event,
            "--emoji",
            arguments.emoji,
        )
    )


if __name__ == "__main__":
    main()
