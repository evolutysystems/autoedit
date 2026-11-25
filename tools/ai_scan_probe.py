# 採点スイープ (S1) を 1 本の VOD に対して実際に回す開発用スクリプト
# (docs/AiEditor/resolve2.md §14 P2)
#
# P2 の目的は「**採点へ混ぜる前に**、AI が挙げた山が妥当かを人が見る」ことである。
# そのため本スクリプトは Timeline も採点結果も一切変えない。山を並べて見せるだけ。
#
#   python tools/ai_scan_probe.py <vod.mp4>
#   python tools/ai_scan_probe.py <vod.mp4> --chunks 2        先頭 2 区間だけ試す
#   python tools/ai_scan_probe.py <vod.mp4> --use shorts      重みの組を変える
#   python tools/ai_scan_probe.py <vod.mp4> --json out.json   結果を書き出す
#
# 事前に `python -m src.services.login_check` でログインしておくこと。
# 配布物には含めない (tools/ 配下)。
import argparse
import json
import os
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.ai import config as ai_config                      # noqa: E402
from src.ai import scene_scanner                            # noqa: E402
from src.ai.provider import StretheusProvider               # noqa: E402
from src.ai.usage_log import UsageTracker                   # noqa: E402
from src.archive import comment_source, features            # noqa: E402
from src.services.config import api_config                  # noqa: E402
from src.services.stretheus_auth import StretheusAuth       # noqa: E402
from src.settings.settings_window import load_settings      # noqa: E402


def _provider(settings):
    cfg = api_config(settings)
    auth = StretheusAuth(cfg["base_url"], cfg["timeout_sec"])
    if not auth.is_logged_in():
        print("ログインしていません。先に python -m src.services.login_check を実行してください")

        return None
    user = auth.user() or {}
    print("ユーザー: {0}".format(user.get("displayName")))

    return StretheusProvider(auth, settings=settings, tracker=UsageTracker(settings))


# 機械測定 (セル) を用意する。スイープは音量の数値も見るため (§4.2)。
def _cells(input_path, settings):
    from src.archive import config as archive_config

    cfg = archive_config.method_a_config(settings)
    cell_sec = max(1, cfg["slide_sec"])
    duration, cells, _scan = features.extract_cells(input_path, settings, cell_sec)
    print("機械測定: 全長 {0:.0f}s / セル {1} 件".format(duration, len(cells)))

    return duration, cells


def _comments(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        comments = comment_source.normalize_comments(json.load(handle))
    print("コメント: {0} 件".format(len(comments)))

    return comments


def main():
    parser = argparse.ArgumentParser(description="採点スイープ (S1) を実際に回す")
    parser.add_argument("vod", help="元 VOD の mp4")
    parser.add_argument("--chat", default="", help="chat json (省略可)")
    parser.add_argument("--use", default="long", choices=("long", "shorts"))
    parser.add_argument("--chunks", type=int, default=0,
                        help="何区間だけ試すか (0 = 全部)")
    parser.add_argument("--start", type=float, default=0.0,
                        help="この秒から試す (既定 0)")
    parser.add_argument("--json", default="", help="結果の書き出し先")
    args = parser.parse_args()

    settings = load_settings()
    if not ai_config.is_enabled(settings):
        print("ai.enabled が false です")

        return 1
    provider = _provider(settings)
    if provider is None:
        return 1

    duration, cells = _cells(args.vod, settings)
    comments = _comments(args.chat)
    if args.chunks > 0:
        # 数区間だけ試す = 費用を抑えて手応えを見る
        span = float(ai_config.ai_config(settings)["scan"]["chunk_sec"]) * args.chunks
        duration = min(duration, args.start + span)
        print("{0:.0f}s から {1:.0f}s までを試します".format(args.start, duration))

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ai_scan_") as workdir:
        moments = scene_scanner.scan(
            args.vod, settings, provider, cells=cells, comments=comments,
            duration_sec=duration, use=args.use, workdir=workdir,
            start_sec=args.start,
            progress_cb=lambda ratio, done, total: print(
                "  区間 {0}/{1}…".format(done, total)))
    took = time.monotonic() - started

    print("\n=== 山 {0} 件 / {1:.0f} 秒 ===".format(len(moments), took))
    for moment in sorted(moments, key=lambda m: m.scan_score, reverse=True):
        print("{0:7.1f}-{1:7.1f} ({2:5.1f}s) 点={3:5.1f} 山={4:7.1f}  {5}".format(
            moment.start, moment.end, moment.end - moment.start,
            moment.scan_score, moment.peak, moment.title_line))
        print("        {0}".format(moment.why))
        print("        " + " ".join(
            "{0}={1:.0f}".format(key, value) for key, value in moment.scores.items()))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump([m.as_dict() for m in moments], handle,
                      ensure_ascii=False, indent=2)
        print("\n書き出しました: {0}".format(args.json))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
