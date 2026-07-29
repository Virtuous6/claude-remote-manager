#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from integrations.crm_acp import (
    ClaudeModelCatalog,
    ClaudeModelOption,
    CrmAcpFactory,
    FactoryIdentity,
    parse_runtime,
)


PRIVATE_KEY_A = "1" * 64
PRIVATE_KEY_B = "2" * 64
TEST_CATALOG = ClaudeModelCatalog(
    "claude-fable-5[1m]",
    (
        ClaudeModelOption("default", "Default"),
        ClaudeModelOption("claude-fable-5[1m]", "Fable"),
        ClaudeModelOption("sonnet", "Sonnet"),
    ),
)


class FactoryIdentityTest(unittest.TestCase):
    def test_stable_secret_identity_does_not_expose_secret(self):
        first = FactoryIdentity.from_environment(
            {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Maxine",
            }
        )
        renamed = FactoryIdentity.from_environment(
            {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Maxine Bell",
            }
        )

        self.assertEqual(first.fingerprint, renamed.fingerprint)
        self.assertNotIn(PRIVATE_KEY_A, repr(first))
        self.assertNotIn(PRIVATE_KEY_A, json.dumps(first.public_record()))

    def test_distinct_managed_identities_are_isolated(self):
        first = FactoryIdentity.from_environment(
            {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Editor",
            }
        )
        second = FactoryIdentity.from_environment(
            {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_B,
                "BUZZ_ACP_SESSION_TITLE": "Editor",
            }
        )

        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertNotEqual(first.proposed_slug, second.proposed_slug)

    def test_invalid_secret_fails_without_echoing_it(self):
        secret = "definitely-not-a-private-key"

        with self.assertRaises(ValueError) as raised:
            FactoryIdentity.from_environment(
                {
                    "BUZZ_PRIVATE_KEY": secret,
                    "BUZZ_ACP_SESSION_TITLE": "Agent",
                }
            )

        self.assertNotIn(secret, str(raised.exception))


class CrmAcpFactoryTest(unittest.TestCase):
    def make_factory(self, root: Path, service: Mock | None = None) -> CrmAcpFactory:
        home = root / "home"
        documents = home / "Documents"
        repos = home / "repos"
        documents.mkdir(parents=True)
        repos.mkdir()
        return CrmAcpFactory(
            crm_root=root / "crm",
            template_root=Path.cwd(),
            home=home,
            service_runner=service or Mock(),
            model_catalog=lambda _environment: TEST_CATALOG,
        )

    def test_preview_has_model_without_provisioning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = self.make_factory(root)

            config = factory.resolve({})

            self.assertEqual(config.model_id, "claude-fable-5[1m]")
            self.assertEqual(len(config.model_options), 3)
            self.assertFalse(config.prompt_enabled)
            self.assertFalse((root / "crm/factory").exists())

    def test_managed_agent_without_private_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            factory = self.make_factory(Path(temporary))

            with self.assertRaisesRegex(ValueError, "managed identity"):
                factory.resolve({"BUZZ_MANAGED_AGENT": "desktop-instance"})

    def test_first_spawn_provisions_private_agent_and_starts_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock()
            factory = self.make_factory(root, service)
            environment = {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Maxine",
            }

            first = factory.resolve(environment)
            second = factory.resolve(environment)

            self.assertEqual(first, second)
            self.assertTrue(first.prompt_enabled)
            self.assertTrue(first.agent_name.startswith("buzz-maxine-"))
            self.assertEqual(first.model_id, "claude-fable-5[1m]")
            self.assertEqual(service.call_count, 1)

            identities = list((root / "crm/factory/identities").glob("*.json"))
            self.assertEqual(len(identities), 1)
            record = json.loads(identities[0].read_text())
            agent_dir = Path(record["agent_dir"])
            config = json.loads((agent_dir / "config.json").read_text())

            self.assertEqual(config["claude_session_id"], record["session_id"])
            self.assertEqual(config["model"], "claude-fable-5[1m]")
            self.assertEqual(config["startup_delay"], 5)
            self.assertFalse(config["telegram_enabled"])
            self.assertEqual(config["crons"], [])
            self.assertNotIn(PRIVATE_KEY_A, identities[0].read_text())
            self.assertNotIn(PRIVATE_KEY_A, (agent_dir / "CLAUDE.md").read_text())
            instructions = (agent_dir / "CLAUDE.md").read_text()
            self.assertIn("Buzz Workflow", instructions)
            self.assertIn("--workflow", instructions)
            self.assertIn("UTC", instructions)
            self.assertEqual(agent_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((agent_dir / "config.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((agent_dir / "CLAUDE.md").stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (agent_dir / ".claude/settings.json").stat().st_mode & 0o777,
                0o600,
            )
            called = service.call_args.args
            self.assertEqual(called[0], first.agent_name)
            self.assertEqual(called[1], agent_dir)
            self.assertNotIn(PRIVATE_KEY_A, repr(service.call_args))

    def test_first_spawn_uses_model_selected_in_buzz(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock()
            factory = self.make_factory(root, service)

            created = factory.resolve(
                {
                    "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                    "BUZZ_ACP_SESSION_TITLE": "Maxine V2",
                    "BUZZ_ACP_MODEL": "opus[1m]",
                }
            )

            config = json.loads(
                (
                    root
                    / "crm/factory/agents"
                    / created.agent_name
                    / "config.json"
                ).read_text()
            )
            self.assertEqual(created.model_id, "opus[1m]")
            self.assertEqual(config["model"], "opus[1m]")
            service.assert_called_once()

    def test_buzz_model_edit_persists_and_reloads_existing_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock()
            factory = self.make_factory(root, service)
            environment = {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Writer",
            }
            created = factory.resolve(environment)
            agent_dir = root / "crm/factory/agents" / created.agent_name
            before = json.loads((agent_dir / "config.json").read_text())

            updated = factory.resolve(
                {
                    **environment,
                    "BUZZ_ACP_MODEL": "sonnet",
                }
            )

            after = json.loads((agent_dir / "config.json").read_text())
            self.assertEqual(updated.model_id, "sonnet")
            self.assertEqual(after["model"], "sonnet")
            self.assertEqual(after["claude_session_id"], before["claude_session_id"])
            self.assertEqual(service.call_count, 2)

    def test_unsafe_buzz_model_is_rejected_before_service_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock()
            factory = self.make_factory(root, service)

            with self.assertRaisesRegex(ValueError, "model"):
                factory.resolve(
                    {
                        "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                        "BUZZ_ACP_SESSION_TITLE": "Writer",
                        "BUZZ_ACP_MODEL": "sonnet; touch /tmp/x",
                    }
                )

            service.assert_not_called()

    def test_rename_reuses_existing_slug_and_updates_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock()
            factory = self.make_factory(root, service)
            first = factory.resolve(
                {
                    "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                    "BUZZ_ACP_SESSION_TITLE": "Maxine",
                }
            )
            renamed = factory.resolve(
                {
                    "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                    "BUZZ_ACP_SESSION_TITLE": "Maxine Bell",
                }
            )

            self.assertEqual(first.agent_name, renamed.agent_name)
            self.assertEqual(renamed.title, "Maxine Bell")
            self.assertEqual(service.call_count, 1)

    def test_concurrent_spawn_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock()
            factory = self.make_factory(root, service)
            environment = {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Concurrent",
            }

            with ThreadPoolExecutor(max_workers=4) as pool:
                configs = list(pool.map(lambda _: factory.resolve(environment), range(4)))

            self.assertTrue(all(config == configs[0] for config in configs))
            self.assertEqual(service.call_count, 1)
            self.assertEqual(
                len(list((root / "crm/factory/identities").glob("*.json"))),
                1,
            )

    def test_default_workspaces_and_sessions_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = self.make_factory(root)
            first = factory.resolve(
                {
                    "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                    "BUZZ_ACP_SESSION_TITLE": "Editor",
                }
            )
            second = factory.resolve(
                {
                    "BUZZ_PRIVATE_KEY": PRIVATE_KEY_B,
                    "BUZZ_ACP_SESSION_TITLE": "Editor",
                }
            )

            first_config = json.loads(
                (
                    root
                    / "crm/factory/agents"
                    / first.agent_name
                    / "config.json"
                ).read_text()
            )
            second_config = json.loads(
                (
                    root
                    / "crm/factory/agents"
                    / second.agent_name
                    / "config.json"
                ).read_text()
            )
            self.assertNotEqual(
                first_config["working_directory"],
                second_config["working_directory"],
            )
            self.assertNotEqual(
                first_config["claude_session_id"],
                second_config["claude_session_id"],
            )

    def test_session_rollover_in_agent_config_updates_factory_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = self.make_factory(root)
            environment = {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Rollover",
            }
            config = factory.resolve(environment)
            agent_config_path = (
                root
                / "crm/factory/agents"
                / config.agent_name
                / "config.json"
            )
            agent_config = json.loads(agent_config_path.read_text())
            rotated = "87654321-4321-4321-8321-cba987654321"
            agent_config["claude_session_id"] = rotated
            agent_config_path.write_text(json.dumps(agent_config))

            factory.resolve(environment)

            record_path = next((root / "crm/factory/identities").glob("*.json"))
            record = json.loads(record_path.read_text())
            self.assertEqual(record["session_id"], rotated)
            self.assertEqual(
                json.loads(agent_config_path.read_text())["claude_session_id"],
                rotated,
            )

    def test_selected_model_survives_factory_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = self.make_factory(root)
            environment = {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Writer",
            }
            created = factory.resolve(environment)
            config_path = (
                root
                / "crm/factory/agents"
                / created.agent_name
                / "config.json"
            )
            agent_config = json.loads(config_path.read_text())
            agent_config["model"] = "sonnet"
            config_path.write_text(json.dumps(agent_config))

            restarted = factory.resolve(environment)

            self.assertEqual(restarted.model_id, "sonnet")
            self.assertEqual(json.loads(config_path.read_text())["model"], "sonnet")

    def test_custom_workspace_must_be_absolute_existing_and_approved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = self.make_factory(root)
            approved = root / "home/Documents/1000months"
            approved.mkdir()

            config = factory.resolve(
                {
                    "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                    "BUZZ_ACP_SESSION_TITLE": "Maxine",
                    "CRM_WORKSPACE": str(approved),
                }
            )
            agent_config = json.loads(
                (
                    root
                    / "crm/factory/agents"
                    / config.agent_name
                    / "config.json"
                ).read_text()
            )
            self.assertEqual(agent_config["working_directory"], str(approved.resolve()))

            for unsafe in ("relative/path", str(root / "outside")):
                with self.subTest(unsafe=unsafe):
                    with self.assertRaisesRegex(ValueError, "workspace"):
                        factory.resolve(
                            {
                                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_B,
                                "BUZZ_ACP_SESSION_TITLE": "Unsafe",
                                "CRM_WORKSPACE": unsafe,
                            }
                        )

    def test_existing_agent_can_switch_workspace_and_starts_fresh_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock()
            factory = self.make_factory(root, service)
            environment = {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Maxine V2",
            }
            created = factory.resolve(environment)
            agent_dir = root / "crm/factory/agents" / created.agent_name
            first = json.loads((agent_dir / "config.json").read_text())
            writing_corner = root / "home/Documents/1000months"
            writing_corner.mkdir()

            updated = factory.resolve(
                {
                    **environment,
                    "CRM_WORKSPACE": str(writing_corner),
                }
            )

            config = json.loads((agent_dir / "config.json").read_text())
            record = json.loads(
                next((root / "crm/factory/identities").glob("*.json")).read_text()
            )
            self.assertEqual(updated.agent_name, created.agent_name)
            self.assertEqual(
                config["working_directory"],
                str(writing_corner.resolve()),
            )
            self.assertNotEqual(config["claude_session_id"], first["claude_session_id"])
            self.assertEqual(record["session_id"], config["claude_session_id"])
            self.assertEqual(record["workspace"], config["working_directory"])
            self.assertEqual(service.call_count, 2)

    def test_repeated_workspace_value_does_not_restart_or_rotate_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock()
            factory = self.make_factory(root, service)
            writing_corner = root / "home/Documents/1000months"
            writing_corner.mkdir()
            environment = {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Maxine V2",
                "CRM_WORKSPACE": str(writing_corner),
            }

            created = factory.resolve(environment)
            agent_dir = root / "crm/factory/agents" / created.agent_name
            first = json.loads((agent_dir / "config.json").read_text())
            factory.resolve(environment)
            second = json.loads((agent_dir / "config.json").read_text())

            self.assertEqual(second["claude_session_id"], first["claude_session_id"])
            service.assert_called_once()

    def test_invalid_workspace_edit_preserves_existing_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock()
            factory = self.make_factory(root, service)
            environment = {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Writer",
            }
            created = factory.resolve(environment)
            agent_dir = root / "crm/factory/agents" / created.agent_name
            config_path = agent_dir / "config.json"
            record_path = next((root / "crm/factory/identities").glob("*.json"))
            original_config = config_path.read_text()
            original_record = record_path.read_text()

            with self.assertRaisesRegex(ValueError, "workspace"):
                factory.resolve(
                    {
                        **environment,
                        "CRM_WORKSPACE": str(root / "outside"),
                    }
                )

            self.assertEqual(config_path.read_text(), original_config)
            self.assertEqual(record_path.read_text(), original_record)
            service.assert_called_once()

    def test_workspace_reload_failure_rolls_back_record_and_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Mock(side_effect=[None, RuntimeError("reload failed")])
            factory = self.make_factory(root, service)
            environment = {
                "BUZZ_PRIVATE_KEY": PRIVATE_KEY_A,
                "BUZZ_ACP_SESSION_TITLE": "Writer",
            }
            created = factory.resolve(environment)
            agent_dir = root / "crm/factory/agents" / created.agent_name
            config_path = agent_dir / "config.json"
            record_path = next((root / "crm/factory/identities").glob("*.json"))
            original_config = config_path.read_text()
            original_record = record_path.read_text()
            writing_corner = root / "home/Documents/1000months"
            writing_corner.mkdir()

            with self.assertRaisesRegex(RuntimeError, "reload"):
                factory.resolve(
                    {
                        **environment,
                        "CRM_WORKSPACE": str(writing_corner),
                    }
                )

            self.assertEqual(config_path.read_text(), original_config)
            self.assertEqual(record_path.read_text(), original_record)


class FactoryCliAndArtifactTest(unittest.TestCase):
    def test_factory_cli_resolves_preview(self):
        runtime = parse_runtime(
            ["--factory"],
            environment={"CRM_CLAUDE_MODEL_DISCOVERY": "disabled"},
        )

        self.assertEqual(runtime.model_id, "default")
        self.assertFalse(runtime.prompt_enabled)

    def test_factory_cli_rejects_unsafe_instance_before_writing(self):
        with self.assertRaisesRegex(ValueError, "instance"):
            parse_runtime(
                ["--factory"],
                environment={"CRM_INSTANCE_ID": "../../escape"},
            )

    def test_factory_process_reports_model_before_agent_creation(self):
        requests = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 2},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": "/tmp"},
                }
            )
            + "\n"
        )
        environment = dict(os.environ)
        environment.pop("BUZZ_PRIVATE_KEY", None)
        environment.pop("BUZZ_AUTH_TAG", None)
        environment.pop("BUZZ_MANAGED_AGENT", None)
        environment["CRM_BUZZ_RELAY_PUBKEY"] = "8" * 64
        environment["CRM_CLAUDE_MODEL_DISCOVERY"] = "disabled"

        completed = subprocess.run(
            [sys.executable, "integrations/crm_acp.py", "--factory"],
            input=requests,
            text=True,
            capture_output=True,
            check=True,
            env=environment,
        )
        responses = {
            response["id"]: response
            for response in map(json.loads, completed.stdout.splitlines())
        }

        self.assertEqual(responses[1]["result"]["info"]["name"], "crm-acp-crm-factory-preview")
        model = responses[2]["result"]["configOptions"][0]
        self.assertEqual(model["currentValue"], "default")

    def test_generic_harness_has_no_fixed_agent(self):
        harness = json.loads(Path("integrations/harnesses/crm-acp.json").read_text())

        self.assertEqual(harness["id"], "crm-acp")
        self.assertEqual(harness["label"], "CRM ACP (Auto-Provision)")
        self.assertIn("CRM_WORKSPACE", harness["installHint"])
        self.assertTrue(harness["docsUrl"].endswith("/docs/CRM_ACP.md"))
        self.assertNotIn("name", harness)
        self.assertEqual(harness["args"][-1], "--factory")
        self.assertNotIn("--agent", harness["args"])
        self.assertEqual(harness["env"]["CRM_INSTANCE_ID"], "default")
        self.assertRegex(
            harness["env"]["CRM_BUZZ_RELAY_PUBKEY"],
            r"^[0-9a-f]{64}$",
        )
        serialized = json.dumps(harness).lower()
        self.assertNotIn("private_key", serialized)
        self.assertNotIn("auth_tag", serialized)

    def test_runtime_scripts_support_external_agent_dir_and_exact_session(self):
        wrapper = Path("core/scripts/agent-wrapper.sh").read_text()
        launchd = Path("core/scripts/generate-launchd.sh").read_text()
        restart = Path("core/bus/self-restart.sh").read_text()
        hard_restart = Path("core/bus/hard-restart.sh").read_text()
        fast_checker = Path("core/scripts/fast-checker.sh").read_text()

        self.assertIn('AGENT_DIR="${3:-${TEMPLATE_ROOT}/agents/${AGENT}}"', wrapper)
        self.assertIn('CLAUDE_SESSION_ID=$(jq -r', wrapper)
        self.assertIn("ARGS=(--session-id", wrapper)
        self.assertIn("ARGS=(--resume", wrapper)
        self.assertIn("<agent_dir>", launchd)
        self.assertIn("<string>${AGENT_DIR}</string>", launchd)
        self.assertIn("<key>CRM_AGENT_DIR</key>", launchd)
        self.assertIn('AGENT_DIR="${CRM_AGENT_DIR:-', restart)
        self.assertIn("ARGS=(--resume", restart)
        self.assertIn('--quiet)', restart)
        self.assertIn('RESTART_NOTIFY=""', restart)
        self.assertIn('AGENT_DIR="${CRM_AGENT_DIR:-', hard_restart)
        self.assertIn("claude_session_id", hard_restart)
        self.assertIn("bash '${BUS_DIR}/hard-restart.sh'", fast_checker)

    def test_hard_restart_rotates_factory_session_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            template = root / "template"
            crm_root = root / "crm"
            agent = "buzz-rollover-test"
            agent_dir = crm_root / "factory/agents" / agent
            fake_bin = root / "bin"
            plist_dir = home / "Library/LaunchAgents"
            for directory in (template, agent_dir, fake_bin, plist_dir):
                directory.mkdir(parents=True)
            (template / ".env").write_text("CRM_INSTANCE_ID=test\n")
            config_path = agent_dir / "config.json"
            original = "12345678-1234-4234-8234-123456789abc"
            config_path.write_text(json.dumps({"claude_session_id": original}))
            (plist_dir / f"com.claude-remote.test.{agent}.plist").write_text("test")
            nohup = fake_bin / "nohup"
            nohup.write_text("#!/bin/sh\nexit 0\n")
            nohup.chmod(0o755)

            completed = subprocess.run(
                ["bash", "core/bus/hard-restart.sh", "--reason", "test"],
                text=True,
                capture_output=True,
                check=True,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "CRM_TEMPLATE_ROOT": str(template),
                    "CRM_AGENT_NAME": agent,
                    "CRM_AGENT_DIR": str(agent_dir),
                    "CRM_ROOT": str(crm_root),
                },
            )

            rotated = json.loads(config_path.read_text())["claude_session_id"]
            self.assertNotEqual(rotated, original)
            self.assertRegex(
                rotated,
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}$",
            )
            self.assertNotIn(original, completed.stdout)


if __name__ == "__main__":
    unittest.main()
