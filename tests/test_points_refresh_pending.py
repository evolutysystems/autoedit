# 残高インジケータの更新要求の保留 (ver9 resolve17 §4.3) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 背景 (2026-11-02 の報告):
#   Stripe で支払って加入したのに、メイン画面のサブスクリプションボタンが
#   押せるままで、表示も変わらなかった。
#
#   原因の 1 つが refresh() の取り逃がしである。取得中に来た要求を
#   黙って捨てていたため、支払いの反映のような**1 回しか来ない通知**を
#   落とすと、次の更新が定期更新 (既定 300 秒) まで来なかった。
#
# 重点:
#   ・取得中の要求を捨てず、完了後に取り直すこと
#   ・保留は 1 回に畳むこと (同じ GET /api/points を並べない)
#   ・force は強い側を残すこと (force=True を force=False で薄めない)
#   ・取得中でなければ従来どおり即座に走ること
import unittest
from unittest import mock

from src.gui import points_indicator
from src.gui.points_indicator import PointsIndicator


# 走っているふりをするだけのワーカー
class _FakeWorker:

    def __init__(self, points, force=False, parent=None):
        self.force = force
        self.running = True
        self.loaded = mock.Mock()
        self.started = False

    def isRunning(self):
        return self.running

    def start(self):
        self.started = True


class _FakePoints:

    def last_balance(self):
        return {"unlimited": False}


# refresh() と _on_loaded() だけを取り出して試す (Qt を起動しない)。
# test_points_indicator_shutdown.py と同じ流儀。
class _Indicator:
    refresh = PointsIndicator.refresh
    _on_loaded = PointsIndicator._on_loaded

    def __init__(self):
        self._points = _FakePoints()
        self._worker = None
        self._pending = False
        self._pending_force = False
        self.balance_changed = mock.Mock()
        self.applied = []

    def _apply_state(self, balance=None, offline=False):
        self.applied.append((balance, offline))


class RefreshPendingTest(unittest.TestCase):

    def setUp(self):
        self._patch = mock.patch.object(points_indicator, "BalanceWorker", _FakeWorker)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.indicator = _Indicator()

    # 取得中でなければ、その場で取得が始まる (従来の挙動)
    def test_it_starts_immediately_when_idle(self):
        self.indicator.refresh(force=True)
        self.assertIsNotNone(self.indicator._worker)
        self.assertTrue(self.indicator._worker.started)
        self.assertTrue(self.indicator._worker.force)
        self.assertFalse(self.indicator._pending)

    # 取得中の要求は捨てずに保留する
    def test_a_request_during_a_fetch_is_held(self):
        self.indicator.refresh(force=True)
        first = self.indicator._worker

        self.indicator.refresh(force=True)
        # 新しいワーカーは作らない
        self.assertIs(self.indicator._worker, first)
        self.assertTrue(self.indicator._pending)
        self.assertTrue(self.indicator._pending_force)

    # 保留した要求は、取得が終わったら実行される
    def test_the_held_request_runs_after_the_fetch(self):
        self.indicator.refresh(force=True)
        first = self.indicator._worker
        self.indicator.refresh(force=True)

        first.running = False
        self.indicator._on_loaded({"unlimited": True})

        self.assertIsNot(self.indicator._worker, first)
        self.assertTrue(self.indicator._worker.started)
        self.assertFalse(self.indicator._pending)

    # 保留は 1 回に畳む。3 回重なっても取り直しは 1 回
    def test_the_held_request_is_collapsed(self):
        self.indicator.refresh(force=True)
        first = self.indicator._worker
        for _ in range(3):
            self.indicator.refresh(force=True)

        first.running = False
        self.indicator._on_loaded({"unlimited": True})
        second = self.indicator._worker
        self.assertIsNot(second, first)

        # 2 回目の完了ではもう取り直さない
        second.running = False
        self.indicator._on_loaded({"unlimited": True})
        self.assertIs(self.indicator._worker, second)

    # force は強い側を残す (force=True を後続の force=False で薄めない)
    def test_force_is_sticky(self):
        self.indicator.refresh(force=True)
        first = self.indicator._worker
        self.indicator.refresh(force=True)
        self.indicator.refresh(force=False)

        first.running = False
        self.indicator._on_loaded({"unlimited": True})
        self.assertTrue(self.indicator._worker.force)

    # 保留が無ければ取り直さない
    def test_no_refetch_without_a_pending_request(self):
        self.indicator.refresh(force=True)
        first = self.indicator._worker

        first.running = False
        self.indicator._on_loaded({"unlimited": True})
        self.assertIs(self.indicator._worker, first)


if __name__ == "__main__":
    unittest.main()
