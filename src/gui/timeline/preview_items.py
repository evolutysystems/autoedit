# プレビュー上のオーバーレイ要素 (docs/request/ver3/resolve.md §6.5)
# 画像・字幕を QGraphicsItem として重ね、クリックで選択・ドラッグで位置変更できるようにする。
# シーン座標はキャンバス実寸 (例 1920x1080) で持つため、正規化座標 (-1〜1) と
# レンダリング側の座標計算が一致する。
#
# 字幕の描画は Qt による近似であり libass と完全一致しない (§6.4-4 に明記した仕様)。
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsSimpleTextItem,
)

# ASS テンキー配置 → 水平/垂直の寄せ (resolve_export と同じ対応)
_ALIGN_LEFT = (1, 4, 7)
_ALIGN_RIGHT = (3, 6, 9)
_ALIGN_BOTTOM = (1, 2, 3)
_ALIGN_TOP = (7, 8, 9)

# 大きさ変更ハンドルの既定値 (呼び出し側が設定を渡さなかった場合の保険 / resolve7 §7)
_DEFAULT_OVERLAY_CFG = {
    "min_scale": 0.02,
    "max_scale": 4.0,
    "resize_handle_px": 10,
}

# 選択枠の色 (琥珀)
# ver3 resolve3 §5.6: 映像の上に置く操作用の目印のため、アクセント (赤) へ寄せない。
# 赤にすると赤い映像の上で見失い、Timeline では再生ヘッドとも紛れる (§10-Q10)。
_SELECTION_COLOR = QColor(255, 214, 92)


# ASS 色文字列 (&HAABBGGRR) / HTML (#RRGGBB) を QColor へ変換する
def ass_color_to_qcolor(value, default="#FFFFFF"):
    text = str(value or "").strip()
    if text.startswith("#") and len(text) == 7:
        return QColor(text)
    if text.upper().startswith("&H"):
        digits = text[2:].rstrip("&")
        if len(digits) == 8:
            alpha = 255 - int(digits[0:2], 16)
            bb, gg, rr = digits[2:4], digits[4:6], digits[6:8]
            color = QColor(int(rr, 16), int(gg, 16), int(bb, 16))
            color.setAlpha(alpha)
            return color
        if len(digits) == 6:
            bb, gg, rr = digits[0:2], digits[2:4], digits[4:6]
            return QColor(int(rr, 16), int(gg, 16), int(bb, 16))
    return QColor(default)


# 字幕設定の配置(alignment)+余白から、位置未指定時の描画位置を求める
# 戻り値は (x_px, y_px, 水平アンカー, 垂直アンカー)。
def default_subtitle_anchor(eff_cfg, canvas_width, canvas_height):
    alignment = int(eff_cfg.get("alignment", 2) or 2)
    margin_l = int(eff_cfg.get("margin_l", 40) or 0)
    margin_r = int(eff_cfg.get("margin_r", 40) or 0)
    margin_v = int(eff_cfg.get("margin_v", 60) or 0)

    if alignment in _ALIGN_LEFT:
        x, h_anchor = float(margin_l), "left"
    elif alignment in _ALIGN_RIGHT:
        x, h_anchor = float(canvas_width - margin_r), "right"
    else:
        x, h_anchor = canvas_width / 2.0, "center"

    if alignment in _ALIGN_BOTTOM:
        y, v_anchor = float(canvas_height - margin_v), "bottom"
    elif alignment in _ALIGN_TOP:
        y, v_anchor = float(margin_v), "top"
    else:
        y, v_anchor = canvas_height / 2.0, "middle"
    return x, y, h_anchor, v_anchor


# 正規化座標 (-1〜1・Y は上が正) をキャンバスのピクセル座標へ
def normalized_to_pixel(x, y, canvas_width, canvas_height):
    return ((float(x) + 1.0) * canvas_width / 2.0,
            (1.0 - float(y)) * canvas_height / 2.0)


# キャンバスのピクセル座標を正規化座標へ
def pixel_to_normalized(px, py, canvas_width, canvas_height):
    return (px / (canvas_width / 2.0) - 1.0,
            1.0 - py / (canvas_height / 2.0))


# 素材のピクセル数どおりに置くための拡大率 (キャンバス幅に対する比率 / resolve7 §5.5)
# 求められないとき (素材が無い・寸法が取れない) は None を返す。
def native_scale(timeline, clip):
    media = timeline.media_by_id(getattr(clip, "media_id", "") or "")
    if media is None or not media.width or not timeline.width:
        return None
    return float(media.width) / float(timeline.width)


# 選択・ドラッグに対応するオーバーレイ要素の共通振る舞い
class _OverlayMixin:

    # 位置確定を伝える先 (PreviewPanel が設定する)
    move_finished = None

    def _init_overlay(self, clip):
        self.clip_id = clip.id
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setCursor(Qt.OpenHandCursor)

    # ドラッグ確定時に 1 回だけ通知する (ドラッグ中は積まない = 履歴を汚さない / §6.5-1)
    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if callable(self.move_finished):
            center = self.sceneBoundingRect().center()
            self.move_finished(self.clip_id, center.x(), center.y())

    # 選択枠を描く
    def _paint_selection(self, painter):
        if not self.isSelected():
            return
        painter.setPen(QPen(_SELECTION_COLOR, 2, Qt.DashLine))
        painter.setBrush(QBrush(Qt.NoBrush))
        painter.drawRect(self.boundingRect())


# 大きさ変更ハンドル (四隅 / resolve7 §5.3)
# ビューは fitInView でキャンバス全体を縮めて表示するため、ItemIgnoresTransformations を
# 付けて「画面上で常に同じ大きさ」に見えるようにする。
# 辺 (上下左右) のハンドルは出さない。出すと「縦だけ伸ばす」操作を見せてしまい、
# 縦横比固定という要望に反するため、四隅だけにすることが UI 上の担保になる (§3-1)。
class _ResizeHandleItem(QGraphicsRectItem):

    def __init__(self, owner, corner, size_px):
        half = float(size_px) / 2.0
        super().__init__(QRectF(-half, -half, float(size_px), float(size_px)), owner)
        self._owner = owner
        self._corner = corner
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setBrush(QBrush(_SELECTION_COLOR))
        self.setPen(QPen(QColor(40, 40, 40), 1))
        self.setCursor(Qt.SizeFDiagCursor if corner in ("tl", "br")
                       else Qt.SizeBDiagCursor)
        self.setZValue(1.0)

    # 押した瞬間に掴む (親の移動へ流さない = 掴んだ角でクリップごと動かさない)
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        self._owner.begin_resize()
        event.accept()

    def mouseMoveEvent(self, event):
        self._owner.update_resize(event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event):
        self._owner.finish_resize()
        event.accept()


# 画像・動画オーバーレイ (V2 以降のクリップ)
class ImageOverlayItem(_OverlayMixin, QGraphicsPixmapItem):

    # 大きさ確定を伝える先 (PreviewPanel が設定する / move_finished と同じ規約)
    resize_finished = None

    def __init__(self, clip, pixmap, canvas_size, parent=None, overlay_cfg=None):
        super().__init__(parent)
        self._init_overlay(clip)
        self._canvas_size = canvas_size
        cfg = dict(_DEFAULT_OVERLAY_CFG)
        cfg.update(overlay_cfg or {})
        self._min_scale = float(cfg["min_scale"])
        self._max_scale = float(cfg["max_scale"])
        # 素材の原寸 (縦横比の計算に使う。拡縮後の pixmap からは取れないため先に控える)
        self._native_width = max(pixmap.width(), 1)
        self._native_height = max(pixmap.height(), 1)
        # この絵を描いたときの拡大率。ドラッグ中の setScale はこれとの比で与える。
        self._base_scale = max(float(clip.transform.scale or 1.0), 1e-6)
        self._pending_scale = self._base_scale
        self._resizable = True

        target_width = max(int(canvas_size[0] * self._base_scale), 2)
        if not pixmap.isNull():
            scaled = pixmap.scaledToWidth(target_width, Qt.SmoothTransformation)
            self.setPixmap(scaled)
        self.setOpacity(float(clip.transform.opacity or 1.0))
        # transform.x/y はクリップ中心を指す
        cx, cy = normalized_to_pixel(
            clip.transform.x or 0.0, clip.transform.y or 0.0, *canvas_size)
        rect = self.boundingRect()
        self.setPos(cx - rect.width() / 2.0, cy - rect.height() / 2.0)
        # 中心を基準に拡縮する (大きさを変えても位置が動かない / resolve7 §3-2)
        self.setTransformOriginPoint(rect.center())

        self._handles = [
            _ResizeHandleItem(self, corner, int(cfg["resize_handle_px"]))
            for corner in ("tl", "tr", "bl", "br")
        ]
        self._layout_handles()
        self._sync_handles()

    # ハンドルを四隅へ置く (子アイテムの座標は親のローカル座標。親の setScale で追従する)
    def _layout_handles(self):
        rect = self.boundingRect()
        corners = {
            "tl": (rect.left(), rect.top()),
            "tr": (rect.right(), rect.top()),
            "bl": (rect.left(), rect.bottom()),
            "br": (rect.right(), rect.bottom()),
        }
        for handle in self._handles:
            x, y = corners[handle._corner]
            handle.setPos(x, y)

    # ハンドルの表示条件: 選択中 かつ 掴める状態 (再生中は掴ませない)
    def _sync_handles(self):
        visible = bool(self.isSelected()) and self._resizable
        for handle in getattr(self, "_handles", []):
            handle.setVisible(visible)

    # 再生中など、大きさを変えさせない状態を切り替える
    def set_resizable(self, resizable):
        self._resizable = bool(resizable)
        self._sync_handles()

    # 選択が変わったらハンドルの表示を合わせる (選択中のときだけ出す)
    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self._sync_handles()
        return result

    # ------------------------------------------------------------------
    # 大きさ変更 (ハンドルから呼ばれる / resolve7 §3-3)
    # ------------------------------------------------------------------

    def begin_resize(self):
        self._pending_scale = self._base_scale

    # ドラッグ中は setScale で見た目だけ追従させる。
    # QPixmap.scaledToWidth は綺麗だがコストが高く、毎フレーム実行してはならない (§3-3)。
    def update_resize(self, scene_pos):
        scale = self._scale_from_cursor(scene_pos)
        self._pending_scale = min(max(scale, self._min_scale), self._max_scale)
        self.setScale(self._pending_scale / self._base_scale)

    # 確定時にコマンドを 1 回だけ積む (ドラッグ中は積まない = 履歴を汚さない)
    def finish_resize(self):
        if callable(self.resize_finished):
            self.resize_finished(self.clip_id, self._pending_scale)

    # 中心を固定したまま、カーソル位置から新しい拡大率を求める (resolve7 §3-2)
    #   カーソルが角へ吸い付くよう、横・縦それぞれで必要な拡大率の大きい方を採る。
    #   縦横比は「幅から高さを決める」ため常に保たれる。
    def _scale_from_cursor(self, scene_pos):
        canvas_w = float(self._canvas_size[0])
        center = self.sceneBoundingRect().center()
        dx = abs(scene_pos.x() - center.x())
        dy = abs(scene_pos.y() - center.y())
        ratio = float(self._native_height) / float(self._native_width)
        scale_x = 2.0 * dx / max(canvas_w, 1.0)
        scale_y = 2.0 * dy / max(canvas_w * ratio, 1.0)
        return max(scale_x, scale_y)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        self._paint_selection(painter)


# 字幕オーバーレイ (Qt による近似描画 / §6.4-4)
class SubtitleOverlayItem(_OverlayMixin, QGraphicsSimpleTextItem):

    def __init__(self, clip, eff_cfg, font_profile, canvas_size, parent=None):
        # ASS の改行 \N を実改行へ戻して表示する
        text = str(clip.text or "").replace("\\N", "\n")
        role = str(clip.role or "streamer")
        if role == "comment" and font_profile.comment_label:
            text = f"{font_profile.comment_label}\n{text}"
        super().__init__(text, parent)
        self._init_overlay(clip)
        self._canvas_size = canvas_size

        font = QFont(clip.font or font_profile.family)
        # ASS の Fontsize は PlayRes 単位 = キャンバスのピクセル
        font.setPixelSize(int(clip.font_size or font_profile.size or 48))
        font.setBold(bool(font_profile.bold))
        font.setItalic(bool(font_profile.italic))
        font.setUnderline(bool(font_profile.underline))
        self.setFont(font)

        # クリップ個別の色があれば優先する (resolve6 §3-4)。空文字なら役割の色に従う。
        fill = (clip.color
                or font_profile.role_colors.get(role, font_profile.color_hex))
        self.setBrush(QBrush(ass_color_to_qcolor(fill)))
        outline = (clip.outline_color
                   or font_profile.role_outline_colors.get(
                       role, font_profile.outline_color))
        width = max(int(font_profile.outline_width or 0), 0)
        if width > 0:
            pen = QPen(ass_color_to_qcolor(outline, "#000000"))
            pen.setWidth(width)
            pen.setJoinStyle(Qt.RoundJoin)
            self.setPen(pen)
        else:
            self.setPen(QPen(Qt.NoPen))

        self._place(clip, eff_cfg, canvas_size)

    # 位置指定があればその中心へ、無ければ設定の配置(alignment)+余白へ置く
    def _place(self, clip, eff_cfg, canvas_size):
        rect = self.boundingRect()
        if clip.transform.is_positioned():
            cx, cy = normalized_to_pixel(clip.transform.x, clip.transform.y, *canvas_size)
            self.setPos(cx - rect.width() / 2.0, cy - rect.height() / 2.0)
            return
        x, y, h_anchor, v_anchor = default_subtitle_anchor(eff_cfg, *canvas_size)
        if h_anchor == "left":
            left = x
        elif h_anchor == "right":
            left = x - rect.width()
        else:
            left = x - rect.width() / 2.0
        if v_anchor == "top":
            top = y
        elif v_anchor == "bottom":
            top = y - rect.height()
        else:
            top = y - rect.height() / 2.0
        self.setPos(left, top)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        self._paint_selection(painter)


# ベース映像フレームのアイテム (選択・移動は不可)
class BaseFrameItem(QGraphicsPixmapItem):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setZValue(-1000)
        self.setFlag(QGraphicsItem.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.ItemIsMovable, False)

    # 空フレーム (取得できないとき) は黒で塗る
    # 直前に縮小フレームを表示していた場合に備え、拡縮と位置も戻す (resolve6 §5.8)
    def show_blank(self, canvas_size):
        pixmap = QPixmap(int(canvas_size[0]), int(canvas_size[1]))
        pixmap.fill(QColor(0, 0, 0))
        self.setPixmap(pixmap)
        self.setScale(1.0)
        self.setPos(0.0, 0.0)

    def bounding_canvas(self, canvas_size):
        return QRectF(0, 0, float(canvas_size[0]), float(canvas_size[1]))
