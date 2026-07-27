#!/usr/bin/env python3
"""Run allowlisted, read-only Buzz queries as Steve."""

from __future__ import annotations

import argparse
from pathlib import Path

from integrations.buzz_bridge import BuzzBridge, read_command


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)

    search = subparsers.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)

    feed = subparsers.add_parser("feed")
    feed.add_argument("--limit", type=int, default=20)

    subparsers.add_parser("channels")

    members = subparsers.add_parser("members")
    members.add_argument("channel")

    thread = subparsers.add_parser("thread")
    thread.add_argument("channel")
    thread.add_argument("event")
    thread.add_argument("--limit", type=int, default=20)

    arguments = parser.parse_args()
    command = read_command(
        arguments.action,
        query=getattr(arguments, "query", ""),
        channel=getattr(arguments, "channel", ""),
        event=getattr(arguments, "event", ""),
        limit=getattr(arguments, "limit", 20),
    )
    bridge = BuzzBridge(
        Path.home() / ".config/buzz/steve-kingsley/identity.json",
        Path.home() / ".claude-remote/default",
        Path.home() / ".config/buzz/steve-kingsley/bridge-state.json",
        policy_path=Path.home() / ".config/buzz/steve-kingsley/policy.json",
    )
    print(bridge.buzz(*command))


if __name__ == "__main__":
    main()
