# 採点グラフウィジェット (flow17 R2 / resolve17 §4.7.1)
# 縦=スコア / 横=動画時間 の折れ線 (窓スコア列) に、TOP5 区間をマーカ表示する。
# マーカ/折れ線のクリックで最寄りクリップを選び clip_selected(index) を emit し、
# 結果画面の下段 (字幕編集・プレビュー) を同期させる。
from PySide6.QtCharts import (
    QAreaSeries,
    QChart,
    QChartView,
    QLineSeries,
    QScatterSeries,
    QValueAxis,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QVBoxLayout, QWidget

from . import theme


# 窓スコア (curve 要素) の中央時刻を返す
def _mid(entry):
    return (float(entry.get("start", 0.0)) + float(entry.get("end", 0.0))) / 2.0


class ScoreGraphWidget(QWidget):

    # クリップ選択シグナル (clip["index"] を渡す)
    clip_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clip_points = []  # [(mid_time, clip_index)] マーカ→クリップ逆引き用
        # 選択中クリップの帯 (ver3 resolve5 §3-5)。軸は帯の付け替えに使う。
        self._highlight = None
        self._highlight_bounds = None    # 帯を形づくる上下の系列 (参照保持用)
        self._axis_x = None
        self._axis_y = None
        self._score_range = (0.0, 1.0)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._chart = QChart()
        self._chart.setTitle("採点グラフ (縦=スコア / 横=時間[秒]・マーカ=TOP5)")
        self._chart.legend().hide()

        self._view = QChartView(self._chart)
        self._view.setRenderHint(QPainter.Antialiasing)
        layout.addWidget(self._view)
        self._apply_theme()

    # グラフの配色をテーマへ揃える (resolve3 §5.1)。
    # QtCharts は QSS が効かないため、背景・文字色・系列色を API で指定する。
    # ui.theme = "system" のときは何もせず QtCharts の既定配色のままにする。
    def _apply_theme(self):
        if not theme.is_enabled():
            return
        # 背景はウィンドウのガラスを透かす (グラフ自体は塗らない)
        self._chart.setBackgroundBrush(QBrush(Qt.transparent))
        self._chart.setPlotAreaBackgroundVisible(False)
        self._chart.setBackgroundRoundness(0)
        text_color = theme.color("text.primary")
        self._chart.setTitleBrush(QBrush(text_color))
        self._view.setBackgroundBrush(QBrush(Qt.transparent))
        self._view.setFrameShape(QChartView.NoFrame)

    # 軸をテーマ色で塗る (目盛り線は控えめ・文字は本文色)
    def _style_axis(self, axis):
        if not theme.is_enabled():
            return
        axis.setLabelsBrush(QBrush(theme.color("text.primary")))
        axis.setTitleBrush(QBrush(theme.color("text.primary")))
        axis.setLinePen(QPen(theme.color("text.secondary")))
        axis.setGridLinePen(QPen(theme.color("glass.border")))

    # 窓スコア列 curve と TOP5 clips を描画する
    # curve: [{"start","end","total",...}] / clips: [{"index","start","end","score"}]
    def set_data(self, curve, clips):
        self._chart.removeAllSeries()
        # 既存軸を除去してから再構築する
        for axis in list(self._chart.axes()):
            self._chart.removeAxis(axis)
        self._clip_points = []
        self._highlight = None
        self._highlight_bounds = None

        curve = curve or []
        clips = clips or []

        # 折れ線 (窓スコア)
        line = QLineSeries()
        max_time = 1.0
        min_score = 0.0
        max_score = 1.0
        for entry in curve:
            x = _mid(entry)
            y = float(entry.get("total", 0.0))
            line.append(x, y)
            max_time = max(max_time, x)
            min_score = min(min_score, y)
            max_score = max(max_score, y)
        line.clicked.connect(self._on_series_clicked)
        # 折れ線は控えめな色、TOP5 マーカはアクセント (赤) で目立たせる (resolve3 §5.1)
        if theme.is_enabled():
            line.setPen(QPen(theme.color("text.secondary"), 2))
        self._chart.addSeries(line)

        # TOP5 マーカ (散布)
        scatter = QScatterSeries()
        scatter.setMarkerSize(14.0)
        for clip in clips:
            x = (float(clip.get("start", 0.0)) + float(clip.get("end", 0.0))) / 2.0
            y = float(clip.get("score", 0.0))
            scatter.append(x, y)
            self._clip_points.append((x, int(clip.get("index", 0))))
            max_time = max(max_time, x)
            min_score = min(min_score, y)
            max_score = max(max_score, y)
        scatter.clicked.connect(self._on_series_clicked)
        if theme.is_enabled():
            scatter.setBrush(QBrush(theme.color("accent")))
            scatter.setPen(QPen(theme.color("text.primary"), 1))
        self._chart.addSeries(scatter)

        # 軸 (時間 / スコア)。スコアは上下に少し余白を持たせる。
        axis_x = QValueAxis()
        axis_x.setTitleText("時間[秒]")
        axis_x.setRange(0.0, max_time * 1.02)
        axis_y = QValueAxis()
        axis_y.setTitleText("スコア")
        margin = max(1.0, (max_score - min_score) * 0.1)
        axis_y.setRange(min_score - margin, max_score + margin)

        self._style_axis(axis_x)
        self._style_axis(axis_y)
        self._chart.addAxis(axis_x, Qt.AlignBottom)
        self._chart.addAxis(axis_y, Qt.AlignLeft)
        for series in self._chart.series():
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)
        self._axis_x = axis_x
        self._axis_y = axis_y
        self._score_range = (axis_y.min(), axis_y.max())

    # 選択中クリップの区間を帯で示す (ver3 resolve5 §3-5)。
    # start/end は元動画(VOD)の秒。None を渡すと帯を消す。
    # 再生ヘッドの追従は行わない (回答 Q4。QtCharts の再描画が重いため)。
    def set_selected_range(self, start_sec, end_sec):
        if self._highlight is not None:
            self._chart.removeSeries(self._highlight)
            self._highlight = None
            self._highlight_bounds = None
        if start_sec is None or end_sec is None or end_sec <= start_sec:
            return
        if self._axis_x is None or self._axis_y is None:
            return
        low, high = self._score_range
        # QAreaSeries は上下の QLineSeries の所有権を取らない。Python 側で参照を
        # 手放すと解放され、描画時に落ちるため、帯と一緒に保持しておく。
        upper = QLineSeries(self)
        upper.append(float(start_sec), high)
        upper.append(float(end_sec), high)
        lower = QLineSeries(self)
        lower.append(float(start_sec), low)
        lower.append(float(end_sec), low)
        self._highlight_bounds = (upper, lower)
        band = QAreaSeries(upper, lower)
        # 帯は折れ線・マーカを隠さないよう薄く塗り、輪郭は描かない
        color = theme.color("accent") if theme.is_enabled() else QColor(120, 170, 255)
        fill = QColor(color)
        fill.setAlpha(48)
        band.setBrush(QBrush(fill))
        band.setPen(QPen(Qt.NoPen))
        self._chart.addSeries(band)
        band.attachAxis(self._axis_x)
        band.attachAxis(self._axis_y)
        self._highlight = band

    # 折れ線/マーカのクリック → 最寄りクリップを選択して emit
    def _on_series_clicked(self, point):
        if not self._clip_points:
            return
        nearest = min(self._clip_points, key=lambda cp: abs(cp[0] - point.x()))
        self.clip_selected.emit(nearest[1])
