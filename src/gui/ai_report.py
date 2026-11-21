# レビュー PDF の書き出し (docs/AiEditor/resolve1.md §12.6 / R15)
#
# 本文の組み立ては ai/review_report.py (Qt 非依存) が行い、ここは**紙にするだけ**である。
# PDF 化は PySide6 の QTextDocument + QPdfWriter で行う。PySide6 は既に依存にあるため
# **PDF ライブラリを追加しない** (§12.6)。
#
# PDF にできない環境では HTML を残す (内容は失われない)。
import os

from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

from ..ai import config as ai_config
from ..ai import review_report
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 余白 (mm)
_MARGIN_MM = 12.0
# 既定の解像度 (dpi)
_RESOLUTION = 300


# レビューの読み物を書き出す。返り値は書き出したパスのリスト。
#
# content : review_report.build_html の結果
# 形式は ai.report.format ("pdf" / "html" / "both")。PDF に失敗したら HTML へ落ちる。
def write(settings, content, project_path):
    cfg = ai_config.ai_config(settings)["report"]
    if not cfg.get("enabled", True):
        return []
    fmt = str(cfg.get("format") or "pdf").strip().lower()
    written = []

    if fmt in ("html", "both"):
        written.append(review_report.write_html(
            content, review_report.report_path_for(settings, project_path, "html")))
    if fmt in ("pdf", "both"):
        dest = review_report.report_path_for(settings, project_path, "pdf")
        if write_pdf(content, dest, font=str(cfg.get("font") or "")):
            written.append(dest)
        elif fmt == "pdf":
            # PDF にできない環境でも内容は失わない (§12.6)。
            written.append(review_report.write_html(
                content, review_report.report_path_for(settings, project_path,
                                                       "html")))

    _logger.info("レビューの読み物を書き出しました: %s", ", ".join(written) or "(なし)")

    return written


# HTML を PDF へ。できなければ False (呼び出し側が HTML へ落とす)。
def write_pdf(content, dest, font=""):
    try:
        folder = os.path.dirname(os.path.abspath(dest))
        if folder:
            os.makedirs(folder, exist_ok=True)
        writer = QPdfWriter(dest)
        writer.setResolution(_RESOLUTION)
        layout = QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                             QMarginsF(_MARGIN_MM, _MARGIN_MM, _MARGIN_MM,
                                       _MARGIN_MM), QPageLayout.Millimeter)
        writer.setPageLayout(layout)

        document = QTextDocument()
        if font:
            document.setDefaultFont(_font(font))
        document.setHtml(content)
        document.setPageSize(writer.pageLayout().paintRectPixels(
            writer.resolution()).size().toSizeF())
        document.print_(writer)
    except Exception as e:  # noqa: BLE001 (PDF にできなくても処理は続ける)
        _logger.warning("PDF を作れませんでした (HTML で残します): %s", e)

        return False

    return os.path.exists(dest)


def _font(name):
    from PySide6.QtGui import QFont

    return QFont(str(name))
