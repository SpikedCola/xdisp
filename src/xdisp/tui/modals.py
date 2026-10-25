"""Modal screens: confirmation, new-display form, and the detail view."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from xdisp.displays import DEFAULT_SIZE, XdispError, parse_display, vnc_port


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation. ←/→ move between buttons, y/n answer directly."""

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n,escape", "cancel", "No", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, message: str, *, title: str = "Confirm", yes_label: str = "Yes") -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._yes_label = yes_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self._title, classes="modal-title")
            yield Static(self._message, classes="modal-body", markup=False)
            with Horizontal(classes="modal-buttons"):
                yield Button(self._yes_label, id="yes")
                yield Button("Cancel", id="no")
            yield Static(
                f"← → · enter  ·  y {self._yes_label.lower()}  ·  n / esc cancel",
                classes="modal-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass
class NewDisplayForm:
    number: int
    size: str
    vnc: bool
    public: bool
    cua: bool
    openbox: bool


class NewDisplayModal(ModalScreen["NewDisplayForm | None"]):
    """Display number (any; 0-99 get a 59xx VNC port), size, and add-ons."""

    # ↑/↓ walk the fields like tab / shift+tab (single-line Inputs and
    # Checkboxes don't use them, so nothing is shadowed).
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("down", "app.focus_next", show=False),
        Binding("up", "app.focus_previous", show=False),
    ]

    def __init__(self, suggested: int) -> None:
        super().__init__()
        self._suggested = suggested

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label("Start a new display", classes="modal-title")
            yield Input(value=f":{self._suggested}", placeholder="display, e.g. :42", id="number")
            yield Input(value=DEFAULT_SIZE, placeholder="WxH or WxHxDEPTH", id="size")
            yield Checkbox("x11vnc", True, id="vnc")
            yield Checkbox("on all interfaces (untick for localhost only)", True, id="public")
            yield Checkbox("openbox window manager", True, id="openbox")
            yield Checkbox("cua-driver on this display", True, id="cua")
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Start", id="start")
                yield Button("Cancel", id="cancel")
            yield Static("enter start  ·  ↑ ↓ / tab move  ·  space toggle  ·  esc cancel",
                         classes="modal-hint")

    def on_mount(self) -> None:
        self._update_vnc_label()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "number":
            self._update_vnc_label()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        # "all interfaces" only means something with VNC on; it keeps its
        # value while disabled so re-ticking VNC restores it.
        if event.checkbox.id == "vnc":
            self.query_one("#public", Checkbox).disabled = not event.value

    def _update_vnc_label(self) -> None:
        """Name the real port the typed display maps to (:97 → 5997)."""
        checkbox = self.query_one("#vnc", Checkbox)
        try:
            port = vnc_port(parse_display(self.query_one("#number", Input).value))
        except XdispError:
            checkbox.label = "x11vnc (enter a display number)"
            return
        checkbox.label = f"x11vnc on port {port}" if port else "x11vnc (no port above :99)"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        error = self.query_one("#form-error", Static)
        try:
            number = parse_display(self.query_one("#number", Input).value)
        except XdispError as exc:
            error.update(str(exc))
            return
        vnc = self.query_one("#vnc", Checkbox).value
        if vnc and vnc_port(number) is None:
            error.update("Only :0-:99 get a 59xx VNC port; untick VNC or pick another.")
            return
        self.dismiss(
            NewDisplayForm(
                number=number,
                size=self.query_one("#size", Input).value.strip() or DEFAULT_SIZE,
                vnc=vnc,
                public=vnc and self.query_one("#public", Checkbox).value,
                cua=self.query_one("#cua", Checkbox).value,
                openbox=self.query_one("#openbox", Checkbox).value,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class OutputModal(ModalScreen[None]):
    """Scrollable text panel (display details)."""

    BINDINGS = [Binding("escape,q,enter", "dismiss_modal", "Close", show=False)]

    def __init__(self, title: str, body: Text) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide"):
            yield Label(self._title, classes="modal-title")
            with VerticalScroll(classes="modal-output"):
                yield Static(self._body)
            with Horizontal(classes="modal-buttons"):
                yield Button("Close", id="close")
            yield Static("esc close", classes="modal-hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
