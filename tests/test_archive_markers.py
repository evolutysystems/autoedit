# ストリームマーカーの採点反映 (ver3 resolve16) の単体テスト
# 実行: python -m unittest discover -s tests
# 要望 (docs/request/ver3/request16.md):
#   ・マーカーの前後 2 分を必ずセクションとして追加する
#   ・採点結果がその区間と被るならマージする
# ネットワークは使わない。Helix 応答を固定 JSON で与え、変換とマージだけを見る
# (test_twitch_quality.py と同方針)。
import unittest

from src.archive import config as archive_config
from src.archive import (
    marker_source,
    project_resume,
    scoring,
    timeline_builder,
    twitch_auth,
)
from src.timeline.model import MEDIA_VIDEO, MediaRef

# OP/ED を無効化した設定コピー相当 (test_archive_timeline と同じ)
_CLIP_SETTINGS = {
    "general": {"opening_enabled": False, "ending_enabled": False},
    "ffmpeg": {"output_width": 1920, "output_height": 1080, "output_fps": 60},
}


# ffprobe を起こさずに素材情報を返す (テストは印の引き回しだけを見る)
def _fake_probe(path, media_id, settings=None, cfg=None):
    return MediaRef(media_id, MEDIA_VIDEO, path, duration_sec=60.0,
                    width=1920, height=1080, fps=60, has_audio=True)


# Helix `GET /helix/streams/markers` の応答 (実際の形。position_seconds = 配信開始からの秒)
_HELIX = {
    "data": [{
        "user_id": "123",
        "videos": [{
            "video_id": "456",
            "markers": [
                {"id": "m2", "created_at": "2026-09-01T21:00:00Z",
                 "description": "後半", "position_seconds": 3600},
                {"id": "m1", "created_at": "2026-09-01T20:10:03Z",
                 "description": "神回", "position_seconds": 300},
            ],
        }],
    }],
    "pagination": {},
}


# 採点済み窓 (curve) を作る。既定の窓幅 180 秒 / スライド 45 秒に合わせない簡易版。
def _window(start, end, total):
    return {"start": float(start), "end": float(end), "total": float(total),
            "emotion": float(total), "comment": 0.0}


class NormalizeMarkersTest(unittest.TestCase):
    """resolve16 §5.2: 取得方式の差を吸収して内部形式へ揃える"""

    def test_normalize_helix_payload(self):
        markers = marker_source.normalize_markers(_HELIX)
        self.assertEqual([m["offset_sec"] for m in markers], [300.0, 3600.0])
        self.assertEqual(markers[0]["description"], "神回")
        self.assertEqual(markers[0]["id"], "m1")

    # marker 配列を直接渡しても同じ結果になる
    def test_normalize_flat_list(self):
        flat = _HELIX["data"][0]["videos"][0]["markers"]
        self.assertEqual(marker_source.normalize_markers(flat),
                         marker_source.normalize_markers(_HELIX))

    # 内部正規化フォーマットはそのまま受け付ける (再正規化しても壊れない)
    def test_normalize_internal_format(self):
        internal = [{"offset_sec": 12.5, "description": "a", "id": "x"}]
        self.assertEqual(marker_source.normalize_markers(internal), internal)
        self.assertEqual(
            marker_source.normalize_markers(marker_source.normalize_markers(internal)),
            internal)

    def test_normalize_sorts_by_offset(self):
        raw = [{"position_seconds": 900}, {"position_seconds": 10},
               {"position_seconds": 100}]
        self.assertEqual([m["offset_sec"] for m in marker_source.normalize_markers(raw)],
                         [10.0, 100.0, 900.0])

    # 壊れた要素は捨てる (採点を止めない)
    def test_normalize_ignores_broken(self):
        raw = [{"position_seconds": 100}, {"description": "位置なし"},
               {"position_seconds": "あ"}, "文字列", None]
        markers = marker_source.normalize_markers(raw)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["offset_sec"], 100.0)

    def test_normalize_handles_empty(self):
        self.assertEqual(marker_source.normalize_markers(None), [])
        self.assertEqual(marker_source.normalize_markers({}), [])
        self.assertEqual(marker_source.normalize_markers({"data": []}), [])


class MarkerSectionTest(unittest.TestCase):
    """resolve16 §5.2 / 要望 I2: マーカーの前後 2 分を区間にする"""

    def test_sections_are_before_after(self):
        sections = marker_source.to_sections(
            [{"offset_sec": 300, "description": "神回"}], 120, 120, 7200)
        self.assertEqual(len(sections), 1)
        self.assertEqual((sections[0]["start"], sections[0]["end"]), (180.0, 420.0))
        self.assertEqual(sections[0]["label"], "神回")

    # 説明が空でも印を残せるよう既定ラベルを入れる
    def test_section_label_defaults(self):
        sections = marker_source.to_sections([{"offset_sec": 300}], 120, 120, 7200)
        self.assertEqual(sections[0]["label"], "マーカー")

    # 動画の端では前後がクランプされる (負の開始・尺超過を作らない)
    def test_sections_clamp_head_and_tail(self):
        head = marker_source.to_sections([{"offset_sec": 30}], 120, 120, 7200)
        self.assertEqual((head[0]["start"], head[0]["end"]), (0.0, 150.0))
        tail = marker_source.to_sections([{"offset_sec": 7190}], 120, 120, 7200)
        self.assertEqual((tail[0]["start"], tail[0]["end"]), (7070.0, 7200.0))

    # VOD の外を指すマーカーは捨てる (配信断で VOD が分かれた回など)
    def test_sections_drop_out_of_range(self):
        raw = [{"offset_sec": -10}, {"offset_sec": 9000}, {"offset_sec": 300}]
        sections = marker_source.to_sections(raw, 120, 120, 7200)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["start"], 180.0)

    # 尺が取れないときは区間を作らない (クランプの基準が無い)
    def test_sections_need_duration(self):
        self.assertEqual(marker_source.to_sections([{"offset_sec": 300}], 120, 120, 0), [])

    def test_sections_sorted_by_start(self):
        sections = marker_source.to_sections(_HELIX, 120, 120, 7200)
        self.assertEqual([s["start"] for s in sections], [180.0, 3480.0])


class ForcedSectionTest(unittest.TestCase):
    """resolve16 §5.4 / 要望 I2・I3: 必ず残す・被ればマージする"""

    # top_n を使い切っていてもマーカー区間はセクションとして残る (要望 I2)
    def test_forced_section_is_always_kept(self):
        curve = [_window(0, 180, 90.0)]
        forced = [{"start": 3000, "end": 3240, "label": "神回"}]
        clips = scoring.select_top_events(curve, 1, 0, 7200, forced=forced)
        self.assertEqual(len(clips), 2)
        marker_clips = [c for c in clips if c["marker"]]
        self.assertEqual(len(marker_clips), 1)
        self.assertEqual((marker_clips[0]["start"], marker_clips[0]["end"]), (3000.0, 3240.0))
        self.assertEqual(marker_clips[0]["marker_labels"], ["神回"])

    # 採点区間と被れば 1 セクションへ統合し、区間は和集合になる (要望 I3)
    def test_forced_merges_with_overlapping_score(self):
        curve = [_window(3100, 3280, 70.0)]
        forced = [{"start": 3000, "end": 3240, "label": "神回"}]
        clips = scoring.select_top_events(curve, 10, 0, 7200, forced=forced)
        self.assertEqual(len(clips), 1)
        self.assertEqual((clips[0]["start"], clips[0]["end"]), (3000.0, 3280.0))
        self.assertTrue(clips[0]["marker"])
        # 代表スコアは高いほう (採点の 70.0) が残る
        self.assertEqual(clips[0]["score"], 70.0)

    # 端が接するだけでも統合する (scoring._merge_time_sections と同規約)
    def test_forced_merges_on_touch(self):
        curve = [_window(3240, 3420, 60.0)]
        forced = [{"start": 3000, "end": 3240, "label": "神回"}]
        clips = scoring.select_top_events(curve, 10, 0, 7200, forced=forced)
        self.assertEqual(len(clips), 1)
        self.assertEqual((clips[0]["start"], clips[0]["end"]), (3000.0, 3420.0))

    # 4 分以内に並んだマーカーは 1 セクションになる (resolve16 §9 Q3)
    def test_forced_adjacent_markers_merge(self):
        forced = marker_source.to_sections(
            [{"offset_sec": 600, "description": "A"}, {"offset_sec": 700, "description": "B"}],
            120, 120, 7200)
        clips = scoring.select_top_events([], 10, 0, 7200, forced=forced)
        self.assertEqual(len(clips), 1)
        self.assertEqual((clips[0]["start"], clips[0]["end"]), (480.0, 820.0))
        self.assertEqual(clips[0]["marker_labels"], ["A", "B"])

    # 離れたマーカーは別セクションのまま
    def test_forced_distant_markers_stay_separate(self):
        forced = marker_source.to_sections(
            [{"offset_sec": 600}, {"offset_sec": 3000}], 120, 120, 7200)
        clips = scoring.select_top_events([], 10, 0, 7200, forced=forced)
        self.assertEqual(len(clips), 2)

    # マーカーは top_n の枠を消費しない (resolve16 §3-4 / §9 Q1)
    def test_forced_does_not_consume_top_n(self):
        curve = [_window(0, 180, 90.0), _window(600, 780, 80.0), _window(1200, 1380, 70.0)]
        forced = [{"start": 3000, "end": 3240, "label": "神回"}]
        clips = scoring.select_top_events(curve, 2, 0, 7200, forced=forced)
        self.assertEqual(len(clips), 3)          # 採点 2 件 + マーカー 1 件
        self.assertEqual(sum(1 for c in clips if c["marker"]), 1)

    # 番号は VOD 時系列順に振り直される (従来どおり)
    def test_index_follows_vod_order(self):
        curve = [_window(3000, 3180, 90.0)]
        forced = [{"start": 300, "end": 540, "label": "先頭"}]
        clips = scoring.select_top_events(curve, 10, 0, 7200, forced=forced)
        self.assertEqual([c["index"] for c in clips], [1, 2])
        self.assertTrue(clips[0]["marker"])      # 先頭がマーカー由来
        self.assertFalse(clips[1]["marker"])

    # forced 無しなら従来と同じ結果 (非破壊の担保)
    def test_forced_none_keeps_current_result(self):
        curve = [_window(0, 180, 90.0), _window(600, 780, 80.0)]
        base = scoring.select_top_events(curve, 10, 0, 7200)
        with_empty = scoring.select_top_events(curve, 10, 0, 7200, forced=[])
        self.assertEqual(base, with_empty)
        self.assertEqual([(c["start"], c["end"], c["score"]) for c in base],
                         [(0.0, 180.0, 90.0), (600.0, 780.0, 80.0)])
        self.assertFalse(any(c["marker"] for c in base))
        self.assertTrue(all(c["marker_labels"] == [] for c in base))

    # 強制区間も duration でクランプする
    def test_forced_is_clamped_to_duration(self):
        clips = scoring.select_top_events(
            [], 10, 0, 500, forced=[{"start": -50, "end": 900, "label": "端"}])
        self.assertEqual((clips[0]["start"], clips[0]["end"]), (0.0, 500.0))


class ScoreForRangeTest(unittest.TestCase):
    """resolve16 §5.4(1): 区間スコアの規則は 1 か所だけに置く"""

    def test_score_for_range_matches_estimate(self):
        curve = [_window(0, 100, 12.0), _window(100, 200, 44.5), _window(200, 300, 8.0)]
        for start, end in ((120, 180), (50, 250), (0, 10), (1000, 2000)):
            self.assertEqual(scoring.score_for_range(curve, start, end),
                             timeline_builder.estimate_section_score(curve, start, end))

    def test_best_window_is_max_total(self):
        curve = [_window(0, 100, 12.0), _window(50, 150, 44.5)]
        best = scoring.best_window_for_range(curve, 60, 90)
        self.assertEqual(best["total"], 44.5)
        self.assertIsNone(scoring.best_window_for_range(curve, 1000, 2000))

    # マーカー区間に重なる窓が無ければ 0 点 (採点していない区間を偽らない)
    def test_forced_score_zero_without_window(self):
        clips = scoring.select_top_events(
            [], 10, 0, 7200, forced=[{"start": 300, "end": 540, "label": "神回"}])
        self.assertEqual(clips[0]["score"], 0.0)
        self.assertEqual(clips[0]["emotion"], 0.0)
        self.assertEqual(clips[0]["comment"], 0.0)


class MarkerFlagPassthroughTest(unittest.TestCase):
    """resolve16 §5.8: マーカー由来の印を Timeline と保存プロジェクトまで運ぶ"""

    def setUp(self):
        self._real_probe = timeline_builder.media_probe.probe
        timeline_builder.media_probe.probe = _fake_probe

    def tearDown(self):
        timeline_builder.media_probe.probe = self._real_probe

    def _prepared(self):
        return [
            {"index": 1, "start": 100.0, "end": 130.0, "score": 80.0,
             "normalized_path": "clip1.mp4", "normalized_duration": 30.0,
             "keep_segments": [(0.0, 30.0)], "items": [],
             "profile": {"width": 1920, "height": 1080, "orientation": "landscape"},
             "marker": True, "marker_labels": ["神回"]},
            # 旧データ相当 (印のキーを持たない) も混ぜる
            {"index": 2, "start": 300.0, "end": 330.0, "score": 70.0,
             "normalized_path": "clip2.mp4", "normalized_duration": 30.0,
             "keep_segments": [(0.0, 30.0)], "items": [],
             "profile": {"width": 1920, "height": 1080, "orientation": "landscape"}},
        ]

    def test_clip_meta_keeps_marker(self):
        timeline = timeline_builder.build_archive_timeline(
            self._prepared(), _CLIP_SETTINGS, source_path="vod.mp4")
        clips = timeline_builder.archive_section(timeline)["clips"]
        self.assertEqual([c["marker"] for c in clips], [True, False])
        self.assertEqual(clips[0]["marker_labels"], ["神回"])
        self.assertEqual(clips[1]["marker_labels"], [])

    # 保存プロジェクトを開き直しても印が戻る (旧プロジェクトは既定で補う)
    def test_rebuild_prepared_restores_marker(self):
        timeline = timeline_builder.build_archive_timeline(
            self._prepared(), _CLIP_SETTINGS, source_path="vod.mp4")
        prepared = project_resume.rebuild_prepared(timeline)
        self.assertEqual([p["marker"] for p in prepared], [True, False])
        self.assertEqual(prepared[0]["marker_labels"], ["神回"])


class MarkerScopeTest(unittest.TestCase):
    """resolve16 §3-5: 旧トークン (スコープ無し) を検出してマーカー取得だけを飛ばす"""

    def _auth(self, scope):
        auth = twitch_auth.TwitchAuth(client_id="dummy", token_path=None)
        auth._token = {"access_token": "t", "scope": scope, "token_type": "bearer"}
        return auth

    def test_has_scope(self):
        self.assertFalse(self._auth("").has_scope(twitch_auth.MARKER_SCOPE))
        self.assertTrue(
            self._auth("user:read:broadcast").has_scope(twitch_auth.MARKER_SCOPE))
        # 複数スコープは空白区切りで返る
        self.assertTrue(
            self._auth("user:read:email user:read:broadcast").has_scope(
                twitch_auth.MARKER_SCOPE))
        # 部分一致で誤判定しない
        self.assertFalse(self._auth("user:read:broadcasts").has_scope(
            twitch_auth.MARKER_SCOPE))

    # 未ログイン (トークン無し) でも例外にせず False
    def test_has_scope_without_token(self):
        auth = twitch_auth.TwitchAuth(client_id="dummy", token_path=None)
        self.assertFalse(auth.has_scope(twitch_auth.MARKER_SCOPE))


class MarkerConfigTest(unittest.TestCase):
    """resolve16 §7: 設定は既定で有効・前後 2 分・1 つで機能ごと止められる"""

    def test_defaults(self):
        cfg = archive_config.marker_config({})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["before_sec"], 120.0)
        self.assertEqual(cfg["after_sec"], 120.0)

    def test_disabled(self):
        cfg = archive_config.marker_config({"archive": {"markers": {"enabled": False}}})
        self.assertFalse(cfg["enabled"])

    def test_custom_seconds(self):
        cfg = archive_config.marker_config(
            {"archive": {"markers": {"before_sec": 60, "after_sec": 30}}})
        self.assertEqual((cfg["before_sec"], cfg["after_sec"]), (60.0, 30.0))

    # マーカー取得に要るスコープは既定で要求する (既存 setting.json には無いキー)
    def test_auth_scopes_default(self):
        self.assertEqual(archive_config.auth_config({})["scopes"],
                         [twitch_auth.MARKER_SCOPE])
        self.assertEqual(
            archive_config.auth_config({"archive": {"auth": {"scopes": []}}})["scopes"], [])


if __name__ == "__main__":
    unittest.main()
