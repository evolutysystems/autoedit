# ログ送信 (request1 / resolve1 §6 / §9) の単体・結合テスト
# 実行: python -m unittest discover -s tests
# 要望 (request1 / resolve1):
#   ・利用者の操作が始まったら打ち切り、sending の残りは waiting へ戻す (R3 / §6.3)
#   ・オフライン時は溜めるだけ。オンラインで送る (R5 / §6.5)
#   ・依存順序を守る。Projects より先に ExportLogs を送らない (§3.1 / §6.6)
#   ・再送して直らない失敗だけ error へ置く (R12 / §9)
#   ・AnalyticsEvents は 1 リクエストへまとめる (§7.4)
# ネットワークは使わない。API クライアントを差し替えて応答だけを与える。
import shutil
import tempfile
import unittest

from src.exceptions import ApiError, ApiOfflineError, ReauthRequiredError
from src.services import log_queue
from src.services.log_queue import LogQueue
from src.services.log_sender import LogSender

_SETTINGS = {
    "telemetry": {
        "enabled": True,
        "send_errors": True,
        "send_analytics": True,
        # テストでは待たない
        "request_interval_ms": 0,
    },
}


# 応答をテストから指定できる API クライアント。
class _FakeClient:

    def __init__(self):
        self.calls = []
        # パス別のエラー (呼ぶたびに pop する list も可)
        self.errors = {}
        self.responses = {}
        # 送信のたびに呼ばれるフック (中断の差し込みに使う)
        self.on_post = None

    def post(self, path, body=None, authenticated=True):
        self.calls.append((path, body))
        if self.on_post is not None:
            self.on_post(len(self.calls))

        error = self.errors.get(path)
        if isinstance(error, list):
            error = error.pop(0) if error else None
        if error is not None:
            raise error
        return self.responses.get(path)

    def paths(self):
        return [path for path, _body in self.calls]


class _FakeAuth:

    def __init__(self, session=True):
        self.client = _FakeClient()
        self._session = session

    def ensure_session(self):
        return self._session

    def has_session(self):
        return self._session


class LogSenderTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="logsender_test_")
        self.queue = LogQueue(base_dir=self.base)
        self.auth = _FakeAuth()
        self.sender = LogSender(self.auth, _SETTINGS, queue=self.queue)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    # ---- 積む小道具 ----
    def _error(self, message="boom"):
        self.queue.enqueue(log_queue.TABLE_ERRORS, "/api/errors",
                           {"module": "m", "exceptionType": "E", "message": message})

    def _project(self, project_id="p1"):
        self.queue.enqueue(log_queue.TABLE_PROJECTS, "/api/analytics/projects",
                           {"projectId": project_id, "resolution": "1920x1080"})

    def _export(self, project_id="p1"):
        self.queue.enqueue(log_queue.TABLE_EXPORT_LOGS, "/api/analytics/export-logs",
                           {"projectId": project_id, "resolution": "1920x1080",
                            "codec": "libx264", "success": True, "duration": 1},
                           depends_on={"project": project_id})

    def _event(self, name="AppStart"):
        self.queue.enqueue(log_queue.TABLE_EVENTS, "/api/analytics/events",
                           {"event": name, "time": "2026-10-18T00:00:00+00:00"})

    def _count(self, table, state):
        return self.queue.count(table, state)

    # I1: オフライン → 溜まる / オンライン → 送られる
    def test_offline_keeps_queue_untouched(self):
        self.auth._session = False
        self._error()

        result = self.sender.flush()

        self.assertTrue(result.offline)
        self.assertEqual([], self.auth.client.paths())
        self.assertEqual(1, self._count(log_queue.TABLE_ERRORS, log_queue.WAITING))

    def test_online_sends_and_moves_to_sent(self):
        self._error()

        result = self.sender.flush()

        self.assertEqual(1, result.sent)
        self.assertEqual(["/api/errors"], self.auth.client.paths())
        self.assertEqual(0, self._count(log_queue.TABLE_ERRORS, log_queue.WAITING))
        self.assertEqual(1, self._count(log_queue.TABLE_ERRORS, log_queue.SENT))

    # I2: 429 は waiting へ戻し、フェーズを打ち切る
    def test_rate_limited_returns_to_waiting(self):
        self._error("1")
        self._error("2")
        self.auth.client.errors["/api/errors"] = ApiError("too many", status=429)

        result = self.sender.flush()

        self.assertTrue(result.offline)
        self.assertEqual(1, len(self.auth.client.calls), "1 件で打ち切る")
        self.assertEqual(2, self._count(log_queue.TABLE_ERRORS, log_queue.WAITING))
        self.assertEqual(0, self._count(log_queue.TABLE_ERRORS, log_queue.ERROR))

    # 401 も打ち切り (残りも同じ結果になるため 1 件ずつ試さない)
    def test_reauth_required_breaks_phase(self):
        self._error("1")
        self._error("2")
        self.auth.client.errors["/api/errors"] = ReauthRequiredError(
            "login required", status=401, code="reauth_required")

        result = self.sender.flush()

        self.assertTrue(result.offline)
        self.assertEqual(1, len(self.auth.client.calls))
        self.assertEqual(2, self._count(log_queue.TABLE_ERRORS, log_queue.WAITING))

    # 5xx / 接続不可も同じ扱い
    def test_offline_error_breaks_phase(self):
        self._error()
        self.auth.client.errors["/api/errors"] = ApiOfflineError("unreachable")

        result = self.sender.flush()

        self.assertTrue(result.offline)
        self.assertEqual(1, self._count(log_queue.TABLE_ERRORS, log_queue.WAITING))

    # I3: 400 は再送しても通らないので error へ
    def test_bad_request_goes_to_error(self):
        self._error()
        self.auth.client.errors["/api/errors"] = ApiError("invalid", status=400)

        result = self.sender.flush()

        self.assertEqual(1, result.failed)
        self.assertEqual(0, self._count(log_queue.TABLE_ERRORS, log_queue.WAITING))
        self.assertEqual(1, self._count(log_queue.TABLE_ERRORS, log_queue.ERROR))

    # U8 / §6.6: Projects が未送信のあいだ ExportLogs を送らない
    def test_export_waits_for_project(self):
        self._project("p1")
        self._export("p1")
        # Projects だけ失敗させる
        self.auth.client.errors["/api/analytics/projects"] = ApiOfflineError("down")

        result = self.sender.flush()

        self.assertTrue(result.offline)
        self.assertNotIn("/api/analytics/export-logs", self.auth.client.paths())
        self.assertEqual(1, self._count(log_queue.TABLE_EXPORT_LOGS, log_queue.WAITING))

    # 依存順序: Projects → ExportLogs の順で送る
    def test_sends_in_dependency_order(self):
        self._export("p1")
        self._project("p1")

        self.sender.flush()

        paths = self.auth.client.paths()
        self.assertEqual(["/api/analytics/projects", "/api/analytics/export-logs"], paths)

    # I4: プロジェクト未登録の 404 は捨てずに waiting へ戻す (§9)。
    # Projects が待ち行列へ入れば、次の flush で先に送られて通るようになる。
    def test_project_404_returns_to_waiting(self):
        self._project("p1")
        self._export("p1")
        self.auth.client.errors["/api/analytics/export-logs"] = [
            ApiError("not found", status=404)]

        result = self.sender.flush()

        self.assertEqual(1, result.sent, "Projects は送れている")
        self.assertEqual(1, self._count(log_queue.TABLE_EXPORT_LOGS, log_queue.WAITING))
        self.assertEqual(0, self._count(log_queue.TABLE_EXPORT_LOGS, log_queue.ERROR))

        # 2 回目は 404 が解消しているので送れる
        self.auth.client.errors.pop("/api/analytics/export-logs")
        result = self.sender.flush()
        self.assertEqual(1, result.sent)
        self.assertEqual(1, self._count(log_queue.TABLE_EXPORT_LOGS, log_queue.SENT))

    # 直らないまま attempts の上限に達したら error へ落ちる
    def test_project_404_gives_up_at_attempt_limit(self):
        self._project("p1")
        self._export("p1")
        self.auth.client.errors["/api/analytics/export-logs"] = [
            ApiError("not found", status=404) for _ in range(log_queue.MAX_ATTEMPTS + 2)]

        for _ in range(log_queue.MAX_ATTEMPTS + 1):
            self.sender.flush()

        self.assertEqual(0, self._count(log_queue.TABLE_EXPORT_LOGS, log_queue.WAITING))
        self.assertEqual(1, self._count(log_queue.TABLE_EXPORT_LOGS, log_queue.ERROR))

    # セッションの end が 404 (開始が残っていない) なら再送しても通らない
    def test_session_end_404_goes_to_error(self):
        self.queue.enqueue(log_queue.TABLE_SESSIONS, "/api/analytics/sessions/s1/end",
                           {"endAt": "2026-10-18T00:00:00+00:00", "crash": False})
        self.auth.client.errors["/api/analytics/sessions/s1/end"] = ApiError(
            "not found", status=404)

        result = self.sender.flush()

        self.assertEqual(1, result.failed)
        self.assertEqual(1, self._count(log_queue.TABLE_SESSIONS, log_queue.ERROR))

    # U9: 中断すると、以降は送らず waiting へ戻る
    def test_stop_returns_remaining_to_waiting(self):
        for index in range(5):
            self._error(str(index))

        self.auth.client.on_post = lambda count: (
            self.sender.request_stop() if count == 2 else None)

        result = self.sender.flush()

        self.assertTrue(result.stopped)
        self.assertEqual(2, len(self.auth.client.calls))
        self.assertEqual(2, self._count(log_queue.TABLE_ERRORS, log_queue.SENT))
        self.assertEqual(3, self._count(log_queue.TABLE_ERRORS, log_queue.WAITING))
        self.assertEqual(0, self._count(log_queue.TABLE_ERRORS, log_queue.SENDING))

    # 中断が立っていると 1 件も送らない
    def test_stopped_before_start(self):
        self._error()
        self.sender.request_stop()

        result = self.sender.flush()

        self.assertTrue(result.stopped)
        self.assertEqual([], self.auth.client.paths())

        # allow() で再開できる
        self.sender.allow()
        result = self.sender.flush()
        self.assertEqual(1, result.sent)

    # I5: イベントは 1 リクエストへまとめ、一部 reject でも全件 sent にする
    def test_events_are_batched(self):
        for index in range(3):
            self._event(f"Event{index}")
        self.auth.client.responses["/api/analytics/events"] = {
            "accepted": 2, "rejected": 1, "errors": ["[2] time が不正です。"]}

        result = self.sender.flush()

        self.assertEqual(1, len(self.auth.client.calls), "3 件を 1 リクエストで送る")
        sent_body = self.auth.client.calls[0][1]
        self.assertEqual(3, len(sent_body))
        self.assertEqual(3, result.sent)
        self.assertEqual(3, self._count(log_queue.TABLE_EVENTS, log_queue.SENT))

    # 1 件も受け付けられなければ error へ (本文の作り方が間違っている)
    def test_events_all_rejected_go_to_error(self):
        self._event()
        self.auth.client.responses["/api/analytics/events"] = {
            "accepted": 0, "rejected": 1, "errors": ["[0] EventName が不正です。"]}

        result = self.sender.flush()

        self.assertEqual(1, result.failed)
        self.assertEqual(1, self._count(log_queue.TABLE_EVENTS, log_queue.ERROR))

    # Errors だけ止められる (プライバシーポリシーの記載単位 / §10)
    def test_send_errors_switch(self):
        settings = {"telemetry": {"enabled": True, "send_errors": False,
                                  "send_analytics": True, "request_interval_ms": 0}}
        sender = LogSender(self.auth, settings, queue=self.queue)
        self._error()
        self._event()

        sender.flush()

        self.assertEqual(["/api/analytics/events"], self.auth.client.paths())
        self.assertEqual(1, self._count(log_queue.TABLE_ERRORS, log_queue.WAITING))

    # 無効なら 1 件も送らない
    def test_disabled_sender_does_nothing(self):
        sender = LogSender(self.auth, {"telemetry": {"enabled": False}}, queue=self.queue)
        self._error()

        result = sender.flush()

        self.assertTrue(result.skipped)
        self.assertEqual([], self.auth.client.paths())

    # 起動時の後始末: sending の残骸が waiting へ戻ってから送られる (§5.7)
    def test_startup_recovers_sending(self):
        self._error()
        self.queue.pick(log_queue.TABLE_ERRORS, 1)  # 送信中に落ちた状態を作る
        self.assertEqual(1, self._count(log_queue.TABLE_ERRORS, log_queue.SENDING))

        result = self.sender.flush()

        self.assertEqual(1, result.sent)
        self.assertEqual(1, self._count(log_queue.TABLE_ERRORS, log_queue.SENT))

    # 1 回の flush のリクエスト数に上限がある (レート制限に当てない / §3.4)
    def test_request_budget(self):
        settings = {"telemetry": {"enabled": True, "send_errors": True,
                                  "send_analytics": True, "request_interval_ms": 0,
                                  "max_requests_per_flush": 2}}
        sender = LogSender(self.auth, settings, queue=self.queue)
        for index in range(5):
            self._error(str(index))

        result = sender.flush()

        self.assertEqual(2, result.sent)
        self.assertEqual(2, len(self.auth.client.calls))
        self.assertEqual(3, self._count(log_queue.TABLE_ERRORS, log_queue.WAITING))


if __name__ == "__main__":
    unittest.main()
