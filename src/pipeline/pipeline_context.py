# パイプライン中間コンテキスト
# 設計書 4. クラス構成 - PipelineContext に対応
import os
import tempfile

from ..utils.logger import get_logger

_logger = get_logger(__name__)


# 各処理工程間で受け渡す DTO
class PipelineContext:

    # 初期化
    def __init__(self, input_path, settings, progress_callback=None,
                 working_dir=None, total_steps=4, subtitle_review_callback=None,
                 volume_analysis_callback=None):
        self.input_path = input_path
        self.settings = settings
        self.progress_callback = progress_callback or _noop_progress
        self.total_steps = total_steps
        self.current_step = 0

        # 字幕編集画面フック (GUI 実行時のみ注入。None ならレビュー無し=全件使用)
        # 形式: callback(items) -> edited_items / None(キャンセル) (resolve3 §7.1)
        self.subtitle_review_callback = subtitle_review_callback

        # 音量解析・カット閾値確認ダイアログのフック (GUI 実行時のみ注入)
        # 形式: callback(info) -> 確定dB(int) / None(変更しない) (resolve7 §3.2)
        self.volume_analysis_callback = volume_analysis_callback

        # 作業ディレクトリ (未指定なら一時ディレクトリ生成)
        self._tempdir_obj = None
        if working_dir:
            os.makedirs(working_dir, exist_ok=True)
            self.working_dir = working_dir
        else:
            self._tempdir_obj = tempfile.TemporaryDirectory(prefix="autoedit_")
            self.working_dir = self._tempdir_obj.name

        # 入力ファイルを起点として中間ファイルを順次更新する
        self._current_video_path = input_path
        self.intermediate_paths = {}
        self.output_path = None

    # 現在の処理対象動画パスを取得する
    def current_video_path(self):
        return self._current_video_path

    # 現在の処理対象動画パスを更新する
    def set_current_video_path(self, path):
        self._current_video_path = path

    # 中間ファイル用パスを割り当てる
    def allocate_intermediate(self, filename):
        path = os.path.join(self.working_dir, filename)
        self.intermediate_paths[filename] = path
        return path

    # 工程開始を通知して進捗バーを進める
    def begin_step(self, label):
        self.current_step += 1
        ratio = (self.current_step - 1) / max(self.total_steps, 1)
        self.progress_callback(ratio, f"[{self.current_step}/{self.total_steps}] {label} 開始")

    # 工程完了通知
    def end_step(self, label):
        ratio = self.current_step / max(self.total_steps, 1)
        self.progress_callback(ratio, f"[{self.current_step}/{self.total_steps}] {label} 完了")

    # 工程内 FFmpeg 進捗を 0-1 に正規化して全体進捗へ伝える
    def progress_subcallback(self, label):
        step_index = self.current_step  # クロージャ用キャプチャ
        total = max(self.total_steps, 1)

        def _inner(ratio_within_step, _kv):
            global_ratio = ((step_index - 1) + ratio_within_step) / total
            if global_ratio < 0.0:
                global_ratio = 0.0
            if global_ratio > 1.0:
                global_ratio = 1.0
            self.progress_callback(global_ratio, label)

        return _inner

    # 作業ディレクトリの後処理 (パイプライン終了時に一時ファイルを削除する)
    def cleanup(self):
        # 自動生成した一時ディレクトリは中身ごと削除する
        if self._tempdir_obj is not None:
            self._tempdir_obj.cleanup()
            return

        # working_dir 明示指定時は TemporaryDirectory の自動削除が効かないため、
        # 割り当てた中間 (一時) ファイルを個別に削除する。
        # 最終出力は output_writer が working_dir 外へ move 済みのため対象外。
        for path in self.intermediate_paths.values():
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                _logger.warning("一時ファイル削除に失敗: %s", path)


# 進捗コールバック未指定時のデフォルト動作
def _noop_progress(_ratio, _label):
    pass
