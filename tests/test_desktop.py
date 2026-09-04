"""The double-click entry point (22.1).

A program is not a command with a shortcut on it. What separates them is where
the projects go when nobody passed a path, what happens when the port is
taken, and whether the thing that opens actually serves anything. The build
itself is checked by the `desktop` CI job, which runs the executable on all
three platforms from outside the checkout.
"""

from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aicut.desktop import build_parser, default_workspace, free_port, main


class WorkspaceLocationTests(unittest.TestCase):
    """A double-clicked executable starts in whatever directory the shell felt
    like, so the working directory is the one place projects must not go."""

    def test_windows_uses_local_appdata(self):
        with mock.patch("sys.platform", "win32"), \
             mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\j\AppData\Local"}, clear=False):
            self.assertEqual(default_workspace().parts[-1], "aicut")
            self.assertIn("AppData", str(default_workspace()))

    def test_macos_uses_application_support(self):
        with mock.patch("sys.platform", "darwin"):
            self.assertIn("Application Support", str(default_workspace()))

    def test_linux_honours_xdg(self):
        with mock.patch("sys.platform", "linux"), \
             mock.patch.dict("os.environ", {"XDG_DATA_HOME": "/custom/data"}, clear=False):
            self.assertEqual(default_workspace(), Path("/custom/data/aicut"))

    def test_it_is_never_the_working_directory(self):
        self.assertNotEqual(default_workspace().resolve(), Path.cwd().resolve())


class PortTests(unittest.TestCase):
    def test_the_preferred_port_is_used_when_free(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        self.assertEqual(free_port("127.0.0.1", free), free)

    def test_a_taken_port_does_not_stop_the_program(self):
        """Usually the port is taken by an older copy of itself, and a program
        people have to reboot to restart is not one they keep."""
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            busy = taken.getsockname()[1]
            chosen = free_port("127.0.0.1", busy)
        self.assertNotEqual(chosen, busy)
        self.assertGreater(chosen, 0)


class StartupTests(unittest.TestCase):
    def test_check_starts_the_server_and_gets_a_page_back(self):
        """--check is what CI runs: a window that opens and serves nothing is
        the failure it exists to catch."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            code = main(["--check", "--no-browser", "--workspace", tmp, "--port", "0"])
        self.assertEqual(code, 0)

    def test_it_makes_the_workspace_it_was_given(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "made" / "here"
            main(["--check", "--no-browser", "--workspace", str(target), "--port", "0"])
            self.assertTrue(target.is_dir())

    def test_no_browser_is_opened_when_asked_not_to(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
             mock.patch("webbrowser.open") as opened:
            main(["--check", "--no-browser", "--workspace", tmp, "--port", "0"])
        opened.assert_not_called()

    def test_the_producer_choice_is_limited_to_what_exists(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--producer", "gpt"])


class ReadinessTests(unittest.TestCase):
    def test_a_missing_ffmpeg_is_said_before_a_run_not_during(self):
        """The person who double-clicked this will not run `aicut doctor`, and
        a run that dies twenty minutes in is the worst place to learn."""
        import io
        from contextlib import redirect_stdout

        from aicut.desktop import _report_readiness

        buffer = io.StringIO()
        with mock.patch("aicut.media.ffmpeg_util.have_ffmpeg", return_value=False), \
             redirect_stdout(buffer):
            _report_readiness()
        out = buffer.getvalue()
        self.assertIn("ffmpeg was not found", out)
        self.assertIn("aicut fetch-ffmpeg", out, "it must say how to fix it, not only that it is broken")


if __name__ == "__main__":
    unittest.main()
