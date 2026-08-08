import json

import pytest

from lm3_teleop_bridge.protocol import PROTOCOL, ProtocolError, decode_envelope, encode_envelope


def test_round_trip_accepts_first_sequence_zero() -> None:
    text = encode_envelope("heartbeat", 0, 1234, {"deadman": False})
    envelope = decode_envelope(text)
    assert envelope.seq == 0
    assert envelope.type == "heartbeat"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_is_rejected(constant: str) -> None:
    text = (
        '{"protocol":"lm3-teleop.v1","type":"heartbeat","seq":0,'
        f'"sent_at_ms":1,"body":{{"deadman":false,"bad":{constant}}}}}'
    )
    with pytest.raises(ProtocolError, match="non-finite"):
        decode_envelope(text)


def test_boolean_is_not_a_valid_sequence() -> None:
    value = {
        "protocol": PROTOCOL,
        "type": "heartbeat",
        "seq": True,
        "sent_at_ms": 1,
        "body": {"deadman": False},
    }
    with pytest.raises(ProtocolError, match="seq"):
        decode_envelope(json.dumps(value))
