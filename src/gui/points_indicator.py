# 残高インジケータ (ver5 resolve.md §5.6 R11)
#
# メイン画面の右上へ常時置き、残高と次回リセット日を出す。
#   ・未ログイン        → 「ログイン」ボタン (課金は任意。押さなければ従来どおり使える)
#   ・サブスク会員      → 「サブスク特典で無制限」
#   ・オフライン        → 最後に取得できた値を薄字で出す (取れていなければ断り書き)
#
# 取得は UI スレッドを止めないよう QThread で行い、失敗しても画面は開いたままにする。
# ポイント処理そのものと同じく、ここでの失敗は出力を妨げない (§4-3)。
import threading

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from ..services.stretheus_auth import parse_timestamp
from ..utils.logger import get_logger
from . import theme

_logger = get_logger(__name__)

# 残高の再取得間隔の下限 (秒)。設定が極端な値でも画面が張り付かないようにする。
_MIN_REFRESH_SEC = 30

# ラベルとボタンの間隔 (px)
_GAP_PX = 8

# 透かしが入ることの確認文 (R12)。動画出力と Resolve 出力で言い回しを分ける。
WATERMARK_MESSAGE = (
    "ポイントが不足しているため、この出力には透かし (watermark) が入ります。\n"
    "続行しますか？")
WATERMARK_MESSAGE_RESOLVE = (
    "ポイントが不足しているため、この書き出しには透かしのクリップが入ります。\n"
    "続行しますか？")


# その場 (GUI スレッド) で確認を出す関数を返す (R12)。
# ワーカースレッドから呼ぶ場合は WatermarkConfirmBridge を使う
# (GUI スレッドで待ち合わせると画面が止まるため)。
def watermark_confirm(parent=None, message=WATERMARK_MESSAGE):
    def _confirm():
        answer = QMessageBox.question(
            parent, "ポイントが不足しています", message,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    return _confirm


# 残高の取得をワーカースレッドで行う。
# 送れずに残っていた commit / cancel の再送もここで片付ける (R14 の「次回起動時」)。
class BalanceWorker(QThread):

    # 取得できた残高 (dict) / 取れなければ None を薄字表示のために空 dict で通知する
    loaded = Signal(object)

    def __init__(self, points, force=True, parent=None):
        super().__init__(parent)
        self._points = points
        self._force = force

    def run(self):
        balance = None
        try:
            self._points.flush_pending()
            balance = self._points.balance(force=self._force)
        except Exception:  # noqa: BLE001 (残高表示の失敗で画面を壊さない)
            _logger.exception("残高の取得に失敗")
        self.loaded.emit(balance)


# ログイン / ログアウトをワーカースレッドで行う (認可待ちで UI を止めない)
class LoginWorker(QThread):

    # 成功 (ユーザー情報) / 失敗 (メッセージ)
    logged_in = Signal(object)
    failed = Signal(str)

    def __init__(self, auth, timeout=180, parent=None):
        super().__init__(parent)
        self._auth = auth
        self._timeout = timeout

    def run(self):
        try:
            self.logged_in.emit(self._auth.login(timeout=self._timeout))
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知するため広く捕捉)
            _logger.exception("ログインに失敗")
            self.failed.emit(str(e))


# 次回リセット日を「10/11」の形にする (UTC → ローカル)
def format_reset(balance):
    reset_at = parse_timestamp((balance or {}).get("nextResetAt"))
    if reset_at is None:
        return ""
    return reset_at.astimezone().strftime("%m/%d").lstrip("0").replace("/0", "/")


# 残高の表示文を組み立てる (アカウントタブでも使う)
def format_balance(balance):
    if not balance:
        return "残高を取得できません"
    if balance.get("unlimited"):
        return "サブスク特典で無制限"

    available = balance.get("available")
    if available is None:
        available = balance.get("balance", 0)
    reset = format_reset(balance)
    text = f"残り {int(available)} pt"
    return f"{text} / 次回リセット {reset}" if reset else text


class PointsIndicator(QWidget):

    # ログイン状態が変わった (設定画面のアカウントタブと合わせるため)
    login_changed = Signal(bool)

    def __init__(self, points, settings=None, parent=None):
        super().__init__(parent)
        self._points = points
        self._worker = None
        self._login_worker = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_GAP_PX)

        self.label = QLabel("")
        theme.mark_note(self.label)
        layout.addWidget(self.label)

        self.login_button = QPushButton("ログイン")
        self.login_button.setToolTip("Stretheus アカウントへログインします")
        self.login_button.clicked.connect(self._on_login)
        layout.addWidget(self.login_button)

        # 定期更新 (既定 300 秒)。設定値が小さすぎる場合は下限へ丸める。
        interval = int(max((settings or {}).get("points", {}).get("balance_refresh_sec", 300)
                           or 300, _MIN_REFRESH_SEC))
        self._timer = QTimer(self)
        self._timer.setInterval(interval * 1000)
        self._timer.timeout.connect(lambda: self.refresh(force=True))
        self._timer.start()

        self._apply_state()
        self.refresh(force=True)

    # 残高を取り直す。未ログインなら何もしない。
    def refresh(self, force=False):
        if not self._points.auth.is_logged_in():
            self._apply_state()
            return
        if self._worker is not None and self._worker.isRunning():
            return

        self._worker = BalanceWorker(self._points, force=force, parent=self)
        self._worker.loaded.connect(self._on_loaded)
        self._worker.start()

    # ログアウト後などに外から呼ぶ
    def reload(self):
        self._apply_state()
        self.refresh(force=True)

    def _on_loaded(self, balance):
        # オフラインなら最後に取得できた値を薄字で出す
        self._apply_state(balance if balance else self._points.last_balance(),
                          offline=not balance)

    def _apply_state(self, balance=None, offline=False):
        logged_in = self._points.auth.is_logged_in()
        self.login_button.setVisible(not logged_in)
        self.label.setVisible(logged_in)
        if not logged_in:
            self.label.setText("")
            return

        self.label.setText(format_balance(balance))
        # オフライン表示は薄字にする (無効化で灰色になる)
        self.label.setEnabled(not offline)
        self.label.setToolTip(
            "サーバーへ接続できないため、最後に取得した残高です" if offline else "")

    def _on_login(self):
        if self._login_worker is not None and self._login_worker.isRunning():
            return

        self.login_button.setEnabled(False)
        self.login_button.setText("ブラウザで許可してください…")
        self._login_worker = LoginWorker(self._points.auth, parent=self)
        self._login_worker.logged_in.connect(self._on_logged_in)
        self._login_worker.failed.connect(self._on_login_failed)
        self._login_worker.finished.connect(self._reset_login_button)
        self._login_worker.start()

    def _on_logged_in(self, user):
        _logger.info("ログインしました: %s", (user or {}).get("displayName", ""))
        self.login_changed.emit(True)
        self.reload()

    def _on_login_failed(self, message):
        QMessageBox.warning(self, "ログインできません",
                            f"ログインに失敗しました。\n{message}")

    def _reset_login_button(self):
        self.login_button.setEnabled(True)
        self.login_button.setText("ログイン")


# 透かし入りでの出力を利用者へ確認する (ver5 resolve §5.6 R12)
#
# 予約の応答を見てから出すため、ワーカースレッドから呼ばれる。
# BlurFailureBridge と同じ機構 (threading.Event によるブロッキング同期)。
# 既定は「続行」ではなく「取りやめ」にする。気づかずに透かし入りを出す方が損害が大きい。
class WatermarkConfirmBridge(QObject):

    confirm_requested = Signal()

    def __init__(self, parent_window=None):
        super().__init__()
        self._parent_window = parent_window
        self._event = threading.Event()
        self._result = False
        self.confirm_requested.connect(self._on_confirm_requested, Qt.QueuedConnection)

    # ワーカースレッドから呼ばれる。True = 透かし入りで続ける / False = 取りやめ
    def __call__(self):
        self._event.clear()
        self._result = False
        self.confirm_requested.emit()
        self._event.wait()
        return self._result

    def _on_confirm_requested(self):
        try:
            answer = QMessageBox.question(
                self._parent_window, "ポイントが不足しています",
                WATERMARK_MESSAGE, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            self._result = answer == QMessageBox.Yes
        finally:
            self._event.set()


