# AI を使う前の同意 (docs/AiEditor/resolve1.md §5.5 / §13.4)
#
# 「どこへ何を送るか」を利用者が**事前に**確認できるよう、初回の AI 利用時に 1 度だけ
# 同意を取る。文面が変わったら版を上げて再確認する (`ai.consent.accepted_version`)。
#
# §13.4 の決まりにより、**学習 (編集の差分を端末内に記録すること) も同意へ含める。**
# 黙って記録しない。
#
# ここは Qt 非依存である。文面を組むところまでを持ち、
#   ・見せる (ダイアログ)
#   ・同意を保存する (setting.json へ書く)
# は画面側が行う (`gui/archive_tab.py`)。settings_window は PySide6 を引くため
# ai/ からは触らない (§4)。
from ..i18n import tr
from ..utils.logger import get_logger
from . import config as ai_config

_logger = get_logger(__name__)

# 文面の版。**文面を変えたら必ず上げる** (上げると再確認になる / §5.5)。
CURRENT_VERSION = 1


# まだ同意を取っていない (または文面が新しくなった) か。
def needs_consent(settings):
    return accepted_version(settings) < CURRENT_VERSION


def accepted_version(settings):
    value = ai_config.ai_config(settings)["consent"].get("accepted_version")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# 同意を settings へ記録する (保存は呼び出し側が行う)。
#
# 渡された dict をその場で書き換えて返す。**保存 (save_settings) は画面側の責務**
# であり、ここでファイルへ書かない (設定の書き込み口を 1 か所に保つ)。
def accept(settings, version=CURRENT_VERSION):
    if not isinstance(settings, dict):
        return settings
    consent = settings.setdefault("ai", {}).setdefault("consent", {})
    consent["accepted_version"] = int(version)
    _logger.info("AI の利用に同意を記録しました (版 %d)", int(version))

    return settings


# 同意を取り消す (次に使うときへ再確認が出る)。
def revoke(settings):
    return accept(settings, version=0)


# 確認の本文。**送るもの・送らないもの・端末内に記録するもの**を並べる。
#
# 文面は設定によって変わる (学習が無効なら学習の行を出さない / コメント投稿者名を
# 送る設定にしていればそう書く)。実際の挙動と文面が食い違わないようにするため、
# 設定から組み立てる。
def text(settings):
    cfg = ai_config.ai_config(settings)
    sends = [tr("数値の解析結果 (音量・無音率・コメント数・既存の採点)"),
             tr("配信のタイトルとゲーム名")]
    if cfg["privacy"].get("include_transcript", True):
        sends.append(tr("切り抜き候補の区間の文字起こし"))
    if cfg["privacy"].get("include_comment_user"):
        sends.append(tr("コメントの本文と投稿者名"))
    else:
        sends.append(tr("コメントの本文 (投稿者名は送りません)"))
    if cfg["vision"].get("enabled", True):
        sends.append(tr("レビュー時の静止画 (長辺 {edge}px まで縮小。音声は送りません)")
                     .format(edge=int(cfg["vision"].get("max_edge_px") or 768)))

    keeps = [tr("AI へ送った量と費用の記録 (トークン数・モデル名)")]
    if cfg["learning"].get("enabled"):
        keeps.append(
            tr("AI の編集と利用者の修正の差分 ({days} 日で消えます)")
            .format(days=int(cfg["learning"].get("keep_days") or 180)))

    lines = [tr("AI による編集を使うと、次のものが Stretheus のサーバー経由で "
                "AI (Anthropic Claude) へ送られます。")]
    lines.extend("・" + item for item in sends)
    lines.append("")
    lines.append(tr("送らないもの: 音声・映像そのもの、ファイルのパス、"
                    "端末の名前、メールアドレス。"))
    lines.append("")
    lines.append(tr("次のものはこの端末の中だけに残ります (サーバーへは送りません)。"))
    lines.extend("・" + item for item in keeps)
    lines.append("")
    lines.append(tr("AI の利用料は Stretheus のポイントから引かれます "
                    "(単価が決まるまでは記録だけで消費しません)。"))
    lines.append(tr("同意しない場合は AI を使わず、従来どおりの切り抜きを行います。"))

    return "\n".join(lines)


# 確認の見出し。
def title():
    return tr("AI へ送る内容の確認")
