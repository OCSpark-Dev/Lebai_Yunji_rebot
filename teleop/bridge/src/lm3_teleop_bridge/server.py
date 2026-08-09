from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .backends import (
    HardwareBackend,
    RobotBackend,
    RobotSnapshot,
    SimulatorBackend,
    backend_ready,
)
from .camera import CameraProvider, NullCameraProvider, OpenCVCameraProvider
from .config import AppConfig, ConfigError, ORIENTATION_GIMBAL_LOCK_MARGIN_RAD
from .protocol import (
    Envelope,
    ProtocolError,
    decode_envelope,
    encode_envelope,
    require_bool,
    require_int,
    require_number,
    require_string,
    require_vector,
)
from .recorder import EpisodeRecorder
from .safety import (
    Lease,
    LeaseManager,
    TokenBucket,
    clamp_twist,
    joints_within_margin,
    predict_orientation_ok,
    predict_workspace_ok,
    shortest_angular_distance_rad,
)


LOGGER = logging.getLogger("lm3_teleop_bridge")
FEEDBACK_CHANGE_EPSILON = 1e-6
POSE_ANGULAR_DEADBAND_RPS = 1e-3
FEEDBACK_MIN_LINEAR_PROGRESS_M = 2e-5
FEEDBACK_MIN_ANGULAR_PROGRESS_RAD = 2e-5
FEEDBACK_MIN_JOINT_PROGRESS_RAD = 2e-5
FEEDBACK_OBSERVABILITY_FACTOR = 2.0
FEEDBACK_JOINT_VELOCITY_EPSILON_RAD_S = 1e-5
FEEDBACK_DIRECTION_COSINE_MIN = 0.5
POSE_MIN_CONFIDENCE = 0.8
POSE_MIN_INTERVAL_MS = 20
POSE_MAX_INTERVAL_MS = 300
POSE_MAX_SINGLE_FRAME_DELTA_RAD = 0.25
POSE_MAX_INPUT_ANGULAR_RPS = 6.0
POSE_PRIMING_EPSILON_RAD = 1e-9
POSE_MAX_CALIBRATION_ID_LENGTH = 128
MOTION_SNAPSHOT_MAX_AGE_MS = 200
POSE_SAMPLE_BODY_KEYS = frozenset(
    {
        "lease_id",
        "deadman",
        "frame",
        "mapping",
        "calibration_id",
        "sensor_timestamp_ms",
        "tracking_state",
        "confidence",
        "angular_delta_rad",
    }
)


@dataclass(slots=True)
class ClientSession:
    websocket: ServerConnection
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    hello_complete: bool = False
    client_id: str = ""
    client_name: str = ""
    platform: str = ""
    last_inbound_seq: int = -1
    outbound_seq: int = 0
    last_lease_status_monotonic: float = 0.0
    motion_bucket: TokenBucket = field(default_factory=lambda: TokenBucket(20.0, 1.0))
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message_type: str, body: dict[str, Any]) -> None:
        async with self.send_lock:
            sequence = self.outbound_seq
            self.outbound_seq += 1
            payload = encode_envelope(message_type, sequence, _wall_ms(), body)
            await self.websocket.send(payload)


@dataclass(frozen=True, slots=True)
class PoseSampleState:
    calibration_id: str
    last_sensor_timestamp_ms: int


@dataclass(slots=True)
class FeedbackMotionState:
    started_monotonic: float
    last_accounted_monotonic: float
    command_expires_monotonic: float
    linear_mps: tuple[float, float, float]
    angular_rps: tuple[float, float, float]
    baseline_joint_position_rad: tuple[float, ...]
    baseline_tcp_xyz: tuple[float, float, float]
    baseline_tcp_orientation_rad: tuple[float, float, float]
    expected_linear_m: float = 0.0
    expected_angular_rad: float = 0.0


class TeleopServer:
    def __init__(
        self,
        config: AppConfig,
        token: str | None = None,
        *,
        backend: RobotBackend | None = None,
        camera: CameraProvider | None = None,
        recorder: EpisodeRecorder | None = None,
        hardware_flag: bool = False,
        allow_lan_flag: bool = False,
        allow_ephemeral_port: bool = False,
    ) -> None:
        config.validate(
            hardware_flag=hardware_flag,
            allow_lan_flag=allow_lan_flag,
            allow_ephemeral_port=allow_ephemeral_port,
        )
        # Retained only for programmatic compatibility with older callers.
        # Authentication tokens are no longer inspected, stored, or logged.
        del token
        self.config = config
        if backend is None:
            backend = (
                HardwareBackend(config.robot)
                if config.robot.backend.lower() == "hardware"
                else SimulatorBackend(config.robot)
            )
        configured_backend = config.robot.backend.lower()
        injected_mode = getattr(backend, "mode", None)
        if not isinstance(injected_mode, str) or injected_mode.lower() != configured_backend:
            raise ConfigError(
                "backend.mode must match robot.backend; injected backends cannot bypass "
                "the simulator/hardware safety configuration"
            )
        self.backend = backend
        if camera is None:
            camera = OpenCVCameraProvider(config.cameras) if config.cameras else NullCameraProvider()
        self.camera = camera
        if recorder is not None and recorder.fps != config.server.state_hz:
            raise ConfigError(
                "injected recorder.fps must equal server.state_hz"
            )
        self.recorder = recorder or EpisodeRecorder(config.recording.root, config.recording.fps)
        self.leases = LeaseManager(config.server.lease_ms)
        self.sessions: dict[str, ClientSession] = {}
        self._backend_lock = asyncio.Lock()
        self._backend_stop_lock = asyncio.Lock()
        self._record_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._ws_server: Server | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._closing = False
        self._safety_epoch = 0
        self._motion_active = False
        self._last_valid_motion = 0.0
        self._feedback_motion_expected = False
        self._motion_command_expires_monotonic = 0.0
        self._last_feedback_change_monotonic = time.monotonic()
        self._feedback_signature: tuple[float, ...] | None = None
        self._feedback_motion_state: FeedbackMotionState | None = None
        self._latest_snapshot: RobotSnapshot | None = None
        self._latest_snapshot_started_monotonic: float | None = None
        self._motion_inflight_generation = 0
        self._motion_inflight_token: int | None = None
        self._motion_inflight_deadline_monotonic = 0.0
        self._recording_cameras: list[str] = []
        self._pose_states: dict[str, PoseSampleState] = {}
        self._last_command: dict[str, Any] = {
            "type": "none",
            "client_seq": None,
            "sent_at_ms": None,
            "received_at_ms": None,
            "network_age_ms": None,
            "deadman": False,
            "linear_mps": [0.0, 0.0, 0.0],
            "angular_rps": [0.0, 0.0, 0.0],
            "duration_ms": 0,
            "clamped": False,
        }

    @property
    def bound_port(self) -> int:
        if self._ws_server is None or not self._ws_server.sockets:
            raise RuntimeError("server is not running")
        return int(self._ws_server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._ws_server is not None:
            return
        self._closing = False
        self._ws_server = await serve(
            self._handler,
            self.config.server.host,
            self.config.server.port,
            max_size=256 * 1024,
            ping_interval=5,
            ping_timeout=5,
        )
        self._tasks = [
            asyncio.create_task(self._watchdog_loop(), name="lm3-watchdog"),
            asyncio.create_task(self._state_loop(), name="lm3-state"),
        ]
        LOGGER.info(
            "bridge listening on %s:%s%s in %s mode",
            self.config.server.host,
            self.bound_port,
            self.config.server.path,
            self.backend.mode,
        )

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._safe_stop("SERVER_SHUTDOWN", "bridge is shutting down", revoke=True, stop_recording=True)
        self.camera.close()
        await asyncio.to_thread(self.backend.close)

    async def wait_closed(self) -> None:
        if self._ws_server is None:
            raise RuntimeError("server is not running")
        await self._ws_server.wait_closed()

    async def _handler(self, websocket: ServerConnection) -> None:
        request = getattr(websocket, "request", None)
        request_path = getattr(request, "path", self.config.server.path)
        if request_path != self.config.server.path:
            await websocket.close(code=1008, reason="invalid websocket path")
            return
        session = ClientSession(
            websocket=websocket,
            motion_bucket=TokenBucket(self.config.safety.command_rate_hz, 1.0),
        )
        self.sessions[session.session_id] = session
        peer = _peer_log_label(websocket)
        LOGGER.info("session.open session=%s peer=%s", _session_log_id(session), peer)
        try:
            async for message in websocket:
                if not isinstance(message, str):
                    await self._send_error(
                        session,
                        ProtocolError("INVALID_MESSAGE", "binary messages are not accepted", recoverable=False),
                    )
                    await websocket.close(code=1003, reason="text JSON required")
                    break
                await self._handle_text(session, message)
        except ConnectionClosed:
            pass
        finally:
            was_owner = self.leases.current is not None and self.leases.current.session_id == session.session_id
            self.sessions.pop(session.session_id, None)
            self._pose_states.pop(session.session_id, None)
            try:
                if was_owner:
                    await self._safe_stop(
                        "CLIENT_DISCONNECTED",
                        "control owner disconnected",
                        revoke=True,
                        stop_recording=True,
                    )
            finally:
                LOGGER.info(
                    "session.close session=%s peer=%s hello_complete=%s was_owner=%s close_code=%s",
                    _session_log_id(session),
                    peer,
                    session.hello_complete,
                    was_owner,
                    getattr(websocket, "close_code", None),
                )

    async def _handle_text(self, session: ClientSession, text: str) -> None:
        stop_was_prioritized = session.hello_complete and _raw_message_type(text) == "motion.stop"
        if stop_was_prioritized:
            try:
                if self.leases.current is not None and not self._owns_lease(session):
                    stop_confirmed = await self._safe_stop(
                        "EXTERNAL_STOP",
                        "a session-established non-owner requested a safety stop",
                        revoke=True,
                        stop_recording=True,
                    )
                    if not stop_confirmed:
                        await self._send_error(
                            session,
                            ProtocolError(
                                "BACKEND_ERROR",
                                "software stop could not be confirmed",
                                recoverable=False,
                            ),
                        )
                        return
                else:
                    await self._execute_stop()
            except Exception as error:
                LOGGER.exception("prioritized stop failed")
                if self._owns_lease(session):
                    await self._safe_stop(
                        "BACKEND_ERROR",
                        str(error),
                        revoke=True,
                        stop_recording=True,
                    )
                await self._send_error(
                    session,
                    ProtocolError(
                        "BACKEND_ERROR",
                        "software stop could not be confirmed",
                        recoverable=False,
                    ),
                )
                return
        try:
            envelope = decode_envelope(text)
        except ProtocolError as error:
            if session.hello_complete and self._owns_lease(session):
                await self._safe_stop(error.code, error.message, revoke=True, stop_recording=True)
            await self._send_error(session, error)
            if not error.recoverable:
                await session.websocket.close(code=1008, reason=error.code)
            return

        if not session.hello_complete:
            if envelope.type != "session.hello":
                await self._send_error(
                    session,
                    ProtocolError(
                        "HELLO_REQUIRED",
                        "session.hello must be the first message",
                        recoverable=False,
                        ack_seq=envelope.seq,
                    ),
                )
                await session.websocket.close(code=1008, reason="session.hello required")
                return
            try:
                # The hello frame establishes a lease-free session and returns
                # server_time_ms so a client with a skewed wall clock can
                # synchronize. Every post-hello frame is still checked by the
                # strict timestamp validation below.
                await self._handle_hello(session, envelope)
            except ProtocolError as error:
                error.ack_seq = envelope.seq if error.ack_seq is None else error.ack_seq
                await self._send_error(session, error)
                if not error.recoverable:
                    await session.websocket.close(code=1008, reason=error.code)
            return

        if envelope.type == "session.hello":
            if self._owns_lease(session):
                await self._safe_stop(
                    "INVALID_MESSAGE",
                    "session.hello cannot be repeated",
                    revoke=True,
                    stop_recording=True,
                )
            await self._send_error(
                session,
                ProtocolError("INVALID_MESSAGE", "session.hello cannot be repeated", ack_seq=envelope.seq),
            )
            return

        if envelope.seq <= session.last_inbound_seq:
            if self._owns_lease(session):
                await self._safe_stop(
                    "OUT_OF_ORDER",
                    "client sequence was repeated or moved backwards",
                    revoke=True,
                    stop_recording=True,
                )
            await self._send_error(
                session,
                ProtocolError(
                    "OUT_OF_ORDER",
                    "client sequence must be strictly increasing",
                    ack_seq=envelope.seq,
                ),
            )
            return

        try:
            self._validate_message_time(envelope)
        except ProtocolError as error:
            if self._owns_lease(session):
                await self._safe_stop(error.code, error.message, revoke=True, stop_recording=True)
            await self._send_error(session, error)
            return

        session.last_inbound_seq = envelope.seq
        try:
            await self._dispatch(session, envelope, stop_was_prioritized=stop_was_prioritized)
        except ProtocolError as error:
            error.ack_seq = envelope.seq if error.ack_seq is None else error.ack_seq
            actuator_message = (
                envelope.type.startswith("motion.") and envelope.type != "motion.stop"
            ) or envelope.type in {"gripper.set", "pose.sample"}
            if (actuator_message or error.code == "LEASE_EXPIRED") and self._owns_lease(session):
                await self._safe_stop(error.code, error.message, revoke=True, stop_recording=True)
            await self._send_error(session, error)
        except Exception as error:  # fail closed at the hardware boundary
            LOGGER.exception("backend or server error while handling %s", envelope.type)
            if self._owns_lease(session):
                await self._safe_stop(
                    "BACKEND_ERROR", str(error), revoke=True, stop_recording=True
                )
            await self._send_error(
                session,
                ProtocolError(
                    "BACKEND_ERROR",
                    "backend operation failed",
                    recoverable=False,
                    ack_seq=envelope.seq,
                ),
            )

    async def _handle_hello(self, session: ClientSession, envelope: Envelope) -> None:
        if envelope.seq != 0:
            await self._send_error(
                session,
                ProtocolError(
                    "OUT_OF_ORDER",
                    "the first client message must use seq=0",
                    recoverable=False,
                    ack_seq=envelope.seq,
                ),
            )
            await session.websocket.close(code=1008, reason="first seq must be zero")
            return
        body = envelope.body
        client_id = require_string(body, "client_id")
        client_name = require_string(body, "client_name")
        platform = require_string(body, "platform")
        require_string(body, "app_version")
        capabilities = body.get("capabilities")
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            raise ProtocolError("INVALID_MESSAGE", "capabilities must be a string array")
        session.client_id = client_id
        session.client_name = client_name
        session.platform = platform
        session.last_inbound_seq = 0
        await session.send(
            "session.welcome",
            {
                "session_id": session.session_id,
                "server_time_ms": _wall_ms(),
                "mode": self.backend.mode,
                "watchdog_ms": self.config.safety.watchdog_ms,
                "command_rate_hz": self.config.safety.command_rate_hz,
                "base_locked": self.config.robot.base_locked,
                "limits": {
                    "max_linear_mps": self.config.safety.max_linear_mps,
                    "max_angular_rps": self.config.safety.max_angular_rps,
                    "max_command_duration_ms": self.config.safety.max_command_duration_ms,
                    "workspace_min_m": list(self.config.safety.workspace_min_m),
                    "workspace_max_m": list(self.config.safety.workspace_max_m),
                    "orientation_configured": self.config.safety.orientation_configured,
                    "orientation_center_rad": list(self.config.safety.orientation_center_rad),
                    "orientation_tolerance_rad": list(
                        self.config.safety.orientation_tolerance_rad
                    ),
                    "orientation_gimbal_lock_margin_rad": ORIENTATION_GIMBAL_LOCK_MARGIN_RAD,
                    "joint_min_rad": list(self.config.safety.joint_min_rad),
                    "joint_max_rad": list(self.config.safety.joint_max_rad),
                    "joint_limit_margin_rad": self.config.safety.joint_limit_margin_rad,
                },
            },
        )
        session.hello_complete = True
        snapshot = await self._read_snapshot()
        await session.send(
            "robot.state",
            snapshot.protocol_body(watchdog_ok=True, recording=self.recorder.status.recording),
        )

    async def _dispatch(
        self,
        session: ClientSession,
        envelope: Envelope,
        *,
        stop_was_prioritized: bool,
    ) -> None:
        message_type = envelope.type
        if message_type == "control.acquire":
            await self._control_acquire(session, envelope)
        elif message_type == "control.release":
            lease_id = require_string(envelope.body, "lease_id")
            if await self._stop_expired_lease():
                LOGGER.info(
                    "control.release session=%s result=already_released reason=lease_expired",
                    _session_log_id(session),
                )
                await self._send_ack(
                    session,
                    envelope,
                    accepted=True,
                    detail="control lease already released",
                )
                return
            current = self.leases.current
            if current is None:
                LOGGER.info(
                    "control.release session=%s result=already_released reason=no_current_lease",
                    _session_log_id(session),
                )
                await self._send_ack(
                    session,
                    envelope,
                    accepted=True,
                    detail="control lease already released",
                )
                return
            if current.session_id != session.session_id or current.lease_id != lease_id:
                raise ProtocolError("LEASE_REQUIRED", "a valid control lease is required")
            lease = self._require_lease(session, envelope.body)
            await self._safe_stop(
                "CONTROL_RELEASED",
                "operator released the control lease",
                revoke=True,
                stop_recording=True,
                emit_event=False,
            )
            LOGGER.info("control.release session=%s result=released", _session_log_id(session))
            await self._send_ack(session, envelope, accepted=True, detail=f"released {lease.lease_id}")
        elif message_type == "heartbeat":
            lease_id = envelope.body.get("lease_id")
            require_bool(envelope.body, "deadman")
            if envelope.body["deadman"] is not False:
                raise ProtocolError("DEADMAN_REQUIRED", "heartbeat deadman must be false")
            if lease_id is not None:
                if not isinstance(lease_id, str):
                    raise ProtocolError("INVALID_MESSAGE", "lease_id must be a string")
                self._require_lease(session, envelope.body)
                await self._maybe_send_lease_status(session, force=False)
            await self._send_ack(session, envelope, accepted=True)
        elif message_type == "motion.cartesian_velocity":
            await self._motion_velocity(session, envelope)
        elif message_type == "motion.stop":
            if not stop_was_prioritized:
                await self._execute_stop()
            await self._send_ack(session, envelope, accepted=True, detail="motion stopped")
        elif message_type == "gripper.set":
            await self._gripper(session, envelope)
        elif message_type == "recording.start":
            await self._recording_start(session, envelope)
        elif message_type == "recording.stop":
            reason = require_string(envelope.body, "reason")
            lease = self._require_lease(session, envelope.body)
            command_epoch = self._safety_epoch
            async with self._record_lock:
                self._assert_command_current(session, lease.lease_id, command_epoch)
                status = await asyncio.to_thread(self.recorder.stop, reason)
            self._recording_cameras = []
            await self._send_ack(session, envelope, accepted=True)
            await session.send("recording.status", status.body())
        elif message_type == "pose.sample":
            await self._pose_sample(session, envelope)

    async def _control_acquire(self, session: ClientSession, envelope: Envelope) -> None:
        body = envelope.body
        requested = require_int(body, "requested_lease_ms")
        hold = require_int(body, "operator_hold_ms")
        safety_ack = body.get("safety_ack")
        if not isinstance(safety_ack, dict):
            raise ProtocolError("INVALID_MESSAGE", "safety_ack must be an object")
        required_checks = ("base_stationary", "workspace_clear", "estop_accessible", "tool_secure")
        if hold < 1_500 or not all(require_bool(safety_ack, item) for item in required_checks):
            self._log_control_acquire(
                session,
                granted=False,
                reason="safety_check_or_hold_incomplete",
            )
            await session.send(
                "control.status",
                {
                    "granted": False,
                    "lease_id": "",
                    "owner_client_id": self.leases.current.client_id if self.leases.current else "",
                    "expires_at_ms": 0,
                    "reason": "safety_check_or_hold_incomplete",
                },
            )
            return
        if await self._stop_expired_lease():
            self._log_control_acquire(session, granted=False, reason="lease_expired")
            return
        acquire_epoch = self._safety_epoch
        if self._stop_lock.locked():
            self._log_control_acquire(session, granted=False, reason="safety_stop_in_progress")
            await session.send(
                "control.status",
                {
                    "granted": False,
                    "lease_id": "",
                    "owner_client_id": "",
                    "expires_at_ms": 0,
                    "reason": "safety_stop_in_progress",
                },
            )
            return
        snapshot = await self._read_snapshot()
        if snapshot.robot_state_code != 5 or not self._snapshot_ready(snapshot):
            self._log_control_acquire(
                session,
                granted=False,
                reason="robot_must_be_idle_ready_and_base_locked",
                detail=f"state_code={snapshot.robot_state_code}",
            )
            await session.send(
                "control.status",
                {
                    "granted": False,
                    "lease_id": "",
                    "owner_client_id": self.leases.current.client_id if self.leases.current else "",
                    "expires_at_ms": 0,
                    "reason": "robot_must_be_idle_ready_and_base_locked",
                },
            )
            return
        envelope_error = self._motion_envelope_error(
            snapshot,
            linear=(0.0, 0.0, 0.0),
            angular=(0.0, 0.0, 0.0),
            duration_ms=0,
        )
        if envelope_error is not None:
            self._log_control_acquire(
                session,
                granted=False,
                reason="robot_not_within_configured_motion_envelope",
                detail=f"envelope_error={envelope_error}",
            )
            await session.send(
                "control.status",
                {
                    "granted": False,
                    "lease_id": "",
                    "owner_client_id": self.leases.current.client_id if self.leases.current else "",
                    "expires_at_ms": 0,
                    "reason": "robot_not_within_configured_motion_envelope",
                },
            )
            return
        interrupted_by_stop = False
        async with self._stop_lock:
            if acquire_epoch != self._safety_epoch:
                interrupted_by_stop = True
                lease = None
            else:
                lease = self.leases.acquire(
                    session_id=session.session_id,
                    client_id=session.client_id,
                    requested_ms=requested,
                )
        if interrupted_by_stop:
            self._log_control_acquire(session, granted=False, reason="safety_stop_in_progress")
            await session.send(
                "control.status",
                {
                    "granted": False,
                    "lease_id": "",
                    "owner_client_id": "",
                    "expires_at_ms": 0,
                    "reason": "safety_stop_in_progress",
                },
            )
            return
        if lease is None and await self._stop_expired_lease():
            self._log_control_acquire(session, granted=False, reason="lease_expired")
            return
        if lease is None:
            current = self.leases.current
            self._log_control_acquire(
                session,
                granted=False,
                reason="lease_busy",
                detail=(
                    f"owner_session={_session_log_id_value(current.session_id)}"
                    if current is not None
                    else "owner_session=-"
                ),
            )
            await session.send(
                "control.status",
                {
                    "granted": False,
                    "lease_id": "",
                    "owner_client_id": current.client_id if current else "",
                    "expires_at_ms": self._lease_expiry(current) if current else 0,
                    "reason": "lease_busy",
                },
            )
            return
        self._log_control_acquire(
            session,
            granted=True,
            reason="granted",
            detail=f"lease_ms={lease.duration_ms}",
        )
        await self._send_control_status(session, lease, granted=True, reason="granted")

    @staticmethod
    def _log_control_acquire(
        session: ClientSession,
        *,
        granted: bool,
        reason: str,
        detail: str = "",
    ) -> None:
        LOGGER.log(
            logging.INFO if granted else logging.WARNING,
            "control.acquire session=%s granted=%s reason=%s detail=%s",
            _session_log_id(session),
            granted,
            reason,
            detail or "-",
        )

    async def _motion_velocity(self, session: ClientSession, envelope: Envelope) -> None:
        body = envelope.body
        if require_bool(body, "deadman") is not True:
            raise ProtocolError("DEADMAN_REQUIRED", "deadman must be true")
        if require_string(body, "frame") != "base":
            raise ProtocolError("UNSUPPORTED_MODE", "v1 only accepts the base frame")
        linear = require_vector(body, "linear_mps", ("x", "y", "z"))
        angular = require_vector(body, "angular_rps", ("rx", "ry", "rz"))
        requested_duration = require_int(body, "duration_ms")
        lease = self._require_lease(session, body)
        await self._execute_cartesian_velocity(
            session,
            envelope,
            lease=lease,
            linear=linear,
            angular=angular,
            requested_duration=requested_duration,
        )

    async def _pose_sample(self, session: ClientSession, envelope: Envelope) -> None:
        body = envelope.body
        if set(body) != POSE_SAMPLE_BODY_KEYS:
            missing = sorted(POSE_SAMPLE_BODY_KEYS - set(body))
            extra = sorted(set(body) - POSE_SAMPLE_BODY_KEYS)
            detail = []
            if missing:
                detail.append(f"missing={','.join(missing)}")
            if extra:
                detail.append(f"extra={','.join(extra)}")
            raise ProtocolError(
                "INVALID_MESSAGE",
                "pose.sample body must contain exactly the v1 fields"
                + (f" ({'; '.join(detail)})" if detail else ""),
            )
        if require_bool(body, "deadman") is not True:
            raise ProtocolError("DEADMAN_REQUIRED", "pose.sample deadman must be true")
        if require_string(body, "frame") != "phone_calibrated":
            raise ProtocolError(
                "UNSUPPORTED_MODE", "pose.sample frame must be phone_calibrated"
            )
        if require_string(body, "mapping") != "tcp_orientation":
            raise ProtocolError(
                "UNSUPPORTED_MODE", "pose.sample mapping must be tcp_orientation"
            )
        calibration_id = require_string(body, "calibration_id")
        if (
            calibration_id != calibration_id.strip()
            or len(calibration_id) > POSE_MAX_CALIBRATION_ID_LENGTH
        ):
            raise ProtocolError(
                "INVALID_MESSAGE",
                f"calibration_id must be trimmed and at most {POSE_MAX_CALIBRATION_ID_LENGTH} characters",
            )
        if require_string(body, "tracking_state") != "tracking":
            raise ProtocolError("INVALID_MESSAGE", "pose tracking_state must be tracking")
        confidence = require_number(body, "confidence")
        if not POSE_MIN_CONFIDENCE <= confidence <= 1.0:
            raise ProtocolError(
                "INVALID_MESSAGE",
                f"pose confidence must be between {POSE_MIN_CONFIDENCE} and 1.0",
            )
        sensor_timestamp_ms = require_int(body, "sensor_timestamp_ms")
        if sensor_timestamp_ms <= 0:
            raise ProtocolError("INVALID_MESSAGE", "sensor_timestamp_ms must be positive")
        angular_delta = _require_exact_vector(
            body, "angular_delta_rad", ("rx", "ry", "rz")
        )
        delta_norm = _vector_norm(angular_delta)
        if delta_norm > POSE_MAX_SINGLE_FRAME_DELTA_RAD:
            raise ProtocolError(
                "INVALID_MESSAGE",
                "pose angular_delta_rad exceeds the single-frame jump limit",
            )
        lease = self._require_lease(session, body)
        state = self._pose_states.get(session.session_id)
        if state is None or state.calibration_id != calibration_id:
            if delta_norm > POSE_PRIMING_EPSILON_RAD:
                raise ProtocolError(
                    "INVALID_MESSAGE",
                    "the first pose sample for a calibration must carry zero angular delta",
                )
            if not session.motion_bucket.consume():
                raise ProtocolError("RATE_LIMITED", "motion command rate exceeds 20 Hz")
            if self._motion_active:
                await self._execute_stop()
                lease = self._require_lease(session, body)
            command_epoch = self._safety_epoch
            lease_id = lease.lease_id
            snapshot = await self._read_snapshot()
            envelope_error = self._motion_envelope_error(
                snapshot,
                linear=(0.0, 0.0, 0.0),
                angular=(0.0, 0.0, 0.0),
                duration_ms=0,
            )
            if envelope_error == "WORKSPACE_LIMIT":
                raise ProtocolError(
                    envelope_error,
                    "current TCP is outside the configured workspace",
                )
            if envelope_error == "ORIENTATION_LIMIT":
                raise ProtocolError(
                    envelope_error,
                    "current TCP orientation is outside the configured envelope",
                )
            self._assert_command_current(session, lease_id, command_epoch)
            self._pose_states[session.session_id] = PoseSampleState(
                calibration_id=calibration_id,
                last_sensor_timestamp_ms=sensor_timestamp_ms,
            )
            received_at_ms = _wall_ms()
            self._last_command = {
                "type": envelope.type,
                "client_seq": envelope.seq,
                "sent_at_ms": envelope.sent_at_ms,
                "received_at_ms": received_at_ms,
                "network_age_ms": max(0, received_at_ms - envelope.sent_at_ms),
                "deadman": True,
                "linear_mps": [0.0, 0.0, 0.0],
                "angular_rps": [0.0, 0.0, 0.0],
                "duration_ms": 0,
                "clamped": False,
                "priming": True,
                "frame": "phone_calibrated",
                "mapping": "tcp_orientation",
                "calibration_id": calibration_id,
                "sensor_timestamp_ms": sensor_timestamp_ms,
                "sensor_interval_ms": None,
                "tracking_state": "tracking",
                "confidence": confidence,
                "angular_delta_rad": list(angular_delta),
                "input_angular_rps": [0.0, 0.0, 0.0],
            }
            await self._send_ack(
                session,
                envelope,
                accepted=True,
                clamped=False,
                detail="pose calibration primed; no motion executed",
            )
            await self._maybe_send_lease_status(session, force=False)
            return

        interval_ms = sensor_timestamp_ms - state.last_sensor_timestamp_ms
        if interval_ms <= 0:
            raise ProtocolError(
                "INVALID_MESSAGE", "sensor_timestamp_ms must increase strictly"
            )
        max_interval_ms = min(POSE_MAX_INTERVAL_MS, self.config.safety.watchdog_ms)
        if not POSE_MIN_INTERVAL_MS <= interval_ms <= max_interval_ms:
            raise ProtocolError(
                "INVALID_MESSAGE",
                f"pose sensor interval must be between {POSE_MIN_INTERVAL_MS} and {max_interval_ms} ms",
            )
        interval_s = interval_ms / 1_000
        input_angular = tuple(component / interval_s for component in angular_delta)
        if _vector_norm(input_angular) > POSE_MAX_INPUT_ANGULAR_RPS:
            raise ProtocolError(
                "INVALID_MESSAGE", "pose-derived angular velocity exceeds the input limit"
            )
        await self._execute_cartesian_velocity(
            session,
            envelope,
            lease=lease,
            linear=(0.0, 0.0, 0.0),
            angular=input_angular,
            requested_duration=interval_ms,
            apply_pose_deadband=True,
            audit={
                "priming": False,
                "frame": "phone_calibrated",
                "mapping": "tcp_orientation",
                "calibration_id": calibration_id,
                "sensor_timestamp_ms": sensor_timestamp_ms,
                "sensor_interval_ms": interval_ms,
                "tracking_state": "tracking",
                "confidence": confidence,
                "angular_delta_rad": list(angular_delta),
                "input_angular_rps": list(input_angular),
            },
        )
        self._pose_states[session.session_id] = PoseSampleState(
            calibration_id=calibration_id,
            last_sensor_timestamp_ms=sensor_timestamp_ms,
        )

    async def _execute_cartesian_velocity(
        self,
        session: ClientSession,
        envelope: Envelope,
        *,
        lease: Lease,
        linear: tuple[float, float, float],
        angular: tuple[float, float, float],
        requested_duration: int,
        apply_pose_deadband: bool = False,
        audit: dict[str, Any] | None = None,
    ) -> None:
        command_deadline_monotonic = (
            time.monotonic() + self.config.safety.watchdog_ms / 1_000
        )
        command_epoch = self._safety_epoch
        if not session.motion_bucket.consume():
            raise ProtocolError("RATE_LIMITED", "motion command rate exceeds 20 Hz")
        duration = max(
            self.config.safety.min_command_duration_ms,
            min(requested_duration, self.config.safety.max_command_duration_ms),
        )
        linear, angular, velocity_clamped = clamp_twist(linear, angular, self.config.safety)
        pose_deadbanded = False
        if apply_pose_deadband and _vector_norm(angular) < POSE_ANGULAR_DEADBAND_RPS:
            pose_deadbanded = True
            angular = (0.0, 0.0, 0.0)
        nonzero_motion = any(component != 0.0 for component in (*linear, *angular))
        # Explicit zero cartesian velocity is a protocol-level stop redundancy and
        # must still reach speedl.  Pose deadband frames are the sole exception:
        # they only refresh the lease/watchdog without touching the controller.
        execute_speed_command = not pose_deadbanded
        clamped = velocity_clamped or duration != requested_duration or pose_deadbanded
        motion_id: int | None = None
        async with self._backend_lock:
            snapshot = self._fresh_motion_snapshot_locked()
            if snapshot is None:
                snapshot_started_monotonic = time.monotonic()
                snapshot = await asyncio.to_thread(self.backend.snapshot)
                self._remember_snapshot(snapshot, snapshot_started_monotonic)
            snapshot_started_monotonic = self._latest_snapshot_started_monotonic
            max_snapshot_age_ms = min(
                MOTION_SNAPSHOT_MAX_AGE_MS,
                self.config.safety.watchdog_ms,
            )
            if (
                snapshot_started_monotonic is None
                or (time.monotonic() - snapshot_started_monotonic) * 1_000
                > max_snapshot_age_ms
            ):
                raise ProtocolError(
                    "STALE_MESSAGE",
                    "robot snapshot exceeded the motion freshness limit",
                )
            if time.monotonic() > command_deadline_monotonic:
                raise ProtocolError(
                    "STALE_MESSAGE",
                    "motion command exceeded the watchdog before backend execution",
                )
            # A frame can be fresh when it enters the WebSocket queue and become stale
            # while waiting for a slow controller snapshot. Recheck immediately before
            # touching the actuator.
            self._validate_message_time(envelope)
            if not self._snapshot_ready(snapshot):
                raise ProtocolError("ROBOT_NOT_READY", "robot state does not allow motion")
            envelope_error = self._motion_envelope_error(
                snapshot,
                linear=linear,
                angular=angular,
                duration_ms=duration,
            )
            if envelope_error == "WORKSPACE_LIMIT":
                raise ProtocolError(
                    "WORKSPACE_LIMIT",
                    "current or predicted TCP is outside the workspace",
                )
            if envelope_error == "ORIENTATION_LIMIT":
                raise ProtocolError(
                    "ORIENTATION_LIMIT",
                    "current or predicted TCP orientation is outside the configured envelope",
                )
            self._assert_command_current(session, lease.lease_id, command_epoch)
            if execute_speed_command:
                inflight_token = self._begin_motion_inflight(command_deadline_monotonic)
                try:
                    motion_id = await asyncio.to_thread(
                        self.backend.speed_cartesian, linear, angular, duration
                    )
                finally:
                    self._finish_motion_inflight(inflight_token)
        if not self._command_is_current(session, lease.lease_id, command_epoch):
            await self._execute_stop()
            raise ProtocolError(
                "LEASE_REQUIRED",
                "motion command was cancelled by a newer safety stop or lease change",
            )
        if execute_speed_command and (motion_id is None or motion_id <= 0):
            raise RuntimeError("Lebai speedl did not return a positive motion id")
        now_mono = time.monotonic()
        if nonzero_motion:
            self._set_feedback_motion_command(linear, angular, duration, now_mono)
        else:
            self._clear_feedback_motion_tracking()
        self._motion_active = True
        self._last_valid_motion = now_mono
        received_at_ms = _wall_ms()
        last_command = {
            "type": envelope.type,
            "client_seq": envelope.seq,
            "sent_at_ms": envelope.sent_at_ms,
            "received_at_ms": received_at_ms,
            "network_age_ms": max(0, received_at_ms - envelope.sent_at_ms),
            "deadman": True,
            "linear_mps": list(linear),
            "angular_rps": list(angular),
            "duration_ms": duration if execute_speed_command else 0,
            "clamped": clamped,
            "deadbanded": pose_deadbanded,
        }
        if motion_id is not None:
            last_command["motion_id"] = motion_id
        if audit:
            last_command.update(audit)
        self._last_command = last_command
        await self._send_ack(
            session,
            envelope,
            accepted=True,
            clamped=clamped,
            detail=(
                "pose angular velocity was inside the server deadband; no motion executed"
                if pose_deadbanded
                else ""
            ),
        )
        await self._maybe_send_lease_status(session, force=False)

    async def _gripper(self, session: ClientSession, envelope: Envelope) -> None:
        if require_bool(envelope.body, "deadman") is not True:
            raise ProtocolError("DEADMAN_REQUIRED", "deadman must be true")
        requested = require_number(envelope.body, "position_pct")
        lease = self._require_lease(session, envelope.body)
        command_epoch = self._safety_epoch
        position = max(0.0, min(100.0, requested))
        snapshot = await self._read_snapshot()
        if not self._snapshot_ready(snapshot):
            raise ProtocolError("ROBOT_NOT_READY", "robot state does not allow gripper motion")
        envelope_error = self._motion_envelope_error(
            snapshot,
            linear=(0.0, 0.0, 0.0),
            angular=(0.0, 0.0, 0.0),
            duration_ms=0,
        )
        if envelope_error == "WORKSPACE_LIMIT":
            raise ProtocolError(
                envelope_error,
                "current TCP is outside the configured workspace",
            )
        if envelope_error == "ORIENTATION_LIMIT":
            raise ProtocolError(
                envelope_error,
                "current TCP orientation is outside the configured envelope",
            )
        async with self._backend_lock:
            self._assert_command_current(session, lease.lease_id, command_epoch)
            await asyncio.to_thread(self.backend.set_gripper, position)
        if not self._command_is_current(session, lease.lease_id, command_epoch):
            raise ProtocolError(
                "LEASE_REQUIRED",
                "gripper command completed after its lease or safety epoch was invalidated",
            )
        self._last_command = {
            **self._last_command,
            "type": envelope.type,
            "client_seq": envelope.seq,
            "sent_at_ms": envelope.sent_at_ms,
            "received_at_ms": _wall_ms(),
            "network_age_ms": max(0, _wall_ms() - envelope.sent_at_ms),
            "deadman": True,
            "gripper_position_pct": position,
            "clamped": position != requested,
        }
        await self._send_ack(session, envelope, accepted=True, clamped=position != requested)
        await self._maybe_send_lease_status(session, force=False)

    async def _recording_start(self, session: ClientSession, envelope: Envelope) -> None:
        task = require_string(envelope.body, "task")
        requested_id = envelope.body.get("episode_id")
        if requested_id is not None and not isinstance(requested_id, str):
            raise ProtocolError("INVALID_MESSAGE", "episode_id must be a string")
        cameras = envelope.body.get("cameras")
        if not isinstance(cameras, list) or not cameras or not all(isinstance(item, str) for item in cameras):
            raise ProtocolError("INVALID_MESSAGE", "cameras must be a non-empty string array")
        if len(cameras) != len(set(cameras)) or any(item not in self.config.cameras for item in cameras):
            raise ProtocolError(
                "INVALID_MESSAGE",
                "every requested camera must be uniquely configured on the bridge",
            )
        lease = self._require_lease(session, envelope.body)
        command_epoch = self._safety_epoch
        async with self._record_lock:
            self._assert_command_current(session, lease.lease_id, command_epoch)
            status = await asyncio.to_thread(
                self.recorder.start,
                task=task,
                requested_episode_id=requested_id,
                cameras=cameras,
                session_id=session.session_id,
                client_id=session.client_id,
                mode=self.backend.mode,
                context={
                    "protocol": "lm3-teleop.v1",
                    "watchdog_ms": self.config.safety.watchdog_ms,
                    "command_rate_hz": self.config.safety.command_rate_hz,
                    "workspace_min_m": list(self.config.safety.workspace_min_m),
                    "workspace_max_m": list(self.config.safety.workspace_max_m),
                    "orientation_configured": self.config.safety.orientation_configured,
                    "orientation_center_rad": list(self.config.safety.orientation_center_rad),
                    "orientation_tolerance_rad": list(
                        self.config.safety.orientation_tolerance_rad
                    ),
                    "orientation_gimbal_lock_margin_rad": ORIENTATION_GIMBAL_LOCK_MARGIN_RAD,
                    "joint_min_rad": list(self.config.safety.joint_min_rad),
                    "joint_max_rad": list(self.config.safety.joint_max_rad),
                    "joint_limit_margin_rad": self.config.safety.joint_limit_margin_rad,
                },
            )
        self._recording_cameras = list(cameras)
        await self._send_ack(session, envelope, accepted=True)
        await session.send("recording.status", status.body())

    def _require_lease(self, session: ClientSession, body: dict[str, Any]) -> Lease:
        lease_id = require_string(body, "lease_id")
        if self.leases.expired() is not None:
            raise ProtocolError("LEASE_EXPIRED", "control lease expired")
        lease = self.leases.renew(session.session_id, lease_id)
        if lease is None:
            if self.leases.expired() is not None:
                raise ProtocolError("LEASE_EXPIRED", "control lease expired")
            raise ProtocolError("LEASE_REQUIRED", "a valid control lease is required")
        return lease

    async def _stop_expired_lease(self) -> bool:
        if self.leases.expired() is None:
            return False
        await self._safe_stop(
            "LEASE_EXPIRED",
            "control lease expired",
            revoke=True,
            stop_recording=True,
        )
        return True

    async def _maybe_send_lease_status(self, session: ClientSession, *, force: bool) -> None:
        lease = self.leases.current
        if lease is None or lease.session_id != session.session_id:
            return
        now = time.monotonic()
        # A client can only extend its local lease deadline from control.status;
        # heartbeat ACKs intentionally carry no authoritative expiry.  Make a
        # renewed deadline eligible after at most half of a short lease, while
        # retaining the 2 Hz cap for ordinary 1-2 second leases.
        refresh_interval_s = min(0.5, lease.duration_ms / 2_000)
        if force or now - session.last_lease_status_monotonic >= refresh_interval_s:
            session.last_lease_status_monotonic = now
            await self._send_control_status(session, lease, granted=True, reason="renewed")

    async def _send_control_status(
        self, session: ClientSession, lease: Lease, *, granted: bool, reason: str
    ) -> None:
        session.last_lease_status_monotonic = time.monotonic()
        await session.send(
            "control.status",
            {
                "granted": granted,
                "lease_id": lease.lease_id if granted else "",
                "owner_client_id": lease.client_id,
                "expires_at_ms": self._lease_expiry(lease),
                "reason": reason,
            },
        )

    async def _send_ack(
        self,
        session: ClientSession,
        envelope: Envelope,
        *,
        accepted: bool,
        clamped: bool | None = None,
        detail: str = "",
    ) -> None:
        body: dict[str, Any] = {
            "ack_seq": envelope.seq,
            "ack_type": envelope.type,
            "accepted": accepted,
        }
        if clamped is not None:
            body["clamped"] = clamped
        if detail:
            body["detail"] = detail
        await session.send("ack", body)

    async def _send_error(self, session: ClientSession, error: ProtocolError) -> None:
        LOGGER.log(
            logging.WARNING if error.recoverable else logging.ERROR,
            "protocol.error session=%s code=%s recoverable=%s ack_seq=%s",
            _session_log_id(session),
            error.code,
            error.recoverable,
            error.ack_seq,
        )
        body: dict[str, Any] = {
            "code": error.code,
            "message": error.message,
            "recoverable": error.recoverable,
        }
        if error.ack_seq is not None:
            body["ack_seq"] = error.ack_seq
        try:
            await session.send("error", body)
        except ConnectionClosed:
            pass

    async def _broadcast(self, message_type: str, body: dict[str, Any]) -> None:
        sessions = [session for session in self.sessions.values() if session.hello_complete]
        if not sessions:
            return
        results = await asyncio.gather(
            *(session.send(message_type, body) for session in sessions), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, ConnectionClosed):
                LOGGER.debug("broadcast failure: %s", result)

    async def _read_snapshot(self) -> RobotSnapshot:
        async with self._backend_lock:
            snapshot_started_monotonic = time.monotonic()
            snapshot = await asyncio.to_thread(self.backend.snapshot)
            return self._remember_snapshot(snapshot, snapshot_started_monotonic)

    def _remember_snapshot(
        self,
        snapshot: RobotSnapshot,
        snapshot_started_monotonic: float,
    ) -> RobotSnapshot:
        self._observe_feedback(snapshot)
        self._latest_snapshot = snapshot
        # Use the sampling start, not completion, so cache age conservatively
        # includes time spent collecting controller fields.
        self._latest_snapshot_started_monotonic = snapshot_started_monotonic
        return snapshot

    def _fresh_motion_snapshot_locked(self) -> RobotSnapshot | None:
        snapshot = self._latest_snapshot
        started = self._latest_snapshot_started_monotonic
        if snapshot is None or started is None:
            return None
        age_ms = (time.monotonic() - started) * 1_000
        max_age_ms = min(MOTION_SNAPSHOT_MAX_AGE_MS, self.config.safety.watchdog_ms)
        if age_ms < 0 or age_ms > max_age_ms:
            return None
        return snapshot

    def _begin_motion_inflight(self, deadline_monotonic: float) -> int:
        self._motion_inflight_generation += 1
        token = self._motion_inflight_generation
        self._motion_inflight_token = token
        self._motion_inflight_deadline_monotonic = deadline_monotonic
        return token

    def _finish_motion_inflight(self, token: int) -> None:
        if self._motion_inflight_token != token:
            return
        self._motion_inflight_token = None
        self._motion_inflight_deadline_monotonic = 0.0

    async def _execute_stop(self, *, invalidate: bool = True) -> None:
        if invalidate:
            self._invalidate_commands()
        self._last_command = {
            **self._last_command,
            "type": "motion.stop",
            "deadman": False,
            "linear_mps": [0.0, 0.0, 0.0],
            "angular_rps": [0.0, 0.0, 0.0],
            "duration_ms": 0,
            "clamped": False,
        }
        timeout_s = self.config.robot.emergency_stop_timeout_ms / 1_000 + 0.05
        async with self._backend_stop_lock:
            await asyncio.wait_for(asyncio.to_thread(self.backend.stop), timeout=timeout_s)

    async def _safe_stop(
        self,
        code: str,
        message: str,
        *,
        revoke: bool,
        stop_recording: bool,
        emit_event: bool = True,
    ) -> bool:
        motion_was_active = self._motion_active
        self._invalidate_commands()
        released = self.leases.release() if revoke else None
        LOGGER.warning(
            "safe_stop code=%s revoke_requested=%s lease_revoked=%s owner_session=%s motion_was_active=%s",
            code,
            revoke,
            released is not None,
            _session_log_id_value(released.session_id) if released is not None else "-",
            motion_was_active,
        )
        stop_error: Exception | None = None
        async with self._stop_lock:
            try:
                await self._execute_stop(invalidate=False)
            except Exception as error:
                stop_error = error
                LOGGER.exception("stop_move failed during %s", code)
            event_code = "STOP_UNCONFIRMED" if stop_error is not None else code
            event_message = (
                f"{code}: {message}; software stop could not be confirmed"
                if stop_error is not None
                else message
            )
            if self.recorder.status.recording:
                async with self._record_lock:
                    if self.recorder.status.recording:
                        self.recorder.add_safety_event(event_code, event_message, _wall_ms())
                    if stop_recording and self.recorder.status.recording:
                        await asyncio.to_thread(self.recorder.stop, code.lower())
                        self._recording_cameras = []
            if emit_event:
                await self._broadcast(
                    "safety.event",
                    {
                        "severity": "error",
                        "code": event_code,
                        "message": event_message,
                        "action": "stop",
                    },
                )
            if released is not None:
                await self._broadcast(
                    "control.status",
                    {
                        "granted": False,
                        "lease_id": "",
                        "owner_client_id": "",
                        "expires_at_ms": 0,
                        "reason": code.lower(),
                    },
                )
        return stop_error is None

    async def _watchdog_loop(self) -> None:
        interval = max(0.02, self.config.safety.watchdog_ms / 4_000)
        while True:
            await asyncio.sleep(interval)
            if await self._stop_expired_lease():
                continue
            now_monotonic = time.monotonic()
            if (
                self._motion_inflight_token is not None
                and now_monotonic > self._motion_inflight_deadline_monotonic
            ):
                await self._safe_stop(
                    "WATCHDOG_TIMEOUT",
                    "motion backend call exceeded the watchdog deadline",
                    revoke=True,
                    stop_recording=True,
                )
                continue
            if self._motion_active:
                age_ms = (now_monotonic - self._last_valid_motion) * 1_000
                if age_ms > self.config.safety.watchdog_ms:
                    await self._safe_stop(
                        "WATCHDOG_TIMEOUT",
                        f"no valid motion command for {int(age_ms)} ms",
                        revoke=True,
                        stop_recording=True,
                    )

    async def _state_loop(self) -> None:
        period = 1.0 / self.config.server.state_hz
        while True:
            started = time.monotonic()
            try:
                snapshot = await self._read_snapshot()
                if self._motion_active and not self._snapshot_ready(snapshot):
                    await self._safe_stop(
                        "ROBOT_NOT_READY",
                        "robot state became unsafe while moving",
                        revoke=True,
                        stop_recording=True,
                    )
                elif self.leases.current is not None or self._motion_active:
                    envelope_error = self._motion_envelope_error(
                        snapshot,
                        linear=(0.0, 0.0, 0.0),
                        angular=(0.0, 0.0, 0.0),
                        duration_ms=0,
                    )
                    if envelope_error is not None:
                        await self._safe_stop(
                            envelope_error,
                            "robot feedback left the configured TCP motion envelope",
                            revoke=True,
                            stop_recording=True,
                        )
                now_mono = time.monotonic()
                if self._feedback_stalled(now_mono):
                    await self._safe_stop(
                        "FEEDBACK_STALLED",
                        "joint and TCP feedback made no directional progress during observable continuous motion",
                        revoke=True,
                        stop_recording=True,
                    )
                watchdog_ok = not self._motion_active or (
                    (time.monotonic() - self._last_valid_motion) * 1_000
                    <= self.config.safety.watchdog_ms
                )
                await self._broadcast(
                    "robot.state",
                    snapshot.protocol_body(
                        watchdog_ok=watchdog_ok,
                        recording=self.recorder.status.recording,
                    ),
                )
                if self.recorder.status.recording:
                    images, camera_status = self.camera.latest()
                    for camera_name in self._recording_cameras:
                        camera_status.setdefault(
                            camera_name,
                            "waiting_for_frame" if camera_name in self.config.cameras else "camera_not_configured",
                        )
                    lease = self.leases.current
                    frame = {
                        "wall_time_ms": _wall_ms(),
                        "monotonic_ns": time.monotonic_ns(),
                        "robot": snapshot.protocol_body(
                            watchdog_ok=watchdog_ok,
                            recording=True,
                        ),
                        "command": dict(self._last_command),
                        "control": {
                            "lease_id": lease.lease_id if lease else "",
                            "owner_client_id": lease.client_id if lease else "",
                        },
                        "safety": {
                            "watchdog_ok": watchdog_ok,
                            "base_locked": snapshot.base_locked,
                            "workspace_configured": self.config.safety.workspace_configured,
                            "orientation_configured": self.config.safety.orientation_configured,
                            "orientation_center_rad": list(
                                self.config.safety.orientation_center_rad
                            ),
                            "orientation_tolerance_rad": list(
                                self.config.safety.orientation_tolerance_rad
                            ),
                            "orientation_gimbal_lock_margin_rad": (
                                ORIENTATION_GIMBAL_LOCK_MARGIN_RAD
                            ),
                        },
                    }
                    async with self._record_lock:
                        await asyncio.to_thread(self.recorder.record, frame, images, camera_status)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.exception("state loop failed")
                await self._safe_stop(
                    "BACKEND_ERROR",
                    str(error),
                    revoke=True,
                    stop_recording=True,
                )
            remaining = period - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)

    def _validate_message_time(self, envelope: Envelope) -> None:
        now = _wall_ms()
        if now - envelope.sent_at_ms > self.config.server.max_message_age_ms:
            raise ProtocolError(
                "STALE_MESSAGE", "message is older than the configured limit", ack_seq=envelope.seq
            )
        if envelope.sent_at_ms - now > self.config.server.max_future_skew_ms:
            raise ProtocolError(
                "STALE_MESSAGE", "message timestamp is too far in the future", ack_seq=envelope.seq
            )

    def _owns_lease(self, session: ClientSession) -> bool:
        return self.leases.current is not None and self.leases.current.session_id == session.session_id

    def _command_is_current(self, session: ClientSession, lease_id: str, epoch: int) -> bool:
        lease = self.leases.current
        return (
            epoch == self._safety_epoch
            and lease is not None
            and lease.session_id == session.session_id
            and lease.lease_id == lease_id
            and lease.expires_monotonic > time.monotonic()
        )

    def _assert_command_current(self, session: ClientSession, lease_id: str, epoch: int) -> None:
        if not self._command_is_current(session, lease_id, epoch):
            raise ProtocolError(
                "LEASE_REQUIRED",
                "command was invalidated by lease expiry or a newer safety stop",
            )

    def _invalidate_commands(self) -> None:
        self._safety_epoch += 1
        self._motion_active = False
        self._clear_feedback_motion_tracking()
        self._motion_inflight_generation += 1
        self._motion_inflight_token = None
        self._motion_inflight_deadline_monotonic = 0.0
        self._pose_states.clear()

    def _clear_feedback_motion_tracking(self) -> None:
        self._feedback_motion_expected = False
        self._motion_command_expires_monotonic = 0.0
        self._feedback_motion_state = None

    def _set_feedback_motion_command(
        self,
        linear: tuple[float, float, float],
        angular: tuple[float, float, float],
        duration_ms: int,
        now_monotonic: float,
    ) -> None:
        snapshot = self._latest_snapshot
        if snapshot is None:
            self._clear_feedback_motion_tracking()
            return
        expires_monotonic = now_monotonic + duration_ms / 1_000
        previous = self._feedback_motion_state
        state_period_s = 1.0 / self.config.server.state_hz
        temporally_continuous = (
            previous is not None
            and now_monotonic <= previous.command_expires_monotonic + state_period_s
        )
        if temporally_continuous and previous is not None:
            self._advance_feedback_expected(previous, now_monotonic)
            same_direction = _same_motion_direction(
                previous.linear_mps, linear
            ) and _same_motion_direction(previous.angular_rps, angular)
            if not same_direction:
                # A direction change needs a fresh directional baseline, but it
                # is not evidence of feedback progress and must not restart the
                # stall window.  Expected travel remains accumulated so rapid
                # reversals cannot indefinitely hide frozen feedback.
                previous.baseline_joint_position_rad = tuple(snapshot.joint_position_rad)
                previous.baseline_tcp_xyz = tuple(
                    snapshot.tcp_pose[axis] for axis in ("x", "y", "z")
                )
                previous.baseline_tcp_orientation_rad = tuple(
                    snapshot.tcp_pose[axis] for axis in ("rx", "ry", "rz")
                )
            previous.linear_mps = linear
            previous.angular_rps = angular
            previous.last_accounted_monotonic = now_monotonic
            previous.command_expires_monotonic = expires_monotonic
        else:
            self._feedback_motion_state = FeedbackMotionState(
                started_monotonic=now_monotonic,
                last_accounted_monotonic=now_monotonic,
                command_expires_monotonic=expires_monotonic,
                linear_mps=linear,
                angular_rps=angular,
                baseline_joint_position_rad=tuple(snapshot.joint_position_rad),
                baseline_tcp_xyz=tuple(snapshot.tcp_pose[axis] for axis in ("x", "y", "z")),
                baseline_tcp_orientation_rad=tuple(
                    snapshot.tcp_pose[axis] for axis in ("rx", "ry", "rz")
                ),
            )
            self._last_feedback_change_monotonic = now_monotonic
        self._feedback_motion_expected = True
        self._motion_command_expires_monotonic = expires_monotonic

    @staticmethod
    def _advance_feedback_expected(state: FeedbackMotionState, now_monotonic: float) -> None:
        active_until = min(now_monotonic, state.command_expires_monotonic)
        if active_until <= state.last_accounted_monotonic:
            return
        duration_s = active_until - state.last_accounted_monotonic
        state.expected_linear_m += _vector_norm(state.linear_mps) * duration_s
        state.expected_angular_rad += _vector_norm(state.angular_rps) * duration_s
        state.last_accounted_monotonic = active_until

    def _feedback_stalled(self, now_monotonic: float) -> bool:
        state = self._feedback_motion_state
        if not self._feedback_motion_expected or state is None:
            return False
        state_period_s = 1.0 / self.config.server.state_hz
        if now_monotonic > state.command_expires_monotonic + state_period_s:
            return False
        self._advance_feedback_expected(state, now_monotonic)
        if (
            now_monotonic - state.started_monotonic
        ) * 1_000 <= self.config.safety.feedback_stall_ms:
            return False
        return (
            state.expected_linear_m
            >= FEEDBACK_MIN_LINEAR_PROGRESS_M * FEEDBACK_OBSERVABILITY_FACTOR
            or state.expected_angular_rad
            >= FEEDBACK_MIN_ANGULAR_PROGRESS_RAD * FEEDBACK_OBSERVABILITY_FACTOR
        )

    def _observe_feedback(self, snapshot: RobotSnapshot) -> None:
        signature = (
            *snapshot.joint_position_rad,
            *(snapshot.tcp_pose[axis] for axis in ("x", "y", "z", "rx", "ry", "rz")),
        )
        state = self._feedback_motion_state
        if state is not None:
            now_monotonic = time.monotonic()
            self._advance_feedback_expected(state, now_monotonic)
            tcp_xyz = tuple(snapshot.tcp_pose[axis] for axis in ("x", "y", "z"))
            tcp_orientation = tuple(
                snapshot.tcp_pose[axis] for axis in ("rx", "ry", "rz")
            )
            linear_progress = _directional_progress(
                tuple(
                    current - baseline
                    for current, baseline in zip(tcp_xyz, state.baseline_tcp_xyz, strict=True)
                ),
                state.linear_mps,
            )
            angular_progress = _directional_progress(
                tuple(
                    shortest_angular_distance_rad(current, baseline)
                    for current, baseline in zip(
                        tcp_orientation,
                        state.baseline_tcp_orientation_rad,
                        strict=True,
                    )
                ),
                state.angular_rps,
            )
            joint_progress = max(
                (
                    abs(current - baseline)
                    for current, baseline, velocity in zip(
                        snapshot.joint_position_rad,
                        state.baseline_joint_position_rad,
                        snapshot.joint_velocity_rad_s,
                        strict=True,
                    )
                    if abs(velocity) > FEEDBACK_JOINT_VELOCITY_EPSILON_RAD_S
                    and (current - baseline) * velocity > 0
                ),
                default=0.0,
            )
            linear_observable = (
                state.expected_linear_m
                >= FEEDBACK_MIN_LINEAR_PROGRESS_M * FEEDBACK_OBSERVABILITY_FACTOR
            )
            angular_observable = (
                state.expected_angular_rad
                >= FEEDBACK_MIN_ANGULAR_PROGRESS_RAD * FEEDBACK_OBSERVABILITY_FACTOR
            )
            progress_confirmed = (
                linear_observable and linear_progress >= FEEDBACK_MIN_LINEAR_PROGRESS_M
            ) or (
                angular_observable and angular_progress >= FEEDBACK_MIN_ANGULAR_PROGRESS_RAD
            ) or (
                (linear_observable or angular_observable)
                and joint_progress >= FEEDBACK_MIN_JOINT_PROGRESS_RAD
            )
            if progress_confirmed:
                state.started_monotonic = now_monotonic
                state.last_accounted_monotonic = now_monotonic
                state.baseline_joint_position_rad = tuple(snapshot.joint_position_rad)
                state.baseline_tcp_xyz = tcp_xyz
                state.baseline_tcp_orientation_rad = tcp_orientation
                state.expected_linear_m = 0.0
                state.expected_angular_rad = 0.0
                self._last_feedback_change_monotonic = now_monotonic
                self._feedback_signature = signature
                return
        previous_signature = self._feedback_signature
        feedback_changed = previous_signature is None or len(signature) != len(previous_signature)
        if not feedback_changed and previous_signature is not None:
            ordinary_end = len(snapshot.joint_position_rad) + 3
            feedback_changed = any(
                abs(current - previous) > FEEDBACK_CHANGE_EPSILON
                for current, previous in zip(
                    signature[:ordinary_end],
                    previous_signature[:ordinary_end],
                    strict=True,
                )
            ) or any(
                abs(shortest_angular_distance_rad(current, previous))
                > FEEDBACK_CHANGE_EPSILON
                for current, previous in zip(
                    signature[ordinary_end:],
                    previous_signature[ordinary_end:],
                    strict=True,
                )
            )
        if feedback_changed:
            self._feedback_signature = signature
            self._last_feedback_change_monotonic = time.monotonic()

    def _motion_envelope_error(
        self,
        snapshot: RobotSnapshot,
        *,
        linear: tuple[float, float, float],
        angular: tuple[float, float, float],
        duration_ms: int,
    ) -> str | None:
        tcp_xyz = tuple(snapshot.tcp_pose[axis] for axis in ("x", "y", "z"))
        if not predict_workspace_ok(tcp_xyz, linear, duration_ms, self.config.safety):
            return "WORKSPACE_LIMIT"
        if self.config.safety.orientation_configured:
            tcp_orientation = tuple(snapshot.tcp_pose[axis] for axis in ("rx", "ry", "rz"))
            if not predict_orientation_ok(
                tcp_orientation,
                angular,
                duration_ms,
                self.config.safety,
            ):
                return "ORIENTATION_LIMIT"
        return None

    def _snapshot_ready(self, snapshot: RobotSnapshot) -> bool:
        return backend_ready(
            snapshot, self.config.safety.allowed_robot_states
        ) and joints_within_margin(snapshot.joint_position_rad, self.config.safety)

    @staticmethod
    def _lease_expiry(lease: Lease) -> int:
        return LeaseManager.expires_at_ms(lease)


def _wall_ms() -> int:
    return int(time.time() * 1_000)


def _require_exact_vector(
    body: dict[str, Any], key: str, axes: tuple[str, str, str]
) -> tuple[float, float, float]:
    value = body.get(key)
    if not isinstance(value, dict) or set(value) != set(axes):
        raise ProtocolError(
            "INVALID_MESSAGE", f"{key} must contain exactly {', '.join(axes)}"
        )
    return tuple(require_number(value, axis) for axis in axes)  # type: ignore[return-value]


def _vector_norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(component * component for component in vector))


def _same_motion_direction(
    previous: tuple[float, float, float], current: tuple[float, float, float]
) -> bool:
    previous_norm = _vector_norm(previous)
    current_norm = _vector_norm(current)
    if previous_norm == 0.0 or current_norm == 0.0:
        return previous_norm == current_norm
    cosine = sum(
        old * new for old, new in zip(previous, current, strict=True)
    ) / (previous_norm * current_norm)
    return cosine >= FEEDBACK_DIRECTION_COSINE_MIN


def _directional_progress(
    displacement: tuple[float, float, float],
    commanded_velocity: tuple[float, float, float],
) -> float:
    velocity_norm = _vector_norm(commanded_velocity)
    if velocity_norm == 0.0:
        return 0.0
    return max(
        0.0,
        sum(
            delta * velocity
            for delta, velocity in zip(displacement, commanded_velocity, strict=True)
        )
        / velocity_norm,
    )


def _raw_message_type(text: str) -> str | None:
    # Safety-only pre-parser: it never authenticates or dispatches a command. It
    # merely lets a session that completed session.hello stop before strict validation.
    try:
        import json

        value = json.loads(text)
    except Exception:
        return None
    return value.get("type") if isinstance(value, dict) and isinstance(value.get("type"), str) else None


def _session_log_id(session: ClientSession) -> str:
    return _session_log_id_value(session.session_id)


def _session_log_id_value(session_id: str) -> str:
    return session_id[:8] if session_id else "-"


def _peer_log_label(websocket: ServerConnection) -> str:
    peer = getattr(websocket, "remote_address", None)
    if isinstance(peer, tuple) and peer:
        host = str(peer[0])
        port = peer[1] if len(peer) > 1 else None
        return f"{host}:{port}" if port is not None else host
    return "unknown"
