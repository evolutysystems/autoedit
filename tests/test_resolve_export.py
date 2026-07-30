# Resolve 出力のタイムライン/スタイル組み立て (src/export/resolve_export.py) の単体テスト
# 実行: python -m unittest discover -s tests
# FFmpeg 実行を避けるため出力プロファイルは明示指定し、総尺取得は失敗時フォールバックに任せる。
# resolve21: 字幕は既定で caption (字幕トラック) へ変換され、SRT サイドカーが併記される。
import os
import tempfile
import unittest

from src.export import resolve_export

# 横 1920x1080 の出力プロファイル (縦判定のための ffprobe を回避する)
_PROFILE = {"is_portrait": False, "orientation": "landscape", "width": 1920, "height": 1080}


# テスト用の最小設定 (既定と同じ構造。ffprobe は解決できない値にして総尺取得を失敗させる)
def _settings(subtitle_mode=None, **overrides):
    settings = {
        "general": {"output_directory": ""},
        "ffmpeg": {"output_fps": 60, "output_width": 1920, "output_height": 1080,
                   "ffprobe_executable": "no_such_ffprobe_for_test"},
        "subtitle": {"font_family": "Yu Gothic UI", "font_size": 48,
                     "own_subtitle_color": "#FFFFFF", "sub_subtitle_color": "#FFF100",
                     "comment_subtitle_color": "#00FF00", "comment_label": "コメント：",
                     "outline_color": "&H00000000", "outline_width": 3,
                     "alignment": 2, "margin_l": 40, "margin_r": 40, "margin_v": 60},
        "vertical": {"enabled": True},
        "archive": {"output": {"clip_prefix": "archive"},
                    "intro_card": {"enabled": True, "buffer_sec": 1.5,
                                   "title_font_size": 112, "title_color": "#FFFFFF",
                                   "tag_font_size": 40, "tag_color": "#FFFFFF",
                                   "tag_margin_l": 40, "tag_margin_v": 30}},
        "export": {"resolve": {"enabled": True, "clip_prefix": "clip"}},
    }
    if subtitle_mode is not None:
        settings["export"]["resolve"]["subtitle"] = {"mode": subtitle_mode}
    settings.update(overrides)
    return settings


class _SourceFileCase(unittest.TestCase):

    # 実在するダミーのソースファイルを用意する (存在検証を通すためだけの空ファイル)
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="resolve_export_test_")
        self.source = os.path.join(self._dir, "sample.mp4")
        with open(self.source, "wb") as f:
            f.write(b"")

    def tearDown(self):
        for name in os.listdir(self._dir):
            try:
                os.remove(os.path.join(self._dir, name))
            except OSError:
                pass
        try:
            os.rmdir(self._dir)
        except OSError:
            pass


class TestClipSpec(_SourceFileCase):

    # 残す区間がクリップ列になり、字幕は既定で caption (タイムライン時間) になる (resolve21 §5.4)
    def test_keep_segments_and_captions(self):
        items = [
            {"start": 0.5, "end": 2.0, "text": "あいさつ", "use": True, "role": "streamer"},
            {"start": 3.0, "end": 4.0, "text": "使わない", "use": False, "role": "streamer"},
        ]
        spec = resolve_export.build_clip_spec(
            self.source, [(10.0, 20.0), (30.0, 35.0)], items, _settings(), profile=_PROFILE)
        self.assertEqual([(c["start"], c["duration"]) for c in spec["clips"]],
                         [(10.0, 10.0), (30.0, 5.0)])
        # use=False は出力しない。既定 mode=caption のため titles は空 (テーマなし)
        self.assertEqual(len(spec["captions"]), 1)
        self.assertEqual(spec["titles"], [])
        self.assertEqual(spec["captions"][0]["offset"], 0.5)
        self.assertEqual(spec["captions"][0]["duration"], 1.5)
        self.assertEqual(spec["caption_role"], "iTT?captionFormat=ITT.ja")
        self.assertEqual(spec["fps"], 60)
        self.assertEqual((spec["width"], spec["height"]), (1920, 1080))

    # mode="title" では従来どおり title (Text+) を生成し caption は作らない (resolve21 §5.4)
    def test_title_mode_generates_titles(self):
        items = [{"start": 0.5, "end": 2.0, "text": "あいさつ", "use": True, "role": "streamer"}]
        spec = resolve_export.build_clip_spec(
            self.source, [(10.0, 20.0)], items, _settings(subtitle_mode="title"),
            profile=_PROFILE)
        self.assertEqual(len(spec["titles"]), 1)
        self.assertEqual(spec["captions"], [])
        self.assertEqual(spec["titles"][0]["offset"], 0.5)

    # mode="both" では caption と title の両方を生成する
    def test_both_mode_generates_captions_and_titles(self):
        items = [{"start": 0.5, "end": 2.0, "text": "あいさつ", "use": True, "role": "streamer"}]
        spec = resolve_export.build_clip_spec(
            self.source, [(10.0, 20.0)], items, _settings(subtitle_mode="both"),
            profile=_PROFILE)
        self.assertEqual(len(spec["captions"]), 1)
        self.assertEqual(len(spec["titles"]), 1)

    # 不正な mode は caption へフォールバックする (§6)
    def test_invalid_mode_falls_back_to_caption(self):
        items = [{"start": 0.5, "end": 2.0, "text": "あいさつ", "use": True, "role": "streamer"}]
        spec = resolve_export.build_clip_spec(
            self.source, [(10.0, 20.0)], items, _settings(subtitle_mode="bogus"),
            profile=_PROFILE)
        self.assertEqual(len(spec["captions"]), 1)
        self.assertEqual(spec["titles"], [])

    # SRT サイドカー用 entries は mode に関わらず収集される (resolve21 §5.6)
    def test_srt_entries_are_collected(self):
        items = [
            {"start": 0.5, "end": 2.0, "text": "1行目\\N2行目", "use": True, "role": "comment"},
            {"start": 3.0, "end": 4.0, "text": "不使用", "use": False, "role": "streamer"},
        ]
        spec = resolve_export.build_clip_spec(
            self.source, [(10.0, 20.0)], items, _settings(subtitle_mode="title"),
            profile=_PROFILE)
        self.assertEqual(len(spec["srt_entries"]), 1)
        entry = spec["srt_entries"][0]
        self.assertEqual((entry["start"], entry["end"]), (0.5, 2.0))
        # コメントラベルと実改行が反映される
        self.assertEqual(entry["text"], "コメント：1行目\n2行目")

    # 編集点が無い (無音カット OFF/未実行) 場合は全長 1 クリップにする (§5.8 / §7)
    def test_without_keep_segments_falls_back_to_single_clip(self):
        items = [{"start": 0.0, "end": 5.0, "text": "本文", "use": True, "role": "streamer"}]
        spec = resolve_export.build_clip_spec(
            self.source, None, items, _settings(), profile=_PROFILE)
        self.assertEqual(len(spec["clips"]), 1)
        self.assertEqual(spec["clips"][0]["start"], 0.0)
        # 総尺が取得できない環境では字幕終端で代用する
        self.assertEqual(spec["clips"][0]["duration"], 5.0)

    # 個別フォント/サイズは item 優先、無ければ設定既定を使う (§5.5)
    def test_item_font_overrides(self):
        items = [
            {"start": 0.0, "end": 1.0, "text": "既定", "use": True, "role": "streamer"},
            {"start": 1.0, "end": 2.0, "text": "上書き", "use": True, "role": "streamer",
             "font": "Meiryo", "font_size": 90},
        ]
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 10.0)], items, _settings(), profile=_PROFILE)
        self.assertEqual((spec["captions"][0]["font"], spec["captions"][0]["font_size"]),
                         ("Yu Gothic UI", 48))
        self.assertEqual((spec["captions"][1]["font"], spec["captions"][1]["font_size"]),
                         ("Meiryo", 90))

    # 役割で色が変わり、コメント役割は本文先頭にラベルが付く (§10-8)
    def test_role_color_and_comment_label(self):
        items = [
            {"start": 0.0, "end": 1.0, "text": "配信者", "use": True, "role": "streamer"},
            {"start": 1.0, "end": 2.0, "text": "サブ", "use": True, "role": "sub"},
            {"start": 2.0, "end": 3.0, "text": "コメ", "use": True, "role": "comment"},
        ]
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 10.0)], items, _settings(), profile=_PROFILE)
        self.assertEqual(spec["captions"][0]["color"], "#FFFFFF")
        self.assertEqual(spec["captions"][1]["color"], "#FFF100")
        self.assertEqual(spec["captions"][2]["color"], "#00FF00")
        self.assertEqual(spec["captions"][2]["text"], "コメント：コメ")

    # ASS の改行 \N は実改行へ戻す
    def test_newline_conversion(self):
        items = [{"start": 0.0, "end": 1.0, "text": "1行目\\N2行目", "use": True,
                  "role": "streamer"}]
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 5.0)], items, _settings(), profile=_PROFILE)
        self.assertEqual(spec["captions"][0]["text"], "1行目\n2行目")

    # caption の placement は配置から上下を決める (下段系→bottom / 上段系→top / §5.4)
    def test_caption_placement(self):
        items = [{"start": 0.0, "end": 1.0, "text": "位置", "use": True, "role": "streamer"}]
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 5.0)], items, _settings(), profile=_PROFILE)
        self.assertEqual(spec["captions"][0]["placement"], "bottom")
        settings = _settings()
        settings["subtitle"]["alignment"] = 8  # 上段中央
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 5.0)], items, settings, profile=_PROFILE)
        self.assertEqual(spec["captions"][0]["placement"], "top")

    # 配置(下段中央=2)＋余白 60px → 正規化座標の下寄せ中央へ近似変換する (title mode / §5.5)
    def test_position_mapping_bottom_center(self):
        items = [{"start": 0.0, "end": 1.0, "text": "位置", "use": True, "role": "streamer"}]
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 5.0)], items, _settings(subtitle_mode="title"),
            profile=_PROFILE)
        x, y = spec["titles"][0]["position"]
        self.assertAlmostEqual(x, 0.0)
        # y = (H/2 - (H - margin_v)) / (H/2) = (540 - 1020)/540
        self.assertAlmostEqual(y, (540.0 - 1020.0) / 540.0, places=6)
        self.assertEqual(spec["titles"][0]["align"], "center")

    # 左上(7)＋余白は左上寄せの座標になる (title mode)
    def test_position_mapping_top_left(self):
        settings = _settings(subtitle_mode="title")
        settings["subtitle"]["alignment"] = 7
        items = [{"start": 0.0, "end": 1.0, "text": "位置", "use": True, "role": "streamer"}]
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 5.0)], items, settings, profile=_PROFILE)
        x, y = spec["titles"][0]["position"]
        self.assertAlmostEqual(x, (40.0 - 960.0) / 960.0, places=6)
        self.assertAlmostEqual(y, (540.0 - 60.0) / 540.0, places=6)
        self.assertEqual(spec["titles"][0]["align"], "left")

    # ソース未特定/不在は ExportError で中止する (§7)
    def test_missing_source(self):
        from src.exceptions import ExportError
        with self.assertRaises(ExportError):
            resolve_export.build_clip_spec("", None, [], _settings(), profile=_PROFILE)
        with self.assertRaises(ExportError):
            resolve_export.build_clip_spec(
                os.path.join(self._dir, "none.mp4"), None, [], _settings(), profile=_PROFILE)


class TestArchiveSpec(_SourceFileCase):

    # クリップ内の残す区間を元 VOD 相対へ直して連結し、字幕を本編開始位置へ乗せる (§5.4 案①)
    def test_clip_keep_segments_are_absolute(self):
        entries = [{
            "index": 1, "start": 100.0, "end": 130.0,
            "keep_segments": [(0.0, 10.0), (12.0, 20.0)],
            "items": [{"start": 1.0, "end": 2.0, "text": "字幕", "use": True,
                       "role": "streamer"}],
            "theme": "",
        }]
        spec = resolve_export.build_archive_spec(
            self.source, entries, _settings(), profile=_PROFILE)
        self.assertEqual([(c["start"], c["duration"]) for c in spec["clips"]],
                         [(100.0, 10.0), (112.0, 8.0)])
        self.assertEqual(spec["captions"][0]["offset"], 1.0)
        self.assertEqual(spec["srt_entries"][0]["start"], 1.0)

    # 無音カット OFF (keep_segments なし) は生のクリップ区間そのままになる (§5.8)
    def test_without_keep_segments_uses_raw_region(self):
        entries = [{"index": 1, "start": 100.0, "end": 130.0, "keep_segments": None,
                    "items": [], "theme": ""}]
        spec = resolve_export.build_archive_spec(
            self.source, entries, _settings(), profile=_PROFILE)
        self.assertEqual([(c["start"], c["duration"]) for c in spec["clips"]],
                         [(100.0, 30.0)])

    # 複数クリップは TOP 順に連結され、字幕オフセットが累積する
    def test_multiple_clips_accumulate_offset(self):
        entries = [
            {"index": 1, "start": 100.0, "end": 110.0, "keep_segments": [(0.0, 10.0)],
             "items": [{"start": 1.0, "end": 2.0, "text": "A", "use": True,
                        "role": "streamer"}], "theme": ""},
            {"index": 2, "start": 200.0, "end": 205.0, "keep_segments": [(0.0, 5.0)],
             "items": [{"start": 1.0, "end": 2.0, "text": "B", "use": True,
                        "role": "streamer"}], "theme": ""},
        ]
        spec = resolve_export.build_archive_spec(
            self.source, entries, _settings(), profile=_PROFILE)
        self.assertEqual([c["offset"] for c in spec["captions"]], [1.0, 11.0])
        self.assertEqual([e["start"] for e in spec["srt_entries"]], [1.0, 11.0])

    # テーマ入力ありはイントロ区間＋中央テーマ＋左上タグを title として含める (§5.4 / §10-7)
    # (テーマ演出は字幕ではないため mode に関わらず title のまま / resolve21 §5.4)
    def test_theme_adds_intro_and_tag(self):
        entries = [{"index": 1, "start": 100.0, "end": 110.0,
                    "keep_segments": [(0.0, 10.0)], "items": [], "theme": "神回"}]
        spec = resolve_export.build_archive_spec(
            self.source, entries, _settings(), profile=_PROFILE)
        # イントロ (100-1.5 〜 100) が先頭に入る
        self.assertEqual((spec["clips"][0]["start"], spec["clips"][0]["duration"]),
                         (98.5, 1.5))
        names = [t["name"] for t in spec["titles"]]
        self.assertEqual(names, ["テーマ1", "タグ1"])
        # 中央テーマはイントロ区間、タグは本編全長
        self.assertEqual((spec["titles"][0]["offset"], spec["titles"][0]["duration"]),
                         (0.0, 1.5))
        self.assertEqual((spec["titles"][1]["offset"], spec["titles"][1]["duration"]),
                         (1.5, 10.0))

    # include_intro_card=false ならテーマ演出を出力しない
    def test_intro_card_disabled_by_setting(self):
        settings = _settings()
        settings["export"]["resolve"]["include_intro_card"] = False
        entries = [{"index": 1, "start": 100.0, "end": 110.0,
                    "keep_segments": [(0.0, 10.0)], "items": [], "theme": "神回"}]
        spec = resolve_export.build_archive_spec(
            self.source, entries, settings, profile=_PROFILE)
        self.assertEqual(len(spec["clips"]), 1)
        self.assertEqual(spec["titles"], [])


class TestOutputPathAndWrite(_SourceFileCase):

    # ファイル名は {prefix}_{stem}.fcpxml (prefix 空なら {stem}.fcpxml) / §5.7
    def test_default_output_path(self):
        path = resolve_export.default_output_path(_settings(), self.source, "clip")
        self.assertEqual(os.path.basename(path), "clip_sample.fcpxml")
        path = resolve_export.default_output_path(_settings(), self.source, "")
        self.assertEqual(os.path.basename(path), "sample.fcpxml")

    # 書き出しは [fcpxml, srt] を返し、一時ファイルを残さない / 上書き拒否で None (§7 / §5.6)
    def test_export_spec_writes_fcpxml_and_srt_sidecar(self):
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 5.0)],
            [{"start": 0.0, "end": 1.0, "text": "本文", "use": True, "role": "streamer"}],
            _settings(), profile=_PROFILE)
        dest = os.path.join(self._dir, "out.fcpxml")
        written = resolve_export.export_spec(spec, dest, settings=_settings())
        srt_path = os.path.join(self._dir, "out.srt")
        self.assertEqual(written, [dest, srt_path])
        self.assertTrue(os.path.exists(dest))
        self.assertTrue(os.path.exists(srt_path))
        self.assertFalse(os.path.exists(dest + ".tmp"))
        self.assertFalse(os.path.exists(srt_path + ".tmp"))
        # SRT の中身 (連番・時刻・本文)
        with open(srt_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("1\n00:00:00,000 --> 00:00:01,000\n本文", content)
        # 既存ファイルがあり確認が False なら書き換えずに None
        self.assertIsNone(resolve_export.export_spec(
            spec, dest, settings=_settings(), overwrite_confirm=lambda _p: False))

    # srt_sidecar=false なら FCPXML のみ出力する (§6)
    def test_export_spec_without_sidecar(self):
        settings = _settings()
        settings["export"]["resolve"]["subtitle"] = {"srt_sidecar": False}
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 5.0)],
            [{"start": 0.0, "end": 1.0, "text": "本文", "use": True, "role": "streamer"}],
            settings, profile=_PROFILE)
        dest = os.path.join(self._dir, "out2.fcpxml")
        written = resolve_export.export_spec(spec, dest, settings=settings)
        self.assertEqual(written, [dest])
        self.assertFalse(os.path.exists(os.path.join(self._dir, "out2.srt")))

    # 字幕 0 件なら SRT サイドカーを出力しない (§7)
    def test_export_spec_without_subtitles_skips_srt(self):
        spec = resolve_export.build_clip_spec(
            self.source, [(0.0, 5.0)], [], _settings(), profile=_PROFILE)
        dest = os.path.join(self._dir, "out3.fcpxml")
        written = resolve_export.export_spec(spec, dest, settings=_settings())
        self.assertEqual(written, [dest])
        self.assertFalse(os.path.exists(os.path.join(self._dir, "out3.srt")))


class TestSrtWriter(unittest.TestCase):

    # SRT の時刻表記と組み立て (resolve21 §5.6)
    def test_build_srt(self):
        from src.export import srt_writer
        content = srt_writer.build_srt([
            {"start": 0.5, "end": 2.0, "text": "1行目\n2行目"},
            {"start": 3661.25, "end": 3662.0, "text": "後半"},
            {"start": 5.0, "end": 5.0, "text": "尺ゼロは出力しない"},
            {"start": 6.0, "end": 7.0, "text": "  "},
        ])
        blocks = content.split("\n\n")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0], "1\n00:00:00,500 --> 00:00:02,000\n1行目\n2行目")
        self.assertEqual(blocks[1], "2\n01:01:01,250 --> 01:01:02,000\n後半\n")

    def test_format_srt_time(self):
        from src.export import srt_writer
        self.assertEqual(srt_writer.format_srt_time(0), "00:00:00,000")
        self.assertEqual(srt_writer.format_srt_time(59.9994), "00:00:59,999")
        self.assertEqual(srt_writer.format_srt_time(-1), "00:00:00,000")
        self.assertEqual(srt_writer.format_srt_time(None), "00:00:00,000")


if __name__ == "__main__":
    unittest.main()
