from PySide6.QtCore import Qt


LIGHT = dict(
    page="#eef2f7", panel="#ffffff", canvas="#f7f9fc", text="#172940", muted="#4f627a",
    line="#d4deeb", accent="#2055a5", accent_bg="#e8f0fe", button="#2055a5", hover="#174684",
    changed="#8a500a", changed_bg="#fff4df", affected="#146756", affected_bg="#e5f6f0",
    neutral="#52647b", neutral_bg="#f1f4f9", edge="#627c98",
)
DARK = dict(
    page="#111925", panel="#192536", canvas="#141f2e", text="#ecf2fc", muted="#b3c1d5",
    line="#38495e", accent="#a2c5ff", accent_bg="#253c5c", button="#285ca8", hover="#316dbd",
    changed="#ffc87c", changed_bg="#382b20", affected="#83dfc6", affected_bg="#1b3735",
    neutral="#b3c1d5", neutral_bg="#263548", edge="#91a9c4",
)


def colors_for(app):
    return DARK if app.styleHints().colorScheme() == Qt.ColorScheme.Dark else LIGHT


def stylesheet(c):
    return f"""
        QMainWindow, QWidget#page {{ background-color: {c['page']}; }}
        QWidget {{ color: {c['text']}; font-size: 14px; }}
        QLabel#brand {{ font-size: 28px; font-weight: bold; }}
        QLabel#muted {{ color: {c['muted']}; }}
        QFrame#sourceCard {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 12px; }}
        QPushButton, QComboBox, QLineEdit {{
            min-height: 20px; padding: 6px 10px; background: {c['panel']};
            border: 1px solid {c['line']}; border-radius: 6px;
        }}
        QCheckBox {{ color: {c['text']}; spacing: 7px; }}
        QCheckBox:disabled {{ color: {c['muted']}; }}
        QCheckBox::indicator {{
            width: 13px; height: 13px; border: 1px solid {c['edge']};
            border-radius: 4px; background: {c['panel']};
        }}
        QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
        QCheckBox::indicator:disabled {{ border-color: {c['line']}; }}
        QComboBox {{ padding-right: 20px; }}
        QComboBox QAbstractItemView {{
            background: {c['panel']}; color: {c['text']}; selection-background-color: {c['accent_bg']};
            selection-color: {c['text']};
        }}
        QPushButton:hover {{ background: {c['accent_bg']}; }}
        QPushButton#primary {{ background: {c['button']}; border-color: {c['button']}; color: white; font-weight: bold; }}
        QPushButton#primary:hover {{ background: {c['hover']}; }}
        QPushButton:disabled, QLineEdit:disabled, QComboBox:disabled {{ color: {c['muted']}; }}
        QPushButton#primary:disabled {{ background: {c['accent_bg']}; border-color: {c['line']}; color: {c['muted']}; }}
        QPushButton:focus, QLineEdit:focus, QComboBox:focus {{ border: 1px solid {c['accent']}; }}
        QTabWidget::tab-bar {{ alignment: left; }}
        QTabWidget::pane {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 8px; }}
        QTabBar::tab {{ padding: 9px 13px; color: {c['muted']}; border-bottom: 3px solid transparent; }}
        QTabBar::tab:selected {{ color: {c['accent']}; border-bottom-color: {c['accent']}; font-weight: bold; }}
        QTextBrowser, QTreeWidget, QGraphicsView {{
            background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 7px;
            selection-background-color: {c['accent_bg']}; selection-color: {c['text']};
        }}
        QTreeWidget::item {{ padding: 4px; }}
        QHeaderView::section {{
            color: {c['muted']}; background: {c['canvas']}; padding: 6px;
            border: 0; border-bottom: 1px solid {c['line']};
        }}
        QScrollBar:vertical {{ background: {c['panel']}; width: 11px; margin: 0; }}
        QScrollBar::handle:vertical {{ background: {c['line']}; min-height: 24px; border-radius: 5px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QProgressBar {{ background: {c['line']}; border: 0; border-radius: 2px; }}
        QProgressBar::chunk {{ background: {c['accent']}; }}
    """
