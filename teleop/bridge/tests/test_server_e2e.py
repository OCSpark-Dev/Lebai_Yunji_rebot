import asyncio
import json
import math
import threading
import time
from pathlib import Path

from websockets.asyncio.client import connect

import pytest

from lm3_teleop_bridge.backends import RobotSnapshot, SimulatorBackend
from lm3_teleop_bridge.camera import NullCameraProvider
from lm3_teleop_bridge.config import (
    AppConfig,
    CameraConfig,
    RecordingConfig,
    RobotConfig,
    SafetyConfig,
    ServerConfig,
)
from lm3_teleop_bridge.protocol import PROTOCOL, Envelope, ProtocolError
from lm3_teleop_bridge.recorder import EpisodeRecorder
from lm3_teleop_bridge.server import ClientSession, TeleopServer


TOKEN = "test-token-0123456789"
# The 20 Hz bucket refills every 50 ms; functional tests keep scheduler margin.
MOTION_COMMAND_TEST_SPACING_S = 0.10


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
        self.gripper_calls = 0

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
        self.gripper_calls += 1

    def close(self) -> None:
        return None


class _MutableFeedbackBackend(_FrozenFeedbackBackend):
    def __init__(self, tcp_pose: dict[str, float] | None = None) -> None:
        super().__init__()
        self.tcp_pose = tcp_pose or {
            "x": 0.4,
            "y": 0.0,
            "z": 0.3,
            "rx": 0.0,
            "ry": 3.14,
            "rz": 0.0,
        }

    def snapshot(self) -> RobotSnapshot:
        return RobotSnapshot(
            robot_state="IDLE",
            robot_state_code=5,
            estop_reason="",
            joint_position_rad=[0.0] * 6,
            joint_velocity_rad_s=[0.0] * 6,
            tcp_pose=dict(self.tcp_pose),
            gripper_pct=50.0,
            base_locked=True,
        )


class _BlockingSnapshotBackend(_MutableFeedbackBackend):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_started = threading.Event()
        self.release_snapshot = threading.Event()

    def snapshot(self) -> RobotSnapshot:
        self.snapshot_started.set()
        if not self.release_snapshot.wait(timeout=2.0):
            raise TimeoutError("test did not release snapshot")
        return super().snapshot()


class _OrderedSnapshotBackend(_MutableFeedbackBackend):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_started = threading.Event()
        self.release_first_snapshot = threading.Event()
        self.snapshot_calls = 0
        self.operation_order: list[str] = []

    def snapshot(self) -> RobotSnapshot:
        self.snapshot_calls += 1
        self.operation_order.append(f"snapshot-{self.snapshot_calls}")
        if self.snapshot_calls == 1:
            self.snapshot_started.set()
            if not self.release_first_snapshot.wait(timeout=2.0):
                raise TimeoutError("test did not release first snapshot")
        return super().snapshot()

    def speed_cartesian(self, linear_mps, angular_rps, duration_ms: int) -> int:
        self.operation_order.append("speed")
        return super().speed_cartesian(linear_mps, angular_rps, duration_ms)


class _CountingSnapshotBackend(_MutableFeedbackBackend):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_calls = 0

    def snapshot(self) -> RobotSnapshot:
        self.snapshot_calls += 1
        return super().snapshot()


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
        self.snapshot_calls = 0

    def snapshot(self) -> RobotSnapshot:
        self.snapshot_calls += 1
        if self.failure == "snapshot" and self.snapshot_calls >= 2:
            raise TimeoutError("pose snapshot failed")
        return super().snapshot()

    def speed_cartesian(self, linear_mps, angular_rps, duration_ms: int) -> int:
        self.speed_calls += 1
        if self.failure == "speed_exception":
            raise TimeoutError("pose speed command failed")
        if self.failure == "speed_zero":
            return 0
        return self.speed_calls


def _message(
    message_type: str,
    seq: int,
    body: dict,
    *,
    sent_at_ms: int | None = None,
) -> str:
    return json.dumps(
        {
            "protocol": PROTOCOL,
            "type": message_type,
            "seq": seq,
            "sent_at_ms": int(time.time() * 1_000) if sent_at_ms is None else sent_at_ms,
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
    session.hello_complete = True
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
            if value["type"] == "error":
                raise AssertionError(
                    f"expected server frame {expected!r}, received protocol error: {value!r}"
                )

    return await asyncio.wait_for(receive(), timeout)


def test_successful_handshake_first_server_frame_is_welcome_seq_zero(tmp_path: Path) -> None:
    asyncio.run(_successful_handshake_first_server_frame_is_welcome_seq_zero(tmp_path))


async def _successful_handshake_first_server_frame_is_welcome_seq_zero(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0, state_hz=50),
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=50),
    )
    server = TeleopServer(
        config,
        backend=SimulatorBackend(config.robot),
        allow_ephemeral_port=True,
    )
    await server.start()
    try:
        async with connect(f"ws://127.0.0.1:{server.bound_port}/ws") as websocket:
            await websocket.send(
                _message(
                    "session.hello",
                    0,
                    {
                        "client_id": "android-first-frame-test",
                        "client_name": "pytest",
                        "platform": "android",
                        "app_version": "0.1.0",
                        "capabilities": ["cartesian_velocity"],
                    },
                )
            )
            first_frame = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2.0))
            assert first_frame["type"] == "session.welcome"
            assert first_frame["seq"] == 0
            limits = first_frame["body"]["limits"]
            assert limits["orientation_configured"] is False
            assert limits["orientation_center_rad"] == [0.0, 0.0, 0.0]
            assert limits["orientation_tolerance_rad"] == [0.05, 0.05, 0.05]
            assert limits["orientation_gimbal_lock_margin_rad"] == pytest.approx(0.1)
    finally:
        await server.close()


def test_first_non_hello_frame_returns_hello_required_seq_zero(tmp_path: Path) -> None:
    asyncio.run(_first_non_hello_frame_returns_hello_required_seq_zero(tmp_path))


async def _first_non_hello_frame_returns_hello_required_seq_zero(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0, state_hz=50),
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=50),
    )
    server = TeleopServer(
        config,
        backend=SimulatorBackend(config.robot),
        allow_ephemeral_port=True,
    )
    await server.start()
    try:
        async with connect(f"ws://127.0.0.1:{server.bound_port}/ws") as websocket:
            await websocket.send(_message("heartbeat", 0, {"deadman": False}))
            first_frame = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2.0))
            assert first_frame["type"] == "error"
            assert first_frame["seq"] == 0
            assert first_frame["body"]["ack_seq"] == 0
            assert first_frame["body"]["code"] == "HELLO_REQUIRED"
            assert first_frame["body"]["recoverable"] is False
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("clock_skew_ms", "message_fragment"),
    [
        (-60_000, "older than"),
        (60_000, "future"),
    ],
)
@pytest.mark.parametrize(
    ("next_message_type", "next_body"),
    [
        ("heartbeat", {"deadman": False}),
        (
            "control.acquire",
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
        ),
    ],
)
def test_hello_accepts_wall_clock_skew_but_next_message_remains_strict(
    tmp_path: Path,
    clock_skew_ms: int,
    message_fragment: str,
    next_message_type: str,
    next_body: dict,
) -> None:
    asyncio.run(
        _hello_accepts_wall_clock_skew_but_next_message_remains_strict(
            tmp_path,
            clock_skew_ms,
            message_fragment,
            next_message_type,
            next_body,
        )
    )


async def _hello_accepts_wall_clock_skew_but_next_message_remains_strict(
    tmp_path: Path,
    clock_skew_ms: int,
    message_fragment: str,
    next_message_type: str,
    next_body: dict,
) -> None:
    config = AppConfig(
        server=ServerConfig(
            host="127.0.0.1",
            port=0,
            state_hz=50,
            max_message_age_ms=500,
            max_future_skew_ms=500,
        ),
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=50),
    )
    server = TeleopServer(
        config,
        backend=SimulatorBackend(config.robot),
        allow_ephemeral_port=True,
    )
    await server.start()
    try:
        async with connect(f"ws://127.0.0.1:{server.bound_port}/ws") as websocket:
            skewed_sent_at_ms = int(time.time() * 1_000) + clock_skew_ms
            await websocket.send(
                _message(
                    "session.hello",
                    0,
                    {
                        "client_id": "clock-skew-test",
                        "client_name": "pytest",
                        "platform": "android",
                        "app_version": "0.1.0",
                        "capabilities": ["cartesian_velocity"],
                    },
                    sent_at_ms=skewed_sent_at_ms,
                )
            )
            first_frame = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2.0))
            assert first_frame["type"] == "session.welcome"
            assert first_frame["seq"] == 0
            assert server.leases.current is None

            await websocket.send(
                _message(
                    next_message_type,
                    1,
                    next_body,
                    sent_at_ms=skewed_sent_at_ms,
                )
            )
            error = await _receive_type(websocket, "error")
            assert error["body"]["code"] == "STALE_MESSAGE"
            assert error["body"]["ack_seq"] == 1
            assert message_fragment in error["body"]["message"]
            assert server.leases.current is None
    finally:
        await server.close()


@pytest.mark.parametrize("legacy_token", ["", "wrong-token"])
def test_legacy_auth_token_is_ignored(tmp_path: Path, legacy_token: str) -> None:
    asyncio.run(_legacy_auth_token_is_ignored(tmp_path, legacy_token))


async def _legacy_auth_token_is_ignored(tmp_path: Path, legacy_token: str) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0, state_hz=50),
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=50),
    )
    server = TeleopServer(
        config,
        TOKEN,
        backend=SimulatorBackend(config.robot),
        allow_ephemeral_port=True,
    )
    await server.start()
    try:
        async with connect(f"ws://127.0.0.1:{server.bound_port}/ws") as websocket:
            await websocket.send(
                _message(
                    "session.hello",
                    0,
                    {
                        "client_id": "android-auth-failure-test",
                        "client_name": "pytest",
                        "platform": "android",
                        "app_version": "0.1.0",
                        "auth_token": legacy_token,
                        "capabilities": ["cartesian_velocity"],
                    },
                )
            )
            first_frame = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2.0))
            assert first_frame["type"] == "session.welcome"
            assert first_frame["seq"] == 0
    finally:
        await server.close()


def test_full_simulator_lease_motion_and_watchdog(tmp_path: Path) -> None:
    asyncio.run(_full_simulator_lease_motion_and_watchdog(tmp_path))


def test_short_lease_reports_renewed_expiry_before_half_life(tmp_path: Path) -> None:
    asyncio.run(_short_lease_reports_renewed_expiry_before_half_life(tmp_path))


def test_short_lease_stays_valid_at_android_heartbeat_cadence(tmp_path: Path) -> None:
    asyncio.run(_short_lease_stays_valid_at_android_heartbeat_cadence(tmp_path))


async def _short_lease_reports_renewed_expiry_before_half_life(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(lease_ms=500),
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    server = TeleopServer(config, TOKEN, backend=SimulatorBackend(config.robot))
    session, websocket, lease_id = _owned_session(server)
    lease = server.leases.current
    assert lease is not None
    assert lease.duration_ms == 500

    session.last_lease_status_monotonic = time.monotonic() - 0.24
    await server._maybe_send_lease_status(session, force=False)
    assert websocket.sent == []

    assert server.leases.renew(session.session_id, lease_id) is lease
    session.last_lease_status_monotonic = time.monotonic() - 0.26
    await server._maybe_send_lease_status(session, force=False)
    messages = _decoded_messages(websocket)
    assert len(messages) == 1
    assert messages[0]["type"] == "control.status"
    assert messages[0]["body"]["granted"] is True
    assert messages[0]["body"]["lease_id"] == lease_id
    assert messages[0]["body"]["reason"] == "renewed"


async def _short_lease_stays_valid_at_android_heartbeat_cadence(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0, state_hz=20, lease_ms=500),
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    server = TeleopServer(
        config,
        TOKEN,
        backend=SimulatorBackend(config.robot),
        allow_ephemeral_port=True,
    )
    await server.start()
    try:
        async with connect(
            f"ws://127.0.0.1:{server.bound_port}/ws",
            proxy=None,
            compression=None,
        ) as websocket:
            await websocket.send(
                _message(
                    "session.hello",
                    0,
                    {
                        "client_id": "android-short-lease-test",
                        "client_name": "pytest",
                        "platform": "android",
                        "app_version": "0.1.0",
                        "auth_token": TOKEN,
                        "capabilities": [],
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
            granted = await _receive_type(websocket, "control.status")
            lease_id = granted["body"]["lease_id"]
            client_deadline_ms = granted["body"]["expires_at_ms"]
            renewed_count = 0
            sequence = 2
            started = time.monotonic()

            while time.monotonic() - started < 1.5:
                await asyncio.sleep(0.15)
                assert int(time.time() * 1_000) < client_deadline_ms
                await websocket.send(
                    _message(
                        "heartbeat",
                        sequence,
                        {"lease_id": lease_id, "deadman": False},
                    )
                )
                while True:
                    message = json.loads(
                        await asyncio.wait_for(websocket.recv(), timeout=0.4)
                    )
                    if message["type"] == "error":
                        raise AssertionError(f"heartbeat failed: {message!r}")
                    if message["type"] == "control.status":
                        assert message["body"]["granted"] is True
                        assert message["body"]["lease_id"] == lease_id
                        assert message["body"]["reason"] == "renewed"
                        client_deadline_ms = message["body"]["expires_at_ms"]
                        renewed_count += 1
                    if (
                        message["type"] == "ack"
                        and message["body"]["ack_seq"] == sequence
                    ):
                        break
                sequence += 1

            assert renewed_count >= 4
            assert int(time.time() * 1_000) < client_deadline_ms
    finally:
        await server.close()


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
            await asyncio.sleep(MOTION_COMMAND_TEST_SPACING_S)

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
    session.hello_complete = True
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


@pytest.mark.parametrize(
    ("tcp_pose", "safety"),
    [
        (
            {"x": 0.81, "y": 0.0, "z": 0.3, "rx": 0.0, "ry": 3.14, "rz": 0.0},
            SafetyConfig(),
        ),
        (
            {"x": 0.4, "y": 0.0, "z": 0.3, "rx": 0.2, "ry": 3.14, "rz": 0.0},
            SafetyConfig(
                orientation_configured=True,
                orientation_center_rad=(0.0, 3.14, 0.0),
                orientation_tolerance_rad=(0.1, 0.1, 0.1),
            ),
        ),
    ],
)
def test_acquire_rejects_robot_outside_motion_envelope(
    tmp_path: Path,
    tcp_pose: dict[str, float],
    safety: SafetyConfig,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            safety=safety,
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _MutableFeedbackBackend(tcp_pose)
        server = TeleopServer(config, TOKEN, backend=backend)
        websocket = _MemoryWebSocket()
        session = ClientSession(websocket=websocket)  # type: ignore[arg-type]
        session.hello_complete = True
        session.client_id = "phone"
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
        assert response["body"]["reason"] == "robot_not_within_configured_motion_envelope"
        assert server.leases.current is None
        assert backend.stop_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tcp_pose", "safety", "expected_code"),
    [
        (
            {"x": 0.81, "y": 0.0, "z": 0.3, "rx": 0.0, "ry": 3.14, "rz": 0.0},
            SafetyConfig(),
            "WORKSPACE_LIMIT",
        ),
        (
            {"x": 0.4, "y": 0.0, "z": 0.3, "rx": 0.2, "ry": 3.14, "rz": 0.0},
            SafetyConfig(
                orientation_configured=True,
                orientation_center_rad=(0.0, 3.14, 0.0),
                orientation_tolerance_rad=(0.1, 0.1, 0.1),
            ),
            "ORIENTATION_LIMIT",
        ),
    ],
)
def test_gripper_rejects_current_tcp_outside_motion_envelope(
    tmp_path: Path,
    tcp_pose: dict[str, float],
    safety: SafetyConfig,
    expected_code: str,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            safety=safety,
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _MutableFeedbackBackend(tcp_pose)
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)

        await server._handle_text(
            session,
            _message(
                "gripper.set",
                1,
                {
                    "lease_id": lease_id,
                    "deadman": True,
                    "position_pct": 50.0,
                },
            ),
        )

        messages = _decoded_messages(websocket)
        error = next(message for message in messages if message["type"] == "error")
        assert error["body"]["code"] == expected_code
        assert error["body"]["ack_seq"] == 1
        assert any(
            message["type"] == "safety.event"
            and message["body"]["code"] == expected_code
            for message in messages
        )
        assert any(
            message["type"] == "control.status"
            and message["body"]["granted"] is False
            and message["body"]["reason"] == expected_code.lower()
            for message in messages
        )
        assert backend.gripper_calls == 0
        assert backend.stop_count >= 1
        assert server.leases.current is None

    asyncio.run(scenario())


def test_acquire_cannot_cross_safety_stop_during_snapshot(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _BlockingSnapshotBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        websocket = _MemoryWebSocket()
        session = ClientSession(websocket=websocket)  # type: ignore[arg-type]
        session.hello_complete = True
        session.client_id = "phone"
        server.sessions = {session.session_id: session}
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

        acquire_task = asyncio.create_task(server._control_acquire(session, envelope))
        assert await asyncio.to_thread(backend.snapshot_started.wait, 1.0)
        await server._safe_stop(
            "EXTERNAL_STOP",
            "test stop during readiness snapshot",
            revoke=True,
            stop_recording=True,
        )
        backend.release_snapshot.set()
        await acquire_task

        messages = _decoded_messages(websocket)
        assert server.leases.current is None
        assert not any(
            message["type"] == "control.status" and message["body"]["granted"] is True
            for message in messages
        )
        assert any(
            message["type"] == "control.status"
            and message["body"]["reason"] == "safety_stop_in_progress"
            for message in messages
        )

    asyncio.run(scenario())


def test_pose_priming_cannot_write_state_after_concurrent_safety_stop(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _BlockingSnapshotBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)

        priming_task = asyncio.create_task(
            server._handle_text(
                session,
                _message("pose.sample", 1, _pose_body(lease_id)),
            )
        )
        assert await asyncio.to_thread(backend.snapshot_started.wait, 1.0)
        await server._safe_stop(
            "EXTERNAL_STOP",
            "test stop during pose priming snapshot",
            revoke=True,
            stop_recording=True,
        )
        backend.release_snapshot.set()
        await priming_task

        messages = _decoded_messages(websocket)
        assert server.leases.current is None
        assert server._pose_states == {}
        assert not any(message["type"] == "ack" for message in messages)
        error = next(message for message in messages if message["type"] == "error")
        assert error["body"]["code"] == "LEASE_REQUIRED"
        assert error["body"]["ack_seq"] == 1

        websocket.sent.clear()
        await server._handle_text(
            session,
            _message(
                "control.acquire",
                2,
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
            ),
        )
        granted = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "control.status" and message["body"]["granted"] is True
        )
        new_lease_id = granted["body"]["lease_id"]
        websocket.sent.clear()

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                3,
                _pose_body(new_lease_id, angular_delta_rad=(0.001, 0.0, 0.0)),
            ),
        )

        messages = _decoded_messages(websocket)
        error = next(message for message in messages if message["type"] == "error")
        assert error["body"]["code"] == "INVALID_MESSAGE"
        assert "zero angular delta" in error["body"]["message"]
        assert backend.speed_calls == 0
        assert server._pose_states == {}

    asyncio.run(scenario())


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
    session.hello_complete = True
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


def test_motion_snapshot_validation_and_speed_are_atomic_against_state_reads(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _OrderedSnapshotBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)

        motion_task = asyncio.create_task(
            server._handle_text(
                session,
                _message(
                    "motion.cartesian_velocity",
                    1,
                    {
                        "lease_id": lease_id,
                        "deadman": True,
                        "frame": "base",
                        "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
                        "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
                        "duration_ms": 100,
                    },
                ),
            )
        )
        assert await asyncio.to_thread(backend.snapshot_started.wait, 1.0)
        state_read_task = asyncio.create_task(server._read_snapshot())
        await asyncio.sleep(0)
        backend.release_first_snapshot.set()

        await motion_task
        await state_read_task

        assert backend.operation_order[:3] == ["snapshot-1", "speed", "snapshot-2"]
        assert backend.speed_calls == 1
        assert any(
            message["type"] == "ack"
            and message["body"]["ack_type"] == "motion.cartesian_velocity"
            for message in _decoded_messages(websocket)
        )

    asyncio.run(scenario())


def test_motion_reuses_fresh_snapshot_without_second_controller_read(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _CountingSnapshotBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, _, lease_id = _owned_session(server)

        await server._read_snapshot()
        await server._handle_text(
            session,
            _message(
                "motion.cartesian_velocity",
                1,
                {
                    "lease_id": lease_id,
                    "deadman": True,
                    "frame": "base",
                    "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
                    "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
                    "duration_ms": 100,
                },
            ),
        )

        assert backend.snapshot_calls == 1
        assert backend.speed_calls == 1

    asyncio.run(scenario())


def test_motion_refreshes_expired_snapshot_before_speed(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _CountingSnapshotBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, _, lease_id = _owned_session(server)

        await server._read_snapshot()
        server._latest_snapshot_started_monotonic = time.monotonic() - 1.0
        await server._handle_text(
            session,
            _message(
                "motion.cartesian_velocity",
                1,
                {
                    "lease_id": lease_id,
                    "deadman": True,
                    "frame": "base",
                    "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
                    "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
                    "duration_ms": 100,
                },
            ),
        )

        assert backend.snapshot_calls == 2
        assert backend.speed_calls == 1

    asyncio.run(scenario())


def test_motion_that_becomes_stale_waiting_for_backend_lock_never_executes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            server=ServerConfig(max_message_age_ms=50),
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _BlockingSnapshotBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)

        state_read_task = asyncio.create_task(server._read_snapshot())
        assert await asyncio.to_thread(backend.snapshot_started.wait, 1.0)
        motion_task = asyncio.create_task(
            server._handle_text(
                session,
                _message(
                    "motion.cartesian_velocity",
                    1,
                    {
                        "lease_id": lease_id,
                        "deadman": True,
                        "frame": "base",
                        "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
                        "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
                        "duration_ms": 100,
                    },
                ),
            )
        )
        await asyncio.sleep(0.08)
        backend.release_snapshot.set()
        await state_read_task
        await motion_task

        assert backend.speed_calls == 0
        error = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "error"
        )
        assert error["body"]["code"] == "STALE_MESSAGE"
        assert server.leases.current is None

    asyncio.run(scenario())


def test_watchdog_stops_first_motion_while_speed_backend_is_still_inflight(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True, emergency_stop_timeout_ms=80),
            safety=SafetyConfig(watchdog_ms=100, feedback_stall_ms=100),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _DelayedSpeedBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)

        motion_task = asyncio.create_task(
            server._handle_text(
                session,
                _message(
                    "motion.cartesian_velocity",
                    1,
                    {
                        "lease_id": lease_id,
                        "deadman": True,
                        "frame": "base",
                        "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
                        "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
                        "duration_ms": 100,
                    },
                ),
            )
        )
        assert await asyncio.to_thread(backend.speed_started.wait, 1.0)
        watchdog_task = asyncio.create_task(server._watchdog_loop())
        try:
            deadline = asyncio.get_running_loop().time() + 1.0
            while server.leases.current is not None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            assert server.leases.current is None
            assert backend.stop_count >= 1
        finally:
            backend.release_speed.set()
            await motion_task
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)

        assert server._motion_active is False
        assert not any(
            message["type"] == "ack"
            and message["body"].get("ack_type") == "motion.cartesian_velocity"
            and message["body"].get("accepted") is True
            for message in _decoded_messages(websocket)
        )

    asyncio.run(scenario())


def test_session_established_non_owner_stop_revokes_existing_lease(tmp_path: Path) -> None:
    asyncio.run(_session_established_non_owner_stop_revokes_existing_lease(tmp_path))


async def _session_established_non_owner_stop_revokes_existing_lease(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = SimulatorBackend(config.robot)
    server = TeleopServer(config, TOKEN, backend=backend)
    owner = ClientSession(websocket=_MemoryWebSocket())  # type: ignore[arg-type]
    owner.hello_complete = True
    owner.client_id = "owner"
    owner.last_inbound_seq = 0
    outsider = ClientSession(websocket=_MemoryWebSocket())  # type: ignore[arg-type]
    outsider.hello_complete = True
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


def test_release_after_safety_revoke_is_idempotent(tmp_path: Path) -> None:
    asyncio.run(_release_after_safety_revoke_is_idempotent(tmp_path))


async def _release_after_safety_revoke_is_idempotent(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = SimulatorBackend(config.robot)
    server = TeleopServer(config, TOKEN, backend=backend)
    session, websocket, lease_id = _owned_session(server)

    await server._safe_stop(
        "WORKSPACE_LIMIT",
        "test safety revocation",
        revoke=True,
        stop_recording=True,
    )
    stop_count_after_revoke = backend.stop_count
    websocket.sent.clear()

    await server._handle_text(
        session,
        _message("control.release", 1, {"lease_id": lease_id}),
    )

    messages = _decoded_messages(websocket)
    assert server.leases.current is None
    assert backend.stop_count == stop_count_after_revoke
    assert not any(message["type"] == "error" for message in messages)
    ack = next(message for message in messages if message["type"] == "ack")
    assert ack["body"]["ack_seq"] == 1
    assert ack["body"]["ack_type"] == "control.release"
    assert ack["body"]["accepted"] is True
    assert ack["body"]["detail"] == "control lease already released"


def test_release_does_not_clear_another_sessions_valid_lease(tmp_path: Path) -> None:
    asyncio.run(_release_does_not_clear_another_sessions_valid_lease(tmp_path))


async def _release_does_not_clear_another_sessions_valid_lease(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = SimulatorBackend(config.robot)
    server = TeleopServer(config, TOKEN, backend=backend)
    owner, _, lease_id = _owned_session(server, client_id="owner")
    outsider_socket = _MemoryWebSocket()
    outsider = ClientSession(websocket=outsider_socket)  # type: ignore[arg-type]
    outsider.hello_complete = True
    outsider.client_id = "outsider"
    outsider.last_inbound_seq = 0
    server.sessions[outsider.session_id] = outsider

    await server._handle_text(
        outsider,
        _message("control.release", 1, {"lease_id": lease_id}),
    )

    messages = _decoded_messages(outsider_socket)
    assert server.leases.current is not None
    assert server.leases.current.session_id == owner.session_id
    assert server.leases.current.lease_id == lease_id
    assert backend.stop_count == 0
    error = next(message for message in messages if message["type"] == "error")
    assert error["body"]["code"] == "LEASE_REQUIRED"
    assert error["body"]["ack_seq"] == 1


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
    owner.hello_complete = True
    owner.client_id = "owner"
    outsider_socket = _MemoryWebSocket()
    outsider = ClientSession(websocket=outsider_socket)  # type: ignore[arg-type]
    outsider.hello_complete = True
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


def test_equivalent_euler_wrap_feedback_does_not_refresh_stall_clock(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )
    backend = _MutableFeedbackBackend()
    server = TeleopServer(config, TOKEN, backend=backend)
    backend.tcp_pose["rz"] = math.pi
    server._observe_feedback(backend.snapshot())
    server._last_feedback_change_monotonic = 123.0
    backend.tcp_pose["rz"] = -math.pi

    server._observe_feedback(backend.snapshot())

    assert server._last_feedback_change_monotonic == 123.0


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
    session.hello_complete = True
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
            await asyncio.sleep(MOTION_COMMAND_TEST_SPACING_S)
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


def test_state_loop_revokes_lease_when_feedback_leaves_orientation_envelope(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            server=ServerConfig(state_hz=50),
            robot=RobotConfig(base_locked=True),
            safety=SafetyConfig(
                orientation_configured=True,
                orientation_center_rad=(0.0, 3.14, 0.0),
                orientation_tolerance_rad=(0.1, 0.1, 0.1),
            ),
            recording=RecordingConfig(root=tmp_path / "raw", fps=50),
        )
        backend = _MutableFeedbackBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        _, websocket, _ = _owned_session(server)
        state_task = asyncio.create_task(server._state_loop())
        try:
            await asyncio.sleep(0.04)
            backend.tcp_pose["rx"] = 0.2
            await asyncio.wait_for(
                _wait_for_safety_event(websocket, "ORIENTATION_LIMIT"),
                timeout=1.0,
            )
            await asyncio.wait_for(
                _wait_for_control_reason(websocket, "orientation_limit"),
                timeout=1.0,
            )
        finally:
            state_task.cancel()
            await asyncio.gather(state_task, return_exceptions=True)

        messages = _decoded_messages(websocket)
        assert any(
            message["type"] == "control.status"
            and message["body"]["granted"] is False
            and message["body"]["reason"] == "orientation_limit"
            for message in messages
        )
        assert backend.speed_calls == 0
        assert backend.stop_count >= 1
        assert server.leases.current is None
        assert server._pose_states == {}

    asyncio.run(scenario())


def test_recording_audit_includes_orientation_configuration(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            server=ServerConfig(state_hz=50),
            robot=RobotConfig(base_locked=True),
            safety=SafetyConfig(
                orientation_configured=True,
                orientation_center_rad=(0.0, 3.14, 0.0),
                orientation_tolerance_rad=(0.1, 0.1, 0.1),
            ),
            recording=RecordingConfig(root=tmp_path / "raw", fps=50),
            cameras={"camera_wrist": CameraConfig(source="0", fps=50)},
        )
        backend = _MutableFeedbackBackend()
        server = TeleopServer(
            config,
            TOKEN,
            backend=backend,
            camera=NullCameraProvider(["camera_wrist"]),
        )
        session, _, lease_id = _owned_session(server)
        await server._recording_start(
            session,
            Envelope(
                "recording.start",
                1,
                int(time.time() * 1_000),
                {
                    "lease_id": lease_id,
                    "task": "orientation audit",
                    "episode_id": "orientation-audit",
                    "cameras": ["camera_wrist"],
                },
            ),
        )
        metadata_path = tmp_path / "raw" / "orientation-audit" / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["context"]["orientation_configured"] is True
        assert metadata["context"]["orientation_center_rad"] == [0.0, 3.14, 0.0]
        assert metadata["context"]["orientation_tolerance_rad"] == [0.1, 0.1, 0.1]
        assert metadata["context"]["orientation_gimbal_lock_margin_rad"] == pytest.approx(0.1)

        state_task = asyncio.create_task(server._state_loop())
        try:
            while server.recorder.status.frame_count < 1:
                await asyncio.sleep(0.01)
        finally:
            state_task.cancel()
            await asyncio.gather(state_task, return_exceptions=True)
        await asyncio.to_thread(server.recorder.stop, "operator_stop")

        frame_path = tmp_path / "raw" / "orientation-audit" / "frames.jsonl"
        frame = json.loads(frame_path.read_text(encoding="utf-8").splitlines()[0])
        assert frame["safety"]["orientation_configured"] is True
        assert frame["safety"]["orientation_center_rad"] == [0.0, 3.14, 0.0]
        assert frame["safety"]["orientation_tolerance_rad"] == [0.1, 0.1, 0.1]
        assert frame["safety"]["orientation_gimbal_lock_margin_rad"] == pytest.approx(0.1)

    asyncio.run(scenario())


async def _wait_for_lease_release(server: TeleopServer) -> None:
    while server.leases.current is not None:
        await asyncio.sleep(0.01)


async def _wait_for_safety_event(websocket: _MemoryWebSocket, code: str) -> None:
    while not any(
        message["type"] == "safety.event" and message["body"]["code"] == code
        for message in _decoded_messages(websocket)
    ):
        await asyncio.sleep(0.01)


async def _wait_for_control_reason(websocket: _MemoryWebSocket, reason: str) -> None:
    while not any(
        message["type"] == "control.status" and message["body"]["reason"] == reason
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


def test_expired_lease_discovered_by_acquire_clears_pose_and_requires_reprime(
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
        assert server._pose_states
        assert server.leases.current is not None
        server.leases.current.expires_monotonic = time.monotonic() - 1.0
        websocket.sent.clear()

        await server._handle_text(
            session,
            _message(
                "control.acquire",
                2,
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
            ),
        )

        messages = _decoded_messages(websocket)
        assert server.leases.current is None
        assert server._pose_states == {}
        assert backend.stop_count >= 1
        assert any(
            message["type"] == "safety.event"
            and message["body"]["code"] == "LEASE_EXPIRED"
            for message in messages
        )
        assert not any(
            message["type"] == "control.status" and message["body"]["granted"] is True
            for message in messages
        )

        websocket.sent.clear()
        await server._handle_text(
            session,
            _message(
                "control.acquire",
                3,
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
            ),
        )
        granted = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "control.status" and message["body"]["granted"] is True
        )
        new_lease_id = granted["body"]["lease_id"]
        websocket.sent.clear()

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                4,
                _pose_body(new_lease_id, angular_delta_rad=(0.001, 0.0, 0.0)),
            ),
        )

        error = next(
            message for message in _decoded_messages(websocket) if message["type"] == "error"
        )
        assert error["body"]["code"] == "INVALID_MESSAGE"
        assert "zero angular delta" in error["body"]["message"]
        assert backend.speed_calls == 0
        assert server._pose_states == {}

    asyncio.run(scenario())


def test_motion_with_expired_lease_stops_and_does_not_swallow_expiry(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _FrozenFeedbackBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        assert server.leases.current is not None
        server.leases.current.expires_monotonic = time.monotonic() - 1.0
        websocket.sent.clear()

        await server._handle_text(
            session,
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
            ),
        )

        messages = _decoded_messages(websocket)
        error = next(message for message in messages if message["type"] == "error")
        assert error["body"]["code"] == "LEASE_EXPIRED"
        assert error["body"]["ack_seq"] == 2
        assert any(
            message["type"] == "safety.event"
            and message["body"]["code"] == "LEASE_EXPIRED"
            for message in messages
        )
        assert backend.speed_calls == 0
        assert backend.stop_count >= 1
        assert server.leases.current is None
        assert server._pose_states == {}

    asyncio.run(scenario())


def test_watchdog_expiry_clears_pose_before_same_calibration_can_resume(
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
        assert server.leases.current is not None
        server.leases.current.expires_monotonic = time.monotonic() - 1.0
        websocket.sent.clear()

        watchdog_task = asyncio.create_task(server._watchdog_loop())
        try:
            await asyncio.wait_for(
                _wait_for_safety_event(websocket, "LEASE_EXPIRED"),
                timeout=1.0,
            )
        finally:
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)

        assert server.leases.current is None
        assert server._pose_states == {}
        assert backend.stop_count >= 1
        websocket.sent.clear()
        await server._handle_text(
            session,
            _message(
                "control.acquire",
                2,
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
            ),
        )
        granted = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "control.status" and message["body"]["granted"] is True
        )
        websocket.sent.clear()

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                3,
                _pose_body(
                    granted["body"]["lease_id"],
                    angular_delta_rad=(0.001, 0.0, 0.0),
                ),
            ),
        )

        error = next(
            message for message in _decoded_messages(websocket) if message["type"] == "error"
        )
        assert error["body"]["code"] == "INVALID_MESSAGE"
        assert "zero angular delta" in error["body"]["message"]
        assert backend.speed_calls == 0

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
        await asyncio.sleep(MOTION_COMMAND_TEST_SPACING_S)

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
        await asyncio.sleep(MOTION_COMMAND_TEST_SPACING_S)
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
        await asyncio.sleep(MOTION_COMMAND_TEST_SPACING_S)

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


def test_pose_sample_priming_after_external_envelope_exit_fails_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            safety=SafetyConfig(
                orientation_configured=True,
                orientation_center_rad=(0.0, 3.14, 0.0),
                orientation_tolerance_rad=(0.1, 0.1, 0.1),
            ),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _MutableFeedbackBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)
        await _prime_pose(server, session, lease_id)
        assert server._pose_states
        await asyncio.sleep(MOTION_COMMAND_TEST_SPACING_S)
        websocket.sent.clear()
        backend.tcp_pose["rx"] = 0.2

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

        messages = _decoded_messages(websocket)
        error = next(message for message in messages if message["type"] == "error")
        assert error["body"]["code"] == "ORIENTATION_LIMIT"
        assert error["body"]["ack_seq"] == 2
        assert any(
            message["type"] == "safety.event"
            and message["body"]["code"] == "ORIENTATION_LIMIT"
            for message in messages
        )
        assert any(
            message["type"] == "control.status"
            and message["body"]["granted"] is False
            and message["body"]["reason"] == "orientation_limit"
            for message in messages
        )
        assert backend.speed_calls == 0
        assert backend.stop_count >= 1
        assert server.leases.current is None
        assert server._pose_states == {}

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


@pytest.mark.parametrize("sensor_timestamp_ms", [1_000, 999, 1_019, 1_301])
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


def test_pose_sample_accepts_interval_up_to_three_hundred_ms_watchdog(tmp_path: Path) -> None:
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
        await asyncio.sleep(MOTION_COMMAND_TEST_SPACING_S)

        await server._handle_text(
            session,
            _message(
                "pose.sample",
                2,
                _pose_body(
                    lease_id,
                    sensor_timestamp_ms=1_300,
                    angular_delta_rad=(0.003, 0.0, 0.0),
                ),
            ),
        )

        ack = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "ack"
        )
        assert ack["body"]["accepted"] is True
        assert server._last_command["sensor_interval_ms"] == 300

    asyncio.run(scenario())


def test_pose_sample_interval_is_capped_by_shorter_watchdog(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            safety=SafetyConfig(watchdog_ms=200, feedback_stall_ms=150),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
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
                _pose_body(lease_id, sensor_timestamp_ms=1_201),
            ),
        )

        error = next(
            message
            for message in _decoded_messages(websocket)
            if message["type"] == "error"
        )
        assert error["body"]["code"] == "INVALID_MESSAGE"
        assert "between 20 and 200 ms" in error["body"]["message"]
        assert server.leases.current is None

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
        await asyncio.sleep(MOTION_COMMAND_TEST_SPACING_S)
        if failure == "snapshot":
            server._latest_snapshot_started_monotonic = time.monotonic() - 1.0
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


def test_cartesian_velocity_current_orientation_limit_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            safety=SafetyConfig(
                orientation_configured=True,
                orientation_center_rad=(0.0, 0.0, 0.0),
                orientation_tolerance_rad=(0.1, 0.1, 0.1),
            ),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _FrozenFeedbackBackend()
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
                    "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
                    "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
                    "duration_ms": 100,
                },
            ),
        )

        messages = _decoded_messages(websocket)
        error = next(message for message in messages if message["type"] == "error")
        assert error["body"]["code"] == "ORIENTATION_LIMIT"
        assert error["body"]["ack_seq"] == 1
        assert any(
            message["type"] == "safety.event"
            and message["body"]["code"] == "ORIENTATION_LIMIT"
            for message in messages
        )
        assert any(
            message["type"] == "control.status"
            and message["body"]["granted"] is False
            and message["body"]["reason"] == "orientation_limit"
            for message in messages
        )
        assert backend.speed_calls == 0
        assert backend.stop_count >= 1
        assert server.leases.current is None
        assert server._pose_states == {}

    asyncio.run(scenario())


def test_pose_sample_predicted_orientation_limit_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = AppConfig(
            robot=RobotConfig(base_locked=True),
            safety=SafetyConfig(
                orientation_configured=True,
                orientation_center_rad=(0.0, 3.14, 0.0),
                orientation_tolerance_rad=(0.01, 0.01, 0.01),
            ),
            recording=RecordingConfig(root=tmp_path / "raw", fps=20),
        )
        backend = _FrozenFeedbackBackend()
        server = TeleopServer(config, TOKEN, backend=backend)
        session, websocket, lease_id = _owned_session(server)

        await _prime_pose(server, session, lease_id)
        await asyncio.sleep(MOTION_COMMAND_TEST_SPACING_S)
        await server._handle_text(
            session,
            _message(
                "pose.sample",
                2,
                _pose_body(
                    lease_id,
                    sensor_timestamp_ms=1_100,
                    angular_delta_rad=(0.02, 0.0, 0.0),
                ),
            ),
        )

        messages = _decoded_messages(websocket)
        error = next(message for message in messages if message["type"] == "error")
        assert error["body"]["code"] == "ORIENTATION_LIMIT"
        assert error["body"]["ack_seq"] == 2
        assert any(
            message["type"] == "safety.event"
            and message["body"]["code"] == "ORIENTATION_LIMIT"
            for message in messages
        )
        assert any(
            message["type"] == "control.status"
            and message["body"]["granted"] is False
            and message["body"]["reason"] == "orientation_limit"
            for message in messages
        )
        assert backend.speed_calls == 0
        assert backend.stop_count >= 1
        assert server.leases.current is None
        assert server._pose_states == {}

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


def test_server_constructor_ignores_legacy_programmatic_token(tmp_path: Path) -> None:
    config = AppConfig(
        robot=RobotConfig(base_locked=True),
        recording=RecordingConfig(root=tmp_path / "raw", fps=20),
    )

    server = TeleopServer(config, "short", backend=SimulatorBackend(config.robot))
    assert server.config is config
