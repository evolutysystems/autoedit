# トラッキングぼかしの焼き込み経路 (src/modules/blur_overlay.py / renderer) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve2.md §6 Phase 3〜5 の完了条件):
#   ・機能 OFF なら従来と 1 文字も変わらないこと (R9)
#   ・ぼかしのチェーンが**先頭**に入り、字幕・アイコンをぼかさないこと (R8)
#   ・eof_action=pass が必ず入ること (§3.5 の既知の落とし穴)
#   ・アーカイブ用のサブ Timeline へ指定が引き継がれること (§5.5.4 / §2.8)
#   ・指定が Undo で戻せること (§5.6.5)
#   ・マスクの計画が「ぼかす対象」だけを拾うこと (§5.4)
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import timeline_builder as archive_timeline
from src.blur import decisions as blur_decisions
from src.blur import mask_builder
from src.blur.config import config
from src.modules import blur_overlay
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


# 本編 2 クリップ (各 10 秒) の Timeline を組む
def _build_timeline():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 10.0, 10.0, 30.0, 40.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2"),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=60, width=1920, height=1080,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


# 解析結果の最小形 (人物 2 人 / 主役は p1)
def _build_analysis():
    return {
        "schema": 2,
        "fingerprint": "fp",
        "canvas": {"width": 1920, "height": 1080},
        "sample_fps": 5.0,
        "identities": [
            {"id": "p1", "total_sec": 20.0, "main": True, "tracks": ["t1"]},
            {"id": "p2", "total_sec": 4.0, "main": False, "tracks": ["t2"]},
        ],
        "tracks": [
            {"id": "t1", "identity": "p1", "media_id": "m1", "kind": "person",
             "start_sec": 0.0, "end_sec": 8.0,
             "samples": [{"t": 0.0, "x": 100, "y": 100, "w": 200, "h": 500, "score": 0.9},
                         {"t": 8.0, "x": 140, "y": 100, "w": 200, "h": 500, "score": 0.9}]},
            {"id": "t2", "identity": "p2", "media_id": "m1", "kind": "person",
             "start_sec": 2.0, "end_sec": 6.0,
             "samples": [{"t": 2.0, "x": 900, "y": 120, "w": 180, "h": 480, "score": 0.8},
                         {"t": 6.0, "x": 940, "y": 120, "w": 180, "h": 480, "score": 0.8}]},
        ],
    }


class BlurOverlayChainTest(unittest.TestCase):

    def setUp(self):
        self.cfg = config({"blur": {"enabled": True}})
        self._tmp = tempfile.TemporaryDirectory()
        self.mask_path = os.path.join(self._tmp.name, "mask.mkv")
        with open(self.mask_path, "wb") as handle:
            handle.write(b"dummy")      # 実体があることだけを見る

    def tearDown(self):
        self._tmp.cleanup()

    # マスクが無ければチェーンを 1 本も作らないこと (R9)
    def test_no_mask_no_chains(self):
        chains, label = blur_overlay.build_chains(
            "", "[0:v]", "[out]", 1920, 1080, self.cfg)
        self.assertEqual(chains, [])
        self.assertEqual(label, "[0:v]")

    # 仕様どおりのチェーンが出ること (§3.5)
    def test_chain_shape(self):
        chains, label = blur_overlay.build_chains(
            self.mask_path, "[0:v]", "[out]", 1920, 1080, self.cfg)
        self.assertEqual(label, "[out]")
        joined = ";".join(chains)
        # 先頭は split (簡易フィルタグラフでラベル無し入力を受けられるようにするため)
        self.assertTrue(chains[0].startswith("[0:v]split=2"))
        self.assertIn("movie=", joined)
        self.assertIn("format=gray", joined)
        self.assertIn("alphamerge", joined)
        self.assertIn("gblur=sigma=", joined)
        # eof_action=pass が無いと、マスクが尽きた後も最後の絵が残り続ける
        self.assertIn("overlay=0:0:eof_action=pass[out]", joined)

    # モザイクを選ぶと scale の往復になること
    def test_pixelate_mode(self):
        cfg = config({"blur": {"enabled": True, "render": {"mode": "pixelate"}}})
        chains, _label = blur_overlay.build_chains(
            self.mask_path, "[0:v]", "[out]", 1920, 1080, cfg)
        joined = ";".join(chains)
        self.assertIn("flags=neighbor", joined)
        self.assertNotIn("gblur", joined)

    # ぼかしの強さはキャンバス幅に対する比率で決まること (横と縦で見え方を揃える)
    def test_sigma_scales_with_canvas_width(self):
        from src.blur.config import blur_sigma

        self.assertAlmostEqual(blur_sigma(1920, self.cfg), 1920 * 0.00025 * 50)
        self.assertLess(blur_sigma(1080, self.cfg), blur_sigma(1920, self.cfg))

    # 出力直前の保険: マスクが無い / 適用済みなら何もしないこと (§5.5.3)
    def test_is_required(self):
        context = _FakeContext()
        self.assertFalse(blur_overlay.is_required(context))
        context.blur_mask_path = self.mask_path
        self.assertTrue(blur_overlay.is_required(context))
        context.blur_applied = True
        self.assertFalse(blur_overlay.is_required(context))

    # ぼかし機能を通らない呼び出し (CLI / テスト) では属性が無くても False になること
    def test_is_required_without_attributes(self):
        self.assertFalse(blur_overlay.is_required(object()))


class _FakeContext:

    def __init__(self):
        self.blur_mask_path = None
        self.blur_applied = False
        self.blur_mask_failed = False


class MaskPlanTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()
        self.analysis = _build_analysis()
        self.cfg = config({"blur": {"enabled": True}})

    # 既定方針 (blur_others) では、主役以外だけが対象になること (R3)
    def test_plan_blurs_others_only(self):
        state = blur_decisions.load(self.timeline)
        plan = mask_builder.build_plan(self.timeline, self.analysis, state, self.cfg)
        self.assertEqual(len(plan["shapes"]), 1)
        self.assertEqual(plan["shapes"][0]["samples"][0]["x"], 900)

    # manual_only では、明示指定が無ければ 1 つも対象にならないこと
    def test_plan_manual_only(self):
        state = blur_decisions.load(self.timeline)
        state["default_policy"] = "manual_only"
        plan = mask_builder.build_plan(self.timeline, self.analysis, state, self.cfg)
        self.assertEqual(plan["shapes"], [])

    # 主役を明示指定すればぼかす対象に入ること
    def test_plan_explicit_main(self):
        state = blur_decisions.with_identity(
            blur_decisions.load(self.timeline), "p1", blur_decisions.BLUR)
        plan = mask_builder.build_plan(self.timeline, self.analysis, state, self.cfg)
        self.assertEqual(len(plan["shapes"]), 2)

    # 取りこぼし対策として、対象の区間が前後へ pad_sec ぶん伸びること
    def test_plan_pads_span(self):
        state = blur_decisions.load(self.timeline)
        plan = mask_builder.build_plan(self.timeline, self.analysis, state, self.cfg)
        pad = self.cfg["render"]["pad_sec"]
        self.assertAlmostEqual(plan["shapes"][0]["start"], 2.0 - pad)
        self.assertAlmostEqual(plan["shapes"][0]["end"], 6.0 + pad)

    # 矩形の線形補間 (samples は sample_fps 間隔しか持たない / §5.2.1)
    def test_rect_interpolation(self):
        shape = {"samples": self.analysis["tracks"][0]["samples"]}
        rect = mask_builder._rect_at(shape, 4.0)        # 0.0 と 8.0 の中間
        self.assertAlmostEqual(rect[0], 120.0)
        # 区間の外は端の矩形をそのまま使う (pad_sec ぶんのはみ出しに備える)
        self.assertAlmostEqual(mask_builder._rect_at(shape, -1.0)[0], 100.0)
        self.assertAlmostEqual(mask_builder._rect_at(shape, 99.0)[0], 140.0)


class DecisionsPersistenceTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()

    # 指定が source["blur"] へ入り、読み書きで往復すること (§5.2.2)
    def test_round_trip(self):
        state = blur_decisions.load(self.timeline)
        state = blur_decisions.with_identity(state, "p2", blur_decisions.BLUR)
        blur_decisions.store(self.timeline, state)

        self.assertIn("blur", self.timeline.source)
        reloaded = blur_decisions.load(self.timeline)
        self.assertEqual(reloaded["identities"], {"p2": "blur"})

    # 指定が Undo で戻せること (§5.6.5 / _snapshot が source を deepcopy する)
    def test_undo(self):
        stack = commands.CommandStack()
        self.assertTrue(stack.push(
            self.timeline, commands.SetBlurDecision("p2", blur_decisions.BLUR)))
        self.assertEqual(blur_decisions.load(self.timeline)["identities"], {"p2": "blur"})

        stack.undo(self.timeline)
        self.assertEqual(blur_decisions.load(self.timeline)["identities"], {})

        stack.redo(self.timeline)
        self.assertEqual(blur_decisions.load(self.timeline)["identities"], {"p2": "blur"})

    # 同じ指定を 2 回積んでも履歴を汚さないこと
    def test_no_op_is_not_pushed(self):
        stack = commands.CommandStack()
        stack.push(self.timeline, commands.SetBlurDecision("p2", blur_decisions.BLUR))
        self.assertFalse(stack.push(
            self.timeline, commands.SetBlurDecision("p2", blur_decisions.BLUR)))

    # 領域の追加・削除
    def test_region_commands(self):
        stack = commands.CommandStack()
        region = {"mode": "blur", "label": "建物A", "media_id": "m1",
                  "anchor_sec": 4.0, "path": [[0.1, 0.1], [0.3, 0.1], [0.3, 0.4]]}
        self.assertTrue(stack.push(self.timeline, commands.AddBlurRegion(region)))
        regions = blur_decisions.load(self.timeline)["regions"]
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["id"], "r1")

        self.assertTrue(stack.push(self.timeline, commands.RemoveBlurRegion("r1")))
        self.assertEqual(blur_decisions.load(self.timeline)["regions"], [])

    # 解析結果の在りかを書き留められること (これが無いと書き出しで見つけられない)
    def test_set_analysis(self):
        stack = commands.CommandStack()
        cache = os.path.join(tempfile.gettempdir(), "sample.blur.json")
        project = os.path.join(tempfile.gettempdir(), "sample.timeline.json")
        self.assertTrue(stack.push(
            self.timeline,
            commands.SetBlurAnalysis(cache, "fp123", project_path=project)))
        state = blur_decisions.load(self.timeline)
        self.assertEqual(state["fingerprint"], "fp123")
        self.assertEqual(state["cache_abs"], cache)
        # プロジェクトの隣にあるため相対パスはファイル名だけになる
        self.assertEqual(state["cache"], "sample.blur.json")


class ArchiveInheritanceTest(unittest.TestCase):

    # アーカイブ用のサブ Timeline へ指定が引き継がれること (§2.8 / §5.5.4)
    # これが無いと**アーカイブ用の書き出しだけぼかしが消える**。
    def test_source_is_inherited(self):
        timeline = _build_timeline()
        timeline.source["archive"] = {"clips": [
            {"index": 1, "start": 0.0, "end": 10.0},
            {"index": 2, "start": 10.0, "end": 20.0},
        ]}
        timeline.base_clips()[0].origin = {"type": ORIGIN_SILENCE_CUT, "archive_clip": 1}
        timeline.base_clips()[1].origin = {"type": ORIGIN_SILENCE_CUT, "archive_clip": 2}

        state = blur_decisions.with_identity(
            blur_decisions.load(timeline), "p2", blur_decisions.BLUR)
        blur_decisions.store(timeline, state)

        groups = archive_timeline.split_by_clip(timeline)
        self.assertGreaterEqual(len(groups), 1)
        for _index, sub in groups:
            self.assertEqual(
                blur_decisions.load(sub)["identities"], {"p2": "blur"},
                "サブ Timeline へぼかし指定が引き継がれていません")


if __name__ == "__main__":
    unittest.main()


class BurnSubtitlePreChainTest(unittest.TestCase):

    # ぼかしは字幕を焼く**前**へ入ること (R8)。
    # 後ろ (extra_chains) へ入れると字幕・コメントアイコンまでぼけてしまう。
    def setUp(self):
        from src.modules import subtitle_generator

        self._module = subtitle_generator
        self._original = subtitle_generator.ffmpeg_runner.execute
        self.commands = []
        subtitle_generator.ffmpeg_runner.execute = self._capture

    def tearDown(self):
        self._module.ffmpeg_runner.execute = self._original

    def _capture(self, cmd, **_kwargs):
        self.commands.append(list(cmd))

    def _filter_spec(self):
        index = self.commands[0].index("-vf")
        return self.commands[0][index + 1]

    # pre_chains を渡さなければ従来と完全に同じ -vf になること (R9)
    def test_without_pre_chains(self):
        self._module.burn_subtitle("in.mp4", "s.ass", "out.mp4", {})
        spec = self._filter_spec()
        self.assertTrue(spec.startswith("fps="))
        self.assertNotIn(";", spec)

    # pre_chains は先頭へ入り、その出力ラベルが字幕チェーンの入力になること
    def test_with_pre_chains(self):
        self._module.burn_subtitle(
            "in.mp4", "s.ass", "out.mp4", {},
            pre_chains=["split=2[k][s]", "[s]gblur=sigma=4[b]",
                        "[k][b]overlay=0:0:eof_action=pass[vpre]"])
        spec = self._filter_spec()
        self.assertTrue(spec.startswith("split=2[k][s]"))
        self.assertIn("[vpre]fps=", spec)
        # 字幕はぼかしの後ろ = ぼけない
        self.assertLess(spec.index("gblur"), spec.index("subtitles="))


class MaskBuildTest(unittest.TestCase):

    # マスク動画が実際に作れること (PIL で描き、PyAV/FFmpeg で可逆エンコードする)
    # Phase 3 の完了条件「マスク動画が作れる」に対応する。
    def test_build_mask_video(self):
        timeline = _build_timeline()
        analysis = _build_analysis()
        cfg = config({"blur": {"enabled": True}})
        state = blur_decisions.load(timeline)

        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "mask.mkv")
            result = mask_builder.build(timeline, analysis, state, cfg, out_path)
            if result is None:
                self.skipTest("PIL / PyAV が使えないためマスクを作れません")
            self.assertTrue(os.path.isfile(result))
            # ほぼ真っ黒の絵が続くため、20 秒でも十分小さく収まる (§3.5 案 C)
            self.assertLess(os.path.getsize(result), 5 * 1024 * 1024)

    # ぼかす対象が 1 つも無ければマスクを作らないこと (= フィルタを足さない / R9)
    def test_no_target_no_mask(self):
        timeline = _build_timeline()
        analysis = _build_analysis()
        cfg = config({"blur": {"enabled": True}})
        state = blur_decisions.load(timeline)
        state["default_policy"] = "manual_only"

        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "mask.mkv")
            self.assertIsNone(
                mask_builder.build(timeline, analysis, state, cfg, out_path))
            self.assertFalse(os.path.exists(out_path))

    # 書き出しの入口 (prepare) を、解析済み・明示指定なし・blur_others の状態で通す (§9-1)。
    # 指定画面では主役以外が「ぼかす」と表示されるのに、明示指定が無いだけで
    # マスクを作らず素で出力していた不具合の再発防止。
    def _prepare_after_analysis(self, settings):
        from src.blur import store
        from src.pipeline.pipeline_context import PipelineContext

        timeline = _build_timeline()
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "sample.blur.json")
            project = os.path.join(tmp, "sample.timeline.json")
            store.save(cache, _build_analysis())
            commands.CommandStack().push(timeline, commands.SetBlurAnalysis(
                cache, "fp", project_path=project,
                default_policy=config(settings)["default_policy"]))
            self.assertEqual(blur_decisions.load(timeline)["identities"], {})

            context = PipelineContext(input_path=__file__, settings=settings, working_dir=tmp)
            context.project_path = project
            result = mask_builder.prepare(timeline, context)
            exists = bool(result) and os.path.isfile(result)
            failed = getattr(context, "blur_mask_failed", False)
            context.cleanup()
            return result, exists, failed

    def test_prepare_blurs_others_without_explicit_decisions(self):
        if not (mask_builder._PIL_AVAILABLE and mask_builder._PYAV_AVAILABLE):
            self.skipTest("PIL / PyAV が使えないためマスクを作れません")
        result, exists, failed = self._prepare_after_analysis(
            {"blur": {"enabled": True, "default_policy": "blur_others"}})
        self.assertIsNotNone(result, "明示指定が無いだけでマスクを作っていません")
        self.assertTrue(exists)
        self.assertFalse(failed)

    # manual_only で明示指定が無ければ、マスクを作らず確認も出さないこと
    def test_prepare_manual_only_without_explicit_decisions(self):
        result, _exists, failed = self._prepare_after_analysis(
            {"blur": {"enabled": True, "default_policy": "manual_only"}})
        self.assertIsNone(result)
        self.assertFalse(failed)

    # 縦動画のキャンバスでもマスクの大きさが縦になること (§2.5)
    def test_vertical_canvas_mask_size(self):
        timeline = _build_timeline()
        timeline.width, timeline.height = 1080, 1920
        analysis = _build_analysis()
        cfg = config({"blur": {"enabled": True}})
        state = blur_decisions.load(timeline)

        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "mask.mkv")
            result = mask_builder.build(timeline, analysis, state, cfg, out_path)
            if result is None:
                self.skipTest("PIL / PyAV が使えないためマスクを作れません")
            try:
                import av
            except ImportError:
                self.skipTest("PyAV が無いため寸法を確認できません")
            with av.open(result) as container:
                stream = container.streams.video[0]
                # mask_scale=0.25 → 1080x1920 の 1/4 (偶数へそろえる)
                self.assertEqual(stream.codec_context.width, 270)
                self.assertEqual(stream.codec_context.height, 480)

    # マスクの白い所が「ぼかすと決まった対象の、その時刻の位置」に一致すること
    # (Phase 3 の完了条件「位置が合っている」の自動確認 / §5.4)
    def test_mask_marks_only_target_times(self):
        timeline = _build_timeline()
        analysis = _build_analysis()
        cfg = config({"blur": {"enabled": True}})
        state = blur_decisions.load(timeline)

        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "mask.mkv")
            result = mask_builder.build(timeline, analysis, state, cfg, out_path)
            if result is None:
                self.skipTest("PIL / PyAV が使えないためマスクを作れません")
            try:
                import av
                import numpy as np
            except ImportError:
                self.skipTest("PyAV が無いため中身を確認できません")

            # 対象 (p2) は素材の 2.0〜6.0 秒 = Timeline でも 2.0〜6.0 秒 (c1 は 0 起点)
            frames = {}
            with av.open(result) as container:
                stream = container.streams.video[0]
                for frame in container.decode(stream):
                    sec = float(frame.pts * stream.time_base)
                    if abs(sec - 4.0) < 0.01 or abs(sec - 15.0) < 0.01:
                        frames[round(sec)] = np.asarray(
                            frame.to_ndarray(format="gray"), dtype=np.uint8)

            self.assertIn(4, frames, "対象が映っている時刻のフレームが読めません")
            self.assertGreater(int(frames[4].max()), 200, "ぼかす対象が白く塗られていません")
            if 15 in frames:
                # 対象が映っていない時刻は真っ黒 = ぼかさない
                self.assertEqual(int(frames[15].max()), 0)

            # 白い所は素材の右側 (x=900〜1100) にある。キャンバス 1920 の中央より右。
            # 平らな面のため argmax は左端を指す。重心で位置を見る。
            column_sums = frames[4].sum(axis=0, dtype=np.float64)
            columns = np.arange(frames[4].shape[1], dtype=np.float64)
            centroid = float((columns * column_sums).sum() / column_sums.sum())
            self.assertGreater(centroid / frames[4].shape[1], 0.5,
                               "ぼかしの位置が左右にずれています")
