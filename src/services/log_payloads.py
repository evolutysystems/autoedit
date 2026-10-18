# 送信するログの本文組み立てとマスク (request1 / resolve1 §7 / §8)
#
# API の検証規則 (resolve1 §3.3) をここで通し、**通らない本文は作らない**。
# 400 を貰ってから error フォルダへ落とすより、積む前に捨てる方が安い。
#
# マスク (§8) はここだけに置く。例外メッセージはパスを含みうるため
# (`Could not find file 'C:/Users/名前/input.mp4'`)、送信側でマスクするのが
# 約束である (security.md F3 / FU6)。サーバーにマスクの仕組みは無い。
import os
import re
import uuid
from datetime import datetime, timezone

# ---- エンドポイント ---------------------------------------------------------
PATH_SESSIONS = "/api/analytics/sessions"
PATH_PROJECTS = "/api/analytics/projects"
PATH_EVENTS = "/api/analytics/events"
PATH_EXPORT_LOGS = "/api/analytics/export-logs"
PATH_PROCESSING_LOGS = "/api/analytics/processing-logs"
PATH_ERRORS = "/api/errors"


def session_end_path(session_id):
    return f"{PATH_SESSIONS}/{session_id}/end"


# ---- イベント名 (resolve1 §7.4) ---------------------------------------------
# サーバーの検証は ^[A-Za-z][A-Za-z0-9_.\-]{0,127}$ なので日本語は使えない。
# 集計の軸になるため文字列を散らさず、ここの定数だけを使う。
EVENT_APP_START = "AppStart"
EVENT_TIMELINE_OPEN = "TimelineOpen"
EVENT_TIMELINE_DECIDE = "TimelineDecide"
EVENT_EXPORT_START = "ExportStart"
EVENT_EXPORT_SUCCEEDED = "ExportSucceeded"
EVENT_EXPORT_FAILED = "ExportFailed"
EVENT_EXPORT_CANCELLED = "ExportCancelled"
EVENT_PROJECT_RESUME = "ProjectResume"
EVENT_WATERMARK_ACCEPTED = "WatermarkAccepted"
EVENT_WATERMARK_DECLINED = "WatermarkDeclined"

# ---- 工程名 (resolve1 §7.6) -------------------------------------------------
# PipelineContext.begin_step / end_step へ渡される日本語ラベルを固定の英字名へ写す。
# ここに無いラベルは送らない (ProcessName 列に日本語を混ぜて集計の軸を揺らさない)。
PROCESS_NAMES = {
    "音声解析・正規化": "Normalize",
    "無音カット": "SilenceCut",
    "無音検出": "SilenceDetect",
    "フルテロップ生成": "Subtitle",
    "音声認識": "Transcribe",
    "オープニング結合": "ConcatOpening",
    "エンディング結合": "ConcatEnding",
    "Timeline レンダリング": "Render",
    "素材の復旧": "MediaRecovery",
}

# ---- 検証規則 (サーバーと同じもの) ------------------------------------------
_EVENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,127}$")
_RESOLUTION_RE = re.compile(r"^\d{1,5}x\d{1,5}$")

MAX_MODULE_CHARS = 128
MAX_EXCEPTION_TYPE_CHARS = 256
MAX_PROCESS_NAME_CHARS = 128
MAX_CODEC_CHARS = 64

# サーバーは 1000 文字で切るが、送る前にさらに短くする (resolve1 §8)。
MAX_MESSAGE_CHARS = 500

# 1 リクエストに載せられるイベント件数 (サーバーの MaxEventsPerRequest)。
MAX_EVENTS_PER_REQUEST = 500


# ---- マスク (resolve1 §8) ---------------------------------------------------
# 置換は順番に効かせる。パスを先に潰すと、その中のユーザー名も一緒に消える。
_MASK_RULES = (
    # UNC パス (\\nas\share\a.mp4)
    (re.compile(r"\\\\[^\s\"'<>|]+"), "<path>"),
    # URL のクエリ (?code=...&state=...)。パスより先に潰す。
    # 後にすると "https://..." のクエリまで <path> に飲まれて消える。
    (re.compile(r"\?[^\s\"'<>|]+"), "?<query>"),
    # メールアドレス
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<email>"),
    # ドライブ付きパス (C:\Users\... / C:/Users/...)。
    # 直前が英字のときは除く。"https://" の "s:/" をドライブと見ないため。
    (re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|]*"), "<path>"),
    # 32 文字以上の 16 進・Base64 風の連続 (トークン・ハッシュ)
    (re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b"), "<token>"),
)


# 例外メッセージなど、自由文をマスクして短く切る。
# None / 空文字はそのまま None を返す (送らない)。
def mask_message(text, max_chars=MAX_MESSAGE_CHARS):
    if not text:
        return None

    masked = str(text).replace("\r", " ").replace("\n", " ")
    for pattern, replacement in _MASK_RULES:
        masked = pattern.sub(replacement, masked)

    masked = _mask_user_name(masked)
    masked = re.sub(r"\s{2,}", " ", masked).strip()
    if not masked:
        return None

    return masked[:max_chars]


# パスの外に素で出たユーザー名を潰す。環境変数から取れる名前だけを対象にする。
def _mask_user_name(text):
    for key in ("USERNAME", "USER"):
        name = os.environ.get(key, "")
        if len(name) < 3:
            continue
        text = re.sub(re.escape(name), "<user>", text, flags=re.IGNORECASE)
    return text


# ---- 小道具 ----------------------------------------------------------------
def new_id():
    return str(uuid.uuid4())


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def is_valid_event_name(name):
    return bool(name) and bool(_EVENT_NAME_RE.match(str(name)))


def is_valid_resolution(value):
    return bool(value) and bool(_RESOLUTION_RE.match(str(value)))


# 幅と高さから 1920x1080 形式を作る。作れなければ None。
def resolution(width, height):
    try:
        text = f"{int(width)}x{int(height)}"
    except (TypeError, ValueError):
        return None
    return text if is_valid_resolution(text) else None


def _clip(value, limit):
    return str(value or "")[:limit]


def _ms(value):
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def _iso(value):
    if value is None:
        return utc_now_iso()
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    return str(value)


# ---- 本文 (いずれも (path, body) か None を返す) ----------------------------
# セッション開始 (resolve1 §7.1)。sessionId はクライアント採番。
def session_start(session_id, start_at=None):
    if not session_id:
        return None
    return PATH_SESSIONS, {
        "sessionId": str(session_id),
        "startAt": _iso(start_at),
    }


# セッション終了。未登録のセッションへ送ると 404 になるため、
# 呼び出し側は dependsOn {"session": sessionId} を付けて開始の完了を待つ。
def session_end(session_id, end_at=None, crash=False):
    if not session_id:
        return None
    return session_end_path(session_id), {
        "endAt": _iso(end_at),
        "crash": bool(crash),
    }


# プロジェクトのメタデータ (resolve1 §7.2)。upsert なので再送しても増えない。
# resolution が作れない呼び出しは**積まない** (400 になるため)。
def project_upsert(project_id, video_length, resolution_text, fps, codec):
    if not project_id or not is_valid_resolution(resolution_text):
        return None

    try:
        length = float(video_length or 0.0)
        frame_rate = float(fps or 0.0)
    except (TypeError, ValueError):
        return None

    return PATH_PROJECTS, {
        "projectId": str(project_id),
        "videoLength": round(length, 3),
        "resolution": str(resolution_text),
        "fps": round(frame_rate, 3),
        "codec": _clip(codec, MAX_CODEC_CHARS),
    }


# Analytics イベント 1 件 (resolve1 §7.4)。送信時に配列へまとめる。
def analytics_event(name, when=None, duration_ms=None, session_id=None, project_id=None):
    if not is_valid_event_name(name):
        return None

    body = {"event": str(name), "time": _iso(when)}
    if duration_ms is not None:
        body["duration"] = _ms(duration_ms)
    if session_id:
        body["sessionId"] = str(session_id)
    if project_id:
        body["projectId"] = str(project_id)

    return PATH_EVENTS, body


# 書き出し 1 本の記録 (resolve1 §7.3)。
def export_log(project_id, resolution_text, codec, success, duration_ms):
    if not project_id or not is_valid_resolution(resolution_text):
        return None

    return PATH_EXPORT_LOGS, {
        "projectId": str(project_id),
        "resolution": str(resolution_text),
        "codec": _clip(codec, MAX_CODEC_CHARS),
        "success": bool(success),
        "duration": _ms(duration_ms),
    }


# 工程 1 つの記録 (resolve1 §7.6)。process_name は PROCESS_NAMES の値。
def processing_log(project_id, process_name, duration_ms, success):
    if not project_id or not process_name:
        return None

    return PATH_PROCESSING_LOGS, {
        "projectId": str(project_id),
        "processName": _clip(process_name, MAX_PROCESS_NAME_CHARS),
        "duration": _ms(duration_ms),
        "success": bool(success),
    }


# エラーレポート (resolve1 §7.5)。message は必ずマスクを通す。
#
# ProjectId / SessionId / AppVersion は API が受け取るが **保存しない**
# (ErrorLog に列が無い / resolve1 §3.5 / §13 Q4)。送らない。
def error_report(module, exception_type, message):
    module_name = _clip(module, MAX_MODULE_CHARS)
    type_name = _clip(exception_type, MAX_EXCEPTION_TYPE_CHARS)
    if not module_name or not type_name:
        return None

    return PATH_ERRORS, {
        "module": module_name,
        "exceptionType": type_name,
        "message": mask_message(message),
    }
