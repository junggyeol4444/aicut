"""The double-click entry point (22.1).

Everything else in this package assumes a terminal. A streamer does not have
one open, so this is the shape the program actually ships as: run it, a local
server starts, the browser opens on the operator screens of 15.1, and closing
the window stops it.

It is deliberately thin. All it adds over `aicut ui` is the three things that
make the difference between a command and a program:

* a port that is free rather than one that happens to be 8765,
* a workspace under the user's own data directory rather than the current
  working directory, which for a double-clicked executable is wherever the
  shell felt like starting,
* and the browser opening by itself.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "aicut"


def default_workspace() -> Path:
    """Where a double-clicked program should keep its projects.

    Not the working directory: for an executable started from Explorer or the
    Dock that is wherever the shell happened to be, and a user who moves the
    program loses their projects.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_NAME


def free_port(host: str = "127.0.0.1", preferred: int = 8765) -> int:
    """The preferred port if it is free, otherwise one the OS picks.

    A program that refuses to start because something else is on 8765 - often
    an older copy of itself - is a program people restart their machine for.
    """
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise OSError("no port could be bound on localhost")


def _open_browser(url: str, delay_sec: float = 0.8) -> None:
    """Open the page once the server is actually accepting connections."""
    def go() -> None:
        try:
            webbrowser.open(url)
        except Exception as exc:                      # pragma: no cover - platform dependent
            log.warning("could not open a browser (%s); open %s yourself", exc, url)

    threading.Timer(delay_sec, go).start()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aicut-desktop",
        description="Start aicut and open its screens in a browser.",
    )
    parser.add_argument("--workspace", default=None, help="where projects are kept")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--producer", default="mock", choices=["mock", "anthropic"])
    parser.add_argument("--no-browser", action="store_true", help="start the server only")
    parser.add_argument("--check", action="store_true",
                        help="start, verify the server answers, and exit (used by CI)")
    return parser


def main(argv: list[str] | None = None) -> int:
    from aicut.cli import _force_utf8_console
    from aicut.ui import serve

    _force_utf8_console()
    args = build_parser().parse_args(argv)

    workspace = Path(args.workspace) if args.workspace else default_workspace()
    workspace.mkdir(parents=True, exist_ok=True)

    port = free_port(args.host, args.port)
    httpd, _ui = serve(
        workspace, host=args.host, port=port,
        profile_path=args.profile, producer_name=args.producer,
    )
    url = f"http://{args.host}:{port}/"

    print(f"aicut is running at {url}")
    print(f"  projects are kept in {workspace}")
    print("  localhost only, no authentication - do not expose this port")
    print("  close this window to stop it")
    _report_readiness()

    if args.check:
        # Prove the server answers before claiming the program starts. A window
        # that opens and serves nothing is the failure this catches.
        import urllib.request

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                ok = response.status == 200 and b"<" in response.read(64)
            print("check: the server answered" if ok else "check: the server did not answer")
            return 0 if ok else 1
        finally:
            httpd.shutdown()
            httpd.server_close()

    if not args.no_browser:
        _open_browser(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


def _report_readiness() -> None:
    """Say what will not work before the user finds out mid-run.

    `aicut doctor` exists for this, but a person who double-clicked a program
    is not going to run it, and a run that dies twenty minutes in for a missing
    dependency is the worst place to learn.
    """
    from aicut.config import CalibrationProfile
    from aicut.media.ffmpeg_util import have_ffmpeg

    if not have_ffmpeg():
        print("\n  ffmpeg was not found. Nothing can be cut or rendered without it;")
        print("  the screens will still open. Install it, or let aicut fetch it:")
        print("      aicut fetch-ffmpeg")
    try:
        profile = CalibrationProfile.load()
        if profile.provisional:
            print(f"\n  {len(profile.provisional)} groups of judgement thresholds are still")
            print("  unmeasured guesses (17.5). Results are usable but not yet this")
            print("  channel's: run the calibration once you have a broadcast to measure.")
    except Exception as exc:                          # pragma: no cover - defensive
        log.warning("could not read the calibration profile: %s", exc)


if __name__ == "__main__":                            # pragma: no cover
    raise SystemExit(main())
