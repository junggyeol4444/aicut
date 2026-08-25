"""Data shipped with the package: the default calibration profile and subtitle styles.

These live inside the package rather than beside it because a `pip install` only
carries what the package declares - and without them the very first thing the
CLI does, reading a profile, fails on a fresh install.
"""

from pathlib import Path

RESOURCE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = RESOURCE_DIR / "profiles"
SUBTITLE_STYLE_DIR = RESOURCE_DIR / "subtitle_styles"

__all__ = ["RESOURCE_DIR", "PROFILE_DIR", "SUBTITLE_STYLE_DIR"]
