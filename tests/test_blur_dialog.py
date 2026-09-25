# ぼかしの画面 (src/gui/timeline/blur_spec_dialog.py / blur_canvas.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve8.md §5.10 / §5.11 / §8.3 の受け入れ手順を自動化):
#   ・クリップの中だけを扱うこと (コマ送りが端で止まる / R1-2 / R7)
#   ・1 件目の囲みで「ボカす / ボカさない」を聞き、ボカさないなら全面ぼかしも足すこと (R4 / R6)
#   ・2 件目以降はラジオに従うこと (R4-2)
#   ・枠を動かすとその時刻にキーフレームが打たれること (R8 / R9)
#   ・ハンドルの当たり判定と大きさの変更 (R10)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from src.gui.timeline.blur_canvas import _resized
    from src.gui.timeline.blur_spec_dialog import BlurSpecDialog
    _QT_AVAILABLE = True
except Exception:  # noqa: BLE001 (PySide6 が無い環境ではスキップする)
    _QT_AVAILABLE = False

from src.blur import decisions as blur_decisions
from src.timeline import commands
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

# 30fps / 2 秒 = 60 コマのクリップ 2 本
_FPS = 30


def _build_timeline():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, _FPS, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 2.0, 0.0, 2.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 2.0, 2.0, 5.0, 7.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1"), AudioClip("a2", "c2")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=_FPS, width=1920, height=1080,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


# 画面が使う分だけを備えたコントローラ (履歴つき)
class _Controller:

    def __init__(self, timeline):
        self.timeline = timeline
        self.settings = {"blur": {"enabled": True, "track": {"auto_start": False}},
                         "timeline": {"preview": {"backend": "none"}}}
        self.cfg = {"overlay": {}}
        self.stack = commands.CommandStack()

    def execute(self, command):
        return self.stack.push(self.timeline, command)

    def can_undo(self):
        return self.stack.can_undo()

    def can_redo(self):
        return self.stack.can_redo()

    def undo(self):
        return self.stack.undo(self.timeline)

    def redo(self):
        return self.stack.redo(self.timeline)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップする")
class BlurSpecDialogTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.timeline = _build_timeline()
        self.controller = _Controller(self.timeline)
        self.clip = self.timeline.base_clips()[0]
        self.dialog = BlurSpecDialog(self.controller, None, clip=self.clip,
                                     settings=self.controller.settings)
        self.addCleanup(self.dialog.close)
        # ダイアログは出さず、決まった答えを返す (モーダルで止まらないように)
        self._answer = blur_decisions.KEEP
        self.dialog._ask_first_mode = lambda: self._answer      # noqa: SLF001

    def _specs(self):
        return blur_decisions.load(self.timeline)["specs"]

    # クリップのコマ数が範囲になり、端で止まること (R1-2 / R7)
    def test_frame_navigation_stays_in_the_clip(self):
        self.assertEqual(self.dialog.slider.maximum(), 60 - 1)
        self.dialog._navigate("prev")                            # noqa: SLF001
        self.assertEqual(self.dialog.slider.value(), 0)
        self.dialog._navigate("next")                            # noqa: SLF001
        self.assertEqual(self.dialog.slider.value(), 1)
        self.dialog._navigate("next_fast")                       # noqa: SLF001
        self.assertEqual(self.dialog.slider.value(), 11)
        self.dialog._navigate("end")                             # noqa: SLF001
        self.assertEqual(self.dialog.slider.value(), 59)
        self.dialog._navigate("next")                            # noqa: SLF001
        self.assertEqual(self.dialog.slider.value(), 59, "クリップの外へ出ています")
        self.dialog._navigate("home")                            # noqa: SLF001
        self.assertEqual(self.dialog.slider.value(), 0)

    # 1 件目に「ボカさない」= 全面ぼかしも一緒に足されること (R4 / R6 / R6-2)
    def test_first_keep_adds_the_frame_blur(self):
        self._answer = blur_decisions.KEEP
        self.dialog._on_area_drawn((0.3, 0.3, 0.2, 0.2))         # noqa: SLF001
        specs = self._specs()
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0]["kind"], blur_decisions.KIND_FRAME)
        self.assertEqual(specs[1]["mode"], blur_decisions.KEEP)
        # 1 回の Undo で両方戻ること
        self.controller.undo()
        self.assertEqual(self._specs(), [])

    # 1 件目に「ボカす」= 全面ぼかしは足されないこと (R5)
    def test_first_blur_is_alone(self):
        self._answer = blur_decisions.BLUR
        self.dialog._on_area_drawn((0.3, 0.3, 0.2, 0.2))         # noqa: SLF001
        specs = self._specs()
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["mode"], blur_decisions.BLUR)

    # 2 件目以降はダイアログを出さず、ラジオに従うこと (R4-2)
    def test_second_area_follows_the_radio(self):
        self._answer = blur_decisions.BLUR
        self.dialog._on_area_drawn((0.1, 0.1, 0.2, 0.2))         # noqa: SLF001
        self.dialog._ask_first_mode = self.fail                  # noqa: SLF001 (呼ばれたら失敗)
        self.dialog.keep_radio.setChecked(True)
        self.dialog._on_area_drawn((0.5, 0.5, 0.2, 0.2))         # noqa: SLF001
        specs = self._specs()
        # ボカさないを選んだので、全面ぼかしが下へ入って 3 件になる
        self.assertEqual(len(specs), 3)
        self.assertEqual(specs[0]["kind"], blur_decisions.KIND_FRAME)
        self.assertEqual(specs[-1]["mode"], blur_decisions.KEEP)

    # 小さすぎる囲みは作らないこと
    def test_tiny_area_is_rejected(self):
        self.dialog._on_area_drawn((0.3, 0.3, 0.0, 0.0))         # noqa: SLF001
        self.assertEqual(self._specs(), [])

    # 枠を動かすと、そのコマにキーフレームが打たれること (R8 / R9)
    def test_moving_the_box_adds_a_key(self):
        self._answer = blur_decisions.BLUR
        self.dialog._on_area_drawn((0.3, 0.3, 0.2, 0.2))         # noqa: SLF001
        spec_id = self._specs()[0]["id"]
        self.dialog._navigate("next_fast")                       # noqa: SLF001 (10 コマ先へ)
        self.dialog._on_rect_committed(spec_id, (0.5, 0.3, 0.2, 0.2))    # noqa: SLF001

        keys = blur_decisions.keys_of(self._specs()[0])
        self.assertEqual(len(keys), 2)
        self.assertAlmostEqual(keys[1]["t"], 10 / float(_FPS), places=3)
        self.assertEqual(keys[1]["rect"][0], 0.5)

    # 大きさを変えてもキーフレームになること (R10)
    def test_resizing_adds_a_key(self):
        self._answer = blur_decisions.BLUR
        self.dialog._on_area_drawn((0.3, 0.3, 0.2, 0.2))         # noqa: SLF001
        spec_id = self._specs()[0]["id"]
        self.dialog._navigate("next")                            # noqa: SLF001
        self.dialog._on_rect_committed(spec_id, (0.3, 0.3, 0.4, 0.4))    # noqa: SLF001
        keys = blur_decisions.keys_of(self._specs()[0])
        self.assertEqual([k["rect"][2] for k in keys], [0.2, 0.4])

    # 指定の削除・重ね順・ダブルクリックでの入れ替え
    def test_spec_operations(self):
        self._answer = blur_decisions.KEEP
        self.dialog._on_area_drawn((0.3, 0.3, 0.2, 0.2))         # noqa: SLF001
        area_id = self._specs()[1]["id"]

        self.dialog._on_spec_activated(area_id)                  # noqa: SLF001
        self.assertEqual(self._specs()[1]["mode"], blur_decisions.BLUR)

        self.dialog._on_spec_selected(area_id)                   # noqa: SLF001
        self.dialog._move_spec(-1)                               # noqa: SLF001
        self.assertEqual(self._specs()[0]["id"], area_id)

        self.dialog._delete_selected()                           # noqa: SLF001
        self.assertEqual([s["kind"] for s in self._specs()], [blur_decisions.KIND_FRAME])

    # キーフレームの一覧からその時刻へ飛べること
    def test_key_list_navigation(self):
        self._answer = blur_decisions.BLUR
        self.dialog._navigate("next_fast")                       # noqa: SLF001
        self.dialog._on_area_drawn((0.3, 0.3, 0.2, 0.2))         # noqa: SLF001
        self.dialog._navigate("home")                            # noqa: SLF001
        self.assertEqual(self.dialog.key_list.count(), 1)
        self.dialog._on_key_clicked(self.dialog.key_list.item(0))    # noqa: SLF001
        self.assertEqual(self.dialog.slider.value(), 10)

    # 「動かさない」への切り替え
    def test_toggle_follow(self):
        self._answer = blur_decisions.BLUR
        self.dialog._on_area_drawn((0.3, 0.3, 0.2, 0.2))         # noqa: SLF001
        self.dialog._on_spec_selected(self._specs()[0]["id"])     # noqa: SLF001
        self.dialog._toggle_follow()                              # noqa: SLF001
        self.assertEqual(self._specs()[0]["follow"], blur_decisions.FOLLOW_FIXED)
        self.dialog._toggle_follow()                              # noqa: SLF001
        self.assertEqual(self._specs()[0]["follow"], blur_decisions.FOLLOW_TRACK)

    # 隣のクリップの指定は出てこないこと (R11)
    def test_other_clips_are_not_shown(self):
        other = self.timeline.base_clips()[1]
        span = blur_decisions.span_for(self.timeline, other)
        self.controller.execute(commands.AddBlurSpecs(
            [blur_decisions.make_frame_spec("", str(other.media_id), span)]))
        self.dialog._refresh()                                   # noqa: SLF001
        self.assertEqual(self.dialog.spec_list.count(), 1)
        self.assertIn("まだ指定がありません", self.dialog.spec_list.item(0).text())

    # キャンバスの枠が選べること (全面ぼかしは当たり判定に入らない)
    def test_canvas_hit_test(self):
        self._answer = blur_decisions.KEEP
        self.dialog._on_area_drawn((0.3, 0.3, 0.2, 0.2))         # noqa: SLF001
        area_id = self._specs()[1]["id"]
        # 囲みの中 (キャンバス座標) を突く
        hits = self.dialog.canvas._ids_at((0.4 * 1920, 0.4 * 1080))   # noqa: SLF001
        self.assertEqual(hits, [area_id])
        # 囲みの外は当たらない (全面ぼかしは触れない)
        self.assertEqual(self.dialog.canvas._ids_at((10.0, 10.0)), [])   # noqa: SLF001


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップする")
class ResizeHandleTest(unittest.TestCase):
    """ハンドルの計算 (blur_canvas._resized / R10)"""

    def setUp(self):
        self.rect = (100.0, 100.0, 200.0, 100.0)

    # 東 (右辺) を掴んで広げる = 左辺は動かない
    def test_east_keeps_the_left_edge(self):
        moved = _resized(self.rect, "e", 50.0, 0.0, False, False, 4.0)
        self.assertEqual(moved, (100.0, 100.0, 250.0, 100.0))

    # 西 (左辺) を掴んで広げる = 右辺は動かない
    def test_west_keeps_the_right_edge(self):
        moved = _resized(self.rect, "w", -50.0, 0.0, False, False, 4.0)
        self.assertEqual(moved, (50.0, 100.0, 250.0, 100.0))

    # 北 (上辺) は幅を変えない
    def test_north_keeps_the_width(self):
        moved = _resized(self.rect, "n", 30.0, -20.0, False, False, 4.0)
        self.assertEqual(moved, (100.0, 80.0, 200.0, 120.0))

    # 角は縦横どちらも変わる
    def test_corner_changes_both(self):
        moved = _resized(self.rect, "se", 20.0, 10.0, False, False, 4.0)
        self.assertEqual(moved, (100.0, 100.0, 220.0, 110.0))

    # Shift = 縦横比を保つ
    def test_shift_keeps_the_ratio(self):
        moved = _resized(self.rect, "se", 100.0, 0.0, True, False, 4.0)
        self.assertAlmostEqual(moved[2] / moved[3], 200.0 / 100.0, places=6)

    # Alt = 中心を動かさない
    def test_alt_keeps_the_center(self):
        moved = _resized(self.rect, "se", 20.0, 10.0, False, True, 4.0)
        self.assertAlmostEqual(moved[0] + moved[2] / 2.0, 200.0)
        self.assertAlmostEqual(moved[1] + moved[3] / 2.0, 150.0)

    # 最小の大きさより小さくはならないこと
    def test_minimum_size(self):
        moved = _resized(self.rect, "e", -1000.0, 0.0, False, False, 10.0)
        self.assertEqual(moved[2], 10.0)


if __name__ == "__main__":
    unittest.main()
