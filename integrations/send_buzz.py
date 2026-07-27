#!/usr/bin/env python3
"""Send a proactive message from Steve to Joe's fixed Buzz DM."""

from __future__ import annotations

import argparse
from pathlib import Path

from integrations.buzz_bridge import BuzzBridge, send_proactive


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    arguments = parser.parse_args()
    bridge = BuzzBridge(
        Path.home() / ".config/buzz/steve-kingsley/identity.json",
        Path.home() / ".claude-remote/default",
        Path.home() / ".config/buzz/steve-kingsley/bridge-state.json",
        policy_path=Path.home() / ".config/buzz/steve-kingsley/policy.json",
    )
    print(send_proactive(bridge.buzz, arguments.message))


if __name__ == "__main__":
    main()
