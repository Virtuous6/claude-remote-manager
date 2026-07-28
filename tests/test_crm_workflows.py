#!/usr/bin/env python3

import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from integrations.crm_acp import (
    AgentConfig,
    BuzzDestination,
    BuzzTurnAuthorizer,
    BuzzWorkflowManager,
    BuzzWorkflowOperation,
    CrmReply,
    _nostr_public_key,
    _verify_nip_oa_tag,
)


CHANNEL_ID = "312d60a4-0198-4ef0-a13e-9d0e121f1833"
WORKFLOW_ID = "a93c02b0-5f55-41f7-aea1-01e955c49afd"
TURN_ID = "acp-" + ("b" * 32)
OWNER_PUBKEY = (
    "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
)
SIBLING_PUBKEY = (
    "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
)
RELAY_PUBKEY = (
    "8bcc2fb4a106d00d12e15ded255d3b13b13d367705e30d076d8a02e270c8f9be"
)
SPEC_CONDITIONS = "kind=1&created_at<1713957000"
SPEC_SIGNATURE = (
    "8b7df2575caf0a108374f8471722b233c53f9ff827a8b0f91861966c3b9dd5c"
    "b2e189eae9f49d72187674c2f5bd244145e10ff86c9f257ffe65a1ee5f108b369"
)


def event_prompt(author: str, tags: list[list[str]], content: str = "Do work.") -> str:
    return (
        "[Context]\n"
        "Scope: channel\n"
        f"Channel: Testing (#{CHANNEL_ID})\n"
        "[Event]\n"
        f"From: Sender (npub: npub1test, hex: {author})\n"
        f"Content: {content}\n"
        f"Tags: {json.dumps(tags)}"
    )


class BuzzTurnAuthorizerTest(unittest.TestCase):
    def test_derives_nostr_xonly_public_key(self):
        self.assertEqual(_nostr_public_key("1".zfill(64)), OWNER_PUBKEY)
        self.assertEqual(_nostr_public_key("2".zfill(64)), SIBLING_PUBKEY)

    def test_verifies_official_nip_oa_vector(self):
        tag = ["auth", OWNER_PUBKEY, SPEC_CONDITIONS, SPEC_SIGNATURE]

        self.assertEqual(_verify_nip_oa_tag(tag, SIBLING_PUBKEY), OWNER_PUBKEY)

        changed = list(tag)
        changed[2] = "kind=9"
        with self.assertRaisesRegex(ValueError, "auth"):
            _verify_nip_oa_tag(changed, SIBLING_PUBKEY)

    def test_accepts_owner_verified_sibling_and_own_relay_workflow_only(self):
        target = "f" * 64
        authorizer = BuzzTurnAuthorizer(
            owner_pubkey=OWNER_PUBKEY,
            agent_pubkey=target,
            relay_pubkey=RELAY_PUBKEY,
            respond_to="allowlist",
            respond_to_allowlist={RELAY_PUBKEY},
        )
        owner_turn = authorizer.authorize(event_prompt(OWNER_PUBKEY, []))
        sibling_turn = authorizer.authorize(
            event_prompt(
                SIBLING_PUBKEY,
                [["auth", OWNER_PUBKEY, SPEC_CONDITIONS, SPEC_SIGNATURE]],
            )
        )
        workflow_turn = authorizer.authorize(
            event_prompt(
                RELAY_PUBKEY,
                [
                    ["p", target],
                    ["h", CHANNEL_ID],
                    ["buzz:workflow", "true"],
                ],
                "@Target\n[CRM scheduled task: daily]\nReview.",
            )
        )

        self.assertTrue(owner_turn.owner)
        self.assertEqual(sibling_turn.source, "sibling")
        self.assertEqual(workflow_turn.source, "workflow")

        with self.assertRaisesRegex(PermissionError, "Buzz turn"):
            authorizer.authorize(event_prompt("e" * 64, []))
        with self.assertRaisesRegex(PermissionError, "workflow"):
            authorizer.authorize(
                event_prompt(
                    RELAY_PUBKEY,
                    [
                        ["p", "e" * 64],
                        ["h", CHANNEL_ID],
                        ["buzz:workflow", "true"],
                    ],
                )
            )

    def test_honors_buzz_owner_anyone_and_allowlist_policies(self):
        external = "e" * 64
        owner_only = BuzzTurnAuthorizer(
            owner_pubkey=OWNER_PUBKEY,
            agent_pubkey="f" * 64,
            relay_pubkey=RELAY_PUBKEY,
        )
        anyone = BuzzTurnAuthorizer(
            owner_pubkey=OWNER_PUBKEY,
            agent_pubkey="f" * 64,
            relay_pubkey=RELAY_PUBKEY,
            respond_to="anyone",
        )
        allowlist = BuzzTurnAuthorizer(
            owner_pubkey=OWNER_PUBKEY,
            agent_pubkey="f" * 64,
            relay_pubkey=RELAY_PUBKEY,
            respond_to="allowlist",
            respond_to_allowlist={external, RELAY_PUBKEY},
        )

        with self.assertRaisesRegex(PermissionError, "policy"):
            owner_only.authorize(event_prompt(external, []))
        self.assertEqual(anyone.authorize(event_prompt(external, [])).source, "external")
        self.assertEqual(
            allowlist.authorize(event_prompt(external, [])).source,
            "allowlist",
        )
        with self.assertRaisesRegex(PermissionError, "policy"):
            allowlist.authorize(event_prompt("d" * 64, []))

    def test_workflows_require_anyone_or_relay_allowlist(self):
        target = "f" * 64
        prompt = event_prompt(
            RELAY_PUBKEY,
            [["p", target], ["buzz:workflow", "true"]],
        )
        owner_only = BuzzTurnAuthorizer(
            owner_pubkey=OWNER_PUBKEY,
            agent_pubkey=target,
            relay_pubkey=RELAY_PUBKEY,
        )
        wrong_allowlist = BuzzTurnAuthorizer(
            owner_pubkey=OWNER_PUBKEY,
            agent_pubkey=target,
            relay_pubkey=RELAY_PUBKEY,
            respond_to="allowlist",
            respond_to_allowlist={"e" * 64},
        )
        anyone = BuzzTurnAuthorizer(
            owner_pubkey=OWNER_PUBKEY,
            agent_pubkey=target,
            relay_pubkey=RELAY_PUBKEY,
            respond_to="anyone",
        )

        with self.assertRaisesRegex(PermissionError, "policy"):
            owner_only.authorize(prompt)
        with self.assertRaisesRegex(PermissionError, "policy"):
            wrong_allowlist.authorize(prompt)
        self.assertEqual(anyone.authorize(prompt).source, "workflow")

    def test_reads_buzz_managed_response_policy_environment(self):
        environment = {
            "BUZZ_PRIVATE_KEY": "2".zfill(64),
            "BUZZ_AUTH_TAG": json.dumps(
                ["auth", OWNER_PUBKEY, SPEC_CONDITIONS, SPEC_SIGNATURE]
            ),
            "CRM_BUZZ_RELAY_PUBKEY": RELAY_PUBKEY,
            "BUZZ_ACP_RESPOND_TO": "allowlist",
            "BUZZ_ACP_RESPOND_TO_ALLOWLIST": f"{RELAY_PUBKEY},{'e' * 64}",
        }

        authorizer = BuzzTurnAuthorizer.from_environment(environment)

        self.assertEqual(authorizer.respond_to, "allowlist")
        self.assertEqual(
            authorizer.respond_to_allowlist,
            frozenset({RELAY_PUBKEY, "e" * 64}),
        )

    def test_only_owner_can_mutate_workflows(self):
        authorizer = BuzzTurnAuthorizer(
            owner_pubkey=OWNER_PUBKEY,
            agent_pubkey="f" * 64,
            relay_pubkey=RELAY_PUBKEY,
        )
        sibling = authorizer.authorize(
            event_prompt(
                SIBLING_PUBKEY,
                [["auth", OWNER_PUBKEY, SPEC_CONDITIONS, SPEC_SIGNATURE]],
            )
        )

        with self.assertRaisesRegex(PermissionError, "owner"):
            authorizer.require_owner(sibling)


class BuzzWorkflowOperationTest(unittest.TestCase):
    def test_parses_typed_utc_upsert(self):
        operation = BuzzWorkflowOperation.from_payload(
            {
                "action": "upsert",
                "name": "weekday-writing",
                "cron": "0 15 * * 1-5",
                "timezone": "UTC",
                "task": "Review the writing corner and post the next useful move.",
            }
        )

        self.assertEqual(operation.name, "weekday-writing")
        self.assertEqual(operation.cron, "0 15 * * 1-5")
        self.assertIsNone(operation.interval)

    def test_rejects_raw_yaml_mentions_and_non_utc_calendar_schedules(self):
        invalid = (
            {
                "action": "upsert",
                "name": "weekday-writing",
                "yaml": "steps: []",
                "cron": "0 15 * * 1-5",
                "timezone": "UTC",
                "task": "Write.",
            },
            {
                "action": "upsert",
                "name": "weekday-writing",
                "cron": "0 15 * * 1-5",
                "timezone": "America/Denver",
                "task": "Write.",
            },
            {
                "action": "upsert",
                "name": "weekday-writing",
                "interval": "30m",
                "task": "Wake @Another Agent.",
            },
            {
                "action": "delete",
                "name": "../someone-else",
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "workflow"):
                    BuzzWorkflowOperation.from_payload(payload)

    def test_rejects_short_intervals_and_unknown_actions(self):
        for payload in (
            {
                "action": "upsert",
                "name": "fast",
                "interval": "30s",
                "task": "Check.",
            },
            {"action": "shell", "name": "unsafe"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "workflow"):
                    BuzzWorkflowOperation.from_payload(payload)


class BuzzWorkflowManagerTest(unittest.TestCase):
    def make_manager(
        self,
        root: Path,
        runner: Mock,
        *,
        authorized: bool = True,
    ) -> BuzzWorkflowManager:
        return BuzzWorkflowManager(
            AgentConfig.create("maxine", "Maxine"),
            root,
            runner=runner,
            authorization_material="test-managed-auth" if authorized else None,
        )

    def test_upsert_creates_channel_bound_mention_workflow_and_private_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = Mock(
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"workflow_id": WORKFLOW_ID}),
                    stderr="",
                )
            )
            manager = self.make_manager(root, runner)
            operation = BuzzWorkflowOperation.from_payload(
                {
                    "action": "upsert",
                    "name": "weekday-writing",
                    "cron": "0 15 * * 1-5",
                    "timezone": "UTC",
                    "task": "Review the writing corner.",
                }
            )

            status = manager.apply(
                operation,
                BuzzDestination(CHANNEL_ID),
            )

            call = runner.call_args
            self.assertEqual(
                call.args[0],
                [
                    "buzz",
                    "workflows",
                    "create",
                    "--channel",
                    CHANNEL_ID,
                    "--yaml",
                    "-",
                ],
            )
            definition = json.loads(call.kwargs["input"])
            self.assertEqual(
                definition["trigger"],
                {"on": "schedule", "cron": "0 15 * * 1-5"},
            )
            self.assertEqual(definition["steps"][0]["action"], "send_message")
            self.assertTrue(
                definition["steps"][0]["text"].startswith("@Maxine\n")
            )
            self.assertNotIn("BUZZ_PRIVATE_KEY", call.kwargs["input"])
            self.assertEqual(
                status,
                "Buzz schedule created: weekday-writing (0 15 * * 1-5 UTC).",
            )

            registry_path = root / "state/maxine-buzz-workflows.json"
            registry = json.loads(registry_path.read_text())
            self.assertEqual(
                registry["schedules"]["weekday-writing"]["workflow_id"],
                WORKFLOW_ID,
            )
            self.assertEqual(registry_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("auth", registry_path.read_text().lower())
            self.assertNotIn("private_key", registry_path.read_text().lower())

    def test_repeated_upsert_updates_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = Mock(
                side_effect=[
                    subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps({"workflow_id": WORKFLOW_ID}),
                        stderr="",
                    ),
                    subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
                ]
            )
            manager = self.make_manager(root, runner)
            first = BuzzWorkflowOperation.from_payload(
                {
                    "action": "upsert",
                    "name": "daily-draft",
                    "interval": "1h",
                    "task": "Review drafts.",
                }
            )
            changed = BuzzWorkflowOperation.from_payload(
                {
                    "action": "upsert",
                    "name": "daily-draft",
                    "interval": "2h",
                    "task": "Review drafts and record the strongest line.",
                }
            )

            manager.apply(first, BuzzDestination(CHANNEL_ID))
            status = manager.apply(changed, BuzzDestination(CHANNEL_ID))

            self.assertEqual(runner.call_count, 2)
            self.assertEqual(
                runner.call_args.args[0],
                [
                    "buzz",
                    "workflows",
                    "update",
                    "--channel",
                    CHANNEL_ID,
                    "--workflow",
                    WORKFLOW_ID,
                    "--yaml",
                    "-",
                ],
            )
            definition = json.loads(runner.call_args.kwargs["input"])
            self.assertEqual(definition["trigger"]["interval"], "2h")
            self.assertEqual(status, "Buzz schedule updated: daily-draft (2h).")

    def test_pause_resume_run_delete_and_list_use_recorded_workflow_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = Mock(
                side_effect=[
                    subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps({"workflow_id": WORKFLOW_ID}),
                        stderr="",
                    ),
                    subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
                ]
            )
            manager = self.make_manager(root, runner)
            destination = BuzzDestination(CHANNEL_ID)
            manager.apply(
                BuzzWorkflowOperation.from_payload(
                    {
                        "action": "upsert",
                        "name": "weekly-review",
                        "interval": "7d",
                        "task": "Review the week.",
                    }
                ),
                destination,
            )

            paused = manager.apply(
                BuzzWorkflowOperation.from_payload(
                    {"action": "pause", "name": "weekly-review"}
                ),
                destination,
            )
            listed = manager.apply(
                BuzzWorkflowOperation.from_payload({"action": "list"}),
                destination,
            )
            resumed = manager.apply(
                BuzzWorkflowOperation.from_payload(
                    {"action": "resume", "name": "weekly-review"}
                ),
                destination,
            )
            ran = manager.apply(
                BuzzWorkflowOperation.from_payload(
                    {"action": "run_now", "name": "weekly-review"}
                ),
                destination,
            )
            deleted = manager.apply(
                BuzzWorkflowOperation.from_payload(
                    {"action": "delete", "name": "weekly-review"}
                ),
                destination,
            )

            self.assertEqual(paused, "Buzz schedule paused: weekly-review.")
            self.assertIn("weekly-review: paused, every 7d", listed)
            self.assertEqual(resumed, "Buzz schedule resumed: weekly-review.")
            self.assertEqual(ran, "Buzz schedule triggered: weekly-review.")
            self.assertEqual(deleted, "Buzz schedule deleted: weekly-review.")
            self.assertEqual(
                runner.call_args_list[-2].args[0],
                ["buzz", "workflows", "trigger", "--workflow", WORKFLOW_ID],
            )
            self.assertEqual(
                runner.call_args_list[-1].args[0],
                ["buzz", "workflows", "delete", "--workflow", WORKFLOW_ID],
            )

    def test_mutation_requires_managed_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = Mock()
            manager = self.make_manager(
                Path(temporary),
                runner,
                authorized=False,
            )

            with self.assertRaisesRegex(PermissionError, "authorization"):
                manager.apply(
                    BuzzWorkflowOperation.from_payload(
                        {
                            "action": "upsert",
                            "name": "daily",
                            "interval": "1h",
                            "task": "Review.",
                        }
                    ),
                    BuzzDestination(CHANNEL_ID),
                )

            runner.assert_not_called()

    def test_tampered_registry_fails_before_buzz_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = Mock(
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"workflow_id": WORKFLOW_ID}),
                    stderr="",
                )
            )
            manager = self.make_manager(root, runner)
            manager.apply(
                BuzzWorkflowOperation.from_payload(
                    {
                        "action": "upsert",
                        "name": "daily",
                        "interval": "1h",
                        "task": "Review.",
                    }
                ),
                BuzzDestination(CHANNEL_ID),
            )
            registry_path = root / "state/maxine-buzz-workflows.json"
            registry = json.loads(registry_path.read_text())
            registry["schedules"]["daily"]["workflow_id"] = (
                "ffffffff-ffff-4fff-afff-ffffffffffff"
            )
            registry_path.write_text(json.dumps(registry))
            runner.reset_mock()

            with self.assertRaisesRegex(ValueError, "integrity"):
                manager.apply(
                    BuzzWorkflowOperation.from_payload(
                        {"action": "delete", "name": "daily"}
                    ),
                    BuzzDestination(CHANNEL_ID),
                )

            runner.assert_not_called()

    def test_public_identity_fingerprint_cannot_forge_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_identity = "1" * 64
            runner = Mock(
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"workflow_id": WORKFLOW_ID}),
                    stderr="",
                )
            )
            manager = BuzzWorkflowManager(
                AgentConfig.create("maxine", "Maxine"),
                root,
                runner=runner,
                authorization_material=private_identity,
            )
            manager.apply(
                BuzzWorkflowOperation.from_payload(
                    {
                        "action": "upsert",
                        "name": "daily",
                        "interval": "1h",
                        "task": "Review.",
                    }
                ),
                BuzzDestination(CHANNEL_ID),
            )
            registry_path = root / "state/maxine-buzz-workflows.json"
            registry = json.loads(registry_path.read_text())
            registry["schedules"]["daily"]["task"] = "Changed locally."
            unsigned = {
                key: value for key, value in registry.items() if key != "integrity"
            }
            public_fingerprint = hashlib.sha256(
                private_identity.encode("utf-8")
            ).digest()
            registry["integrity"] = hmac.new(
                public_fingerprint,
                json.dumps(
                    unsigned,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            registry_path.write_text(json.dumps(registry))
            runner.reset_mock()

            with self.assertRaisesRegex(ValueError, "integrity"):
                manager.apply(
                    BuzzWorkflowOperation.from_payload({"action": "list"}),
                    BuzzDestination(CHANNEL_ID),
                )

            runner.assert_not_called()

    def test_title_reconciliation_updates_scheduled_mention(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = Mock(
                side_effect=[
                    subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps({"workflow_id": WORKFLOW_ID}),
                        stderr="",
                    ),
                    subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
                ]
            )
            manager = self.make_manager(root, runner)
            manager.apply(
                BuzzWorkflowOperation.from_payload(
                    {
                        "action": "upsert",
                        "name": "daily",
                        "interval": "1h",
                        "task": "Review.",
                    }
                ),
                BuzzDestination(CHANNEL_ID),
            )
            renamed = BuzzWorkflowManager(
                AgentConfig.create("maxine", "Maxine Bell"),
                root,
                runner=runner,
                authorization_material="test-managed-auth",
            )

            renamed.reconcile_title()

            definition = json.loads(runner.call_args.kwargs["input"])
            self.assertTrue(
                definition["steps"][0]["text"].startswith("@Maxine Bell\n")
            )


class WorkflowReplyEnvelopeTest(unittest.TestCase):
    def test_reply_parses_one_typed_workflow_operation(self):
        reply = CrmReply.from_message(
            {
                "text": "I will schedule the review.",
                "buzz_workflow_operation": {
                    "action": "upsert",
                    "name": "daily-review",
                    "interval": "1h",
                    "task": "Review the project.",
                },
            }
        )

        self.assertEqual(reply.workflow_operation.name, "daily-review")

    def test_helper_reads_only_isolated_operation_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operation_file = root / "state/maxine-workflow-op.json"
            operation_file.parent.mkdir()
            operation_file.write_text(
                json.dumps(
                    {
                        "action": "pause",
                        "name": "daily-review",
                    }
                )
            )
            environment = {
                **os.environ,
                "CRM_ROOT": str(root),
                "CRM_AGENT_NAME": "maxine",
            }

            subprocess.run(
                [
                    "bash",
                    "core/bus/send-acp-reply.sh",
                    "buzz-acp-maxine",
                    TURN_ID,
                    "Paused.",
                    "--workflow",
                    str(operation_file),
                ],
                text=True,
                capture_output=True,
                check=True,
                env=environment,
            )

            envelope_file = next((root / "inbox/buzz-acp-maxine").glob("*.json"))
            envelope = json.loads(envelope_file.read_text())
            self.assertEqual(
                envelope["buzz_workflow_operation"],
                {"action": "pause", "name": "daily-review"},
            )
            self.assertFalse(operation_file.exists())


if __name__ == "__main__":
    unittest.main()
