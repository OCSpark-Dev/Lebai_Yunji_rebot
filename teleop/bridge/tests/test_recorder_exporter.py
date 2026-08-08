import io
import json
import sys
from types import ModuleType
from pathlib import Path

import pytest

import lm3_teleop_bridge.exporter as exporter_module
from lm3_teleop_bridge.camera import CapturedImage
from lm3_teleop_bridge.exporter import (
    ExportError,
    create_export_plan,
    export_lerobot_v3,
    load_episode,
)
from lm3_teleop_bridge.recorder import EpisodeRecorder, _write_manifest, verify_manifest


def _frame(wall_time_ms: int, joint_offset: float) -> dict:
    return {
        "wall_time_ms": wall_time_ms,
        "monotonic_ns": wall_time_ms * 1_000_000,
        "robot": {
            "robot_state": "IDLE",
            "estop_reason": "",
            "joint_position_rad": [joint_offset + index for index in range(6)],
            "joint_velocity_rad_s": [0.0] * 6,
            "tcp_pose": {"x": 0.4, "y": 0.0, "z": 0.3, "rx": 0.0, "ry": 3.14, "rz": 0.0},
            "gripper_pct": 50.0,
            "base_locked": True,
            "watchdog_ok": True,
            "recording": True,
        },
        "command": {
            "linear_mps": [0.01, 0.0, 0.0],
            "angular_rps": [0.0, 0.0, 0.0],
            "network_age_ms": 12,
        },
        "control": {"lease_id": "lease", "owner_client_id": "phone"},
        "safety": {"watchdog_ok": True, "base_locked": True},
    }


def test_episode_manifest_and_next_state_export_plan(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(tmp_path, fps=20)
    status = recorder.start(
        task="拿起红色方块",
        requested_episode_id="demo-1",
        cameras=["camera_wrist"],
        session_id="session",
        client_id="phone",
        mode="simulator",
    )
    recorder.record(
        _frame(1_000, 0.0),
        {"camera_wrist": CapturedImage(b"first", 1_000)},
        {"camera_wrist": "ok"},
    )
    recorder.record(
        _frame(1_050, 0.1),
        {"camera_wrist": CapturedImage(b"second", 1_050)},
        {"camera_wrist": "ok"},
    )
    recorder.stop("task_complete")

    episode_path = Path(status.path)
    assert verify_manifest(episode_path) == []
    episode = load_episode(episode_path)
    plan = create_export_plan([episode], ["camera_wrist"], max_image_delta_ms=20)
    assert len(plan[0]["rows"]) == 1
    assert plan[0]["rows"][0]["observation.state"][0] == 0.0
    assert plan[0]["rows"][0]["action"][0] == 0.1
    assert plan[0]["rows"][0]["task"] == "拿起红色方块"
    assert plan[0]["rows"][0]["teleop.timestamp_s"] == [0.0]


def test_pose_sample_exports_executed_cartesian_velocity() -> None:
    frame = _frame(1_000, 0.0)
    frame["command"] = {
        "type": "pose.sample",
        "linear_mps": [0.0, 0.0, 0.0],
        "angular_rps": [0.05, -0.1, 0.15],
    }

    assert exporter_module.action_velocity(frame) == [0.0, 0.0, 0.0, 0.05, -0.1, 0.15]


def test_manifest_requires_complete_safe_file_set(tmp_path: Path) -> None:
    episode = tmp_path / "episode"
    episode.mkdir()
    (episode / "metadata.json").write_text("{}", encoding="utf-8")
    (episode / "frames.jsonl").write_text("{}\n", encoding="utf-8")
    (episode / "manifest.sha256").write_text("", encoding="utf-8")

    failures = verify_manifest(episode)

    assert "manifest.sha256 is empty" in failures
    assert "required file is not covered by manifest: metadata.json" in failures
    assert "unlisted file: frames.jsonl" in failures


def test_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    episode = tmp_path / "episode"
    episode.mkdir()
    (episode / "metadata.json").write_text("{}", encoding="utf-8")
    (episode / "frames.jsonl").write_text("{}\n", encoding="utf-8")
    (episode / "manifest.sha256").write_text(f"{'0' * 64}  ../outside\n", encoding="utf-8")

    assert "unsafe manifest path: ../outside" in verify_manifest(episode)


def test_manifest_rejects_root_symlink(tmp_path: Path) -> None:
    episode = tmp_path / "episode"
    episode.mkdir()
    outside = tmp_path / "outside-manifest"
    outside.write_text("", encoding="utf-8")
    try:
        (episode / "manifest.sha256").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this host")

    assert verify_manifest(episode) == ["manifest.sha256 must not be a symlink"]


def test_manifest_covers_nested_file_named_manifest_sha256(tmp_path: Path) -> None:
    episode = tmp_path / "episode"
    nested = episode / "nested"
    nested.mkdir(parents=True)
    (episode / "metadata.json").write_text("{}", encoding="utf-8")
    (episode / "frames.jsonl").write_text("{}\n", encoding="utf-8")
    (nested / "manifest.sha256").write_text("payload", encoding="utf-8")

    _write_manifest(episode)

    manifest_text = (episode / "manifest.sha256").read_text(encoding="utf-8")
    assert "nested/manifest.sha256" in manifest_text
    assert verify_manifest(episode) == []


def test_recorder_rejects_escaping_camera_name(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(tmp_path, fps=20)
    recorder.start(
        task="test",
        requested_episode_id="unsafe-camera",
        cameras=["camera_wrist"],
        session_id="session",
        client_id="phone",
        mode="simulator",
    )

    with pytest.raises(RuntimeError, match="unsafe camera name"):
        recorder.record(
            _frame(1_000, 0.0),
            {"..\\..\\escaped": CapturedImage(b"image", 1_000)},
            {},
        )
    assert not (tmp_path / "escaped").exists()


def test_recorder_rejects_unsafe_image_extension(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(tmp_path, fps=20)
    recorder.start(
        task="test",
        requested_episode_id="unsafe-extension",
        cameras=["camera_wrist"],
        session_id="session",
        client_id="phone",
        mode="simulator",
    )

    with pytest.raises(RuntimeError, match="unsafe image extension"):
        recorder.record(
            _frame(1_000, 0.0),
            {"camera_wrist": CapturedImage(b"image", 1_000, "/../../outside")},
            {},
        )
    assert not (tmp_path / "outside").exists()


def test_recorder_rejects_escaping_camera_directory_symlink(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(tmp_path / "raw", fps=20)
    status = recorder.start(
        task="test",
        requested_episode_id="camera-symlink",
        cameras=["camera_wrist"],
        session_id="session",
        client_id="phone",
        mode="simulator",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    images = Path(status.path) / "images"
    images.mkdir(exist_ok=True)
    try:
        (images / "camera_wrist").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(RuntimeError, match="escapes the episode directory"):
        recorder.record(
            _frame(1_000, 0.0),
            {"camera_wrist": CapturedImage(b"image", 1_000)},
            {},
        )
    assert list(outside.iterdir()) == []


def test_fixed_fps_resampling_does_not_extrapolate_past_episode_end(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(tmp_path, fps=20)
    status = recorder.start(
        task="test",
        requested_episode_id="no-tail-extrapolation",
        cameras=[],
        session_id="session",
        client_id="phone",
        mode="simulator",
    )
    recorder.record(_frame(1_000, 0.0), {}, {})
    recorder.record(_frame(1_080, 0.1), {}, {})
    recorder.stop("task_complete")

    plan = create_export_plan([load_episode(Path(status.path))], [], max_image_delta_ms=20)

    assert len(plan[0]["rows"]) == 1
    assert plan[0]["rows"][0]["observation.state"][0] == 0.0
    assert plan[0]["rows"][0]["action"][0] == 0.1


def test_fixed_fps_resampling_uses_integer_grid_above_float_precision_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = EpisodeRecorder(tmp_path, fps=20)
    status = recorder.start(
        task="test",
        requested_episode_id="large-monotonic",
        cameras=[],
        session_id="session",
        client_id="phone",
        mode="simulator",
    )
    first_ns = 9_642_988_448_908_287
    for index in range(40):
        frame = _frame(1_000 + index * 50, index / 10)
        frame["monotonic_ns"] = first_ns + index * 50_000_000
        recorder.record(frame, {}, {})
    recorder.stop("task_complete")
    episode = load_episode(Path(status.path))
    targets: list[int] = []
    original_nearest_frame = exporter_module._nearest_frame

    def capture_target(frames: list[dict], timestamps: list[int], target_ns: int) -> dict:
        targets.append(target_ns)
        return original_nearest_frame(frames, timestamps, target_ns)

    monkeypatch.setattr(exporter_module, "_nearest_frame", capture_target)
    resampled = exporter_module._resample_episode(episode)

    assert len(resampled) == 40
    assert all(isinstance(target, int) for target in targets)
    assert targets[-1] == int(episode.frames[-1]["monotonic_ns"])


def test_load_episode_checks_declared_frame_count(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(tmp_path, fps=20)
    status = recorder.start(
        task="test",
        requested_episode_id="count-mismatch",
        cameras=[],
        session_id="session",
        client_id="phone",
        mode="simulator",
    )
    recorder.record(_frame(1_000, 0.0), {}, {})
    recorder.record(_frame(1_050, 0.1), {}, {})
    recorder.stop("task_complete")
    episode = Path(status.path)
    metadata_path = episode / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["frame_count"] = 99
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _write_manifest(episode)

    with pytest.raises(ExportError, match="does not match"):
        load_episode(episode)


def test_export_rejects_episodes_without_a_shared_camera(tmp_path: Path) -> None:
    episode_paths: list[str] = []
    for episode_id, camera in (("wrist", "camera_wrist"), ("top", "camera_top")):
        recorder = EpisodeRecorder(tmp_path / "raw", fps=20)
        status = recorder.start(
            task="test",
            requested_episode_id=episode_id,
            cameras=[camera],
            session_id="session",
            client_id="phone",
            mode="simulator",
        )
        recorder.record(_frame(1_000, 0.0), {camera: CapturedImage(b"first", 1_000)}, {})
        recorder.record(_frame(1_050, 0.1), {camera: CapturedImage(b"second", 1_050)}, {})
        recorder.stop("task_complete")
        episode_paths.append(status.path)

    with pytest.raises(ExportError, match="shared by every episode"):
        export_lerobot_v3(episode_paths, tmp_path / "export")


def test_export_removes_partial_output_when_finalize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_module = pytest.importorskip("PIL.Image")
    pytest.importorskip("numpy")
    image_buffer = io.BytesIO()
    image_module.new("RGB", (4, 3), color=(255, 0, 0)).save(image_buffer, format="JPEG")
    image_bytes = image_buffer.getvalue()

    recorder = EpisodeRecorder(tmp_path / "raw", fps=20)
    status = recorder.start(
        task="test",
        requested_episode_id="cleanup",
        cameras=["camera_wrist"],
        session_id="session",
        client_id="phone",
        mode="simulator",
    )
    recorder.record(
        _frame(1_000, 0.0),
        {"camera_wrist": CapturedImage(image_bytes, 1_000)},
        {"camera_wrist": "ok"},
    )
    recorder.record(
        _frame(1_050, 0.1),
        {"camera_wrist": CapturedImage(image_bytes, 1_050)},
        {"camera_wrist": "ok"},
    )
    recorder.stop("task_complete")

    class FailingDataset:
        @classmethod
        def create(cls, *, root: Path, **kwargs):
            root.mkdir(parents=True, exist_ok=False)
            return cls()

        def add_frame(self, frame: dict) -> None:
            return None

        def save_episode(self, parallel_encoding: bool) -> None:
            return None

        def finalize(self) -> None:
            raise RuntimeError("footer failed")

    lerobot_module = ModuleType("lerobot")
    datasets_module = ModuleType("lerobot.datasets")
    dataset_module = ModuleType("lerobot.datasets.lerobot_dataset")
    dataset_module.LeRobotDataset = FailingDataset  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lerobot", lerobot_module)
    monkeypatch.setitem(sys.modules, "lerobot.datasets", datasets_module)
    monkeypatch.setitem(sys.modules, "lerobot.datasets.lerobot_dataset", dataset_module)
    output = tmp_path / "export"

    with pytest.raises(ExportError, match="footer failed"):
        export_lerobot_v3([status.path], output, cameras=["camera_wrist"])
    assert not output.exists()
