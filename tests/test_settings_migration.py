# 設定の既定補完・レガシー値の自己修復 (src/settings/settings_window.py) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 重点 (error 20260820):
#   ・更新インストールではインストーラが旧 setting.json を復元するため、
#     配布 setting.json にしか無い値は新しい版へ更新しても届かない。
#     アプリ側 (DEFAULT_SETTINGS + 起動時の補完) で自己修復できることを守る。
#   ・利用者が意図して入れた値は上書きしないこと。
import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import comment_decor, subtitle_generator
from src.settings import settings_window as sw

# アイコン位置の確認に使うコメント役割の字幕 (resolve14 §10-3)
_COMMENT = {"start": 3.1, "end": 5.4, "role": "comment",
            "text": "ここにコメント"}


def _existing(**auth_overrides):
    # 既存インストールの setting.json 相当 (キーは在るが値が古い状態)
    data = copy.deepcopy(sw.DEFAULT_SETTINGS)
    data["archive"] = copy.deepcopy(data["archive"])
    data["archive"]["auth"] = dict(data["archive"]["auth"], **auth_overrides)
    return data


class TwitchClientIdTest(unittest.TestCase):

    def setUp(self):
        self.default_id = sw.DEFAULT_SETTINGS["archive"]["auth"]["client_id"]

    # 配布物は Client-ID を持っていること (空だとログインできない)
    def test_default_settings_has_client_id(self):
        self.assertTrue(self.default_id,
                        "DEFAULT_SETTINGS に Twitch Client-ID がありません")

    # 更新環境 (空のまま残っている) は起動時に既定で埋まる
    def test_empty_client_id_is_filled(self):
        merged = sw._merge_with_defaults(_existing(client_id=""))
        # マージだけでは既存の空文字が残る (キーが在るため既定が届かない)
        self.assertEqual(merged["archive"]["auth"]["client_id"], "")
        changed = sw._normalize_legacy_values(merged)
        self.assertTrue(changed, "補完した場合は永続化のため True を返すこと")
        self.assertEqual(merged["archive"]["auth"]["client_id"], self.default_id)

    # 空白だけの値も未設定とみなす
    def test_whitespace_client_id_is_filled(self):
        merged = sw._merge_with_defaults(_existing(client_id="   "))
        sw._normalize_legacy_values(merged)
        self.assertEqual(merged["archive"]["auth"]["client_id"], self.default_id)

    # 利用者が自前の Client-ID を入れている場合は上書きしない
    def test_user_client_id_is_kept(self):
        merged = sw._merge_with_defaults(_existing(client_id="my-own-client-id"))
        sw._normalize_legacy_values(merged)
        self.assertEqual(merged["archive"]["auth"]["client_id"], "my-own-client-id")

    # archive セクションごと無い古いファイルでも落ちない
    def test_missing_archive_section(self):
        merged = sw._merge_with_defaults({"general": {}})
        sw._normalize_legacy_values(merged)
        self.assertEqual(merged["archive"]["auth"]["client_id"], self.default_id)

    # 補完すべきものが無ければ False (無用な保存を起こさない)
    def test_no_change_reports_false(self):
        merged = sw._merge_with_defaults(_existing())
        self.assertFalse(sw._fill_twitch_client_id(merged))


class LegacyFfmpegPathTest(unittest.TestCase):

    # 同じ経路で直している既存の自己修復 (20260708) が壊れていないこと
    def test_bare_ffmpeg_name_is_normalized(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["ffmpeg"] = dict(data["ffmpeg"], executable="ffmpeg",
                              ffprobe_executable="ffprobe.exe")
        merged = sw._merge_with_defaults(data)
        self.assertTrue(sw._normalize_legacy_values(merged))
        self.assertEqual(merged["ffmpeg"]["executable"],
                         sw.DEFAULT_SETTINGS["ffmpeg"]["executable"])
        self.assertEqual(merged["ffmpeg"]["ffprobe_executable"],
                         sw.DEFAULT_SETTINGS["ffmpeg"]["ffprobe_executable"])


class NestedDefaultsTest(unittest.TestCase):

    # timeline の入れ子は新バージョンで増えたキーが補完される (ver3 resolve2 §6)
    def test_timeline_nested_keys_are_filled(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["timeline"] = copy.deepcopy(data["timeline"])
        data["timeline"]["project"] = {"save_button": True}      # 古い形
        merged = sw._merge_with_defaults(data)
        self.assertTrue(sw._normalize_legacy_values(merged))
        project = merged["timeline"]["project"]
        # resolve9 で増えたキーが埋まっていること
        for key in ("keep_audio", "delete_to_trash", "archive_suffix", "library"):
            self.assertIn(key, project, f"{key} が補完されていません")
        self.assertIn("max_items", project["library"])

    # コメント用の字幕トラック設定も入れ子として補完される (ver3 resolve11 §7.3)
    def test_subtitle_tracks_defaults_are_filled(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["timeline"] = copy.deepcopy(data["timeline"])
        data["timeline"].pop("subtitle_tracks", None)
        merged = sw._merge_with_defaults(data)
        tracks = merged["timeline"].get("subtitle_tracks")
        self.assertIsNotNone(tracks, "subtitle_tracks が補完されていません")
        self.assertEqual(tracks["comment_track_id"], "S2")


# コメント役割の先頭ラベル「コメント：」の撤去 (ver3 resolve11 C3 / §8-1)
class CommentLabelMigrationTest(unittest.TestCase):

    # 既定は空文字 (アイコン表示へ置き換えたため)
    def test_default_is_empty(self):
        self.assertEqual(sw.DEFAULT_SETTINGS["subtitle"]["comment_label"], "")

    # 旧既定値が残っている環境は起動時に空へ寄る
    def test_legacy_label_is_removed(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["subtitle"] = dict(data["subtitle"], comment_label="コメント：")
        merged = sw._merge_with_defaults(data)
        # マージだけでは既存値が残る (キーが在るため既定が届かない)
        self.assertEqual(merged["subtitle"]["comment_label"], "コメント：")
        self.assertTrue(sw._normalize_legacy_values(merged))
        self.assertEqual(merged["subtitle"]["comment_label"], "")

    # 利用者が入れた独自の文言は尊重する (上書きしない)
    def test_custom_label_is_kept(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["subtitle"] = dict(data["subtitle"], comment_label="視聴者：")
        merged = sw._merge_with_defaults(data)
        sw._normalize_legacy_values(merged)
        self.assertEqual(merged["subtitle"]["comment_label"], "視聴者：")




class CommentIconSizeMigrationTest(unittest.TestCase):
    """ver3 resolve14 §10-3: コメントアイコンの拡大を旧 setting.json へ反映する

    アイコンの位置は x = comment_margin_l - gap - size で逆算されるため
    (resolve14 §2.2)、大きさと左余白は対で意味を持つ。片方だけ寄せると
    アイコンが画面端へはみ出す。移行は必ず 2 つまとめて行う。
    """

    # 旧既定値 (resolve14 以前)
    LEGACY = {
        "subtitle": {"comment_icon_size_px": 100, "comment_margin_l": 190},
        "vertical": {"comment_icon_size_px": 50, "comment_margin_l": 140},
    }

    def _legacy_settings(self, **overrides):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        for section, values in self.LEGACY.items():
            data[section] = dict(data[section], **values)
        for section, values in overrides.items():
            data[section] = dict(data[section], **values)
        return data

    def _new(self, section, key):
        return sw.DEFAULT_SETTINGS[section][key]

    # 旧既定のままなら新既定 (横 1.5 倍 / 縦 2 倍) へ寄せる
    def test_comment_icon_size_migrated(self):
        data = self._legacy_settings()
        self.assertTrue(sw._migrate_comment_icon_size(data))
        self.assertEqual(data["subtitle"]["comment_icon_size_px"], 150)
        self.assertEqual(data["subtitle"]["comment_margin_l"], 240)
        self.assertEqual(data["vertical"]["comment_icon_size_px"], 100)
        self.assertEqual(data["vertical"]["comment_margin_l"], 190)

    # 拡大の比が要望どおりであること (横 1.5 倍 / 縦 2 倍)
    def test_scale_ratio(self):
        self.assertEqual(self._new("subtitle", "comment_icon_size_px"),
                         self.LEGACY["subtitle"]["comment_icon_size_px"] * 1.5)
        self.assertEqual(self._new("vertical", "comment_icon_size_px"),
                         self.LEGACY["vertical"]["comment_icon_size_px"] * 2)

    # アイコンの大きさを独自に変えていれば、そのセクションは 2 つとも触らない
    def test_custom_icon_size_is_kept(self):
        data = self._legacy_settings(subtitle={"comment_icon_size_px": 120})
        sw._migrate_comment_icon_size(data)
        self.assertEqual(data["subtitle"]["comment_icon_size_px"], 120)
        self.assertEqual(data["subtitle"]["comment_margin_l"], 190)   # 据え置き
        # 触っていない縦だけは移行される
        self.assertEqual(data["vertical"]["comment_icon_size_px"], 100)

    # 左余白だけ独自に変えていても、そのセクションは 2 つとも触らない
    def test_custom_margin_is_kept(self):
        data = self._legacy_settings(subtitle={"comment_margin_l": 200})
        sw._migrate_comment_icon_size(data)
        self.assertEqual(data["subtitle"]["comment_icon_size_px"], 100)
        self.assertEqual(data["subtitle"]["comment_margin_l"], 200)

    # 2 度目は何も変えない (起動のたびに保存し直さない)
    def test_migration_is_idempotent(self):
        data = self._legacy_settings()
        self.assertTrue(sw._migrate_comment_icon_size(data))
        self.assertFalse(sw._migrate_comment_icon_size(data))

    # 読み込み全体 (_normalize_legacy_values) からも呼ばれること
    def test_called_from_normalize(self):
        data = self._legacy_settings()
        self.assertTrue(sw._normalize_legacy_values(data))
        self.assertEqual(data["subtitle"]["comment_icon_size_px"], 150)

    # 移行後もアイコンがキャンバスからはみ出さないこと (resolve14 §2.4 の担保)
    # 大きさだけ寄せて左余白を放置すると、ここが clamped=True になって落ちる。
    def test_icon_stays_inside_canvas_after_migration(self):
        data = self._legacy_settings()
        sw._migrate_comment_icon_size(data)

        landscape = comment_decor.icon_box(_COMMENT, data["subtitle"], 1920, 1080)
        self.assertEqual((landscape["x"], landscape["size"]), (40.0, 150))
        self.assertFalse(landscape["clamped"])

        portrait_cfg = subtitle_generator.build_effective_subtitle_cfg(
            data["subtitle"], data["vertical"], {"is_portrait": True})
        portrait = comment_decor.icon_box(_COMMENT, portrait_cfg, 1080, 1920)
        self.assertEqual((portrait["x"], portrait["size"]), (40.0, 100))
        self.assertFalse(portrait["clamped"])

    # セクションが欠けていても落ちない (壊れた setting.json への保険)
    def test_missing_section_is_ignored(self):
        data = {"subtitle": dict(self.LEGACY["subtitle"])}
        self.assertTrue(sw._migrate_comment_icon_size(data))
        self.assertEqual(data["subtitle"]["comment_icon_size_px"], 150)

if __name__ == "__main__":
    unittest.main()


# トラッキングぼかしの設定 (ver5 resolve8 §7)
class BlurSettingsTest(unittest.TestCase):

    # 旧 setting.json (blur セクションが無い) でも起動時に補われること。
    # 更新インストールでは旧ファイルが復元されるため、ここが効かないと
    # 新しいキーがいつまでも届かない。
    def test_blur_section_is_filled(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data.pop("blur", None)
        merged = sw._merge_with_defaults(data)
        self.assertIn("blur", merged)
        self.assertFalse(merged["blur"]["enabled"])        # 既定は無効 (R9)
        self.assertEqual(merged["blur"]["model"]["detector_input"], 416)

    # 入れ子 (model / track / render / editor) の欠落キーも補われること
    def test_blur_nested_keys_are_filled(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["blur"] = {"enabled": True, "render": {"strength": 80}}
        merged = sw._merge_with_defaults(data)
        sw._normalize_legacy_values(merged)
        self.assertEqual(merged["blur"]["render"]["strength"], 80)      # 利用者の値は残る
        self.assertEqual(merged["blur"]["render"]["mask_scale"], 0.25)  # 欠落は補う
        self.assertIn("detector", merged["blur"]["model"])

    # 画面に出していない値 (モデル・しきい値) が保存で消えないこと
    def test_settings_window_round_trip(self):
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        window = sw.SettingsWindow()
        try:
            window.blur_enabled_check.setChecked(True)
            window.blur_strength_edit.setText("70")
            collected = window._collect_settings()
        finally:
            window.close()
        self.assertTrue(collected["blur"]["enabled"])
        self.assertEqual(collected["blur"]["render"]["strength"], 70)
        # 画面に出していない値はそのまま残る
        self.assertEqual(collected["blur"]["model"]["detector"], "models/yolox_tiny.onnx")
        self.assertEqual(collected["blur"]["render"]["mask_scale"], 0.25)
        self.assertIn("sample_fps", collected["blur"]["track"])
        self.assertEqual(collected["blur"]["editor"]["frame_cache"], 32)
        del app
