import math

import pytest

from lm3_teleop_bridge.config import SafetyConfig
from lm3_teleop_bridge.safety import (
    LeaseManager,
    TokenBucket,
    clamp_twist,
    orientation_within_envelope,
    predict_orientation_ok,
    predict_workspace_ok,
    shortest_angular_distance_rad,
)


def test_single_writer_lease_and_expiry() -> None:
    leases = LeaseManager(2_000)
    first = leases.acquire(session_id="a", client_id="phone-a", requested_ms=2_000, now=10.0)
    assert first is not None
    assert leases.acquire(session_id="b", client_id="phone-b", requested_ms=2_000, now=10.1) is None
    assert leases.renew("a", first.lease_id, now=11.0) is first
    assert leases.expire(now=13.1) is first
    assert leases.current is None


def test_acquire_and_renew_do_not_silently_consume_expired_lease() -> None:
    leases = LeaseManager(default_lease_ms=2_000)
    first = leases.acquire(session_id="a", client_id="phone-a", requested_ms=500, now=10.0)
    assert first is not None

    assert leases.renew("a", first.lease_id, now=10.6) is None
    assert leases.current is first
    assert leases.acquire(
        session_id="b",
        client_id="phone-b",
        requested_ms=500,
        now=10.6,
    ) is None
    assert leases.current is first
    assert leases.expire(now=10.6) is first
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


def test_orientation_envelope_uses_shortest_distance_across_wraparound() -> None:
    config = SafetyConfig(
        orientation_configured=True,
        orientation_center_rad=(3.13, 0.0, 0.0),
        orientation_tolerance_rad=(0.05, 0.05, 0.05),
    )
    assert abs(shortest_angular_distance_rad(-3.13, 3.13)) < 0.05
    assert orientation_within_envelope((-3.13, 0.0, 0.0), config)


def test_orientation_envelope_rejects_current_orientation_outside_limit() -> None:
    config = SafetyConfig(
        orientation_configured=True,
        orientation_center_rad=(0.0, 0.0, 0.0),
        orientation_tolerance_rad=(0.1, 0.1, 0.1),
    )
    assert not predict_orientation_ok((0.11, 0.0, 0.0), (0.0, 0.0, 0.0), 100, config)


def test_orientation_envelope_rejects_predicted_orientation_outside_limit() -> None:
    config = SafetyConfig(
        orientation_configured=True,
        orientation_center_rad=(0.0, 0.0, 0.0),
        orientation_tolerance_rad=(0.1, 0.1, 0.1),
    )
    assert not predict_orientation_ok((0.09, 0.0, 0.0), (0.15, 0.0, 0.0), 100, config)


def test_orientation_envelope_rejects_path_that_leaves_then_wraps_back_inside() -> None:
    config = SafetyConfig(
        orientation_configured=True,
        orientation_center_rad=(0.0, 0.0, 0.0),
        orientation_tolerance_rad=(0.1, 0.1, 0.1),
    )
    displacement_rad = 2 * math.pi - 0.18
    assert not predict_orientation_ok(
        (0.09, 0.0, 0.0),
        (displacement_rad, 0.0, 0.0),
        1_000,
        config,
    )


def test_orientation_envelope_rejects_zyx_gimbal_lock_region_at_runtime() -> None:
    config = SafetyConfig(
        orientation_configured=True,
        orientation_center_rad=(0.0, 0.0, 0.0),
        orientation_tolerance_rad=(0.1, 0.1, 0.1),
    )
    assert not orientation_within_envelope((0.0, math.pi / 2 - 0.05, 0.0), config)
