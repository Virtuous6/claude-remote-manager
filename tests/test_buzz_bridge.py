#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from integrations.buzz_bridge import (
    ALLOWED_AUTHORS,
    BridgeState,
    BuzzBridge,
    JOE_DM_CHANNEL,
    OWNER_PUBKEY,
    Policy,
    STEVE_PUBKEY,
    accepts_event,
    attachment_specs,
    build_reply_arguments,
    crm_message,
    health_snapshot,
    load_policy,
    parse_buzz_messages,
    presence_for_health,
    render_context,
    safe_attachment_path,
    send_proactive,
    validate_outbound_file,
)


class BuzzBridgeTest(unittest.TestCase):
    def make_bridge(self, temporary):
        root = Path(temporary)
        identity = root / "identity.json"
        identity.write_text(
            json.dumps({"private_key": "secret", "public_key": STEVE_PUBKEY})
        )
        policy = root / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "owner_pubkey": OWNER_PUBKEY,
                    "allowed_authors": [OWNER_PUBKEY],
                    "joe_dm_channel": JOE_DM_CHANNEL,
                    "allowed_upload_roots": [temporary],
                    "max_outbound_attempts": 2,
                }
            )
        )
        os.chmod(policy, 0o600)
        return BuzzBridge(
            identity,
            root / "crm",
            root / "state.json",
            policy_path=policy,
        )

    def test_parse_buzz_messages_filters_self_and_old_events(self):
        raw = json.dumps(
            [
                {
                    "id": "new-event",
                    "pubkey": "human",
                    "content": "@Steve review this",
                    "created_at": 20,
                    "channel_id": "channel-1",
                },
                {
                    "id": "self-event",
                    "pubkey": "steve",
                    "content": "reply",
                    "created_at": 21,
                    "channel_id": "channel-1",
                },
                {
                    "id": "old-event",
                    "pubkey": "human",
                    "content": "old",
                    "created_at": 9,
                    "channel_id": "channel-1",
                },
            ]
        )

        messages = parse_buzz_messages(raw, "steve", since=10)

        self.assertEqual([message["id"] for message in messages], ["new-event"])

    def test_crm_message_preserves_reply_route(self):
        message = crm_message(
            {
                "id": "event-1",
                "pubkey": "human",
                "content": "hello",
                "created_at": 20,
                "channel_id": "channel-1",
            },
            crm_id="buzz-event-1",
        )

        self.assertEqual(message["from"], "buzz")
        self.assertEqual(message["to"], "steve-kingsley")
        self.assertIn("channel-1", message["text"])
        self.assertIn("event-1", message["text"])
        self.assertIn("hello", message["text"])

    def test_crm_message_uses_name_context_attachments_and_owner_priority(self):
        message = crm_message(
            {
                "id": "event-1",
                "pubkey": OWNER_PUBKEY,
                "content": "review",
                "created_at": 20,
                "channel_id": JOE_DM_CHANNEL,
            },
            crm_id="buzz-event-1",
            display_name="JLUCKY",
            context="JLUCKY: earlier message",
            attachments=["/safe/inbox/report.pdf"],
        )

        self.assertEqual(message["priority"], "urgent")
        self.assertIn("from JLUCKY", message["text"])
        self.assertIn("Recent context", message["text"])
        self.assertIn("/safe/inbox/report.pdf", message["text"])

    def test_state_round_trip_and_event_dedup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            state = BridgeState(path)
            state.data["since"] = 0
            state.mark_event("event-1", 42)
            state.map_crm_reply("crm-1", "channel-1", "event-1")
            state.save()

            restored = BridgeState(path)
            self.assertTrue(restored.seen_event("event-1"))
            self.assertEqual(
                restored.reply_route("crm-1"),
                {"channel_id": "channel-1", "event_id": "event-1"},
            )
            self.assertEqual(restored.since, 41)

    def test_outbound_failures_reach_dead_letter_threshold(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = BridgeState(Path(temporary) / "state.json")
            self.assertFalse(state.record_outbound_failure("reply.json", "relay down", 2))
            self.assertTrue(state.record_outbound_failure("reply.json", "relay down", 2))
            self.assertEqual(state.outbound_attempts("reply.json"), 2)

    def test_proactive_send_is_restricted_to_joe_dm(self):
        calls = []

        def fake_buzz(*arguments, stdin=None):
            calls.append((arguments, stdin))
            return '{"accepted":true}'

        send_proactive(fake_buzz, "Daily brief ready")

        self.assertEqual(
            calls,
            [
                (
                    (
                        "messages",
                        "send",
                        "--channel",
                        JOE_DM_CHANNEL,
                        "--content",
                        "-",
                    ),
                    "Daily brief ready",
                )
            ],
        )

    def test_proactive_send_rejects_empty_message(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            send_proactive(lambda *args, **kwargs: "", "   ")

    def test_owner_dm_does_not_require_mention(self):
        event = {"pubkey": OWNER_PUBKEY, "tags": [], "content": "Morning"}
        self.assertTrue(accepts_event(event, JOE_DM_CHANNEL))

    def test_channel_requires_allowed_author_and_steve_mention(self):
        allowed_agent = next(pubkey for pubkey in ALLOWED_AUTHORS if pubkey != OWNER_PUBKEY)
        tagged = {
            "pubkey": allowed_agent,
            "tags": [["p", STEVE_PUBKEY]],
            "content": "Please review",
        }
        untagged = {**tagged, "tags": []}
        stranger = {**tagged, "pubkey": "stranger"}

        self.assertTrue(accepts_event(tagged, "channel-1"))
        self.assertFalse(accepts_event(untagged, "channel-1"))
        self.assertFalse(accepts_event(stranger, "channel-1"))

    def test_dm_reply_is_linear_but_channel_reply_is_threaded(self):
        self.assertEqual(
            build_reply_arguments(JOE_DM_CHANNEL, "event-1"),
            (
                "messages",
                "send",
                "--channel",
                JOE_DM_CHANNEL,
                "--content",
                "-",
            ),
        )
        self.assertEqual(
            build_reply_arguments("channel-1", "event-1"),
            (
                "messages",
                "send",
                "--channel",
                "channel-1",
                "--reply-to",
                "event-1",
                "--content",
                "-",
            ),
        )

    def test_recent_context_is_bounded_and_excludes_current_event(self):
        context = render_context(
            [
                {"id": "one", "pubkey": "a", "content": "first"},
                {"id": "two", "pubkey": "b", "content": "second"},
                {"id": "current", "pubkey": "c", "content": "current"},
            ],
            current_event_id="current",
            names={"a": "Alice", "b": "Bob"},
            limit=2,
        )
        self.assertEqual(context, "Alice: first\nBob: second")

    def test_policy_requires_private_file_and_valid_pubkeys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "owner_pubkey": OWNER_PUBKEY,
                        "allowed_authors": [OWNER_PUBKEY],
                        "joe_dm_channel": JOE_DM_CHANNEL,
                        "allowed_upload_roots": [temporary],
                    }
                )
            )
            os.chmod(path, 0o600)
            policy = load_policy(path)
            self.assertEqual(policy.owner_pubkey, OWNER_PUBKEY)

            os.chmod(path, 0o644)
            with self.assertRaisesRegex(PermissionError, "0600"):
                load_policy(path)

    def test_attachment_tag_parsing_and_safe_path(self):
        event = {
            "tags": [
                [
                    "imeta",
                    "url https://buzz.neustac.com/media/abc.pdf",
                    "m application/pdf",
                    "name ../report.pdf",
                    "size 120",
                ]
            ]
        }
        specs = attachment_specs(event)
        self.assertEqual(specs[0]["mime"], "application/pdf")
        self.assertEqual(specs[0]["name"], "../report.pdf")
        self.assertEqual(
            safe_attachment_path(Path("/safe/inbox"), specs[0]["name"]),
            Path("/safe/inbox/report.pdf"),
        )

    def test_outbound_file_must_be_in_allowed_root_and_under_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "allowed"
            root.mkdir()
            allowed = root / "report.jpg"
            allowed.write_text("safe")
            outside = Path(temporary) / "outside.txt"
            outside.write_text("no")
            policy = Policy(
                owner_pubkey=OWNER_PUBKEY,
                allowed_authors={OWNER_PUBKEY},
                joe_dm_channel=JOE_DM_CHANNEL,
                allowed_upload_roots=(root,),
                max_attachment_bytes=10,
            )

            self.assertEqual(validate_outbound_file(allowed, policy), allowed.resolve())
            with self.assertRaisesRegex(PermissionError, "approved"):
                validate_outbound_file(outside, policy)

    def test_health_and_presence_reflect_tmux_state_and_queues(self):
        health = health_snapshot(
            relay_ok=True,
            tmux_ok=False,
            claude_ok=False,
            inbound_queue=2,
            outbound_queue=1,
            dead_letters=0,
            now=100,
        )
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(presence_for_health(health), "away")
        self.assertEqual(presence_for_health({**health, "relay_ok": False}), "offline")

    def test_profile_cache_avoids_repeat_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = BridgeState(Path(temporary) / "state.json")
            lookup = Mock(return_value="JLUCKY")
            self.assertEqual(state.profile_name(OWNER_PUBKEY, lookup), "JLUCKY")
            self.assertEqual(state.profile_name(OWNER_PUBKEY, lookup), "JLUCKY")
            lookup.assert_called_once_with(OWNER_PUBKEY)

    def test_bridge_channels_profiles_and_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            responses = {
                ("channels", "list", "--member"): json.dumps(
                    [{"channel_id": "channel-1"}, {"id": "channel-2"}, {}]
                ),
                ("users", "get", "--pubkey", OWNER_PUBKEY): json.dumps(
                    [{"display_name": "JLUCKY"}]
                ),
                (
                    "messages",
                    "get",
                    "--channel",
                    JOE_DM_CHANNEL,
                    "--limit",
                    "12",
                ): json.dumps(
                    [
                        {
                            "id": "old",
                            "pubkey": OWNER_PUBKEY,
                            "content": "Earlier",
                        },
                        {
                            "id": "current",
                            "pubkey": OWNER_PUBKEY,
                            "content": "Now",
                        },
                    ]
                ),
            }
            bridge.buzz = Mock(side_effect=lambda *args, **kwargs: responses[args])

            self.assertEqual(bridge.member_channels(), ["channel-1", "channel-2"])
            self.assertEqual(bridge.lookup_profile(OWNER_PUBKEY), "JLUCKY")
            self.assertEqual(
                bridge.recent_context(JOE_DM_CHANNEL, "current"), "JLUCKY: Earlier"
            )

    def test_bridge_injects_event_atomically_with_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.lookup_profile = Mock(return_value="JLUCKY")
            bridge.recent_context = Mock(return_value="JLUCKY: earlier")
            bridge.download_attachments = Mock(return_value=[])
            event = {
                "id": "event-1",
                "pubkey": OWNER_PUBKEY,
                "content": "Now",
                "created_at": 20,
                "channel_id": JOE_DM_CHANNEL,
            }

            bridge.inject_event(event)
            bridge.inject_event(event)

            files = list((bridge.crm_root / "inbox/steve-kingsley").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(
                bridge.state.reply_route("buzz-event-1"),
                {"channel_id": JOE_DM_CHANNEL, "event_id": "event-1"},
            )

    def test_forward_reply_success_and_dead_letter(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            inbox = bridge.crm_root / "inbox/buzz"
            inbox.mkdir(parents=True)
            bridge.state.map_crm_reply("crm-1", "channel-1", "event-1")
            bridge.state.save()

            first = inbox / "first.json"
            first.write_text(json.dumps({"reply_to": "crm-1", "text": "ok"}))
            bridge.buzz = Mock(return_value='{"accepted":true}')
            bridge.forward_replies()
            self.assertTrue((bridge.crm_root / "processed/buzz/first.json").exists())

            second = inbox / "second.json"
            second.write_text(json.dumps({"reply_to": "crm-1", "text": "fail"}))
            error = subprocess.CalledProcessError(3, ["buzz"], stderr="denied")
            bridge.buzz = Mock(side_effect=error)
            bridge.forward_replies()
            bridge.forward_replies()
            self.assertTrue((bridge.crm_root / "dead-letter/buzz/second.json").exists())

    def test_download_attachment_and_reject_oversize_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.policy = Policy(
                **{
                    **bridge.policy.__dict__,
                    "max_attachment_bytes": 5,
                }
            )
            event = {
                "id": "event-1",
                "tags": [
                    [
                        "imeta",
                        "url https://buzz.neustac.com/media/a.txt",
                        "m text/plain",
                        "name a.txt",
                        "size 4",
                    ]
                ],
            }

            def fake_buzz(*arguments, **_kwargs):
                Path(arguments[-1]).write_text("safe")
                return ""

            bridge.buzz = fake_buzz
            paths = bridge.download_attachments(event)
            self.assertEqual(Path(paths[0]).read_text(), "safe")

            def oversized(*arguments, **_kwargs):
                Path(arguments[-1]).write_text("too-large")
                return ""

            bridge.buzz = oversized
            with self.assertRaisesRegex(ValueError, "exceeds"):
                bridge.download_attachments({**event, "id": "event-2"})

    def test_health_write_presence_and_tmux_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.tmux_health = Mock(return_value=(True, True))
            bridge.buzz = Mock(return_value='{"accepted":true}')
            health = bridge.write_health(relay_ok=True)
            self.assertEqual(health["status"], "healthy")
            self.assertTrue((bridge.config_dir / "health.json").exists())

            bridge.refresh_presence(health)
            bridge.refresh_presence(health)
            bridge.buzz.assert_called_once()

            bridge.tmux_health = BuzzBridge.tmux_health.__get__(bridge)
            with patch("integrations.buzz_bridge.subprocess.run") as run:
                run.return_value.returncode = 1
                self.assertEqual(bridge.tmux_health(), (False, False))

    def test_poll_filters_unauthorized_and_injects_authorized(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.state.data["since"] = 0
            bridge.member_channels = Mock(return_value=[JOE_DM_CHANNEL])
            bridge.forward_replies = Mock()
            bridge.write_health = Mock(return_value={
                "relay_ok": True,
                "tmux_ok": True,
                "claude_ok": True,
            })
            bridge.refresh_presence = Mock()
            bridge.inject_event = Mock()
            events = [
                {
                    "id": "allowed",
                    "pubkey": OWNER_PUBKEY,
                    "content": "yes",
                    "created_at": 10,
                    "channel_id": JOE_DM_CHANNEL,
                    "tags": [],
                },
                {
                    "id": "denied",
                    "pubkey": "f" * 64,
                    "content": "no",
                    "created_at": 11,
                    "channel_id": JOE_DM_CHANNEL,
                    "tags": [],
                },
            ]
            bridge.buzz = Mock(return_value=json.dumps(events))

            bridge.poll()

            bridge.inject_event.assert_called_once()
            self.assertTrue(bridge.state.seen_event("denied"))


if __name__ == "__main__":
    unittest.main()
