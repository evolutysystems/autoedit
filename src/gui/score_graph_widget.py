# 採点グラフウィジェット (flow17 R2 / resolve17 §4.7.1)
# 縦=スコア / 横=動画時間 の折れ線 (窓スコア列) に、TOP5 区間をマーカ表示する。
# マーカ/折れ線のクリックで最寄りクリップを選び clip_selected(index) を emit し、
# 結果画面の下段 (字幕編集・プレビュー) を同期させる。
from PySide6.QtCharts import (
    QChart,
    QChartView,
    QLineSeries,
    QScatterSeries,
    QValueAxis,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QVBoxLayout, QWidget


# 窓スコア (curve 要素) の中央時刻を返す
def _mid(entry):
    return (float(entry.get("start", 0.0)) + float(entry.get("end", 0.0))) / 2.0


class ScoreGraphWidget(QWidget):

    # クリップ選択シグナル (clip["index"] を渡す)
    clip_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clip_points = []  # [(mid_time, clip_index)] マーカ→クリップ逆引き用
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

    # 窓スコア列 curve と TOP5 clips を描画する
    # curve: [{"start","end","total",...}] / clips: [{"index","start","end","score"}]
    def set_data(self, curve, clips):
        self._chart.removeAllSeries()
        # 既存軸を除去してから再構築する
        for axis in list(self._chart.axes()):
            self._chart.removeAxis(axis)
        self._clip_points = []

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
        self._chart.addSeries(scatter)

        # 軸 (時間 / スコア)。スコアは上下に少し余白を持たせる。
        axis_x = QValueAxis()
        axis_x.setTitleText("時間[秒]")
        axis_x.setRange(0.0, max_time * 1.02)
        axis_y = QValueAxis()
        axis_y.setTitleText("スコア")
        margin = max(1.0, (max_score - min_score) * 0.1)
        axis_y.setRange(min_score - margin, max_score + margin)

        self._chart.addAxis(axis_x, Qt.AlignBottom)
        self._chart.addAxis(axis_y, Qt.AlignLeft)
        for series in self._chart.series():
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)

    # 折れ線/マーカのクリック → 最寄りクリップを選択して emit
    def _on_series_clicked(self, point):
        if not self._clip_points:
            return
        nearest = min(self._clip_points, key=lambda cp: abs(cp[0] - point.x()))
        self.clip_selected.emit(nearest[1])
