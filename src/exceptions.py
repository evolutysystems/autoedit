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


# DaVinci Resolve プロジェクトファイル出力エラー (resolve20 §7)
# ソース未特定・書き出し失敗など、エクスポート固有の失敗を表す。
# 読み取り専用の追加機能のため、この例外はパイプライン本体へ影響させない。
class ExportError(AutoEditError):
    pass


# Twitch ログイン/取得エラー (flow17 R3 / resolve17 §4.3)
# 認証失敗・所有判定不成立・twitch-dl 実行失敗などを表す。
class TwitchError(AutoEditError):
    pass


# Timeline (ver3) のモデル・プロジェクト JSON・レンダリングに関するエラー
# (docs/request/ver3/resolve.md §10)。スキーマ不正・未知の schema_version・
# 編集操作の前提違反などを表す。
class TimelineError(AutoEditError):
    pass
