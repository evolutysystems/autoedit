# 採点スイープ用のフレーム抽出 (docs/AiEditor/resolve2.md §7 / P1) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 要件 (docs/AiEditor/resolve2.md §7):
#   ・**1 回のデコードで全部抜く** (1 枚ごとに ffmpeg を起動しない)
#   ・キーフレームだけデコードする (-skip_frame nokey)
#   ・時刻は連番から逆算せず、showinfo の pts_time を 1 枚ごとに添える
#   ・対応が取れないときは**間違った時刻を付けずに捨てる**
# ffmpeg は呼ばない (run を差し替える)。
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ai import vision

_SETTINGS = {"ffmpeg": {},
             "ai": {"scan": {"enabled": True, "frame_interval_sec": 30,
                             "frame_edge_px": 512, "max_frames": 400,
                             "fast_decode": True}}}


# showinfo が stderr へ出す行を模す
def _showinfo(times):
    return "\n".join(
        "[Parsed_showinfo_2 @ 0000] n:{0} pts:{1} pts_time:{2} "
        "pos:1 fmt:yuvj420p sar:1/1 s:512x288".format(index, int(at * 1000), at)
        for index, at in enumerate(times))


# ffmpeg の代わり。出力パターンの連番で count 枚だけ作り、stderr を返す。
def _fake_run(count, times, returncode=0):
    def run(command):
        pattern = command[-1]
        for index in range(count):
            with open(pattern % (index + 1), "wb") as handle:
                handle.write(b"jpeg")
        run.commands.append(list(command))

        return returncode, _showinfo(times)

    run.commands = []

    return run


class FramesEveryTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.workdir = os.path.join(self._dir.name, "scan")
        self.input_path = os.path.join(self._dir.name, "vod.mp4")
        with open(self.input_path, "wb") as handle:
            handle.write(b"0")

    def tearDown(self):
        self._dir.cleanup()

    def _call(self, run, **kwargs):
        return vision.frames_every(self.input_path, self.workdir, _SETTINGS,
                                   run=run, **kwargs)

    # 1 回のデコードで抜く (ffmpeg の起動は 1 回だけ)
    def test_single_pass(self):
        run = _fake_run(3, [0.0, 30.0, 60.0])
        frames = self._call(run)
        self.assertEqual(len(run.commands), 1)
        self.assertEqual(len(frames), 3)

    # キーフレームだけデコードし、showinfo を通し、1 ファイルへ連番で書く
    def test_command_shape(self):
        run = _fake_run(1, [0.0])
        self._call(run)
        command = run.commands[0]
        self.assertIn("-skip_frame", command)
        self.assertEqual(command[command.index("-skip_frame") + 1], "nokey")
        chain = command[command.index("-vf") + 1]
        self.assertIn("select=", chain)
        self.assertIn("showinfo", chain)
        self.assertIn("512", chain)
        self.assertIn("-vsync", command)

    # keyframes_only=False なら -skip_frame を付けない
    def test_all_frames_mode(self):
        settings = {"ffmpeg": {}, "ai": {"scan": dict(_SETTINGS["ai"]["scan"],
                                                      fast_decode=False)}}
        run = _fake_run(1, [0.0])
        vision.frames_every(self.input_path, self.workdir, settings, run=run)
        self.assertNotIn("-skip_frame", run.commands[0])

    # 時刻は showinfo の pts_time を順に当てる (連番から逆算しない)
    def test_times_come_from_showinfo(self):
        times = [0.0, 33.333, 66.35, 98.6]
        frames = self._call(_fake_run(len(times), times))
        self.assertEqual([round(f.at_sec, 3) for f in frames], times)
        for frame in frames:
            self.assertTrue(os.path.exists(frame.path))

    # 行がファイルより少ない = 対応を取れない → 全部捨てる (間違った時刻を付けない)
    def test_missing_showinfo_lines_discards_everything(self):
        frames = self._call(_fake_run(5, [0.0, 30.0]))
        self.assertEqual(frames, [])
        self.assertEqual(os.listdir(self.workdir), [])

    # 上限を超えたら均等に間引き、落とした絵は消す
    def test_thins_out_to_the_limit(self):
        times = [float(i) * 30.0 for i in range(10)]
        frames = self._call(_fake_run(10, times), max_frames=4)
        self.assertEqual(len(frames), 4)
        self.assertEqual(len(os.listdir(self.workdir)), 4)
        # 端は残す
        self.assertEqual(frames[0].at_sec, 0.0)
        self.assertEqual(frames[-1].at_sec, 270.0)

    # 0 を渡すと無制限 (間引かない)
    def test_zero_limit_keeps_all(self):
        times = [float(i) * 30.0 for i in range(10)]
        self.assertEqual(len(self._call(_fake_run(10, times), max_frames=0)), 10)

    # 元動画が無ければ空 (例外にしない)
    def test_missing_input_returns_empty(self):
        run = _fake_run(1, [0.0])
        self.assertEqual(
            vision.frames_every("nope.mp4", self.workdir, _SETTINGS, run=run), [])
        self.assertEqual(run.commands, [])

    # ffmpeg が失敗しても空を返す (スイープを飛ばして機械採点で続ける)
    def test_failure_returns_empty(self):
        self.assertEqual(self._call(_fake_run(2, [0.0, 30.0], returncode=1)), [])

    # 例外が出ても外へ出さない
    def test_exception_returns_empty(self):
        def run(command):
            raise OSError("ffmpeg が無い")

        self.assertEqual(self._call(run), [])

    # 間隔 0 は何もしない
    def test_zero_interval_returns_empty(self):
        run = _fake_run(1, [0.0])
        self.assertEqual(self._call(run, interval_sec=-1.0), [])
        self.assertEqual(run.commands, [])

    # フィルタへ入れる数値はロケールに依らない書式
    def test_filter_number_format(self):
        self.assertEqual(vision._ff_number(30.0), "30")
        self.assertEqual(vision._ff_number(7.5), "7.5")

    def test_scan_frame_repr(self):
        frame = vision.ScanFrame(12.5, "a.jpg")
        self.assertIn("12.500", repr(frame))


if __name__ == "__main__":
    unittest.main()
