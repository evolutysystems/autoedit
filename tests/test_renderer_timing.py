# レンダリング時の尺決定と中間エンコード設定の単体テスト
# 実行: python -m unittest discover -s tests
# 重点: 書き出し軸の累積ずれ対策 (docs/error/20260812/resolve3.md)。
#   ・部品の尺を絶対フレーム番号の差で決めるため、端数を持つクリップを何本並べても
#     誤差が積み上がらないこと (§3-2 / 変更 2)
#   ・中間パートの音声を非圧縮にできること (§5.2 / 変更 1a)
# FFmpeg は起動しない。尺計算とオプション組み立てだけを検証する。
import unittest

from src.modules import ffmpeg_runner
from src.timeline import renderer
from src.timeline.model import (
    AudioClip,
    Clip,
    MediaRef,
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_VIDEO,
    Timeline,
    Track,
)

FPS = 60
FRAME = 1.0 / FPS


# 60fps のフレーム境界に乗らない端数尺のクリップ列で Timeline を組む
# starts_and_durations: [(timeline_start, duration), ...]
def _build_timeline(starts_and_durations):
    media = [MediaRef("m1", "video", "C:/work/src.mp4", 3600.0, 1920, 1080, FPS, True)]
    clips = []
    audio = []
    for index, (start, duration) in enumerate(starts_and_durations):
        clips.append(Clip(f"c{index}", "m1", start, duration, start, start + duration,
                          origin={"type": ORIGIN_SILENCE_CUT, "segment_index": index}))
        audio.append(AudioClip(f"a{index}", f"c{index}"))
    tracks = [
        Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=clips),
        Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=audio),
    ]
    return Timeline(fps=FPS, width=1920, height=1080,
                    source={"media_id": "m1", "duration_sec": 3600.0},
                    media_pool=media, tracks=tracks)


# renderer が参照する timeline.render 設定を組む
def _cfg(gap_policy="black", quantize=True):
    return {"render": {"gap_policy": gap_policy, "frame_quantize": quantize}}


class BuildSegmentsTest(unittest.TestCase):

    # 端数尺のクリップを隙間なく並べた場合、各部品の尺が 1/fps の整数倍になり、
    # 総和は「最終クリップ終端をフレーム量子化した値」と一致する (誤差が累積しない)
    def test_quantized_durations_do_not_accumulate_error(self):
        cursor = 0.0
        layout = []
        for _ in range(80):
            duration = 5.0041          # フレーム境界に乗らない端数尺
            layout.append((cursor, duration))
            cursor += duration
        timeline = _build_timeline(layout)

        segments = renderer._build_segments(timeline, _cfg())
        self.assertEqual(len(segments), 80)
        for segment in segments:
            frames = segment["duration"] * FPS
            self.assertAlmostEqual(frames, round(frames), places=6)

        total = sum(s["duration"] for s in segments)
        expected = timeline.quantize(layout[-1][0] + layout[-1][1])
        self.assertAlmostEqual(total, expected, places=6)
        # 従来 (量子化なし) との差は半フレーム未満に収まる
        self.assertLess(abs(total - cursor), FRAME)

    # frame_quantize=False では従来どおりモデルの秒がそのまま返る (切り戻し経路)
    def test_quantize_disabled_keeps_model_seconds(self):
        layout = [(0.0, 5.0041), (5.0041, 3.3337)]
        timeline = _build_timeline(layout)
        segments = renderer._build_segments(timeline, _cfg(quantize=False))
        self.assertEqual([s["duration"] for s in segments], [5.0041, 3.3337])

    # ギャップを含む場合もタイル化に従い、隙間・重なりが生じない
    def test_gap_segments_tile_without_overlap(self):
        layout = [(0.0, 5.0041), (7.5013, 3.3337), (12.0071, 4.1119)]
        timeline = _build_timeline(layout)
        segments = renderer._build_segments(timeline, _cfg())

        self.assertEqual([s["kind"] for s in segments],
                         ["clip", "gap", "clip", "gap", "clip"])
        for segment in segments:
            frames = segment["duration"] * FPS
            self.assertAlmostEqual(frames, round(frames), places=6)
        total = sum(s["duration"] for s in segments)
        self.assertAlmostEqual(total, timeline.quantize(layout[-1][0] + layout[-1][1]),
                               places=6)

    # gap_policy="close" ではギャップ部品を作らない (従来どおり)
    def test_gap_policy_close_drops_gaps(self):
        layout = [(0.0, 5.0041), (7.5013, 3.3337)]
        timeline = _build_timeline(layout)
        segments = renderer._build_segments(timeline, _cfg(gap_policy="close"))
        self.assertEqual([s["kind"] for s in segments], ["clip", "clip"])

    # 1 フレーム未満のクリップでも 0 尺の部品は作らない
    def test_sub_frame_clip_keeps_one_frame(self):
        timeline = _build_timeline([(0.0, 0.004)])
        segments = renderer._build_segments(timeline, _cfg())
        self.assertAlmostEqual(segments[0]["duration"], FRAME, places=6)


class DurationArgTest(unittest.TestCase):

    # フレーム境界は必ず切り捨てて渡す (丸め上げると映像が 1 フレーム余分に出る / §5.3)
    # 割り切れる尺は境界ちょうどで良い (FFmpeg の打ち切りは「PTS < -t」の厳密比較のため)。
    def test_frame_boundary_is_truncated_not_rounded_up(self):
        self.assertEqual(renderer._duration_arg(301 / 60.0), "5.016666")
        self.assertEqual(renderer._duration_arg(300 / 60.0), "5.000000")
        self.assertEqual(renderer._duration_arg(3603 / 60.0), "60.050000")

    # 切り捨て幅は 1 マイクロ秒未満 (音声のサンプル数は目標値へ丸められる)
    def test_truncation_is_below_one_microsecond(self):
        for frames in range(1, 4000):
            duration = frames / 60.0
            gap = duration - float(renderer._duration_arg(duration))
            # 決して要求尺を超えない (超えると映像が 1 フレーム増える)
            self.assertGreaterEqual(gap, 0.0)
            self.assertLess(gap, 1e-6)

    # 常にフレーム境界の内側 (次のフレームの PTS 未満) に収まる
    def test_stays_inside_the_requested_frame_count(self):
        for frames in range(1, 4000):
            value = float(renderer._duration_arg(frames / 60.0))
            self.assertLess(value, (frames + 1) / 60.0)
            self.assertGreaterEqual(value, (frames - 1) / 60.0)

    # 負値・0 は 0 秒として扱う (異常値でコマンドを壊さない)
    def test_non_positive_is_clamped(self):
        self.assertEqual(renderer._duration_arg(0.0), "0.000000")
        self.assertEqual(renderer._duration_arg(-1.0), "0.000000")


class IntermediateEncodeOptionsTest(unittest.TestCase):

    def setUp(self):
        self.ffmpeg_cfg = {"video_codec": "libx264", "preset": "medium",
                           "crf": 20, "audio_codec": "aac"}

    # 中間用は音声だけ差し替わり、映像側のオプションは build_encode_options と同一
    def test_audio_codec_is_replaced(self):
        base = ffmpeg_runner.build_encode_options(self.ffmpeg_cfg)
        options = ffmpeg_runner.build_intermediate_encode_options(
            self.ffmpeg_cfg, "pcm_s16le")
        self.assertEqual(options[-2:], ["-c:a", "pcm_s16le"])
        self.assertEqual(options[:-2], base[:-2])

    # 未指定 / 空文字は従来どおり (切り戻し経路)
    def test_no_codec_falls_back_to_default(self):
        base = ffmpeg_runner.build_encode_options(self.ffmpeg_cfg)
        self.assertEqual(
            ffmpeg_runner.build_intermediate_encode_options(self.ffmpeg_cfg), base)
        self.assertEqual(
            ffmpeg_runner.build_intermediate_encode_options(self.ffmpeg_cfg, ""), base)

    # 非圧縮のときだけ容器を .mov にする (mp4 は pcm_s16le を格納できない)
    def test_suffix_depends_on_codec(self):
        self.assertEqual(ffmpeg_runner.intermediate_suffix("pcm_s16le"), ".mov")
        self.assertEqual(ffmpeg_runner.intermediate_suffix("aac"), ".mp4")
        self.assertEqual(ffmpeg_runner.intermediate_suffix(None), ".mp4")
        self.assertEqual(
            ffmpeg_runner.intermediate_suffix("pcm_s24le", ".mkv"), ".mkv")


class RenderConfigDefaultsTest(unittest.TestCase):

    # 設定ファイルに新キーが無くても既定値で補完される (既存 setting.json 互換)
    def test_defaults_are_filled(self):
        from src.timeline.builder import timeline_config
        render = timeline_config({"timeline": {"render": {}}})["render"]
        self.assertEqual(render["intermediate_audio_codec"], "pcm_s16le")
        self.assertEqual(render["intermediate_container"], ".mov")
        self.assertTrue(render["frame_quantize"])

    # 設定で従来動作へ戻せる
    def test_values_are_configurable(self):
        from src.timeline.builder import timeline_config
        render = timeline_config({"timeline": {"render": {
            "intermediate_audio_codec": "aac", "frame_quantize": False}}})["render"]
        self.assertEqual(render["intermediate_audio_codec"], "aac")
        self.assertFalse(render["frame_quantize"])


if __name__ == "__main__":
    unittest.main()
