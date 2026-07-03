# 音量解析・カット閾値の確認ダイアログ
# resolve7 §10 に対応
# 無音カット前に解析した「発話区間の最低dB」を編集可能なテキストボックスに表示する。
# OK 時は入力値を確定カット閾値として返し、「変更しない」時は None を返す
# (None はパイプライン中断ではなく、既定値での続行を意味する)。
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

# ウィンドウ既定幅
_DIALOG_MIN_WIDTH = 380


# カット閾値確認ダイアログ
# initial_db   : テキストボックスの初期表示値 (整数)
# measured_db  : 解析で測定された最低dB (None=測定不能)
# region_count : 測定対象とした発話候補区間数
class VolumeThresholdDialog(QDialog):

    # 初期化
    def __init__(self, initial_db, measured_db=None, region_count=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("音量解析 — カット閾値の確認")
        self.setMinimumWidth(_DIALOG_MIN_WIDTH)
        self._initial_db = int(initial_db)
        self._build_ui(measured_db, region_count)

    # 画面構築
    def _build_ui(self, measured_db, region_count):
        root = QVBoxLayout(self)

        # 説明
        root.addWidget(QLabel(
            "入力動画を解析しました。この dB 以下をカット、\n"
            "以上を動画として使用します。値は手修正できます。"
        ))

        # 閾値入力欄 (整数のみ・負値可)
        row = QHBoxLayout()
        row.addWidget(QLabel("発話区間の最低dB:"))
        self.db_edit = QLineEdit(str(self._initial_db))
        self.db_edit.setValidator(QIntValidator())
        row.addWidget(self.db_edit)
        row.addWidget(QLabel("dB"))
        root.addLayout(row)

        # 補助情報 (測定値・対象区間数)
        if measured_db is not None:
            info = f"（測定値: {int(measured_db)} dB / 対象区間: {region_count}）"
        else:
            info = "（自動測定できませんでした。既定値を表示しています）"
        root.addWidget(QLabel(info))

        # ボタン (OK=確定保存 / 変更しない=既定で続行)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.ok_button = QPushButton("OK")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        self.keep_button = QPushButton("変更しない")
        self.keep_button.clicked.connect(self.reject)
        button_row.addWidget(self.ok_button)
        button_row.addWidget(self.keep_button)
        root.addLayout(button_row)

    # OK 時の入力値(int)を返す。空・不正時は初期値へフォールバックする
    def result_value(self):
        text = self.db_edit.text().strip()
        try:
            return int(text)
        except (TypeError, ValueError):
            return self._initial_db
