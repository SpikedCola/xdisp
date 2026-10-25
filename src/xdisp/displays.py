"""Discover and control X displays: Xvfb servers, their x11vnc, and cua-driver.

Everything is read straight from the system on each scan — /proc, the
/tmp/.X*-lock files and /tmp/.X11-unix sockets — so displays started by
anything (xvfb-run, scripts, cua) show up, not just ones xdisp started.

VNC port convention: display :N is served on 59NN (``:7`` → 5907, ``:99`` →
5999), so only displays 0-99 can have a VNC port.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

X_SERVERS = {"Xvfb", "Xorg", "Xvnc", "Xtigervnc", "Xephyr", "Xwayland", "Xdummy"}
VNC_SERVERS = {"Xvnc", "Xtigervnc"}
DEFAULT_SIZE = "1600x1000x24"
LOCK_DIR = Path("/tmp")
SOCKET_DIR = Path("/tmp/.X11-unix")
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "xdisp"
VNC_PASSWD = Path.home() / ".vnc" / "passwd"
CUA_SOCKET_DIR = Path.home() / ".cache/cua-driver"
STOP_WAIT_S = 3.0

_DISPLAY_ARG = re.compile(r"^:(\d+)$")
_DISPLAY_ENV = re.compile(r"^(?:localhost|127\.0\.0\.1|unix)?:(\d+)(?:\.\d+)?$")
_PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
_CLK_TCK = os.sysconf("SC_CLK_TCK")

# Popen handles for processes this xdisp started, polled on every scan so
# they don't linger as zombies while the TUI keeps running.
_children: list[subprocess.Popen] = []


class XdispError(Exception):
    """A user-facing failure (bad display number, port taken, ...)."""


# -- data -----------------------------------------------------------------------


@dataclass
class Client:
    pid: int
    name: str
    rss_kb: int
    cmdline: str


@dataclass
class Vnc:
    pid: int | None
    kind: str  # "x11vnc" | "Xvnc" (the X server itself serves VNC)
    port: int
    listening: bool
    localhost: bool
    password: bool
    viewers: int


@dataclass
class Display:
    number: int
    server: str | None  # "Xvfb", ... ; None when only a stale lock/socket remains
    pid: int | None
    started: float | None
    cmdline: str = ""
    size: str | None = None
    windows: list[str] | None = None  # visible window titles; None = unknown
    rss_kb: int = 0
    clients: list[Client] = field(default_factory=list)
    vnc: Vnc | None = None
    cua: bool = False
    cua_socket: str | None = None  # --socket of the cua-driver serving it
    stale_pid: int | None = None  # lock-file pid when the server is gone
    log: str | None = None

    @property
    def name(self) -> str:
        return f":{self.number}"

    @property
    def stale(self) -> bool:
        return self.server is None

    @property
    def vnc_port(self) -> int | None:
        return vnc_port(self.number)

    def to_json(self) -> dict:
        data = asdict(self)
        data["display"] = self.name
        data["stale"] = self.stale
        data["vnc_port"] = self.vnc_port
        return data


def vnc_port(number: int) -> int | None:
    return 5900 + number if 0 <= number <= 99 else None


def parse_display(text: str) -> int:
    """``"99"`` / ``":99"`` / ``":99.0"`` → 99."""
    m = re.fullmatch(r":?(\d+)(?:\.\d+)?", text.strip())
    if not m:
        raise XdispError(f"not a display number: {text!r} (use e.g. 99 or :99)")
    return int(m.group(1))


# -- /proc scanning ---------------------------------------------------------------


@dataclass
class _Proc:
    pid: int
    uid: int
    name: str
    argv: list[str]
    started: float
    rss_kb: int
    display: int | None  # from DISPLAY in its environment, if readable


def _boot_time() -> float:
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return 0.0


def _read_proc(pid: int, btime: float) -> _Proc | None:
    base = Path("/proc") / str(pid)
    try:
        stat = (base / "stat").read_text()
        raw_argv = (base / "cmdline").read_bytes()
        uid = (base / "status").stat().st_uid
        statm = (base / "statm").read_text().split()
    except OSError:
        return None
    # comm may contain spaces/parens; fields resume after the last ')'
    rest = stat[stat.rfind(")") + 2 :].split()
    if rest[0] == "Z":
        return None
    argv = [a.decode(errors="replace") for a in raw_argv.split(b"\0") if a]
    name = stat[stat.find("(") + 1 : stat.rfind(")")]
    if argv:
        name = os.path.basename(argv[0]) or name
    display = None
    try:
        for entry in (base / "environ").read_bytes().split(b"\0"):
            if entry.startswith(b"DISPLAY="):
                m = _DISPLAY_ENV.match(entry[8:].decode(errors="replace"))
                display = int(m.group(1)) if m else None
                break
    except OSError:
        pass
    return _Proc(
        pid=pid,
        uid=uid,
        name=name,
        argv=argv,
        started=btime + int(rest[19]) / _CLK_TCK,
        rss_kb=int(statm[1]) * _PAGE_KB,
        display=display,
    )


def _scan_procs() -> list[_Proc]:
    for child in list(_children):
        if child.poll() is not None:
            _children.remove(child)
    btime = _boot_time()
    procs = []
    for entry in os.listdir("/proc"):
        if entry.isdigit() and (proc := _read_proc(int(entry), btime)):
            procs.append(proc)
    return procs


def _display_arg(argv: list[str]) -> int | None:
    for arg in argv[1:]:
        if m := _DISPLAY_ARG.match(arg):
            return int(m.group(1))
    return None


def _arg_value(argv: list[str], flag: str) -> str | None:
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _tcp_ports() -> tuple[set[int], dict[int, int]]:
    """(listening ports, established-connection count per local port)."""
    listening: set[int] = set()
    established: dict[int, int] = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(path).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            port = int(cols[1].rsplit(":", 1)[1], 16)
            if cols[3] == "0A":
                listening.add(port)
            elif cols[3] == "01":
                established[port] = established.get(port, 0) + 1
    return listening, established


def _lock_pid(number: int) -> int | None:
    try:
        return int((LOCK_DIR / f".X{number}-lock").read_text().strip())
    except (OSError, ValueError):
        return None


def _known_numbers() -> set[int]:
    numbers = set()
    for path in LOCK_DIR.glob(".X*-lock"):
        if m := re.fullmatch(r"\.X(\d+)-lock", path.name):
            numbers.add(int(m.group(1)))
    if SOCKET_DIR.is_dir():
        for path in SOCKET_DIR.iterdir():
            if m := re.fullmatch(r"X(\d+)", path.name):
                numbers.add(int(m.group(1)))
    return numbers


# -- X queries (cached per server pid: the screen of a running Xvfb is fixed) ---

_size_cache: dict[tuple[int, int], str | None] = {}


def _query_size(number: int, pid: int, argv: list[str]) -> str | None:
    key = (number, pid)
    if key in _size_cache:
        return _size_cache[key]
    size = None
    if shutil.which("xdpyinfo"):
        try:
            out = subprocess.run(
                ["xdpyinfo", "-display", f":{number}"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            dims = re.search(r"dimensions:\s+(\d+x\d+)", out)
            depth = re.search(r"depth of root window:\s+(\d+)", out)
            if dims:
                size = dims.group(1) + (f"x{depth.group(1)}" if depth else "")
        except (OSError, subprocess.TimeoutExpired):
            pass
    if size is None:
        screen = _arg_value(argv, "-screen")
        if screen is not None:
            idx = argv.index("-screen")
            size = argv[idx + 2] if idx + 2 < len(argv) else None
    _size_cache[key] = size
    return size


def _xrun(number: int, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=3,
        env={**os.environ, "DISPLAY": f":{number}"},
    )


def _window_ids(text: str) -> list[str]:
    return re.findall(r"0x[0-9a-fA-F]+", text)


def _window_title(number: int, wid: str) -> str:
    out = _xrun(number, "xprop", "-id", wid, "_NET_WM_NAME", "WM_NAME").stdout
    m = re.search(r'= "(.*)"', out)
    return m.group(1) if m else ""


def _query_windows(number: int) -> list[str] | None:
    """Titles of the display's application windows.

    Not every mapped X window: menus, tooltips, overlays and their
    sub-windows would inflate the count (an open openbox menu alone is ~20).
    With a window manager running, its _NET_CLIENT_LIST is the answer (what a
    taskbar shows); without one, count mapped top-level windows that aren't
    override-redirect (popups/overlays).
    """
    if not (shutil.which("xprop") and shutil.which("xwininfo")):
        return None
    try:
        root = _xrun(number, "xprop", "-root",
                     "_NET_SUPPORTING_WM_CHECK", "_NET_CLIENT_LIST").stdout
        check = re.search(r"_NET_SUPPORTING_WM_CHECK\(WINDOW\): window id # (0x[0-9a-fA-F]+)", root)
        # the property outlives a dead WM; only trust it if its window exists
        if check and _xrun(number, "xwininfo", "-id", check.group(1)).returncode == 0:
            clients = re.search(r"_NET_CLIENT_LIST\(WINDOW\): window id #(.*)", root)
            ids = _window_ids(clients.group(1)) if clients else []
        else:
            children = _xrun(number, "xwininfo", "-root", "-children").stdout
            # "1 child:" / "N children:" then one line per child; "0 children." has none
            listing = re.split(r"\d+ child(?:ren)?:", children, maxsplit=1)
            ids = []
            for wid in _window_ids(listing[1] if len(listing) > 1 else ""):
                info = _xrun(number, "xwininfo", "-id", wid).stdout
                if "IsViewable" in info and "Override Redirect State: no" in info:
                    ids.append(wid)
        return [_window_title(number, wid) for wid in ids]
    except (OSError, subprocess.TimeoutExpired):
        return None


# -- the scan ---------------------------------------------------------------------


def scan(*, windows: bool = True) -> list[Display]:
    procs = _scan_procs()
    listening, established = _tcp_ports()
    servers = {}
    for proc in procs:
        if proc.name in X_SERVERS and (n := _display_arg(proc.argv)) is not None:
            servers[n] = proc
    vnc_procs: dict[int, _Proc] = {}
    for proc in procs:
        if proc.name == "x11vnc":
            target = _arg_value(proc.argv, "-display")
            n = parse_display(target) if target and _DISPLAY_ENV.match(target) else proc.display
            if n is not None:
                vnc_procs[n] = proc

    displays = []
    for n in sorted(_known_numbers() | servers.keys(), reverse=True):
        server = servers.get(n)
        if server is None:
            lock_pid = _lock_pid(n)
            displays.append(
                Display(number=n, server=None, pid=None, started=None, stale_pid=lock_pid)
            )
            continue
        d = Display(
            number=n,
            server=server.name,
            pid=server.pid,
            started=server.started,
            cmdline=" ".join(server.argv),
            rss_kb=server.rss_kb,
        )
        log = STATE_DIR / f"X{n}.log"
        d.log = str(log) if log.exists() else None
        d.size = _query_size(n, server.pid, server.argv)
        if windows:
            d.windows = _query_windows(n)

        vproc = vnc_procs.get(n)
        if server.name in VNC_SERVERS:
            port = int(_arg_value(server.argv, "-rfbport") or 5900 + n)
            d.vnc = Vnc(
                pid=None, kind=server.name, port=port,
                listening=port in listening,
                localhost="-localhost" in server.argv,
                password=(_arg_value(server.argv, "-SecurityTypes") or "").lower() != "none",
                viewers=established.get(port, 0),
            )
        elif vproc is not None:
            port = int(_arg_value(vproc.argv, "-rfbport") or 5900)
            d.vnc = Vnc(
                pid=vproc.pid, kind="x11vnc", port=port,
                listening=port in listening,
                localhost="-localhost" in vproc.argv
                or _arg_value(vproc.argv, "-listen") in ("localhost", "127.0.0.1"),
                password="-nopw" not in vproc.argv,
                viewers=established.get(port, 0),
            )

        for proc in procs:
            if proc.display != n or proc.pid == server.pid:
                continue
            if vproc is not None and proc.pid == vproc.pid:
                continue
            d.clients.append(
                Client(proc.pid, proc.name, proc.rss_kb, " ".join(proc.argv))
            )
            if proc.name == "cua-driver" and "serve" in proc.argv:
                d.cua = True
                d.cua_socket = _arg_value(proc.argv, "--socket") or str(
                    CUA_SOCKET_DIR / "cua-driver.sock"
                )
        d.rss_kb += sum(c.rss_kb for c in d.clients)
        displays.append(d)
    return displays


def find(number: int) -> Display | None:
    return next((d for d in scan(windows=False) if d.number == number), None)


def next_free(start: int = 99) -> int:
    """Highest free display number at or below ``start`` (so it has a 59xx port)."""
    taken = _known_numbers()
    for n in range(start, -1, -1):
        if n not in taken:
            return n
    raise XdispError("no free display number in 0-99")


# -- process control --------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return state[state.rfind(")") + 2] != "Z"


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        raise XdispError(f"not allowed to signal pid {pid} (owned by another user)")


def _stop(pids: list[int]) -> list[int]:
    """SIGTERM, wait up to STOP_WAIT_S, SIGKILL leftovers. Returns force-killed pids."""
    for pid in pids:
        _signal(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return []
        time.sleep(0.1)
    forced = [p for p in pids if _alive(p)]
    for pid in forced:
        _signal(pid, signal.SIGKILL)
    return forced


def _spawn(argv: list[str], log: Path, env: dict | None = None) -> subprocess.Popen:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as fh:
        fh.write(f"\n--- {time.strftime('%F %T')} {' '.join(argv)}\n".encode())
        fh.flush()
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    _children.append(child)
    return child


def _wait_for(check, timeout: float, child: subprocess.Popen, what: str, log: Path) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        if child.poll() is not None:
            tail = log.read_text(errors="replace").strip().splitlines()[-3:]
            raise XdispError(f"{what} exited ({child.returncode}): " + " / ".join(tail))
        time.sleep(0.1)
    raise XdispError(f"{what} did not come up within {timeout:.0f}s (see {log})")


def _cleanup_stale(number: int) -> None:
    lock = LOCK_DIR / f".X{number}-lock"
    pid = _lock_pid(number)
    if pid is not None and _alive(pid):
        return
    for path in (lock, SOCKET_DIR / f"X{number}"):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except PermissionError:
            raise XdispError(f"cannot remove {path} (owned by another user)")


def start_display(
    number: int,
    size: str = DEFAULT_SIZE,
    *,
    vnc: bool = True,
    public: bool = False,
    cua: bool = False,
    openbox: bool = False,
) -> list[str]:
    """Start Xvfb on :number (plus openbox, x11vnc on 59NN and/or cua-driver).
    Returns progress messages."""
    if not re.fullmatch(r"\d+x\d+(x\d+)?", size):
        raise XdispError(f"bad screen size {size!r} (use WxH or WxHxDEPTH)")
    if size.count("x") == 1:
        size += "x24"
    if vnc and vnc_port(number) is None:
        raise XdispError(f":{number} has no 59xx VNC port; use 0-99 or skip VNC")
    existing = find(number)
    if existing is not None and not existing.stale:
        raise XdispError(f":{number} is already running ({existing.server}, pid {existing.pid})")
    if not shutil.which("Xvfb"):
        raise XdispError("Xvfb is not installed")
    _cleanup_stale(number)
    log = STATE_DIR / f"X{number}.log"
    child = _spawn(["Xvfb", f":{number}", "-screen", "0", size, "-nolisten", "tcp"], log)
    socket = SOCKET_DIR / f"X{number}"
    _wait_for(socket.exists, 5, child, "Xvfb", log)
    messages = [f"started Xvfb :{number} {size} (pid {child.pid})"]
    if openbox:
        messages.append(start_openbox(number))
    if vnc:
        messages.append(start_vnc(number, public=public))
    if cua:
        messages.append(start_cua(number))
    return messages


def start_openbox(number: int) -> str:
    if not shutil.which("openbox"):
        raise XdispError("openbox is not installed")
    log = STATE_DIR / f"openbox{number}.log"
    child = _spawn(["openbox"], log, env={**os.environ, "DISPLAY": f":{number}"})
    # openbox has no ready signal to poll; a crash on startup shows up fast
    time.sleep(0.5)
    if child.poll() is not None:
        tail = log.read_text(errors="replace").strip().splitlines()[-3:]
        raise XdispError(f"openbox exited ({child.returncode}): " + " / ".join(tail))
    return f"openbox running on :{number} (pid {child.pid})"


def start_vnc(number: int, *, public: bool = False) -> str:
    port = vnc_port(number)
    if port is None:
        raise XdispError(f":{number} has no 59xx VNC port (only 0-99)")
    d = find(number)
    if d is None or d.stale:
        raise XdispError(f":{number} is not running")
    if d.vnc is not None:
        raise XdispError(f":{number} already has VNC on {d.vnc.port}")
    if port in _tcp_ports()[0]:
        raise XdispError(f"port {port} is already in use by something else")
    if not shutil.which("x11vnc"):
        raise XdispError("x11vnc is not installed")
    argv = ["x11vnc", "-display", f":{number}", "-rfbport", str(port),
            "-forever", "-shared", "-quiet", "-noxdamage"]
    argv += ["-rfbauth", str(VNC_PASSWD)] if VNC_PASSWD.exists() else ["-nopw"]
    if not public:
        argv.append("-localhost")
    log = STATE_DIR / f"vnc{number}.log"
    child = _spawn(argv, log)
    _wait_for(lambda: port in _tcp_ports()[0], 8, child, "x11vnc", log)
    where = "all interfaces" if public else "localhost only"
    return f"x11vnc :{number} listening on {port} ({where})"


def stop_vnc(number: int) -> str:
    d = find(number)
    if d is None or d.vnc is None:
        raise XdispError(f":{number} has no VNC running")
    if d.vnc.pid is None:
        raise XdispError(f":{number} is an {d.vnc.kind} server; its VNC stops with it")
    _stop([d.vnc.pid])
    return f"stopped x11vnc on {d.vnc.port}"


def cua_socket(number: int) -> Path:
    return CUA_SOCKET_DIR / f"xdisp-{number}.sock"


def start_cua(number: int) -> str:
    """Start a cua-driver daemon bound to :number on its own socket, so one
    can run per display."""
    if not shutil.which("cua-driver"):
        raise XdispError("cua-driver is not installed")
    d = find(number)
    if d is None or d.stale:
        raise XdispError(f":{number} is not running")
    if d.cua:
        raise XdispError(f"cua-driver is already running on :{number} ({d.cua_socket})")
    sock = cua_socket(number)
    if sock.exists():
        sock.unlink()  # left over from a daemon that died; nothing is serving it
    log = STATE_DIR / f"cua{number}.log"
    child = _spawn(["cua-driver", "serve", "--socket", str(sock)], log,
                   env={**os.environ, "DISPLAY": f":{number}"})
    _wait_for(sock.exists, 8, child, "cua-driver", log)
    return f"cua-driver serving :{number} on {sock} (pid {child.pid})"


def stop_cua(number: int) -> str:
    d = find(number)
    pids = [
        c.pid for c in (d.clients if d else [])
        if c.name == "cua-driver" and " serve" in c.cmdline
    ]
    if not pids:
        raise XdispError(f"cua-driver is not running on :{number}")
    _stop(pids)
    cua_socket(number).unlink(missing_ok=True)
    return f"stopped cua-driver on :{number}"


def kill_display(number: int, *, everything: bool = False) -> list[str]:
    """Stop the X server (and its x11vnc). With ``everything``, stop every
    process using the display first. A stale display just gets its leftover
    lock and socket removed."""
    d = find(number)
    if d is None:
        raise XdispError(f":{number} does not exist")
    if d.stale:
        _cleanup_stale(number)
        return [f"removed stale lock/socket for :{number}"]
    messages = []
    if everything and d.clients:
        forced = _stop([c.pid for c in d.clients])
        messages.append(
            f"stopped {len(d.clients)} process{'es' if len(d.clients) != 1 else ''}"
            + (f" ({len(forced)} force-killed)" if forced else "")
        )
    if d.vnc is not None and d.vnc.pid is not None:
        _stop([d.vnc.pid])
        messages.append(f"stopped x11vnc on {d.vnc.port}")
    forced = _stop([d.pid])
    messages.append(f"stopped {d.server} :{number}" + (" (force-killed)" if forced else ""))
    _cleanup_stale(number)
    if everything:
        cua_socket(number).unlink(missing_ok=True)
    return messages
