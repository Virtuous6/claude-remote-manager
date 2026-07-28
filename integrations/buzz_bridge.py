#!/usr/bin/env python3
"""Bridge Buzz channel messages into the existing CRM agent bus."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from integrations.nostr_ephemeral import EphemeralPublisher
except ModuleNotFoundError:
    from nostr_ephemeral import EphemeralPublisher

JOE_DM_CHANNEL = "09eec80f-1a32-41a6-bf46-399a79d87b67"
OWNER_PUBKEY = "91ef77d63dd6668711ca0a76a0cae780f50b9d960dd800b131d3971ab494bdea"
STEVE_PUBKEY = "b04f8a2725c6a7a07ddaf3125b38c1df1b5aa0f7a7a704e82266e6185f4a2c8d"
ALLOWED_AUTHORS = {
    OWNER_PUBKEY,
    "df7b96ba274d2462a6ed8ea0c3d8fbf887c4e9354b8de1c3e2d3daf0a4f3dba1",
    "8cbb201628b405db773b4e56c4fca03edd602a21d21e4c349d74c9559f054c43",
}
ALLOWED_ATTACHMENT_MIMES = {
    "application/pdf",
    "image/gif",
    "image/jpeg",
    "image/png",
    "text/csv",
    "text/markdown",
    "text/plain",
}
ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".csv",
    ".gif",
    ".jpeg",
    ".jpg",
    ".md",
    ".pdf",
    ".png",
    ".txt",
}
ALLOWED_OUTBOUND_MIMES = {
    "application/pdf",
    "image/gif",
    "image/jpeg",
    "image/png",
}
ALLOWED_OUTBOUND_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".pdf", ".png"}


@dataclass(frozen=True)
class SubscriptionRule:
    channels: set[str]
    kinds: set[int]
    require_mention: bool


def default_subscription_rules() -> tuple[SubscriptionRule, ...]:
    return (
        SubscriptionRule(
            channels={JOE_DM_CHANNEL},
            kinds={9, 46010, 40007},
            require_mention=False,
        ),
        SubscriptionRule(
            channels={"*"},
            kinds={9, 46010, 40007},
            require_mention=True,
        ),
        SubscriptionRule(
            channels={"*"},
            kinds={45001, 45003},
            require_mention=True,
        ),
    )


@dataclass(frozen=True)
class Policy:
    owner_pubkey: str
    allowed_authors: set[str]
    joe_dm_channel: str
    allowed_upload_roots: tuple[Path, ...]
    max_attachment_bytes: int = 10 * 1024 * 1024
    max_outbound_attempts: int = 5
    max_inbound_attempts: int = 5
    max_pending_per_channel: int = 500
    max_batch_events: int = 50
    stall_alert_seconds: int = 600
    max_turn_seconds: int = 7200
    subscription_rules: tuple[SubscriptionRule, ...] = field(
        default_factory=default_subscription_rules
    )


def default_policy() -> Policy:
    return Policy(
        owner_pubkey=OWNER_PUBKEY,
        allowed_authors=set(ALLOWED_AUTHORS),
        joe_dm_channel=JOE_DM_CHANNEL,
        allowed_upload_roots=(
            Path.home() / "repos/claude-remote-manager/agents/steve-kingsley",
            Path.home() / "Documents/OBSIDIAN/Lucky Obsidian/_CC",
        ),
    )


def load_policy(path: Path) -> Policy:
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise PermissionError(f"{path} must have mode 0600")
    payload = json.loads(path.read_text())
    owner = str(payload["owner_pubkey"])
    allowed = {str(value) for value in payload["allowed_authors"]}
    if not re.fullmatch(r"[0-9a-f]{64}", owner):
        raise ValueError("owner_pubkey must be 64 lowercase hex characters")
    if owner not in allowed or any(not re.fullmatch(r"[0-9a-f]{64}", key) for key in allowed):
        raise ValueError("allowed_authors must contain valid owner pubkey")
    max_inbound_attempts = int(payload.get("max_inbound_attempts", 5))
    max_pending_per_channel = int(payload.get("max_pending_per_channel", 500))
    max_batch_events = int(payload.get("max_batch_events", 50))
    stall_alert_seconds = int(payload.get("stall_alert_seconds", 600))
    max_turn_seconds = int(payload.get("max_turn_seconds", 7200))
    if (
        not 1 <= max_inbound_attempts <= 20
        or not 1 <= max_pending_per_channel <= 5000
        or not 1 <= max_batch_events <= 100
        or not 30 <= stall_alert_seconds <= 86400
        or not 60 <= max_turn_seconds <= 604800
        or stall_alert_seconds >= max_turn_seconds
    ):
        raise ValueError("policy bounds are unsafe or inconsistent")
    raw_rules = payload.get("subscription_rules")
    if raw_rules is None:
        subscription_rules = default_subscription_rules()
    else:
        subscription_rules = tuple(
            SubscriptionRule(
                channels={str(channel) for channel in rule.get("channels", [])},
                kinds={int(kind) for kind in rule.get("kinds", [])},
                require_mention=bool(rule.get("require_mention", True)),
            )
            for rule in raw_rules
        )
        if not subscription_rules or any(
            not rule.channels
            or not rule.kinds
            or any(kind <= 0 or kind > 65535 for kind in rule.kinds)
            for rule in subscription_rules
        ):
            raise ValueError("subscription rules must define channels and valid kinds")
    return Policy(
        owner_pubkey=owner,
        allowed_authors=allowed,
        joe_dm_channel=str(payload["joe_dm_channel"]),
        allowed_upload_roots=tuple(
            Path(value).expanduser().resolve()
            for value in payload.get("allowed_upload_roots", [])
        ),
        max_attachment_bytes=int(payload.get("max_attachment_bytes", 10 * 1024 * 1024)),
        max_outbound_attempts=int(payload.get("max_outbound_attempts", 5)),
        max_inbound_attempts=max_inbound_attempts,
        max_pending_per_channel=max_pending_per_channel,
        max_batch_events=max_batch_events,
        stall_alert_seconds=stall_alert_seconds,
        max_turn_seconds=max_turn_seconds,
        subscription_rules=subscription_rules,
    )


def send_proactive(buzz_call: Any, content: str) -> str:
    message = content.strip()
    if not message:
        raise ValueError("Buzz message cannot be empty")
    return buzz_call(
        "messages",
        "send",
        "--channel",
        JOE_DM_CHANNEL,
        "--content",
        "-",
        stdin=message,
    )


def accepts_event(
    event: dict[str, Any], channel_id: str, policy: Policy | None = None
) -> bool:
    policy = policy or default_policy()
    author = str(event.get("pubkey") or "")
    if author not in policy.allowed_authors:
        return False
    if channel_id == policy.joe_dm_channel and author != policy.owner_pubkey:
        return False
    return matches_subscription(event, channel_id, policy.subscription_rules)


def matches_subscription(
    event: dict[str, Any],
    channel_id: str,
    rules: tuple[SubscriptionRule, ...],
) -> bool:
    try:
        kind = int(event["kind"])
    except (KeyError, TypeError, ValueError):
        return False
    mentioned = any(
        isinstance(tag, list)
        and len(tag) >= 2
        and tag[0] == "p"
        and tag[1] == STEVE_PUBKEY
        for tag in event.get("tags", [])
    )
    for rule in rules:
        if "*" not in rule.channels and channel_id not in rule.channels:
            continue
        if kind not in rule.kinds:
            continue
        if rule.require_mention and not mentioned:
            continue
        return True
    return False


def subscription_kinds(
    channel_id: str,
    rules: tuple[SubscriptionRule, ...],
) -> list[int]:
    return sorted(
        {
            kind
            for rule in rules
            if "*" in rule.channels or channel_id in rule.channels
            for kind in rule.kinds
        }
    )


def thread_references(event: dict[str, Any]) -> dict[str, str | None]:
    root = None
    for tag in event.get("tags", []):
        if (
            isinstance(tag, list)
            and len(tag) >= 4
            and tag[0] == "e"
            and re.fullmatch(r"[0-9a-f]{64}", str(tag[1]))
            and tag[3] == "root"
        ):
            root = str(tag[1])
            break
    parent = str(event.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", parent):
        parent = None
    return {"root_event_id": root, "parent_event_id": parent}


def owner_control(event: dict[str, Any], policy: Policy | None = None) -> str | None:
    policy = policy or default_policy()
    if str(event.get("pubkey") or "") != policy.owner_pubkey:
        return None
    if int(event.get("kind") or 0) != 9:
        return None
    if str(event.get("channel_id") or "") != policy.joe_dm_channel and not any(
        isinstance(tag, list)
        and len(tag) >= 2
        and tag[0] == "p"
        and tag[1] == STEVE_PUBKEY
        for tag in event.get("tags", [])
    ):
        return None
    command = str(event.get("content") or "").strip().lower()
    if command.startswith("!replace "):
        return "replace"
    return command[1:] if command in {"!cancel", "!rotate"} else None


def batch_events_by_channel(
    events: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    channels: dict[str, list[dict[str, Any]]] = {}
    for event in sorted(events, key=lambda item: (item["created_at"], item["id"])):
        channels.setdefault(event["channel_id"], []).append(event)
    return sorted(
        channels.values(),
        key=lambda batch: (batch[0]["created_at"], batch[0]["id"]),
    )


def read_command(
    action: str,
    *,
    query: str = "",
    channel: str = "",
    event: str = "",
    limit: int = 20,
) -> tuple[str, ...]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if action == "search":
        if not query.strip():
            raise ValueError("search query cannot be empty")
        return ("messages", "search", "--query", query.strip(), "--limit", str(limit))
    if action == "feed":
        return ("feed", "get", "--limit", str(limit))
    if action == "channels":
        return ("channels", "list", "--member")
    if not re.fullmatch(r"[0-9a-f-]{36}", channel):
        raise ValueError("channel must be a UUID")
    if action == "members":
        return ("channels", "members", "--channel", channel)
    if action == "thread":
        if not re.fullmatch(r"[0-9a-f]{64}", event):
            raise ValueError("event must be 64 lowercase hex characters")
        return (
            "messages",
            "thread",
            "--channel",
            channel,
            "--event",
            event,
            "--limit",
            str(limit),
        )
    raise ValueError("unsupported read action")


def build_reply_arguments(channel_id: str, event_id: str) -> tuple[str, ...]:
    arguments = ["messages", "send", "--channel", channel_id]
    if channel_id != JOE_DM_CHANNEL:
        arguments.extend(["--reply-to", event_id])
    arguments.extend(["--content", "-"])
    return tuple(arguments)


def reply_mention_pubkeys(event: dict[str, Any], channel_id: str) -> list[str]:
    if channel_id == JOE_DM_CHANNEL:
        return []
    pubkeys = [str(event.get("pubkey") or "")]
    pubkeys.extend(
        str(tag[1])
        for tag in event.get("tags", [])
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "p"
    )
    return list(
        dict.fromkeys(
            pubkey
            for pubkey in pubkeys
            if pubkey and pubkey != STEVE_PUBKEY
        )
    )


def render_context(
    messages: list[dict[str, Any]],
    current_event_id: str,
    names: dict[str, str],
    limit: int = 8,
) -> str:
    lines = []
    for item in messages:
        if str(item.get("id")) == current_event_id:
            continue
        content = " ".join(str(item.get("content") or "").split())
        if not content:
            continue
        author = str(item.get("pubkey") or "")
        lines.append(f"{names.get(author, author[:12])}: {content[:500]}")
    return "\n".join(lines[-limit:])


def attachment_specs(event: dict[str, Any]) -> list[dict[str, Any]]:
    specs = []
    for tag in event.get("tags", []):
        if not isinstance(tag, list) or not tag or tag[0] != "imeta":
            continue
        fields: dict[str, str] = {}
        for value in tag[1:]:
            key, _, data = str(value).partition(" ")
            if key and data:
                fields[key] = data
        if "url" in fields:
            specs.append(
                {
                    "url": fields["url"],
                    "mime": fields.get("m", ""),
                    "name": fields.get("name", Path(fields["url"]).name),
                    "size": int(fields.get("size", "0") or 0),
                }
            )
    return specs


def safe_attachment_path(directory: Path, supplied_name: str) -> Path:
    name = Path(supplied_name).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name or name in {".", ".."}:
        name = "attachment"
    return directory / name


def validate_attachment_spec(spec: dict[str, Any], policy: Policy) -> None:
    extension = Path(str(spec["name"])).suffix.lower()
    if spec.get("mime") not in ALLOWED_ATTACHMENT_MIMES:
        raise ValueError("attachment MIME type is not allowed")
    if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise ValueError("attachment extension is not allowed")
    size = int(spec.get("size") or 0)
    if size < 0 or size > policy.max_attachment_bytes:
        raise ValueError("attachment exceeds size limit")
    if not str(spec["url"]).startswith("https://buzz.neustac.com/"):
        raise ValueError("attachment URL is outside the Buzz relay")


def validate_outbound_file(path: Path, policy: Policy) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not any(
        resolved.is_relative_to(root.expanduser().resolve())
        for root in policy.allowed_upload_roots
    ):
        raise PermissionError("file is outside approved upload roots")
    if resolved.stat().st_size > policy.max_attachment_bytes:
        raise ValueError("file exceeds size limit")
    mime, _ = mimetypes.guess_type(resolved.name)
    if resolved.suffix.lower() not in ALLOWED_OUTBOUND_EXTENSIONS:
        raise ValueError("file extension is not allowed")
    if not mime or mime not in ALLOWED_OUTBOUND_MIMES:
        raise ValueError("file MIME type is not allowed")
    return resolved


def health_snapshot(
    *,
    relay_ok: bool,
    tmux_ok: bool,
    claude_ok: bool,
    inbound_queue: int,
    outbound_queue: int,
    dead_letters: int,
    inbound_dead_letters: int = 0,
    oldest_inbound_age: int = 0,
    bridge_pending: int = 0,
    oldest_pending_age: int = 0,
    active_channel: str = "",
    active_turn_age: int = 0,
    typing_ok: bool = True,
    last_typing_error: str = "",
    last_delivery_at: int = 0,
    last_error: str = "",
    now: int | None = None,
) -> dict[str, Any]:
    healthy = (
        relay_ok
        and tmux_ok
        and claude_ok
        and dead_letters == 0
        and inbound_dead_letters == 0
    )
    return {
        "checked_at": now or int(time.time()),
        "status": "healthy" if healthy else "degraded",
        "relay_ok": relay_ok,
        "tmux_ok": tmux_ok,
        "claude_ok": claude_ok,
        "inbound_queue": inbound_queue,
        "outbound_queue": outbound_queue,
        "dead_letters": dead_letters,
        "inbound_dead_letters": inbound_dead_letters,
        "oldest_inbound_age": oldest_inbound_age,
        "bridge_pending": bridge_pending,
        "oldest_pending_age": oldest_pending_age,
        "active_channel": active_channel,
        "active_turn_age": active_turn_age,
        "typing_ok": typing_ok,
        "last_typing_error": last_typing_error,
        "last_delivery_at": last_delivery_at,
        "last_error": last_error,
    }


def presence_for_health(health: dict[str, Any]) -> str:
    if not health["relay_ok"]:
        return "offline"
    if not health["tmux_ok"] or not health["claude_ok"]:
        return "away"
    return "online"


def stable_pane_digest(pane: str) -> str:
    stable_lines = []
    for line in pane.splitlines():
        if re.search(
            r"esc to interrupt| tokens\)|\([0-9]+[ms]|bypass permissions on",
            line,
        ):
            continue
        if line.strip() in {"", "❯"}:
            continue
        stable_lines.append(line)
    return hashlib.sha256("\n".join(stable_lines).encode()).hexdigest()


def parse_buzz_messages(raw: str, self_pubkey: str, since: int) -> list[dict[str, Any]]:
    payload = json.loads(raw or "[]")
    if isinstance(payload, dict):
        payload = payload.get("messages", payload.get("items", []))
    messages = []
    for item in payload:
        event_id = str(item.get("id") or item.get("event_id") or "")
        author = str(item.get("pubkey") or item.get("author_pubkey") or item.get("author") or "")
        created_at = int(item.get("created_at") or item.get("timestamp") or 0)
        if not event_id or author == self_pubkey or created_at <= since:
            continue
        messages.append(
            {
                **item,
                "id": event_id,
                "pubkey": author,
                "created_at": created_at,
                "channel_id": str(item.get("channel_id") or item.get("channel") or ""),
                "content": str(item.get("content") or item.get("text") or ""),
                "kind": int(item.get("kind") or 0),
            }
        )
    return sorted(messages, key=lambda item: (item["created_at"], item["id"]))


def crm_message(
    event: dict[str, Any],
    crm_id: str,
    display_name: str | None = None,
    context: str = "",
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    channel_id = event["channel_id"]
    event_id = event["id"]
    author = display_name or event["pubkey"]
    context_block = f"\n\nRecent context:\n{context}" if context else ""
    attachment_block = ""
    if attachments:
        attachment_block = "\n\nValidated attachments:\n" + "\n".join(
            f"- {path}" for path in attachments
        )
    text = (
        f"=== BUZZ MESSAGE from {author} "
        f"[channel:{channel_id}] [event:{event_id}] ===\n"
        f"{event['content']}{context_block}{attachment_block}\n"
        "Reply through the normal agent-message reply command; the Buzz bridge "
        "will route it back to this thread."
    )
    return {
        "id": crm_id,
        "from": "buzz",
        "to": "steve-kingsley",
        "priority": "urgent" if event["pubkey"] == OWNER_PUBKEY else "normal",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "text": text,
        "reply_to": None,
    }


def crm_message_batch(
    events: list[dict[str, Any]],
    crm_id: str,
    display_name: str,
    context: str = "",
    attachments: list[str] | None = None,
    steering: bool = False,
    supersede: bool = False,
) -> dict[str, Any]:
    if not events:
        raise ValueError("event batch cannot be empty")
    latest = events[-1]
    if len(events) == 1:
        event = latest
        if supersede:
            event = {
                **latest,
                "content": (
                    "[Replacement request from Joe. Stop the prior task and use "
                    "this as the active request.]\n\n"
                    + latest["content"]
                ),
            }
        elif steering:
            event = {
                **latest,
                "content": (
                    "[Steering message received while you were working. "
                    "Incorporate it into the current task unless Joe explicitly "
                    "replaces the task.]\n\n"
                    + latest["content"]
                ),
            }
        return crm_message(event, crm_id, display_name, context, attachments)
    content = "\n\n".join(
        f"[{index + 1}/{len(events)}] {event['content']}"
        for index, event in enumerate(events)
    )
    if supersede:
        content = (
            "[Replacement request from Joe. Stop the prior task and use this as "
            "the active request.]\n\n" + content
        )
    elif steering:
        content = (
            "[Steering message received while you were working. Incorporate it "
            "into the current task unless Joe explicitly replaces the task.]\n\n"
            + content
        )
    return crm_message(
        {**latest, "content": content},
        crm_id,
        display_name,
        context,
        attachments,
    )


class BridgeState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "since": int(time.time()) - 5,
            "channel_cursors": {},
            "events": {},
            "pending": {},
            "inbound_failures": {},
            "inbound_dead_letters": [],
            "active_turn": None,
            "outbound_failures": {},
            "profiles": {},
            "reply_routes": {},
            "last_delivery_at": 0,
            "last_error": "",
        }
        if path.exists():
            self.data.update(json.loads(path.read_text()))
        self.data.setdefault("startup_cursor", int(self.data["since"]))
        self.data.setdefault("channel_cursors", {})
        self.data.setdefault("pending", {})
        self.data.setdefault("inbound_failures", {})
        self.data.setdefault("inbound_dead_letters", [])
        self.data.setdefault("active_turn", None)

    @property
    def since(self) -> int:
        return int(self.data["since"])

    def seen_event(self, event_id: str) -> bool:
        if event_id in self.data["events"]:
            return True
        if any(
            event.get("id") == event_id
            for queue in self.data["pending"].values()
            for event in queue
        ):
            return True
        active = self.data.get("active_turn") or {}
        return any(event.get("id") == event_id for event in active.get("events", []))

    def mark_event(self, event_id: str, created_at: int) -> None:
        self.data["events"][event_id] = created_at
        self.data["since"] = max(self.since, created_at - 1)
        if len(self.data["events"]) > 5000:
            oldest = sorted(
                self.data["events"],
                key=lambda key: (self.data["events"][key], key),
            )[:-5000]
            for key in oldest:
                self.data["events"].pop(key, None)

    def cursor_for(self, channel_id: str) -> int:
        return int(
            self.data["channel_cursors"].get(
                channel_id,
                min(int(self.data.get("startup_cursor", self.since)), self.since),
            )
        )

    def mark_observed(self, channel_id: str, event_id: str, created_at: int) -> None:
        self.mark_event(event_id, created_at)
        self.data["channel_cursors"][channel_id] = max(
            self.cursor_for(channel_id),
            created_at - 1,
        )
        self.save()

    def queue_event(
        self,
        event: dict[str, Any],
        max_pending: int,
    ) -> dict[str, Any] | None:
        if self.seen_event(str(event["id"])):
            return None
        channel_id = str(event["channel_id"])
        queue = self.data["pending"].setdefault(channel_id, [])
        queue.append(event)
        queue.sort(key=lambda item: (int(item["created_at"]), str(item["id"])))
        dropped = queue.pop(0) if len(queue) > max_pending else None
        self.data["channel_cursors"][channel_id] = max(
            self.cursor_for(channel_id),
            int(event["created_at"]) - 1,
        )
        if dropped:
            self.mark_event(str(dropped["id"]), int(dropped["created_at"]))
            self.data["inbound_dead_letters"].append(
                {"event": dropped, "error": "queue overflow", "failed_at": int(time.time())}
            )
            self.data["inbound_dead_letters"] = self.data["inbound_dead_letters"][-1000:]
        self.save()
        return dropped

    def pending_count(self) -> int:
        return sum(len(queue) for queue in self.data["pending"].values())

    def oldest_pending_age(self, now: int | None = None) -> int:
        timestamps = [
            int(event["created_at"])
            for queue in self.data["pending"].values()
            for event in queue
        ]
        return max(0, (now or int(time.time())) - min(timestamps)) if timestamps else 0

    def take_next_batch(
        self,
        *,
        max_events: int,
        owner_pubkey: str,
        owner_only: bool = False,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        current = now or int(time.time())
        candidates: list[tuple[int, str, str]] = []
        for channel_id, queue in self.data["pending"].items():
            ready = [
                event
                for event in queue
                if int(event.get("_next_retry_at", 0)) <= current
            ]
            if owner_only:
                ready = [
                    event for event in ready if event.get("pubkey") == owner_pubkey
                ]
            if ready:
                event = min(
                    ready,
                    key=lambda item: (int(item["created_at"]), str(item["id"])),
                )
                candidates.append(
                    (int(event["created_at"]), str(event["id"]), channel_id)
                )
        if not candidates:
            return None
        _, _, channel_id = min(candidates)
        queue = self.data["pending"][channel_id]
        selected = [
            event
            for event in queue
            if int(event.get("_next_retry_at", 0)) <= current
            and (not owner_only or event.get("pubkey") == owner_pubkey)
        ][:max_events]
        selected_ids = {str(event["id"]) for event in selected}
        self.data["pending"][channel_id] = [
            event for event in queue if str(event["id"]) not in selected_ids
        ]
        if not self.data["pending"][channel_id]:
            self.data["pending"].pop(channel_id, None)
        self.save()
        return {"channel_id": channel_id, "events": selected}

    def record_inbound_failure(
        self,
        events: list[dict[str, Any]],
        error: str,
        max_attempts: int,
        *,
        permanent: bool = False,
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        current = now or int(time.time())
        dead: list[dict[str, Any]] = []
        for event in events:
            event_id = str(event["id"])
            for channel_id, queue in list(self.data["pending"].items()):
                self.data["pending"][channel_id] = [
                    queued for queued in queue if str(queued["id"]) != event_id
                ]
                if not self.data["pending"][channel_id]:
                    self.data["pending"].pop(channel_id, None)
            record = self.data["inbound_failures"].setdefault(
                event_id, {"attempts": 0, "last_error": ""}
            )
            record["attempts"] += 1
            record["last_error"] = error[:500]
            if permanent or int(record["attempts"]) >= max_attempts:
                item = {"event": event, "error": error[:500], "failed_at": current}
                dead.append(item)
                self.data["inbound_dead_letters"].append(item)
                self.data["inbound_dead_letters"] = self.data["inbound_dead_letters"][-1000:]
                self.mark_event(event_id, int(event["created_at"]))
                continue
            retry = {
                **event,
                "_attempts": int(record["attempts"]),
                "_next_retry_at": current + min(300, 5 * (2 ** (int(record["attempts"]) - 1))),
            }
            self.data["pending"].setdefault(str(event["channel_id"]), []).append(retry)
        self.save()
        return dead

    def map_crm_reply(
        self,
        crm_id: str,
        channel_id: str,
        event_id: str,
        mention_pubkeys: list[str] | None = None,
    ) -> None:
        self.data["reply_routes"][crm_id] = {
            "channel_id": channel_id,
            "event_id": event_id,
            "mention_pubkeys": mention_pubkeys or [],
        }

    def reply_route(self, crm_id: str) -> dict[str, Any] | None:
        return self.data["reply_routes"].get(crm_id)

    def record_outbound_failure(
        self, filename: str, error: str, max_attempts: int
    ) -> bool:
        record = self.data["outbound_failures"].setdefault(
            filename, {"attempts": 0, "last_error": ""}
        )
        record["attempts"] += 1
        record["last_error"] = error[:500]
        self.save()
        return int(record["attempts"]) >= max_attempts

    def outbound_attempts(self, filename: str) -> int:
        return int(self.data["outbound_failures"].get(filename, {}).get("attempts", 0))

    def clear_outbound_failure(self, filename: str) -> None:
        self.data["outbound_failures"].pop(filename, None)
        self.save()

    def record_delivery(self) -> None:
        self.data["last_delivery_at"] = int(time.time())
        self.data["last_error"] = ""
        self.save()

    def record_error(self, error: str) -> None:
        self.data["last_error"] = error[:500]
        self.save()

    def clear_error(self) -> None:
        if self.data.get("last_error"):
            self.data["last_error"] = ""
            self.save()

    def profile_name(self, pubkey: str, lookup: Any) -> str:
        cached = self.data["profiles"].get(pubkey)
        if cached:
            return str(cached)
        name = str(lookup(pubkey))
        self.data["profiles"][pubkey] = name
        self.save()
        return name

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)


class BuzzBridge:
    def __init__(
        self,
        identity_path: Path,
        crm_root: Path,
        state_path: Path,
        policy_path: Path | None = None,
    ):
        identity = json.loads(identity_path.read_text())
        self.private_key = identity["private_key"]
        self.public_key = identity["public_key"]
        self.crm_root = crm_root
        self.template_root = Path(__file__).resolve().parent.parent
        self.state = BridgeState(state_path)
        self.config_dir = state_path.parent
        self.policy = load_policy(policy_path) if policy_path else default_policy()
        self.last_presence = ""
        self.last_presence_at = 0
        self.typing_publisher = EphemeralPublisher(
            "https://buzz.neustac.com",
            self.private_key,
        )
        self.last_typing_at = 0
        self.last_typing_error = ""

    def buzz(self, *arguments: str, stdin: str | None = None) -> str:
        environment = {
            **os.environ,
            "BUZZ_RELAY_URL": "https://buzz.neustac.com",
            "BUZZ_PRIVATE_KEY": self.private_key,
        }
        completed = subprocess.run(
            ["buzz", *arguments],
            input=stdin,
            text=True,
            capture_output=True,
            check=True,
            env=environment,
        )
        return completed.stdout

    def member_channels(self) -> list[str]:
        payload = json.loads(self.buzz("channels", "list", "--member"))
        return [
            str(item.get("channel_id") or item.get("id"))
            for item in payload
            if item.get("channel_id") or item.get("id")
        ]

    def lookup_profile(self, pubkey: str) -> str:
        payload = json.loads(self.buzz("users", "get", "--pubkey", pubkey))
        if not payload:
            return pubkey[:12]
        return str(payload[0].get("display_name") or pubkey[:12])

    def recent_context(self, channel_id: str, event_id: str) -> str:
        payload = json.loads(
            self.buzz(
                "messages",
                "get",
                "--channel",
                channel_id,
                "--limit",
                "12",
            )
        )
        names = {}
        for item in payload:
            pubkey = str(item.get("pubkey") or "")
            if pubkey:
                names[pubkey] = self.state.profile_name(pubkey, self.lookup_profile)
        return render_context(payload, event_id, names)

    def conversation_context(self, event: dict[str, Any]) -> str:
        channel_id = str(event["channel_id"])
        event_id = str(event["id"])
        if channel_id == self.policy.joe_dm_channel:
            return self.recent_context(channel_id, event_id)
        references = thread_references(event)
        root = references["root_event_id"] or event_id
        payload = json.loads(
            self.buzz(
                "messages",
                "thread",
                "--channel",
                channel_id,
                "--event",
                root,
                "--limit",
                "20",
                "--depth-limit",
                "20",
            )
        )
        names = {}
        for item in payload:
            pubkey = str(item.get("pubkey") or "")
            if pubkey:
                names[pubkey] = self.state.profile_name(pubkey, self.lookup_profile)
        return render_context(payload, event_id, names, limit=20)

    def download_attachments(self, event: dict[str, Any]) -> list[str]:
        downloaded = []
        event_dir = self.config_dir / "attachments" / event["id"]
        for spec in attachment_specs(event):
            validate_attachment_spec(spec, self.policy)
            event_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = safe_attachment_path(event_dir, spec["name"])
            self.buzz("media", "get", spec["url"], "--output", str(target))
            os.chmod(target, 0o600)
            if target.stat().st_size > self.policy.max_attachment_bytes:
                target.unlink()
                raise ValueError("downloaded attachment exceeds size limit")
            downloaded.append(str(target))
        return downloaded

    def inject_event(self, event: dict[str, Any], *, steering: bool = False) -> None:
        self.inject_events([event], steering=steering)

    def inject_events(
        self,
        events: list[dict[str, Any]],
        *,
        steering: bool = False,
        supersede: bool = False,
    ) -> None:
        events = [event for event in events if not self.state.seen_event(event["id"])]
        if not events:
            return
        event = events[-1]
        if self.state.seen_event(event["id"]):
            return
        crm_id = (
            f"buzz-{event['id']}"
            if len(events) == 1
            else f"buzz-batch-{event['id']}"
        )
        display_name = self.state.profile_name(event["pubkey"], self.lookup_profile)
        context = self.conversation_context(event)
        attachments = [
            path
            for item in events
            for path in self.download_attachments(item)
        ]
        message = crm_message_batch(
            events,
            crm_id,
            display_name,
            context=context,
            attachments=attachments,
            steering=steering,
            supersede=supersede,
        )
        inbox = self.crm_root / "inbox" / "steve-kingsley"
        inbox.mkdir(parents=True, exist_ok=True)
        final = inbox / f"2-{event['created_at']}-from-buzz-{event['id'][:12]}.json"
        temporary = final.with_suffix(".tmp")
        temporary.write_text(json.dumps(message) + "\n")
        temporary.replace(final)
        self.state.map_crm_reply(
            crm_id,
            event["channel_id"],
            event["id"],
            reply_mention_pubkeys(event, event["channel_id"]),
        )
        for item in events:
            self.state.mark_event(item["id"], item["created_at"])
            self.state.data["inbound_failures"].pop(str(item["id"]), None)
        now = int(time.time())
        references = thread_references(event)
        active = self.state.data.get("active_turn")
        if steering and active:
            active["crm_ids"] = [*active.get("crm_ids", []), crm_id]
            active["events"] = [*active.get("events", []), *events]
            active["steering"] = True
            active["channel_id"] = event["channel_id"]
            active.update(references)
        else:
            self.state.data["active_turn"] = {
                "channel_id": event["channel_id"],
                "crm_ids": [crm_id],
                "events": events,
                "started_at": now,
                "last_activity_at": now,
                "pane_hash": "",
                "stall_alerted": False,
                "steering": steering,
                "supersede": supersede,
                **references,
            }
        self.state.record_delivery()
        self.state.save()

    def agent_busy(self) -> bool:
        pane = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-t",
                "crm-default-steve-kingsley:0.0",
                "-p",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        tail = "\n".join(line for line in pane.splitlines() if line.strip())[-2000:]
        return bool(re.search(r"esc to interrupt| tokens\)|\([0-9]+[ms]", tail))

    def pane_digest(self) -> str:
        pane = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-t",
                "crm-default-steve-kingsley:0.0",
                "-p",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return stable_pane_digest(pane)

    def alert_stalled_turn(self, active: dict[str, Any], age: int) -> None:
        subprocess.run(
            [
                "bash",
                str(self.template_root / "core/bus/send-telegram.sh"),
                "1242084718",
                (
                    "Steve Buzz turn stalled: "
                    f"channel={active.get('channel_id')} age={age}s"
                ),
            ],
            check=False,
            capture_output=True,
        )

    def expire_active_turn(self, active: dict[str, Any]) -> None:
        self.cancel_current_turn()
        events = list(active.get("events", []))
        self.state.data["active_turn"] = None
        for event in events:
            self.state.data["events"].pop(str(event["id"]), None)
            self.state.queue_event(event, self.policy.max_pending_per_channel)
        self.state.save()

    def monitor_turn(self, now: int | None = None) -> None:
        active = self.state.data.get("active_turn")
        if not active:
            return
        current = now or int(time.time())
        age = current - int(active.get("started_at", current))
        if not self.agent_busy():
            if age >= 15:
                self.state.data["active_turn"] = None
                self.state.save()
            return
        if age >= self.policy.max_turn_seconds:
            self.expire_active_turn(active)
            return
        digest = self.pane_digest()
        if digest != active.get("pane_hash"):
            active["pane_hash"] = digest
            active["last_activity_at"] = current
            active["stall_alerted"] = False
            self.state.save()
            return
        stalled_for = current - int(active.get("last_activity_at", current))
        if (
            stalled_for >= self.policy.stall_alert_seconds
            and not active.get("stall_alerted")
        ):
            self.alert_stalled_turn(active, stalled_for)
            active["stall_alerted"] = True
            self.state.save()

    def write_inbound_dead_letter(self, item: dict[str, Any]) -> Path:
        directory = self.crm_root / "dead-letter" / "buzz-inbound"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        event_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(item["event"]["id"]))
        path = directory / f"{event_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(item, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        return path

    def alert_inbound_dead_letter(self, item: dict[str, Any]) -> None:
        event = item["event"]
        subprocess.run(
            [
                "bash",
                str(self.template_root / "core/bus/send-telegram.sh"),
                "1242084718",
                (
                    "Buzz inbound event quarantined: "
                    f"channel={event.get('channel_id')} event={event.get('id')} "
                    f"error={item.get('error', '')[:300]}"
                ),
            ],
            check=False,
            capture_output=True,
        )

    def quarantine_inbound(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            self.write_inbound_dead_letter(item)
            self.alert_inbound_dead_letter(item)

    def dispatch_pending(self) -> None:
        busy = self.agent_busy()
        batch = self.state.take_next_batch(
            max_events=self.policy.max_batch_events,
            owner_pubkey=self.policy.owner_pubkey,
            owner_only=busy,
        )
        if not batch:
            return
        events = batch["events"]
        supersede = any(bool(event.get("_supersede")) for event in events)
        try:
            self.inject_events(
                events,
                steering=busy and not supersede,
                supersede=supersede,
            )
        except (ValueError, PermissionError) as error:
            dead = self.state.record_inbound_failure(
                events,
                str(error),
                self.policy.max_inbound_attempts,
                permanent=True,
            )
            self.quarantine_inbound(dead)
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as error:
            detail = (
                error.stderr.strip()
                if isinstance(error, subprocess.CalledProcessError) and error.stderr
                else str(error)
            )
            permanent = isinstance(error, subprocess.CalledProcessError) and error.returncode == 3
            dead = self.state.record_inbound_failure(
                events,
                detail,
                self.policy.max_inbound_attempts,
                permanent=permanent,
            )
            self.quarantine_inbound(dead)

    def cancel_current_turn(self) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-t", "crm-default-steve-kingsley:0", "C-c"],
            check=True,
            capture_output=True,
        )

    def rotate_session(self) -> None:
        subprocess.run(
            [
                "bash",
                str(self.template_root / "core/bus/hard-restart.sh"),
                "--reason",
                "Buzz owner requested context rotation",
            ],
            check=True,
            capture_output=True,
        )

    def send_control_confirmation(self, event: dict[str, Any], content: str) -> None:
        self.buzz(
            *build_reply_arguments(event["channel_id"], event["id"]),
            stdin=content,
        )

    def handle_control(self, event: dict[str, Any]) -> bool:
        command = owner_control(event, self.policy)
        if command is None:
            return False
        if command == "cancel":
            self.cancel_current_turn()
            confirmation = "Cancelled Steve's current turn."
        elif command == "rotate":
            self.send_control_confirmation(event, "Rotating Steve's session context.")
            self.rotate_session()
            confirmation = ""
        else:
            self.cancel_current_turn()
            self.state.data["active_turn"] = None
            replacement = str(event["content"]).strip()[len("!replace ") :].strip()
            self.state.queue_event(
                {**event, "content": replacement, "_supersede": True},
                self.policy.max_pending_per_channel,
            )
            confirmation = ""
        if confirmation:
            self.send_control_confirmation(event, confirmation)
        if command != "replace":
            self.state.mark_observed(
                event["channel_id"],
                event["id"],
                event["created_at"],
            )
        return True

    def alert_dead_letter(self, filename: str, detail: str) -> None:
        subprocess.run(
            [
                "bash",
                str(self.template_root / "core/bus/send-telegram.sh"),
                "1242084718",
                f"Buzz delivery failed permanently: {filename}: {detail[:300]}",
            ],
            check=False,
            capture_output=True,
        )

    def forward_replies(self) -> None:
        inbox = self.crm_root / "inbox" / "buzz"
        processed = self.crm_root / "processed" / "buzz"
        dead_letters = self.crm_root / "dead-letter" / "buzz"
        inbox.mkdir(parents=True, exist_ok=True)
        processed.mkdir(parents=True, exist_ok=True)
        dead_letters.mkdir(parents=True, exist_ok=True)
        for path in sorted(inbox.glob("*.json")):
            message = json.loads(path.read_text())
            route = self.state.reply_route(str(message.get("reply_to") or ""))
            if route is None:
                continue
            arguments = build_reply_arguments(route["channel_id"], route["event_id"])
            content = str(message.get("text") or "")
            mentions = [
                f"@{self.state.profile_name(pubkey, self.lookup_profile)}"
                for pubkey in route.get("mention_pubkeys", [])
            ]
            if mentions:
                content = f"{' '.join(mentions)} {content}"
            try:
                self.buzz(*arguments, stdin=content)
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or "Buzz send failed").strip()
                if self.state.record_outbound_failure(
                    path.name, detail, self.policy.max_outbound_attempts
                ):
                    path.replace(dead_letters / path.name)
                    self.alert_dead_letter(path.name, detail)
                continue
            self.state.clear_outbound_failure(path.name)
            self.state.record_delivery()
            active = self.state.data.get("active_turn") or {}
            if str(message.get("reply_to") or "") in active.get("crm_ids", []):
                self.state.data["active_turn"] = None
                self.state.save()
            path.replace(processed / path.name)

    def tmux_health(self) -> tuple[bool, bool]:
        target = "crm-default-steve-kingsley"
        exists = subprocess.run(
            ["tmux", "has-session", "-t", target],
            capture_output=True,
            check=False,
        ).returncode == 0
        if not exists:
            return False, False
        command = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{target}:0.0", "#{pane_current_command}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        return True, command not in {"", "bash", "zsh"}

    def write_health(self, relay_ok: bool) -> dict[str, Any]:
        tmux_ok, claude_ok = self.tmux_health()
        inbound_files = list((self.crm_root / "inbox/steve-kingsley").glob("*.json"))
        now = int(time.time())
        oldest_inbound_age = (
            max(0, now - int(min(path.stat().st_mtime for path in inbound_files)))
            if inbound_files
            else 0
        )
        active = self.state.data.get("active_turn") or {}
        health = health_snapshot(
            relay_ok=relay_ok,
            tmux_ok=tmux_ok,
            claude_ok=claude_ok,
            inbound_queue=len(inbound_files),
            outbound_queue=len(list((self.crm_root / "inbox/buzz").glob("*.json"))),
            dead_letters=len(list((self.crm_root / "dead-letter/buzz").glob("*.json"))),
            inbound_dead_letters=len(
                list((self.crm_root / "dead-letter/buzz-inbound").glob("*.json"))
            ),
            oldest_inbound_age=oldest_inbound_age,
            bridge_pending=self.state.pending_count(),
            oldest_pending_age=self.state.oldest_pending_age(now),
            active_channel=str(active.get("channel_id", "")),
            active_turn_age=(
                max(0, now - int(active.get("started_at", now))) if active else 0
            ),
            typing_ok=not bool(self.last_typing_error),
            last_typing_error=self.last_typing_error,
            last_delivery_at=int(self.state.data.get("last_delivery_at", 0)),
            last_error=str(self.state.data.get("last_error", "")),
            now=now,
        )
        path = self.config_dir / "health.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(health, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        return health

    def refresh_presence(self, health: dict[str, Any], force: bool = False) -> None:
        desired = presence_for_health(health)
        now = int(time.time())
        if force or desired != self.last_presence or now - self.last_presence_at >= 60:
            self.buzz("users", "set-presence", "--status", desired)
            self.last_presence = desired
            self.last_presence_at = now

    def refresh_typing(self, now: int | None = None) -> None:
        active = self.state.data.get("active_turn") or {}
        if not active or not self.agent_busy():
            return
        current = now or int(time.time())
        if current - self.last_typing_at < 3:
            return
        try:
            self.typing_publisher.publish_typing(
                str(active["channel_id"]),
                active.get("root_event_id"),
                active.get("parent_event_id"),
            )
            self.last_typing_at = current
            self.last_typing_error = ""
        except Exception as error:
            self.last_typing_error = str(error)[:500]

    def poll(self) -> None:
        self.forward_replies()
        channels = self.member_channels()
        for channel_id in channels:
            cursor = self.state.cursor_for(channel_id)
            kinds = subscription_kinds(
                channel_id,
                self.policy.subscription_rules,
            )
            if not kinds:
                continue
            raw = self.buzz(
                "messages",
                "get",
                "--channel",
                channel_id,
                "--since",
                str(cursor),
                "--limit",
                "100",
                "--kinds",
                ",".join(str(kind) for kind in kinds),
            )
            for event in parse_buzz_messages(raw, self.public_key, cursor):
                if not event["channel_id"]:
                    event["channel_id"] = channel_id
                if self.handle_control(event):
                    continue
                if not accepts_event(event, channel_id, self.policy):
                    self.state.mark_observed(
                        channel_id,
                        event["id"],
                        event["created_at"],
                    )
                    continue
                dropped = self.state.queue_event(
                    event,
                    self.policy.max_pending_per_channel,
                )
                if dropped:
                    item = {
                        "event": dropped,
                        "error": "queue overflow",
                        "failed_at": int(time.time()),
                    }
                    self.quarantine_inbound([item])
        self.dispatch_pending()
        self.monitor_turn()
        self.refresh_typing()
        self.state.clear_error()
        self.refresh_presence(self.write_health(relay_ok=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument(
        "--identity",
        type=Path,
        default=Path.home() / ".config/buzz/steve-kingsley/identity.json",
    )
    parser.add_argument(
        "--crm-root",
        type=Path,
        default=Path.home() / ".claude-remote/default",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path.home() / ".config/buzz/steve-kingsley/bridge-state.json",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path.home() / ".config/buzz/steve-kingsley/policy.json",
    )
    arguments = parser.parse_args()
    bridge = BuzzBridge(
        arguments.identity,
        arguments.crm_root,
        arguments.state,
        policy_path=arguments.policy,
    )
    bridge.state.save()
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        bridge.buzz("users", "set-presence", "--status", "online")
        while not stopping:
            try:
                bridge.poll()
            except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as error:
                detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
                bridge.state.record_error(detail)
                bridge.write_health(relay_ok=False)
                print(f"buzz bridge poll failed: {detail[:500]}", flush=True)
            if arguments.once:
                return
            time.sleep(arguments.interval)
    finally:
        bridge.typing_publisher.close()
        try:
            bridge.buzz("users", "set-presence", "--status", "offline")
        except subprocess.CalledProcessError:
            pass


if __name__ == "__main__":
    main()
