"""Colours and stylesheets for the light and dark themes.

Design: a deep indigo-to-violet sidebar, a gradient page banner, white (or deep navy) cards
tinted with the colour of what they show, and one vivid colour per activity state that is
used identically in every chart, indicator and report.
"""
from __future__ import annotations

# One colour per activity state, used by every chart and indicator.
STATUS_COLORS = {
    "ACTIVE": "#10b981",       # emerald
    "IDLE": "#f59e0b",         # amber
    "LOCKED": "#0ea5e9",       # sky
    "SLEEP_OFF": "#8b5cf6",    # violet
    "UNMONITORED": "#cbd5e1",  # slate
}
STATUS_LABELS = {
    "ACTIVE": "Active", "IDLE": "Idle", "LOCKED": "Locked",
    "SLEEP_OFF": "Sleep / Off", "UNMONITORED": "Unmonitored",
}
CATEGORY_COLORS = {
    "productive": "#10b981", "neutral": "#94a3b8", "distraction": "#f43f5e",
    "personal": "#6366f1", "custom": "#d946ef", "uncategorized": "#cbd5e1",
}
# Extra accent colours for tiles that are not an activity state.
ACCENTS = {"indigo": "#6366f1", "pink": "#ec4899", "sky": "#0ea5e9", "orange": "#f97316",
           "violet": "#8b5cf6", "teal": "#14b8a6"}

# tone name -> base colour. A Card picks its tone from the accent colour it is given.
TONES = {
    "active": STATUS_COLORS["ACTIVE"], "idle": STATUS_COLORS["IDLE"],
    "locked": STATUS_COLORS["LOCKED"], "sleep": STATUS_COLORS["SLEEP_OFF"],
    "neutral": CATEGORY_COLORS["neutral"], "distraction": CATEGORY_COLORS["distraction"],
    "personal": CATEGORY_COLORS["personal"], "custom": CATEGORY_COLORS["custom"],
    **ACCENTS,
}
TONE_BY_COLOR = {v.lower(): k for k, v in TONES.items()}

PALETTES = {
    "light": dict(bg="#f4f5ff", surface="#ffffff", border="#e6e8f5", text="#1e2140",
                  muted="#6b7090", accent="#6366f1", accent2="#8b5cf6", banner1="#6366f1",
                  banner2="#8b5cf6", banner3="#ec4899", side1="#2e2a86", side2="#5b3fd1",
                  alt_row="#f8f9ff", tint=0.12, tint_end=0.0),
    "dark": dict(bg="#0e0f1f", surface="#171932", border="#2a2d4d", text="#eef0ff",
                 muted="#9aa0c8", accent="#8b8cff", accent2="#a78bfa", banner1="#4f46e5",
                 banner2="#7c3aed", banner3="#db2777", side1="#12122b", side2="#2c1a63",
                 alt_row="#1b1d3a", tint=0.20, tint_end=0.04),
}


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def blend(color: str, base: str, amount: float) -> str:
    """`amount` of `color` mixed into `base` (0 = base, 1 = color)."""
    (r1, g1, b1), (r2, g2, b2) = _rgb(color), _rgb(base)
    mix = lambda a, b: round(b + (a - b) * amount)
    return f"#{mix(r1, r2):02x}{mix(g1, g2):02x}{mix(b1, b2):02x}"


def _tone_rules(p: dict, theme: str) -> str:
    """Per-tone card colours (tinted gradient, coloured top edge, coloured number)."""
    out = []
    for name, base in TONES.items():
        start = blend(base, p["surface"], p["tint"])
        end = blend(base, p["surface"], p["tint_end"])
        value = base if theme == "dark" else blend("#000000", base, 0.18)   # darker on white
        out.append(f"""
    #card[tone="{name}"] {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {start}, stop:1 {end});
                          border: 1px solid {blend(base, p['border'], 0.35)};
                          border-top: 3px solid {base}; }}
    #card[tone="{name}"] #cardValue {{ color: {value}; }}""")
    return "".join(out)


def stylesheet(theme: str, icons: dict | None = None) -> str:
    """`icons` maps check / up / down to small image paths (tick and arrows) drawn at start-up."""
    p = PALETTES.get(theme, PALETTES["light"])
    icons = icons or {}
    tick = f"image: url({icons["check"]});" if "check" in icons else ""
    down = f"image: url({icons["down"]});" if "down" in icons else ""
    up = f"image: url({icons["up"]});" if "up" in icons else ""
    return f"""
    QWidget {{ font-family: 'Segoe UI Variable Text', 'Segoe UI'; font-size: 13px; color: {p['text']}; }}
    QMainWindow, QStackedWidget, #page {{ background: {p['bg']}; }}

    /* ---- sidebar ---- */
    #sidebar {{ background: qlineargradient(x1:0,y1:0,x2:0.6,y2:1, stop:0 {p['side1']}, stop:1 {p['side2']}); }}
    #sidebar QLabel {{ color: white; background: transparent; }}
    #brandName {{ font-size: 19px; font-weight: 700; color: white; }}
    #brandTag {{ font-size: 11px; color: rgba(255,255,255,0.65); }}
    QPushButton#nav {{ text-align: left; padding: 10px 14px; border: none; border-radius: 10px;
                       margin: 2px 12px; background: transparent; color: rgba(255,255,255,0.86); }}
    QPushButton#nav:hover {{ background: rgba(255,255,255,0.12); color: white; }}
    QPushButton#nav:checked {{ background: rgba(255,255,255,0.22); color: white; font-weight: 600; }}

    /* ---- page banner ---- */
    #banner {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {p['banner1']}, stop:0.55 {p['banner2']},
                                          stop:1 {p['banner3']}); border-radius: 18px; }}
    #bannerTitle {{ font-size: 24px; font-weight: 700; color: white; background: transparent; }}
    #bannerSub {{ font-size: 13px; color: rgba(255,255,255,0.88); background: transparent; }}
    #h1 {{ font-size: 22px; font-weight: 700; }}
    #sub, .muted {{ color: {p['muted']}; }}

    /* ---- cards and panels ---- */
    #card {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 14px; }}
    #cardTitle {{ color: {p['muted']}; font-size: 12px; font-weight: 600; background: transparent; }}
    #cardValue {{ font-size: 26px; font-weight: 700; background: transparent; }}
    #card QLabel#sub {{ background: transparent; }}
    #panelTitle {{ font-size: 14px; font-weight: 700; background: transparent; }}
    {_tone_rules(p, theme)}

    /* ---- assistant chat ---- */
    #userBubble {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #5457e6, stop:1 #7c3aed); color: white;
                   border-radius: 16px; padding: 10px 16px; }}
    #botBubble {{ background: {blend(p['accent'], p['surface'], 0.07)}; border: 1px solid {blend(p['accent'], p['border'], 0.25)};
                  border-radius: 16px; }}
    #fileRow {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 12px; }}
    #fileName {{ font-weight: 700; font-size: 13px; background: transparent; }}
    #chatArea {{ background: transparent; }}
    QPushButton#chip {{ background: {blend(p['accent'], p['surface'], 0.10)}; border: 1px solid {blend(p['accent'], p['border'], 0.35)};
                        border-radius: 15px; padding: 6px 14px; color: {p['accent']}; font-weight: 600; }}
    QPushButton#chip:hover {{ background: {blend(p['accent'], p['surface'], 0.22)}; }}
    QMenu {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 10px; padding: 6px; }}
    QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {blend(p['accent'], p['surface'], 0.18)}; }}
    QPushButton::menu-indicator {{ image: none; width: 0; }}

    /* ---- buttons ---- */
    QPushButton {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 10px;
                   padding: 8px 16px; }}
    QPushButton:hover {{ border-color: {p['accent']}; background: {blend(p['accent'], p['surface'], 0.08)}; }}
    QPushButton:pressed {{ background: {blend(p['accent'], p['surface'], 0.16)}; }}
    QPushButton:disabled {{ color: {p['muted']}; }}
    QPushButton#primary {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #5457e6, stop:1 #7c3aed);
                           color: white; border: none; font-weight: 600; }}
    QPushButton#primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #6b6ef0,
                                                             stop:1 #8f55f3); }}
    QPushButton#primary:disabled {{ background: {p['border']}; color: {p['muted']}; }}

    /* ---- tables ---- */
    QTableWidget {{ background: transparent; border: none; gridline-color: transparent;
                    alternate-background-color: {p['alt_row']}; selection-background-color: {blend(p['accent'], p['surface'], 0.18)};
                    selection-color: {p['text']}; }}
    QTableWidget::item {{ padding: 3px; }}
    QHeaderView {{ background: transparent; }}
    QTableCornerButton::section {{ background: transparent; border: none; }}
    QHeaderView::section {{ background: transparent; border: none; border-bottom: 1px solid {p['border']};
                            color: {p['muted']}; padding: 5px 4px; font-weight: 600; text-align: left; }}

    /* ---- tabs ---- */
    QTabWidget::pane {{ border: none; background: transparent; }}
    QTabBar::tab {{ padding: 9px 20px; background: transparent; border: none;
                    border-bottom: 3px solid transparent; color: {p['muted']}; font-weight: 600; }}
    QTabBar::tab:selected {{ color: {p['accent']}; border-bottom: 3px solid {p['accent']}; }}
    QTabBar::tab:hover {{ color: {p['text']}; }}

    /* ---- inputs ---- */
    QLineEdit, QComboBox, QSpinBox, QDateEdit, QListWidget {{ background: {p['surface']};
        border: 1px solid {p['border']}; border-radius: 9px; padding: 5px 8px;
        selection-background-color: {p['accent']}; selection-color: white; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus {{ border: 1px solid {p['accent']}; }}
    QComboBox QAbstractItemView {{ background: {p['surface']}; border: 1px solid {p['border']};
        selection-background-color: {blend(p['accent'], p['surface'], 0.2)}; selection-color: {p['text']}; }}
    QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
    QCheckBox::indicator {{ width: 18px; height: 18px; border: 2px solid {blend(p['muted'], p['surface'], 0.6)};
                            border-radius: 6px; background: {p['surface']}; }}
    QCheckBox::indicator:hover {{ border-color: {p['accent']}; }}
    QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; {tick} }}
    QRadioButton::indicator {{ width: 18px; height: 18px; border: 2px solid {blend(p['muted'], p['surface'], 0.6)};
                               border-radius: 11px; background: {p['surface']}; }}
    QRadioButton::indicator:checked {{ width: 10px; height: 10px; border: 6px solid {p['accent']};
                                       border-radius: 11px; background: white; }}
    QComboBox::drop-down, QDateEdit::drop-down {{ border: none; width: 26px; }}
    QComboBox::down-arrow, QDateEdit::down-arrow {{ {down} width: 12px; height: 12px; }}
    QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 22px; border: none; }}
    QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 22px; border: none; }}
    QSpinBox::up-arrow {{ {up} width: 10px; height: 10px; }}
    QSpinBox::down-arrow {{ {down} width: 10px; height: 10px; }}

    /* ---- scrolling ---- */
    QScrollArea {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {blend(p['accent'], p['border'], 0.35)}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {p['accent']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{ background: {blend(p['accent'], p['border'], 0.35)}; border-radius: 5px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QToolTip {{ background: {p['surface']}; color: {p['text']}; border: 1px solid {p['border']}; padding: 4px; }}
    """


def chart_colors(theme: str) -> dict:
    p = PALETTES.get(theme, PALETTES["light"])
    return dict(bg=p["surface"], text=p["text"], muted=p["muted"], grid=p["border"])


def fmt_duration(seconds: float) -> str:
    """Compact human duration: 5h 02m, 12m 05s, 40s."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"
