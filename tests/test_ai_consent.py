# AI を使う前の同意の単体テスト (docs/AiEditor/resolve1.md §5.5 / §13.4)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・初回は同意が必要。記録すると次からは出ない
#   ・文面の版が上がったら**再確認**になる
#   ・文面は設定と食い違わない (投稿者名を送る設定ならそう書く / 学習オフなら書かない)
#   ・**学習 (端末内への記録) も同意に含める** (黙って記録しない / §13.4)
#   ・送らないもの (音声・映像・パス・端末名・メール) を明記する
#   ・保存は画面側の責務。ここではファイルへ書かない
import unittest

from src.ai import consent


class NeedsConsentTest(unittest.TestCase):

    def test_first_use_needs_consent(self):
        self.assertTrue(consent.needs_consent({}))
        self.assertEqual(0, consent.accepted_version({}))

    def test_accept_records_the_version(self):
        settings = {}

        consent.accept(settings)

        self.assertFalse(consent.needs_consent(settings))
        self.assertEqual(consent.CURRENT_VERSION,
                         settings["ai"]["consent"]["accepted_version"])

    # 文面を変えたら版が上がり、再確認になる。
    def test_old_version_needs_consent_again(self):
        settings = {"ai": {"consent": {"accepted_version": consent.CURRENT_VERSION - 1}}}

        self.assertTrue(consent.needs_consent(settings))

    def test_broken_value_is_treated_as_not_accepted(self):
        self.assertTrue(consent.needs_consent(
            {"ai": {"consent": {"accepted_version": "はい"}}}))

    def test_revoke(self):
        settings = {}
        consent.accept(settings)

        consent.revoke(settings)

        self.assertTrue(consent.needs_consent(settings))

    # 保存はしない (設定の書き込み口を画面側 1 か所に保つ)。
    def test_accept_does_not_write_files(self):
        settings = {"ai": {"consent": {}}}

        returned = consent.accept(settings)

        self.assertIs(settings, returned)


class TextTest(unittest.TestCase):

    def test_lists_what_is_sent_and_not_sent(self):
        body = consent.text({})

        self.assertIn("解析結果", body)
        self.assertIn("文字起こし", body)
        self.assertIn("送らないもの", body)
        self.assertIn("音声・映像そのもの", body)
        self.assertIn("メールアドレス", body)

    # 既定では投稿者名を送らないと書く (回答 Q8)。
    def test_comment_user_is_described_as_not_sent(self):
        body = consent.text({})

        self.assertIn("投稿者名は送りません", body)

    def test_comment_user_setting_changes_the_text(self):
        body = consent.text({"ai": {"privacy": {"include_comment_user": True}}})

        self.assertIn("投稿者名", body)
        self.assertNotIn("投稿者名は送りません", body)

    def test_transcript_off_is_not_listed(self):
        body = consent.text({"ai": {"privacy": {"include_transcript": False}}})

        self.assertNotIn("文字起こし", body)

    # 学習 (端末内への記録) も同意に含める (§13.4)。
    def test_learning_is_included(self):
        body = consent.text({})

        self.assertIn("この端末の中だけに残ります", body)
        self.assertIn("差分", body)
        self.assertIn("180 日", body)

    def test_learning_off_is_not_listed(self):
        body = consent.text({"ai": {"learning": {"enabled": False}}})

        self.assertNotIn("差分", body)

    def test_vision_off_is_not_listed(self):
        body = consent.text({"ai": {"vision": {"enabled": False}}})

        self.assertNotIn("静止画", body)

    def test_declining_is_explained(self):
        self.assertIn("同意しない場合", consent.text({}))

    def test_title(self):
        self.assertTrue(consent.title())


if __name__ == "__main__":
    unittest.main()
