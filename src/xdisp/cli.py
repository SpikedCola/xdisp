"""xdisp command line: the watch TUI by default, plus scriptable subcommands."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from xdisp import displays
from xdisp.displays import XdispError, parse_display

EPILOG = """\
examples:
  %(prog)s                          live watch screen (n new · k kill · K kill all · v vnc · c cua)
  %(prog)s list [--json]            one-shot listing
  %(prog)s new 42 --size 1920x1080  Xvfb :42 + x11vnc on 5942
  %(prog)s new --cua --openbox      next free display with cua-driver and openbox
  %(prog)s kill :42 [--all]         stop the X server [and every process on it]
  %(prog)s vnc :42 [--stop]         start/stop x11vnc on 59xx
  %(prog)s cua :42 [--stop]         start/stop cua-driver on the display

VNC listens on localhost only unless --public (or XDISP_VNC_PUBLIC=1);
~/.vnc/passwd is used as the password when it exists.
"""


def _public_default() -> bool:
    return os.environ.get("XDISP_VNC_PUBLIC", "").lower() in ("1", "true", "yes")


def _print_list(as_json: bool) -> None:
    from xdisp.tui.widgets import client_summary, format_duration, format_mem

    snap = displays.scan()
    if as_json:
        print(json.dumps([d.to_json() for d in snap], indent=2))
        return
    if not snap:
        print("no X displays")
        return
    now = time.time()
    for d in snap:
        if d.stale:
            print(f"{d.name:<5} stale (lock pid {d.stale_pid}, no server)")
            continue
        vnc = "-"
        if d.vnc:
            vnc = f"{d.vnc.port}{'' if d.vnc.listening else '!'} ({d.vnc.viewers}v"
            vnc += f", {'local' if d.vnc.localhost else 'public'})"
        windows = len(d.windows) if d.windows is not None else "?"
        up = format_duration(now - d.started) if d.started else "?"
        print(
            f"{d.name:<5} {d.server:<5} {d.size or '?':<13} up {up:<7} "
            f"vnc {vnc:<20} {windows} win  {format_mem(d.rss_kb):>7}"
            f"{'  cua' if d.cua else ''}  {client_summary(d)}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xdisp",
        description="Watch, start and kill Xvfb displays (x11vnc on 59xx, openbox, and cua-driver).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--public", action="store_true", default=_public_default(),
                        help="start VNC on all interfaces instead of localhost")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("watch", help="live watch screen (default)")
    p = sub.add_parser("list", help="list displays")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("new", help="start Xvfb (+ x11vnc) on a display")
    p.add_argument("display", nargs="?", help="e.g. 42 or :42 (default: highest free ≤ 99)")
    p.add_argument("--size", default=displays.DEFAULT_SIZE)
    p.add_argument("--no-vnc", action="store_true")
    p.add_argument("--cua", action="store_true", help="also start cua-driver on it")
    p.add_argument("--openbox", action="store_true", help="also run openbox on it")
    p = sub.add_parser("kill", help="stop a display's X server")
    p.add_argument("display")
    p.add_argument("--all", action="store_true", help="also stop every process on it")
    p = sub.add_parser("vnc", help="start/stop x11vnc on 59xx")
    p.add_argument("display")
    p.add_argument("--stop", action="store_true")
    p = sub.add_parser("cua", help="start/stop cua-driver on a display")
    p.add_argument("display")
    p.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.cmd in (None, "watch"):
            from xdisp.tui.app import run

            return run(public_vnc=args.public)
        if args.cmd == "list":
            _print_list(args.json)
            return 0
        if args.cmd == "new":
            number = parse_display(args.display) if args.display else displays.next_free()
            out = displays.start_display(number, args.size, vnc=not args.no_vnc,
                                         public=args.public, cua=args.cua,
                                         openbox=args.openbox)
        elif args.cmd == "kill":
            out = displays.kill_display(parse_display(args.display), everything=args.all)
        elif args.cmd == "vnc":
            n = parse_display(args.display)
            out = displays.stop_vnc(n) if args.stop else displays.start_vnc(n, public=args.public)
        elif args.cmd == "cua":
            n = parse_display(args.display)
            out = displays.stop_cua(n) if args.stop else displays.start_cua(n)
    except XdispError as exc:
        print(f"xdisp: {exc}", file=sys.stderr)
        return 1
    print("\n".join(out) if isinstance(out, list) else out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
