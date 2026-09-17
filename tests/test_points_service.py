# ポイント制のクライアント側ファサード (ver5 resolve §5.2) の単体テスト
# 実行: python -m unittest discover -s tests
# 要望 (ver5 resolve):
#   ・watermark の有無はサーバーの応答だけで決める (R9)
#   ・ポイント処理で出力を壊さない。API の失敗は外へ出さない (§4-3)
#   ・オフラインは watermark あり。ただしサブスク会員はキャッシュで救済する (§3.3)
#   ・送れなかった commit / cancel は次回へ持ち越して再送する (R14)
# ネットワークは使わない。API クライアントを差し替えて応答だけを与える。
import datetime
import json
import os
import shutil
import tempfile
import unittest

from src.exceptions import ApiError, ApiOfflineError, ReauthRequiredError
from src.services.points import (
    JOB_ARCHIVE,
    JOB_CLIP,
    OUTPUT_RESOLVE_PROJECT,
    OUTPUT_VIDEO,
    PointsService,
)

_SETTINGS = {"points": {"subscription_cache_hours": 72, "balance_refresh_sec": 0}}


def _iso(hours_ago):
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return moment.isoformat()


# 応答をテストから指定できる API クライアント。
class _FakeClient:

    def __init__(self):
        self.responses = {}
        self.errors = {}
        self.calls = []

    def get(self, path, authenticated=True):
        return self._respond("GET", path, None)

    def post(self, path, body=None, authenticated=True):
        return self._respond("POST", path, body)

    def _respond(self, method, path, body):
        self.calls.append((method, path, body))
        error = self.errors.get(path)
        if error is not None:
            raise error
        return self.responses.get(path)


class _FakeAuth:

    def __init__(self, logged_in=True):
        self.client = _FakeClient()
        self._logged_in = logged_in

    def is_logged_in(self):
        return self._logged_in

    def set_logged_in(self, value):
        self._logged_in = value


class PointsServiceTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="stretheus-points-test-")
        self._auth = _FakeAuth()
        self._service = PointsService(self._auth, _SETTINGS, store_dir=self._dir)

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _reservation_response(self, amount=25, watermark=False, unlimited=False):
        return {
            "reservationId": "11111111-1111-1111-1111-111111111111",
            "status": "pending",
            "amount": amount,
            "watermarkRequired": watermark,
            "unlimited": unlimited,
            "expiresAt": "2026-09-15T18:00:00.1234567Z",
            "wallet": {"balance": 150, "reserved": amount, "available": 150 - amount},
        }

    # ---- 予約 -------------------------------------------------------------

    def test_reserve_uses_the_server_answer(self):
        self._auth.client.responses["/api/points/reservations"] = self._reservation_response()

        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        self.assertFalse(reservation.watermark_required)
        self.assertEqual(25, reservation.amount)
        self.assertTrue(reservation.is_tracked())
        self.assertFalse(reservation.offline)

        _method, _path, body = self._auth.client.calls[0]
        self.assertEqual(JOB_CLIP, body["jobType"])
        self.assertEqual(OUTPUT_VIDEO, body["outputType"])
        self.assertEqual(reservation.client_job_id, body["clientJobId"])

    def test_reserve_when_the_balance_is_short(self):
        # 残高不足は正常系。出力はブロックせず watermark を入れる (R4)。
        self._auth.client.responses["/api/points/reservations"] = self._reservation_response(
            amount=0, watermark=True)

        reservation = self._service.reserve(JOB_ARCHIVE, OUTPUT_VIDEO)

        self.assertTrue(reservation.watermark_required)
        self.assertEqual(0, reservation.amount)
        self.assertTrue(reservation.is_tracked())

    def test_reserve_without_login_does_not_call_the_api(self):
        # 未ログインのユーザーは従来どおり無償で使える (§4-5)。
        self._auth.set_logged_in(False)

        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        self.assertFalse(reservation.watermark_required)
        self.assertFalse(reservation.is_tracked())
        self.assertTrue(reservation.offline)
        self.assertEqual([], self._auth.client.calls)

    def test_reserve_offline_requires_a_watermark(self):
        self._auth.client.errors["/api/points/reservations"] = ApiOfflineError("接続できません")

        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        self.assertTrue(reservation.watermark_required)
        self.assertTrue(reservation.offline)
        self.assertFalse(reservation.is_tracked())

    def test_reserve_offline_keeps_subscribers_watermark_free(self):
        # 直近の判定でサブスク会員だったなら、オフラインでも救済する (§3.3)。
        self._write_state({"subscribed": True, "checkedAt": _iso(1)})
        self._auth.client.errors["/api/points/reservations"] = ApiOfflineError("接続できません")

        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        self.assertFalse(reservation.watermark_required)
        self.assertTrue(reservation.unlimited)

    def test_reserve_offline_ignores_a_stale_subscription(self):
        self._write_state({"subscribed": True, "checkedAt": _iso(100)})
        self._auth.client.errors["/api/points/reservations"] = ApiOfflineError("接続できません")

        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        self.assertTrue(reservation.watermark_required)

    def test_reserve_on_an_unexpected_error_falls_back_to_the_safe_side(self):
        self._auth.client.errors["/api/points/reservations"] = ApiError(
            "想定外", status=409, code="client_job_conflict")

        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        # 出力は止めない。ただし watermark は入れる。
        self.assertTrue(reservation.watermark_required)
        self.assertFalse(reservation.is_tracked())

    def test_reserve_remembers_the_subscription(self):
        self._auth.client.responses["/api/points/reservations"] = self._reservation_response(
            amount=0, unlimited=True)

        self._service.reserve(JOB_CLIP, OUTPUT_RESOLVE_PROJECT)

        self.assertTrue(self._read_state()["subscribed"])

    # ---- commit / cancel --------------------------------------------------

    def test_commit_sends_the_reservation(self):
        self._auth.client.responses["/api/points/reservations"] = self._reservation_response()
        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)
        self._auth.client.calls.clear()

        self._service.commit(reservation)

        _method, path, _body = self._auth.client.calls[0]
        self.assertEqual(f"/api/points/reservations/{reservation.reservation_id}/commit", path)
        self.assertEqual([], self._read_queue())

    def test_commit_of_an_untracked_reservation_does_nothing(self):
        self._auth.set_logged_in(False)
        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        self._service.commit(reservation)

        self.assertEqual([], self._auth.client.calls)

    def test_commit_is_queued_when_offline(self):
        self._auth.client.responses["/api/points/reservations"] = self._reservation_response()
        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        path = f"/api/points/reservations/{reservation.reservation_id}/commit"
        self._auth.client.errors[path] = ApiOfflineError("接続できません")

        self._service.commit(reservation)

        queued = self._read_queue()
        self.assertEqual(1, len(queued))
        self.assertEqual("commit", queued[0]["action"])
        self.assertEqual(reservation.reservation_id, queued[0]["reservationId"])

    def test_rejected_commit_is_not_queued(self):
        # 404 / 409 は再送しても結果が変わらない。
        self._auth.client.responses["/api/points/reservations"] = self._reservation_response()
        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        path = f"/api/points/reservations/{reservation.reservation_id}/cancel"
        self._auth.client.errors[path] = ApiError("確定済み", status=409,
                                                  code="reservation_committed")

        self._service.cancel(reservation)

        self.assertEqual([], self._read_queue())

    def test_reauth_required_does_not_break_the_output(self):
        self._auth.client.errors["/api/points/reservations"] = ReauthRequiredError(
            "再ログインが必要です。", status=401, code="reauth_required")

        reservation = self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        self.assertTrue(reservation.watermark_required)
        self.assertTrue(reservation.offline)

    # ---- 再送 -------------------------------------------------------------

    def test_flush_pending_resends_and_clears(self):
        self._write_queue([
            {"action": "commit", "reservationId": "aaa"},
            {"action": "cancel", "reservationId": "bbb"},
        ])

        self._service.flush_pending()

        paths = [call[1] for call in self._auth.client.calls]
        self.assertIn("/api/points/reservations/aaa/commit", paths)
        self.assertIn("/api/points/reservations/bbb/cancel", paths)
        self.assertEqual([], self._read_queue())

    def test_flush_pending_keeps_entries_while_offline(self):
        self._write_queue([{"action": "commit", "reservationId": "aaa"}])
        self._auth.client.errors["/api/points/reservations/aaa/commit"] = ApiOfflineError("断")

        self._service.flush_pending()

        self.assertEqual(1, len(self._read_queue()))

    def test_reserve_flushes_the_queue_first(self):
        self._write_queue([{"action": "commit", "reservationId": "aaa"}])
        self._auth.client.responses["/api/points/reservations"] = self._reservation_response()

        self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        self.assertEqual("/api/points/reservations/aaa/commit", self._auth.client.calls[0][1])
        self.assertEqual([], self._read_queue())

    # ---- 残高 -------------------------------------------------------------

    def test_balance_returns_none_when_offline(self):
        self._auth.client.errors["/api/points"] = ApiOfflineError("断")

        self.assertIsNone(self._service.balance())

    def test_balance_returns_none_without_login(self):
        self._auth.set_logged_in(False)

        self.assertIsNone(self._service.balance())
        self.assertEqual([], self._auth.client.calls)

    def test_balance_is_updated_by_the_reservation_response(self):
        self._auth.client.responses["/api/points"] = {
            "balance": 150, "reserved": 0, "available": 150, "unlimited": False,
        }
        self._auth.client.responses["/api/points/reservations"] = self._reservation_response()

        self._service.balance()
        self._service.reserve(JOB_CLIP, OUTPUT_VIDEO)

        # 予約の応答に含まれる残高で更新し、再取得を省く。
        self.assertEqual(25, self._service.last_balance()["reserved"])
        self.assertEqual(125, self._service.last_balance()["available"])

    # ---- ヘルパー ---------------------------------------------------------

    def _read_state(self):
        with open(os.path.join(self._dir, "points.json"), encoding="utf-8") as f:
            return json.load(f)

    def _write_state(self, state):
        with open(os.path.join(self._dir, "points.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)

    def _read_queue(self):
        path = os.path.join(self._dir, "pending_commits.json")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _write_queue(self, queue):
        with open(os.path.join(self._dir, "pending_commits.json"), "w", encoding="utf-8") as f:
            json.dump(queue, f)


if __name__ == "__main__":
    unittest.main()
