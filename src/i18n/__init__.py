# 多言語化の中核 (ver8 resolve §3)
#
# 本モジュールは「表示文言の唯一の出どころ」である。
# 日本語の原文そのものをキーとして扱い、英語表示のときだけ対訳表 (catalog_en) を引く。
#
# 設計上の要点:
#   * 日本語 (既定) では tr() が引数をそのまま返すため、従来の挙動と完全に同じになる。
#     対訳表に漏れがあっても日本語表示には一切影響しない (§3-1)。
#   * 対訳表に無いキーは原文のまま返し、ログへ 1 度だけ記録する。
#     文言が英語にならないことはあっても、画面が壊れることはない (§7 例外方針)。
#   * プロジェクト内の他モジュールへ依存しない (標準ライブラリのみ)。
#     settings_window も theme も pipeline も、循環を気にせず import できる (§3-3)。
#
# 使い方:
#   from ...i18n import tr
#   label.setText(tr("設定"))
#   label.setText(tr("完了: {path}", path=output_path))   # 旧 f"完了: {output_path}"
import logging

from .catalog_en import CATALOG as _CATALOG_EN

# モジュールロガー (theme.py と同方針。標準ライブラリのみ使用)
_logger = logging.getLogger(__name__)

# 対応言語コード。ja が既定 (従来の挙動)
LANG_JA = "ja"
LANG_EN = "en"
SUPPORTED_LANGUAGES = (LANG_JA, LANG_EN)

# 言語切替ドロップダウンに出す表示名。
# 「その言語の話者が読める綴り」で書く (英語話者に「日本語」は読めないため
# 日本語表示中でも English は English のまま出す / §5.2)。
LANGUAGE_NAMES = {
    LANG_JA: "日本語",
    LANG_EN: "English",
}

# 言語ごとの対訳表。ja は原文そのものなので表を持たない
_CATALOGS = {
    LANG_EN: _CATALOG_EN,
}

# 現在の言語 (既定は日本語)
_language = LANG_JA

# 言語が変わったときに呼ぶコールバック (引数なし)。
# 画面を作り直さずに文言だけ貼り替えるために使う (theme.watch_color_scheme と同方針)
_listeners = []

# 対訳表に無かったキー (同じ文言でログが溢れないよう記録して 1 度だけ出す)
_missing = set()


# 現在の言語コードを返す ("ja" | "en")
def language():
    return _language


# 日本語表示かどうか (対訳表を引く必要があるかの判定に使う)
def is_japanese():
    return _language == LANG_JA


# 言語コードを正規化する。未対応・未指定は日本語へ倒す。
# "en-US" / "EN" のような綴りも先頭 2 文字で受け付ける (setting.json の手書きを救済)
def normalize(code):
    if not isinstance(code, str):
        return LANG_JA
    text = code.strip().lower().replace("_", "-")
    if text in SUPPORTED_LANGUAGES:
        return text
    head = text.split("-", 1)[0]
    if head in SUPPORTED_LANGUAGES:
        return head
    return LANG_JA


# 言語を切り替える。変わったときだけ True を返し、購読者へ通知する。
# 通知の最中に例外が出ても他の購読者へは届ける (1 画面の失敗で全体を壊さない)
def set_language(code):
    global _language
    resolved = normalize(code)
    if resolved == _language:
        return False
    _language = resolved
    _missing.clear()  # 言語が変われば「漏れていた文言」も変わる
    _logger.info("表示言語を切り替えました: %s", resolved)
    for callback in list(_listeners):
        try:
            callback()
        except Exception:  # noqa: BLE001 (文言の貼り替え失敗で操作を止めない)
            _logger.exception("言語切替の通知に失敗しました")
    return True


# setting.json の ui.language を反映する (theme.apply と同じ流儀)。
# 起動時に 1 度呼ぶ。反映後の言語コードを返す。
def apply(settings=None):
    ui = settings.get("ui") if isinstance(settings, dict) else None
    code = ui.get("language") if isinstance(ui, dict) else None
    resolved = normalize(code)
    global _language
    if resolved != _language:
        set_language(resolved)
    else:
        _language = resolved
    return _language


# 言語切替の通知を購読する。解除用に同じ callback を unsubscribe へ渡す。
# 購読者は強参照で保持するため、閉じる画面は必ず unsubscribe すること
# (メイン画面のように起動中ずっと生きているものは解除不要)
def subscribe(callback):
    if callback not in _listeners:
        _listeners.append(callback)


# 言語切替の通知を解除する (未登録でも何もしない)
def unsubscribe(callback):
    try:
        _listeners.remove(callback)
    except ValueError:
        pass


# 原文 (日本語) を現在の言語へ訳して返す。
#
# kwargs を渡すと str.format で差し込む。f-string を置き換えるときは
#   f"完了: {path}"  →  tr("完了: {path}", path=path)
# のように名前付きプレースホルダへ直す (語順が言語で変わるため位置指定は使わない)。
#
# 対訳表に無い / 差し込みに失敗した場合も、必ず何らかの文字列を返す。
#
# 第 1 引数は位置専用 (/) にしてある。差し込み名に text を使う文言
# (tr("{text} / 次回リセット {reset}", text=…)) が引数名と衝突しないようにするため。
def tr(text, /, **kwargs):
    if not isinstance(text, str):
        return text
    result = text
    if _language != LANG_JA:
        catalog = _CATALOGS.get(_language)
        translated = catalog.get(text) if catalog else None
        if translated:
            result = translated
        elif text not in _missing:
            _missing.add(text)
            _logger.info("対訳表に無い文言のため原文で表示します (%s): %s", _language, text)
    if not kwargs:
        return result
    try:
        return result.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        # 対訳のプレースホルダが原文と食い違っていても画面を壊さない。
        # 原文側で差し込めるならそちらへ落とす
        _logger.warning("文言への差し込みに失敗しました (%s): %s", _language, result)
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return result


# 対訳表に無かった文言の一覧を返す (翻訳漏れの洗い出し用)。
# テストと開発時の確認でのみ使う
def missing_keys():
    return sorted(_missing)
