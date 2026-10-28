# 元 VOD の区間だけを取り直す経路の単体テスト (docs/request/ver9/resolve5.md §7.1)
# 実行: python -m unittest discover -s tests
#
# 方針: 「元 VOD を手放しても、要る区間だけ取り直せばセクションを足せる」
#
# 重点:
#   ・**位置が狂わないこと**。素材の原点はプレイリストの EXTINF 累積で決まる。
#     ここがずれると、指定とは違う場面が挿入されてしまう。
#     期待値は実測値そのもの (vod_2910070011 / セグメント 20 = 218.993 秒) を使う。
#   ・twitch-dl の切り詰めに頼らないこと (整数秒へ切り捨てられ末尾が最大 1 秒欠ける)。
#     --start/--end へは余裕を付けた整数秒を渡し、正確な切り出しは自前で行う。
#   ・取得が不完全なら**黙って進めない**こと (欠番・範囲不足は失敗させる)。
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import twitch_source, vod_range
from src.exceptions import InputError, TwitchError

# 実データ (vod_2910070011 / chunked) の先頭 24 セグメントをそのまま写したもの。
# EXTINF はセグメントごとに違うため、均一な作り物では原点の検証にならない。
_EXTINF = [10.637, 10.000, 10.001, 10.563, 10.001, 10.585, 11.195, 10.916,
           11.685, 12.897, 10.833, 10.335, 11.006, 12.240, 10.567, 10.362,
           10.358, 10.818, 11.997, 11.997, 11.997, 11.997, 11.997, 11.997]

# セグメント 20 の開始秒 (実測値)。twitch-dl が算出する crop_start と一致した値。
_ORIGIN_20 = 218.993


def _playlist(durations=None, total=None):
    rows = list(durations if durations is not None else _EXTINF)
    declared = sum(rows) if total is None else total
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:14",
             "#EXT-X-PLAYLIST-TYPE:EVENT", "#EXT-X-TWITCH-ELAPSED-SECS:0.000",
             f"#EXT-X-TWITCH-TOTAL-SECS:{declared:.3f}"]
    for index, duration in enumerate(rows):
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(f"{index}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


class ParsePlaylistTest(unittest.TestCase):

    # セグメントの開始秒が EXTINF 累積で並ぶこと (要素番号 = 保存名の番号)
    def test_starts_follow_extinf(self):
        starts, total = vod_range.parse_segment_starts(_playlist())
        self.assertEqual(len(starts), len(_EXTINF))
        self.assertAlmostEqual(starts[0], 0.0, places=6)
        self.assertAlmostEqual(starts[1], 10.637, places=6)
        self.assertAlmostEqual(total, sum(_EXTINF), places=6)

    # 本題: セグメント 20 の原点が実測値と一致すること
    def test_origin_matches_the_measured_value(self):
        starts, _total = vod_range.parse_segment_starts(_playlist())
        self.assertAlmostEqual(vod_range.segment_origin(starts, 20),
                               _ORIGIN_20, places=3)

    # 表に無い番号は失敗させる (位置の分からない素材を 0 秒扱いにしない)
    def test_unknown_segment_is_rejected(self):
        starts, _total = vod_range.parse_segment_starts(_playlist())
        for index in (len(starts), -1, None):
            with self.assertRaises(TwitchError):
                vod_range.segment_origin(starts, index)

    # 宣言された総尺を読めること
    def test_declared_total(self):
        self.assertAlmostEqual(vod_range.declared_total_sec(_playlist()),
                               sum(_EXTINF), places=3)
        self.assertIsNone(vod_range.declared_total_sec("#EXTM3U\n"))

    # EXTINF を伴わない URI 行は数に入れない (位置が 1 つずれるのを防ぐ)
    def test_uri_without_extinf_is_ignored(self):
        text = "#EXTM3U\nstray.ts\n#EXTINF:10.000,\n0.ts\n"
        starts, total = vod_range.parse_segment_starts(text)
        self.assertEqual(len(starts), 1)
        self.assertAlmostEqual(total, 10.0, places=6)


class VerifyTest(unittest.TestCase):

    # 累積と宣言が合っていれば通る
    def test_total_matches(self):
        text = _playlist()
        _starts, total = vod_range.parse_segment_starts(text)
        vod_range.verify_total(text, total)        # 例外が出ないこと

    # 食い違えば位置計算の前提が崩れているため失敗させる
    def test_total_mismatch_is_rejected(self):
        text = _playlist(total=sum(_EXTINF) + 60.0)
        _starts, total = vod_range.parse_segment_starts(text)
        with self.assertRaises(TwitchError):
            vod_range.verify_total(text, total)

    # 宣言が無いプレイリストは突き合わせない (独自タグのため無い場合がある)
    def test_total_without_declaration_is_skipped(self):
        vod_range.verify_total("#EXTM3U\n", 123.0)

    # 連番なら番号の一覧を返す
    def test_continuous_segments(self):
        self.assertEqual(
            vod_range.verify_continuous(["00020.ts", "00021.ts", "00022.ts"]),
            [20, 21, 22])

    # 欠番は取得が不完全な証拠。繋げると中身が飛ぶため失敗させる
    def test_missing_segment_is_rejected(self):
        with self.assertRaises(TwitchError):
            vod_range.verify_continuous(["00020.ts", "00022.ts"])

    # 番号として読めない名前が混ざっていたら失敗させる
    def test_unreadable_name_is_rejected(self):
        with self.assertRaises(TwitchError):
            vod_range.verify_continuous(["00020.ts", "playlist.m3u8"])

    # 指定区間を覆っていれば通る
    def test_coverage_ok(self):
        vod_range.verify_coverage(218.993, 47.988, 225.0, 255.0)

    # 前が足りない / 後ろが足りない場合は失敗させる
    def test_coverage_shortfall_is_rejected(self):
        with self.assertRaises(TwitchError):
            vod_range.verify_coverage(218.993, 47.988, 200.0, 255.0)
        with self.assertRaises(TwitchError):
            vod_range.verify_coverage(218.993, 47.988, 225.0, 300.0)


class PlanWindowTest(unittest.TestCase):

    # 開始は切り捨て、終了は切り上げ (twitch-dl は整数秒しか受けない)
    def test_window_is_integer_seconds_with_pad(self):
        self.assertEqual(vod_range.plan_window(225.4, 405.2, 3.0), (222, 409))

    # VOD の先頭より前は要求しない
    def test_window_does_not_go_before_zero(self):
        self.assertEqual(vod_range.plan_window(1.0, 20.0, 5.0), (0, 25))

    # 全長が分かっていれば終端で丸める
    def test_window_is_clamped_to_the_vod_length(self):
        self.assertEqual(vod_range.plan_window(8980.0, 8991.0, 5.0, 8991.436),
                         (8975, 8992))

    # twitch-dl は end <= start を受け付けないため、必ず 1 秒以上にする
    def test_window_is_never_empty(self):
        start, end = vod_range.plan_window(0.0, 0.2, 0.0, 0.2)
        self.assertLess(start, end)

    # --start/--end の書式は "H:MM:SS" (分秒は 0〜59 でなければ弾かれる)
    def test_hms_format(self):
        self.assertEqual(vod_range.to_hms(222), "0:03:42")
        self.assertEqual(vod_range.to_hms(8992), "2:29:52")
        self.assertEqual(vod_range.to_hms(0), "0:00:00")
        self.assertEqual(vod_range.to_hms(-5), "0:00:00")

    # 空き容量の見込みはセグメントと連結後の 2 本ぶん
    def test_required_bytes_counts_two_copies(self):
        self.assertEqual(vod_range.required_bytes(60, 60), 60 * 1024 * 1024 * 2)
        self.assertEqual(vod_range.required_bytes(0, 60), 0)


class SegmentNameTest(unittest.TestCase):

    # プレイリストの "20.ts" はキャッシュでは "00020.ts" になる。
    # 番号で突き合わせるため、ゼロ埋めの有無で取り違えないこと
    def test_segment_index_ignores_zero_padding(self):
        self.assertEqual(twitch_source.segment_index("00020.ts"), 20)
        self.assertEqual(twitch_source.segment_index("20.ts"), 20)
        self.assertIsNone(twitch_source.segment_index("playlist.m3u8"))
        self.assertIsNone(twitch_source.segment_index(""))

    # セグメントは番号順に並べる (文字列順ではない)
    def test_list_segments_is_sorted_by_number(self):
        with tempfile.TemporaryDirectory(prefix="vod_range_test_") as folder:
            for name in ("00009.ts", "00010.ts", "00008.ts", "playlist.m3u8"):
                with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
                    handle.write("x")
            self.assertEqual(twitch_source.list_segments(folder),
                             ["00008.ts", "00009.ts", "00010.ts"])

    # 無いディレクトリでも例外にしない (呼び側が理由を出す)
    def test_list_segments_of_missing_dir(self):
        self.assertEqual(twitch_source.list_segments("no_such_dir"), [])


class ConcatTest(unittest.TestCase):

    # 連結リストは行頭 file + 単一引用符。Windows の \\ は / へ直す
    # (リスト内ではエスケープとして解釈されるため)
    def test_concat_list_uses_forward_slashes(self):
        with tempfile.TemporaryDirectory(prefix="vod_range_concat_") as folder:
            dest = os.path.join(folder, "range.mp4")
            with mock.patch.object(vod_range.ffmpeg_runner, "get_ffmpeg_exe",
                                   return_value="ffmpeg"), \
                    mock.patch.object(vod_range.ffmpeg_runner, "execute") as execute:
                vod_range.concat_segments(
                    os.path.join(folder, "seg"), ["00020.ts", "00021.ts"],
                    dest, {})
            with open(os.path.join(folder, "concat.txt"), encoding="utf-8") as handle:
                written = handle.read()
            self.assertNotIn("\\", written)
            self.assertIn("00020.ts'", written)
            self.assertTrue(all(line.startswith("file '")
                                for line in written.strip().splitlines()))
            # 再エンコードしない (素材は VOD からのコピーであるべき)
            command = execute.call_args[0][0]
            self.assertIn("-c", command)
            self.assertIn("copy", command)


class FetchRangeTest(unittest.TestCase):

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(prefix="vod_range_fetch_")
        self.workdir = self._folder.name
        self.addCleanup(self._folder.cleanup)
        self.settings = {
            "ffmpeg": {},
            "archive": {
                "download": {"twitch_dl_path": "twitch-dl"},
                "intro_card": {"enabled": True, "buffer_sec": 1.5},
                "section_add": {"refetch": {"enabled": True, "pad_sec": 3.0,
                                            "min_free_mb_per_min": 60}},
            },
        }
        self.asked = {}

    # download_vod_range を差し替え、セグメントとプレイリストを置いたふりをする
    def _stub_download(self, segments=("00020.ts", "00021.ts", "00022.ts", "00023.ts"),
                       playlist_text=None):
        def _fake(video_id, cache_root, start_hms, end_hms, **kwargs):
            self.asked = {"video_id": video_id, "start": start_hms, "end": end_hms,
                          "quality": kwargs.get("quality")}
            segment_dir = os.path.join(cache_root, str(video_id), "chunked")
            os.makedirs(segment_dir, exist_ok=True)
            for name in segments:
                with open(os.path.join(segment_dir, name), "w", encoding="utf-8") as h:
                    h.write("x")
            playlist = os.path.join(segment_dir, twitch_source.PLAYLIST_NAME)
            with open(playlist, "w", encoding="utf-8") as handle:
                handle.write(_playlist() if playlist_text is None else playlist_text)
            return {"segment_dir": segment_dir, "segments": list(segments),
                    "playlist": playlist, "quality": "chunked"}

        return mock.patch.object(twitch_source, "download_vod_range",
                                 side_effect=_fake)

    def _run(self, start=225.0, end=255.0, duration=47.988, **kwargs):
        with self._stub_download(**kwargs), \
                mock.patch.object(vod_range, "concat_segments",
                                  side_effect=lambda _d, _s, dest, _c: dest), \
                mock.patch.object(vod_range.ffmpeg_runner, "probe_duration",
                                  return_value=duration), \
                mock.patch.object(vod_range.disk, "ensure_free"):
            return vod_range.fetch_range(
                "2910070011", start, end, self.workdir, self.settings,
                quality="source", tag="1", vod_duration_sec=8991.436)

    # 本題: 素材の原点が EXTINF 累積どおりに返ること
    def test_origin_comes_from_the_playlist(self):
        result = self._run()
        self.assertAlmostEqual(result["origin_sec"], _ORIGIN_20, places=3)
        self.assertAlmostEqual(result["end_sec"], _ORIGIN_20 + 47.988, places=3)
        self.assertEqual(result["segments"], 4)
        self.assertEqual(result["quality"], "chunked")

    # twitch-dl へは余裕を付けた整数秒を渡す (切り詰めは任せない)
    def test_download_window_has_padding(self):
        self._run(start=225.0, end=255.0)
        self.assertEqual(self.asked["video_id"], "2910070011")
        self.assertEqual(self.asked["start"], "0:03:42")   # 225 - 3
        self.assertEqual(self.asked["end"], "0:04:18")     # 255 + 3
        self.assertEqual(self.asked["quality"], "source")

    # イントロカードの素 (開始の buffer_sec 秒手前) も入るだけ広げる
    def test_padding_covers_the_intro_buffer(self):
        self.settings["archive"]["intro_card"]["buffer_sec"] = 10.0
        self.settings["archive"]["section_add"]["refetch"]["pad_sec"] = 1.0
        self._run(start=225.0, end=255.0)
        self.assertEqual(self.asked["start"], "0:03:35")   # 225 - 10
        self.assertEqual(self.asked["end"], "0:04:25")     # 255 + 10

    # 欠番のまま素材を作らない
    def test_gap_in_segments_fails(self):
        with self.assertRaises(TwitchError):
            self._run(segments=("00020.ts", "00022.ts"))

    # 連結できた尺が足りなければ失敗させる (静かに短い素材を足さない)
    def test_short_material_fails(self):
        with self.assertRaises(TwitchError):
            self._run(duration=5.0)

    # 連結に失敗した (尺 0) ときも失敗させる
    def test_zero_duration_fails(self):
        with self.assertRaises(TwitchError):
            self._run(duration=0.0)

    # プレイリストの合計が宣言と食い違えば失敗させる
    def test_broken_playlist_fails(self):
        with self.assertRaises(TwitchError):
            self._run(playlist_text=_playlist(total=sum(_EXTINF) + 120.0))

    # 設定で無効なら取りに行かない
    def test_disabled_by_setting(self):
        self.settings["archive"]["section_add"]["refetch"]["enabled"] = False
        with self.assertRaises(InputError):
            self._run()

    # 短すぎる区間は取りに行かない
    def test_too_short_range(self):
        with self.assertRaises(InputError):
            self._run(start=100.0, end=100.0)


if __name__ == "__main__":
    unittest.main()
