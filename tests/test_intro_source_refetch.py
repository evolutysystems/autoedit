# テーマ区間だけを取り直してイントロカードの素を用意する処理の単体テスト
# (ver9 resolve12 §3.3〜§3.5 / §6.3)
# 実行: python -m unittest discover -s tests
#
# 背景:
#   元 VOD を手放したプロジェクトでは、テーマを入れてもカードの素が無く、
#   イントロカードだけが黙って省かれていた。書き出しの直前に、テーマが
#   入っているセクションに限って [開始 - buffer_sec, 開始] を取り直す。
#
# 重点 (ネットワークは使わない。fetch_range を mock にして「呼ばれる条件」を見る):
#   ・通信するのは「テーマあり × 控えなし × 元 VOD なし × 取得元あり」のときだけ
#   ・取得範囲がイントロカードの設計どおりであること
#   ・取得素材の原点 (origin_sec) を添えて切り出すこと
#   ・失敗しても他のセクションと書き出しが続くこと
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import intro_source
from src.exceptions import TwitchError

_FFMPEG_CFG = {"video_codec": "libx264", "preset": "medium", "crf": 20,
               "audio_codec": "aac"}


def _settings(refetch=True, enabled=True, buffer_sec=3.0):
    return {
        "ffmpeg": _FFMPEG_CFG,
        "archive": {
            "intro_card": {"enabled": enabled, "buffer_sec": buffer_sec},
            "section_add": {"refetch": {"enabled": refetch}},
        },
    }


# Timeline の最小フェイク (取得元の控えとセクション・素材の控えだけを持つ)
def _timeline(vod="", clips=None, media=None, video_id="2912229276"):
    archive = {
        "vod_path": vod,
        "clips": clips if clips is not None else [
            {"index": 1, "media_id": "m1", "vod_start": 140.0, "vod_end": 540.0}],
        "media": media if media is not None else [
            {"media_id": "m1", "vod_start": 140.0, "vod_end": 540.0}],
    }
    if video_id:
        archive["vod_source"] = {"provider": "twitch", "video_id": video_id,
                                 "quality": "source", "vod_duration_sec": 6171.0}
    return types.SimpleNamespace(source={"input_path": vod, "archive": archive})


class EnsureIntroSourcesTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.media_dir = os.path.join(self.root, "proj.media")
        self.workdir = os.path.join(self.root, "work")
        os.makedirs(self.workdir, exist_ok=True)
        self.fetched = []
        self.exported = []

        def fake_fetch(video_id, start_sec, end_sec, workdir, settings, **kwargs):
            self.fetched.append({"video_id": video_id, "start": start_sec,
                                 "end": end_sec, "quality": kwargs.get("quality"),
                                 "tag": kwargs.get("tag")})
            path = os.path.join(workdir, f"range_{len(self.fetched)}.mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"0" * 16)
            # 取得はセグメント単位のため、要求より手前から始まる素材が返る
            return {"path": path, "origin_sec": float(start_sec) - 5.0,
                    "end_sec": float(end_sec) + 5.0, "quality": "chunked",
                    "bytes": 16, "segments": 2}

        patcher = mock.patch.object(intro_source.vod_range, "fetch_range",
                                    side_effect=fake_fetch)
        self.fetch_mock = patcher.start()
        self.addCleanup(patcher.stop)

        def fake_export(path, clip_start, buffer_sec, dest, ffmpeg_cfg,
                        source_origin_sec=0.0):
            self.exported.append({"path": path, "clip_start": clip_start,
                                  "buffer_sec": buffer_sec, "dest": dest,
                                  "origin": source_origin_sec})
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(b"0" * 16)
            return dest

        patcher = mock.patch.object(intro_source.clip_writer, "export_intro_source",
                                    side_effect=fake_export)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, prepared=None, edited=None, timeline=None, settings=None,
             media_dir=None):
        prepared = prepared if prepared is not None else [
            {"index": 1, "start": 140.0, "intro_source": ""}]
        edited = edited if edited is not None else [
            {"index": 1, "theme": "むじかの朝", "use": True}]
        return prepared, intro_source.ensure_intro_sources(
            prepared, edited, timeline or _timeline(),
            settings or _settings(),
            media_dir=self.media_dir if media_dir is None else media_dir,
            workdir=self.workdir, ffmpeg_cfg=_FFMPEG_CFG)

    # ---- 本題: 取得範囲はイントロカードの設計どおり ----

    def test_fetches_only_the_card_range(self):
        prepared, summary = self._run()

        self.assertEqual(len(self.fetched), 1)
        self.assertEqual(self.fetched[0]["start"], 137.0)    # 140 - buffer 3
        self.assertEqual(self.fetched[0]["end"], 140.0)
        self.assertEqual(self.fetched[0]["video_id"], "2912229276")
        self.assertEqual(summary["fetched"], 1)
        self.assertEqual(summary["filled"], 1)
        self.assertEqual(prepared[0]["intro_source"],
                         os.path.join(self.media_dir, "m1_intro.mp4"))

    # 取得素材の原点を添えて切る (元 VOD から切ったのと同じフレームになる)
    def test_passes_the_origin_to_the_cut(self):
        self._run()

        self.assertEqual(len(self.exported), 1)
        self.assertEqual(self.exported[0]["clip_start"], 140.0)
        self.assertEqual(self.exported[0]["buffer_sec"], 3.0)
        self.assertEqual(self.exported[0]["origin"], 132.0)  # 137 - 5 (セグメント境界)

    # 控えは「セクション開始を担当する素材」の名前で残す (resolve12 §3.2)
    def test_names_the_file_after_the_media_that_owns_the_start(self):
        timeline = _timeline(
            clips=[{"index": 1, "media_id": "m1", "vod_start": 140.0, "vod_end": 540.0}],
            media=[{"media_id": "m1", "vod_start": 180.0, "vod_end": 540.0},
                   {"media_id": "m10", "vod_start": 140.0, "vod_end": 180.0}])

        self._run(timeline=timeline)

        self.assertEqual(self.exported[0]["dest"],
                         os.path.join(self.media_dir, "m10_intro.mp4"))

    # 素材フォルダが無い (未保存) ときは作業フォルダへ置く = その実行かぎり
    def test_falls_back_to_the_workdir(self):
        self._run(media_dir="")

        self.assertEqual(self.exported[0]["dest"],
                         os.path.join(self.workdir, "m1_intro.mp4"))

    # ---- 通信しない条件 ----

    def test_skips_without_theme(self):
        _prepared, summary = self._run(
            edited=[{"index": 1, "theme": "", "use": True}])

        self.assertEqual(self.fetched, [])
        self.assertEqual(summary, {"filled": 0, "fetched": 0, "skipped": []})

    def test_skips_unused_clip(self):
        _prepared, summary = self._run(
            edited=[{"index": 1, "theme": "テーマ", "use": False}])

        self.assertEqual(self.fetched, [])
        self.assertEqual(summary["filled"], 0)

    def test_skips_when_the_intro_source_is_already_there(self):
        kept = os.path.join(self.root, "m1_intro.mp4")
        with open(kept, "wb") as f:
            f.write(b"0" * 16)

        _prepared, summary = self._run(
            prepared=[{"index": 1, "start": 140.0, "intro_source": kept}])

        self.assertEqual(self.fetched, [])
        self.assertEqual(summary["filled"], 1)

    def test_skips_when_the_vod_is_on_hand(self):
        vod = os.path.join(self.root, "vod_1.mp4")
        with open(vod, "wb") as f:
            f.write(b"0" * 16)

        _prepared, summary = self._run(timeline=_timeline(vod=vod))

        self.assertEqual(self.fetched, [])
        self.assertEqual(summary["fetched"], 0)

    def test_skips_when_the_card_is_disabled(self):
        _prepared, summary = self._run(settings=_settings(enabled=False))

        self.assertEqual(self.fetched, [])
        self.assertEqual(summary["filled"], 0)

    def test_skips_when_refetch_is_off(self):
        _prepared, summary = self._run(settings=_settings(refetch=False))

        self.assertEqual(self.fetched, [])
        self.assertEqual(summary["skipped"], [(1, intro_source.SKIP_NO_SOURCE)])

    # ローカル動画から採点したプロジェクト (取得元が分からない)
    def test_skips_without_a_video_id(self):
        _prepared, summary = self._run(timeline=_timeline(video_id=""))

        self.assertEqual(self.fetched, [])
        self.assertEqual(summary["skipped"], [(1, intro_source.SKIP_NO_SOURCE)])

    # VOD の先頭すぎて手前を確保できない区間は、元 VOD があっても作れない
    def test_skips_when_the_buffer_does_not_fit(self):
        _prepared, summary = self._run(
            prepared=[{"index": 1, "start": 2.0, "intro_source": ""}])

        self.assertEqual(self.fetched, [])
        self.assertEqual(summary["skipped"], [(1, intro_source.SKIP_NO_BUFFER)])

    # ---- 失敗しても止めない ----

    def test_one_failure_does_not_stop_the_others(self):
        def flaky(video_id, start_sec, end_sec, workdir, settings, **kwargs):
            if start_sec < 200:
                raise TwitchError("取得できません")
            path = os.path.join(workdir, "range.mp4")
            with open(path, "wb") as f:
                f.write(b"0" * 16)
            return {"path": path, "origin_sec": float(start_sec), "end_sec": end_sec,
                    "quality": "chunked", "bytes": 16, "segments": 1}

        self.fetch_mock.side_effect = flaky
        prepared = [{"index": 1, "start": 140.0, "intro_source": ""},
                    {"index": 2, "start": 990.0, "intro_source": ""}]
        edited = [{"index": 1, "theme": "A", "use": True},
                  {"index": 2, "theme": "B", "use": True}]
        timeline = _timeline(
            clips=[{"index": 1, "media_id": "m1", "vod_start": 140.0, "vod_end": 540.0},
                   {"index": 2, "media_id": "m2", "vod_start": 990.0, "vod_end": 1170.0}],
            media=[{"media_id": "m1", "vod_start": 140.0, "vod_end": 540.0},
                   {"media_id": "m2", "vod_start": 990.0, "vod_end": 1170.0}])

        prepared, summary = self._run(prepared=prepared, edited=edited,
                                      timeline=timeline)

        self.assertEqual(summary["skipped"], [(1, intro_source.SKIP_FAILED)])
        self.assertEqual(summary["filled"], 1)
        self.assertEqual(prepared[0]["intro_source"], "")
        self.assertTrue(prepared[1]["intro_source"])

    # 中止は区間の切れ目で効く (取得済みのぶんは活きる)
    def test_cancel_stops_before_the_next_range(self):
        prepared = [{"index": 1, "start": 140.0, "intro_source": ""},
                    {"index": 2, "start": 990.0, "intro_source": ""}]
        edited = [{"index": 1, "theme": "A", "use": True},
                  {"index": 2, "theme": "B", "use": True}]
        timeline = _timeline(
            clips=[{"index": 1, "media_id": "m1", "vod_start": 140.0, "vod_end": 540.0},
                   {"index": 2, "media_id": "m2", "vod_start": 990.0, "vod_end": 1170.0}],
            media=[{"media_id": "m1", "vod_start": 140.0, "vod_end": 540.0},
                   {"media_id": "m2", "vod_start": 990.0, "vod_end": 1170.0}])
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 1        # 2 区間目に入る前に中止する

        summary = intro_source.ensure_intro_sources(
            prepared, edited, timeline, _settings(), media_dir=self.media_dir,
            workdir=self.workdir, ffmpeg_cfg=_FFMPEG_CFG, cancel_cb=cancel)

        self.assertEqual(len(self.fetched), 1)
        self.assertEqual(summary["filled"], 1)


if __name__ == "__main__":
    unittest.main()
