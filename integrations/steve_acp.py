#!/usr/bin/env python3
"""ACP stdio adapter for the existing Claude Remote Manager Steve session."""

from __future__ import annotations

import asyncio
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
from typing import Any, Awaitable, Callable


ADAPTER_NAME = "buzz-acp"
STEVE_AGENT = "steve-kingsley"
FIXED_MODEL_ID = "crm-steve-current"
FIXED_MODEL_LABEL = "Existing Steve CRM session — Claude default"
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
EVENT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
Notify = Callable[[dict[str, Any]], Awaitable[None]]


class TurnCancelled(Exception):
    """Raised when Buzz cancels the active Steve ACP turn."""


@dataclass(frozen=True)
class BuzzDestination:
    channel_id: str
    reply_to: str | None = None

    def __post_init__(self) -> None:
        if not UUID_PATTERN.fullmatch(self.channel_id):
            raise ValueError("invalid Buzz channel UUID")
        if self.reply_to is not None and not EVENT_PATTERN.fullmatch(self.reply_to):
            raise ValueError("invalid Buzz reply event ID")


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


def fixed_model_config() -> list[dict[str, Any]]:
    return [
        {
            "configId": "model",
            "id": "model",
            "name": "Model",
            "displayName": "Model",
            "description": "Model is owned by Steve's existing CRM Claude session.",
            "category": "model",
            "type": "select",
            "currentValue": FIXED_MODEL_ID,
            "options": [
                {
                    "value": FIXED_MODEL_ID,
                    "name": FIXED_MODEL_LABEL,
                    "displayName": FIXED_MODEL_LABEL,
                }
            ],
        }
    ]


class CrmBus:
    def __init__(
        self,
        root: Path,
        *,
        agent_name: str = STEVE_AGENT,
        adapter_name: str = ADAPTER_NAME,
        poll_interval: float = 0.25,
    ):
        self.root = root
        self.agent_name = agent_name
        self.adapter_name = adapter_name
        self.poll_interval = poll_interval

    def inject(self, turn_id: str, session_id: str, prompt: str) -> Path:
        inbox = self.root / "inbox" / self.agent_name
        inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        message = {
            "id": turn_id,
            "from": self.adapter_name,
            "to": self.agent_name,
            "priority": "high",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "text": (
                f"=== BUZZ ACP TURN [session:{session_id}] ===\n"
                f"{prompt}\n\n"
                "Reply through this agent-message envelope. "
                "The ACP adapter publishes to Buzz."
            ),
            "reply_to": None,
        }
        final = inbox / f"1-{time.time_ns()}-from-{self.adapter_name}.json"
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
    ) -> str:
        inbox = self.root / "inbox" / self.adapter_name
        processed = self.root / "processed" / self.adapter_name
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
                    or message.get("from") != self.agent_name
                ):
                    continue
                content = str(message.get("text") or "").strip()
                if not content:
                    raise ValueError("Steve returned an empty ACP reply")
                path.replace(processed / path.name)
                return content
            try:
                await asyncio.wait_for(cancel.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue
        raise TimeoutError("timed out waiting for Steve's CRM reply")

    def cancel_active(self) -> None:
        target = os.environ.get(
            "STEVE_TMUX_TARGET",
            "crm-default-steve-kingsley:0.0",
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "C-c"],
            check=False,
            capture_output=True,
        )


class BuzzPublisher:
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
        completed = subprocess.run(
            arguments,
            input=message,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout


class SteveAcpAgent:
    def __init__(
        self,
        bus: CrmBus,
        publisher: BuzzPublisher,
        *,
        reply_timeout: float = 600,
    ):
        self.bus = bus
        self.publisher = publisher
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
                "name": "steve-acp",
                "title": "Steve Kingsley",
                "version": "0.1.0",
            },
            "capabilities": {"session": {}},
            "agentCapabilities": {},
        }

    async def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = f"steve-{uuid.uuid4().hex}"
        self.sessions[session_id] = {
            "cwd": str(params.get("cwd") or ""),
            "system_prompt": str(params.get("systemPrompt") or ""),
        }
        self.cancel_events[session_id] = asyncio.Event()
        return {
            "sessionId": session_id,
            "configOptions": fixed_model_config(),
        }

    async def load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId") or "")
        if not session_id:
            raise ValueError("session/load requires sessionId")
        self.sessions.setdefault(session_id, {"cwd": str(params.get("cwd") or "")})
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
        if config_id != "model" or value != FIXED_MODEL_ID:
            raise ValueError("Steve ACP supports only its fixed model")
        return {"configOptions": fixed_model_config()}

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
        system_prompt = str(self.sessions[session_id].get("system_prompt") or "").strip()
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
                response = await self.bus.wait_reply(
                    turn_id,
                    cancel,
                    timeout=self.reply_timeout,
                )
                self.publisher.publish(destination, response)
                await notify(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": response},
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
    def __init__(self, agent: SteveAcpAgent):
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
                await self.write(
                    {"jsonrpc": "2.0", "id": request_id, "result": result}
                )
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


async def main() -> None:
    timeout = float(os.environ.get("STEVE_ACP_REPLY_TIMEOUT", "600"))
    agent = SteveAcpAgent(
        CrmBus(default_crm_root()),
        BuzzPublisher(),
        reply_timeout=timeout,
    )
    await JsonRpcServer(agent).run()


if __name__ == "__main__":
    asyncio.run(main())
