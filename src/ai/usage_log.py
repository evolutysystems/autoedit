# AI の利用量の記録と 1 プロジェクト分の上限 (docs/AiEditor/resolve1.md §17.2 / §17.3)
#
# ポイントとトークンの換算は実測で決めるため (回答 Q2 / §22 T1)、次を必ず残す。
#   役割 / 解決されたモデル / effort / usage の 4 項目 / 画像の枚数 /
#   ポイント (charged と estimated) / 工程別の合計
#
# **残さないもの: API キー・プロンプト全文・応答全文・コメント本文。**
#
# 上限 (ai.cost.*) はクライアント側の自衛である。サーバーも日次・プロジェクト単位で
# 独立に強制しており (申告は信用されない)、ここで止めるのは
# 「無駄な往復を減らす」ためである。
import hashlib
import json

from ..exceptions import AiProviderError
from ..i18n import tr
from ..utils.logger import get_logger
from . import config as ai_config

_logger = get_logger(__name__)

# 上限で打ち切ったことを表すクライアント側のコード。
# サーバーの ai_quota_exceeded とは出どころが違うため別の値にする。
CODE_BUDGET_EXCEEDED = "ai_budget_exceeded"

# キャッシュが効いていないと判断するまでの連続回数 (§6.4)。
# 1 回目は前置きを作った回 (cache_creation) なので 0 でも正常である。
_CACHE_MISS_WARN_STREAK = 2


# 1 プロジェクト (VOD 1 本) 分の記録。
class UsageTracker:

    def __init__(self, settings=None, project_id=None, telemetry=None):
        config = ai_config.ai_config(settings)
        self._max_calls, self._max_input_tokens = ai_config.cost_limits(config)
        self._project_id = project_id
        self._telemetry = telemetry

        self.calls = 0
        self.failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self.points_charged = 0
        self.points_estimated = 0
        self.images = 0

        # 役割ごとの内訳 (工程別の費用を出すため / §17.3)
        self.by_role = {}

        # 同じ前置きでキャッシュが効かなかった連続回数
        self._cache_miss_streak = {}

    # 呼び出す前に上限を確かめる。超えていれば打ち切る。
    def guard(self, role=None):
        if self._max_calls and self.calls >= self._max_calls:
            self._reject("calls", role)

        if self._max_input_tokens and self.prompt_tokens >= self._max_input_tokens:
            self._reject("input_tokens", role)

    # 1 回の結果を記録する。
    def record(self, request, completion):
        usage = completion.usage

        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens
        self.points_charged += completion.points_charged
        self.points_estimated += completion.points_estimated
        self.images += len(request.images or [])

        role = str(request.role)
        entry = self.by_role.setdefault(role, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "points_charged": 0, "points_estimated": 0, "models": [],
        })
        entry["calls"] += 1
        entry["input_tokens"] += usage.input_tokens
        entry["output_tokens"] += usage.output_tokens
        entry["cache_read_input_tokens"] += usage.cache_read_input_tokens
        entry["cache_creation_input_tokens"] += usage.cache_creation_input_tokens
        entry["points_charged"] += completion.points_charged
        entry["points_estimated"] += completion.points_estimated
        if completion.model and completion.model not in entry["models"]:
            # 希望どおりに通ったかを後から確かめられるよう、解決後のモデルを残す。
            entry["models"].append(completion.model)

        self._check_cache(request, completion)

        return entry

    # 失敗も回数として数える (上限の判断に効く)。
    def record_failure(self, role=None):
        self.calls += 1
        self.failures += 1
        if role:
            entry = self.by_role.setdefault(str(role), {
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                "points_charged": 0, "points_estimated": 0, "models": [],
            })
            entry["calls"] += 1

    @property
    def prompt_tokens(self):
        return (self.input_tokens
                + self.cache_creation_input_tokens
                + self.cache_read_input_tokens)

    # 1 プロジェクト分の集計。
    def summary(self):
        return {
            "project_id": self._project_id,
            "calls": self.calls,
            "failures": self.failures,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "prompt_tokens": self.prompt_tokens,
            "points_charged": self.points_charged,
            "points_estimated": self.points_estimated,
            "images": self.images,
            "by_role": self.by_role,
        }

    # 1 行にまとめてログへ残す (工程別の内訳つき)。
    def log_summary(self):
        _logger.info(
            "AI 合計: calls=%d failures=%d input=%d output=%d cache_read=%d "
            "cache_create=%d points=%d/%d images=%d roles=%s",
            self.calls, self.failures, self.input_tokens, self.output_tokens,
            self.cache_read_input_tokens, self.cache_creation_input_tokens,
            self.points_charged, self.points_estimated, self.images,
            ",".join(sorted(self.by_role)))

        # **工程ごとの内訳も残す。** ポイント単価とモデル割り当ては
        # 「どの工程がどれだけ使ったか」が分からないと決められない (§22 T1 / 回答 Q2)。
        # 1 行 1 工程にしておけば、ログから表へ写すだけで済む。
        for role in sorted(self.by_role):
            entry = self.by_role[role]
            _logger.info(
                "AI 内訳: role=%s calls=%d input=%d output=%d cache_read=%d "
                "cache_create=%d points=%d/%d models=%s",
                role, entry["calls"], entry["input_tokens"],
                entry["output_tokens"], entry["cache_read_input_tokens"],
                entry["cache_creation_input_tokens"], entry["points_charged"],
                entry["points_estimated"], ",".join(entry["models"]))

        # テレメトリーは既存の口を使い、所要と件数だけを送る (本文は送らない / §17.4)。
        if self._telemetry is not None:
            try:
                self._telemetry.event("ai_project_usage", None, self._project_id)
            except Exception as e:  # noqa: BLE001 - 計測で本処理を止めない
                _logger.debug("AI のテレメトリー送信に失敗しました: %s", e)

    # キャッシュが効いているかを見る (§6.4)。
    #
    # 数えるのは **前置きの実体が同じ呼び出し** だけである。cache_prefix_id は
    # 「同じ前置きを使い回す意思表示」であって、中身が同じことの保証ではない。
    # 前置き (役割の固定文 + 指示 + スキーマ) が違えば 1 回目は必ず外れるため、
    # id だけで数えると「出力先ごとに 1 回ずつ呼ぶ」正常な実行でも警告が出る
    # (2026-11-24: plan 役割で誤検知していた)。
    #
    # 実体が同じなのに続けて外れるときだけ警告する。これは本当に直すべき状態
    # (前置きに時刻や乱数が混ざった / TTL より呼び出し間隔が長い) を指す。
    def _check_cache(self, request, completion):
        key = request.cache_prefix_id
        if not key:
            return

        key = (key, _prefix_digest(request))

        if completion.usage.cache_read_input_tokens > 0:
            self._cache_miss_streak.pop(key, None)

            return

        streak = self._cache_miss_streak.get(key, 0) + 1
        self._cache_miss_streak[key] = streak

        if streak >= _CACHE_MISS_WARN_STREAK:
            _logger.warning(
                "AI のプロンプトキャッシュが効いていません "
                "(cache_prefix_id=%s 前置き=%s 連続=%d role=%s)。"
                "同じ前置きで続けて外れています。",
                key[0], key[1], streak, request.role)

    def _reject(self, reason, role):
        _logger.warning(
            "AI の利用上限に達したため打ち切ります (%s / calls=%d prompt_tokens=%d role=%s)。",
            reason, self.calls, self.prompt_tokens, role)

        raise AiProviderError(
            tr("この動画での AI の利用上限に達しました。"),
            code=CODE_BUDGET_EXCEEDED)


# 前置きの実体を表す短い指紋。キャッシュの前方一致に入るもの
# (役割・指示・スキーマ) だけから作る。payload は breakpoint の後ろにあり
# キャッシュを壊さないため入れない (実測で確認 / 2026-11-24)。
def _prefix_digest(request):
    schema = request.schema
    material = "\n".join((
        str(request.role),
        str(request.system or ""),
        json.dumps(schema, sort_keys=True, ensure_ascii=False,
                   default=str) if schema is not None else "",
        str(len(request.images or [])),
    ))

    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:8]
