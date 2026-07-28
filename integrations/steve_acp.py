#!/usr/bin/env python3
"""Compatibility entrypoint for the original Steve ACP harness."""

from __future__ import annotations

import asyncio
import os

try:
    from integrations.crm_acp import (
        AgentConfig,
        BuzzDestination,
        BuzzMemoryWriter,
        BuzzPublisher,
        CrmAcpAgent,
        CrmBus as GenericCrmBus,
        CrmReply,
        JsonRpcServer,
        MemoryUpdate,
        TurnCancelled,
        default_crm_root,
        parse_buzz_destination,
        prompt_text,
    )
except ModuleNotFoundError:
    from crm_acp import (  # type: ignore[no-redef]
        AgentConfig,
        BuzzDestination,
        BuzzMemoryWriter,
        BuzzPublisher,
        CrmAcpAgent,
        CrmBus as GenericCrmBus,
        CrmReply,
        JsonRpcServer,
        MemoryUpdate,
        TurnCancelled,
        default_crm_root,
        parse_buzz_destination,
        prompt_text,
    )

__all__ = [
    "BuzzDestination",
    "BuzzMemoryWriter",
    "BuzzPublisher",
    "CrmBus",
    "CrmReply",
    "JsonRpcServer",
    "MemoryUpdate",
    "SteveAcpAgent",
    "TurnCancelled",
    "default_crm_root",
    "fixed_model_config",
    "parse_buzz_destination",
    "prompt_text",
]


STEVE_CONFIG = AgentConfig.steve_compatibility()
ADAPTER_NAME = STEVE_CONFIG.adapter_name
STEVE_AGENT = STEVE_CONFIG.agent_name
FIXED_MODEL_ID = STEVE_CONFIG.model_id
FIXED_MODEL_LABEL = STEVE_CONFIG.model_label


def fixed_model_config():
    return STEVE_CONFIG.model_config()


class CrmBus(GenericCrmBus):
    def __init__(
        self,
        root,
        *,
        agent_name: str = STEVE_AGENT,
        adapter_name: str = ADAPTER_NAME,
        poll_interval: float = 0.25,
    ):
        config = AgentConfig.create(
            agent_name,
            "Steve Kingsley",
            adapter_name=adapter_name,
            model_id=FIXED_MODEL_ID,
            model_label=FIXED_MODEL_LABEL,
            tmux_target=os.environ.get(
                "STEVE_TMUX_TARGET",
                "crm-default-steve-kingsley:0.0",
            ),
        )
        super().__init__(root, config=config, poll_interval=poll_interval)


class SteveAcpAgent(CrmAcpAgent):
    def __init__(
        self,
        bus: CrmBus,
        publisher: BuzzPublisher,
        *,
        reply_timeout: float = 600,
        memory: BuzzMemoryWriter | None = None,
    ):
        super().__init__(
            STEVE_CONFIG,
            bus,
            publisher,
            memory or BuzzMemoryWriter(),
            reply_timeout=reply_timeout,
        )

    async def initialize(self, params):
        result = await super().initialize(params)
        result["info"]["name"] = "steve-acp"
        return result


async def main() -> None:
    timeout = float(
        os.environ.get(
            "STEVE_ACP_REPLY_TIMEOUT",
            os.environ.get("CRM_ACP_REPLY_TIMEOUT", "600"),
        )
    )
    bus = CrmBus(default_crm_root())
    agent = SteveAcpAgent(
        bus,
        BuzzPublisher(),
        reply_timeout=timeout,
        memory=BuzzMemoryWriter(),
    )
    await JsonRpcServer(agent).run()


if __name__ == "__main__":
    asyncio.run(main())
