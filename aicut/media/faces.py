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
    """Haar cascade face detection over sampled frames.

    A cascade is used rather than a DNN on purpose: this runs over every sampled
    frame of a multi-hour broadcast, and the question being asked is coarse -
    "is a face filling much of the screen" - not "who is this". A better detector
    can be dropped in behind the same interface if 17.3 shows the labels are
    wrong often enough to matter.
    """

    def __init__(self, cascade_path: str | None = None, *, scale_factor: float = 1.15, min_neighbors: int = 5):
        import cv2

        self._cv2 = cv2
        path = cascade_path or str(Path(cv2.data.haarcascades) / _CASCADE)
        self._cascade = cv2.CascadeClassifier(path)
        if self._cascade.empty():
            raise RuntimeError(f"could not load the face cascade at {path}")
        self.scale_factor = scale_factor
        self.min_neighbors = min_neighbors

    def read_frame(self, image_path: str, at_sec: float) -> FaceReading:
        cv2 = self._cv2
        image = cv2.imread(image_path)
        if image is None:
            return FaceReading(at_sec=at_sec, face_ratio=0.0)
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        faces = self._cascade.detectMultiScale(gray, self.scale_factor, self.min_neighbors)
        if len(faces) == 0:
            return FaceReading(at_sec=at_sec, face_ratio=0.0)
        biggest = max(faces, key=lambda f: f[2] * f[3])
        x, y, w, h = (int(v) for v in biggest)
        return FaceReading(
            at_sec=at_sec,
            face_ratio=(w * h) / float(width * height),
            face_count=len(faces),
            box=(x, y, w, h),
        )

    def read_frames(self, frames: Sequence[tuple[float, str]]) -> list[FaceReading]:
        return [self.read_frame(path, at) for at, path in frames]


def build_detector(cascade_path: str | None = None) -> "FaceDetector | None":
    """A detector, or None when OpenCV is not installed. Never raises on absence."""
    if not available():
        log.info(
            "OpenCV is not installed: talk/gameplay labelling stays UNKNOWN and the expression"
            " signal is unavailable (5.3, 11.1). Install aicut[vision] to enable it."
        )
        return None
    try:
        return FaceDetector(cascade_path)
    except Exception as exc:
        log.warning("face detection unavailable (%s); continuing without the face signal", exc)
        return None


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
