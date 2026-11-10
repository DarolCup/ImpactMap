import argparse
from html import escape
import os
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QStandardPaths, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QProgressBar, QPushButton, QSplitter, QStyle,
    QTabWidget, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from analysis import AnalysisError
from github_repo import REPOSITORY, analyze_github, parse_repository
from graph import DependencyGraph
from local_repo import analyze_example, analyze_local
from result import ResultModel, chain_text, display_path, finding_text, limitation_text
from theme import colors_for, stylesheet


ROLE = Qt.ItemDataRole.UserRole


class AnalysisThread(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, source: Path | str | None, cache: Path, analyzer: str | None, parent: QWidget):
        super().__init__(parent)
        self.source = source
        self.cache = cache
        self.analyzer = analyzer

    def run(self):
        try:
            if self.source is None:
                result = analyze_example(self.analyzer)
            elif isinstance(self.source, Path):
                result = analyze_local(self.source, self.analyzer)
            else:
                result = analyze_github(self.cache, self.source, self.analyzer)
            self.ready.emit(result)
        except (AnalysisError, OSError) as exc:
            self.failed.emit(str(exc))


def plain_label(text="", name=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


def browser(name):
    widget = QTextBrowser()
    widget.setAccessibleName(name)
    widget.setOpenLinks(False)
    widget.document().setDocumentMargin(14)
    widget.setMinimumSize(0, 72)
    return widget


class MainWindow(QMainWindow):
    def __init__(self, analyzer: str | None):
        super().__init__()
        self.analyzer = analyzer
        self.worker = None
        self.model = None
        self.selected_path = None
        self.last_github = None
        self.active_source = None
        self.cache = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation))
        self.colors = colors_for(QApplication.instance())
        self.setStyleSheet(stylesheet(self.colors))
        self.setWindowTitle("ImpactMap")
        self.resize(1100, 740)
        self.setMinimumSize(575, 465)

        page = QWidget(objectName="page")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(self.build_source())

        self.summary = plain_label(name="muted")
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setAccessibleName("Выполняется проверка")
        self.progress.hide()
        layout.addWidget(self.progress)

        self.tabs = QTabWidget()
        self.overview = browser("Обзор результата")
        self.overview.anchorClicked.connect(self.overview_link)
        self.tabs.addTab(self.overview, "Обзор")
        self.tabs.addTab(self.build_map(), "Карта")
        self.tabs.addTab(self.build_files(), "Файлы и подробности")
        self.tabs.currentChanged.connect(self.tab_changed)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(page)
        self.show_mode()

    def build_source(self):
        card = QFrame(objectName="sourceCard")
        content = QVBoxLayout(card)
        content.setContentsMargins(14, 10, 14, 12)
        top = QHBoxLayout()
        top.setSpacing(12)
        brand = plain_label("ImpactMap", "brand")
        top.addWidget(brand)
        top.addWidget(plain_label("Проверка влияния изменений", "muted"), 1)
        self.demo = QPushButton("▷  Попробовать на примере")
        self.demo.setAccessibleName("Попробовать на примере")
        self.demo.clicked.connect(lambda: self.start(None))
        top.addWidget(self.demo)
        content.addLayout(top)

        self.mode = QComboBox()
        self.mode.addItems(["GitHub", "Локальная папка"])
        self.mode.setAccessibleName("Источник репозитория")
        self.mode.currentIndexChanged.connect(self.show_mode)
        self.repository = QLineEdit(REPOSITORY)
        self.repository.setPlaceholderText("владелец/репозиторий или ссылка GitHub")
        self.repository.setAccessibleName("Публичный репозиторий GitHub")
        self.repository.returnPressed.connect(self.check_source)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Путь к Git-репозиторию")
        self.folder.setAccessibleName("Папка репозитория")
        self.folder.returnPressed.connect(self.check_source)
        self.choose = QPushButton("Обзор…")
        self.choose.setAccessibleName("Выбрать папку Git-репозитория")
        self.choose.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.choose.clicked.connect(self.choose_folder)
        self.check = QPushButton("Проверить изменения", objectName="primary")
        self.check.clicked.connect(self.check_source)
        row = QHBoxLayout()
        for widget in (self.mode, self.repository, self.folder, self.choose, self.check):
            row.addWidget(widget, 1 if widget in (self.repository, self.folder) else 0)
        content.addLayout(row)
        return card

    def build_map(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        tools = QHBoxLayout()
        self.full = QCheckBox("Полная карта")
        self.full.toggled.connect(self.refresh_map)
        self.picker = QComboBox()
        self.picker.setAccessibleName("Выбрать файл на карте")
        self.picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.picker.setMinimumContentsLength(9)
        self.picker.currentIndexChanged.connect(self.pick_path)
        tools.addWidget(self.full)
        tools.addWidget(self.picker, 1)
        self.graph = DependencyGraph(self.colors)
        for text, label, action in (
            ("−", "Уменьшить масштаб", lambda: self.graph.zoom(1 / 1.25)),
            ("+", "Увеличить масштаб", lambda: self.graph.zoom(1.25)),
            ("Вписать в окно", "Вписать карту в окно", self.graph.fit),
        ):
            button = QPushButton(text)
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.clicked.connect(action)
            if len(text) == 1:
                button.setFixedWidth(36)
            tools.addWidget(button)
        layout.addLayout(tools)

        c = self.colors
        legend = QLabel(
            f'<span style="color:{c["changed"]}">● Изменённые</span> &nbsp; '
            f'<span style="color:{c["affected"]}">● Затронутые</span> &nbsp; '
            f'<span style="color:{c["accent"]}">◆ Выбранный</span> &nbsp; · &nbsp; A → B: A использует B'
        )
        legend.setWordWrap(True)
        legend_row = QHBoxLayout()
        legend_row.addWidget(legend, 1)
        self.only = QCheckBox("Только выбранная цепочка")
        self.only.setEnabled(False)
        self.only.toggled.connect(self.redraw_selection)
        legend_row.addWidget(self.only)
        layout.addLayout(legend_row)
        self.graph.selected.connect(self.select_path)
        self.map_detail = browser("Пояснение к выбранному файлу")
        self.map_detail.setMinimumHeight(116)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.graph)
        splitter.addWidget(self.map_detail)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 145])
        layout.addWidget(splitter, 1)
        return panel

    def build_files(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(plain_label("Выберите файл или раскройте группу предупреждений.", "muted"))
        self.files = QTreeWidget()
        self.files.setHeaderLabels(["Файл или группа", "Состояние"])
        self.files.setUniformRowHeights(True)
        self.files.setAccessibleName("Изменения, связанные файлы и предупреждения")
        self.files.header().setStretchLastSection(False)
        self.files.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.files.currentItemChanged.connect(self.file_selected)
        self.file_detail = browser("Полный текст и цепочки")
        self.file_detail.anchorClicked.connect(self.detail_link)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.files)
        splitter.addWidget(self.file_detail)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(1, False)
        layout.addWidget(splitter, 1)
        return panel

    def write(self, target, content):
        target.setHtml(
            f'<body style="color:{self.colors["text"]}; font-size:11pt;">{content}</body>'
        )

    def heading(self, text, level=2):
        return f"<h{level}>{escape(text)}</h{level}>"

    def link(self, text, target):
        return f'<a style="color:{self.colors["accent"]}" href="{target}">{escape(text)}</a>'

    def tab_changed(self, index):
        if index == 1:
            self.graph.fit()

    def show_mode(self):
        local = self.mode.currentIndex() == 1
        self.folder.setVisible(local)
        self.choose.setVisible(local)
        self.repository.setVisible(not local)
        self.clear_result()
        self.summary.setText("Источник · Локальная папка" if local else "Источник · Публичный репозиторий GitHub")
        prompt = ("Выберите папку проекта с Git. Сравним рабочую папку с последним коммитом (HEAD)."
                  if local else "Введите имя публичного репозитория или ссылку на него. Сравним последний коммит ветки по умолчанию с его первым родителем.")
        self.write(self.overview, self.heading("Проверьте свой проект", 1) + f"<p>{prompt}</p>"
                   "<p>Нажмите «Проверить изменения». Начать можно и с готового примера из трёх файлов Python.</p>")

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Выберите папку Git-репозитория", self.folder.text())
        if path:
            self.folder.setText(path)

    def check_source(self):
        try:
            if self.mode.currentIndex() == 0:
                source = parse_repository(self.repository.text())
                self.repository.setText(source)
            else:
                if not self.folder.text():
                    raise AnalysisError("Выберите папку Git-репозитория и повторите проверку.")
                source = Path(self.folder.text()).expanduser()
        except (AnalysisError, RuntimeError) as exc:
            self.show_error(str(exc))
            return
        self.start(source)

    def clear_result(self):
        self.model = None
        self.selected_path = None
        self.tabs.setCurrentIndex(0)
        self.tabs.setTabEnabled(1, False)
        self.tabs.setTabEnabled(2, False)
        self.files.clear()
        self.graph.show_result(None)

    def start(self, source):
        if self.worker is not None:
            return
        self.active_source = source
        self.clear_result()
        self.summary.setText(str(source) if source is not None else "Учебный проект · Python")
        self.progress.show()
        message = ("Загружаем последний коммит и его первый родитель с GitHub."
                   if isinstance(source, str) else "Готовим снимки и ищем связи между файлами.")
        self.write(self.overview, self.heading("Проверяем изменения…", 1) + f"<p>{message}</p>")
        self.set_controls_enabled(False)
        self.worker = AnalysisThread(source, self.cache, self.analyzer, self)
        self.worker.ready.connect(self.show_result)
        self.worker.failed.connect(self.analysis_failed)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def show_error(self, message):
        self.clear_result()
        self.summary.setText("Проверка прервана")
        if message.startswith("Не удалось обновить"):
            hint = "Проверьте подключение и имя публичного репозитория, затем повторите проверку."
        else:
            hint = "Проверьте источник или попробуйте учебный пример."
        self.write(self.overview, self.heading("Не удалось проверить изменения", 1)
                   + f"<p>{hint}</p><p>{escape(message).replace(chr(10), '<br>')}</p>")

    def analysis_failed(self, message):
        if (message.startswith("Не удалось обновить") and self.last_github is not None
                and self.last_github[0] == self.active_source):
            self.show_result(self.last_github[1])
            self.render_overview(message)
        else:
            self.show_error(message)

    def show_result(self, result):
        self.model = model = ResultModel(result)
        if isinstance(self.active_source, str):
            self.last_github = (self.active_source, result)
        self.summary.setText(f"{result.source}   ·   {result.comparison}")
        self.tabs.setTabEnabled(1, True)
        self.tabs.setTabEnabled(2, True)
        with QSignalBlocker(self.full), QSignalBlocker(self.only):
            self.full.setChecked(False)
            self.only.setChecked(False)
        self.populate_files()
        self.refresh_map()
        self.render_overview()
        self.tabs.setCurrentIndex(0)

    def render_overview(self, refresh_error=None):
        model = self.model
        result = model.result
        c = self.colors
        counts = [("Изменено файлов", len(model.changes)), ("Связано с правками", len(model.affected)),
                  ("Требуют внимания", len(model.findings))]
        cells = "".join(
            f'<td bgcolor="{c["canvas"]}" width="33%"><span style="color:{c["accent"]};font-size:21pt">'
            f'<b>{count}</b></span><br>{label}</td>' for label, count in counts
        )
        content = [self.heading("Результат проверки", 1),
                   f'<table width="100%" cellspacing="6" cellpadding="8"><tr>{cells}</tr></table><p></p>']
        if refresh_error:
            content.insert(0, self.heading("GitHub недоступен. Показана предыдущая проверка.")
                           + f"<p>{escape(refresh_error).replace(chr(10), '<br>')}</p>")
        content.extend(f"<p>{escape(note)}</p>" for note in result.notes)
        content.append(self.heading("Что проверить"))
        if not model.findings:
            content.append("<p>Анализатор не сообщил о конкретных проблемах.</p>")
        for item in model.findings:
            title, detail, action = finding_text(item)
            confidence = "Подтверждённая связь" if item["confidence"] == "confirmed" else "Возможная проблема"
            priority = " · Высокий приоритет" if item["severity"] == "high" else ""
            content.append(f'<p><span style="color:{c["changed"]}"><b>{confidence}{priority}</b></span><br>'
                           f'<b>{escape(title)}</b><br>{escape(detail)} {escape(action)}</p>')
        if model.affected:
            content.append(self.heading("Почему файлы связаны"))
            content.append("<p>Прямая связь означает, что файл использует изменённый файл. "
                           "Косвенная связь проходит через другие файлы.</p>")
            for index, path in enumerate(model.affected):
                route = model.chains.get(path, ())
                text = chain_text(route) if route else display_path(path)
                kind = "Прямая связь" if model.affected[path]["distance"] == 1 else "Косвенная связь"
                content.append(f"<p>{kind} · {self.link(text, f'chain:{index}')}</p>")
        else:
            content.append(self.heading("Связанных файлов не найдено"))
        if model.limitations or model.warnings or "impact" not in result.response:
            content.append(self.heading("Ограничения проверки"))
            if "impact" not in result.response:
                content.append("<p>Эта версия анализатора показывает связи между файлами. "
                               "Подробная проверка удалений, переименований и объявлений недоступна.</p>")
            if model.warnings:
                content.append("<p>Часть импортов или исходников могла остаться вне проверки.</p>")
            warnings = {message for messages in model.warnings.values() for message in messages}
            limitations = (note for note in model.limitations if note not in warnings)
            content.extend(f"<p>{escape(note)}</p>" for note in dict.fromkeys(map(limitation_text, limitations)))
        content.append(f'<p>{self.link("Файлы и подробности →", "files:open")}</p>')
        self.write(self.overview, "".join(content))

    def overview_link(self, url):
        if url.scheme() == "chain":
            path = list(self.model.affected)[int(url.path())]
            self.tabs.setCurrentIndex(1)
            self.select_path(path, center=True)
        elif url.scheme() == "files":
            self.tabs.setCurrentIndex(2)

    def refresh_map(self):
        self.graph.show_result(self.model, self.full.isChecked())
        with QSignalBlocker(self.picker):
            self.picker.clear()
            self.picker.addItem("Выберите файл")
            for path in sorted(self.graph.nodes):
                self.picker.addItem(display_path(path), path)
                self.picker.setItemData(self.picker.count() - 1, display_path(path), Qt.ItemDataRole.ToolTipRole)
        if self.selected_path not in self.graph.nodes:
            self.selected_path = None
        self.select_path(self.selected_path)

    def pick_path(self, _index):
        self.select_path(self.picker.currentData(), center=True)

    def select_path(self, path, *, center=False):
        self.selected_path = path
        with QSignalBlocker(self.picker):
            self.picker.setCurrentIndex(max(0, self.picker.findData(path)) if path else 0)
        self.only.setEnabled(path is not None)
        if self.only.isChecked():
            if not path:
                with QSignalBlocker(self.only):
                    self.only.setChecked(False)
            self.redraw_selection()
        else:
            self.graph.highlight(path, center=center)
        self.write(self.map_detail, self.path_detail(path) if path else
                   "<p>Нажмите на файл или выберите его в списке, чтобы увидеть его цепочку. "
                   "Масштабируйте колёсиком и перетаскивайте карту мышью.</p>")

    def redraw_selection(self):
        only = self.selected_path if self.only.isChecked() else None
        self.graph.show_result(self.model, self.full.isChecked(), only=only)
        self.graph.highlight(self.selected_path)

    def path_detail(self, path):
        model = self.model
        title = self.heading(f"{display_path(path)} · {model.role(path)}", 3)
        info = ""
        change = model.changes.get(path)
        if change and change.old_path:
            info += f"<p>Прежнее имя · {escape(display_path(change.old_path))}</p>"
        if path in model.chains:
            chain = model.chains[path]
            if len(chain) == 2:
                explanation = f"{display_path(path)} использует изменённый файл {display_path(chain[-1])}."
            elif len(chain) > 2:
                explanation = (f"{display_path(path)} зависит от правки в {display_path(chain[-1])} "
                               f"через {', '.join(map(display_path, chain[1:-1]))}.")
            else:
                explanation = f"Анализатор связал {display_path(path)} с изменениями."
            info += f"<p>{escape(explanation)}<br><b>{escape(chain_text(chain))}</b></p>"
        elif path in model.changed:
            nodes, _ = model.selection(path)
            routes = [model.chains[node] for node in sorted(nodes & model.chains.keys())]
            info += ("<p>Файлы, которые зависят от этой правки:</p>" +
                     "".join(f"<p>{escape(chain_text(chain))}</p>" for chain in routes)
                     if routes else "<p>Связанных файлов не найдено.</p>")
        elif path in model.affected:
            info += "<p>Анализатор сообщил о связи с изменением. Полный путь связи недоступен.</p>"
        else:
            neighbours = sorted(model.outgoing[path]), sorted(model.incoming[path])
            for label, files in zip(("Использует", "Используется в"), neighbours):
                if files:
                    info += f"<p>{label} · {escape(', '.join(map(display_path, files)))}</p>"
        for finding in model.findings:
            if path in finding["paths"]:
                _, detail, action = finding_text(finding)
                info += f"<p>{escape(detail)} {escape(action)}</p>"
        return title + info

    def populate_files(self):
        model = self.model
        self.files.clear()
        self.add_file_group("Изменения", model.changes)
        self.add_file_group("Затронутые файлы", model.affected)
        diagnostics = QTreeWidgetItem(self.files, ["Технические предупреждения", str(sum(map(len, model.warnings.values())))])
        for path, messages in sorted(model.warnings.items()):
            group = QTreeWidgetItem(diagnostics, [display_path(path), str(len(messages))])
            group.setData(0, ROLE, "\n\n".join(messages))
            group.setToolTip(0, display_path(path))
            for index, message in enumerate(messages, 1):
                item = QTreeWidgetItem(group, [f"Сообщение {index}"])
                item.setData(0, ROLE, message)
        if model.findings or model.limitations:
            details = QTreeWidgetItem(self.files, ["Находки и ограничения"])
            for finding in model.findings:
                label = finding_text(finding)[0]
                item = QTreeWidgetItem(details, [label])
                item.setToolTip(0, label)
                item.setData(0, ROLE, finding["evidence"])
            if model.limitations:
                item = QTreeWidgetItem(details, ["Полный текст ограничений", str(len(model.limitations))])
                item.setData(0, ROLE, "\n\n".join(model.limitations))
        self.write(self.file_detail, "<p>Выберите файл или предупреждение, чтобы прочитать подробности.</p>")

    def add_file_group(self, title, files):
        group = QTreeWidgetItem(self.files, [title, str(len(files))])
        for path in files:
            item = QTreeWidgetItem(group, [display_path(path), self.model.role(path)])
            item.setToolTip(0, display_path(path))
            item.setData(0, ROLE, path)
        group.setExpanded(True)

    def file_selected(self, item, _previous):
        if item is None or self.model is None:
            return
        value = item.data(0, ROLE)
        if value is not None and item.parent() and self.files.indexOfTopLevelItem(item.parent()) in (0, 1):
            self.select_path(value)
            body = self.path_detail(value)
            if value in self.graph.nodes:
                body += f"<p>{self.link('Открыть на карте →', 'map:open')}</p>"
            self.write(self.file_detail, body)
        elif value is not None:
            self.file_detail.setPlainText(value)
        else:
            self.write(self.file_detail, "<p>Раскройте группу и выберите строку.</p>")

    def detail_link(self, url):
        if url.scheme() == "map" and self.selected_path:
            self.tabs.setCurrentIndex(1)
            self.select_path(self.selected_path, center=True)

    def finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.progress.hide()
        self.set_controls_enabled(True)

    def set_controls_enabled(self, enabled):
        for control in (self.mode, self.repository, self.folder, self.choose, self.check, self.demo):
            control.setEnabled(enabled)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser(description="ImpactMap для GitHub и локальных Git-репозиториев")
    parser.add_argument("--analyzer", default=os.environ.get("IMPACTMAP_ANALYZER"), help="путь к Rust-анализатору")
    args = parser.parse_args()
    app = QApplication([])
    app.setApplicationName("ImpactMap")
    window = MainWindow(args.analyzer)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
