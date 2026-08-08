from __future__ import annotations

import bisect
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .recorder import verify_manifest


class ExportError(ValueError):
    pass


@dataclass(slots=True)
class RawEpisode:
    directory: Path
    metadata: dict[str, Any]
    frames: list[dict[str, Any]]

    @property
    def task(self) -> str:
        return str(self.metadata.get("task", "")).strip()

    @property
    def fps(self) -> int:
        value = self.metadata.get("fps")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ExportError(f"{self.directory.name} metadata fps must be a positive integer")
        return value


def load_episode(directory: str | Path) -> RawEpisode:
    root = Path(directory).resolve()
    failures = verify_manifest(root)
    if failures:
        raise ExportError("raw episode manifest failed: " + "; ".join(failures))
    metadata_path = root / "metadata.json"
    frames_path = root / "frames.jsonl"
    if not metadata_path.is_file() or not frames_path.is_file():
        raise ExportError("raw episode requires metadata.json and frames.jsonl")
    try:
        metadata = json.loads(
            metadata_path.read_text(encoding="utf-8"), parse_constant=_reject_constant
        )
        frames = [
            json.loads(line, parse_constant=_reject_constant)
            for line in frames_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExportError(f"raw episode contains invalid JSON: {exc}") from exc
    if not isinstance(metadata, dict) or not all(isinstance(frame, dict) for frame in frames):
        raise ExportError("metadata and every raw frame must be JSON objects")
    if metadata.get("schema") != "lm3-teleop.raw-episode.v1":
        raise ExportError("unsupported raw episode schema")
    if not metadata.get("complete", False):
        raise ExportError("episode is marked incomplete; review it before export")
    if len(frames) < 2:
        raise ExportError("at least two raw frames are required")
    if not str(metadata.get("task", "")).strip():
        raise ExportError("task text is missing")
    requested_cameras = metadata.get("cameras_requested", [])
    if not isinstance(requested_cameras, list) or not all(
        isinstance(camera, str) and _safe_feature_name(camera) for camera in requested_cameras
    ):
        raise ExportError("metadata cameras_requested must contain safe camera names")
    declared_count = metadata.get("frame_count")
    if isinstance(declared_count, bool) or not isinstance(declared_count, int):
        raise ExportError("metadata frame_count must be an integer")
    if declared_count != len(frames):
        raise ExportError(
            f"metadata frame_count {declared_count} does not match {len(frames)} frame records"
        )
    fps = metadata.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise ExportError("metadata fps must be a positive integer")
    previous_wall = -1
    previous_monotonic = -1
    for index, frame in enumerate(frames):
        if frame.get("frame_index") != index:
            raise ExportError(f"frame {index} has a non-sequential frame_index")
        wall = frame.get("wall_time_ms")
        if not isinstance(wall, int) or wall <= previous_wall:
            raise ExportError(f"frame {index} wall_time_ms is not strictly increasing")
        previous_wall = wall
        monotonic_ns = frame.get("monotonic_ns")
        if not isinstance(monotonic_ns, int) or monotonic_ns <= previous_monotonic:
            raise ExportError(f"frame {index} monotonic_ns is not strictly increasing")
        previous_monotonic = monotonic_ns
        state_vector(frame)
        action_velocity(frame)
        _validate_frame_images(root, frame, index)
    return RawEpisode(root, metadata, frames)


def state_vector(frame: dict[str, Any]) -> list[float]:
    robot = frame.get("robot")
    if not isinstance(robot, dict):
        raise ExportError("frame robot field is missing")
    joints = robot.get("joint_position_rad")
    gripper = robot.get("gripper_pct")
    if not isinstance(joints, list) or len(joints) != 6:
        raise ExportError("joint_position_rad must contain six values")
    try:
        values = [float(value) for value in joints] + [float(gripper)]
    except (TypeError, ValueError) as exc:
        raise ExportError("joint and gripper state must contain numeric values") from exc
    if not all(math.isfinite(value) for value in values):
        raise ExportError("joint and gripper state must contain only finite values")
    return values


def action_velocity(frame: dict[str, Any]) -> list[float]:
    command = frame.get("command", {})
    if not isinstance(command, dict):
        raise ExportError("frame command field must be an object")
    if command.get("type") not in (None, "motion.cartesian_velocity"):
        return [0.0] * 6
    linear = command.get("linear_mps", [0.0, 0.0, 0.0])
    angular = command.get("angular_rps", [0.0, 0.0, 0.0])
    if not isinstance(linear, list) or not isinstance(angular, list) or len(linear) != 3 or len(angular) != 3:
        raise ExportError("command velocity must contain two three-axis arrays")
    try:
        values = [float(value) for value in (*linear, *angular)]
    except (TypeError, ValueError) as exc:
        raise ExportError("command velocity must contain numeric values") from exc
    if not all(math.isfinite(value) for value in values):
        raise ExportError("command velocity must contain only finite values")
    return values


def common_cameras(episodes: list[RawEpisode]) -> list[str]:
    requested_sets = [set(map(str, episode.metadata.get("cameras_requested", []))) for episode in episodes]
    if not requested_sets:
        return []
    return sorted(set.intersection(*requested_sets))


def create_export_plan(
    episodes: list[RawEpisode], cameras: list[str], max_image_delta_ms: int
) -> list[dict[str, Any]]:
    if max_image_delta_ms < 0:
        raise ExportError("max_image_delta_ms must be non-negative")
    if len(cameras) != len(set(cameras)) or not all(_safe_feature_name(camera) for camera in cameras):
        raise ExportError("camera names must be unique safe feature names")
    plan: list[dict[str, Any]] = []
    for episode in episodes:
        camera_samples = _camera_samples(episode, cameras)
        episode_rows = []
        resampled = _resample_episode(episode)
        for index, (target_wall_ms, timestamp_s, frame) in enumerate(resampled[:-1]):
            row: dict[str, Any] = {
                "observation.state": state_vector(frame),
                "action": state_vector(resampled[index + 1][2]),
                "action.cartesian_velocity": action_velocity(frame),
                "teleop.timestamp_s": [timestamp_s],
                "teleop.network_age_ms": [_network_age_ms(frame)],
                "task": episode.task,
                "images": {},
            }
            for camera in cameras:
                sample = _nearest_image(camera_samples[camera], target_wall_ms)
                if sample is None or abs(sample[0] - target_wall_ms) > max_image_delta_ms:
                    raise ExportError(
                        f"{episode.directory.name} frame {index} has no {camera} image within "
                        f"{max_image_delta_ms} ms"
                    )
                row["images"][camera] = str(_resolve_episode_file(episode.directory, sample[1]))
            episode_rows.append(row)
        if not episode_rows:
            raise ExportError(f"episode {episode.directory.name} has no rows after fixed-FPS resampling")
        plan.append({"episode": episode.directory.name, "rows": episode_rows})
    return plan


def export_lerobot_v3(
    episode_directories: list[str | Path],
    output: str | Path,
    *,
    repo_id: str = "local/lm3_up",
    cameras: list[str] | None = None,
    max_image_delta_ms: int = 100,
    lerobot_source: str | Path | None = None,
) -> dict[str, Any]:
    if not episode_directories:
        raise ExportError("at least one raw episode is required")
    episodes = [load_episode(path) for path in episode_directories]
    selected_cameras = common_cameras(episodes) if cameras is None else cameras
    if not selected_cameras:
        raise ExportError(
            "LeRobot VLA export requires at least one camera shared by every episode; "
            "state-only export is not supported"
        )
    plan = create_export_plan(episodes, selected_cameras, max_image_delta_ms)
    output_path = Path(output).resolve()
    if output_path.exists():
        raise ExportError("output directory must not already exist")
    if lerobot_source is not None:
        source = Path(lerobot_source).resolve()
        src_path = source / "src" if (source / "src").is_dir() else source
        sys.path.insert(0, str(src_path))
    try:
        import numpy as np
        from PIL import Image
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError as exc:
        raise ExportError(
            "official export requires LeRobot v0.4.2 plus its dependencies; "
            "install the bridge [export] extra or pass --lerobot-source"
        ) from exc

    first_image_shapes = _validate_export_images(plan, selected_cameras, Image)

    axes = ["q1", "q2", "q3", "q4", "q5", "q6", "gripper"]
    features: dict[str, dict[str, Any]] = {
        "observation.state": {"dtype": "float32", "shape": (7,), "names": axes},
        "action": {"dtype": "float32", "shape": (7,), "names": axes},
        "action.cartesian_velocity": {
            "dtype": "float32",
            "shape": (6,),
            "names": ["vx", "vy", "vz", "vrx", "vry", "vrz"],
        },
        "teleop.timestamp_s": {"dtype": "float32", "shape": (1,), "names": ["time"]},
        "teleop.network_age_ms": {"dtype": "float32", "shape": (1,), "names": ["age"]},
    }
    for camera, shape in first_image_shapes.items():
        features[f"observation.images.{camera}"] = {
            "dtype": "video",
            "shape": shape,
            "names": ["height", "width", "channels"],
        }

    fps_values = {episode.fps for episode in episodes}
    if len(fps_values) != 1:
        raise ExportError("all episodes must use the same fps")
    fps = fps_values.pop()
    dataset = None
    finalized = False
    try:
        dataset = LeRobotDataset.create(
            repo_id=repo_id,
            fps=fps,
            features=features,
            root=output_path,
            robot_type="lm3_up",
            use_videos=bool(selected_cameras),
            image_writer_threads=4 if selected_cameras else 0,
            batch_encoding_size=1,
        )
        for episode in plan:
            for row in episode["rows"]:
                frame = {
                    "observation.state": np.asarray(row["observation.state"], dtype=np.float32),
                    "action": np.asarray(row["action"], dtype=np.float32),
                    "action.cartesian_velocity": np.asarray(
                        row["action.cartesian_velocity"], dtype=np.float32
                    ),
                    "teleop.timestamp_s": np.asarray(row["teleop.timestamp_s"], dtype=np.float32),
                    "teleop.network_age_ms": np.asarray(
                        row["teleop.network_age_ms"], dtype=np.float32
                    ),
                    "task": row["task"],
                }
                for camera in selected_cameras:
                    with Image.open(row["images"][camera]) as image:
                        frame[f"observation.images.{camera}"] = np.asarray(
                            image.convert("RGB"), dtype=np.uint8
                        )
                dataset.add_frame(frame)
            dataset.save_episode(parallel_encoding=False)
        dataset.finalize()
        finalized = True

        reloaded = LeRobotDataset(repo_id=repo_id, root=output_path, download_videos=False)
        expected_frames = sum(len(item["rows"]) for item in plan)
        if len(reloaded) != expected_frames or reloaded.num_episodes != len(plan):
            raise ExportError("LeRobot reload validation did not match exported totals")
        if int(reloaded.fps) != fps:
            raise ExportError("LeRobot reload validation found an unexpected fps")
        if not set(features).issubset(set(reloaded.features)):
            raise ExportError("LeRobot reload validation found missing exported features")
        _validate_stats_file(output_path / "meta" / "stats.json")
    except Exception as exc:
        if dataset is not None and not finalized:
            try:
                dataset.finalize()
            except Exception as finalize_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"secondary finalize failure: {finalize_error}")
        cleanup_error = _cleanup_partial_export(output_path)
        if cleanup_error is not None and hasattr(exc, "add_note"):
            exc.add_note(f"partial export cleanup failed: {cleanup_error}")
        if isinstance(exc, ExportError):
            raise
        raise ExportError(f"official LeRobot export failed: {exc}") from exc

    expected_frames = sum(len(item["rows"]) for item in plan)
    return {
        "output": str(output_path),
        "repo_id": repo_id,
        "episodes": len(plan),
        "frames": expected_frames,
        "cameras": selected_cameras,
        "features": sorted(features),
    }


def _camera_samples(episode: RawEpisode, cameras: list[str]) -> dict[str, list[tuple[int, str]]]:
    result = {camera: [] for camera in cameras}
    for frame in episode.frames:
        images = frame.get("images", {})
        if not isinstance(images, dict):
            continue
        for camera in cameras:
            item = images.get(camera)
            if isinstance(item, dict) and isinstance(item.get("captured_at_ms"), int) and isinstance(
                item.get("path"), str
            ):
                result[camera].append((item["captured_at_ms"], item["path"]))
    for camera, samples in result.items():
        samples.sort()
        if not samples:
            raise ExportError(f"episode {episode.directory.name} contains no frames for {camera}")
    return result


def _nearest_image(samples: list[tuple[int, str]], target_ms: int) -> tuple[int, str] | None:
    timestamps = [item[0] for item in samples]
    index = bisect.bisect_left(timestamps, target_ms)
    candidates = []
    if index < len(samples):
        candidates.append(samples[index])
    if index > 0:
        candidates.append(samples[index - 1])
    return min(candidates, key=lambda item: abs(item[0] - target_ms)) if candidates else None


def _resample_episode(episode: RawEpisode) -> list[tuple[int, float, dict[str, Any]]]:
    fps = episode.fps
    nanoseconds_per_second = 1_000_000_000
    first_ns = int(episode.frames[0]["monotonic_ns"])
    last_ns = int(episode.frames[-1]["monotonic_ns"])
    first_wall = int(episode.frames[0]["wall_time_ms"])
    target_count = ((last_ns - first_ns) * fps) // nanoseconds_per_second + 1
    timestamps = [int(frame["monotonic_ns"]) for frame in episode.frames]
    tolerance_ns = (
        nanoseconds_per_second + (2 * fps) - 1
    ) // (2 * fps) + 5_000_000
    result: list[tuple[int, float, dict[str, Any]]] = []
    for index in range(target_count):
        target_offset_ns = (index * nanoseconds_per_second) // fps
        target_ns = first_ns + target_offset_ns
        frame = _nearest_frame(episode.frames, timestamps, target_ns)
        if abs(int(frame["monotonic_ns"]) - target_ns) > tolerance_ns:
            raise ExportError(
                f"episode {episode.directory.name} has a state-sampling gap near frame {index}"
            )
        target_wall_ms = first_wall + (target_offset_ns + 500_000) // 1_000_000
        result.append((target_wall_ms, index / fps, frame))
    return result


def _nearest_frame(
    frames: list[dict[str, Any]], timestamps: list[int], target_ns: int
) -> dict[str, Any]:
    index = bisect.bisect_left(timestamps, target_ns)
    candidates: list[int] = []
    if index < len(frames):
        candidates.append(index)
    if index > 0:
        candidates.append(index - 1)
    if not candidates:
        raise ExportError("episode contains no state samples")
    selected = min(candidates, key=lambda item: abs(timestamps[item] - target_ns))
    return frames[selected]


def _network_age_ms(frame: dict[str, Any]) -> float:
    command = frame.get("command", {})
    if not isinstance(command, dict):
        raise ExportError("frame command field must be an object")
    value = command.get("network_age_ms")
    if value is None:
        return -1.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ExportError("network_age_ms must be finite when present")
    if value < 0:
        raise ExportError("network_age_ms must not be negative")
    return float(value)


def _validate_frame_images(root: Path, frame: dict[str, Any], frame_index: int) -> None:
    images = frame.get("images", {})
    if not isinstance(images, dict):
        raise ExportError(f"frame {frame_index} images must be an object")
    for camera, item in images.items():
        if not isinstance(camera, str) or not _safe_feature_name(camera) or not isinstance(item, dict):
            raise ExportError(f"frame {frame_index} contains an invalid camera record")
        captured_at_ms = item.get("captured_at_ms")
        path = item.get("path")
        if not isinstance(captured_at_ms, int) or not isinstance(path, str):
            raise ExportError(f"frame {frame_index} camera {camera} record is incomplete")
        sync_delta = item.get("sync_delta_ms")
        if sync_delta is not None and (
            isinstance(sync_delta, bool)
            or not isinstance(sync_delta, (int, float))
            or not math.isfinite(sync_delta)
        ):
            raise ExportError(f"frame {frame_index} camera {camera} sync delta is invalid")
        _resolve_episode_file(root, path)


def _resolve_episode_file(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative or "\x00" in relative:
        raise ExportError(f"unsafe episode file path: {relative}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ExportError(f"unsafe episode file path: {relative}")
    if pure.parts and re.fullmatch(r"[A-Za-z]:", pure.parts[0]):
        raise ExportError(f"unsafe episode file path: {relative}")
    path = (root / Path(*pure.parts)).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ExportError(f"episode file is missing or outside the episode: {relative}")
    return path


def _validate_export_images(
    plan: list[dict[str, Any]], cameras: list[str], image_module: Any
) -> dict[str, tuple[int, int, int]]:
    expected: dict[str, tuple[int, int, int]] = {}
    for episode in plan:
        for row_index, row in enumerate(episode["rows"]):
            for camera in cameras:
                image_path = Path(row["images"][camera])
                try:
                    with image_module.open(image_path) as image:
                        image.verify()
                    with image_module.open(image_path) as image:
                        width, height = image.size
                except Exception as exc:
                    raise ExportError(
                        f"{episode['episode']} row {row_index} contains an invalid {camera} image"
                    ) from exc
                shape = (height, width, 3)
                if camera in expected and expected[camera] != shape:
                    raise ExportError(
                        f"camera {camera} changes shape from {expected[camera]} to {shape}"
                    )
                expected[camera] = shape
    return expected


def _validate_stats_file(path: Path) -> None:
    if not path.is_file():
        raise ExportError("LeRobot export did not produce meta/stats.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExportError("LeRobot stats.json is invalid") from exc
    _assert_finite_tree(value, "stats")


def _assert_finite_tree(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ExportError(f"LeRobot {path} contains a non-finite value")
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            _assert_finite_tree(nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_finite_tree(nested, f"{path}[{index}]")
        return
    raise ExportError(f"LeRobot {path} contains an unsupported value")


def _cleanup_partial_export(path: Path) -> OSError | None:
    if not path.exists():
        return None
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return exc
    return None


def _safe_feature_name(value: str) -> bool:
    return re.fullmatch(r"camera_[A-Za-z0-9_-]+", value) is not None


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant rejected: {value}")
