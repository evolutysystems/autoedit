# ASS タイムスタンプの丸めと、保存時の丸め (ver9 resolve8 §5) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・隣接する 2 本 (前の end == 次の start) が ASS で重ならないこと
#     → 1 フレームの重なりで libass の衝突回避が働くため、ここが本件の引き金だった
#   ・10ms 未満のテロップが切り捨てで消えないこと
#   ・字幕クリップの保存往復で境界がずれないこと (§3.4)
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import subtitle_generator
from src.settings.settings_window import DEFAULT_SETTINGS
from src.timeline.model import SubtitleClip


def _cfg(**overrides):
    cfg = dict(DEFAULT_SETTINGS["subtitle"])
    cfg.update(overrides)
    return cfg


# items から ASS を書き出して (開始, 終了) の並びを返す
def _times(items):
    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "out.ass")
        subtitle_generator.build_subtitle_file(
            items, subtitle_generator.build_font_profile(_cfg()), path,
            video_width=1920, video_height=1080, subtitle_cfg=_cfg())
        with open(path, encoding="utf-8") as f:
            return [tuple(line.split(",", 3)[1:3])
                    for line in f.read().splitlines() if line.startswith("Dialogue: ")]


class FormatTest(unittest.TestCase):

    # 1/100 秒へ切り捨てる (四捨五入しない)
    def test_truncates_to_centiseconds(self):
        for seconds, expected in (
                (0.0, "0:00:00.00"),
                (0.009, "0:00:00.00"),
                (0.019, "0:00:00.01"),
                (81.615, "0:01:21.61"),
                # 浮動小数の誤差 (81.615 + 誤差) でも同じ値へ落ちることが要点
                (81.61500000000001, "0:01:21.61"),
                (3661.999, "1:01:01.99"),
                (59.999, "0:00:59.99"),
        ):
            self.assertEqual(subtitle_generator._format_ass_time(seconds), expected,
                             f"seconds={seconds!r}")

    # 負値・不正値は 0 へ倒す (従来と同じ安全側)
    def test_invalid_is_zero(self):
        for value in (-1.0, -0.001, None, "bogus"):
            self.assertEqual(subtitle_generator._format_ass_time(value), "0:00:00.00",
                             f"value={value!r}")

    # 時・分の繰り上がり
    def test_carry(self):
        self.assertEqual(subtitle_generator._format_ass_time(3600.0), "1:00:00.00")
        self.assertEqual(subtitle_generator._format_ass_time(7265.43), "2:01:05.43")


class RangeTest(unittest.TestCase):

    # 隙間ゼロで連続する 2 本が ASS で重ならないこと (§2.2 の引き金を塞ぐ)
    def test_adjacent_items_do_not_overlap(self):
        items = [
            {"start": 77.266, "end": 81.615, "text": "前", "role": "streamer"},
            {"start": 81.615, "end": 85.040, "text": "後", "role": "sub"},
        ]
        times = _times(items)
        self.assertEqual(times, [("0:01:17.26", "0:01:21.61"),
                                 ("0:01:21.61", "0:01:25.04")])
        # 前の終わり <= 次の始まり (重なりが無い)
        self.assertLessEqual(times[0][1], times[1][0])

    # 浮動小数の誤差で end だけ上へずれる組でも重ならないこと (報告の件 1)
    def test_float_error_does_not_overlap(self):
        start, duration = 77.266, 4.349
        items = [
            {"start": start, "end": start + duration, "text": "前", "role": "streamer"},
            {"start": 81.615, "end": 85.040, "text": "後", "role": "sub"},
        ]
        times = _times(items)
        self.assertLessEqual(times[0][1], times[1][0])

    # 10ms 未満のテロップが消えないこと (切り捨てで尺ゼロになる)
    def test_short_item_keeps_duration(self):
        start, end = subtitle_generator._format_ass_range(1.0, 1.004)
        self.assertEqual((start, end), ("0:00:01.00", "0:00:01.01"))
        # end < start の壊れた入力でも尺を残す
        self.assertEqual(subtitle_generator._format_ass_range(5.0, 4.0),
                         ("0:00:05.00", "0:00:05.01"))

    # 通常の尺はそのまま (持ち上げない)
    def test_normal_range_is_not_lifted(self):
        self.assertEqual(subtitle_generator._format_ass_range(1.0, 2.5),
                         ("0:00:01.00", "0:00:02.50"))


class SaveRoundingTest(unittest.TestCase):

    # 終端を保ったまま尺を丸める (§3.4)。
    # 開始と尺を別々に丸めると 453.160 + 3.858 = 457.018 の終端が 1ms ずれ、
    # 次のテロップと重なって libass の押し上げを呼ぶ。
    def test_end_is_preserved(self):
        clip = SubtitleClip("s1", 453.1604, 3.8576, text="あ")
        data = clip.to_dict()
        self.assertEqual(data["timeline_start"], 453.16)
        # 終端 457.018 を保つ尺 (= 3.858) になる
        self.assertEqual(data["duration"], 3.858)
        self.assertEqual(round(data["timeline_start"] + data["duration"], 3), 457.018)

    # 隣り合うクリップの境界が保存往復で重ならないこと
    # (ms で比べる。浮動小数の表現誤差は ASS 生成側が ms 丸めで吸収する / §3.3)
    def test_adjacent_clips_keep_boundary(self):
        first = SubtitleClip("s1", 77.2664, 4.3486, text="前")
        second = SubtitleClip("s2", first.timeline_end, 3.4251, text="後")
        a, b = first.to_dict(), second.to_dict()
        self.assertLessEqual(round(a["timeline_start"] + a["duration"], 3),
                             b["timeline_start"])
        # 焼き込みの時刻も重ならない
        restored_a = SubtitleClip.from_dict(a)
        restored_b = SubtitleClip.from_dict(b)
        self.assertLessEqual(
            subtitle_generator._format_ass_time(restored_a.timeline_end),
            subtitle_generator._format_ass_time(restored_b.timeline_start))

    # 復元しても値が壊れないこと (旧ファイルと同じ形式のまま)
    def test_roundtrip(self):
        clip = SubtitleClip("s1", 1.2345, 2.3456, text="あ")
        restored = SubtitleClip.from_dict(clip.to_dict())
        self.assertEqual(restored.timeline_start, 1.234)
        self.assertEqual(round(restored.timeline_end, 6), 3.58)


if __name__ == "__main__":
    unittest.main()
