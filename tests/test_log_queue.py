# ログ送信のローカルキュー (request1 / resolve1 §5) の単体テスト
# 実行: python -m unittest discover -s tests
# 要望 (request1 / resolve1):
#   ・ファイル名は yyyyMMddHHmmss_(テーブル名).log。同一秒は -NN で衝突を避ける (§5.2)
#   ・移動は os.replace 1 操作。waiting と sending の両方に同時に存在しない (§5.6)
#   ・落ちて sending に残った分は waiting へ戻す (§5.7)
#   ・sent は 7 日 / error は 30 日で消す (§5.8)
#   ・溜めすぎたら古いものから捨てる (§5.9)
# ネットワークは使わない。フォルダの状態だけを見る。
import os
import shutil
import tempfile
import time
import unittest

from src.services import log_queue
from src.services.log_queue import LogQueue


class LogQueueTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="logqueue_test_")
        self.queue = LogQueue(base_dir=self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _dir(self, table, state):
        path = os.path.join(self.base, table, state)
        os.makedirs(path, exist_ok=True)
        return path

    def _names(self, table, state):
        path = self._dir(table, state)
        return sorted(os.listdir(path)) if os.path.isdir(path) else []

    def _enqueue_error(self, message="boom"):
        return self.queue.enqueue(
            log_queue.TABLE_ERRORS, "/api/errors",
            {"module": "m", "exceptionType": "E", "message": message})

    # U1: ファイル名の書式と、同一秒の 2 件目が 1 件目を消さないこと
    def test_file_name_and_collision(self):
        first = self._enqueue_error("1 件目")
        second = self._enqueue_error("2 件目")
        third = self._enqueue_error("3 件目")

        self.assertTrue(first.endswith("_Errors.log"))
        self.assertEqual(14, len(first.split("_")[0]))

        names = self._names(log_queue.TABLE_ERRORS, log_queue.WAITING)
        # 同一秒でも 3 件すべて残る (上書きしない)
        self.assertEqual(3, len(names))
        self.assertEqual(3, len({first, second, third}))
        if first.split("_")[0] == second.split("_")[0]:
            self.assertIn("-02", second)

    # U2: 書き込みの途中で落ちた残骸 (.tmp / 0 バイト) が残らない
    def test_recover_removes_debris(self):
        self._enqueue_error()
        waiting = self._dir(log_queue.TABLE_ERRORS, log_queue.WAITING)
        with open(os.path.join(waiting, "zombie.log.tmp"), "w", encoding="utf-8") as f:
            f.write("{")
        with open(os.path.join(waiting, "20260101000000_Errors.log"), "w",
                  encoding="utf-8"):
            pass  # 名前だけ押さえて落ちた 0 バイトのログ

        self.queue.recover()

        names = self._names(log_queue.TABLE_ERRORS, log_queue.WAITING)
        self.assertEqual(1, len(names))
        self.assertFalse(any(name.endswith(".tmp") for name in names))

    # U3: waiting → sending → sent で、2 箇所に同時に存在しない
    def test_move_is_single_operation(self):
        name = self._enqueue_error()

        picked = self.queue.pick(log_queue.TABLE_ERRORS, 10)
        self.assertEqual(1, len(picked))
        self.assertEqual([], self._names(log_queue.TABLE_ERRORS, log_queue.WAITING))
        self.assertEqual([name], self._names(log_queue.TABLE_ERRORS, log_queue.SENDING))

        self.queue.mark_sent(picked[0])
        self.assertEqual([], self._names(log_queue.TABLE_ERRORS, log_queue.SENDING))
        self.assertEqual([name], self._names(log_queue.TABLE_ERRORS, log_queue.SENT))

    # U3: 本文がそのまま読み出せる (API のリクエスト本文を素で持つ)
    def test_body_round_trip(self):
        self.queue.enqueue(log_queue.TABLE_EXPORT_LOGS, "/api/analytics/export-logs",
                           {"projectId": "p1", "success": True},
                           depends_on={"project": "p1"})

        picked = self.queue.pick(log_queue.TABLE_EXPORT_LOGS, 10)
        self.assertEqual("/api/analytics/export-logs", picked[0].path)
        self.assertEqual({"projectId": "p1", "success": True}, picked[0].body)
        self.assertEqual({"project": "p1"}, picked[0].depends_on)

    # U4: sending に残っていた分は waiting へ戻り、attempts が増える
    def test_recover_returns_sending_to_waiting(self):
        self._enqueue_error()
        picked = self.queue.pick(log_queue.TABLE_ERRORS, 10)
        self.assertEqual(1, len(picked))

        recovered = self.queue.recover()

        self.assertEqual(1, recovered)
        self.assertEqual([], self._names(log_queue.TABLE_ERRORS, log_queue.SENDING))
        again = self.queue.pick(log_queue.TABLE_ERRORS, 10)
        self.assertEqual(1, again[0].attempts)

    # 一時的な失敗は waiting へ戻る。上限を超えたら error へ落ちる
    def test_release_until_max_attempts(self):
        self._enqueue_error()

        for expected in range(1, log_queue.MAX_ATTEMPTS + 1):
            picked = self.queue.pick(log_queue.TABLE_ERRORS, 10)
            self.assertEqual(1, len(picked), f"{expected} 回目で拾えなくなった")
            self.queue.release(picked[0], reason="offline")
            self.assertEqual(1, self.queue.count(log_queue.TABLE_ERRORS, log_queue.WAITING))

        picked = self.queue.pick(log_queue.TABLE_ERRORS, 10)
        self.queue.release(picked[0], reason="offline")

        self.assertEqual(0, self.queue.count(log_queue.TABLE_ERRORS, log_queue.WAITING))
        self.assertEqual(1, self.queue.count(log_queue.TABLE_ERRORS, log_queue.ERROR))

    # 中断は失敗ではないため attempts を増やさない (§6.3)
    def test_return_to_waiting_keeps_attempts(self):
        self._enqueue_error()
        picked = self.queue.pick(log_queue.TABLE_ERRORS, 10)

        self.queue.return_to_waiting(picked, reason="利用者の操作を優先")

        again = self.queue.pick(log_queue.TABLE_ERRORS, 10)
        self.assertEqual(0, again[0].attempts)

    # 壊れた JSON は error へ移し、送信対象にしない
    def test_broken_file_goes_to_error(self):
        waiting = self._dir(log_queue.TABLE_ERRORS, log_queue.WAITING)
        with open(os.path.join(waiting, "20260101000000_Errors.log"), "w",
                  encoding="utf-8") as f:
            f.write("{ broken")

        picked = self.queue.pick(log_queue.TABLE_ERRORS, 10)

        self.assertEqual([], picked)
        self.assertEqual(1, self.queue.count(log_queue.TABLE_ERRORS, log_queue.ERROR))

    # U5: sent は 7 日で消える。6 日前は残る
    def test_purge_by_age(self):
        self._enqueue_error()
        picked = self.queue.pick(log_queue.TABLE_ERRORS, 10)
        self.queue.mark_sent(picked[0])

        self._enqueue_error()
        picked = self.queue.pick(log_queue.TABLE_ERRORS, 10)
        self.queue.mark_sent(picked[0])

        sent_dir = self._dir(log_queue.TABLE_ERRORS, log_queue.SENT)
        names = sorted(os.listdir(sent_dir))
        old = os.path.join(sent_dir, names[0])
        eight_days_ago = time.time() - 8 * 86400
        os.utime(old, (eight_days_ago, eight_days_ago))

        removed = self.queue.purge(sent_days=7, error_days=30)

        self.assertEqual(1, removed)
        self.assertEqual(1, self.queue.count(log_queue.TABLE_ERRORS, log_queue.SENT))

    # U6: 上限を超えたら古いものから捨てる
    def test_trim_drops_oldest(self):
        queue = LogQueue(base_dir=self.base, limits={log_queue.TABLE_ERRORS: 3})
        names = []
        for index in range(5):
            names.append(queue.enqueue(
                log_queue.TABLE_ERRORS, "/api/errors",
                {"module": "m", "exceptionType": "E", "message": str(index)}))

        remaining = self._names(log_queue.TABLE_ERRORS, log_queue.WAITING)
        self.assertEqual(3, len(remaining))
        # 最後に積んだものは必ず残る
        self.assertIn(names[-1], remaining)

    # 依存先が残っているあいだは拾わせない (§6.6 の skip)
    def test_pick_skips_by_predicate(self):
        self.queue.enqueue(log_queue.TABLE_EXPORT_LOGS, "/api/analytics/export-logs",
                           {"projectId": "p1"}, depends_on={"project": "p1"})
        self.queue.enqueue(log_queue.TABLE_EXPORT_LOGS, "/api/analytics/export-logs",
                           {"projectId": "p2"}, depends_on={"project": "p2"})

        picked = self.queue.pick(
            log_queue.TABLE_EXPORT_LOGS, 10,
            skip=lambda data: data["dependsOn"].get("project") == "p1")

        self.assertEqual(1, len(picked))
        self.assertEqual("p2", picked[0].body["projectId"])
        self.assertEqual(1, self.queue.count(log_queue.TABLE_EXPORT_LOGS, log_queue.WAITING))

    # 依存の判定に使う「まだ送っていない ID」の集合
    def test_pending_values(self):
        self.queue.enqueue(log_queue.TABLE_PROJECTS, "/api/analytics/projects",
                           {"projectId": "p1"})
        self.queue.enqueue(log_queue.TABLE_PROJECTS, "/api/analytics/projects",
                           {"projectId": "p2"})
        picked = self.queue.pick(log_queue.TABLE_PROJECTS, 1)
        self.queue.mark_sent(picked[0])

        pending = self.queue.pending_values(log_queue.TABLE_PROJECTS, "projectId")

        self.assertEqual({"p2"}, pending)

    # 未知のテーブルは捨てる (フォルダを勝手に増やさない)
    def test_unknown_table_is_dropped(self):
        self.assertIsNone(self.queue.enqueue("Unknown", "/api/x", {}))
        self.assertFalse(os.path.isdir(os.path.join(self.base, "Unknown")))


if __name__ == "__main__":
    unittest.main()
