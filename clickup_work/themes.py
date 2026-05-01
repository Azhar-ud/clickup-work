"""Ben 10 theme — Omnitrix-green-on-black palette + a binary-scroll banner.

Apply with :func:`apply_theme` in an ``App.on_mount`` handler, passing the
chosen theme name. ``"ben10"`` swaps the palette to Omnitrix green and tells
the picker to render the :class:`OmnitrixBanner`. ``"default"`` (or unset)
leaves the standard Textual dark theme untouched.

Why this lives in its own module: the picker, plan screen, post-Claude
flow, and workload TUI all want the same theme treatment, but each is its
own ``App``. Centralising the theme + banner widget means none of them need
to know the palette details.
"""

from __future__ import annotations

from textual.app import App
from textual.theme import Theme
from textual.widgets import Static

# ---- palette ------------------------------------------------------------

# Omnitrix-canonical green: bright neon on near-black. The dim variants are
# used for borders and de-emphasized text so the active foreground always
# reads as the brightest thing on screen.
_OMNITRIX_GREEN = "#7eff00"
_OMNITRIX_GREEN_DIM = "#3a7700"
_OMNITRIX_BG = "#000000"
_OMNITRIX_PANEL = "#0a1a05"
_OMNITRIX_BOOST = "#0f2a0a"

BEN10_THEME = Theme(
    name="ben10",
    primary=_OMNITRIX_GREEN,
    secondary=_OMNITRIX_GREEN_DIM,
    accent=_OMNITRIX_GREEN,
    background=_OMNITRIX_BG,
    surface=_OMNITRIX_PANEL,
    panel=_OMNITRIX_BOOST,
    foreground=_OMNITRIX_GREEN,
    success=_OMNITRIX_GREEN,
    warning="#ffaa00",
    error="#ff3333",
    dark=True,
)

# Map of human names → Theme objects we know how to register. Add more here
# (clinical-trial-of-Vilgax-purple? Plumber-blue?) and the rest of the code
# stays unchanged.
_THEMES: dict[str, Theme] = {
    BEN10_THEME.name: BEN10_THEME,
}

VALID_THEMES = ("default", *sorted(_THEMES.keys()))


def apply_theme(app: App, theme_name: str | None) -> None:
    """Register and activate ``theme_name`` on ``app``.

    A name of ``None``, ``""``, or ``"default"`` is a no-op so callers can
    pass through whatever the user/config produced without sanitising it.
    Unknown names are silently ignored — failing the whole app over a bad
    theme name is worse UX than just running with the default palette.
    """
    if not theme_name or theme_name == "default":
        return
    theme = _THEMES.get(theme_name)
    if theme is None:
        return
    # register_theme is idempotent — calling it on every app boot is fine.
    app.register_theme(theme)
    app.theme = theme.name


# ---- Omnitrix banner ----------------------------------------------------

# A 64-char binary string that loops cleanly. Resampling from a longer seed
# every tick gives the impression of a scrolling marquee without keeping
# rolling state per cell. Hand-crafted so the digit distribution looks
# "noisy" rather than periodic.
_BINARY_SEED = (
    "0110100101101001010101101001100110100101100101101001011001011010"
    "1001011010010110101001100110101100110100101100110100101100101101"
)

# Title row content. The double-arrows on either side of "BEN 10 · OMNITRIX"
# evoke the Omnitrix hourglass without trying to draw the full faceplate
# (which never reads cleanly at 1-cell resolution).
_TITLE_TEXT = "▼▼▼  BEN 10 · OMNITRIX  ▲▲▲"


class OmnitrixBanner(Static):
    """3-line marquee: scrolling binary on top + bottom, Omnitrix title row in
    the middle. Ticks once every ~150 ms via :meth:`Widget.set_interval` so
    the binary digits visibly drift without burning CPU.
    """

    DEFAULT_CSS = """
    OmnitrixBanner {
        height: 3;
        padding: 0;
        color: $accent;
        background: $background;
        text-align: center;
        border-bottom: heavy $accent;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._offset = 0

    def on_mount(self) -> None:
        self._draw()
        # 150 ms feels lively without distracting from typing.
        self.set_interval(0.15, self._tick)

    def _tick(self) -> None:
        self._offset = (self._offset + 1) % len(_BINARY_SEED)
        self._draw()

    def _draw(self) -> None:
        # Pull two windows: the second one is the seed reversed so the bottom
        # row drifts the opposite direction. Slightly cooler than both
        # scrolling the same way.
        loop = _BINARY_SEED + _BINARY_SEED
        rev = (_BINARY_SEED[::-1]) * 2
        # `self.size.width` may be 0 before the first layout pass — guard so
        # the first draw still renders something.
        width = max(40, self.size.width or 60)
        top = loop[self._offset : self._offset + width]
        bot = rev[self._offset : self._offset + width]
        self.update(
            f"[dim]{top}[/]\n"
            f"[bold]{_TITLE_TEXT.center(width)}[/]\n"
            f"[dim]{bot}[/]"
        )
