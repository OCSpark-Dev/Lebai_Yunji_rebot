from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

from .config import CameraConfig


@dataclass(frozen=True, slots=True)
class CapturedImage:
    data: bytes
    captured_at_ms: int
    extension: str = ".jpg"


class CameraProvider(Protocol):
    def latest(self) -> tuple[dict[str, CapturedImage], dict[str, str]]: ...

    def close(self) -> None: ...


class NullCameraProvider:
    def __init__(self, requested: list[str] | None = None) -> None:
        self._requested = requested or []

    def latest(self) -> tuple[dict[str, CapturedImage], dict[str, str]]:
        return {}, {name: "camera_not_configured" for name in self._requested}

    def close(self) -> None:
        return None


class OpenCVCameraProvider:
    """Non-blocking UVC capture. The control loop only copies each worker's latest JPEG."""

    def __init__(self, cameras: dict[str, CameraConfig]) -> None:
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("camera configuration requires the optional opencv-python dependency") from exc
        self._cv2 = cv2
        self._configs = cameras
        self._lock = threading.Lock()
        self._latest: dict[str, CapturedImage] = {}
        self._status: dict[str, str] = {name: "starting" for name in cameras}
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._run_camera, args=(name, config), daemon=True)
            for name, config in cameras.items()
        ]
        for thread in self._threads:
            thread.start()

    def _run_camera(self, name: str, config: CameraConfig) -> None:
        source: int | str = int(config.source) if config.source.isdigit() else config.source
        capture = self._cv2.VideoCapture(source)
        if not capture.isOpened():
            with self._lock:
                self._status[name] = "open_failed"
            capture.release()
            return
        period = 1.0 / max(1, config.fps)
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                ok, frame = capture.read()
                if not ok:
                    with self._lock:
                        self._status[name] = "read_failed"
                    self._stop.wait(min(period, 0.1))
                    continue
                encoded, buffer = self._cv2.imencode(
                    ".jpg", frame, [int(self._cv2.IMWRITE_JPEG_QUALITY), config.jpeg_quality]
                )
                if encoded:
                    image = CapturedImage(bytes(buffer), int(time.time() * 1_000))
                    with self._lock:
                        self._latest[name] = image
                        self._status[name] = "ok"
                remaining = period - (time.monotonic() - started)
                if remaining > 0:
                    self._stop.wait(remaining)
        finally:
            capture.release()

    def latest(self) -> tuple[dict[str, CapturedImage], dict[str, str]]:
        with self._lock:
            return dict(self._latest), dict(self._status)

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1.0)
