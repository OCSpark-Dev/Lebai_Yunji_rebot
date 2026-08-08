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


class _PoseExecutionFailingBackend(_FrozenFeedbackBackend):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure

    def snapshot(self) -> RobotSnapshot:
        if self.failure == "snapshot":
            raise TimeoutError("pose snapshot failed")
        return super().snapshot()

    def speed_cartesian(self, linear_mps, angular_rps, duration_ms: int) -> int:
        self.speed_calls += 1
        if self.failure == "speed_exception":
            raise TimeoutError("pose speed command failed")
        if self.failure == "speed_zero":
            return 0
        return self.speed_calls


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


def _pose_body(
    lease_id: str,
    *,
    calibration_id: str = "calibration-a",
    sensor_timestamp_ms: int = 1_000,
    confidence: float = 0.95,
    angular_delta_rad: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> dict:
    return {
        "lease_id": lease_id,
        "deadman": True,
        "frame": "phone_calibrated",
        "mapping": "tcp_orientation",
        "calibration_id": calibration_id,
        "sensor_timestamp_ms": sensor_timestamp_ms,
        "tracking_state": "tracking",
        "confidence": confidence,
        "angular_delta_rad": dict(
            zip(("rx", "ry", "rz"), angular_delta_rad, strict=True)
        ),
    }


def _owned_session(
    server: TeleopServer, *, client_id: str = "phone"
) -> tuple[ClientSession, _MemoryWebSocket, str]:
    websocket = _MemoryWebSocket()
    session = ClientSession(websocket=websocket)  # type: ignore[arg-type]
    session.authenticated = True
    session.client_id = client_id
    session.last_inbound_seq = 0
    server.sessions = {session.session_id: session}
    lease = server.leases.acquire(
        session_id=session.session_id,
        client_id=session.client_id,
        requested_ms=2_000,
    )
    assert lease is not None
    return session, websocket, lease.lease_id


def _decoded_messages(websocket: _MemoryWebSocket) -> list[dict]:
    return [json.loads(value) for value in websocket.sent]


async def _prime_pose(
    server: TeleopServer,
    session: ClientSession,
    lease_id: str,
    *,
    seq: int = 1,
    calibration_id: str = "calibration-a",
    sensor_timestamp_ms: int = 1_000,
) -> None:
    await server._handle_text(
        session,
        _message(
            "pose.sample",
            seq,
            _pose_body(
                lease_id,
                calibration_id=calibration_id,
                sensor_timestamp_ms=sensor_timestamp_ms,
            ),
        ),
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


def test_pose_sample_websocket_e2e_controls_only_tcp_orientation(tmp_path: Path) -> None:
    asyncio.run(_pose_sample_websocket_e2e_controls_only_tcp_orientation(tmp_path))


async def _pose_sample_websocket_e2e_controls_only_tcp_orientation(tmp_path: Path) -> None:
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
                        "client_id": "android-pose-e2e",
                        "client_name": "pytest",
                        "platform": "android",
                        "app_version": "0.1.0",
                        "auth_token": TOKEN,
                        "capabilities": ["cartesian_velocity", "pose_sample"],
                    },
                )
            )
            await _receive_type(websocket, "session.welcome")
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

            await websocket.send(
                _message("pose.sample", 2, _pose_body(lease_id, sensor_timestamp_ms=1_000))
            )
            priming_ack = await _receive_type(websocket, "ack")
            assert priming_ack["body"]["ack_type"] == "pose.sample"
            assert "no motion executed" in priming_ack["body"]["detail"]
            assert backend.last_command is None
            await asyncio.sleep(0.06)

            await websocket.send(
                _message(
                    "pose.sample",
                    3,
                    _pose_body(
                        lease_id,
                        sensor_timestamp_ms=1_100,
                        angular_delta_rad=(0.005, 0.0, 0.0),
                    ),
                )
            )
            motion_ack = await _receive_type(websocket, "ack")
            assert motion_ack["body"]["ack_type"] == "pose.sample"
            assert motion_ack["body"]["accepted"] is True
            assert backend.last_command is not None
            assert backend.last_command["linear_mps"] == [0.0, 0.0, 0.0]
            assert backend.last_command["angular_rps"] == pytest.approx([0.05, 0.0, 0.0])
            assert backend.last_command["duration_ms"] == 100
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
            try:
                await server._motion_velocity(session, envelope)
            except ProtocolError as error:
                assert error.code == "LEASE_REQUIRED"
                break
            await asyncio.sleep(0.06)
        await asyncio.wait_for(_wait_for_lease_release(server), timeout=1.0)
        await asyncio.wait_for(
            _wait_for_safety_event(websocket, "FEEDBACK_STALLED"), timeout=1.0
        )
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


async def _wait_for_safety_event(websocket: _MemoryWebSocket, code: str) -> None:
    while not any(
        message["type"] == "safety.event" and message["body"]["code"] == code
        for message in _decoded_messages(websocket)
    ):
        await asyncio.sleep(0.01)


def test_pose_sample_first_frame_primes_without_motion(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = SimulatorBackend(config.robot)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)

        await _prime_pose(server, session, lease_id)

        assert backend.last_command is None
        assert backend.stop_count == 0
        assert server._motion_active is False
        assert server.leases.current is not None
        state = server._pose_states[session.session_id]
        assert state.calibration_id == "calibration-a"
        assert state.last_sensor_timestamp_ms == 1_000
        assert server._last_command == {
            "type": "pose.sample",
            "client_seq": 1,
            "sent_at_ms": server._last_command["sent_at_ms"],
            "received_at_ms": server._last_command["received_at_ms"],
            "network_age_ms": server._last_command["network_age_ms"],
            "deadman": True,
            "linear_mps": [0.0, 0.0, 0.0],
            "angular_rps": [0.0, 0.0, 0.0],
            "duration_ms": 0,
            "clamped": False,
            "priming": True,
            "frame": "phone_calibrated",
            "mapping": "tcp_orientation",
            "calibration_id": "calibration-a",
            "sensor_timestamp_ms": 1_000,
            "sensor_interval_ms": None,
            "tracking_state": "tracking",
            "confidence": 0.95,
            "angular_delta_rad": [0.0, 0.0, 0.0],
            "input_angular_rps": [0.0, 0.0, 0.0],
        }
        ack = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "ack" and message["body"]["ack_seq"] == 1
        )
        assert ack["body"]["accepted"] is True
        assert ack["body"]["clamped"] is False
        assert "no motion executed" in ack["body"]["detail"]

    asyncio.run(scenario())


def test_pose_sample_nonzero_priming_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = SimulatorBackend(config.robot)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                1,
                _pose_body(lease_id, angular_delta_rad=(0.001, 0.0, 0.0)),
            ),
        )

        error = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "error"
        )
        assert error["body"]["code"] == "INVALID_MESSAGE"
        assert "zero angular delta" in error["body"]["message"]
        assert backend.last_command is None
        assert backend.stop_count >= 1
        assert server.leases.current is None
        assert server._pose_states == {}

    asyncio.run(scenario())


def test_pose_sample_second_frame_drives_only_clamped_tcp_rotation(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = SimulatorBackend(config.robot)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        await asyncio.sleep(0.06)

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                2,
                _pose_body(
                    lease_id,
                    sensor_timestamp_ms=1_100,
                    angular_delta_rad=(0.012, 0.016, 0.0),
                ),
            ),
        )

        assert backend.last_command is not None
        assert backend.last_command["linear_mps"] == [0.0, 0.0, 0.0]
        assert backend.last_command["angular_rps"] == pytest.approx([0.09, 0.12, 0.0])
        assert backend.last_command["duration_ms"] == 100
        assert server._last_command["type"] == "pose.sample"
        assert server._last_command["priming"] is False
        assert server._last_command["sensor_interval_ms"] == 100
        assert server._last_command["angular_delta_rad"] == pytest.approx(
            [0.012, 0.016, 0.0]
        )
        assert server._last_command["input_angular_rps"] == pytest.approx(
            [0.12, 0.16, 0.0]
        )
        assert server._last_command["angular_rps"] == pytest.approx([0.09, 0.12, 0.0])
        assert server._last_command["clamped"] is True
        assert server._pose_states[session.session_id].last_sensor_timestamp_ms == 1_100
        ack = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "ack" and message["body"]["ack_seq"] == 2
        )
        assert ack["body"]["accepted"] is True
        assert ack["body"]["clamped"] is True

    asyncio.run(scenario())


def test_pose_sample_new_calibration_stops_and_primes_without_releasing_lease(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _FrozenFeedbackBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        await asyncio.sleep(0.06)
        await server._handle_text(
            session,
            _message(
                "pose.sample",
                2,
                _pose_body(
                    lease_id,
                    sensor_timestamp_ms=1_100,
                    angular_delta_rad=(0.005, 0.0, 0.0),
                ),
            ),
        )
        assert backend.speed_calls == 1
        assert server._motion_active is True
        await asyncio.sleep(0.06)

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                3,
                _pose_body(
                    lease_id,
                    calibration_id="calibration-b",
                    sensor_timestamp_ms=5_000,
                ),
            ),
        )

        assert backend.speed_calls == 1
        assert backend.stop_count == 1
        assert server._motion_active is False
        assert server.leases.current is not None
        assert server.leases.current.lease_id == lease_id
        state = server._pose_states[session.session_id]
        assert state.calibration_id == "calibration-b"
        assert state.last_sensor_timestamp_ms == 5_000
        assert server._last_command["priming"] is True
        ack = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "ack" and message["body"]["ack_seq"] == 3
        )
        assert "primed" in ack["body"]["detail"]

    asyncio.run(scenario())


def test_pose_sample_calibration_priming_cannot_bypass_motion_rate_limit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = SimulatorBackend(config.robot)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        websocket.sent.clear()
        session.motion_bucket.tokens = 0.0
        session.motion_bucket.last = time.monotonic()

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                2,
                _pose_body(
                    lease_id,
                    calibration_id="calibration-b",
                    sensor_timestamp_ms=2_000,
                ),
            ),
        )

        error = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "error"
        )
        assert error["body"]["code"] == "RATE_LIMITED"
        assert backend.last_command is None
        assert backend.stop_count >= 1
        assert server.leases.current is None
        assert server._pose_states == {}

    asyncio.run(scenario())


def test_pose_sample_state_is_cleared_when_commands_are_invalidated(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        server = TeleopServer(config, TOKEN, backend=SimulatorBackend(config.robot))
        session, _, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        previous_epoch = server._safety_epoch

        server._invalidate_commands()

        assert server._pose_states == {}
        assert server._safety_epoch == previous_epoch + 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("missing_field", "INVALID_MESSAGE"),
        ("extra_field", "INVALID_MESSAGE"),
        ("deadman", "DEADMAN_REQUIRED"),
        ("frame", "UNSUPPORTED_MODE"),
        ("mapping", "UNSUPPORTED_MODE"),
        ("tracking", "INVALID_MESSAGE"),
        ("confidence_low", "INVALID_MESSAGE"),
        ("confidence_high", "INVALID_MESSAGE"),
        ("calibration_whitespace", "INVALID_MESSAGE"),
        ("vector_extra_axis", "INVALID_MESSAGE"),
        ("wrong_lease", "LEASE_REQUIRED"),
    ],
)
def test_pose_sample_schema_errors_fail_closed(
    tmp_path: Path, case: str, expected_code: str
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / case / "raw", fps=20),
        )
        backend = SimulatorBackend(config.robot)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        websocket.sent.clear()
        body = _pose_body(lease_id, sensor_timestamp_ms=1_100)
        if case == "missing_field":
            del body["confidence"]
        elif case == "extra_field":
            body["position_m"] = {"x": 0.0, "y": 0.0, "z": 0.0}
        elif case == "deadman":
            body["deadman"] = False
        elif case == "frame":
            body["frame"] = "base"
        elif case == "mapping":
            body["mapping"] = "joint_orientation"
        elif case == "tracking":
            body["tracking_state"] = "limited"
        elif case == "confidence_low":
            body["confidence"] = 0.79
        elif case == "confidence_high":
            body["confidence"] = 1.01
        elif case == "calibration_whitespace":
            body["calibration_id"] = " calibration-a "
        elif case == "vector_extra_axis":
            body["angular_delta_rad"]["w"] = 1.0
        elif case == "wrong_lease":
            body["lease_id"] = "not-the-owned-lease"
        else:  # pragma: no cover - protects the parameter table from drift
            raise AssertionError(f"unknown test case: {case}")

        await server._handle_text(session, _message("pose.sample", 2, body))

        errors = [
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "error"
        ]
        assert errors[-1]["body"]["code"] == expected_code
        assert server.leases.current is None
        assert server._pose_states == {}
        assert backend.stop_count >= 1

    asyncio.run(scenario())


@pytest.mark.parametrize(("field", "value"), [("confidence", float("inf")), ("rx", float("nan"))])
def test_pose_sample_non_finite_numbers_fail_closed(
    tmp_path: Path, field: str, value: float
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / field / "raw", fps=20),
        )
        backend = SimulatorBackend(config.robot)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        websocket.sent.clear()
        body = _pose_body(lease_id, sensor_timestamp_ms=1_100)
        if field == "confidence":
            body["confidence"] = value
        else:
            body["angular_delta_rad"][field] = value

        await server._handle_text(session, _message("pose.sample", 2, body))

        error = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "error"
        )
        assert error["body"]["code"] == "INVALID_MESSAGE"
        assert server.leases.current is None
        assert server._pose_states == {}
        assert backend.stop_count >= 1

    asyncio.run(scenario())


@pytest.mark.parametrize("sensor_timestamp_ms", [1_000, 999, 1_019, 1_151])
def test_pose_sample_timestamp_or_interval_errors_fail_closed(
    tmp_path: Path, sensor_timestamp_ms: int
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(
                root=tmp_path / str(sensor_timestamp_ms) / "raw", fps=20
            ),
        )
        backend = SimulatorBackend(config.robot)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        websocket.sent.clear()

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                2,
                _pose_body(
                    lease_id,
                    sensor_timestamp_ms=sensor_timestamp_ms,
                ),
            ),
        )

        error = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "error"
        )
        assert error["body"]["code"] == "INVALID_MESSAGE"
        assert server.leases.current is None
        assert server._pose_states == {}
        assert backend.stop_count >= 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("sensor_timestamp_ms", "angular_delta_rad", "message_fragment"),
    [
        (1_100, (0.251, 0.0, 0.0), "single-frame jump"),
        (1_020, (0.13, 0.0, 0.0), "input limit"),
    ],
)
def test_pose_sample_jump_and_input_velocity_limits_fail_closed(
    tmp_path: Path,
    sensor_timestamp_ms: int,
    angular_delta_rad: tuple[float, float, float],
    message_fragment: str,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(
                root=tmp_path / str(sensor_timestamp_ms) / "raw", fps=20
            ),
        )
        backend = SimulatorBackend(config.robot)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        websocket.sent.clear()

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                2,
                _pose_body(
                    lease_id,
                    sensor_timestamp_ms=sensor_timestamp_ms,
                    angular_delta_rad=angular_delta_rad,
                ),
            ),
        )

        error = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "error"
        )
        assert error["body"]["code"] == "INVALID_MESSAGE"
        assert message_fragment in error["body"]["message"]
        assert server.leases.current is None
        assert server._pose_states == {}
        assert backend.stop_count >= 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["snapshot", "speed_exception", "speed_zero"])
def test_pose_sample_execution_errors_stop_revoke_and_clear_state(
    tmp_path: Path, failure: str
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / failure / "raw", fps=20),
        )
        backend = _PoseExecutionFailingBackend(failure)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        await asyncio.sleep(0.06)
        websocket.sent.clear()

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                2,
                _pose_body(
                    lease_id,
                    sensor_timestamp_ms=1_100,
                    angular_delta_rad=(0.005, 0.0, 0.0),
                ),
            ),
        )

        error = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "error"
        )
        assert error["body"]["code"] == "BACKEND_ERROR"
        assert error["body"]["recoverable"] is False
        assert server.leases.current is None
        assert server._pose_states == {}
        assert server._motion_active is False
        assert backend.stop_count >= 1

    asyncio.run(scenario())


def test_cartesian_velocity_regression_after_shared_pose_execution_refactor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = SimulatorBackend(config.robot)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)

        await server._handle_text(
            session,
            _message(
                "motion.cartesian_velocity",
                1,
                {
                    "lease_id": lease_id,
                    "deadman": True,
                    "frame": "base",
                    "linear_mps": {"x": 0.01, "y": -0.02, "z": 0.0},
                    "angular_rps": {"rx": 0.1, "ry": 0.0, "rz": -0.05},
                    "duration_ms": 100,
                },
            ),
        )

        assert backend.last_command == {
            "linear_mps": [0.01, -0.02, 0.0],
            "angular_rps": [0.1, 0.0, -0.05],
            "duration_ms": 100,
        }
        assert server._last_command["type"] == "motion.cartesian_velocity"
        assert "priming" not in server._last_command
        assert server._pose_states == {}
        ack = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "ack"
        )
        assert ack["body"]["accepted"] is True
        assert ack["body"]["clamped"] is False

    asyncio.run(scenario())


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
