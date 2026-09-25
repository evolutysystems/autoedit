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
                 volume_analysis_callback=None, timeline_review_callback=None,
                 blur_failure_callback=None):
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

        # Timeline 編集画面フック (ver3 / GUI 実行時のみ注入。None なら編集画面なし)
        # 形式: callback(payload) -> 編集後 Timeline / None(キャンセル)
        # payload: {"timeline","settings","project_path","asr_audio_path"}
        self.timeline_review_callback = timeline_review_callback

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

        # 出力プロファイル (縦/横の判定結果とキャンバス寸法 / request14)
        # パイプライン開始時に resolve_output_profile で解決して設定する。
        # 未設定(None)の場合、各工程は従来どおり横として振る舞う。
        self.output_profile = None

        # 無音カットで残した区間 (元入力動画相対) / resolve20 §5.3 案B
        # Resolve 出力のカット編集点として字幕編集画面へ渡すためだけに保持する。
        # その回の処理専用の一時データであり cleanup() でクリアする (寿命管理)。
        self._keep_segments = None

        # Timeline (ver3)。編集情報の唯一のソースとして工程間を受け渡す。
        # プロジェクト JSON のパスと、認識用に作った音声 (プレビューで再利用する)
        # も同じ寿命で保持し、cleanup() で破棄する (回答 Q10: 永続化しない)。
        self.timeline = None
        self.project_path = None

        # トラッキングぼかし (ver5 resolve2 §5.5.3)。
        # 既定はどれも「無し」。ぼかし機能を通らない呼び出し (CLI / テスト) では
        # 従来と完全に同じ挙動になる。
        # ぼかしを掛けられなかったときの確認フック (GUI 実行時のみ注入 / §5.9)。
        # 形式: callback(reason: str) -> True (ぼかし無しで続ける) / False (中止)
        # None のときは**中止**する。「ぼかすと指定したのに素で出た」を防ぐため、
        # 黙って続行だけは絶対にしない (§4-5)。
        self.blur_failure_callback = blur_failure_callback

        self.blur_mask_path = None      # マスク動画のパス (無ければ None)
        self.blur_applied = False       # 適用済みか
        self.blur_mask_failed = False   # マスクを作れなかったか (§5.9 の確認に使う)

        # 透かし (ver5 resolve §5.4)。
        # required はサーバーの予約応答をそのまま載せる (残高から推測しない / R9)。
        # 既定はどちらも False で、ポイント機能を通らない呼び出し (CLI / テスト) では
        # 従来と完全に同じ挙動になる。
        self.watermark_required = False  # 透かしを入れる出力か
        self.watermark_applied = False   # 焼き込み済みか
        # 読み込んだプロジェクトの初回作成時刻 (上書き保存で引き継ぐ / ver3 resolve7 §5.7)。
        # 新規実行では None のまま = 保存時の時刻がそのまま created_at になる。
        self.project_created_at = None
        self._asr_audio_path = None

    # 現在の処理対象動画パスを取得する
    def current_video_path(self):
        return self._current_video_path

    # 現在の処理対象動画パスを更新する
    def set_current_video_path(self, path):
        self._current_video_path = path

    # 無音カットの残す区間を取得する (未実行/無効時は None / resolve20 §5.3)
    def keep_segments(self):
        return self._keep_segments

    # 無音カットの残す区間を保持する (silence_cutter が算出直後に呼ぶ)
    def set_keep_segments(self, segments):
        self._keep_segments = list(segments) if segments else None

    # 保持中の編集点を破棄する (全処理完了時。次回実行へ持ち越さない / resolve20 §5.3)
    def clear_keep_segments(self):
        self._keep_segments = None

    # 認識用に生成した音声ファイルのパスを取得する (プレビュー再生で再利用する / ver3 §6.4-5)
    def asr_audio_path(self):
        return self._asr_audio_path

    # 認識用音声のパスを保持する (subtitle_generator が生成直後に呼ぶ)
    def set_asr_audio_path(self, path):
        self._asr_audio_path = path

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
        # 保持していた Resolve 出力用の編集点を破棄する (resolve20 §5.3 寿命管理)
        self.clear_keep_segments()
        # Timeline と認識用音声も同じ寿命で破棄する (ver3 / 回答 Q10)
        self.timeline = None
        self._asr_audio_path = None

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
