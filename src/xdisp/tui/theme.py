"""xdisp's Textual themes — the cswap palette (charcoal, one terracotta
accent, desaturated status colors), so the two tools look like siblings."""

from __future__ import annotations

from dataclasses import dataclass

from textual.theme import Theme


@dataclass(frozen=True)
class Palette:
    """Theme colors for Rich renderables, which can't read ``$variables``."""

    accent: str
    foreground: str
    muted: str
    ok: str
    warn: str
    crit: str
    track: str

    @classmethod
    def from_theme(cls, theme: Theme) -> "Palette":
        return cls(
            accent=theme.primary,
            foreground=theme.foreground,
            muted=theme.secondary,
            ok=theme.success,
            warn=theme.warning,
            crit=theme.error,
            track=theme.variables.get("track", "#3a3a3a"),
        )


XDISP_DARK = Theme(
    name="xdisp-dark",
    primary="#d7875f",
    secondary="#8a8a8a",
    accent="#d7875f",
    foreground="#e8e4de",
    background="#141414",
    surface="#1e1e1e",
    panel="#262626",
    success="#87af87",
    warning="#d7af5f",
    error="#d75f5f",
    dark=True,
    variables={
        "footer-key-foreground": "#d7875f",
        "block-cursor-background": "#262626",
        "block-cursor-foreground": "#e8e4de",
        "block-cursor-text-style": "none",
        "track": "#3a3a3a",
    },
)

XDISP_LIGHT = Theme(
    name="xdisp-light",
    primary="#954c2a",
    secondary="#635d55",
    accent="#954c2a",
    foreground="#2b2723",
    background="#faf7f2",
    surface="#efeae1",
    panel="#e2dbcf",
    success="#3d6b3d",
    warning="#795911",
    error="#ad3128",
    dark=False,
    variables={
        "footer-key-foreground": "#954c2a",
        "block-cursor-background": "#e2dbcf",
        "block-cursor-foreground": "#2b2723",
        "block-cursor-text-style": "none",
        "track": "#cec7ba",
    },
)
