# フルテロップ生成モジュール
# 設計書 ② フルテロップ生成 に対応
# 処理順: 音声認識 (faster-whisper) → ASS 字幕ファイル生成 → ASS 焼き込み
# (設計書 10. 不明点 #2 は claude.md ② の指示により解決済。エンジンは参照元
#  ../StreamPipeline/dev と同一の faster-whisper を既定採用)
import os

from ..exceptions import FFmpegError, PipelineCancelled, SubtitleError
from ..utils.logger import get_logger
from . import ffmpeg_runner, silence_cutter

_logger = get_logger(__name__)


# ASS Style のうち要望対象外のため固定維持する値 (マジックナンバー回避)
_ASS_SCALE_X = 100
_ASS_SCALE_Y = 100
_ASS_SHADOW = 1
_ASS_ENCODING = 1

# 不正値フォールバック用の既定 ASS 色
_DEFAULT_OUTLINE_COLOR = "&H00000000"
_DEFAULT_BACK_COLOR = "&H64000000"


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
                 role_outline_colors=None, comment_label="コメント："):
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

    # 後方互換: 配信者色の ASS PrimaryColour (&H00BBGGRR) を返す
    def to_ass_color(self):
        return _hex_to_ass_color(self.color_hex)

    # 役割キーに対応する ASS Style 名を返す (未知の役割は配信者へフォールバック)
    def style_for_role(self, role):
        return self.ROLE_STYLE.get(role, self.ROLE_STYLE[_DEFAULT_ROLE])

    # 指定 Style 名・塗り色 (HTML #RRGGBB)・アウトライン色 (ASS &HAABBGGRR) の
    # ASS Style 値 (Style: 以降) を生成する。塗り=PrimaryColour / アウトライン=OutlineColour。
    def to_ass_style_named(self, style_name, color_hex, outline_color):
        return (
            f"{style_name},{self.family},{self.size},"
            f"{_hex_to_ass_color(color_hex)},{outline_color},{self.back_color},"
            f"{_ass_flag(self.bold)},{_ass_flag(self.italic)},"
            f"{_ass_flag(self.underline)},{_ass_flag(self.strikeout)},"
            f"{_ASS_SCALE_X},{_ASS_SCALE_Y},{self.spacing},{self.angle},"
            f"{self.border_style},{self.outline_width},{_ASS_SHADOW},"
            f"{self.alignment},{self.margin_l},{self.margin_r},{self.margin_v},"
            f"{_ASS_ENCODING}"
        )

    # 全役割の Style 値を (Style名, Style値) のリストで返す
    # 配信者→サブ→コメント の順。各役割の塗り色＋アウトライン色を差し替える (追加2)。
    def iter_role_styles(self):
        return [
            (self.ROLE_STYLE[role],
             self.to_ass_style_named(self.ROLE_STYLE[role],
                                     self.role_colors[role],
                                     self.role_outline_colors[role]))
            for role in ("streamer", "sub", "comment")
        ]


# フィラー(口癖)除去の既定リスト (設定 subtitle.fillers で上書き可能)
_DEFAULT_FILLERS = ["えー", "えっと", "あのー", "そのー", "あー", "うー"]


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
                "faster-whisper が未導入のため音声認識を実行できません "
                "(pip install faster-whisper)。subtitle.engine を 'none' にするとスキップできます。"
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
            timeline.append({"start": start, "end": end, "text": text})

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
            kwargs["vad_parameters"] = {"min_silence_duration_ms": self._vad_min_silence_ms}
            _logger.info(
                "VAD フィルタ有効 (min_silence=%dms, condition_on_previous_text=%s)",
                self._vad_min_silence_ms, self._condition_on_previous_text,
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

    # フィラー(口癖)を除去する
    def _strip_fillers(self, text):
        for filler in self._fillers:
            text = text.replace(filler, "")
        return text.strip()

    # 改行 (ASS の \\N) を挿入する (共通ヘルパへ委譲)
    # wrap_engine="budoux" で文節改行、それ以外/未導入時は文字数改行へフォールバック。
    def _split_lines(self, text):
        return wrap_lines(text, self._min_line_length, self._max_line_length, self._wrap_engine)


# subtitle.engine 設定に応じた TextSource 実装を返す
# "none"/未対応/未導入 の場合は None を返し、呼び出し側でテロップ工程をスキップする
def resolve_text_source(settings):
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


# タイムラインから ASS 字幕ファイルを生成する
# テロップ役割 (配信者/サブ/コメント) ごとに色違いの Style を定義し、
# 各 Dialogue 行は entry["role"] に対応する Style を参照する (request10)。
# role 欠落 entry は配信者(streamer) 扱い=旧来の単一色と同一挙動 (後方互換)。
def build_subtitle_file(timeline, font_profile, output_path, video_width=1920, video_height=1080):
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

    body_lines = []
    for entry in timeline:
        start = _format_ass_time(entry["start"])
        end = _format_ass_time(entry["end"])
        text = entry["text"].replace("\n", "\\N")
        role = entry.get("role", _DEFAULT_ROLE)
        # コメント役割のみ先頭に「コメント：」＋改行(\N)を付与する (request10 追加要望5)。
        # 本文は既に \N 折返し済みのため、ラベルは独立行として折返しに巻き込まれない。
        if role == "comment" and font_profile.comment_label:
            text = f"{font_profile.comment_label}\\N{text}"
        # 役割に対応する Style 名で色分けする (role 未指定は配信者)
        style_name = font_profile.style_for_role(role)
        body_lines.append(f"Dialogue: 0,{start},{end},{style_name},,0,0,0,,{text}")

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
def burn_subtitle(input_path, subtitle_path, output_path, ffmpeg_settings,
                  total_duration=0.0, on_progress=None):
    # subtitles フィルタは2段階エスケープに従う (resolve 20260630 resolve2.md 対策A):
    #   ・シングルクォートでフィルタグラフ階層(空白/カンマ)を保護し、内側の '\' を下位へ通す
    #   ・フィルタ内オプション階層では ':' がなお区切り文字のため '\:' でエスケープする
    # 両者は別階層に作用するため併用が正しい (クォートだけでは ':' 分割を防げず
    # ドライブレターが original_size オプションへ流れて EINVAL になる)。
    safe_path = subtitle_path.replace("\\", "/").replace(":", "\\:")
    # subtitles で字幕を焼いた後、setpts で映像PTSをフレーム番号から単調再構築する (破損TS対策)
    filter_spec = f"subtitles='{safe_path}',setpts=N/FRAME_RATE/TB"
    fps = ffmpeg_runner.get_output_fps(ffmpeg_settings)

    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-fflags", "+genpts",          # 欠落PTSを再生成して読み込む
        "-i", input_path,
        "-vf", filter_spec,
        "-af", "aresample=async=1:first_pts=0",  # 映像PTS再構築に音声を追従させ同期維持
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
def adjust_display_timing(timeline, max_hold_sec=2.0, min_duration_sec=0.0):
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

        # 不正区間 (終了<=開始) は破棄する
        if disp_end <= disp_start:
            continue
        adjusted.append({"start": disp_start, "end": disp_end, "text": entry["text"]})
    return adjusted


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

    edited = review_cb(items)  # 「字幕決定」まで待機し編集結果を返す

    # キャンセル (None) 時はパイプライン全体を中断する (§10-2)
    if edited is None:
        raise PipelineCancelled("字幕編集がキャンセルされたためパイプラインを中断します")

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
         "role": e.get("role", _DEFAULT_ROLE)}
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

    # テキストソース解決 (subtitle.engine に応じた音声認識エンジン)
    text_source = resolve_text_source(settings)
    if text_source is None:
        # engine="none"/未対応/未導入 のため安全にスキップ
        _logger.info("音声認識エンジン無効のためテロップ生成をスキップ")
        return context.current_video_path()

    font_profile = FontProfile(
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
        comment_label=subtitle_cfg.get("comment_label", "コメント："),
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

    input_path = context.current_video_path()
    output_path = context.allocate_intermediate("subtitle.mp4")
    subtitle_path = context.allocate_intermediate("subtitle.ass")

    try:
        timeline = text_source.extract(input_path, subtitle_cfg.get("language", "ja"))
    except Exception as e:
        raise SubtitleError(f"テキストソース抽出に失敗: {e}") from e

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

    build_subtitle_file(timeline, font_profile, subtitle_path)
    burn_subtitle(
        input_path, subtitle_path, output_path, ffmpeg_cfg,
        total_duration=total_duration,
        on_progress=context.progress_subcallback("フルテロップ生成"),
    )
    context.set_current_video_path(output_path)
    return output_path
