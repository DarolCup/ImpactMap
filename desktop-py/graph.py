import math
from collections import defaultdict

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from result import ResultModel, display_path


WIDTH, HEIGHT = 194, 55
STEP_X, STEP_Y = 300, 84


class DependencyGraph(QGraphicsView):
    selected = Signal(str)

    def __init__(self, colors: dict):
        super().__init__()
        self.colors = colors
        self.model = None
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(colors["canvas"]))
        self.setAccessibleName("Карта файлов")
        self.nodes = {}
        self.arrows = {}
        self.pressed_at = None
        self.full = False

    def zoom(self, factor: float):
        current = self.transform().m11()
        target = min(3.0, max(0.08, current * factor))
        if current < 0.08:
            target = min(3.0, current * max(1.0, factor))
        self.scale(target / current, target / current)

    def fit(self):
        self.resetTransform()
        if self.nodes:
            rect = self.sceneRect()
            scale = min(1.0, (self.viewport().width() - 4) / rect.width(),
                        (self.viewport().height() - 4) / rect.height())
            if scale > 0:
                self.scale(scale, scale)
            self.centerOn(rect.center())

    def wheelEvent(self, event):
        self.zoom(1.18 ** (event.angleDelta().y() / 120))
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.pressed_at = event.position()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and self.pressed_at is not None:
            if (event.position() - self.pressed_at).manhattanLength() < 5:
                for item in self.items(event.position().toPoint()):
                    if (path := item.data(0)) is not None:
                        self.selected.emit(path)
                        break
            self.pressed_at = None

    def show_result(self, model: ResultModel | None, full: bool = False, only: str | None = None):
        self.model = model
        self.full = full
        scene = self.scene()
        scene.clear()
        self.nodes.clear()
        self.arrows.clear()
        if model is None:
            return
        paths = model.paths if full else model.focus_paths
        edges = model.edges | model.focus_edges if full else model.focus_edges
        if only:
            paths, edges = model.selection(only, full)
        if not paths:
            label = scene.addSimpleText("Связанных файлов не найдено")
            label.setBrush(QColor(self.colors["muted"]))
            scene.setSceneRect(label.boundingRect())
            return

        columns = defaultdict(list)
        levels = sorted({model.distances[path] for path in paths if path in model.distances}, reverse=True)
        level_columns = {level: index for index, level in enumerate(levels)}
        other = sorted(paths - model.distances.keys())
        # Keep unrelated files in a compact grid at the right of the impact chains.
        for index, path in enumerate(other):
            columns[len(levels) + index // 10].append(path)
        for path in sorted(paths & model.distances.keys()):
            columns[level_columns[model.distances[path]]].append(path)

        adjacency = defaultdict(set)
        for source, target in edges:
            adjacency[source].add(target)
            adjacency[target].add(source)
        rows = {path: row for column in columns.values() for row, path in enumerate(column)}
        # Put files with common neighbours near one another before routing arrows.
        for order in (sorted(columns, reverse=True), sorted(columns)):
            for column in order:
                def position(path):
                    neighbours = adjacency[path]
                    return sum(rows[node] for node in neighbours) / len(neighbours) if neighbours else rows[path]
                columns[column].sort(key=lambda path: (position(path), path))
                rows.update({path: row for row, path in enumerate(columns[column])})

        points = {}
        font = self.font()
        font.setBold(True)
        role_font = self.font()
        role_font.setPointSizeF(max(8, role_font.pointSizeF() - 1))
        for col, column in columns.items():
            for row, path in enumerate(column):
                x, y = col * STEP_X, (row - (len(column) - 1) / 2) * STEP_Y
                points[path] = QPointF(x, y + HEIGHT / 2)
                shape = QPainterPath()
                shape.addRoundedRect(x, y, WIDTH, HEIGHT, 9, 9)
                box = scene.addPath(shape)
                box.setZValue(2)
                box.setData(0, path)
                box.setToolTip(display_path(path))
                box.setCursor(Qt.CursorShape.PointingHandCursor)
                label = QFontMetrics(font).elidedText(display_path(path), Qt.TextElideMode.ElideMiddle, WIDTH - 24)
                name = scene.addSimpleText(label, font)
                role = scene.addSimpleText(model.role(path), role_font)
                for item, offset in ((name, 7), (role, 30)):
                    item.setParentItem(box)
                    item.setPos(x + 12, y + offset)
                self.nodes[path] = (box, name, role)

        for source, target in sorted(edges):
            if source in points and target in points and source != target:
                self.add_arrow(source, target, points)
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-36, -35, 36, 35))
        self.highlight(None)
        self.fit()

    def add_arrow(self, source: str, target: str, points: dict):
        start, end = QPointF(points[source]), QPointF(points[target])
        if start.x() == end.x():
            start += QPointF(WIDTH, 0)
            end += QPointF(WIDTH, 0)
            control1, control2 = start + QPointF(47, 0), end + QPointF(47, 0)
        else:
            if start.x() < end.x():
                start += QPointF(WIDTH, 0)
            else:
                end += QPointF(WIDTH, 0)
            bend = QPointF((end.x() - start.x()) * 0.5, 0)
            control1, control2 = start + bend, end - bend
        curve = QPainterPath(start)
        curve.cubicTo(control1, control2, end)
        angle = math.atan2(end.y() - control2.y(), end.x() - control2.x())
        corners = [end - QPointF(11 * math.cos(angle + offset), 11 * math.sin(angle + offset))
                   for offset in (-0.44, 0.44)]
        line = self.scene().addPath(curve)
        head = self.scene().addPolygon(QPolygonF([end, *corners]))
        line.setToolTip(f"{display_path(source)} использует {display_path(target)}")
        self.arrows[source, target] = (line, head)

    def highlight(self, path: str | None, *, center: bool = False):
        if self.model is None:
            return
        active, edges = self.model.selection(path, self.full) if path else (set(), set())
        for node, (box, name, role) in self.nodes.items():
            if node in self.model.changed:
                tint = "changed"
            elif node in self.model.affected:
                tint = "affected"
            else:
                tint = "neutral"
            border = QColor(self.colors["accent"] if node == path else self.colors[tint])
            box.setPen(QPen(border, 3 if node == path else 1.3))
            box.setBrush(QColor(self.colors[tint + "_bg"]))
            box.setOpacity(1 if path is None or node in active else 0.48)
            name.setBrush(QColor(self.colors["text"]))
            role.setBrush(QColor(self.colors[tint]))
            role.setText(self.model.role(node) + (" · выбран" if node == path else ""))
        for edge, (line, head) in self.arrows.items():
            emphasized = edge in edges
            color = QColor(self.colors["accent"] if emphasized else self.colors["edge"])
            color.setAlpha(255 if emphasized or path is None else 34)
            line.setPen(QPen(color, 2.8 if emphasized else 1.5))
            head.setPen(QPen(Qt.PenStyle.NoPen))
            head.setBrush(QBrush(color))
            line.setZValue(1 if emphasized else 0)
            head.setZValue(1 if emphasized else 0)
        if center and path in self.nodes:
            if self.transform().m11() < 0.72:
                self.resetTransform()
                self.scale(0.82, 0.82)
            self.ensureVisible(self.nodes[path][0], 28, 25)
