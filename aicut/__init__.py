"""aicut - AI autonomous broadcast content producer.

One long livestream VOD goes in; the system understands the whole broadcast,
discovers which self-contained contents exist inside it, decides how many of
them are worth making, and produces that many finished YouTube videos.

Design rule that governs this package: *nothing about the output shape is
hardcoded*. Video structure, content count, content type, cut order and video
length are decisions, not constants (2장). Every judgement threshold lives in a
calibration profile, never in code (17장).
"""

from aicut.version import __version__

__all__ = ["__version__"]
