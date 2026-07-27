#!/usr/bin/env python3

import json
import unittest
from unittest.mock import patch

from integrations import nostr_ephemeral
from integrations.nostr_ephemeral import (
    EphemeralPublisher,
    build_event,
    build_typing_event,
    public_key,
    schnorr_sign,
    websocket_url,
)


class NostrEphemeralTest(unittest.TestCase):
    def test_bip340_official_signing_vector_zero(self):
        secret = bytes.fromhex(
            "0000000000000000000000000000000000000000000000000000000000000003"
        )
        message = bytes(32)
        auxiliary = bytes(32)

        signature = schnorr_sign(message, secret, auxiliary)

        self.assertEqual(
            signature.hex().upper(),
            "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
            "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0",
        )

    def test_build_event_uses_canonical_nostr_id_and_signature(self):
        secret = "0" * 63 + "3"
        event = build_event(
            secret,
            kind=20002,
            content="",
            tags=[["h", "channel"]],
            created_at=100,
            auxiliary=bytes(32),
        )

        self.assertEqual(event["kind"], 20002)
        self.assertEqual(event["created_at"], 100)
        self.assertEqual(event["tags"], [["h", "channel"]])
        self.assertEqual(len(event["id"]), 64)
        self.assertEqual(len(event["sig"]), 128)

    def test_typing_event_preserves_thread_root_and_parent(self):
        event = build_typing_event(
            "0" * 63 + "3",
            channel_id="09eec80f-1a32-41a6-bf46-399a79d87b67",
            root_event_id="a" * 64,
            parent_event_id="b" * 64,
            created_at=100,
            auxiliary=bytes(32),
        )
        self.assertEqual(
            event["tags"],
            [
                ["h", "09eec80f-1a32-41a6-bf46-399a79d87b67"],
                ["e", "a" * 64, "", "root"],
                ["e", "b" * 64, "", "reply"],
            ],
        )

    def test_key_and_url_validation_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "private key"):
            public_key(bytes(32))
        with self.assertRaisesRegex(ValueError, "32-byte"):
            schnorr_sign(b"short", bytes.fromhex("0" * 63 + "3"))
        with self.assertRaisesRegex(ValueError, "relay URL"):
            websocket_url("ftp://buzz.example")
        self.assertEqual(websocket_url("https://buzz.example"), "wss://buzz.example")
        self.assertEqual(websocket_url("http://buzz.example"), "ws://buzz.example")

    def test_publisher_authenticates_and_reuses_connection(self):
        class FakeConnection:
            def __init__(self):
                self.frames = [["AUTH", "challenge"]]
                self.closed = False

            def recv(self, timeout):
                return json.dumps(self.frames.pop(0))

            def send(self, payload):
                frame = json.loads(payload)
                self.frames.append(["OK", frame[1]["id"], True, ""])

            def close(self):
                self.closed = True

        connection = FakeConnection()
        with patch.object(nostr_ephemeral, "connect", return_value=connection):
            publisher = EphemeralPublisher(
                "https://buzz.example",
                "0" * 63 + "3",
            )
            first = publisher.publish_typing("channel", None, None)
            second = publisher.publish_typing("channel", None, None)
            publisher.close()

        self.assertEqual(len(first), 64)
        self.assertEqual(len(second), 64)
        self.assertTrue(connection.closed)

    def test_publisher_surfaces_relay_rejection(self):
        class RejectingConnection:
            def __init__(self):
                self.frames = [["AUTH", "challenge"]]

            def recv(self, timeout):
                return json.dumps(self.frames.pop(0))

            def send(self, payload):
                frame = json.loads(payload)
                accepted = frame[0] == "AUTH"
                self.frames.append(
                    ["OK", frame[1]["id"], accepted, "restricted"]
                )

            def close(self):
                pass

        with patch.object(
            nostr_ephemeral,
            "connect",
            return_value=RejectingConnection(),
        ):
            publisher = EphemeralPublisher(
                "https://buzz.example",
                "0" * 63 + "3",
            )
            with self.assertRaisesRegex(PermissionError, "restricted"):
                publisher.publish_typing("channel", None, None)

if __name__ == "__main__":
    unittest.main()
