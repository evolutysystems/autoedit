# プレビュー再生の応答改善 (ver3 resolve6 §10.1) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・前進デコード / 再生中の縮小 / フレーム間引きの設定が意図どおり効くこと
#   ・音声チャンクが 1 プロセスで組まれること (断片ごとの -ss 入力 + concat)
#   ・設定で従来動作へ戻せること
import unittest

from src.gui.timeline.timeline_controller import TimelineController
from src.timeline import audio_source, frame_source
from src.timeline.builder import timeline_config
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


# 幅だけを持つ素材の代用 (縮小判定は media.width しか見ない)
class _Media:

    def __init__(self, media_id="m1", width=1920):
        self.id = media_id
        self.width = width
        self.path = __file__


class TestFrameSourceConfig(unittest.TestCase):

    # 再生中だけ縮小する。停止中は原寸のまま (resolve6 §5.7)
    def test_scaled_width_only_while_playing(self):
        source = frame_source.PyAvFrameSource(playback_width=960)
        self.assertEqual(source._scaled_width(_Media()), 0)
        source.set_playing(True)
        self.assertEqual(source._scaled_width(_Media()), 960)
        source.set_playing(False)
        self.assertEqual(source._scaled_width(_Media()), 0)

    # 素材が指定幅以下なら縮小しない (拡大は決してしない)
    def test_small_media_is_not_scaled(self):
        source = frame_source.PyAvFrameSource(playback_width=960)
        source.set_playing(True)
        self.assertEqual(source._scaled_width(_Media(width=640)), 0)
        self.assertEqual(source._scaled_width(_Media(width=960)), 0)

    # playback_width=0 なら再生中も原寸 (従来動作)
    def test_disabled_scaling(self):
        source = frame_source.PyAvFrameSource(playback_width=0)
        source.set_playing(True)
        self.assertEqual(source._scaled_width(_Media()), 0)

    # 再生状態を切り替えたらデコーダを捨てて読み直させる (絵の作り直し)
    def test_set_playing_drops_decoder(self):
        source = frame_source.PyAvFrameSource(playback_width=960)
        source._decoders["m1"] = object()
        source._positions["m1"] = 12.0
        source.set_playing(True)
        self.assertNotIn("m1", source._decoders)
        self.assertNotIn("m1", source._positions)

    # 設定から生成される値 (既定は縮小 960 / 間引きなし / 前進デコード 2 秒)
    def test_create_from_settings(self):
        cfg = timeline_config({})["preview"]
        self.assertEqual(cfg["playback_width"], 960)
        self.assertEqual(cfg["playback_skip_frame"], "none")
        self.assertEqual(cfg["sequential_decode_sec"], 2.0)
        source = frame_source.create_frame_source({}, cfg)
        if isinstance(source, frame_source.PyAvFrameSource):
            self.assertEqual(source._playback_width, 960)
            self.assertEqual(source._sequential_sec, 2.0)
        source.close()

    # 不正な間引き指定は none へ寄せる (絵が欠ける設定を黙って有効にしない)
    def test_invalid_skip_frame(self):
        cfg = timeline_config({"timeline": {"preview": {"playback_skip_frame": "all"}}})
        self.assertEqual(cfg["preview"]["playback_skip_frame"], "none")

    # 従来動作へ戻せる (sequential_decode_sec=0 で常に seek する)
    def test_sequential_decode_can_be_disabled(self):
        cfg = timeline_config({"timeline": {"preview": {"sequential_decode_sec": 0}}})
        self.assertEqual(cfg["preview"]["sequential_decode_sec"], 0.0)
        source = frame_source.PyAvFrameSource(sequential_decode_sec=0)
        self.assertEqual(source._sequential_sec, 0.0)


# 本編 3 クリップ (各 10 秒・素材は 1 本) の Timeline
def _timeline():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 10.0, 10.0, 20.0, 30.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c3", "m1", 20.0, 10.0, 50.0, 60.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2"),
        AudioClip("a3", "c3", gain_db=3.0),
    ])
    return Timeline(fps=60, source={"media_id": "m1", "duration_sec": 100.0},
                    media_pool=media, tracks=[video, audio])


class TestAudioChunkSource(unittest.TestCase):

    def setUp(self):
        self.cfg = timeline_config({})["preview"]
        self.commands = []
        self._original_execute = audio_source.ffmpeg_runner.execute
        audio_source.ffmpeg_runner.execute = self._capture

    def tearDown(self):
        audio_source.ffmpeg_runner.execute = self._original_execute

    def _capture(self, command, **_kwargs):
        self.commands.append(list(command))

    def _source(self, **over):
        cfg = dict(self.cfg)
        cfg.update(over)
        return audio_source.AudioChunkSource(
            _timeline(), {"ffmpeg": {}}, ".", preview_cfg=cfg)

    # 断片が何本あっても ffmpeg は 1 回だけ起動する (resolve6 §3-6 R2)
    def test_single_process_build(self):
        source = self._source()
        pieces = source._collect_pieces(5.0, 25.0)
        self.assertGreater(len(pieces), 1)
        source._render_pieces(pieces, "out.wav")
        self.assertEqual(len(self.commands), 1)
        cmd = self.commands[0]
        # 断片ごとに -ss 付きの入力が並ぶ (1 入力 + atrim にしてはならない)
        self.assertEqual(cmd.count("-ss"), len(pieces))
        self.assertEqual(cmd.count("-i"), len(pieces))
        self.assertNotIn("atrim", " ".join(cmd))
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn(f"concat=n={len(pieces)}:v=0:a=1[out]", graph)

    # 品質設定 (24kHz mono wav) が反映される
    def test_quality_settings(self):
        source = self._source()
        source._render_pieces(source._collect_pieces(0.0, 5.0), "out.wav")
        cmd = self.commands[0]
        self.assertEqual(cmd[cmd.index("-ar") + 1], "24000")
        self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "pcm_s16le")

    # 音量指定のある断片だけ volume フィルタを挟む
    def test_gain_filter(self):
        source = self._source()
        source._render_pieces(source._collect_pieces(20.0, 25.0), "out.wav")
        graph = self.commands[0][self.commands[0].index("-filter_complex") + 1]
        self.assertIn("volume=3dB", graph)

    # per_piece へ戻すと断片ごとに ffmpeg を起動する (切り戻し / resolve6 §7)
    def test_per_piece_mode(self):
        source = self._source(audio_build_mode="per_piece")
        self.assertEqual(source._build_mode, "per_piece")
        pieces = source._collect_pieces(5.0, 25.0)
        for piece in pieces:
            source._render_piece(piece, "part.m4a")
        self.assertEqual(len(self.commands), len(pieces))

    # 起動用の短い長さを渡すと、その長さぶんの断片しか作らない (resolve6 §3-6 R3)
    def test_startup_chunk_length(self):
        source = self._source()
        pieces = source._collect_pieces(0.0, 6.0)
        self.assertAlmostEqual(sum(p["duration"] for p in pieces), 6.0, places=3)

    # チャンクは複数保持し、保持本数を超えたら古い順に捨てる (resolve6 §3-6 R5)
    def test_chunk_cache(self):
        source = self._source(audio_chunk_cache=2)
        source._chunks = [("a.wav", 0.0, 6.0), ("b.wav", 6.0, 6.0),
                          ("c.wav", 12.0, 6.0)]
        source._trim_chunks()
        self.assertEqual([c[0] for c in source._chunks], ["b.wav", "c.wav"])

    # 手元のチャンクで足りる区間はキャッシュヒットになる (作り直さない)
    def test_cached_hit(self):
        source = self._source()
        source._chunks = [("a.wav", 10.0, 30.0)]
        self.assertEqual(source.cached(12.0, 6.0), ("a.wav", 10.0))
        # 手前・後ろへはみ出す区間は当たらない
        self.assertIsNone(source.cached(5.0, 6.0))
        self.assertIsNone(source.cached(38.0, 6.0))

    # 編集が入ったら生成済みを捨てる (以降はオンデマンド生成)
    def test_invalidate(self):
        source = self._source()
        source._chunks = [("a.wav", 0.0, 6.0)]
        source.invalidate()
        self.assertEqual(source._chunks, [])
        self.assertIsNone(source.cached(0.0, 1.0))


class ScrubStateTest(unittest.TestCase):
    """ver3 resolve7 §5.12: 再生ヘッドのドラッグ状態 (スクラブ音声の開始・終了)"""

    def setUp(self):
        media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
        video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
            Clip("c1", "m1", 0.0, 30.0, 0.0, 30.0, origin={"type": ORIGIN_SILENCE_CUT}),
        ])
        audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                      clips=[AudioClip("a1", "c1")])
        self.timeline = Timeline(fps=60, source={"media_id": "m1", "duration_sec": 100.0},
                                 media_pool=media, tracks=[video, audio])
        self.controller = TimelineController(self.timeline, {})
        self.events = []
        self.controller.scrub_changed.connect(self.events.append)

    # 掴んだ・離したが 1 回ずつ通知される
    def test_begin_and_end_emit_once(self):
        self.controller.begin_scrub()
        self.controller.end_scrub()
        self.assertEqual(self.events, [True, False])
        self.assertFalse(self.controller.is_scrubbing())

    # 二重に呼んでも多重に出ない (経路が 2 つあるため保険が要る)
    def test_duplicate_calls_are_ignored(self):
        self.controller.begin_scrub()
        self.controller.begin_scrub()
        self.controller.end_scrub()
        self.controller.end_scrub()
        self.assertEqual(self.events, [True, False])

    # 再生ヘッドを動かしただけでは鳴らさない (キーボード操作・自動追従で鳴らないこと)
    def test_moving_playhead_does_not_scrub(self):
        self.controller.set_playhead(5.0)
        self.controller.step_playhead(1)
        self.assertEqual(self.events, [])
        self.assertFalse(self.controller.is_scrubbing())


class ScrubConfigTest(unittest.TestCase):
    """ver3 resolve7 §7: スクラブ設定は既定で有効・設定 1 つで従来どおり無音へ戻せる"""

    def test_defaults(self):
        cfg = timeline_config({})["preview"]
        self.assertTrue(cfg["scrub_audio_enabled"])
        self.assertEqual(cfg["scrub_interval_ms"], 60)
        self.assertEqual(cfg["scrub_rate_mode"], "grain")
        self.assertAlmostEqual(cfg["scrub_chunk_sec"], 4.0)

    def test_can_be_disabled(self):
        cfg = timeline_config(
            {"timeline": {"preview": {"scrub_audio_enabled": False}}})["preview"]
        self.assertFalse(cfg["scrub_audio_enabled"])

    # 想定外の値は既定 ("grain") へ寄せる (環境依存で無音になり得る側へ倒さない)
    def test_unknown_rate_mode_falls_back(self):
        cfg = timeline_config(
            {"timeline": {"preview": {"scrub_rate_mode": "unknown"}}})["preview"]
        self.assertEqual(cfg["scrub_rate_mode"], "grain")


if __name__ == "__main__":
    unittest.main()
