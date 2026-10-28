# 設定の既定補完・レガシー値の自己修復 (src/settings/settings_window.py) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 重点 (error 20260820):
#   ・更新インストールではインストーラが旧 setting.json を復元するため、
#     配布 setting.json にしか無い値は新しい版へ更新しても届かない。
#     アプリ側 (DEFAULT_SETTINGS + 起動時の補完) で自己修復できることを守る。
#   ・利用者が意図して入れた値は上書きしないこと。
import copy
import io
import os
import unittest
from unittest import mock

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

    # 右クリックから足すテロップトラックの設定も、旧 setting.json へ補完される
    # (ver9 resolve2 §9.2)。既存値は温存される。
    def test_new_track_defaults_are_filled(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["timeline"] = copy.deepcopy(data["timeline"])
        # ver9 より前の形 (役割別トラックのキーだけ持つ)
        data["timeline"]["subtitle_tracks"] = {
            "role_track_enabled": True,
            "comment_track_id": "S9",            # 利用者が変えた値
            "comment_track_name": "Comment",
            "comment_track_index": 2,
            "auto_move_on_role_change": True,
        }
        merged = sw._merge_with_defaults(data)
        self.assertTrue(sw._fill_timeline_nested_defaults(merged))
        tracks = merged["timeline"]["subtitle_tracks"]
        for key in ("new_track_enabled", "new_track_id_prefix",
                    "new_track_name_format", "new_track_min_index",
                    "max_tracks", "new_track_moves_selection"):
            self.assertIn(key, tracks, f"{key} が補完されていません")
        self.assertEqual(tracks["new_track_min_index"], 3)
        self.assertEqual(tracks["max_tracks"], 8)
        # 既存値は上書きしない
        self.assertEqual(tracks["comment_track_id"], "S9")

    # 役割変更の自動移動の範囲も旧 setting.json へ補完される (ver9 resolve10 §5 T28)
    def test_role_move_scope_default_is_filled(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["timeline"] = copy.deepcopy(data["timeline"])
        data["timeline"]["subtitle_tracks"] = copy.deepcopy(
            data["timeline"]["subtitle_tracks"])
        # ver9 resolve10 より前の形 (role_move_scope を持たない)
        data["timeline"]["subtitle_tracks"].pop("role_move_scope", None)
        merged = sw._merge_with_defaults(data)
        self.assertTrue(sw._fill_timeline_nested_defaults(merged))
        tracks = merged["timeline"]["subtitle_tracks"]
        self.assertEqual(tracks["role_move_scope"], "role_tracks")

    # 利用者が "all" (従来挙動) へ変えていれば温存する
    def test_role_move_scope_is_kept(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["timeline"] = copy.deepcopy(data["timeline"])
        data["timeline"]["subtitle_tracks"] = copy.deepcopy(
            data["timeline"]["subtitle_tracks"])
        data["timeline"]["subtitle_tracks"]["role_move_scope"] = "all"
        merged = sw._merge_with_defaults(data)
        sw._fill_timeline_nested_defaults(merged)
        self.assertEqual(
            merged["timeline"]["subtitle_tracks"]["role_move_scope"], "all")


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

# プレビューのプロキシ設定 (ver6 resolve3 §5)
#
# 更新インストールではインストーラが旧 setting.json を復元するため、
# 配布 setting.json にしか無いセクションは新しい版へ更新しても届かない。
# timeline の入れ子は _fill_timeline_nested_defaults が 1 段深く補完する。
class ProxySettingsTest(unittest.TestCase):

    def test_default_settings_has_the_proxy_section(self):
        proxy = sw.DEFAULT_SETTINGS["timeline"]["preview"]["proxy"]

        # 既定は "off" (ver8 resolve8 §24-7 / 再生中の暗転の回避)
        self.assertEqual(proxy["mode"], "off")
        self.assertEqual(proxy["scale"], 0.5)
        self.assertEqual(proxy["tile_sec"], 4.0)

    # proxy セクションが無い既存の setting.json でも、起動時に補完される
    def test_missing_proxy_section_is_filled(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["timeline"] = copy.deepcopy(data["timeline"])
        data["timeline"]["preview"] = copy.deepcopy(data["timeline"]["preview"])
        del data["timeline"]["preview"]["proxy"]

        merged = sw._merge_with_defaults(data)
        self.assertNotIn("proxy", merged["timeline"]["preview"])
        changed = sw._normalize_legacy_values(merged)

        self.assertTrue(changed, "補完した場合は永続化のため True を返すこと")
        self.assertIn("proxy", merged["timeline"]["preview"])
        self.assertEqual(merged["timeline"]["preview"]["proxy"]["mode"], "off")

    # 利用者が変えた値は温存する (補完で上書きしない)
    def test_user_values_are_kept(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["timeline"] = copy.deepcopy(data["timeline"])
        data["timeline"]["preview"] = copy.deepcopy(data["timeline"]["preview"])
        data["timeline"]["preview"]["proxy"] = {"mode": "off", "scale": 0.25}

        merged = sw._merge_with_defaults(data)
        sw._normalize_legacy_values(merged)
        proxy = merged["timeline"]["preview"]["proxy"]

        self.assertEqual(proxy["mode"], "off")
        self.assertEqual(proxy["scale"], 0.25)
        # 足りないキーだけが既定で埋まる
        self.assertEqual(proxy["tile_sec"], 4.0)


# 旧既定 ("tile") から "off" への 1 回きりの移行 (ver8 resolve8 §24-7 / §24-8)
#
# "tile" は再生中にタイルの継ぎ目で 4 秒ごとに画面が暗転する。更新インストールでは
# インストーラが旧 setting.json を復元するため、DEFAULT_SETTINGS を変えただけでは
# 既存利用者に届かない。この設定は設定画面に出していないため自力で直す口も無い。
class ProxyBlackoutMigrationTest(unittest.TestCase):

    def _settings(self, **proxy_overrides):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["timeline"] = copy.deepcopy(data["timeline"])
        data["timeline"]["preview"] = copy.deepcopy(data["timeline"]["preview"])
        proxy = dict(data["timeline"]["preview"]["proxy"], **proxy_overrides)
        proxy.pop("blackout_migrated", None)        # 旧ファイルにはフラグが無い
        data["timeline"]["preview"]["proxy"] = proxy
        return data

    def _proxy(self, merged):
        return merged["timeline"]["preview"]["proxy"]

    # 旧既定のまま残っている既存利用者は "off" へ寄る
    def test_legacy_tile_is_migrated(self):
        merged = sw._merge_with_defaults(self._settings(mode="tile"))
        # マージだけでは既存値が残る (キーが在るため既定が届かない)
        self.assertEqual(self._proxy(merged)["mode"], "tile")
        self.assertTrue(sw._normalize_legacy_values(merged))
        self.assertEqual(self._proxy(merged)["mode"], "off")
        self.assertTrue(self._proxy(merged)["blackout_migrated"])

    # 移行後に利用者が意図して "tile" へ戻した場合は二度と触らない
    def test_tile_after_migration_is_kept(self):
        data = self._settings(mode="tile")
        data["timeline"]["preview"]["proxy"]["blackout_migrated"] = True
        merged = sw._merge_with_defaults(data)
        sw._normalize_legacy_values(merged)
        self.assertEqual(self._proxy(merged)["mode"], "tile")

    # 2 度目は何も変えない (起動のたびに保存し直さない)
    def test_migration_is_idempotent(self):
        merged = sw._merge_with_defaults(self._settings(mode="tile"))
        self.assertTrue(sw._migrate_preview_proxy_mode(merged))
        self.assertFalse(sw._migrate_preview_proxy_mode(merged))

    # 既に "off" の環境ではモードを変えずフラグだけ立てる
    def test_already_off_only_marks_the_flag(self):
        merged = sw._merge_with_defaults(self._settings(mode="off"))
        self.assertTrue(sw._migrate_preview_proxy_mode(merged))
        self.assertEqual(self._proxy(merged)["mode"], "off")
        self.assertTrue(self._proxy(merged)["blackout_migrated"])

    # proxy セクションごと無い壊れた setting.json でも落ちない
    def test_missing_proxy_section_is_ignored(self):
        self.assertFalse(sw._migrate_preview_proxy_mode({"timeline": {"preview": {}}}))
        self.assertFalse(sw._migrate_preview_proxy_mode({"timeline": {}}))
        self.assertFalse(sw._migrate_preview_proxy_mode({}))

    # 配布 setting.json も "off" であること (新規インストールと既存で揃える)
    def test_shipped_settings_is_off(self):
        import json
        with io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "src", "settings", "setting.json"),
                     encoding="utf-8-sig") as handle:
            shipped = json.load(handle)
        self.assertEqual(
            shipped["timeline"]["preview"]["proxy"]["mode"], "off",
            "配布 setting.json の proxy.mode が DEFAULT_SETTINGS と揃っていません")


if __name__ == "__main__":
    unittest.main()


# サブスクリプション (Stripe) の設定 (ver6 resolve2 §1.4 / §5.1)
#
# 背景: setting.json にだけ billing セクションを足しても、DEFAULT_SETTINGS に
#   無いあいだは _merge_with_defaults が落とすため、load_settings → save_settings で
#   セクションごと消えていた。同じことを再発させないための回帰テスト。
class BillingSettingsTest(unittest.TestCase):

    _KEYS = ("config_cache_sec", "activation_poll_sec", "activation_timeout_sec")

    # 配布物が billing セクションを持っていること
    def test_default_settings_has_billing(self):
        billing = sw.DEFAULT_SETTINGS.get("billing")
        self.assertIsInstance(billing, dict)
        for key in self._KEYS:
            self.assertIn(key, billing)

    # 既存の setting.json にある billing が保存で消えないこと (§1.4 の回帰)
    def test_billing_section_survives_a_merge(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        merged = sw._merge_with_defaults(data)
        self.assertIn("billing", merged)
        self.assertEqual(sw.DEFAULT_SETTINGS["billing"]["activation_timeout_sec"],
                         merged["billing"]["activation_timeout_sec"])

    # 旧 setting.json (billing セクションが無い) でも起動時に補われること
    def test_billing_section_is_filled(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data.pop("billing", None)
        merged = sw._merge_with_defaults(data)
        self.assertIn("billing", merged)
        self.assertTrue(sw._has_new_keys(data, merged))

    # 利用者が変えた値は温存すること
    def test_user_value_is_kept(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["billing"] = {"activation_timeout_sec": 60}
        merged = sw._merge_with_defaults(data)
        self.assertEqual(60, merged["billing"]["activation_timeout_sec"])
        self.assertEqual(3, merged["billing"]["activation_poll_sec"])   # 欠落は補う


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
        # 追加フォント (settings/fonts) はテストでは Qt へ登録しない。
        # テストは画面の無い offscreen プラットフォームで動いており、こちらの
        # フォントデータベースは addApplicationFont を境に文字を描けなくなる。
        # 後続のテストで QPainter が何も描かなくなり、アイコンの色を見る
        # test_subscription_ui が「描かれていない」として落ちる (同一プロセスのみ)。
        # 本番 (windows プラットフォーム) では起きないため製品側は変えない。
        with mock.patch.object(sw, "register_fonts_in_dir"):
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

    # 音量調節 (ラウドネス正規化) のチェックが読み書きできること (ver9 request1)。
    # クリップ用・アーカイブ用はどちらも loudness.enabled を見るため、
    # この 1 項目で両方の音量調節が切り替わる。
    def test_loudness_checkbox_round_trip(self):
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        # 追加フォントを登録しない理由は test_settings_window_round_trip と同じ
        with mock.patch.object(sw, "register_fonts_in_dir"):
            window = sw.SettingsWindow()
        try:
            # 保存値 false が画面へ反映される
            saved = copy.deepcopy(sw.DEFAULT_SETTINGS)
            saved["loudness"] = dict(saved["loudness"], enabled=False)
            window._load_to_ui(saved)
            self.assertFalse(window.loudness_enabled_check.isChecked())
            off = window._collect_settings()

            # チェックを戻せば true で保存される
            window.loudness_enabled_check.setChecked(True)
            on = window._collect_settings()

            # キーが無い設定では既定 (有効) で表示する
            missing = copy.deepcopy(sw.DEFAULT_SETTINGS)
            missing.pop("loudness")
            window._load_to_ui(missing)
            self.assertTrue(window.loudness_enabled_check.isChecked())
        finally:
            window.close()
        self.assertFalse(off["loudness"]["enabled"])
        self.assertTrue(on["loudness"]["enabled"])
        # 画面に出していない目標値などは保存で消えない
        self.assertEqual(off["loudness"]["target_i"], -14.0)
        self.assertTrue(off["loudness"]["two_pass"])
        self.assertEqual(off["loudness"]["audio_bitrate"], "192k")
        del app
