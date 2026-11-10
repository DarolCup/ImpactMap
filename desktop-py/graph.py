import math

from PySide6.QtCore import QPointF
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView


def display_path(path: str) -> str:
    return path.replace("\n", r"\n").replace("\r", r"\r").replace("\t", r"\t")


class DependencyGraph(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def show_result(self, response: dict) -> None:
        scene = self.scene()
        scene.clear()

        changed = set(response["changed_files"])
        distances = {item["path"]: item["distance"] for item in response["affected_files"]}
        distances.update(dict.fromkeys(changed, 0))
        edges = response["edges"]
        other_paths = {edge[end] for edge in edges for end in ("from", "to")} - distances.keys()
        distances.update(dict.fromkeys(other_paths, max(distances.values(), default=0) + 1))

        if not distances:
            scene.addSimpleText("Нет файлов для отображения.")
            scene.setSceneRect(scene.itemsBoundingRect())
            return

        metrics = QFontMetrics(self.font())
        labels = {path: display_path(path) for path in distances}
        width = max(150, max(metrics.horizontalAdvance(label) for label in labels.values()) + 24)
        columns = {distance: index for index, distance in enumerate(sorted(set(distances.values()), reverse=True))}
        rows = dict.fromkeys(columns, 0)
        positions = {}
        for path, distance in sorted(distances.items()):
            x = columns[distance] * (width + 112)
            y = rows[distance] * 72
            rows[distance] += 1
            positions[path] = QPointF(x, y + 17)
            border = QColor("#a16207" if path in changed else "#64748b")
            fill = QColor("#fef3c7" if path in changed else "#f1f5f9")
            box = scene.addRect(x, y, width, 34, QPen(border), QBrush(fill))
            box.setZValue(1)
            box.setToolTip(labels[path])
            label = scene.addSimpleText(labels[path], self.font())
            label.setBrush(QColor("#0f172a"))
            label.setPos(x + 12, y + (34 - label.boundingRect().height()) / 2)
            label.setZValue(2)

        pen = QPen(QColor("#64748b"), 1.5)
        for edge in edges:
            start = QPointF(positions[edge["from"]])
            end = QPointF(positions[edge["to"]])
            if start.x() == end.x():
                start += QPointF(width, 0)
                end += QPointF(width, 0)
                bend = QPointF(40, 0)
                control1, control2 = start + bend, end + bend
                if start == end:
                    start -= QPointF(0, 8)
                    end += QPointF(0, 8)
                    control1 -= QPointF(0, 24)
                    control2 += QPointF(0, 24)
            else:
                if start.x() < end.x():
                    start += QPointF(width, 0)
                else:
                    end += QPointF(width, 0)
                bend = QPointF((end.x() - start.x()) / 2, 0)
                control1, control2 = start + bend, end - bend

            curve = QPainterPath(start)
            curve.cubicTo(control1, control2, end)
            scene.addPath(curve, pen).setToolTip(edge["relation"])
            angle = math.atan2(end.y() - control2.y(), end.x() - control2.x())
            corners = [
                end - QPointF(10 * math.cos(angle + offset), 10 * math.sin(angle + offset))
                for offset in (-0.45, 0.45)
            ]
            scene.addPolygon(QPolygonF([end, *corners]), pen, QBrush(pen.color())).setZValue(3)

        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))
        self.ensureVisible(positions[min(changed or positions)].x(), 0, width, 34)
