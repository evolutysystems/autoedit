# 「編集の続き」の履歴にパスが二重に並ばないことの単体テスト
# (docs/error/fromUser/resolve.md §9-8)
# 実行: python -m unittest discover -s tests
#
# 背景 (2026-10-22 の setting.json):
#   同じプロジェクトが timeline.project.recent_archive に 2 行入っていた。
#
#     C:/Users/.../vod_2905853277.archive.timeline.json     ← QFileDialog 由来
#     C:\Users\...\vod_2905853277.archive.timeline.json     ← os.path.join 由来
#
#   QFileDialog は Windows でも "/" 区切りで返すため、そのまま覚えると
#   パイプラインが組んだ "\" 区切りと別物に見え、コンボに同じ名前が 2 行並び、
#   履歴の枠 (recent_limit) も 1 つ無駄に埋まる。
#
# 重点: 覚えるときに書き方をそろえ、突き合わせは大文字小文字も無視すること。
import os
import unittest

from src.timeline import project_io
from src.timeline.builder import (
    dedupe_recent,
    recent_key,
    recent_path,
    timeline_config,
)


class RecentPathTest(unittest.TestCase):

    def test_separator_is_normalized(self):
        self.assertEqual(recent_path("C:/a/b/p.timeline.json"),
                         os.path.normpath(os.path.abspath("C:/a/b/p.timeline.json")))

    def test_key_ignores_separator_and_case(self):
        self.assertEqual(recent_key("C:/a/p.timeline.json"),
                         recent_key(r"C:\a\p.timeline.json"))
        if os.name == "nt":
            self.assertEqual(recent_key(r"C:\a\p.timeline.json"),
                             recent_key(r"c:\A\P.TIMELINE.JSON"))

    def test_empty_is_harmless(self):
        self.assertEqual(dedupe_recent(None), [])
        self.assertEqual(dedupe_recent(["", None]), [])


class DedupeRecentTest(unittest.TestCase):

    # 本題: 区切り文字だけが違う同じプロジェクトは 1 行へ畳む
    def test_folds_the_same_project(self):
        rows = dedupe_recent([
            "C:/w/vod_1.archive.timeline.json",
            r"C:\w\vod_1.archive.timeline.json",
            r"C:\w\vod_2.archive.timeline.json",
        ])
        self.assertEqual(rows, [
            os.path.normpath(r"C:\w\vod_1.archive.timeline.json"),
            os.path.normpath(r"C:\w\vod_2.archive.timeline.json"),
        ])

    # 先に出てきたものを残す (新しい順の並びを崩さない)
    def test_keeps_the_first_occurrence(self):
        rows = dedupe_recent([
            r"C:\w\b.timeline.json",
            r"C:\w\a.timeline.json",
            "C:/w/b.timeline.json",
        ])
        self.assertEqual([os.path.basename(p) for p in rows],
                         ["b.timeline.json", "a.timeline.json"])


class TimelineConfigRecentTest(unittest.TestCase):

    # 設定を読む時点で畳まれる (画面は timeline_config 経由で読む)
    def test_config_folds_duplicates(self):
        settings = {"timeline": {"project": {
            "recent": ["C:/w/a.timeline.json", r"C:\w\a.timeline.json"],
            "recent_archive": ["C:/w/v.archive.timeline.json",
                               r"C:\w\v.archive.timeline.json",
                               r"C:\w\w.archive.timeline.json"],
        }}}
        cfg = timeline_config(settings)["project"]
        self.assertEqual(len(cfg["recent"]), 1)
        self.assertEqual(len(cfg["recent_archive"]), 2)


class ForgetRecentTest(unittest.TestCase):

    # 削除・リネームの突き合わせも区切り文字をまたぐ
    def test_forget_matches_across_separators(self):
        settings = {"timeline": {"project": {
            "recent_archive": ["C:/w/v.archive.timeline.json"],
        }}}
        self.assertTrue(
            project_io.forget_recent(settings, r"C:\w\v.archive.timeline.json"))
        self.assertEqual(
            settings["timeline"]["project"]["recent_archive"], [])

    def test_rename_writes_a_normalized_path(self):
        settings = {"timeline": {"project": {
            "recent_archive": ["C:/w/v.archive.timeline.json"],
        }}}
        project_io.forget_recent(settings, "C:/w/v.archive.timeline.json",
                                 new_path="C:/w/new.archive.timeline.json")
        self.assertEqual(settings["timeline"]["project"]["recent_archive"],
                         [os.path.normpath(
                             os.path.abspath("C:/w/new.archive.timeline.json"))])


if __name__ == "__main__":
    unittest.main()
