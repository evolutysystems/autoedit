# アーカイブ採点の設定読み出し (ハードコード回避のため setting.json の archive セクションを参照)
# 欠落キーは DEFAULT_SETTINGS 側で補完される前提だが、単体でも安全に既定へフォールバックする。


# 方式A の採点設定を1つの辞書に平坦化して返す (scoring.score_window / select_top_events 用)
def method_a_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    scoring = archive.get("scoring", {})
    method_a = scoring.get("method_a", {})
    return {
        "window_sec": int(scoring.get("window_sec", 300)),
        "slide_sec": int(scoring.get("slide_sec", 60)),
        "top_n": int(scoring.get("top_n", 5)),
        "clip_pad_sec": float(scoring.get("clip_pad_sec", 0)),
        "loud_percentile": float(scoring.get("loud_percentile", 0.8)),
        "silence_ratio_threshold": float(scoring.get("silence_ratio_threshold", 0.6)),
        "weights": method_a.get("weights", {"emotion": 0.55, "comment": 0.45}),
        "emotion_points": method_a.get("emotion_points", {"loud": 10, "long_silence": -8}),
        "comment": method_a.get("comment", {"w_point_per_char": 1, "rate_spike_bonus": 10}),
        "comment_spike": False,  # R1(local)はコメント無しのため急増ボーナス無効
    }


# 切り抜き出力ファイル名の接頭辞を返す (既定 "archive")
def clip_prefix(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    return str(archive.get("output", {}).get("clip_prefix", "archive")) or "archive"
