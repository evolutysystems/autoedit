# 解析データの集約の単体テスト (docs/AiEditor/resolve1.md §5 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・AnalysisBundle の縮約 (セル・窓・コメント・文字起こしの間引き)
#   ・**コメントの投稿者名が落ちる** (既定 ai.privacy.include_comment_user=false / §5.5)
#   ・パス・端末名・利用者の情報を送らない (§5.5)
#   ・候補区間だけ文字起こしし、**原点を足して VOD 秒へ戻す** (§5.3)
#   ・重なる候補は統合して 1 回だけ流す
#   ・概算トークンでバッチを割る (分割の判断にだけ使う / §5.4)
#   ・採点の本数だけ AI 用へ差し替え、設定ファイルは書き換えない (§7.1)
# ffmpeg も AI も呼ばない (切り出しと文字起こしは差し替える)。
import copy
import os
import tempfile
import unittest

from src.ai import context_builder as cb


def _analysis(clips=None, curve=None, duration=3600.0):
    return {"duration": duration,
            "curve": curve if curve is not None else [
                {"start": 0.0, "end": 300.0, "emotion": 50.0, "comment": 10.0,
                 "total": 30.0},
                {"start": 60.0, "end": 360.0, "emotion": 60.0, "comment": 20.0,
                 "total": 40.0}],
            "clips": clips if clips is not None else [
                {"index": 1, "start": 100.0, "end": 130.0, "score": 8.0,
                 "emotion": 7.0, "comment": 9.0, "marker": False,
                 "marker_labels": []},
                {"index": 2, "start": 500.0, "end": 520.0, "score": 3.0,
                 "emotion": 2.0, "comment": 4.0}],
            "audio_scan": None}


def _cells(start, end, step=10.0):
    cells = []
    at = start
    while at < end:
        cells.append({"start": at, "end": at + step, "mean_db": -20.0,
                      "max_db": -5.0, "silence_ratio": 0.1, "comment_count": 3,
                      "w_count": 2, "laugh_word_count": 1, "laugh_emote_count": 0})
        at += step

    return cells


class BundleTest(unittest.TestCase):

    def test_candidates_come_from_the_existing_scoring(self):
        bundle = cb.build(_analysis(), {}, source={"video_id": "123"})

        self.assertEqual([1, 2], bundle.indexes())
        self.assertEqual(3600.0, bundle.duration_sec)
        self.assertEqual(100.0, bundle.candidate(1)["start"])

    # スコア下限で切る (既定 0 = 無効)。
    def test_min_score_filters(self):
        bundle = cb.build(_analysis(), {"ai": {"candidates": {"min_score": 5}}})

        self.assertEqual([1], bundle.indexes())

    def test_broken_clips_are_skipped(self):
        bundle = cb.build(_analysis(clips=[None, "x", {"index": 3, "start": 1.0,
                                                       "end": 2.0, "score": 1.0}]), {})

        self.assertEqual([3], bundle.indexes())

    # 採点の本数だけ差し替える。元の設定は書き換えない (§7.1)。
    def test_analysis_settings_only_changes_the_count(self):
        settings = {"archive": {"scoring": {"top_n": 10, "window_sec": 300}}}
        original = copy.deepcopy(settings)

        copied = cb.analysis_settings(settings)

        self.assertEqual(30, copied["archive"]["scoring"]["top_n"])
        self.assertEqual(300, copied["archive"]["scoring"]["window_sec"])
        self.assertEqual(original, settings, "設定ファイル側は触らない")

    def test_window_sec_can_be_overridden(self):
        copied = cb.analysis_settings(
            {"ai": {"candidates": {"top_n": 5, "window_sec": 120}}})

        self.assertEqual(5, copied["archive"]["scoring"]["top_n"])
        self.assertEqual(120, copied["archive"]["scoring"]["window_sec"])


class ReductionTest(unittest.TestCase):

    def _bundle(self):
        bundle = cb.build(_analysis(), {}, source={"video_id": "123",
                                                   "title": "配信タイトル",
                                                   "game": "ゲーム名",
                                                   "width": 1920, "height": 1080,
                                                   "fps": 60.0})
        bundle.cells = _cells(0.0, 600.0)
        bundle.transcript = [{"start": 105.0, "end": 108.0, "text": "候補の中"},
                             {"start": 400.0, "end": 402.0, "text": "候補の外"}]
        bundle.comments = [{"offset_sec": 101.0, "user": "viewer1", "text": "ｗｗｗ",
                            "emotes": []},
                           {"offset_sec": 102.0, "user": "viewer2", "text": "草",
                            "emotes": []},
                           {"offset_sec": 400.0, "user": "viewer3", "text": "外",
                            "emotes": []}]

        return bundle

    def test_only_the_candidate_window_is_sent(self):
        payload = cb.for_prompt(self._bundle(), {}, indexes=[1])

        self.assertEqual(1, len(payload["candidates"]))
        candidate = payload["candidates"][0]
        self.assertEqual([{"start": 105.0, "end": 108.0, "text": "候補の中"}],
                         candidate["transcript"])
        self.assertEqual(["ｗｗｗ", "草"],
                         [row["text"] for row in candidate["comments"]])
        self.assertTrue(all(cell["end"] > 40.0 for cell in candidate["cells"]),
                        "候補の前後 cell_pad_sec 秒だけ")

    # 全候補の要約は先頭に固定する (キャッシュは前方一致 / §6.4)。
    def test_overview_comes_first(self):
        payload = cb.for_prompt(self._bundle(), {}, indexes=[1])
        keys = list(payload)

        self.assertEqual(["source", "candidates_overview", "candidates"], keys)
        self.assertEqual([1, 2], [row["index"]
                                  for row in payload["candidates_overview"]])

    # 投稿者名は送らない (回答 Q8 / §5.5)。
    def test_comment_user_is_dropped(self):
        payload = cb.for_prompt(self._bundle(), {}, indexes=[1])

        for row in payload["candidates"][0]["comments"]:
            self.assertNotIn("user", row)
        self.assertNotIn("viewer1", repr(payload))

    def test_comment_user_can_be_included(self):
        payload = cb.for_prompt(self._bundle(),
                                {"ai": {"privacy": {"include_comment_user": True}}},
                                indexes=[1])

        self.assertEqual("viewer1", payload["candidates"][0]["comments"][0]["user"])

    # ｗ・笑い語・エモートを含むものを優先して間引く。
    def test_comments_are_thinned_by_laughter(self):
        bundle = self._bundle()
        bundle.comments = ([{"offset_sec": 100.0 + i, "user": "u", "text": "普通",
                             "emotes": []} for i in range(10)]
                           + [{"offset_sec": 120.0, "user": "u", "text": "ｗｗｗ",
                               "emotes": []}])

        payload = cb.for_prompt(
            bundle, {"ai": {"budget": {"max_comments_per_cell": 2}}}, indexes=[1])
        texts = [row["text"] for row in payload["candidates"][0]["comments"]]

        self.assertIn("ｗｗｗ", texts)
        self.assertEqual(2, len(texts))

    # 送らないもの: パス・端末名・利用者の情報 (§5.5)。
    def test_source_has_no_paths(self):
        bundle = self._bundle()
        bundle.source["input_path"] = r"C:\Users\someone\video.mp4"

        payload = cb.for_prompt(bundle, {}, indexes=[1])

        self.assertNotIn("someone", repr(payload))
        self.assertNotIn("input_path", payload["source"])
        self.assertEqual("配信タイトル", payload["source"]["title"])

    def test_curve_is_limited_to_the_span(self):
        bundle = self._bundle()
        bundle.curve = [{"start": float(i * 10), "end": float(i * 10 + 300),
                         "emotion": 1.0, "comment": 1.0, "total": 1.0}
                        for i in range(20)]

        payload = cb.for_prompt(bundle, {"ai": {"budget": {"curve_window_span": 1}}},
                                indexes=[1])

        self.assertEqual(3, len(payload["candidates"][0]["curve"]))


class BatchTest(unittest.TestCase):

    def _bundle(self, count):
        clips = [{"index": i + 1, "start": float(i * 100), "end": float(i * 100 + 30),
                  "score": 5.0} for i in range(count)]

        return cb.build(_analysis(clips=clips), {})

    def test_batch_size(self):
        groups = cb.batches(self._bundle(10), {"ai": {"scene_eval": {"batch_size": 4}}})

        self.assertEqual([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]], groups)

    # 概算が上限を超えるバッチはさらに割る (§5.4)。
    def test_token_budget_splits_further(self):
        bundle = self._bundle(4)
        bundle.transcript = [{"start": float(i * 100) + 1.0, "end": float(i * 100) + 5.0,
                              "text": "あ" * 2000} for i in range(4)]

        groups = cb.batches(bundle, {"ai": {"scene_eval": {"batch_size": 4},
                                            "budget": {"max_input_tokens": 500}}})

        self.assertTrue(len(groups) > 1)
        self.assertEqual([1, 2, 3, 4], sorted(i for group in groups for i in group))

    # 上限 0 は無制限。
    def test_zero_budget_means_no_split(self):
        groups = cb.batches(self._bundle(4), {"ai": {"scene_eval": {"batch_size": 4},
                                                     "budget": {"max_input_tokens": 0}}})

        self.assertEqual([[1, 2, 3, 4]], groups)

    def test_estimate_counts_characters(self):
        tokens = cb.estimate_tokens({"text": "あ" * 100},
                                    {"ai": {"budget": {"chars_per_token": 2.0}}})

        self.assertTrue(50 <= tokens <= 60, tokens)


class TranscribeTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cuts = []

    def tearDown(self):
        self._dir.cleanup()

    def _tools(self, origin_shift=0.0, items=None):
        def cut(input_path, start, end, dest, ffmpeg_cfg):
            self.cuts.append((round(start, 1), round(end, 1)))
            with open(dest, "wb") as handle:
                handle.write(b"0")

        def measure(input_path, start, ffmpeg_cfg):
            # 切り出しの原点は要求位置より手前になる (最大 1 GOP)。
            return float(start) - origin_shift

        def transcribe(path, settings, eff_cfg):
            return list(items if items is not None
                        else [{"start": 1.0, "end": 3.0, "text": "やばい"}])

        def filter_speakers(recognized, path, eff_cfg, ffmpeg_cfg):
            return recognized

        return {"cut": cut, "measure": measure, "transcribe": transcribe,
                "filter_speakers": filter_speakers}

    def _run(self, candidates, settings=None, **tools):
        return cb.transcribe_candidates(
            "vod.mp4", settings if settings is not None else {}, candidates,
            self._dir.name, duration_sec=3600.0, **self._tools(**tools))

    # 認識結果は素材内の秒。原点を足して VOD 秒へ戻す (§5.3 の手順 4)。
    def test_times_are_mapped_back_to_vod_seconds(self):
        items = self._run([{"start": 100.0, "end": 130.0}], origin_shift=2.0)

        # 原点 = 100 - 15 (pad) - 2 (ずれ) = 83 → 83 + 1.0 = 84.0
        self.assertEqual([{"start": 84.0, "end": 86.0, "text": "やばい"}], items)

    # 重なる候補は統合して 1 回だけ流す。
    def test_overlapping_candidates_are_merged(self):
        self._run([{"start": 100.0, "end": 130.0}, {"start": 120.0, "end": 150.0}])

        self.assertEqual(1, len(self.cuts))
        self.assertEqual((85.0, 165.0), self.cuts[0])

    def test_separate_candidates_are_cut_separately(self):
        self._run([{"start": 100.0, "end": 130.0}, {"start": 900.0, "end": 930.0}])

        self.assertEqual(2, len(self.cuts))

    # 区間は VOD の中へ収める (負の時刻で切り出させない)。
    def test_range_is_clamped_to_the_vod(self):
        self._run([{"start": 5.0, "end": 20.0}])

        self.assertEqual((0.0, 35.0), self.cuts[0])

    # 作業ファイルは残さない。
    def test_work_files_are_removed(self):
        self._run([{"start": 100.0, "end": 130.0}])

        self.assertEqual([], os.listdir(self._dir.name))

    # 文字起こしが失敗しても経路を止めない (その区間だけ落ちる)。
    def test_failure_is_contained(self):
        def broken(path, settings, eff_cfg):
            raise RuntimeError("boom")

        tools = self._tools()
        tools["transcribe"] = broken
        items = cb.transcribe_candidates("vod.mp4", {}, [{"start": 100.0, "end": 130.0}],
                                         self._dir.name, duration_sec=3600.0, **tools)

        self.assertEqual([], items)

    def test_disabled_transcript_sends_nothing(self):
        items = self._run([{"start": 100.0, "end": 130.0}],
                          settings={"ai": {"privacy": {"include_transcript": False}}})

        self.assertEqual([], items)
        self.assertEqual([], self.cuts)

    # 連続する短文は結合する (§5.4)。
    def test_adjacent_lines_are_merged(self):
        items = self._run([{"start": 100.0, "end": 130.0}],
                          items=[{"start": 1.0, "end": 2.0, "text": "やば"},
                                 {"start": 2.5, "end": 3.0, "text": "すぎ"},
                                 {"start": 10.0, "end": 11.0, "text": "別の話"}])

        self.assertEqual(["やば すぎ", "別の話"], [item["text"] for item in items])

    def test_cancel_stops_cutting(self):
        items = cb.transcribe_candidates(
            "vod.mp4", {}, [{"start": 100.0, "end": 130.0}], self._dir.name,
            duration_sec=3600.0, cancel_cb=lambda: True, **self._tools())

        self.assertEqual([], items)
        self.assertEqual([], self.cuts)


if __name__ == "__main__":
    unittest.main()
