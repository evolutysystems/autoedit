# 終了中の残高インジケータの単体テスト (docs/error/fromUser/resolve.md §9-4)
# 実行: python -m unittest discover -s tests
#
# 背景 (2026-10-22 の報告):
#   Timeline 画面 → メイン画面と閉じたところで
#   RuntimeError: libshiboken: Internal C++ object
#   (PySide6.QtWidgets.QPushButton) already deleted が出た。
#
#   設定画面 / サブスクリプション画面の destroyed はアプリ終了時にも飛ぶ。
#   その時点でメイン画面側のウィジェットは既に C++ 側が解放されているため、
#   reload() が触ると落ちる。
#
# 重点: 解放済みなら何もしないこと。生きている間の動きは変えないこと。
import unittest
from unittest import mock

from src.gui import points_indicator
from src.gui.points_indicator import PointsIndicator


# reload() だけを取り出して試す (Qt を起動しない)
class _Indicator:
    reload = PointsIndicator.reload

    def __init__(self):
        self.login_button = object()
        self.applied = 0
        self.refreshed = 0

    def _apply_state(self):
        self.applied += 1

    def refresh(self, force=False):
        self.refreshed += 1


class PointsIndicatorShutdownTest(unittest.TestCase):

    # 解放済みなら何もしない (本題)
    def test_does_nothing_after_the_widget_is_deleted(self):
        indicator = _Indicator()
        with mock.patch.object(points_indicator, "isValid", return_value=False):
            indicator.reload()                  # 例外が出なければ合格

        self.assertEqual(indicator.applied, 0)
        self.assertEqual(indicator.refreshed, 0)

    # ボタンだけが先に解放された場合も触らない (親の解放中に起こり得る)
    def test_does_nothing_when_only_the_button_is_deleted(self):
        indicator = _Indicator()
        alive = {indicator: True, indicator.login_button: False}
        with mock.patch.object(points_indicator, "isValid",
                               side_effect=lambda obj: alive[obj]):
            indicator.reload()

        self.assertEqual(indicator.applied, 0)
        self.assertEqual(indicator.refreshed, 0)

    # 生きている間は従来どおり取り直す (守りを入れて動かなくなっていないこと)
    def test_reloads_while_alive(self):
        indicator = _Indicator()
        with mock.patch.object(points_indicator, "isValid", return_value=True):
            indicator.reload()

        self.assertEqual(indicator.applied, 1)
        self.assertEqual(indicator.refreshed, 1)


if __name__ == "__main__":
    unittest.main()
