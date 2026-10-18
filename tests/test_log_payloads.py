# 送信するログの本文組み立てとマスク (request1 / resolve1 §7 / §8) の単体テスト
# 実行: python -m unittest discover -s tests
# 要望 (request1 / resolve1):
#   ・例外メッセージのパス・ユーザー名・メール・トークンを送らない (§8 / security.md FU6)
#   ・サーバーの検証規則に通らない本文は作らない (§3.3)
#   ・イベント名は ^[A-Za-z][A-Za-z0-9_.\-]{0,127}$ (日本語は使えない)
import os
import unittest

from src.services import log_payloads as payloads


class MaskMessageTest(unittest.TestCase):

    # U7: Windows のパスが残らない
    def test_masks_windows_path(self):
        masked = payloads.mask_message(
            "Could not find file 'C:/Users/evolu/Videos/input.mp4'")

        self.assertNotIn("evolu", masked)
        self.assertNotIn("input.mp4", masked)
        self.assertIn("<path>", masked)

    def test_masks_backslash_path(self):
        masked = payloads.mask_message(
            r"ffmpeg failed: D:\develop\autoedit\input\a b.mp4 (exit 1)")

        self.assertNotIn("autoedit", masked)
        self.assertIn("<path>", masked)
        self.assertIn("exit 1", masked)

    def test_masks_unc_path(self):
        masked = payloads.mask_message(r"open \\nas\share\movie.mp4 failed")

        self.assertNotIn("nas", masked)
        self.assertIn("<path>", masked)

    def test_masks_email_and_token(self):
        masked = payloads.mask_message(
            "login failed for a.b@example.com token=abcdef0123456789abcdef0123456789ab")

        self.assertNotIn("example.com", masked)
        self.assertNotIn("abcdef0123456789", masked)
        self.assertIn("<email>", masked)
        self.assertIn("<token>", masked)

    def test_masks_url_query(self):
        masked = payloads.mask_message(
            "GET https://example.test/callback?code=xyz&state=abc failed")

        self.assertNotIn("code=xyz", masked)
        self.assertIn("?<query>", masked)

    def test_masks_user_name(self):
        original = os.environ.get("USERNAME")
        os.environ["USERNAME"] = "evoluty"
        try:
            masked = payloads.mask_message("profile for evoluty is broken")
        finally:
            if original is None:
                os.environ.pop("USERNAME", None)
            else:
                os.environ["USERNAME"] = original

        self.assertNotIn("evoluty", masked)
        self.assertIn("<user>", masked)

    # 日本語の本文は壊さない (集計で読めなくなるため)
    def test_keeps_japanese_text(self):
        masked = payloads.mask_message("入力動画が見つかりません")

        self.assertEqual("入力動画が見つかりません", masked)

    # 改行は 1 行へ畳み、長すぎる本文は切る
    def test_truncates_and_flattens(self):
        masked = payloads.mask_message("a\nb\n" + "長い本文です。" * 200)

        self.assertEqual(payloads.MAX_MESSAGE_CHARS, len(masked))
        self.assertNotIn("\n", masked)

    def test_empty_becomes_none(self):
        self.assertIsNone(payloads.mask_message(""))
        self.assertIsNone(payloads.mask_message(None))


class BuildersTest(unittest.TestCase):

    # U10: イベント名の検証 (サーバーと同じ規則)
    def test_event_name_validation(self):
        self.assertTrue(payloads.is_valid_event_name("ExportSucceeded"))
        self.assertTrue(payloads.is_valid_event_name("Export.Succeeded-1_x"))
        self.assertFalse(payloads.is_valid_event_name("1Export"))
        self.assertFalse(payloads.is_valid_event_name("書き出し完了"))
        self.assertFalse(payloads.is_valid_event_name("Export Succeeded"))
        self.assertFalse(payloads.is_valid_event_name(""))

    def test_analytics_event_rejects_bad_name(self):
        self.assertIsNone(payloads.analytics_event("書き出し"))

    def test_analytics_event_body(self):
        path, body = payloads.analytics_event(
            payloads.EVENT_EXPORT_SUCCEEDED, duration_ms=1234.7,
            session_id="s1", project_id="p1")

        self.assertEqual("/api/analytics/events", path)
        self.assertEqual("ExportSucceeded", body["event"])
        self.assertEqual(1235, body["duration"])
        self.assertEqual("s1", body["sessionId"])
        self.assertEqual("p1", body["projectId"])
        self.assertIn("+00:00", body["time"])

    # 解像度は ^\d{1,5}x\d{1,5}$ でなければ本文を作らない
    def test_resolution(self):
        self.assertEqual("1920x1080", payloads.resolution(1920, 1080))
        self.assertIsNone(payloads.resolution(None, 1080))
        self.assertIsNone(payloads.resolution("abc", 1080))

    def test_project_upsert_requires_resolution(self):
        self.assertIsNone(payloads.project_upsert("p1", 10.0, "", 30, "libx264"))
        self.assertIsNone(payloads.project_upsert("", 10.0, "1920x1080", 30, "libx264"))

        path, body = payloads.project_upsert("p1", 12.3456, "1080x1920", 59.94, "libx264")
        self.assertEqual("/api/analytics/projects", path)
        self.assertEqual("1080x1920", body["resolution"])
        self.assertEqual(12.346, body["videoLength"])
        self.assertEqual(59.94, body["fps"])

    def test_export_log_body(self):
        path, body = payloads.export_log("p1", "1920x1080", "libx264", False, 1500.4)

        self.assertEqual("/api/analytics/export-logs", path)
        self.assertEqual(1500, body["duration"])
        self.assertFalse(body["success"])

    def test_export_log_requires_project(self):
        self.assertIsNone(payloads.export_log(None, "1920x1080", "libx264", True, 1))

    def test_processing_log_body(self):
        path, body = payloads.processing_log("p1", "Render", 2000, True)

        self.assertEqual("/api/analytics/processing-logs", path)
        self.assertEqual("Render", body["processName"])

    # 工程名は固定の英字名へ写す。未知のラベルは写さない (日本語を送らない)
    def test_process_names(self):
        self.assertEqual("Render", payloads.PROCESS_NAMES["Timeline レンダリング"])
        self.assertEqual("Transcribe", payloads.PROCESS_NAMES["音声認識"])
        self.assertNotIn("未知の工程", payloads.PROCESS_NAMES)

    def test_error_report_masks_message(self):
        path, body = payloads.error_report(
            "pipeline_runner", "FFmpegError",
            r"failed: C:\Users\evolu\a.mp4")

        self.assertEqual("/api/errors", path)
        self.assertEqual("pipeline_runner", body["module"])
        self.assertEqual("FFmpegError", body["exceptionType"])
        self.assertNotIn("evolu", body["message"])

    # ProjectId / SessionId / AppVersion はサーバーが保存しないため送らない (§3.5 / §13 Q4)
    def test_error_report_sends_only_stored_fields(self):
        _path, body = payloads.error_report("m", "E", "x")

        self.assertEqual({"module", "exceptionType", "message"}, set(body))

    def test_error_report_requires_module_and_type(self):
        self.assertIsNone(payloads.error_report("", "E", "x"))
        self.assertIsNone(payloads.error_report("m", "", "x"))

    def test_session_bodies(self):
        path, body = payloads.session_start("s1")
        self.assertEqual("/api/analytics/sessions", path)
        self.assertEqual("s1", body["sessionId"])

        path, body = payloads.session_end("s1", crash=True)
        self.assertEqual("/api/analytics/sessions/s1/end", path)
        self.assertTrue(body["crash"])

    def test_field_limits(self):
        _path, body = payloads.error_report("m" * 300, "E" * 400, "x")

        self.assertEqual(payloads.MAX_MODULE_CHARS, len(body["module"]))
        self.assertEqual(payloads.MAX_EXCEPTION_TYPE_CHARS, len(body["exceptionType"]))


if __name__ == "__main__":
    unittest.main()
