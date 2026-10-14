# プロキシのタイル生成 (src/timeline/proxy_render.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver6/resolve3.md §8.1):
#   ・タイル境界がフレーム境界に乗ること (tile_sec が fps の整数倍でなくても)
#   ・内容ハッシュ (tile_key) が「内容が同じなら同じ」になること。
#     これが崩れると、編集したのに古い絵が残る / 無関係なタイルまで作り直しになる。
#   ・Undo で元へ戻せばキャッシュが効き直すこと (スナップショット方式との相性 §1.5)
#   ・タイル境界を跨ぐクリップ・オーバーレイの区間とオフセットが正しいこと
# FFmpeg は起動しない。コマンドの組み立てと鍵の計算だけを検証する。
import copy
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import subtitle_generator
from src.timeline import commands, compositor, crop, proxy_render
from src.timeline.model import (
    AudioClip,
    Clip,
    MediaRef,
    SubtitleClip,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    Timeline,
    Track,
    Transform,
)

_FPS = 60
_TILE_SEC = 4.0

# タイルの ASS を書く場所 (テストの作業場。リポジトリへ残さない)
_WORK = []


def setUpModule():
    _WORK.append(tempfile.mkdtemp(prefix="proxy_render_test_"))


def tearDownModule():
    for path in _WORK:
        shutil.rmtree(path, ignore_errors=True)
    _WORK.clear()


def _timeline(crop_layout=None):
    media = [
        MediaRef("m1", "video", __file__, 100.0, 1920, 1080, _FPS, True),
        MediaRef("m2", "video", __file__, 100.0, 1280, 720, _FPS, True),
        MediaRef("img1", "image", __file__, None, 400, 300, 0.0, False),
    ]
    base = Track("V1", TRACK_VIDEO, 1, is_base=True, clips=[
        # 0〜6s (タイル 0 と 1 に跨る) / 7〜10s (ギャップを挟む)
        Clip("c1", "m1", 0.0, 6.0, 2.0),
        Clip("c2", "m1", 7.0, 3.0, 20.0),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2", gain_db=-6.0),
    ])
    overlay = Track("V2", TRACK_VIDEO, 2, clips=[
        # タイル 0 と 1 に跨る動画オーバーレイ
        Clip("o1", "m2", 3.0, 3.0, 1.0, z_order=10,
             transform=Transform(x=0.0, y=0.0, scale=0.5)),
        # 画像オーバーレイ (タイル 1 にだけ乗る)
        Clip("o2", "img1", 5.0, 2.0, 0.0, z_order=11,
             transform=Transform(x=0.5, y=0.5, scale=0.25)),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, clips=[
        SubtitleClip("s1", 1.0, 2.0, "タイル 0 の字幕"),
        SubtitleClip("s2", 3.5, 2.0, "境界を跨ぐ字幕"),
    ])
    source = {"media_id": "m1", "input_path": __file__}
    if crop_layout is not None:
        source[crop.KEY] = crop_layout
    return Timeline(fps=_FPS, width=1920, height=1080, source=source,
                    media_pool=media, tracks=[base, audio, overlay, subtitle])


def _settings(**overrides):
    settings = {
        "ffmpeg": {"output_fps": _FPS, "audio_sample_rate": 48000},
        "silence_cut": {"fade_enabled": True, "fade_duration_sec": 0.05},
        "subtitle": {}, "vertical": {}, "watermark": {}, "blur": {},
    }
    settings.update(overrides)
    return settings


def _env(timeline, settings=None, work_dir=None, scale=0.5, **overrides):
    settings = settings or _settings()
    profile = compositor.CanvasProfile.proxy(timeline, settings["ffmpeg"], scale)
    eff_cfg = subtitle_generator.build_effective_subtitle_cfg({}, {}, None)
    kwargs = {
        "eff_cfg": eff_cfg,
        "font_profile": subtitle_generator.build_font_profile(eff_cfg),
    }
    kwargs.update(overrides)
    return proxy_render.ProxyEnvironment(
        settings, profile, {"tile_sec": _TILE_SEC, "preset": "ultrafast", "crf": 28,
                            "audio_bitrate": "96k"},
        work_dir or _WORK[0], **kwargs)


class TileRangeTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _timeline()

    def test_tiles_are_contiguous_and_on_frame_boundaries(self):
        previous_end = 0.0
        for index in range(proxy_render.tile_count(self.timeline, _TILE_SEC, _FPS)):
            start, end = proxy_render.tile_range(
                self.timeline, index, _TILE_SEC, _FPS)
            self.assertAlmostEqual(start, previous_end, places=9,
                                   msg="タイルの間に隙間 / 重なりがあります")
            for value in (start, end):
                self.assertAlmostEqual(value * _FPS, round(value * _FPS), places=6,
                                       msg=f"{value} がフレーム境界に乗っていません")
            previous_end = end
        self.assertAlmostEqual(previous_end, self.timeline.duration_sec(), places=6)

    # tile_sec が fps の整数倍でなくても成立すること (フレーム番号から逆算するため)
    def test_odd_tile_sec_still_lands_on_frames(self):
        tile_sec = 1.0 / 7.0 * 10.0        # 1.428571… 秒
        previous_end = 0.0
        for index in range(proxy_render.tile_count(self.timeline, tile_sec, _FPS)):
            start, end = proxy_render.tile_range(
                self.timeline, index, tile_sec, _FPS)
            self.assertAlmostEqual(start, previous_end, places=9)
            self.assertAlmostEqual(end * _FPS, round(end * _FPS), places=6)
            previous_end = end
        self.assertAlmostEqual(previous_end, self.timeline.duration_sec(), places=6)

    # 末尾のタイルは Timeline の終わりで切り詰める
    def test_last_tile_is_clipped_to_the_timeline(self):
        count = proxy_render.tile_count(self.timeline, _TILE_SEC, _FPS)
        _start, end = proxy_render.tile_range(
            self.timeline, count - 1, _TILE_SEC, _FPS)

        self.assertAlmostEqual(end, self.timeline.duration_sec(), places=6)
        # 範囲外のタイルは空になる (作る必要が無い)
        beyond = proxy_render.tile_range(self.timeline, count + 5, _TILE_SEC, _FPS)
        self.assertEqual(beyond[0], beyond[1])

    def test_tile_index_at(self):
        self.assertEqual(proxy_render.tile_index_at(0.0, _TILE_SEC), 0)
        self.assertEqual(proxy_render.tile_index_at(3.999, _TILE_SEC), 0)
        self.assertEqual(proxy_render.tile_index_at(4.0, _TILE_SEC), 1)
        self.assertEqual(proxy_render.tile_index_at(-5.0, _TILE_SEC), 0)

    def test_tile_count_of_empty_timeline(self):
        empty = Timeline(fps=_FPS, width=1920, height=1080, source={}, media_pool=[])

        self.assertEqual(proxy_render.tile_count(empty, _TILE_SEC, _FPS), 0)


class TileKeyTest(unittest.TestCase):
    """§8.1 proxy-2〜5: 内容が同じなら鍵が同じ / 変わったタイルだけ鍵が変わる"""

    def setUp(self):
        self.timeline = _timeline()
        self.env = _env(self.timeline)

    def _keys(self):
        return [proxy_render.tile_key(self.timeline, self.env, index)
                for index in range(3)]

    def test_key_is_stable(self):
        self.assertEqual(self._keys(), self._keys())

    def test_keys_differ_between_tiles(self):
        keys = self._keys()
        self.assertEqual(len(set(keys)), len(keys), "タイルごとに鍵が違うこと")

    # クリップを 1 つ動かしたら、重なるタイルの鍵だけが変わる
    def test_moving_a_clip_changes_only_the_overlapping_tiles(self):
        before = self._keys()
        stack = commands.CommandStack()
        # 7.0 → 8.0 へ動かす (タイル 1 と 2 に関わり、タイル 0 には関わらない)
        self.assertTrue(stack.push(self.timeline, commands.MoveClip("c2", 8.0)))
        after = self._keys()

        self.assertEqual(before[0], after[0], "関係ないタイルの鍵が変わっています")
        self.assertNotEqual(before[1], after[1])
        self.assertNotEqual(before[2], after[2])

    # Undo で元へ戻せば鍵も戻る = キャッシュが効き直す (§1.5 / §3.5)
    def test_undo_restores_the_keys(self):
        before = self._keys()
        stack = commands.CommandStack()
        stack.push(self.timeline, commands.MoveClip("c2", 8.0))
        self.assertNotEqual(before, self._keys())
        stack.undo(self.timeline)

        self.assertEqual(before, self._keys(), "Undo しても鍵が戻っていません")

    # 字幕の文字を変えたら、その字幕が乗るタイルの鍵が変わる
    def test_editing_a_subtitle_changes_its_tile(self):
        before = self._keys()
        clip = self.timeline.clip_by_id("s1")
        clip.text = "書き換えた字幕"
        after = self._keys()

        self.assertNotEqual(before[0], after[0])
        self.assertEqual(before[2], after[2])

    # オーバーレイの位置を変えたら、またがる両方のタイルの鍵が変わる
    def test_moving_an_overlay_changes_both_tiles(self):
        before = self._keys()
        stack = commands.CommandStack()
        self.assertTrue(stack.push(self.timeline, commands.MoveOverlay("o1", 0.3, 0.2)))
        after = self._keys()

        self.assertNotEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])

    # 透かしの要否が変わったら全タイルの鍵が変わる (§4.5)
    def test_watermark_flag_changes_every_key(self):
        before = self._keys()
        self.env.watermark_required = True
        after = self._keys()

        self.assertTrue(all(a != b for a, b in zip(before, after)))

    # ぼかしの判定が変わったら全タイルの鍵が変わる
    def test_blur_signature_changes_every_key(self):
        before = self._keys()
        self.env.blur_signature = "mask-v2"
        after = self._keys()

        self.assertTrue(all(a != b for a, b in zip(before, after)))

    # プロキシ寸法が変わったら鍵が変わる (作り直しになる)
    def test_profile_changes_the_key(self):
        before = self._keys()
        self.env = _env(self.timeline, scale=0.25)

        self.assertNotEqual(before, self._keys())

    # 絵に影響する設定を変えたら鍵が変わる
    def test_relevant_settings_change_the_key(self):
        before = self._keys()
        self.env = _env(self.timeline,
                        settings=_settings(subtitle={"font_size": 72}))
        self.assertNotEqual(before, self._keys())

        self.env = _env(self.timeline,
                        settings=_settings(vertical={"crop": {"blur_radius": 40}}))
        self.assertNotEqual(before, self._keys())

        self.env = _env(self.timeline,
                        settings=_settings(watermark={"scale_ratio": 0.4}))
        self.assertNotEqual(before, self._keys())

    # 絵に関係ない設定では鍵が変わらない (無関係な設定変更で全部作り直しにしない)
    def test_irrelevant_settings_keep_the_key(self):
        before = self._keys()
        self.env = _env(self.timeline,
                        settings=_settings(timeline={"preview": {"play_fps": 15}}))

        self.assertEqual(before, self._keys())

    # 切り抜き指定を変えたら鍵が変わる
    def test_crop_layout_changes_the_key(self):
        before = self._keys()
        media = self.timeline.media_by_id("m1")
        layout = crop.make_layout(crop.MODE_SINGLE, [(420, 0, 1080, 1080)],
                                  media, 1080, 1920)
        self.timeline.source[crop.KEY] = layout

        self.assertNotEqual(before, self._keys())


class PlanTileTest(unittest.TestCase):
    """コマンドの組み立て (§4.2)。ffmpeg は起動しない"""

    def setUp(self):
        self.timeline = _timeline()
        self.env = _env(self.timeline)

    def _plan(self, index):
        command = proxy_render.plan_tile(
            self.timeline, self.env, index, f"out{index}.mp4")
        self.assertIsNotNone(command, f"タイル {index} のコマンドが組めていません")
        return command

    def _graph(self, index):
        command = self._plan(index)
        return command[command.index("-filter_complex") + 1]

    # タイル index の断片 label を作るチェーン 1 本を返す
    def _piece(self, index, label):
        for chain in self._graph(index).split(";"):
            if chain.endswith(f"[{label}]"):
                return chain
        self.fail(f"タイル {index} に断片 {label} がありません")
        return ""

    # タイルの尺ぶんだけ書き出す (-t)
    def test_duration_matches_the_tile(self):
        command = self._plan(0)
        self.assertEqual(command[command.index("-t", command.index("-map")) + 1],
                         "4.000000")
        last = self._plan(2)
        start, end = proxy_render.tile_range(self.timeline, 2, _TILE_SEC, _FPS)
        self.assertEqual(last[last.index("-t", last.index("-map")) + 1],
                         f"{end - start:.6f}")

    # プロキシ寸法・fps・エンコード指定
    def test_encode_options(self):
        command = self._plan(0)

        self.assertIn("-c:v", command)
        self.assertEqual(command[command.index("-preset") + 1], "ultrafast")
        self.assertEqual(command[command.index("-crf") + 1], "28")
        self.assertEqual(command[command.index("-r") + 1], str(_FPS),
                         "fps は本番と同じにする (§3.2)")
        self.assertEqual(command[command.index("-b:a") + 1], "96k")
        self.assertIn("+faststart", command)

    # 断片ごとに -ss / -t 付きの入力を並べる (入力シークを効かせる / §1.6)
    def test_pieces_use_input_seek(self):
        command = self._plan(1)
        # タイル 1 は 4〜8s: c1 の 4〜6s (素材 6〜8s) → ギャップ → c2 の 7〜8s
        self.assertIn("-ss", command)
        self.assertEqual(command[command.index("-ss") + 1], "6.000")
        self.assertIn("color=c=black:s=960x540:r=60", " ".join(command))

    # ギャップは黒 + 無音で埋める
    def test_gap_is_black_and_silent(self):
        text = " ".join(self._plan(1))

        self.assertIn("color=c=black", text)
        self.assertIn("anullsrc=channel_layout=stereo", text)

    # 複数断片は concat で 1 本へ繋ぐ
    def test_pieces_are_concatenated(self):
        graph = self._graph(1)

        self.assertIn("concat=n=", graph)
        self.assertIn(":v=1:a=1[cv][ca]", graph)

    # 断片のラベルとオーバーレイの出力ラベルが衝突しないこと。
    # 衝突すると filtergraph の意味が変わる (同じ名前の後勝ちになる)。
    def test_labels_are_unique(self):
        import re
        for index in range(3):
            graph = self._graph(index)
            outputs = re.findall(r"\[([a-z0-9]+)\](?=;|$)", graph)
            self.assertEqual(len(outputs), len(set(outputs)),
                             f"タイル {index} で出力ラベルが重複しています: {outputs}")

    # §8.1 proxy-6: タイル 2 枚目以降は enable / setpts がタイル開始ぶんずれる
    def test_overlay_times_are_offset_by_the_tile_start(self):
        first = self._graph(0)
        second = self._graph(1)

        # o1 は Timeline 3〜6s。タイル 0 では 3〜4s、タイル 1 では 0〜2s に見える
        self.assertIn("enable='between(t,3.000,4.000)'", first)
        self.assertIn("setpts=PTS+3.000/TB", first)
        self.assertIn("enable='between(t,0.000,2.000)'", second)
        self.assertIn("setpts=PTS+0.000/TB", second)

    # §8.1 proxy-7: タイル境界に跨るオーバーレイは、各タイルで必要な区間だけ取る
    def test_overlay_input_is_clipped_to_the_tile(self):
        first = " ".join(self._plan(0))
        second = " ".join(self._plan(1))

        # o1 は素材 1.0s から。タイル 0 では 1.0s から 1.0 秒ぶん
        self.assertIn("-ss 1.000 -t 1.000", first)
        # タイル 1 では素材 2.0s から 2.0 秒ぶん
        self.assertIn("-ss 2.000 -t 2.000", second)

    # 画像オーバーレイはタイル内で必要な長さだけループする
    def test_image_overlay_duration_is_tile_local(self):
        second = " ".join(self._plan(1))

        # o2 は Timeline 5〜7s → タイル 1 (4〜8s) では 1〜3s に見える
        self.assertIn("-loop 1 -framerate 60 -t 3.000", second)

    # 字幕はタイル開始を 0 とした時刻で書き出す
    def test_subtitle_times_are_tile_local(self):
        import tempfile
        with tempfile.TemporaryDirectory() as work:
            env = _env(self.timeline, work_dir=work)
            proxy_render.plan_tile(self.timeline, env, 1, "out.mp4")
            path = os.path.join(work, "proxy_tile_00001.ass")
            self.assertTrue(os.path.exists(path), "タイルの ASS が書かれていません")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()

        # s2 は Timeline 3.5〜5.5s → タイル 1 (4〜8s) では 0〜1.5s
        self.assertIn("0:00:00.00", text)
        self.assertIn("0:00:01.50", text)
        self.assertNotIn("0:00:03.50", text)

    # フェードはクリップ全体を基準に判断する (タイルの継ぎ目で暗転させない)
    #
    # c1 は Timeline 0〜6s でタイル 0 / 1 に跨る。
    #   タイル 0 の断片 = クリップの先頭 → 入りだけ
    #   タイル 1 の断片 = クリップの末尾 → 出だけ
    # c2 (7〜10s) はタイル 1 から始まるクリップなので、そちらには入りが付く。
    def test_fade_follows_the_clip_not_the_tile(self):
        head = self._piece(0, "pcv0")
        tail = self._piece(1, "pcv0")
        next_clip = self._piece(1, "pcv2")

        self.assertIn("fade=t=in:st=0:d=0.05", head)
        self.assertNotIn("fade=t=out", head)
        self.assertIn("fade=t=out", tail)
        self.assertNotIn("fade=t=in", tail)
        # 別のクリップの先頭なので、こちらには入りが付く (出は付かない)
        self.assertIn("fade=t=in:st=0:d=0.05", next_clip)
        self.assertNotIn("fade=t=out", next_clip)

    # 透かしが要るときだけチェーンの最後へ入る
    def test_watermark_is_last(self):
        graph = self._graph(0)
        self.assertNotIn("[wm]", graph)

        self.env.watermark_required = True
        graph = self._graph(0)
        from src.modules import watermark_overlay
        if watermark_overlay.asset_path() is None:
            self.skipTest("透かし素材が無い環境では確認できない")
        self.assertIn("[wm]", graph)
        self.assertTrue(graph.rstrip().endswith("[pwm]")
                        or "[pwm]" in graph.split(";")[-2])

    # 空の Timeline では作らない
    def test_empty_timeline_plans_nothing(self):
        empty = Timeline(fps=_FPS, width=1920, height=1080, source={}, media_pool=[])

        self.assertIsNone(proxy_render.plan_tile(empty, _env(empty), 0, "x.mp4"))

    # 素材が見つからないクリップはギャップとして扱う (開ける / 書ける状態を保つ)
    def test_missing_media_becomes_a_gap(self):
        timeline = copy.deepcopy(self.timeline)
        timeline.media_by_id("m1").path = "C:/does/not/exist.mp4"
        command = proxy_render.plan_tile(timeline, _env(timeline), 0, "out.mp4")

        self.assertIsNotNone(command)
        self.assertIn("color=c=black", " ".join(command))


class ProxyEnvironmentTest(unittest.TestCase):

    # 0 / 未設定は既定 (4 秒) へ、下限より小さい値は下限へ丸める
    def test_tile_sec_falls_back_and_clamps(self):
        env = _env(_timeline())

        env.cfg["tile_sec"] = 0.0
        self.assertEqual(env.tile_sec, 4.0)
        env.cfg.pop("tile_sec")
        self.assertEqual(env.tile_sec, 4.0)
        env.cfg["tile_sec"] = 0.01
        self.assertEqual(env.tile_sec, proxy_render.MIN_TILE_SEC)


if __name__ == "__main__":
    unittest.main()
