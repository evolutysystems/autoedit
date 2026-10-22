# 作業フォルダの残骸掃除の単体テスト (docs/error/fromUser/resolve.md §7)
# 実行: python -m unittest discover -s tests
# 重点:
#   ・古い残骸だけを消し、新しいもの (走っている可能性がある) は残すこと
#   ・自分が作る名前以外には触らないこと
#   ・消せないものがあっても例外を外へ出さないこと (起動を妨げない)
import os
import tempfile
import time
import unittest
from unittest import mock

from src.utils import workspace


def _make_dir(base, name, age_hours, size=0):
    path = os.path.join(base, name)
    os.makedirs(path, exist_ok=True)
    if size:
        with open(os.path.join(path, "payload.bin"), "wb") as handle:
            handle.write(b"x" * size)
    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))
    return path


class SweepStaleTest(unittest.TestCase):

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.base = self._temp.name

    def tearDown(self):
        self._temp.cleanup()

    # 古いものは消え、新しいものは残る
    def test_removes_only_old_folders(self):
        old = _make_dir(self.base, "autoedit_old", age_hours=48, size=256)
        fresh = _make_dir(self.base, "autoedit_fresh", age_hours=1)

        result = workspace.sweep_stale(base_dir=self.base, max_age_hours=24)

        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertEqual(result["removed"], 1)
        self.assertGreaterEqual(result["freed"], 256)

    # media_recovery が作る名前も対象に含む
    def test_removes_recover_folders(self):
        recover = _make_dir(self.base, "autoedit_recover_abc", age_hours=48)
        workspace.sweep_stale(base_dir=self.base, max_age_hours=24)
        self.assertFalse(os.path.exists(recover))

    # 対象外の名前には触らない (他アプリ・利用者のフォルダを壊さない)
    def test_keeps_unrelated_folders(self):
        other = _make_dir(self.base, "something_else", age_hours=500)
        workspace.sweep_stale(base_dir=self.base, max_age_hours=24)
        self.assertTrue(os.path.exists(other))

    # 0 以下なら何もしない (設定で無効にできる)
    def test_zero_age_disables_the_sweep(self):
        old = _make_dir(self.base, "autoedit_old", age_hours=500)
        result = workspace.sweep_stale(base_dir=self.base, max_age_hours=0)
        self.assertTrue(os.path.exists(old))
        self.assertEqual(result["removed"], 0)

    # ファイルは対象にしない (フォルダだけを消す)
    def test_ignores_files(self):
        path = os.path.join(self.base, "autoedit_note.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        stamp = time.time() - 500 * 3600
        os.utime(path, (stamp, stamp))
        workspace.sweep_stale(base_dir=self.base, max_age_hours=24)
        self.assertTrue(os.path.exists(path))

    # 消せないものがあっても例外は外へ出さない
    def test_survives_undeletable_folders(self):
        _make_dir(self.base, "autoedit_locked", age_hours=48)
        with mock.patch("shutil.rmtree", side_effect=OSError("busy")):
            result = workspace.sweep_stale(base_dir=self.base, max_age_hours=24)
        self.assertEqual(result["removed"], 0)

    # 探せない場所を渡されても落ちない
    def test_missing_base_dir(self):
        result = workspace.sweep_stale(
            base_dir=os.path.join(self.base, "nope"), max_age_hours=24)
        self.assertEqual(result["removed"], 0)


if __name__ == "__main__":
    unittest.main()
