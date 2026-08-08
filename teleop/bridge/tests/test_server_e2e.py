import asyncio
import json
import threading
import time
from pathlib import Path

from websockets.asyncio.client import connect

import pytest

from lm3_teleop_bridge.backends import RobotSnapshot, SimulatorBackend
from lm3_teleop_bridge.config import (
    AppConfig,
    RecordingConfig,
    RobotConfig,
    SafetyConfig,
    ServerConfig,
)
from lm3_teleop_bridge.protocol import PROTOCOL, Envelope, ProtocolError
from lm3_teleop_bridge.recorder import EpisodeRecorder
from lm3_teleop_bridge.server import ClientSession, TeleopServer


TOKEN = "test-token-0123456789"


class _MemoryWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, value: str) -> None:
        self.sent.append(value)


class _DelayedSpeedBackend:
    mode = "simulator"

    def __init__(self) -> None:
        self.speed_started = threading.Event()
        self.release_speed = threading.Event()
        self.speed_calls = 0
        self.stop_count = 0

    def snapshot(self) -> RobotSnapshot:
        return RobotSnapshot(
            robot_state="IDLE",
            robot_state_code=5,
            estop_reason="",
            joint_position_rad=[0.0] * 6,
            joint_velocity_rad_s=[0.0] * 6,
            tcp_pose={"x": 0.4, "y": 0.0, "z": 0.3, "rx": 0.0, "ry": 3.14, "rz": 0.0},
            gripper_pct=50.0,
            base_locked=True,
        )

    def speed_cartesian(self, linear_mps, angular_rps, duration_ms: int) -> int:
        self.speed_calls += 1
        self.speed_started.set()
        if not self.release_speed.wait(timeout=2.0):
            raise TimeoutError("test did not release speed call")
        return 1

    def stop(self) -> None:
        self.stop_count += 1

    def set_gripper(self, position_pct: float) -> None:
        return None

    def close(self) -> None:
        return None


class _FrozenFeedbackBackend:
    mode = "simulator"

    def __init__(self) -> None:
        self.speed_calls = 0
        self.stop_count = 0

    def snapshot(self) -> RobotSnapshot:
        return RobotSnapshot(
            robot_state="IDLE",
            robot_state_code=5,
            estop_reason="",
            joint_position_rad=[0.0] * 6,
            joint_velocity_rad_s=[0.0] * 6,
            tcp_pose={"x": 0.4, "y": 0.0, "z": 0.3, "rx": 0.0, "ry": 3.14, "rz": 0.0},
            gripper_pct=50.0,
            base_locked=True,
        )

    def speed_cartesian(self, linear_mps, angular_rps, duration_ms: int) -> int:
        self.speed_calls += 1
        return self.speed_calls

    def stop(self) -> None:
        self.stop_count += 1

    def set_gripper(self, position_pct: float) -> None:
        return None

    def close(self) -> None:
        return None


class _FailingStopBackend(_FrozenFeedbackBackend):
    def stop(self) -> None:
        self.stop_count += 1
        raise TimeoutError("stop channel timed out")


class _HardwareModeBackend(_FrozenFeedbackBackend):
    mode = "hardware"


def _message(message_type: str, seq: int, body: dict) -> str:
    return json.dumps(
        {
            "protocol": PROTOCOL,
            "type": message_type,
            "seq": seq,
            "sent_at_ms": int(time.time() * 1_000),
            "body": body,
        }
    )


async def _receive_type(websocket, expected: str, timeout: float = 2.0) -> dict:
    async def receive() -> dict:
        while True:
            value = json.loads(await websocket.recv())
            if value["type"] == expected:
                return value

    return await asyncio.wait_for(receive(), timeout)


def test_full_simulator_lease_motion_and_watchdog(tmp_path: Path) -> None:
    asyncio.run(_full_simulator_lease_motion_and_watchdog(tmp_path))


async def _full_simulator_lease_motion_and_watchdog(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0, state_hz=20),
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = SimulatorBackend(config.robot)
    server = TeleopServer(config, TOKEN, backend=backend, allow_ephemeral_port=True)
    await server.start()
    try:
        async with connect(f"ws://127.0.0.1:{server.bound_port}/ws") as websocket:
            await websocket.send(
                _message(
                    "session.hello",
                    0,
                    {
                        "client_id": "android-test",
                        "client_name": "pytest",
                        "platform": "android",
                        "app_version": "0.1.0",
                        "auth_token": TOKEN,
                        "capabilities": ["cartesian_velocity", "gripper", "recording"],
                    },
                )
            )
            welcome = await _receive_type(websocket, "session.welcome")
            assert welcome["seq"] == 0
            await _receive_type(websocket, "robot.state")

            await websocket.send(
                _message(
                    "control.acquire",
                    1,
                    {
                        "requested_lease_ms": 2_000,
                        "operator_hold_ms": 1_500,
                        "safety_ack": {
                            "base_stationary": True,
                            "workspace_clear": True,
                            "estop_accessible": True,
                            "tool_secure": True,
                        },
                    },
                )
            )
            control = await _receive_type(websocket, "control.status")
            lease_id = control["body"]["lease_id"]
            assert control["body"]["granted"] is True

            await websocket.send(
                _message(
                    "motion.cartesian_velocity",
                    2,
                    {
                        "lease_id": lease_id,
                        "deadman": True,
                        "frame": "base",
                        "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
                        "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
                        "duration_ms": 100,
                    },
                )
            )
            ack = await _receive_type(websocket, "ack")
            assert ack["body"]["accepted"] is True
            assert backend.last_command is not None

            event = await _receive_type(websocket, "safety.event", timeout=1.5)
            assert event["body"]["code"] == "WATCHDOG_TIMEOUT"
            assert backend.stop_count >= 1
    finally:
        await server.close()


def test_stale_sequence_stop_is_still_executed(tmp_path: Path) -> None:
    asyncio.run(_stale_sequence_stop_is_still_executed(tmp_path))


async def _stale_sequence_stop_is_still_executed(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0, state_hz=10),
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=10),
    )
    backend = SimulatorBackend(config.robot)
    server = TeleopServer(config, TOKEN, backend=backend, allow_ephemeral_port=True)
    await server.start()
    try:
        async with connect(f"ws://127.0.0.1:{server.bound_port}/ws") as websocket:
            await websocket.send(
                _message(
                    "session.hello",
                    0,
                    {
                        "client_id": "harmony-test",
                        "client_name": "pytest",
                        "platform": "HarmonyOS",
                        "app_version": "0.1.0",
                        "auth_token": TOKEN,
                        "capabilities": ["cartesian_velocity"],
                    },
                )
            )
            await _receive_type(websocket, "session.welcome")
            await websocket.send(_message("heartbeat", 1, {"deadman": False}))
            await _receive_type(websocket, "ack")
            before = backend.stop_count
            await websocket.send(_message("motion.stop", 1, {"reason": "duplicate_seq_stop"}))
            error = await _receive_type(websocket, "error")
            assert error["body"]["code"] == "OUT_OF_ORDER"
            assert backend.stop_count > before
    finally:
        await server.close()


def test_acquire_requires_official_idle_state(tmp_path: Path) -> None:
    asyncio.run(_acquire_requires_official_idle_state(tmp_path))


async def _acquire_requires_official_idle_state(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = SimulatorBackend(config.robot)
    backend.speed_cartesian((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1_000)
    websocket = _MemoryWebSocket()
    session = ClientSession(websocket=websocket)  # type: ignore[arg-type]
    session.authenticated = True
    session.client_id = "phone"
    server = TeleopServer(config, TOKEN, backend=backend)
    envelope = Envelope(
        "control.acquire",
        1,
        int(time.time() * 1_000),
        {
            "requested_lease_ms": 2_000,
            "operator_hold_ms": 1_500,
            "safety_ack": {
                "base_stationary": True,
                "workspace_clear": True,
                "estop_accessible": True,
                "tool_secure": True,
            },
        },
    )

    await server._control_acquire(session, envelope)

    response = json.loads(websocket.sent[-1])
    assert response["type"] == "control.status"
    assert response["body"]["granted"] is False
    assert response["body"]["reason"] == "robot_must_be_idle_ready_and_base_locked"


def test_safety_epoch_stops_inflight_command_from_becoming_active(tmp_path: Path) -> None:
    asyncio.run(_safety_epoch_stops_inflight_command_from_becoming_active(tmp_path))


async def _safety_epoch_stops_inflight_command_from_becoming_active(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = _DelayedSpeedBackend()
    server = TeleopServer(config, TOKEN, backend=backend)
    websocket = _MemoryWebSocket()
    session = ClientSession(websocket=websocket)  # type: ignore[arg-type]
    session.authenticated = True
    session.client_id = "phone"
    lease = server.leases.acquire(
        session_id=session.session_id,
        client_id=session.client_id,
        requested_ms=2_000,
    )
    assert lease is not None
    envelope = Envelope(
        "motion.cartesian_velocity",
        1,
        int(time.time() * 1_000),
        {
            "lease_id": lease.lease_id,
            "deadman": True,
            "frame": "base",
            "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
            "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
            "duration_ms": 100,
        },
    )
    motion = asyncio.create_task(server._motion_velocity(session, envelope))
    assert await asyncio.to_thread(backend.speed_started.wait, 1.0)

    await server._safe_stop("TEST_STOP", "test stop", revoke=True, stop_recording=True)
    backend.release_speed.set()

    with pytest.raises(ProtocolError, match="cancelled"):
        await motion
    assert backend.speed_calls == 1
    assert backend.stop_count >= 2
    assert server.leases.current is None
    assert server._motion_active is False


def test_authenticated_non_owner_stop_revokes_existing_lease(tmp_path: Path) -> None:
    asyncio.run(_authenticated_non_owner_stop_revokes_existing_lease(tmp_path))


async def _authenticated_non_owner_stop_revokes_existing_lease(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = SimulatorBackend(config.robot)
    server = TeleopServer(config, TOKEN, backend=backend)
    owner = ClientSession(websocket=_MemoryWebSocket())  # type: ignore[arg-type]
    owner.authenticated = True
    owner.client_id = "owner"
    owner.last_inbound_seq = 0
    outsider = ClientSession(websocket=_MemoryWebSocket())  # type: ignore[arg-type]
    outsider.authenticated = True
    outsider.client_id = "safety-observer"
    outsider.last_inbound_seq = 0
    server.sessions = {owner.session_id: owner, outsider.session_id: outsider}
    lease = server.leases.acquire(
        session_id=owner.session_id,
        client_id=owner.client_id,
        requested_ms=2_000,
    )
    assert lease is not None

    await server._handle_text(
        outsider,
        _message("motion.stop", 1, {"lease_id": "", "reason": "observer_stop"}),
    )

    assert backend.stop_count >= 1
    assert server.leases.current is None
    owner_messages = [json.loads(value) for value in owner.websocket.sent]  # type: ignore[attr-defined]
    assert any(
        value["type"] == "control.status"
        and value["body"]["reason"] == "external_stop"
        for value in owner_messages
    )


def test_external_stop_failure_is_not_acknowledged_as_success(tmp_path: Path) -> None:
    asyncio.run(_external_stop_failure_is_not_acknowledged_as_success(tmp_path))


async def _external_stop_failure_is_not_acknowledged_as_success(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = _FailingStopBackend()
    server = TeleopServer(config, TOKEN, backend=backend)
    owner = ClientSession(websocket=_MemoryWebSocket())  # type: ignore[arg-type]
    owner.authenticated = True
    owner.client_id = "owner"
    outsider_socket = _MemoryWebSocket()
    outsider = ClientSession(websocket=outsider_socket)  # type: ignore[arg-type]
    outsider.authenticated = True
    outsider.client_id = "safety-observer"
    outsider.last_inbound_seq = 0
    server.sessions = {owner.session_id: owner, outsider.session_id: outsider}
    lease = server.leases.acquire(
        session_id=owner.session_id,
        client_id=owner.client_id,
        requested_ms=2_000,
    )
    assert lease is not None

    await server._handle_text(
        outsider,
        _message("motion.stop", 1, {"reason": "observer_stop"}),
    )

    messages = [json.loads(value) for value in outsider_socket.sent]
    assert server.leases.current is None
    assert any(
        value["type"] == "safety.event"
        and value["body"]["code"] == "STOP_UNCONFIRMED"
        for value in messages
    )
    assert any(
        value["type"] == "error"
        and value["body"]["code"] == "BACKEND_ERROR"
        for value in messages
    )
    assert not any(value["type"] == "ack" for value in messages)


def test_continuous_motion_with_frozen_feedback_fails_closed(tmp_path: Path) -> None:
    asyncio.run(_continuous_motion_with_frozen_feedback_fails_closed(tmp_path))


async def _continuous_motion_with_frozen_feedback_fails_closed(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(state_hz=20),
        robot=RobotConfig(base_locked=True),
        safety=SafetyConfig(feedback_stall_ms=150),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = _FrozenFeedbackBackend()
    server = TeleopServer(config, TOKEN, backend=backend)
    websocket = _MemoryWebSocket()
    session = ClientSession(websocket=websocket)  # type: ignore[arg-type]
    session.authenticated = True
    session.client_id = "phone"
    server.sessions = {session.session_id: session}
    lease = server.leases.acquire(
        session_id=session.session_id,
        client_id=session.client_id,
        requested_ms=2_000,
    )
    assert lease is not None
    state_task = asyncio.create_task(server._state_loop())
    try:
        for seq in range(1, 10):
            if server.leases.current is None:
                break
            envelope = Envelope(
                "motion.cartesian_velocity",
                seq,
                int(time.time() * 1_000),
                {
                    "lease_id": lease.lease_id,
                    "deadman": True,
                    "frame": "base",
                    "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
                    "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
                    "duration_ms": 100,
                },
            )
            await server._motion_velocity(session, envelope)
            await asyncio.sleep(0.06)
        await asyncio.wait_for(_wait_for_lease_release(server), timeout=1.0)
    finally:
        state_task.cancel()
        await asyncio.gather(state_task, return_exceptions=True)

    assert backend.speed_calls >= 2
    assert backend.stop_count >= 1
    assert server.leases.current is None
    messages = [json.loads(value) for value in websocket.sent]
    assert any(
        value["type"] == "safety.event"
        and value["body"]["code"] == "FEEDBACK_STALLED"
        for value in messages
    )


async def _wait_for_lease_release(server: TeleopServer) -> None:
    while server.leases.current is not None:
        await asyncio.sleep(0.01)


def test_server_constructor_rejects_untruthful_recording_fps(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(state_hz=20),
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=10),
    )
    with pytest.raises(ValueError, match="must equal"):
        TeleopServer(config, TOKEN, backend=SimulatorBackend(config.robot))


def test_server_constructor_rejects_unsafe_robot_state_allowlist(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        safety=SafetyConfig(allowed_robot_states=(5, 11)),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    with pytest.raises(ValueError, match="exactly IDLE=5 and MOVING=7"):
        TeleopServer(config, TOKEN, backend=SimulatorBackend(config.robot))


def test_server_constructor_cannot_bypass_hardware_double_opt_in(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(
            backend="hardware",
            robot_ip="192.0.2.10",
            base_locked=True,
            hardware_enabled=True,
        ),
        safety=SafetyConfig(
            workspace_configured=True,
            joint_limits_configured=True,
        ),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    with pytest.raises(ValueError, match="--hardware CLI flag"):
        TeleopServer(config, TOKEN, backend=SimulatorBackend(config.robot))


def test_server_constructor_rejects_injected_recorder_fps_mismatch(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    recorder = EpisodeRecorder(tmp_path / "other", fps=10)
    with pytest.raises(ValueError, match="injected recorder.fps"):
        TeleopServer(
            config,
            TOKEN,
            backend=SimulatorBackend(config.robot),
            recorder=recorder,
        )


def test_server_constructor_rejects_injected_backend_mode_mismatch(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(backend="simulator", base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )

    with pytest.raises(ValueError, match="backend.mode must match robot.backend"):
        TeleopServer(config, TOKEN, backend=_HardwareModeBackend())


def test_server_constructor_rejects_short_programmatic_token(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )

    with pytest.raises(ValueError, match="at least 16 characters"):
        TeleopServer(config, "short", backend=SimulatorBackend(config.robot))
