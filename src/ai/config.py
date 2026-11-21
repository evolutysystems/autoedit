# ai セクションの読み出し (docs/AiEditor/resolve1.md §16 / R14-a)
#
# setting.json の _merge_with_defaults はセクション単位の浅いマージであり、
# 入れ子の欠落キーを補完しない。そのため **入れ子の既定への落とし込みはここで行う**
# (既存の archive/config.py と同じ作法)。
#
# 既定値の出どころは settings_window.DEFAULT_SETTINGS["ai"] 1 か所であり、
# ここの DEFAULT_AI は「settings を渡されなかった / セクションが欠けた」ときの保険として
# 同じ値を持つ。両者が食い違っていないことは tests/test_ai_config.py が見る。
#
# 鍵はここには無い。Anthropic の API キーはサーバー (StretheusAPI) だけが持つ (§6.1)。
import copy

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 役割 (サーバーへ送る role の素)。
ROLE_SCENE_EVAL = "scene_eval"
ROLE_PLAN = "plan"
ROLE_REVIEW = "review"
ROLE_PROFILE = "profile"
ROLE_CLASSIFY = "classify"

# 設定上のモデルキー。役割と 1 対 1 ではない
# (review_critic は役割 review のまま、より強いモデルを希望する)。
MODEL_KEY_REVIEW_CRITIC = "review_critic"

# 許可する effort。これ以外はサーバーが役割の既定へ落とす (§6.3)。
_EFFORTS = ("low", "medium", "high")

DEFAULT_AI = {
    "enabled": False,
    "direct_mode": False,
    "endpoint": {
        "complete": "/api/ai/complete",
        "count": "/api/ai/count",
        "config": "/api/ai/config",
        "jobs": "/api/ai/jobs",
    },
    "models": {
        "scene_eval": "claude-sonnet-5-5",
        "plan": "claude-opus-5-5",
        "review": "claude-sonnet-5-5",
        "review_critic": "claude-opus-5-5",
        "profile": "claude-sonnet-5-5",
        "classify": "claude-haiku-5-5",
    },
    "effort": {
        "scene_eval": "medium",
        "plan": "high",
        "review": "medium",
        "profile": "medium",
        "classify": "low",
    },
    "max_output_tokens": 8192,
    "timeout_sec": 180,
    "retry": {"max_attempts": 3, "backoff_sec": 2.0},
    "jobs": {"wait_sec": 900, "poll_min_ms": 1000},
    "consent": {"accepted_version": 0},
    "privacy": {"include_comment_user": False, "include_transcript": True},
    "candidates": {
        "top_n": 30, "allow_overlap": False, "min_score": 0,
        "window_sec": 0, "transcribe_pad_sec": 15.0,
    },
    "transcribe": {"speaker_filter": True, "reuse_existing": True},
    "sweep": {"enabled": False, "chunk_sec": 600},
    "scene_eval": {
        "batch_size": 8, "max_reason_chars": 120, "base_score_weight": 0.2,
        "weights": {
            "long": {"funny": 0.30, "surprise": 0.15, "first_time_clarity": 0.15,
                     "excitement": 0.20, "shorts_fit": 0.0, "long_fit": 0.20,
                     "standalone": 0.0, "context_dependency": -0.10},
            "shorts": {"funny": 0.30, "surprise": 0.20, "first_time_clarity": 0.15,
                       "excitement": 0.10, "shorts_fit": 0.25, "long_fit": 0.0,
                       "standalone": 0.20, "context_dependency": -0.20},
        },
    },
    "plan": {
        "targets": ["youtube_long", "youtube_shorts", "tiktok"],
        "youtube_long": {"min_sec": 180, "max_sec": 900, "max_scenes": 12},
        "youtube_shorts": {"min_sec": 15, "max_sec": 60, "max_scenes": 5},
        "tiktok": {"min_sec": 15, "max_sec": 60, "max_scenes": 5},
        "subtitle_mode": "full",
        "variants": 1,
        "allow_reuse_scene": True,
    },
    "sounds": {"enabled": True, "presets": {}},
    "review": {
        "enabled": False,
        "roles": {"director": True, "critic": True, "viewer": True,
                  "platform": True, "qa": False},
        "max_rounds": 2, "max_issues_per_round": 5, "min_severity": "major",
    },
    "vision": {
        "enabled": True, "frames_per_scene": 1, "max_frames": 12,
        "max_edge_px": 768, "points": ["peak", "head"],
    },
    "report": {
        "enabled": True, "format": "pdf", "auto_open": False,
        "font": "", "include_frames": True, "name_suffix": "_review",
    },
    "cost": {"max_calls_per_project": 20, "max_input_tokens_per_project": 300000,
             "confirm_over_points": 0},
    "budget": {
        "max_input_tokens": 60000, "chars_per_token": 2.0, "cell_pad_sec": 60,
        "curve_window_span": 2, "max_comments_per_cell": 20,
        "merge_transcript_gap_sec": 1.0, "cache_prefix": True,
    },
    "learning": {
        "enabled": True, "upload": False, "operation_log": False,
        "min_evidence": 10, "match_tolerance_sec": 0.3,
        "keep_days": 180, "exclude": [],
    },
    "profile": {"active": {"youtube_long": "", "youtube_shorts": "", "tiktok": ""}},
    "debug": {"dump_payloads": False},
}


# ai セクションを既定で埋めた辞書として返す。
#
# 欠落キーは既定へ落とし、型の違う値は既定の型へ寄せる (壊れた setting.json でも落とさない)。
# 既定に無いキーはそのまま残す (将来キーを足したときに古いクライアントが消さないため)。
def ai_config(settings):
    section = (settings or {}).get("ai") if isinstance(settings, dict) else None

    return _fill(section if isinstance(section, dict) else {}, DEFAULT_AI)


# AI 機能が有効か。これが false のとき呼び出し側は ai/ の他のモジュールへ触らない。
def is_enabled(settings):
    return bool(ai_config(settings)["enabled"])


# 開発用の直接呼び出しか (§6.5)。配布では false。
def is_direct_mode(settings):
    return bool(ai_config(settings)["direct_mode"])


# エンドポイントのパスを返す (complete / count / config / jobs)。
def endpoint_path(config, name):
    paths = config.get("endpoint", {})
    default = DEFAULT_AI["endpoint"].get(name, "")
    value = paths.get(name, default)

    return value if isinstance(value, str) and value.strip() else default


# 役割 (またはモデルキー) に対する希望モデル。
# 希望であり、最終決定はサーバー (§6.1)。空文字ならサーバーの既定に任せる。
def model_hint(config, model_key):
    models = config.get("models", {})
    value = models.get(model_key)
    if not isinstance(value, str) or not value.strip():
        # review_critic のような派生キーが未設定なら、素の役割の希望へ落とす。
        value = models.get(_base_role(model_key), "")

    return value.strip() if isinstance(value, str) else ""


# 役割に対する effort。許可値でなければ空を返し、サーバーの役割既定に委ねる。
def effort_hint(config, model_key):
    efforts = config.get("effort", {})
    value = efforts.get(model_key)
    if not isinstance(value, str) or not value.strip():
        value = efforts.get(_base_role(model_key), "")
    value = value.strip().lower() if isinstance(value, str) else ""

    return value if value in _EFFORTS else ""


# モデルキーからサーバーへ送る役割名を決める (review_critic → review)。
def _base_role(model_key):
    key = str(model_key or "")

    return key.split("_critic")[0] if key.endswith("_critic") else key


# サーバーへ送る役割名 (snake_case)。
def role_of(model_key):
    return _base_role(model_key)


# 1 回の出力トークンの希望上限。サーバーが役割と全体の上限でクランプする。
def max_output_tokens(config):
    return _positive_int(config.get("max_output_tokens"), DEFAULT_AI["max_output_tokens"])


# 1 回の HTTP 呼び出しのタイムアウト (秒)。
def timeout_sec(config):
    return _positive_float(config.get("timeout_sec"), DEFAULT_AI["timeout_sec"])


# 再試行の回数と待ち時間 (§6.2)。
def retry_policy(config):
    retry = config.get("retry", {})

    return (
        _positive_int(retry.get("max_attempts"), DEFAULT_AI["retry"]["max_attempts"]),
        _positive_float(retry.get("backoff_sec"), DEFAULT_AI["retry"]["backoff_sec"]),
    )


# 202 で受理された処理を待つ上限 (秒) とポーリング間隔の下限 (秒)。
def job_policy(config):
    jobs = config.get("jobs", {})
    poll_min_ms = _positive_int(jobs.get("poll_min_ms"), DEFAULT_AI["jobs"]["poll_min_ms"])

    return (
        _positive_float(jobs.get("wait_sec"), DEFAULT_AI["jobs"]["wait_sec"]),
        poll_min_ms / 1000.0,
    )


# 1 プロジェクトで使う量の上限 (呼び出し回数 / 入力トークン)。0 は無制限。
def cost_limits(config):
    cost = config.get("cost", {})

    return (
        _non_negative_int(cost.get("max_calls_per_project"),
                          DEFAULT_AI["cost"]["max_calls_per_project"]),
        _non_negative_int(cost.get("max_input_tokens_per_project"),
                          DEFAULT_AI["cost"]["max_input_tokens_per_project"]),
    )


# 送信本文を作業フォルダへ落とすか (既定オフ / §17.2)。
def dumps_payloads(config):
    return bool(config.get("debug", {}).get("dump_payloads"))


# ---- 既定への落とし込み --------------------------------------------------

# 既定と同じ形へ寄せる。既定が辞書なら再帰し、それ以外は型を見て寄せる。
def _fill(value, default):
    if isinstance(default, dict):
        merged = {}
        source = value if isinstance(value, dict) else {}
        for key, default_value in default.items():
            merged[key] = _fill(source.get(key, default_value), default_value)

        # 既定に無いキーは落とさずに残す (将来の追加キーを消さない)。
        for key, extra in source.items():
            if key not in merged:
                merged[key] = copy.deepcopy(extra)

        return merged

    return _coerce(value, default)


def _coerce(value, default):
    if isinstance(default, bool):
        return value if isinstance(value, bool) else bool(default)
    if isinstance(default, int):
        return _coerce_int(value, default)
    if isinstance(default, float):
        return _coerce_float(value, default)
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, list):
        return copy.deepcopy(value) if isinstance(value, list) else copy.deepcopy(default)

    return value if value is not None else copy.deepcopy(default)


def _coerce_int(value, default):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default):
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _positive_int(value, default):
    number = _coerce_int(value, default)

    return number if number > 0 else default


def _non_negative_int(value, default):
    number = _coerce_int(value, default)

    return number if number >= 0 else default


def _positive_float(value, default):
    number = _coerce_float(value, default)

    return number if number > 0 else float(default)
