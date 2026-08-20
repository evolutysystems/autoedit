# 見つからない素材の再リンク画面 (docs/request/ver3/resolve7.md §5.8 / Phase 5)
#
# 保存済みプロジェクトを開くとき、素材が元の場所に無いことがある
# (別フォルダへ移した・名前を変えた・別 PC で開いた)。
# media_recovery が自動で見つけられなかった素材について、この画面で差し替え先を尋ねる。
#
# 差し替えなければ従来どおり該当クリップを無効化して続行する (処理は止めない / §10)。
# 素材 1 件ごとに開く。media_recovery が「直前に選んだフォルダ」を覚えて次の素材を
# 自動で探すため、フォルダごと移した場合は 1 回答えるだけで済む。
import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ...timeline import media_probe
from ...timeline.model import MEDIA_IMAGE
from ...utils.logger import get_logger
from .. import theme

_logger = get_logger(__name__)


class MissingMediaDialog(QDialog):

    # info     : {"media_id","path","kind"} (media_recovery が渡す)
    # settings : setting.json (ファイル選択のフィルタに使う)
    def __init__(self, info, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("素材が見つかりません")
        theme.install_window_background(self)
        self._info = dict(info or {})
        self._settings = settings or {}
        self._path = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("素材が見つかりません")
        theme.mark_title(title)
        root.addWidget(title)

        missing = str(self._info.get("path", "") or "")
        kind_label = "画像" if self._info.get("kind") == MEDIA_IMAGE else "動画"
        message = QLabel(
            f"このプロジェクトが使っている{kind_label}素材が元の場所にありません。\n"
            f"差し替える素材を選ぶか、この素材を使わずに続けることができます。")
        message.setWordWrap(True)
        root.addWidget(message)

        detail = QLabel(f"素材 {self._info.get('media_id', '')}\n{missing}")
        detail.setWordWrap(True)
        theme.mark_note(detail)
        root.addWidget(detail)

        note = QLabel(
            "※ 使わない場合、この素材を参照しているクリップは無効になります"
            "（映像には出ません）。")
        note.setWordWrap(True)
        theme.mark_note(note)
        root.addWidget(note)

        row = QHBoxLayout()
        row.addStretch(1)
        browse = QPushButton("参照...")
        browse.setAutoDefault(False)
        browse.setToolTip("差し替える素材ファイルを選びます")
        browse.clicked.connect(self._on_browse)
        theme.mark_primary(browse)
        row.addWidget(browse)

        skip = QPushButton("この素材を使わない")
        skip.setAutoDefault(False)
        skip.clicked.connect(self.reject)
        theme.mark_danger(skip)
        row.addWidget(skip)
        root.addLayout(row)

    # 差し替え先を選ぶ。選べたらそのまま閉じる。
    def _on_browse(self):
        cfg = media_probe.media_config(self._settings)
        extensions = (cfg["image_extensions"] if self._info.get("kind") == MEDIA_IMAGE
                      else cfg["video_extensions"])
        pattern = " ".join(f"*{e}" for e in extensions)
        start_dir = os.path.dirname(str(self._info.get("path", "") or ""))
        if not os.path.isdir(start_dir):
            start_dir = ""
        path, _ = QFileDialog.getOpenFileName(
            self, "差し替える素材を選択", start_dir,
            f"素材ファイル ({pattern});;すべてのファイル (*)")
        if not path:
            return          # 選ばなかった = 画面はそのまま (もう一度選び直せる)
        self._path = path
        _logger.info("素材を差し替えます: %s → %s", self._info.get("media_id", ""), path)
        self.accept()

    # 選ばれた差し替え先 (使わない場合は None)
    def result_path(self):
        return self._path


# 素材の再リンク画面をワーカースレッド→メインスレッドで橋渡しする
# (ver3 resolve7 Phase 5。resolve9 でアーカイブ用の再編集からも使うため、
#  main_window から本モジュールへ移して 1 つの実装を共有する)
class MediaRelinkBridge(QObject):

    # メインスレッドへ画面表示を依頼するシグナル (info dict を渡す)
    relink_requested = Signal(object)

    def __init__(self, settings, parent_window=None):
        super().__init__()
        self._settings = settings
        self._parent_window = parent_window
        self._event = threading.Event()
        self._result = None
        self.relink_requested.connect(self._on_requested, Qt.QueuedConnection)

    # ワーカースレッドから呼ばれる (media_recovery.recover の relink_callback)
    # info: {"media_id","path","kind"} / 戻り値: 差し替え先のパス / None (使わない)
    def __call__(self, info):
        self._event.clear()
        self._result = None
        self.relink_requested.emit(info)
        # 利用者が答えるまでワーカースレッドをブロックする
        self._event.wait()
        return self._result

    # メインスレッドで実行されるスロット
    def _on_requested(self, info):
        try:
            dialog = MissingMediaDialog(info, self._settings,
                                        parent=self._parent_window)
            if dialog.exec() == MissingMediaDialog.Accepted:
                self._result = dialog.result_path()
            else:
                self._result = None      # 「この素材を使わない」/ × クローズ
        except Exception:  # noqa: BLE001 (画面生成の失敗でワーカーを固めない)
            _logger.exception("素材の再リンク画面の表示に失敗しました")
            self._result = None
        finally:
            # 例外有無に関わらずワーカーを再開させる (デッドロック防止)
            self._event.set()
