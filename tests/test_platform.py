"""Assumptions that only break off Linux.

Everything in this repository was written and run on Linux. 22.1 asks for a
desktop program and the people who would use it are mostly on Windows, where
the differences are concrete: no `resource` module, `Scripts\\` instead of
`bin/`, backslash paths that land inside ffmpeg filter strings, and a console
codepage that cannot print a Korean filename.

What can be checked from any platform is checked here; the rest is what the
Windows and macOS CI jobs are for. Nothing in this file claims the program has
been *run* on Windows - it has not been, by me.
"""

from __future__ import annotations

import io
import pathlib
import sys
import unittest

from aicut.cli import _force_utf8_console
from aicut.render.ffmpeg import RenderSettings, _escape_filter_path, build_final_command

KOREAN_NAME = "방송_2026-08-19 [하이라이트].mkv"


class FilterPathTests(unittest.TestCase):
    """A path goes into an ffmpeg filter argument, where ':' separates options."""

    def test_a_windows_path_is_escaped_for_the_filter(self):
        escaped = _escape_filter_path(r"C:\work space\subs.ass")
        self.assertEqual(escaped, "C\\:/work space/subs.ass")
        self.assertNotIn("\\w", escaped, "backslashes must not survive into the filter")

    def test_a_posix_path_is_left_alone(self):
        self.assertEqual(_escape_filter_path("/tmp/a b/subs.ass"), "/tmp/a b/subs.ass")

    def test_korean_characters_pass_through_untouched(self):
        self.assertIn("방송", _escape_filter_path(f"/media/{KOREAN_NAME}"))

    def test_a_quote_in_the_path_cannot_close_the_filter_argument(self):
        escaped = _escape_filter_path("/media/it's here/subs.ass")
        self.assertNotIn("'", escaped.replace("\\'", ""))

    def test_the_font_directory_is_escaped_like_the_subtitle_path(self):
        """It was not, once: a Windows fonts_dir would have broken the filter."""
        cmd = build_final_command(
            "/j.mp4", "/o.mp4", RenderSettings(),
            ass_path=r"C:\ws\s.ass", fonts_dir=r"C:\Windows\Fonts",
        )
        filters = cmd[cmd.index("-vf") + 1]
        self.assertIn("subtitles='C\\:/ws/s.ass'", filters)
        self.assertIn("fontsdir='C\\:/Windows/Fonts'", filters)


class ConsoleEncodingTests(unittest.TestCase):
    """A Korean filename printed on a Western codepage raises, and the CLI
    prints paths and titles in two dozen places."""

    def _print_under(self, codepage: str, *, fix: bool) -> str:
        saved = sys.stdout
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding=codepage)
        try:
            if fix:
                _force_utf8_console()
            try:
                print(KOREAN_NAME)
                sys.stdout.flush()
                return "ok"
            except UnicodeEncodeError:
                return "UnicodeEncodeError"
        finally:
            sys.stdout = saved

    def test_a_western_codepage_would_have_crashed(self):
        self.assertEqual(self._print_under("cp1252", fix=False), "UnicodeEncodeError")
        self.assertEqual(self._print_under("cp437", fix=False), "UnicodeEncodeError")

    def test_reconfiguring_makes_it_printable(self):
        for codepage in ("cp1252", "cp437", "cp949"):
            with self.subTest(codepage=codepage):
                self.assertEqual(self._print_under(codepage, fix=True), "ok")

    def test_a_stream_that_cannot_be_reconfigured_is_left_alone(self):
        saved = sys.stdout
        sys.stdout = io.StringIO()          # no reconfigure attribute
        try:
            _force_utf8_console()           # must not raise
            print(KOREAN_NAME)
        finally:
            sys.stdout = saved


class PortabilityTests(unittest.TestCase):
    def test_no_module_imports_a_posix_only_stdlib_module_at_top_level(self):
        import ast

        posix_only = {"resource", "fcntl", "pwd", "grp", "termios", "posix", "syslog"}
        package = pathlib.Path(__file__).resolve().parent.parent / "aicut"
        for module in package.rglob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name in posix_only:
                        self.fail(f"{module.name}:{node.lineno} imports {name}, which Windows lacks")

    def test_no_module_hardcodes_a_posix_only_directory(self):
        package = pathlib.Path(__file__).resolve().parent.parent / "aicut"
        for module in package.rglob("*.py"):
            source = module.read_text(encoding="utf-8")
            for bad in ('"/tmp/', "'/tmp/", '"/usr/', '"/var/', '"/home/'):
                with self.subTest(module=module.name, snippet=bad):
                    self.assertNotIn(bad, source)

    def test_concat_entries_use_forward_slashes(self):
        """ffmpeg's concat demuxer reads the list itself; backslashes in an
        entry are escape characters there, not separators."""
        import inspect

        from aicut.render.ffmpeg import Renderer

        self.assertIn("as_posix()", inspect.getsource(Renderer.render))

    def test_the_workspace_is_built_from_path_objects_not_string_joins(self):
        import inspect

        from aicut.pipeline import context

        source = inspect.getsource(context)
        self.assertNotIn('+ "/"', source)
        self.assertNotIn("'/' +", source)


if __name__ == "__main__":
    unittest.main()
