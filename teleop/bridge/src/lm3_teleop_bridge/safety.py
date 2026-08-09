from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass

from .config import ORIENTATION_GIMBAL_LOCK_MARGIN_RAD, SafetyConfig


@dataclass(slots=True)
class Lease:
    lease_id: str
    session_id: str
    client_id: str
    expires_monotonic: float
    duration_ms: int


class LeaseManager:
    def __init__(self, default_lease_ms: int) -> None:
        self.default_lease_ms = default_lease_ms
        self.current: Lease | None = None

    def acquire(
        self,
        *,
        session_id: str,
        client_id: str,
        requested_ms: int,
        now: float | None = None,
    ) -> Lease | None:
        current_time = time.monotonic() if now is None else now
        if self.expired(now=current_time) is not None:
            return None
        if self.current is not None and self.current.session_id != session_id:
            return None
        duration_ms = max(500, min(requested_ms, self.default_lease_ms))
        lease = Lease(
            lease_id=self.current.lease_id if self.current else secrets.token_urlsafe(18),
            session_id=session_id,
            client_id=client_id,
            expires_monotonic=current_time + duration_ms / 1_000,
            duration_ms=duration_ms,
        )
        self.current = lease
        return lease

    def renew(self, session_id: str, lease_id: str, *, now: float | None = None) -> Lease | None:
        current_time = time.monotonic() if now is None else now
        if self.expired(now=current_time) is not None:
            return None
        lease = self.current
        if lease is None or lease.session_id != session_id or lease.lease_id != lease_id:
            return None
        lease.expires_monotonic = current_time + lease.duration_ms / 1_000
        return lease

    def valid(self, session_id: str, lease_id: str, *, now: float | None = None) -> bool:
        return self.renew(session_id, lease_id, now=now) is not None

    def release(self, session_id: str | None = None) -> Lease | None:
        if self.current is None:
            return None
        if session_id is not None and self.current.session_id != session_id:
            return None
        released = self.current
        self.current = None
        return released

    def expire(self, *, now: float | None = None) -> Lease | None:
        if self.expired(now=now) is not None:
            return self.release()
        return None

    def expired(self, *, now: float | None = None) -> Lease | None:
        current_time = time.monotonic() if now is None else now
        if self.current is not None and self.current.expires_monotonic <= current_time:
            return self.current
        return None

    @staticmethod
    def expires_at_ms(lease: Lease, *, now_mono: float | None = None, now_ms: int | None = None) -> int:
        monotonic_now = time.monotonic() if now_mono is None else now_mono
        wall_now = int(time.time() * 1_000) if now_ms is None else now_ms
        remaining_ms = max(0, int((lease.expires_monotonic - monotonic_now) * 1_000))
        return wall_now + remaining_ms


class TokenBucket:
    def __init__(self, rate_hz: float, capacity: float = 1.0) -> None:
        self.rate_hz = rate_hz
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.monotonic()

    def consume(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        self.tokens = min(self.capacity, self.tokens + max(0.0, current - self.last) * self.rate_hz)
        self.last = current
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


def clamp_twist(
    linear: tuple[float, float, float],
    angular: tuple[float, float, float],
    config: SafetyConfig,
) -> tuple[tuple[float, float, float], tuple[float, float, float], bool]:
    clamped_linear, linear_clamped = _clamp_norm(linear, config.max_linear_mps)
    clamped_angular, angular_clamped = _clamp_norm(angular, config.max_angular_rps)
    return clamped_linear, clamped_angular, linear_clamped or angular_clamped


def predict_workspace_ok(
    tcp_xyz: tuple[float, float, float],
    linear: tuple[float, float, float],
    duration_ms: int,
    config: SafetyConfig,
) -> bool:
    duration_s = duration_ms / 1_000
    predicted = tuple(position + speed * duration_s for position, speed in zip(tcp_xyz, linear, strict=True))
    for current, future, low, high in zip(
        tcp_xyz,
        predicted,
        config.workspace_min_m,
        config.workspace_max_m,
        strict=True,
    ):
        if not (low <= current <= high and low <= future <= high):
            return False
    return True


def shortest_angular_distance_rad(angle_rad: float, center_rad: float) -> float:
    """Return the signed shortest distance from ``center_rad`` to ``angle_rad``."""

    delta = angle_rad - center_rad
    return math.atan2(math.sin(delta), math.cos(delta))


def orientation_within_envelope(
    orientation_rad: tuple[float, float, float], config: SafetyConfig
) -> bool:
    if any(
        abs(shortest_angular_distance_rad(orientation_rad[1], singularity))
        <= ORIENTATION_GIMBAL_LOCK_MARGIN_RAD
        for singularity in (-math.pi / 2, math.pi / 2)
    ):
        return False
    return all(
        abs(shortest_angular_distance_rad(angle, center)) <= tolerance
        for angle, center, tolerance in zip(
            orientation_rad,
            config.orientation_center_rad,
            config.orientation_tolerance_rad,
            strict=True,
        )
    )


def predict_orientation_ok(
    orientation_rad: tuple[float, float, float],
    angular_rps: tuple[float, float, float],
    duration_ms: int,
    config: SafetyConfig,
) -> bool:
    duration_s = duration_ms / 1_000
    if not orientation_within_envelope(orientation_rad, config):
        return False
    for angle, speed, center, tolerance in zip(
        orientation_rad,
        angular_rps,
        config.orientation_center_rad,
        config.orientation_tolerance_rad,
        strict=True,
    ):
        current_delta = shortest_angular_distance_rad(angle, center)
        predicted_delta = current_delta + speed * duration_s
        if abs(predicted_delta) > tolerance:
            return False
    predicted_ry = orientation_rad[1] + angular_rps[1] * duration_s
    return all(
        abs(shortest_angular_distance_rad(predicted_ry, singularity))
        > ORIENTATION_GIMBAL_LOCK_MARGIN_RAD
        for singularity in (-math.pi / 2, math.pi / 2)
    )


def joints_within_margin(joints: list[float], config: SafetyConfig) -> bool:
    if len(joints) != 6:
        return False
    return all(
        low + config.joint_limit_margin_rad <= value <= high - config.joint_limit_margin_rad
        for value, low, high in zip(joints, config.joint_min_rad, config.joint_max_rad, strict=True)
    )


def _clamp_norm(vector: tuple[float, float, float], limit: float) -> tuple[tuple[float, float, float], bool]:
    norm = math.sqrt(sum(component * component for component in vector))
    if norm <= limit or norm == 0:
        return vector, False
    scale = limit / norm
    return tuple(component * scale for component in vector), True  # type: ignore[return-value]
