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
    """Register every known theme + activate ``theme_name`` (if specified) +
    wire a watcher that persists subsequent theme changes to ``config.toml``.

    Why register all of them: Textual's command palette (``Ctrl+P``) lists
    every theme passed to ``app.register_theme``. Registering ``ben10`` even
    when the user hasn't activated it lets them discover it through
    ``Ctrl+P → Change theme`` instead of having to know the CLI flag.

    The persistence watcher fires whenever ``app.theme`` changes after mount
    — typically because the user picked a different theme via ``Ctrl+P``.
    Built-in Textual themes (``textual-dark``, ``textual-light``, etc.) are
    mapped to our cleared / "default" state.

    A theme name of ``None`` / ``""`` / ``"default"`` skips activation but
    still does the registration + watcher wiring. Unknown custom names are
    silently ignored at activation — better to fall back to the default
    palette than crash the app over a typo.
    """
    for theme in _THEMES.values():
        # register_theme is idempotent on the same name; safe to call every
        # app boot.
        app.register_theme(theme)

    if theme_name and theme_name != "default":
        theme = _THEMES.get(theme_name)
        if theme is not None:
            app.theme = theme.name

    # init=False so we don't persist the initial textual-dark on startup of
    # an unconfigured user. After mount, every change goes through this.
    app.watch(app, "theme", _persist_theme_change, init=False)


def _persist_theme_change(new_theme: str) -> None:
    """Save the user's picked theme back to ``config.toml``. Silent on any
    config-write failure — the user can always re-set via the CLI, and a
    pop-up here would interrupt the visual change they just made.
    """
    from clickup_work.config import ConfigError, save_theme

    # Textual's built-in themes all share the ``textual-`` prefix; treat
    # picking any of them as "clear my custom preference".
    persistable = "default" if new_theme.startswith("textual-") else new_theme
    try:
        save_theme(persistable)
    except ConfigError:
        pass


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
