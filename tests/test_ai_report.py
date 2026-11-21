# レビュー PDF (読み物) の単体テスト (docs/AiEditor/resolve1.md §12.6 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・**レビュー未実施でも出る** (構成 + シーン評価 + 適用できなかった指示だけになる)
#   ・PDF にできない環境では HTML へ落ちる (内容を失わない)
#   ・**新規ライブラリを import しない** (PDF は PySide6 の QTextDocument + QPdfWriter)
#   ・手元に無い効果音は「プリセット名ではなく説明」で載る
#   ・本文は HTML エスケープする (テロップ本文がそのまま解釈されない)
# Qt の実描画は行わない (PDF 化の成否は環境依存のため、落ちる経路だけを見る)。
import os
import tempfile
import unittest
from unittest import mock

from src.ai import plan_schema as ps
from src.ai import review_report
from src.ai import scene_finder as sf


def _plan():
    plan = ps.new_plan("youtube_shorts", {"kind": "vod", "video_id": "123"},
                       duration_sec=3600.0, model="claude-opus-5-5")
    plan["scenes"] = [{"id": "sc1", "source_start": 100.0, "source_end": 130.0,
                       "role": "hook", "order": 1, "reason": "意外な一言"}]

    return plan


def _scene():
    scene = sf.RankedScene(1, 100.0, 130.0,
                           scores={key: 70.0 for key in ps.SCORE_KEYS},
                           summary="配信者が慌てる")
    scene.rank_score = 71.5

    return scene


def _issue(op="trim", preset="", detail="指摘の内容"):
    return {"id": "i1", "role": "critic",
            "at": {"start": 105.0, "end": 110.0, "coord": "source"},
            "scene_id": "sc1", "kind": "pacing", "severity": "major",
            "detail": detail, "reason": "理由",
            "fix": {"op": op, "target": "sc1", "source_start": 106.0,
                    "source_end": None, "text": "", "animation": "",
                    "preset": preset, "order": []},
            "expected": "効果", "priority": 1}


class BuildTest(unittest.TestCase):

    # レビューを回していなくても出る (§12.6)。
    def test_without_review(self):
        content = review_report.build_html(_plan(), scenes=[_scene()], settings={})

        self.assertIn("構成", content)
        self.assertIn("シーン評価", content)
        self.assertNotIn("<h2>指摘</h2>", content)

    def test_with_review(self):
        content = review_report.build_html(_plan(), scenes=[_scene()],
                                           issues=[_issue()], settings={})

        self.assertIn("<h2>指摘</h2>", content)
        self.assertIn("区間を詰める", content)

    # 手元に無い音は「プリセット名ではなく説明」で載る (回答 Q9 / §12.6)。
    def test_sound_suggestion_without_a_preset(self):
        content = review_report.build_html(
            _plan(), issues=[_issue(op="add_sound", detail="気付く瞬間に短い音")],
            settings={})

        self.assertIn("効果音の提案", content)
        self.assertIn("(登録なし)", content)
        self.assertIn("気付く瞬間に短い音", content)

    def test_subtitle_suggestions(self):
        content = review_report.build_html(
            _plan(), issues=[_issue(op="set_animation")], settings={})

        self.assertIn("テロップの提案", content)

    # 適用できなかった指示は件数ではなく理由まで載せる (§10 / §12.6)。
    def test_dropped_notes(self):
        notes = [{"check": "V14", "where": "sounds[0]", "action": "drop",
                  "detail": "unknown"},
                 {"check": "V7", "where": "scenes[0]", "action": "cut",
                  "detail": "3600.0"}]

        content = review_report.build_html(_plan(), notes=notes, settings={})

        self.assertIn("適用できなかった AI の指示", content)
        self.assertIn("V14", content)
        self.assertNotIn("V7", content, "切っただけの記録は落ちた扱いにしない")

    def test_machine_checks(self):
        checks = [{"kind": "subtitle", "severity": "minor", "detail": "短い",
                   "scene_id": "sc1", "at": 101.0}]

        content = review_report.build_html(_plan(), checks=checks, settings={})

        self.assertIn("技術的な注意", content)

    def test_images_are_embedded(self):
        content = review_report.build_html(
            _plan(), images=[{"data_base64": "AAAA", "media_type": "image/jpeg",
                              "caption": "105.0s"}], settings={})

        self.assertIn("data:image/jpeg;base64,AAAA", content)

    # 本文はエスケープする (テロップ本文がそのまま解釈されない)。
    def test_text_is_escaped(self):
        content = review_report.build_html(
            _plan(), issues=[_issue(detail="<script>alert(1)</script>")],
            settings={})

        self.assertNotIn("<script>", content)
        self.assertIn("&lt;script&gt;", content)

    def test_model_is_on_the_cover(self):
        content = review_report.build_html(_plan(), settings={})

        self.assertIn("claude-opus-5-5", content)

    def test_without_a_model_says_so(self):
        plan = _plan()
        plan["generated"]["model"] = ""

        content = review_report.build_html(plan, settings={})

        self.assertIn("AI 無しで構成", content)


class PathTest(unittest.TestCase):

    def test_output_directory_is_used(self):
        path = review_report.report_path_for(
            {"general": {"output_directory": os.path.join("out", "videos")}},
            "video.ai_youtube_long.timeline.json")

        self.assertTrue(path.endswith("_review.pdf"))
        self.assertIn("videos", path)

    def test_suffix_can_be_changed(self):
        path = review_report.report_path_for(
            {"general": {"output_directory": "out"},
             "ai": {"report": {"name_suffix": "_ai"}}},
            "video.timeline.json", extension="html")

        self.assertTrue(path.endswith("video_ai.html"))

    def test_write_html(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "a", "report.html")

            review_report.write_html("<html></html>", dest)

            self.assertTrue(os.path.exists(dest))


class NoNewLibraryTest(unittest.TestCase):

    # PDF ライブラリを増やさない (§12.6)。本文側は標準ライブラリだけで作る。
    def test_report_builder_has_no_third_party_imports(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "ai", "review_report.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()

        imports = [line for line in source.splitlines()
                   if line.startswith("import ") or line.startswith("from ")]
        for line in imports:
            for forbidden in ("reportlab", "weasyprint", "pdfkit", "fpdf",
                              "PySide6"):
                self.assertNotIn(forbidden, line)
        self.assertIn("import html", imports)

    def test_pdf_writer_uses_pyside_only(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "gui", "ai_report.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("QPdfWriter", source)
        for forbidden in ("reportlab", "weasyprint", "pdfkit", "fpdf"):
            self.assertNotIn(forbidden, source)


class PdfFallbackTest(unittest.TestCase):

    # PDF にできない環境では HTML を残す (§12.6)。
    def test_failed_pdf_falls_back_to_html(self):
        from src.gui import ai_report

        with tempfile.TemporaryDirectory() as folder:
            settings = {"general": {"output_directory": folder},
                        "ai": {"report": {"enabled": True, "format": "pdf"}}}
            with mock.patch.object(ai_report, "write_pdf", lambda *a, **k: False):
                written = ai_report.write(settings, "<html></html>",
                                          "video.timeline.json")

            self.assertEqual(1, len(written))
            self.assertTrue(written[0].endswith(".html"))
            self.assertTrue(os.path.exists(written[0]))

    def test_disabled_writes_nothing(self):
        from src.gui import ai_report

        written = ai_report.write({"ai": {"report": {"enabled": False}}},
                                  "<html></html>", "video.timeline.json")

        self.assertEqual([], written)

    def test_broken_pdf_writer_does_not_raise(self):
        from src.gui import ai_report

        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "report.pdf")
            with mock.patch.object(ai_report, "QPdfWriter",
                                   side_effect=RuntimeError("boom")):
                self.assertFalse(ai_report.write_pdf("<html></html>", dest))


if __name__ == "__main__":
    unittest.main()
