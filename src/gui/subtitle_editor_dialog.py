# 字幕編集画面 (フルテロップ後の確認・編集ダイアログ)
# resolve3 §8 に対応
# 生成テロップを一覧表示し、テキスト修正・使用可否の選択を行う。
# 列構成: 時間(表示のみ) / 字幕(編集可) / 使用(チェックボックス・初期全チェック)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

# 既存の ASS タイムスタンプ整形を再利用し、画面・ASS で表記を揃える (§8.3)
from ..modules.subtitle_generator import _format_ass_time

# 列インデックス定義 (マジックナンバー回避)
_COL_TIME = 0
_COL_TEXT = 1
_COL_USE = 2
_COLUMN_HEADERS = ["時間", "字幕", "使用"]

# ウィンドウ既定サイズ
_DIALOG_WIDTH = 720
_DIALOG_HEIGHT = 520


# 開始/終了秒を「H:MM:SS.cc → H:MM:SS.cc」形式の表示文字列にする
def _format_time_range(start, end):
    return f"{_format_ass_time(start)} → {_format_ass_time(end)}"


# 字幕編集ダイアログ
# items: [{"start": float, "end": float, "text": str, "use": bool}]
class SubtitleEditorDialog(QDialog):

    # 初期化 (items の use 初期値は呼び出し側で全件 True を渡す想定)
    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.setWindowTitle("字幕編集")
        self.resize(_DIALOG_WIDTH, _DIALOG_HEIGHT)

        # start/end は編集不可のため元値を保持しておき、確定時にそのまま返す
        self._items = [dict(item) for item in items]

        self._build_ui()
        self._populate(self._items)

    # 画面構築
    def _build_ui(self):
        root = QVBoxLayout(self)

        # 説明ラベル
        root.addWidget(QLabel(
            "誤訳の修正と使用可否を選択し、「字幕決定」で焼き込みに進みます。\n"
            "「時間」は編集できません。チェックを外した字幕は焼き込まれません。"
        ))

        # 一覧テーブル
        self.table = QTableWidget(self)
        self.table.setColumnCount(len(_COLUMN_HEADERS))
        self.table.setHorizontalHeaderLabels(_COLUMN_HEADERS)
        # 字幕列を広く取り、他列は内容に合わせる
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_COL_TIME, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_TEXT, QHeaderView.Stretch)
        header.setSectionResizeMode(_COL_USE, QHeaderView.ResizeToContents)
        # 行全体ではなくセル単位で編集させる
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        root.addWidget(self.table)

        # 全選択 / 全解除 (利便用)
        select_row = QHBoxLayout()
        self.select_all_button = QPushButton("全て使用")
        self.select_all_button.clicked.connect(lambda: self._set_all_use(True))
        self.deselect_all_button = QPushButton("全て不使用")
        self.deselect_all_button.clicked.connect(lambda: self._set_all_use(False))
        select_row.addWidget(self.select_all_button)
        select_row.addWidget(self.deselect_all_button)
        select_row.addStretch(1)
        root.addLayout(select_row)

        # 決定 / キャンセル
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.decide_button = QPushButton("字幕決定")
        self.decide_button.setDefault(True)
        self.decide_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.decide_button)
        button_row.addWidget(self.cancel_button)
        root.addLayout(button_row)

    # items をテーブルへ反映する
    def _populate(self, items):
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            # 時間列: 表示のみ (編集不可)
            time_item = QTableWidgetItem(_format_time_range(item["start"], item["end"]))
            time_item.setFlags(time_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, _COL_TIME, time_item)

            # 字幕列: 編集可。ASS の改行 \N を表示用に実改行へ変換する
            text_item = QTableWidgetItem(str(item.get("text", "")).replace("\\N", "\n"))
            self.table.setItem(row, _COL_TEXT, text_item)

            # 使用列: チェックボックス (初期値は items の use)
            use_item = QTableWidgetItem()
            use_item.setFlags(
                (use_item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable
            )
            use_item.setCheckState(
                Qt.Checked if item.get("use", True) else Qt.Unchecked
            )
            use_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, _COL_USE, use_item)

        self.table.resizeRowsToContents()

    # 全行の使用チェックを一括設定する
    def _set_all_use(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.table.rowCount()):
            use_item = self.table.item(row, _COL_USE)
            if use_item is not None:
                use_item.setCheckState(state)

    # 「字幕決定」確定後に編集結果を返す
    # 戻り値: [{"start", "end", "text", "use"}] (start/end は不変、text/use は編集反映)
    def result_items(self):
        results = []
        for row, original in enumerate(self._items):
            text_item = self.table.item(row, _COL_TEXT)
            use_item = self.table.item(row, _COL_USE)
            # 表示用の実改行を ASS の \N へ戻す
            text = "" if text_item is None else text_item.text().replace("\n", "\\N")
            use = use_item is not None and use_item.checkState() == Qt.Checked
            results.append({
                "start": original["start"],
                "end": original["end"],
                "text": text,
                "use": use,
            })
        return results
