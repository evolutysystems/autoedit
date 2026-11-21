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


# ディスク空き容量が足りない (docs/error/fromUser/resolve.md §6-2)
#
# FFmpegError と分けているのは「続けても必ず失敗する」ことを型で表すためである。
# 正規化や字幕の失敗は握り潰して続行してよいが、容量不足だけは握り潰すと
# 原因から離れた工程で別のエラーとして見えてしまう (resolve.md §5-4)。
# AutoEditError 派生のため、既存の except AutoEditError 経路はそのまま効く。
#
# path     : 足りなかった場所
# free     : 実測の空き (バイト / 取得できなければ None)
# required : 必要と判断した量 (バイト / 不明なら None)
class DiskSpaceError(AutoEditError):

    def __init__(self, message, path=None, free=None, required=None):
        super().__init__(message)
        self.path = path
        self.free = free
        self.required = required


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


# Stretheus API 呼び出しエラー (StretheusAPI resolve2 §6.5)
# status は HTTP ステータス、code は ProblemDetails の機械判読用コード
# (validation_failed / reauth_required / insufficient_scope など)。
class ApiError(AutoEditError):

    def __init__(self, message, status=None, code=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


# 再ログインが必要 (401 reauth_required)。
# リフレッシュでは回復できないため、呼び出し側はログインを促す。
class ReauthRequiredError(ApiError):
    pass


# API へ到達できない (5xx / 接続失敗 / タイムアウト)。
# オフライン扱いとし、機能を止めずに縮退させる判断材料にする。
class ApiOfflineError(ApiError):
    pass


# AI 機能 (AI Clip Studio) の失敗の親 (docs/AiEditor/resolve1.md §17.1)
# AutoEditError 派生のため、既存の except AutoEditError 経路はそのまま効く。
# PipelineCancelled と DiskSpaceError は AI 層でも握り潰さず貫通させる。
class AiError(AutoEditError):
    pass


# 通信・認証・レート制限・上限超過。
# status は HTTP ステータス、code は ProblemDetails の機械判読用コード
# (ai_busy / ai_quota_exceeded / insufficient_points / login_required など)。
class AiProviderError(AiError):

    def __init__(self, message, status=None, code=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


# AI へ到達できない (5xx / 接続失敗 / タイムアウト / 機能停止中)。
# オフライン扱いとし、既存の採点結果へ縮退させる判断材料にする。
#
# 設計書 §17.1 の図では AiProviderError と兄弟だが、既存の
# ApiOfflineError(ApiError) と同じく「一般の失敗の下に到達不能を置く」形にした。
# status / code をそのまま持てるうえ、except AiProviderError で両方を受けられる。
class AiOfflineError(AiProviderError):
    pass


# 応答が JSON でない / スキーマ不正 / 検証で致命。
# 安全側の拒否 (stop_reason = refusal) もこれに寄せ、再試行で押し通さない。
class AiPlanError(AiError):

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code
