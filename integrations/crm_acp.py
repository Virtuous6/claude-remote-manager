#!/usr/bin/env python3
"""ACP stdio adapter for isolated Claude Remote Manager agents."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
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
MAX_CORE_BYTES = 16_384
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
class BuzzDestination:
    channel_id: str
    reply_to: str | None = None

    def __post_init__(self) -> None:
        if not UUID_PATTERN.fullmatch(self.channel_id):
            raise ValueError("invalid Buzz channel UUID")
        if self.reply_to is not None and not EVENT_PATTERN.fullmatch(self.reply_to):
            raise ValueError("invalid Buzz reply event ID")


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
        return cls(text.strip(), tuple(updates))


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
                "'<reply>' '<full-core-file>'"
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
        *,
        reply_timeout: float = 600,
    ):
        self.config = config
        self.bus = bus
        self.publisher = publisher
        self.memory = memory
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
        session_id = str(params.get("sessionId") or "")
        if session_id not in self.sessions:
            raise ValueError("unknown ACP session")
        content = prompt_text(list(params.get("prompt") or []))
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
                self.publisher.publish(destination, reply.text)
                await notify(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": reply.text},
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


def parse_agent_config(argv: list[str] | None = None) -> AgentConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--adapter-name")
    parser.add_argument("--model-id")
    parser.add_argument("--model-label")
    parser.add_argument("--tmux-target")
    arguments = parser.parse_args(argv)
    return AgentConfig.create(
        arguments.agent,
        arguments.display_name,
        adapter_name=arguments.adapter_name,
        model_id=arguments.model_id,
        model_label=arguments.model_label,
        tmux_target=arguments.tmux_target,
    )


async def run_agent(config: AgentConfig) -> None:
    timeout = float(os.environ.get("CRM_ACP_REPLY_TIMEOUT", "600"))
    bus = CrmBus(default_crm_root(), config=config)
    agent = CrmAcpAgent(
        config,
        bus,
        BuzzPublisher(),
        BuzzMemoryWriter(),
        reply_timeout=timeout,
    )
    await JsonRpcServer(agent).run()


async def main(argv: list[str] | None = None) -> None:
    await run_agent(parse_agent_config(argv))


if __name__ == "__main__":
    asyncio.run(main())
