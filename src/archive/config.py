# アーカイブ採点の設定読み出し (ハードコード回避のため setting.json の archive セクションを参照)
# 欠落キーは DEFAULT_SETTINGS 側で補完される前提だが、単体でも安全に既定へフォールバックする。
import os
import re
import shutil
import time

from ..utils import trash
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 取得物 (twitch-dl が作ったもの) だけに一致させる
# (docs/error/fromUser/resolve.md §6-6)。
#
# 前置き一致 ("vod_" で始まる) にすると、同じフォルダに置かれる
# **保存済みプロジェクト** vod_<id>.archive.json や書き出した
# vod_<id>.archive.fcpxml まで削除対象に入ってしまう。
# 消してよいのは「取り直せるもの」= 取得した動画とチャットだけである。
_SOURCE_NAME_RE = re.compile(r"^(?:vod_\d+\.mp4|chat_\d+\.json)$")

# イントロカードの素の控えのファイル名の末尾
# (<素材フォルダ>/<media_id>_intro.mp4 / docs/error/fromUser/resolve.md §9-7)。
#
# clip_writer (作る側) と vod_retention (自己完結の判定で**除外する**側) の
# 両方が要る。clip_writer は renderer まで引き込むため、軽いここに置く。
INTRO_SOURCE_SUFFIX = "_intro.mp4"


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
        # コメント未取得時に重みを 0..100 へ正規化する (docs/quality/resolve1.md §2.3 F4)。
        # 従来は comment=0 のまま total = emotion * 0.55 となり満点が 55 点だった。
        "normalize_weights": bool(method_a.get("normalize_weights", True)),
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
        # 取得物の保持日数 / 失敗時の断片キャッシュの扱い
        # (docs/error/fromUser/resolve.md §6-3・§6-6)
        "keep_source_days": _coerce_int(dl.get("keep_source_days", 0), 0),
        "keep_cache_on_error": bool(dl.get("keep_cache_on_error", False)),
        # 元 VOD を残し続けるか (false = 自己完結したら捨てる / resolve.md §6-6)
        "keep_vod": bool(dl.get("keep_vod", False)),
    }


# 設定値を int へ直す (不正値は既定へ倒す)。負値は 0 (= 無期限/無効) とみなす。
def _coerce_int(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else 0


# 取得物の作業ディレクトリを絶対パスで解決し、無ければ作成して返す。
# 相対指定は src/settings 基準 (resolve_fonts_dir と同方針・ハードコード回避)。
def resolve_work_dir(settings):
    work_dir = download_config(settings)["work_dir"]
    if not os.path.isabs(work_dir):
        work_dir = os.path.join(work_base_dir(), work_dir)
    os.makedirs(work_dir, exist_ok=True)
    _migrate_token(work_dir, settings)
    return work_dir


# 相対指定の work_dir を解決する基準フォルダ (docs/error/fromUser/resolve.md §6-6-6)
#
# 従来は src/settings 基準 = **インストールフォルダ配下**だった。
# 取得物は 1 本 10〜25 GB になるため、アプリの置き場が数十 GB 膨らみ、
# しかもシステムドライブ固定で利用者には見えなかった (2026-10-22 の報告)。
# 利用者データの置き場である %LOCALAPPDATA%\Stretheus 配下へ移す。
def work_base_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Stretheus")


# 旧版 (v1.5.8 以前) が使っていた作業フォルダ。
# インストールフォルダ配下のため、**アプリ名変更前の AutoEdit 配下**にもあり得る
# (AppId 据え置きで上書き更新した利用者は …\Programs\AutoEdit\ のまま / §8-1)。
# ここは常に「この実行中のアプリ自身の」場所として解決すれば足りる。
def legacy_work_dir(settings):
    work_dir = download_config(settings)["work_dir"]
    if os.path.isabs(work_dir):
        return ""           # 絶対指定の利用者は移行の対象外 (自分で決めている)
    return os.path.join(_settings_dir(), work_dir)


# Twitch のログイン情報だけは新しい置き場へ引き継ぐ (§6-6-6)。
#
# トークンは作業フォルダ配下に置いているため、置き場を変えると**ログアウトしてしまう**。
# 取得物 (GB 級) は移さないが、これだけは小さく、移さないと利用者が困る。
def _migrate_token(new_work_dir, settings):
    new_token = os.path.join(new_work_dir, "twitch_token.json")
    if os.path.exists(new_token):
        return
    old_dir = legacy_work_dir(settings)
    old_token = os.path.join(old_dir, "twitch_token.json") if old_dir else ""
    if not old_token or not os.path.isfile(old_token):
        return
    try:
        shutil.copy2(old_token, new_token)
    except OSError as e:
        _logger.warning("Twitch のログイン情報を引き継げませんでした: %s", e)
        return
    _logger.info("Twitch のログイン情報を新しい作業フォルダへ引き継ぎました")


# Twitch トークンの保存先 (ローカル限定・機微情報。resolve17 §4.3.0/§7)。
# 作業ディレクトリ配下に置く (書込可能・アンインストールで消える運用)。
def twitch_token_path(settings):
    return os.path.join(resolve_work_dir(settings), "twitch_token.json")


# 作業ディレクトリに溜まった取得物の合計サイズ (バイト) と件数を返す
# (docs/error/fromUser/resolve.md §6-6)。
# 戻り値: {"bytes": int, "files": int, "paths": [パス…]}
# 画面へ「今いくら使っているか」を出すために使う。測れないものは数えない。
def source_usage(settings):
    work_dir = resolve_work_dir(settings)
    total, paths = 0, []
    for name in sorted(os.listdir(work_dir)):
        if not _SOURCE_NAME_RE.match(name):
            continue
        path = os.path.join(work_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            total += os.path.getsize(path)
        except OSError:
            continue
        paths.append(path)
    return {"bytes": total, "files": len(paths), "paths": paths}


# 保持日数を過ぎた取得物を**ゴミ箱へ送る** (docs/error/fromUser/resolve.md §6-6)。
#
# keep_source_days=0 (既定) のときは何もしない。
# パイプラインの最後ではなくアーカイブ画面を開いたときに呼ぶ:
# 「続きから」で同じ VOD を使い直す運用があるため、直後に消してはいけない。
#
# 完全削除ではなくゴミ箱を使う。利用者が時間をかけて取得した素材であり、
# 自動で消す以上は取り戻せなければならない (実装中に開発端末で 45 GB を
# os.remove で消してしまい、復旧できなかった事故による)。
# ゴミ箱が使えない環境では**何もしない**。黙って完全削除はしない (utils/trash.py と同方針)。
# 戻り値: 回収したバイト数。失敗しても例外は外へ出さない (画面を止めない)。
def sweep_old_sources(settings, now=None):
    days = download_config(settings)["keep_source_days"]
    if days <= 0:
        return 0
    if not trash.is_available():
        _logger.info("ゴミ箱を使えないため取得物の自動削除は行いません")
        return 0

    deadline = (now if now is not None else time.time()) - days * 86400
    targets, sizes = [], {}
    for path in source_usage(settings)["paths"]:
        try:
            if os.path.getmtime(path) >= deadline:
                continue
            sizes[path] = os.path.getsize(path)
        except OSError:
            continue
        targets.append(path)
    if not targets:
        return 0

    result = trash.send_to_trash(targets)
    freed = sum(sizes.get(path, 0) for path in result["trashed"])
    for path in result["trashed"]:
        _logger.info("保持日数を過ぎた取得物をゴミ箱へ送りました: %s",
                     os.path.basename(path))
    for item in result["failed"]:
        _logger.warning("取得物をゴミ箱へ送れませんでした: %s (%s)",
                        item["path"], item["reason"])
    if freed:
        _logger.info("取得物の掃除で %.1f GB を回収しました", freed / (1024 ** 3))
    return freed


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
        # カット閾値をどの範囲で決めるか (docs/quality/resolve1.md §2.3 F7)
        #   "per_clip" : クリップごとに測り直す (従来)。クリップ間で揃わない
        #   "vod"      : VOD 全体で 1 回測り、全クリップへ同じ閾値を使う
        #   "saved"    : volume_analysis.last_cut_db を固定で使う
        "cut_db_scope": str(pipe.get("cut_db_scope", "per_clip") or "per_clip"),
        # ラウドネス正規化をどの範囲で行うか (§4.3 F9)
        #   "per_clip" : クリップごとに -14 LUFS へ揃える (従来)。山場が潰れる
        #   "vod"      : VOD 全体で 1 回測ったゲインを全クリップへ適用する
        "loudness_scope": str(pipe.get("loudness_scope", "per_clip") or "per_clip"),
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
        # 元 VOD を手放したあとの「区間だけ取り直し」(ver9 resolve5 §4.8)
        "refetch": _section_refetch_config(add.get("refetch", {})),
    }


# セクション追加で元 VOD が無いときに、必要な区間だけ取り直す設定 (ver9 resolve5 §4.8)
# 元 VOD は既定 (keep_vod=false) で自己完結後にゴミ箱へ送るため、
# これが無効だと「後から区間を足す」機能そのものが使えなくなる。
def _section_refetch_config(refetch):
    refetch = refetch if isinstance(refetch, dict) else {}
    return {
        "enabled": bool(refetch.get("enabled", True)),
        # 取得範囲へ足す余裕 (秒)。取得はセグメント単位 (約 12 秒) のため実質は保険だが、
        # イントロカードの素 (クリップ開始の intro_card.buffer_sec 秒手前) も要るため
        # 実効値は buffer_sec と比べて大きいほうを使う (resolve5 §5.3)。
        "pad_sec": max(float(refetch.get("pad_sec", 3.0) or 0.0), 0.0),
        # 1 分あたりに見込む取得量 (MB)。空き容量の確認にだけ使う。
        # 実測は 1080p で約 17MB/分 (resolve5 §2.6) のため既定は余裕を見た値。
        "min_free_mb_per_min": max(
            float(refetch.get("min_free_mb_per_min", 60.0) or 0.0), 0.0),
        # 記録された画質が使えないとき、別の画質へ落として取得してよいか。
        # 既定 false = 落とさない。画質が変わると既存素材と寸法が割れるため、
        # 黙って通さず呼び出し側で確認を取る (resolve5 §5.4)。
        "allow_quality_fallback": bool(refetch.get("allow_quality_fallback", False)),
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
        # 本編に掛けた正規化ゲインをイントロへも適用する (F8)
        "match_body_level": bool(card.get("match_body_level", True)),
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
