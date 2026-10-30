# 縦動画プロジェクトの作成画面 (src/gui/timeline/) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve9.md §8.1):
#   ・枠が上限・下限・比率・ソースの内側に収まること (R11 / R16)
#   ・「作成」でクリップ用の縦プロジェクトが書き出されること (R6 / R14)
#   ・対象外の Timeline ではメニューを出さないこと (§10 #10)
#   ・アーカイブ用は素材を元 VOD へ張り替えて書き出すこと (resolve10 §3.1)
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.gui.timeline.crop_canvas import CropCanvas
from src.timeline import crop, project_io
from src.timeline.model import (
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    Timeline,
    Track,
)

_CANVAS = (1080, 1920)
_SOURCE = (1920, 1080)


def _app():
    return QApplication.instance() or QApplication([])


def _timeline(portrait=False, archive=False):
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    width, height = (1080, 1920) if portrait else (1920, 1080)
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 0.0, 5.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 5.0, 4.0, 20.0, 24.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1"), AudioClip("a2", "c2")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    source = {"media_id": "m1", "input_path": os.path.abspath(__file__)}
    if archive:
        source["archive"] = {"vod_path": os.path.abspath(__file__),
                             "media": [{"media_id": "m1", "vod_start": 100.0,
                                        "vod_end": 160.0}]}
    return Timeline(fps=60, width=width, height=height,
                    orientation="portrait" if portrait else "landscape",
                    source=source, media_pool=media, tracks=[video, audio, subtitle])


class CropCanvasTest(unittest.TestCase):

    def setUp(self):
        _app()
        self.canvas = CropCanvas()
        self.canvas.setup(_SOURCE, _CANVAS, 2.0)
        self.addCleanup(self.canvas.deleteLater)

    # 全体の既定枠はソース中央の正方形 (上限 1080x1080 / R4)
    def test_single_default(self):
        self.canvas.set_mode(crop.MODE_SINGLE)
        frames = self.canvas.frames()

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][2:], (1080, 1080))
        self.assertEqual(frames[0][0], (1920 - 1080) // 2)

    # 分割は枠 2 つで、それぞれ比率が固定されること (R5)
    def test_split_keeps_the_aspect(self):
        self.canvas.set_mode(crop.MODE_SPLIT)
        frames = self.canvas.frames()

        self.assertEqual(len(frames), 2)
        for index, rect in enumerate(frames):
            expected = crop.aspect(crop.MODE_SPLIT, index, *_CANVAS)
            self.assertAlmostEqual(rect[2] / rect[3], expected, places=2)

    # 大きすぎる枠はソースへ収まるまで縮むこと
    def test_oversized_frame_is_clamped(self):
        self.canvas.set_mode(crop.MODE_SINGLE)
        self.canvas.set_frames([(0, 0, 4000, 4000)])
        x, y, width, height = self.canvas.frames()[0]

        self.assertLessEqual(x + width, _SOURCE[0])
        self.assertLessEqual(y + height, _SOURCE[1])

    # 小さすぎる枠は拡大率の上限で止まること (R16)
    def test_undersized_frame_is_clamped(self):
        self.canvas.set_mode(crop.MODE_SPLIT)
        self.canvas.set_frames([(0, 0, 10, 10), (0, 0, 10, 10)])

        for index, rect in enumerate(self.canvas.frames()):
            min_w, min_h = crop.min_src_size(crop.MODE_SPLIT, index, *_CANVAS, 2.0)
            self.assertGreaterEqual(rect[2], min_w)
            self.assertGreaterEqual(rect[3], min_h)

    # ソースの外へは出せないこと
    def test_frame_stays_inside_the_source(self):
        self.canvas.set_mode(crop.MODE_SINGLE)
        self.canvas.set_frames([(5000, 5000, 1080, 1080)])
        x, y, width, height = self.canvas.frames()[0]

        self.assertEqual((x + width, y + height), _SOURCE)

    # 拡大させない設定では、枠はキャンバスと同じ大きさより小さくできないこと
    def test_max_scale_one(self):
        self.canvas.setup(_SOURCE, _CANVAS, 1.0)
        self.canvas.set_mode(crop.MODE_SPLIT)
        self.canvas.set_frames([(0, 0, 100, 100), (0, 0, 100, 100)])
        top = self.canvas.frames()[0]

        self.assertGreaterEqual(top[2], 1080)


# FFmpeg を起動せず、単色のフレームを返す差し替え
class _FakeFrameSource:

    def frame_at(self, _media, _source_sec):
        width, height = _SOURCE
        return (width, height, bytes(width * height * 3))

    def close(self):
        pass


class VerticalProjectDialogTest(unittest.TestCase):

    def setUp(self):
        _app()
        self.timeline = _timeline()

    def _dialog(self, settings=None):
        from src.gui.timeline import vertical_project_dialog as module

        class _Controller:
            def __init__(self, timeline):
                self.timeline = timeline

        # 実素材をデコードしない (テストの入力は .py ファイルのため)
        original = module.create_frame_source
        module.create_frame_source = lambda *_a, **_k: _FakeFrameSource()
        try:
            dialog = module.VerticalProjectDialog(
                _Controller(self.timeline), self.timeline.base_video_track().clips,
                settings or {}, parent=None)
        finally:
            module.create_frame_source = original
        self.addCleanup(dialog.deleteLater)
        return dialog

    # 開いた直後は「全体」で、縦キャンバスの仕上がりが出ること
    def test_opens_with_single_mode(self):
        dialog = self._dialog()

        self.assertTrue(dialog.single_radio.isChecked())
        self.assertEqual(dialog.canvas.mode(), crop.MODE_SINGLE)
        self.assertIsNotNone(dialog.preview_label.pixmap())

    # 分割へ切り替えると枠が 2 つになり、背景の選択が無効になること
    def test_switch_to_split(self):
        dialog = self._dialog()
        dialog.split_radio.setChecked(True)

        self.assertEqual(dialog.canvas.mode(), crop.MODE_SPLIT)
        self.assertEqual(len(dialog.canvas.frames()), 2)
        self.assertFalse(dialog.background_combo.isEnabled())

    # 完了・確認のダイアログを出さずに「作成」を実行する
    def _create(self, dialog, dest):
        from src.gui.timeline import vertical_project_dialog as module

        shown = []
        original = module.QMessageBox
        module.QMessageBox = type("_Box", (), {
            "information": staticmethod(lambda *a, **k: shown.append(a[2])),
            "warning": staticmethod(lambda *a, **k: shown.append(a[2])),
            "critical": staticmethod(lambda *a, **k: shown.append(a[2])),
            "question": staticmethod(lambda *a, **k: 0),
            "Yes": 0, "No": 1,
        })
        try:
            dialog._dest_path = dest
            dialog._on_create()
        finally:
            module.QMessageBox = original
        return shown

    # 「作成」でクリップ用の縦プロジェクトが書き出されること (R6 / R14)
    def test_create_writes_a_clip_project(self):
        dialog = self._dialog()
        with tempfile.TemporaryDirectory() as tmp:
            self._create(dialog, os.path.join(tmp, "out.timeline.json"))

            self.assertEqual(dialog.saved_path(), dialog._dest_path)
            timeline, _meta = project_io.load_project(dialog._dest_path)

        self.assertEqual((timeline.width, timeline.height), _CANVAS)
        self.assertEqual(timeline.orientation, "portrait")
        self.assertEqual(project_io.project_kind(timeline), project_io.KIND_CLIP)
        self.assertEqual(len(timeline.base_video_track().clips), 2)
        self.assertIsNotNone(crop.load(timeline))

    # 保存先の既定は元動画名 + 接尾辞
    def test_default_destination(self):
        dialog = self._dialog({"vertical": {"crop": {"project_suffix": "_tate"}}})

        self.assertIn("_tate", os.path.basename(dialog._dest_path))

    # アーカイブ用は素材が元 VOD へ張り替わって保存されること (resolve10 §3.1)
    def test_create_rebases_archive_media(self):
        from unittest import mock

        from src.archive import cut_origin, vod_rebase

        self.timeline = _timeline(archive=True)
        dialog = self._dialog()
        with mock.patch.object(cut_origin, "measure",
                               lambda *_a, **_k: 98.5):
            dialog._make_rebase = lambda: vod_rebase.plan(self.timeline, {})
            with tempfile.TemporaryDirectory() as tmp:
                self._create(dialog, os.path.join(tmp, "out.timeline.json"))
                timeline, _meta = project_io.load_project(dialog._dest_path)

        self.assertEqual(project_io.project_kind(timeline), project_io.KIND_CLIP)
        self.assertEqual([media.path for media in timeline.media_pool],
                         [os.path.abspath(__file__)])
        clips = timeline.base_video_track().clips
        # 素材内 0.0 / 20.0 が VOD の 98.5 / 118.5 になる
        self.assertAlmostEqual(clips[0].source_in, 98.5)
        self.assertAlmostEqual(clips[1].source_in, 118.5)

    # 背景の選択が指定へ入ること
    def test_background_is_stored(self):
        dialog = self._dialog()
        dialog.background_combo.setCurrentIndex(
            dialog.background_combo.findData(crop.BG_BLACK))

        self.assertEqual(dialog._layout()["background"], crop.BG_BLACK)


class MenuGateTest(unittest.TestCase):

    # 縦の Timeline と、画面の種別と中身が食い違うものでは出さないこと
    def test_gate(self):
        from src.gui.timeline import timeline_editor_dialog as module

        class _Dialog:
            _project_kind = staticmethod(lambda: project_io.KIND_CLIP)

            def __init__(self, timeline, kind=project_io.KIND_CLIP):
                self.controller = type("C", (), {"timeline": timeline})()
                self._project_kind = lambda: kind

            _can_make_vertical = module.TimelineEditorDialog._can_make_vertical

        self.assertTrue(_Dialog(_timeline())._can_make_vertical())
        self.assertFalse(_Dialog(_timeline(portrait=True))._can_make_vertical())
        # クリップ用の画面にアーカイブ用の中身 (逆も) は食い違いのため出さない
        self.assertFalse(_Dialog(_timeline(archive=True))._can_make_vertical())
        self.assertFalse(
            _Dialog(_timeline(), kind=project_io.KIND_ARCHIVE)._can_make_vertical())
        # アーカイブ用の画面 + アーカイブ用の中身は対象 (resolve10 §3.1)
        self.assertTrue(
            _Dialog(_timeline(archive=True),
                    kind=project_io.KIND_ARCHIVE)._can_make_vertical())

    # アーカイブ用の画面は、元 VOD が無くても対象 (ver9 resolve14 §3.3)。
    # 既定では素材を複製して自己完結させるため、元 VOD は要らない。
    def _archive_dialog(self, timeline, settings=None):
        from src.gui.timeline import archive_timeline_dialog as module

        dialog = module.ArchiveTimelineDialog.__new__(module.ArchiveTimelineDialog)
        dialog.controller = type("C", (), {"timeline": timeline})()
        dialog._project_kind = lambda: project_io.KIND_ARCHIVE
        dialog._settings = settings or {}
        return dialog

    def _without_vod(self):
        gone = _timeline(archive=True)
        gone.source = dict(gone.source,
                           archive=dict(gone.source["archive"],
                                        vod_path="D:/does/not/exist.mp4"),
                           input_path="D:/does/not/exist.mp4")
        return gone

    def test_archive_gate_does_not_need_vod(self):
        self.assertTrue(
            self._archive_dialog(_timeline(archive=True))._can_make_vertical())
        # 元 VOD が無くても対象のまま (複製して自己完結させるため)
        self.assertTrue(self._archive_dialog(self._without_vod())._can_make_vertical())

    # 張り替え方式を選んでいるときだけ、元 VOD が無いと理由を出す
    def test_rebase_mode_reports_the_missing_vod(self):
        rebase = {"vertical": {"crop": {"media_mode": "rebase"}}}
        dialog = self._archive_dialog(self._without_vod(), rebase)
        self.assertTrue(dialog._can_make_vertical())
        self.assertIn("元動画", dialog._vertical_blocked_reason([]))
        # 既定 (複製) では理由なし = そのまま作れる
        self.assertEqual(
            self._archive_dialog(self._without_vod())._vertical_blocked_reason([]), "")


# 「続きから」の履歴 (ver9 resolve14 追補)
#   ・作った縦プロジェクトが recent へ積まれること
#   ・アーカイブ用の画面から作っても積み先は recent (recent_archive ではない)
class RecentHistoryTest(unittest.TestCase):

    # 履歴は os.path.abspath/normpath で書き方をそろえて積まれる。
    # 突き合わせはその形で行うため、期待値もここで通しておく。
    def _norm(self, path):
        return os.path.normpath(os.path.abspath(path))

    def test_remember_recent_pushes_to_the_front(self):
        old = self._norm("work/a.timeline.json")
        settings = {"timeline": {"project": {"recent": [old]}}}

        self.assertTrue(
            project_io.remember_recent(settings, "work/b.timeline.json"))

        recent = settings["timeline"]["project"]["recent"]
        self.assertEqual(recent, [self._norm("work/b.timeline.json"), old])

    # 区切り文字・大文字小文字の違いで二重に積まない (resolve.md §9-8)
    def test_remember_recent_dedupes(self):
        settings = {"timeline": {"project": {
            "recent": [self._norm("work/A.timeline.json")]}}}

        project_io.remember_recent(settings, "work/a.timeline.json")

        self.assertEqual(len(settings["timeline"]["project"]["recent"]), 1)

    def test_remember_recent_respects_the_limit(self):
        settings = {"timeline": {"project": {"recent_limit": 2, "recent": [
            self._norm("work/a.timeline.json"),
            self._norm("work/b.timeline.json")]}}}

        project_io.remember_recent(settings, "work/c.timeline.json")

        recent = settings["timeline"]["project"]["recent"]
        self.assertEqual([os.path.basename(path) for path in recent],
                         ["c.timeline.json", "a.timeline.json"])

    def test_remember_recent_splits_by_kind(self):
        settings = {}

        project_io.remember_recent(settings, "work/a.archive.timeline.json",
                                   project_io.KIND_ARCHIVE)

        project_cfg = settings["timeline"]["project"]
        self.assertEqual(len(project_cfg["recent_archive"]), 1)
        self.assertNotIn("recent", project_cfg)

    # 編集画面が作成後に履歴へ積むこと。アーカイブ用の画面でも積み先は recent
    # (作られるのは縦のクリップ用プロジェクトで、開くのはクリップ用タブ)。
    def test_editor_records_the_created_vertical_project(self):
        from src.gui.timeline import timeline_editor_dialog as editor_module
        from src.gui.timeline import vertical_project_dialog as vertical_module

        created = self._norm("work/vod_vertical.timeline.json")

        class _FakeDialog:
            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                return 1

            def saved_path(self):
                return created

        dialog = editor_module.TimelineEditorDialog.__new__(
            editor_module.TimelineEditorDialog)
        dialog._settings = {}
        dialog._project_kind = lambda: project_io.KIND_ARCHIVE
        dialog._vertical_target_clips = lambda: [object()]
        dialog._vertical_rebase = lambda _clips: None
        dialog.controller = type("C", (), {"timeline": _timeline()})()
        dialog.preview = type("P", (), {"set_status": lambda _self, _text: None})()

        saved = []
        original_save = editor_module.save_settings
        original_dialog = vertical_module.VerticalProjectDialog
        editor_module.save_settings = saved.append
        vertical_module.VerticalProjectDialog = _FakeDialog
        try:
            dialog._open_vertical_project()
        finally:
            editor_module.save_settings = original_save
            vertical_module.VerticalProjectDialog = original_dialog

        project_cfg = dialog._settings["timeline"]["project"]
        self.assertEqual(project_cfg["recent"], [created])
        self.assertNotIn("recent_archive", project_cfg)
        # setting.json へ書き戻すところまで (書かないと次の起動で消える)
        self.assertEqual(len(saved), 1)


if __name__ == "__main__":
    unittest.main()
