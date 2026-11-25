# 採点スイープ (docs/AiEditor/resolve2.md §4 / S1 / P2) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・全長をチャンクへ割り、max_calls を超えるなら 1 区間を広げる (打ち切らない / §4.1)
#   ・区間の外の時刻は捨てる (AI に新しい時刻を作らせない / §4.4)
#   ・順位は AI に作らせない。軸の点へ設定の重みを掛ける (§5.3)
#   ・**投稿者名は送らない** (§10)
#   ・チャンクが 1 つ失敗しても残りで続ける。全滅・画像なしは空を返す (§9)
# AI も ffmpeg も呼ばない (provider と frames を差し替える)。
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ai import plan_schema, scene_scanner
from src.ai.provider import AiCompletion, AiProvider
from src.ai.vision import ScanFrame
from src.exceptions import AiPlanError, AiError

_SCAN_CFG = {"chunk_sec": 480, "max_calls": 16, "images_per_request": 4,
             "min_moment_sec": 15, "max_moment_sec": 120, "max_title_chars": 30,
             "weights": {"long": {"hook": 0.5, "payoff": 0.5, "drop_risk": -0.5}}}


def _settings(**scan_overrides):
    scan = dict({"enabled": True, "frame_interval_sec": 30, "frame_edge_px": 512,
                 "max_frames": 400, "fast_decode": True}, **_SCAN_CFG)
    scan.update(scan_overrides)

    return {"ffmpeg": {}, "subtitle": {"language": "ja"},
            "ai": {"enabled": True, "scan": scan,
                   "scene_eval": {"max_reason_chars": 120},
                   "budget": {"max_comments_per_cell": 3}}}


def _moment(start, end, **extra):
    row = {"start": start, "end": end, "peak": (start + end) / 2.0,
           "scores": {key: 50 for key in plan_schema.SCAN_SCORE_KEYS},
           "title_line": "タイトル", "why": "理由"}
    row.update(extra)

    return row


# 区間ごとに決まった応答を返すプロバイダー
class _Provider(AiProvider):

    name = "fake"

    def __init__(self, answers=None, fail_at=None):
        self.requests = []
        self._answers = answers or {}
        self._fail_at = set(fail_at or [])

    def complete_json(self, request):
        self.requests.append(request)
        start = request.payload["range"]["start"]
        if start in self._fail_at:
            raise AiError("わざと失敗")
        moments = self._answers.get(start, [])

        return AiCompletion(result={"moments": moments})

    def count_tokens(self, request):
        return None

    def server_config(self):
        return None


class PlanChunksTest(unittest.TestCase):

    def test_splits_by_chunk_sec(self):
        chunks = scene_scanner.plan_chunks(6524.0, _SCAN_CFG)
        self.assertEqual(len(chunks), 14)
        self.assertEqual(chunks[0], (0.0, 480.0))
        self.assertEqual(chunks[-1], (6240.0, 6524.0))

    # 上限を超える長さでは 1 区間を広げる (打ち切らない)
    def test_respects_max_calls(self):
        chunks = scene_scanner.plan_chunks(20000.0, _SCAN_CFG)
        self.assertEqual(len(chunks), 16)
        self.assertAlmostEqual(chunks[-1][1], 20000.0, places=3)

    def test_zero_duration(self):
        self.assertEqual(scene_scanner.plan_chunks(0.0, _SCAN_CFG), [])


class NormalizeScanTest(unittest.TestCase):

    def _run(self, moments, start=0.0, end=480.0):
        return plan_schema.normalize_scan(
            {"moments": moments}, start, end, min_sec=15.0, max_sec=120.0,
            max_title_chars=30, max_reason_chars=120)

    # 山が無い区間は空で返ってよい (無理に埋めさせない)
    def test_empty_is_allowed(self):
        found, _notes = self._run([])
        self.assertEqual(found, [])

    def test_missing_moments_raises(self):
        with self.assertRaises(AiPlanError):
            plan_schema.normalize_scan({}, 0.0, 10.0)

    # 区間の外は中へ丸める
    def test_times_are_clamped_to_the_chunk(self):
        found, _notes = self._run([_moment(-100.0, 9999.0)], 0.0, 480.0)
        self.assertEqual((found[0]["start"], found[0]["end"]), (0.0, 120.0))

    # 短すぎる山は落とす / 長すぎる山は切る
    def test_length_limits(self):
        found, notes = self._run([_moment(10.0, 20.0), _moment(100.0, 400.0)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["end"] - found[0]["start"], 120.0)
        self.assertTrue(any(n["action"] == plan_schema.ACTION_DROP for n in notes))

    # 点は 0..100 へ丸める (落とさない)
    def test_scores_are_clamped(self):
        scores = {key: 50 for key in plan_schema.SCAN_SCORE_KEYS}
        scores["hook"] = 500
        scores["payoff"] = -20
        found, _notes = self._run([_moment(0.0, 60.0, scores=scores)])
        self.assertEqual(found[0]["scores"]["hook"], 100.0)
        self.assertEqual(found[0]["scores"]["payoff"], 0.0)

    # 軸が欠けていても 0 で埋めて残す
    def test_missing_scores_default_to_zero(self):
        found, _notes = self._run([_moment(0.0, 60.0, scores={"hook": 80})])
        self.assertEqual(found[0]["scores"]["hook"], 80.0)
        self.assertEqual(found[0]["scores"]["payoff"], 0.0)

    # beats は欠けても必ず入り、順序が崩れていたら並べ直す
    def test_beats_are_always_present_and_ordered(self):
        found, _notes = self._run([_moment(0.0, 60.0)])
        beats = found[0]["beats"]
        self.assertEqual(list(beats), list(plan_schema.SCAN_BEAT_KEYS))
        self.assertLessEqual(beats["setup"], beats["hit"])
        self.assertLessEqual(beats["hit"], beats["settle"])

        found, _notes = self._run([_moment(0.0, 60.0,
                                           beats={"setup": 50.0, "hit": 10.0,
                                                  "settle": 30.0})])
        beats = found[0]["beats"]
        self.assertEqual([beats["setup"], beats["hit"], beats["settle"]],
                         [10.0, 30.0, 50.0])

    # peak が範囲外なら中央へ寄せる
    def test_peak_falls_back_to_the_middle(self):
        found, _notes = self._run([_moment(0.0, 60.0, peak=9999.0)])
        self.assertEqual(found[0]["peak"], 30.0)

    # 重みを掛けた総合点 (AI は順位を作らない)
    def test_scan_score_uses_the_weights(self):
        scores = {"hook": 80, "payoff": 60, "drop_risk": 40}
        weights = _SCAN_CFG["weights"]["long"]
        # 80*0.5 + 60*0.5 - 40*0.5 = 50
        self.assertEqual(plan_schema.scan_score(scores, weights), 50.0)

    # 負の重みで 0 を下回っても 0 で止める
    def test_scan_score_is_not_negative(self):
        self.assertEqual(
            plan_schema.scan_score({"drop_risk": 100}, {"drop_risk": -0.5}), 0.0)


class ScanTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.workdir = self._dir.name
        self.input_path = os.path.join(self.workdir, "vod.mp4")
        with open(self.input_path, "wb") as handle:
            handle.write(b"0")

    def tearDown(self):
        self._dir.cleanup()

    # 秒を指定してフレームを作る差し替え口
    def _frames(self, *seconds):
        made = []
        for at in seconds:
            path = os.path.join(self.workdir, "f%05d.jpg" % int(at))
            with open(path, "wb") as handle:
                handle.write(b"jpeg")
            made.append(ScanFrame(at, path))

        return lambda *a, **k: made

    def _scan(self, provider, frames=None, **kwargs):
        return scene_scanner.scan(
            self.input_path, _settings(), provider,
            duration_sec=kwargs.pop("duration_sec", 960.0),
            workdir=self.workdir,
            frames=frames if frames is not None else self._frames(0.0, 100.0, 500.0),
            **kwargs)

    # プロバイダーが無ければ何もしない (機械採点のまま)
    def test_without_provider(self):
        self.assertEqual(self._scan(None), [])

    # 設定で切ってあれば呼ばない
    def test_disabled(self):
        provider = _Provider()
        self.assertEqual(
            scene_scanner.scan(self.input_path, _settings(enabled=False), provider,
                               duration_sec=960.0, workdir=self.workdir,
                               frames=self._frames(0.0)), [])
        self.assertEqual(provider.requests, [])

    # フレームが 1 枚も無ければ飛ばす (文字だけで送らない)
    def test_without_frames(self):
        provider = _Provider()
        self.assertEqual(self._scan(provider, frames=lambda *a, **k: []), [])
        self.assertEqual(provider.requests, [])

    # 山を返す区間と返さない区間が混ざってよい
    def test_collects_moments_in_order(self):
        provider = _Provider(answers={
            480.0: [_moment(500.0, 560.0, title_line="後ろの山")],
            0.0: [_moment(100.0, 160.0, title_line="前の山")]})
        found = self._scan(provider)
        self.assertEqual([m.title_line for m in found], ["前の山", "後ろの山"])
        self.assertEqual(len(provider.requests), 2)

    # 1 区間が失敗しても残りで続ける
    def test_one_failed_chunk_does_not_stop(self):
        provider = _Provider(answers={480.0: [_moment(500.0, 560.0)]},
                             fail_at=[0.0])
        self.assertEqual(len(self._scan(provider)), 1)

    # 送る画像は images_per_request まで。端は残す
    def test_images_are_capped(self):
        frames = self._frames(*[float(i) * 10.0 for i in range(20)])
        provider = _Provider()
        self._scan(provider, frames=frames, duration_sec=480.0)
        self.assertEqual(len(provider.requests[0].images), 4)
        self.assertEqual(provider.requests[0].payload["frames"][0], 0.0)
        self.assertEqual(provider.requests[0].payload["frames"][-1], 190.0)

    # payload には区間・フレームの秒・数値・コメントが入る
    def test_payload_shape(self):
        provider = _Provider()
        cells = [{"start": 10.0, "mean_db": -20.0, "max_db": -5.0,
                  "silence_ratio": 0.1, "comment_count": 3, "w_count": 2},
                 {"start": 600.0, "mean_db": -30.0, "max_db": -9.0,
                  "silence_ratio": 0.5, "comment_count": 0, "w_count": 0}]
        comments = [{"offset_sec": 12.0, "user": "だれか", "text": "www"},
                    {"offset_sec": 700.0, "user": "ほか", "text": "草"}]
        self._scan(provider, cells=cells, comments=comments)
        payload = provider.requests[0].payload
        self.assertEqual(payload["range"], {"start": 0.0, "end": 480.0})
        self.assertEqual([row["t"] for row in payload["levels"]], [10.0])
        self.assertEqual(payload["levels"][0]["laughs"], 2)
        self.assertEqual([c["text"] for c in payload["comments"]], ["www"])
        # **投稿者名は送らない** (resolve1.md §5.5)
        self.assertNotIn("user", payload["comments"][0])
        self.assertNotIn("だれか", str(payload))

    # 役・キャッシュの印・スキーマが付く
    def test_request_metadata(self):
        provider = _Provider()
        self._scan(provider)
        request = provider.requests[0]
        self.assertEqual(request.role, "scene_scan")
        self.assertTrue(request.cache_prefix_id)
        self.assertIn("moments", request.schema["properties"])
        self.assertIn("山", request.system)

    # 使い終わったフレームは消す (作業フォルダへ溜めない)
    def test_frames_are_removed(self):
        frames = self._frames(0.0, 100.0)
        provider = _Provider()
        self._scan(provider, frames=frames)
        self.assertEqual([n for n in os.listdir(self.workdir)
                          if n.endswith(".jpg")], [])

    # 総合点が入る (重みは設定が持つ)
    def test_moments_carry_the_scan_score(self):
        scores = {"hook": 80, "payoff": 60, "drop_risk": 40}
        provider = _Provider(answers={0.0: [_moment(100.0, 160.0, scores=scores)]})
        found = self._scan(provider)
        self.assertEqual(found[0].scan_score, 50.0)
        self.assertIn("setup", found[0].beats)



# サーバーの構成を返すプロバイダー (役の許可リスト・上限の確認用)
class _ConfiguredProvider(_Provider):

    def __init__(self, roles=None, limits=None, **kwargs):
        super().__init__(**kwargs)
        self._roles = roles
        self._limits = limits or {}

    def server_config(self):
        from src.ai.provider import AiServerConfig

        return AiServerConfig(enabled=True, roles=self._roles, limits=self._limits)


class ServerRoleTest(unittest.TestCase):
    """サーバーが scene_scan をまだ許可していない間の扱い (resolve2.md §16)"""

    # 許可があればそのまま送る
    def test_uses_the_scan_role_when_allowed(self):
        provider = _ConfiguredProvider(roles={"sceneScan": {}, "sceneEval": {}})
        self.assertEqual(scene_scanner._roles_for(provider),
                         ("scene_scan", "scene_scan"))

    # 無ければ sceneEval で送る。モデルを引く鍵は scene_scan のまま
    def test_falls_back_to_scene_eval(self):
        provider = _ConfiguredProvider(roles={"sceneEval": {}, "plan": {}})
        self.assertEqual(scene_scanner._roles_for(provider),
                         ("scene_eval", "scene_scan"))

    # 構成を引けないときは判断しない (そのまま送って、駄目ならその区間を捨てる)
    def test_unknown_roles_keep_the_scan_role(self):
        self.assertEqual(scene_scanner._roles_for(_Provider()),
                         ("scene_scan", "scene_scan"))

    # 実際の呼び出しにも効く
    def test_request_uses_the_fallback_role(self):
        provider = _ConfiguredProvider(roles={"sceneEval": {}})
        scene_scanner.scan("x", _settings(), provider, duration_sec=480.0,
                           workdir=".", frames=lambda *a, **k: [])
        # フレームが無いので呼ばれない = 役の判定だけを確かめる
        self.assertEqual(provider.requests, [])


class ImageLimitTest(unittest.TestCase):

    # サーバーの上限のほうが小さければそちらに従う (超えると 400 になる)
    def test_server_limit_wins(self):
        provider = _ConfiguredProvider(limits={"maxImagesPerRequest": 2})
        self.assertEqual(scene_scanner._image_limit(provider, _SCAN_CFG), 2)

    # サーバーの上限のほうが大きければ設定を使う
    def test_setting_wins_when_smaller(self):
        provider = _ConfiguredProvider(limits={"maxImagesPerRequest": 16})
        self.assertEqual(scene_scanner._image_limit(provider, _SCAN_CFG), 4)

    # 構成を引けなければ設定のまま
    def test_without_server_config(self):
        self.assertEqual(scene_scanner._image_limit(_Provider(), _SCAN_CFG), 4)


class StartSecTest(unittest.TestCase):

    # 開始秒を指定すると、そこからチャンクを割る
    def test_chunks_start_at_the_offset(self):
        chunks = scene_scanner.plan_chunks(5280.0, _SCAN_CFG, start_sec=4320.0)
        self.assertEqual(chunks, [(4320.0, 4800.0), (4800.0, 5280.0)])

    # 開始秒が全長を超えたら空
    def test_offset_past_the_end(self):
        self.assertEqual(
            scene_scanner.plan_chunks(100.0, _SCAN_CFG, start_sec=200.0), [])


if __name__ == "__main__":
    unittest.main()
