"""The xdisp Textual app: a live watch screen of every X display.

Modelled on cswap's watch screen: hands-off monitor by default, no cursor.
An action key (k / K / v / c / i) arms selection with the cursor on the
first display; Enter picks the target, Esc disarms. Scans and every action
run in thread workers so the UI never blocks on /proc, X queries, or waits for
processes to exit.
"""

from __future__ import annotations

import time
from functools import partial

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from xdisp import displays
from xdisp.displays import Display, XdispError
from xdisp.tui.modals import ConfirmModal, NewDisplayForm, NewDisplayModal, OutputModal
from xdisp.tui.theme import XDISP_DARK, XDISP_LIGHT, Palette
from xdisp.tui.widgets import DisplayItem, client_summary, format_duration, format_mem

# action id → prompt shown while selection is armed
PROMPTS = {
    "kill": "stop which X server?",
    "killall": "stop which display and everything on it?",
    "vnc": "start / stop vnc on which display?",
    "cua": "start / stop cua-driver on which display?",
    "info": "details for which display?",
}


class WatchScreen(Screen):
    BINDINGS = [
        Binding("n", "new", "New"),
        Binding("k", "arm('kill')", "Kill X"),
        Binding("K", "arm('killall')", "Kill all"),
        Binding("v", "arm('vnc')", "VNC"),
        Binding("c", "arm('cua')", "cua"),
        Binding("i", "arm('info')", "Info"),
        Binding("enter", "select_highlighted", "Confirm", priority=True),
        Binding("r", "app.refresh_now", "Refresh", show=False),
        Binding("escape,q", "back", "Quit"),
        Binding("down", "nav_down", show=False),
        Binding("up", "nav_up", show=False),
    ]

    app: "XdispApp"

    def __init__(self) -> None:
        super().__init__()
        self._numbers: list[int] = []
        self._pending: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield ListView(id="displays")
        yield Static("", id="empty")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "status", lambda _s: self._update_title())
        self._update_title()

    # -- live list --------------------------------------------------------------

    async def _on_snapshot(self, snap: list[Display] | None) -> None:
        if snap is None:
            return
        listview = self.query_one("#displays", ListView)
        numbers = [d.number for d in snap]
        if numbers != self._numbers:
            previous = listview.index
            await listview.clear()
            await listview.extend(DisplayItem(d) for d in snap)
            self._numbers = numbers
            if self._pending is not None and numbers:
                listview.index = min(previous or 0, len(numbers) - 1)
            else:
                listview.index = None
        else:
            for item, d in zip(listview.query(DisplayItem), snap):
                item.set_display(d)
        self.query_one("#empty", Static).update(
            "" if snap else "No X displays running.  n starts one."
        )
        self._update_title()

    def _update_title(self) -> None:
        title = self.query_one("#list-title", Static)
        if self._pending is not None:
            title.update(f"{PROMPTS[self._pending]} · enter confirm · esc cancel")
            return
        snap = self.app.snapshot or []
        count = f"watching {len(snap)} display{'s' if len(snap) != 1 else ''}"
        title.update(f"{count} · {self.app.status}" if self.app.status else count)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "select_highlighted" and self._pending is None:
            return False  # hidden and inert until selection is armed
        return True

    # -- selection ----------------------------------------------------------------

    def _set_pending(self, action: str | None) -> None:
        self._pending = action
        listview = self.query_one("#displays", ListView)
        if action is not None and self._numbers:
            if listview.index is None:
                listview.index = 0
            listview.focus()
        else:
            listview.index = None
            self.set_focus(None)
        self._update_title()
        self.refresh_bindings()

    def action_arm(self, action: str) -> None:
        if not self._numbers:
            self.app.notify("No displays — n starts one", severity="warning")
            return
        self._set_pending(None if self._pending == action else action)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self._pending is None:
            return  # a stray click while just watching
        item = event.item
        if isinstance(item, DisplayItem):
            action = self._pending
            self._set_pending(None)  # stay here, keep watching
            self.app.run_on(action, item.display_info)

    def action_select_highlighted(self) -> None:
        if self._pending is not None:
            self.query_one("#displays", ListView).action_select_cursor()

    def action_back(self) -> None:
        if self._pending is not None:
            self._set_pending(None)
        else:
            self.app.exit()

    def action_new(self) -> None:
        self._set_pending(None)
        self.app.open_new()

    def action_nav_down(self) -> None:
        listview = self.query_one("#displays", ListView)
        if self._pending is not None:
            listview.action_cursor_down()
        else:
            listview.scroll_down(animate=False)

    def action_nav_up(self) -> None:
        listview = self.query_one("#displays", ListView)
        if self._pending is not None:
            listview.action_cursor_up()
        else:
            listview.scroll_up(animate=False)


class XdispApp(App):
    TITLE = "xdisp"
    CSS_PATH = "xdisp.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+t", "toggle_theme", "Theme", show=False)]

    POLL_INTERVAL_S = 3.0

    snapshot: reactive[list[Display] | None] = reactive(None)
    status: reactive[str] = reactive("")

    def __init__(self, *, public_vnc: bool = False) -> None:
        super().__init__()
        self.public_vnc = public_vnc
        self._scanning = False
        self._busy = False
        self._scanned_at: float | None = None

    def on_mount(self) -> None:
        self.register_theme(XDISP_DARK)
        self.register_theme(XDISP_LIGHT)
        self.theme = "xdisp-dark"
        self.push_screen(WatchScreen())
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self.set_interval(1.0, self._update_status)
        self._tick()

    def action_toggle_theme(self) -> None:
        self.theme = "xdisp-light" if self.theme == "xdisp-dark" else "xdisp-dark"

    # -- scanning -------------------------------------------------------------------

    def _tick(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self.run_worker(self._scan_blocking, thread=True, group="scan", exit_on_error=False)

    def _scan_blocking(self) -> None:
        try:
            snap = displays.scan()
        finally:
            self.call_from_thread(setattr, self, "_scanning", False)
        self.call_from_thread(self._apply, snap)

    def _apply(self, snap: list[Display]) -> None:
        self._scanned_at = time.monotonic()
        self.snapshot = snap
        self._update_status()

    def _update_status(self) -> None:
        if self._busy:
            self.status = "working…"
        elif self._scanned_at is not None and time.monotonic() - self._scanned_at > 10:
            self.status = f"last scan {format_duration(time.monotonic() - self._scanned_at)} ago"
        else:
            self.status = ""

    def action_refresh_now(self) -> None:
        self._tick()

    # -- actions ----------------------------------------------------------------------

    def _start_action(self, label: str, fn) -> None:
        if self._busy:
            self.notify("Another action is still running", severity="warning")
            return
        self._busy = True
        self._update_status()
        self.run_worker(partial(self._action_blocking, label, fn), thread=True,
                        group="action", exit_on_error=False)

    def _action_blocking(self, label: str, fn) -> None:
        try:
            result = fn()
            messages = result if isinstance(result, list) else [result]
            self.call_from_thread(self._action_done, label, messages, None)
        except (XdispError, OSError) as exc:
            self.call_from_thread(self._action_done, label, [], str(exc))

    def _action_done(self, label: str, messages: list[str], error: str | None) -> None:
        self._busy = False
        if error:
            self.notify(error, title=label, severity="error", timeout=8)
        else:
            self.notify("\n".join(messages), title=label)
        self._tick()

    def run_on(self, action: str, d: Display) -> None:
        if action == "info":
            self.push_screen(OutputModal(f"{d.name} details", self._details(d)))
        elif action == "kill":
            if d.stale:
                msg = f"Remove the leftover lock and socket for {d.name}?"
            else:
                msg = f"Stop {d.server} {d.name} (pid {d.pid})"
                if d.vnc and d.vnc.pid:
                    msg += f" and its x11vnc on {d.vnc.port}"
                msg += "?"
                if d.clients:
                    msg += (f"\n\n{len(d.clients)} process(es) using it are left running "
                            f"(they'll likely exit on their own): {client_summary(d)}")
            self._confirm(msg, "Kill", partial(displays.kill_display, d.number))
        elif action == "killall":
            msg = f"Stop {d.server or 'stale'} {d.name}"
            if d.clients:
                msg += f" and every process on it?\n\n{client_summary(d)}"
                msg += "\n\n" + "\n".join(f"  {c.pid:>7}  {c.cmdline[:70]}" for c in d.clients[:12])
                if len(d.clients) > 12:
                    msg += f"\n  … and {len(d.clients) - 12} more"
            else:
                msg += "?"
            self._confirm(msg, "Kill all",
                          partial(displays.kill_display, d.number, everything=True))
        elif action == "vnc":
            if d.stale:
                self.notify(f"{d.name} is stale", severity="warning")
            elif d.vnc is None:
                self._start_action("VNC", partial(displays.start_vnc, d.number,
                                                  public=self.public_vnc))
            else:
                viewers = f" ({d.vnc.viewers} viewer(s) connected)" if d.vnc.viewers else ""
                self._confirm(f"Stop VNC on {d.vnc.port}{viewers}?", "Stop VNC",
                              partial(displays.stop_vnc, d.number))
        elif action == "cua":
            if d.stale:
                self.notify(f"{d.name} is stale", severity="warning")
            elif d.cua:
                self._confirm(f"Stop cua-driver on {d.name}?", "Stop cua",
                              partial(displays.stop_cua, d.number))
            else:
                self._start_action("cua", partial(displays.start_cua, d.number))

    def _confirm(self, message: str, yes_label: str, fn) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self._start_action(yes_label, fn)

        self.push_screen(ConfirmModal(message, title=yes_label, yes_label=yes_label), done)

    def open_new(self) -> None:
        try:
            suggested = displays.next_free()
        except XdispError:
            suggested = 99
        modal = NewDisplayModal(suggested)

        def done(form: NewDisplayForm | None) -> None:
            if form is not None:
                self._start_action(
                    f"New :{form.number}",
                    partial(displays.start_display, form.number, form.size,
                            vnc=form.vnc, public=form.public, cua=form.cua,
                            openbox=form.openbox),
                )

        self.push_screen(modal, done)

    def _details(self, d: Display) -> Text:
        p = Palette.from_theme(self.current_theme)
        text = Text()

        def row(label: str, value: str, style: str | None = None) -> None:
            text.append(f"{label:<10}", style=p.muted)
            text.append(f"{value}\n", style=style or p.foreground)

        if d.stale:
            row("state", "stale lock/socket, no server", p.warn)
            row("lock pid", str(d.stale_pid))
            return text
        row("server", f"{d.server} pid {d.pid}")
        row("command", d.cmdline)
        row("screen", d.size or "unknown")
        row("memory", f"{format_mem(d.rss_kb)} (server + clients)")
        if d.vnc:
            row("vnc", f"{d.vnc.kind} pid {d.vnc.pid or d.pid} · port {d.vnc.port} · "
                       f"{'localhost' if d.vnc.localhost else 'all interfaces'} · "
                       f"{'password' if d.vnc.password else 'no password'}")
        if d.cua_socket:
            row("cua", d.cua_socket)
        if d.log:
            row("log", d.log)
        text.append("\nwindows\n", style=f"bold {p.accent}")
        if d.windows is None:
            text.append("  (xprop / xwininfo not installed)\n", style=p.muted)
        for title in d.windows or []:
            text.append(f"  {title or '(untitled)'}\n")
        text.append("\nprocesses\n", style=f"bold {p.accent}")
        if not d.clients:
            text.append("  none\n", style=p.muted)
        for c in d.clients:
            text.append(f"  {c.pid:>7}  ", style=p.muted)
            text.append(f"{format_mem(c.rss_kb):>8}  ", style=p.muted)
            text.append(f"{c.cmdline[:160]}\n")
        return text


def run(*, public_vnc: bool = False) -> int:
    XdispApp(public_vnc=public_vnc).run()
    return 0
