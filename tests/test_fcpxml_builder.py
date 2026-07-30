# FCPXML 生成 (src/export/fcpxml_builder.py) の単体テスト (resolve20 §11-1 / resolve21 §5.1)
# 実行: python -m unittest discover -s tests
# builder は純関数のため FFmpeg/GUI を必要とせず単体で検証できる。
import unittest
import xml.etree.ElementTree as ET

from src.export import fcpxml_builder as builder


# XML 宣言と DOCTYPE を除いた本体を ElementTree へパースする
def _parse(xml_text):
    body = xml_text.split("<!DOCTYPE fcpxml>\n", 1)[1]
    return ET.fromstring(body)


class TestTimeFormat(unittest.TestCase):

    # 時刻はフレームへ量子化され、割り切れる場合は秒表記になる
    def test_format_time_quantizes_to_frames(self):
        self.assertEqual(builder.format_time(0, 60), "0s")
        self.assertEqual(builder.format_time(1.0, 60), "1s")
        self.assertEqual(builder.format_time(0.5, 60), "30/60s")
        # 最近傍フレームへ丸める (0.008s ≒ 0.5フレーム未満 → 0)
        self.assertEqual(builder.format_time(0.008, 60), "0s")
        # 負値・不正値は 0 とする
        self.assertEqual(builder.format_time(-5, 60), "0s")
        self.assertEqual(builder.format_time(None, 60), "0s")

    # 尺は最低 1 フレームを保証する (長さ 0 のクリップを作らない)
    def test_format_duration_has_minimum_one_frame(self):
        self.assertEqual(builder.format_duration(0.0, 60), "1/60s")
        self.assertEqual(builder.format_duration(2.0, 60), "2s")

    # 秒→整数フレーム、整数フレーム→有理数表記の基本変換 (resolve21 §5.3)
    def test_frame_helpers(self):
        self.assertEqual(builder.to_frames(2.5, 60), 150)
        self.assertEqual(builder.to_frames(None, 60), 0)
        self.assertEqual(builder.frames_to_text(150, 60), "150/60s")
        self.assertEqual(builder.frames_to_text(120, 60), "2s")
        self.assertEqual(builder.frames_to_text(0, 60), "0s")


class TestColorConversion(unittest.TestCase):

    # #RRGGBB → RGBA(0-1 実数4値)
    def test_hex_to_rgba(self):
        self.assertEqual(builder.hex_to_rgba("#FFFFFF"), "1.000000 1.000000 1.000000 1.000000")
        self.assertEqual(builder.hex_to_rgba("#000000", 0.5), "0.000000 0.000000 0.000000 0.500000")

    # 不正値は白へフォールバックする (生成を止めない)
    def test_hex_to_rgba_invalid(self):
        self.assertEqual(builder.hex_to_rgba("bad"), "1.000000 1.000000 1.000000 1.000000")


class TestBuildFcpxml(unittest.TestCase):

    def setUp(self):
        self.spec = {
            "version": "1.9",
            "event_name": "Stretheus",
            "project_name": "clip_sample",
            "fps": 60, "width": 1920, "height": 1080,
            "source": {"path": "/tmp/sample.mp4", "name": "sample", "duration": 600.0},
            "clips": [
                {"start": 2.0, "duration": 5.0, "name": "cut1"},
                {"start": 10.0, "duration": 4.0, "name": "cut2"},
            ],
            "titles": [{
                "offset": 0.5, "duration": 1.5, "text": "こんにちは\n世界",
                "font": "Yu Gothic UI", "font_size": 130, "color": "#ED0002",
                "stroke_color": "#000000", "stroke_alpha": 1.0, "stroke_width": 3,
                "bold": True, "italic": False, "underline": False,
                "align": "center", "position": (0.0, -0.75), "name": "字幕1",
            }],
            "title_effect_uid": "",
        }

    # keep 区間が spine のクリップ列になり、offset が累積和で並ぶ (§5.2)
    def test_clips_are_concatenated_by_cumulative_offset(self):
        root = _parse(builder.build_fcpxml(self.spec))
        clips = root.findall("./library/event/project/sequence/spine/asset-clip")
        self.assertEqual([c.get("offset") for c in clips], ["0s", "5s"])
        self.assertEqual([c.get("start") for c in clips], ["2s", "10s"])
        self.assertEqual([c.get("duration") for c in clips], ["5s", "4s"])

    # シーケンス尺はクリップ尺の合計になる
    def test_sequence_duration_is_total(self):
        root = _parse(builder.build_fcpxml(self.spec))
        sequence = root.find("./library/event/project/sequence")
        self.assertEqual(sequence.get("duration"), "9s")

    # タイムライン位置は量子化済み尺の累積で決まり、丸め誤差が蓄積しない (resolve21 §5.3)
    # 2.009s → 121 フレーム。旧実装 (float 累積) では 2 個目の offset が 241 フレームに
    # なり 1 フレーム重なるが、新実装では 121 フレームちょうどに並ぶ。
    def test_cumulative_offsets_use_quantized_durations(self):
        self.spec["titles"] = []
        self.spec["clips"] = [
            {"start": 0.0, "duration": 2.009, "name": "cut1"},
            {"start": 10.0, "duration": 2.009, "name": "cut2"},
        ]
        root = _parse(builder.build_fcpxml(self.spec))
        clips = root.findall("./library/event/project/sequence/spine/asset-clip")
        self.assertEqual([c.get("offset") for c in clips], ["0s", "121/60s"])
        sequence = root.find("./library/event/project/sequence")
        self.assertEqual(sequence.get("duration"), "242/60s")

    # 字幕 title は担当クリップの子要素になり、offset は親ローカル時間で表される (resolve21 §5.2)
    # タイムライン 0.5s はクリップ1 (ソース in 点 2.0s) 上 → 2.0 + 0.5 = 2.5s = 150/60s。
    def test_title_is_anchored_to_parent_clip_local_time(self):
        root = _parse(builder.build_fcpxml(self.spec))
        clips = root.findall("./library/event/project/sequence/spine/asset-clip")
        title = clips[0].find("./title")
        self.assertIsNotNone(title)
        self.assertEqual(title.get("lane"), "1")
        self.assertEqual(title.get("offset"), "150/60s")
        self.assertEqual(title.get("duration"), "90/60s")
        # spine 直下には置かれない (旧実装の仕様外配置を廃止)
        self.assertIsNone(root.find("./library/event/project/sequence/spine/title"))

    # クリップ境界を跨ぐ字幕は分割され、それぞれの親ローカル時間に配置される (resolve21 §5.2)
    # タイムライン 4.0-6.0s: クリップ1 (0-5s) 側 = ソース 6.0s から 1s、
    # クリップ2 (5-9s) 側 = ソース 10.0s から 1s。
    def test_title_split_across_clip_boundary(self):
        self.spec["titles"][0].update({"offset": 4.0, "duration": 2.0})
        root = _parse(builder.build_fcpxml(self.spec))
        clips = root.findall("./library/event/project/sequence/spine/asset-clip")
        first = clips[0].find("./title")
        second = clips[1].find("./title")
        self.assertEqual((first.get("offset"), first.get("duration")), ("6s", "1s"))
        self.assertEqual((second.get("offset"), second.get("duration")), ("10s", "1s"))
        # 分割断片には枝番が付く
        self.assertEqual((first.get("name"), second.get("name")), ("字幕1_1", "字幕1_2"))

    # タイムライン終端を超える字幕は末尾へクランプされる (resolve21 §7)
    def test_title_beyond_timeline_is_clamped(self):
        self.spec["titles"][0].update({"offset": 20.0, "duration": 1.0})
        root = _parse(builder.build_fcpxml(self.spec))
        clips = root.findall("./library/event/project/sequence/spine/asset-clip")
        title = clips[1].find("./title")
        self.assertIsNotNone(title)
        # タイムライン 8-9s = クリップ2 (offset 5s, ソース 10s) 上のソース 13s から 1s
        self.assertEqual((title.get("offset"), title.get("duration")), ("13s", "1s"))

    # スタイル (フォント/サイズ/太字/整列/縁取り) が移送される (§5.5)
    def test_title_carries_style(self):
        root = _parse(builder.build_fcpxml(self.spec))
        style = root.find(
            "./library/event/project/sequence/spine/asset-clip/title/text-style-def/text-style")
        self.assertEqual(style.get("font"), "Yu Gothic UI")
        self.assertEqual(style.get("fontSize"), "130")
        self.assertEqual(style.get("bold"), "1")
        self.assertEqual(style.get("alignment"), "center")
        self.assertEqual(style.get("strokeWidth"), "3")

    # 本文は改行を保持し、整形インデントが混入しない
    def test_title_text_has_no_indent_noise(self):
        root = _parse(builder.build_fcpxml(self.spec))
        styled = root.find(
            "./library/event/project/sequence/spine/asset-clip/title/text/text-style")
        self.assertEqual(styled.text, "こんにちは\n世界")

    # 位置は param(Position) として付与される (近似・§5.5)
    def test_title_position_param(self):
        root = _parse(builder.build_fcpxml(self.spec))
        param = root.find("./library/event/project/sequence/spine/asset-clip/title/param")
        self.assertEqual(param.get("name"), "Position")
        self.assertEqual(param.get("value"), "0.0000 -0.7500")

    # 字幕 caption は担当クリップの子要素になり role で字幕トラックへ分類される (resolve21 §5.4)
    def test_caption_is_anchored_with_role(self):
        self.spec["titles"] = []
        self.spec["captions"] = [{
            "offset": 0.5, "duration": 1.5, "text": "こんにちは",
            "font": "Yu Gothic UI", "font_size": 48, "color": "#FFFFFF",
            "bold": True, "italic": False, "underline": False,
            "placement": "bottom", "name": "字幕1",
        }]
        self.spec["caption_role"] = "iTT?captionFormat=ITT.ja"
        root = _parse(builder.build_fcpxml(self.spec))
        caption = root.find("./library/event/project/sequence/spine/asset-clip/caption")
        self.assertIsNotNone(caption)
        self.assertEqual(caption.get("role"), "iTT?captionFormat=ITT.ja")
        # lane は付けない (role でトラック分類される)
        self.assertIsNone(caption.get("lane"))
        # 親ローカル時間 (2.0 + 0.5 = 2.5s)
        self.assertEqual(caption.get("offset"), "150/60s")
        self.assertEqual(caption.get("duration"), "90/60s")
        text = caption.find("./text")
        self.assertEqual(text.get("placement"), "bottom")
        style = caption.find("./text-style-def/text-style")
        self.assertEqual(style.get("font"), "Yu Gothic UI")
        self.assertEqual(style.get("fontSize"), "48")
        self.assertEqual(style.get("bold"), "1")

    # caption と title が併存しても text-style-def の id は文書全体で一意になる
    def test_style_ids_are_unique_across_captions_and_titles(self):
        self.spec["captions"] = [{
            "offset": 0.5, "duration": 1.0, "text": "caption 側",
            "font": "Yu Gothic UI", "font_size": 48, "color": "#FFFFFF",
            "bold": False, "italic": False, "underline": False,
            "placement": "bottom", "name": "字幕c",
        }]
        root = _parse(builder.build_fcpxml(self.spec))
        ids = [d.get("id") for d in root.iter("text-style-def")]
        self.assertEqual(len(ids), len(set(ids)))

    # 字幕が無い場合はタイトル effect を生成しない
    def test_no_effect_without_titles(self):
        self.spec["titles"] = []
        root = _parse(builder.build_fcpxml(self.spec))
        self.assertIsNone(root.find("./resources/effect"))

    # caption のみの場合もタイトル effect は不要 (caption は effect を参照しない)
    def test_no_effect_with_captions_only(self):
        self.spec["titles"] = []
        self.spec["captions"] = [{
            "offset": 0.5, "duration": 1.0, "text": "字幕", "font": "Yu Gothic UI",
            "font_size": 48, "color": "#FFFFFF", "bold": False, "italic": False,
            "underline": False, "placement": "bottom", "name": "字幕1",
        }]
        root = _parse(builder.build_fcpxml(self.spec))
        self.assertIsNone(root.find("./resources/effect"))

    # ソースは media-rep の file:// URI で参照する
    def test_source_media_rep(self):
        root = _parse(builder.build_fcpxml(self.spec))
        media = root.find("./resources/asset/media-rep")
        self.assertTrue(media.get("src").startswith("file://"))

    # 空 spec でも例外を出さず最小の XML を返す (想定外データで落ちない)
    def test_empty_spec(self):
        root = _parse(builder.build_fcpxml({}))
        self.assertEqual(root.tag, "fcpxml")


if __name__ == "__main__":
    unittest.main()
