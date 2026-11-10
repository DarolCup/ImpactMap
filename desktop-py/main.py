import argparse
import os
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
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
from graph import DependencyGraph, display_path


class AnalysisThread(QThread):
    changes_found = Signal(object)
    result_ready = Signal(object, bool)
    failed = Signal(str)

    def __init__(self, folder: Path, analyzer: str | None, parent: QWidget):
        super().__init__(parent)
        self.folder = folder
        self.analyzer = analyzer

    def run(self):
        try:
            repo, changes = repository_changes(self.folder)
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
        self.setWindowTitle("ImpactMap")
        self.resize(1050, 650)

        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Путь к локальному Git-репозиторию")
        self.folder.setAccessibleName("Папка репозитория")
        self.folder.returnPressed.connect(self.check_changes)
        self.choose = QPushButton("Выбрать папку…")
        self.choose.clicked.connect(self.choose_folder)
        self.check = QPushButton("Проверить изменения")
        self.check.clicked.connect(self.check_changes)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.folder, 1)
        toolbar.addWidget(self.choose)
        toolbar.addWidget(self.check)

        self.changed = QListWidget()
        self.changed.setAccessibleName("Изменённые файлы относительно HEAD")
        self.changed_title = QLabel("Изменённые файлы относительно HEAD")
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
        self.status = QLabel("Выберите папку репозитория, чтобы начать.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addLayout(toolbar)
        layout.addWidget(content, 1)
        layout.addWidget(self.status)
        self.setCentralWidget(page)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку Git-репозитория", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def check_changes(self):
        if self.worker is not None:
            return
        folder = self.folder.text()
        if not folder:
            self.status.setText("Укажите папку Git-репозитория.")
            return
        self.changed.clear()
        self.changed_title.setText("Изменённые файлы относительно HEAD")
        self.graph.scene().clear()
        self.affected.clear()
        self.warnings.clear()
        self.tabs.setTabText(1, "Затронутые файлы")
        self.tabs.setTabText(2, "Предупреждения")
        self.tabs.setCurrentIndex(0)
        self.status.setText("Проверяю изменения…")
        for control in (self.folder, self.choose, self.check):
            control.setEnabled(False)

        self.worker = AnalysisThread(Path(os.path.expanduser(folder)), self.analyzer, self)
        self.worker.changes_found.connect(self.show_changes)
        self.worker.result_ready.connect(self.show_result)
        self.worker.failed.connect(self.show_error)
        self.worker.finished.connect(self.analysis_finished)
        self.worker.start()

    def show_changes(self, changes: list[str]):
        self.changed.addItems([display_path(path) for path in changes])
        self.changed_title.setText(f"Изменённые файлы относительно HEAD ({len(changes)})")

    def show_result(self, response: dict, example: bool):
        self.graph.show_result(response)
        affected = response["affected_files"]
        for item in affected:
            self.affected.addTopLevelItem(
                QTreeWidgetItem([display_path(item["path"]), str(item["distance"]), item["reason"]])
            )
        warnings = response["warnings"]
        self.warnings.addItems(warnings)
        self.tabs.setTabText(1, f"Затронутые файлы ({len(affected)})")
        self.tabs.setTabText(2, f"Предупреждения ({len(warnings)})")
        self.status.setText(
            "Анализатор не найден. Во вкладках показан пример JSON, слева — изменения репозитория."
            if example else "Проверка завершена."
        )

    def show_error(self, message: str):
        self.status.setText("Проверка завершилась с ошибкой. Подробности во вкладке «Предупреждения».")
        self.warnings.addItem(message)
        self.tabs.setTabText(2, "Предупреждения (1)")
        self.tabs.setCurrentWidget(self.warnings)

    def analysis_finished(self):
        self.worker.deleteLater()
        self.worker = None
        for control in (self.folder, self.choose, self.check):
            control.setEnabled(True)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            return
        super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser(description="ImpactMap для локальных Git-репозиториев")
    parser.add_argument("--analyzer", default=os.environ.get("IMPACTMAP_ANALYZER"), help="путь к Rust-анализатору")
    args = parser.parse_args()
    app = QApplication([])
    window = MainWindow(args.analyzer)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
