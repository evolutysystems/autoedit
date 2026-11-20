# テロップアニメーション (docs/request/ver12/resolve2-add.md) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・12 種すべてがキーフレームの層を返し、ASS のイベントへ展開できること (§4)
#   ・キーフレームの補間 (state_at) と区間の切り方が尺に収まること (§3)
#   ・フェードが区間ごとに点滅せず、テロップ全体で 1 回だけ掛かること (\fade の負の時刻 / §2)
#   ・基準点が上下中央へ移っても位置が変わらないこと (行数 x 文字サイズ / §5)
#   ・震えるがイベントの上限に収まること (§6)
#   ・**アニメ指定の無いテロップの ASS が 1 文字も変わらないこと** (resolve2 §0)
#   ・コメント役割はフェードのみになること (resolve2 §12 ②)
#   ・廃止した種類 (word_pop / bounce / big_word / clip_in / glow_wipe / wave /
#     ez_from_right / ez_from_left) は動きなしとして扱うこと (§1 / §13)
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
_ANCHOR = {"an": 2, "x": 960.0, "y": 1020.0}


def _entry(**fields):
    base = {"start": 1.0, "end": 3.0, "text": _TEXT, "use": True,
            "role": "streamer", "font": "", "font_size": None}
    base.update(fields)
    return base


def _base():
    return {"fill": "FFFFFF", "outline": "000000", "a1": 0, "a3": 0,
            "a4": 0x64, "bord": 3.0}


class KindsTest(unittest.TestCase):

    # 要望の 12 種が揃っている (リストに無いもの・削除したものは入れない / resolve2-add §1 / §13)
    def test_seventeen_kinds(self):
        self.assertEqual(len(subtitle_animation.ANIMATIONS), 12)
        self.assertEqual(len(set(subtitle_animation.ANIMATIONS)), 12)

    def test_removed_kinds_are_not_animations(self):
        for old in ("word_pop", "bounce", "big_word", "clip_in", "glow_wipe", "wave",
                    "ez_from_right", "ez_from_left"):
            self.assertEqual(subtitle_animation.animation_of({"animation": old}), "")

    # どの種類も層を返し、最後は「動き終わった状態」(既定値) に落ち着く
    # (震えるは表示中ずっと動くため除く)
    def test_every_kind_settles(self):
        cfg = subtitle_animation.config(_CFG)
        for kind in subtitle_animation.ANIMATIONS:
            plan = subtitle_animation.spec(kind, cfg, 48, 3.0, 1920)
            self.assertIsNotNone(plan, kind)
            self.assertTrue(plan["layers"], kind)
            if kind == subtitle_animation.ANIM_SHAKE:
                continue
            for layer in plan["layers"]:
                if not layer["hold"]:
                    continue
                final = subtitle_animation.state_at(layer["keys"], 3.0)
                for key, value in subtitle_animation.NEUTRAL.items():
                    self.assertAlmostEqual(final[key], value, places=3,
                                           msg=f"{kind}: {key}")

    # キーは時刻順で、尺を超えない (短いテロップでは動きの途中で終わる)
    def test_keys_are_cut_by_the_duration(self):
        cfg = subtitle_animation.config(_CFG)
        for kind in subtitle_animation.ANIMATIONS:
            plan = subtitle_animation.spec(kind, cfg, 48, 0.1, 1920)
            for layer in plan["layers"]:
                times = [t for t, _s in layer["keys"]]
                self.assertEqual(times, sorted(times), kind)
                self.assertTrue(all(t <= 0.1 + 1e-9 for t in times), kind)


class StateTest(unittest.TestCase):

    def test_state_at_interpolates_linearly(self):
        keys = [(0.0, subtitle_animation._state(sx=60)),
                (1.0, subtitle_animation._state(sx=100))]
        self.assertAlmostEqual(subtitle_animation.state_at(keys, 0.5)["sx"], 80.0)
        # キーの外は端の状態を保つ
        self.assertAlmostEqual(subtitle_animation.state_at(keys, -1.0)["sx"], 60.0)
        self.assertAlmostEqual(subtitle_animation.state_at(keys, 5.0)["sx"], 100.0)

    # 白・黒への寄せは塗りと縁の両方の色を変える
    def test_mix_color(self):
        white = subtitle_animation._state(white=1.0)
        dark = subtitle_animation._state(dark=1.0)
        self.assertEqual(subtitle_animation.mix_color("FF0000", white), "#FFFFFF")
        self.assertEqual(subtitle_animation.mix_color("FF0000", dark), "#000000")
        self.assertEqual(subtitle_animation.mix_color("FF0000",
                                                      subtitle_animation.NEUTRAL),
                         "#FF0000")

    # 表示範囲の px 換算は直線 (光の帯の幅が区間で変わらない)
    def test_clip_range_is_linear(self):
        cfg = subtitle_animation.config(_CFG)
        widths = []
        for center in (0.2, 0.5, 0.8):
            state = subtitle_animation._state(clip_l=center - 0.1, clip_r=center + 0.1)
            x1, x2 = subtitle_animation.clip_range(100.0, 500.0, 40, cfg, state)
            widths.append(round(x2 - x1, 6))
        self.assertEqual(len(set(widths)), 1)


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

    # 区間のイベントへ付けるフェードは、テロップ開始からのずれを引いた時刻になる
    def test_event_fade_is_relative_to_the_clip(self):
        tag = subtitle_animation._event_fade_tag(200, 200, 2000, 500)
        self.assertEqual(tag, "\\fade(255,0,255,-500,-300,1300,1500)")
        self.assertEqual(subtitle_animation._event_fade_tag(0, 0, 2000, 0), "")


class EventsTest(unittest.TestCase):

    def setUp(self):
        self.cfg = subtitle_animation.config(_CFG)
        self.profile = subtitle_generator.build_font_profile(_CFG)

    def _events(self, entry, anchor=_ANCHOR):
        return subtitle_animation.events(
            entry, self.cfg, _CFG, self.profile, 1920, 1080, anchor=anchor,
            base=_base())

    # 指定が無ければ何も作らない (呼び出し側は従来どおり 1 行書く)
    def test_no_events_without_animation(self):
        self.assertEqual(self._events(_entry()), [])
        self.assertEqual(self._events(_entry(fade_in=True)), [],
                         "フェードだけで区間ごとのイベントを作ってはいけない")

    # 基準点が無ければ動かせない (libass に位置を任せると動きが崩れる)
    def test_no_events_without_anchor(self):
        self.assertEqual(self._events(_entry(animation="poyon"), anchor=None), [])

    # どの種類もイベントを出し、すべて表示時間の中に収まる
    def test_every_kind_makes_events_within_the_clip(self):
        for kind in subtitle_animation.ANIMATIONS:
            entry = _entry(animation=kind)
            events = self._events(entry)
            self.assertTrue(events, kind)
            for event in events:
                self.assertGreaterEqual(event["start"], entry["start"] - 1e-6, kind)
                self.assertLessEqual(event["end"], entry["end"] + 1e-6, kind)
                self.assertRegex(event["text"], r"\\(pos|move)\(", kind)

    # 下段基準 (\an2) は上下中央 (\an5) へ移し、y を塊の高さの半分だけ上げる
    # (libass で実測: 2 行 Fontsize 48 の \an2\pos(x,300) と \an5\pos(x,252) が一致)
    def test_anchor_moves_to_the_vertical_center(self):
        size = _CFG["font_size"]
        event = self._events(_entry(animation="shock1"))[-1]
        self.assertIn(f"\\an5\\pos(960.0,{1020.0 - size / 2.0:.1f})", event["text"])
        two_lines = self._events(_entry(text="今日は\\N良い天気", animation="shock1"))[-1]
        self.assertIn(f"\\an5\\pos(960.0,{1020.0 - size:.1f})", two_lines["text"])

    # 拡大の種類は \fscx/\fscy を \t で 100 へ戻す
    def test_poyon_scales_back(self):
        events = self._events(_entry(animation="poyon"))
        self.assertIn("\\fscx60.0\\fscy60.0", events[0]["text"])
        self.assertTrue(any("\\t(0," in e["text"] and "\\fscx100.0" in e["text"]
                            for e in events))

    # 発光は塗りと縁を白へ寄せ、縁を太らせてぼかす
    def test_glow_in_whitens(self):
        first = self._events(_entry(animation="glow_in"))[0]["text"]
        self.assertIn("\\1c&HFFFFFF&\\3c&HFFFFFF&", first)
        self.assertIn("\\bord9.00", first)       # 元の縁 3 + glow_border 6
        self.assertIn("\\blur10.00", first)

    # 黒ブラーは黒へ寄せる。不透明度は影の元のアルファ (0x64) を保ったまま薄くする
    def test_black_blur_darkens(self):
        first = self._events(_entry(animation="black_blur_zoom_in"))[0]["text"]
        self.assertIn("\\1c&H000000&", first)
        self.assertIn("\\4a&H", first)

    # 衝撃1 は本文の下に四角い光 (描画) を出し、光は消える (残らない)
    def test_shock1_draws_a_box_under_the_text(self):
        entry = _entry(animation="shock1")
        events = self._events(entry)
        self.assertIn("\\p1", events[0]["text"])
        self.assertIn("\\bord0\\shad0", events[0]["text"])
        self.assertLess(events[0]["end"], entry["end"])
        self.assertNotIn("\\p1", events[-1]["text"])
        self.assertAlmostEqual(events[-1]["end"], entry["end"], places=3)

    # 衝撃2 は本文の上に大きな半透明の残像を重ねる
    def test_shock2_ghost_on_top(self):
        events = self._events(_entry(animation="shock2"))
        self.assertIn("\\pos(", events[0]["text"])          # 本文 (静止)
        self.assertIn("\\fscx170.0", events[1]["text"])     # 残像

    # ぴかっとは \clip を \t で動かす (光の帯が横切る)
    def test_shine_uses_animated_clip(self):
        events = self._events(_entry(animation="shine"))
        self.assertTrue(any("\\t(0," in e["text"] and "\\clip(" in e["text"]
                            for e in events))

    # フェードは種類と併用でき、区間ごとではなくテロップ全体で掛かる
    def test_fade_is_combined_with_the_kind(self):
        entry = _entry(animation="poyon", fade_in=True)
        for event in self._events(entry):
            offset = int(round((event["start"] - entry["start"]) * 1000))
            self.assertIn(f"\\fade(255,0,0,{-offset},{200 - offset},", event["text"])

    # 震えるはイベントの上限に収まる (長いテロップでも刻みを粗くする)
    def test_shake_respects_the_event_limit(self):
        limit = self.cfg["max_events_per_clip"]
        events = self._events(_entry(start=0.0, end=30.0, animation="shake"))
        self.assertLessEqual(len(events), limit + 2)

    # 機能ごと切れる (enabled=false では何も作らない)
    def test_disabled_makes_no_events(self):
        cfg = copy.deepcopy(_CFG)
        cfg["animation"]["enabled"] = False
        self.cfg = subtitle_animation.config(cfg)
        self.assertEqual(self._events(_entry(animation="poyon")), [])


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

    def _dialogues(self, entries, cfg=None):
        return [line for line in self._build(entries, cfg).splitlines()
                if line.startswith("Dialogue:")]

    # 【要】アニメ指定が無いテロップの ASS は 1 文字も変わらない
    def test_plain_subtitle_is_unchanged(self):
        expected = ("Dialogue: 0,0:00:01.00,0:00:03.00,Streamer,,0,0,0,,"
                    "{\\an2\\pos(960.0,1020.0)}" + _TEXT)
        self.assertEqual(self._dialogues([_entry()]), [expected])

    # 廃止した種類を持つテロップも従来の 1 行になる (動きなし)
    def test_removed_kind_writes_the_plain_line(self):
        expected = ("Dialogue: 0,0:00:01.00,0:00:03.00,Streamer,,0,0,0,,"
                    "{\\an2\\pos(960.0,1020.0)}" + _TEXT)
        self.assertEqual(self._dialogues([_entry(animation="word_pop")]), [expected])

    # フェードだけなら従来の 1 行へタグが足される
    def test_fade_only_keeps_one_line(self):
        lines = self._dialogues([_entry(fade_in=True, fade_out=True)])
        self.assertEqual(len(lines), 1)
        self.assertIn("{\\fad(200,200)\\an2\\pos(960.0,1020.0)}", lines[0])

    # 動きを付けると区間ごとの行になる (従来の 1 行は書かれない)
    def test_animation_replaces_the_plain_line(self):
        lines = self._dialogues([_entry(animation="poyon")])
        self.assertGreater(len(lines), 1)
        self.assertFalse(any("\\an2\\pos(" in line for line in lines),
                         "従来の 1 行が残っている")

    # 個別のフォント・サイズ・色は区間ごとの行にも付く
    def test_individual_overrides_are_kept(self):
        lines = self._dialogues([_entry(animation="poyon", font="Meiryo",
                                        font_size=80, color="#FF0000")])
        self.assertTrue(lines)
        for line in lines:
            self.assertIn("\\fnMeiryo", line)
            self.assertIn("\\fs80", line)
            self.assertIn("\\1c&H0000FF&", line)

    # 発光で白へ寄せた色は、個別色 (赤) から戻ってくる
    def test_glow_returns_to_the_individual_color(self):
        lines = self._dialogues([_entry(animation="glow_in", color="#FF0000")])
        self.assertTrue(any("\\1c&H0000FF&\\3c" in line.split("}", 1)[0]
                            .split("\\t(")[-1] for line in lines))

    # コメント役割は動きを落としてフェードだけにする
    def test_comment_role_gets_fade_only(self):
        lines = self._dialogues([_entry(role="comment", animation="poyon",
                                        fade_in=True)])
        # 背景 (箱) + 本文 の 2 行。区間ごとの行は作らない
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("\\fad(200,0)", line)
        self.assertFalse(any("\\fscx60" in line for line in lines))

    # 設定で機能を切ったら従来と同じ 1 行に戻る
    def test_disabled_falls_back_to_the_plain_line(self):
        cfg = copy.deepcopy(_CFG)
        cfg["animation"]["enabled"] = False
        lines = self._dialogues([_entry(animation="poyon", fade_in=True)], cfg)
        self.assertEqual(lines, [
            "Dialogue: 0,0:00:01.00,0:00:03.00,Streamer,,0,0,0,,"
            "{\\an2\\pos(960.0,1020.0)}" + _TEXT])

    # 色と縁の基準は個別色 → 役割の色 の順で決まる
    def test_animation_base(self):
        base = subtitle_generator.animation_base(_entry(color="#123456"), self.profile)
        self.assertEqual(base["fill"], "123456")
        plain = subtitle_generator.animation_base(_entry(), self.profile)
        self.assertEqual(plain["fill"], subtitle_generator._normalize_hex_color(
            self.profile.role_colors["streamer"]) or "FFFFFF")
        self.assertEqual(plain["bord"], float(self.profile.outline_width))


if __name__ == "__main__":
    unittest.main()
