# テロップアニメーション (docs/request/ver12/resolve2.md) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・単語の切り方の 3 段 (認識由来 → 句へ切って按分) が切り替わること (§2.1)
#   ・各アニメが期待する ASS タグを出すこと (\fad / \t / \move / \pos / \fs)
#   ・語数・イベント数の上限を超えたら行単位の動きへ落ちること (§10)
#   ・**アニメ指定の無いテロップの ASS が 1 文字も変わらないこと** (§0)
#   ・コメント役割はフェードのみになること (§12 ②)
#   ・個別のフォント・サイズ・色が単語ごとの行にも付き直すこと (§R3)
# FFmpeg は起動しない。ASS の文字列だけを検証する。
import copy
import io
import os
import tempfile
import unittest

from src.modules import subtitle_animation, subtitle_generator
from src.settings.settings_window import DEFAULT_SETTINGS

_CFG = copy.deepcopy(DEFAULT_SETTINGS["subtitle"])
_TEXT = "今日はとても良い天気ですね"


def _entry(**fields):
    base = {"start": 1.0, "end": 3.0, "text": _TEXT, "use": True,
            "role": "streamer", "font": "", "font_size": None}
    base.update(fields)
    return base


# 単語時刻 (素材時間 / origin["words"] と同じ形)
def _asr_words():
    return [
        {"start": 100.0, "end": 100.4, "text": "今日は"},
        {"start": 100.5, "end": 101.0, "text": "とても"},
        {"start": 101.1, "end": 101.4, "text": "良い"},
        {"start": 101.5, "end": 102.4, "text": "天気ですね"},
    ]


class SplitWordsTest(unittest.TestCase):

    # ① 認識由来の単語時刻をそのまま使う (本文が未編集のとき)
    def test_uses_asr_word_times(self):
        cfg = subtitle_animation.config(_CFG)
        words = subtitle_animation.split_words(
            _entry(words=_asr_words()), cfg)

        self.assertEqual([w["text"] for w in words],
                         ["今日は", "とても", "良い", "天気ですね"])
        # 先頭語からの相対秒になっている (素材時間がそのまま出ない)
        self.assertAlmostEqual(words[0]["offset"], 0.0, places=3)
        self.assertAlmostEqual(words[1]["offset"], 0.5, places=3)
        self.assertEqual(subtitle_animation.word_source(
            _entry(words=_asr_words())), "asr")

    # ② 本文を手直ししたら単語時刻は使えない (句へ切って按分する)
    def test_falls_back_to_phrases_when_text_was_edited(self):
        cfg = subtitle_animation.config(_CFG)
        entry = _entry(text="書き換えた本文です", words=_asr_words())
        words = subtitle_animation.split_words(entry, cfg)

        self.assertTrue(words)
        self.assertEqual("".join(w["text"] for w in words), "書き換えた本文です")
        self.assertEqual(subtitle_animation.word_source(entry), "phrase")
        # 尺は文字数で按分されるため、最後の語の始まりは表示時間の中に収まる
        self.assertLess(words[-1]["offset"], 2.0)

    # 単語時刻が無い (手入力・旧プロジェクト) ときも句へ切れる
    def test_splits_plain_text_without_word_times(self):
        cfg = subtitle_animation.config(_CFG)
        words = subtitle_animation.split_words(_entry(), cfg)

        self.assertTrue(words)
        self.assertEqual("".join(w["text"] for w in words), _TEXT)

    # 強調する語は設定の一覧とテロップ個別指定の合算で決まる (§2.2)
    def test_emphasis_comes_from_settings_and_the_clip(self):
        cfg = subtitle_animation.config(_CFG)
        words = subtitle_animation.split_words(
            _entry(words=_asr_words(), emphasis="天気"), cfg)
        self.assertEqual([w["text"] for w in words if w["emphasis"]],
                         ["天気ですね"])

        from_settings = copy.deepcopy(_CFG)
        from_settings["animation"]["emphasis_words"] = ["とても"]
        cfg2 = subtitle_animation.config(from_settings)
        words2 = subtitle_animation.split_words(_entry(words=_asr_words()), cfg2)
        self.assertEqual([w["text"] for w in words2 if w["emphasis"]], ["とても"])

    # 折り返し (\N) をまたぐ語は行の境目で分ける
    def test_words_are_assigned_to_lines(self):
        cfg = subtitle_animation.config(_CFG)
        words = subtitle_animation.split_words(
            _entry(text="今日はとても\\N良い天気ですね"), cfg)

        self.assertTrue(any(w["line"] == 1 for w in words))
        self.assertEqual("".join(w["text"] for w in words), _TEXT)


class FadeTest(unittest.TestCase):

    def test_no_fade_tag_without_flags(self):
        cfg = subtitle_animation.config(_CFG)
        self.assertEqual(subtitle_animation.fade_tag(_entry(), cfg), "")

    def test_fade_tag(self):
        cfg = subtitle_animation.config(_CFG)
        self.assertEqual(
            subtitle_animation.fade_tag(_entry(fade_in=True, fade_out=True), cfg),
            "\\fad(200,200)")
        self.assertEqual(
            subtitle_animation.fade_tag(_entry(fade_in=True), cfg),
            "\\fad(200,0)")

    # 表示時間より長いフェードは縮める (出た瞬間に消えないように)
    def test_fade_is_clamped_to_the_duration(self):
        cfg = subtitle_animation.config(_CFG)
        entry = _entry(start=0.0, end=0.2, fade_in=True, fade_out=True)
        self.assertEqual(subtitle_animation.fade_tag(entry, cfg), "\\fad(100,100)")

    # プレビュー用の不透明度は \fad と同じ直線補間になる
    def test_fade_alpha_follows_the_tag(self):
        cfg = subtitle_animation.config(_CFG)
        entry = _entry(fade_in=True, fade_out=True)
        self.assertAlmostEqual(
            subtitle_animation.fade_alpha(entry, cfg, 0.0), 0.0, places=3)
        self.assertAlmostEqual(
            subtitle_animation.fade_alpha(entry, cfg, 0.1), 0.5, places=3)
        self.assertAlmostEqual(
            subtitle_animation.fade_alpha(entry, cfg, 1.0), 1.0, places=3)
        self.assertAlmostEqual(
            subtitle_animation.fade_alpha(entry, cfg, 1.9), 0.5, places=3)


class EventsTest(unittest.TestCase):

    def setUp(self):
        self.cfg = subtitle_animation.config(_CFG)
        self.profile = subtitle_generator.build_font_profile(_CFG)
        self.anchor = {"an": 2, "x": 960.0, "y": 1020.0}

    def _events(self, entry):
        return subtitle_animation.events(
            entry, self.cfg, _CFG, self.profile, 1920, 1080, anchor=self.anchor)

    # 指定が無ければ何も作らない (呼び出し側は従来どおり 1 行書く)
    def test_no_events_without_animation(self):
        self.assertEqual(self._events(_entry()), [])
        self.assertEqual(self._events(_entry(fade_in=True)), [],
                         "フェードだけで単語ごとのイベントを作ってはいけない")

    # Word Pop: 単語ごとに 1 イベント。拡大 → 元の大きさへ戻す。
    def test_word_pop_events(self):
        entry = _entry(words=_asr_words(), animation="word_pop", emphasis="天気")
        events = self._events(entry)

        self.assertEqual(len(events), 4)
        for event in events:
            self.assertIn("\\an5\\pos(", event["text"])
            self.assertIn("\\fscx130\\fscy130", event["text"])
            self.assertIn("\\t(80,200,\\fscx100\\fscy100)", event["text"])
            # 現れた語はテロップの終わりまで残る (読み返せるように)
            self.assertAlmostEqual(event["end"], entry["end"], places=3)
        # 語は時間順に現れる
        starts = [e["start"] for e in events]
        self.assertEqual(starts, sorted(starts))
        self.assertAlmostEqual(starts[0], entry["start"], places=3)
        # 強調語だけ色と大きさが変わる
        emphasised = [e for e in events if "\\c&H4DD3FF&" in e["text"]]
        self.assertEqual(len(emphasised), 1)
        # 強調語の大きさは「設定の文字サイズ x emphasis_scale」
        size = round(_CFG["font_size"] * _CFG["animation"]["emphasis_scale"] / 100.0)
        self.assertIn(f"\\fs{size}", emphasised[0]["text"])

    # Bounce: \move を段に分ける (ASS の \t は位置を動かせないため / §1)
    def test_bounce_events(self):
        entry = _entry(words=_asr_words(), animation="bounce")
        events = self._events(entry)

        self.assertGreater(len(events), 4)
        self.assertTrue(any("\\move(" in e["text"] for e in events))
        # 跳ね終わった語はテロップの終わりまで静止して残る
        held = [e for e in events if "\\pos(" in e["text"]]
        self.assertTrue(held, "静止して残るイベントが無い")
        self.assertAlmostEqual(held[0]["end"], entry["end"], places=3)
        # どのイベントも表示時間の中に収まる
        for event in events:
            self.assertGreaterEqual(event["start"], entry["start"] - 1e-6)
            self.assertLessEqual(event["end"], entry["end"] + 1e-6)

    # Big Word: 画面中央へ 1 語ずつ。前の語は消える (置き換わる)。
    def test_big_word_events(self):
        entry = _entry(words=_asr_words(), animation="big_word")
        events = self._events(entry)

        self.assertEqual(len(events), 4)
        # 文字サイズは「設定の文字サイズ x big_word_scale」
        size = round(_CFG["font_size"] * _CFG["animation"]["big_word_scale"] / 100.0)
        for event in events:
            self.assertIn("\\an5\\pos(960.0,540.0)", event["text"])
            self.assertIn(f"\\fs{size}", event["text"])
        # 区間は重ならず、順に切り替わる
        for before, after in zip(events, events[1:]):
            self.assertAlmostEqual(before["end"], after["start"], places=3)

    # 1 語が短すぎても最低時間 (big_word_min_ms) は出す
    def test_big_word_minimum_duration(self):
        cfg = copy.deepcopy(_CFG)
        cfg["animation"]["big_word_min_ms"] = 500
        self.cfg = subtitle_animation.config(cfg)
        spans = subtitle_animation.big_word_spans(
            _entry(words=_asr_words(), animation="big_word"), self.cfg)
        self.assertTrue(spans)
        self.assertGreaterEqual(spans[0]["end"] - spans[0]["start"], 0.4)

    # フェードは種類と併用できる
    def test_fade_is_combined_with_the_kind(self):
        entry = _entry(words=_asr_words(), animation="word_pop", fade_in=True)
        for event in self._events(entry):
            self.assertIn("\\fad(200,0)", event["text"])

    # 語数が上限を超えたら行単位の動きへ落ちる (§10)
    def test_too_many_words_falls_back_to_the_line(self):
        cfg = copy.deepcopy(_CFG)
        cfg["animation"]["max_words_per_clip"] = 2
        self.cfg = subtitle_animation.config(cfg)
        events = self._events(_entry(words=_asr_words(), animation="word_pop"))

        self.assertEqual(len(events), 1, "行単位なら 1 イベントになる")
        self.assertIn("\\fscx130", events[0]["text"])
        self.assertNotIn("\\pos(", events[0]["text"])

    def test_too_many_events_falls_back_to_the_line(self):
        cfg = copy.deepcopy(_CFG)
        cfg["animation"]["max_events_per_clip"] = 3
        self.cfg = subtitle_animation.config(cfg)
        events = self._events(_entry(words=_asr_words(), animation="bounce"))

        self.assertLessEqual(len(events), 8)
        self.assertTrue(all("\\move(" in e["text"] or "\\pos(" in e["text"]
                            for e in events))

    # 機能ごと切れる (enabled=false では何も作らない)
    def test_disabled_makes_no_events(self):
        cfg = copy.deepcopy(_CFG)
        cfg["animation"]["enabled"] = False
        self.cfg = subtitle_animation.config(cfg)
        self.assertEqual(
            self._events(_entry(words=_asr_words(), animation="word_pop")), [])


class AssFileTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="anim_ass_")
        self.path = os.path.join(self.dir, "out.ass")
        self.profile = subtitle_generator.build_font_profile(_CFG)

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _build(self, entries, cfg=None):
        subtitle_generator.build_subtitle_file(
            entries, self.profile, self.path, 1920, 1080, cfg or _CFG)
        with io.open(self.path, encoding="utf-8") as fp:
            return fp.read()

    def _dialogues(self, entries):
        return [line for line in self._build(entries).splitlines()
                if line.startswith("Dialogue:")]

    # 【要】アニメ指定が無いテロップの ASS は 1 文字も変わらない (§0)
    def test_plain_subtitle_is_unchanged(self):
        expected = ("Dialogue: 0,0:00:01.00,0:00:03.00,Streamer,,0,0,0,,"
                    "{\\an2\\pos(960.0,1020.0)}" + _TEXT)
        self.assertEqual(self._dialogues([_entry()]), [expected])

    # フェードだけなら従来の 1 行へタグが足される
    def test_fade_only_keeps_one_line(self):
        lines = self._dialogues([_entry(fade_in=True, fade_out=True)])
        self.assertEqual(len(lines), 1)
        self.assertIn("{\\fad(200,200)\\an2\\pos(960.0,1020.0)}", lines[0])

    # Word Pop は単語ごとの行になる (従来の 1 行は書かれない)
    def test_word_pop_writes_one_line_per_word(self):
        lines = self._dialogues([_entry(words=_asr_words(), animation="word_pop")])
        self.assertEqual(len(lines), 4)
        for line in lines:
            self.assertIn("\\an5\\pos(", line)
        self.assertFalse(any(line.endswith("}" + _TEXT) for line in lines),
                         "本文まるごとの行が残っている")

    # 個別のフォント・サイズ・色は単語ごとの行にも付く (§R3)
    def test_individual_overrides_are_kept(self):
        lines = self._dialogues([_entry(words=_asr_words(), animation="word_pop",
                                        font="Meiryo", font_size=80,
                                        color="#FF0000")])
        self.assertTrue(lines)
        for line in lines:
            self.assertIn("\\fnMeiryo", line)
            self.assertIn("\\fs80", line)
            self.assertIn("\\1c&H0000FF&", line)

    # コメント役割は動きを落としてフェードだけにする (§12 ②)
    def test_comment_role_gets_fade_only(self):
        lines = self._dialogues([_entry(role="comment", animation="word_pop",
                                        fade_in=True, words=_asr_words())])
        # 背景 (箱) + 本文 の 2 行。単語ごとの行は作らない
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("\\fad(200,0)", line)
        self.assertFalse(any("\\fscx130" in line for line in lines))

    # 設定で機能を切ったら従来と同じ 1 行に戻る (§11)
    def test_disabled_falls_back_to_the_plain_line(self):
        cfg = copy.deepcopy(_CFG)
        cfg["animation"]["enabled"] = False
        text = self._build(
            [_entry(words=_asr_words(), animation="word_pop", fade_in=True)], cfg)
        lines = [line for line in text.splitlines()
                 if line.startswith("Dialogue:")]
        self.assertEqual(lines, [
            "Dialogue: 0,0:00:01.00,0:00:03.00,Streamer,,0,0,0,,"
            "{\\an2\\pos(960.0,1020.0)}" + _TEXT])


if __name__ == "__main__":
    unittest.main()
