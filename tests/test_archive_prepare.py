# アーカイブ切り抜きの prepare 経路 (src/archive/clip_writer.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/error/20260811/resolve.md §9-1):
#   ・Timeline 経路では実カット (silence_cutter.run / _silence_cut) を行わないこと
#   ・Timeline 経路の字幕が recognize_for_timeline 由来であること
#     (= 残す区間を詰めた理想の軸。実カット後ファイルの軸ではない)
#   ・従来画面経路 (timeline_review=false) の手順が変わっていないこと
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import clip_writer

# 出力プロファイル (横) と、切り抜き対象クリップ 1 件
_PROFILE = {"width": 1920, "height": 1080, "orientation": "landscape",
            "is_portrait": False}
_USED = [{"index": 1, "start": 100.0, "end": 130.0, "score": 88.0, "use": True}]

_SETTINGS = {
    "subtitle": {"enabled": True, "engine": "whisper"},
    "vertical": {"enabled": True},
    "volume_analysis": {"last_cut_db": -27},
    "ffmpeg": {"output_width": 1920, "output_height": 1080, "output_fps": 60},
}

# 編集点検出の戻り値 (残す区間 2 件・素材 30 秒)
_KEEP = [(0.0, 5.0), (10.0, 15.0)]
_META = {"detector": "silencedetect", "source_duration_sec": 30.0}
# 区間を詰めた後の軸で得られる字幕 (0-10 秒の範囲へ収まる)
_ITEMS = [{"start": 1.0, "end": 3.0, "text": "あ", "use": True,
           "role": "streamer", "font": "", "font_size": None},
          {"start": 6.0, "end": 8.0, "text": "い", "use": True,
           "role": "streamer", "font": "", "font_size": None}]


class PrepareClipsTest(unittest.TestCase):

    def setUp(self):
        self._workdir_obj = tempfile.TemporaryDirectory(prefix="archive_prepare_test_")
        self.workdir = self._workdir_obj.name
        self.calls = []          # 呼ばれた工程を順に記録する

        def _track(name, result):
            def _inner(*_args, **_kwargs):
                self.calls.append(name)
                return result
            return _inner

        # FFmpeg を起こす処理はすべて差し替える (テストは分岐だけを見る)
        self.patches = [
            mock.patch.object(clip_writer, "cut_region",
                              side_effect=_track("cut_region", "raw.mp4")),
            mock.patch.object(clip_writer.loudness_normalizer, "normalize_file",
                              side_effect=_track("normalize", "normalized.mp4")),
            mock.patch.object(clip_writer, "_apply_volume_analysis",
                              side_effect=_track("volume", None)),
            mock.patch.object(clip_writer.output_profile, "resolve_output_profile",
                              side_effect=_track("profile", _PROFILE)),
            mock.patch.object(clip_writer.ffmpeg_runner, "probe_duration",
                              side_effect=_track("probe_duration", 30.0)),
            # Timeline 経路
            mock.patch.object(clip_writer.silence_cutter, "detect_edit_points",
                              side_effect=_track("detect", (_KEEP, _META))),
            mock.patch.object(clip_writer.subtitle_generator, "recognize_for_timeline",
                              side_effect=_track("recognize", (_ITEMS, {"enabled": True}))),
            # 従来画面経路
            mock.patch.object(clip_writer.silence_cutter, "run",
                              side_effect=_track("silence_run", "silence_cut.mp4")),
            mock.patch.object(clip_writer, "_silence_cut",
                              side_effect=_track("silence_cut", ("silence_cut.mp4", _KEEP))),
            mock.patch.object(clip_writer, "_transcribe",
                              side_effect=_track("transcribe", _ITEMS)),
        ]
        self.mocks = {p.attribute: p.start() for p in self.patches}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._workdir_obj.cleanup()

    def _prepare(self, use_timeline):
        return clip_writer._prepare_clips(
            "vod.mp4", _SETTINGS, dict(_SETTINGS), _USED,
            _SETTINGS["ffmpeg"], self.workdir, None, use_timeline=use_timeline)

    # Timeline 経路では実カットしない (これが字幕ズレの根本原因だった / §2-3)
    def test_timeline_path_does_not_cut(self):
        self._prepare(use_timeline=True)
        self.assertNotIn("silence_cut", self.calls)
        self.assertNotIn("silence_run", self.calls)

    # Timeline 経路は 編集点検出 → 区間音声で認識 の順 (クリップ用と同じ 2 段構え)
    def test_timeline_path_detects_then_recognizes(self):
        self._prepare(use_timeline=True)
        self.assertEqual(
            [c for c in self.calls if c in ("detect", "recognize")],
            ["detect", "recognize"])

    # 字幕は recognize_for_timeline の戻り値 (= 残す区間を詰めた理想の軸)
    def test_timeline_path_items_come_from_recognizer(self):
        prepared = self._prepare(use_timeline=True)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0]["items"], _ITEMS)
        self.assertEqual(prepared[0]["keep_segments"], _KEEP)

    # 実カット後ファイルは作らないため prepared_path は空。素材は normalized_path
    def test_timeline_path_has_no_prepared_path(self):
        prepared = self._prepare(use_timeline=True)
        self.assertEqual(prepared[0]["prepared_path"], "")
        self.assertEqual(prepared[0]["normalized_path"], "normalized.mp4")

    # 素材尺は detect_edit_points の meta を流用する (ffprobe の重複呼び出しをしない)
    def test_timeline_path_reuses_probed_duration(self):
        prepared = self._prepare(use_timeline=True)
        self.assertAlmostEqual(prepared[0]["normalized_duration"], 30.0, places=6)
        self.assertNotIn("probe_duration", self.calls)

    # 出力プロファイルは実カット前の normalized で判定する (寸法しか見ないため同値)
    def test_timeline_path_profiles_normalized(self):
        self._prepare(use_timeline=True)
        self.mocks["resolve_output_profile"].assert_called_once_with(
            "normalized.mp4", _SETTINGS)

    # 認識が空 (エンジン無効・失敗) でもクリップは失わない
    def test_timeline_path_survives_empty_items(self):
        self.mocks["recognize_for_timeline"].side_effect = None
        self.mocks["recognize_for_timeline"].return_value = ([], {"enabled": True})
        prepared = self._prepare(use_timeline=True)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0]["items"], [])

    # 従来画面経路は現状のまま (実カット → 実カット後ファイルで認識)
    def test_legacy_path_keeps_real_cut(self):
        prepared = self._prepare(use_timeline=False)
        self.assertEqual(
            [c for c in self.calls if c in ("silence_cut", "transcribe", "detect",
                                            "recognize")],
            ["silence_cut", "transcribe"])
        self.assertEqual(prepared[0]["prepared_path"], "silence_cut.mp4")
        self.assertAlmostEqual(prepared[0]["normalized_duration"], 30.0, places=6)
        self.mocks["resolve_output_profile"].assert_called_once_with(
            "silence_cut.mp4", _SETTINGS)

    # 既定は従来画面経路 (引数を渡さない呼び出しの互換)
    def test_default_is_legacy_path(self):
        clip_writer._prepare_clips(
            "vod.mp4", _SETTINGS, dict(_SETTINGS), _USED,
            _SETTINGS["ffmpeg"], self.workdir, None)
        self.assertIn("silence_cut", self.calls)
        self.assertNotIn("detect", self.calls)


class PrepareEditPointsTest(unittest.TestCase):

    # 編集点検出には音量解析で確定した閾値 (clip_settings) と縦横プロファイルを渡す
    def test_context_carries_settings_and_profile(self):
        seen = {}

        def _detect(ctx):
            seen["settings"] = ctx.settings
            seen["input"] = ctx.current_video_path()
            seen["profile"] = ctx.output_profile
            return _KEEP, _META

        def _recognize(ctx, keep_segments):
            seen["recognize_keep"] = keep_segments
            seen["recognize_profile"] = ctx.output_profile
            return _ITEMS, {"enabled": True}

        clip_settings = {"volume_analysis": {"last_cut_db": -31}}
        with tempfile.TemporaryDirectory(prefix="archive_edit_points_test_") as clip_dir, \
                mock.patch.object(clip_writer.silence_cutter, "detect_edit_points",
                                  side_effect=_detect), \
                mock.patch.object(clip_writer.subtitle_generator,
                                  "recognize_for_timeline", side_effect=_recognize):
            keep, items, duration = clip_writer._prepare_edit_points(
                "normalized.mp4", clip_settings, clip_dir, _PROFILE)

        self.assertIs(seen["settings"], clip_settings)
        self.assertEqual(seen["input"], "normalized.mp4")
        self.assertEqual(seen["profile"], _PROFILE)
        self.assertEqual(seen["recognize_keep"], _KEEP)
        self.assertEqual(seen["recognize_profile"], _PROFILE)
        self.assertEqual(keep, _KEEP)
        self.assertEqual(items, _ITEMS)
        self.assertAlmostEqual(duration, 30.0, places=6)


if __name__ == "__main__":
    unittest.main()
