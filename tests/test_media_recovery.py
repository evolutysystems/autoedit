# 保存済みプロジェクトの素材復旧 (src/timeline/media_recovery.py) の単体テスト
# 実行: python -m unittest discover -s tests
# ver3 resolve7 §3-5 / §10.1:
#   ・判定順 (そのまま在る → 相対パス → 作り直し → 再リンク)
#   ・media_path == input_path の保存では作り直さない
#   ・再リンクを断った素材だけが「見つからない」として残る
# ラウドネス正規化そのものは呼ばずに差し替えて確かめる (ffmpeg を動かさない)。
import os
import shutil
import tempfile
import unittest

from src.timeline import media_recovery
from src.timeline.model import (
    Clip,
    MediaRef,
    ORIGIN_SILENCE_CUT,
    TRACK_VIDEO,
    Timeline,
    Track,
)

_SETTINGS = {"timeline": {}}


# 本編素材 1 件 + 追加素材 1 件の Timeline を組む。
# 追加素材は V2 のクリップとして載せる (どのクリップからも使われていない素材は
# 復旧の対象外になったため / ver5 resolve6 §3.3)。
def _build(media_path, extra_path, input_path, media_role=None, path_rel=""):
    body = MediaRef("m1", "video", media_path, 100.0, 1920, 1080, 60, True)
    extra = MediaRef("m2", "image", extra_path, None, 640, 360, 0, False,
                     path_rel=path_rel)
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    overlay = Track("V2", TRACK_VIDEO, 2, name="Video 2", clips=[
        Clip("c2", "m2", 0.0, 3.0, 0.0, 3.0),
    ])
    source = {"input_path": input_path, "media_path": media_path, "media_id": "m1",
              "duration_sec": 100.0}
    if media_role:
        source["media_role"] = media_role
    return Timeline(fps=60, source=source, media_pool=[body, extra],
                    tracks=[video, overlay])


class MediaRecoveryTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="media_recovery_test_")
        self.project_path = os.path.join(self._dir, "sample.timeline.json")
        self.input_path = self._touch("sample.mp4")
        # 中間ファイル (一時ディレクトリ想定。実体は作らない = 消えた状態)
        self.media_path = os.path.join(self._dir, "work", "loudness_normalized.mp4")
        self.extra_path = self._touch("logo.png")
        # 正規化を呼ばずに済むよう差し替える (呼ばれたことだけを記録する)
        self._calls = []
        self._original = media_recovery.loudness_normalizer.normalize_file
        media_recovery.loudness_normalizer.normalize_file = self._fake_normalize

    def tearDown(self):
        media_recovery.loudness_normalizer.normalize_file = self._original
        shutil.rmtree(self._dir, ignore_errors=True)

    def _touch(self, name):
        path = os.path.join(self._dir, name)
        with open(path, "wb") as f:
            f.write(b"")
        return path

    # normalize_file の代役 (出力先に実体を作って成功したことにする)
    def _fake_normalize(self, input_path, output_path, settings, on_progress=None):
        self._calls.append(input_path)
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(b"")
        return output_path

    # ① そのまま在る素材には触らない
    def test_existing_media_is_untouched(self):
        timeline = _build(self.input_path, self.extra_path, self.input_path)
        result = media_recovery.recover(timeline, self.project_path, _SETTINGS)
        self.assertEqual(result["recovered"], [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(self._calls, [])

    # ② 相対パスで見つかればそれを採用する (プロジェクトごと移した場合)
    def test_relative_path_is_used(self):
        moved = os.path.join(self._dir, "assets")
        os.makedirs(moved, exist_ok=True)
        actual = os.path.join(moved, "logo.png")
        shutil.move(self.extra_path, actual)
        timeline = _build(self.input_path, os.path.join("C:", "gone", "logo.png"),
                          self.input_path, path_rel=os.path.join("assets", "logo.png"))
        result = media_recovery.recover(timeline, self.project_path, _SETTINGS)
        self.assertIn("m2", result["recovered"])
        self.assertEqual(timeline.media_by_id("m2").path, os.path.normpath(actual))

    # ③ 本編の中間ファイルが無ければ元動画から作り直す
    def test_body_media_is_renormalized(self):
        timeline = _build(self.media_path, self.extra_path, self.input_path,
                          media_role="normalized")
        result = media_recovery.recover(timeline, self.project_path, _SETTINGS)
        self.assertIn("m1", result["recovered"])
        self.assertTrue(result["renormalized"])
        self.assertEqual(self._calls, [self.input_path])
        self.assertTrue(os.path.exists(timeline.media_by_id("m1").path))
        # source の記録も新しい素材へ更新される
        self.assertEqual(timeline.source["media_path"], timeline.media_by_id("m1").path)

    # ③' media_path == input_path の保存 (正規化なし) では作り直さない
    def test_original_source_is_not_renormalized(self):
        missing_input = os.path.join(self._dir, "moved", "sample.mp4")
        timeline = _build(missing_input, self.extra_path, self.input_path,
                          media_role="original")
        result = media_recovery.recover(timeline, self.project_path, _SETTINGS)
        self.assertIn("m1", result["recovered"])
        self.assertFalse(result["renormalized"])
        self.assertEqual(self._calls, [])
        self.assertEqual(timeline.media_by_id("m1").path, self.input_path)

    # ③'' media_role が無い旧ファイルは media_path != input_path で判定する
    def test_legacy_project_without_media_role(self):
        timeline = _build(self.media_path, self.extra_path, self.input_path)
        result = media_recovery.recover(timeline, self.project_path, _SETTINGS)
        self.assertIn("m1", result["recovered"])
        self.assertEqual(self._calls, [self.input_path])

    # ③''' 設定で「元動画をそのまま使う」を選べる
    def test_use_source_policy(self):
        settings = {"timeline": {"project": {"missing_media_policy": "use_source"}}}
        timeline = _build(self.media_path, self.extra_path, self.input_path,
                          media_role="normalized")
        result = media_recovery.recover(timeline, self.project_path, settings)
        self.assertEqual(self._calls, [])
        self.assertFalse(result["renormalized"])
        self.assertEqual(timeline.media_by_id("m1").path, self.input_path)

    # ④ 再リンクで差し替えられる
    def test_relink_callback_replaces_path(self):
        replacement = self._touch("logo2.png")
        timeline = _build(self.input_path, os.path.join(self._dir, "gone.png"),
                          self.input_path)
        result = media_recovery.recover(
            timeline, self.project_path, _SETTINGS,
            relink_callback=lambda info: replacement)
        self.assertIn("m2", result["recovered"])
        self.assertEqual(timeline.media_by_id("m2").path, replacement)

    # ④' 再リンクを断った素材だけが「見つからない」として残る
    def test_declined_relink_is_reported_missing(self):
        timeline = _build(self.input_path, os.path.join(self._dir, "gone.png"),
                          self.input_path)
        result = media_recovery.recover(timeline, self.project_path, _SETTINGS,
                                        relink_callback=lambda info: None)
        self.assertEqual(result["missing"], ["m2"])

    # ④ 一度差し替えたフォルダに同じ名前があれば、2 件目以降は聞き直さない
    def test_known_folder_is_reused(self):
        moved = os.path.join(self._dir, "moved")
        os.makedirs(moved, exist_ok=True)
        first = os.path.join(moved, "logo.png")
        second = os.path.join(moved, "logo2.png")
        for path in (first, second):
            with open(path, "wb") as f:
                f.write(b"")

        # どちらの素材も元の場所には無い (フォルダごと移した想定)
        timeline = _build(self.input_path, os.path.join(self._dir, "gone", "logo.png"),
                          self.input_path)
        timeline.media_pool.append(
            MediaRef("m3", "image", os.path.join(self._dir, "gone", "logo2.png"),
                     None, 640, 360, 0, False))
        timeline.track_by_id("V2").clips.append(Clip("c3", "m3", 5.0, 3.0, 0.0, 3.0))

        asked = []

        def relink(info):
            asked.append(info["media_id"])
            return first

        result = media_recovery.recover(timeline, self.project_path, _SETTINGS,
                                        relink_callback=relink)
        self.assertEqual(asked, ["m2"])          # 尋ねるのは 1 回だけ
        self.assertEqual(sorted(result["recovered"]), ["m2", "m3"])
        self.assertEqual(timeline.media_by_id("m3").path, second)

    # 元動画も無ければ本編は復旧できない (処理は止めず missing として返す)
    def test_missing_input_path_reports_missing(self):
        gone_input = os.path.join(self._dir, "no_such_input.mp4")
        timeline = _build(self.media_path, self.extra_path, gone_input,
                          media_role="normalized")
        result = media_recovery.recover(timeline, self.project_path, _SETTINGS)
        self.assertEqual(result["missing"], ["m1"])
        self.assertEqual(self._calls, [])


if __name__ == "__main__":
    unittest.main()
