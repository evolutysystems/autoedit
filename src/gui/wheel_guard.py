# コンボ・数値入力がマウスホイールで勝手に変わるのを止める (error 20261122)
#
# Qt の QComboBox / QAbstractSpinBox は Windows 既定でホイールが効き、
# **フォーカスが無くてもカーソルが載っているだけで値が変わる**。
# しかも値を変えたうえで activated まで出すため、「利用者が選び直したときだけ
# 拾う」(ver9 resolve10 §3.5 の activated 化) では止められない。
#
# 設定画面はホイール 1 ノッチで「縁取りスタイル」が
# 縁取り+ドロップシャドウ → 不透明ボックス へ飛び、そのまま保存されると
# テロップの背後に縁の色で塗った箱が出る (error 20261122 §2.4)。
# 画面では分からないまま 1 本まるごと焼き上がるため、
# **ホイールでは値を変えない**ことにする。
#
# 値を変える口はクリック (一覧から選ぶ) と、フォーカスしたうえでの矢印キーに残す。
# ホイールで設定値を変えたい場面は無いと判断した
# (ver9 resolve10 §3.6 Q3 で「コンボをホイールで変えられなくして問題ない」と既に結論済み)。
from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractSpinBox, QComboBox

# 守る対象。QAbstractSpinBox は QSpinBox / QDoubleSpinBox の親。
_TARGET_TYPES = (QComboBox, QAbstractSpinBox)


# ホイールイベントだけを捨てるイベントフィルタ
#
# 画面側のウィジェットには一切手を入れない (差し替えや継承をしない) ため、
# 既存の画面構築コードをそのまま残したまま後から被せられる。
class WheelGuard(QObject):

    # ホイールなら捨てる (True = ここで止める)。それ以外は素通しする。
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and isinstance(watched, _TARGET_TYPES):
            event.ignore()
            return True
        return False


# root の配下にある対象ウィジェットすべてへガードを入れる。
#
# root 自身を親にしたガードを 1 つだけ作って使い回す (言語切替などで
# タブを組み直して呼び直されても、ガードが増えないようにするため)。
# 入れた数を返す (ログ・テスト用)。
def install(root):
    if root is None:
        return 0
    guard = root.findChild(WheelGuard)
    if guard is None:
        guard = WheelGuard(root)
    count = 0
    for widget_type in _TARGET_TYPES:
        for widget in root.findChildren(widget_type):
            # 同じフィルタを入れ直しても Qt 側で重複は持たない
            widget.installEventFilter(guard)
            count += 1
    if isinstance(root, _TARGET_TYPES):
        root.installEventFilter(guard)
        count += 1
    return count
