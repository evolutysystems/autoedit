# 縦動画プロジェクトの作成画面 (src/gui/timeline/) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve9.md §8.1):
#   ・枠が上限・下限・比率・ソースの内側に収まること (R11 / R16)
#   ・「作成」でクリップ用の縦プロジェクトが書き出されること (R6 / R14)
#   ・対象外の Timeline ではメニューを出さないこと (§3.7 / §10 #10)
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
        source["archive"] = {"vod_path": "x.mp4"}
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

    # 背景の選択が指定へ入ること
    def test_background_is_stored(self):
        dialog = self._dialog()
        dialog.background_combo.setCurrentIndex(
            dialog.background_combo.findData(crop.BG_BLACK))

        self.assertEqual(dialog._layout()["background"], crop.BG_BLACK)


class MenuGateTest(unittest.TestCase):

    # 縦・アーカイブ用の Timeline では作成メニューを出さないこと
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
        self.assertFalse(_Dialog(_timeline(archive=True))._can_make_vertical())
        self.assertFalse(
            _Dialog(_timeline(), kind=project_io.KIND_ARCHIVE)._can_make_vertical())


if __name__ == "__main__":
    unittest.main()
