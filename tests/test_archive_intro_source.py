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
from src.archive import timeline_builder as archive_timeline
from src.exceptions import FFmpegError

_FFMPEG_CFG = {"video_codec": "libx264", "preset": "medium", "crf": 20,
               "audio_codec": "aac"}


def _touch(path, size=16):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"0" * size)
    return path


# Timeline の最小フェイク。sections_by_vod / intro_media_id / clip_range が
# 見るものだけを持つ (V1 クリップは「セクションが Timeline 上に在る」ことを表す)。
def _timeline(vod, clips, media_rows, live=None):
    rows = list(clips)
    track = types.SimpleNamespace(clips=[
        types.SimpleNamespace(
            origin={archive_timeline.ORIGIN_ARCHIVE_INDEX: entry.get("index")},
            timeline_start=pos * 10.0, timeline_end=pos * 10.0 + 10.0, enabled=True)
        for pos, entry in enumerate(rows)
        if live is None or entry.get("index") in live])
    return types.SimpleNamespace(
        source={"input_path": vod,
                "archive": {"vod_path": vod, "clips": rows,
                            "media": list(media_rows)}},
        base_video_track=lambda: track)


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

    def _dialog(self, keep=True, vod=None, buffer_sec=3.0, prepared_by_index=None,
                clips=None, media_rows=None, live=None):
        atd = self._atd
        # 控えは**セクション**単位で作る (ver9 resolve12 §3.2)。
        # 既定は「セクションと素材が 1 対 1」の素直な形。
        if clips is None:
            clips = [{"index": 1, "media_id": "m1", "vod_start": 100.0, "vod_end": 280.0},
                     {"index": 2, "media_id": "m2", "vod_start": 900.0, "vod_end": 1080.0}]
        if media_rows is None:
            media_rows = [{"media_id": "m1", "vod_start": 100.0, "vod_end": 280.0},
                          {"media_id": "m2", "vod_start": 900.0, "vod_end": 1080.0}]
        timeline = _timeline(self.vod if vod is None else vod, clips, media_rows,
                             live=live)
        media = [types.SimpleNamespace(id="m1", path="x"),
                 types.SimpleNamespace(id="m2", path="y")]

        class _Dialog:
            _keep_extra_beside_project = \
                atd.ArchiveTimelineDialog._keep_extra_beside_project
            # 元 VOD が無いときに「準備中の控え」を探す経路 (ver9 resolve5 §4.5)
            _copy_prepared_intro_source = \
                atd.ArchiveTimelineDialog._copy_prepared_intro_source

            def __init__(inner):
                inner.controller = types.SimpleNamespace(timeline=timeline)
                inner.preview = types.SimpleNamespace(set_status=lambda *_a: None)
                inner._project_cfg = {"archive_keep_intro_source": keep}
                # 取り直した区間の控え。既定は空 = 控えが無い状態
                inner._prepared_by_index = dict(prepared_by_index or {})
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

    # 元 VOD を手放した後の上書き保存では何もしない (既存の控えは残る)。
    # 取り直した区間の控えも無い場合であり、従来どおりの振る舞いになる。
    def test_skips_when_vod_is_already_gone(self):
        self._save(self._dialog(vod=os.path.join(self.root, "no_such.mp4")))
        self.assertEqual(self.exported, [])

    # 統合されたセクションは「開始を担当する素材」の名前で控える
    # (ver9 resolve12 §1.7)。代表 (m1) で控えると 177〜180 秒という
    # 無関係な位置がカードの背景になってしまう。
    def test_uses_the_media_that_owns_the_section_start(self):
        dialog = self._dialog(
            clips=[{"index": 1, "media_id": "m1",
                    "vod_start": 140.0, "vod_end": 540.0}],
            media_rows=[{"media_id": "m1", "vod_start": 180.0, "vod_end": 540.0},
                        {"media_id": "m10", "vod_start": 140.0, "vod_end": 180.0}])

        self._save(dialog)

        self.assertEqual(
            [(start, dest) for _vod, start, _buf, dest in self.exported],
            [(140.0, os.path.join(self.folder, "m10_intro.mp4"))])

    # 素材の控え (media) を持たない旧プロジェクトは代表へ落ちる = 従来どおり
    def test_falls_back_to_the_section_media_id(self):
        dialog = self._dialog(
            clips=[{"index": 1, "media_id": "m1",
                    "vod_start": 140.0, "vod_end": 540.0}],
            media_rows=[])

        self._save(dialog)

        self.assertEqual(
            [(start, dest) for _vod, start, _buf, dest in self.exported],
            [(140.0, os.path.join(self.folder, "m1_intro.mp4"))])

    # Timeline 上に実体が無いセクション (全クリップを消した) は書き出されないため控えない
    def test_skips_sections_without_clips_on_the_timeline(self):
        self._save(self._dialog(live={2}))

        self.assertEqual(
            [(start, dest) for _vod, start, _buf, dest in self.exported],
            [(900.0, os.path.join(self.folder, "m2_intro.mp4"))])

    # 元 VOD が無くても、取り直した区間の控えがあれば素材フォルダへ移す
    # (ver9 resolve5 §4.5)。これが無いと追加セクションのイントロが作れない。
    def test_uses_prepared_intro_source_without_vod(self):
        kept = _touch(os.path.join(self.root, "kept_intro.mp4"))
        dialog = self._dialog(
            vod=os.path.join(self.root, "no_such.mp4"),
            prepared_by_index={9: {"index": 9, "start": 100.0,
                                   "intro_source": kept}})
        self._save(dialog)
        # 元 VOD が無いため切り出しは起きない (控えを写すだけ)
        self.assertEqual(self.exported, [])
        self.assertTrue(os.path.exists(
            self._atd.clip_writer.intro_source_path(self.folder, "m1")))
        # 控えを持たない m2 は作られない
        self.assertFalse(os.path.exists(
            self._atd.clip_writer.intro_source_path(self.folder, "m2")))


class SaveProjectWiringTest(unittest.TestCase):

    # 保存フックが**呼ばれるか**だけを検査する (ver9 resolve12 §6.1)
    #
    # 今回の不具合は「フックはあるのに save_project から呼ばれていない」ことだった。
    # 中身の試験 (KeepIntroSourceOnSaveTest) は関数を直接呼ぶため配線を見ておらず、
    # 全件緑のまま機能が死んでいた。ここで配線そのものを押さえる。
    def setUp(self):
        from src.gui.timeline import timeline_editor_dialog as ted

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = os.path.join(self._tmp.name, "proj.timeline.json")
        self.order = []
        patcher = mock.patch.object(
            ted.project_io, "save",
            side_effect=lambda *_a, **_k: self.order.append("save"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ted, "project_thumbnail", mock.MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self._ted = ted

    def _dialog(self, modified=True):
        ted = self._ted
        outer = self

        class _Dialog:
            save_project = ted.TimelineEditorDialog.save_project
            _on_autosave = ted.TimelineEditorDialog._on_autosave

            def __init__(inner):
                inner.extra_calls = []
                inner._project_path = outer.target
                inner._settings = {"ffmpeg": _FFMPEG_CFG}
                inner._created_at = "2026-01-01T00:00:00"
                inner._project_cfg = {"keep_media": False, "keep_audio": False,
                                      "keep_blur_cache": False, "recent_limit": 10}
                inner.controller = types.SimpleNamespace(
                    timeline=object(), mark_saved=lambda: None,
                    is_modified=lambda: modified,
                    cfg={"project_suffix": ".timeline.json"})
                inner.preview = types.SimpleNamespace(set_status=lambda *_a: None)

            def _keep_media_for_kind(inner):
                return False

            def _keep_extra_beside_project(inner, project_path):
                inner.extra_calls.append(project_path)
                outer.order.append("extra")

            def _autosave_path(inner):
                return os.path.join(outer._tmp.name, "proj.autosave.json")

            def _update_window_title(inner):
                pass

            def _remember_recent(inner, path):
                pass

            def _discard_autosave(inner):
                pass

        return _Dialog()

    # 本題: 本体の保存でフックが呼ばれる (これが抜けていた)
    def test_save_project_calls_the_extra_hook(self):
        dialog = self._dialog()

        self.assertTrue(dialog.save_project())

        self.assertEqual(len(dialog.extra_calls), 1)
        self.assertEqual(dialog.extra_calls[0], dialog._project_path)

    # JSON を書く前に呼ぶ (控えを作ってから保存を確定する)
    def test_hook_runs_before_the_json_is_written(self):
        dialog = self._dialog()

        dialog.save_project()

        self.assertEqual(self.order, ["extra", "save"])

    # 自動保存では呼ばない (60 秒ごとに ffmpeg を回さない)
    def test_autosave_does_not_call_the_hook(self):
        dialog = self._dialog()

        dialog._on_autosave()

        self.assertEqual(dialog.extra_calls, [])
        self.assertEqual(self.order, ["save"])

    # クリップ用 (既定の実装) は何もしないだけで例外にならない
    def test_default_hook_is_a_no_op(self):
        self.assertIsNone(
            self._ted.TimelineEditorDialog._keep_extra_beside_project(
                object(), self.target))


if __name__ == "__main__":
    unittest.main()
