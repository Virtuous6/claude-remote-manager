#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from integrations import read_buzz
from integrations.buzz_bridge import (
    ALLOWED_AUTHORS,
    BridgeState,
    BuzzBridge,
    JOE_DM_CHANNEL,
    OWNER_PUBKEY,
    Policy,
    SubscriptionRule,
    STEVE_PUBKEY,
    accepts_event,
    attachment_specs,
    batch_events_by_channel,
    build_reply_arguments,
    crm_message,
    crm_message_batch,
    health_snapshot,
    load_policy,
    parse_buzz_messages,
    presence_for_health,
    owner_control,
    read_command,
    render_context,
    safe_attachment_path,
    send_proactive,
    stable_pane_digest,
    thread_references,
    matches_subscription,
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

    def test_owner_controls_are_owner_only_and_not_forwarded(self):
        cancel = {
            "id": "cancel",
            "pubkey": OWNER_PUBKEY,
            "kind": 9,
            "content": "!cancel",
            "channel_id": "channel-1",
            "tags": [["p", STEVE_PUBKEY]],
        }
        rotate = {**cancel, "id": "rotate", "content": "!rotate"}
        stranger = {**cancel, "pubkey": "f" * 64}

        self.assertEqual(owner_control(cancel), "cancel")
        self.assertEqual(owner_control(rotate), "rotate")
        self.assertEqual(
            owner_control({**cancel, "content": "!replace use the second file"}),
            "replace",
        )
        self.assertIsNone(owner_control(stranger))
        self.assertIsNone(owner_control({**cancel, "content": "!shutdown"}))

    def test_events_batch_per_channel_in_timestamp_order(self):
        events = [
            {"id": "b", "channel_id": "two", "created_at": 3},
            {"id": "c", "channel_id": "one", "created_at": 2},
            {"id": "a", "channel_id": "one", "created_at": 1},
        ]

        batches = batch_events_by_channel(events)

        self.assertEqual([[event["id"] for event in batch] for batch in batches], [["a", "c"], ["b"]])

    def test_batch_message_routes_reply_to_latest_event(self):
        events = [
            {
                "id": "one",
                "pubkey": OWNER_PUBKEY,
                "content": "first",
                "created_at": 10,
                "channel_id": JOE_DM_CHANNEL,
            },
            {
                "id": "two",
                "pubkey": OWNER_PUBKEY,
                "content": "second",
                "created_at": 11,
                "channel_id": JOE_DM_CHANNEL,
            },
        ]

        message = crm_message_batch(events, "buzz-batch-two", "JLUCKY")

        self.assertIn("first", message["text"])
        self.assertIn("second", message["text"])
        self.assertIn("[event:two]", message["text"])

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

    def test_state_tracks_independent_channel_cursors(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = BridgeState(Path(temporary) / "state.json")
            state.data["since"] = 5
            state.data["startup_cursor"] = 5

            state.mark_observed("one", "event-1", 20)

            self.assertEqual(state.cursor_for("one"), 19)
            self.assertEqual(state.cursor_for("two"), 5)

    def test_pending_queue_is_bounded_and_fair(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = BridgeState(Path(temporary) / "state.json")
            dropped = state.queue_event(
                {"id": "a", "channel_id": "one", "created_at": 1, "pubkey": "x"},
                max_pending=1,
            )
            self.assertIsNone(dropped)
            dropped = state.queue_event(
                {"id": "b", "channel_id": "one", "created_at": 3, "pubkey": "x"},
                max_pending=1,
            )
            state.queue_event(
                {"id": "c", "channel_id": "two", "created_at": 2, "pubkey": "x"},
                max_pending=2,
            )

            self.assertEqual(dropped["id"], "a")
            self.assertEqual(
                state.take_next_batch(max_events=10, owner_pubkey=OWNER_PUBKEY)["channel_id"],
                "two",
            )

    def test_busy_queue_only_dispatches_owner_steering(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = BridgeState(Path(temporary) / "state.json")
            state.queue_event(
                {"id": "agent", "channel_id": "one", "created_at": 1, "pubkey": "a"},
                max_pending=10,
            )
            state.queue_event(
                {
                    "id": "owner",
                    "channel_id": "two",
                    "created_at": 2,
                    "pubkey": OWNER_PUBKEY,
                },
                max_pending=10,
            )

            batch = state.take_next_batch(
                max_events=10,
                owner_pubkey=OWNER_PUBKEY,
                owner_only=True,
            )

            self.assertEqual([event["id"] for event in batch["events"]], ["owner"])
            self.assertEqual(state.pending_count(), 1)

    def test_transient_inbound_failure_requeues_then_dead_letters(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = BridgeState(Path(temporary) / "state.json")
            event = {
                "id": "bad",
                "channel_id": "one",
                "created_at": 1,
                "pubkey": OWNER_PUBKEY,
            }

            dead = state.record_inbound_failure([event], "relay down", 2, now=10)
            self.assertEqual(dead, [])
            self.assertEqual(state.pending_count(), 1)
            dead = state.record_inbound_failure([event], "relay down", 2, now=20)

            self.assertEqual([item["event"]["id"] for item in dead], ["bad"])
            self.assertEqual(state.pending_count(), 0)

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
        event = {"pubkey": OWNER_PUBKEY, "kind": 9, "tags": [], "content": "Morning"}
        self.assertTrue(accepts_event(event, JOE_DM_CHANNEL))

    def test_channel_requires_allowed_author_and_steve_mention(self):
        allowed_agent = next(pubkey for pubkey in ALLOWED_AUTHORS if pubkey != OWNER_PUBKEY)
        tagged = {
            "pubkey": allowed_agent,
            "kind": 9,
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

    def test_thread_references_prefer_marked_root_and_current_parent(self):
        event = {
            "id": "c" * 64,
            "tags": [
                ["e", "a" * 64, "", "root"],
                ["e", "b" * 64, "", "reply"],
            ],
        }

        self.assertEqual(
            thread_references(event),
            {"root_event_id": "a" * 64, "parent_event_id": "c" * 64},
        )

    def test_subscription_rules_fail_closed_by_kind_and_channel(self):
        rules = (
            SubscriptionRule(
                channels={"channel-1"},
                kinds={9, 45001},
                require_mention=True,
            ),
        )
        event = {
            "kind": 9,
            "tags": [["p", STEVE_PUBKEY]],
        }

        self.assertTrue(matches_subscription(event, "channel-1", rules))
        self.assertFalse(
            matches_subscription({**event, "kind": 99999}, "channel-1", rules)
        )
        self.assertFalse(matches_subscription(event, "channel-2", rules))
        self.assertFalse(matches_subscription({**event, "tags": []}, "channel-1", rules))
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

    def test_policy_rejects_unsafe_queue_and_timeout_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "owner_pubkey": OWNER_PUBKEY,
                        "allowed_authors": [OWNER_PUBKEY],
                        "joe_dm_channel": JOE_DM_CHANNEL,
                        "allowed_upload_roots": [temporary],
                        "max_batch_events": 0,
                        "max_turn_seconds": 5,
                    }
                )
            )
            os.chmod(path, 0o600)

            with self.assertRaisesRegex(ValueError, "policy bounds"):
                load_policy(path)

    def test_policy_loads_explicit_forum_subscription(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "owner_pubkey": OWNER_PUBKEY,
                        "allowed_authors": [OWNER_PUBKEY],
                        "joe_dm_channel": JOE_DM_CHANNEL,
                        "allowed_upload_roots": [temporary],
                        "subscription_rules": [
                            {
                                "channels": ["forum-channel"],
                                "kinds": [45001, 45003],
                                "require_mention": False,
                            }
                        ],
                    }
                )
            )
            os.chmod(path, 0o600)

            policy = load_policy(path)

            self.assertTrue(
                matches_subscription(
                    {"kind": 45001, "tags": []},
                    "forum-channel",
                    policy.subscription_rules,
                )
            )
            self.assertFalse(
                matches_subscription(
                    {"kind": 9, "tags": []},
                    "forum-channel",
                    policy.subscription_rules,
                )
            )

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
            oldest_inbound_age=12,
            last_delivery_at=90,
            last_error="",
            now=100,
        )
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(presence_for_health(health), "away")
        self.assertEqual(presence_for_health({**health, "relay_ok": False}), "offline")
        self.assertEqual(health["oldest_inbound_age"], 12)
        self.assertEqual(health["last_delivery_at"], 90)

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

    def test_channel_context_reads_only_the_triggering_thread(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            root = "a" * 64
            current = "c" * 64
            bridge.lookup_profile = Mock(return_value="JLUCKY")
            bridge.buzz = Mock(
                return_value=json.dumps(
                    [
                        {
                            "id": root,
                            "pubkey": OWNER_PUBKEY,
                            "content": "Root",
                        },
                        {
                            "id": current,
                            "pubkey": OWNER_PUBKEY,
                            "content": "Current",
                        },
                    ]
                )
            )

            context = bridge.conversation_context(
                {
                    "id": current,
                    "channel_id": "channel-1",
                    "tags": [["e", root, "", "root"]],
                }
            )

            self.assertEqual(context, "JLUCKY: Root")
            bridge.buzz.assert_called_once_with(
                "messages",
                "thread",
                "--channel",
                "channel-1",
                "--event",
                root,
                "--limit",
                "20",
                "--depth-limit",
                "20",
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
            bridge.alert_dead_letter = Mock()
            bridge.forward_replies()
            bridge.forward_replies()
            self.assertTrue((bridge.crm_root / "dead-letter/buzz/second.json").exists())
            bridge.alert_dead_letter.assert_called_once()

    def test_owner_cancel_and_rotate_actions(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.cancel_current_turn = Mock()
            bridge.rotate_session = Mock()
            bridge.send_control_confirmation = Mock()

            cancel = {
                "id": "cancel",
                "pubkey": OWNER_PUBKEY,
                "kind": 9,
                "content": "!cancel",
                "created_at": 10,
                "channel_id": JOE_DM_CHANNEL,
                "tags": [],
            }
            rotate = {**cancel, "id": "rotate", "content": "!rotate", "created_at": 11}

            self.assertTrue(bridge.handle_control(cancel))
            self.assertTrue(bridge.handle_control(rotate))
            bridge.cancel_current_turn.assert_called_once()
            bridge.rotate_session.assert_called_once()
            self.assertTrue(bridge.state.seen_event("cancel"))
            self.assertTrue(bridge.state.seen_event("rotate"))

    def test_owner_replace_cancels_and_queues_superseding_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.cancel_current_turn = Mock()
            bridge.state.data["active_turn"] = {"events": []}
            event = {
                "id": "replace",
                "pubkey": OWNER_PUBKEY,
                "kind": 9,
                "content": "!replace use the second file",
                "created_at": 12,
                "channel_id": JOE_DM_CHANNEL,
                "tags": [],
            }

            self.assertTrue(bridge.handle_control(event))

            bridge.cancel_current_turn.assert_called_once()
            batch = bridge.state.take_next_batch(
                max_events=10,
                owner_pubkey=OWNER_PUBKEY,
            )
            self.assertEqual(batch["events"][0]["content"], "use the second file")
            self.assertTrue(batch["events"][0]["_supersede"])

    def test_dispatch_queues_agents_but_steers_owner_when_busy(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.agent_busy = Mock(return_value=True)
            bridge.inject_events = Mock()
            bridge.state.queue_event(
                {
                    "id": "agent",
                    "channel_id": "one",
                    "created_at": 1,
                    "pubkey": "a",
                },
                max_pending=10,
            )
            bridge.state.queue_event(
                {
                    "id": "owner",
                    "channel_id": "two",
                    "created_at": 2,
                    "pubkey": OWNER_PUBKEY,
                },
                max_pending=10,
            )

            bridge.dispatch_pending()

            bridge.inject_events.assert_called_once()
            self.assertEqual(bridge.inject_events.call_args.args[0][0]["id"], "owner")
            self.assertTrue(bridge.inject_events.call_args.kwargs["steering"])
            self.assertEqual(bridge.state.pending_count(), 1)

    def test_permanent_inbound_failure_is_quarantined_and_alerted(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.agent_busy = Mock(return_value=False)
            bridge.alert_inbound_dead_letter = Mock()
            bridge.inject_events = Mock(side_effect=ValueError("bad attachment"))
            event = {
                "id": "bad",
                "channel_id": "one",
                "created_at": 1,
                "pubkey": OWNER_PUBKEY,
            }
            bridge.state.queue_event(event, max_pending=10)

            bridge.dispatch_pending()

            self.assertEqual(bridge.state.pending_count(), 0)
            bridge.alert_inbound_dead_letter.assert_called_once()
            self.assertTrue(
                (bridge.crm_root / "dead-letter/buzz-inbound/bad.json").exists()
            )

    def test_stalled_turn_alerts_once_then_requeues_at_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.policy = Policy(
                **{
                    **bridge.policy.__dict__,
                    "stall_alert_seconds": 10,
                    "max_turn_seconds": 20,
                }
            )
            event = {
                "id": "work",
                "channel_id": "one",
                "created_at": 1,
                "pubkey": OWNER_PUBKEY,
            }
            bridge.state.data["active_turn"] = {
                "channel_id": "one",
                "crm_ids": ["crm-work"],
                "events": [event],
                "started_at": 100,
                "last_activity_at": 100,
                "pane_hash": "same",
                "stall_alerted": False,
            }
            bridge.agent_busy = Mock(return_value=True)
            bridge.pane_digest = Mock(return_value="same")
            bridge.alert_stalled_turn = Mock()
            bridge.cancel_current_turn = Mock()

            bridge.monitor_turn(now=111)
            bridge.monitor_turn(now=112)
            bridge.monitor_turn(now=121)

            bridge.alert_stalled_turn.assert_called_once()
            bridge.cancel_current_turn.assert_called_once()
            self.assertEqual(bridge.state.pending_count(), 1)
            self.assertIsNone(bridge.state.data["active_turn"])

    def test_absolute_turn_deadline_applies_despite_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.policy = Policy(
                **{**bridge.policy.__dict__, "max_turn_seconds": 20}
            )
            event = {
                "id": "work",
                "channel_id": "one",
                "created_at": 1,
                "pubkey": OWNER_PUBKEY,
            }
            bridge.state.data["active_turn"] = {
                "channel_id": "one",
                "crm_ids": ["crm-work"],
                "events": [event],
                "started_at": 100,
                "last_activity_at": 119,
                "pane_hash": "old",
                "stall_alerted": False,
            }
            bridge.agent_busy = Mock(return_value=True)
            bridge.pane_digest = Mock(return_value="new-progress")
            bridge.cancel_current_turn = Mock()

            bridge.monitor_turn(now=121)

            bridge.cancel_current_turn.assert_called_once()
            self.assertEqual(bridge.state.pending_count(), 1)

    def test_pane_digest_ignores_spinner_time_but_tracks_real_output(self):
        first = "Read file.py\n✶ Working… (10s · 2k tokens)\n❯\nbypass permissions on"
        later = "Read file.py\n✻ Working… (11s · 2.1k tokens)\n❯\nbypass permissions on"
        progress = "Read file.py\nWrote report.md\n✻ Working… (11s · 2.1k tokens)"

        self.assertEqual(stable_pane_digest(first), stable_pane_digest(later))
        self.assertNotEqual(stable_pane_digest(first), stable_pane_digest(progress))

    def test_typing_refresh_uses_active_thread_and_throttles(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.typing_publisher = Mock()
            bridge.agent_busy = Mock(return_value=True)
            bridge.state.data["active_turn"] = {
                "channel_id": "channel-1",
                "root_event_id": "a" * 64,
                "parent_event_id": "b" * 64,
            }

            bridge.refresh_typing(now=100)
            bridge.refresh_typing(now=101)
            bridge.refresh_typing(now=103)

            self.assertEqual(bridge.typing_publisher.publish_typing.call_count, 2)
            bridge.typing_publisher.publish_typing.assert_called_with(
                "channel-1",
                "a" * 64,
                "b" * 64,
            )

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
            bridge.dispatch_pending = Mock()
            bridge.monitor_turn = Mock()
            events = [
                {
                    "id": "allowed",
                    "pubkey": OWNER_PUBKEY,
                    "kind": 9,
                    "content": "yes",
                    "created_at": 10,
                    "channel_id": JOE_DM_CHANNEL,
                    "tags": [],
                },
                {
                    "id": "denied",
                    "pubkey": "f" * 64,
                    "kind": 9,
                    "content": "no",
                    "created_at": 11,
                    "channel_id": JOE_DM_CHANNEL,
                    "tags": [],
                },
            ]
            bridge.buzz = Mock(return_value=json.dumps(events))

            bridge.poll()

            bridge.dispatch_pending.assert_called_once()
            self.assertEqual(bridge.state.pending_count(), 1)
            self.assertTrue(bridge.state.seen_event("denied"))

    def test_poll_uses_one_recovery_cursor_across_channels(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.make_bridge(temporary)
            bridge.state.data["since"] = 7
            bridge.state.data["last_error"] = "stale failure"
            bridge.member_channels = Mock(return_value=["channel-1", "channel-2"])
            bridge.forward_replies = Mock()
            bridge.write_health = Mock(return_value={
                "relay_ok": True,
                "tmux_ok": True,
                "claude_ok": True,
            })
            bridge.refresh_presence = Mock()
            bridge.dispatch_pending = Mock()
            bridge.monitor_turn = Mock()
            bridge.buzz = Mock(return_value="[]")

            bridge.poll()

            since_values = [
                call.args[call.args.index("--since") + 1]
                for call in bridge.buzz.call_args_list
            ]
            self.assertEqual(since_values, ["7", "7"])
            self.assertEqual(bridge.state.data["last_error"], "")

    def test_read_only_buzz_commands_are_validated(self):
        self.assertEqual(
            read_command("search", query="launch", limit=5),
            ("messages", "search", "--query", "launch", "--limit", "5"),
        )
        self.assertEqual(
            read_command("feed", limit=10),
            ("feed", "get", "--limit", "10"),
        )
        with self.assertRaisesRegex(ValueError, "channel"):
            read_command("thread", channel="../bad", event="f" * 64)
        with self.assertRaisesRegex(ValueError, "limit"):
            read_command("search", query="x", limit=101)

    def test_read_buzz_cli_uses_allowlisted_command(self):
        fake_bridge = Mock()
        fake_bridge.buzz.return_value = '[{"type":"mention"}]'
        with (
            patch.object(read_buzz, "BuzzBridge", return_value=fake_bridge),
            patch("sys.argv", ["read_buzz", "feed", "--limit", "5"]),
            patch("builtins.print") as output,
        ):
            read_buzz.main()

        fake_bridge.buzz.assert_called_once_with("feed", "get", "--limit", "5")
        output.assert_called_once_with('[{"type":"mention"}]')


if __name__ == "__main__":
    unittest.main()
