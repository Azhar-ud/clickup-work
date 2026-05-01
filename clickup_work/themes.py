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


# ---- Omnitrix watermark -------------------------------------------------

# Hourglass shape inside a circular faceplate, drawn with light-shade blocks
# (``░``) so the result reads as frosted glass rather than a solid sticker.
# Each row is the same width so the centred render lines up cleanly. The
# hourglass narrows row-by-row to a single character at the waist, then
# widens again — same shape as the Omnitrix face on Ben's wrist.
_OMNITRIX_ART = (
    "       ╭───────────╮       \n"
    "      ╱             ╲      \n"
    "     │  ░░░░░░░░░░░  │     \n"
    "     │   ░░░░░░░░░   │     \n"
    "     │    ░░░░░░░    │     \n"
    "     │     ░░░░░     │     \n"
    "     │      ░░░      │     \n"
    "     │       ░       │     \n"
    "     │      ░░░      │     \n"
    "     │     ░░░░░     │     \n"
    "     │    ░░░░░░░    │     \n"
    "     │   ░░░░░░░░░   │     \n"
    "     │  ░░░░░░░░░░░  │     \n"
    "      ╲             ╱      \n"
    "       ╰───────────╯       "
)


class OmnitrixWatermark(Static):
    """Frosted-glass Omnitrix faceplate centred behind the picker UI.

    Rendered on a dedicated ``watermark`` layer so the filter bar, ticket
    list, status row, and footer compose on top. The chrome around those
    widgets is set ``background: transparent`` here so the dim green
    watermark bleeds through the gaps and behind the row text.

    Why ``░`` instead of ``█``: the light-shade block reads as a translucent
    overlay against the near-black theme background, which is the "glass"
    feel we want. Solid blocks turn it into a sticker that competes with
    the foreground text.
    """

    DEFAULT_CSS = """
    OmnitrixWatermark {
        layer: watermark;
        /* Fixed size matching the ASCII art so we only obscure cells the
           watermark actually paints. Textual composites topmost-wins per
           cell — there's no real alpha — so a full-screen watermark would
           block listview text even in the space cells. Sized to the art,
           only the 27×15 footprint is touched. */
        width: 27;
        height: 15;
        background: transparent;
        /* Very dim green so the symbol reads as a watermark behind the
           bright-green ticket rows that pass through its footprint. */
        color: #1a4400;
        text-align: center;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(_OMNITRIX_ART, **kwargs)
