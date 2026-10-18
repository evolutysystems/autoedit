# ログを積む側の唯一の入口 (request1 / resolve1 §4.1)
#
# アプリの各所から呼ばれるのはここだけで、**ネットワークも認証も触らない**。
# ファイルを 1 本書くだけなのでオフラインでも成功し、呼び出し側は結果を
# 見なくてよい。送信は log_sender が起動時と出力完了時にまとめて行う。
#
# points.py と同じ約束を置く:
#   ・例外を外へ出さない。ログを積めなくても本体の動作を止めない。
#   ・設定で無効 (telemetry.enabled = false) のときは何も書かない。
#   ・未設定 (configure 未呼び出し = CLI / テスト) でも安全に呼べる。
import json
import os
import threading
import time

from ..utils.logger import get_logger
from . import log_payloads as payloads
from . import log_queue
from .auth_store import default_auth_path

_logger = get_logger(__name__)

# いま走っているセッション。次回起動でこれが残っていたらクラッシュとみなす。
_SESSION_FILE = "session.json"
# プロジェクトファイル (または入力動画) と projectId の対応 (resolve1 §7.2)
_PROJECT_IDS_FILE = "project_ids.json"
# 対応を覚えておく件数の上限。古いものから捨てる。
_MAX_PROJECT_IDS = 200

# イベント名はここから参照する (呼び出し側が log_payloads を直接触らないように)。
EVENT_APP_START = payloads.EVENT_APP_START
EVENT_TIMELINE_OPEN = payloads.EVENT_TIMELINE_OPEN
EVENT_TIMELINE_DECIDE = payloads.EVENT_TIMELINE_DECIDE
EVENT_EXPORT_START = payloads.EVENT_EXPORT_START
EVENT_PROJECT_RESUME = payloads.EVENT_PROJECT_RESUME
EVENT_WATERMARK_ACCEPTED = payloads.EVENT_WATERMARK_ACCEPTED
EVENT_WATERMARK_DECLINED = payloads.EVENT_WATERMARK_DECLINED

_lock = threading.Lock()
_state = {
    "queue": None,
    "enabled": False,
    "analytics": False,
    "errors": False,
    "session_id": None,
    "base_dir": None,
}


# ---- 初期化 ----------------------------------------------------------------
# 設定を読んでキューを用意する。GUI 起動時に 1 回だけ呼ぶ。
def configure(settings, queue=None, base_dir=None):
    config = (settings or {}).get("telemetry", {}) if isinstance(settings, dict) else {}
    enabled = bool(config.get("enabled", True))

    with _lock:
        _state["enabled"] = enabled
        _state["analytics"] = enabled and bool(config.get("send_analytics", True))
        _state["errors"] = enabled and bool(config.get("send_errors", False))
        _state["base_dir"] = base_dir or os.path.dirname(default_auth_path())
        _state["session_id"] = None
        _state["queue"] = None

        if not enabled:
            return

        _state["queue"] = queue or log_queue.LogQueue(
            base_dir=None if base_dir is None else os.path.join(base_dir, "logqueue"),
            limits={
                log_queue.TABLE_ERRORS: config.get("queue_limit_errors"),
                log_queue.TABLE_EVENTS: config.get("queue_limit_events"),
                "default": config.get("queue_limit_default"),
            })


# テストと後始末のために内部状態を捨てる。
def reset():
    with _lock:
        _state.update({"queue": None, "enabled": False, "analytics": False,
                       "errors": False, "session_id": None, "base_dir": None})


def enabled():
    return bool(_state["enabled"] and _state["queue"] is not None)


def session_id():
    return _state["session_id"]


def queue():
    return _state["queue"]


# ---- セッション (resolve1 §7.1) --------------------------------------------
# 起動時に呼ぶ。前回が異常終了していれば crash を先に積み、
# 新しいセッションを開始して AppStart を積む。戻り値は sessionId (無効なら None)。
def app_started(startup_ms=None):
    if not _analytics():
        return None

    try:
        _close_stale_session()

        new_id = payloads.new_id()
        started = payloads.utc_now_iso()
        if not _enqueue(log_queue.TABLE_SESSIONS, payloads.session_start(new_id, started)):
            return None

        _state["session_id"] = new_id
        _write_json(_path(_SESSION_FILE), {"sessionId": new_id, "startedAt": started})
        event(payloads.EVENT_APP_START, duration_ms=startup_ms)
        return new_id
    except Exception:  # noqa: BLE001 (起動を止めない)
        _logger.exception("セッション開始ログの記録に失敗")
        return None


# 終了時に呼ぶ。正常終了なら session.json を消し、次回起動でクラッシュ扱いにしない。
def app_closing(crash=False):
    if not _analytics():
        return

    current = _state["session_id"]
    if not current:
        return

    try:
        _enqueue(log_queue.TABLE_SESSIONS,
                 payloads.session_end(current, crash=crash),
                 depends_on={"session": current})
        _state["session_id"] = None
        if not crash:
            _remove(_path(_SESSION_FILE))
    except Exception:  # noqa: BLE001 (終了を止めない)
        _logger.exception("セッション終了ログの記録に失敗")


# 前回のセッションが残っていたら crash = true で閉じる。
def _close_stale_session():
    path = _path(_SESSION_FILE)
    stale = _read_json(path)
    stale_id = (stale or {}).get("sessionId")
    if not stale_id:
        _remove(path)
        return

    _logger.info("前回のセッションが正常終了していません。クラッシュとして記録します。")
    _enqueue(log_queue.TABLE_SESSIONS,
             payloads.session_end(stale_id, crash=True),
             depends_on={"session": stale_id})
    _remove(path)


# ---- プロジェクト (resolve1 §7.2) ------------------------------------------
# 出力の開始時に呼ぶ。projectId を決めて Projects を積み、その ID を返す。
# ID はパス (プロジェクトファイル優先 / 無ければ入力動画) に紐づけて覚えるため、
# 同じ素材を何度書き出しても Projects は 1 行のまま (upsert) になる。
# 積めなかった場合も ID は返す (ExportLogs 側で dependsOn により待つ)。
def register_project(input_path, settings, project_path=None, output_profile=None,
                     video_length=None):
    if not _analytics():
        return None

    try:
        project_id = _project_id_for(project_path or input_path)
        if not project_id:
            return None

        body = payloads.project_upsert(
            project_id,
            video_length if video_length is not None else _probe_length(input_path, settings),
            _resolution_of(output_profile, settings),
            (settings or {}).get("ffmpeg", {}).get("output_fps"),
            (settings or {}).get("ffmpeg", {}).get("video_codec"))

        _enqueue(log_queue.TABLE_PROJECTS, body)
        return project_id
    except Exception:  # noqa: BLE001 (出力を止めない)
        _logger.exception("プロジェクト情報の記録に失敗")
        return None


# 出力の開始時に呼ぶ。開始の記録と、所要時間の起点を返す。
# 戻り値は time.monotonic() の値で、export_finished へ渡す経過時間の計算に使う。
def export_started(project_id, resumed=False):
    event(EVENT_PROJECT_RESUME if resumed else EVENT_EXPORT_START, project_id=project_id)
    return time.monotonic()


# ---- 書き出し (resolve1 §7.3) ----------------------------------------------
# 出力の完了時に呼ぶ。成功・失敗・キャンセルのすべてで呼ぶこと
# (どの工程で諦めたかが分析の対象になる)。
def export_finished(project_id, success, duration_ms, settings=None,
                    output_profile=None, cancelled=False, codec=None):
    if not _analytics() or not project_id:
        return

    try:
        resolution_text = _resolution_of(output_profile, settings)
        _enqueue(log_queue.TABLE_EXPORT_LOGS,
                 payloads.export_log(
                     project_id, resolution_text,
                     codec or (settings or {}).get("ffmpeg", {}).get("video_codec"),
                     success, duration_ms),
                 depends_on={"project": project_id})

        if cancelled:
            name = payloads.EVENT_EXPORT_CANCELLED
        elif success:
            name = payloads.EVENT_EXPORT_SUCCEEDED
        else:
            name = payloads.EVENT_EXPORT_FAILED
        event(name, duration_ms=duration_ms, project_id=project_id)
    except Exception:  # noqa: BLE001
        _logger.exception("書き出しログの記録に失敗")


# ---- 工程 (resolve1 §7.6) --------------------------------------------------
# PipelineContext.end_step / cleanup から呼ぶ。label は日本語の工程名で、
# PROCESS_NAMES に無いものは送らない。
def processing_finished(project_id, label, duration_ms, success):
    if not _analytics() or not project_id:
        return

    name = payloads.PROCESS_NAMES.get(str(label or "").strip())
    if not name:
        return

    try:
        _enqueue(log_queue.TABLE_PROCESSING_LOGS,
                 payloads.processing_log(project_id, name, duration_ms, success),
                 depends_on={"project": project_id})
    except Exception:  # noqa: BLE001
        _logger.exception("処理ログの記録に失敗")


# ---- イベント (resolve1 §7.4) ----------------------------------------------
def event(name, duration_ms=None, project_id=None):
    if not _analytics():
        return

    try:
        _enqueue(log_queue.TABLE_EVENTS,
                 payloads.analytics_event(name, duration_ms=duration_ms,
                                          session_id=_state["session_id"],
                                          project_id=project_id),
                 depends_on=_event_depends(project_id))
    except Exception:  # noqa: BLE001
        _logger.exception("イベントの記録に失敗")


# イベントは sessionId / projectId が未登録でも 400 にはならないが、
# 関連付けが黙って外れる。依存を書いておき、登録が済むまで待たせる。
def _event_depends(project_id):
    depends = {}
    if _state["session_id"]:
        depends["session"] = _state["session_id"]
    if project_id:
        depends["project"] = project_id
    return depends


# ---- エラー (resolve1 §7.5) ------------------------------------------------
# module はモジュール名だけ (パスを入れない)。message は必ずマスクを通る。
def report_error(module, error):
    if not (_state["enabled"] and _state["errors"] and _state["queue"] is not None):
        return

    try:
        _enqueue(log_queue.TABLE_ERRORS,
                 payloads.error_report(module, type(error).__name__, str(error)))
    except Exception:  # noqa: BLE001
        _logger.exception("エラーログの記録に失敗")


# ---- 内部 ------------------------------------------------------------------
def _analytics():
    return bool(_state["enabled"] and _state["analytics"] and _state["queue"] is not None)


def _enqueue(table, built, depends_on=None):
    if not built:
        return None
    path, body = built
    return _state["queue"].enqueue(table, path, body, depends_on=depends_on)


# 出力キャンバスの寸法から 1920x1080 形式を作る。
# output_profile があればそれを使い (縦動画で寸法が変わる)、無ければ設定値を使う。
def _resolution_of(output_profile, settings):
    profile = output_profile if isinstance(output_profile, dict) else {}
    text = payloads.resolution(profile.get("width"), profile.get("height"))
    if text:
        return text

    ffmpeg_cfg = (settings or {}).get("ffmpeg", {}) if isinstance(settings, dict) else {}
    return payloads.resolution(ffmpeg_cfg.get("output_width"),
                               ffmpeg_cfg.get("output_height"))


# 入力動画の長さ (秒)。取れなければ 0.0。
# ffprobe を 1 回呼ぶが、この後に分単位の書き出しが走るため実質無視できる。
def _probe_length(input_path, settings):
    if not input_path:
        return 0.0
    try:
        from ..modules import ffmpeg_runner
        return float(ffmpeg_runner.probe_duration(
            input_path, (settings or {}).get("ffmpeg", {})))
    except Exception:  # noqa: BLE001 (長さが取れなくても記録は続ける)
        return 0.0


# パスに対応する projectId を返す。無ければ採番して覚える。
def _project_id_for(key):
    if not key:
        return None

    normalized = os.path.normcase(os.path.abspath(str(key)))
    path = _path(_PROJECT_IDS_FILE)

    with _lock:
        store = _read_json(path)
        if not isinstance(store, dict):
            store = {}

        known = store.get(normalized)
        if known:
            return str(known)

        project_id = payloads.new_id()
        store[normalized] = project_id

        # 古いものから捨てる (dict は挿入順を保つ)
        while len(store) > _MAX_PROJECT_IDS:
            store.pop(next(iter(store)))

        _write_json(path, store)
        return project_id


def _path(name):
    base = _state["base_dir"] or os.path.dirname(default_auth_path())
    return os.path.join(base, name)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, value):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
    except OSError as e:
        _logger.warning("ログ送信用の状態を保存できませんでした (%s): %s",
                        os.path.basename(path), e)


def _remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
