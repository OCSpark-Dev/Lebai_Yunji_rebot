from types import SimpleNamespace

import pytest

from lm3_teleop_bridge import backends
from lm3_teleop_bridge.backends import HardwareBackend
from lm3_teleop_bridge.config import RobotConfig


class _TcpProxy:
    def __init__(self) -> None:
        self._values = {
            "x": 0.4,
            "y": 0.0,
            "z": 0.3,
            "rx": 0.0,
            "ry": 3.14,
            "rz": 0.0,
        }

    def __getitem__(self, key: str) -> float:
        return self._values[key]


class _FakeRobot:
    def __init__(self, ip: str, simulator: bool) -> None:
        assert ip == "192.0.2.10"
        assert simulator is False

    def get_robot_state(self) -> int:
        return 7

    def get_estop_reason(self) -> int:
        return 0

    def get_actual_joint_positions(self) -> list[float]:
        return [0.1] * 6

    def get_actual_joint_speed(self) -> list[float]:
        return [0.0] * 6

    def get_actual_tcp_pose(self) -> _TcpProxy:
        return _TcpProxy()

    def get_claw(self) -> object:
        return SimpleNamespace(force=20.0, amplitude=42.0, hold_on=True)


class _FakeNative:
    def __init__(self) -> None:
        self.get_kin_data_calls = 0

    def get_kin_data(self) -> object:
        self.get_kin_data_calls += 1
        return SimpleNamespace(
            actual_joint_pose=[0.2] * 6,
            actual_joint_speed=[0.01] * 6,
            actual_tcp_pose=_TcpProxy(),
        )


class _FastFakeRobot(_FakeRobot):
    def __init__(self, ip: str, simulator: bool) -> None:
        super().__init__(ip, simulator)
        self.native = _FakeNative()

    def get_actual_joint_positions(self) -> list[float]:
        raise AssertionError("fast snapshot must use native.get_kin_data")

    def get_actual_joint_speed(self) -> list[float]:
        raise AssertionError("fast snapshot must use native.get_kin_data")

    def get_actual_tcp_pose(self) -> _TcpProxy:
        raise AssertionError("fast snapshot must use native.get_kin_data")


def test_hardware_snapshot_accepts_swig_proxy_and_clawdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backends.importlib,
        "import_module",
        lambda name: SimpleNamespace(Robot=_FakeRobot),
    )
    backend = HardwareBackend(RobotConfig(robot_ip="192.0.2.10", base_locked=True))

    snapshot = backend.snapshot()

    assert snapshot.robot_state == "MOVING"
    assert snapshot.robot_state_code == 7
    assert snapshot.tcp_pose["x"] == pytest.approx(0.4)
    assert snapshot.gripper_pct == pytest.approx(42.0)


def test_hardware_snapshot_uses_single_native_kin_data_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backends.importlib,
        "import_module",
        lambda name: SimpleNamespace(Robot=_FastFakeRobot),
    )
    backend = HardwareBackend(RobotConfig(robot_ip="192.0.2.10", base_locked=True))

    snapshot = backend.snapshot()

    assert backend._robot.native.get_kin_data_calls == 1  # type: ignore[attr-defined]
    assert snapshot.joint_position_rad == pytest.approx([0.2] * 6)
    assert snapshot.joint_velocity_rad_s == pytest.approx([0.01] * 6)
    assert snapshot.tcp_pose["x"] == pytest.approx(0.4)


def test_hardware_import_error_requires_built_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(name: str) -> object:
        raise ImportError("missing l_master")

    monkeypatch.setattr(backends.importlib, "import_module", unavailable)
    with pytest.raises(RuntimeError, match="built wheel/extension"):
        HardwareBackend(RobotConfig(robot_ip="192.0.2.10"))


def test_direct_stop_uses_bounded_independent_json_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class FakeSocket:
        def settimeout(self, value: float) -> None:
            calls["socket_timeout"] = value

    class FakeResponse:
        status = 200

        def read(self, amount: int | None = None) -> bytes:
            return b'{"jsonrpc":"2.0","id":1,"result":null}'

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            calls.update(host=host, port=port, timeout=timeout)
            self.sock = None

        def connect(self) -> None:
            self.sock = FakeSocket()

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            calls.update(method=method, path=path, body=body, headers=headers)

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(backends.http.client, "HTTPConnection", FakeConnection)

    backends._direct_stop_move("192.0.2.10", 3031, 200)

    assert calls["host"] == "192.0.2.10"
    assert calls["port"] == 3031
    assert calls["timeout"] == pytest.approx(0.2)
    assert b'"method":"stop_move"' in calls["body"]
    assert calls["closed"] is True


@pytest.mark.parametrize("timeout_stage", ["connect", "request", "first_byte", "body"])
def test_direct_stop_timeout_reports_phase_and_total_elapsed(
    monkeypatch: pytest.MonkeyPatch, timeout_stage: str
) -> None:
    calls: dict[str, object] = {}

    class FakeSocket:
        def settimeout(self, value: float) -> None:
            calls["socket_timeout"] = value

    class FakeResponse:
        status = 200

        def read(self, amount: int | None = None) -> bytes:
            if timeout_stage == "body":
                raise TimeoutError("body timed out")
            return b'{"jsonrpc":"2.0","id":1,"result":null}'

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.sock = None

        def connect(self) -> None:
            if timeout_stage == "connect":
                raise TimeoutError("connect timed out")
            self.sock = FakeSocket()

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            if timeout_stage == "request":
                raise TimeoutError("request timed out")

        def getresponse(self) -> FakeResponse:
            if timeout_stage == "first_byte":
                raise TimeoutError("first byte timed out")
            return FakeResponse()

        def close(self) -> None:
            calls["closed"] = True

    monotonic_values = iter([10.0, 10.125])
    monkeypatch.setattr(backends.time, "monotonic", lambda: next(monotonic_values, 10.125))
    monkeypatch.setattr(backends.http.client, "HTTPConnection", FakeConnection)

    with pytest.raises(TimeoutError) as captured:
        backends._direct_stop_move("192.0.2.10", 3031, 200)

    message = str(captured.value)
    assert f"stage={timeout_stage}" in message
    assert "total_elapsed_ms=125.000" in message
    assert "configured_timeout_ms=200" in message
    assert calls["closed"] is True


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b'{"jsonrpc":"2.0","id":2,"result":null}',
        b'{"jsonrpc":"2.0","id":1}',
        b'{"jsonrpc":"2.0","id":1,"error":{"code":-1}}',
    ],
)
def test_direct_stop_rejects_unconfirmed_json_rpc_response(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    class FakeSocket:
        def settimeout(self, value: float) -> None:
            return None

    class FakeResponse:
        status = 200

        def read(self, amount: int | None = None) -> bytes:
            return payload

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.sock = None

        def connect(self) -> None:
            self.sock = FakeSocket()

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            return None

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            return None

    monkeypatch.setattr(backends.http.client, "HTTPConnection", FakeConnection)

    with pytest.raises(RuntimeError, match="error response"):
        backends._direct_stop_move("192.0.2.10", 3031, 200)
