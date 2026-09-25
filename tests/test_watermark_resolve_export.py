# Resolve 書き出しへの透かしとポイントの差し込み (ver5 resolve.md §5.5) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・残高不足でも書き出しをブロックせず、最上位レーンへ透かしクリップが載ること (R4)
#   ・透かしは字幕 (lane 1) より上のレーンへ置くこと
#   ・書き出せたときだけ commit し、失敗では cancel すること (R3 / R6)
#   ・単価の種別が入口で決まること (クリップ 25pt / アーカイブ 100pt / 確認事項 #3)
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.export import fcpxml_builder, resolve_export
from src.modules import watermark_overlay
from src.services.points import Reservation


def _spec():
    return {
        "version": "1.9",
        "event_name": "Stretheus",
        "project_name": "test",
        "fps": 60,
        "width": 1920,
        "height": 1080,
        "source": {"path": os.path.abspath(__file__), "name": "src", "duration": 30.0},
        "clips": [{"start": 0.0, "duration": 10.0, "name": "c1"}],
        "titles": [],
        "captions": [],
        "srt_entries": [],
    }


class _FakePoints:

    def __init__(self, watermark_required=False):
        self.reserved = []
        self.committed = []
        self.cancelled = []
        self._required = watermark_required

    def reserve(self, job_type, output_type, project_id=None):
        reservation = Reservation(job_type, output_type, reservation_id="r1",
                                  watermark_required=self._required)
        self.reserved.append(reservation)
        return reservation

    def commit(self, reservation):
        self.committed.append(reservation)

    def cancel(self, reservation):
        self.cancelled.append(reservation)


class ResolveClipSpecTest(unittest.TestCase):

    def setUp(self):
        self.cfg = watermark_overlay.config({})

    # 大きさはキャンバス幅に対する比率、位置は右下の余白ぶん内側 (R8)
    def test_size_and_position(self):
        overlay = watermark_overlay.resolve_clip_spec(1920, 1080, 30.0, self.cfg)

        self.assertEqual(overlay["width"], 480)
        self.assertAlmostEqual(overlay["scale"], 480 / float(overlay["source_width"]))
        center_x, center_y = overlay["center_px"]
        self.assertAlmostEqual(center_x, 1920 - 38 - overlay["width"] / 2.0)
        self.assertAlmostEqual(center_y, 1080 - 38 - overlay["height"] / 2.0)

    def test_position_follows_the_setting(self):
        cfg = watermark_overlay.config({"watermark": {"position": "top_left"}})
        overlay = watermark_overlay.resolve_clip_spec(1920, 1080, 30.0, cfg)

        center_x, center_y = overlay["center_px"]
        self.assertAlmostEqual(center_x, 38 + overlay["width"] / 2.0)
        self.assertAlmostEqual(center_y, 38 + overlay["height"] / 2.0)

    # 素材が読めなければ書き出し自体は止めずに省く (§4-3)
    def test_without_the_asset(self):
        original = watermark_overlay.asset_path
        watermark_overlay.asset_path = lambda: None
        try:
            self.assertIsNone(watermark_overlay.resolve_clip_spec(1920, 1080, 30.0, self.cfg))
            spec = _spec()
            self.assertEqual(watermark_overlay.add_resolve_clip(spec, {}), spec)
            self.assertNotIn("overlays", spec)
        finally:
            watermark_overlay.asset_path = original

    # 尺 0 (クリップなし) には足さない
    def test_without_clips(self):
        spec = dict(_spec(), clips=[])
        watermark_overlay.add_resolve_clip(spec, {})
        self.assertNotIn("overlays", spec)

    # タイムライン全長ぶんの 1 クリップになること
    def test_spans_the_whole_timeline(self):
        spec = _spec()
        spec["clips"].append({"start": 20.0, "duration": 5.0, "name": "c2"})
        watermark_overlay.add_resolve_clip(spec, {})

        self.assertEqual(len(spec["overlays"]), 1)
        self.assertEqual(spec["overlays"][0]["duration"], 15.0)
        self.assertEqual(spec["overlays"][0]["offset"], 0.0)


class FcpxmlOverlayTest(unittest.TestCase):

    def _build(self, spec):
        return ET.fromstring(fcpxml_builder.build_fcpxml(spec).split("\n", 2)[2])

    # 素材 (asset/format) とレーン付きの video 要素が出ること
    def test_overlay_becomes_a_video_element(self):
        spec = _spec()
        watermark_overlay.add_resolve_clip(spec, {})
        spec["overlays"][0]["position"] = (0.71, -0.75)
        root = self._build(spec)

        videos = root.findall(".//video")
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0].get("lane"), "2", "字幕 (lane 1) より上に置くこと")
        self.assertEqual(videos[0].get("duration"), "10s")
        self.assertEqual(videos[0].get("start"), "0s")

        transform = videos[0].find("adjust-transform")
        self.assertIsNotNone(transform)
        self.assertEqual(transform.get("position"), "0.7100 -0.7500")
        self.assertTrue(transform.get("scale").startswith("0.23"))
        self.assertEqual(videos[0].find("adjust-blend").get("amount"), "0.750")

        ref = videos[0].get("ref")
        asset = root.find(f".//asset[@id='{ref}']")
        self.assertIsNotNone(asset, "透かし素材の asset が resources にありません")
        self.assertEqual(asset.get("duration"), "0s", "静止画の asset は 0s")
        self.assertTrue(asset.find("media-rep").get("src").endswith("watermark.png"))

    # 透かしが無ければ従来と 1 文字も変わらないこと
    def test_without_overlays(self):
        self.assertEqual(fcpxml_builder.build_fcpxml(_spec()),
                         fcpxml_builder.build_fcpxml(dict(_spec(), overlays=[])))


class ExportSpecTest(unittest.TestCase):

    def _export(self, points, spec=None):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "out.fcpxml")
            written = resolve_export.export_spec(
                spec or _spec(), dest, settings={}, points=points)
            content = ""
            if written:
                with open(dest, encoding="utf-8") as f:
                    content = f.read()
            return written, content

    # 残高不足でも書き出しは通り、透かしクリップが入ること (R4)
    def test_watermark_is_added_without_blocking(self):
        points = _FakePoints(watermark_required=True)
        written, content = self._export(points)

        self.assertTrue(written)
        self.assertIn("watermark.png", content)
        self.assertIn('lane="2"', content)
        self.assertEqual(points.committed, points.reserved)

    # 残高が足りていれば透かしは入らないこと (R9)
    def test_no_watermark_when_not_required(self):
        points = _FakePoints(watermark_required=False)
        _written, content = self._export(points)

        self.assertNotIn("watermark.png", content)

    # ポイントを注入しない呼び出しは従来どおり
    def test_without_points(self):
        _written, content = self._export(None)
        self.assertNotIn("watermark.png", content)

    # 書き出せたときだけ確定すること (R3)
    def test_commits_after_writing(self):
        points = _FakePoints()
        self._export(points)

        self.assertEqual(len(points.reserved), 1)
        self.assertEqual(points.reserved[0].output_type, "resolveProject")
        self.assertEqual(points.cancelled, [])

    # 生成に失敗したら消費しないこと (R6)
    def test_cancels_on_failure(self):
        from src.exceptions import ExportError

        points = _FakePoints()
        original = fcpxml_builder.build_fcpxml
        fcpxml_builder.build_fcpxml = lambda _spec: (_ for _ in ()).throw(
            RuntimeError("テスト: 生成に失敗"))
        try:
            with self.assertRaises(ExportError):
                self._export(points)
        finally:
            fcpxml_builder.build_fcpxml = original

        self.assertEqual(points.committed, [])
        self.assertEqual(points.cancelled, points.reserved)

    # 上書きしない選択では予約すらしないこと
    def test_no_reservation_when_overwrite_declined(self):
        points = _FakePoints()
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "out.fcpxml")
            with open(dest, "w", encoding="utf-8") as f:
                f.write("既存")
            result = resolve_export.export_spec(
                _spec(), dest, settings={}, overwrite_confirm=lambda _p: False, points=points)

        self.assertIsNone(result)
        self.assertEqual(points.reserved, [])


class JobTypeTest(unittest.TestCase):

    # Timeline の出自で単価の種別が変わること (確認事項 #3)
    def test_archive_timeline_is_charged_as_archive(self):
        class _Timeline:
            def __init__(self, source):
                self.source = source

        self.assertEqual(resolve_export._job_type(_Timeline({"input_path": "a.mp4"})), "clip")
        self.assertEqual(
            resolve_export._job_type(_Timeline({"input_path": "a.mp4", "archive": {}})),
            "archive")

    # 結果画面からの書き出しは、入口で単価の種別が決まること
    def test_entry_points_choose_the_job_type(self):
        seen = []
        originals = {
            "export_spec": resolve_export.export_spec,
            "build_archive_spec": resolve_export.build_archive_spec,
            "build_clip_spec": resolve_export.build_clip_spec,
            "default_output_path": resolve_export.default_output_path,
        }
        resolve_export.export_spec = lambda *_a, **kw: seen.append(kw.get("job_type"))
        resolve_export.build_archive_spec = lambda *_a, **_k: _spec()
        resolve_export.build_clip_spec = lambda *_a, **_k: _spec()
        resolve_export.default_output_path = lambda *_a, **_k: "out.fcpxml"
        try:
            resolve_export.export_archive_result("vod.mp4", [{"index": 1}], {})
            resolve_export.export_clip_review({"settings": {}, "source_path": "a.mp4"}, [])
        finally:
            for name, original in originals.items():
                setattr(resolve_export, name, original)

        self.assertEqual(seen, ["archive", "clip"])


if __name__ == "__main__":
    unittest.main()
