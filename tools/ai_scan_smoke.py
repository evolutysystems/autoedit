# 採点スイープの役 sceneScan が通るかを 1 回だけ確かめる
# (docs/AiEditor/resolve2.md §16.1)
#
# サーバー (StretheusAPI) へ役を足したあと、クライアントが
#   ・sceneEval へのフォールバックを外し
#   ・sceneScan として送り
#   ・応答を正規化できる
# ことを**最小の 1 回**で確かめる。実映像は使わず、小さな生成画像 1 枚で送る。
#
#   python tools/ai_scan_smoke.py
#
# 事前に `python -m src.services.login_check` でログインしておくこと。
# 配布物には含めない (tools/ 配下)。
import base64
import io
import json
import os
import struct
import sys
import zlib

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.ai import config as ai_config                      # noqa: E402
from src.ai import plan_schema, prompts, remote             # noqa: E402
from src.ai import scene_scanner                            # noqa: E402
from src.ai.provider import AiImage, AiRequest, StretheusProvider  # noqa: E402
from src.ai.usage_log import UsageTracker                   # noqa: E402
from src.exceptions import AiError                          # noqa: E402
from src.services.config import api_config                  # noqa: E402
from src.services.stretheus_auth import StretheusAuth       # noqa: E402
from src.settings.settings_window import load_settings      # noqa: E402


# 真っ黒な 16x16 の PNG を作る (実映像を送らずに「画像つき」を試すため)
def _tiny_png():
    width = height = 16
    raw = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))

    def chunk(tag, data):
        body = tag + data

        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))

    return base64.b64encode(png).decode("ascii")


def main():
    settings = load_settings()
    if not ai_config.is_enabled(settings):
        print("ai.enabled が false です")

        return 1

    cfg = api_config(settings)
    auth = StretheusAuth(cfg["base_url"], cfg["timeout_sec"])
    if not auth.is_logged_in():
        print("ログインしていません。python -m src.services.login_check を先に実行してください")

        return 1
    provider = StretheusProvider(auth, settings=settings, tracker=UsageTracker(settings))

    config = provider.server_config()
    wire = remote.wire_role(ai_config.ROLE_SCENE_SCAN)
    print("接続先      :", cfg["base_url"])
    print("サーバーの役:", sorted(config.roles))
    if wire not in config.roles:
        print("→ {0} はまだサーバーに無い。デプロイが済んでいない".format(wire))

        return 1
    print("→ {0} あり: {1}".format(wire, config.roles[wire].__dict__
                                   if hasattr(config.roles[wire], "__dict__")
                                   else config.roles[wire]))

    role, model_key = scene_scanner._roles_for(provider)
    print("クライアントが選ぶ役:", role, "(モデルの鍵:", model_key, ")")
    if role != ai_config.ROLE_SCENE_SCAN:
        print("→ フォールバックが外れていない")

        return 1

    scan_cfg = ai_config.ai_config(settings)["scan"]
    request = AiRequest(
        role=role, model_key=model_key,
        system=scene_scanner._system_prompt(settings, scan_cfg),
        payload={
            "range": {"start": 0.0, "end": 60.0},
            "frames": [0.0],
            "levels": [{"t": 0.0, "mean_db": -30.0, "max_db": -20.0,
                        "silence": 0.9, "comments": 0, "laughs": 0}],
            "comments": [],
        },
        schema=plan_schema.scene_scan_schema(
            max_title_chars=int(scan_cfg.get("max_title_chars") or 30)),
        images=[AiImage(media_type="image/png", data_base64=_tiny_png())],
        cache_prefix_id="scene_scan-v1",
        max_output_tokens=2048)

    print("\n送信中… (真っ黒な画像 1 枚 / 無音の数値だけ = 山は出ない想定)")
    try:
        completion = provider.complete_json(request)
    except AiError as e:
        print("失敗:", getattr(e, "code", ""), e)

        return 1

    print("モデル :", completion.model)
    print("応答   :", json.dumps(completion.result, ensure_ascii=False)[:300])
    moments, notes = plan_schema.normalize_scan(completion.result, 0.0, 60.0)
    print("正規化 : 山 {0} 件 / 記録 {1} 件".format(len(moments), len(notes)))
    print("\n疎通 OK。sceneScan 役で通りました。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
