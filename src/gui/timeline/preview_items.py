# プレビュー上のオーバーレイ要素 (docs/request/ver3/resolve.md §6.5)
# 画像・字幕を QGraphicsItem として重ね、クリックで選択・ドラッグで位置変更できるようにする。
# シーン座標はキャンバス実寸 (例 1920x1080) で持つため、正規化座標 (-1〜1) と
# レンダリング側の座標計算が一致する。
#
# 字幕の描画は Qt による近似であり libass と完全一致しない (§6.4-4 に明記した仕様)。
# ただし ver6 resolve3 §4.7 で「影」「アウトラインの太さ」「行送り」の 3 点を
# ASS (libass) 側へ寄せた。グリフの形そのもの (ヒンティング・字間) は依然として近似。
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsSimpleTextItem,
)

from ...modules import comment_decor, subtitle_generator

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

# コメント役割 (背景とアイコンを付ける対象 / ver3 resolve11)
_ROLE_COMMENT = "comment"

# コメントアイコンの読み込み結果を使い回すためのキャッシュ (キー: (パス, 一辺px))。
# 再生中は毎フレーム作り直されるため、その都度ディスクから読むと引っかかる。
_icon_cache = {}


# 指定サイズのコメントアイコンを返す (読めなければ None)
def _comment_icon_pixmap(eff_cfg, size):
    path = comment_decor.resolve_icon_path(eff_cfg)
    if not path:
        return None
    key = (path, int(size))
    pixmap = _icon_cache.get(key)
    if pixmap is None:
        source = QPixmap(path)
        if source.isNull():
            return None
        pixmap = source.scaled(int(size), int(size),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        _icon_cache[key] = pixmap
    return pixmap


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
# role にコメントを渡すと役割別の配置 (comment_alignment / comment_margin_*) を使う
# (ver3 resolve11 §5.7)。キーが無ければ共通値へ落ちるため旧設定でも動く。
def default_subtitle_anchor(eff_cfg, canvas_width, canvas_height, role=""):
    prefix = "comment_" if str(role or "") == _ROLE_COMMENT else ""

    # 役割別のキー (comment_*) を優先し、無ければ共通のキーへ落とす
    def value(key, default):
        if prefix and eff_cfg.get(prefix + key) is not None:
            return eff_cfg.get(prefix + key)
        return eff_cfg.get(key, default)

    alignment = int(value("alignment", 2) or 2)
    margin_l = int(value("margin_l", 40) or 0)
    margin_r = int(value("margin_r", 40) or 0)
    margin_v = int(value("margin_v", 60) or 0)

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
        # 下書き (近似描画) を描くか (ver6 resolve3 §4.4)。
        # プロキシの絵が出ているときは False にする。中身は二重描画になるため描かず、
        # **選択枠とハンドル・当たり判定だけを残す**。こうすると位置の変更は
        # 今までどおり行えて、見えている絵は出力と一致したプロキシのものになる。
        self._draft_visible = True

    # 下書きを描くかを切り替える (プロキシの絵が在るときは False)
    def set_draft_visible(self, visible):
        visible = bool(visible)
        if visible == getattr(self, "_draft_visible", True):
            return
        self._draft_visible = visible
        self.update()

    def draft_visible(self):
        return bool(getattr(self, "_draft_visible", True))

    # 位置として保存する基準点 (シーン座標)
    # 既定は外接矩形の中心。コメント字幕だけ「文字の左中央」を返す (ver3 resolve11 §3 D-7)。
    # 背景・アイコンを子アイテムとして持つと sceneBoundingRect() にそれらが混ざるため、
    # ここを切り出して「何を基準に保存するか」をアイテム側が決められるようにする。
    def anchor_scene_point(self):
        return self.sceneBoundingRect().center()

    # ドラッグ確定時に 1 回だけ通知する (ドラッグ中は積まない = 履歴を汚さない / §6.5-1)
    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if callable(self.move_finished):
            point = self.anchor_scene_point()
            self.move_finished(self.clip_id, point.x(), point.y())

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
        if self.draft_visible():
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
        pixel_size = int(clip.font_size or font_profile.size or 48)
        font.setPixelSize(pixel_size)
        font.setBold(bool(font_profile.bold))
        font.setItalic(bool(font_profile.italic))
        font.setUnderline(bool(font_profile.underline))
        self.setFont(font)
        # 位置の基準 (anchor_scene_point) を役割で切り替えるため覚えておく
        self._role = role

        # クリップ個別の色があれば優先する (resolve6 §3-4)。空文字なら役割の色に従う。
        fill = (clip.color
                or font_profile.role_colors.get(role, font_profile.color_hex))
        self.setBrush(QBrush(ass_color_to_qcolor(fill)))
        outline = (clip.outline_color
                   or font_profile.role_outline_colors.get(
                       role, font_profile.outline_color))
        # アウトラインと影は paint() で自前に描く (ver6 resolve3 §4.7-1 / §4.7-2)。
        # Qt の pen は中心線ストロークのため、ASS の「字の外側に W px」より
        # 細く・字が痩せて見えていた。pen は使わない。
        self._outline_width = max(int(font_profile.outline_width or 0), 0)
        self._outline_color = ass_color_to_qcolor(outline, "#000000")
        self._shadow_px = max(float(subtitle_generator.ass_shadow_px()), 0.0)
        self._shadow_color = ass_color_to_qcolor(font_profile.back_color, "#000000")
        self.setPen(QPen(Qt.NoPen))

        # 行送りは ASS 側と同じ値 (comment_bg_line_height_ratio) で決める (§4.7-3)。
        # コメント背景の箱の高さも comment_decor.text_extent が同じ式で出すため、
        # 箱と文字が食い違わない。
        self._lines = str(self.text() or "").split("\n")
        self._line_height = pixel_size * _line_height_ratio(eff_cfg)
        metrics = QFontMetricsF(font)
        self._line_widths = [metrics.horizontalAdvance(line) for line in self._lines]
        self._text_width = max(self._line_widths) if self._line_widths else 0.0
        self._text_height = len(self._lines) * self._line_height
        # 行ごとの寄せ。libass は \an の水平成分に従って各行を寄せる。
        self._line_align = _line_alignment(clip, eff_cfg, canvas_size, role)
        self._text_path_cache = None

        self._place(clip, eff_cfg, canvas_size)
        # コメントは背景 (角丸の箱) とアイコンを子アイテムとして持たせる
        # (ver3 resolve11 §5.11-6)。親を動かせば一緒に付いてくる。
        if role == _ROLE_COMMENT:
            self._add_comment_decor(clip, eff_cfg, canvas_size, text)

    # 位置として保存する基準点 (ver3 resolve11 §3 D-4 / D-7)
    # コメントは \an4 (左中央) を基準に位置を持つため、文字の矩形の左中央を返す。
    # boundingRect() は子アイテム (背景・アイコン) を含まないため、装飾を足しても基準は動かない。
    def anchor_scene_point(self):
        # 影とアウトラインのぶん外へ広い boundingRect ではなく文字の矩形を使う
        # (ver6 resolve3 §4.7)。基準が変わると保存される位置がずれるため。
        rect = self.text_rect()
        if self._role == _ROLE_COMMENT:
            return self.mapToScene(rect.left(), rect.center().y())
        return self.mapToScene(rect.center())

    # 文字そのものの外接矩形 (ASS の行送りで積んだ推定値 / ver6 resolve3 §4.7-3)
    # 位置決め・選択枠・コメント装飾の基準はすべてこれを使う。
    def text_rect(self):
        return QRectF(0.0, 0.0, float(self._text_width), float(self._text_height))

    # 描画範囲は影とアウトラインのぶんだけ外へ広い (はみ出すと描画が欠ける)
    def boundingRect(self):
        margin = float(self._outline_width) + float(self._shadow_px) + 1.0
        return self.text_rect().adjusted(-margin, -margin, margin, margin)

    # 掴める範囲は文字の矩形とする (行間の隙間でも掴めるほうが操作しやすい)
    def shape(self):
        path = QPainterPath()
        path.addRect(self.text_rect())
        return path

    # 行ごとに ASS と同じ行送りで積んだ文字のパス (paint / 当たり判定の素)
    # 文字と font は生成後に変わらないため 1 回だけ作って使い回す。
    def _text_path(self):
        if self._text_path_cache is not None:
            return self._text_path_cache
        font = self.font()
        metrics = QFontMetricsF(font)
        path = QPainterPath()
        # 行ボックスの中央へ字を置く (行高 - 字の高さ) / 2 を上の余白とする
        inner = (self._line_height - (metrics.ascent() + metrics.descent())) / 2.0
        for index, line in enumerate(self._lines):
            if not line:
                continue
            width = self._line_widths[index]
            if self._line_align == "right":
                left = self._text_width - width
            elif self._line_align == "center":
                left = (self._text_width - width) / 2.0
            else:
                left = 0.0
            baseline = index * self._line_height + inner + metrics.ascent()
            path.addText(left, baseline, font, line)
        self._text_path_cache = path
        return path

    # 選択枠は文字の矩形に合わせる (boundingRect は影と縁のぶん外へ広い)
    def _paint_selection(self, painter):
        if not self.isSelected():
            return
        painter.setPen(QPen(_SELECTION_COLOR, 2, Qt.DashLine))
        painter.setBrush(QBrush(Qt.NoBrush))
        painter.drawRect(self.text_rect())

    # 背景 (角丸の箱) とアイコンを子アイテムとして足す (ver3 resolve11 §5.11-6)
    # 大きさは comment_decor の推定値をそのまま使う。Qt の実測 (QFontMetrics) は使わない:
    # 実測すると焼き込み (推定) と別の箱になり、プレビューと出力がズレるため (§3 D-10)。
    def _add_comment_decor(self, clip, eff_cfg, canvas_size, text):
        item = clip.to_item()
        # 子アイテムの座標は親 (文字) の左上が原点。キャンバス座標との差を引いて置く。
        origin = self.scenePos()

        box = comment_decor.background_box(
            item, eff_cfg, canvas_size[0], canvas_size[1], text=text)
        if box:
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(box["x"] - origin.x(), box["y"] - origin.y(),
                       box["w"], box["h"]),
                float(box["radius"]), float(box["radius"]))
            background = QGraphicsPathItem(path, self)
            background.setBrush(QBrush(ass_color_to_qcolor(
                eff_cfg.get("comment_bg_color"), "#000000")))
            background.setPen(QPen(Qt.NoPen))
            # 負の Z を持つ子は親より先に (= 下に) 描かれる
            background.setZValue(-1)
            background.setFlag(QGraphicsItem.ItemIsSelectable, False)
            background.setFlag(QGraphicsItem.ItemIsMovable, False)

        icon = comment_decor.icon_box(
            item, eff_cfg, canvas_size[0], canvas_size[1])
        if not icon:
            return
        pixmap = _comment_icon_pixmap(eff_cfg, icon["size"])
        if pixmap is None or pixmap.isNull():
            return
        item_icon = QGraphicsPixmapItem(pixmap, self)
        item_icon.setOffset(icon["x"] - origin.x(), icon["y"] - origin.y())
        item_icon.setZValue(1)
        item_icon.setFlag(QGraphicsItem.ItemIsSelectable, False)
        item_icon.setFlag(QGraphicsItem.ItemIsMovable, False)

    # 位置指定があればその中心へ、無ければ設定の配置(alignment)+余白へ置く
    # コメントは中心ではなく「文字の左中央」を基準に置く (ver3 resolve11 §3 D-4)。
    def _place(self, clip, eff_cfg, canvas_size):
        rect = self.text_rect()
        is_comment = str(clip.role or "") == _ROLE_COMMENT
        if clip.transform.is_positioned():
            cx, cy = normalized_to_pixel(clip.transform.x, clip.transform.y, *canvas_size)
            left = cx if is_comment else cx - rect.width() / 2.0
            self.setPos(left, cy - rect.height() / 2.0)
            return
        x, y, h_anchor, v_anchor = default_subtitle_anchor(
            eff_cfg, *canvas_size, role=clip.role)
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

    # ASS (libass) の見え方へ寄せて描く (ver6 resolve3 §4.7-1 / §4.7-2)
    #   影 : 字 + アウトラインの形を右下へ ASS Shadow ぶんずらして置く
    #   縁 : 幅 2W のストロークを字の**下**へ敷く = 字の外側に W px 出る
    #   字 : 最後に塗りを乗せる (縁が字の内側を削らない)
    # 基底クラス (QGraphicsSimpleTextItem) の描画は使わない。
    def paint(self, painter, option, widget=None):
        if not self.draft_visible():
            self._paint_selection(painter)
            return
        path = self._text_path()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)

        stroke = None
        if self._outline_width > 0:
            stroker = QPainterPathStroker()
            # ASS の Outline=W は「字の外側に W px」。中心線ストロークで同じ太さを
            # 出すには幅 2W にして、内側へ出たぶんを字の塗りで覆う。
            stroker.setWidth(float(self._outline_width) * 2.0)
            stroker.setJoinStyle(Qt.RoundJoin)
            stroker.setCapStyle(Qt.RoundCap)
            stroke = stroker.createStroke(path).united(path)

        if self._shadow_px > 0:
            silhouette = stroke if stroke is not None else path
            painter.translate(self._shadow_px, self._shadow_px)
            painter.fillPath(silhouette, QBrush(self._shadow_color))
            painter.translate(-self._shadow_px, -self._shadow_px)

        if stroke is not None:
            painter.fillPath(stroke, QBrush(self._outline_color))
        painter.fillPath(path, self.brush())
        painter.restore()
        self._paint_selection(painter)


# 行送りの比 (ASS 側と同じ値を使う / ver6 resolve3 §4.7-3)
def _line_height_ratio(eff_cfg):
    try:
        ratio = float((eff_cfg or {}).get("comment_bg_line_height_ratio") or 1.2)
    except (TypeError, ValueError):
        ratio = 1.2
    return ratio if ratio > 0 else 1.2


# 行ごとの水平の寄せを決める (ver6 resolve3 §4.7-3)
#   位置指定あり: コメントは \an4 (左) / それ以外は \an5 (中央) で焼かれる
#                 (subtitle_generator の \an 決定と同じ規約)
#   位置指定なし: Style の alignment に従う
def _line_alignment(clip, eff_cfg, canvas_size, role):
    if clip.transform.is_positioned():
        return "left" if str(role or "") == _ROLE_COMMENT else "center"
    _x, _y, h_anchor, _v_anchor = default_subtitle_anchor(
        eff_cfg, canvas_size[0], canvas_size[1], role=role)
    return h_anchor


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
