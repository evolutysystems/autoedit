# アーカイブ切り抜き用 Timeline から縦プロジェクトを作る (ver5 resolve9 §3.7 改訂) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・素材が元 VOD 1 本へ張り替わり、時刻が VOD 基準へ直ること
#   ・足す値が vod_start ではなく「切り出しの先頭 (キーフレーム)」であること
#   ・保存した縦プロジェクトがクリップ用として開けること (archive セクションが消える)
#   ・切り抜きの指定が張り替え後も有効なままであること
#   ・元の Timeline を変更しないこと
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import vod_rebase
from src.exceptions import InputError
from src.timeline import crop, project_io, vertical_builder
from src.timeline.model import (
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    SubtitleClip,
    Timeline,
    Track,
)

# 実在するファイルを VOD 役に使う (存在確認と ffprobe の失敗フォールバックを通す)
VOD = os.path.abspath(__file__)


# アーカイブ切り抜き用の Timeline を組む。
#   m1 = VOD 100.0-160.0 から切り出した素材 / m2 = VOD 500.0-560.0 から
#   m9 = 後から足した素材 (控えが無い = 張り替えない)
def _archive_timeline():
    media = [MediaRef("m1", "video", VOD, 60.0, 1920, 1080, 60, True),
             MediaRef("m2", "video", VOD, 60.0, 1920, 1080, 60, True),
             MediaRef("m9", "video", VOD, 30.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 10.0, 15.0,
             origin={"type": ORIGIN_SILENCE_CUT, "archive_clip_index": 1}),
        Clip("c2", "m2", 5.0, 4.0, 2.0, 6.0,
             origin={"type": ORIGIN_SILENCE_CUT, "archive_clip_index": 2}),
        Clip("c3", "m9", 9.0, 3.0, 1.0, 4.0,
             origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2"), AudioClip("a3", "c3"),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 1.0, 2.0, "c1 の中"),
    ])
    source = {
        "input_path": VOD,
        "duration_sec": 12.0,
        "archive": {
            "vod_path": VOD,
            "clips": [{"index": 1, "media_id": "m1", "vod_start": 100.0, "vod_end": 160.0},
                      {"index": 2, "media_id": "m2", "vod_start": 500.0, "vod_end": 560.0}],
            "media": [{"media_id": "m1", "vod_start": 100.0, "vod_end": 160.0,
                       "media_role": "normalized"},
                      {"media_id": "m2", "vod_start": 500.0, "vod_end": 560.0,
                       "media_role": "normalized"}],
            "curve": [],
        },
    }
    return Timeline(fps=60, width=1920, height=1080, source=source,
                    media_pool=media, tracks=[video, audio, subtitle])


def _layout(timeline, media_id="m1"):
    media = timeline.media_by_id(media_id)
    return crop.make_layout(crop.MODE_SINGLE, [(420, 0, 1080, 1080)], media, 1080, 1920)


# キーフレームの実測を「要求位置の 1.5 秒手前」に固定する
def _stub_keyframe(vod_path, position, ffmpeg_cfg, window_sec=None):
    return max(position - 1.5, 0.0)


class PlanTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _archive_timeline()

    def test_clip_project_is_not_rebased(self):
        timeline = _archive_timeline()
        timeline.source = {"input_path": VOD}          # archive セクション無し = クリップ用
        self.assertIsNone(vod_rebase.plan(timeline, {}))

    def test_missing_vod_is_reported(self):
        timeline = _archive_timeline()
        source = dict(timeline.source)
        source["archive"] = dict(source["archive"], vod_path="D:/does/not/exist.mp4")
        source["input_path"] = "D:/does/not/exist.mp4"
        timeline.source = source
        with self.assertRaises(InputError):
            vod_rebase.plan(timeline, {})

    # 控えのある素材だけが張り替え対象になり、足す値はキーフレーム位置になること
    def test_offsets_use_keyframe(self):
        with mock.patch.object(vod_rebase, "_keyframe_at_or_before", _stub_keyframe):
            plan = vod_rebase.plan(self.timeline, {})
        self.assertEqual(plan["offsets"], {"m1": 98.5, "m2": 498.5})
        self.assertEqual(plan["media"].path, VOD)
        self.assertEqual(plan["source"]["input_path"], VOD)
        self.assertEqual(plan["source"]["media_role"], "source")

    # 控えの無い素材と、音量正規化について知らせること
    def test_warnings(self):
        clips = self.timeline.base_video_track().clips
        with mock.patch.object(vod_rebase, "_keyframe_at_or_before", _stub_keyframe):
            plan = vod_rebase.plan(self.timeline, {}, clips)
        text = "\n".join(plan["warnings"])
        self.assertIn("元 VOD から作られていない素材 1 件", text)
        self.assertIn("音量の正規化", text)


class KeyframeProbeTest(unittest.TestCase):

    # ffprobe の出力から「要求位置以前で最も近いキーフレーム」を選ぶこと
    def test_picks_nearest_before(self):
        stdout = "\n".join([
            "96.000000,K__", "98.000000,K__", "99.000000,___", "100.500000,K__"])
        with mock.patch("src.archive.vod_rebase.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout=stdout)):
            self.assertEqual(vod_rebase._keyframe_at_or_before(VOD, 100.0, {}), 98.0)

    # 測れなければ要求位置をそのまま使い、作成を止めないこと
    def test_falls_back_on_failure(self):
        with mock.patch("src.archive.vod_rebase.subprocess.run",
                        return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(vod_rebase._keyframe_at_or_before(VOD, 100.0, {}), 100.0)

    # 先頭 (0 秒) は測るまでもないこと
    def test_zero_needs_no_probe(self):
        with mock.patch("src.archive.vod_rebase.subprocess.run") as run:
            self.assertEqual(vod_rebase._keyframe_at_or_before(VOD, 0.0, {}), 0.0)
        run.assert_not_called()


class BuildTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _archive_timeline()
        self.layout = _layout(self.timeline)
        with mock.patch.object(vod_rebase, "_keyframe_at_or_before", _stub_keyframe):
            self.plan = vod_rebase.plan(self.timeline, {})
        self.vertical, self.warnings = vertical_builder.build(
            self.timeline, ["c1", "c2", "c3"], self.layout, {}, rebase=self.plan)

    # 控えのある素材は VOD へ張り替わり、素材内の秒が VOD の秒になること
    def test_clips_point_at_vod(self):
        clips = {c.id: c for c in self.vertical.base_video_track().clips}
        vod_id = self.plan["media"].id
        self.assertEqual(clips["c1"].media_id, vod_id)
        self.assertAlmostEqual(clips["c1"].source_in, 108.5)
        self.assertAlmostEqual(clips["c1"].source_out, 113.5)
        self.assertEqual(clips["c2"].media_id, vod_id)
        self.assertAlmostEqual(clips["c2"].source_in, 500.5)
        self.assertAlmostEqual(clips["c2"].source_out, 504.5)

    # 控えの無い素材はそのまま残ること
    def test_unknown_media_is_kept(self):
        clip = self.vertical.clip_by_id("c3")
        self.assertEqual(clip.media_id, "m9")
        self.assertAlmostEqual(clip.source_in, 1.0)
        self.assertIn("m9", {media.id for media in self.vertical.media_pool})

    # 素材プールは「VOD + 張り替えなかったもの」だけになること
    def test_media_pool(self):
        ids = sorted(media.id for media in self.vertical.media_pool)
        self.assertEqual(ids, sorted([self.plan["media"].id, "m9"]))
        self.assertEqual(self.vertical.media_by_id(self.plan["media"].id).path, VOD)

    # クリップ用プロジェクトとして開けること (archive セクションが残らない)
    def test_saved_as_clip_project(self):
        self.assertEqual(project_io.project_kind(self.vertical), project_io.KIND_CLIP)
        self.assertNotIn("archive", self.vertical.source)
        self.assertEqual(self.vertical.source["input_path"], VOD)
        self.assertEqual(self.vertical.source["media_id"], self.plan["media"].id)
        self.assertEqual(self.vertical.source["media_role"], "source")

    # 切り抜きの指定が張り替え後の素材と噛み合うこと (捨てられない)
    def test_crop_layout_follows_rebase(self):
        layout = crop.load(self.vertical)
        media = self.vertical.media_by_id(self.plan["media"].id)
        self.assertEqual(layout["media_id"], media.id)
        self.assertTrue(crop.is_valid(layout, media, 1080, 1920))

    # 元の Timeline は変わらないこと
    def test_source_timeline_untouched(self):
        clip = self.timeline.clip_by_id("c1")
        self.assertEqual(clip.media_id, "m1")
        self.assertAlmostEqual(clip.source_in, 10.0)
        self.assertIn("archive", self.timeline.source)
        self.assertEqual(self.layout["media_id"], "m1")

    # 縦キャンバス・詰め直し・字幕といった従来の動きは変わらないこと
    def test_keeps_existing_behaviour(self):
        self.assertEqual((self.vertical.width, self.vertical.height), (1080, 1920))
        clips = self.vertical.base_video_track().clips
        self.assertAlmostEqual(clips[0].timeline_start, 0.0)
        self.assertAlmostEqual(clips[1].timeline_start, 5.0)
        self.assertEqual(len(self.vertical.base_subtitle_track().clips), 1)


if __name__ == "__main__":
    unittest.main()
