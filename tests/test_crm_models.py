#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from integrations.crm_acp import (
    AgentConfig,
    ClaudeModelCatalog,
    ClaudeModelOption,
    CrmAcpAgent,
    CrmModelManager,
    _reload_factory_model,
)


CATALOG_RESPONSE = {
    "stable": {
        "configOptions": [
            {
                "id": "model",
                "category": "model",
                "currentValue": "claude-fable-5[1m]",
                "options": [
                    {
                        "value": "default",
                        "name": "Default (recommended)",
                        "description": "Claude Code default",
                    },
                    {
                        "value": "opus[1m]",
                        "name": "Opus (1M context)",
                        "description": "Deep work",
                    },
                    {
                        "value": "claude-fable-5[1m]",
                        "name": "Fable",
                        "description": "Longest tasks",
                    },
                    {
                        "value": "sonnet",
                        "name": "Sonnet",
                        "description": "Routine tasks",
                    },
                    {
                        "value": "haiku",
                        "name": "Haiku",
                        "description": "Fast answers",
                    },
                ],
            }
        ]
    }
}


def sample_catalog() -> ClaudeModelCatalog:
    return ClaudeModelCatalog.from_buzz_models(CATALOG_RESPONSE)


class ClaudeModelCatalogTest(unittest.TestCase):
    def test_discovers_exact_claude_catalog_without_forwarding_buzz_secrets(self):
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(CATALOG_RESPONSE),
                stderr="",
            )
        )

        catalog = ClaudeModelCatalog.discover(
            buzz_acp=Path("/tools/buzz-acp"),
            claude_acp=Path("/tools/claude-agent-acp"),
            runner=runner,
            environment={
                "HOME": "/Users/test",
                "PATH": "/usr/bin",
                "BUZZ_PRIVATE_KEY": "secret-private-key",
                "BUZZ_AUTH_TAG": "secret-auth-tag",
            },
        )

        self.assertEqual(catalog.current, "claude-fable-5[1m]")
        self.assertEqual(
            [option.value for option in catalog.options],
            ["default", "opus[1m]", "claude-fable-5[1m]", "sonnet", "haiku"],
        )
        call = runner.call_args
        self.assertEqual(
            call.args[0],
            ["/tools/buzz-acp", "models", "--json"],
        )
        child_env = call.kwargs["env"]
        self.assertEqual(
            child_env["BUZZ_ACP_AGENT_COMMAND"],
            "/tools/claude-agent-acp",
        )
        self.assertNotIn("BUZZ_PRIVATE_KEY", child_env)
        self.assertNotIn("BUZZ_AUTH_TAG", child_env)

    def test_rejects_malformed_or_unsafe_catalog_values(self):
        unsafe = json.loads(json.dumps(CATALOG_RESPONSE))
        unsafe["stable"]["configOptions"][0]["options"][0]["value"] = "sonnet; touch /tmp/x"

        with self.assertRaisesRegex(ValueError, "model"):
            ClaudeModelCatalog.from_buzz_models(unsafe)
        with self.assertRaisesRegex(ValueError, "model"):
            ClaudeModelCatalog.from_buzz_models({"stable": {"configOptions": []}})

    def test_agent_config_exposes_live_options_and_custom_models(self):
        catalog = sample_catalog()
        config = AgentConfig.create(
            "maxine-v2",
            "Maxine V2",
            model_id=catalog.current,
            model_label=catalog.label_for(catalog.current),
            model_options=catalog.options,
            allow_custom_model=True,
        )

        model = config.model_config()[0]

        self.assertEqual(model["currentValue"], "claude-fable-5[1m]")
        self.assertEqual(
            [option["value"] for option in model["options"]],
            ["default", "opus[1m]", "claude-fable-5[1m]", "sonnet", "haiku"],
        )


class CrmModelManagerTest(unittest.TestCase):
    def test_persists_valid_model_privately_and_schedules_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(json.dumps({"agent_name": "maxine-v2"}))
            os.chmod(config_path, 0o600)
            reload_model = Mock()
            manager = CrmModelManager(config_path, reload_model=reload_model)

            manager.apply("opus[1m]")

            self.assertEqual(json.loads(config_path.read_text())["model"], "opus[1m]")
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            reload_model.assert_called_once_with("opus[1m]")

    def test_reload_failure_rolls_back_persisted_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(json.dumps({"model": "sonnet"}))
            manager = CrmModelManager(
                config_path,
                reload_model=Mock(side_effect=RuntimeError("restart failed")),
            )

            with self.assertRaisesRegex(RuntimeError, "restart"):
                manager.apply("opus[1m]")

            self.assertEqual(json.loads(config_path.read_text())["model"], "sonnet")

    def test_rejects_shell_input_symlinks_and_non_object_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config_path.write_text("[]")
            manager = CrmModelManager(config_path, reload_model=Mock())
            with self.assertRaisesRegex(ValueError, "config"):
                manager.apply("sonnet")

            config_path.write_text("{}")
            with self.assertRaisesRegex(ValueError, "model"):
                manager.apply("sonnet; rm -rf /")

            target = root / "target.json"
            target.write_text("{}")
            config_path.unlink()
            config_path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "config"):
                manager.apply("sonnet")

    def test_live_tmux_model_change_uses_quiet_restart_without_buzz_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "factory/agents/writer/config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("{}")
            runner = Mock(
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                ]
            )
            config = AgentConfig.create(
                "writer",
                "Writer",
                model_id="sonnet",
                model_label="Sonnet",
                model_options=(ClaudeModelOption("sonnet", "Sonnet"),),
                allow_custom_model=True,
            )

            _reload_factory_model(
                config,
                config_path,
                "opus[1m]",
                crm_root=root,
                template_root=Path("/runtime"),
                runner=runner,
                environment={
                    "HOME": "/Users/test",
                    "PATH": "/usr/bin",
                    "BUZZ_PRIVATE_KEY": "secret",
                    "BUZZ_AUTH_TAG": "secret",
                },
            )

            restart = runner.call_args_list[1]
            self.assertEqual(restart.args[0][-1], "--quiet")
            self.assertNotIn("BUZZ_PRIVATE_KEY", restart.kwargs["env"])
            self.assertNotIn("BUZZ_AUTH_TAG", restart.kwargs["env"])

    def test_initial_launch_does_not_restart_before_tmux_exists(self):
        runner = Mock(
            return_value=subprocess.CompletedProcess([], 1, "", ""),
        )
        config = AgentConfig.create(
            "writer",
            "Writer",
            model_id="sonnet",
            model_label="Sonnet",
            model_options=(ClaudeModelOption("sonnet", "Sonnet"),),
            allow_custom_model=True,
        )

        _reload_factory_model(
            config,
            Path("/crm/factory/agents/writer/config.json"),
            "opus[1m]",
            crm_root=Path("/crm"),
            template_root=Path("/runtime"),
            runner=runner,
            environment={"HOME": "/Users/test", "PATH": "/usr/bin"},
        )

        runner.assert_called_once()


class DynamicModelSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_buzz_selection_persists_and_updates_session_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text("{}")
            manager = CrmModelManager(config_path, reload_model=Mock())
            catalog = sample_catalog()
            config = AgentConfig.create(
                "maxine-v2",
                "Maxine V2",
                model_id=catalog.current,
                model_label=catalog.label_for(catalog.current),
                model_options=catalog.options,
                allow_custom_model=True,
            )
            agent = CrmAcpAgent(
                config,
                Mock(),
                Mock(),
                Mock(),
                model_manager=manager,
            )
            created = await agent.new_session({"cwd": "/tmp"})

            selected = await agent.set_config_option(
                {
                    "sessionId": created["sessionId"],
                    "configId": "model",
                    "value": "opus[1m]",
                }
            )

            self.assertEqual(
                selected["configOptions"][0]["currentValue"],
                "opus[1m]",
            )
            self.assertEqual(json.loads(config_path.read_text())["model"], "opus[1m]")

    async def test_custom_model_is_validated_before_persistence(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text("{}")
            manager = CrmModelManager(config_path, reload_model=Mock())
            catalog = sample_catalog()
            config = AgentConfig.create(
                "writer",
                "Writer",
                model_id=catalog.current,
                model_label=catalog.label_for(catalog.current),
                model_options=catalog.options,
                allow_custom_model=True,
            )
            agent = CrmAcpAgent(
                config,
                Mock(),
                Mock(),
                Mock(),
                model_manager=manager,
            )
            created = await agent.new_session({"cwd": "/tmp"})

            with self.assertRaisesRegex(ValueError, "model"):
                await agent.set_config_option(
                    {
                        "sessionId": created["sessionId"],
                        "configId": "model",
                        "value": "../../bad model",
                    }
                )

            selected = await agent.set_config_option(
                {
                    "sessionId": created["sessionId"],
                    "configId": "model",
                    "value": "claude-custom-20260729",
                }
            )

            model = selected["configOptions"][0]
            self.assertEqual(model["currentValue"], "claude-custom-20260729")
            self.assertIn(
                "claude-custom-20260729",
                [option["value"] for option in model["options"]],
            )
            self.assertEqual(
                json.loads(config_path.read_text())["model"],
                "claude-custom-20260729",
            )


if __name__ == "__main__":
    unittest.main()
