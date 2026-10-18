# ログを積む側 (request1 / resolve1 §4.1 / §7) の単体テスト
# 実行: python -m unittest discover -s tests
# 要望 (request1 / resolve1):
#   ・積むだけ。ネットワークも認証も触らないのでオフラインでも失敗しない (§4.3)
#   ・前回が異常終了していたら次回起動で crash として記録する (§7.1)
#   ・projectId はパスに紐づけて覚え、同じ素材なら使い回す (§7.2)
#   ・telemetry.enabled = false なら 1 件も書かない (§10)
#   ・Errors は send_errors で別に止められる (§10)
import json
import os
import shutil
import tempfile
import unittest

from src.services import log_queue, telemetry

_ENABLED = {"telemetry": {"enabled": True, "send_errors": True, "send_analytics": True}}


class TelemetryTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="telemetry_test_")
        telemetry.configure(_ENABLED, base_dir=self.base)

    def tearDown(self):
        telemetry.reset()
        shutil.rmtree(self.base, ignore_errors=True)

    def _bodies(self, table, state=log_queue.WAITING):
        directory = os.path.join(self.base, "logqueue", table, state)
        if not os.path.isdir(directory):
            return []
        bodies = []
        for name in sorted(os.listdir(directory)):
            with open(os.path.join(directory, name), "r", encoding="utf-8") as f:
                bodies.append(json.load(f))
        return bodies

    def _count(self, table):
        return len(self._bodies(table))

    # 起動でセッションと AppStart が 1 件ずつ積まれる
    def test_app_started(self):
        session_id = telemetry.app_started(startup_ms=1234)

        self.assertTrue(session_id)
        sessions = self._bodies(log_queue.TABLE_SESSIONS)
        self.assertEqual(1, len(sessions))
        self.assertEqual(session_id, sessions[0]["body"]["sessionId"])

        events = self._bodies(log_queue.TABLE_EVENTS)
        self.assertEqual(1, len(events))
        self.assertEqual("AppStart", events[0]["body"]["event"])
        self.assertEqual(1234, events[0]["body"]["duration"])
        self.assertEqual(session_id, events[0]["body"]["sessionId"])

    # 正常終了なら end が積まれ、session.json は消える
    def test_app_closing(self):
        session_id = telemetry.app_started()

        telemetry.app_closing()

        ends = [b for b in self._bodies(log_queue.TABLE_SESSIONS)
                if b["path"].endswith("/end")]
        self.assertEqual(1, len(ends))
        self.assertFalse(ends[0]["body"]["crash"])
        self.assertEqual({"session": session_id}, ends[0]["dependsOn"])
        self.assertFalse(os.path.exists(os.path.join(self.base, "session.json")))

    # 異常終了 (session.json が残っている) は次回起動で crash として記録する
    def test_crash_detected_on_next_start(self):
        first = telemetry.app_started()
        # app_closing を呼ばずに落ちた状態を作る
        telemetry.configure(_ENABLED, base_dir=self.base)

        second = telemetry.app_started()

        self.assertNotEqual(first, second)
        crashes = [b for b in self._bodies(log_queue.TABLE_SESSIONS)
                   if b["path"].endswith("/end") and b["body"]["crash"]]
        self.assertEqual(1, len(crashes))
        self.assertIn(first, crashes[0]["path"])

    # 同じパスなら projectId を使い回す (Projects は upsert なので増えない)
    def test_project_id_is_reused_per_path(self):
        first = telemetry.register_project(None, _ENABLED, project_path="D:/a.timeline.json",
                                           output_profile={"width": 1920, "height": 1080})
        second = telemetry.register_project(None, _ENABLED, project_path="D:/a.timeline.json",
                                            output_profile={"width": 1920, "height": 1080})
        other = telemetry.register_project(None, _ENABLED, project_path="D:/b.timeline.json",
                                           output_profile={"width": 1920, "height": 1080})

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(3, self._count(log_queue.TABLE_PROJECTS))

    # 解像度が決まらない呼び出しでは Projects を積まない (400 になるため)
    def test_project_without_resolution_is_not_queued(self):
        project_id = telemetry.register_project(None, {}, project_path="D:/a.timeline.json")

        self.assertTrue(project_id)
        self.assertEqual(0, self._count(log_queue.TABLE_PROJECTS))

    # 書き出しの完了で ExportLogs とイベントが積まれ、依存が付く
    def test_export_finished(self):
        telemetry.app_started()
        telemetry.export_finished("p1", True, 1500,
                                  settings={"ffmpeg": {"video_codec": "libx264"}},
                                  output_profile={"width": 1080, "height": 1920})

        exports = self._bodies(log_queue.TABLE_EXPORT_LOGS)
        self.assertEqual(1, len(exports))
        self.assertEqual("1080x1920", exports[0]["body"]["resolution"])
        self.assertEqual("libx264", exports[0]["body"]["codec"])
        self.assertEqual(1500, exports[0]["body"]["duration"])
        self.assertEqual({"project": "p1"}, exports[0]["dependsOn"])

        names = [b["body"]["event"] for b in self._bodies(log_queue.TABLE_EVENTS)]
        self.assertIn("ExportSucceeded", names)

    def test_export_cancelled_event(self):
        telemetry.export_finished("p1", False, 10, cancelled=True,
                                  output_profile={"width": 1920, "height": 1080})

        names = [b["body"]["event"] for b in self._bodies(log_queue.TABLE_EVENTS)]
        self.assertIn("ExportCancelled", names)

    # 工程名は固定の英字名へ写す。未知のラベルは積まない (日本語を送らない)
    def test_processing_finished_maps_label(self):
        telemetry.processing_finished("p1", "音声認識", 2500, True)
        telemetry.processing_finished("p1", "未知の工程", 100, True)

        logs = self._bodies(log_queue.TABLE_PROCESSING_LOGS)
        self.assertEqual(1, len(logs))
        self.assertEqual("Transcribe", logs[0]["body"]["processName"])
        self.assertEqual(2500, logs[0]["body"]["duration"])

    # projectId が無い (CLI / 送信無効) なら工程ログは積まない
    def test_processing_without_project_is_skipped(self):
        telemetry.processing_finished(None, "音声認識", 2500, True)

        self.assertEqual(0, self._count(log_queue.TABLE_PROCESSING_LOGS))

    # エラーはマスクを通って積まれる
    def test_report_error_is_masked(self):
        telemetry.report_error("pipeline_runner",
                               OSError(r"failed C:\Users\evolu\a.mp4"))

        errors = self._bodies(log_queue.TABLE_ERRORS)
        self.assertEqual(1, len(errors))
        self.assertEqual("OSError", errors[0]["body"]["exceptionType"])
        self.assertNotIn("evolu", errors[0]["body"]["message"])

    # send_errors = false なら Errors は積まない (分析系は積む)
    def test_send_errors_switch(self):
        telemetry.configure({"telemetry": {"enabled": True, "send_errors": False}},
                            base_dir=self.base)

        telemetry.report_error("m", OSError("x"))
        telemetry.app_started()

        self.assertEqual(0, self._count(log_queue.TABLE_ERRORS))
        self.assertEqual(1, self._count(log_queue.TABLE_SESSIONS))

    # enabled = false なら 1 件も書かない
    def test_disabled_writes_nothing(self):
        telemetry.configure({"telemetry": {"enabled": False}}, base_dir=self.base)

        self.assertIsNone(telemetry.app_started())
        telemetry.report_error("m", OSError("x"))
        telemetry.export_finished("p1", True, 1)
        self.assertIsNone(telemetry.register_project(None, _ENABLED))

        self.assertFalse(telemetry.enabled())
        self.assertFalse(os.path.isdir(os.path.join(self.base, "logqueue")))

    # 未設定 (CLI / テスト) でも安全に呼べる
    def test_unconfigured_is_safe(self):
        telemetry.reset()

        self.assertIsNone(telemetry.app_started())
        telemetry.app_closing()
        telemetry.processing_finished("p1", "音声認識", 1, True)
        telemetry.export_finished("p1", True, 1)
        telemetry.report_error("m", OSError("x"))
        telemetry.event("AppStart")


if __name__ == "__main__":
    unittest.main()
