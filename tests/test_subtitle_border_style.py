# 縁取りスタイル (ASS BorderStyle) と、設定画面のホイール事故の防止 (error 20261122)
# 実行: python -m unittest discover -s tests
# 重点:
#   ・既定は 1 (縁取り + ドロップシャドウ) であること
#   ・設定値が ASS の Style 行の BorderStyle 列へそのまま出ること
#   ・3 (不透明ボックス) のときは、箱が「縁の色」で塗られることをログへ残すこと
#   ・ドロップダウン / 数値入力がホイールで変わらないこと (クリック相当は今までどおり)
import json
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )

    from src.gui import wheel_guard
    _QT_AVAILABLE = True
except Exception:  # noqa: BLE001 (PySide6 が無い環境ではスキップする)
    _QT_AVAILABLE = False

from src.modules import subtitle_generator
from src.settings.settings_window import DEFAULT_SETTINGS, SETTINGS_FILE

# ASS Style 行の BorderStyle は 15 列目 (Format の並び / 0 始まりで 14)
_BORDER_STYLE_COLUMN = 14


def _profile(border_style):
    return subtitle_generator.FontProfile(
        family="TestFont", size=48, color_hex="#FFFFFF",
        outline_color="&H00FFFFFF", outline_width=10,
        back_color="&H64000000", border_style=border_style,
    )


def _item():
    return {"start": 1.0, "end": 3.0, "text": "こんばんは", "use": True,
            "role": "streamer", "font": "", "font_size": None}


# ASS を書き出して Style 行 (Style: 以降) を返す
def _style_lines(profile):
    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "out.ass")
        subtitle_generator.build_subtitle_file([_item()], profile, path,
                                               video_width=1920, video_height=1080)
        with open(path, encoding="utf-8") as f:
            text = f.read()
    return [line for line in text.splitlines() if line.startswith("Style:")]


class BorderStyleDefaultTest(unittest.TestCase):

    # 既定は 1 = 縁取り + ドロップシャドウ (3 にすると白い箱になる)
    def test_default_settings(self):
        self.assertEqual(DEFAULT_SETTINGS["subtitle"]["border_style"], 1)

    # 同梱する setting.json も 1 (配布物へ 3 が焼き付かないこと)
    def test_shipped_setting_json(self):
        with open(SETTINGS_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
        self.assertEqual(data["subtitle"]["border_style"], 1)

    # FontProfile の既定も 1 (呼び出し側が省略しても箱にならない)
    def test_font_profile_default(self):
        profile = subtitle_generator.FontProfile(
            family="TestFont", size=48, color_hex="#FFFFFF")
        self.assertEqual(profile.border_style, 1)


class BorderStyleAssTest(unittest.TestCase):

    # 設定値が 3 役割すべての Style 行の BorderStyle 列へそのまま出る
    def test_value_is_written_to_style_line(self):
        for value in (1, 3):
            lines = _style_lines(_profile(value))
            self.assertEqual(len(lines), 3, "役割ごとの Style 行は 3 本")
            for line in lines:
                columns = line[len("Style:"):].split(",")
                self.assertEqual(int(columns[_BORDER_STYLE_COLUMN]), value, line)

    # 3 のときだけ「箱は縁の色で塗られる」と警告する
    def test_box_style_is_logged(self):
        with self.assertLogs("src.modules.subtitle_generator", level="WARNING") as log:
            _style_lines(_profile(3))
        joined = "\n".join(log.output)
        self.assertIn("不透明ボックス", joined)
        self.assertIn("&H00FFFFFF", joined, "箱の色になる縁の色をログへ出すこと")

    # 1 のときは何も警告しない (従来どおりの静かな出力)
    def test_outline_style_is_not_logged(self):
        logger = subtitle_generator._logger      # noqa: SLF001 (単体テスト)
        with self.assertLogs(logger, level="WARNING") as log:
            logger.warning("ダミー")             # assertLogs は 0 件を許さないため
            _style_lines(_profile(1))
        self.assertEqual(len(log.output), 1, log.output)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class WheelGuardTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # 「縁取りスタイル」と同じ 2 択のコンボを、入れ子のレイアウトへ入れて作る
    def _panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        combo = QComboBox()
        combo.addItem("縁取り + ドロップシャドウ", 1)
        combo.addItem("不透明ボックス", 3)
        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setValue(10)
        inner_layout.addWidget(combo)
        inner_layout.addWidget(spin)
        layout.addWidget(inner)
        return panel, combo, spin

    def _wheel(self, widget):
        event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, -120),
                            QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                            Qt.NoScrollPhase, False)
        self.app.sendEvent(widget, event)

    # 素の Qt ではホイール 1 ノッチで 1 → 3 へ飛ぶ (事故の再現 = ガードの前提)
    def test_unguarded_combo_changes_on_wheel(self):
        _panel, combo, _spin = self._panel()
        self._wheel(combo)
        self.assertEqual(combo.currentData(), 3)

    # ガードを入れるとホイールでは変わらない (activated も飛ばない)
    def test_guarded_combo_ignores_wheel(self):
        panel, combo, _spin = self._panel()
        fired = []
        combo.activated.connect(fired.append)
        self.assertGreaterEqual(wheel_guard.install(panel), 2)
        self._wheel(combo)
        self.assertEqual(combo.currentData(), 1, "ホイールで縁取りスタイルが変わった")
        self.assertEqual(fired, [], "ホイールで activated が飛んだ")

    # 数値入力にも効く
    def test_guarded_spinbox_ignores_wheel(self):
        panel, _combo, spin = self._panel()
        wheel_guard.install(panel)
        self._wheel(spin)
        self.assertEqual(spin.value(), 10)

    # クリックで選び直す経路 (setCurrentIndex) は今までどおり動く
    def test_guard_keeps_normal_selection(self):
        panel, combo, spin = self._panel()
        wheel_guard.install(panel)
        combo.setCurrentIndex(1)
        spin.setValue(20)
        self.assertEqual(combo.currentData(), 3)
        self.assertEqual(spin.value(), 20)

    # 呼び直してもガードは増えない (言語切替でタブを組み直す経路)
    def test_install_is_idempotent(self):
        panel, combo, _spin = self._panel()
        wheel_guard.install(panel)
        wheel_guard.install(panel)
        self.assertEqual(len(panel.findChildren(wheel_guard.WheelGuard)), 1)
        self._wheel(combo)
        self.assertEqual(combo.currentData(), 1)


if __name__ == "__main__":
    unittest.main()
