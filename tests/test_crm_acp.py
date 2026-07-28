#!/usr/bin/env python3

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from integrations.crm_acp import (
    AgentConfig,
    BuzzDestination,
    BuzzMemoryWriter,
    CrmAcpAgent,
    CrmBus,
    CrmReply,
    MemoryUpdate,
    parse_agent_config,
)


CHANNEL_ID = "312d60a4-0198-4ef0-a13e-9d0e121f1833"
EVENT_ID = "a" * 64
TURN_ID = "acp-" + ("b" * 32)


def buzz_prompt() -> str:
    return (
        "[Context]\n"
        "Scope: channel\n"
        f"Channel: Testing Maxine (#{CHANNEL_ID})\n"
        f"Reply using --reply-to {EVENT_ID}.\n"
        "[Event]\n"
        "From: JLUCKY\n"
        "Content: Help me find the pulse in this draft."
    )


class AgentConfigTest(unittest.TestCase):
    def test_maxine_has_isolated_runtime_defaults(self):
        config = AgentConfig.create("maxine", "Maxine")

        self.assertEqual(config.agent_name, "maxine")
        self.assertEqual(config.adapter_name, "buzz-acp-maxine")
        self.assertEqual(config.model_id, "crm-maxine-current")
        self.assertEqual(config.tmux_target, "crm-default-maxine:0.0")

    def test_steve_compatibility_defaults_remain_stable(self):
        config = AgentConfig.steve_compatibility()

        self.assertEqual(config.agent_name, "steve-kingsley")
        self.assertEqual(config.adapter_name, "buzz-acp")
        self.assertEqual(config.model_id, "crm-steve-current")
        self.assertEqual(config.title, "Steve Kingsley")

    def test_agent_identity_fails_closed(self):
        for invalid in ("", "../maxine", "Maxine", "maxine_2", "a" * 64):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "agent"):
                    AgentConfig.create(invalid, "Maxine")

    def test_cli_builds_the_same_isolated_maxine_config(self):
        config = parse_agent_config(
            [
                "--agent",
                "maxine",
                "--display-name",
                "Maxine",
                "--tmux-target",
                "crm-default-maxine:0.0",
            ]
        )

        self.assertEqual(config, AgentConfig.create("maxine", "Maxine"))


class MaxinePilotFilesTest(unittest.TestCase):
    def test_config_is_writing_scoped_and_telegram_optional(self):
        config = json.loads(Path("agents/maxine/config.json").read_text())

        self.assertEqual(config["agent_name"], "maxine")
        self.assertFalse(config["telegram_enabled"])
        self.assertEqual(config["crons"], [])
        self.assertEqual(
            config["working_directory"],
            (
                "/Users/josephsanchez/Documents/OBSIDIAN/Lucky Obsidian/"
                "_CC/content/joe/1000months"
            ),
        )
        serialized = json.dumps(config).lower()
        self.assertNotIn("token", serialized)
        self.assertNotIn("private_key", serialized)

    def test_persona_protects_voice_and_has_explicit_core_policy(self):
        instructions = Path("agents/maxine/CLAUDE.md").read_text()

        self.assertIn("Keep Joe's voice", instructions)
        self.assertIn("buzz-acp-maxine", instructions)
        self.assertIn("send-acp-reply.sh", instructions)
        self.assertIn("without owner approval", instructions)
        self.assertIn("Never put secrets", instructions)
        self.assertIn("No Telegram identity is configured", instructions)

    def test_harness_uses_generic_adapter_without_credentials(self):
        harness = json.loads(Path("integrations/harnesses/maxine-acp.json").read_text())

        self.assertEqual(harness["id"], "maxine-acp")
        self.assertEqual(
            harness["args"][-4:], ["--agent", "maxine", "--display-name", "Maxine"]
        )
        self.assertEqual(harness["env"], {"CRM_INSTANCE_ID": "default"})
        serialized = json.dumps(harness).lower()
        self.assertNotIn("private_key", serialized)
        self.assertNotIn("auth_tag", serialized)


class CrmReplyTest(unittest.TestCase):
    def test_parses_core_update_from_correlated_envelope(self):
        reply = CrmReply.from_message(
            {
                "text": "The opening has the pulse.",
                "buzz_memory_updates": [
                    {
                        "slug": "core",
                        "value": "I am Maxine, Joe's 1000 Months writing companion.\n",
                    }
                ],
            }
        )

        self.assertEqual(reply.text, "The opening has the pulse.")
        self.assertEqual(
            reply.memory_updates,
            (
                MemoryUpdate(
                    "core",
                    "I am Maxine, Joe's 1000 Months writing companion.\n",
                ),
            ),
        )

    def test_rejects_implicit_or_unsafe_memory_updates(self):
        bad_updates = (
            [{"slug": "mem/private", "value": "no"}],
            [{"slug": "core", "value": ""}],
            [{"slug": "core", "value": "x", "extra": True}],
            [{"slug": "core", "value": "x" * 16385}],
            "core: changed",
        )
        for updates in bad_updates:
            with self.subTest(updates=type(updates).__name__):
                with self.assertRaisesRegex(ValueError, "memory"):
                    CrmReply.from_message(
                        {"text": "Visible reply", "buzz_memory_updates": updates}
                    )


class CrmBusIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_maxine_uses_only_her_agent_and_return_inbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = AgentConfig.create("maxine", "Maxine")
            bus = CrmBus(root, config=config, poll_interval=0.001)
            inbound = bus.inject(TURN_ID, "session-1", buzz_prompt())
            injected = json.loads(inbound.read_text())

            self.assertEqual(injected["to"], "maxine")
            self.assertEqual(injected["from"], "buzz-acp-maxine")
            self.assertIn("send-acp-reply.sh", injected["text"])

            inbox = root / "inbox/buzz-acp-maxine"
            inbox.mkdir(parents=True)
            (inbox / "steve.json").write_text(
                json.dumps(
                    {
                        "from": "steve-kingsley",
                        "to": "buzz-acp-maxine",
                        "reply_to": TURN_ID,
                        "text": "wrong agent",
                    }
                )
            )
            (inbox / "wrong-target.json").write_text(
                json.dumps(
                    {
                        "from": "maxine",
                        "to": "buzz-acp-steve-kingsley",
                        "reply_to": TURN_ID,
                        "text": "wrong inbox identity",
                    }
                )
            )
            (inbox / "maxine.json").write_text(
                json.dumps(
                    {
                        "from": "maxine",
                        "to": "buzz-acp-maxine",
                        "reply_to": TURN_ID,
                        "text": "The middle wants less explanation.",
                    }
                )
            )

            reply = await bus.wait_reply(
                TURN_ID,
                asyncio.Event(),
                timeout=1,
            )

            self.assertEqual(reply.text, "The middle wants less explanation.")
            self.assertTrue((root / "processed/buzz-acp-maxine/maxine.json").exists())

    def test_cancel_targets_only_configured_tmux_session(self):
        config = AgentConfig.create(
            "maxine",
            "Maxine",
            tmux_target="crm-writing-maxine:0.0",
        )
        bus = CrmBus(Path("/tmp/unused"), config=config)
        runner = Mock()

        bus.cancel_active(runner=runner)

        runner.assert_called_once_with(
            [
                "tmux",
                "send-keys",
                "-t",
                "crm-writing-maxine:0.0",
                "C-c",
            ],
            check=False,
            capture_output=True,
        )


class BuzzMemoryWriterTest(unittest.TestCase):
    def test_creates_missing_core_using_stdin_only(self):
        runner = Mock()
        runner.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="[]", stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"accepted":true}', stderr=""),
        ]
        writer = BuzzMemoryWriter(runner=runner)

        writer.apply((MemoryUpdate("core", "Maxine core\n"),))

        self.assertEqual(
            runner.call_args_list[0].args[0], ["buzz", "mem", "ls", "--json"]
        )
        set_call = runner.call_args_list[1]
        self.assertEqual(
            set_call.args[0],
            ["buzz", "mem", "set", "core", "-"],
        )
        self.assertEqual(set_call.kwargs["input"], "Maxine core\n")
        self.assertNotIn("Maxine core", " ".join(set_call.args[0]))

    def test_patches_existing_core_with_exact_base_hash(self):
        old = "Maxine protects Joe's voice.\n"
        new = "Maxine protects Joe's voice and keeps mystery.\n"
        runner = Mock()
        runner.side_effect = [
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps([{"slug": "core"}]),
                stderr="",
            ),
            subprocess.CompletedProcess([], 0, stdout=old, stderr=""),
            subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
        ]
        writer = BuzzMemoryWriter(runner=runner)

        writer.apply((MemoryUpdate("core", new),))

        patch_call = runner.call_args_list[2]
        self.assertEqual(
            patch_call.args[0],
            [
                "buzz",
                "mem",
                "patch",
                "core",
                "--base-hash",
                hashlib.sha256(old.encode()).hexdigest(),
            ],
        )
        self.assertIn("-Maxine protects Joe's voice.", patch_call.kwargs["input"])
        self.assertIn(
            "+Maxine protects Joe's voice and keeps mystery.",
            patch_call.kwargs["input"],
        )


class CrmAcpAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_maxine_routes_reply_and_core_update_as_one_turn(self):
        config = AgentConfig.create("maxine", "Maxine")
        bus = Mock()
        bus.wait_reply = AsyncMock(
            return_value=CrmReply(
                "Keep the first line.",
                (MemoryUpdate("core", "Maxine protects Joe's voice.\n"),),
            )
        )
        publisher = Mock()
        memory = Mock()
        agent = CrmAcpAgent(
            config,
            bus,
            publisher,
            memory,
            reply_timeout=1,
        )
        created = await agent.new_session(
            {
                "cwd": "/tmp",
                "systemPrompt": "[Agent Memory — core]\nPortable memory.",
            }
        )
        notify = AsyncMock()

        result = await agent.prompt(
            {
                "sessionId": created["sessionId"],
                "prompt": [{"type": "text", "text": buzz_prompt()}],
            },
            notify,
        )

        memory.apply.assert_called_once_with(
            (MemoryUpdate("core", "Maxine protects Joe's voice.\n"),)
        )
        publisher.publish.assert_called_once_with(
            BuzzDestination(CHANNEL_ID, EVENT_ID),
            "Keep the first line.",
        )
        injected = bus.inject.call_args.args[2]
        self.assertIn("[Agent Memory — core]", injected)
        self.assertEqual(result, {"stopReason": "end_turn"})
        self.assertEqual(
            notify.await_args.args[0]["params"]["update"]["content"]["text"],
            "Keep the first line.",
        )


class AcpReplyScriptTest(unittest.TestCase):
    def test_helper_adds_core_without_exposing_it_in_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core_file = root / "state/maxine-core.md"
            core_file.parent.mkdir()
            core_file.write_text("Maxine keeps Joe beside the reader.\n")
            environment = {
                **os.environ,
                "CRM_ROOT": str(root),
                "CRM_AGENT_NAME": "maxine",
            }

            completed = subprocess.run(
                [
                    "bash",
                    "core/bus/send-acp-reply.sh",
                    "buzz-acp-maxine",
                    TURN_ID,
                    "The ending has the echo.",
                    str(core_file),
                ],
                text=True,
                capture_output=True,
                check=True,
                env=environment,
            )

            files = list((root / "inbox/buzz-acp-maxine").glob("*.json"))
            self.assertEqual(len(files), 1)
            envelope = json.loads(files[0].read_text())
            self.assertEqual(envelope["text"], "The ending has the echo.")
            self.assertEqual(
                envelope["buzz_memory_updates"],
                [
                    {
                        "slug": "core",
                        "value": "Maxine keeps Joe beside the reader.\n",
                    }
                ],
            )
            self.assertNotIn("Maxine keeps", " ".join(completed.args))

    def test_helper_rejects_core_files_outside_agent_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsafe = root / "untrusted.md"
            unsafe.write_text("Do not read arbitrary paths.\n")

            completed = subprocess.run(
                [
                    "bash",
                    "core/bus/send-acp-reply.sh",
                    "buzz-acp-maxine",
                    TURN_ID,
                    "Visible reply",
                    str(unsafe),
                ],
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "CRM_ROOT": str(root),
                    "CRM_AGENT_NAME": "maxine",
                },
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((root / "inbox/buzz-acp-maxine").exists())


if __name__ == "__main__":
    unittest.main()
