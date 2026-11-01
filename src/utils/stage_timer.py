# 工程ごとの所要時間の計測 (docs/quality/resolve3.md §7.1)
#
# resolve2.md の調査は本体を計装して測った。**次からは計装なしで比べられるようにする**ため、
# 書き出しの各工程の所要を 1 本のログ行へまとめて出す。
#
# 設計の要点:
#   ・ライブラリを増やさない (time.monotonic と標準の contextmanager だけを使う)
#   ・**計測の失敗で書き出しを壊さない。** 記録は書き込み専用で、誰も戻り値を見ない。
#   ・工程をまたいで足し上がる値 (文字起こし・ベース映像構築・字幕焼き込みは
#     クリップごとに何度も走る) があるため、名前ごとに加算する。
#   ・1 回の書き出しの単位で reset() する。並行して 2 本書き出す経路は無い。
import time
from collections import OrderedDict
from contextlib import contextmanager

from .logger import get_logger

_logger = get_logger(__name__)

# 工程名 → 累計秒。記録順を保つため OrderedDict を使う。
_stages = OrderedDict()


# 計測を初期化する (1 回の書き出しの開始時に呼ぶ)
def reset():
    _stages.clear()


# 工程の所要を加算する
def add(name, seconds):
    try:
        _stages[name] = _stages.get(name, 0.0) + max(float(seconds), 0.0)
    except (TypeError, ValueError):
        pass


# with 構文で囲んだ区間の所要を加算する
#
# 例外が出ても (= 失敗した工程でも) そこまでの所要を残す。
# 失敗の原因を追うとき「どこで何秒かかって落ちたか」が分かるようにするためである。
@contextmanager
def timed(name):
    started = time.monotonic()
    try:
        yield
    finally:
        add(name, time.monotonic() - started)


# 記録した工程の累計を返す ({工程名: 秒})
def snapshot():
    return dict(_stages)


# 工程の所要を 1 行のログへ出す (§7.1)
#
# 出力例:
#   書き出し工程の所要: 解析 42.1s / 準備 988.3s (文字起こし 949.0s) /
#   レンダリング 490.2s (ベース 88.1s / 焼き込み 402.1s) / 結合 11.8s / 合計 1532.4s
#
# total_sec を渡すと「合計」にその値を使う (工程の外側も含めた実時間)。
# 省略時は記録した主要工程の和を使う。
def report(label="書き出し", total_sec=None):
    if not _stages:
        return
    main = [
        ("解析", ()),
        ("準備", ("文字起こし",)),
        ("レンダリング", ("ベース", "焼き込み")),
        ("個別出力", ()),
        ("結合", ()),
    ]
    parts = []
    main_total = 0.0
    for name, children in main:
        if name not in _stages:
            continue
        main_total += _stages[name]
        inner = [f"{child} {_stages[child]:.1f}s"
                 for child in children if child in _stages]
        text = f"{name} {_stages[name]:.1f}s"
        if inner:
            text += " (" + " / ".join(inner) + ")"
        parts.append(text)

    # 主要工程に当てはまらない記録も落とさず末尾へ並べる
    known = {name for name, _children in main}
    known.update(child for _name, children in main for child in children)
    for name, seconds in _stages.items():
        if name not in known:
            parts.append(f"{name} {seconds:.1f}s")

    total = float(total_sec) if total_sec is not None else main_total
    _logger.info("%s工程の所要: %s / 合計 %.1fs", label, " / ".join(parts), total)
