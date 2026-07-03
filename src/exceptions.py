# autoedit 共通例外定義
# 設計書 7. エラー処理方針 に対応


# 全例外の親クラス
class AutoEditError(Exception):
    pass


# 設定不整合エラー
class ConfigError(AutoEditError):
    pass


# 入力動画関連エラー
class InputError(AutoEditError):
    pass


# FFmpeg 実行エラー (戻り値 != 0 等)
class FFmpegError(AutoEditError):

    # FFmpeg コマンドと stderr 末尾を保持する
    def __init__(self, message, command=None, stderr_tail=None, returncode=None):
        super().__init__(message)
        self.command = command
        self.stderr_tail = stderr_tail
        self.returncode = returncode


# テロップ生成エラー
class SubtitleError(AutoEditError):
    pass


# 字幕編集画面でユーザーがキャンセルしたことによる中断
# 異常終了ではなく「ユーザー意図による中断」を表す (resolve3 §9 / §10-2)
class PipelineCancelled(AutoEditError):
    pass
