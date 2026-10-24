# 話者照合によるテロップの絞り込み (src/modules/speaker_filter.py) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 重点 (docs/quality/resolve1.md §12.5 / F15):
#   ・既定 (off) では何もしないこと = 従来と完全に同一
#   ・代表の声 (多数派) が刈り込み平均で求まること
#   ・"flag" は落とさず印を付けるだけ / "drop" は落とすこと
#   ・モデルが無い・音声が取れない・短すぎる発話では素通りすること
#     (どこで失敗しても字幕を失わないのが F15 の前提)
import math
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import speaker_filter as sf


def _unit(vector):
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


class ConfigTest(unittest.TestCase):

    # 既定は何もしない (off)
    def test_defaults(self):
        cfg = sf.speaker_config({})
        self.assertEqual(cfg["mode"], "off")
        self.assertFalse(sf.enabled(cfg))

    def test_enabled_modes(self):
        self.assertTrue(sf.enabled(sf.speaker_config({"speaker_filter": "flag"})))
        self.assertTrue(sf.enabled(sf.speaker_config({"speaker_filter": "drop"})))
        self.assertFalse(sf.enabled(sf.speaker_config({"speaker_filter": "off"})))

    # 大文字・前後の空白を吸収する
    def test_normalizes_values(self):
        cfg = sf.speaker_config({"speaker_filter": " FLAG "})
        self.assertEqual(cfg["mode"], "flag")


class SimilarityTest(unittest.TestCase):

    def test_identical(self):
        a = _unit([1.0, 0.0, 0.0])
        self.assertAlmostEqual(sf.similarity(a, a), 1.0)

    def test_orthogonal(self):
        a = _unit([1.0, 0.0])
        b = _unit([0.0, 1.0])
        self.assertAlmostEqual(sf.similarity(a, b), 0.0)


class CenterTest(unittest.TestCase):

    # 全体平均を引いてから正規化する (背景音の共通成分を除く / §12.6)
    def test_center_removes_common_part(self):
        # 共通成分 (10, 10) に ±1 の違いが乗っている
        vectors = [[11.0, 10.0], [9.0, 10.0]]
        centered = sf._center(vectors)
        # 共通成分が消え、互いに反対を向く
        self.assertAlmostEqual(sf.similarity(centered[0], centered[1]), -1.0, places=5)

    # 取れなかったものは None のまま位置を保つ
    def test_center_keeps_none(self):
        centered = sf._center([[1.0, 2.0], None, [3.0, 4.0]])
        self.assertEqual(len(centered), 3)
        self.assertIsNone(centered[1])

    def test_center_all_none(self):
        self.assertEqual(sf._center([None, None]), [None, None])


class DominantCenterTest(unittest.TestCase):

    # 多数派 (= 最も長く喋る声) の方向へ収束すること
    def test_finds_majority(self):
        majority = _unit([1.0, 0.0])
        outlier = _unit([0.0, 1.0])
        vectors = [majority] * 8 + [outlier] * 2
        center = sf.dominant_center(vectors, core_ratio=0.5)
        self.assertGreater(sf.similarity(center, majority), 0.99)
        self.assertLess(sf.similarity(center, outlier), 0.2)

    # 発話長の重みが効くこと (件数は少なくても長く喋っている声が代表になる)
    def test_weights_by_duration(self):
        long_voice = _unit([1.0, 0.0])
        short_voice = _unit([0.0, 1.0])
        vectors = [long_voice] * 2 + [short_voice] * 5
        weights = [30.0, 30.0] + [0.5] * 5
        center = sf.dominant_center(vectors, core_ratio=0.3, weights=weights)
        self.assertGreater(sf.similarity(center, long_voice),
                           sf.similarity(center, short_voice))

    def test_empty(self):
        self.assertIsNone(sf.dominant_center([]))
        self.assertIsNone(sf.dominant_center([None, None]))


class SpeechSpanTest(unittest.TestCase):

    # 単語の時刻があれば発話の実時間を使う (表示区間の無音を含めない)
    def test_uses_words(self):
        item = {"start": 0.0, "end": 10.0,
                "words": [{"start": 2.0, "end": 2.5, "text": "あ"},
                          {"start": 3.0, "end": 3.5, "text": "い"}]}
        self.assertEqual(sf._speech_span(item), (2.0, 3.5))

    # 単語が無ければ表示区間をそのまま使う
    def test_falls_back_to_span(self):
        self.assertEqual(sf._speech_span({"start": 1.0, "end": 4.0}), (1.0, 4.0))


class ApplyTest(unittest.TestCase):

    def setUp(self):
        self.items = [
            {"start": 0.0, "end": 2.0, "text": "配信者A"},
            {"start": 3.0, "end": 5.0, "text": "配信者B"},
            {"start": 6.0, "end": 8.0, "text": "ゲーム"},
        ]

    # off では FFmpeg も呼ばず items をそのまま返す (従来と完全に同一)
    def test_off_does_nothing(self):
        with mock.patch.object(sf, "build_embedder") as builder:
            out, stats = sf.apply(self.items, "a.mp4", {"speaker_filter": "off"}, {})
        self.assertIs(out, self.items)
        self.assertEqual(stats["scored"], 0)
        builder.assert_not_called()

    # 声紋を取れない (モデル無し等) なら items をそのまま返す
    def test_no_embedder_passes_through(self):
        with mock.patch.object(sf, "build_embedder", return_value=None):
            out, stats = sf.apply(self.items, "a.mp4", {"speaker_filter": "flag"}, {})
        self.assertIs(out, self.items)
        self.assertEqual(stats["scored"], 0)

    # 音声が取り出せないなら items をそのまま返す
    def test_no_audio_passes_through(self):
        with mock.patch.object(sf, "build_embedder", return_value=lambda s: [1.0, 0.0]), \
                mock.patch.object(sf, "extract_wave", return_value=None):
            out, stats = sf.apply(self.items, "a.mp4", {"speaker_filter": "flag"}, {})
        self.assertIs(out, self.items)
        self.assertEqual(stats["scored"], 0)

    # flag: 落とさず印を付けるだけ
    def test_flag_marks_without_dropping(self):
        out, stats = self._run("flag")
        self.assertEqual(len(out), 3)
        self.assertEqual(stats["other"], 1)
        self.assertEqual(stats["dropped"], 0)
        self.assertTrue(out[2].get("speaker_other"))
        self.assertFalse(out[0].get("speaker_other", False))
        # 類似度は全件に入る (編集画面の色分け・計測に使う)
        for entry in out:
            self.assertIn("speaker_score", entry)

    # drop: 別人とみなしたものを落とす
    def test_drop_removes(self):
        out, stats = self._run("drop")
        self.assertEqual(len(out), 2)
        self.assertEqual(stats["dropped"], 1)
        self.assertEqual([e["text"] for e in out], ["配信者A", "配信者B"])

    # 短すぎる発話は判定せず素通りさせる (声紋が安定しないため / 安全側)
    def test_short_items_pass_through(self):
        items = [{"start": 0.0, "end": 0.2, "text": "短い"}] + self.items
        out, stats = self._run("drop", items=items)
        self.assertIn("短い", [e["text"] for e in out])
        self.assertNotIn("speaker_score", out[0])

    # 2 人ぶんの声紋を作って apply を通す補助
    def _run(self, mode, items=None):
        items = items or self.items
        # 本文で声を決める作り物の声紋 (配信者 2 件 = 多数派 / ゲーム 1 件 = 外れ)
        table = {"配信者A": [1.0, 0.02], "配信者B": [1.0, -0.02],
                 "ゲーム": [-1.0, 0.0], "短い": [0.0, 1.0]}

        def embedder(samples):
            return table[samples]

        # _slice の代わりに本文を返し、どの item の声紋かを決める
        def fake_slice(raw, rate, item, min_sec):
            if item["end"] - item["start"] < max(min_sec, 0.0):
                return None
            return item["text"]

        cfg = {"speaker_filter": mode, "speaker_threshold": 0.0,
               "speaker_min_sec": 0.6}
        with mock.patch.object(sf, "build_embedder", return_value=embedder), \
                mock.patch.object(sf, "extract_wave", return_value="w.wav"), \
                mock.patch.object(sf, "load_wave", return_value=(b"\\0" * 32000, 16000)), \
                mock.patch.object(sf, "_slice", side_effect=fake_slice), \
                mock.patch.object(sf.os.path, "exists", return_value=False):
            return sf.apply(items, "a.mp4", cfg, {})


if __name__ == "__main__":
    unittest.main()
