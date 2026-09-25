# 同梱物のライセンス表記 (licenses/ と tools/collect_licenses.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve2.md §5.10):
#   ・manifest が壊れていないこと (全文の出どころが実在すること)
#   ・licenses/ が exe と同じ階層に置く前提で解決できること (§5.10.3)
#   ・再配布に表記が要る同梱物 (FFmpeg / Qt / モデル) が漏れていないこと
import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.settings.settings_window import resolve_licenses_dir

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MANIFEST = os.path.join(_ROOT, "tools", "license_manifest.json")
_LICENSES = os.path.join(_ROOT, "licenses")

# 表記が無いと再配布できない同梱物 (漏れると法務上の問題になる)
# ver5 resolve8 で osnet (torchreid) と MobileSAM は同梱しなくなった
_REQUIRED = ("FFmpeg", "PySide6", "Python", "YOLOX")


class LicenseManifestTest(unittest.TestCase):

    def setUp(self):
        with open(_MANIFEST, encoding="utf-8") as handle:
            self.manifest = json.load(handle)

    # manifest が読めて、必要な項目が揃っていること
    def test_manifest_shape(self):
        self.assertIn("python_roots", self.manifest)
        self.assertIn("components", self.manifest)
        for component in self.manifest["components"]:
            self.assertTrue(component.get("id"), "id が無い項目があります")
            self.assertTrue(component.get("text"), f"{component['id']}: text が無い")

    # text で指した全文ファイルが実在すること (リリース時に空のフォルダを作らないため)
    def test_license_texts_exist(self):
        specs = [c["text"] for c in self.manifest["components"]]
        specs += [v["text"] for v in self.manifest.get("python_overrides", {}).values()
                  if v.get("text")]
        for spec in specs:
            if spec.startswith("spdx:"):
                path = os.path.join(_ROOT, "tools", "license_texts",
                                    spec[len("spdx:"):] + ".txt")
            elif spec.startswith("file:"):
                path = os.path.join(_ROOT, spec[len("file:"):])
            else:
                path = os.path.join(_ROOT, spec)
            self.assertTrue(os.path.isfile(path), f"全文が見つかりません: {spec}")
            self.assertGreater(os.path.getsize(path), 100, f"全文が短すぎます: {spec}")

    # 同梱している FFmpeg は GPL ビルドのため、全文と入手先を必ず書くこと
    def test_ffmpeg_entry_has_source_url(self):
        ffmpeg = next(c for c in self.manifest["components"] if c["id"] == "FFmpeg")
        self.assertIn("GPL", ffmpeg["license"])
        self.assertTrue(ffmpeg.get("source_url"), "GPL のソース入手先が書かれていません")


class LicensesFolderTest(unittest.TestCase):

    def setUp(self):
        if not os.path.isdir(_LICENSES):
            self.skipTest("licenses/ が未生成です (python tools/collect_licenses.py)")

    # 一覧 (README.txt) があり、メモ帳で開ける書式 (UTF-8 BOM) であること
    def test_readme_is_bom_utf8(self):
        path = os.path.join(_LICENSES, "README.txt")
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as handle:
            head = handle.read(3)
        self.assertEqual(head, b"\xef\xbb\xbf", "README.txt が UTF-8 BOM ではありません")

    # 表記が要る同梱物が漏れていないこと
    def test_required_components_present(self):
        for name in _REQUIRED:
            self.assertTrue(
                os.path.isdir(os.path.join(_LICENSES, name)),
                f"{name} のライセンスが licenses/ にありません")

    # 各フォルダに中身のある全文が入っていること
    def test_every_component_has_text(self):
        for name in sorted(os.listdir(_LICENSES)):
            directory = os.path.join(_LICENSES, name)
            if not os.path.isdir(directory):
                continue
            files = [f for f in os.listdir(directory)
                     if os.path.getsize(os.path.join(directory, f)) > 0]
            self.assertTrue(files, f"{name} のライセンス全文が空です")

    # 設定画面の「ライセンス表記を開く」が場所を解決できること (§5.10.3)
    def test_resolve_licenses_dir(self):
        self.assertEqual(os.path.normcase(resolve_licenses_dir()),
                         os.path.normcase(_LICENSES))


if __name__ == "__main__":
    unittest.main()
