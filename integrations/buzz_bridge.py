#!/usr/bin/env python3
"""Bridge Buzz channel messages into the existing CRM agent bus."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
class Policy:
    owner_pubkey: str
    allowed_authors: set[str]
    joe_dm_channel: str
    allowed_upload_roots: tuple[Path, ...]
    max_attachment_bytes: int = 10 * 1024 * 1024
    max_outbound_attempts: int = 5


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
    if channel_id == policy.joe_dm_channel:
        return author == policy.owner_pubkey
    return any(
        isinstance(tag, list)
        and len(tag) >= 2
        and tag[0] == "p"
        and tag[1] == STEVE_PUBKEY
        for tag in event.get("tags", [])
    )


def owner_control(event: dict[str, Any], policy: Policy | None = None) -> str | None:
    policy = policy or default_policy()
    if str(event.get("pubkey") or "") != policy.owner_pubkey:
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


def build_reply_arguments(channel_id: str, event_id: str) -> tuple[str, ...]:
    arguments = ["messages", "send", "--channel", channel_id]
    if channel_id != JOE_DM_CHANNEL:
        arguments.extend(["--reply-to", event_id])
    arguments.extend(["--content", "-"])
    return tuple(arguments)


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
    oldest_inbound_age: int = 0,
    last_delivery_at: int = 0,
    last_error: str = "",
    now: int | None = None,
) -> dict[str, Any]:
    healthy = relay_ok and tmux_ok and claude_ok and dead_letters == 0
    return {
        "checked_at": now or int(time.time()),
        "status": "healthy" if healthy else "degraded",
        "relay_ok": relay_ok,
        "tmux_ok": tmux_ok,
        "claude_ok": claude_ok,
        "inbound_queue": inbound_queue,
        "outbound_queue": outbound_queue,
        "dead_letters": dead_letters,
        "oldest_inbound_age": oldest_inbound_age,
        "last_delivery_at": last_delivery_at,
        "last_error": last_error,
    }


def presence_for_health(health: dict[str, Any]) -> str:
    if not health["relay_ok"]:
        return "offline"
    if not health["tmux_ok"] or not health["claude_ok"]:
        return "away"
    return "online"


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
) -> dict[str, Any]:
    if not events:
        raise ValueError("event batch cannot be empty")
    latest = events[-1]
    if len(events) == 1:
        return crm_message(latest, crm_id, display_name, context, attachments)
    content = "\n\n".join(
        f"[{index + 1}/{len(events)}] {event['content']}"
        for index, event in enumerate(events)
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
            "events": {},
            "outbound_failures": {},
            "profiles": {},
            "reply_routes": {},
            "last_delivery_at": 0,
            "last_error": "",
        }
        if path.exists():
            self.data.update(json.loads(path.read_text()))

    @property
    def since(self) -> int:
        return int(self.data["since"])

    def seen_event(self, event_id: str) -> bool:
        return event_id in self.data["events"]

    def mark_event(self, event_id: str, created_at: int) -> None:
        self.data["events"][event_id] = created_at
        self.data["since"] = max(self.since, created_at - 1)

    def map_crm_reply(self, crm_id: str, channel_id: str, event_id: str) -> None:
        self.data["reply_routes"][crm_id] = {
            "channel_id": channel_id,
            "event_id": event_id,
        }

    def reply_route(self, crm_id: str) -> dict[str, str] | None:
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

    def inject_event(self, event: dict[str, Any]) -> None:
        self.inject_events([event])

    def inject_events(self, events: list[dict[str, Any]]) -> None:
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
        context = self.recent_context(event["channel_id"], event["id"])
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
        )
        inbox = self.crm_root / "inbox" / "steve-kingsley"
        inbox.mkdir(parents=True, exist_ok=True)
        final = inbox / f"2-{event['created_at']}-from-buzz-{event['id'][:12]}.json"
        temporary = final.with_suffix(".tmp")
        temporary.write_text(json.dumps(message) + "\n")
        temporary.replace(final)
        self.state.map_crm_reply(crm_id, event["channel_id"], event["id"])
        for item in events:
            self.state.mark_event(item["id"], item["created_at"])
        self.state.record_delivery()
        self.state.save()

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
        else:
            self.send_control_confirmation(event, "Rotating Steve's session context.")
            self.rotate_session()
            confirmation = ""
        if confirmation:
            self.send_control_confirmation(event, confirmation)
        self.state.mark_event(event["id"], event["created_at"])
        self.state.save()
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
            try:
                self.buzz(*arguments, stdin=str(message.get("text") or ""))
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
        health = health_snapshot(
            relay_ok=relay_ok,
            tmux_ok=tmux_ok,
            claude_ok=claude_ok,
            inbound_queue=len(inbound_files),
            outbound_queue=len(list((self.crm_root / "inbox/buzz").glob("*.json"))),
            dead_letters=len(list((self.crm_root / "dead-letter/buzz").glob("*.json"))),
            oldest_inbound_age=oldest_inbound_age,
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

    def poll(self) -> None:
        self.forward_replies()
        channels = self.member_channels()
        cursor = self.state.since
        accepted: list[dict[str, Any]] = []
        for channel_id in channels:
            raw = self.buzz(
                "messages",
                "get",
                "--channel",
                channel_id,
                "--since",
                str(cursor),
                "--limit",
                "100",
            )
            for event in parse_buzz_messages(raw, self.public_key, self.state.since):
                if not event["channel_id"]:
                    event["channel_id"] = channel_id
                if self.handle_control(event):
                    continue
                if not accepts_event(event, channel_id, self.policy):
                    self.state.mark_event(event["id"], event["created_at"])
                    self.state.save()
                    continue
                accepted.append(event)
        for batch in batch_events_by_channel(accepted):
            if len(batch) == 1:
                self.inject_event(batch[0])
            else:
                self.inject_events(batch)
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
        try:
            bridge.buzz("users", "set-presence", "--status", "offline")
        except subprocess.CalledProcessError:
            pass


if __name__ == "__main__":
    main()
