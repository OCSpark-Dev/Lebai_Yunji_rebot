from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


PROTOCOL = "lm3-teleop.v1"
CLIENT_TYPES = {
    "session.hello",
    "control.acquire",
    "control.release",
    "heartbeat",
    "motion.cartesian_velocity",
    "motion.stop",
    "gripper.set",
    "recording.start",
    "recording.stop",
    "pose.sample",
}


class ProtocolError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool = True,
        ack_seq: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable
        self.ack_seq = ack_seq


@dataclass(frozen=True, slots=True)
class Envelope:
    type: str
    seq: int
    sent_at_ms: int
    body: dict[str, Any]


def decode_envelope(text: str) -> Envelope:
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ProtocolError) as exc:
        raise ProtocolError("INVALID_MESSAGE", f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("INVALID_MESSAGE", "message must be a JSON object")
    if value.get("protocol") != PROTOCOL:
        raise ProtocolError("PROTOCOL_MISMATCH", f"expected protocol {PROTOCOL}", recoverable=False)
    message_type = value.get("type")
    if not isinstance(message_type, str) or message_type not in CLIENT_TYPES:
        raise ProtocolError("INVALID_MESSAGE", "unknown or missing message type")
    seq = _strict_int(value.get("seq"), "seq")
    sent_at_ms = _strict_int(value.get("sent_at_ms"), "sent_at_ms")
    body = value.get("body")
    if not isinstance(body, dict):
        raise ProtocolError("INVALID_MESSAGE", "body must be an object", ack_seq=seq)
    _assert_finite(body, ack_seq=seq)
    return Envelope(message_type, seq, sent_at_ms, body)


def encode_envelope(message_type: str, seq: int, sent_at_ms: int, body: dict[str, Any]) -> str:
    return json.dumps(
        {
            "protocol": PROTOCOL,
            "type": message_type,
            "seq": seq,
            "sent_at_ms": sent_at_ms,
            "body": body,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def require_string(body: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = body.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ProtocolError("INVALID_MESSAGE", f"{key} must be a non-empty string")
    return value


def require_bool(body: dict[str, Any], key: str) -> bool:
    value = body.get(key)
    if not isinstance(value, bool):
        raise ProtocolError("INVALID_MESSAGE", f"{key} must be boolean")
    return value


def require_number(body: dict[str, Any], key: str) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProtocolError("INVALID_MESSAGE", f"{key} must be a finite number")
    return float(value)


def require_int(body: dict[str, Any], key: str) -> int:
    return _strict_int(body.get(key), key)


def require_vector(body: dict[str, Any], key: str, axes: tuple[str, str, str]) -> tuple[float, float, float]:
    value = body.get(key)
    if not isinstance(value, dict):
        raise ProtocolError("INVALID_MESSAGE", f"{key} must be an object")
    return tuple(require_number(value, axis) for axis in axes)  # type: ignore[return-value]


def _strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError("INVALID_MESSAGE", f"{name} must be a non-negative integer")
    return value


def _assert_finite(value: Any, *, ack_seq: int | None = None) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ProtocolError("INVALID_MESSAGE", "non-finite number rejected", ack_seq=ack_seq)
    if isinstance(value, dict):
        for nested in value.values():
            _assert_finite(nested, ack_seq=ack_seq)
    elif isinstance(value, list):
        for nested in value:
            _assert_finite(nested, ack_seq=ack_seq)


def _reject_constant(value: str) -> None:
    raise ProtocolError("INVALID_MESSAGE", f"non-finite JSON constant rejected: {value}")
