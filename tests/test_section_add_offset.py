# 取り直した区間からセクションを足す経路の単体テスト (docs/request/ver9/resolve5.md §7.2)
# 実行: python -m unittest discover -s tests
#
# 方針: 「素材が元 VOD でも区間の取り直しでも、**同じ VOD 絶対秒**でセクションを扱う」
#
# 重点:
#   ・切り出しだけを素材の原点ぶん平行移動し、**保存する start/end は絶対秒のまま**に
#     すること。vod_start/vod_end は素材の復元に使う唯一のキーで、ここを相対秒に
#     すると開き直したときに復元が壊れる。
#   ・原点を渡さない従来の呼び出しが 1 ビットも変わらないこと。
#   ・取得元の控えが無い既存プロジェクトでも、ファイル名から取り直せること。
#   ・取り直せないときはボタンを押せる状態にしないこと。
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import clip_writer
from src.archive import timeline_builder as archive_timeline
from src.gui.timeline import archive_timeline_dialog as atd

_PROFILE = {"width": 1920, "height": 1080, "orientation": "landscape",
            "is_portrait": False}
_SETTINGS = {
    "subtitle": {"enabled": True, "engine": "whisper"},
    "vertical": {"enabled": True},
    "volume_analysis": {"last_cut_db": -27},
    "ffmpeg": {"output_width": 1920, "output_height": 1080, "output_fps": 60},
}
_KEEP = [(0.0, 5.0), (10.0, 15.0)]
_META = {"detector": "silencedetect", "source_duration_sec": 30.0}
_ITEMS = [{"start": 1.0, "end": 3.0, "text": "あ", "use": True,
           "role": "streamer", "font": "", "font_size": None}]

# 取り直した素材の原点 (実測値 / resolve5 §2.3)
_ORIGIN = 218.993


class PrepareOneClipOffsetTest(unittest.TestCase):

    def setUp(self):
        self._workdir_obj = tempfile.TemporaryDirectory(prefix="section_offset_test_")
        self.workdir = self._workdir_obj.name
        self.addCleanup(self._workdir_obj.cleanup)
        self.cuts = []

        def _cut(_input_path, start, end, dest, _ffmpeg_cfg):
            self.cuts.append((start, end))
            return dest

        self.patches = [
            mock.patch.object(clip_writer, "cut_region", side_effect=_cut),
            mock.patch.object(clip_writer.loudness_normalizer, "normalize_file",
                              return_value="normalized.mp4"),
            mock.patch.object(clip_writer, "_apply_volume_analysis",
                              return_value=None),
            mock.patch.object(clip_writer.output_profile, "resolve_output_profile",
                              return_value=_PROFILE),
            mock.patch.object(clip_writer.ffmpeg_runner, "probe_duration",
                              return_value=30.0),
            mock.patch.object(clip_writer.silence_cutter, "detect_edit_points",
                              return_value=(_KEEP, _META)),
            mock.patch.object(clip_writer.subtitle_generator, "recognize_for_timeline",
                              return_value=(_ITEMS, {"enabled": True})),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _prepare(self, **kwargs):
        clip = {"index": 7, "start": 225.0, "end": 405.0, "score": 55.0}
        return clip_writer.prepare_one_clip(
            "range.mp4", _SETTINGS, dict(_SETTINGS), clip,
            _SETTINGS["ffmpeg"], self.workdir, use_timeline=True, **kwargs)

    # 本題: 切り出しは素材内の相対秒で行う
    def test_cut_is_shifted_by_the_origin(self):
        self._prepare(source_origin_sec=_ORIGIN)
        self.assertEqual(len(self.cuts), 1)
        start, end = self.cuts[0]
        self.assertAlmostEqual(start, 225.0 - _ORIGIN, places=6)
        self.assertAlmostEqual(end, 405.0 - _ORIGIN, places=6)

    # 本題: 保存する start/end は VOD 絶対秒のまま (復元のキーであるため)
    def test_entry_keeps_absolute_seconds(self):
        entry = self._prepare(source_origin_sec=_ORIGIN)
        self.assertAlmostEqual(entry["start"], 225.0, places=6)
        self.assertAlmostEqual(entry["end"], 405.0, places=6)

    # 原点を渡さない従来の呼び出しは切り出し位置が変わらない
    def test_default_origin_keeps_the_old_behaviour(self):
        self._prepare()
        self.assertEqual(self.cuts, [(225.0, 405.0)])

    # 原点 0.0 を明示しても同じ (既定と差が無いこと)
    def test_zero_origin_is_the_same(self):
        self._prepare(source_origin_sec=0.0)
        self.assertEqual(self.cuts, [(225.0, 405.0)])

    # VOD 全体で測った音量をそのまま下へ渡すこと (追加セクションだけ基準が
    # 変わらないようにするため / resolve5 §4.4)
    def test_vod_analysis_is_passed_to_volume_analysis(self):
        with mock.patch.object(clip_writer, "_apply_volume_analysis") as applied:
            self._prepare(vod_analysis={"cut_db": -34, "measured": {"input_i": -20.0}})
        self.assertEqual(applied.call_args.kwargs.get("fixed_db"), -34)


class IntroSourceOffsetTest(unittest.TestCase):

    # イントロの素も素材内の位置へ直して切り出すこと。
    # clip_start は VOD 絶対秒で渡せる (呼び側で引き算させない)
    def test_intro_source_is_shifted_by_the_origin(self):
        seen = {}

        def _execute(command, **_kwargs):
            seen["ss"] = float(command[command.index("-ss") + 1])

        with tempfile.TemporaryDirectory(prefix="intro_offset_") as folder:
            dest = os.path.join(folder, "intro.mp4")
            source = os.path.join(folder, "range.mp4")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write("x")
            with mock.patch.object(clip_writer.ffmpeg_runner, "get_ffmpeg_exe",
                                   return_value="ffmpeg"), \
                    mock.patch.object(clip_writer.ffmpeg_runner, "build_encode_options",
                                      return_value=[]), \
                    mock.patch.object(clip_writer.ffmpeg_runner, "execute",
                                      side_effect=_execute):
                # 書き出せたふりをする (中身は見ない)
                with mock.patch.object(os.path, "getsize", return_value=1):
                    clip_writer.export_intro_source(
                        source, 225.0, 3.0, dest, {}, source_origin_sec=_ORIGIN)
        # 素材内では 225 - 218.993 - 3.0 = 3.007 秒の位置
        self.assertAlmostEqual(seen["ss"], 225.0 - _ORIGIN - 3.0, places=3)

    # 素材の先頭でバッファを確保できないなら作らない (従来どおり)
    def test_intro_source_is_skipped_at_the_head(self):
        self.assertIsNone(clip_writer.export_intro_source(
            "range.mp4", 220.0, 3.0, "dest.mp4", {}, source_origin_sec=_ORIGIN))


def _timeline(archive):
    return types.SimpleNamespace(source={"input_path": archive.get("vod_path", ""),
                                         "archive": archive})


class VodSourceRecordTest(unittest.TestCase):

    # 記録があればそれを使う
    def test_record_is_used(self):
        timeline = _timeline({"vod_path": r"C:\x\vod_111.mp4",
                              "vod_source": {"provider": "twitch",
                                             "video_id": "2910070011",
                                             "quality": "chunked",
                                             "vod_duration_sec": 8991.468}})
        resolved = archive_timeline.resolve_vod_source(timeline, {})
        self.assertEqual(resolved["video_id"], "2910070011")
        self.assertEqual(resolved["quality"], "chunked")
        self.assertAlmostEqual(resolved["vod_duration_sec"], 8991.468, places=3)
        self.assertTrue(resolved["from_record"])

    # 記録が無い既存プロジェクトはファイル名から推測する (後方互換 / §4.3)
    def test_falls_back_to_the_file_name(self):
        timeline = _timeline({"vod_path": r"C:\x\vod_2910070011.mp4"})
        resolved = archive_timeline.resolve_vod_source(timeline, {})
        self.assertEqual(resolved["video_id"], "2910070011")
        self.assertFalse(resolved["from_record"])
        # 画質は設定の希望値へ落とす
        self.assertEqual(resolved["quality"], "source")

    # 推測もできなければ取り直せない (video_id が空)
    def test_unknown_source_has_no_video_id(self):
        for path in (r"C:\x\my_movie.mp4", r"C:\x\vod_abc.mp4", ""):
            resolved = archive_timeline.resolve_vod_source(_timeline({"vod_path": path}), {})
            self.assertEqual(resolved["video_id"], "", path)

    def test_guess_video_id(self):
        self.assertEqual(archive_timeline.guess_video_id(r"C:\x\vod_123.mp4"), "123")
        self.assertEqual(archive_timeline.guess_video_id("vod_123.mkv"), "123")
        self.assertEqual(archive_timeline.guess_video_id("vod_.mp4"), "")
        self.assertEqual(archive_timeline.guess_video_id("movie.mp4"), "")

    # 音量の基準は prepare_one_clip へそのまま渡せる形で返る
    def test_vod_analysis_record(self):
        timeline = _timeline({"vod_analysis": {"cut_db": -34,
                                               "measured": {"input_i": -20.0}}})
        record = archive_timeline.vod_analysis_record(timeline)
        self.assertEqual(record["cut_db"], -34)
        self.assertEqual(record["measured"], {"input_i": -20.0})

    # 記録が無ければ None (= クリップごとに測る従来の挙動)
    def test_vod_analysis_record_is_none_without_record(self):
        self.assertIsNone(archive_timeline.vod_analysis_record(_timeline({})))

    # video_id の無い控えは残さない (取り直しに使えないため)
    def test_record_without_video_id_is_dropped(self):
        self.assertEqual(archive_timeline._trim_vod_source({"quality": "chunked"}), {})
        self.assertEqual(archive_timeline._trim_vod_source(None), {})

    # 控えは保存に要る欄だけを写す
    def test_record_is_trimmed(self):
        trimmed = archive_timeline._trim_vod_source(
            {"video_id": "1", "quality": "chunked", "vod_duration_sec": 10.0,
             "provider": "twitch", "extra": "x"})
        self.assertEqual(set(trimmed), {"provider", "video_id", "quality",
                                        "vod_duration_sec"})


class SectionAddAvailabilityTest(unittest.TestCase):

    # resolve5 §4.6 の表どおりに可否が決まることを確かめる
    def _dialog(self, vod_exists=False, video_id="", refetch=True, enabled=True,
                workdir="work", clip_settings=None):
        class _Dialog:
            _has_local_vod = atd.ArchiveTimelineDialog._has_local_vod
            _refetch_available = atd.ArchiveTimelineDialog._refetch_available
            _section_add_available = atd.ArchiveTimelineDialog._section_add_available
            _section_add_blocked_by_missing_source = \
                atd.ArchiveTimelineDialog._section_add_blocked_by_missing_source

            def __init__(inner):
                inner._section_cfg = {"enabled": enabled,
                                      "refetch": {"enabled": refetch}}
                inner._clip_workdir = workdir
                inner._clip_settings = ({"ffmpeg": {}} if clip_settings is None
                                        else clip_settings)
                inner._source_path = self.vod if vod_exists else "no_such_vod.mp4"
                inner._vod_source = {"video_id": video_id, "quality": "source"}

        return _Dialog()

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(prefix="section_available_")
        self.addCleanup(self._folder.cleanup)
        self.vod = os.path.join(self._folder.name, "vod_1.mp4")
        with open(self.vod, "w", encoding="utf-8") as handle:
            handle.write("x")

    # 元 VOD があれば従来どおり使える
    def test_available_with_local_vod(self):
        dialog = self._dialog(vod_exists=True)
        self.assertTrue(dialog._section_add_available())
        self.assertFalse(dialog._section_add_blocked_by_missing_source())

    # 本題: 元 VOD が無くても取得元が分かれば使える
    def test_available_by_refetch(self):
        dialog = self._dialog(video_id="2910070011")
        self.assertTrue(dialog._section_add_available())
        self.assertFalse(dialog._section_add_blocked_by_missing_source())

    # 取得元が分からなければ使えない (理由を出すため blocked になる)
    def test_blocked_without_video_id(self):
        dialog = self._dialog()
        self.assertFalse(dialog._section_add_available())
        self.assertTrue(dialog._section_add_blocked_by_missing_source())

    # 取り直しを設定で切っていれば、元 VOD が無い限り使えない
    def test_blocked_when_refetch_is_disabled(self):
        dialog = self._dialog(video_id="2910070011", refetch=False)
        self.assertFalse(dialog._section_add_available())
        self.assertTrue(dialog._section_add_blocked_by_missing_source())
        # 元 VOD があれば取り直しの設定に関わらず使える
        self.assertTrue(self._dialog(vod_exists=True, refetch=False)
                        ._section_add_available())

    # 機能そのものを切っていればボタンを出さない (blocked でもない)
    def test_disabled_feature_shows_nothing(self):
        dialog = self._dialog(video_id="2910070011", enabled=False)
        self.assertFalse(dialog._section_add_available())
        self.assertFalse(dialog._section_add_blocked_by_missing_source())

    # 素材の置き場が渡らない経路 (CLI など) では出さない
    def test_without_workdir_shows_nothing(self):
        dialog = self._dialog(vod_exists=True, workdir="")
        self.assertFalse(dialog._section_add_available())
        self.assertFalse(dialog._section_add_blocked_by_missing_source())


if __name__ == "__main__":
    unittest.main()
