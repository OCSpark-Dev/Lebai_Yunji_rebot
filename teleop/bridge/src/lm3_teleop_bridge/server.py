from __future__ import annotations

import asyncio
import hmac
import logging
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
from .config import AppConfig, ConfigError
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
    predict_workspace_ok,
)


LOGGER = logging.getLogger("lm3_teleop_bridge")
FEEDBACK_CHANGE_EPSILON = 1e-6


@dataclass(slots=True)
class ClientSession:
    websocket: ServerConnection
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    authenticated: bool = False
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


class TeleopServer:
    def __init__(
        self,
        config: AppConfig,
        token: str,
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
        if not isinstance(token, str) or len(token) < 16:
            raise ConfigError("authentication token must contain at least 16 characters")
        self.config = config
        self.token = token
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
        self._latest_snapshot: RobotSnapshot | None = None
        self._recording_cameras: list[str] = []
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
            self.sessions.pop(session.session_id, None)
            if self.leases.current is not None and self.leases.current.session_id == session.session_id:
                await self._safe_stop(
                    "CLIENT_DISCONNECTED",
                    "control owner disconnected",
                    revoke=True,
                    stop_recording=True,
                )

    async def _handle_text(self, session: ClientSession, text: str) -> None:
        stop_was_prioritized = session.authenticated and _raw_message_type(text) == "motion.stop"
        if stop_was_prioritized:
            try:
                if self.leases.current is not None and not self._owns_lease(session):
                    stop_confirmed = await self._safe_stop(
                        "EXTERNAL_STOP",
                        "an authenticated non-owner requested a safety stop",
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
            if session.authenticated and self._owns_lease(session):
                await self._safe_stop(error.code, error.message, revoke=True, stop_recording=True)
            await self._send_error(session, error)
            if not error.recoverable:
                await session.websocket.close(code=1008, reason=error.code)
            return

        if not session.authenticated:
            if envelope.type != "session.hello":
                await self._send_error(
                    session,
                    ProtocolError(
                        "AUTH_FAILED",
                        "session.hello must be the first message",
                        recoverable=False,
                        ack_seq=envelope.seq,
                    ),
                )
                await session.websocket.close(code=1008, reason="authentication required")
                return
            try:
                self._validate_message_time(envelope)
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
            ) or envelope.type == "gripper.set"
            if actuator_message and self._owns_lease(session):
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
        supplied_token = require_string(body, "auth_token")
        capabilities = body.get("capabilities")
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            raise ProtocolError("INVALID_MESSAGE", "capabilities must be a string array")
        if not hmac.compare_digest(supplied_token, self.token):
            await self._send_error(
                session,
                ProtocolError("AUTH_FAILED", "authentication failed", recoverable=False, ack_seq=0),
            )
            await session.websocket.close(code=1008, reason="authentication failed")
            return
        session.authenticated = True
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
                    "joint_min_rad": list(self.config.safety.joint_min_rad),
                    "joint_max_rad": list(self.config.safety.joint_max_rad),
                    "joint_limit_margin_rad": self.config.safety.joint_limit_margin_rad,
                },
            },
        )
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
            lease = self._require_lease(session, envelope.body)
            await self._safe_stop(
                "CONTROL_RELEASED",
                "operator released the control lease",
                revoke=True,
                stop_recording=True,
                emit_event=False,
            )
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
            raise ProtocolError(
                "UNSUPPORTED_MODE",
                "phone pose control is reserved until LM3-specific calibration is complete",
            )

    async def _control_acquire(self, session: ClientSession, envelope: Envelope) -> None:
        body = envelope.body
        requested = require_int(body, "requested_lease_ms")
        hold = require_int(body, "operator_hold_ms")
        safety_ack = body.get("safety_ack")
        if not isinstance(safety_ack, dict):
            raise ProtocolError("INVALID_MESSAGE", "safety_ack must be an object")
        required_checks = ("base_stationary", "workspace_clear", "estop_accessible", "tool_secure")
        if hold < 1_500 or not all(require_bool(safety_ack, item) for item in required_checks):
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
        if self._stop_lock.locked():
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
        if self._stop_lock.locked():
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
        lease = self.leases.acquire(
            session_id=session.session_id,
            client_id=session.client_id,
            requested_ms=requested,
        )
        if lease is None:
            current = self.leases.current
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
        await self._send_control_status(session, lease, granted=True, reason="granted")

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
        command_epoch = self._safety_epoch
        if not session.motion_bucket.consume():
            raise ProtocolError("RATE_LIMITED", "motion command rate exceeds 20 Hz")
        duration = max(
            self.config.safety.min_command_duration_ms,
            min(requested_duration, self.config.safety.max_command_duration_ms),
        )
        linear, angular, velocity_clamped = clamp_twist(linear, angular, self.config.safety)
        clamped = velocity_clamped or duration != requested_duration
        snapshot = await self._read_snapshot()
        if not self._snapshot_ready(snapshot):
            raise ProtocolError("ROBOT_NOT_READY", "robot state does not allow motion")
        tcp_xyz = tuple(snapshot.tcp_pose[axis] for axis in ("x", "y", "z"))
        if not predict_workspace_ok(tcp_xyz, linear, duration, self.config.safety):
            raise ProtocolError("WORKSPACE_LIMIT", "current or predicted TCP is outside the workspace")
        async with self._backend_lock:
            self._assert_command_current(session, lease.lease_id, command_epoch)
            motion_id = await asyncio.to_thread(
                self.backend.speed_cartesian, linear, angular, duration
            )
        if not self._command_is_current(session, lease.lease_id, command_epoch):
            await self._execute_stop()
            raise ProtocolError(
                "LEASE_REQUIRED",
                "motion command was cancelled by a newer safety stop or lease change",
            )
        if motion_id <= 0:
            raise RuntimeError("Lebai speedl did not return a positive motion id")
        now_mono = time.monotonic()
        feedback_motion_expected = any(
            abs(component) > FEEDBACK_CHANGE_EPSILON for component in (*linear, *angular)
        )
        if feedback_motion_expected and not self._feedback_motion_expected:
            self._last_feedback_change_monotonic = now_mono
        self._feedback_motion_expected = feedback_motion_expected
        self._motion_command_expires_monotonic = (
            now_mono + duration / 1_000 if feedback_motion_expected else 0.0
        )
        self._motion_active = True
        self._last_valid_motion = now_mono
        self._last_command = {
            "type": envelope.type,
            "client_seq": envelope.seq,
            "sent_at_ms": envelope.sent_at_ms,
            "received_at_ms": _wall_ms(),
            "network_age_ms": max(0, _wall_ms() - envelope.sent_at_ms),
            "deadman": True,
            "linear_mps": list(linear),
            "angular_rps": list(angular),
            "duration_ms": duration,
            "clamped": clamped,
            "motion_id": motion_id,
        }
        await self._send_ack(session, envelope, accepted=True, clamped=clamped)
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
        lease = self.leases.renew(session.session_id, lease_id)
        if lease is None:
            raise ProtocolError("LEASE_REQUIRED", "a valid control lease is required")
        return lease

    async def _maybe_send_lease_status(self, session: ClientSession, *, force: bool) -> None:
        lease = self.leases.current
        if lease is None or lease.session_id != session.session_id:
            return
        now = time.monotonic()
        if force or now - session.last_lease_status_monotonic >= 0.5:
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
        sessions = [session for session in self.sessions.values() if session.authenticated]
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
            snapshot = await asyncio.to_thread(self.backend.snapshot)
        self._observe_feedback(snapshot)
        self._latest_snapshot = snapshot
        return snapshot

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
        self._invalidate_commands()
        released = self.leases.release() if revoke else None
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
            expired = self.leases.expire()
            if expired is not None:
                await self._safe_stop(
                    "LEASE_EXPIRED",
                    "control lease expired",
                    revoke=False,
                    stop_recording=True,
                )
                continue
            if self._motion_active:
                age_ms = (time.monotonic() - self._last_valid_motion) * 1_000
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
                now_mono = time.monotonic()
                state_period_s = 1.0 / self.config.server.state_hz
                if (
                    self._feedback_motion_expected
                    and now_mono <= self._motion_command_expires_monotonic + state_period_s
                    and (now_mono - self._last_feedback_change_monotonic) * 1_000
                    > self.config.safety.feedback_stall_ms
                ):
                    await self._safe_stop(
                        "FEEDBACK_STALLED",
                        "joint and TCP feedback did not change during continuous non-zero motion",
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
        self._feedback_motion_expected = False
        self._motion_command_expires_monotonic = 0.0

    def _observe_feedback(self, snapshot: RobotSnapshot) -> None:
        signature = (
            *snapshot.joint_position_rad,
            *(snapshot.tcp_pose[axis] for axis in ("x", "y", "z", "rx", "ry", "rz")),
        )
        if self._feedback_signature is None or any(
            abs(current - previous) > FEEDBACK_CHANGE_EPSILON
            for current, previous in zip(signature, self._feedback_signature, strict=True)
        ):
            self._feedback_signature = signature
            self._last_feedback_change_monotonic = time.monotonic()

    def _snapshot_ready(self, snapshot: RobotSnapshot) -> bool:
        return backend_ready(
            snapshot, self.config.safety.allowed_robot_states
        ) and joints_within_margin(snapshot.joint_position_rad, self.config.safety)

    @staticmethod
    def _lease_expiry(lease: Lease) -> int:
        return LeaseManager.expires_at_ms(lease)


def _wall_ms() -> int:
    return int(time.time() * 1_000)


def _raw_message_type(text: str) -> str | None:
    # Safety-only pre-parser: it never authenticates or dispatches a command. It
    # merely lets an already-authenticated session stop before strict validation.
    try:
        import json

        value = json.loads(text)
    except Exception:
        return None
    return value.get("type") if isinstance(value, dict) and isinstance(value.get("type"), str) else None
