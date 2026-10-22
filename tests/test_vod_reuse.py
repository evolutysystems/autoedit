# 取得済み VOD の再利用判定の単体テスト (docs/error/fromUser/resolve.md §7)
# 実行: python -m unittest discover -s tests
#
# 背景 (2026-10-22 の利用者報告):
#   twitch-dl の --skip-existing は「存在するか」しか見ないため、
#   前回の取得が容量不足で途中終了して壊れた vod_*.mp4 が残っていると
#   「取得済み」として素通りし、採点の最初の ffprobe で
#   「処理に失敗しました。ffprobeが失敗。」として現れていた。
#
# 重点:
#   ・壊れた取得物は削除して取得し直すこと (ffprobe の失敗まで進ませない)
#   ・健全な取得物はそのまま再利用すること (従来の利点を失わない)
#   ・取得後の検証に失敗したら採点へ進ませないこと
#   ・断片キャッシュを work_dir 配下へ置き、失敗時に片づけること
import os
import tempfile
import unittest
from unittest import mock

from src.archive import twitch_source
from src.exceptions import DiskSpaceError, FFmpegError, TwitchError

_FFMPEG_CFG = {"ffprobe_executable": "ffprobe"}


def _write(path, size=1024):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"x" * size)
    return path


class DownloadVodReuseTest(unittest.TestCase):

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.work_dir = self._temp.name
        self.expected = os.path.join(self.work_dir, "vod_123.mp4")

    def tearDown(self):
        self._temp.cleanup()

    # 健全な取得物があれば twitch-dl を動かさずそのまま使う
    def test_reuses_a_healthy_download(self):
        _write(self.expected)
        with mock.patch.object(twitch_source, "_run") as run, \
                mock.patch("src.modules.ffmpeg_runner.probe_duration", return_value=120.0):
            result = twitch_source.download_vod(
                "123", self.work_dir, ffmpeg_cfg=_FFMPEG_CFG)
        self.assertEqual(result, self.expected)
        run.assert_not_called()

    # 0 バイトの残骸は消して取得し直す
    def test_empty_file_is_discarded_and_downloaded_again(self):
        _write(self.expected, size=0)

        def _fake_run(*_args, **_kwargs):
            _write(self.expected)        # 取得できたことにする
            return []

        with mock.patch.object(twitch_source, "_run", side_effect=_fake_run) as run, \
                mock.patch.object(twitch_source, "list_qualities", return_value=[]), \
                mock.patch("src.modules.ffmpeg_runner.probe_duration", return_value=120.0):
            result = twitch_source.download_vod(
                "123", self.work_dir, ffmpeg_cfg=_FFMPEG_CFG)
        self.assertEqual(result, self.expected)
        run.assert_called_once()

    # ffprobe が通らない (壊れた) 取得物も消して取得し直す
    def test_corrupted_file_is_discarded_and_downloaded_again(self):
        _write(self.expected)
        probe_results = [FFmpegError("ffprobe が失敗"), 120.0]

        def _probe(*_args, **_kwargs):
            value = probe_results.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        def _fake_run(*_args, **_kwargs):
            _write(self.expected)        # 取り直しで健全なファイルができる
            return []

        with mock.patch.object(twitch_source, "_run", side_effect=_fake_run) as run, \
                mock.patch.object(twitch_source, "list_qualities", return_value=[]), \
                mock.patch("src.modules.ffmpeg_runner.probe_duration", side_effect=_probe):
            result = twitch_source.download_vod(
                "123", self.work_dir, ffmpeg_cfg=_FFMPEG_CFG)
        self.assertEqual(result, self.expected)
        run.assert_called_once()

    # 取得した「ことになっている」ファイルが壊れていたら採点へ進ませない
    def test_broken_result_after_download_is_rejected(self):
        def _fake_run(*_args, **_kwargs):
            _write(self.expected)
            return []

        with mock.patch.object(twitch_source, "_run", side_effect=_fake_run), \
                mock.patch.object(twitch_source, "list_qualities", return_value=[]), \
                mock.patch("src.modules.ffmpeg_runner.probe_duration",
                           side_effect=FFmpegError("ffprobe が失敗")):
            with self.assertRaises(TwitchError):
                twitch_source.download_vod("123", self.work_dir, ffmpeg_cfg=_FFMPEG_CFG)
        # 壊れたファイルは残さない (次回また素通りさせないため)
        self.assertFalse(os.path.exists(self.expected))

    # --skip-existing は使わず、断片キャッシュは work_dir 配下へ置く
    def test_command_uses_managed_cache_dir(self):
        captured = {}

        def _fake_run(argv, *_args, **_kwargs):
            captured["argv"] = argv
            _write(self.expected)
            return []

        with mock.patch.object(twitch_source, "_run", side_effect=_fake_run), \
                mock.patch.object(twitch_source, "list_qualities", return_value=[]), \
                mock.patch("src.modules.ffmpeg_runner.probe_duration", return_value=120.0):
            twitch_source.download_vod("123", self.work_dir, ffmpeg_cfg=_FFMPEG_CFG)

        argv = captured["argv"]
        self.assertNotIn("--skip-existing", argv)
        self.assertIn("--cache-dir", argv)
        cache_dir = argv[argv.index("--cache-dir") + 1]
        self.assertTrue(cache_dir.startswith(os.path.join(self.work_dir, "cache")))

    # 取得に失敗したら断片キャッシュを片づける (48 GB 滞留の再発防止)
    def test_cleans_cache_on_failure(self):
        cache_dir = os.path.join(self.work_dir, "cache", "123")
        _write(os.path.join(cache_dir, "chunked", "part.ts"), size=2048)

        with mock.patch.object(twitch_source, "_run",
                               side_effect=TwitchError("失敗")), \
                mock.patch.object(twitch_source, "list_qualities", return_value=[]):
            with self.assertRaises(TwitchError):
                twitch_source.download_vod("123", self.work_dir, ffmpeg_cfg=_FFMPEG_CFG)
        self.assertFalse(os.path.exists(cache_dir))

    # 設定で残すこともできる (再試行を速くしたい利用者向け)
    def test_keeps_cache_when_configured(self):
        cache_dir = os.path.join(self.work_dir, "cache", "123")
        _write(os.path.join(cache_dir, "chunked", "part.ts"))

        with mock.patch.object(twitch_source, "_run",
                               side_effect=TwitchError("失敗")), \
                mock.patch.object(twitch_source, "list_qualities", return_value=[]):
            with self.assertRaises(TwitchError):
                twitch_source.download_vod("123", self.work_dir, ffmpeg_cfg=_FFMPEG_CFG,
                                           keep_cache_on_error=True)
        self.assertTrue(os.path.exists(cache_dir))

    # 容量不足は包み直さず DiskSpaceError のまま伝える
    def test_disk_space_error_is_not_wrapped(self):
        with mock.patch.object(twitch_source, "_run",
                               side_effect=DiskSpaceError("空き容量が足りません")), \
                mock.patch.object(twitch_source, "list_qualities", return_value=[]):
            with self.assertRaises(DiskSpaceError):
                twitch_source.download_vod("123", self.work_dir, ffmpeg_cfg=_FFMPEG_CFG)

    # 取得前に空きが足りなければ 1 バイトも書かずに止める
    def test_checks_free_space_before_downloading(self):
        with mock.patch.object(twitch_source, "_run") as run, \
                mock.patch("src.utils.disk.free_bytes", return_value=1024):
            with self.assertRaises(DiskSpaceError):
                twitch_source.download_vod(
                    "123", self.work_dir, ffmpeg_cfg=_FFMPEG_CFG,
                    min_free_bytes=20 * 1024 ** 3)
        run.assert_not_called()


class ChildExitTest(unittest.TestCase):

    # 子プロセスの容量不足は DiskSpaceError へ変換する (TwitchError にしない)
    def test_child_disk_full_exit_code(self):
        proc = mock.Mock()
        proc.stdout = iter(["__TWITCHDL_ERROR__ OSError: [Errno 28] No space left on device"])
        proc.wait.return_value = twitch_source._EXIT_DISK_FULL
        with mock.patch("subprocess.Popen", return_value=proc):
            with self.assertRaises(DiskSpaceError):
                twitch_source._run(["x", "download", "--output", os.getcwd()])

    # それ以外の失敗は従来どおり TwitchError (子が残した 1 行を添える)
    def test_child_other_error_keeps_twitch_error(self):
        proc = mock.Mock()
        proc.stdout = iter(["__TWITCHDL_ERROR__ RuntimeError: boom"])
        proc.wait.return_value = 90
        with mock.patch("subprocess.Popen", return_value=proc):
            with self.assertRaises(TwitchError) as caught:
                twitch_source._run(["x", "download", "--output", os.getcwd()])
        self.assertIn("RuntimeError: boom", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
