"""What a `pip install` actually ships.

The package read its calibration profile and subtitle styles from a directory
beside the package, which exists in a git checkout and nowhere else. Installed,
the very first thing the CLI does - load a profile - died with
FileNotFoundError. These guard the shipped surface rather than the code.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aicut.config import DEFAULT_PROFILE_PATH
from aicut.render.subtitles import STYLE_DIR
from aicut.resources import PROFILE_DIR, SUBTITLE_STYLE_DIR

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "aicut"
PROJECT_ROOT = PACKAGE_ROOT.parent

# Windows puts the executables in Scripts\ and names them .exe.
BIN_DIR = "Scripts" if sys.platform == "win32" else "bin"
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


def _venv_exe(venv: Path, name: str) -> Path:
    return venv / BIN_DIR / f"{name}{EXE_SUFFIX}"


class ShippedResourceTests(unittest.TestCase):
    def test_the_default_profile_lives_inside_the_package(self):
        self.assertTrue(DEFAULT_PROFILE_PATH.exists())
        self.assertTrue(
            DEFAULT_PROFILE_PATH.is_relative_to(PACKAGE_ROOT),
            f"{DEFAULT_PROFILE_PATH} sits outside the package and will not be installed",
        )

    def test_the_subtitle_styles_live_inside_the_package(self):
        self.assertTrue((SUBTITLE_STYLE_DIR / "default.json").exists())
        self.assertTrue(STYLE_DIR.is_relative_to(PACKAGE_ROOT))

    def test_the_sql_schema_lives_inside_the_package(self):
        self.assertTrue((PACKAGE_ROOT / "db" / "schema.sql").exists())

    def test_package_data_declares_every_non_python_file_the_code_reads(self):
        import re

        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        block = pyproject.split("[tool.setuptools.package-data]")[1]
        # Stop at the next section header, not the next bracket: the value
        # itself is a bracketed list.
        block = re.split(r"\n\[", block)[0]
        patterns = re.findall(r'"([^"]+)"', block)
        self.assertTrue(patterns, "no package-data patterns were parsed")

        shipped = {
            path.relative_to(PACKAGE_ROOT).as_posix()
            for path in PACKAGE_ROOT.rglob("*")
            if path.is_file() and path.suffix in {".json", ".sql", ".html"}
        }
        for relative in sorted(shipped):
            with self.subTest(file=relative):
                self.assertTrue(
                    any(Path(relative).match(pattern) for pattern in patterns),
                    f"{relative} is read at runtime but no package-data pattern ships it",
                )

    def test_no_module_reaches_outside_the_package_for_data(self):
        """The bug in one line: a path that climbs past the package root."""
        for module in PACKAGE_ROOT.rglob("*.py"):
            source = module.read_text(encoding="utf-8")
            with self.subTest(module=module.name):
                self.assertNotIn(
                    'parent.parent / "config"', source,
                    "resource paths must not point outside the installed package",
                )


class InstalledPackageTests(unittest.TestCase):
    """Build and install into a throwaway environment, then run it from elsewhere."""

    def test_a_fresh_install_can_load_its_own_resources(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            venv = Path(tmp) / "venv"
            subprocess.run([sys.executable, "-m", "venv", venv], check=True,
                           capture_output=True, timeout=180)
            pip = _venv_exe(venv, "pip")
            install = subprocess.run(
                [str(pip), "install", "--quiet", "--no-deps", str(PROJECT_ROOT)],
                capture_output=True, text=True, timeout=600,
            )
            self.assertEqual(install.returncode, 0, install.stderr[-800:])

            # Run from a directory that is not the checkout, so a repo-relative
            # path cannot accidentally resolve.
            probe = subprocess.run(
                [str(_venv_exe(venv, "python")), "-c",
                 "import json;from aicut.config import CalibrationProfile;"
                 "from aicut.render.subtitles import SubtitleStyleProfile;"
                 "p=CalibrationProfile.load();"
                 "s=SubtitleStyleProfile.load('default');"
                 "print(json.dumps({'profile':p.name,'provisional':len(p.provisional),"
                 "'font':s.resolved('default')['fontname']}))"],
                cwd=tmp, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr[-800:])
            result = json.loads(probe.stdout.strip().splitlines()[-1])
            self.assertEqual(result["profile"], "default")
            self.assertGreater(result["provisional"], 0)
            self.assertTrue(result["font"])

    def test_the_console_script_is_installed_and_works(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            venv = Path(tmp) / "venv"
            subprocess.run([sys.executable, "-m", "venv", venv], check=True,
                           capture_output=True, timeout=180)
            subprocess.run([str(_venv_exe(venv, "pip")), "install", "--quiet", "--no-deps",
                            str(PROJECT_ROOT)], check=True, capture_output=True, timeout=600)

            aicut = _venv_exe(venv, "aicut")
            self.assertTrue(aicut.exists(), "the aicut console script was not installed")
            result = subprocess.run([str(aicut), "profile"], cwd=tmp, capture_output=True,
                                    text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            self.assertIn("provisional_parameters", result.stdout)


if __name__ == "__main__":
    unittest.main()
