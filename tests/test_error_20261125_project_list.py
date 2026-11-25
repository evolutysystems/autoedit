# AI 編集で保存したプロジェクトが一覧・「続きから」に出ない件 (docs/error/20261125)
# 実行: python -m unittest discover -s tests
#
# 要件 (docs/error/20261125/resolve.md §6):
#   ・履歴から拾ったパスの種別は「どちらの履歴に入っていたか」で決める (R1)
#     AI 編集の名前 (.ai_<出力先>) は archive_suffix で終わらないため、
#     ファイル名で判定するとアーカイブの一覧から消える
#   ・走査フォルダから拾ったものは従来どおりファイル名で判定する (R1)
#   ・AI 編集の完了時に作られたプロジェクトを履歴へ積む (R2)
#     縦を作った出力先 (shorts / tiktok) は縦だけを recent へ積んで
#     クリップ用タブで開けること。縦が無い出力先 (youtube_long) は横を
#     recent_archive へ積むこと
#   ・run_plan が採点グラフの窓スコア列 (curve) を保存まで運ぶ (R3)
# ffmpeg も ffprobe も AI も呼ばない。
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.gui import archive_tab
from src.timeline import project_io

# 既存のテストヘルパを使い回す (設定の形・経路 D の下ごしらえを 2 通り持たないため)
from test_ai_runner import RunnerBase, _plan, _scene
from test_project_library import _settings

# AI 編集が作る名前 (ai_runner._default_project_path の name_suffix=".ai_<出力先>")
_AI_NAMES = ("vod_1.ai_youtube_long.timeline.json",
             "vod_1.ai_youtube_shorts.timeline.json",
             "vod_1.ai_tiktok.timeline.json")

# 縦ターゲットで一緒に作られる縦プロジェクト (ai_runner._make_vertical の
# name_suffix = crop.project_suffix "_vertical")。中身は source.archive を持たない
# = クリップ用である。
_VERTICAL_NAMES = ("vod_1.ai_youtube_shorts.timeline_vertical.timeline.json",
                   "vod_1.ai_tiktok.timeline_vertical.timeline.json")


# scan_projects は中身を読まないため空のファイルで足りる
def _touch(directory, name):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}")

    return path


class ScanProjectsKindTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    # 走査フォルダを持たない設定 (履歴だけが一覧の元になる = 本件の環境)
    def _history_settings(self, recent=(), recent_archive=()):
        settings = _settings(self.root, recent=list(recent),
                             recent_archive=list(recent_archive))
        settings["general"]["output_directory"] = ""
        settings["timeline"]["project_dir"] = ""

        return settings

    def _scan(self, settings, kind=None):
        return [p.lower()
                for p in project_io.scan_projects(settings, kind=kind)["paths"]]

    # T1: recent_archive に積んだ AI 編集のプロジェクトがアーカイブの一覧に出る
    def test_ai_project_in_recent_archive_is_listed_as_archive(self):
        path = _touch(self.root, _AI_NAMES[0])
        settings = self._history_settings(recent_archive=[path])
        self.assertEqual(self._scan(settings, project_io.KIND_ARCHIVE),
                         [path.lower()])

    # T2: 同じパスはクリップ用の一覧には出ない
    def test_ai_project_is_not_listed_as_clip(self):
        path = _touch(self.root, _AI_NAMES[0])
        settings = self._history_settings(recent_archive=[path])
        self.assertEqual(self._scan(settings, project_io.KIND_CLIP), [])

    # T3: recent (クリップ用) に積んだものは従来どおりクリップ用にだけ出る
    def test_recent_clip_project_stays_clip(self):
        path = _touch(self.root, "movie.timeline.json")
        settings = self._history_settings(recent=[path])
        self.assertEqual(self._scan(settings, project_io.KIND_CLIP), [path.lower()])
        self.assertEqual(self._scan(settings, project_io.KIND_ARCHIVE), [])

    # T3-2: .archive の名前でも recent (クリップ用) に積んであればクリップ用として出る
    #       = 積んだ側の申告をファイル名より優先する
    def test_recent_key_wins_over_name(self):
        path = _touch(self.root, "vod_2.archive.timeline.json")
        settings = self._history_settings(recent=[path])
        self.assertEqual(self._scan(settings, project_io.KIND_CLIP), [path.lower()])
        self.assertEqual(self._scan(settings, project_io.KIND_ARCHIVE), [])

    # T4: 走査フォルダから拾ったものは従来どおりファイル名で判定する
    def test_scanned_folder_still_judged_by_name(self):
        archive = _touch(self.root, "vod_3.archive.timeline.json")
        clip = _touch(self.root, "movie2.timeline.json")
        ai_named = _touch(self.root, _AI_NAMES[1])
        settings = _settings(self.root)      # output_directory = root を走査する
        self.assertEqual(self._scan(settings, project_io.KIND_ARCHIVE),
                         [archive.lower()])
        # 履歴に無い AI 編集の名前は従来どおりクリップ用と判定される
        # (一覧では project_library_dialog が中身を見て取り除く)
        self.assertEqual(sorted(self._scan(settings, project_io.KIND_CLIP)),
                         sorted([clip.lower(), ai_named.lower()]))

    # T5: kind を渡さなければ履歴・走査フォルダのすべてが返る
    def test_no_kind_returns_everything(self):
        in_recent = _touch(self.root, _AI_NAMES[0])
        on_disk = _touch(self.root, "movie3.timeline.json")
        settings = _settings(self.root, recent_archive=[in_recent])
        self.assertEqual(sorted(self._scan(settings)),
                         sorted([in_recent.lower(), on_disk.lower()]))


# 画面を作らずに _remember_ai_projects だけを借りる器。
# ArchiveTabWidget は QWidget であり、一覧・コンボまで組むと重いため。
class _TabStub:

    _remember_ai_projects = archive_tab.ArchiveTabWidget._remember_ai_projects

    def __init__(self, settings):
        self._settings = settings
        self.resume_row = None


class RememberAiProjectsTest(unittest.TestCase):

    def _paths(self):
        return [os.path.join(os.sep + "work", name) for name in _AI_NAMES]

    def _vertical_paths(self):
        return [os.path.join(os.sep + "work", name) for name in _VERTICAL_NAMES]

    def _settings(self):
        return {"timeline": {"project": {"recent": [], "recent_archive": [],
                                         "recent_limit": 10}}}

    # T6: 横のプロジェクトを recent_archive へ積み、1 本目を先頭にする
    def test_all_projects_are_remembered_with_first_on_top(self):
        settings = self._settings()
        tab = _TabStub(settings)
        paths = self._paths()
        with mock.patch.object(archive_tab, "save_settings") as saved:
            tab._remember_ai_projects(paths)
        saved.assert_called_once_with(settings)
        project = settings["timeline"]["project"]
        self.assertEqual([project_io.recent_key(p) for p in project["recent_archive"]],
                         [project_io.recent_key(p) for p in paths])
        # 縦が無ければクリップ用の履歴は触らない
        self.assertEqual(project["recent"], [])

    # T8: 縦 (shorts / tiktok 用) は recent へ積む = クリップ用タブで開ける
    def test_vertical_projects_go_to_clip_history(self):
        settings = self._settings()
        tab = _TabStub(settings)
        vertical = self._vertical_paths()
        with mock.patch.object(archive_tab, "save_settings"):
            tab._remember_ai_projects(self._paths(), vertical)
        project = settings["timeline"]["project"]
        self.assertEqual([project_io.recent_key(p) for p in project["recent"]],
                         [project_io.recent_key(p) for p in vertical])
        # 横はアーカイブ用のまま (縦が混ざらない)
        self.assertEqual([project_io.recent_key(p) for p in project["recent_archive"]],
                         [project_io.recent_key(p) for p in self._paths()])

    # T8-2: 積んだ縦プロジェクトがクリップ用の一覧に出る (種別が recent で決まる)
    def test_vertical_project_is_listed_as_clip(self):
        with tempfile.TemporaryDirectory() as root:
            path = _touch(root, _VERTICAL_NAMES[0])
            settings = _settings(root, recent=[path])
            settings["general"]["output_directory"] = ""
            settings["timeline"]["project_dir"] = ""
            listed = project_io.scan_projects(
                settings, kind=project_io.KIND_CLIP)["paths"]
            self.assertEqual([p.lower() for p in listed], [path.lower()])

    # 履歴の書き戻しに失敗しても例外を外へ出さない (編集結果は残っている)
    def test_save_failure_is_swallowed(self):
        settings = {"timeline": {"project": {"recent": [], "recent_archive": []}}}
        tab = _TabStub(settings)
        with mock.patch.object(archive_tab, "save_settings",
                               side_effect=OSError("disk full")):
            tab._remember_ai_projects(self._paths()[:1])
        self.assertEqual(len(settings["timeline"]["project"]["recent_archive"]), 1)

    # 積むものが無ければ設定を書き戻さない
    def test_nothing_to_remember_does_not_save(self):
        settings = {"timeline": {"project": {"recent": [], "recent_archive": []}}}
        tab = _TabStub(settings)
        with mock.patch.object(archive_tab, "save_settings") as saved:
            tab._remember_ai_projects([])
        saved.assert_not_called()


class RunPlanCurveTest(RunnerBase):

    def _saved_curve(self, result):
        with open(result.project_path, encoding="utf-8") as handle:
            data = json.load(handle)

        return ((data.get("source") or {}).get("archive") or {}).get("curve")

    # T7: run_plan が渡された curve を source.archive.curve まで運ぶ
    def test_curve_is_saved_into_project(self):
        curve = [{"start": index * 45.0, "end": index * 45.0 + 180.0,
                  "total": 10.0 + index} for index in range(6)]
        result = self.run_plan(_plan([_scene("sc1", 100.0, 130.0)]), curve=curve)
        saved = self._saved_curve(result)
        self.assertTrue(
            saved, "source.archive.curve が空のままである (採点グラフの折れ線が出ない)")
        self.assertEqual(saved[0]["total"], curve[0]["total"])

    # curve を渡さない従来の呼び方でも落ちない (既定は空)
    def test_curve_is_optional(self):
        result = self.run_plan(_plan([_scene("sc1", 100.0, 130.0)]))
        self.assertEqual(self._saved_curve(result), [])


# AiRunResult の代わり (履歴へ積むものを選ぶのに要るのは 2 つのパスだけ)
class _Run:

    def __init__(self, project_path, vertical_path=""):
        self.project_path = project_path
        self.vertical_path = vertical_path


class AiHistoryTargetsTest(unittest.TestCase):

    def _runs(self):
        work = os.sep + "work"
        return [
            _Run(os.path.join(work, _AI_NAMES[0])),
            _Run(os.path.join(work, _AI_NAMES[1]),
                 os.path.join(work, _VERTICAL_NAMES[0])),
            _Run(os.path.join(work, _AI_NAMES[2]),
                 os.path.join(work, _VERTICAL_NAMES[1])),
        ]

    # T9: 縦がある出力先は縦だけ、縦が無い出力先は横を積む
    def test_vertical_targets_contribute_only_vertical(self):
        archive_paths, clip_paths = (
            archive_tab.ArchiveTabWidget._ai_history_targets(self._runs()))
        self.assertEqual([os.path.basename(p) for p in archive_paths],
                         [_AI_NAMES[0]])
        self.assertEqual([os.path.basename(p) for p in clip_paths],
                         list(_VERTICAL_NAMES))

    # T9-2: 縦を作れなかった出力先は横を積む (どこからも開けない結果にしない)
    def test_failed_vertical_falls_back_to_landscape(self):
        runs = self._runs()
        runs[1].vertical_path = ""      # 縦を作れなかった
        archive_paths, clip_paths = (
            archive_tab.ArchiveTabWidget._ai_history_targets(runs))
        self.assertEqual(sorted(os.path.basename(p) for p in archive_paths),
                         sorted([_AI_NAMES[0], _AI_NAMES[1]]))
        self.assertEqual([os.path.basename(p) for p in clip_paths],
                         [_VERTICAL_NAMES[1]])

    def test_no_runs_gives_empty_lists(self):
        self.assertEqual(
            archive_tab.ArchiveTabWidget._ai_history_targets(None), ([], []))


if __name__ == "__main__":
    unittest.main()
