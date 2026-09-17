# モデルファイルの解決と ONNX セッションの生成 (ver5 resolve2 §3.7 / §8-6)
#
# onnxruntime は既に配布物へ入っているが、**遅延 import** する。
# 機能が無効な利用者の起動時間を 1ms も増やさないため (§4-1)。
# faster-whisper と同じ任意依存の作法 (subtitle_generator.py の前例) に合わせる。
#
# モデルが見つからない・読めないときは例外を投げず、理由の文字列を返す。
# 呼び出し側は「機能ごと無効化して理由を画面へ出す」だけでよい (§4-5)。
import os
import sys
import threading

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 生成済みセッションの共有 (同じモデルを解析のたびに読み直さない)。
# 解析は QThread から呼ばれるためロックで守る。
_sessions = {}
_lock = threading.Lock()


# モデル置き場の基準ディレクトリを返す (comment_decor.resolve_icon_path と同じ考え方)
# 非凍結: src/            凍結: sys._MEIPASS/src (datas で同梱したもの)
def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "src")
    # src/blur/models.py → src
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 設定のモデルパス (src/ からの相対、または絶対) を実体パスへ直す。
# 見つからなければ None。
def resolve_path(configured):
    path = str(configured or "").strip()
    if not path:
        return None
    if os.path.isabs(path):
        return path if os.path.isfile(path) else None
    full = os.path.join(_base_dir(), path.replace("/", os.sep))
    return full if os.path.isfile(full) else None


# モデルが使えるか調べる。戻り値 (使えるか, 理由の文字列)。
# 理由は設定画面とツールチップへそのまま出す (§5.6.1 / §5.8)。
def availability(cfg):
    model_cfg = cfg["model"]
    missing = []
    if resolve_path(model_cfg["detector"]) is None:
        missing.append(f"人物検出モデル ({model_cfg['detector']})")
    if resolve_path(model_cfg["reid"]) is None:
        missing.append(f"人物同定モデル ({model_cfg['reid']})")
    if missing:
        return False, "モデルが見つかりません: " + " / ".join(missing)
    if not is_runtime_available():
        return False, "onnxruntime が利用できないため、ぼかしの解析は行えません"
    return True, "モデル: 準備完了"


# onnxruntime を import できるか (遅延 import の可否確認)
def is_runtime_available():
    return _import_runtime() is not None


# onnxruntime を遅延 import する。失敗したら None を返す (例外にしない)。
def _import_runtime():
    try:
        import onnxruntime                      # noqa: PLC0415 (遅延 import が目的)
    except Exception as error:                  # noqa: BLE001 (未導入環境でも起動する)
        _logger.warning("onnxruntime を読み込めません: %s", error)
        return None
    return onnxruntime


# ONNX セッションを作る (同じパスなら使い回す)。読めなければ None。
def load_session(model_path, providers):
    if not model_path:
        return None
    key = (os.path.abspath(model_path), tuple(providers or ()))
    with _lock:
        if key in _sessions:
            return _sessions[key]

    runtime = _import_runtime()
    if runtime is None:
        return None

    try:
        options = runtime.SessionOptions()
        # 解析は背後で走る。プレビュー再生を邪魔しないようスレッドを絞る (§5.3.5)
        options.intra_op_num_threads = _worker_threads()
        options.log_severity_level = 3          # 警告以下は onnxruntime 側で出させない
        session = runtime.InferenceSession(
            model_path, sess_options=options, providers=list(providers or []))
    except Exception as error:                  # noqa: BLE001 (壊れたモデルで落とさない)
        _logger.error("モデルを読み込めません: %s (%s)", model_path, error)
        return None

    _logger.info("モデルを読み込みました: %s", os.path.basename(model_path))
    with _lock:
        _sessions[key] = session
    return session


# 推論に使うスレッド数。CPU を占有して画面が固まるのを避けるため半分までにする。
def _worker_threads():
    count = os.cpu_count() or 2
    return max(count // 2, 1)


# 保持しているセッションを捨てる (設定でモデルを差し替えたときに呼ぶ)
def reset():
    with _lock:
        _sessions.clear()
