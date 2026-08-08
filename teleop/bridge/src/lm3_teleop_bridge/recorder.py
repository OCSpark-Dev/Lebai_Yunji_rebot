from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .camera import CapturedImage


_SAFE_EPISODE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_CAMERA = re.compile(r"camera_[A-Za-z0-9_-]+")
_SAFE_IMAGE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_REQUIRED_EPISODE_FILES = {"metadata.json", "frames.jsonl"}


@dataclass(slots=True)
class RecordingStatus:
    recording: bool = False
    episode_id: str = ""
    frame_count: int = 0
    started_at_ms: int = 0
    path: str = ""
    reason: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "recording": self.recording,
            "episode_id": self.episode_id,
            "frame_count": self.frame_count,
            "started_at_ms": self.started_at_ms,
            "path": self.path,
            "reason": self.reason,
        }


class EpisodeRecorder:
    def __init__(self, root: Path, fps: int) -> None:
        self.root = root
        self.fps = fps
        self.status = RecordingStatus()
        self._directory: Path | None = None
        self._frames_handle: Any = None
        self._metadata: dict[str, Any] = {}
        self._last_camera_timestamp: dict[str, int] = {}

    def start(
        self,
        *,
        task: str,
        requested_episode_id: str | None,
        cameras: list[str],
        session_id: str,
        client_id: str,
        mode: str,
        context: dict[str, Any] | None = None,
    ) -> RecordingStatus:
        if self.status.recording:
            raise RuntimeError("an episode is already recording")
        self.root.mkdir(parents=True, exist_ok=True)
        episode_id = _episode_id(requested_episode_id)
        directory = self.root / episode_id
        directory.mkdir(parents=False, exist_ok=False)
        (directory / "images").mkdir()
        started_at_ms = int(time.time() * 1_000)
        self._metadata = {
            "schema": "lm3-teleop.raw-episode.v1",
            "episode_id": episode_id,
            "task": task,
            "fps": self.fps,
            "mode": mode,
            "session_id": session_id,
            "client_id": client_id,
            "context": context or {},
            "cameras_requested": cameras,
            "started_at_ms": started_at_ms,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "complete": False,
            "frame_count": 0,
            "safety_events": [],
        }
        _atomic_json(directory / "metadata.json", self._metadata)
        self._frames_handle = (directory / "frames.jsonl").open("x", encoding="utf-8", newline="\n")
        self._directory = directory
        self._last_camera_timestamp.clear()
        self.status = RecordingStatus(
            recording=True,
            episode_id=episode_id,
            frame_count=0,
            started_at_ms=started_at_ms,
            path=str(directory),
        )
        return self.status

    def add_safety_event(self, code: str, message: str, at_ms: int) -> None:
        if self.status.recording:
            self._metadata["safety_events"].append({"code": code, "message": message, "at_ms": at_ms})

    def record(
        self,
        frame: dict[str, Any],
        images: dict[str, CapturedImage],
        camera_status: dict[str, str],
    ) -> None:
        if not self.status.recording or self._directory is None or self._frames_handle is None:
            return
        frame_index = self.status.frame_count
        image_records: dict[str, Any] = {}
        for name, image in images.items():
            if _SAFE_CAMERA.fullmatch(name) is None:
                raise RuntimeError(f"unsafe camera name from provider: {name}")
            if _SAFE_IMAGE_EXTENSION.fullmatch(image.extension) is None:
                raise RuntimeError(f"unsafe image extension from provider: {image.extension}")
            if self._last_camera_timestamp.get(name) == image.captured_at_ms:
                continue
            relative = Path("images") / name / f"{frame_index:08d}{image.extension}"
            episode_root = self._directory.resolve()
            absolute = (episode_root / relative).resolve()
            if not absolute.is_relative_to(episode_root):
                raise RuntimeError(f"camera output escapes the episode directory: {name}")
            absolute.parent.mkdir(parents=True, exist_ok=True)
            with absolute.open("xb") as handle:
                handle.write(image.data)
            self._last_camera_timestamp[name] = image.captured_at_ms
            image_records[name] = {
                "path": relative.as_posix(),
                "captured_at_ms": image.captured_at_ms,
                "sync_delta_ms": image.captured_at_ms - int(frame["wall_time_ms"]),
            }
        output = dict(frame)
        output["frame_index"] = frame_index
        output["images"] = image_records
        output["camera_status"] = camera_status
        self._frames_handle.write(json.dumps(output, ensure_ascii=False, allow_nan=False) + "\n")
        self._frames_handle.flush()
        self.status.frame_count += 1

    def stop(self, reason: str) -> RecordingStatus:
        if not self.status.recording or self._directory is None:
            return self.status
        if self._frames_handle is not None:
            self._frames_handle.flush()
            os.fsync(self._frames_handle.fileno())
            self._frames_handle.close()
            self._frames_handle = None
        self._metadata.update(
            {
                "complete": reason == "task_complete" or reason == "operator_stop",
                "reason": reason,
                "frame_count": self.status.frame_count,
                "stopped_at_ms": int(time.time() * 1_000),
                "stopped_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _atomic_json(self._directory / "metadata.json", self._metadata)
        _write_manifest(self._directory)
        self.status.recording = False
        self.status.reason = reason
        return self.status


def verify_manifest(directory: Path) -> list[str]:
    root = directory.resolve()
    manifest = root / "manifest.sha256"
    if not manifest.is_file():
        return ["manifest.sha256 is missing"]
    if manifest.is_symlink():
        return ["manifest.sha256 must not be a symlink"]
    failures: list[str] = []
    lines = manifest.read_text(encoding="utf-8").splitlines()
    if not lines:
        failures.append("manifest.sha256 is empty")
    listed: set[str] = set()
    for line in lines:
        expected, separator, relative = line.partition("  ")
        if not separator:
            failures.append(f"invalid manifest line: {line}")
            continue
        if _SHA256.fullmatch(expected) is None:
            failures.append(f"invalid sha256 digest for: {relative}")
            continue
        normalized = _safe_relative_manifest_path(relative)
        if normalized is None:
            failures.append(f"unsafe manifest path: {relative}")
            continue
        if normalized == "manifest.sha256":
            failures.append("manifest.sha256 must not list itself")
            continue
        if normalized in listed:
            failures.append(f"duplicate manifest path: {normalized}")
            continue
        listed.add(normalized)
        path = (root / Path(*PurePosixPath(normalized).parts)).resolve()
        if not path.is_relative_to(root):
            failures.append(f"unsafe manifest path: {relative}")
            continue
        if not path.is_file():
            failures.append(f"missing file: {normalized}")
            continue
        actual = _sha256(path)
        if actual != expected.lower():
            failures.append(f"hash mismatch: {normalized}")

    actual_files: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or path == manifest:
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            failures.append(f"episode contains an escaping symlink: {path.relative_to(root).as_posix()}")
            continue
        actual_files.add(path.relative_to(root).as_posix())

    for required in sorted(_REQUIRED_EPISODE_FILES - listed):
        failures.append(f"required file is not covered by manifest: {required}")
    for relative in sorted(actual_files - listed):
        failures.append(f"unlisted file: {relative}")
    for relative in sorted(listed - actual_files):
        if not any(f"missing file: {relative}" == failure for failure in failures):
            failures.append(f"manifest lists a non-episode file: {relative}")
    return failures


def _episode_id(requested: str | None) -> str:
    if requested:
        cleaned = _SAFE_EPISODE.sub("-", requested.strip()).strip("-._")[:64]
        if cleaned:
            return cleaned
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"episode-{stamp}-{secrets.token_hex(4)}"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _write_manifest(directory: Path) -> None:
    root = directory.resolve()
    manifest = root / "manifest.sha256"
    if manifest.is_symlink():
        raise RuntimeError("manifest.sha256 must not be a symlink")
    entries = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path == manifest:
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise RuntimeError(f"episode contains an escaping symlink: {path.relative_to(root)}")
        relative = path.relative_to(root).as_posix()
        entries.append(f"{_sha256(path)}  {relative}")
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8", newline="\n")


def _safe_relative_manifest_path(value: str) -> str | None:
    if not value or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if path.parts and re.fullmatch(r"[A-Za-z]:", path.parts[0]):
        return None
    return path.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
