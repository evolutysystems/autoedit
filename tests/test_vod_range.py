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
#   ・**fMP4 (.mp4) の VOD でも成立すること** (error 20261118)。
#     ハイライト等のセグメントは .ts ではなく .mp4 で、#EXT-X-MAP の
#     init セグメントと組にしないと読めないため、連結経路を分ける。
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

    # fMP4 の VOD (ハイライト等) は .mp4 で落ちてくる (error 20261118)
    def test_segment_index_accepts_fmp4(self):
        self.assertEqual(twitch_source.segment_index("00307.mp4"), 307)
        self.assertEqual(twitch_source.segment_index("00307.m4s"), 307)

    # init セグメントをセグメント本体と取り違えないこと
    # (拾ってしまうと番号の連続性検査が壊れる)
    def test_segment_index_ignores_init_sections(self):
        self.assertIsNone(twitch_source.segment_index("init-0.mp4"))
        self.assertIsNone(twitch_source.segment_index("2930581402v0-init.mp4"))
        self.assertIsNone(twitch_source.segment_index("00307.mp4.tmp"))

    # セグメントは番号順に並べる (文字列順ではない)
    def test_list_segments_is_sorted_by_number(self):
        with tempfile.TemporaryDirectory(prefix="vod_range_test_") as folder:
            for name in ("00009.ts", "00010.ts", "00008.ts", "playlist.m3u8"):
                with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
                    handle.write("x")
            self.assertEqual(twitch_source.list_segments(folder),
                             ["00008.ts", "00009.ts", "00010.ts"])

    # fMP4 のキャッシュから本体だけを番号順に拾うこと (init・.tmp は混ぜない)
    def test_list_segments_picks_fmp4_only(self):
        with tempfile.TemporaryDirectory(prefix="vod_range_fmp4_") as folder:
            for name in ("00307.mp4", "00306.mp4", "init-0.mp4",
                         "2930581402v0-init.mp4", "00308.mp4.tmp",
                         "playlist.m3u8", "playlist_downloaded.m3u8"):
                with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
                    handle.write("x")
            self.assertEqual(twitch_source.list_segments(folder),
                             ["00306.mp4", "00307.mp4"])

    # 無いディレクトリでも例外にしない (呼び側が理由を出す)
    def test_list_segments_of_missing_dir(self):
        self.assertEqual(twitch_source.list_segments("no_such_dir"), [])

    # 連結方法を決める材料。混在は安全側の "fmp4" (HLS なら両方読める)
    def test_segment_format(self):
        self.assertEqual(twitch_source.segment_format(["00020.ts", "00021.ts"]), "ts")
        self.assertEqual(twitch_source.segment_format(["00306.mp4"]), "fmp4")
        self.assertEqual(twitch_source.segment_format(["00306.m4s"]), "fmp4")
        self.assertEqual(twitch_source.segment_format(["00020.ts", "00306.mp4"]),
                         "fmp4")
        self.assertEqual(twitch_source.segment_format([]), "")


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


# 連結経路の選択 (error 20261118 §9 R4)
#
# MPEG-TS は従来の concat デマルチプレクサ、fMP4 は取得分プレイリスト経由。
# fMP4 を concat へ流すと "error reading header" で落ちるため、
# ここを取り違えると fMP4 の VOD で区間取り直しが成立しない。
class JoinRangeTest(unittest.TestCase):

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(prefix="vod_range_join_")
        self.folder = self._folder.name
        self.addCleanup(self._folder.cleanup)
        self.dest = os.path.join(self.folder, "range.mp4")

    # セグメントと取得分プレイリストを置いた偽キャッシュを作る
    def _fetched(self, segments, with_playlist=True):
        segment_dir = os.path.join(self.folder, "chunked")
        os.makedirs(segment_dir, exist_ok=True)
        playlist = os.path.join(segment_dir, twitch_source.DOWNLOADED_PLAYLIST_NAME)
        if with_playlist:
            with open(playlist, "w", encoding="utf-8") as handle:
                handle.write("#EXTM3U\n")
        return {"segment_dir": segment_dir, "segments": list(segments),
                "downloaded_playlist": playlist if with_playlist else "",
                "format": twitch_source.segment_format(segments)}

    def _join(self, fetched, method="auto"):
        with mock.patch.object(vod_range.ffmpeg_runner, "get_ffmpeg_exe",
                               return_value="ffmpeg"), \
                mock.patch.object(vod_range.ffmpeg_runner, "execute") as execute:
            vod_range.join_range(fetched, self.dest, {}, method=method)
        return execute.call_args[0][0]

    # 本題: fMP4 は取得分プレイリストを HLS デマルチプレクサへ渡す
    def test_fmp4_goes_through_the_playlist(self):
        command = self._join(self._fetched(["00306.mp4", "00307.mp4"]))
        self.assertIn("-i", command)
        self.assertEqual(command[command.index("-i") + 1],
                         os.path.join(self.folder, "chunked",
                                      twitch_source.DOWNLOADED_PLAYLIST_NAME))
        self.assertNotIn("concat", command)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "concat.txt")))
        # 再エンコードせず、先頭を 0 秒へ揃える (concat 側と同じ作法)
        self.assertIn("copy", command)
        self.assertIn("make_zero", command)

    # MPEG-TS は従来どおり concat デマルチプレクサ (退行がないこと)
    def test_ts_keeps_the_concat_route(self):
        command = self._join(self._fetched(["00020.ts", "00021.ts"]))
        self.assertIn("concat", command)
        self.assertTrue(os.path.exists(os.path.join(self.folder, "concat.txt")))

    # 設定で経路を固定できる (Twitch 側が変わったときの退避用)
    def test_setting_can_force_the_route(self):
        fmp4 = self._fetched(["00306.mp4", "00307.mp4"])
        self.assertIn("concat", self._join(fmp4, method="concat"))
        ts = self._fetched(["00020.ts", "00021.ts"])
        self.assertNotIn("concat", self._join(ts, method="playlist"))

    # 取得分プレイリストが無ければ失敗させる (素のセグメントを concat へ流さない)
    def test_missing_playlist_fails(self):
        fetched = self._fetched(["00306.mp4"], with_playlist=False)
        with mock.patch.object(vod_range.ffmpeg_runner, "get_ffmpeg_exe",
                               return_value="ffmpeg"), \
                mock.patch.object(vod_range.ffmpeg_runner, "execute"):
            with self.assertRaises(TwitchError):
                vod_range.join_range(fetched, self.dest, {})


# 取得後の検証 (error 20261118 §9 R3・R5)
#
# 重点: 失敗したら**断片キャッシュを引き取る**こと。
# 残すと次の試行で twitch-dl が init セグメントを置き直せず
# FileExistsError (WinError 183) になり、再試行が永久に通らない。
class DownloadRangeTest(unittest.TestCase):

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(prefix="vod_range_dl_")
        self.cache_root = self._folder.name
        self.addCleanup(self._folder.cleanup)
        self.video_id = "2930581402"
        self.segment_dir = os.path.join(self.cache_root, self.video_id, "chunked")

    # twitch-dl の代わりにキャッシュを置く (ネットワークを使わない)
    def _stub_run(self, files):
        def _fake(*_args, **_kwargs):
            os.makedirs(self.segment_dir, exist_ok=True)
            for name, text in files.items():
                with open(os.path.join(self.segment_dir, name), "w",
                          encoding="utf-8") as handle:
                    handle.write(text)
            return []

        return mock.patch.object(twitch_source, "_run", side_effect=_fake)

    def _download(self, files):
        with self._stub_run(files), \
                mock.patch.object(twitch_source, "_resolve_twitchdl",
                                  return_value=["twitch-dl"]):
            return twitch_source.download_vod_range(
                self.video_id, self.cache_root, "0:51:07", "1:44:23")

    # 本題: fMP4 のキャッシュから連結材料まで揃えて返すこと
    def test_fmp4_cache_is_accepted(self):
        result = self._download({
            "00306.mp4": "x", "00307.mp4": "x",
            "init-0.mp4": "x",
            twitch_source.PLAYLIST_NAME: _playlist(),
            twitch_source.DOWNLOADED_PLAYLIST_NAME:
                '#EXTM3U\n#EXT-X-MAP:URI="init-0.mp4"\n#EXTINF:10,\n00306.mp4\n',
        })
        self.assertEqual(result["segments"], ["00306.mp4", "00307.mp4"])
        self.assertEqual(result["format"], "fmp4")
        self.assertEqual(
            result["downloaded_playlist"],
            os.path.join(self.segment_dir, twitch_source.DOWNLOADED_PLAYLIST_NAME))

    # MPEG-TS は従来どおり (戻り値のキーが増えただけ)
    def test_ts_cache_is_accepted(self):
        result = self._download({
            "00020.ts": "x", "00021.ts": "x",
            twitch_source.PLAYLIST_NAME: _playlist(),
        })
        self.assertEqual(result["format"], "ts")
        self.assertEqual(result["segments"], ["00020.ts", "00021.ts"])

    # init セグメントが無ければ連結できないので失敗させる
    def test_missing_init_section_fails(self):
        with self.assertRaises(TwitchError):
            self._download({
                "00306.mp4": "x",
                twitch_source.PLAYLIST_NAME: _playlist(),
                twitch_source.DOWNLOADED_PLAYLIST_NAME:
                    '#EXTM3U\n#EXT-X-MAP:URI="init-0.mp4"\n#EXTINF:10,\n00306.mp4\n',
            })
        # 失敗したら残骸を引き取る (次の試行が FileExistsError で落ちないように)
        self.assertFalse(os.path.isdir(os.path.join(self.cache_root, self.video_id)))

    # セグメントとして読めないときは「何があったのか」を文言に含める
    def test_unreadable_cache_reports_the_names(self):
        with self.assertRaises(TwitchError) as caught:
            self._download({"strange.bin": "x",
                            twitch_source.PLAYLIST_NAME: _playlist()})
        self.assertIn("strange.bin", str(caught.exception))
        self.assertFalse(os.path.isdir(os.path.join(self.cache_root, self.video_id)))

    # 再試行を壊す残骸 (init・.tmp) だけを消し、セグメント本体は残すこと。
    # 本体を消すと区間ぶんを毎回落とし直すことになる (今回の報告では 3.4 GB)。
    def test_leftovers_are_cleared_before_download(self):
        os.makedirs(self.segment_dir, exist_ok=True)
        for name in ("00306.mp4", "init-0.mp4", "00307.mp4.tmp",
                     twitch_source.PLAYLIST_NAME):
            with open(os.path.join(self.segment_dir, name), "w",
                      encoding="utf-8") as handle:
                handle.write("x")
        twitch_source._clear_broken_leftovers(
            os.path.join(self.cache_root, self.video_id))
        self.assertEqual(sorted(os.listdir(self.segment_dir)), ["00306.mp4"])


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
            downloaded = os.path.join(segment_dir,
                                      twitch_source.DOWNLOADED_PLAYLIST_NAME)
            with open(downloaded, "w", encoding="utf-8") as handle:
                handle.write("#EXTM3U\n")
            return {"segment_dir": segment_dir, "segments": list(segments),
                    "playlist": playlist, "downloaded_playlist": downloaded,
                    "format": twitch_source.segment_format(segments),
                    "quality": "chunked"}

        return mock.patch.object(twitch_source, "download_vod_range",
                                 side_effect=_fake)

    # 連結は ffmpeg の呼び出し手前まで通す (経路の選択まで本物を使う)
    def _run(self, start=225.0, end=255.0, duration=47.988, **kwargs):
        self.commands = []
        with self._stub_download(**kwargs), \
                mock.patch.object(vod_range.ffmpeg_runner, "get_ffmpeg_exe",
                                  return_value="ffmpeg"), \
                mock.patch.object(vod_range.ffmpeg_runner, "execute",
                                  side_effect=self.commands.append), \
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

    # 本題 (error 20261118): fMP4 の VOD でも原点が EXTINF 累積どおりに決まり、
    # 連結は取得分プレイリスト経由になること
    def test_fmp4_segments_keep_the_origin(self):
        result = self._run(segments=("00020.mp4", "00021.mp4",
                                     "00022.mp4", "00023.mp4"))
        self.assertAlmostEqual(result["origin_sec"], _ORIGIN_20, places=3)
        self.assertEqual(result["segments"], 4)
        command = self.commands[0]
        self.assertEqual(command[command.index("-i") + 1],
                         os.path.join(self.workdir, "vod_range_1", "2910070011",
                                      "chunked",
                                      twitch_source.DOWNLOADED_PLAYLIST_NAME))

    # MPEG-TS は従来どおり concat デマルチプレクサを通ること
    def test_ts_segments_use_concat(self):
        self._run()
        self.assertIn("concat", self.commands[0])


if __name__ == "__main__":
    unittest.main()
