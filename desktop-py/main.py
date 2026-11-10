import argparse
import os
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from analysis import AnalysisError, analyze, repository_changes
from github_repo import REPOSITORY, GitHubResult, analyze_github, parse_repository
from graph import DependencyGraph, display_path


class AnalysisThread(QThread):
    changes_found = Signal(object)
    result_ready = Signal(object, bool)
    github_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, source: Path | str, cache: Path, analyzer: str | None, parent: QWidget):
        super().__init__(parent)
        self.source = source
        self.cache = cache
        self.analyzer = analyzer

    def run(self):
        try:
            if isinstance(self.source, str):
                self.github_ready.emit(analyze_github(self.cache, self.source, self.analyzer))
                return
            repo, changes = repository_changes(self.source)
            self.changes_found.emit(changes)
            result, example = analyze(repo, changes, self.analyzer)
            self.result_ready.emit(result, example)
        except (AnalysisError, OSError) as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, analyzer: str | None):
        super().__init__()
        self.analyzer = analyzer
        self.worker = None
        self.github_result: GitHubResult | None = None
        self.github_error: str | None = None
        self.cache = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation))
        self.setWindowTitle("ImpactMap")
        self.resize(1050, 650)

        self.mode = QComboBox()
        self.mode.addItems(["GitHub", "Локальная папка"])
        self.mode.setAccessibleName("Источник репозитория")
        self.mode.currentIndexChanged.connect(self.show_mode)
        self.repository = QLineEdit(REPOSITORY)
        self.repository.setPlaceholderText("владелец/репозиторий или ссылка GitHub")
        self.repository.setAccessibleName("Публичный репозиторий GitHub")
        self.repository.returnPressed.connect(self.refresh_github)
        self.github_source = QLabel()
        self.github_source.setTextFormat(Qt.TextFormat.PlainText)
        self.github_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.github_source.setWordWrap(True)
        self.refresh = QPushButton("Обновить с GitHub")
        self.refresh.clicked.connect(self.refresh_github)

        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Путь к локальному Git-репозиторию")
        self.folder.setAccessibleName("Папка репозитория")
        self.folder.returnPressed.connect(self.check_changes)
        self.choose = QPushButton("Выбрать папку…")
        self.choose.clicked.connect(self.choose_folder)
        self.check = QPushButton("Проверить изменения")
        self.check.clicked.connect(self.check_changes)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.mode)
        toolbar.addWidget(self.repository, 1)
        toolbar.addWidget(self.refresh)
        toolbar.addWidget(self.folder, 1)
        toolbar.addWidget(self.choose)
        toolbar.addWidget(self.check)

        self.changed = QListWidget()
        self.changed.setAccessibleName("Изменённые файлы")
        self.changed_title = QLabel()
        changed_panel = QWidget()
        changed_layout = QVBoxLayout(changed_panel)
        changed_layout.addWidget(self.changed_title)
        changed_layout.addWidget(self.changed)

        self.graph = DependencyGraph()
        graph_panel = QWidget()
        graph_layout = QVBoxLayout(graph_panel)
        legend = QLabel("A → B означает, что A использует B. Изменённые файлы выделены жёлтым.")
        legend.setWordWrap(True)
        graph_layout.addWidget(legend)
        graph_layout.addWidget(self.graph)

        self.affected = QTreeWidget()
        self.affected.setHeaderLabels(["Файл", "Расстояние", "Причина"])
        self.affected.setRootIsDecorated(False)
        self.affected.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.affected.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)

        self.warnings = QListWidget()
        self.warnings.setWordWrap(True)
        self.tabs = QTabWidget()
        self.tabs.addTab(graph_panel, "Карта зависимостей")
        self.tabs.addTab(self.affected, "Затронутые файлы")
        self.tabs.addTab(self.warnings, "Предупреждения")

        content = QSplitter(Qt.Orientation.Horizontal)
        content.addWidget(changed_panel)
        content.addWidget(self.tabs)
        content.setSizes([280, 770])
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addLayout(toolbar)
        layout.addWidget(self.github_source)
        layout.addWidget(content, 1)
        layout.addWidget(self.status)
        self.setCentralWidget(page)
        self.show_mode()

    def show_mode(self):
        github = self.mode.currentIndex() == 0
        for control in (self.repository, self.github_source, self.refresh):
            control.setVisible(github)
        for control in (self.folder, self.choose, self.check):
            control.setVisible(not github)
        self.clear_result()
        if github:
            self.github_source.clear()
            self.status.setText("Нажмите «Обновить с GitHub», чтобы проверить изменения.")
            if self.github_result is not None:
                self.show_github_result(self.github_result)
            if self.github_error is not None:
                self.show_github_error(self.github_error)
        else:
            self.status.setText("Выберите папку репозитория, чтобы начать.")

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку Git-репозитория", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def check_changes(self):
        if self.worker is not None or self.mode.currentIndex() == 0:
            return
        folder = self.folder.text()
        if not folder:
            self.status.setText("Укажите папку Git-репозитория.")
            return
        self.clear_result()
        self.tabs.setCurrentIndex(0)
        self.status.setText("Проверяю изменения…")
        self.start_analysis(Path(os.path.expanduser(folder)))

    def refresh_github(self):
        if self.worker is not None or self.mode.currentIndex() != 0:
            return
        try:
            repository = parse_repository(self.repository.text())
        except AnalysisError as exc:
            self.show_github_error(str(exc))
            return
        self.repository.setText(repository)
        self.status.setText(f"Обновляю {repository} с GitHub и проверяю изменения…")
        self.start_analysis(repository)

    def clear_result(self):
        self.changed.clear()
        self.changed_title.setText(
            "Изменённые файлы" if self.mode.currentIndex() == 0 else "Изменённые файлы относительно HEAD"
        )
        self.graph.scene().clear()
        self.affected.clear()
        self.tabs.setTabText(1, "Затронутые файлы")
        self.show_warnings([])

    def start_analysis(self, source: Path | str):
        self.set_controls_enabled(False)
        self.worker = AnalysisThread(source, self.cache, self.analyzer, self)
        self.worker.changes_found.connect(self.show_changes)
        self.worker.result_ready.connect(self.show_result)
        self.worker.github_ready.connect(self.accept_github_result)
        self.worker.failed.connect(self.show_github_error if isinstance(source, str) else self.show_error)
        self.worker.finished.connect(self.analysis_finished)
        self.worker.start()

    def show_changes(self, changes: list[str]):
        self.changed.addItems([display_path(path) for path in changes])
        title = "Изменённые файлы" if self.mode.currentIndex() == 0 else "Изменённые файлы относительно HEAD"
        self.changed_title.setText(f"{title} ({len(changes)})")

    def accept_github_result(self, result: GitHubResult):
        self.github_result = result
        self.github_error = None
        self.show_github_result(result)

    def show_github_result(self, result: GitHubResult):
        self.clear_result()
        self.github_source.setText(f"{result.repository} / {result.branch} · {result.commit}")
        self.show_changes(result.changes)
        if result.response is None:
            self.status.setText("В ветке только один коммит. Сравнивать пока не с чем.")
        else:
            self.show_result(result.response, result.example)

    def show_github_error(self, message: str):
        self.github_error = message
        result = self.github_result
        warnings = result.response["warnings"] if result is not None and result.response is not None else []
        self.show_warnings([message, *warnings])
        if result is None:
            self.status.setText("Не удалось проверить GitHub. Подробности во вкладке «Предупреждения».")
            self.tabs.setCurrentWidget(self.warnings)
        else:
            self.status.setText(
                "Обновление не удалось. Показан предыдущий результат. Подробности во вкладке «Предупреждения»."
            )

    def show_result(self, response: dict, example: bool):
        self.graph.show_result(response)
        affected = response["affected_files"]
        for item in affected:
            self.affected.addTopLevelItem(
                QTreeWidgetItem([display_path(item["path"]), str(item["distance"]), item["reason"]])
            )
        self.tabs.setTabText(1, f"Затронутые файлы ({len(affected)})")
        self.show_warnings(response["warnings"])
        if example:
            status = "Анализатор не найден. Во вкладках показан пример JSON, слева — изменения репозитория."
        elif affected:
            status = "Проверка завершена."
        else:
            status = "Проверка завершена. Затронутые файлы не найдены."
        self.status.setText(status)

    def show_error(self, message: str):
        self.status.setText("Проверка завершилась с ошибкой. Подробности во вкладке «Предупреждения».")
        self.show_warnings([message])
        self.tabs.setCurrentWidget(self.warnings)

    def show_warnings(self, warnings: list[str]):
        self.warnings.clear()
        self.warnings.addItems(warnings)
        title = f"Предупреждения ({len(warnings)})" if warnings else "Предупреждения"
        self.tabs.setTabText(2, title)

    def analysis_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.set_controls_enabled(True)

    def set_controls_enabled(self, enabled: bool):
        for control in (self.mode, self.repository, self.folder, self.choose, self.check, self.refresh):
            control.setEnabled(enabled)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            return
        super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser(description="ImpactMap для GitHub и локальных Git-репозиториев")
    parser.add_argument("--analyzer", default=os.environ.get("IMPACTMAP_ANALYZER"), help="путь к Rust-анализатору")
    args = parser.parse_args()
    app = QApplication([])
    app.setApplicationName("ImpactMap")
    window = MainWindow(args.analyzer)
    window.show()
    QTimer.singleShot(0, window.refresh_github)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
