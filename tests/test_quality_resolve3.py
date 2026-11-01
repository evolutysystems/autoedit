# docs/quality/resolve3.md の各項目の単体テスト (R3a / R6 / R7 / R9 / R10 / R13)
# 実行: python -m unittest discover -s tests
# 重点 (§10 テスト計画):
#   R3a : 中間用 preset/crf が空なら従来と完全に同一の内容
#   R6  : 同一設定で 1 回だけ構築 / 設定が違えば作り直す / release_models で解放
#   R7  : ranges 指定で多入力 concat になる / 失敗で全長測定へ落ちる / 上限で間引く
#   R9  : cancel_cb が True で PipelineCancelled が貫通する / 進捗が単調増加する
#   R10 : 1 クリップ失敗で文言に出る / 全滅で TimelineError
#   R13 : 想定時間が設定の係数で決まる (ハードコードしない)
# FFmpeg は起動しない。コマンドの組み立てと分岐だけを検証する。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import clip_writer
from src.archive import config as archive_config
from src.exceptions import DiskSpaceError, PipelineCancelled, TimelineError
from src.modules import ffmpeg_runner, loudness_normalizer, subtitle_generator
from src.utils import progress as progress_util


# ------------------------------------------------------------------
# R3a: 中間ファイルのエンコード設定
# ------------------------------------------------------------------

class IntermediateEncodeTest(unittest.TestCase):

    # 空のときは最終出力と同じ preset/crf = 従来と完全に同一
    def test_empty_values_keep_the_final_settings(self):
        cfg = {"preset": "medium", "crf": 20}
        self.assertEqual(
            ffmpeg_runner.build_intermediate_encode_options(cfg, "pcm_s16le"),
            ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-c:a", "pcm_s16le"])

    # 指定があれば中間用の値を使う
    def test_intermediate_values_are_used(self):
        cfg = {"preset": "medium", "crf": 20,
               "intermediate_preset": "veryfast", "intermediate_crf": "18"}
        self.assertEqual(
            ffmpeg_runner.build_intermediate_encode_options(cfg, "pcm_s16le"),
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-c:a", "pcm_s16le"])

    # audio_codec を渡さない呼び出しは build_encode_options と同一 (切り戻し経路)
    def test_without_audio_codec_matches_build_encode_options(self):
        cfg = {"preset": "medium", "crf": 20,
               "intermediate_preset": "veryfast", "intermediate_crf": "18"}
        self.assertEqual(
            ffmpeg_runner.build_intermediate_encode_options(cfg, None),
            ffmpeg_runner.build_encode_options(cfg))

    # 空白だけの指定も「未指定」として扱う
    def test_blank_values_are_ignored(self):
        cfg = {"preset": "medium", "crf": 20,
               "intermediate_preset": "  ", "intermediate_crf": " "}
        self.assertEqual(ffmpeg_runner.intermediate_preset(cfg), "medium")
        self.assertEqual(ffmpeg_runner.intermediate_crf(cfg), "20")


# ------------------------------------------------------------------
# R6: Whisper モデルのプロセス内キャッシュ
# ------------------------------------------------------------------

class _FakeModel:

    instances = 0

    def __init__(self, name, device=None, compute_type=None):
        _FakeModel.instances += 1
        self.name = name
        self.device = device
        self.compute_type = compute_type


class WhisperModelCacheTest(unittest.TestCase):

    def setUp(self):
        _FakeModel.instances = 0
        subtitle_generator.release_models()
        self.addCleanup(subtitle_generator.release_models)

    # 同じ設定なら 1 回だけ構築して使い回す
    def test_same_settings_build_once(self):
        key = ("large-v3", "cpu", "int8")
        first = subtitle_generator._get_model(_FakeModel, key, True)
        second = subtitle_generator._get_model(_FakeModel, key, True)
        self.assertIs(first, second)
        self.assertEqual(_FakeModel.instances, 1)

    # 設定が違えば作り直し、古いモデルは抱えない (メモリ 1 本ぶんに保つ)
    def test_different_settings_rebuild(self):
        subtitle_generator._get_model(_FakeModel, ("large-v3", "cpu", "int8"), True)
        subtitle_generator._get_model(_FakeModel, ("medium", "cpu", "int8"), True)
        self.assertEqual(_FakeModel.instances, 2)
        self.assertEqual(len(subtitle_generator._model_cache), 1)

    # キャッシュ無効では毎回構築する (切り戻し経路)
    def test_cache_disabled_builds_every_time(self):
        key = ("large-v3", "cpu", "int8")
        subtitle_generator._get_model(_FakeModel, key, False)
        subtitle_generator._get_model(_FakeModel, key, False)
        self.assertEqual(_FakeModel.instances, 2)
        self.assertEqual(subtitle_generator._model_cache, {})

    # release_models で解放される
    def test_release_clears_the_cache(self):
        subtitle_generator._get_model(_FakeModel, ("large-v3", "cpu", "int8"), True)
        subtitle_generator.release_models()
        self.assertEqual(subtitle_generator._model_cache, {})

    # 設定キーの既定は true (使い回す)
    def test_default_setting_is_enabled(self):
        source = subtitle_generator.WhisperTextSource({})
        self.assertTrue(source._use_model_cache)
        self.assertFalse(
            subtitle_generator.WhisperTextSource(
                {"whisper_model_cache": False})._use_model_cache)


# ------------------------------------------------------------------
# R7: 採用区間だけのラウドネス測定
# ------------------------------------------------------------------

class LoudnessRangesTest(unittest.TestCase):

    def _config(self, scope="selected", limit=20):
        return loudness_normalizer.loudness_config(
            {"loudness": {"measure_scope": scope, "measure_max_ranges": limit}})

    def _capture(self, returncode=0, stderr=""):
        calls = []

        def _fake(cmd, total_duration=0.0, on_progress=None, progress_timeout_sec=0,
                  cancel_cb=None):
            calls.append((list(cmd), total_duration))
            return returncode, stderr

        self.addCleanup(setattr, loudness_normalizer, "run_ffmpeg_progress",
                        loudness_normalizer.run_ffmpeg_progress)
        loudness_normalizer.run_ffmpeg_progress = _fake
        return calls

    # 区間を渡すと多入力 concat のコマンドになる
    def test_ranges_build_a_multi_input_concat(self):
        calls = self._capture(returncode=1)      # JSON は見ないので失敗で止める
        loudness_normalizer.measure_loudness(
            "vod.mp4", self._config(), {}, ranges=[(10.0, 20.0), (30.0, 45.0)])
        cmd = calls[0][0]
        self.assertEqual(cmd.count("-i"), 2)
        self.assertIn("-ss", cmd)
        spec = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[0:a:0][1:a:0]concat=n=2:v=0:a=1", spec)
        self.assertIn("loudnorm=", spec)
        self.assertAlmostEqual(calls[0][1], 25.0)       # 合計尺

    # 区間測定が失敗したら全長測定へ 1 回だけ落ちる
    def test_range_failure_falls_back_to_whole_vod(self):
        calls = self._capture(returncode=1)
        loudness_normalizer.measure_loudness(
            "vod.mp4", self._config(), {}, ranges=[(10.0, 20.0)])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("-filter_complex", calls[1][0])
        self.assertIn("-vn", calls[1][0])

    # measure_scope="vod" では区間を渡しても全長を測る (切り戻し)
    def test_vod_scope_ignores_ranges(self):
        calls = self._capture(returncode=1)
        loudness_normalizer.measure_loudness(
            "vod.mp4", self._config(scope="vod"), {}, ranges=[(10.0, 20.0)])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-filter_complex", calls[0][0])

    # 区間が 0 件なら全長測定を使う
    def test_empty_ranges_use_the_whole_vod(self):
        calls = self._capture(returncode=1)
        loudness_normalizer.measure_loudness("vod.mp4", self._config(), {}, ranges=[])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-filter_complex", calls[0][0])

    # 上限を超えたら長い順に間引く
    def test_too_many_ranges_are_thinned(self):
        cfg = self._config(limit=3)
        ranges = [(0.0, 1.0), (10.0, 20.0), (30.0, 32.0), (40.0, 55.0), (60.0, 61.0)]
        picked = loudness_normalizer._pick_ranges(ranges, cfg)
        self.assertEqual(len(picked), 3)
        self.assertEqual(picked, [(10.0, 20.0), (30.0, 32.0), (40.0, 55.0)])
        # 時系列へ戻してから渡す (concat の順序が素材の順と一致する)
        self.assertEqual(picked, sorted(picked))

    # 尺 0 以下の区間は捨てる
    def test_degenerate_ranges_are_dropped(self):
        self.assertIsNone(
            loudness_normalizer._pick_ranges([(10.0, 10.0)], self._config()))


# ------------------------------------------------------------------
# R9: 進捗と中止
# ------------------------------------------------------------------

class _FakeStdout:

    # iter(obj.readline, "") で読み切れる最小の口
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


class _FakeProcess:

    # run_ffmpeg_progress が読む最小限の口だけ持つ偽プロセス
    # stdout は iter(readline, "") で消費されるため readline を持たせる
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stderr = None
        self.returncode = 0
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class CancelTest(unittest.TestCase):

    def _run(self, cancel_cb, lines=None):
        lines = lines or ["out_time_us=1000000\n", "progress=continue\n",
                          "out_time_us=2000000\n", "progress=end\n"]
        created = {}

        def _popen(_cmd, **_kwargs):
            created["process"] = _FakeProcess(lines)
            return created["process"]

        self.addCleanup(setattr, progress_util.subprocess, "Popen",
                        progress_util.subprocess.Popen)
        progress_util.subprocess.Popen = _popen
        return created, progress_util.run_ffmpeg_progress(
            ["ffmpeg"], total_duration=10.0, cancel_cb=cancel_cb)

    # cancel_cb が True を返すと PipelineCancelled が送出される
    def test_cancel_raises(self):
        created = {}

        def _popen(_cmd, **_kwargs):
            created["process"] = _FakeProcess(
                ["out_time_us=1000000\n", "progress=continue\n"])
            return created["process"]

        self.addCleanup(setattr, progress_util.subprocess, "Popen",
                        progress_util.subprocess.Popen)
        progress_util.subprocess.Popen = _popen
        with self.assertRaises(PipelineCancelled):
            progress_util.run_ffmpeg_progress(
                ["ffmpeg"], total_duration=10.0, cancel_cb=lambda: True)
        # プロセスは必ず終わらせる (書きかけのファイルを残さない)
        self.assertTrue(created["process"].terminated)

    # cancel_cb が None / False のときは従来どおり完走する
    def test_no_cancel_runs_to_completion(self):
        _created, result = self._run(None)
        self.assertEqual(result[0], 0)
        _created, result = self._run(lambda: False)
        self.assertEqual(result[0], 0)

    # cancel_cb が例外を投げても書き出しを落とさない
    def test_broken_cancel_cb_does_not_break_the_run(self):
        def _boom():
            raise RuntimeError("壊れたコールバック")

        _created, result = self._run(_boom)
        self.assertEqual(result[0], 0)

    # 進捗は単調増加し 0.0〜1.0 に収まる
    def test_progress_is_monotonic(self):
        seen = []
        created = {}

        def _popen(_cmd, **_kwargs):
            created["process"] = _FakeProcess([
                "out_time_us=2000000\n", "progress=continue\n",
                "out_time_us=5000000\n", "progress=continue\n",
                "out_time_us=10000000\n", "progress=end\n",
            ])
            return created["process"]

        self.addCleanup(setattr, progress_util.subprocess, "Popen",
                        progress_util.subprocess.Popen)
        progress_util.subprocess.Popen = _popen
        progress_util.run_ffmpeg_progress(
            ["ffmpeg"], total_duration=10.0,
            on_progress=lambda ratio, _kv: seen.append(ratio))
        self.assertEqual(seen, sorted(seen))
        self.assertAlmostEqual(seen[0], 0.2)
        self.assertAlmostEqual(seen[-1], 1.0)


class SingleStepProgressTest(unittest.TestCase):

    # use_single_step では工程内比率がそのまま全体比率になる
    def test_single_step_passes_the_ratio_through(self):
        from src.pipeline.pipeline_context import PipelineContext

        seen = []
        context = PipelineContext(
            input_path=__file__, settings={},
            progress_callback=lambda ratio, label: seen.append((ratio, label)))
        context.use_single_step()
        notify = context.progress_subcallback("字幕焼き込み")
        notify(0.0, {})
        notify(0.5, {})
        notify(1.0, {})
        self.assertEqual([round(r, 6) for r, _ in seen], [0.0, 0.5, 1.0])
        self.assertEqual({label for _, label in seen}, {"字幕焼き込み"})


# ------------------------------------------------------------------
# R10: 脱落クリップの明示
# ------------------------------------------------------------------

class SkippedClipsTest(unittest.TestCase):

    def _burned(self, index=1, theme="", parts=2):
        return {"clip": {"index": index, "theme": theme},
                "parts": ["intro", "body"][:parts], "body": "body"}

    # 脱落が無ければ従来どおり「完了」
    def test_no_notes_means_done(self):
        self.assertEqual(clip_writer._finish_label([self._burned()], []), "完了")
        self.assertEqual(clip_writer._finish_label([self._burned()]), "完了")

    # 脱落したクリップ番号が文言へ出る
    def test_skipped_clips_are_listed(self):
        label = clip_writer._finish_label([self._burned()], [3])
        self.assertIn("clip 3", label)
        self.assertIn("書き出しに失敗", label)

    # イントロカード欠落 (従来) と脱落 (追加) が並ぶ
    def test_both_notes_are_shown(self):
        burned = [self._burned(index=1, theme="テーマ", parts=1)]
        label = clip_writer._finish_label(burned, [2])
        self.assertIn("イントロカード", label)
        self.assertIn("書き出しに失敗", label)
        self.assertIn("／", label)

    # 複数の脱落は「・」で並べる
    def test_multiple_skipped_clips(self):
        label = clip_writer._finish_label([self._burned()], [2, 4])
        self.assertIn("2・4", label)


class AllClipsFailedTest(unittest.TestCase):

    # 全滅した場合はキャンセルと区別して例外にする
    def test_all_failed_raises_timeline_error(self):
        self.addCleanup(setattr, clip_writer, "_render_clips",
                        clip_writer._render_clips)
        clip_writer._render_clips = lambda *a, **k: ([], [1, 2])
        with self.assertRaises(TimelineError) as caught:
            clip_writer.finish_clips(
                "vod.mp4", {}, {}, object(), [], [], "workdir", ffmpeg_cfg={})
        self.assertIn("clip 1・2", str(caught.exception))

    # 脱落が無く 0 件なら従来どおり空リスト (= キャンセル扱い)
    def test_zero_without_skips_returns_empty(self):
        self.addCleanup(setattr, clip_writer, "_render_clips",
                        clip_writer._render_clips)
        clip_writer._render_clips = lambda *a, **k: ([], [])
        self.assertEqual(
            clip_writer.finish_clips("vod.mp4", {}, {}, object(), [], [], "workdir",
                                     ffmpeg_cfg={}),
            [])


class CheckCancelTest(unittest.TestCase):

    # 中止が要求されていれば PipelineCancelled になる
    def test_requested_cancel_raises(self):
        with self.assertRaises(PipelineCancelled):
            clip_writer._check_cancel(lambda: True)

    # 要求が無ければ何も起きない
    def test_no_request_passes(self):
        clip_writer._check_cancel(None)
        clip_writer._check_cancel(lambda: False)

    # コールバックが壊れていても処理を止めない
    def test_broken_callback_is_ignored(self):
        def _boom():
            raise RuntimeError("壊れたコールバック")

        clip_writer._check_cancel(_boom)


# ------------------------------------------------------------------
# R13: 想定書き出し時間
# ------------------------------------------------------------------

class EstimateExportTest(unittest.TestCase):

    def _settings(self, per_sec=1.06, fixed=60, per_clip=8):
        return {"archive": {"estimate": {"sec_per_content_sec": per_sec,
                                         "fixed_sec": fixed,
                                         "sec_per_clip": per_clip}}}

    # 係数は設定から読む (ハードコードしない)
    def test_uses_the_configured_coefficients(self):
        self.assertAlmostEqual(
            archive_config.estimate_export_sec(self._settings(), 1800, 6),
            1800 * 1.06 + 60 + 6 * 8)

    # 設定が無くても既定値で動く
    def test_defaults_when_unset(self):
        self.assertAlmostEqual(archive_config.estimate_export_sec({}, 1800, 6),
                               1800 * 1.06 + 60 + 6 * 8)

    # 係数を変えると結果が変わる (機械差を設定で吸収できる)
    def test_coefficients_change_the_result(self):
        slow = archive_config.estimate_export_sec(self._settings(per_sec=2.0), 1800, 6)
        fast = archive_config.estimate_export_sec(self._settings(per_sec=0.5), 1800, 6)
        self.assertGreater(slow, fast)

    # 壊れた値は既定へ落ちる
    def test_broken_values_fall_back(self):
        broken = {"archive": {"estimate": {"sec_per_content_sec": "x",
                                           "fixed_sec": None,
                                           "sec_per_clip": -5}}}
        self.assertAlmostEqual(archive_config.estimate_export_sec(broken, 1800, 6),
                               1800 * 1.06 + 60 + 6 * 8)

    # 負にはならない
    def test_never_negative(self):
        self.assertGreaterEqual(archive_config.estimate_export_sec({}, -100, -5), 0.0)


if __name__ == "__main__":
    unittest.main()
