import math
from pathlib import Path

import pytest

from lm3_teleop_bridge.config import (
    AppConfig,
    ConfigError,
    RecordingConfig,
    RobotConfig,
    SafetyConfig,
    ServerConfig,
    load_config,
)


def test_load_simulator_config_resolves_recording_root(tmp_path: Path) -> None:
    config_path = tmp_path / "sim.toml"
    config_path.write_text(
        """
[server]
host = "127.0.0.1"

[robot]
backend = "simulator"

[recording]
root = "records"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.recording.root == (tmp_path / "records").resolve()
    config.validate()


def test_non_loopback_requires_two_explicit_flags() -> None:
    config = AppConfig(server=ServerConfig(host="0.0.0.0", allow_lan=True))
    with pytest.raises(ConfigError, match="--allow-lan"):
        config.validate(allow_lan_flag=False)
    config.validate(allow_lan_flag=True)


def test_string_false_cannot_enable_lan_listening(tmp_path: Path) -> None:
    config_path = tmp_path / "allow-lan.toml"
    config_path.write_text(
        """
[server]
host = "0.0.0.0"
allow_lan = "false"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    with pytest.raises(ConfigError, match=r"server\.allow_lan must be boolean"):
        config.validate(allow_lan_flag=True)


def test_hardware_requires_all_interlocks() -> None:
    config = AppConfig(
        robot=RobotConfig(
            backend="hardware",
            robot_ip="192.168.1.20",
            base_locked=True,
            hardware_enabled=True,
        ),
        safety=SafetyConfig(workspace_configured=False),
    )
    with pytest.raises(ConfigError, match="measured TCP workspace"):
        config.validate(hardware_flag=True)


def test_hardware_defaults_do_not_claim_site_measurements() -> None:
    config = AppConfig(
        robot=RobotConfig(
            backend="hardware",
            robot_ip="192.168.1.20",
            hardware_enabled=True,
        )
    )
    with pytest.raises(ConfigError, match="base_locked"):
        config.validate(hardware_flag=True)


def test_hardware_requires_measured_orientation_envelope() -> None:
    config = AppConfig(
        robot=RobotConfig(
            backend="hardware",
            robot_ip="192.168.1.20",
            base_locked=True,
            hardware_enabled=True,
        ),
        safety=SafetyConfig(
            workspace_configured=True,
            joint_limits_configured=True,
            orientation_configured=False,
        ),
    )
    with pytest.raises(ConfigError, match="orientation envelope"):
        config.validate(hardware_flag=True)


@pytest.mark.parametrize("field", ["workspace_configured", "joint_limits_configured"])
def test_string_false_cannot_bypass_measured_hardware_gate(
    tmp_path: Path,
    field: str,
) -> None:
    config_path = tmp_path / f"{field}.toml"
    config_path.write_text(
        f"""
[safety]
{field} = "false"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    with pytest.raises(ConfigError, match=rf"safety\.{field} must be boolean"):
        config.validate()


@pytest.mark.parametrize(
    "tolerance",
    [
        (0.0, 0.1, 0.1),
        (-0.01, 0.1, 0.1),
        (0.351, 0.1, 0.1),
    ],
)
def test_orientation_tolerance_must_be_positive_and_conservative(
    tolerance: tuple[float, float, float],
) -> None:
    config = AppConfig(safety=SafetyConfig(orientation_tolerance_rad=tolerance))
    with pytest.raises(ConfigError, match="orientation tolerances"):
        config.validate()


def test_non_finite_orientation_center_is_rejected() -> None:
    config = AppConfig(
        safety=SafetyConfig(orientation_center_rad=(0.0, float("nan"), 0.0))
    )
    with pytest.raises(ConfigError, match="orientation center"):
        config.validate()


def test_orientation_envelope_dimensions_are_validated() -> None:
    config = AppConfig(
        safety=SafetyConfig(
            orientation_center_rad=(0.0, 0.0),  # type: ignore[arg-type]
        )
    )
    with pytest.raises(ConfigError, match="three values"):
        config.validate()


def test_orientation_envelope_cannot_include_zyx_gimbal_lock_region() -> None:
    config = AppConfig(
        safety=SafetyConfig(
            orientation_center_rad=(0.0, math.pi / 2, 0.0),
            orientation_tolerance_rad=(0.05, 0.05, 0.05),
        )
    )
    with pytest.raises(ConfigError, match=r"away from \+/-pi/2"):
        config.validate()


def test_load_config_converts_orientation_arrays_to_tuples(tmp_path: Path) -> None:
    config_path = tmp_path / "orientation.toml"
    config_path.write_text(
        """
[safety]
orientation_configured = true
orientation_center_rad = [0.1, 0.0, -3.1]
orientation_tolerance_rad = [0.05, 0.06, 0.07]
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.safety.orientation_center_rad == (0.1, 0.0, -3.1)
    assert config.safety.orientation_tolerance_rad == (0.05, 0.06, 0.07)
    config.validate()


def test_non_finite_safety_values_are_rejected() -> None:
    config = AppConfig(safety=SafetyConfig(max_linear_mps=float("nan")))
    with pytest.raises(ConfigError, match="velocity limits"):
        config.validate()


def test_recording_fps_must_match_state_sampling_rate() -> None:
    config = AppConfig(
        server=ServerConfig(state_hz=20),
        recording=RecordingConfig(fps=10),
    )
    with pytest.raises(ConfigError, match="must equal"):
        config.validate()


def test_emergency_stop_timeout_must_fit_watchdog() -> None:
    config = AppConfig(
        robot=RobotConfig(emergency_stop_timeout_ms=301),
        safety=SafetyConfig(watchdog_ms=300),
    )
    with pytest.raises(ConfigError, match="no longer than the watchdog"):
        config.validate()


def test_feedback_stall_window_must_cover_two_state_samples() -> None:
    config = AppConfig(
        server=ServerConfig(state_hz=5),
        safety=SafetyConfig(watchdog_ms=300, feedback_stall_ms=250),
        recording=RecordingConfig(fps=5),
    )
    with pytest.raises(ConfigError, match="two configured state samples"):
        config.validate()


def test_v1_robot_state_allowlist_cannot_enable_teaching() -> None:
    config = AppConfig(safety=SafetyConfig(allowed_robot_states=(5, 11)))
    with pytest.raises(ConfigError, match="exactly IDLE=5 and MOVING=7"):
        config.validate()


def test_token_never_has_a_config_default() -> None:
    config = AppConfig()
    with pytest.raises(ConfigError, match="at least 16"):
        config.resolved_token({})
    assert config.resolved_token({"LM3_TELEOP_TOKEN": "0123456789abcdef"}) == "0123456789abcdef"
