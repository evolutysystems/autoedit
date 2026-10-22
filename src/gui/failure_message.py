# 失敗通知の文面づくり (docs/error/fromUser/resolve.md §6-12-3)
#
# 背景: 2026-10-22 の報告では「処理に失敗しました。ffprobeが失敗。」としか出ず、
# 対象のファイルも returncode も分からなかった。原因は 2 つある。
#   ・FFmpegError は command / returncode / stderr_tail を持っているのに、
#     画面へ出すときに stderr_tail しか見ていなかった
#   ・アーカイブ用タブは str(e) を直接出しており、クリップ用タブとは別実装だった
# そこで整形をここへ一本化し、どちらのタブでも同じ情報が出るようにする。
import os

from ..i18n import tr

# stderr から抜き出す行数 (末尾の実エラー行だけを見せる)
_STDERR_EXCERPT_LINES = 6


# 外部コマンドの引数列から「対象ファイル」らしきものを選ぶ。
# コマンド全体はパスが長く画面では読みづらいため、対象だけを出す
# (全文はログへ DEBUG で残している)。
def _target_of(command):
    if not command:
        return ""
    # ffprobe/ffmpeg とも対象は末尾側にある。オプション以外の最後の値を採る。
    for value in reversed([str(a) for a in command]):
        if value.startswith("-"):
            continue
        if os.path.sep in value or os.path.altsep and os.path.altsep in value:
            return value
    return ""


# 例外から利用者向けの本文を組み立てる。
# AutoEditError 系はそのまま読める文言を持っているため、本文はそれを使い、
# 原因を追うのに要る情報だけを足す。
def format_failure_message(error):
    message = str(error)

    returncode = getattr(error, "returncode", None)
    command = getattr(error, "command", None)
    target = _target_of(command)
    # 対象パスは本文に出ていないことが多い。出ていなければ添える。
    if target and target not in message:
        message = tr("{message}\n対象: {path}", message=message, path=target)
    # returncode も本文に無ければ添える (stderr が空の失敗で唯一の手がかりになる)
    if returncode is not None and str(returncode) not in message:
        message = tr("{message}\n終了コード: {code}", message=message, code=returncode)

    stderr_tail = getattr(error, "stderr_tail", None)
    if stderr_tail:
        tail_lines = [ln for ln in str(stderr_tail).splitlines() if ln.strip()]
        excerpt = "\n".join(tail_lines[-_STDERR_EXCERPT_LINES:])  # 末尾の実エラー行のみ抜粋
        if excerpt:
            message = tr("{message}\n\nFFmpeg エラー詳細:\n{excerpt}",
                         message=message, excerpt=excerpt)
    return message
