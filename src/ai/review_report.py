# レビュー PDF の本文組み立て (docs/AiEditor/resolve1.md §12.6 / R15 / 回答 Q9)
#
# **「この動画をどう見たか」を 1 枚の読み物として書き出す。** 画面を変えない方針 (回答 Q12)
# のもとで、AI の意図を利用者へ届ける唯一の読み物になる。
#
# ここは Qt 非依存で、作るのは **HTML 1 枚**だけである。PDF 化は gui/ai_report.py
# (PySide6 の QTextDocument + QPdfWriter) が行う。PDF ライブラリは増やさない (§12.6)。
#
# **レビューを回していなくても出せる。** その場合は「構成の表 + シーン評価 +
# 適用できなかった指示」だけの PDF になる (= AI が何を考えて組んだかの記録)。
import html
import os

from ..utils.logger import get_logger
from . import config as ai_config
from . import plan_schema

_logger = get_logger(__name__)

# 見出しの文言 (PDF は日本語固定。画面文言ではないため i18n の対象外 / §19 の 9)
_TITLE = "AI 編集レビュー"

# 8 軸の表示名
_AXIS_LABELS = {
    "funny": "面白さ", "surprise": "意外性", "context_dependency": "文脈依存",
    "first_time_clarity": "初見理解", "excitement": "盛り上がり",
    "shorts_fit": "ショート適性", "long_fit": "長尺適性", "standalone": "独立性",
}

_SEVERITY_LABELS = {"minor": "軽", "major": "中", "critical": "重"}
_KIND_LABELS = {"pacing": "テンポ", "clarity": "分かりやすさ", "funny": "面白さ",
                "subtitle": "テロップ", "audio": "音", "visual": "映像",
                "platform": "出力先"}
_ROLE_LABELS = {"director": "演出", "critic": "評論", "viewer": "初見",
                "platform": "出力先", "qa": "技術"}

_CSS = """
body { font-family: sans-serif; font-size: 10.5pt; color: #222; }
h1 { font-size: 18pt; margin: 0 0 2pt 0; }
h2 { font-size: 13pt; margin: 14pt 0 4pt 0; border-bottom: 1px solid #bbb; }
p { margin: 3pt 0; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0; }
th, td { border: 1px solid #bbb; padding: 3pt 5pt; text-align: left;
         vertical-align: top; font-size: 9.5pt; }
th { background-color: #eee; }
.meta { color: #666; font-size: 9pt; }
.bar { background-color: #777; color: #fff; }
.sev-critical { background-color: #f3d6d6; }
.sev-major { background-color: #faf0d6; }
.num { text-align: right; }
img { margin: 4pt 6pt 0 0; }
"""


# PDF / HTML の置き場 (<出力名>_review.<拡張子> / §12.6)。
def report_path_for(settings, project_path, extension="pdf"):
    cfg = ai_config.ai_config(settings)["report"]
    suffix = str(cfg.get("name_suffix") or "_review")
    folder = str((settings or {}).get("general", {}).get("output_directory", "") or "")
    stem = os.path.splitext(os.path.basename(str(project_path or "")))[0]
    # <名前>.ai_youtube_long.timeline → 2 段の拡張子を落とす
    stem = os.path.splitext(stem)[0] or stem or "review"
    if not folder:
        folder = os.path.dirname(os.path.abspath(str(project_path or "")))

    return os.path.join(folder, "{0}{1}.{2}".format(stem, suffix, extension))


# HTML を書き出す (PDF にできない環境でも内容は失われない / §12.6)。
def write_html(content, dest):
    folder = os.path.dirname(os.path.abspath(dest))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as handle:
        handle.write(content)

    return dest


# レビュー PDF の本文 (HTML 1 枚) を組む。
#
# plan      : EditPlan (必須)
# video_plan: VideoPlan (無ければ構成の表は EditPlan から作る)
# scenes    : RankedScene のリスト (無ければシーン評価の節を出さない)
# issues    : レビューの指摘 (無ければその節を出さない)
# checks    : 機械の測定結果 (§12.2)
# notes     : plan_validator / plan_applier の記録 (適用できなかった指示)
# images    : [{"data_base64","media_type","caption"}] (無ければ参考画像を出さない)
def build_html(plan, video_plan=None, scenes=None, issues=None, checks=None,
               notes=None, images=None, settings=None, source=None,
               generated_at=""):
    parts = ["<html><head><meta charset=\"utf-8\"><style>", _CSS,
             "</style></head><body>"]
    parts.append(_cover(plan, source, generated_at))
    parts.append(_overview(issues, video_plan))
    parts.append(_sections_table(plan, video_plan))
    parts.append(_scenes_table(scenes))
    parts.append(_issues_table(issues))
    parts.append(_sound_suggestions(issues))
    parts.append(_subtitle_suggestions(issues))
    parts.append(_checks_table(checks))
    parts.append(_notes_table(notes))
    parts.append(_images(images))
    parts.append("</body></html>")

    return "".join(part for part in parts if part)


# ---- 表紙 ----------------------------------------------------------------

def _cover(plan, source, generated_at):
    project = (plan or {}).get("project") or {}
    generated = (plan or {}).get("generated") or {}
    info = source or {}
    total = sum(float(scene["source_end"]) - float(scene["source_start"])
                for scene in (plan or {}).get("scenes") or [])
    rows = [
        ("元の配信", _text(info.get("title")) or _text(project.get("source", {}).get(
            "video_id")) or "(不明)"),
        ("ゲーム", _text(info.get("game")) or "-"),
        ("出力先", _text(project.get("target"))),
        ("シーン", "{0} 件 / 合計 {1:.1f} 秒".format(
            len((plan or {}).get("scenes") or []), total)),
        ("モデル", _text(generated.get("model")) or "(AI 無しで構成)"),
        ("作成", _text(generated_at) or _text(generated.get("at")) or "-"),
    ]

    return ("<h1>{0}</h1><p class=\"meta\">{1}</p>".format(
        _esc(_TITLE),
        " / ".join("{0}: {1}".format(_esc(key), _esc(value)) for key, value in rows)))


# ---- 総評 ----------------------------------------------------------------

def _overview(issues, video_plan):
    lines = []
    concept = _text((video_plan or {}).get("concept"))
    title = _text((video_plan or {}).get("title"))
    if title:
        lines.append("題名の案: {0}".format(title))
    if concept:
        lines.append("構成の狙い: {0}".format(concept))
    counts = {}
    for issue in issues or []:
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
    if counts:
        lines.append("指摘: " + " / ".join(
            "{0} {1} 件".format(_SEVERITY_LABELS.get(key, key), counts[key])
            for key in ("critical", "major", "minor") if counts.get(key)))
    elif issues is not None:
        lines.append("指摘: なし (レビューで直すべき点は挙がらなかった)")
    if not lines:
        return ""

    return "<h2>総評</h2>" + "".join(
        "<p>{0}</p>".format(_esc(line)) for line in lines)


# ---- 構成の表 ------------------------------------------------------------

def _sections_table(plan, video_plan):
    rows = []
    sections = (video_plan or {}).get("sections") or []
    if sections:
        for section in sections:
            rows.append((_text(section.get("role")),
                         _range(section.get("start"), section.get("end")),
                         _duration(section.get("start"), section.get("end")),
                         _text(section.get("reason"))))
    else:
        for scene in (plan or {}).get("scenes") or []:
            rows.append((_text(scene.get("role")),
                         _range(scene.get("source_start"), scene.get("source_end")),
                         _duration(scene.get("source_start"),
                                   scene.get("source_end")),
                         _text(scene.get("reason"))))
    if not rows:
        return ""

    return ("<h2>構成</h2>"
            + _table(("役割", "区間 (元動画)", "尺", "採用の理由"), rows))


# ---- シーン評価 ----------------------------------------------------------

def _scenes_table(scenes):
    if not scenes:
        return ""
    axes = [key for key in plan_schema.SCORE_KEYS]
    header = ["区間", "総合"] + [_AXIS_LABELS.get(key, key) for key in axes] + ["一言"]
    rows = []
    for scene in scenes:
        values = [_bar(scene.scores.get(key, 0.0)) for key in axes]
        rows.append([_range(scene.start, scene.end),
                     "{0:.1f}".format(scene.rank_score)] + values
                    + [_text(scene.summary)])

    return "<h2>シーン評価</h2>" + _table(header, rows, raw_columns=range(2, 2 + len(axes)))


# 0..100 を短い棒と数字で表す (画像を使わない / §12.6 の「簡単な棒」)。
def _bar(value):
    number = max(min(float(value or 0.0), 100.0), 0.0)
    filled = int(round(number / 20.0))

    return ("<span class=\"num\">{0:.0f}</span> "
            "<span class=\"bar\">{1}</span>".format(number, "&#9608;" * filled))


# ---- 指摘一覧 ------------------------------------------------------------

def _issues_table(issues):
    if not issues:
        return ""
    rows = []
    for issue in sorted(issues, key=lambda item: (item["priority"],
                                                  item["at"].get("start") or 0.0)):
        rows.append((_issue_at(issue),
                     _SEVERITY_LABELS.get(issue["severity"], issue["severity"]),
                     _KIND_LABELS.get(issue["kind"], issue["kind"]),
                     _ROLE_LABELS.get(issue.get("role", ""), issue.get("role", "")),
                     _text(issue["detail"]),
                     _fix_text(issue["fix"]),
                     _text(issue["expected"])))

    return ("<h2>指摘</h2>"
            + _table(("時刻", "重大度", "種類", "担当", "内容", "修正案", "期待される効果"),
                     rows))


def _issue_at(issue):
    at = issue.get("at") or {}
    start = at.get("start")
    end = at.get("end")
    if start is None:
        return "-"
    label = "編集後" if at.get("coord") == "timeline" else "元動画"

    return "{0} {1}".format(label, _range(start, end))


def _fix_text(fix):
    op = (fix or {}).get("op", "none")
    labels = {"trim": "区間を詰める", "drop": "シーンを外す", "reorder": "並べ替える",
              "edit_subtitle": "テロップを直す", "add_subtitle": "テロップを足す",
              "add_sound": "効果音を入れる", "set_animation": "動きを付ける",
              "none": "-"}
    detail = []
    if fix.get("source_start") is not None:
        detail.append(_range(fix.get("source_start"), fix.get("source_end")))
    if fix.get("text"):
        detail.append("「{0}」".format(fix["text"]))
    if fix.get("animation"):
        detail.append(fix["animation"])
    if fix.get("preset"):
        detail.append(fix["preset"])

    return " ".join([labels.get(op, op)] + detail)


# ---- 効果音の提案 (§12.6) ------------------------------------------------
#
# 手元に無い音は**プリセット名ではなく説明**で書く (登録されていない音は EditPlan へ
# 入らないため、ここが唯一の伝え先になる)。
def _sound_suggestions(issues):
    rows = []
    for issue in issues or []:
        if issue["fix"]["op"] != "add_sound":
            continue
        preset = _text(issue["fix"].get("preset"))
        rows.append((_issue_at(issue),
                     preset or "(登録なし)",
                     _text(issue["detail"]),
                     _text(issue["expected"])))
    if not rows:
        return ""

    return ("<h2>効果音の提案</h2>"
            + _table(("時刻", "プリセット", "欲しい音", "狙い"), rows)
            + "<p class=\"meta\">プリセットが「(登録なし)」の行は手元に音が無いため"
              "適用していない。ai.sounds.presets へ登録すると次回から載る。</p>")


# ---- テロップの提案 ------------------------------------------------------

def _subtitle_suggestions(issues):
    rows = []
    for issue in issues or []:
        if issue["fix"]["op"] not in ("edit_subtitle", "add_subtitle",
                                      "set_animation"):
            continue
        rows.append((_issue_at(issue), _fix_text(issue["fix"]),
                     _text(issue["detail"])))
    if not rows:
        return ""

    return "<h2>テロップの提案</h2>" + _table(("時刻", "内容", "理由"), rows)


# ---- 技術的な注意 (機械の測定 / §12.2) ----------------------------------

def _checks_table(checks):
    if not checks:
        return ""
    rows = [(("{0:.1f}s".format(check["at"]) if check.get("at") is not None else "-"),
             _SEVERITY_LABELS.get(check["severity"], check["severity"]),
             _text(check.get("scene_id")) or "-",
             _text(check["detail"])) for check in checks]

    return ("<h2>技術的な注意 (機械の測定)</h2>"
            + _table(("時刻", "重大度", "シーン", "内容"), rows))


# ---- 適用できなかった AI の指示 (§10) -----------------------------------

def _notes_table(notes):
    dropped = [note for note in notes or []
               if note.get("action") == plan_schema.ACTION_DROP]
    if not dropped:
        return ""
    rows = [(_text(note.get("check")), _text(note.get("where")),
             _text(note.get("detail"))) for note in dropped]

    return ("<h2>適用できなかった AI の指示</h2>"
            + _table(("検査", "場所", "理由"), rows)
            + "<p class=\"meta\">検証で落とした項目である。"
              "AI の意図は通っていたが、素材・設定・上限に合わなかった。</p>")


# ---- 参考画像 (§12.5) ----------------------------------------------------

def _images(images):
    if not images:
        return ""
    cells = []
    for image in images:
        data = _text(image.get("data_base64"))
        if not data:
            continue
        cells.append(
            "<div><img src=\"data:{0};base64,{1}\" width=\"240\"/>"
            "<div class=\"meta\">{2}</div></div>".format(
                _esc(_text(image.get("media_type")) or "image/jpeg"), data,
                _esc(_text(image.get("caption")))))
    if not cells:
        return ""

    return "<h2>参考画像</h2>" + "".join(cells)


# ---- 小物 ----------------------------------------------------------------

def _table(header, rows, raw_columns=()):
    raw = set(raw_columns or ())
    head = "".join("<th>{0}</th>".format(_esc(str(name))) for name in header)
    body = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            text = value if index in raw else _esc(str(value))
            cells.append("<td>{0}</td>".format(text))
        body.append("<tr>{0}</tr>".format("".join(cells)))

    return "<table><tr>{0}</tr>{1}</table>".format(head, "".join(body))


def _range(start, end):
    if start is None:
        return "-"
    if end is None:
        return "{0:.1f}s".format(float(start))

    return "{0:.1f}〜{1:.1f}s".format(float(start), float(end))


def _duration(start, end):
    if start is None or end is None:
        return "-"

    return "{0:.1f}s".format(max(float(end) - float(start), 0.0))


def _text(value):
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _esc(value):
    return html.escape(_text(value), quote=True)
