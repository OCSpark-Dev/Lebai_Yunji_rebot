import pytest

from lm3_teleop_bridge.config import SafetyConfig
from lm3_teleop_bridge.safety import LeaseManager, TokenBucket, clamp_twist, predict_workspace_ok


def test_single_writer_lease_and_expiry() -> None:
    leases = LeaseManager(2_000)
    first = leases.acquire(session_id="a", client_id="phone-a", requested_ms=2_000, now=10.0)
    assert first is not None
    assert leases.acquire(session_id="b", client_id="phone-b", requested_ms=2_000, now=10.1) is None
    assert leases.renew("a", first.lease_id, now=11.0) is first
    assert leases.expire(now=13.1) is first
    assert leases.current is None


def test_token_bucket_is_single_slot_and_enforces_minimum_spacing() -> None:
    bucket = TokenBucket(20.0, capacity=1.0)
    bucket.last = 0.0
    bucket.tokens = 1.0
    assert bucket.consume(0.0)
    assert not bucket.consume(0.0)
    assert bucket.consume(0.05)


def test_requested_lease_is_bounded_by_server_limit() -> None:
    leases = LeaseManager(2_000)
    lease = leases.acquire(session_id="a", client_id="phone-a", requested_ms=5_000, now=10.0)
    assert lease is not None
    assert lease.duration_ms == 2_000


def test_twist_is_norm_clamped_and_workspace_predicted() -> None:
    config = SafetyConfig(max_linear_mps=0.03, max_angular_rps=0.15)
    linear, angular, clamped = clamp_twist((0.03, 0.04, 0.0), (0.0, 0.0, 0.3), config)
    assert clamped
    assert sum(value * value for value in linear) ** 0.5 == pytest.approx(0.03)
    assert angular[2] == pytest.approx(0.15)
    assert predict_workspace_ok((0.4, 0.0, 0.3), linear, 100, config)
    assert not predict_workspace_ok((0.81, 0.0, 0.3), (0.0, 0.0, 0.0), 100, config)
