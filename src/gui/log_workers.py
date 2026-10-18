# ログ送信のワーカースレッド (request1 / resolve1 §4.1 / §6.4)
#
# billing_workers.py と同じ約束で、HTTP はすべてここ (ワーカースレッド) で行う。
# GUI スレッドから LogSender.flush() を直接呼ぶと、API へ到達できないときに
# タイムアウト (既定 15 秒 × 件数) ぶん画面が固まる。
#
# 優先度は最低で起動する。利用者の操作より後回しにするのが要件 (R3 / R6)。
from PySide6.QtCore import QThread, Signal

from ..utils.logger import get_logger

_logger = get_logger(__name__)


class LogFlushWorker(QThread):

    # 送信結果 (log_sender.FlushResult)。失敗しても必ず 1 回は飛ぶ。
    finished_with = Signal(object)

    def __init__(self, sender, parent=None):
        super().__init__(parent)
        self._sender = sender

    def run(self):
        result = None
        try:
            result = self._sender.flush()
        except Exception:  # noqa: BLE001 (ログ送信の失敗で画面を壊さない)
            _logger.exception("ログ送信に失敗")
        self.finished_with.emit(result)

    # 最低優先度で走らせる。既に走っていれば何もしない。
    def start_low(self):
        if self.isRunning():
            return
        self.start(QThread.LowestPriority)
