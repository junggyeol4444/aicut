"""Face signal (5.2, 5.3, 9.2, 11.1).

1.3 names "no visual awareness" as one of the three failures this project exists
to fix, and three separate judgements need to know where the streamer's face is
and what it is doing:

* **5.3** - whether a stretch is solo talk or gameplay. Without this signal the
  label stays UNKNOWN, because a guess here would quietly mislead every pass
  above it.
* **9.2** - is the person frozen during this silence, or moving? A stunned stare
  and a walk to a quest marker are both silence.
* **11.1** - how much the expression changes, which is one of the three thumbnail
  signals.

OpenCV is optional. When it is absent, every function here returns None rather
than a number, and the callers degrade to UNKNOWN / redistributed weights. A
fabricated face ratio would be worse than no face ratio.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

_CASCADE = "haarcascade_frontalface_default.xml"


@dataclass
class FaceReading:
    """What one sampled frame showed."""

    at_sec: float
    face_ratio: float          # largest face box area / frame area, 0 when none
    face_count: int = 0
    box: tuple[int, int, int, int] | None = None      # x, y, w, h in the source frame

    @property
    def center(self) -> tuple[float, float] | None:
        """Normalised face centre - what the zoom effect needs (10.4-1)."""
        if self.box is None:
            return None
        x, y, w, h = self.box
        return (x + w / 2, y + h / 2)


def available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


class FaceDetector:
    """Face detection over sampled frames, on whichever backend OpenCV offers.

    Two backends, because the answer changed under us: OpenCV 4.x ships a Haar
    cascade and 5.0 removed ``CascadeClassifier`` entirely, so code written for
    one silently detects nothing on the other. ``FaceDetectorYN`` (YuNet) is
    preferred where a model file is available - it is a DNN and more accurate -
    and the cascade is the fallback.

    Either way the question being asked is coarse: "is a face filling much of
    the screen", not "who is this". A better detector can be dropped in behind
    this interface if 17.3 shows the labels are wrong often enough to matter.
    """

    def __init__(
        self,
        cascade_path: str | None = None,
        *,
        model_path: str | None = None,
        scale_factor: float = 1.15,
        min_neighbors: int = 5,
        score_threshold: float = 0.6,
    ):
        import cv2

        self._cv2 = cv2
        self.backend = ""
        self._cascade = None
        self._yunet = None
        self.scale_factor = scale_factor
        self.min_neighbors = min_neighbors

        model = model_path or os.environ.get("AICUT_FACE_MODEL")
        if model and hasattr(cv2, "FaceDetectorYN") and Path(model).exists():
            self._yunet = cv2.FaceDetectorYN.create(str(model), "", (320, 320), score_threshold)
            self.backend = "yunet"
            return

        if hasattr(cv2, "CascadeClassifier"):
            path = cascade_path or str(Path(cv2.data.haarcascades) / _CASCADE)
            cascade = cv2.CascadeClassifier(path)
            if cascade.empty():
                raise RuntimeError(f"could not load the face cascade at {path}")
            self._cascade = cascade
            self.backend = "cascade"
            return

        raise RuntimeError(
            f"OpenCV {cv2.__version__} has no usable face detector: CascadeClassifier was removed in 5.0 "
            "and no YuNet model was supplied. Set AICUT_FACE_MODEL to a face_detection_yunet .onnx file, "
            "or install opencv-python<5."
        )

    def read_frame(self, image_path: str, at_sec: float) -> FaceReading:
        cv2 = self._cv2
        image = cv2.imread(image_path)
        if image is None:
            return FaceReading(at_sec=at_sec, face_ratio=0.0)
        height, width = image.shape[:2]
        boxes = self._detect(image, width, height)
        if not boxes:
            return FaceReading(at_sec=at_sec, face_ratio=0.0)
        x, y, w, h = max(boxes, key=lambda b: b[2] * b[3])
        return FaceReading(
            at_sec=at_sec,
            face_ratio=(w * h) / float(width * height),
            face_count=len(boxes),
            box=(x, y, w, h),
        )

    def _detect(self, image, width: int, height: int) -> list[tuple[int, int, int, int]]:
        cv2 = self._cv2
        if self._yunet is not None:
            self._yunet.setInputSize((width, height))
            _, faces = self._yunet.detect(image)
            if faces is None:
                return []
            return [(int(f[0]), int(f[1]), int(f[2]), int(f[3])) for f in faces]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        found = self._cascade.detectMultiScale(gray, self.scale_factor, self.min_neighbors)
        return [(int(x), int(y), int(w), int(h)) for x, y, w, h in found]

    def read_frames(self, frames: Sequence[tuple[float, str]]) -> list[FaceReading]:
        return [self.read_frame(path, at) for at, path in frames]


def build_detector(cascade_path: str | None = None, *, model_path: str | None = None) -> "FaceDetector | None":
    """A detector, or None when none can be built. Never raises on absence.

    Returning None is the honest answer: the callers then leave the talk/gameplay
    label UNKNOWN rather than guessing it (5.3).
    """
    if not available():
        log.info(
            "OpenCV is not installed: talk/gameplay labelling stays UNKNOWN and the expression"
            " signal is unavailable (5.3, 11.1). Install aicut[vision] to enable it."
        )
        return None
    try:
        detector = FaceDetector(cascade_path, model_path=model_path)
    except Exception as exc:
        log.warning("face detection unavailable (%s); continuing without the face signal", exc)
        return None
    log.info("face detection backend: %s", detector.backend)
    return detector


def face_ratio_lookup(readings: Sequence[FaceReading]):
    """A ``(start, end) -> mean face ratio`` callable for :func:`label_situations`."""
    ordered = sorted(readings, key=lambda r: r.at_sec)

    def lookup(start_sec: float, end_sec: float) -> float:
        inside = [r.face_ratio for r in ordered if start_sec <= r.at_sec <= end_sec]
        return sum(inside) / len(inside) if inside else 0.0

    return lookup


def expression_change(readings: Sequence[FaceReading], at_sec: float, *, window_sec: float = 2.0) -> float:
    """How much the face box moved or resized around a moment (11.1).

    A proxy, and labelled as one: a landmark model would measure the face itself.
    This measures the box, which catches leaning in, recoiling and turning away -
    the movements that make a thumbnail - and misses a change of eyes alone.
    """
    window = [r for r in readings if abs(r.at_sec - at_sec) <= window_sec and r.box]
    if len(window) < 2:
        return 0.0
    ratios = [r.face_ratio for r in window]
    centers = [r.center for r in window if r.center]
    size_change = max(ratios) - min(ratios)
    move = 0.0
    if len(centers) >= 2:
        xs = [c[0] for c in centers]
        ys = [c[1] for c in centers]
        move = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5
    # Normalised against a nominal 1080p frame diagonal so the number stays 0..1.
    return min(1.0, size_change * 4 + move / 2200.0)
