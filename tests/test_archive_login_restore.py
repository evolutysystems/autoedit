# 保存済み Twitch トークンからのログイン状態の復元 (src/gui/archive_tab.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・起動時に画面を止めない (Helix の問い合わせはワーカースレッドで行う)
#   ・復元できたら自 VOD 一覧まで入れる (入れないとログイン済みでも毎回ログインし直すことになる)
#   ・失効・回線断では未ログイン表示に戻し、画面は開いたままにする
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from src.gui import archive_tab as archive_tab_module
from src.gui.archive_tab import ArchiveTabWidget

_VIDEOS = [
    {"id": "1", "title": "配信その1", "url": "https://www.twitch.tv/videos/1",
     "created_at": "2026-09-24T10:00:00Z"},
    {"id": "2", "title": "配信その2", "url": "https://www.twitch.tv/videos/2",
     "created_at": "2026-09-23T10:00:00Z"},
]


# ネットワークへ出ない TwitchAuth の代役
class _FakeAuth:

    def __init__(self, logged_in=True, fail=None, videos=None, marker=True, **_kwargs):
        self._logged_in = logged_in
        self._fail = fail
        self._videos = _VIDEOS if videos is None else videos
        self._marker = marker
        self.self_calls = 0
        self.video_calls = 0

    def is_logged_in(self):
        return self._logged_in

    def has_scope(self, _scope):
        return self._marker

    def get_self(self):
        self.self_calls += 1
        if self._fail:
            raise RuntimeError(self._fail)
        return {"id": "42", "login": "tester", "display_name": "テスト配信者"}

    def list_own_videos(self, first=20):
        self.video_calls += 1
        return list(self._videos)[:first]


class ArchiveLoginRestoreTest(unittest.TestCase):

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self._original = archive_tab_module.TwitchAuth
        self.addCleanup(lambda: setattr(archive_tab_module, "TwitchAuth", self._original))

    def _build(self, **kwargs):
        created = {}

        def _factory(**auth_kwargs):
            created["auth"] = _FakeAuth(**dict(kwargs, **auth_kwargs))
            return created["auth"]

        archive_tab_module.TwitchAuth = _factory
        tab = ArchiveTabWidget()
        self.addCleanup(tab.deleteLater)
        return tab, created["auth"]

    # ワーカーが終わるまで待つ (シグナルは GUI スレッドへ届くため待ち合わせが要る)
    def _wait(self, tab):
        worker = tab._restore_worker
        self.assertIsNotNone(worker, "復元のワーカーが起動していません")
        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        QTimer.singleShot(5000, loop.quit)
        if worker.isRunning():
            loop.exec()
        self.app.processEvents()

    # 起動時は問い合わせ中の表示にし、GUI スレッドで Helix を呼ばないこと
    def test_startup_does_not_block(self):
        tab, auth = self._build()

        self.assertEqual(tab.login_status.text(), "ログイン状態を確認中…")
        self._wait(tab)
        self.assertEqual(auth.self_calls, 1)

    # 復元できたらログイン表示になり、自 VOD 一覧まで入ること
    def test_restores_the_session_and_the_vod_list(self):
        tab, _auth = self._build()
        self._wait(tab)

        self.assertEqual(tab.login_status.text(), "ログイン中: テスト配信者")
        # 先頭の案内 + VOD 2 件
        self.assertEqual(tab.vod_combo.count(), 3)
        self.assertEqual(tab.vod_combo.itemText(0), "(自分のVODを選択)")
        self.assertIn("配信その1", tab.vod_combo.itemText(1))
        self.assertEqual(tab.vod_combo.itemData(1), "https://www.twitch.tv/videos/1")

    # 失効・回線断では未ログイン表示に戻すこと (画面は開いたまま)
    def test_failure_falls_back_to_logged_out(self):
        tab, auth = self._build(fail="テスト: トークンが失効")
        self._wait(tab)

        self.assertEqual(tab.login_status.text(), "未ログイン")
        self.assertEqual(auth.video_calls, 0, "失敗後に一覧を引こうとしています")
        self.assertEqual(tab.vod_combo.count(), 1)

    # 未ログインなら問い合わせに行かないこと
    def test_no_request_when_logged_out(self):
        tab, auth = self._build(logged_in=False)

        self.assertIsNone(tab._restore_worker)
        self.assertEqual(auth.self_calls, 0)
        self.assertEqual(tab.login_status.text(), "未ログイン")

    # VOD が 0 件なら案内文のままにする (空のプルダウンにしない)
    def test_empty_vod_list_keeps_the_placeholder(self):
        tab, _auth = self._build(videos=[])
        self._wait(tab)

        self.assertEqual(tab.login_status.text(), "ログイン中: テスト配信者")
        self.assertEqual(tab.vod_combo.count(), 1)


class LoginButtonTest(ArchiveLoginRestoreTest):

    # セッションが生きているあいだはログインボタンを押せないこと
    # (押せると、ログイン済みでも毎回押してしまい許可画面が出る)
    def test_disabled_while_the_session_is_alive(self):
        tab, _auth = self._build()

        # 確認中も押せない (二重ログインを防ぐ)
        self.assertFalse(tab.login_button.isEnabled())
        self._wait(tab)
        self.assertFalse(tab.login_button.isEnabled())
        self.assertIn("ログイン済み", tab.login_button.toolTip())

    # 未ログインなら押せること
    def test_enabled_when_logged_out(self):
        tab, _auth = self._build(logged_in=False)

        self.assertTrue(tab.login_button.isEnabled())

    # 復元できなかったら押せるように戻すこと
    def test_enabled_again_after_a_failed_restore(self):
        tab, _auth = self._build(fail="テスト: トークンが失効")
        self._wait(tab)

        self.assertTrue(tab.login_button.isEnabled())

    # マーカーの権限が無い古いトークンでは、再ログインが唯一の直し方なので押せること
    def test_enabled_when_the_marker_scope_is_missing(self):
        tab, _auth = self._build(marker=False)
        self._wait(tab)

        self.assertEqual(tab.login_status.text(), "ログイン中: テスト配信者（マーカー未許可）")
        self.assertTrue(tab.login_button.isEnabled())
        self.assertIn("再ログイン", tab.login_button.toolTip())

    # 実行が終わっても、ログイン中なら押せないままであること
    def test_stays_disabled_after_a_run(self):
        tab, _auth = self._build()
        self._wait(tab)

        tab._set_running(True)
        self.assertFalse(tab.login_button.isEnabled())
        tab._set_running(False)
        self.assertFalse(tab.login_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
