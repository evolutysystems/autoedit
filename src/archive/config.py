# アーカイブ採点の設定読み出し (ハードコード回避のため setting.json の archive セクションを参照)
# 欠落キーは DEFAULT_SETTINGS 側で補完される前提だが、単体でも安全に既定へフォールバックする。
import os


# src/settings ディレクトリの絶対パスを返す (work_dir/トークン等の相対解決の基準)。
# settings_window.SETTINGS_DIR と同一だが、PySide 依存を避けるため独立に算出する。
def _settings_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings")


# 方式A の採点設定を1つの辞書に平坦化して返す (scoring.score_window / select_top_events 用)
def method_a_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    scoring = archive.get("scoring", {})
    method_a = scoring.get("method_a", {})
    return {
        "window_sec": int(scoring.get("window_sec", 300)),
        "slide_sec": int(scoring.get("slide_sec", 60)),
        "top_n": int(scoring.get("top_n", 5)),
        "clip_pad_sec": float(scoring.get("clip_pad_sec", 0)),
        "loud_percentile": float(scoring.get("loud_percentile", 0.8)),
        "silence_ratio_threshold": float(scoring.get("silence_ratio_threshold", 0.6)),
        "weights": method_a.get("weights", {"emotion": 0.55, "comment": 0.45}),
        "emotion_points": method_a.get("emotion_points", {"loud": 10, "long_silence": -8}),
        "comment": method_a.get("comment", {"w_point_per_char": 1, "rate_spike_bonus": 10}),
        # 急増ボーナスの有効化と平均コメント数/分は pipeline がコメント取得時に上書きする
        # (未取得なら False/0 のまま = ボーナス無効 = 従来どおり音声のみ採点)。
        "comment_spike": False,
        "avg_comments_per_min": 0.0,
    }


# Twitch ログイン認証設定を平坦化して返す (resolve17 §4.3.0 / §4.9)
def auth_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    auth = archive.get("auth", {})
    return {
        "client_id": str(auth.get("client_id", "") or ""),
        "client_secret": str(auth.get("client_secret", "") or ""),
        "redirect_port": int(auth.get("redirect_port", 3737)),
        "owner_only": bool(auth.get("owner_only", True)),
        # 認可を求めるスコープ (ver3 resolve16 §5.7)。ストリームマーカーの取得に
        # user:read:broadcast が要る。既存 setting.json の archive.auth にはこのキーが
        # 無く、入れ子の補完も行われないためコード側の既定が効く (resolve16 §2.6/§3-5)。
        # 空リストにすると従来どおりスコープ無しで認可する (マーカーは使えない)。
        "scopes": [str(s) for s in auth.get("scopes", ["user:read:broadcast"]) if str(s)],
    }


# ストリームマーカーの設定を平坦化して返す (ver3 resolve16 §7)
# マーカー位置の前後を「必ず残すセクション」にするための秒数を持つ。
def marker_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    markers = archive.get("markers", {})
    return {
        "enabled": bool(markers.get("enabled", True)),
        # マーカー位置の前後に必ず含める秒数 (要望: 前後 2 分)
        "before_sec": float(markers.get("before_sec", 120)),
        "after_sec": float(markers.get("after_sec", 120)),
    }


# 取得(download)設定を平坦化して返す (resolve17 §4.3 / §4.9)
def download_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    dl = archive.get("download", {})
    return {
        "downloader": str(dl.get("downloader", "local") or "local"),
        "twitch_dl_path": str(dl.get("twitch_dl_path", "twitch-dl") or "twitch-dl"),
        "vod_format": str(dl.get("vod_format", "source") or "source"),
        # 希望画質が無い VOD で利用できる最高画質へ自動で落とす (error 20260810)。
        # false にすると従来どおり希望画質のまま実行し、無ければ失敗する。
        "vod_quality_fallback": bool(dl.get("vod_quality_fallback", True)),
        "chat_source": str(dl.get("chat_source", "twitch-dl") or "twitch-dl"),
        "work_dir": str(dl.get("work_dir", "archive_work") or "archive_work"),
    }


# 取得物の作業ディレクトリを絶対パスで解決し、無ければ作成して返す。
# 相対指定は src/settings 基準 (resolve_fonts_dir と同方針・ハードコード回避)。
def resolve_work_dir(settings):
    work_dir = download_config(settings)["work_dir"]
    if not os.path.isabs(work_dir):
        work_dir = os.path.join(_settings_dir(), work_dir)
    os.makedirs(work_dir, exist_ok=True)
    return work_dir


# Twitch トークンの保存先 (ローカル限定・機微情報。resolve17 §4.3.0/§7)。
# 作業ディレクトリ配下に置く (書込可能・アンインストールで消える運用)。
def twitch_token_path(settings):
    return os.path.join(resolve_work_dir(settings), "twitch_token.json")


# クリップ処理 (clip_pipeline) の設定を平坦化して返す
def clip_pipeline_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    pipe = archive.get("clip_pipeline", {})
    return {
        "silence_cut": bool(pipe.get("silence_cut", True)),
        "subtitle_review": bool(pipe.get("subtitle_review", True)),
        "volume_dialog": bool(pipe.get("volume_dialog", False)),
        # true=Timeline 編集画面 / false=従来の一括結果画面 (ver3 resolve5 §7)
        "timeline_review": bool(pipe.get("timeline_review", True)),
    }


# アーカイブのレビューを Timeline 編集画面で行うか (ver3 resolve5)
# timeline.enabled=false のときは Timeline 自体が無効なため従来画面を使う。
def use_timeline_review(settings):
    if not (settings or {}).get("timeline", {}).get("enabled", True):
        return False
    return clip_pipeline_config(settings)["timeline_review"]


# セクション追加の設定を平坦化して返す (ver3 resolve13 §5.2)
# 編集画面から元動画の区間を指定してセクションを足す機能。既定は有効。
def section_add_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    add = archive.get("section_add", {})
    scoring = archive.get("scoring", {})
    return {
        "enabled": bool(add.get("enabled", True)),
        # 追加ダイアログの既定尺。採点の窓幅へ揃える (別々に持つと意味が割れる)
        "default_length_sec": float(add.get("default_length_sec",
                                            scoring.get("window_sec", 180)) or 180),
        "min_length_sec": float(add.get("min_length_sec", 1.0) or 1.0),
        # 重なり時に 1 つへ統合するか (false = 別セクションとして並べる / 切り戻し用)
        "merge_on_overlap": bool(add.get("merge_on_overlap", True)),
        # マージ方式 "delta"=差分だけ用意 (既定 / §3-4 案B)。
        # "rebuild" (和集合の作り直し) は未実装のため delta へ寄せる (§7)。
        "merge_mode": "delta",
        # 何を「使用中」とみなすか (ver5 resolve6 §3.4)
        #   "used"     = Timeline に実際に載っている区間だけ (既定)
        #   "declared" = セクションが宣言した区間 (従来の挙動 / 切り戻し用)
        # 宣言区間は作成時のもので、無音カットや利用者の削除では更新されない。
        # "declared" のままだと、もう Timeline に無い区間まで塞いでしまう。
        "occupied_by": ("declared"
                        if str(add.get("occupied_by", "")).strip() == "declared"
                        else "used"),
        # 使用中の区間どうしの隙間がこの秒数以内なら 1 つに繋ぐ。
        # 無音カットが空ける細かい穴で、追加区間が刻まれすぎるのを防ぐ。
        "used_gap_merge_sec": max(float(add.get("used_gap_merge_sec", 10.0) or 0.0), 0.0),
        # 1 回の追加で用意する区間の上限。超える指定は理由を出して断る。
        "max_ranges": max(int(add.get("max_ranges", 20) or 20), 1),
    }


# 切り抜き出力ファイル名の接頭辞を返す (既定 "archive")
def clip_prefix(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    return str(archive.get("output", {}).get("clip_prefix", "archive")) or "archive"


# テーマ・イントロカードの設定を平坦化して返す (resolve19 §5)
# 欠落キーは DEFAULT_SETTINGS 側で補完される前提だが、単体でも安全に既定へフォールバックする。
def intro_card_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    card = archive.get("intro_card", {})
    return {
        "enabled": bool(card.get("enabled", True)),
        "buffer_sec": float(card.get("buffer_sec", 1.5)),
        "blur_sigma": float(card.get("blur_sigma", 18)),
        "margin_top_px": int(card.get("margin_top_px", 300)),
        "margin_bottom_px": int(card.get("margin_bottom_px", 300)),
        "box_color": str(card.get("box_color", "black")) or "black",
        "box_opacity": float(card.get("box_opacity", 1.0)),
        "title_font_family": str(card.get("title_font_family", "") or ""),
        "title_font_size": int(card.get("title_font_size", 112)),
        "title_color": str(card.get("title_color", "#FFFFFF")) or "#FFFFFF",
        "tag_font_family": str(card.get("tag_font_family", "") or ""),
        "tag_font_size": int(card.get("tag_font_size", 40)),
        "tag_color": str(card.get("tag_color", "#FFFFFF")) or "#FFFFFF",
        "tag_bg_opacity": float(card.get("tag_bg_opacity", 0.45)),
        "tag_margin_l": int(card.get("tag_margin_l", 40)),
        "tag_margin_v": int(card.get("tag_margin_v", 30)),
        "mute_intro": bool(card.get("mute_intro", False)),
    }
