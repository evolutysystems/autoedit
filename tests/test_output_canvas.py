# 出力キャンバスの解決 (src/modules/output_profile.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/quality/resolve3.md §10 / R2):
#   ・"source" は素材等倍。**拡大は決して行わない**
#   ・output_width/height は上限として効く (2560x1440 → 1920x1080 = 現状と同じ)
#   ・fps を標準値へ丸める (28.8 → 30) / 上限 output_fps でクランプする
#   ・寸法を偶数へ切り下げる (yuv420p は奇数を扱えない)
#   ・probe 失敗・異常値では "fixed" へ落ちる (安全側)
#   ・縦動画は寸法を 1080x1920 のまま保ち、fps だけ素材に従う
#   ・解決値は**実行用の設定コピー**へ書き戻し、保存値は変えない
# FFmpeg / ffprobe は起動しない。probe だけ差し替えて判定を検証する。
import json
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.exceptions import FFmpegError
from src.modules import ffmpeg_runner, output_profile
from src.settings import settings_window


def _settings(width=1920, height=1080, fps=60, mode="source", standards=None,
              vertical_enabled=True):
    ffmpeg_cfg = {
        "output_width": width,
        "output_height": height,
        "output_fps": fps,
        "output_scale_mode": mode,
    }
    if standards is not None:
        ffmpeg_cfg["output_fps_standards"] = standards
    return {"ffmpeg": ffmpeg_cfg,
            "vertical": {"enabled": vertical_enabled,
                         "output_width": 1080, "output_height": 1920}}


class _Probe:

    # probe_dimensions / probe_stream_params を差し替えるためのスタブ
    def __init__(self, testcase, size=(1280, 720), fps="30/1", fail=False):
        self._size = size
        self._fps = fps
        self._fail = fail
        testcase.addCleanup(
            setattr, ffmpeg_runner, "probe_dimensions", ffmpeg_runner.probe_dimensions)
        testcase.addCleanup(
            setattr, ffmpeg_runner, "probe_stream_params",
            ffmpeg_runner.probe_stream_params)
        ffmpeg_runner.probe_dimensions = self._dimensions
        ffmpeg_runner.probe_stream_params = self._params

    def _dimensions(self, _path, _cfg):
        if self._fail:
            raise FFmpegError("probe 失敗")
        return self._size

    def _params(self, _path, _cfg):
        if self._fail:
            raise FFmpegError("probe 失敗")
        return {"v_codec": "h264", "width": self._size[0], "height": self._size[1],
                "pix_fmt": "yuv420p", "fps": self._fps,
                "a_codec": "aac", "sample_rate": 48000, "channels": 2}


class ResolveCanvasTest(unittest.TestCase):

    # 720p30 の素材は等倍になる (拡大しない)
    def test_source_keeps_the_material_size(self):
        _Probe(self, size=(1280, 720), fps="30/1")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"]), (1280, 720))
        self.assertEqual(canvas["fps"], 30)
        self.assertEqual(canvas["mode"], "source")

    # 2K (2560x1440) は上限 1920x1080 へ縮小される = 現状と同じ出力
    def test_2k_is_capped_to_the_limit(self):
        _Probe(self, size=(2560, 1440), fps="60/1")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"]), (1920, 1080))
        self.assertEqual(canvas["fps"], 60)

    # 4K も同じく 1920x1080 へ縮小される
    def test_4k_is_capped_to_the_limit(self):
        _Probe(self, size=(3840, 2160), fps="60/1")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"]), (1920, 1080))

    # 1080p の素材は等倍 (従来と同一の出力)
    def test_1080p_is_unchanged(self):
        _Probe(self, size=(1920, 1080), fps="60/1")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"]), (1920, 1080))
        self.assertEqual(canvas["fps"], 60)

    # 4:3 の素材はアスペクト比を保ったまま収まる
    def test_aspect_ratio_is_kept(self):
        _Probe(self, size=(1440, 1080), fps="30/1")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"]), (1440, 1080))

    # 奇数の寸法は偶数へ切り下げる
    def test_odd_size_is_rounded_down_to_even(self):
        _Probe(self, size=(1281, 721), fps="30/1")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"]), (1280, 720))

    # 非整数の fps は標準値へ丸める (実測ログの 28.8fps)
    def test_fps_is_rounded_to_a_standard_value(self):
        _Probe(self, size=(1280, 720), fps="144/5")      # = 28.8
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertEqual(canvas["fps"], 30)

    # 59.94 (60000/1001) は 60 へ丸める
    def test_ntsc_fps_is_rounded_to_60(self):
        _Probe(self, size=(1920, 1080), fps="60000/1001")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertEqual(canvas["fps"], 60)

    # 素材の fps が上限を超えたらクランプする
    def test_fps_is_capped(self):
        _Probe(self, size=(1280, 720), fps="120/1")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings(fps=60))
        self.assertEqual(canvas["fps"], 60)

    # 候補が空なら丸めず素材の値を使う
    def test_empty_standards_keeps_the_source_fps(self):
        _Probe(self, size=(1280, 720), fps="144/5")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings(standards=[]))
        self.assertEqual(canvas["fps"], 29)             # 28.8 を四捨五入

    # mode="fixed" は従来どおり設定値をそのまま使う (probe も見ない)
    def test_fixed_uses_the_configured_values(self):
        _Probe(self, size=(1280, 720), fps="30/1")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings(mode="fixed"))
        self.assertEqual((canvas["width"], canvas["height"], canvas["fps"]),
                         (1920, 1080, 60))
        self.assertEqual(canvas["mode"], "fixed")

    # probe が失敗したら固定キャンバスへ落ちる (書き出しは止めない)
    def test_probe_failure_falls_back_to_fixed(self):
        _Probe(self, fail=True)
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"], canvas["fps"]),
                         (1920, 1080, 60))
        self.assertEqual(canvas["mode"], "fixed")

    # 異常に小さい素材でも 2px 未満にはしない
    def test_tiny_source_is_not_degenerate(self):
        _Probe(self, size=(2, 2), fps="30/1")
        canvas = output_profile.resolve_canvas("vod.mp4", _settings())
        self.assertGreaterEqual(canvas["width"], 2)
        self.assertGreaterEqual(canvas["height"], 2)


class ApplyCanvasTest(unittest.TestCase):

    # 解決値は実行用のコピーへ書き戻し、元の設定は変えない
    def test_original_settings_are_untouched(self):
        settings = _settings()
        resolved = output_profile.apply_canvas_to_settings(
            settings, canvas={"width": 1280, "height": 720, "fps": 30})
        self.assertEqual(resolved["ffmpeg"]["output_width"], 1280)
        self.assertEqual(resolved["ffmpeg"]["output_height"], 720)
        self.assertEqual(resolved["ffmpeg"]["output_fps"], 30)
        self.assertEqual(settings["ffmpeg"]["output_width"], 1920)
        self.assertEqual(settings["ffmpeg"]["output_height"], 1080)
        self.assertEqual(settings["ffmpeg"]["output_fps"], 60)

    # Timeline の軸は書き戻した output_fps から作られる
    # (timeline/builder と archive/timeline_builder がどちらもこの値を読む)
    def test_output_fps_is_the_timeline_axis(self):
        resolved = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 1280, "height": 720, "fps": 30})
        self.assertEqual(resolved["ffmpeg"]["output_fps"], 30)

    # 基準の高さ (テロップの絶対 px の拠り所) は上書き前の値を保つ
    def test_reference_height_survives(self):
        resolved = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 1280, "height": 720, "fps": 30})
        self.assertEqual(output_profile.reference_height(resolved["ffmpeg"]), 1080)

    # 2 回通しても基準はずれない
    def test_reference_height_is_stable_across_two_passes(self):
        once = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 1280, "height": 720, "fps": 30})
        twice = output_profile.apply_canvas_to_settings(
            once, canvas={"width": 1280, "height": 720, "fps": 30})
        self.assertEqual(output_profile.reference_height(twice["ffmpeg"]), 1080)

    # input_path も canvas も無ければ何もしない (従来の呼び出しを壊さない)
    def test_without_input_nothing_changes(self):
        settings = _settings()
        self.assertIs(output_profile.apply_canvas_to_settings(settings), settings)


class OutputProfileReferenceTest(unittest.TestCase):

    # 素材等倍で縮んだときは PlayRes 用の基準が 1920x1080 になる
    def test_landscape_reference_is_the_authored_canvas(self):
        resolved = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 1280, "height": 720, "fps": 30})
        _Probe(self, size=(1280, 720), fps="30/1")
        profile = output_profile.resolve_output_profile("vod.mp4", resolved)
        self.assertEqual((profile["width"], profile["height"]), (1280, 720))
        self.assertEqual((profile["reference_width"], profile["reference_height"]),
                         (1920, 1080))
        self.assertAlmostEqual(profile["scale"], 720 / 1080.0)

    # 1080p 以上では基準 = 実キャンバス / scale=1.0 (= 従来と同一の ASS)
    def test_1080p_reference_equals_the_canvas(self):
        _Probe(self, size=(1920, 1080), fps="60/1")
        profile = output_profile.resolve_output_profile("vod.mp4", _settings())
        self.assertEqual((profile["reference_width"], profile["reference_height"]),
                         (1920, 1080))
        self.assertEqual(profile["scale"], 1.0)

    # 4:3 の素材では基準も 4:3 になる (縦横が食い違わない)
    def test_reference_keeps_the_canvas_aspect_ratio(self):
        resolved = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 960, "height": 720, "fps": 30})
        _Probe(self, size=(960, 720), fps="30/1")
        profile = output_profile.resolve_output_profile("vod.mp4", resolved)
        self.assertEqual(profile["reference_height"], 1080)
        self.assertEqual(profile["reference_width"], 1440)   # 960 / (720/1080)

    # 縦動画は寸法 1080x1920 のまま / fps だけ素材に従う
    def test_portrait_keeps_the_fixed_canvas(self):
        resolved = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 1080, "height": 1920, "fps": 30})
        _Probe(self, size=(1080, 1920), fps="30/1")
        profile = output_profile.resolve_output_profile("vod.mp4", resolved)
        self.assertTrue(profile["is_portrait"])
        self.assertEqual((profile["width"], profile["height"]), (1080, 1920))
        self.assertEqual(profile["fps"], 30)


# ------------------------------------------------------------------
# ver11 resolve2: 実行時の解決値を保存値へ残さない (R1 / R2 / R3) と
#                 縦素材の上限枠 (R4)
# ------------------------------------------------------------------

class RuntimeCanvasPersistTest(unittest.TestCase):

    # 保存先を一時ファイルへ差し替える (実ファイルを汚さない)
    def _redirect_settings_file(self):
        directory = tempfile.mkdtemp(prefix="autoedit_settings_")
        self.addCleanup(shutil.rmtree, directory, True)
        self.addCleanup(setattr, settings_window, "SETTINGS_FILE",
                        settings_window.SETTINGS_FILE)
        self.addCleanup(setattr, settings_window, "SETTINGS_DIR",
                        settings_window.SETTINGS_DIR)
        settings_window.SETTINGS_DIR = directory
        settings_window.SETTINGS_FILE = os.path.join(directory, "setting.json")
        return settings_window.SETTINGS_FILE

    def _saved(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # R1/R2: 解決済みのコピーを保存しても、保存値は元の上限枠のまま
    def test_resolved_canvas_is_not_saved(self):
        path = self._redirect_settings_file()
        resolved = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 606, "height": 1080, "fps": 60})
        settings_window.save_settings(resolved)
        saved = self._saved(path)["ffmpeg"]
        self.assertEqual(saved["output_width"], 1920)
        self.assertEqual(saved["output_height"], 1080)
        self.assertEqual(saved["output_fps"], 60)

    # R2: 実行時だけのキーはファイルに残らない
    def test_runtime_keys_are_not_saved(self):
        path = self._redirect_settings_file()
        resolved = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 1280, "height": 720, "fps": 30})
        settings_window.save_settings(resolved)
        saved = self._saved(path)["ffmpeg"]
        self.assertNotIn(output_profile.OUTPUT_REFERENCE_KEY, saved)
        self.assertNotIn(output_profile.OUTPUT_CANVAS_BACKUP_KEY, saved)

    # R2: 解決を通していない設定は手を加えずそのまま保存する (従来と同一)
    def test_untouched_settings_pass_through(self):
        settings = _settings()
        self.assertIs(output_profile.without_runtime_canvas(settings), settings)

    # R2: 渡された設定は書き換えない (実行中の工程が見ている解決値を壊さない)
    def test_cleanup_does_not_mutate_the_running_settings(self):
        resolved = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 1280, "height": 720, "fps": 30})
        output_profile.without_runtime_canvas(resolved)
        self.assertEqual(resolved["ffmpeg"]["output_width"], 1280)
        self.assertIn(output_profile.OUTPUT_CANVAS_BACKUP_KEY, resolved["ffmpeg"])

    # R2: 2 回通してからの保存でも、最初の保存値へ戻る
    def test_two_passes_still_restore_the_saved_values(self):
        path = self._redirect_settings_file()
        once = output_profile.apply_canvas_to_settings(
            _settings(), canvas={"width": 1280, "height": 720, "fps": 30})
        twice = output_profile.apply_canvas_to_settings(
            once, canvas={"width": 606, "height": 1080, "fps": 60})
        settings_window.save_settings(twice)
        saved = self._saved(path)["ffmpeg"]
        self.assertEqual((saved["output_width"], saved["output_height"]), (1920, 1080))

    # R3: 控えを持たない版が焼き付けた保存値を復元する (606x1080 → 1920x1080)
    def test_broken_settings_are_repaired(self):
        ffmpeg_cfg = {"output_width": 606, "output_height": 1080, "output_fps": 60,
                      output_profile.OUTPUT_REFERENCE_KEY: 1080}
        self.assertTrue(output_profile.restore_runtime_canvas(ffmpeg_cfg))
        self.assertEqual((ffmpeg_cfg["output_width"], ffmpeg_cfg["output_height"]),
                         (1920, 1080))
        self.assertNotIn(output_profile.OUTPUT_REFERENCE_KEY, ffmpeg_cfg)

    # R3: 正常な設定には触れない
    def test_clean_settings_are_untouched(self):
        ffmpeg_cfg = {"output_width": 1920, "output_height": 1080, "output_fps": 60}
        self.assertFalse(output_profile.restore_runtime_canvas(ffmpeg_cfg))
        self.assertEqual(ffmpeg_cfg,
                         {"output_width": 1920, "output_height": 1080, "output_fps": 60})

    # R3: 壊れた setting.json は起動時の読み込みで直り、直った内容が保存される
    def test_load_settings_repairs_and_persists(self):
        path = self._redirect_settings_file()
        broken = {"ffmpeg": {"output_width": 606, "output_height": 1080,
                             "output_fps": 60,
                             output_profile.OUTPUT_REFERENCE_KEY: 1080}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(broken, f)
        loaded = settings_window.load_settings()
        self.assertEqual(loaded["ffmpeg"]["output_width"], 1920)
        self.assertEqual(self._saved(path)["ffmpeg"]["output_width"], 1920)


class PortraitCanvasLimitTest(unittest.TestCase):

    # R4: 縦素材は縦の枠 (1080x1920) で収める (横枠だと 606x1080 まで潰れていた)
    def test_portrait_source_uses_the_vertical_limits(self):
        _Probe(self, size=(1278, 2272), fps="60/1")
        canvas = output_profile.resolve_canvas("clip.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"]), (1080, 1920))

    # R4: 縦の枠に収まる縦素材は等倍のまま (拡大しない)
    def test_small_portrait_source_is_not_enlarged(self):
        _Probe(self, size=(720, 1280), fps="30/1")
        canvas = output_profile.resolve_canvas("clip.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"]), (720, 1280))

    # R4: 縦対応が無効なら従来どおり横の枠で収める (切り戻し経路)
    def test_vertical_disabled_keeps_the_landscape_limits(self):
        _Probe(self, size=(1278, 2272), fps="60/1")
        canvas = output_profile.resolve_canvas(
            "clip.mp4", _settings(vertical_enabled=False))
        self.assertEqual((canvas["width"], canvas["height"]), (606, 1080))

    # R4: 正方形は横扱いのまま (resolve_output_profile の判定と揃える)
    def test_square_source_uses_the_landscape_limits(self):
        _Probe(self, size=(1440, 1440), fps="30/1")
        canvas = output_profile.resolve_canvas("clip.mp4", _settings())
        self.assertEqual((canvas["width"], canvas["height"]), (1080, 1080))


if __name__ == "__main__":
    unittest.main()
