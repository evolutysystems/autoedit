# docs/quality/resolve1.md の実装分の単体テスト
# 実行: python -m unittest discover -s tests
#
# 重点 (resolve1.md §6.0 の「合否が数値・テストで決まるもの」):
#   F11 : 最終出力に -b:a が付く / PCM には付かない
#   F4  : コメント未取得でも満点が 100 になる / 笑い語・エモートが点になる
#   F7  : 代表 dB が分位点で決まり、外れ値 1 件で崩壊しない
#   G1  : テロップ下限が改行下限と独立している
#   G2  : 間 (ポーズ) で切れる / 間が無ければ繋ぐ
#   G3  : 上限超過は文節境界まで戻して切れる
#   G5  : 実寸 (px) の物差しで「2 行で画面外に出ない」ことが保証される
#   B1  : 配信タイトル・ゲーム名が initial_prompt へ入る / hotwords が組める
#   B3  : 置換辞書が改行より前に効く
#   G4  : 結合で本文が連なり、分割で本文が分かれる
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import comment_source, scoring
from src.modules import ffmpeg_runner, subtitle_generator
from src.modules import volume_analyzer
from src.timeline import commands
from src.timeline.model import SubtitleClip, Timeline, Track, TRACK_SUBTITLE

# ASS の改行。ソースに直接書くと Python の文字名エスケープになるため定数で持つ
BR = chr(92) + "N"


# ------------------------------------------------------------------
# F11 最終出力の音声ビットレート
# ------------------------------------------------------------------
class AudioBitrateTest(unittest.TestCase):

    # 設定した値が -b:a として出る (従来は指定が無く aac 既定 ≒128kbps だった)
    def test_bitrate_is_passed(self):
        options = ffmpeg_runner.build_encode_options(
            {"audio_codec": "aac", "audio_bitrate": "256k"})
        self.assertEqual(options[options.index("-b:a") + 1], "256k")

    # 空文字なら従来どおり指定しない (切り戻し経路)
    def test_empty_bitrate_omits_option(self):
        options = ffmpeg_runner.build_encode_options(
            {"audio_codec": "aac", "audio_bitrate": ""})
        self.assertNotIn("-b:a", options)

    # 非圧縮の中間ファイルには付けない (無意味なうえ警告が出る)
    def test_pcm_intermediate_has_no_bitrate(self):
        options = ffmpeg_runner.build_intermediate_encode_options(
            {"audio_codec": "aac", "audio_bitrate": "256k"}, "pcm_s16le")
        self.assertEqual(options[options.index("-c:a") + 1], "pcm_s16le")
        self.assertNotIn("-b:a", options)

    # 中間が AAC のときは従来どおりビットレートが付く
    def test_aac_intermediate_keeps_bitrate(self):
        options = ffmpeg_runner.build_intermediate_encode_options(
            {"audio_codec": "aac", "audio_bitrate": "256k"}, "aac")
        self.assertEqual(options[options.index("-b:a") + 1], "256k")


# ------------------------------------------------------------------
# F4 採点の重み正規化と笑い語・エモート
# ------------------------------------------------------------------
def _cells(count, mean_db=-20.0, max_db=-5.0):
    return [{"start": i * 45.0, "end": (i + 1) * 45.0,
             "mean_db": mean_db, "max_db": max_db,
             "silence_ratio": 0.0, "w_count": 0, "comment_count": 0}
            for i in range(count)]


def _cfg(**overrides):
    cfg = {
        "weights": {"emotion": 0.55, "comment": 0.45},
        "emotion_points": {"loud": 10, "long_silence": -8},
        "comment": {"w_point_per_char": 1, "rate_spike_bonus": 10,
                    "laugh_word_point": 1, "emote_point": 1},
        "silence_ratio_threshold": 0.6,
        "comment_spike": False,
        "avg_comments_per_min": 0.0,
        "normalize_weights": True,
    }
    cfg.update(overrides)
    return cfg


class ScoreWeightTest(unittest.TestCase):

    # コメント未取得なら感情点だけで 0..100 へ正規化する (満点 55 問題の解消)
    def test_normalized_without_comments(self):
        cells = _cells(4, mean_db=-10.0)
        # 全セル同値 → 正規化音量は 0。大声加点で満点へ寄せる
        norm = [1.0] * 4
        window = scoring.score_window(cells, norm, (0, 4), -6.0, _cfg())
        self.assertEqual(window["emotion"], 100.0)
        self.assertEqual(window["total"], 100.0)

    # 従来挙動 (normalize_weights=false) では満点が 55 のまま
    def test_legacy_weights_cap_at_55(self):
        cells = _cells(4, mean_db=-10.0)
        norm = [1.0] * 4
        window = scoring.score_window(cells, norm, (0, 4), -6.0,
                                      _cfg(normalize_weights=False))
        self.assertEqual(window["total"], 55.0)

    # コメントを取得できている場合は従来の重みで合算する
    def test_with_comments_uses_both_weights(self):
        cells = _cells(4, mean_db=-10.0)
        cells[0]["w_count"] = 100
        norm = [1.0] * 4
        window = scoring.score_window(cells, norm, (0, 4), -6.0,
                                      _cfg(comment_spike=True))
        self.assertEqual(window["comment"], 100.0)
        self.assertEqual(window["total"], 100.0)

    # 笑い語・笑いエモートが点になる (F4)
    def test_laugh_words_and_emotes_score(self):
        cells = _cells(2, mean_db=-30.0)
        cells[0]["laugh_word_count"] = 3
        cells[0]["laugh_emote_count"] = 2
        norm = [0.0] * 2
        window = scoring.score_window(cells, norm, (0, 2), None,
                                      _cfg(comment_spike=True))
        self.assertEqual(window["comment"], 5.0)


class LaughCountTest(unittest.TestCase):

    # 「草」「笑」を数える。長い語を先に数えて二重計上しない
    def test_count_laugh_words(self):
        count = comment_source.count_laugh_words("草草くさ", ["草", "くさ"])
        self.assertEqual(count, 3)

    def test_count_laugh_words_empty(self):
        self.assertEqual(comment_source.count_laugh_words("", ["草"]), 0)
        self.assertEqual(comment_source.count_laugh_words("草", []), 0)

    # 笑いエモートは大小を無視して突き合わせる
    def test_count_laugh_emotes(self):
        count = comment_source.count_laugh_emotes(
            ["lul", "KEKW", "Kappa"], ["LUL", "KEKW"])
        self.assertEqual(count, 2)

    # セルへ集計され、未指定なら 0 のまま (従来と同一)
    def test_aggregate_into_cells(self):
        cells = _cells(2)
        comments = [{"offset_sec": 1.0, "user": "a", "text": "草ｗ",
                     "emotes": ["LUL"]}]
        comment_source.aggregate_into_cells(
            comments, cells, 45.0, laugh_words=["草"], laugh_emotes=["LUL"])
        self.assertEqual(cells[0]["laugh_word_count"], 1)
        self.assertEqual(cells[0]["laugh_emote_count"], 1)
        self.assertEqual(cells[0]["w_count"], 1)

    def test_aggregate_without_config_keeps_zero(self):
        cells = _cells(2)
        comments = [{"offset_sec": 1.0, "user": "a", "text": "草ｗ",
                     "emotes": ["LUL"]}]
        comment_source.aggregate_into_cells(comments, cells, 45.0)
        self.assertEqual(cells[0]["laugh_word_count"], 0)
        self.assertEqual(cells[0]["laugh_emote_count"], 0)


# ------------------------------------------------------------------
# F7 代表 dB を分位点で決める
# ------------------------------------------------------------------
class PercentileTest(unittest.TestCase):

    # 外れ値 1 件では動かない (従来の最小値採用では -41 まで落ちていた)
    def test_outlier_does_not_collapse(self):
        values = [-41.0, -24.0, -23.0, -22.0, -21.0, -20.0, -19.0, -18.0,
                  -17.0, -16.0]
        self.assertEqual(volume_analyzer._percentile(values, 10), -24.0)
        self.assertEqual(min(values), -41.0)

    def test_bounds(self):
        values = [-30.0, -20.0, -10.0]
        self.assertEqual(volume_analyzer._percentile(values, 0), -30.0)
        self.assertEqual(volume_analyzer._percentile(values, 100), -10.0)
        self.assertIsNone(volume_analyzer._percentile([], 10))


# ------------------------------------------------------------------
# G5 実寸 (px) の物差し
# ------------------------------------------------------------------
class TextMeasureTest(unittest.TestCase):

    # 文字数の物差しは従来と同じ判定
    def test_length_measure(self):
        measure = subtitle_generator.TextMeasure(20, 15)
        self.assertTrue(measure.fits("あ" * 20))
        self.assertFalse(measure.fits("あ" * 21))
        self.assertTrue(measure.reached_min("あ" * 15))
        self.assertFalse(measure.reached_min("あ" * 14))

    # 実寸の物差しは「幅」で判定する (1 文字 = 10px の作り物で検証)
    def test_width_measure(self):
        measure = subtitle_generator.TextMeasure(
            100, 50, lambda s: len(s) * 10.0, "width")
        self.assertTrue(measure.fits("あ" * 10))
        self.assertFalse(measure.fits("あ" * 11))

    # 1 本のテロップぶん = 1 行 × 最大行数
    def test_scaled(self):
        measure = subtitle_generator.TextMeasure(100, 50, None, "width")
        scaled = measure.scaled(2)
        self.assertEqual(scaled.limit, 200)
        self.assertEqual(scaled.mode, "width")

    # wrap_mode=length なら文字数の物差しが返る (従来と完全に同一)
    def test_build_measure_length(self):
        measure = subtitle_generator.build_text_measure(
            {"wrap_mode": "length", "min_line_length": 5, "max_line_length": 20}, 1920)
        self.assertEqual(measure.mode, "length")
        self.assertEqual(measure.limit, 20)

    # 出力幅が不明なら文字数へ落ちる (安全側)
    def test_build_measure_without_width(self):
        measure = subtitle_generator.build_text_measure(
            {"wrap_mode": "width", "font_family": "Yu Gothic UI"}, 0)
        self.assertEqual(measure.mode, "length")

    # フォント名が空なら文字数へ落ちる (読めないフォントと同じ扱い)
    def test_build_measure_without_font(self):
        measure = subtitle_generator.build_text_measure(
            {"wrap_mode": "width", "font_family": ""}, 1920)
        self.assertEqual(measure.mode, "length")


class WrapWithMeasureTest(unittest.TestCase):

    # 実寸の物差しで折り返すと、全行が上限幅に収まる
    # = 要望の条件「2 行で表示したときに画面外に出ない」が仕様として保証される
    def test_every_line_fits(self):
        measure = subtitle_generator.TextMeasure(
            100, 50, lambda s: len(s) * 10.0, "width")
        wrapped = subtitle_generator.wrap_by_length("あ" * 35, 0, measure=measure)
        for line in wrapped.split("\\N"):
            self.assertTrue(measure.fits(line), f"はみ出した行: {line}")

    # 幅が広いフォントほど 1 行が短くなる (文字数では表せない差)
    def test_wider_font_breaks_earlier(self):
        narrow = subtitle_generator.TextMeasure(100, 0, lambda s: len(s) * 5.0, "width")
        wide = subtitle_generator.TextMeasure(100, 0, lambda s: len(s) * 20.0, "width")
        narrow_lines = subtitle_generator.wrap_by_length(
            "あ" * 40, 0, measure=narrow).split("\\N")
        wide_lines = subtitle_generator.wrap_by_length(
            "あ" * 40, 0, measure=wide).split("\\N")
        self.assertLess(len(narrow_lines), len(wide_lines))

    # 手動改行 (\\N) は尊重する
    def test_manual_break_is_kept(self):
        measure = subtitle_generator.TextMeasure(
            1000, 0, lambda s: len(s) * 10.0, "width")
        wrapped = subtitle_generator.wrap_by_length("ああ\\Nいい", 0, measure=measure)
        self.assertEqual(wrapped, "ああ\\Nいい")


# ------------------------------------------------------------------
# G1 / G2 / G3 テロップの区切り
# ------------------------------------------------------------------
def _word(start, end, text):
    return {"start": start, "end": end, "text": text}


def _item(words):
    return {"start": words[0]["start"], "end": words[-1]["end"],
            "text": "".join(w["text"] for w in words), "words": list(words)}


# 素材 60 秒を 1 区間として扱う (カット境界の影響を受けない形で区切りだけを見る)
_KEEP = [(0.0, 60.0)]


class SubtitleMinCharsTest(unittest.TestCase):

    # 下限が大きいと、認識セグメントの境界では切れずに 1 本へ繋がる (G1)
    def test_high_min_chars_merges(self):
        first = _item([_word(0.0, 1.0, "あいうえお")])
        second = _item([_word(1.0, 2.0, "かきくけこ")])
        out, count = subtitle_generator.regroup_items_by_length(
            [first, second], _KEEP, min_chars=16, max_chars=40)
        self.assertEqual(count, 1)
        self.assertEqual(out[0]["text"], "あいうえおかきくけこ")

    # 下限が小さいと境界ごとに切れる (従来の min_line_length=5 相当の症状)
    def test_low_min_chars_splits(self):
        first = _item([_word(0.0, 1.0, "あいうえお")])
        second = _item([_word(1.0, 2.0, "かきくけこ")])
        out, count = subtitle_generator.regroup_items_by_length(
            [first, second], _KEEP, min_chars=5, max_chars=40)
        self.assertEqual(count, 2)


class PauseSplitTest(unittest.TestCase):

    # 下限達成後、間が開いたら切る (G2)
    def test_long_pause_splits(self):
        words = [_word(0.0, 1.0, "あいうえおかきくけこ"),
                 _word(2.0, 3.0, "さしすせそ")]      # 間 1.0 秒
        out, count = subtitle_generator.regroup_items_by_length(
            [_item(words)], _KEEP, min_chars=5, max_chars=40,
            split_min_pause_sec=0.35)
        self.assertEqual(count, 2)

    # 間が無ければ切らない (G2)。間を見ない従来は認識セグメント境界で切れていた。
    def test_short_pause_glues(self):
        first = _item([_word(0.0, 1.0, "あいうえおかきくけこ")])
        second = _item([_word(1.05, 2.0, "さしすせそ")])   # 間 0.05 秒
        out, count = subtitle_generator.regroup_items_by_length(
            [first, second], _KEEP, min_chars=5, max_chars=40,
            merge_max_pause_sec=0.20)
        self.assertEqual(count, 1)

    # 上限は間に関わらず絶対 (B1 の規約は変えない)
    def test_over_limit_splits_even_when_glued(self):
        words = [_word(0.0, 1.0, "あ" * 30), _word(1.01, 2.0, "い" * 30)]
        out, count = subtitle_generator.regroup_items_by_length(
            [_item(words)], _KEEP, min_chars=5, max_chars=40,
            merge_max_pause_sec=0.20)
        self.assertEqual(count, 2)


class MaxLinesTest(unittest.TestCase):
    """折り返した結果の行数が subtitle_max_lines を超えないこと (H1 / §12.4)

    従来は「1 本の幅 = 1 行 × 最大行数」で上限を見ていたため、BudouX の文節境界が
    早く来て 1 行が短くなると、幅には収まっても 3 行に折れていた
    (実測: 2 行指定で 42 文字 3 行のテロップ / docs/quality/kensyou)。
    """

    # 1 行 = 最大 6 文字相当の物差し。2 行指定で 3 行にならないこと
    def _measure(self, per_line=6):
        return subtitle_generator.TextMeasure(
            per_line * 10.0, 0, lambda s: len(s) * 10.0, "width")

    def test_line_count_limits_telop(self):
        # 1 行 6 文字・2 行まで → 1 本は 12 文字相当まで。
        # 「あ」を 1 文字ずつ 20 語に分けて積む
        words = [_word(i * 0.1, i * 0.1 + 0.05, "あ") for i in range(20)]
        measure = self._measure()

        def wrap(text):
            return subtitle_generator.wrap_by_length(text, 0, measure=measure)

        out, _count = subtitle_generator.regroup_items_by_length(
            [_item(words)], [(0.0, 60.0)], wrap=wrap,
            min_chars=1, max_chars=100, measure=measure.scaled(2), max_lines=2)
        for entry in out:
            lines = entry["text"].split(BR)
            self.assertLessEqual(len(lines), 2,
                                 f"{len(lines)} 行になっています: {entry['text']}")

    # max_lines=0 (未指定) なら従来どおり幅だけで判断する (切り戻し経路)
    def test_zero_max_lines_keeps_legacy(self):
        words = [_word(i * 0.1, i * 0.1 + 0.05, "あ") for i in range(20)]
        measure = self._measure()

        def wrap(text):
            return subtitle_generator.wrap_by_length(text, 0, measure=measure)

        out, count = subtitle_generator.regroup_items_by_length(
            [_item(words)], [(0.0, 60.0)], wrap=wrap,
            min_chars=1, max_chars=100, measure=measure.scaled(2), max_lines=0)
        # 幅 120px ぶん (12 文字) で切れる = 2 本
        self.assertEqual(count, 2)

    # 文字も失わない
    def test_no_text_lost(self):
        words = [_word(i * 0.1, i * 0.1 + 0.05, "あ") for i in range(20)]
        measure = self._measure()

        def wrap(text):
            return subtitle_generator.wrap_by_length(text, 0, measure=measure)

        out, _count = subtitle_generator.regroup_items_by_length(
            [_item(words)], [(0.0, 60.0)], wrap=wrap,
            min_chars=1, max_chars=100, measure=measure.scaled(2), max_lines=2)
        joined = "".join(e["text"].replace(BR, "") for e in out)
        self.assertEqual(joined, "あ" * 20)


class ShortLineTest(unittest.TestCase):
    """1〜2 文字だけの行が出ないこと (H1 / §12.4)

    実測で「イーン隊早く帰ったらどうぞ1478兵隊集積所へ\\Nの\\N攻撃を完了早」のように
    「の」だけの行が出ていた。
    """

    # 短い末尾行は前の行へ寄せる
    def test_short_tail_merges(self):
        measure = subtitle_generator.TextMeasure(
            100.0, 0, lambda s: len(s) * 10.0, "width")
        merged = subtitle_generator._merge_short_lines(
            ["あいうえおか", "の", "きくけこ"], measure)
        self.assertEqual(merged, ["あいうえおかの", "きくけこ"])

    # 寄せると上限を超える場合はそのまま残す (画面外へ出すより短い行の方が害が小さい)
    def test_does_not_overflow(self):
        measure = subtitle_generator.TextMeasure(
            100.0, 0, lambda s: len(s) * 10.0, "width")
        lines = ["あいうえおかきくけこ", "の"]        # 10 文字 = 上限ちょうど
        self.assertEqual(subtitle_generator._merge_short_lines(lines, measure), lines)

    # 1 行だけなら何もしない
    def test_single_line(self):
        measure = subtitle_generator.TextMeasure(100.0, 0, None, "width")
        self.assertEqual(subtitle_generator._merge_short_lines(["あ"], measure), ["あ"])


class PauseCeilingTest(unittest.TestCase):
    """下限文字数に達していなくても、間が大きく空いたら切れること (回帰テスト)

    docs/quality/kensyou の実測で、17 文字のテロップが 76.8 秒に伸びていた。
    区切り判定が `enough and soft` の形だったため、下限文字数に達するまで
    間・文末・カット境界のどれも効かず、疎な発話が何十秒でも 1 本へ繋がっていた。
    """

    # 下限未達 + 大きな間 → 切れる (以前は繋がっていた)
    def test_long_pause_splits_below_min_chars(self):
        words = [_word(0.0, 1.0, "あぶねー"),        # 4 文字 (下限 16 未満)
                 _word(40.0, 41.0, "撃沈")]          # 間 39 秒
        out, count = subtitle_generator.regroup_items_by_length(
            [_item(words)], [(0.0, 60.0)], min_chars=16, max_chars=40,
            split_max_pause_sec=1.0)
        self.assertEqual(count, 2)
        self.assertLess(out[0]["end"] - out[0]["start"], 2.0)

    # 上限が 0 なら従来どおり (切り戻し経路)
    def test_zero_keeps_legacy_behaviour(self):
        words = [_word(0.0, 1.0, "あぶねー"), _word(40.0, 41.0, "撃沈")]
        out, count = subtitle_generator.regroup_items_by_length(
            [_item(words)], [(0.0, 60.0)], min_chars=16, max_chars=40,
            split_max_pause_sec=0.0)
        self.assertEqual(count, 1)

    # 上限未満の間では下限が優先される (G1 の意図は保つ)
    def test_short_pause_still_merges(self):
        words = [_word(0.0, 1.0, "あいうえ"), _word(1.3, 2.0, "おかきく")]
        out, count = subtitle_generator.regroup_items_by_length(
            [_item(words)], [(0.0, 60.0)], min_chars=16, max_chars=40,
            split_min_pause_sec=0.35, split_max_pause_sec=1.0)
        self.assertEqual(count, 1)

    # 実測の症状そのまま: 疎な発話が 1 本に繋がらないこと
    def test_sparse_speech_does_not_glue(self):
        words = [_word(0.0, 1.0, "22発"), _word(20.0, 21.0, "あぶねーって"),
                 _word(60.0, 61.0, "パルコネルを"), _word(76.0, 77.0, "撃沈")]
        out, _count = subtitle_generator.regroup_items_by_length(
            [_item(words)], [(0.0, 90.0)], min_chars=16, max_chars=40,
            split_max_pause_sec=1.0)
        longest = max(e["end"] - e["start"] for e in out)
        self.assertLess(longest, 5.0, f"1 本が {longest:.1f} 秒に伸びています")


class PhraseBoundaryTest(unittest.TestCase):

    # 上限超過で切るとき、文節境界まで戻す (G3)
    # 戻した語は次のテロップの先頭へ繰り越され、文字は失われない。
    def test_carry_keeps_all_text(self):
        words = [_word(0.0, 0.5, "今日は"), _word(0.5, 1.0, "とても"),
                 _word(1.0, 1.5, "良い"), _word(1.5, 2.0, "天気ですね")]
        out, _count = subtitle_generator.regroup_items_by_length(
            [_item(words)], _KEEP, min_chars=4, max_chars=8,
            split_on_phrase_boundary=True)
        joined = "".join(e["text"].replace("\\N", "") for e in out)
        self.assertEqual(joined, "今日はとても良い天気ですね")

    # 文節境界が無い (1 語) ときは従来どおり単語境界で切る
    def test_single_word_falls_back(self):
        words = [_word(0.0, 1.0, "あ" * 50)]
        out, count = subtitle_generator.regroup_items_by_length(
            [_item(words)], _KEEP, min_chars=4, max_chars=8,
            split_on_phrase_boundary=True)
        self.assertEqual(count, 1)


# ------------------------------------------------------------------
# B1 / B3 認識へ渡すドメイン知識と置換辞書
# ------------------------------------------------------------------
class PromptTest(unittest.TestCase):

    # 配信タイトルとゲーム名が前提テキストへ入る (Q1)
    def test_prompt_from_vod(self):
        prompt = subtitle_generator.build_initial_prompt(
            {"initial_prompt_from_vod": True}, "深夜の雑談", "Minecraft")
        self.assertEqual(prompt, "深夜の雑談。ゲーム: Minecraft。")

    # 手入力の前提テキストは VOD の情報の後ろへ連なる (両方効く)
    def test_prompt_appends_manual(self):
        prompt = subtitle_generator.build_initial_prompt(
            {"initial_prompt_from_vod": True, "initial_prompt": "固有名詞: ほげ"},
            "配信", "")
        self.assertEqual(prompt, "配信。固有名詞: ほげ")

    # 無効にすると手入力だけになる
    def test_prompt_disabled(self):
        prompt = subtitle_generator.build_initial_prompt(
            {"initial_prompt_from_vod": False, "initial_prompt": "手入力"},
            "配信", "ゲーム")
        self.assertEqual(prompt, "手入力")

    # 何も無ければ空 = 従来と同一の認識 (引数を渡さない)
    def test_prompt_empty(self):
        self.assertEqual(subtitle_generator.build_initial_prompt({}), "")

    # 用語リストと hotwords が 1 文字列へまとまる
    def test_hotwords(self):
        words = subtitle_generator.build_hotwords(
            {"hotwords": "ほげ", "vocabulary": ["ふが", "ぴよ"]})
        self.assertEqual(words, "ほげ ふが ぴよ")

    def test_hotwords_empty(self):
        self.assertEqual(subtitle_generator.build_hotwords({}), "")


class ReplacementTest(unittest.TestCase):

    def test_plain_replacement(self):
        rules = [{"from": "ほげ", "to": "ホゲ"}]
        self.assertEqual(
            subtitle_generator.apply_replacements("ほげとほげ", rules), "ホゲとホゲ")

    def test_regex_replacement(self):
        rules = [{"from": r"\d+", "to": "N", "regex": True}]
        self.assertEqual(
            subtitle_generator.apply_replacements("12と345", rules), "NとN")

    # 壊れた正規表現 1 件でテロップ全損にしない (握り潰して次へ)
    def test_broken_regex_is_skipped(self):
        rules = [{"from": "(", "to": "x", "regex": True},
                 {"from": "あ", "to": "い"}]
        self.assertEqual(subtitle_generator.apply_replacements("あ", rules), "い")

    def test_no_rules_returns_text(self):
        self.assertEqual(subtitle_generator.apply_replacements("そのまま"), "そのまま")


# ------------------------------------------------------------------
# G4 テロップの結合・分割
# ------------------------------------------------------------------
def _subtitle_timeline():
    timeline = Timeline()
    track = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    timeline.tracks = [track]
    return timeline, track


class MergeSubtitlesTest(unittest.TestCase):

    def setUp(self):
        self.timeline, self.track = _subtitle_timeline()
        self.track.clips.append(SubtitleClip("s1", 0.0, 2.0, text="あいう"))
        self.track.clips.append(SubtitleClip("s2", 2.0, 2.0, text="えおか"))

    # 2 本が 1 本になり、本文が時間順に連なる
    def test_merge_joins_text_and_span(self):
        command = commands.MergeSubtitles(["s1", "s2"])
        self.assertTrue(command.apply(self.timeline))
        self.assertEqual(len(self.track.clips), 1)
        merged = self.track.clips[0]
        self.assertEqual(merged.text, "あいうえおか")
        self.assertEqual(merged.timeline_start, 0.0)
        self.assertEqual(merged.timeline_end, 4.0)
        self.assertEqual(merged.origin["merged_from"], ["s1", "s2"])

    # 整形関数を渡すと本文を引き直す (改行のやり直し)
    def test_merge_applies_wrap(self):
        command = commands.MergeSubtitles(["s1", "s2"], wrap=lambda s: s + "!")
        command.apply(self.timeline)
        self.assertEqual(self.track.clips[0].text, "あいうえおか!")

    # 1 本では結合しない
    def test_merge_needs_two(self):
        command = commands.MergeSubtitles(["s1"])
        self.assertFalse(command.apply(self.timeline))

    # 時間順が逆でも結果は同じ (並べ直す)
    def test_merge_sorts_by_time(self):
        command = commands.MergeSubtitles(["s2", "s1"])
        command.apply(self.timeline)
        self.assertEqual(self.track.clips[0].text, "あいうえおか")


class SplitSubtitleTest(unittest.TestCase):

    # 単語の時刻があれば語の境界で本文が分かれる
    def test_split_uses_words(self):
        timeline, track = _subtitle_timeline()
        origin = {"type": "asr", "words": [
            {"start": 10.0, "end": 10.5, "text": "あい"},
            {"start": 11.0, "end": 11.5, "text": "うえお"},
        ]}
        track.clips.append(SubtitleClip("s1", 0.0, 2.0, text="あいうえお",
                                        origin=origin))
        command = commands.SplitSubtitle("s1", 1.0)
        self.assertTrue(command.apply(timeline))
        clips = sorted(track.clips, key=lambda c: c.timeline_start)
        self.assertEqual([c.text for c in clips], ["あい", "うえお"])

    # 単語の時刻が無ければ尺の比率で按分する (手入力テロップ)
    def test_split_without_words_uses_ratio(self):
        timeline, track = _subtitle_timeline()
        track.clips.append(SubtitleClip("s1", 0.0, 4.0, text="あいうえおかきく"))
        command = commands.SplitSubtitle("s1", 2.0)
        self.assertTrue(command.apply(timeline))
        clips = sorted(track.clips, key=lambda c: c.timeline_start)
        self.assertEqual([c.text for c in clips], ["あいうえ", "おかきく"])

    # 分割しても文字は失われない
    def test_split_keeps_all_text(self):
        timeline, track = _subtitle_timeline()
        track.clips.append(SubtitleClip("s1", 0.0, 4.0, text="あいうえおかきく"))
        commands.SplitSubtitle("s1", 2.0).apply(timeline)
        joined = "".join(c.text for c in
                         sorted(track.clips, key=lambda c: c.timeline_start))
        self.assertEqual(joined, "あいうえおかきく")

    # 最小尺を満たさない位置では割れない (従来の規約どおり)
    def test_split_respects_min_duration(self):
        timeline, track = _subtitle_timeline()
        track.clips.append(SubtitleClip("s1", 0.0, 1.0, text="あいう"))
        command = commands.SplitSubtitle("s1", 0.01, min_clip_sec=0.05)
        self.assertFalse(command.apply(timeline))
        self.assertEqual(len(track.clips), 1)


if __name__ == "__main__":
    unittest.main()
