# イントロカードの素を保存して元 VOD 無しでも作れることの単体テスト
# (docs/error/fromUser/resolve.md §9-7)
# 実行: python -m unittest discover -s tests
#
# 背景:
#   イントロカードは「決定ボタン押下後」の書き出しで作るが、素は**元 VOD の
#   クリップ開始直前 buffer_sec 秒**である。この区間はクリップ素材 (vod_start
#   始まり) に入っていないため、§6-6 で元 VOD を手放せるようにした結果、
#   開き直して書き出すとイントロカードだけが作れなくなっていた。
#
# 重点:
#   ・保存時に <素材フォルダ>/<media_id>_intro.mp4 を作ること (再エンコードで
#     フレーム正確に buffer_sec 秒ぶん)。2 回目の保存で作り直さないこと
#   ・元 VOD が無いとき _build_intro_card がその控えを使うこと
#   ・元 VOD があるときは従来どおり元 VOD の時刻で切ること (新規実行)
#   ・控えも元 VOD も無いときは例外にせず省くこと
#   ・イントロの控えだけを自己完結と誤判定しないこと (元 VOD を誤って捨てない)
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import clip_writer, vod_retention
from src.exceptions import FFmpegError

_FFMPEG_CFG = {"video_codec": "libx264", "preset": "medium", "crf": 20,
               "audio_codec": "aac"}


def _touch(path, size=16):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"0" * size)
    return path


class IntroSourcePathTest(unittest.TestCase):

    def test_path_is_media_id_plus_suffix(self):
        self.assertEqual(
            clip_writer.intro_source_path(os.path.join("x", "proj.media"), "m3"),
            os.path.join("x", "proj.media", "m3_intro.mp4"))

    def test_path_is_empty_without_folder_or_id(self):
        self.assertEqual(clip_writer.intro_source_path("", "m1"), "")
        self.assertEqual(clip_writer.intro_source_path("x", ""), "")

    def test_existing_ignores_empty_file(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "m1_intro.mp4")
            self.assertEqual(clip_writer.existing_intro_source(folder, "m1"), "")
            open(dest, "wb").close()        # 0 バイト = 使えない
            self.assertEqual(clip_writer.existing_intro_source(folder, "m1"), "")
            _touch(dest)
            self.assertEqual(clip_writer.existing_intro_source(folder, "m1"), dest)


class ExportIntroSourceTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.vod = _touch(os.path.join(self.root, "vod_1.mp4"))
        self.dest = os.path.join(self.root, "proj.media", "m1_intro.mp4")

    # ffmpeg の呼び出しを控え、実際には空ファイルを作る
    def _run(self, clip_start, buffer_sec, vod=None):
        calls = []

        def fake_execute(cmd, total_duration=None):
            calls.append(list(cmd))
            _touch(cmd[-1])

        with mock.patch("src.modules.ffmpeg_runner.execute",
                        side_effect=fake_execute), \
                mock.patch("src.modules.ffmpeg_runner.get_ffmpeg_exe",
                           return_value="ffmpeg"):
            result = clip_writer.export_intro_source(
                self.vod if vod is None else vod, clip_start, buffer_sec,
                self.dest, _FFMPEG_CFG)
        return result, calls

    # クリップ開始の直前 buffer_sec 秒を**再エンコードで**取る (フレーム正確)
    def test_cuts_the_buffer_before_the_clip_start(self):
        result, calls = self._run(100.0, 3.0)

        self.assertEqual(result, self.dest)
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("-ss") + 1], "97.000")
        self.assertEqual(cmd[cmd.index("-i") + 1], self.vod)
        self.assertEqual(cmd[cmd.index("-t") + 1], "3.000")
        # -c copy ではキーフレームまでしか戻れないため再エンコードであること
        self.assertIn("-c:v", cmd)
        self.assertNotIn("copy", cmd)

    # VOD の先頭でバッファを確保できないクリップは作らない
    def test_skips_when_buffer_does_not_fit(self):
        result, calls = self._run(1.0, 3.0)

        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.dest))

    def test_skips_when_buffer_is_zero(self):
        result, calls = self._run(100.0, 0.0)
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_skips_when_vod_is_gone(self):
        result, calls = self._run(100.0, 3.0, vod=os.path.join(self.root, "no.mp4"))
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    # ffmpeg が失敗しても保存は続けられる (None を返すだけ)
    def test_failure_leaves_no_file(self):
        def fail(cmd, total_duration=None):
            _touch(cmd[-1])
            raise FFmpegError("boom")

        with mock.patch("src.modules.ffmpeg_runner.execute", side_effect=fail), \
                mock.patch("src.modules.ffmpeg_runner.get_ffmpeg_exe",
                           return_value="ffmpeg"):
            result = clip_writer.export_intro_source(
                self.vod, 100.0, 3.0, self.dest, _FFMPEG_CFG)

        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.dest))


class BuildIntroCardSourceTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.workdir = os.path.join(self.root, "work")
        os.makedirs(self.workdir, exist_ok=True)
        self.card = {
            "enabled": True, "buffer_sec": 3.0, "blur_sigma": 18.0,
            "margin_top_px": 300, "margin_bottom_px": 300,
            "box_color": "black", "box_opacity": 1.0,
            "title_font_family": "", "title_font_size": 112,
            "title_color": "#FFFFFF", "tag_font_family": "",
            "tag_font_size": 40, "tag_color": "#FFFFFF", "tag_bg_opacity": 0.45,
            "tag_margin_l": 40, "tag_margin_v": 30, "mute_intro": False,
        }

    def _build(self, source, intro_source=""):
        calls = []

        def fake_execute(cmd, total_duration=None):
            calls.append(list(cmd))
            _touch(cmd[-1])

        with mock.patch("src.modules.ffmpeg_runner.execute",
                        side_effect=fake_execute), \
                mock.patch("src.modules.ffmpeg_runner.get_ffmpeg_exe",
                           return_value="ffmpeg"):
            result = clip_writer._build_intro_card(
                source, 100.0, "テーマ", self.card, {}, _FFMPEG_CFG, "",
                self.workdir, 1, intro_source=intro_source)
        return result, calls

    # 新規実行: 元 VOD があるなら従来どおり VOD 内の時刻で切る
    def test_prefers_the_vod_when_it_exists(self):
        vod = _touch(os.path.join(self.root, "vod_1.mp4"))
        intro = _touch(os.path.join(self.root, "proj.media", "m1_intro.mp4"))

        result, calls = self._build(vod, intro_source=intro)

        self.assertIsNotNone(result)
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("-i") + 1], vod)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "97.000")

    # 本題: 元 VOD が無ければ控えを先頭から使う (-ss を付けない)
    def test_falls_back_to_the_saved_intro_source(self):
        gone = os.path.join(self.root, "vod_1.mp4")
        intro = _touch(os.path.join(self.root, "proj.media", "m1_intro.mp4"))

        result, calls = self._build(gone, intro_source=intro)

        self.assertIsNotNone(result)
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("-i") + 1], intro)
        self.assertNotIn("-ss", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "3.000")

    # どちらも無ければ例外にせず省く (本編は出せる)
    def test_skips_without_any_source(self):
        result, calls = self._build(os.path.join(self.root, "vod_1.mp4"))

        self.assertIsNone(result)
        self.assertEqual(calls, [])


class SelfContainedIgnoresIntroSourceTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.project = _touch(os.path.join(self.root, "vod_1.archive.timeline.json"))
        self.media_dir = os.path.join(self.root, "vod_1.archive.media")
        os.makedirs(self.media_dir, exist_ok=True)
        self.settings = {
            "timeline": {"project_suffix": ".timeline.json",
                         "project": {"media_dir_suffix": ".media"}},
        }

    # イントロの控えだけでは自己完結ではない (まだ元 VOD が要る)
    def test_intro_source_alone_is_not_self_contained(self):
        _touch(os.path.join(self.media_dir, "m1_intro.mp4"))
        self.assertFalse(
            vod_retention.is_self_contained(self.project, self.settings))

    # クリップ素材が在れば従来どおり自己完結
    def test_media_makes_it_self_contained(self):
        _touch(os.path.join(self.media_dir, "m1_intro.mp4"))
        _touch(os.path.join(self.media_dir, "m1_normalized.mp4"))
        self.assertTrue(
            vod_retention.is_self_contained(self.project, self.settings))


class KeepIntroSourceOnSaveTest(unittest.TestCase):

    # 保存フック (_keep_extra_beside_project) だけを取り出して試す (Qt を起動しない)
    def setUp(self):
        from src.gui.timeline import archive_timeline_dialog as atd

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.vod = _touch(os.path.join(self.root, "vod_1.mp4"))
        self.folder = os.path.join(self.root, "proj.media")
        patcher = mock.patch.object(atd, "QApplication", mock.MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.exported = []

        def fake_export(vod, clip_start, buffer_sec, dest, ffmpeg_cfg):
            self.exported.append((vod, clip_start, buffer_sec, dest))
            return _touch(dest)

        patcher = mock.patch.object(atd.clip_writer, "export_intro_source",
                                    side_effect=fake_export)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._atd = atd

    def _dialog(self, keep=True, vod=None, buffer_sec=3.0):
        atd = self._atd
        timeline = types.SimpleNamespace(source={
            "input_path": self.vod if vod is None else vod,
            "archive": {
                "vod_path": self.vod if vod is None else vod,
                "clips": [],
                "media": [
                    {"media_id": "m1", "vod_start": 100.0, "vod_end": 280.0},
                    {"media_id": "m2", "vod_start": 900.0, "vod_end": 1080.0},
                ],
            },
        })
        media = [types.SimpleNamespace(id="m1", path="x"),
                 types.SimpleNamespace(id="m2", path="y")]

        class _Dialog:
            _keep_extra_beside_project = \
                atd.ArchiveTimelineDialog._keep_extra_beside_project

            def __init__(inner):
                inner.controller = types.SimpleNamespace(timeline=timeline)
                inner.preview = types.SimpleNamespace(set_status=lambda *_a: None)
                inner._project_cfg = {"archive_keep_intro_source": keep}
                inner._settings = {
                    "ffmpeg": _FFMPEG_CFG,
                    "archive": {"intro_card": {"enabled": True,
                                               "buffer_sec": buffer_sec}},
                }

            def _media_to_copy(inner):
                return media

            def _media_dir(inner, _project_path):
                return self.folder

        return _Dialog()

    def _save(self, dialog):
        dialog._keep_extra_beside_project(
            os.path.join(self.root, "proj.timeline.json"))

    # 本題: 素材 1 本ごとに控えを作る
    def test_exports_one_intro_source_per_media(self):
        self._save(self._dialog())

        self.assertEqual([(start, dest) for _vod, start, _buf, dest in self.exported],
                         [(100.0, os.path.join(self.folder, "m1_intro.mp4")),
                          (900.0, os.path.join(self.folder, "m2_intro.mp4"))])

    # 2 回目の保存では作り直さない (保存のたびに ffmpeg を回さない)
    def test_does_not_rebuild_on_the_second_save(self):
        dialog = self._dialog()
        self._save(dialog)
        self.exported.clear()

        self._save(dialog)

        self.assertEqual(self.exported, [])

    # 設定で切れる
    def test_setting_can_turn_it_off(self):
        self._save(self._dialog(keep=False))
        self.assertEqual(self.exported, [])

    # イントロカードを使わない設定なら作らない
    def test_skips_when_buffer_is_zero(self):
        self._save(self._dialog(buffer_sec=0.0))
        self.assertEqual(self.exported, [])

    # 元 VOD を手放した後の上書き保存では何もしない (既存の控えは残る)
    def test_skips_when_vod_is_already_gone(self):
        self._save(self._dialog(vod=os.path.join(self.root, "no_such.mp4")))
        self.assertEqual(self.exported, [])


if __name__ == "__main__":
    unittest.main()
