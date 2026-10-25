"""Display cards: one header line plus labelled detail rows, cswap-style."""

from __future__ import annotations

import time
from collections import Counter

from rich.text import Text
from textual.widgets import ListItem, Static

from xdisp.displays import Display
from xdisp.tui.theme import Palette

LABEL_W = 9


def format_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def format_started(ts: float, now: float) -> str:
    fmt = "%H:%M" if now - ts < 86400 else "%b %d %H:%M"
    return time.strftime(fmt, time.localtime(ts))


def format_mem(kb: int) -> str:
    return f"{kb / 1024 / 1024:.1f} GB" if kb >= 1024 * 1024 else f"{kb // 1024} MB"


def client_summary(d: Display) -> str:
    counts = Counter(c.name for c in d.clients)
    return " · ".join(f"{name} ×{n}" if n > 1 else name for name, n in counts.most_common())


def _row(text: Text, label: str, palette: Palette) -> None:
    text.append("\n    ")
    text.append(f"{label:<{LABEL_W}}", style=palette.muted)


def display_card_text(d: Display, palette: Palette, now: float | None = None) -> Text:
    now = now if now is not None else time.time()
    text = Text()
    text.append(f"{d.name:<4} ", style=f"bold {palette.accent}")

    if d.stale:
        gone = f"lock pid {d.stale_pid} is gone" if d.stale_pid else "no server process"
        text.append("stale", style=f"bold {palette.warn}")
        text.append(f"   {gone} · kill removes the leftover lock/socket", style=palette.muted)
        return text

    text.append(f"{d.server}", style=f"bold {palette.foreground}")
    if d.cua:
        text.append("   ● cua", style=f"bold {palette.accent}")
        if d.cua_socket:
            stem = d.cua_socket.rsplit("/", 1)[-1].removesuffix(".sock")
            text.append(f" {stem}", style=palette.accent)
    if d.started:
        text.append(
            f"   up {format_duration(now - d.started)} · since {format_started(d.started, now)}",
            style=palette.muted,
        )

    _row(text, "screen", palette)
    text.append(d.size or "size unknown", style=palette.foreground)
    if d.windows is not None:
        n = len(d.windows)
        text.append(f" · {n} window{'s' if n != 1 else ''}", style=palette.foreground)
    text.append(f" · pid {d.pid} · {format_mem(d.rss_kb)}", style=palette.muted)

    _row(text, "vnc", palette)
    vnc = d.vnc
    if vnc is None:
        if d.vnc_port is None:
            text.append("off · no 59xx port above :99", style=palette.muted)
        else:
            text.append(f"off · v starts it on {d.vnc_port}", style=palette.muted)
    else:
        if vnc.listening:
            text.append(f"{vnc.port}", style=f"bold {palette.ok}")
        else:
            text.append(f"{vnc.port} not listening", style=palette.warn)
        if vnc.kind != "x11vnc":
            text.append(f" · {vnc.kind}", style=palette.muted)
        viewers = f"{vnc.viewers} viewer{'s' if vnc.viewers != 1 else ''}"
        text.append(f" · {viewers}", style=palette.foreground if vnc.viewers else palette.muted)
        text.append(
            " · localhost only" if vnc.localhost else " · all interfaces",
            style=palette.muted,
        )
        if not vnc.password:
            text.append(" · no password", style=palette.warn if not vnc.localhost else palette.muted)

    _row(text, "clients", palette)
    if d.clients:
        text.append(client_summary(d), style=palette.foreground)
    else:
        text.append("none", style=palette.muted)
    return text


class DisplayCard(Static):
    def __init__(self, d: Display) -> None:
        super().__init__()
        self._display = d

    def set_display(self, d: Display) -> None:
        self._display = d
        self.refresh(layout=True)

    def render(self) -> Text:
        return display_card_text(self._display, Palette.from_theme(self.app.current_theme))


class DisplayItem(ListItem):
    """ListView row wrapping a :class:`DisplayCard`; remembers its number."""

    def __init__(self, d: Display) -> None:
        super().__init__(DisplayCard(d))
        self.number = d.number
        self.display_info = d

    def set_display(self, d: Display) -> None:
        self.display_info = d
        self.query_one(DisplayCard).set_display(d)
