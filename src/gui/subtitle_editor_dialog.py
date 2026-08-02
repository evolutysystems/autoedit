# 字幕編集画面 (フルテロップ後の確認・編集ダイアログ)
# resolve3 §8 に対応
# 生成テロップを一覧表示し、テキスト修正・使用可否の選択を行う。
# 列構成: 時間(表示のみ) / 字幕(編集可) / 使用(チェックボックス・初期全チェック)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..exceptions import AutoEditError
from ..export import resolve_export
from .subtitle_preview_widget import SubtitlePreviewWidget, is_preview_enabled

# 既存の ASS タイムスタンプ整形を再利用し、画面・ASS で表記を揃える (§8.3)
from ..modules.subtitle_generator import _format_ass_time
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 列インデックス定義 (マジックナンバー回避)
_COL_TIME = 0
_COL_TEXT = 1
_COL_USE = 2
# テロップ役割列 (使用の右隣。1行につき1つだけ排他選択する / request10)
_COL_STREAMER = 3
_COL_SUB = 4
_COL_COMMENT = 5
# テロップ個別フォント/サイズ列 (役割の右隣 / request15 / resolve16 §4.4)
_COL_FONT = 6
_COL_SIZE = 7
_COLUMN_HEADERS = ["時間", "字幕", "使用", "配信者", "サブ", "コメント", "フォント", "サイズ"]

# フォント個別列のプレビュー用ポイントサイズ (画面表示専用 / resolve16 §4.1)
_FONT_PREVIEW_POINT_SIZE = 12
# サイズ入力欄の最大幅 (小さめのテキストボックス / 要望3)
_SIZE_FIELD_MAX_WIDTH = 60
# フォント列の Blank 選択 (=デフォルト使用) の userData
_FONT_BLANK_VALUE = ""

# 役割キー(内部識別子) と 列インデックスの対応。焼き込み時の色分けに使う。
# (role, 列インデックス) の順。既定選択は先頭の配信者(streamer)。
_ROLE_COLUMNS = [
    ("streamer", _COL_STREAMER),
    ("sub", _COL_SUB),
    ("comment", _COL_COMMENT),
]
# 既定役割 (役割未指定・未選択時のフォールバック)
_DEFAULT_ROLE = "streamer"

# ウィンドウ既定サイズ
_DIALOG_WIDTH = 720
_DIALOG_HEIGHT = 520
# プレビュー欄あり時の既定サイズ (resolve23 §5.3)
_DIALOG_PREVIEW_WIDTH = 1120
_DIALOG_PREVIEW_HEIGHT = 560

# 改行挿入に使う修飾キー (Alt / Shift + Enter で改行)
_NEWLINE_MODIFIERS = Qt.AltModifier | Qt.ShiftModifier

# DaVinci Resolve 出力ボタンの文言 (両画面で共通 / resolve20)
RESOLVE_EXPORT_BUTTON_TEXT = "DaVinci Resolve ファイル出力"


# 「DaVinci Resolve 出力」の共通処理 (クリップ用 字幕一覧 / アーカイブ用 結果画面 で共用)
# export_call(overwrite_confirm) -> 出力パスの list or None を呼び出し、結果を画面へ通知する。
# 例外は画面へ集約通知し、既存フロー (焼き込み・パイプライン) には影響させない (resolve20 §7)。
def run_resolve_export(parent, export_call):
    def _confirm_overwrite(path):
        answer = QMessageBox.question(
            parent, "上書き確認",
            f"既に同名のファイルがあります。上書きしますか?\n{path}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    try:
        result = export_call(_confirm_overwrite)
    except AutoEditError as e:
        QMessageBox.critical(parent, "DaVinci Resolve 出力", str(e))
        return None
    except Exception as e:  # noqa: BLE001 (GUI へ集約通知するため広く捕捉)
        _logger.exception("DaVinci Resolve 出力に失敗")
        QMessageBox.critical(
            parent, "DaVinci Resolve 出力", f"出力に失敗しました:\n{e}")
        return None

    if result is None:
        return None
    # 戻り値は出力パスの list ([fcpxml] または [fcpxml, srt] / resolve21 §5.7)
    paths = result if isinstance(result, list) else [result]
    message = (
        "プロジェクトファイルを出力しました。\n"
        + "\n".join(paths)
        + "\n\nDaVinci Resolve の File > Import > Timeline から取り込んでください。"
    )
    if any(str(p).lower().endswith(".srt") for p in paths):
        message += (
            "\n字幕(.srt) はメディアプール右クリック > 字幕の読み込み (Import Subtitle) "
            "からも取り込めます。\n字幕のフォント等はタイムラインの字幕トラックヘッダー選択 "
            "→ インスペクタ → トラックスタイルで一括設定できます。"
        )
    QMessageBox.information(parent, "DaVinci Resolve 出力", message)
    return paths


# 「字幕」セル内の複数行エディタ
# Alt+Enter / Shift+Enter で改行を挿入し、修飾なし Enter で編集確定する。
class _MultilineCellEdit(QPlainTextEdit):

    # 修飾なし Enter で編集確定 (コミット&クローズ) を要求するシグナル
    commit_requested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & _NEWLINE_MODIFIERS:
                # Alt / Shift + Enter → カーソル位置に改行を挿入
                self.insertPlainText("\n")
                return
            # 修飾なし Enter → 編集を確定する
            self.commit_requested.emit()
            return
        # それ以外のキー (Esc=破棄 等) は既定動作に委ねる
        super().keyPressEvent(event)


# 「字幕」列を複数行編集にするための delegate
# 既定の単一行 QLineEdit を複数行エディタに差し替え、改行入力を可能にする。
class _MultilineTextDelegate(QStyledItemDelegate):

    # 複数行エディタを生成する
    def createEditor(self, parent, option, index):
        editor = _MultilineCellEdit(parent)
        # 修飾なし Enter でコミットして閉じる
        editor.commit_requested.connect(lambda: self._commit_and_close(editor))
        return editor

    # エディタの内容を確定し、エディタを閉じる
    def _commit_and_close(self, editor):
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    # item のテキスト (既に実改行を含む) をエディタへ反映する
    def setEditorData(self, editor, index):
        editor.setPlainText(index.data(Qt.EditRole) or "")

    # エディタのテキスト (実改行を含む) を item へ書き戻す
    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText(), Qt.EditRole)

    # エディタをセル矩形に合わせて配置する
    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


# 開始/終了秒を「H:MM:SS.cc → H:MM:SS.cc」形式の表示文字列にする
def _format_time_range(start, end):
    return f"{_format_ass_time(start)} → {_format_ass_time(end)}"


# ラジオボタンをセル内で中央寄せするためのコンテナウィジェットを生成する
# (setCellWidget へ直接 QRadioButton を置くと左寄せになるため)
def _centered_widget(inner):
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addStretch(1)
    layout.addWidget(inner)
    layout.addStretch(1)
    return container


# 列インデックスから役割キーを逆引きする (未選択=-1 等は既定役割へフォールバック)
def _col_to_role(col):
    for role, c in _ROLE_COLUMNS:
        if c == col:
            return role
    return _DEFAULT_ROLE


# 既定役割 (_DEFAULT_ROLE) に対応する列インデックスを返す
def _role_default_column():
    for role, c in _ROLE_COLUMNS:
        if role == _DEFAULT_ROLE:
            return c
    return _ROLE_COLUMNS[0][1]


# 字幕編集ウィジェット (埋め込み可能な編集テーブル本体 / resolve17 §4.7.2)
# ダイアログ (SubtitleEditorDialog) とアーカイブ結果画面 (ArchiveResultWindow) の
# 双方から再利用する。テーブル・テーマ欄・全選択ボタンのみを持ち、説明ラベルや
# 決定/キャンセルボタンは埋め込み側 (ダイアログ等) が付与する。
# items: [{"start","end","text","use","role","font","font_size"}]
#   role は "streamer"/"sub"/"comment"。省略時は配信者。
# default_font/default_size: フォント/サイズ列の「Blank=デフォルト」時の実効値 (画面併記用)。
# font_families: フォント列の選択肢。省略時はインストール済フォント一覧。
# show_theme_field=True のときだけ上部にテーマ入力欄を出す (resolve19)。
class SubtitleEditorWidget(QWidget):

    def __init__(self, items=None, parent=None, default_font="", default_size=None,
                 font_families=None, show_theme_field=False, theme_placeholder="",
                 theme_text=""):
        super().__init__(parent)
        # テーマ欄の表示可否・プレースホルダ・初期値 (resolve19)
        self._show_theme_field = bool(show_theme_field)
        self._theme_placeholder = theme_placeholder or ""
        self._theme_text = theme_text or ""
        self.theme_edit = None  # show_theme_field=True のとき QLineEdit を割り当てる

        # start/end は編集不可のため元値を保持しておき、確定時にそのまま返す
        self._items = [dict(item) for item in (items or [])]
        # 行ごとの役割ラジオを排他管理する QButtonGroup を保持する (result 取得用)
        self._role_groups = []
        # 行ごとのフォント/サイズウィジェット参照 (result 取得用 / resolve16 §4.4)
        self._font_combos = []
        self._size_edits = []
        # 「Blank=デフォルト」時の実効値 (画面併記・戻り値の初期値には使わない)
        self._default_font = default_font or ""
        self._default_size = default_size
        # フォント列の選択肢 (追加フォント含む一覧を呼び出し側から受け取れる)
        self._font_families = (
            list(font_families) if font_families is not None
            else list(QFontDatabase.families())
        )

        self._build_ui()
        self._populate(self._items)

    # 「Blank=デフォルト」時の実効値を説明文用に整形する (resolve16 §4.4)
    def _default_desc(self):
        font = self._default_font or "(設定のフォント)"
        if self._default_size in (None, ""):
            return font
        return f"{font} / {self._default_size}"

    # 説明文 (埋め込み側が説明ラベルに使えるよう公開する)
    def description_text(self):
        return (
            "誤訳の修正と使用可否を選択します。\n"
            "「時間」は編集できません。チェックを外した字幕は焼き込まれません。\n"
            "各行の「配信者/サブ/コメント」で色を選べます(1行につき1つ)。\n"
            f"「フォント」「サイズ」は行ごとに上書きできます(空欄=デフォルト: {self._default_desc()})。"
        )

    # 画面構築 (テーマ欄 + テーブル + 全選択ボタン。説明/決定ボタンは埋め込み側が付ける)
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # テーマ入力欄 (resolve19 / アーカイブ切り抜き経路のみ)。
        if self._show_theme_field:
            theme_row = QHBoxLayout()
            theme_row.addWidget(QLabel("テーマ:"))
            self.theme_edit = QLineEdit()
            self.theme_edit.setPlaceholderText(self._theme_placeholder)
            self.theme_edit.setText(self._theme_text)
            theme_row.addWidget(self.theme_edit)
            root.addLayout(theme_row)

        # 一覧テーブル
        self.table = QTableWidget(self)
        self.table.setColumnCount(len(_COLUMN_HEADERS))
        self.table.setHorizontalHeaderLabels(_COLUMN_HEADERS)
        # 字幕列を広く取り、他列は内容に合わせる
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_COL_TIME, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_TEXT, QHeaderView.Stretch)
        header.setSectionResizeMode(_COL_USE, QHeaderView.ResizeToContents)
        # 役割列 (配信者/サブ/コメント) は内容に合わせる
        for _role, col in _ROLE_COLUMNS:
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        # フォント/サイズ列も内容に合わせる (resolve16 §4.4)
        header.setSectionResizeMode(_COL_FONT, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_SIZE, QHeaderView.ResizeToContents)
        # 行全体ではなくセル単位で編集させる
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        # 「字幕」列を複数行編集にし、Alt/Shift+Enter で改行できるようにする
        self.table.setItemDelegateForColumn(_COL_TEXT, _MultilineTextDelegate(self.table))
        # 明示改行を折り返し表示する
        self.table.setWordWrap(True)
        # 編集で改行が増減した際に行の高さを追従させる
        self.table.itemChanged.connect(self._on_item_changed)
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

    # 別クリップ等へテーブルの内容を差し替える (resolve17 §4.7.2 クリップ切替)
    def set_items(self, items):
        self._items = [dict(item) for item in (items or [])]
        self._populate(self._items)

    # テーマ欄の値を設定する (クリップ切替時の復元用 / resolve19)
    def set_theme(self, text):
        if self.theme_edit is not None:
            self.theme_edit.setText(text or "")

    # items をテーブルへ反映する
    def _populate(self, items):
        self.table.setRowCount(len(items))
        self._role_groups = []
        self._font_combos = []
        self._size_edits = []
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

            # 役割列: 配信者/サブ/コメント を行内で排他選択させる
            # (QButtonGroup を行ごとに作り、各セルへ中央寄せしたラジオを配置)
            group = QButtonGroup(self)
            group.setExclusive(True)
            current_role = str(item.get("role", _DEFAULT_ROLE))
            for role_key, col in _ROLE_COLUMNS:
                radio = QRadioButton()
                radio.setChecked(role_key == current_role)
                self.table.setCellWidget(row, col, _centered_widget(radio))
                group.addButton(radio, col)  # id=列番号 で選択列を逆引き可能に
            # いずれも未選択(不正 role) の場合は既定役割を選択状態にする
            if group.checkedId() < 0:
                default_button = group.button(_role_default_column())
                if default_button is not None:
                    default_button.setChecked(True)
            self._role_groups.append(group)

            # フォント列: Blank(=デフォルト使用) + フォント一覧。行ごとに任意で上書きする
            font_combo = self._make_font_combo(str(item.get("font", "") or ""))
            self.table.setCellWidget(row, _COL_FONT, font_combo)
            self._font_combos.append(font_combo)

            # サイズ列: 小さめテキストボックス (空欄=デフォルト / 整数のみ)
            size_edit = QLineEdit()
            size_edit.setValidator(QIntValidator())
            size_edit.setMaximumWidth(_SIZE_FIELD_MAX_WIDTH)
            size_value = item.get("font_size")
            if size_value not in (None, ""):
                size_edit.setText(str(size_value))
            self.table.setCellWidget(row, _COL_SIZE, size_edit)
            self._size_edits.append(size_edit)

        self.table.resizeRowsToContents()

    # フォント個別列のコンボを生成する (先頭 Blank=デフォルト / 各項目を自フォント描画)
    def _make_font_combo(self, current_font):
        combo = QComboBox()
        combo.addItem("", _FONT_BLANK_VALUE)  # Blank = デフォルト使用
        for family in self._font_families:
            combo.addItem(family, family)
            # 各項目を自フォントで描画してプレビュー代わりにする (resolve16 §4.1)
            combo.setItemData(
                combo.count() - 1, QFont(family, _FONT_PREVIEW_POINT_SIZE), Qt.FontRole
            )
        # 既存の font 指定があれば選択状態にする (無ければ Blank)
        index = combo.findData(current_font) if current_font else 0
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo

    # セル編集で改行が増減した際に、その行の高さを内容へ追従させる
    def _on_item_changed(self, item):
        self.table.resizeRowToContents(item.row())

    # 全行の使用チェックを一括設定する
    def _set_all_use(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.table.rowCount()):
            use_item = self.table.item(row, _COL_USE)
            if use_item is not None:
                use_item.setCheckState(state)

    # 「字幕決定」確定後に編集結果を返す
    # 戻り値: [{"start", "end", "text", "use", "role", "font", "font_size"}]
    #   start/end は不変、text/use/role/font/font_size は編集反映。
    #   role は焼き込み時の色分け、font/font_size は個別上書き (resolve16 §4.4)。
    #   font="" / font_size=None は「デフォルト使用」を表す。
    def result_items(self):
        results = []
        for row, original in enumerate(self._items):
            text_item = self.table.item(row, _COL_TEXT)
            use_item = self.table.item(row, _COL_USE)
            # 表示用の実改行を ASS の \N へ戻す
            text = "" if text_item is None else text_item.text().replace("\n", "\\N")
            use = use_item is not None and use_item.checkState() == Qt.Checked
            # 選択された役割ラジオの列番号から役割キーを逆引きする
            group = self._role_groups[row] if row < len(self._role_groups) else None
            role = _col_to_role(group.checkedId()) if group is not None else _DEFAULT_ROLE
            results.append({
                "start": original["start"],
                "end": original["end"],
                "text": text,
                "use": use,
                "role": role,
                # 個別フォント/サイズ (Blank/空欄はデフォルト使用)
                "font": self._font_at(row),
                "font_size": self._size_at(row),
            })
        return results

    # テーマ入力欄の値を返す (resolve19)。欄が無ければ空文字。
    def theme_value(self):
        if self.theme_edit is None:
            return ""
        return self.theme_edit.text().strip()

    # 指定行の開始秒を返す (プレビューのシーク連動用 / resolve23)。範囲外は None。
    def row_start(self, row):
        if 0 <= row < len(self._items):
            return float(self._items[row].get("start", 0.0))
        return None

    # 指定行のフォント個別指定を返す (Blank は "" = デフォルト使用)
    def _font_at(self, row):
        combo = self._font_combos[row] if row < len(self._font_combos) else None
        if combo is None:
            return ""
        return combo.currentData() or ""

    # 指定行のサイズ個別指定を返す (空欄/非数値は None = デフォルト使用)
    def _size_at(self, row):
        edit = self._size_edits[row] if row < len(self._size_edits) else None
        if edit is None:
            return None
        text = edit.text().strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None


# 字幕編集ダイアログ (SubtitleEditorWidget を包む薄いラッパ)
# items: [{"start","end","text","use","role"}] / role 省略時は配信者。
# export_context: DaVinci Resolve 出力の材料 (resolve20 §5.3)。
#   {"source_path","keep_segments","settings","profile"}。既定 None = 出力ボタン非表示
#   (既存呼び出しは無改修で従来どおり動作する)。
# preview_context: 動画プレビューの材料 (resolve23 §5.3)。
#   {"video_path","eff_cfg","profile","settings"}。既定 None = プレビュー欄非表示
#   (video_path は items と時間軸が一致する無音カット後の中間動画)。
class SubtitleEditorDialog(QDialog):

    def __init__(self, items, parent=None, default_font="", default_size=None,
                 font_families=None, show_theme_field=False, theme_placeholder="",
                 theme_text="", export_context=None, preview_context=None):
        super().__init__(parent)
        self.setWindowTitle("字幕編集")

        # Resolve 出力の材料 (元入力パス・編集点・設定)
        self._export_context = export_context or None
        # 動画プレビューの材料 (resolve23。無効/未注入なら現行レイアウトのまま)
        self._preview_context = preview_context or None
        self.preview = None

        root = QVBoxLayout(self)

        # 編集本体 (テーブル・テーマ欄・全選択ボタン)
        self.editor = SubtitleEditorWidget(
            items, parent=self,
            default_font=default_font, default_size=default_size,
            font_families=font_families, show_theme_field=show_theme_field,
            theme_placeholder=theme_placeholder, theme_text=theme_text,
        )

        # 説明ラベル (従来ダイアログの文言。「字幕決定」で焼き込みに進む旨)
        description = QLabel(
            "誤訳の修正と使用可否を選択し、「字幕決定」で焼き込みに進みます。\n"
            "「時間」は編集できません。チェックを外した字幕は焼き込まれません。\n"
            "各行の「配信者/サブ/コメント」で色を選べます(1行につき1つ)。\n"
            f"「フォント」「サイズ」は行ごとに上書きできます(空欄=デフォルト: {self.editor._default_desc()})。"
        )

        if is_preview_enabled(self._preview_context):
            # プレビューあり: 左=説明+テーブル / 右=プレビュー の横分割 (resolve23 §5.3)
            self.resize(_DIALOG_PREVIEW_WIDTH, _DIALOG_PREVIEW_HEIGHT)
            splitter = QSplitter(Qt.Horizontal)
            left = QWidget()
            left_layout = QVBoxLayout(left)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.addWidget(description)
            left_layout.addWidget(self.editor)
            splitter.addWidget(left)
            self.preview = SubtitlePreviewWidget(
                self._preview_context.get("settings") or {}, parent=self)
            self.preview.bind_editor(self.editor)
            self.preview.set_source(
                self._preview_context.get("video_path", ""),
                self._preview_context.get("eff_cfg") or {},
                self._preview_context.get("profile"),
            )
            splitter.addWidget(self.preview)
            splitter.setStretchFactor(0, 3)
            splitter.setStretchFactor(1, 2)
            root.addWidget(splitter)
        else:
            # プレビューなし: 現行と完全同一のレイアウト・サイズ (完全後方互換)
            self.resize(_DIALOG_WIDTH, _DIALOG_HEIGHT)
            root.addWidget(description)
            root.addWidget(self.editor)

        # DaVinci Resolve ファイル出力 / 決定 / キャンセル
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        # 出力ボタン (材料が揃い、かつ設定で有効なときだけ追加する / resolve20 §5.3)
        self.export_button = None
        if self._can_export():
            self.export_button = QPushButton(RESOLVE_EXPORT_BUTTON_TEXT)
            self.export_button.setToolTip(
                "現在の字幕とカット編集点を DaVinci Resolve 用プロジェクトファイル"
                "(.fcpxml) として出力します。"
            )
            self.export_button.clicked.connect(self._on_export_resolve)
            button_row.addWidget(self.export_button)
        self.decide_button = QPushButton("字幕決定")
        self.decide_button.setDefault(True)
        self.decide_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.decide_button)
        button_row.addWidget(self.cancel_button)
        root.addLayout(button_row)

    # Resolve 出力ボタンを出せるか (材料の有無 + 設定の有効/無効)
    def _can_export(self):
        if not self._export_context or not self._export_context.get("source_path"):
            return False
        return resolve_export.is_enabled(self._export_context.get("settings") or {})

    # 「DaVinci Resolve ファイル出力」押下: 現在の編集内容で FCPXML を書き出す (resolve20 §5.3)
    def _on_export_resolve(self):
        run_resolve_export(
            self,
            lambda confirm: resolve_export.export_clip_review(
                self._export_context, self.result_items(), overwrite_confirm=confirm),
        )

    # ダイアログ終了時 (決定/キャンセル/×) にプレビューの再生停止と一時領域掃除を行う
    # (resolve23 §5.3。以降パイプラインが再開し中間動画が削除されても参照しない)
    def done(self, code):
        if self.preview is not None:
            self.preview.shutdown()
        super().done(code)

    # 後方互換: 既存コードが参照し得る theme_edit をエディタへ委譲する
    @property
    def theme_edit(self):
        return self.editor.theme_edit

    # 編集結果を返す (SubtitleEditorWidget へ委譲・戻り値は従来と同一)
    def result_items(self):
        return self.editor.result_items()

    # テーマ入力欄の値を返す (SubtitleEditorWidget へ委譲)
    def theme_value(self):
        return self.editor.theme_value()
