# ぼかし指定画面のキャンバス (ver5 resolve2 §5.6.2 / §5.6.3 / ver5 resolve3 §5.5)
#
# フレームを表示し、その上に
#   ・検出枠・追加した枠 (ぼかす = 赤 / ぼかさない = 緑 / 主役 = 黄 / 対象外 = 灰)
#     輪郭や手描きの形があれば、その形で描く
#   ・選択中の枠 (太線 + 点線の外枠)
#   ・ドラッグで描いた自由曲線の囲み
#   ・仕上がり表示 (最終マスクを赤く半透明で重ねる / resolve3 §5.5.4)
# を重ねる。座標は**すべてキャンバス座標** (timeline.width x timeline.height) で扱い、
# 素材ピクセルとの変換は blur.geometry だけが知る (§4-4)。
#
# 操作 (resolve3 §5.5.2):
#   ドラッグ       囲みを描く (path_drawn)
#   クリック       枠を選ぶ。同じ位置を続けて押すと、重なった奥の枠へ順に移る (shape_selected)
#   ダブルクリック 枠の「ぼかす / ぼかさない」を入れ替える (shape_activated)
#   右クリック     枠のメニュー (context_requested)
#   Delete         選択中の枠を削除 (delete_requested)
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ...blur.geometry import point_in_polygon
from ...utils.logger import get_logger

_logger = get_logger(__name__)

# 枠の色 (ぼかす / ぼかさない / 主役 / 対象外)
_COLOR_BLUR = QColor(232, 80, 80)
_COLOR_KEEP = QColor(80, 200, 120)
_COLOR_MAIN = QColor(240, 200, 80)
_COLOR_UNUSED = QColor(160, 160, 160)
# 描画中の囲み
_COLOR_PATH = QColor(90, 170, 255)

# 囲みとみなす最小の点数 (これ未満の点はクリックとみなして捨てる)
_MIN_PATH_POINTS = 3
# 囲みとみなす最小の大きさ (キャンバス px)。これより小さい動きはクリックとして扱う
_MIN_PATH_EXTENT = 6.0
# 「同じ位置を続けてクリックした」とみなす距離 (キャンバス px)
_SAME_CLICK_DISTANCE = 8.0


class BlurCanvas(QGraphicsView):

    # 囲みを描き終えた (キャンバス座標の点列)
    path_drawn = Signal(list)
    # 枠が選ばれた (枠のキー / 空文字 = 選択解除)
    shape_selected = Signal(str)
    # 枠がダブルクリックされた (枠のキー)
    shape_activated = Signal(str)
    # 枠の上で右クリックされた (枠のキー, 画面座標)
    context_requested = Signal(str, QPoint)
    # Delete キーが押された
    delete_requested = Signal()

    def __init__(self, canvas_width, canvas_height, parent=None):
        super().__init__(parent)
        self._canvas_width = int(canvas_width)
        self._canvas_height = int(canvas_height)

        self._scene = QGraphicsScene(self)
        self._scene.setSceneRect(0, 0, self._canvas_width, self._canvas_height)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.StrongFocus)

        self._frame_item = QGraphicsPixmapItem()
        self._frame_item.setZValue(0)
        self._scene.addItem(self._frame_item)

        self._overlay_item = QGraphicsPixmapItem()
        self._overlay_item.setZValue(5)
        self._scene.addItem(self._overlay_item)

        self._shape_items = []      # 枠 (形 + ラベル)
        self._shapes = []           # [{"key","rect","polygon","role","main","excluded","label"}]
        self._selected_key = ""
        self._path_item = None      # 描画中の囲み
        self._points = []
        self._drawing = False
        self._last_click = None     # (点, そこで選んだ候補の番号)

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------

    # フレームを差し替える (rgb24 のバイト列)
    def set_frame(self, width, height, rgb_bytes):
        if not rgb_bytes:
            self._frame_item.setPixmap(QPixmap())
            return
        image = QImage(rgb_bytes, width, height, width * 3, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(image.copy())
        # フレームはキャンバス寸法へ合わせて表示する (以降の座標がキャンバス座標になる)
        if width != self._canvas_width or height != self._canvas_height:
            pixmap = pixmap.scaled(self._canvas_width, self._canvas_height,
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._frame_item.setPixmap(pixmap)
        # レターボックスの余白ぶんを中央へ寄せる
        self._frame_item.setOffset(
            (self._canvas_width - pixmap.width()) / 2.0,
            (self._canvas_height - pixmap.height()) / 2.0)

    # 「表示できません」の状態にする
    def clear_frame(self):
        self._frame_item.setPixmap(QPixmap())

    # 仕上がり表示の絵を重ねる (QImage / None で消す)。キャンバス全体へ引き伸ばす。
    def set_overlay(self, image):
        if image is None or image.isNull():
            self._overlay_item.setPixmap(QPixmap())
            return
        pixmap = QPixmap.fromImage(image).scaled(
            self._canvas_width, self._canvas_height, Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation)
        self._overlay_item.setPixmap(pixmap)

    # 枠を描き直す。
    # shapes: [{"key": 枠のキー, "rect": (x, y, w, h), "polygon": [(x, y), …] or None,
    #           "role": "blur"/"keep"/None, "main": 主役か, "excluded": 削除済みか,
    #           "label": 表示名}]
    def set_shapes(self, shapes, selected_key=""):
        for item in self._shape_items:
            self._scene.removeItem(item)
        self._shape_items = []
        self._shapes = list(shapes or [])
        self._selected_key = str(selected_key or "")

        pen_width = max(self._canvas_width / 400.0, 2.0)
        for entry in self._shapes:
            rect = entry.get("rect")
            if not rect:
                continue
            color = _color_of(entry)
            selected = entry.get("key") and entry.get("key") == self._selected_key

            polygon = entry.get("polygon")
            if polygon and len(polygon) >= 3:
                item = QGraphicsPolygonItem(QPolygonF([QPointF(x, y) for x, y in polygon]))
            else:
                item = QGraphicsRectItem(QRectF(rect[0], rect[1], rect[2], rect[3]))
            pen = QPen(color, pen_width * (2.0 if selected else 1.0))
            if entry.get("excluded") or entry.get("role") is None:
                pen.setStyle(Qt.DashLine)
            item.setPen(pen)
            item.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(),
                                        70 if selected else 40)))
            item.setZValue(12 if selected else 10)
            self._scene.addItem(item)
            self._shape_items.append(item)

            if selected:
                # 形の外側へ点線の外枠を出し、どれを選んでいるか分かるようにする
                margin = pen_width * 3
                frame = QGraphicsRectItem(QRectF(rect[0] - margin, rect[1] - margin,
                                                 rect[2] + margin * 2, rect[3] + margin * 2))
                frame.setPen(QPen(QColor(255, 255, 255), pen_width, Qt.DotLine))
                frame.setZValue(13)
                self._scene.addItem(frame)
                self._shape_items.append(frame)

            label = QGraphicsSimpleTextItem(str(entry.get("label") or ""))
            label.setBrush(QBrush(color))
            font = label.font()
            font.setPointSizeF(max(self._canvas_width / 90.0, 10.0))
            font.setBold(True)
            label.setFont(font)
            label.setPos(rect[0], max(rect[1] - font.pointSizeF() * 1.6, 0))
            label.setZValue(14)
            self._scene.addItem(label)
            self._shape_items.append(label)

    # 今描いている枠の一覧 (囲みの当て込みで使う)
    def shapes(self):
        return list(self._shapes)

    def selected_key(self):
        return self._selected_key

    # 描いた囲みを消す
    def clear_path(self):
        if self._path_item is not None:
            self._scene.removeItem(self._path_item)
            self._path_item = None
        self._points = []

    # 表示倍率を枠へ合わせる (ウィンドウの大きさが変わるたびに呼ぶ)
    def fit(self):
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event):       # noqa: N802 (Qt の命名に合わせる)
        super().resizeEvent(event)
        self.fit()

    # ------------------------------------------------------------------
    # マウス・キー操作
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):   # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.MouseFocusReason)
        self._drawing = True
        self.clear_path()
        point = self.mapToScene(event.position().toPoint())
        self._points = [(point.x(), point.y())]

        path = QPainterPath(QPointF(point))
        self._path_item = QGraphicsPathItem(path)
        self._path_item.setPen(QPen(_COLOR_PATH, max(self._canvas_width / 300.0, 2.0),
                                    Qt.DashLine))
        self._path_item.setBrush(QBrush(QColor(90, 170, 255, 50)))
        self._path_item.setZValue(20)
        self._scene.addItem(self._path_item)

    def mouseMoveEvent(self, event):    # noqa: N802
        if not self._drawing or self._path_item is None:
            super().mouseMoveEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        self._points.append((point.x(), point.y()))
        path = self._path_item.path()
        path.lineTo(QPointF(point))
        self._path_item.setPath(path)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if not self._drawing:
            super().mouseReleaseEvent(event)
            return
        self._drawing = False

        if len(self._points) < _MIN_PATH_POINTS or _extent(self._points) < _MIN_PATH_EXTENT:
            # 点を打っただけ = 枠のクリック (選択)
            point = self._points[0] if self._points else None
            self.clear_path()
            self._click(point)
            return

        # 離したら自動で閉じる (§5.6.3-2)
        path = self._path_item.path()
        path.closeSubpath()
        self._path_item.setPath(path)
        self.path_drawn.emit(list(self._points))

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        keys = self.keys_at((point.x(), point.y()))
        if keys:
            key = self._selected_key if self._selected_key in keys else keys[0]
            self.shape_activated.emit(key)

    def contextMenuEvent(self, event):  # noqa: N802
        point = self.mapToScene(event.pos())
        keys = self.keys_at((point.x(), point.y()))
        if not keys:
            return
        key = self._selected_key if self._selected_key in keys else keys[0]
        if key != self._selected_key:
            self.shape_selected.emit(key)
        self.context_requested.emit(key, event.globalPos())

    def keyPressEvent(self, event):     # noqa: N802
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_requested.emit()
            return
        super().keyPressEvent(event)

    # その点に重なっている枠のキー (小さい枠 = 手前にあるものから順)
    def keys_at(self, point):
        if not point:
            return []
        x, y = point
        hits = []
        for entry in self._shapes:
            rect = entry.get("rect")
            key = str(entry.get("key") or "")
            if not rect or not key:
                continue
            polygon = entry.get("polygon")
            if polygon and len(polygon) >= 3:
                inside = point_in_polygon((x, y), polygon)
            else:
                inside = rect[0] <= x <= rect[0] + rect[2] and rect[1] <= y <= rect[1] + rect[3]
            if inside:
                hits.append((float(rect[2]) * float(rect[3]), key))
        hits.sort(key=lambda item: item[0])
        return [key for _area, key in hits]

    # クリックで選ぶ。同じ位置を続けて押すと、重なった奥の枠へ順に移る (resolve3 §5.5.2)
    def _click(self, point):
        keys = self.keys_at(point)
        if not keys:
            self._last_click = None
            self.shape_selected.emit("")
            return
        index = 0
        if self._last_click is not None and point is not None:
            (last_x, last_y), last_index = self._last_click
            if abs(point[0] - last_x) + abs(point[1] - last_y) <= _SAME_CLICK_DISTANCE:
                index = (last_index + 1) % len(keys)
        self._last_click = (point, index)
        self.shape_selected.emit(keys[index])


# 枠の色 (役割で決める)
def _color_of(entry):
    if entry.get("excluded") or entry.get("role") is None:
        return _COLOR_UNUSED
    if entry.get("main") and entry.get("role") == "keep":
        return _COLOR_MAIN
    return _COLOR_BLUR if entry.get("role") == "blur" else _COLOR_KEEP


# 点列の外接矩形の大きい方の辺 (クリックと囲みの区別に使う)
def _extent(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return max(max(xs) - min(xs), max(ys) - min(ys)) if points else 0.0
