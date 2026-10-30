# テロップトラックの既定色 (ver9 resolve13) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 背景:
#   「役割をサブへ変える → 文字色を変える → 別トラックへドラッグ」という 3 手を
#   やめ、色はトラックが持つようにした。役割はトラックが決める一方通行になる。
#
# 重点:
#   ・優先順が 個別 → トラック → 役割 → 既定 であること (§3.2)
#   ・焼き込み・プレビュー・Resolve 出力の 3 系統で同じ色になること (§6.2)
#   ・S1/S2/S3 が最初からそろい、S3 には設定のサブ色が入ること (§3.3 / §4.1)
#   ・移動すると役割が落ち先へ合うこと (§3.7)
#   ・色を決めていないトラックは保存にも出力にも現れないこと (旧版と同じ / §7)
import os
import tempfile
import unittest

from src.export import resolve_export
from src.modules import subtitle_generator
from src.timeline import commands, subtitle_style
from src.timeline.model import (
    BASE_SUBTITLE_TRACK_ID,
    COMMENT_SUBTITLE_TRACK_ID,
    DEFAULT_ROLE,
    ROLE_COMMENT,
    TRACK_SUBTITLE,
    SubtitleClip,
    Timeline,
    Track,
)

_ROLE_COLORS = {"streamer": "#FFFFFF", "sub": "#00FF00", "comment": "#0000FF"}


def _profile():
    return subtitle_generator.FontProfile(
        family="TestFont", size=48, color_hex="#FFFFFF",
        outline_color="&H00000000", outline_width=3,
        role_colors=dict(_ROLE_COLORS),
        role_outline_colors={"streamer": "&H00000000"},
    )


# S1 / S3 にテロップを 1 本ずつ置いた Timeline
def _timeline(track_color="", clip_color="", role=DEFAULT_ROLE):
    timeline = Timeline()
    base = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    extra = Track("S3", TRACK_SUBTITLE, 3, name="Subtitle 3", color=track_color)
    comment = Track(COMMENT_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 2, name="Comment")
    extra.clips.append(SubtitleClip("s1", 1.0, 2.0, text="こんばんは", role=role,
                                    color=clip_color))
    timeline.tracks = [base, comment, extra]
    return timeline


def _cfg(**over):
    cfg = dict(commands._DEFAULT_SUBTITLE_TRACKS_CFG)   # noqa: SLF001 (単体テスト)
    cfg.update(over)
    return cfg


class ColorPriorityTest(unittest.TestCase):

    def _color(self, timeline):
        clip = timeline.clip_by_id("s1")
        track = timeline.track_of_clip("s1")
        return subtitle_style.resolve_text_color(
            clip, track, _ROLE_COLORS, "#FFFFFF")

    # ① 個別指定が最優先 (要望 R5)
    def test_individual_wins(self):
        timeline = _timeline(track_color="#123456", clip_color="#ABCDEF")
        self.assertEqual(self._color(timeline), "#ABCDEF")

    # ② 個別が無ければトラックの既定色 (要望 R4)
    def test_track_color_is_used(self):
        self.assertEqual(self._color(_timeline(track_color="#123456")), "#123456")

    # ③ どちらも無ければ役割の色 (従来どおり)
    def test_role_color_is_the_fallback(self):
        self.assertEqual(self._color(_timeline(role="sub")), "#00FF00")

    # ④ 役割の色も無ければ配信者色
    def test_default_color(self):
        clip = SubtitleClip("x", 0.0, 1.0, role="unknown")
        self.assertEqual(
            subtitle_style.resolve_text_color(clip, None, _ROLE_COLORS, "#FFFFFF"),
            "#FFFFFF")

    # 読めない色でも例外にせず、そのまま下流へ渡す (下流が既定へ倒す)
    def test_broken_color_does_not_raise(self):
        self.assertEqual(self._color(_timeline(track_color="あか")), "あか")

    # 色を決めていないトラックは対応表に出ない (= 旧版と同じ値で動く)
    def test_map_skips_tracks_without_color(self):
        self.assertEqual(subtitle_style.track_color_map(_timeline()), {})
        self.assertEqual(
            subtitle_style.track_color_map(_timeline(track_color="#123456")),
            {"s1": "#123456"})


class ToItemTest(unittest.TestCase):

    # to_item がトラック色を個別色と同じ場所へ合成する (下流は無改造で済む)
    def test_track_color_is_merged(self):
        clip = SubtitleClip("s1", 1.0, 2.0, text="あ")
        self.assertNotIn("color", clip.to_item())
        self.assertEqual(clip.to_item("#123456")["color"], "#123456")

    def test_individual_color_wins(self):
        clip = SubtitleClip("s1", 1.0, 2.0, text="あ", color="#ABCDEF")
        self.assertEqual(clip.to_item("#123456")["color"], "#ABCDEF")


class ThreeOutputsAgreeTest(unittest.TestCase):

    # 焼き込み (ASS) / Resolve 出力 / 色解決の共通関数が同じ色を指すこと。
    # 以前は 3 か所に同じ式が書かれていたため、ここが食い違う余地があった (§1.3)。
    def _items(self, timeline):
        colors = subtitle_style.track_color_map(timeline)
        return [clip.to_item(colors.get(clip.id, ""))
                for track in timeline.subtitle_tracks() for clip in track.clips]

    def test_all_three_use_the_track_color(self):
        timeline = _timeline(track_color="#123456")
        items = self._items(timeline)
        profile = _profile()

        # ① 焼き込み: ASS のインライン上書きに入る (#123456 → ASS は BGR 並び)
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "out.ass")
            subtitle_generator.build_subtitle_file(
                items, profile, path, video_width=1920, video_height=1080)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        self.assertIn("\\1c&H563412&", text)

        # ② Resolve 出力: title 辞書の色
        self.assertEqual(
            subtitle_style.resolve_item_color(
                items[0], profile.role_colors, profile.color_hex),
            "#123456")

        # ③ プレビューが使う解決 (クリップ + トラック)
        self.assertEqual(
            subtitle_style.resolve_text_color(
                timeline.clip_by_id("s1"), timeline.track_of_clip("s1"),
                profile.role_colors, profile.color_hex),
            "#123456")

    # 色を決めていなければ従来どおり役割色が出る (回帰の要)
    def test_without_track_color_nothing_changes(self):
        items = self._items(_timeline(role="sub"))
        self.assertNotIn("color", items[0])
        self.assertEqual(
            subtitle_style.resolve_item_color(
                items[0], _ROLE_COLORS, "#FFFFFF"),
            "#00FF00")

    # Resolve 出力の実経路でもトラック色が載る
    def test_resolve_export_items(self):
        timeline = _timeline(track_color="#123456")
        items = resolve_export._timeline_items_as_is(timeline)   # noqa: SLF001
        self.assertEqual(items[0]["color"], "#123456")


class InitialTracksTest(unittest.TestCase):

    def test_creates_s1_s2_s3(self):
        timeline = Timeline()
        created = commands.ensure_initial_subtitle_tracks(
            timeline, _cfg(), sub_color="#00FF00")
        self.assertEqual([track.id for track in created],
                         [BASE_SUBTITLE_TRACK_ID, COMMENT_SUBTITLE_TRACK_ID, "S3"])
        self.assertEqual([track.index for track in created], [1, 2, 3])

    # 既定 S3 には設定のサブ色が入る (役割「サブ」の代わり / §4.1)
    def test_third_track_gets_the_sub_color(self):
        timeline = Timeline()
        commands.ensure_initial_subtitle_tracks(timeline, _cfg(), sub_color="#00FF00")
        self.assertEqual(timeline.track_by_id("S3").color, "#00FF00")
        self.assertEqual(timeline.track_by_id(BASE_SUBTITLE_TRACK_ID).color, "")
        self.assertEqual(timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID).color, "")

    # 1 本に絞ると v1.7.0 以前と同じ (切り戻し)
    def test_single_track_setting(self):
        timeline = Timeline()
        created = commands.ensure_initial_subtitle_tracks(
            timeline, _cfg(initial_tracks=1))
        self.assertEqual([track.id for track in created], [BASE_SUBTITLE_TRACK_ID])

    # 役割別トラックを使わない運用でも 1 本だけ
    def test_role_track_disabled(self):
        timeline = Timeline()
        created = commands.ensure_initial_subtitle_tracks(
            timeline, _cfg(role_track_enabled=False))
        self.assertEqual([track.id for track in created], [BASE_SUBTITLE_TRACK_ID])

    # 既にあるトラックには触らない (開き直したプロジェクト)
    def test_existing_tracks_are_kept(self):
        timeline = _timeline(track_color="#123456")
        created = commands.ensure_initial_subtitle_tracks(timeline, _cfg(),
                                                          sub_color="#00FF00")
        self.assertEqual(created, [])
        self.assertEqual(timeline.track_by_id("S3").color, "#123456")
        self.assertEqual(len(timeline.clip_by_id("s1").text), 5)


class RoleFollowsTrackTest(unittest.TestCase):

    def test_comment_track_gives_the_comment_role(self):
        timeline = _timeline()
        self.assertEqual(
            commands.role_for_subtitle_track(
                timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID), _cfg()),
            ROLE_COMMENT)

    def test_other_tracks_give_the_default_role(self):
        timeline = _timeline()
        self.assertEqual(
            commands.role_for_subtitle_track(timeline.track_by_id("S3"), _cfg()),
            DEFAULT_ROLE)

    def test_none_when_role_tracks_are_off(self):
        timeline = _timeline()
        self.assertIsNone(commands.role_for_subtitle_track(
            timeline.track_by_id("S3"), _cfg(role_track_enabled=False)))

    # S2 へ移すとコメントになる (役割コントロールの代わり / §3.7)
    def test_move_into_comment_track_sets_the_role(self):
        timeline = _timeline()
        stack = commands.CommandStack()
        stack.push(timeline, commands.MoveSubtitleToTrack(
            "s1", COMMENT_SUBTITLE_TRACK_ID, subtitle_tracks_cfg=_cfg()))
        self.assertEqual(timeline.clip_by_id("s1").role, ROLE_COMMENT)

    # S2 から出すと通常テロップへ戻る
    def test_move_out_of_comment_track_restores_the_role(self):
        timeline = _timeline(role=ROLE_COMMENT)
        stack = commands.CommandStack()
        stack.push(timeline, commands.MoveSubtitleToTrack(
            "s1", BASE_SUBTITLE_TRACK_ID, subtitle_tracks_cfg=_cfg()))
        self.assertEqual(timeline.clip_by_id("s1").role, DEFAULT_ROLE)


class SetTrackColorCommandTest(unittest.TestCase):

    def _stack(self, timeline):
        return commands.CommandStack()

    def test_sets_and_undoes(self):
        timeline = _timeline()
        stack = self._stack(timeline)
        self.assertTrue(stack.push(
            timeline, commands.SetSubtitleTrackColor("S3", "#123456")))
        self.assertEqual(timeline.track_by_id("S3").color, "#123456")
        stack.undo(timeline)
        self.assertEqual(timeline.track_by_id("S3").color, "")

    def test_same_color_is_not_pushed(self):
        timeline = _timeline(track_color="#123456")
        stack = self._stack(timeline)
        self.assertFalse(stack.push(
            timeline, commands.SetSubtitleTrackColor("S3", "#123456")))

    def test_empty_clears_the_color(self):
        timeline = _timeline(track_color="#123456")
        stack = self._stack(timeline)
        self.assertTrue(stack.push(
            timeline, commands.SetSubtitleTrackColor("S3", "")))
        self.assertEqual(timeline.track_by_id("S3").color, "")

    def test_video_track_is_refused(self):
        timeline = _timeline()
        stack = self._stack(timeline)
        self.assertFalse(stack.push(
            timeline, commands.SetSubtitleTrackColor("V1", "#123456")))


# ヘッダの色見本まわり (Qt が無い環境ではスキップ)
try:
    from PySide6.QtWidgets import QApplication
    _QT_AVAILABLE = True
except ImportError:                                   # pragma: no cover
    _QT_AVAILABLE = False


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class HeaderSwatchTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def _header(self, settings=None):
        from src.gui.timeline.timeline_controller import TimelineController
        from src.gui.timeline.timeline_view import TrackHeaderWidget, build_rows

        timeline = _timeline()
        controller = TimelineController(timeline, settings or {})
        return controller, TrackHeaderWidget(controller), build_rows(
            timeline, controller.cfg)

    def _row(self, rows, track_id):
        return next(row for row in rows if row["track"].id == track_id)

    # テロップトラックには色見本を出し、行の中に収まっていること
    def test_swatch_is_inside_the_row(self):
        _controller, header, rows = self._header()
        row = self._row(rows, "S3")
        rect = header._swatch_rect(row)                # noqa: SLF001 (単体テスト)
        self.assertTrue(header._has_swatch(row["track"]))   # noqa: SLF001
        self.assertGreaterEqual(rect.top(), row["y"])
        self.assertLessEqual(rect.bottom(), row["y"] + row["height"])
        self.assertLess(rect.right(), header.width())

    # 設定で色見本を消せる (切り戻し)
    def test_swatch_can_be_disabled(self):
        settings = {"timeline": {"subtitle_tracks": {"header_color_enabled": False}}}
        _controller, header, rows = self._header(settings)
        self.assertFalse(header._has_swatch(                # noqa: SLF001
            self._row(rows, "S3")["track"]))

    # 色を決めるとトラックへ入る (ポップアップから呼ばれる口)
    def test_apply_color_updates_the_track(self):
        from PySide6.QtWidgets import QMenu

        controller, header, _rows = self._header()
        header._apply_color(QMenu(), "S3", "#123456")   # noqa: SLF001
        self.assertEqual(controller.timeline.track_by_id("S3").color, "#123456")
        self.assertTrue(controller.can_undo())

    # コメント用トラックだけ目印アイコンを出す (要望 R3)
    def test_comment_track_shows_the_icon(self):
        _controller, header, rows = self._header()
        self.assertTrue(header._is_comment_track(           # noqa: SLF001
            self._row(rows, COMMENT_SUBTITLE_TRACK_ID)["track"]))
        self.assertFalse(header._is_comment_track(          # noqa: SLF001
            self._row(rows, "S3")["track"]))
        # 素材は焼き込みと同じ comment_icon.png を使う (見つかること)
        self.assertIsNotNone(header._icon_pixmap())         # noqa: SLF001


class SaveFormatTest(unittest.TestCase):

    # 色を決めていないトラックの辞書は旧版と 1 文字も変わらない
    def test_to_dict_omits_empty_color(self):
        track = Track("S3", TRACK_SUBTITLE, 3, name="Subtitle 3")
        self.assertNotIn("color", track.to_dict())

    def test_to_dict_includes_color(self):
        track = Track("S3", TRACK_SUBTITLE, 3, name="Subtitle 3", color="#123456")
        self.assertEqual(track.to_dict()["color"], "#123456")

    def test_round_trip(self):
        track = Track("S3", TRACK_SUBTITLE, 3, name="Subtitle 3", color="#123456")
        self.assertEqual(Track.from_dict(track.to_dict()).color, "#123456")

    # 旧プロジェクト (color キーが無い) はそのまま開ける
    def test_from_dict_without_color(self):
        track = Track.from_dict({"id": "S3", "kind": TRACK_SUBTITLE, "index": 3,
                                 "name": "Subtitle 3", "clips": []})
        self.assertEqual(track.color, "")


if __name__ == "__main__":
    unittest.main()
