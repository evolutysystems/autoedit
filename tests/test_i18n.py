# 多言語化 (src/i18n) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver8/resolve.md §10):
#   ・日本語では tr() が原文をそのまま返すこと (従来の挙動を変えない)
#   ・英語では対訳表を引き、無いキーは原文へ落ちること
#   ・差し込み ({name}) が原文と対訳で食い違わないこと
#   ・言語コードの正規化と、切替の通知が働くこと
#   ・src 内の tr("…") の原文が対訳表にそろっていること (訳し漏れの検出)
import ast
import os
import pathlib
import re
import unittest

from src import i18n
from src.i18n.catalog_en import CATALOG

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
_SKIP_DIRS = {"build", "dist", "__pycache__", "i18n"}
_JP = re.compile(r"[぀-ヿ一-鿿]")
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[^}]*)?\}")


def _python_files():
    for path in sorted(_SRC.rglob("*.py")):
        if _SKIP_DIRS & set(path.parts):
            continue
        yield path


# src 内の tr("…") に直接書かれている原文を集める。
# 定数や表を経由するものはここでは拾えないため、対訳表の網羅検査は
# 「直接書かれているもの」に限る (間接ぶんは build 時の生成で担保する)。
def _direct_literals():
    found = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "tr":
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and _JP.search(arg.value):
                found.setdefault(arg.value, f"{path.name}:{node.lineno}")
    return found


class LanguageTest(unittest.TestCase):

    def setUp(self):
        self._original = i18n.language()

    def tearDown(self):
        i18n.set_language(self._original)

    # 日本語では原文をそのまま返す (対訳表を一切見ない)
    def test_japanese_returns_the_source_text(self):
        i18n.set_language(i18n.LANG_JA)
        self.assertEqual(i18n.tr("設定"), "設定")
        self.assertEqual(i18n.tr("対訳表に無い架空の文言"), "対訳表に無い架空の文言")

    # 英語では対訳表を引く
    def test_english_uses_the_catalog(self):
        i18n.set_language(i18n.LANG_EN)
        self.assertEqual(i18n.tr("設定"), CATALOG["設定"])
        self.assertNotEqual(i18n.tr("設定"), "設定")

    # 対訳表に無いキーは原文のまま返し、画面を壊さない
    def test_missing_key_falls_back_to_the_source_text(self):
        i18n.set_language(i18n.LANG_EN)
        self.assertEqual(i18n.tr("対訳表に無い架空の文言"), "対訳表に無い架空の文言")
        self.assertIn("対訳表に無い架空の文言", i18n.missing_keys())

    # 差し込みは名前付きで行う。text という名前も使える (第 1 引数は位置専用)
    def test_format_uses_named_placeholders(self):
        i18n.set_language(i18n.LANG_JA)
        self.assertEqual(i18n.tr("完了: {path}", path="a.mp4"), "完了: a.mp4")
        self.assertEqual(i18n.tr("{text} / 次回リセット {reset}", text="残り 1 pt",
                                 reset="10/11"),
                         "残り 1 pt / 次回リセット 10/11")

    # 差し込みに失敗しても例外を投げない (画面を止めない)
    def test_format_failure_does_not_raise(self):
        i18n.set_language(i18n.LANG_JA)
        self.assertEqual(i18n.tr("{missing} です", other="x"), "{missing} です")

    # 言語コードの正規化 (未対応・綴り違いは日本語へ倒す)
    def test_normalize(self):
        self.assertEqual(i18n.normalize("en"), i18n.LANG_EN)
        self.assertEqual(i18n.normalize("EN-US"), i18n.LANG_EN)
        self.assertEqual(i18n.normalize("en_GB"), i18n.LANG_EN)
        self.assertEqual(i18n.normalize("fr"), i18n.LANG_JA)
        self.assertEqual(i18n.normalize(None), i18n.LANG_JA)
        self.assertEqual(i18n.normalize(""), i18n.LANG_JA)

    # setting.json の ui.language を反映する
    def test_apply_reads_the_setting(self):
        self.assertEqual(i18n.apply({"ui": {"language": "en"}}), i18n.LANG_EN)
        self.assertEqual(i18n.apply({"ui": {"language": "ja"}}), i18n.LANG_JA)
        self.assertEqual(i18n.apply({}), i18n.LANG_JA)

    # 切替の通知。同じ言語への切替では呼ばれない
    def test_subscribe(self):
        calls = []
        i18n.set_language(i18n.LANG_JA)
        i18n.subscribe(lambda: calls.append(i18n.language()))
        try:
            self.assertTrue(i18n.set_language(i18n.LANG_EN))
            self.assertFalse(i18n.set_language(i18n.LANG_EN))
            self.assertEqual(calls, [i18n.LANG_EN])
        finally:
            i18n._listeners.clear()

    # 購読を解除したら呼ばれない
    def test_unsubscribe(self):
        calls = []

        def listener():
            calls.append(1)

        i18n.set_language(i18n.LANG_JA)
        i18n.subscribe(listener)
        i18n.unsubscribe(listener)
        i18n.set_language(i18n.LANG_EN)
        self.assertEqual(calls, [])


class CatalogTest(unittest.TestCase):

    # 訳が空のまま残っていないこと
    def test_no_empty_translations(self):
        empty = [key for key, value in CATALOG.items() if not str(value).strip()]
        self.assertEqual(empty, [], f"訳が空の文言があります: {empty[:5]}")

    # 日本語がそのまま残っている (訳し忘れの) 項目が無いこと
    def test_translations_are_not_japanese(self):
        untranslated = [key for key, value in CATALOG.items() if _JP.search(str(value))]
        self.assertEqual(untranslated, [],
                         f"英訳に日本語が残っています: {untranslated[:5]}")

    # 差し込みの名前が原文と対訳で一致すること (食い違うと英語にならない)
    def test_placeholders_match(self):
        mismatched = []
        for key, value in CATALOG.items():
            if set(_PLACEHOLDER.findall(key)) != set(_PLACEHOLDER.findall(str(value))):
                mismatched.append(key)
        self.assertEqual(mismatched, [],
                         f"差し込みの名前が原文と違います: {mismatched[:5]}")

    # src 内の tr("…") の原文が対訳表にそろっていること。
    # 新しい文言を足したら、この検査が対訳表への追記を促す。
    def test_catalog_covers_the_source(self):
        missing = {key: where for key, where in _direct_literals().items()
                   if key not in CATALOG}
        self.assertEqual(missing, {},
                         "対訳表に無い文言があります: "
                         + ", ".join(f"{w} {k!r}" for k, w in list(missing.items())[:5]))

    # 取り残しの検査は置いていない。原文は複数行の暗黙連結や \t を含み、
    # ソースの文字列と 1 対 1 で突き合わせられないため、誤検知のほうが多くなる。


if __name__ == "__main__":
    unittest.main()
