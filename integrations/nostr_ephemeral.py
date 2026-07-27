#!/usr/bin/env python3
"""Minimal Nostr signer and authenticated ephemeral-event publisher."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

FIELD = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GENERATOR = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
Point = tuple[int, int] | None


def _point_add(left: Point, right: Point) -> Point:
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 != y2 or y1 == 0):
        return None
    if x1 == x2:
        slope = (3 * x1 * x1) * pow(2 * y1, FIELD - 2, FIELD) % FIELD
    else:
        slope = (y2 - y1) * pow(x2 - x1, FIELD - 2, FIELD) % FIELD
    x3 = (slope * slope - x1 - x2) % FIELD
    return x3, (slope * (x1 - x3) - y1) % FIELD


def _point_mul(scalar: int, point: Point = GENERATOR) -> Point:
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _tagged_hash(tag: str, payload: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + payload).digest()


def public_key(secret_key: bytes) -> bytes:
    secret = int.from_bytes(secret_key, "big")
    if not 1 <= secret < ORDER:
        raise ValueError("invalid secp256k1 private key")
    point = _point_mul(secret)
    if point is None:
        raise ValueError("invalid secp256k1 public point")
    return point[0].to_bytes(32, "big")


def schnorr_sign(message: bytes, secret_key: bytes, auxiliary: bytes | None = None) -> bytes:
    if len(message) != 32 or len(secret_key) != 32:
        raise ValueError("BIP-340 requires 32-byte message and secret")
    auxiliary = os.urandom(32) if auxiliary is None else auxiliary
    if len(auxiliary) != 32:
        raise ValueError("BIP-340 auxiliary randomness must be 32 bytes")
    secret = int.from_bytes(secret_key, "big")
    if not 1 <= secret < ORDER:
        raise ValueError("invalid secp256k1 private key")
    point = _point_mul(secret)
    if point is None:
        raise ValueError("invalid secp256k1 public point")
    effective_secret = secret if point[1] % 2 == 0 else ORDER - secret
    pubkey = point[0].to_bytes(32, "big")
    masked = bytes(
        left ^ right
        for left, right in zip(
            effective_secret.to_bytes(32, "big"),
            _tagged_hash("BIP0340/aux", auxiliary),
            strict=True,
        )
    )
    nonce = int.from_bytes(
        _tagged_hash("BIP0340/nonce", masked + pubkey + message), "big"
    ) % ORDER
    if nonce == 0:
        raise RuntimeError("BIP-340 nonce generation failed")
    nonce_point = _point_mul(nonce)
    if nonce_point is None:
        raise RuntimeError("BIP-340 nonce point failed")
    effective_nonce = nonce if nonce_point[1] % 2 == 0 else ORDER - nonce
    rx = nonce_point[0].to_bytes(32, "big")
    challenge = int.from_bytes(
        _tagged_hash("BIP0340/challenge", rx + pubkey + message), "big"
    ) % ORDER
    signature_scalar = (effective_nonce + challenge * effective_secret) % ORDER
    return rx + signature_scalar.to_bytes(32, "big")


def build_event(
    private_key: str,
    *,
    kind: int,
    content: str,
    tags: list[list[str]],
    created_at: int | None = None,
    auxiliary: bytes | None = None,
) -> dict[str, Any]:
    secret = bytes.fromhex(private_key)
    pubkey = public_key(secret).hex()
    timestamp = int(time.time()) if created_at is None else created_at
    serialized = json.dumps(
        [0, pubkey, timestamp, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    event_id = hashlib.sha256(serialized.encode()).digest()
    return {
        "id": event_id.hex(),
        "pubkey": pubkey,
        "created_at": timestamp,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": schnorr_sign(event_id, secret, auxiliary).hex(),
    }


def build_typing_event(
    private_key: str,
    *,
    channel_id: str,
    root_event_id: str | None = None,
    parent_event_id: str | None = None,
    created_at: int | None = None,
    auxiliary: bytes | None = None,
) -> dict[str, Any]:
    tags = [["h", channel_id]]
    if parent_event_id:
        if root_event_id and root_event_id != parent_event_id:
            tags.append(["e", root_event_id, "", "root"])
        tags.append(["e", parent_event_id, "", "reply"])
    return build_event(
        private_key,
        kind=20002,
        content="",
        tags=tags,
        created_at=created_at,
        auxiliary=auxiliary,
    )


def websocket_url(relay_url: str) -> str:
    parsed = urlparse(relay_url)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(
        parsed.scheme
    )
    if scheme is None:
        raise ValueError("relay URL must use http(s) or ws(s)")
    return urlunparse((scheme, parsed.netloc, parsed.path, "", "", ""))


class EphemeralPublisher:
    def __init__(self, relay_url: str, private_key: str):
        self.ws_url = websocket_url(relay_url)
        self.private_key = private_key
        self.connection: Any = None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _connect(self) -> None:
        self.close()
        self.connection = connect(
            self.ws_url,
            open_timeout=5,
            close_timeout=1,
        )
        for _ in range(5):
            frame = json.loads(self.connection.recv(timeout=5))
            if isinstance(frame, list) and len(frame) >= 2 and frame[0] == "AUTH":
                auth = build_event(
                    self.private_key,
                    kind=22242,
                    content="",
                    tags=[
                        ["relay", self.ws_url],
                        ["challenge", str(frame[1])],
                    ],
                )
                self.connection.send(json.dumps(["AUTH", auth], separators=(",", ":")))
                self._expect_ok(auth["id"])
                return
        raise ConnectionError("Buzz relay did not provide NIP-42 challenge")

    def _expect_ok(self, event_id: str) -> None:
        if self.connection is None:
            raise ConnectionError("Buzz WebSocket is not connected")
        for _ in range(5):
            frame = json.loads(self.connection.recv(timeout=5))
            if (
                isinstance(frame, list)
                and len(frame) >= 4
                and frame[0] == "OK"
                and frame[1] == event_id
            ):
                if frame[2] is True:
                    return
                raise PermissionError(str(frame[3]))
        raise ConnectionError("Buzz relay did not acknowledge event")

    def publish_typing(
        self,
        channel_id: str,
        root_event_id: str | None,
        parent_event_id: str | None,
    ) -> str:
        event = build_typing_event(
            self.private_key,
            channel_id=channel_id,
            root_event_id=root_event_id,
            parent_event_id=parent_event_id,
        )
        for attempt in range(2):
            try:
                if self.connection is None:
                    self._connect()
                self.connection.send(
                    json.dumps(["EVENT", event], separators=(",", ":"))
                )
                self._expect_ok(event["id"])
                return event["id"]
            except PermissionError:
                raise
            except (ConnectionError, OSError, TimeoutError, WebSocketException):
                self.close()
                if attempt:
                    raise
        raise ConnectionError("typing publish failed")
