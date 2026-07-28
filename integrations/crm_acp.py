#!/usr/bin/env python3
"""ACP stdio adapter for isolated Claude Remote Manager agents."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import fcntl
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence


AGENT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ADAPTER_PATTERN = re.compile(r"^buzz-acp(?:-[a-z0-9][a-z0-9-]{0,62})?$")
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
EVENT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WORKFLOW_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
CRON_PATTERN = re.compile(r"^[0-9*/,-]+(?: [0-9*/,-]+){4}$")
INTERVAL_PATTERN = re.compile(r"^([1-9][0-9]*)([smhd])$")
MAX_CORE_BYTES = 16_384
MAX_WORKFLOW_TASK_BYTES = 4_096
SECP256K1_FIELD = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_GENERATOR = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
Notify = Callable[[dict[str, Any]], Awaitable[None]]


class TurnCancelled(Exception):
    """Raised when Buzz cancels the active ACP turn."""


@dataclass(frozen=True)
class AgentConfig:
    agent_name: str
    title: str
    adapter_name: str
    model_id: str
    model_label: str
    tmux_target: str
    prompt_enabled: bool = True

    @classmethod
    def create(
        cls,
        agent_name: str,
        title: str,
        *,
        adapter_name: str | None = None,
        model_id: str | None = None,
        model_label: str | None = None,
        tmux_target: str | None = None,
        instance_id: str | None = None,
        prompt_enabled: bool = True,
    ) -> "AgentConfig":
        if not AGENT_PATTERN.fullmatch(agent_name):
            raise ValueError("invalid CRM agent name")
        clean_title = title.strip()
        if (
            not clean_title
            or len(clean_title) > 80
            or any(ord(character) < 32 for character in clean_title)
        ):
            raise ValueError("invalid CRM agent title")
        resolved_adapter = adapter_name or f"buzz-acp-{agent_name}"
        if not ADAPTER_PATTERN.fullmatch(resolved_adapter):
            raise ValueError("invalid CRM ACP adapter name")
        resolved_model = model_id or f"crm-{agent_name}-current"
        if not re.fullmatch(r"^crm-[a-z0-9-]+-current$", resolved_model):
            raise ValueError("invalid CRM model ID")
        resolved_instance = instance_id or os.environ.get("CRM_INSTANCE_ID", "default")
        if not AGENT_PATTERN.fullmatch(resolved_instance):
            raise ValueError("invalid CRM instance ID")
        resolved_target = tmux_target or f"crm-{resolved_instance}-{agent_name}:0.0"
        if not re.fullmatch(r"^[a-zA-Z0-9_.-]+:[0-9]+\.[0-9]+$", resolved_target):
            raise ValueError("invalid CRM tmux target")
        return cls(
            agent_name=agent_name,
            title=clean_title,
            adapter_name=resolved_adapter,
            model_id=resolved_model,
            model_label=model_label
            or f"Existing {clean_title} CRM session - Claude default",
            tmux_target=resolved_target,
            prompt_enabled=prompt_enabled,
        )

    @classmethod
    def steve_compatibility(cls) -> "AgentConfig":
        return cls.create(
            "steve-kingsley",
            "Steve Kingsley",
            adapter_name="buzz-acp",
            model_id="crm-steve-current",
            model_label="Existing Steve CRM session — Claude default",
        )

    def model_config(self) -> list[dict[str, Any]]:
        return [
            {
                "configId": "model",
                "id": "model",
                "name": "Model",
                "displayName": "Model",
                "description": (
                    f"Model is owned by {self.title}'s existing CRM Claude session."
                ),
                "category": "model",
                "type": "select",
                "currentValue": self.model_id,
                "options": [
                    {
                        "value": self.model_id,
                        "name": self.model_label,
                        "displayName": self.model_label,
                    }
                ],
            }
        ]


@dataclass(frozen=True)
class FactoryIdentity:
    fingerprint: str
    title: str
    proposed_slug: str
    session_id: str

    @classmethod
    def from_environment(cls, environment: dict[str, str]) -> "FactoryIdentity":
        private_key = environment.get("BUZZ_PRIVATE_KEY", "").strip()
        if not (
            re.fullmatch(r"[0-9a-fA-F]{64}", private_key)
            or re.fullmatch(r"nsec1[02-9ac-hj-np-z]{58}", private_key)
        ):
            raise ValueError("invalid Buzz managed identity")
        title = _factory_title(environment.get("BUZZ_ACP_SESSION_TITLE", "CRM Agent"))
        fingerprint = hashlib.sha256(private_key.encode("ascii")).hexdigest()
        readable = _factory_slug(title)
        proposed_slug = f"buzz-{readable}-{fingerprint[:8]}"[:63].rstrip("-")
        session_id = str(
            uuid.uuid5(
                uuid.UUID("57e3011b-94d2-4a76-94ab-1584e1f9a146"),
                fingerprint,
            )
        )
        return cls(fingerprint, title, proposed_slug, session_id)

    def public_record(self) -> dict[str, str]:
        return {
            "fingerprint": self.fingerprint,
            "title": self.title,
            "proposed_slug": self.proposed_slug,
            "session_id": self.session_id,
        }


def _factory_title(value: str) -> str:
    title = value.strip()
    if (
        not title
        or len(title) > 80
        or any(ord(character) < 32 or ord(character) == 127 for character in title)
    ):
        raise ValueError("invalid Buzz agent title")
    return title


def _factory_slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug or "agent")[:48].rstrip("-")


class CrmAcpFactory:
    def __init__(
        self,
        *,
        crm_root: Path,
        template_root: Path,
        home: Path | None = None,
        service_runner: Callable[[str, Path], Any] | None = None,
        instance_id: str = "default",
    ):
        if not AGENT_PATTERN.fullmatch(instance_id):
            raise ValueError("invalid CRM instance ID")
        self.crm_root = crm_root.expanduser().resolve()
        self.template_root = template_root.expanduser().resolve()
        self.home = (home or Path.home()).expanduser().resolve()
        self.instance_id = instance_id
        self.factory_root = self.crm_root / "factory"
        self.service_runner = service_runner or self._start_service

    def resolve(self, environment: dict[str, str]) -> AgentConfig:
        private_key = environment.get("BUZZ_PRIVATE_KEY", "").strip()
        if not private_key:
            if environment.get("BUZZ_MANAGED_AGENT"):
                raise ValueError("Buzz managed identity is unavailable")
            return AgentConfig.create(
                "crm-factory-preview",
                "CRM ACP",
                adapter_name="buzz-acp-factory-preview",
                model_id="crm-claude-current",
                model_label="CRM Claude session - Claude default",
                prompt_enabled=False,
                instance_id=self.instance_id,
            )

        identity = FactoryIdentity.from_environment(environment)
        self._private_directory(self.factory_root)
        lock_path = self.factory_root / "factory.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            with os.fdopen(lock_fd, "r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return self._resolve_locked(identity, environment)
        except Exception:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            raise

    def _resolve_locked(
        self,
        identity: FactoryIdentity,
        environment: dict[str, str],
    ) -> AgentConfig:
        identities_dir = self.factory_root / "identities"
        agents_dir = self.factory_root / "agents"
        workspaces_dir = self.factory_root / "workspaces"
        for directory in (identities_dir, agents_dir, workspaces_dir):
            self._private_directory(directory)

        record_path = identities_dir / f"{identity.fingerprint}.json"
        created = not record_path.exists()
        if created:
            slug = identity.proposed_slug
            workspace = self._workspace(
                environment.get("CRM_WORKSPACE", ""),
                workspaces_dir / slug,
            )
            agent_dir = agents_dir / slug
            record: dict[str, Any] = {
                "schema_version": 1,
                "fingerprint": identity.fingerprint,
                "slug": slug,
                "title": identity.title,
                "session_id": identity.session_id,
                "agent_dir": str(agent_dir),
                "workspace": str(workspace),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "service_registered": False,
            }
        else:
            record = json.loads(record_path.read_text())
            if record.get("fingerprint") != identity.fingerprint:
                raise ValueError("CRM factory identity record mismatch")
            record["title"] = identity.title
            agent_dir = Path(str(record["agent_dir"])).resolve()
            workspace = Path(str(record["workspace"])).resolve()
            agent_config_path = agent_dir / "config.json"
            if agent_config_path.exists():
                agent_config = json.loads(agent_config_path.read_text())
                current_session = str(agent_config.get("claude_session_id") or "")
                if not UUID_PATTERN.fullmatch(current_session):
                    raise ValueError("invalid CRM factory Claude session")
                record["session_id"] = current_session

        slug = str(record["slug"])
        if not AGENT_PATTERN.fullmatch(slug):
            raise ValueError("invalid CRM factory agent record")
        expected_agent_dir = (agents_dir / slug).resolve()
        if agent_dir != expected_agent_dir:
            raise ValueError("invalid CRM factory agent directory")
        if not workspace.is_dir():
            raise ValueError("CRM workspace no longer exists")

        self._write_agent_files(agent_dir, record)
        self._atomic_json(record_path, record)
        config = AgentConfig.create(
            slug,
            identity.title,
            adapter_name=f"buzz-acp-{slug}",
            model_id="crm-claude-current",
            model_label="CRM Claude session - Claude default",
            instance_id=self.instance_id,
        )
        if not record.get("service_registered"):
            self.service_runner(slug, agent_dir)
            record["service_registered"] = True
            self._atomic_json(record_path, record)
        return config

    def _workspace(self, configured: str, default: Path) -> Path:
        if not configured.strip():
            self._private_directory(default)
            return default.resolve()
        workspace = Path(configured).expanduser()
        if not workspace.is_absolute() or not workspace.exists() or not workspace.is_dir():
            raise ValueError("CRM workspace must be an existing absolute directory")
        resolved = workspace.resolve()
        approved_roots = (
            (self.home / "Documents").resolve(),
            (self.home / "repos").resolve(),
            (self.factory_root / "workspaces").resolve(),
        )
        if not any(resolved == root or resolved.is_relative_to(root) for root in approved_roots):
            raise ValueError("CRM workspace is outside approved roots")
        return resolved

    def _write_agent_files(self, agent_dir: Path, record: dict[str, Any]) -> None:
        self._private_directory(agent_dir)
        claude_dir = agent_dir / ".claude"
        self._private_directory(claude_dir)
        adapter = f"buzz-acp-{record['slug']}"
        instructions = (
            f"# {record['title']}\n\n"
            "You are a Buzz-managed CRM agent. Buzz is your homebase. Your identity, "
            "role, voice, and portable core memory arrive in each ACP turn under "
            "`[Buzz ACP managed instructions]`; follow them as your primary persona.\n\n"
            "Use the local workspace for working files and durable operational context. "
            "Never place credentials, private keys, tokens, client secrets, unpublished "
            "writing, or sensitive personal data in Buzz core memory.\n\n"
            "For every Buzz ACP inbox turn, reply only through the correlated envelope "
            "using `core/bus/send-acp-reply.sh`. The turn itself includes the exact "
            f"command and return adapter `{adapter}`. Do not publish the same reply "
            "through Telegram or another channel. Telegram and crons are disabled by "
            "default. Explicit core-memory replacements are allowed without owner "
            "approval when they improve durable identity or working preferences; keep "
            "them concise and exclude secrets.\n\n"
            "For reliable scheduled Buzz work, use a Buzz Workflow operation only "
            "when the owner explicitly asks to create, change, list, pause, resume, "
            "delete, or run a schedule. Write one typed JSON object to "
            f"`$CRM_ROOT/state/{record['slug']}-workflow-op.json` and pass it with "
            "`--workflow` to the correlated reply helper. Never write raw workflow "
            "YAML or request a channel ID/workflow ID directly. Upsert fields are "
            "`action`, slug-like `name`, `task`, and either `interval` (minimum 60s) "
            "or five-field `cron` plus `timezone: UTC`. Buzz calendar cron is UTC. "
            "Tasks may not contain @ mentions. Other actions are `list`, `pause`, "
            "`resume`, `delete`, and `run_now`; mutations use the local schedule name. "
            "The adapter—not this Claude session—uses the managed Buzz identity.\n"
        )
        config = {
            "agent_name": record["slug"],
            "enabled": True,
            "telegram_enabled": False,
            "startup_delay": 0,
            "max_session_seconds": 255600,
            "working_directory": record["workspace"],
            "claude_session_id": record["session_id"],
            "crons": [],
        }
        self._atomic_text(agent_dir / "CLAUDE.md", instructions)
        self._atomic_json(agent_dir / "config.json", config)
        self._atomic_json(claude_dir / "settings.json", {})

    def _start_service(self, slug: str, agent_dir: Path) -> None:
        subprocess.run(
            [
                str(self.template_root / "core/scripts/generate-launchd.sh"),
                slug,
                str(agent_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _private_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)

    @staticmethod
    def _atomic_text(path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(value)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    @classmethod
    def _atomic_json(cls, path: Path, value: dict[str, Any]) -> None:
        cls._atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


@dataclass(frozen=True)
class BuzzDestination:
    channel_id: str
    reply_to: str | None = None

    def __post_init__(self) -> None:
        if not UUID_PATTERN.fullmatch(self.channel_id):
            raise ValueError("invalid Buzz channel UUID")
        if self.reply_to is not None and not EVENT_PATTERN.fullmatch(self.reply_to):
            raise ValueError("invalid Buzz reply event ID")


def _point_add(
    first: tuple[int, int] | None,
    second: tuple[int, int] | None,
) -> tuple[int, int] | None:
    if first is None:
        return second
    if second is None:
        return first
    x1, y1 = first
    x2, y2 = second
    if x1 == x2 and (y1 != y2 or y1 == 0):
        return None
    if first == second:
        slope = (3 * x1 * x1) * pow(2 * y1, SECP256K1_FIELD - 2, SECP256K1_FIELD)
    else:
        slope = (y2 - y1) * pow(x2 - x1, SECP256K1_FIELD - 2, SECP256K1_FIELD)
    slope %= SECP256K1_FIELD
    x3 = (slope * slope - x1 - x2) % SECP256K1_FIELD
    y3 = (slope * (x1 - x3) - y1) % SECP256K1_FIELD
    return x3, y3


def _point_multiply(
    scalar: int,
    point: tuple[int, int],
) -> tuple[int, int] | None:
    result = None
    addend: tuple[int, int] | None = point
    value = scalar % SECP256K1_ORDER
    while value:
        if value & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        value >>= 1
    return result


def _lift_x(value: int) -> tuple[int, int]:
    if value >= SECP256K1_FIELD:
        raise ValueError("invalid Nostr public key")
    square = (pow(value, 3, SECP256K1_FIELD) + 7) % SECP256K1_FIELD
    y = pow(square, (SECP256K1_FIELD + 1) // 4, SECP256K1_FIELD)
    if pow(y, 2, SECP256K1_FIELD) != square:
        raise ValueError("invalid Nostr public key")
    return value, y if y % 2 == 0 else SECP256K1_FIELD - y


def _bech32_polymod(values: Sequence[int]) -> int:
    checksum = 1
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _decode_nsec(value: str) -> str:
    if not value.startswith("nsec1") or value.lower() != value:
        raise ValueError("invalid Buzz managed identity")
    payload = value[5:]
    try:
        data = [BECH32_CHARSET.index(character) for character in payload]
    except ValueError:
        raise ValueError("invalid Buzz managed identity") from None
    expanded = [ord(character) >> 5 for character in "nsec"]
    expanded += [0]
    expanded += [ord(character) & 31 for character in "nsec"]
    if len(data) < 7 or _bech32_polymod(expanded + data) != 1:
        raise ValueError("invalid Buzz managed identity")
    bits = 0
    accumulator = 0
    decoded = bytearray()
    for item in data[:-6]:
        accumulator = (accumulator << 5) | item
        bits += 5
        while bits >= 8:
            bits -= 8
            decoded.append((accumulator >> bits) & 0xFF)
    if bits >= 5 or ((accumulator << (8 - bits)) & 0xFF):
        raise ValueError("invalid Buzz managed identity")
    if len(decoded) != 32:
        raise ValueError("invalid Buzz managed identity")
    return decoded.hex()


def _nostr_public_key(private_key: str) -> str:
    secret = _decode_nsec(private_key) if private_key.startswith("nsec1") else private_key
    if not re.fullmatch(r"[0-9a-fA-F]{64}", secret):
        raise ValueError("invalid Buzz managed identity")
    scalar = int(secret, 16)
    if scalar <= 0 or scalar >= SECP256K1_ORDER:
        raise ValueError("invalid Buzz managed identity")
    point = _point_multiply(scalar, SECP256K1_GENERATOR)
    if point is None:
        raise ValueError("invalid Buzz managed identity")
    return f"{point[0]:064x}"


def _tagged_hash(tag: str, value: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode("ascii")).digest()
    return hashlib.sha256(tag_hash + tag_hash + value).digest()


def _verify_bip340(public_key: str, message: bytes, signature: str) -> bool:
    if (
        not re.fullmatch(r"[0-9a-f]{64}", public_key)
        or len(message) != 32
        or not re.fullmatch(r"[0-9a-f]{128}", signature)
    ):
        return False
    public_x = int(public_key, 16)
    r = int(signature[:64], 16)
    s = int(signature[64:], 16)
    if public_x >= SECP256K1_FIELD or r >= SECP256K1_FIELD or s >= SECP256K1_ORDER:
        return False
    try:
        point = _lift_x(public_x)
    except ValueError:
        return False
    challenge = int.from_bytes(
        _tagged_hash(
            "BIP0340/challenge",
            r.to_bytes(32, "big") + public_x.to_bytes(32, "big") + message,
        ),
        "big",
    ) % SECP256K1_ORDER
    negative = (point[0], (-point[1]) % SECP256K1_FIELD)
    result = _point_add(
        _point_multiply(s, SECP256K1_GENERATOR),
        _point_multiply(challenge, negative),
    )
    return result is not None and result[1] % 2 == 0 and result[0] == r


def _valid_nip_oa_conditions(conditions: str) -> bool:
    if not conditions:
        return True
    if any(character.isspace() for character in conditions):
        return False
    for clause in conditions.split("&"):
        match = re.fullmatch(r"(kind=|created_at<|created_at>)(0|[1-9][0-9]*)", clause)
        if match is None:
            return False
        value = int(match.group(2))
        maximum = 65_535 if match.group(1) == "kind=" else 4_294_967_295
        if value > maximum:
            return False
    return True


def _verify_nip_oa_tag(tag: Any, agent_pubkey: str) -> str:
    if (
        not isinstance(tag, list)
        or len(tag) != 4
        or tag[0] != "auth"
        or not all(isinstance(item, str) for item in tag)
    ):
        raise ValueError("invalid Buzz owner auth tag")
    owner_pubkey, conditions, signature = tag[1], tag[2], tag[3]
    if (
        owner_pubkey == agent_pubkey
        or not re.fullmatch(r"[0-9a-f]{64}", owner_pubkey)
        or not _valid_nip_oa_conditions(conditions)
    ):
        raise ValueError("invalid Buzz owner auth tag")
    preimage = f"nostr:agent-auth:{agent_pubkey}:{conditions}".encode("utf-8")
    message = hashlib.sha256(preimage).digest()
    if not _verify_bip340(owner_pubkey, message, signature):
        raise ValueError("invalid Buzz owner auth tag")
    return owner_pubkey


@dataclass(frozen=True)
class BuzzTurnIdentity:
    author_pubkey: str | None
    source: str
    owner: bool = False


class BuzzTurnAuthorizer:
    def __init__(
        self,
        *,
        owner_pubkey: str | None = None,
        agent_pubkey: str | None = None,
        relay_pubkey: str | None = None,
    ):
        values = (owner_pubkey, agent_pubkey, relay_pubkey)
        if any(value is not None for value in values) and not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in values
        ):
            raise ValueError("invalid Buzz turn authorization configuration")
        self.owner_pubkey = owner_pubkey
        self.agent_pubkey = agent_pubkey
        self.relay_pubkey = relay_pubkey

    @classmethod
    def from_environment(cls) -> "BuzzTurnAuthorizer":
        private_key = os.environ.get("BUZZ_PRIVATE_KEY", "").strip()
        auth_tag_json = os.environ.get("BUZZ_AUTH_TAG", "").strip()
        relay_pubkey = os.environ.get("CRM_BUZZ_RELAY_PUBKEY", "").strip().lower()
        if not private_key and not auth_tag_json and not relay_pubkey:
            return cls()
        if not private_key or not auth_tag_json or not relay_pubkey:
            raise ValueError("incomplete Buzz turn authorization configuration")
        agent_pubkey = _nostr_public_key(private_key)
        try:
            auth_tag = json.loads(auth_tag_json)
        except json.JSONDecodeError:
            raise ValueError("invalid Buzz owner auth tag") from None
        owner_pubkey = _verify_nip_oa_tag(auth_tag, agent_pubkey)
        return cls(
            owner_pubkey=owner_pubkey,
            agent_pubkey=agent_pubkey,
            relay_pubkey=relay_pubkey,
        )

    def authorize(self, prompt: str) -> BuzzTurnIdentity:
        if self.owner_pubkey is None:
            return BuzzTurnIdentity(None, "test", owner=True)
        author_matches = re.findall(
            r"(?m)^From: .*\bhex: ([0-9a-f]{64})\)",
            prompt,
        )
        tag_matches = re.findall(r"(?m)^Tags: (.+)$", prompt)
        if not author_matches or not tag_matches:
            raise PermissionError("Buzz turn is missing authenticated event context")
        author = author_matches[-1]
        try:
            tags = json.loads(tag_matches[-1])
        except json.JSONDecodeError:
            raise PermissionError("Buzz turn has invalid event tags") from None
        if not isinstance(tags, list) or not all(isinstance(tag, list) for tag in tags):
            raise PermissionError("Buzz turn has invalid event tags")
        is_workflow = ["buzz:workflow", "true"] in tags
        if is_workflow:
            if (
                author != self.relay_pubkey
                or not tags
                or tags[0] != ["p", self.agent_pubkey]
            ):
                raise PermissionError("Buzz workflow turn failed relay attribution")
            return BuzzTurnIdentity(author, "workflow")
        if author == self.relay_pubkey:
            raise PermissionError("Buzz turn from relay lacks workflow attribution")
        if author == self.owner_pubkey:
            return BuzzTurnIdentity(author, "owner", owner=True)
        for tag in tags:
            if tag and tag[0] == "auth":
                try:
                    owner = _verify_nip_oa_tag(tag, author)
                except ValueError:
                    continue
                if owner == self.owner_pubkey:
                    return BuzzTurnIdentity(author, "sibling")
        raise PermissionError("Buzz turn author is not owner, sibling, or trusted workflow")

    @staticmethod
    def require_owner(identity: BuzzTurnIdentity) -> None:
        if not identity.owner:
            raise PermissionError("only the Buzz agent owner can mutate workflows")


@dataclass(frozen=True)
class BuzzWorkflowOperation:
    action: str
    name: str | None = None
    cron: str | None = None
    interval: str | None = None
    timezone_name: str | None = None
    task: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> "BuzzWorkflowOperation":
        if not isinstance(payload, dict):
            raise ValueError("invalid Buzz workflow operation")
        action = payload.get("action")
        if action == "list":
            if set(payload) != {"action"}:
                raise ValueError("invalid Buzz workflow list operation")
            return cls(action="list")
        if action in {"pause", "resume", "delete", "run_now"}:
            if set(payload) != {"action", "name"}:
                raise ValueError("invalid Buzz workflow mutation")
            name = payload.get("name")
            cls._validate_name(name)
            return cls(action=action, name=name)
        if action != "upsert":
            raise ValueError("invalid Buzz workflow action")

        allowed = {"action", "name", "cron", "interval", "timezone", "task"}
        if not set(payload).issubset(allowed):
            raise ValueError("invalid Buzz workflow fields")
        name = payload.get("name")
        task = payload.get("task")
        cron = payload.get("cron")
        interval = payload.get("interval")
        timezone_name = payload.get("timezone")
        cls._validate_name(name)
        if not isinstance(task, str) or not task.strip():
            raise ValueError("invalid Buzz workflow task")
        clean_task = task.strip()
        if (
            len(clean_task.encode("utf-8")) > MAX_WORKFLOW_TASK_BYTES
            or any(ord(character) < 32 and character not in "\n\t" for character in clean_task)
            or "@" in clean_task
        ):
            raise ValueError("invalid Buzz workflow task")
        if (cron is None) == (interval is None):
            raise ValueError("invalid Buzz workflow schedule")
        if cron is not None:
            if not isinstance(cron, str) or not CRON_PATTERN.fullmatch(cron):
                raise ValueError("invalid Buzz workflow cron")
            if timezone_name != "UTC":
                raise ValueError("invalid Buzz workflow timezone; calendar crons require UTC")
        else:
            if timezone_name is not None:
                raise ValueError("invalid Buzz workflow interval timezone")
            cls._validate_interval(interval)
        return cls(
            action="upsert",
            name=name,
            cron=cron,
            interval=interval,
            timezone_name=timezone_name,
            task=clean_task,
        )

    @staticmethod
    def _validate_name(name: Any) -> None:
        if not isinstance(name, str) or not WORKFLOW_NAME_PATTERN.fullmatch(name):
            raise ValueError("invalid Buzz workflow name")

    @staticmethod
    def _validate_interval(interval: Any) -> None:
        if not isinstance(interval, str):
            raise ValueError("invalid Buzz workflow interval")
        match = INTERVAL_PATTERN.fullmatch(interval)
        if match is None:
            raise ValueError("invalid Buzz workflow interval")
        value = int(match.group(1))
        seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
        if seconds < 60:
            raise ValueError("invalid Buzz workflow interval")

    def schedule_label(self) -> str:
        if self.cron:
            return f"{self.cron} UTC"
        return str(self.interval)


class BuzzWorkflowManager:
    def __init__(
        self,
        config: AgentConfig,
        crm_root: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        authorization_material: str | None = None,
    ):
        self.config = config
        self.crm_root = crm_root.expanduser()
        self.runner = runner
        if authorization_material is None:
            managed_identity = os.environ.get("BUZZ_PRIVATE_KEY", "").strip()
            managed_auth = os.environ.get("BUZZ_AUTH_TAG", "").strip()
            managed_authorization = managed_identity if managed_auth else ""
            if managed_authorization and not (
                re.fullmatch(r"[0-9a-fA-F]{64}", managed_authorization)
                or re.fullmatch(r"nsec1[02-9ac-hj-np-z]{58}", managed_authorization)
            ):
                raise ValueError("invalid Buzz workflow managed identity")
        else:
            managed_authorization = authorization_material
        self.signing_key = (
            hmac.new(
                managed_authorization.encode("utf-8"),
                b"crm-buzz-workflow-registry-v1",
                hashlib.sha256,
            ).digest()
            if managed_authorization
            else None
        )
        self.state_root = self.crm_root / "state"
        self.registry_path = (
            self.state_root / f"{self.config.agent_name}-buzz-workflows.json"
        )
        self.lock_path = (
            self.state_root / f"{self.config.agent_name}-buzz-workflows.lock"
        )

    def apply(
        self,
        operation: BuzzWorkflowOperation,
        destination: BuzzDestination,
    ) -> str:
        if self.signing_key is None:
            raise PermissionError("Buzz workflow authorization is unavailable")
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_root, 0o700)
        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            with os.fdopen(lock_fd, "r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                registry = self._load_registry()
                status = self._apply_locked(operation, destination, registry)
                return status
        except Exception:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            raise

    def reconcile_title(self) -> None:
        if self.signing_key is None or not self.registry_path.exists():
            return
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_root, 0o700)
        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            with os.fdopen(lock_fd, "r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                registry = self._load_registry()
                changed = False
                for name, record in registry["schedules"].items():
                    if record["agent_title"] == self.config.title:
                        continue
                    updated = dict(record)
                    updated["agent_title"] = self.config.title
                    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
                    self._run(
                        [
                            "buzz",
                            "workflows",
                            "update",
                            "--channel",
                            updated["channel_id"],
                            "--workflow",
                            updated["workflow_id"],
                            "--yaml",
                            "-",
                        ],
                        input_text=self._definition(name, updated),
                    )
                    registry["schedules"][name] = updated
                    changed = True
                if changed:
                    self._write_registry(registry)
        except Exception:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            raise

    def _apply_locked(
        self,
        operation: BuzzWorkflowOperation,
        destination: BuzzDestination,
        registry: dict[str, Any],
    ) -> str:
        schedules = registry["schedules"]
        if operation.action == "list":
            if not schedules:
                return "Buzz schedules: none."
            lines = []
            for name, record in sorted(schedules.items()):
                state = "active" if record["enabled"] else "paused"
                schedule = (
                    f"{record['cron']} UTC"
                    if record.get("cron")
                    else f"every {record['interval']}"
                )
                lines.append(f"- {name}: {state}, {schedule}")
            return "Buzz schedules:\n" + "\n".join(lines)

        name = str(operation.name)
        record = schedules.get(name)
        if operation.action == "upsert":
            return self._upsert(operation, destination, registry, record)
        if record is None:
            raise ValueError(f"Buzz workflow not found: {name}")
        if operation.action in {"pause", "resume"}:
            enabled = operation.action == "resume"
            updated = dict(record)
            updated["enabled"] = enabled
            updated["agent_title"] = self.config.title
            updated["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._run(
                [
                    "buzz",
                    "workflows",
                    "update",
                    "--channel",
                    updated["channel_id"],
                    "--workflow",
                    updated["workflow_id"],
                    "--yaml",
                    "-",
                ],
                input_text=self._definition(name, updated),
            )
            schedules[name] = updated
            self._write_registry(registry)
            verb = "resumed" if enabled else "paused"
            return f"Buzz schedule {verb}: {name}."
        if operation.action == "run_now":
            self._run(
                [
                    "buzz",
                    "workflows",
                    "trigger",
                    "--workflow",
                    record["workflow_id"],
                ]
            )
            return f"Buzz schedule triggered: {name}."
        if operation.action == "delete":
            self._run(
                [
                    "buzz",
                    "workflows",
                    "delete",
                    "--workflow",
                    record["workflow_id"],
                ]
            )
            del schedules[name]
            self._write_registry(registry)
            return f"Buzz schedule deleted: {name}."
        raise ValueError("invalid Buzz workflow action")

    def _upsert(
        self,
        operation: BuzzWorkflowOperation,
        destination: BuzzDestination,
        registry: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        record: dict[str, Any] = {
            "name": operation.name,
            "channel_id": (
                existing["channel_id"] if existing else destination.channel_id
            ),
            "cron": operation.cron,
            "interval": operation.interval,
            "timezone": operation.timezone_name,
            "task": operation.task,
            "enabled": existing["enabled"] if existing else True,
            "agent_title": self.config.title,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        if existing:
            record["workflow_id"] = existing["workflow_id"]
            arguments = [
                "buzz",
                "workflows",
                "update",
                "--channel",
                record["channel_id"],
                "--workflow",
                record["workflow_id"],
                "--yaml",
                "-",
            ]
            self._run(arguments, input_text=self._definition(str(operation.name), record))
            verb = "updated"
        else:
            arguments = [
                "buzz",
                "workflows",
                "create",
                "--channel",
                record["channel_id"],
                "--yaml",
                "-",
            ]
            completed = self._run(
                arguments,
                input_text=self._definition(str(operation.name), record),
            )
            record["workflow_id"] = self._workflow_id(completed.stdout)
            verb = "created"
        registry["schedules"][str(operation.name)] = record
        self._write_registry(registry)
        return (
            f"Buzz schedule {verb}: {operation.name} "
            f"({operation.schedule_label()})."
        )

    def _definition(self, name: str, record: dict[str, Any]) -> str:
        trigger: dict[str, str] = {"on": "schedule"}
        if record.get("cron"):
            trigger["cron"] = record["cron"]
        else:
            trigger["interval"] = record["interval"]
        definition = {
            "name": f"CRM {self.config.title}: {name}",
            "description": (
                f"CRM ACP schedule for {self.config.agent_name}; managed from Buzz."
            ),
            "trigger": trigger,
            "steps": [
                {
                    "id": "wake_agent",
                    "action": "send_message",
                    "text": (
                        f"@{self.config.title}\n"
                        f"[CRM scheduled task: {name}]\n"
                        f"{record['task']}"
                    ),
                }
            ],
            "enabled": bool(record["enabled"]),
        }
        return json.dumps(definition, separators=(",", ":"), ensure_ascii=False)

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {
                "schema_version": 1,
                "agent_name": self.config.agent_name,
                "schedules": {},
            }
        if self.registry_path.is_symlink():
            raise ValueError("invalid Buzz workflow registry")
        registry = json.loads(self.registry_path.read_text())
        if (
            not isinstance(registry, dict)
            or registry.get("schema_version") != 1
            or registry.get("agent_name") != self.config.agent_name
            or not isinstance(registry.get("schedules"), dict)
        ):
            raise ValueError("invalid Buzz workflow registry")
        expected_integrity = self._registry_integrity(registry)
        integrity = registry.get("integrity")
        if (
            not isinstance(integrity, str)
            or not hmac.compare_digest(integrity, expected_integrity)
        ):
            raise ValueError("invalid Buzz workflow registry integrity")
        for name, record in registry["schedules"].items():
            self._validate_record(name, record)
        return registry

    def _write_registry(self, registry: dict[str, Any]) -> None:
        registry["integrity"] = self._registry_integrity(registry)
        temporary = self.registry_path.with_name(
            f".{self.registry_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n"
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.registry_path)
        os.chmod(self.registry_path, 0o600)

    def _registry_integrity(self, registry: dict[str, Any]) -> str:
        if self.signing_key is None:
            raise PermissionError("Buzz workflow authorization is unavailable")
        authenticated = {
            key: value for key, value in registry.items() if key != "integrity"
        }
        canonical = json.dumps(
            authenticated,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(self.signing_key, canonical, hashlib.sha256).hexdigest()

    @staticmethod
    def _validate_record(name: Any, record: Any) -> None:
        required = {
            "name",
            "channel_id",
            "cron",
            "interval",
            "timezone",
            "task",
            "enabled",
            "agent_title",
            "created_at",
            "updated_at",
            "workflow_id",
        }
        if (
            not isinstance(name, str)
            or not WORKFLOW_NAME_PATTERN.fullmatch(name)
            or not isinstance(record, dict)
            or set(record) != required
            or record.get("name") != name
            or not isinstance(record.get("enabled"), bool)
            or not UUID_PATTERN.fullmatch(str(record.get("channel_id") or ""))
            or not UUID_PATTERN.fullmatch(str(record.get("workflow_id") or ""))
            or not isinstance(record.get("agent_title"), str)
            or not isinstance(record.get("task"), str)
        ):
            raise ValueError("invalid Buzz workflow registry record")
        operation_payload: dict[str, Any] = {
            "action": "upsert",
            "name": name,
            "task": record["task"],
        }
        if record.get("cron"):
            operation_payload.update(
                {
                    "cron": record["cron"],
                    "timezone": record["timezone"],
                }
            )
        else:
            operation_payload["interval"] = record.get("interval")
        BuzzWorkflowOperation.from_payload(operation_payload)

    def _run(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                arguments,
                input=input_text,
                text=True,
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            raise RuntimeError("Buzz workflow command failed") from None

    @staticmethod
    def _workflow_id(output: str) -> str:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            raise RuntimeError("Buzz workflow create returned an invalid response") from None
        workflow_id = payload.get("workflow_id") if isinstance(payload, dict) else None
        if not isinstance(workflow_id, str) or not UUID_PATTERN.fullmatch(workflow_id):
            raise RuntimeError("Buzz workflow create returned no workflow ID")
        return workflow_id


@dataclass(frozen=True)
class MemoryUpdate:
    slug: str
    value: str

    def __post_init__(self) -> None:
        if self.slug != "core":
            raise ValueError("unsupported Buzz memory slug")
        if not self.value:
            raise ValueError("Buzz memory value cannot be empty")
        if len(self.value.encode("utf-8")) > MAX_CORE_BYTES:
            raise ValueError("Buzz memory value exceeds size limit")


@dataclass(frozen=True)
class CrmReply:
    text: str
    memory_updates: tuple[MemoryUpdate, ...] = ()
    workflow_operation: BuzzWorkflowOperation | None = None

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> "CrmReply":
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("CRM returned an empty ACP reply")
        raw_updates = message.get("buzz_memory_updates", [])
        if not isinstance(raw_updates, list) or len(raw_updates) > 1:
            raise ValueError("invalid Buzz memory updates")
        updates: list[MemoryUpdate] = []
        for raw in raw_updates:
            if not isinstance(raw, dict) or set(raw) != {"slug", "value"}:
                raise ValueError("invalid Buzz memory update")
            slug = raw.get("slug")
            value = raw.get("value")
            if not isinstance(slug, str) or not isinstance(value, str):
                raise ValueError("invalid Buzz memory update")
            updates.append(MemoryUpdate(slug, value))
        raw_workflow = message.get("buzz_workflow_operation")
        workflow = (
            None
            if raw_workflow is None
            else BuzzWorkflowOperation.from_payload(raw_workflow)
        )
        return cls(text.strip(), tuple(updates), workflow)


def prompt_text(blocks: list[dict[str, Any]]) -> str:
    text = "\n".join(
        str(block["text"])
        for block in blocks
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    ).strip()
    if not text:
        raise ValueError("ACP prompt has no text content")
    return text


def parse_buzz_destination(prompt: str) -> BuzzDestination:
    context_match = re.search(
        r"(?ms)^\[Context\]\n(?P<context>.*?)(?=^\[[^\n]+\]\n|\Z)",
        prompt,
    )
    if context_match is None:
        raise ValueError("Buzz prompt is missing Context")
    context = context_match.group("context")
    channel_match = re.search(
        r"(?m)^Channel: .+ \(#(?P<channel>[0-9a-f-]{36})\)$",
        context,
    )
    if channel_match is None:
        channel_match = re.search(
            r"(?m)^Channel: (?P<channel>[0-9a-f-]{36})$",
            context,
        )
    if channel_match is None:
        raise ValueError("Buzz Context is missing a channel UUID")
    reply_match = re.search(r"--reply-to\s+([0-9a-f]{64})", context)
    if reply_match is None and re.search(r"(?m)^Scope: thread$", context):
        reply_match = re.search(r"(?m)^Thread root: ([0-9a-f]{64})$", context)
    return BuzzDestination(
        channel_match.group("channel"),
        reply_match.group(1) if reply_match else None,
    )


def default_crm_root() -> Path:
    configured = os.environ.get("CRM_ROOT")
    if configured:
        return Path(configured).expanduser()
    instance_id = os.environ.get("CRM_INSTANCE_ID", "")
    if not instance_id:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("CRM_INSTANCE_ID="):
                    instance_id = line.partition("=")[2].strip()
                    break
    return Path.home() / ".claude-remote" / (instance_id or "default")


class CrmBus:
    def __init__(
        self,
        root: Path,
        *,
        config: AgentConfig,
        poll_interval: float = 0.25,
    ):
        self.root = root
        self.config = config
        self.poll_interval = poll_interval

    def inject(self, turn_id: str, session_id: str, prompt: str) -> Path:
        inbox = self.root / "inbox" / self.config.agent_name
        inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        helper = Path(__file__).resolve().parent.parent / "core/bus/send-acp-reply.sh"
        message = {
            "id": turn_id,
            "from": self.config.adapter_name,
            "to": self.config.agent_name,
            "priority": "high",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "text": (
                f"=== BUZZ ACP TURN [session:{session_id}] ===\n"
                f"{prompt}\n\n"
                "Reply through the ACP envelope; the adapter publishes to Buzz.\n"
                f"Normal: bash '{helper}' {self.config.adapter_name} {turn_id} "
                "'<reply>'\n"
                f"Core changed: bash '{helper}' {self.config.adapter_name} {turn_id} "
                "'<reply>' '<full-core-file>'\n"
                "Buzz schedule changed: write one typed operation to "
                f"`$CRM_ROOT/state/{self.config.agent_name}-workflow-op.json`, then "
                f"bash '{helper}' {self.config.adapter_name} {turn_id} '<reply>' "
                f"--workflow '$CRM_ROOT/state/{self.config.agent_name}-workflow-op.json'. "
                "Supported actions: upsert, list, pause, resume, delete, run_now. "
                "Upsert uses name, task, and either interval or a five-field UTC cron "
                "with timezone set to UTC. Never write raw workflow YAML."
            ),
            "reply_to": None,
        }
        final = inbox / f"1-{time.time_ns()}-from-{self.config.adapter_name}.json"
        temporary = final.with_suffix(".tmp")
        temporary.write_text(json.dumps(message, separators=(",", ":")) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(final)
        return final

    async def wait_reply(
        self,
        turn_id: str,
        cancel: asyncio.Event,
        *,
        timeout: float,
    ) -> CrmReply:
        inbox = self.root / "inbox" / self.config.adapter_name
        processed = self.root / "processed" / self.config.adapter_name
        inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        processed.mkdir(parents=True, exist_ok=True, mode=0o700)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise TurnCancelled
            for path in sorted(inbox.glob("*.json")):
                try:
                    message = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    str(message.get("reply_to") or "") != turn_id
                    or message.get("from") != self.config.agent_name
                    or message.get("to") != self.config.adapter_name
                ):
                    continue
                reply = CrmReply.from_message(message)
                path.replace(processed / path.name)
                return reply
            try:
                await asyncio.wait_for(cancel.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue
        raise TimeoutError(f"timed out waiting for {self.config.title}'s CRM reply")

    def cancel_active(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        runner(
            ["tmux", "send-keys", "-t", self.config.tmux_target, "C-c"],
            check=False,
            capture_output=True,
        )


class BuzzPublisher:
    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.runner = runner

    def publish(self, destination: BuzzDestination, content: str) -> str:
        message = content.strip()
        if not message:
            raise ValueError("Buzz reply cannot be empty")
        arguments = [
            "buzz",
            "messages",
            "send",
            "--channel",
            destination.channel_id,
        ]
        if destination.reply_to:
            arguments.extend(["--reply-to", destination.reply_to])
        arguments.extend(["--content", "-"])
        completed = self.runner(
            arguments,
            input=message,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout


def _unified_diff(old: str, new: str) -> str:
    lines = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="core",
            tofile="core",
        )
    )
    result: list[str] = []
    for index, line in enumerate(lines):
        if index >= 2 and line and line[0] in " +-":
            if not line.endswith("\n"):
                result.extend([line + "\n", "\\ No newline at end of file\n"])
                continue
        result.append(line)
    return "".join(result)


class BuzzMemoryWriter:
    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.runner = runner

    def _run(self, arguments: list[str], *, input_text: str | None = None):
        return self.runner(
            arguments,
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
        )

    def _slugs(self) -> set[str]:
        completed = self._run(["buzz", "mem", "ls", "--json"])
        payload = json.loads(completed.stdout)
        if isinstance(payload, dict):
            entries = payload.get("memories", payload.get("entries", []))
        else:
            entries = payload
        if not isinstance(entries, list):
            raise ValueError("invalid Buzz memory list response")
        return {
            entry["slug"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
        }

    def apply(self, updates: Sequence[MemoryUpdate]) -> None:
        for update in updates:
            if update.slug not in self._slugs():
                self._run(
                    ["buzz", "mem", "set", update.slug, "-"],
                    input_text=update.value,
                )
                continue
            current = self._run(["buzz", "mem", "get", update.slug]).stdout
            if current == update.value:
                continue
            base_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
            patch = _unified_diff(current, update.value)
            self._run(
                [
                    "buzz",
                    "mem",
                    "patch",
                    update.slug,
                    "--base-hash",
                    base_hash,
                ],
                input_text=patch,
            )


class CrmAcpAgent:
    def __init__(
        self,
        config: AgentConfig,
        bus: CrmBus,
        publisher: BuzzPublisher,
        memory: BuzzMemoryWriter,
        workflows: BuzzWorkflowManager | None = None,
        authorizer: BuzzTurnAuthorizer | None = None,
        *,
        reply_timeout: float = 600,
    ):
        self.config = config
        self.bus = bus
        self.publisher = publisher
        self.memory = memory
        self.workflows = workflows
        self.authorizer = authorizer or BuzzTurnAuthorizer()
        self.reply_timeout = reply_timeout
        self.sessions: dict[str, dict[str, Any]] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.turn_lock = asyncio.Lock()
        self.active_session_id: str | None = None

    async def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = int(params.get("protocolVersion") or 2)
        return {
            "protocolVersion": 2 if requested >= 2 else requested,
            "info": {
                "name": f"crm-acp-{self.config.agent_name}",
                "title": self.config.title,
                "version": "0.2.0",
            },
            "capabilities": {"session": {}},
            "agentCapabilities": {},
        }

    async def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = f"{self.config.agent_name}-{uuid.uuid4().hex}"
        self.sessions[session_id] = {
            "cwd": str(params.get("cwd") or ""),
            "system_prompt": str(params.get("systemPrompt") or ""),
        }
        self.cancel_events[session_id] = asyncio.Event()
        return {
            "sessionId": session_id,
            "configOptions": self.config.model_config(),
        }

    async def load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId") or "")
        if not session_id:
            raise ValueError("session/load requires sessionId")
        self.sessions.setdefault(
            session_id,
            {
                "cwd": str(params.get("cwd") or ""),
                "system_prompt": str(params.get("systemPrompt") or ""),
            },
        )
        self.cancel_events.setdefault(session_id, asyncio.Event())
        return {}

    async def set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId") or "")
        if session_id not in self.sessions:
            raise ValueError("unknown ACP session")
        config_id = str(params.get("configId") or params.get("id") or "")
        value: Any = params.get("value")
        if isinstance(value, dict):
            value = value.get("value")
        if config_id != "model" or value != self.config.model_id:
            raise ValueError(f"{self.config.title} ACP supports only its fixed model")
        return {"configOptions": self.config.model_config()}

    async def prompt(
        self,
        params: dict[str, Any],
        notify: Notify,
    ) -> dict[str, str]:
        if not self.config.prompt_enabled:
            raise ValueError("CRM factory preview cannot process prompts")
        session_id = str(params.get("sessionId") or "")
        if session_id not in self.sessions:
            raise ValueError("unknown ACP session")
        content = prompt_text(list(params.get("prompt") or []))
        turn_identity = self.authorizer.authorize(content)
        destination = parse_buzz_destination(content)
        system_prompt = str(
            self.sessions[session_id].get("system_prompt") or ""
        ).strip()
        crm_prompt = (
            f"[Buzz ACP managed instructions]\n{system_prompt}\n\n{content}"
            if system_prompt
            else content
        )
        async with self.turn_lock:
            cancel = self.cancel_events.setdefault(session_id, asyncio.Event())
            cancel.clear()
            self.active_session_id = session_id
            turn_id = f"acp-{uuid.uuid4().hex}"
            try:
                self.bus.inject(turn_id, session_id, crm_prompt)
                reply = await self.bus.wait_reply(
                    turn_id,
                    cancel,
                    timeout=self.reply_timeout,
                )
                self.memory.apply(reply.memory_updates)
                visible_reply = reply.text
                if reply.workflow_operation is not None:
                    if self.workflows is None:
                        raise RuntimeError("Buzz workflow manager is unavailable")
                    self.authorizer.require_owner(turn_identity)
                    workflow_status = self.workflows.apply(
                        reply.workflow_operation,
                        destination,
                    )
                    visible_reply = f"{visible_reply}\n\n{workflow_status}"
                self.publisher.publish(destination, visible_reply)
                await notify(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": visible_reply},
                            },
                        },
                    }
                )
                return {"stopReason": "end_turn"}
            except TurnCancelled:
                return {"stopReason": "cancelled"}
            finally:
                self.active_session_id = None

    async def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId") or "")
        if session_id == self.active_session_id:
            self.cancel_events.setdefault(session_id, asyncio.Event()).set()
            self.bus.cancel_active()
        return {}


class JsonRpcServer:
    def __init__(self, agent: CrmAcpAgent):
        self.agent = agent
        self.write_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[None]] = set()

    async def write(self, message: dict[str, Any]) -> None:
        async with self.write_lock:
            sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    async def dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        try:
            if method == "initialize":
                result = await self.agent.initialize(params)
            elif method == "session/new":
                result = await self.agent.new_session(params)
            elif method == "session/load":
                result = await self.agent.load_session(params)
            elif method == "session/prompt":
                result = await self.agent.prompt(params, self.write)
            elif method == "session/cancel":
                result = await self.agent.cancel(params)
            elif method == "session/set_config_option":
                result = await self.agent.set_config_option(params)
            else:
                raise LookupError(f"unsupported ACP method: {method}")
            if request_id is not None:
                await self.write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except LookupError as error:
            if request_id is not None:
                await self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": str(error)},
                    }
                )
        except (ValueError, TypeError) as error:
            if request_id is not None:
                await self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": str(error)},
                    }
                )
        except Exception as error:
            if request_id is not None:
                await self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": str(error)},
                    }
                )

    async def run(self) -> None:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                await self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                )
                continue
            task = asyncio.create_task(self.dispatch(message))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        if self.tasks:
            await asyncio.gather(*self.tasks)


def parse_runtime(
    argv: list[str] | None = None,
    *,
    environment: dict[str, str] | None = None,
) -> AgentConfig:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--factory", action="store_true")
    mode.add_argument("--agent")
    parser.add_argument("--display-name")
    parser.add_argument("--adapter-name")
    parser.add_argument("--model-id")
    parser.add_argument("--model-label")
    parser.add_argument("--tmux-target")
    arguments = parser.parse_args(argv)
    if arguments.factory:
        factory_environment = dict(os.environ) if environment is None else environment
        instance_id = factory_environment.get("CRM_INSTANCE_ID", "default")
        if not AGENT_PATTERN.fullmatch(instance_id):
            raise ValueError("invalid CRM instance ID")
        return CrmAcpFactory(
            crm_root=Path.home() / ".claude-remote" / instance_id,
            template_root=Path(__file__).resolve().parent.parent,
            instance_id=instance_id,
        ).resolve(factory_environment)
    if not arguments.display_name:
        parser.error("--display-name is required with --agent")
    return AgentConfig.create(
        arguments.agent,
        arguments.display_name,
        adapter_name=arguments.adapter_name,
        model_id=arguments.model_id,
        model_label=arguments.model_label,
        tmux_target=arguments.tmux_target,
    )


def parse_agent_config(argv: list[str] | None = None) -> AgentConfig:
    return parse_runtime(argv)


async def run_agent(config: AgentConfig) -> None:
    timeout = float(os.environ.get("CRM_ACP_REPLY_TIMEOUT", "600"))
    crm_root = default_crm_root()
    bus = CrmBus(crm_root, config=config)
    workflows = BuzzWorkflowManager(config, crm_root)
    workflows.reconcile_title()
    authorizer = BuzzTurnAuthorizer.from_environment()
    agent = CrmAcpAgent(
        config,
        bus,
        BuzzPublisher(),
        BuzzMemoryWriter(),
        workflows,
        authorizer,
        reply_timeout=timeout,
    )
    await JsonRpcServer(agent).run()


async def main(argv: list[str] | None = None) -> None:
    await run_agent(parse_runtime(argv))


if __name__ == "__main__":
    asyncio.run(main())
