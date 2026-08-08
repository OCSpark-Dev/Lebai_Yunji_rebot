from __future__ import annotations

import http.client
import importlib
import json
import math
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .config import RobotConfig


ROBOT_STATE_NAMES = {
    0: "DISCONNECTED",
    1: "ESTOP",
    2: "BOOTING",
    3: "ROBOT_OFF",
    4: "ROBOT_ON",
    5: "IDLE",
    6: "PAUSED",
    7: "MOVING",
    8: "UPDATING",
    9: "STARTING",
    10: "STOPPING",
    11: "TEACHING",
    12: "STOP",
}


@dataclass(slots=True)
class RobotSnapshot:
    robot_state: str
    robot_state_code: int
    estop_reason: str
    joint_position_rad: list[float]
    joint_velocity_rad_s: list[float]
    tcp_pose: dict[str, float]
    gripper_pct: float
    base_locked: bool
    backend_time_ms: int = field(default_factory=lambda: int(time.time() * 1_000))

    def protocol_body(self, *, watchdog_ok: bool, recording: bool) -> dict[str, Any]:
        return {
            "robot_state": self.robot_state,
            "estop_reason": self.estop_reason,
            "joint_position_rad": self.joint_position_rad,
            "joint_velocity_rad_s": self.joint_velocity_rad_s,
            "tcp_pose": self.tcp_pose,
            "gripper_pct": self.gripper_pct,
            "base_locked": self.base_locked,
            "watchdog_ok": watchdog_ok,
            "recording": recording,
        }


class RobotBackend(Protocol):
    mode: str

    def snapshot(self) -> RobotSnapshot: ...

    def speed_cartesian(
        self,
        linear_mps: tuple[float, float, float],
        angular_rps: tuple[float, float, float],
        duration_ms: int,
    ) -> int: ...

    def stop(self) -> None: ...

    def set_gripper(self, position_pct: float) -> None: ...

    def close(self) -> None: ...


class SimulatorBackend:
    mode = "simulator"

    def __init__(self, config: RobotConfig) -> None:
        self._lock = threading.Lock()
        self._base_locked = config.base_locked
        self._joint_position = [0.0] * 6
        self._joint_velocity = [0.0] * 6
        self._tcp = {"x": 0.40, "y": 0.0, "z": 0.30, "rx": 0.0, "ry": math.pi, "rz": 0.0}
        self._gripper = 50.0
        self._moving_until = 0.0
        self._motion_id = 0
        self.stop_count = 0
        self.last_command: dict[str, Any] | None = None

    def snapshot(self) -> RobotSnapshot:
        with self._lock:
            moving = time.monotonic() < self._moving_until
            if not moving:
                self._joint_velocity = [0.0] * 6
            return RobotSnapshot(
                robot_state="MOVING" if moving else "IDLE",
                robot_state_code=7 if moving else 5,
                estop_reason="",
                joint_position_rad=list(self._joint_position),
                joint_velocity_rad_s=list(self._joint_velocity),
                tcp_pose=dict(self._tcp),
                gripper_pct=self._gripper,
                base_locked=self._base_locked,
            )

    def speed_cartesian(
        self,
        linear_mps: tuple[float, float, float],
        angular_rps: tuple[float, float, float],
        duration_ms: int,
    ) -> int:
        with self._lock:
            duration_s = duration_ms / 1_000
            for axis, speed in zip(("x", "y", "z"), linear_mps, strict=True):
                self._tcp[axis] += speed * duration_s
            for axis, speed in zip(("rx", "ry", "rz"), angular_rps, strict=True):
                self._tcp[axis] += speed * duration_s
            self._moving_until = time.monotonic() + duration_s
            self._motion_id += 1
            self.last_command = {
                "linear_mps": list(linear_mps),
                "angular_rps": list(angular_rps),
                "duration_ms": duration_ms,
            }
            return self._motion_id

    def stop(self) -> None:
        with self._lock:
            self._moving_until = 0.0
            self._joint_velocity = [0.0] * 6
            self.stop_count += 1

    def set_gripper(self, position_pct: float) -> None:
        with self._lock:
            self._gripper = max(0.0, min(100.0, position_pct))

    def close(self) -> None:
        self.stop()


class HardwareBackend:
    """Thin pylebai adapter that never starts/stops the controller or clears an estop.

    Motion/status calls use the installed SDK binding. ``stop_move`` is sent over
    a separate bounded JSON-RPC connection so a stalled SDK call cannot hold the
    bridge's software stop path behind the normal backend lock.
    """

    mode = "hardware"

    def __init__(self, config: RobotConfig) -> None:
        if config.pylebai_path:
            resolved = Path(config.pylebai_path).resolve()
            if not resolved.is_dir():
                raise RuntimeError(f"configured pylebai build artifact directory does not exist: {resolved}")
            path = str(resolved)
            if path not in sys.path:
                sys.path.insert(0, path)
        try:
            pylebai = importlib.import_module("pylebai")
        except ImportError as exc:
            raise RuntimeError(
                "pylebai is unavailable; install a built wheel/extension for this Python version or "
                "configure robot.pylebai_path to a build artifact containing l_master (the SDK source "
                "directory alone is not importable)"
            ) from exc
        robot_class = getattr(pylebai, "Robot", None)
        if not callable(robot_class):
            raise RuntimeError("pylebai does not expose a callable Robot class")
        self._robot = robot_class(config.robot_ip, simulator=False)
        self._base_locked = config.base_locked
        self._acceleration = config.cartesian_acceleration_mps2
        self._gripper_force = config.gripper_force_pct
        self._robot_ip = config.robot_ip
        self._emergency_stop_port = config.emergency_stop_port
        self._emergency_stop_timeout_ms = config.emergency_stop_timeout_ms

    def snapshot(self) -> RobotSnapshot:
        state_code = int(self._robot.get_robot_state())
        estop_raw = self._robot.get_estop_reason()
        joints = _six_values(self._robot.get_actual_joint_positions(), "j")
        speeds = _six_values(self._robot.get_actual_joint_speed(), "j")
        tcp = _pose(self._robot.get_actual_tcp_pose())
        gripper = _gripper_amplitude(self._robot.get_claw())
        return RobotSnapshot(
            robot_state=ROBOT_STATE_NAMES.get(state_code, f"UNKNOWN_{state_code}"),
            robot_state_code=state_code,
            estop_reason="" if estop_raw in (None, "", 0, "0") else str(estop_raw),
            joint_position_rad=joints,
            joint_velocity_rad_s=speeds,
            tcp_pose=tcp,
            gripper_pct=gripper,
            base_locked=self._base_locked,
        )

    def speed_cartesian(
        self,
        linear_mps: tuple[float, float, float],
        angular_rps: tuple[float, float, float],
        duration_ms: int,
    ) -> int:
        velocity = dict(
            zip(("x", "y", "z", "rx", "ry", "rz"), (*linear_mps, *angular_rps), strict=True)
        )
        return int(self._robot.speedl(self._acceleration, velocity, duration_ms / 1_000))

    def stop(self) -> None:
        _direct_stop_move(
            self._robot_ip,
            self._emergency_stop_port,
            self._emergency_stop_timeout_ms,
        )

    def set_gripper(self, position_pct: float) -> None:
        self._robot.set_claw(self._gripper_force, max(0.0, min(100.0, position_pct)))

    def close(self) -> None:
        self.stop()


def backend_ready(snapshot: RobotSnapshot, allowed_states: tuple[int, ...]) -> bool:
    return (
        snapshot.robot_state_code in allowed_states
        and not snapshot.estop_reason
        and snapshot.base_locked
    )


def _six_values(value: Any, prefix: str) -> list[float]:
    if isinstance(value, Mapping):
        candidates = [f"{prefix}{index}" for index in range(1, 7)]
        if all(key in value for key in candidates):
            result = [float(value[key]) for key in candidates]
        else:
            result = [float(item) for item in value.values()]
    else:
        result = [float(item) for item in value]
    if len(result) != 6 or not all(math.isfinite(item) for item in result):
        raise RuntimeError("Lebai backend returned an invalid six-axis vector")
    return result


def _pose(value: Any) -> dict[str, float]:
    axes = ("x", "y", "z", "rx", "ry", "rz")
    if isinstance(value, Mapping):
        result = {axis: float(value[axis]) for axis in axes}
    elif all(hasattr(value, axis) for axis in axes):
        result = {axis: float(getattr(value, axis)) for axis in axes}
    elif hasattr(value, "__getitem__"):
        try:
            result = {axis: float(value[axis]) for axis in axes}
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Lebai backend returned an invalid TCP pose") from exc
    else:
        try:
            values = [float(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Lebai backend returned an invalid TCP pose") from exc
        if len(values) != 6:
            raise RuntimeError("Lebai backend returned an invalid TCP pose")
        result = dict(zip(axes, values, strict=True))
    if not all(math.isfinite(item) for item in result.values()):
        raise RuntimeError("Lebai backend returned a non-finite TCP pose")
    return result


def _gripper_amplitude(value: Any) -> float:
    if isinstance(value, Mapping):
        for key in ("amplitude", "position", "opening"):
            if key in value:
                return _checked_gripper(value[key])
    for key in ("amplitude", "position", "opening"):
        if hasattr(value, key):
            return _checked_gripper(getattr(value, key))
    try:
        values = list(value) if not isinstance(value, (str, bytes)) else []
    except TypeError:
        values = []
    if len(values) >= 2:
        return _checked_gripper(values[1])
    raise RuntimeError("Lebai backend returned an invalid gripper value")


def _checked_gripper(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Lebai backend returned an invalid gripper value") from exc
    if not math.isfinite(result) or not (0.0 <= result <= 100.0):
        raise RuntimeError("Lebai backend returned an out-of-range gripper value")
    return result


def _direct_stop_move(host: str, port: int, timeout_ms: int) -> None:
    """Send stop_move through an independent HTTP JSON-RPC connection.

    This is a bounded software/network operation, not a hard real-time or physical
    emergency-stop guarantee. The caller must still enforce finite controller-side
    command durations and require an accessible physical estop.
    """

    timeout_s = timeout_ms / 1_000
    deadline = time.monotonic() + timeout_s
    request = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "stop_move", "params": []},
        separators=(",", ":"),
    )
    connection = http.client.HTTPConnection(host, port, timeout=timeout_s)
    try:
        connection.connect()
        _set_connection_deadline(connection, deadline)
        connection.request(
            "POST",
            "/jsonrpc",
            body=request.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        _set_connection_deadline(connection, deadline)
        response = connection.getresponse()
        _set_connection_deadline(connection, deadline)
        payload = response.read(65_537)
        if time.monotonic() > deadline:
            raise TimeoutError("stop_move JSON-RPC exceeded the configured deadline")
        if len(payload) > 65_536:
            raise RuntimeError("stop_move JSON-RPC response is unexpectedly large")
        if response.status != 200:
            raise RuntimeError(f"stop_move JSON-RPC returned HTTP {response.status}")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stop_move JSON-RPC returned invalid JSON") from exc
        if (
            not isinstance(value, dict)
            or value.get("jsonrpc") != "2.0"
            or value.get("id") != 1
            or "result" not in value
            or value.get("error") is not None
        ):
            raise RuntimeError("stop_move JSON-RPC returned an error response")
    finally:
        connection.close()


def _set_connection_deadline(connection: http.client.HTTPConnection, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("stop_move JSON-RPC exceeded the configured deadline")
    if connection.sock is None:
        raise RuntimeError("stop_move JSON-RPC connection did not create a socket")
    connection.sock.settimeout(remaining)
