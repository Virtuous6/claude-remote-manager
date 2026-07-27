#!/usr/bin/env python3
"""Send an approved local file from Steve to Joe's fixed Buzz DM."""

from __future__ import annotations

import argparse
from pathlib import Path

from integrations.buzz_bridge import BuzzBridge, validate_outbound_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("message", nargs="?", default="File from Steve")
    arguments = parser.parse_args()
    config = Path.home() / ".config/buzz/steve-kingsley"
    bridge = BuzzBridge(
        config / "identity.json",
        Path.home() / ".claude-remote/default",
        config / "bridge-state.json",
        policy_path=config / "policy.json",
    )
    approved = validate_outbound_file(arguments.file, bridge.policy)
    print(
        bridge.buzz(
            "messages",
            "send",
            "--channel",
            bridge.policy.joe_dm_channel,
            "--content",
            arguments.message,
            "--file",
            str(approved),
        )
    )


if __name__ == "__main__":
    main()
