# フルテロップ生成モジュール
# 設計書 ② フルテロップ生成 に対応
# 処理順: 音声認識 (faster-whisper) → ASS 字幕ファイル生成 → ASS 焼き込み
# (設計書 10. 不明点 #2 は claude.md ② の指示により解決済。エンジンは参照元
#  ../StreamPipeline/dev と同一の faster-whisper を既定採用)
import os

from ..exceptions import (
    AutoEditError,
    DiskSpaceError,
    FFmpegError,
    PipelineCancelled,
    SubtitleError,
)
from ..i18n import tr
from ..settings.settings_window import resolve_fonts_dir
from ..timeline.timemap import TimeMap
from ..utils.logger import get_logger
from . import comment_decor, ffmpeg_runner, silence_cutter

_logger = get_logger(__name__)


# Silero VAD が発話チャンクの前後へ足す余白 (ms) / resolve8 §2-3・§3-2
# faster-whisper の既定は 400ms で、語頭側の無音まで認識対象へ含めてしまう。
# その無音へ先頭語の単語タイムスタンプが寄るため、テロップが発話より手前に出ていた。
# 既定から 300ms 削って 100ms とする:
#   - 0 にすると VAD の検出遅れ (判定窓 32ms 刻み) で語頭が音声から欠け、
#     テロップの文字自体が落ちる恐れがある
#   - 100ms なら早出しとして知覚されにくく、検出遅れも吸収できる
# 語頭の取りこぼしが出るようなら 200ms へ戻す (resolve8 §9 Q1)。
# 設定項目としては出さない (要望 D3)。400 に戻せば従来の挙動へ完全に戻る。
_VAD_SPEECH_PAD_MS = 100

# 秒の比較に使う許容誤差 (ミリ秒精度で保持するため 1e-6 で十分 / timemap と同じ)
_EPS = 1e-6

# ASS Style のうち要望対象外のため固定維持する値 (マジックナンバー回避)
_ASS_SCALE_X = 100
_ASS_SCALE_Y = 100
_ASS_SHADOW = 1
_ASS_ENCODING = 1

# 不正値フォールバック用の既定 ASS 色
_DEFAULT_OUTLINE_COLOR = "&H00000000"
_DEFAULT_BACK_COLOR = "&H64000000"


# ASS Style の影の深さ (px / PlayRes 基準) を返す (ver6 resolve3 §4.7-1)
# プレビューの近似描画 (gui/timeline/preview_items.py) が同じ値で影を置くため、
# 定義元をここ 1 箇所に保つ。ScaledBorderAndShadow: yes なので実フレーム寸法へ
# libass が追従する = PlayRes 単位のまま扱ってよい。
def ass_shadow_px():
    return _ASS_SHADOW


# bool を ASS のフラグ (-1=有効 / 0=無効) に変換する
def _ass_flag(value):
    return -1 if value else 0


# 整数化を試み、失敗時は既定値へフォールバックする
def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ASS 色文字列 (&H...) を検証し、不正なら既定値へフォールバックする
def _safe_ass_color(value, default):
    if not value:
        return default
    text = str(value).strip()
    if text.upper().startswith("&H"):
        return text
    return default


# テロップ役割 (request10) の既定値。役割未指定時はこの色(=配信者色)へ寄せる。
_DEFAULT_ROLE = "streamer"
# コメント役割 (ver3 resolve11)。配置・アイコン・背景の対象になる。
_ROLE_COMMENT = "comment"


# HTML #RRGGBB を ASS PrimaryColour (&H00BBGGRR) へ変換する
# 6桁でない不正値は白(&H00FFFFFF)へフォールバックする
def _hex_to_ass_color(color_hex):
    hex_value = str(color_hex or "").lstrip("#")
    if len(hex_value) != 6:
        return "&H00FFFFFF"
    r, g, b = hex_value[0:2], hex_value[2:4], hex_value[4:6]
    return f"&H00{b}{g}{r}".upper()


# フォント情報を保持する値オブジェクト (将来のフォント差替に対応)
# ASS Style 行に必要なスタイル項目を一括で保持する。
# 色 (塗り・アウトライン) はテロップ役割 (配信者/サブ/コメント) ごとに分けて保持し、
# 色以外 (フォント/太さ/配置/余白/背景) は全役割共通 (request10 / 追加2)。
class FontProfile:

    # 役割キー → ASS Style 名。色 (塗り+アウトライン) 以外の属性は全 Style で共通とする。
    ROLE_STYLE = {"streamer": "Streamer", "sub": "Sub", "comment": "Comment"}

    def __init__(self, family, size, color_hex,
                 outline_color=None, outline_width=3, back_color=None,
                 bold=False, italic=False, underline=False, strikeout=False,
                 spacing=0, angle=0, border_style=1, alignment=2,
                 margin_l=40, margin_r=40, margin_v=60, role_colors=None,
                 role_outline_colors=None, comment_label="",
                 role_placements=None):
        self.family = family
        self.size = int(size) if size is not None else 48
        # 配信者色 (旧来の単一色)。後方互換のため引数名/意味は据え置く。
        self.color_hex = color_hex or "#FFFFFF"
        # 役割別・塗り色マップ (streamer/sub/comment)。欠落・省略時は配信者色へフォールバック。
        self.role_colors = self._normalize_role_colors(role_colors)
        # コメント役割の先頭ラベル (空文字で無効)。焼き込み時にのみ本文先頭へ付与する。
        self.comment_label = comment_label or ""
        # アウトライン/背景色は ASS 形式文字列をそのまま保持 (不正時は既定値)
        self.outline_color = _safe_ass_color(outline_color, _DEFAULT_OUTLINE_COLOR)
        # 役割別・アウトライン色マップ (追加2)。欠落・省略時は配信者アウトライン色へフォールバック。
        self.role_outline_colors = self._normalize_role_outline_colors(role_outline_colors)
        self.outline_width = _to_int(outline_width, 3)
        self.back_color = _safe_ass_color(back_color, _DEFAULT_BACK_COLOR)
        # 装飾フラグ (bool で保持し、展開時に -1/0 へ変換)
        self.bold = bool(bold)
        self.italic = bool(italic)
        self.underline = bool(underline)
        self.strikeout = bool(strikeout)
        self.spacing = _to_int(spacing, 0)
        self.angle = _to_int(angle, 0)
        self.border_style = _to_int(border_style, 1)
        self.alignment = _to_int(alignment, 2)
        self.margin_l = _to_int(margin_l, 40)
        self.margin_r = _to_int(margin_r, 40)
        self.margin_v = _to_int(margin_v, 60)
        # 役割別の配置・余白 (ver3 resolve11 §3 D-3)。
        # 未指定の役割は共通値へ落ちるため、旧来の呼び出しは挙動が変わらない。
        self._role_placements = self._normalize_placements(role_placements)

    # 役割別・塗り色マップを正規化する
    # streamer は配信者色(color_hex)を既定とし、sub/comment の欠落は配信者色へ寄せる
    def _normalize_role_colors(self, role_colors):
        rc = dict(role_colors or {})
        streamer = rc.get("streamer") or self.color_hex
        return {
            "streamer": streamer,
            "sub": rc.get("sub") or streamer,
            "comment": rc.get("comment") or streamer,
        }

    # 役割別・アウトライン色マップを正規化する (追加2)
    # streamer は配信者アウトライン色(self.outline_color)を既定とし、
    # sub/comment の欠落は配信者アウトライン色へ寄せる。各値は _safe_ass_color で検証する。
    def _normalize_role_outline_colors(self, role_outline_colors):
        rc = dict(role_outline_colors or {})
        streamer = _safe_ass_color(rc.get("streamer"), self.outline_color)
        return {
            "streamer": streamer,
            "sub": _safe_ass_color(rc.get("sub"), streamer),
            "comment": _safe_ass_color(rc.get("comment"), streamer),
        }

    # 役割別・配置マップを正規化する (ver3 resolve11 §3 D-3)
    # 各値は (alignment, margin_l, margin_r, margin_v)。欠けている項目は共通値で埋める。
    def _normalize_placements(self, role_placements):
        common = (self.alignment, self.margin_l, self.margin_r, self.margin_v)
        placements = {role: common for role in self.ROLE_STYLE}
        for role, values in dict(role_placements or {}).items():
            if role not in placements or not values:
                continue
            data = dict(values)
            placements[role] = (
                _to_int(data.get("alignment"), self.alignment),
                _to_int(data.get("margin_l"), self.margin_l),
                _to_int(data.get("margin_r"), self.margin_r),
                _to_int(data.get("margin_v"), self.margin_v),
            )
        return placements

    # 役割の配置 (alignment, margin_l, margin_r, margin_v) を返す
    # 未知の役割は配信者 (= 共通値) へフォールバックする。
    def placement_for_role(self, role):
        return self._role_placements.get(
            role, self._role_placements[_DEFAULT_ROLE])

    # 後方互換: 配信者色の ASS PrimaryColour (&H00BBGGRR) を返す
    def to_ass_color(self):
        return _hex_to_ass_color(self.color_hex)

    # 役割キーに対応する ASS Style 名を返す (未知の役割は配信者へフォールバック)
    def style_for_role(self, role):
        return self.ROLE_STYLE.get(role, self.ROLE_STYLE[_DEFAULT_ROLE])

    # 指定 Style 名・塗り色 (HTML #RRGGBB)・アウトライン色 (ASS &HAABBGGRR) の
    # ASS Style 値 (Style: 以降) を生成する。塗り=PrimaryColour / アウトライン=OutlineColour。
    # placement 省略時は共通の配置・余白を使う (旧来の呼び出しと同一の出力)。
    def to_ass_style_named(self, style_name, color_hex, outline_color, placement=None):
        alignment, margin_l, margin_r, margin_v = placement or (
            self.alignment, self.margin_l, self.margin_r, self.margin_v)
        return (
            f"{style_name},{self.family},{self.size},"
            f"{_hex_to_ass_color(color_hex)},{outline_color},{self.back_color},"
            f"{_ass_flag(self.bold)},{_ass_flag(self.italic)},"
            f"{_ass_flag(self.underline)},{_ass_flag(self.strikeout)},"
            f"{_ASS_SCALE_X},{_ASS_SCALE_Y},{self.spacing},{self.angle},"
            f"{self.border_style},{self.outline_width},{_ASS_SHADOW},"
            f"{alignment},{margin_l},{margin_r},{margin_v},"
            f"{_ASS_ENCODING}"
        )

    # 全役割の Style 値を (Style名, Style値) のリストで返す
    # 配信者→サブ→コメント の順。各役割の塗り色＋アウトライン色＋配置を差し替える
    # (色 = 追加2 / 配置 = ver3 resolve11 §3 D-3)。
    def iter_role_styles(self):
        return [
            (self.ROLE_STYLE[role],
             self.to_ass_style_named(self.ROLE_STYLE[role],
                                     self.role_colors[role],
                                     self.role_outline_colors[role],
                                     self.placement_for_role(role)))
            for role in ("streamer", "sub", "comment")
        ]


# フィラー(口癖)除去の既定リスト (設定 subtitle.fillers で上書き可能)
_DEFAULT_FILLERS = ["えー", "えっと", "あのー", "そのー", "あー", "うー"]


# フィラー(口癖)を除去する (ver8 resolve6 §4-2)
# カット跨ぎの分割では単語から本文を組み直すため、認識時に除いたフィラーが
# 戻ってしまう。除去規則を 1 か所に置き、組み直しでも同じ規則を掛ける。
def strip_fillers(text, fillers=None):
    for filler in (fillers if fillers is not None else _DEFAULT_FILLERS):
        text = text.replace(filler, "")
    return text.strip()


# テキストタイムラインのソース抽象
# 実装クラスは extract(input_path, language) -> [{"start": float, "end": float, "text": str}] を返す
class TextSource:

    # 各実装で上書きする
    def extract(self, input_path, language):
        raise NotImplementedError


# whisper_device 設定から実際に使用するデバイスを解決する
# CUDA 不使用方針 (docs/request/resolve8.md) により GPU(CUDA) 実行は廃止し、常に CPU を使用する。
# 設定に旧値("auto"/"cuda")が残っていても CPU に正規化する(後方互換)。
def _resolve_device(configured_device):
    return "cpu"


# 計算精度を解決する
# CPU 実行のため "auto" は int8。GPU 専用の float16 が指定された場合も int8 へ正規化する。
def _resolve_compute_type(configured_compute_type, device):
    ct = (configured_compute_type or "auto").strip().lower()
    if ct == "auto":
        return "int8"
    if ct == "float16":
        # float16 は GPU(CUDA) 専用。CPU では使用できないため int8 へ正規化する
        return "int8"
    return ct


# テキストを最大文字数で改行 (ASS の \\N) する
# 既存の \\N は「手動/強制改行」として尊重し、各区間をさらに max_line_length ごとに
# 分割して連結する (手動改行 + 15文字自動ラップの併用)。
def wrap_by_length(text, max_line_length):
    if max_line_length <= 0 or not text:
        return text
    chunks = []
    # 手動/既存の強制改行 (\\N) で一旦分割し、各区間を最大文字数で折る
    for segment in text.split("\\N"):
        if len(segment) <= max_line_length:
            chunks.append(segment)
            continue
        s = segment
        while len(s) > max_line_length:
            chunks.append(s[:max_line_length])
            s = s[max_line_length:]
        if s:
            chunks.append(s)
    return "\\N".join(chunks)


# BudouX パーサの遅延初期化シングルトン (毎回ロードするコストを避ける)
# budoux は任意依存のため、ここで import 失敗すれば呼び出し側がフォールバックする。
_budoux_parser = None


def _get_budoux_parser():
    global _budoux_parser
    if _budoux_parser is None:
        import budoux  # 任意依存: 未導入時は ImportError -> wrap_lines でフォールバック
        _budoux_parser = budoux.load_default_japanese_parser()
    return _budoux_parser


# BudouX で文節境界を求め、下限〜上限文字数を目安に改行 (ASS の \\N) する。
# 既存の \\N は「手動/強制改行」として尊重し、各区間を文節単位で折り返す。
# ルール:
#   - 行が下限 (min_line_length) 未満の間は文節を足し続ける (短すぎる行を避ける)
#   - 下限達成後、次の文節を足して上限 (max_line_length) を超える場合は文節境界で改行
#   - 単一文節が上限を超える場合のみ、その行を wrap_by_length で文字数ハード分割 (保険)
def wrap_by_budoux(text, min_line_length, max_line_length, parser):
    if max_line_length <= 0 or not text:
        return text
    out_lines = []
    for segment in text.split("\\N"):
        if not segment:
            out_lines.append(segment)
            continue
        phrases = parser.parse(segment)  # 文節リスト -> list[str]
        line = ""
        for ph in phrases:
            if not line:
                line = ph
            elif len(line) < min_line_length:
                # 下限未満: 文節を足して下限に近づける (下限優先)
                line += ph
            elif len(line) + len(ph) <= max_line_length:
                # 下限達成 かつ 上限内: 同一行へ追加
                line += ph
            else:
                # 下限達成 かつ 上限超過: 文節境界で改行
                out_lines.append(line)
                line = ph
        if line:
            out_lines.append(line)

    # 単一文節が上限超過の行のみ、従来ロジックで文字数ハード分割 (上限保険)
    result = []
    for ln in out_lines:
        if len(ln) > max_line_length:
            result.extend(wrap_by_length(ln, max_line_length).split("\\N"))
        else:
            result.append(ln)
    return "\\N".join(result)


# 設定 (wrap_engine) に応じて改行方式を選択する。
# BudouX 未導入/分割失敗時は文字数改行 (wrap_by_length) へ安全にフォールバックする。
def wrap_lines(text, min_line_length, max_line_length, engine="budoux"):
    if engine == "budoux":
        try:
            parser = _get_budoux_parser()
            return wrap_by_budoux(text, min_line_length, max_line_length, parser)
        except ImportError:
            _logger.warning(
                "budoux が未導入のため文字数改行にフォールバックします "
                "(pip install budoux)。subtitle.wrap_engine を 'length' にすると警告を抑止できます。"
            )
        except Exception as e:  # noqa: BLE001 (分割失敗時もテロップ全損を避ける)
            _logger.warning("BudouX 改行に失敗したため文字数改行にフォールバック: %s", e)
    return wrap_by_length(text, max_line_length)


# faster-whisper による音声認識でテキストタイムラインを生成する TextSource 実装
# 参照元 ../StreamPipeline/dev と同一エンジン (faster-whisper) を採用
class WhisperTextSource(TextSource):

    # 設定 (subtitle セクション) から認識パラメータを取り込む (ハードコード回避)
    def __init__(self, subtitle_settings):
        self._model_name = subtitle_settings.get("whisper_model", "large-v3")
        # 設定値を保持 (auto を含む)。実デバイス/精度は extract 時に解決する
        self._device_setting = subtitle_settings.get("whisper_device", "cpu")
        self._compute_type_setting = subtitle_settings.get("whisper_compute_type", "int8")
        self._beam_size = int(subtitle_settings.get("whisper_beam_size", 8))
        # 改行設定 (request11): BudouX による文節改行を既定とし、下限〜上限で折り返す
        self._min_line_length = int(subtitle_settings.get("min_line_length", 15))
        self._max_line_length = int(subtitle_settings.get("max_line_length", 20))
        self._wrap_engine = str(subtitle_settings.get("wrap_engine", "budoux")).strip().lower()
        self._remove_fillers = bool(subtitle_settings.get("remove_fillers", False))
        self._fillers = subtitle_settings.get("fillers", _DEFAULT_FILLERS)
        # ハルシネーション抑制オプション (対策A / docs/error/20260626/resolve.md)
        # 無音/非発話区間への幻聴字幕(「ご視聴ありがとうございました」等)の生成を抑える
        self._vad_filter = bool(subtitle_settings.get("whisper_vad_filter", True))
        self._vad_min_silence_ms = int(
            subtitle_settings.get("whisper_vad_min_silence_ms", 500)
        )
        # 直前出力を文脈に与えない (既定 False) ことで反復ループを抑止する
        self._condition_on_previous_text = bool(
            subtitle_settings.get("whisper_condition_on_previous_text", False)
        )
        self._no_speech_threshold = float(
            subtitle_settings.get("whisper_no_speech_threshold", 0.6)
        )
        # 単語タイムスタンプ (docs/error/20260627/resolve.md 対策A)
        # 各セグメントの発話頭/末を単語境界で取得し、表示を発話タイミングへ整合させる
        self._word_timestamps = bool(subtitle_settings.get("word_timestamps", True))

    # 音声認識を実行し [{"start", "end", "text"}] のタイムラインを返す
    def extract(self, input_path, language):
        # faster-whisper は任意依存のため遅延 import (未導入環境でも本モジュールは読込可能にする)
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise SubtitleError(
                tr("faster-whisper が未導入のため音声認識を実行できません "
                "(pip install faster-whisper)。subtitle.engine を 'none' にするとスキップできます。")
            ) from e

        # CUDA 不使用方針 (docs/request/resolve8.md) により常に CPU で実行する
        device = _resolve_device(self._device_setting)
        compute_type = _resolve_compute_type(self._compute_type_setting, device)
        _logger.info(
            "音声認識開始 (model=%s, device=%s, compute_type=%s, 設定=%s)",
            self._model_name, device, compute_type, self._device_setting,
        )
        return self._recognize(WhisperModel, input_path, language, device, compute_type)

    # モデル構築〜デコードを実行し [{"start", "end", "text"}] を返す
    def _recognize(self, whisper_model_cls, input_path, language, device, compute_type):
        _logger.info("音声認識実行 (device=%s, compute_type=%s)", device, compute_type)
        model = whisper_model_cls(self._model_name, device=device, compute_type=compute_type)
        segments, _info = self._transcribe(model, input_path, language)

        timeline = []
        for seg in segments:
            text = seg.text.strip()
            if self._remove_fillers:
                text = self._strip_fillers(text)
            if not text:
                continue
            text = self._split_lines(text)
            # 発話実時間(先頭語start〜末尾語end)を採用し、語頭/語尾の無音を除く (対策A)
            start, end = self._speech_bounds(seg)
            entry = {"start": start, "end": end, "text": text}
            # 単語の区間はカット跨ぎの分割にだけ使う (ver8 resolve6 §4-1)。
            # 取れたときだけ載せるため、取れない構成では辞書の形が従来と同一になる。
            words = self._word_spans(seg)
            if words:
                entry["words"] = words
            timeline.append(entry)

        _logger.info("音声認識完了 (%d セグメント)", len(timeline))
        return timeline

    # セグメントの発話実時間(先頭語start〜末尾語end)を返す (対策A)
    # 単語タイムスタンプが無い/取得不能な場合はセグメント境界へフォールバックする
    def _speech_bounds(self, seg):
        words = getattr(seg, "words", None)
        if words:
            starts = [w.start for w in words if w.start is not None]
            ends = [w.end for w in words if w.end is not None]
            if starts and ends:
                return float(min(starts)), float(max(ends))
        # フォールバック (単語情報なし): セグメント境界をそのまま使う
        return float(seg.start), float(seg.end)

    # 単語の区間列 [{"start","end","text"}] を返す (ver8 resolve6 §4-1)
    # カットを跨いだセグメントを単語単位で分けるために使う。
    # word_timestamps=False / モデルが返さない場合は空リスト (従来動作へ倒す)。
    @staticmethod
    def _word_spans(seg):
        spans = []
        for word in (getattr(seg, "words", None) or []):
            if word.start is None or word.end is None:
                continue
            spans.append({
                "start": float(word.start),
                "end": float(word.end),
                "text": str(getattr(word, "word", "")),
            })
        return spans

    # ハルシネーション抑制オプション付きで音声認識を実行する (対策A)
    # VAD 初期化に失敗した場合は VAD 無しへフォールバックして処理を継続する (§5)
    def _transcribe(self, model, input_path, language):
        kwargs = {
            "language": language,
            "beam_size": self._beam_size,
            # 反復ループ抑止: 直前出力を次チャンクの文脈に与えない
            "condition_on_previous_text": self._condition_on_previous_text,
            # 無発話判定の足切り
            "no_speech_threshold": self._no_speech_threshold,
            # 単語境界を取得し、表示を発話タイミングへ整合させる (対策A)
            "word_timestamps": self._word_timestamps,
        }
        if self._vad_filter:
            # 内蔵 Silero VAD で無音/非発話区間を認識対象から除外する
            kwargs["vad_filter"] = True
            # speech_pad_ms を明示する。未指定だと既定 400ms で語頭の無音まで
            # 認識対象に入り、テロップが発話より手前に出る (resolve8 §2-3)。
            kwargs["vad_parameters"] = {
                "min_silence_duration_ms": self._vad_min_silence_ms,
                "speech_pad_ms": _VAD_SPEECH_PAD_MS,
            }
            _logger.info(
                "VAD フィルタ有効 (min_silence=%dms, speech_pad=%dms, "
                "condition_on_previous_text=%s)",
                self._vad_min_silence_ms, _VAD_SPEECH_PAD_MS,
                self._condition_on_previous_text,
            )

        try:
            return model.transcribe(input_path, **kwargs)
        except Exception as e:  # noqa: BLE001 (VAD 初期化失敗等を広く捕捉)
            # VAD 有効時のみ、VAD 無しへフォールバックしてテロップ全損を避ける
            if self._vad_filter:
                _logger.warning("VAD 付き認識に失敗したため VAD 無しで再試行します: %s", e)
                kwargs.pop("vad_filter", None)
                kwargs.pop("vad_parameters", None)
                return model.transcribe(input_path, **kwargs)
            raise

    # フィラー(口癖)を除去する (規則は共通ヘルパへ委譲)
    def _strip_fillers(self, text):
        return strip_fillers(text, self._fillers)

    # 改行 (ASS の \\N) を挿入する (共通ヘルパへ委譲)
    # wrap_engine="budoux" で文節改行、それ以外/未導入時は文字数改行へフォールバック。
    def _split_lines(self, text):
        return wrap_lines(text, self._min_line_length, self._max_line_length, self._wrap_engine)


# コメント役割で使える配置 (ver3 resolve11 §3 D-4)
# アイコンと背景を文字の左横へ確実に置くには「文字列の左端」が確定している必要があるため、
# 左寄せ (an1/an4/an7) だけを許す。中央・右寄せは文字幅なしに左端が求まらない。
_COMMENT_ALIGNMENTS = (1, 4, 7)
_DEFAULT_COMMENT_ALIGNMENT = 4


# コメント役割の配置・余白を字幕設定から取り出す (ver3 resolve11 §5.4-1)
# キーが無ければ共通値 (alignment/margin_*) へ落ちるため、旧 setting.json でも挙動不変。
def _comment_placement(subtitle_cfg):
    alignment = _to_int(subtitle_cfg.get("comment_alignment"),
                        _DEFAULT_COMMENT_ALIGNMENT)
    if alignment not in _COMMENT_ALIGNMENTS:
        _logger.warning(
            "コメントの配置は左寄せ (1/4/7) のみ対応のため %d を使用します: %r",
            _DEFAULT_COMMENT_ALIGNMENT, subtitle_cfg.get("comment_alignment"))
        alignment = _DEFAULT_COMMENT_ALIGNMENT
    return {
        "alignment": alignment,
        "margin_l": subtitle_cfg.get("comment_margin_l",
                                     subtitle_cfg.get("margin_l", 40)),
        "margin_r": subtitle_cfg.get("comment_margin_r",
                                     subtitle_cfg.get("margin_r", 40)),
        "margin_v": subtitle_cfg.get("comment_margin_v",
                                     subtitle_cfg.get("margin_v", 60)),
    }


# 字幕設定 (subtitle セクション) から FontProfile を構築する
# run() とアーカイブ切り抜き (clip_writer) の双方で同一のスタイルを得るために共通化する。
# 役割別カラー/アウトライン色・コメントラベル・装飾・余白など現行 run() と同一の対応。
def build_font_profile(subtitle_cfg):
    return FontProfile(
        family=subtitle_cfg.get("font_family", "Yu Gothic UI"),
        size=subtitle_cfg.get("font_size", 48),
        color_hex=subtitle_cfg.get("own_subtitle_color", "#FFFFFF"),
        # テロップ役割別カラー (配信者=own / サブ / コメント)。欠落時は配信者色へ寄せる。
        role_colors={
            "streamer": subtitle_cfg.get("own_subtitle_color", "#FFFFFF"),
            "sub": subtitle_cfg.get("sub_subtitle_color", ""),
            "comment": subtitle_cfg.get("comment_subtitle_color", ""),
        },
        # 役割別アウトライン色 (追加2)。空文字なら配信者アウトライン色へフォールバック。
        role_outline_colors={
            "streamer": subtitle_cfg.get("outline_color", ""),
            "sub": subtitle_cfg.get("sub_outline_color", ""),
            "comment": subtitle_cfg.get("comment_outline_color", ""),
        },
        # コメント役割の先頭ラベル (request10 追加要望5)。空文字ならラベル無し。
        # ver3 resolve11 C3 でアイコン表示へ置き換えたため既定は空文字。
        comment_label=subtitle_cfg.get("comment_label", ""),
        # 役割別の配置 (ver3 resolve11 §3 D-3)。コメントだけ中央の左へ寄せる。
        role_placements={"comment": _comment_placement(subtitle_cfg)},
        outline_color=subtitle_cfg.get("outline_color", _DEFAULT_OUTLINE_COLOR),
        outline_width=subtitle_cfg.get("outline_width", 3),
        back_color=subtitle_cfg.get("back_color", _DEFAULT_BACK_COLOR),
        bold=subtitle_cfg.get("bold", False),
        italic=subtitle_cfg.get("italic", False),
        underline=subtitle_cfg.get("underline", False),
        strikeout=subtitle_cfg.get("strikeout", False),
        spacing=subtitle_cfg.get("spacing", 0),
        angle=subtitle_cfg.get("angle", 0),
        border_style=subtitle_cfg.get("border_style", 1),
        alignment=subtitle_cfg.get("alignment", 2),
        margin_l=subtitle_cfg.get("margin_l", 40),
        margin_r=subtitle_cfg.get("margin_r", 40),
        margin_v=subtitle_cfg.get("margin_v", 60),
    )


# 縦動画時に字幕設定へ反映する縦用の上書きキー (request14)
# フォントサイズ・改行文字数・表示位置・余白のみを縦タブ(vertical)値で差し替える。
# 色/フォント種類/装飾は横設定を共有する。
_VERTICAL_OVERRIDE_KEYS = (
    "font_size", "min_line_length", "max_line_length",
    "alignment", "margin_l", "margin_r", "margin_v",
    # コメント役割の配置とアイコン (ver3 resolve11 §7.2)。
    # 背景 (余白・角丸・色) は縦横で共通のため上書きしない。
    "comment_alignment", "comment_margin_l", "comment_margin_r", "comment_margin_v",
    "comment_icon_size_px", "comment_icon_gap_px",
)


# 出力プロファイルが縦のとき、字幕設定へ縦用の上書き値を反映した実効設定を返す (request14)
# 横動画/縦無効/プロファイル未解決時は元の字幕設定をそのまま返す (挙動不変)。
def build_effective_subtitle_cfg(subtitle_cfg, vertical_cfg, profile):
    if not (profile and profile.get("is_portrait")):
        return subtitle_cfg
    if not vertical_cfg.get("enabled", True):
        return subtitle_cfg
    effective = dict(subtitle_cfg)
    for key in _VERTICAL_OVERRIDE_KEYS:
        if key in vertical_cfg:
            effective[key] = vertical_cfg[key]
    _logger.info("縦動画のため字幕設定を縦用へ上書き (font/改行/配置/余白)")
    return effective


# subtitle.engine 設定に応じた TextSource 実装を返す
# "none"/未対応/未導入 の場合は None を返し、呼び出し側でテロップ工程をスキップする
# subtitle_cfg 明示時はそれを用いる (縦動画の実効設定で音声認識の改行文字数を切り替えるため)
def resolve_text_source(settings, subtitle_cfg=None):
    if subtitle_cfg is None:
        subtitle_cfg = settings.get("subtitle", {})
    engine = str(subtitle_cfg.get("engine", "whisper")).strip().lower()

    # 明示的な無効化
    if engine in ("", "none", "off"):
        _logger.info("音声認識エンジン未使用 (subtitle.engine=%s)", engine or "未設定")
        return None

    if engine == "whisper":
        return WhisperTextSource(subtitle_cfg)

    # 未対応エンジンは安全側に倒してスキップ
    _logger.warning("未対応の音声認識エンジン指定: %s (テロップ工程をスキップ)", engine)
    return None


# テロップ個別フォント/サイズの ASS インライン上書きタグを生成する (resolve16 §4.4)
# font は ASS フォント名、font_size は実ピクセル値。空/None は該当タグを出さない。
# 何も指定が無ければ空文字を返し、Style(役割) の値をそのまま継承する (後方互換)。
def _inline_font_override(font, font_size):
    override = ""
    if font:
        override += f"\\fn{font}"
    if font_size not in (None, ""):
        size = _to_int(font_size, None)
        if size is not None:
            override += f"\\fs{size}"
    return "{" + override + "}" if override else ""


# 字幕の位置上書きタグを生成する (ver3 resolve.md §8.3)
# entry["pos_x"]/["pos_y"] はキャンバス中心を原点とする正規化座標 (-1.0〜1.0、Y は上が正)。
# 未指定 (キー無し / None) のときは空文字を返し、Style の alignment/margin に従う
# = 既存の生成結果と完全に一致する (後方互換)。
def _inline_position_override(entry, video_width, video_height):
    x = entry.get("pos_x")
    y = entry.get("pos_y")
    if x is None or y is None:
        return ""
    # 正規化座標 → ピクセル (\pos はテキストのアンカー位置。\an5 で中央基準に揃える)
    px = (float(x) + 1.0) * video_width / 2.0
    py = (1.0 - float(y)) * video_height / 2.0
    # コメントだけは左中央アンカー (\an4) にする (ver3 resolve11 §3 D-4)。
    # 中央基準 (\an5) だと「文字列の左端 = 中央 - 文字幅/2」となり、文字幅を測れない
    # Python 側からアイコン・背景の位置を決められないため。他の役割は従来どおり \an5。
    anchor = 4 if str(entry.get("role", "")) == _ROLE_COMMENT else 5
    return f"\\an{anchor}\\pos({px:.1f},{py:.1f})"


# ASS 色文字列 (&HAABBGGRR / &HBBGGRR) を (BBGGRR, AA) へ分解する (resolve6 §5.3)
# アルファを持たない 6 桁形式では alpha に None を返す (Style の透明度を引き継ぐ)。
# 不正値は (None, None) を返し、呼び出し側でタグを出さない。
def _split_ass_color(value):
    text = str(value or "").strip().upper()
    if not text.startswith("&H"):
        return None, None
    digits = text[2:].rstrip("&")
    if not all(c in "0123456789ABCDEF" for c in digits):
        return None, None
    if len(digits) == 8:
        return digits[2:], digits[0:2]
    if len(digits) == 6:
        return digits, None
    return None, None


# HTML 色 (#RRGGBB) を検証して 6 桁の大文字 16 進へ寄せる (不正・空欄は空文字)
# _hex_to_ass_color は不正値を白へ倒すが、インライン上書きでは
# 「不正なら何も指定しない」= Style の色に従うのが正しいためここで弾く。
def _normalize_hex_color(value):
    hex_value = str(value or "").strip().lstrip("#")
    if len(hex_value) != 6:
        return ""
    if not all(c in "0123456789abcdefABCDEF" for c in hex_value):
        return ""
    return hex_value.upper()


# 1 エントリぶんの色上書きタグ (塗り → 縁 の順) を返す (resolve6 §3-3)
# entry["color"]         : 塗り (HTML #RRGGBB)
# entry["outline_color"] : 縁 (ASS &HAABBGGRR。透明度を持てる)
# どちらも未指定なら空文字を返す = 既存の生成結果と完全に一致する。
def _inline_color_override(entry):
    override = ""
    color_hex = _normalize_hex_color(entry.get("color", ""))
    if color_hex:
        # _hex_to_ass_color は "&H00BBGGRR" を返す。インラインタグはアルファを
        # 含まない 6 桁のため、先頭の "&H00" を落として使う。
        override += f"\\1c&H{_hex_to_ass_color(color_hex)[4:]}&"
    bgr, alpha = _split_ass_color(entry.get("outline_color", ""))
    if bgr:
        override += f"\\3c&H{bgr}&"
        if alpha is not None:
            override += f"\\3a&H{alpha}&"
    return override


# 1 エントリぶんのインライン上書きタグ (位置 → フォント → サイズ → 塗り → 縁 の順) をまとめる
# 何も指定が無ければ空文字を返す (既存挙動と同一)。
def _inline_overrides(entry, video_width, video_height):
    position = _inline_position_override(entry, video_width, video_height)
    override = ""
    font = entry.get("font", "")
    if font:
        override += f"\\fn{font}"
    size = _to_int(entry.get("font_size"), None) if entry.get("font_size") not in (None, "") else None
    if size is not None:
        override += f"\\fs{size}"
    # クリップ個別の色 (resolve6 §3-3)。未指定なら空文字が返る。
    override += _inline_color_override(entry)
    if not position and not override:
        return ""
    return "{" + position + override + "}"


# 背景と本文の ASS Layer を決める (ver3 resolve11 §5.11-4)
# ASS は Layer の数値が大きいほど前面。背景 < 本文 でなければ箱が文字を隠すため、
# 逆転していたら警告して本文を 1 つ上へ持ち上げる。
# subtitle_cfg 未指定 (背景を出さない呼び出し) では両方 0 = 従来と完全に同一の出力。
def _comment_layers(subtitle_cfg):
    if subtitle_cfg is None:
        return 0, 0
    bg_layer = _to_int(subtitle_cfg.get("comment_bg_layer"), 0)
    text_layer = _to_int(subtitle_cfg.get("comment_text_layer"), 1)
    if text_layer <= bg_layer:
        _logger.warning(
            "コメントの Layer が逆転しているため本文を %d へ引き上げます "
            "(背景=%d / 本文=%d)", bg_layer + 1, bg_layer, text_layer)
        text_layer = bg_layer + 1
    return bg_layer, text_layer


# タイムラインから ASS 字幕ファイルを生成する
# テロップ役割 (配信者/サブ/コメント) ごとに色違いの Style を定義し、
# 各 Dialogue 行は entry["role"] に対応する Style を参照する (request10)。
# role 欠落 entry は配信者(streamer) 扱い=旧来の単一色と同一挙動 (後方互換)。
# entry["font"]/["font_size"] があれば ASS インライン上書きタグで個別適用する (resolve16 §4.4)。
def build_subtitle_file(timeline, font_profile, output_path,
                        video_width=1920, video_height=1080, subtitle_cfg=None):
    # 役割別 Style 行 (Streamer/Sub/Comment) をまとめて出力する
    style_lines = "\n".join(
        f"Style: {value}" for _name, value in font_profile.iter_role_styles()
    )
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_width}\n"
        f"PlayResY: {video_height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style_lines}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    # コメントの背景と重ね順 (ver3 resolve11 §5.11-4)。
    # subtitle_cfg 未指定の呼び出しでは背景を出さず、Layer も 0 のまま = 従来と同一出力。
    bg_layer, comment_layer = _comment_layers(subtitle_cfg)
    background_count = 0

    body_lines = []
    # 個別フォント/サイズ指定の件数 (ログ出力用 / resolve16 §6)
    font_override_count = 0
    size_override_count = 0
    # 個別の色指定の件数 (ログ出力用 / resolve6 §5.3)
    color_override_count = 0
    outline_override_count = 0
    for entry in timeline:
        start = _format_ass_time(entry["start"])
        end = _format_ass_time(entry["end"])
        text = entry["text"].replace("\n", "\\N")
        role = entry.get("role", _DEFAULT_ROLE)
        # コメント役割のみ先頭に「コメント：」＋改行(\N)を付与する (request10 追加要望5)。
        # 本文は既に \N 折返し済みのため、ラベルは独立行として折返しに巻き込まれない。
        if role == "comment" and font_profile.comment_label:
            text = f"{font_profile.comment_label}\\N{text}"
        # コメントは背景 (角丸の箱) を 1 本手前に出す (ver3 resolve11 §5.11-5)。
        # ラベル付与後・上書きタグ付与前の本文で大きさを見積もる (実際に出る行がこれのため)。
        background = None
        if subtitle_cfg is not None and role == _ROLE_COMMENT:
            background = comment_decor.build_background_text(
                entry, subtitle_cfg, video_width, video_height, text=text)
        # 個別フォント/サイズ/位置の上書きタグを本文最先頭へ付与する
        # (resolve16 §4.4 / ver3 §8.3)。ラベル部にも同フォントを適用するため
        # コメントラベル付与より後に前置する (§8-5 確定)。
        entry_font = entry.get("font", "")
        entry_size = entry.get("font_size")
        override = _inline_overrides(entry, video_width, video_height)
        if override:
            text = override + text
            if entry_font:
                font_override_count += 1
            if entry_size not in (None, ""):
                size_override_count += 1
            if _normalize_hex_color(entry.get("color", "")):
                color_override_count += 1
            if _split_ass_color(entry.get("outline_color", ""))[0]:
                outline_override_count += 1
        # 役割に対応する Style 名で色分けする (role 未指定は配信者)
        style_name = font_profile.style_for_role(role)
        # 背景 → 本文 の順に並べ、Layer でも前後関係を明示する (実装依存の描画順に頼らない)
        if background:
            body_lines.append(
                f"Dialogue: {bg_layer},{start},{end},{style_name},,0,0,0,,{background}")
            background_count += 1
        layer = comment_layer if role == _ROLE_COMMENT else 0
        body_lines.append(
            f"Dialogue: {layer},{start},{end},{style_name},,0,0,0,,{text}")

    # 個別指定の件数を INFO 出力する (resolve16 §6 / resolve6 §5.3)。
    # 0 件時のみ従来同様に静かに済ませる。
    if background_count:
        _logger.info("コメント背景: %d 件", background_count)

    if (font_override_count or size_override_count
            or color_override_count or outline_override_count):
        _logger.info(
            "テロップ個別指定 フォント %d 件 / サイズ %d 件 / 文字色 %d 件 / 縁の色 %d 件",
            font_override_count, size_override_count,
            color_override_count, outline_override_count,
        )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(body_lines) + "\n")
    return output_path


# 秒値を ASS タイムスタンプ (H:MM:SS.cc) に変換する
def _format_ass_time(seconds):
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


# 字幕ファイルを動画に焼き込む
# 入力 (無音カット出力) のタイムスタンプ破損 (非単調PTS/異常duration) に備え、
# 映像/音声のタイムスタンプを再生成しつつ CFR で再エンコードする (resolve 20260630 対策A)。
# これにより mux 時の "Application provided duration ... is invalid" (EINVAL=-22) と
# vsync による大量フレーム脱落 (drop) を回避する。
# CFR 化は fps フィルタ (実PTS基準) で行い、映像と音声の同期を保つ (resolve 20260719 対策A)。
def burn_subtitle(input_path, subtitle_path, output_path, ffmpeg_settings,
                  total_duration=0.0, on_progress=None, target_size=None,
                  fonts_dir=None, extra_chains=None, filter_script_path=None,
                  filter_script_chars=0, pre_chains=None, pre_out_label="[vpre]"):
    # subtitles フィルタは2段階エスケープに従う (resolve 20260630 resolve2.md 対策A):
    #   ・シングルクォートでフィルタグラフ階層(空白/カンマ)を保護し、内側の '\' を下位へ通す
    #   ・フィルタ内オプション階層では ':' がなお区切り文字のため '\:' でエスケープする
    # 両者は別階層に作用するため併用が正しい (クォートだけでは ':' 分割を防げず
    # ドライブレターが original_size オプションへ流れて EINVAL になる)。
    safe_path = subtitle_path.replace("\\", "/").replace(":", "\\:")
    # 先に fps フィルタで映像を実時刻基準の CFR へ整え、その後 subtitles を焼く。
    # 【映像/音声 同期ズレ対策 / 順序が重要】(resolve 20260719 対策A)
    #   無音カット出力 (seek抽出→concat -c copy) の映像PTSには、区間境界ごとに
    #   約21ms (=AAC 1フレーム 1024/48000) の空白が入っている。区間ファイルの音声は
    #   AAC のフレーム量子化により必ず映像より 1フレーム長くなり、concat デマルチプレクサは
    #   次区間のオフセットを「ファイル尺=長い方=音声尺」で進めるため、映像側にだけ穴が空く。
    #   この時点では各フレームは正しい時刻に置かれており、映像と音声は同期している。
    #   旧実装の setpts=N/FRAME_RATE/TB はフレーム番号で全フレームを等間隔に振り直すため、
    #   この不均一な空白を一様に均してしまい、カット密度の偏りに応じて映像だけが前後した
    #   (実測: 2時間素材で最大 0.93 秒の映像先行 / docs/error/20260719)。
    #   fps={output_fps} は各フレームの実PTSを見て複製/間引きするため、絵と実時刻の対応が
    #   保たれ、実時刻を保持する音声 (aresample) と整合する。空白は複製フレームで埋まる。
    #   テロップ位置は「実PTS + 実時刻由来の whisper タイムスタンプ」で自然に一致するため、
    #   setpts による人為的な前ズレ補正は不要。
    #   fps 明示により setpts の FRAME_RATE 暗黙解決 (avg か r か / ffmpeg ビルド依存) にも
    #   依存しなくなる。無音カット出力は r_frame_rate が 240/1 等へ壊れることがあるため重要。
    # 縦動画時 (target_size 指定時) は subtitles の前に scale+pad を挟み、
    # 本編を縦キャンバス(例 1080x1920)へ正規化してから字幕を焼く (request14)。
    # アスペクト比を保ったまま縮小し、余白は中央寄せの黒帯で埋める。ASS の PlayRes と一致させる。
    # 横動画 (target_size=None) では scale を挟まず従来と完全に同一の挙動とする。
    scale_chain = ""
    if target_size:
        canvas_w, canvas_h = target_size
        scale_chain = (
            f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
            f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        )
    # 追加フォント(settings/fonts)を libass に解決させるため fontsdir を付与する (resolve16 §4.3)。
    # 別プロセスの ffmpeg/libass は Qt の addApplicationFont を参照できないため、ここで明示する。
    # 未作成/空なら付与せず従来と完全同一の挙動にする。
    subtitles_opt = f"subtitles='{safe_path}'"
    if fonts_dir and os.path.isdir(fonts_dir):
        # subtitle_path と同一の2段階エスケープ ('\'→'/', ':'→'\:') を適用する
        safe_fonts_dir = fonts_dir.replace("\\", "/").replace(":", "\\:")
        subtitles_opt += f":fontsdir='{safe_fonts_dir}'"
        # fontsdir 使用時はディレクトリと登録フォント数を INFO 出力する (resolve16 §6)
        font_count = sum(
            1 for name in os.listdir(fonts_dir)
            if os.path.splitext(name)[1].lower() in (".ttf", ".otf", ".ttc")
        )
        _logger.info("追加フォント fontsdir を適用: %s (%d 件)", fonts_dir, font_count)
    fps = ffmpeg_runner.get_output_fps(ffmpeg_settings)
    filter_spec = f"fps={fps},{scale_chain}{subtitles_opt}"
    # 追加のフィルタチェーン (コメントアイコンの overlay / ver3 resolve11 §5.4-4)。
    # 空なら 1 文字も足さない = 従来と完全に同一のコマンドになる。
    # -vf は「入力1・出力1」ならラベル付きの複数チェーンを書けるため、
    # subtitles の出力へラベルを付けて鎖の続きを繋ぐ (-filter_complex へ作り替えない)。
    if extra_chains:
        filter_spec = f"{filter_spec}[vsub];" + ";".join(extra_chains)
    # 字幕を焼く**前**に掛けるフィルタチェーン (ぼかし / ver5 resolve2 §5.5.2)。
    # 字幕やコメントアイコンをぼかしてはいけない (R8) ため、後ろ (extra_chains) ではなく
    # 先頭へ繋ぐ。pre_chains の最終出力ラベルを、字幕チェーンの入力として受け取る。
    # 空なら 1 文字も足さない = 従来と完全に同一のコマンドになる。
    if pre_chains:
        filter_spec = ";".join(pre_chains) + f";{pre_out_label}{filter_spec}"

    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
    # 表示区間が多いと enable 式が長くなりコマンドライン長の上限に触れるため、
    # 閾値を超えたらフィルタをファイルへ逃がす (ver3 resolve11 §3 D-6)。
    filter_args = ["-vf", filter_spec]
    if (filter_script_path and filter_script_chars
            and len(filter_spec) > int(filter_script_chars)):
        try:
            with open(filter_script_path, "w", encoding="utf-8") as f:
                f.write(filter_spec)
            filter_args = ["-filter_script:v", filter_script_path]
            _logger.info("フィルタが長いためスクリプトへ退避しました (%d 文字): %s",
                         len(filter_spec), filter_script_path)
        except OSError as e:
            _logger.warning("フィルタスクリプトを書けないため直接指定します: %s", e)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-fflags", "+genpts",          # 欠落PTSを再生成して読み込む
        "-i", input_path,
        *filter_args,
        "-af", "aresample=async=1:first_pts=0",  # 音声を実時刻・先頭0基準で保持し同期維持
        *ffmpeg_runner.build_encode_options(ffmpeg_settings),
        "-fps_mode", "cfr",            # 固定フレームレート化 (有効な duration を保証)
        "-r", str(fps),                # 出力フレームレート (setting.json: ffmpeg.output_fps)
        output_path,
    ]
    ffmpeg_runner.execute(cmd, total_duration=total_duration, on_progress=on_progress)
    return output_path


# 発話とみなせる有音区間を検出する (テロップ表示の絞り込み用)
# silence_cutter を再利用し、無音カットより高い閾値(speech_threshold_db)で再解析する
# 戻り値: [(start_sec, end_sec), ...] (発話=有音区間)
def detect_speech_regions(input_path, total_duration, speech_threshold_db,
                          min_duration_sec, ffmpeg_settings):
    silence_ranges = silence_cutter.detect_silence(
        input_path, speech_threshold_db, min_duration_sec, ffmpeg_settings
    )
    # 無音の補集合 = 発話(有音)区間
    return silence_cutter.build_keep_segments(silence_ranges, total_duration)


# 表示区間の前後にパディングを付与しつつ、負値・逆転・隣接重複を補正する
def _apply_padding(timeline, pad_sec):
    if pad_sec <= 0:
        return timeline
    padded = []
    prev_end = None
    for entry in timeline:
        start = entry["start"] - pad_sec
        end = entry["end"] + pad_sec
        if start < 0:
            start = 0.0
        # 直前イベントとの重複を防ぐ (隣接区間がパディングで重ならないよう調整)
        if prev_end is not None and start < prev_end:
            start = prev_end
        # 開始 >= 終了 になった場合は破棄 (不正区間)
        if start >= end:
            continue
        padded.append({"start": start, "end": end, "text": entry["text"]})
        prev_end = end
    return padded


# Whisper タイムラインを発話区間で絞り込む
# mode="trim": 各セグメントを「区間内の最初の発話開始〜最後の発話終了」に丸める
# mode="split": 発話小区間ごとに同一テキストの表示区間を生成する
def gate_timeline_by_speech(timeline, speech_regions, mode="trim", pad_sec=0.0):
    gated = []
    for seg in timeline:
        # セグメントと交差する発話小区間を抽出する
        overlaps = [
            (max(seg["start"], s), min(seg["end"], e))
            for (s, e) in speech_regions
            if e > seg["start"] and s < seg["end"]
        ]
        if not overlaps:
            continue  # 区間内に発話なし → テロップ非表示
        if mode == "split":
            for (s, e) in overlaps:
                gated.append({"start": s, "end": e, "text": seg["text"]})
        else:  # trim: 最初の発話開始〜最後の発話終了に丸める
            gated.append({
                "start": overlaps[0][0],
                "end": overlaps[-1][1],
                "text": seg["text"],
            })
    return _apply_padding(gated, pad_sec)


# 設定値に基づきタイムラインを発話区間で絞り込む
# 前提を満たさない場合や失敗時は安全側に倒し、元のタイムラインを返す
def _gate_with_settings(input_path, timeline, total_duration,
                        subtitle_cfg, silence_cfg, ffmpeg_cfg):
    speech_threshold_db = subtitle_cfg.get("speech_threshold_db", -16)
    noise_threshold_db = silence_cfg.get("noise_threshold_db", -30)

    # 発話検出閾値は無音カット閾値より大きい(=より大きい音量)である必要がある
    if speech_threshold_db <= noise_threshold_db:
        _logger.warning(
            "発話検出閾値(%sdB)が無音カット閾値(%sdB)以下のため絞り込みをスキップ",
            speech_threshold_db, noise_threshold_db,
        )
        return timeline

    min_duration_sec = float(subtitle_cfg.get("speech_min_duration_sec", 0.2))
    mode = str(subtitle_cfg.get("speech_gate_mode", "trim")).strip().lower()
    pad_sec = float(subtitle_cfg.get("speech_pad_sec", 0.1))

    try:
        speech_regions = detect_speech_regions(
            input_path, total_duration, speech_threshold_db, min_duration_sec, ffmpeg_cfg
        )
    except DiskSpaceError:
        raise       # 容量不足は継続しない (resolve.md §6-9)
    except FFmpegError as e:
        # 検出失敗時はテロップ全体を失わないよう従来タイムラインで継続
        _logger.warning("発話区間検出に失敗したため絞り込みをスキップ: %s", e)
        return timeline

    # 発話区間が0件 → 全テロップ消失を避けるためスキップ
    if not speech_regions:
        _logger.warning("発話区間が検出できなかったため絞り込みをスキップ")
        return timeline

    gated = gate_timeline_by_speech(timeline, speech_regions, mode=mode, pad_sec=pad_sec)

    # 絞り込み結果が空 → 安全側に倒して元タイムラインを返す
    if not gated:
        _logger.warning("絞り込み後のテロップが空になったため元のタイムラインを使用")
        return timeline

    _logger.info(
        "発話区間でテロップを絞り込み: %d → %d セグメント (閾値=%sdB, mode=%s)",
        len(timeline), len(gated), speech_threshold_db, mode,
    )
    return gated


# 表示区間を発話タイミングへ整形する (docs/error/20260627/resolve.md 対策B)
# 入力 timeline は発話実時間 ({"start"=発話頭, "end"=発話末, "text"})。
# ルール:
#   - 発話の頭(start)で表示開始する (R1)
#   - 次の発話まで max_hold_sec を超える場合は「発話末+max_hold_sec」で削除する (R2)
#   - max_hold_sec 以内に次の発話がある場合は次の発話開始まで表示し、隙間なく切り替える (R3)
# min_duration_sec はチラつき防止の表示下限 (次の発話に食い込む場合は次発話を優先)。
#
# cut_bounds (ver8 resolve6 R3): 詰めた軸のカット境界 [(head, tail), ...]。
# 渡すと表示をカットの終端で止める (カット頭で必ず次のテロップへ切り替わる)。
# 既定 None で従来どおり = 旧 (物理カット) 経路の挙動は変わらない。
def adjust_display_timing(timeline, max_hold_sec=2.0, min_duration_sec=0.0,
                          cut_bounds=None):
    if not timeline:
        return timeline
    # 発話開始の昇順に整列する (入力リストは破壊しない)
    ordered = sorted(timeline, key=lambda e: e["start"])
    adjusted = []
    for i, entry in enumerate(ordered):
        disp_start = entry["start"]
        speech_end = entry["end"]
        has_next = i + 1 < len(ordered)
        next_start = ordered[i + 1]["start"] if has_next else None

        if has_next and (next_start - speech_end) <= max_hold_sec:
            # 2秒以内に次の発話 → 次の発話開始まで表示し、そのまま切り替える (R3)
            disp_end = next_start
        else:
            # 次の発話なし / 次まで2秒超 → 発話末+最大2秒で削除する (R2)
            disp_end = speech_end + max_hold_sec

        # チラつき防止の下限。次の発話には食い込まないよう抑える
        if min_duration_sec > 0 and (disp_end - disp_start) < min_duration_sec:
            disp_end = disp_start + min_duration_sec
            if has_next:
                disp_end = min(disp_end, next_start)

        # 表示を「発話が終わったカットの終端」で止める (ver8 resolve6 第3部 R3')
        # 第2部は開始を含むカットの終端で切っていたため、カットを跨ぐテロップが
        # 最初のカットの終端で消えていた (§10.2 原因G)。基準を発話終了側へ移し、
        # 跨ぎは許したまま「次のカットへ余分に残り続ける」ことだけを防ぐ。
        # 開始がカット終端ちょうどのときはクランプしない (尺が 0 になるため。
        # 下の不正区間判定へ任せる)。
        if cut_bounds is not None:
            probe = max(float(speech_end) - _EPS, float(disp_start))
            tail = _cut_tail_at(cut_bounds, probe)
            if tail is None:
                tail = _cut_tail_at(cut_bounds, disp_start)
            if tail is not None and tail - disp_start > _EPS:
                disp_end = min(disp_end, tail)

        # 不正区間 (終了<=開始) は破棄する
        if disp_end <= disp_start:
            continue
        adjusted.append({"start": disp_start, "end": disp_end, "text": entry["text"]})
    return adjusted


# 編集画面ブリッジへ Resolve 出力用の付随情報を渡す (resolve20 §5.3)
# 元入力パス・無音カットの編集点・出力プロファイルを渡し、字幕一覧画面から
# 「DaVinci Resolve 出力」を行えるようにする。焼き込み処理には一切影響しない。
def _attach_export_context(review_cb, context):
    setter = getattr(review_cb, "set_export_context", None)
    if not callable(setter):
        return
    keep_getter = getattr(context, "keep_segments", None)
    setter({
        "source_path": getattr(context, "input_path", ""),
        "keep_segments": keep_getter() if callable(keep_getter) else None,
        "settings": context.settings,
        "profile": getattr(context, "output_profile", None),
    })


# 編集画面ブリッジへ動画プレビュー用の付随情報を渡す (resolve23 §5.3)
# レビュー時点の動画 (無音カット後の中間動画) は items と時間軸が一致するため、
# 字幕一覧画面の「字幕適用プレビュー」の再生素材として渡す。焼き込みには影響しない。
# subtitle_cfg は build_effective_subtitle_cfg 適用済みの実効字幕設定を渡すこと。
def _attach_preview_context(review_cb, context, subtitle_cfg):
    setter = getattr(review_cb, "set_preview_context", None)
    if not callable(setter):
        return
    setter({
        "video_path": context.current_video_path(),
        "eff_cfg": subtitle_cfg,
        "profile": getattr(context, "output_profile", None),
        "settings": context.settings,
    })


# 字幕編集画面 (レビュー) を適用する (resolve3 §6.2)
# context にレビューコールバックが注入されていれば呼び出し、編集結果で置き換える。
# 注入が無い (CLI/ヘッドレス) 場合は全件使用としてそのまま返す。
# 戻り値: 焼き込み対象タイムライン ([{start,end,text}])。
#         空リストは「使用0件=テロップ無しで続行」を表す。
# キャンセル時は PipelineCancelled を送出してパイプラインを中断する (§10-2)。
def _review_timeline(context, timeline, subtitle_cfg):
    # 設定で無効化されていればスキップ (全件そのまま使用)
    if not subtitle_cfg.get("review_enabled", True):
        return timeline

    review_cb = getattr(context, "subtitle_review_callback", None)
    if review_cb is None:
        # CLI など画面を持たない実行経路: 全件使用
        return timeline

    # 編集画面へ渡す入力を生成する (初期値は全件チェック=使用、役割は既定=配信者)
    items = [
        {"start": e["start"], "end": e["end"], "text": e["text"], "use": True,
         "role": e.get("role", _DEFAULT_ROLE)}
        for e in timeline
    ]

    # Resolve 出力 (resolve20 §5.3) 用の付随情報を編集画面ブリッジへ渡す。
    # ブリッジが対応していない実行経路 (CLI/テスト) では何もしない。
    _attach_export_context(review_cb, context)
    # 動画プレビュー (resolve23) 用の付随情報も同様に渡す (未対応経路では何もしない)。
    _attach_preview_context(review_cb, context, subtitle_cfg)

    edited = review_cb(items)  # 「字幕決定」まで待機し編集結果を返す

    # キャンセル (None) 時はパイプライン全体を中断する (§10-2)
    if edited is None:
        raise PipelineCancelled(tr("字幕編集がキャンセルされたためパイプラインを中断します"))

    # 使用チェックされたエントリのみを残す (use キーは焼き込み前に除去)
    # 確定時に改行を再適用する (手動改行 \\N は尊重しつつ長い行を折る)。
    # wrap_engine="budoux" で文節改行、それ以外/未導入時は文字数改行 (request11)。
    # 役割 (role) は焼き込み時の色分けに使うため保持する。
    min_len = int(subtitle_cfg.get("min_line_length", 15))
    max_len = int(subtitle_cfg.get("max_line_length", 20))
    engine = str(subtitle_cfg.get("wrap_engine", "budoux")).strip().lower()
    used = [
        {"start": e["start"], "end": e["end"],
         "text": wrap_lines(e["text"], min_len, max_len, engine),
         "role": e.get("role", _DEFAULT_ROLE),
         # 個別フォント/サイズ (Blank/空欄はデフォルト=タグ非付与 / resolve16 §4.4)
         "font": e.get("font", ""),
         "font_size": e.get("font_size")}
        for e in edited
        if e.get("use", True)
    ]

    # 使用0件 → テロップ無しで続行 (§10-3)。呼び出し側でスキップさせる
    if not used:
        return []
    return used


# 公開エントリポイント
def run(context):
    settings = context.settings
    subtitle_cfg = settings.get("subtitle", {})
    silence_cfg = settings.get("silence_cut", {})
    ffmpeg_cfg = settings.get("ffmpeg", {})

    # テロップ ON/OFF
    if not subtitle_cfg.get("enabled", True):
        _logger.info("テロップ生成スキップ (subtitle.enabled=false)")
        return context.current_video_path()

    # 出力プロファイル(縦/横)に応じた実効字幕設定を求める (request14)
    # 縦動画時はフォント/改行文字数/配置/余白を縦タブ(vertical)値で上書きする。
    profile = getattr(context, "output_profile", None)
    subtitle_cfg = build_effective_subtitle_cfg(
        subtitle_cfg, settings.get("vertical", {}), profile
    )
    # 縦動画は 1080x1920 等の縦キャンバスへ正規化する。横は従来どおり scale を挟まない。
    is_portrait = bool(profile and profile.get("is_portrait"))
    canvas_width = int(profile["width"]) if profile else 1920
    canvas_height = int(profile["height"]) if profile else 1080
    target_size = (canvas_width, canvas_height) if is_portrait else None

    # テキストソース解決 (subtitle.engine に応じた音声認識エンジン)
    # 実効字幕設定を渡し、縦動画では音声認識の改行文字数(10〜15)を反映する。
    text_source = resolve_text_source(settings, subtitle_cfg)
    if text_source is None:
        # engine="none"/未対応/未導入 のため安全にスキップ
        _logger.info("音声認識エンジン無効のためテロップ生成をスキップ")
        return context.current_video_path()

    font_profile = build_font_profile(subtitle_cfg)

    input_path = context.current_video_path()
    output_path = context.allocate_intermediate("subtitle.mp4")
    subtitle_path = context.allocate_intermediate("subtitle.ass")

    try:
        timeline = text_source.extract(input_path, subtitle_cfg.get("language", "ja"))
    except Exception as e:
        raise SubtitleError(
            tr("テキストソース抽出に失敗: {error}", error=e)) from e

    if not timeline:
        _logger.warning("テロップタイムラインが空のためスキップ")
        return context.current_video_path()

    total_duration = ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg)

    # 要望(resolve7)により無効化。将来戻す可能性があるため処理を温存する。
    # 旧方式: subtitle の発話検出閾値(dB)でテロップ表示を絞り込んでいた。
    # 無音カットを音量解析の単一閾値へ一本化したため、ここでの絞り込みは行わない。
    # 発話区間によるテロップ絞り込み (発話時のみテロップを表示する)
    # if subtitle_cfg.get("speech_gate_enabled", True):
    #     timeline = _gate_with_settings(
    #         input_path, timeline, total_duration, subtitle_cfg, silence_cfg, ffmpeg_cfg
    #     )

    # 表示タイミング整形 (docs/error/20260627/resolve.md 対策B)
    # 発話のタイミングで表示し、発話後は最大 display_max_hold_sec 秒で削除する。
    # 2秒以内に次の発話がある場合は隙間なく次のテロップへ切り替える。
    # レビュー画面の Start/End を最終表示に一致させるため、レビュー前に適用する (§6-4)。
    max_hold_sec = float(subtitle_cfg.get("display_max_hold_sec", 2.0))
    min_duration_sec = float(subtitle_cfg.get("display_min_duration_sec", 0.5))
    before_count = len(timeline)
    timeline = adjust_display_timing(timeline, max_hold_sec, min_duration_sec)
    _logger.info(
        "テロップ表示タイミング整形: %d → %d 区間 (max_hold=%.1fs, min_dur=%.1fs)",
        before_count, len(timeline), max_hold_sec, min_duration_sec,
    )

    # 字幕編集画面 (レビュー) フック ── GUI 経路でのみ注入される (resolve3 §6.1)
    # 誤訳修正・使用可否の選択を行い、焼き込み対象を確定する
    timeline = _review_timeline(context, timeline, subtitle_cfg)
    if not timeline:
        # 使用0件 → テロップ焼き込みをスキップし、ゲート前の動画をそのまま次工程へ (§10-3)
        _logger.info("使用するテロップが無いため焼き込みをスキップ")
        return context.current_video_path()

    # ASS の PlayRes を出力キャンバス寸法に一致させる (縦=1080x1920 / 横=1920x1080)
    build_subtitle_file(
        timeline, font_profile, subtitle_path,
        video_width=canvas_width, video_height=canvas_height,
        # コメントの背景 (角丸の箱) を出す (ver3 resolve11 §5.6-3)
        subtitle_cfg=subtitle_cfg,
    )
    # コメントアイコンを字幕の上へ重ねる (対象が無ければコマンドは従来と同一)
    icon_chains, _icon_count, _icon_groups = comment_decor.build_icon_chains(
        timeline, subtitle_cfg, canvas_width, canvas_height,
        in_label="[vsub]", out_label="")
    burn_subtitle(
        input_path, subtitle_path, output_path, ffmpeg_cfg,
        total_duration=total_duration,
        on_progress=context.progress_subcallback("フルテロップ生成"),
        # 縦動画のみキャンバス正規化(scale+pad)を行う。横は None で従来挙動。
        target_size=target_size,
        # 追加フォント(settings/fonts)を libass に解決させる (resolve16 §4.3)
        fonts_dir=resolve_fonts_dir(settings),
        extra_chains=icon_chains,
        filter_script_path=context.allocate_intermediate("subtitle_vf.txt"),
        filter_script_chars=subtitle_cfg.get("comment_icon_filter_script_chars", 8000),
    )
    context.set_current_video_path(output_path)
    return output_path


# ==================================================================
# Timeline (ver3) 経路の音声認識
# ==================================================================

# 認識結果 (素材時間) を「残す区間を詰めた軸」= Timeline 時間へ写す
# (docs/error/20260812/resolve2.md §4)
#
# 素材そのままの音声を認識すると時刻の精度が上がる代わりに、時刻は素材時間で返る。
# ここで TimeMap を通して詰めた軸へ写し直すことで、
# 「recognize_for_timeline は詰めた軸の items を返す」という契約を保つ。
#
# ・カットを跨ぐ字幕     : 詰めた結果 Timeline 上では連続するため 1 区間へまとまる
# ・一部がカットへかかる : かかった分だけ短くなる
# ・全体がカットへ落ちる : 載せない (カットした無音への幻聴字幕もここで落ちる)
#
# 戻り値: (写像後の timeline, 落とした件数)
def map_items_to_timeline(timeline_items, keep_segments):
    timemap = TimeMap.from_segments(keep_segments)
    mapped = []
    dropped = 0
    for entry in timeline_items or []:
        pieces = timemap.split_interval(entry["start"], entry["end"])
        if not pieces:
            dropped += 1
            continue
        # 先頭の開始〜末尾の終了で 1 区間にする (連続する区間は split_interval が結合済み)
        mapped.append({**entry, "start": pieces[0][0], "end": pieces[-1][1]})
    return mapped, dropped


# ------------------------------------------------------------------
# カット跨ぎの分割とカット頭への整列 (ver8 resolve6)
# ------------------------------------------------------------------
# 映像カットの単位は「無音で区切った有音区間 (keep_segments)」、テロップの単位は
# 「Whisper の認識セグメント (文の区切り)」で、別の基準で切られている。
# 跨いだセグメントは map_items_to_timeline() が 1 本へまとめるため、
# テロップが前のカットの途中から流れてしまう (resolve6 §2-1)。
# ここでカット跨ぎを単語単位で分け、各カットの先頭テロップだけを頭へ寄せる。
#
# 1 カット内のテロップの本数は変えない (resolve6 §3-3)。
# 分割が本数を増やすのは「カットを跨いだセグメント」だけで、カット内の区切りや
# 2 本目以降の開始位置には一切触らない。
#
# ただしこの「カットごとに必ず 1 本」が文脈の続くテロップを裁断したため
# (resolve6 §10.2 原因F)、既定の単位は文字数へ移した (第3部 R1' /
# regroup_items_by_length)。以下の分割は subtitle_unit = "cut" の退路として残す。


# item と重なる「残す区間」を昇順で返す (2 本以上なら item はカットを跨いでいる)
def _keep_spans_of(item, keep_segments):
    start = float(item.get("start", 0.0))
    end = float(item.get("end", 0.0))
    spans = []
    for seg_start, seg_end in (keep_segments or []):
        span_start = float(seg_start)
        span_end = float(seg_end)
        if min(end, span_end) - max(start, span_start) > _EPS:
            spans.append((span_start, span_end))
    spans.sort()
    return spans


# 単語が属する区間の番号を返す (属せなければ None)
# 通常は「単語の中点が入る区間」で決まる。中点がカット済みの無音へ落ちた単語は
# 重なりが最大の区間へ寄せる (文字を落とさないため)。
def _span_index_for_word(word, spans):
    start = float(word.get("start", 0.0))
    end = float(word.get("end", 0.0))
    middle = (start + end) / 2.0
    best = None
    best_overlap = 0.0
    for index, (span_start, span_end) in enumerate(spans):
        if span_start - _EPS <= middle < span_end - _EPS:
            return index
        overlap = min(end, span_end) - max(start, span_start)
        if overlap > best_overlap:
            best_overlap = overlap
            best = index
    return best


# 単語列の発話長 (先頭語の開始〜末尾語の終了)
def _words_speech_len(words):
    if not words:
        return 0.0
    return max(w["end"] for w in words) - min(w["start"] for w in words)


# 単語を区間ごとに振り分ける (resolve6 §4-2)
# 戻り値: [{"words": 時刻を決める単語, "text_words": 本文を作る単語}, ...]
#
# 先頭の片の発話長が min_piece_sec 未満のときは、その語を次の片の本文の先頭へ
# 寄せる (§4-2-1)。寄せるのは「文字」だけで、時刻は寄せない。時刻まで持ち込むと
# 区間が再びカットを跨ぎ、元の症状へ戻ってしまう。
def _words_by_span(words, spans, min_piece_sec=0.0):
    buckets = [[] for _ in spans]
    for word in (words or []):
        index = _span_index_for_word(word, spans)
        if index is None:
            continue
        buckets[index].append(word)

    pieces = [{"words": b, "text_words": list(b)} for b in buckets if b]
    while len(pieces) >= 2 and _words_speech_len(pieces[0]["words"]) < min_piece_sec - _EPS:
        head = pieces.pop(0)
        pieces[0]["text_words"] = head["text_words"] + pieces[0]["text_words"]
    return pieces


# 認識セグメントを「残す区間」ごとに分ける (resolve6 R1)
# 入力・出力はどちらも素材時間。カットを跨がないセグメントは一切触らない。
#   ・跨いでいない                → そのまま通す (従来と完全に同一)
#   ・跨いでいて単語が取れない    → そのまま通す (安全側へ倒す / R4)
#   ・跨いでいて単語が取れる      → カットごとの字幕へ分ける
# 分けた各片の start/end は「その片に入った単語の実時間」。keep_segments の端では
# なく単語の端を使うことで、カット内での遅れを整列処理 (R2) へ正しく渡す。
#
# wrap: 本文の整形関数 (フィラー除去＋改行)。単語から本文を組み直すため、
#       認識時と同じ整形を掛け直す必要がある。
# 戻り値: (items, 分割で増えた件数)
def split_items_by_keep_segments(items, keep_segments, wrap=None,
                                 min_piece_sec=0.0):
    out = []
    added = 0
    for item in (items or []):
        words = item.get("words")
        spans = _keep_spans_of(item, keep_segments) if words else []
        if len(spans) <= 1:
            out.append(item)
            continue

        pieces = _words_by_span(words, spans, min_piece_sec)
        emitted = 0
        for piece in pieces:
            text = "".join(w["text"] for w in piece["text_words"]).strip()
            if not text:
                continue
            out.append({
                **item,
                "start": min(w["start"] for w in piece["words"]),
                "end": max(w["end"] for w in piece["words"]),
                "text": wrap(text) if wrap else text,
                "words": piece["words"],
            })
            emitted += 1

        if emitted == 0:
            # 単語から本文を作れなかった (防御的なケース) → 元の 1 本で通す
            out.append(item)
            continue
        added += emitted - 1
    return out, added


# ------------------------------------------------------------------
# 文字数で決めるテロップ単位 (ver8 resolve6 第3部 R1')
# ------------------------------------------------------------------
# カットごとに必ず 1 本作ると、1 文が息継ぎ (無音 0.6 秒) で切られた数だけ
# テロップへ裁断される (resolve6 §10.2 原因F)。そこで単位の決め方を
# 「カット」から「文字数」へ移し、カット境界は「切ってよい候補」へ格下げする。
#   ・下限 (min_chars) 未満の間は切らない    → カットを跨いで繋ぐ (1 テロップ : N クリップ)
#   ・下限達成後はカット境界・文末・認識セグメント境界で切る
#                                             → カット頭からテロップが始まる
#   ・上限 (max_chars) 超過はカット内でも切る → 1 クリップ : N テロップ

# テロップ単位の決め方 (subtitle.subtitle_unit)
_SUBTITLE_UNIT_LENGTH = "length"    # 文字数で単位を作る (第3部 / 既定)
_SUBTITLE_UNIT_CUT = "cut"          # カットごとに必ず 1 本 (第2部)
_SUBTITLE_UNIT_SEGMENT = "segment"  # 認識セグメントそのまま (ver8 以前)
_SUBTITLE_UNITS = (_SUBTITLE_UNIT_LENGTH, _SUBTITLE_UNIT_CUT, _SUBTITLE_UNIT_SEGMENT)

# 文末と見なす記号 (下限達成後はここで区切ってよい / resolve6 §11-2 B3)
_SENTENCE_END = ("。", "．", "！", "？", "!", "?")

# カットの外へこぼれた単語を拾う許容差 (秒 / resolve6 第4部 R6)
# silence_cut.min_silence_duration_sec = 0.6 の半分未満にしてあるため、
# 取り除かれた無音の中央は必ず空く = 無音へ出た幻聴字幕は拾わない (§18-5)。
_DEFAULT_WORD_ATTACH_TOLERANCE_SEC = 0.25

# 寄せた単語に残す最低の尺 (秒)。尺 0 の区間は写像で捨てられるため (§18-3)
_MIN_WORD_SEC = 0.05


# テロップ単位の決め方を解決する (resolve6 §13-1 / 後方互換つき)
# subtitle_unit が無い旧 setting.json では split_across_cut から決める
# (false = カットに合わせる処理を入れていない状態 → 認識セグメントそのまま)。
def subtitle_unit(subtitle_cfg):
    cfg = subtitle_cfg or {}
    value = str(cfg.get("subtitle_unit", "")).strip().lower()
    if value in _SUBTITLE_UNITS:
        return value
    if value:
        _logger.warning(
            "subtitle.subtitle_unit が不正なため '%s' を使います: %s",
            _SUBTITLE_UNIT_LENGTH, value)
        return _SUBTITLE_UNIT_LENGTH
    return (_SUBTITLE_UNIT_LENGTH if cfg.get("split_across_cut", True)
            else _SUBTITLE_UNIT_SEGMENT)


# 単語の末尾が文末記号かどうか
def _ends_sentence(word):
    text = str((word or {}).get("text", "")).strip()
    return bool(text) and text.endswith(_SENTENCE_END)


# 単語が属するカット番号と次の走査位置を返す (カットされた無音の中なら番号は None)
# 単語列・keep_segments はどちらも時間順のため、走査位置 cursor を前へ進めるだけで足りる。
# 中点が無音へ落ちた単語は重なりが最大のカットへ寄せる
# (文字を落とさないため。規則は _span_index_for_word() と同じ)。
def _cut_index_of_word(word, keep_segments, cursor=0):
    segments = keep_segments or []
    start = float(word.get("start", 0.0))
    end = float(word.get("end", 0.0))
    middle = (start + end) / 2.0

    # 単語より完全に手前のカットは以後の単語でも使わないため走査位置を進める
    index = min(max(int(cursor), 0), len(segments))
    while index < len(segments) and float(segments[index][1]) <= start + _EPS:
        index += 1

    hit = _cut_index_from(segments, index, start, end, middle)
    if hit is not None or index == 0:
        return hit, index
    # 走査位置が目的のカットを通り過ぎていた場合の保険 (単語の順序が前後したとき /
    # resolve6 §18-2)。先頭から 1 度だけ読み直し、見つかればそこへ走査位置を戻す。
    hit = _cut_index_from(segments, 0, start, end, middle)
    return (hit, hit) if hit is not None else (None, index)


# from_index から前へ走査し、単語が属するカット番号を返す (見つからなければ None)
# 中点が入るカットを優先し、無ければ重なりが最大のカットへ寄せる。
def _cut_index_from(segments, from_index, start, end, middle):
    best = None
    best_overlap = 0.0
    probe = from_index
    while probe < len(segments):
        span_start = float(segments[probe][0])
        span_end = float(segments[probe][1])
        if span_start >= end - _EPS:
            break  # これ以降のカットは単語より後ろ
        if span_start - _EPS <= middle < span_end - _EPS:
            return probe  # 中点が入るカット (通常はここで決まる)
        overlap = min(end, span_end) - max(start, span_start)
        if overlap > best_overlap:
            best_overlap = overlap
            best = probe
        probe += 1
    return best


# 単語の前後で最も近いカットを返す (どの端も許容差を超えるなら None / R6)
# 語頭のこぼれ (次のカットの頭) と語尾のこぼれ (前のカットの尻) の両方を拾い、
# どちらも許容差内なら近い方を選ぶ。
def _nearest_cut_within(word, keep_segments, tolerance_sec):
    if tolerance_sec <= 0:
        return None
    start = float(word.get("start", 0.0))
    end = float(word.get("end", 0.0))
    best = None
    best_gap = None
    for index, (seg_start, seg_end) in enumerate(keep_segments or []):
        span_start = float(seg_start)
        span_end = float(seg_end)
        if span_start >= end - _EPS:
            gap = span_start - end  # 単語はこのカットの手前にある (語頭のこぼれ)
        elif span_end <= start + _EPS:
            gap = start - span_end  # 単語はこのカットの後ろにある (語尾のこぼれ)
        else:
            continue  # 重なっている = ここへは来ない (呼び出し側で解決済み)
        if gap <= tolerance_sec + _EPS and (best_gap is None or gap < best_gap):
            best = index
            best_gap = gap
    return best


# 単語の時刻をカットの中へ収める (R6)
# 寄せた単語がテロップの先頭に来ると開始がカット頭と一致し、写像でも落ちない。
# 尺が 0 になると map_items_to_timeline() が区間を捨てるため最低幅を残す。
# 収め直す必要がなければ元の辞書をそのまま返す (通常はこちら)。
def _clamp_word_to_cut(word, span):
    span_start = float(span[0])
    span_end = float(span[1])
    word_start = float(word.get("start", 0.0))
    word_end = float(word.get("end", 0.0))
    start = min(max(word_start, span_start), span_end)
    end = min(max(word_end, start + _MIN_WORD_SEC), span_end)
    if abs(start - word_start) <= _EPS and abs(end - word_end) <= _EPS:
        return word
    return {**word, "start": start, "end": end}


# 単語を「属するカット番号」と「そのカット内へ収めた時刻」へ解決する (R6)
#
# カット頭 (keep_segments の開始) は silencedetect がレベルを超えた時刻であり、
# 語頭の無声子音や立ち上がり (閾値未満) は無音側へ落ちる。一方 Whisper は VAD の
# speech_pad_ms を足して認識するため、単語はカット頭より前から始まる。この差で
# こぼれた語を落とすとテロップの頭から 1 文字消える (resolve6 §18-1)。
#   ・どれかのカットに重なる単語      → 従来どおり (中点 → 重なり最大) で決める
#   ・重ならず端まで許容差内の単語    → その端のカットへ寄せる (文字を落とさない)
#   ・重ならず許容差より遠い単語      → None (カット済みの無音への幻聴字幕とみて落とす)
#
# 戻り値: (カット番号 or None, 収め直した単語 or None, 次の走査位置)
def _resolve_word_cut(word, keep_segments, cursor=0,
                      attach_tolerance_sec=_DEFAULT_WORD_ATTACH_TOLERANCE_SEC):
    index, cursor = _cut_index_of_word(word, keep_segments, cursor)
    if index is None:
        index = _nearest_cut_within(word, keep_segments, attach_tolerance_sec)
        if index is None:
            return None, None, cursor
    return index, _clamp_word_to_cut(word, keep_segments[index]), cursor


# 単語列から 1 テロップを作る (resolve6 §11-4)
# start/end は単語の実時間。本文は認識時と同じ整形 (フィラー除去＋改行) を掛け直す。
# template は先頭語を出した認識セグメント (付随キーを引き継ぐため)。
# 本文が空になった場合は None を返す (呼び出し側で捨てる)。
def _item_from_words(words, wrap=None, template=None):
    if not words:
        return None
    text = "".join(str(w.get("text", "")) for w in words).strip()
    if not text:
        return None
    item = dict(template or {})
    item.update({
        "start": min(float(w["start"]) for w in words),
        "end": max(float(w["end"]) for w in words),
        "text": wrap(text) if wrap else text,
        "words": list(words),
    })
    return item


# テロップの単位を文字数で作り直す (resolve6 第3部 R1')
#
# 入力・出力はどちらも素材時間。単語列を下限〜上限文字数の範囲でまとめ直す
# (§11-2 の B1〜B5)。カット境界・文末・認識セグメント境界はいずれも
# 「下限に達していれば切ってよい候補」で、下限未満なら跨いで繋ぐ。
# カットを跨ぐテロップが生まれるのは意図どおりで、跨ぎは後段の
# map_items_to_timeline() が詰めた軸の 1 区間へまとめる。
#
# wrap           : 本文の整形関数 (フィラー除去＋改行)。単語から本文を組み直すため必要
# min_chars      : 下限文字数 (これ未満の間は切らない)
# max_chars      : 上限文字数 (これを超えるときは必ず切る)
# max_cut_gap_sec: 取り除かれた無音がこれを超えたら文脈が切れたとみなす (0 で無制限)
# attach_tolerance_sec: カットの外へこぼれた単語を拾う許容差 (0 で拾わない / R6)
#
# 単語が取れない item は他と混ぜられないためそのまま 1 本で通す (安全側 / R4)。
# 戻り値: (items, テロップ件数)
def regroup_items_by_length(items, keep_segments, wrap=None,
                            min_chars=5, max_chars=40,
                            break_on_sentence_end=True,
                            max_cut_gap_sec=0.0,
                            attach_tolerance_sec=_DEFAULT_WORD_ATTACH_TOLERANCE_SEC):
    out = []
    buf = []          # 現在のテロップへ積んだ単語
    buf_len = 0       # 積んだ本文の文字数 (改行記号は数えない)
    buf_item = None   # 先頭語を出した認識セグメント
    last_cut = None   # 直前語が属したカット番号
    last_end = None   # 直前語の終了 (素材時間)
    cursor = 0        # keep_segments の走査位置

    # 積んだ単語を 1 本のテロップとして確定する
    def flush():
        nonlocal buf, buf_len, buf_item
        item = _item_from_words(buf, wrap, buf_item)
        if item is not None:
            out.append(item)
        buf, buf_len, buf_item = [], 0, None

    for item in (items or []):
        words = item.get("words")
        if not words:
            flush()
            out.append(item)
            last_cut = None
            last_end = None
            continue
        boundary = True   # この認識セグメントの最初の語か (B5)
        for raw_word in words:
            # カットへの振り分けと、こぼれた語の寄せ (R6) をここで済ませる
            cut, word, cursor = _resolve_word_cut(
                raw_word, keep_segments, cursor, attach_tolerance_sec)
            if cut is None:
                continue  # 許容差より遠い = カット済みの無音の中の語 (幻聴字幕)
            text = str(word.get("text", "")).strip()
            if not text:
                continue
            if buf:
                # B1: 上限超過 (下限未達でも切る。上限は絶対)
                over = buf_len + len(text) > max_chars
                # B4: 取り除かれた無音が長い (文脈が切れている)
                far = (max_cut_gap_sec > 0 and last_end is not None
                       and float(word["start"]) - last_end > max_cut_gap_sec)
                enough = buf_len >= min_chars
                # B2: 別のカットへ移った (= カット頭から次のテロップが始まる)
                crossed = cut != last_cut
                # B3: 直前語が文末記号で終わる
                ended = break_on_sentence_end and _ends_sentence(buf[-1])
                # B5: 別の認識セグメントへ移った (= 別の文。間に息継ぎがある)
                if over or far or (enough and (crossed or ended or boundary)):
                    flush()
            if not buf:
                buf_item = item
            buf.append(word)
            buf_len += len(text)
            boundary = False
            last_cut = cut
            last_end = float(word["end"])
    flush()
    return out, len(out)


# 詰めた軸の時刻を含むカットの番号を返す (カットの外なら None)
def _cut_index_at(cut_bounds, sec):
    value = float(sec)
    for index, (head, tail) in enumerate(cut_bounds or []):
        if head - _EPS <= value < tail - _EPS:
            return index
    return None


# 詰めた軸の時刻を含むカットの終端を返す (カットの外なら None)
def _cut_tail_at(cut_bounds, sec):
    index = _cut_index_at(cut_bounds, sec)
    return None if index is None else cut_bounds[index][1]


# 各カットの先頭字幕の開始をカット頭へ合わせる (resolve6 R2)
# cut_bounds   : 詰めた軸のカット境界 [(head, tail), ...] (TimeMap.timeline_bounds())
# tolerance_sec: 寄せる許容差。0 以下で無効 (= 何もしない)
#
# 触るのは start だけ。end は後段の adjust_display_timing() が作り直すため、
# ここで尺や重なりの面倒を見る必要がない (§3-2)。
#
# 寄せるのは「そのカットで最初に出るテロップ」1 本だけ (taken 集合)。
# 1 カットに 3 本でも 5 本でもテロップが入るのが通常形で、2 本目以降は
# 認識の発話頭のまま残す (頭へ吸わせると 1 本目と同時に出てしまう / §3-3)。
#
# カットを跨ぐテロップ (第3部 R1') があると「カット頭が直前のテロップに
# 覆われている」状態が起きる。そこへ次のテロップを吸わせると重なり、
# adjust_display_timing() の R3 (次の開始まで表示) が直前のテロップを
# 境界で打ち切ってしまう (resolve6 §10.2 原因H)。よって prev_end を見て、
# 覆われているカット頭へは吸わせない。
#
# 戻り値: (items, 寄せた件数)
def align_items_to_cut_heads(items, cut_bounds, tolerance_sec):
    if tolerance_sec <= 0 or not cut_bounds or not items:
        return items, 0

    aligned = 0
    out = []
    taken = set()
    prev_end = None
    for entry in sorted(items, key=lambda e: e["start"]):
        index = _cut_index_at(cut_bounds, entry["start"])
        if index is None:
            out.append(entry)
            prev_end = max(prev_end or 0.0, float(entry["end"]))
            continue
        head = cut_bounds[index][0]
        gap = float(entry["start"]) - head
        # 直前のテロップがカット頭を覆っているなら動かさない (重なりを作らない)
        covered = prev_end is not None and head < prev_end - _EPS
        # gap < 0 は起きない (split_interval がカット頭へ丸めている)。
        # 許容差より大きい差は動かさない = 長いカットの途中から始まる発話を
        # 頭へ吸わせないための安全弁。
        if index not in taken and not covered and _EPS < gap <= tolerance_sec:
            entry = {**entry, "start": head}
            aligned += 1
        taken.add(index)
        prev_end = max(prev_end or 0.0, float(entry["end"]))
        out.append(entry)
    return out, aligned


# 編集点モード用の音声認識 (docs/request/ver3/resolve.md §7.3 / 20260812 resolve2.md §4)
#
# 認識は「素材そのまま (無音カット前) の音声」に対して行い、得られた素材時間を
# TimeMap で詰めた軸へ写す。残す区間だけを連結した音声を認識すると、継ぎ接ぎの影響で
# 単語タイムスタンプが後方ほど遅れる
# (docs/error/20260812/Analyze.md §3-3-1: 前半 +0.64s → 後半 +1.05s を実測)。
#
# 認識に失敗しても字幕を空にして続行する (パイプラインを止めない / §10)。
# 戻り値: (items, 実効字幕設定)
#   items = [{"start","end","text","use","role","font","font_size"}] (詰めた軸)
def recognize_for_timeline(context, keep_segments):
    settings = context.settings
    subtitle_cfg = settings.get("subtitle", {})
    ffmpeg_cfg = settings.get("ffmpeg", {})

    # 出力プロファイル(縦/横)に応じた実効字幕設定 (request14)
    profile = getattr(context, "output_profile", None)
    eff_cfg = build_effective_subtitle_cfg(
        subtitle_cfg, settings.get("vertical", {}), profile)

    # テロップ ON/OFF
    if not subtitle_cfg.get("enabled", True):
        _logger.info("テロップ生成スキップ (subtitle.enabled=false)")
        return [], eff_cfg

    text_source = resolve_text_source(settings, eff_cfg)
    if text_source is None:
        _logger.info("音声認識エンジン無効のため字幕を作成しません")
        return [], eff_cfg

    # ── プレビューの初回再生ソース用に、残す区間の音声を連結しておく (§6.4-5)
    # 認識には使わない (認識は素材そのままに対して行う / 20260812 resolve2.md §4)。
    # 失敗してもプレビューが都度生成へ切り替わるだけなので、字幕は作り続ける。
    audio_path = context.allocate_intermediate("asr_audio.m4a")
    try:
        silence_cutter.extract_audio_segments(
            context.current_video_path(), keep_segments, audio_path, ffmpeg_cfg,
            on_progress=context.progress_subcallback("プレビュー音声を準備中…"),
        )
    except AutoEditError:
        _logger.exception("プレビュー用音声の抽出に失敗しました (字幕生成は続行します)")
    else:
        setter = getattr(context, "set_asr_audio_path", None)
        if callable(setter):
            setter(audio_path)

    # ── 音声認識 (素材そのまま = 無音カット前に対して行う / 20260812 resolve2.md §4)
    try:
        timeline_items = text_source.extract(
            context.current_video_path(), eff_cfg.get("language", "ja"))
    except Exception:  # noqa: BLE001 (認識失敗で処理全体を止めない / §10)
        _logger.exception("音声認識に失敗したため字幕を作成しません")
        return [], eff_cfg

    if not timeline_items:
        _logger.warning("テロップタイムラインが空のため字幕を作成しません")
        return [], eff_cfg

    # ── テロップの単位を決める (ver8 resolve6 第3部 R1' / 第2部 R1)
    # 写像より前 = 素材時間のうちに行う。でないとカット境界と単語を突き合わせられない。
    unit = subtitle_unit(eff_cfg)
    if unit != _SUBTITLE_UNIT_SEGMENT:
        min_len = int(eff_cfg.get("min_line_length", 15))
        max_len = int(eff_cfg.get("max_line_length", 20))
        engine = str(eff_cfg.get("wrap_engine", "budoux")).strip().lower()
        remove_fillers = bool(eff_cfg.get("remove_fillers", False))
        fillers = eff_cfg.get("fillers", _DEFAULT_FILLERS)

        # 単語から本文を組み直すため、認識時と同じ整形 (フィラー除去＋改行) を掛ける
        def _refine(text):
            if remove_fillers:
                text = strip_fillers(text, fillers)
            return wrap_lines(text, min_len, max_len, engine)

        if unit == _SUBTITLE_UNIT_LENGTH:
            # 文字数で単位を作り直す (第3部 R1')。上限 = 1 行の上限 × 最大行数。
            max_chars = max(max_len * int(eff_cfg.get("subtitle_max_lines", 2)), 1)
            min_chars = max(min(min_len, max_chars), 0)
            timeline_items, count = regroup_items_by_length(
                timeline_items, keep_segments, wrap=_refine,
                min_chars=min_chars, max_chars=max_chars,
                break_on_sentence_end=bool(eff_cfg.get("break_on_sentence_end", True)),
                max_cut_gap_sec=float(eff_cfg.get("merge_max_cut_gap_sec", 0.0)),
                attach_tolerance_sec=float(eff_cfg.get(
                    "word_attach_tolerance_sec",
                    _DEFAULT_WORD_ATTACH_TOLERANCE_SEC)))
            _logger.info("テロップ単位を文字数で再構成: %d 件 (下限 %d / 上限 %d 文字)",
                         count, min_chars, max_chars)
        else:
            # カットごとに必ず 1 本 (第2部 R1 / 旧挙動への退路)
            timeline_items, added = split_items_by_keep_segments(
                timeline_items, keep_segments, wrap=_refine,
                min_piece_sec=float(eff_cfg.get("split_min_piece_sec", 0.3)))
            _logger.info("カット跨ぎの字幕を分割: %d 件増 (計 %d 件)",
                         added, len(timeline_items))

    # ── 素材時間 → 詰めた軸へ写す (20260812 resolve2.md §4)
    before_map = len(timeline_items)
    timeline_items, dropped = map_items_to_timeline(timeline_items, keep_segments)
    _logger.info(
        "字幕時刻を Timeline の軸へ写像: %d → %d 件 (カット区間のため %d 件除外)",
        before_map, len(timeline_items), dropped,
    )
    if not timeline_items:
        _logger.warning("写像後の字幕が 0 件のため字幕を作成しません")
        return [], eff_cfg

    # ── 開始をカット頭へ揃える (ver8 resolve6 R2)
    # 整形より前に行う。整形は次の字幕の開始から終了を決めるため、先に開始を
    # 確定させないと伸ばし先が旧位置になる (§3-2)。
    cut_bounds = TimeMap.from_segments(keep_segments).timeline_bounds()
    if eff_cfg.get("align_to_cut_head", True):
        timeline_items, aligned = align_items_to_cut_heads(
            timeline_items, cut_bounds,
            float(eff_cfg.get("align_tolerance_sec", 0.6)))
        _logger.info("字幕の開始をカット頭へ整列: %d 件", aligned)

    # ── 表示タイミング整形 (詰めた軸の上で行う。整形の意味を変えないため写像の後)
    # カット境界を渡し、表示をカットの終端で止める (R3)。
    max_hold_sec = float(eff_cfg.get("display_max_hold_sec", 2.0))
    min_duration_sec = float(eff_cfg.get("display_min_duration_sec", 0.5))
    before_count = len(timeline_items)
    timeline_items = adjust_display_timing(
        timeline_items, max_hold_sec, min_duration_sec,
        cut_bounds=None if eff_cfg.get("hold_across_cut", False) else cut_bounds)
    _logger.info(
        "テロップ表示タイミング整形: %d → %d 区間 (max_hold=%.1fs, min_dur=%.1fs)",
        before_count, len(timeline_items), max_hold_sec, min_duration_sec,
    )

    # 編集画面が扱う item 形式へ揃える (初期値は全件使用・役割は配信者)
    items = [
        {"start": e["start"], "end": e["end"], "text": e["text"],
         "use": True, "role": _DEFAULT_ROLE, "font": "", "font_size": None}
        for e in timeline_items
    ]
    _logger.info("音声認識完了 (Timeline 用 %d 件)", len(items))
    return items, eff_cfg
