from __future__ import annotations

import ipaddress
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a configuration would weaken a required safety invariant."""


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/ws"
    auth_token_env: str = "LM3_TELEOP_TOKEN"
    allow_lan: bool = False
    state_hz: int = 20
    max_message_age_ms: int = 2_000
    max_future_skew_ms: int = 5_000
    lease_ms: int = 2_000


@dataclass(slots=True)
class RobotConfig:
    backend: str = "simulator"
    robot_ip: str = ""
    pylebai_path: str = ""
    base_locked: bool = False
    hardware_enabled: bool = False
    cartesian_acceleration_mps2: float = 0.1
    gripper_force_pct: float = 20.0
    emergency_stop_port: int = 3031
    emergency_stop_timeout_ms: int = 200


@dataclass(slots=True)
class SafetyConfig:
    command_rate_hz: int = 20
    watchdog_ms: int = 300
    feedback_stall_ms: int = 250
    max_linear_mps: float = 0.03
    max_angular_rps: float = 0.15
    max_command_duration_ms: int = 150
    min_command_duration_ms: int = 20
    workspace_configured: bool = False
    workspace_min_m: tuple[float, float, float] = (0.10, -0.60, 0.02)
    workspace_max_m: tuple[float, float, float] = (0.80, 0.60, 0.80)
    joint_limits_configured: bool = False
    joint_min_rad: tuple[float, ...] = (-6.283, -6.283, -6.283, -6.283, -6.283, -6.283)
    joint_max_rad: tuple[float, ...] = (6.283, 6.283, 6.283, 6.283, 6.283, 6.283)
    joint_limit_margin_rad: float = 0.05
    allowed_robot_states: tuple[int, ...] = (5, 7)


@dataclass(slots=True)
class RecordingConfig:
    root: Path = Path("teleop-data/raw")
    fps: int = 20


@dataclass(slots=True)
class CameraConfig:
    source: str
    fps: int = 20
    jpeg_quality: int = 90


@dataclass(slots=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    robot: RobotConfig = field(default_factory=RobotConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    cameras: dict[str, CameraConfig] = field(default_factory=dict)

    def resolved_token(self, environ: dict[str, str] | None = None) -> str:
        source = os.environ if environ is None else environ
        token = source.get(self.server.auth_token_env, "")
        if len(token) < 16:
            raise ConfigError(
                f"environment variable {self.server.auth_token_env} must contain at least 16 characters"
            )
        return token

    def validate(
        self,
        *,
        hardware_flag: bool = False,
        allow_lan_flag: bool = False,
        allow_ephemeral_port: bool = False,
    ) -> None:
        port_is_valid = _is_int(self.server.port) and (
            1 <= self.server.port <= 65535
            or (allow_ephemeral_port and self.server.port == 0)
        )
        if not port_is_valid:
            raise ConfigError("server.port must be between 1 and 65535")
        if not isinstance(self.server.path, str) or not self.server.path.startswith("/"):
            raise ConfigError("server.path must start with '/'")
        if not _is_int(self.server.state_hz) or not (1 <= self.server.state_hz <= 50):
            raise ConfigError("server.state_hz must be between 1 and 50")
        if not _is_int(self.server.max_message_age_ms) or self.server.max_message_age_ms <= 0:
            raise ConfigError("server.max_message_age_ms must be a positive integer")
        if not _is_int(self.server.max_future_skew_ms) or self.server.max_future_skew_ms < 0:
            raise ConfigError("server.max_future_skew_ms must be a non-negative integer")
        if not _is_int(self.server.lease_ms) or not (500 <= self.server.lease_ms <= 2_000):
            raise ConfigError("server.lease_ms must be between 500 and 2000 ms")
        if not _is_int(self.recording.fps) or not (1 <= self.recording.fps <= 50):
            raise ConfigError("recording.fps must be between 1 and 50")
        if self.recording.fps != self.server.state_hz:
            raise ConfigError("recording.fps must equal server.state_hz so recorded timing is truthful")
        if not _is_int(self.safety.command_rate_hz) or self.safety.command_rate_hz != 20:
            raise ConfigError("v1 requires safety.command_rate_hz = 20")
        if not _is_int(self.safety.watchdog_ms) or not (1 <= self.safety.watchdog_ms <= 300):
            raise ConfigError("safety.watchdog_ms must be between 1 and 300")
        if not _is_int(self.safety.feedback_stall_ms) or not (
            100 <= self.safety.feedback_stall_ms <= self.safety.watchdog_ms
        ):
            raise ConfigError(
                "safety.feedback_stall_ms must be between 100 ms and the watchdog timeout"
            )
        minimum_feedback_window_ms = math.ceil(2_000 / self.server.state_hz)
        if self.safety.feedback_stall_ms < minimum_feedback_window_ms:
            raise ConfigError(
                "safety.feedback_stall_ms must span at least two configured state samples"
            )
        if self.server.lease_ms <= self.safety.watchdog_ms:
            raise ConfigError("server.lease_ms must be longer than the motion watchdog")
        if not _is_finite_positive(self.safety.max_linear_mps) or not _is_finite_positive(
            self.safety.max_angular_rps
        ):
            raise ConfigError("velocity limits must be positive")
        if not (
            _is_int(self.safety.min_command_duration_ms)
            and _is_int(self.safety.max_command_duration_ms)
            and 1
            <= self.safety.min_command_duration_ms
            <= self.safety.max_command_duration_ms
            <= 150
        ):
            raise ConfigError("command duration bounds must fit within 1..150 ms")
        if len(self.safety.workspace_min_m) != 3 or len(self.safety.workspace_max_m) != 3:
            raise ConfigError("workspace bounds must each contain three values")
        if not _all_finite((*self.safety.workspace_min_m, *self.safety.workspace_max_m)):
            raise ConfigError("workspace bounds must contain only finite numbers")
        if any(
            low >= high
            for low, high in zip(
                self.safety.workspace_min_m, self.safety.workspace_max_m, strict=True
            )
        ):
            raise ConfigError("each workspace minimum must be below its maximum")
        if len(self.safety.joint_min_rad) != 6 or len(self.safety.joint_max_rad) != 6:
            raise ConfigError("joint bounds must each contain six values")
        if not _all_finite((*self.safety.joint_min_rad, *self.safety.joint_max_rad)):
            raise ConfigError("joint bounds must contain only finite numbers")
        if not _is_finite_number(self.safety.joint_limit_margin_rad) or self.safety.joint_limit_margin_rad < 0:
            raise ConfigError("joint_limit_margin_rad must not be negative")
        if any(
            low + 2 * self.safety.joint_limit_margin_rad >= high
            for low, high in zip(
                self.safety.joint_min_rad, self.safety.joint_max_rad, strict=True
            )
        ):
            raise ConfigError("joint bounds must leave room for the configured margin")
        if (
            not isinstance(self.safety.allowed_robot_states, tuple)
            or not self.safety.allowed_robot_states
            or not all(_is_int(value) for value in self.safety.allowed_robot_states)
        ):
            raise ConfigError("allowed_robot_states must be a non-empty integer array")
        if len(self.safety.allowed_robot_states) != 2 or set(
            self.safety.allowed_robot_states
        ) != {5, 7}:
            raise ConfigError("v1 requires allowed_robot_states to contain exactly IDLE=5 and MOVING=7")

        if not isinstance(self.robot.base_locked, bool) or not isinstance(
            self.robot.hardware_enabled, bool
        ):
            raise ConfigError("robot safety confirmations must be boolean")
        if not _is_finite_positive(self.robot.cartesian_acceleration_mps2):
            raise ConfigError("robot.cartesian_acceleration_mps2 must be finite and positive")
        if not _is_finite_number(self.robot.gripper_force_pct) or not (
            0 <= self.robot.gripper_force_pct <= 100
        ):
            raise ConfigError("robot.gripper_force_pct must be within 0..100")
        if not _is_int(self.robot.emergency_stop_port) or not (
            1 <= self.robot.emergency_stop_port <= 65535
        ):
            raise ConfigError("robot.emergency_stop_port must be between 1 and 65535")
        if not _is_int(self.robot.emergency_stop_timeout_ms) or not (
            1 <= self.robot.emergency_stop_timeout_ms <= self.safety.watchdog_ms
        ):
            raise ConfigError(
                "robot.emergency_stop_timeout_ms must be positive and no longer than the watchdog"
            )

        host_is_loopback = _is_loopback_host(self.server.host)
        lan_allowed = self.server.allow_lan and allow_lan_flag
        if not host_is_loopback and not lan_allowed:
            raise ConfigError(
                "non-loopback listen address requires both server.allow_lan=true and --allow-lan"
            )

        backend = self.robot.backend.lower()
        if backend not in {"simulator", "hardware"}:
            raise ConfigError("robot.backend must be 'simulator' or 'hardware'")
        if backend == "hardware":
            if not (self.robot.hardware_enabled and hardware_flag):
                raise ConfigError(
                    "hardware mode requires robot.hardware_enabled=true and the --hardware CLI flag"
                )
            if not self.robot.robot_ip:
                raise ConfigError("hardware mode requires robot.robot_ip or --robot-ip")
            try:
                ipaddress.ip_address(self.robot.robot_ip)
            except ValueError as exc:
                raise ConfigError("robot.robot_ip must be a literal IP address") from exc
            if not self.robot.base_locked:
                raise ConfigError("hardware mode requires an explicit base_locked confirmation")
            if not self.safety.workspace_configured:
                raise ConfigError("hardware mode requires a measured TCP workspace")
            if not self.safety.joint_limits_configured:
                raise ConfigError("hardware mode requires measured joint limits")

        for name, camera in self.cameras.items():
            if re.fullmatch(r"camera_[A-Za-z0-9_-]+", name) is None:
                raise ConfigError(
                    "camera names must match camera_[A-Za-z0-9_-]+ and may not contain paths"
                )
            if not isinstance(camera.source, str) or not camera.source.strip():
                raise ConfigError(f"camera {name} source must be a non-empty string")
            if not _is_int(camera.fps) or not (1 <= camera.fps <= 120):
                raise ConfigError(f"camera {name} fps must be between 1 and 120")
            if not _is_int(camera.jpeg_quality) or not (1 <= camera.jpeg_quality <= 100):
                raise ConfigError(f"camera {name} jpeg_quality must be between 1 and 100")


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    server_raw = _section(raw, "server")
    robot_raw = _section(raw, "robot")
    safety_raw = _section(raw, "safety")
    recording_raw = _section(raw, "recording")
    camera_raw = raw.get("cameras", {})
    if not isinstance(camera_raw, dict):
        raise ConfigError("[cameras] must be a table")

    try:
        server = ServerConfig(**server_raw)
        robot = RobotConfig(**robot_raw)
    except TypeError as exc:
        raise ConfigError(f"invalid server or robot configuration: {exc}") from exc
    safety_values = dict(safety_raw)
    for key in (
        "workspace_min_m",
        "workspace_max_m",
        "joint_min_rad",
        "joint_max_rad",
        "allowed_robot_states",
    ):
        if key in safety_values:
            safety_values[key] = tuple(safety_values[key])
    try:
        safety = SafetyConfig(**safety_values)
    except TypeError as exc:
        raise ConfigError(f"invalid safety configuration: {exc}") from exc

    recording_values = dict(recording_raw)
    if "root" in recording_values:
        root = Path(recording_values["root"])
        if not root.is_absolute():
            root = (config_path.parent / root).resolve()
        recording_values["root"] = root
    try:
        recording = RecordingConfig(**recording_values)
    except TypeError as exc:
        raise ConfigError(f"invalid recording configuration: {exc}") from exc

    if robot.pylebai_path:
        sdk_path = Path(robot.pylebai_path)
        if not sdk_path.is_absolute():
            robot.pylebai_path = str((config_path.parent / sdk_path).resolve())

    cameras: dict[str, CameraConfig] = {}
    for name, values in camera_raw.items():
        if not isinstance(name, str) or re.fullmatch(r"camera_[A-Za-z0-9_-]+", name) is None:
            raise ConfigError("camera names must match camera_[A-Za-z0-9_-]+")
        if not isinstance(values, dict):
            raise ConfigError(f"camera {name} must be a table")
        camera_values = dict(values)
        if isinstance(camera_values.get("source"), int) and not isinstance(
            camera_values.get("source"), bool
        ):
            camera_values["source"] = str(camera_values["source"])
        try:
            cameras[name] = CameraConfig(**camera_values)
        except TypeError as exc:
            raise ConfigError(f"invalid camera {name} configuration: {exc}") from exc

    return AppConfig(
        server=server,
        robot=robot,
        safety=safety,
        recording=recording,
        cameras=cameras,
    )


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_finite_positive(value: Any) -> bool:
    return _is_finite_number(value) and value > 0


def _all_finite(values: tuple[Any, ...]) -> bool:
    return all(_is_finite_number(value) for value in values)
