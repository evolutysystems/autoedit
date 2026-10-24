# 話者照合によるテロップの絞り込み (F15 / docs/quality/resolve1.md §12.5)
#
# なぜ要るか:
#   ゲーム内のボイス (無線通信・カットシーンの台詞) は**音響的には人の声そのもの**で、
#   VAD でも音源分離でも配信者の声と分かれない (§12.3-1)。
#   実測では 111 件のテロップのうち 24 件が明らかにゲーム内音声だった。
#
# 何を使うか:
#   配信者は VOD 全体で**一貫した 1 人の声**、ゲームは**多数の別人の声**。
#   この非対称性を使い、
#     ① 発話ごとに声紋 (embedding) を取る
#     ② 最も発話時間が長いまとまり = 配信者 とみなして代表の声を求める
#     ③ 代表から遠い発話を落とす (または印を付ける)
#   という順で絞り込む。
#
# 声紋の取り方:
#   同梱の torchaudio で MFCC の統計量 (平均・標準偏差) を声紋とする。モデル不要。
#
# 【この機能の限界 / docs/quality/resolve1.md §12.6・§12.7】
#   実測の結果、**VOD のミックス 1 本 (ゲーム音が常時乗る音声) では成立しない**。
#   学習済みの話者照合モデル (WeSpeaker ECAPA-TDNN / ResNet34) まで試したが、
#   同じ発話を前半と後半に割った声紋の一致 (自己一致) が 0.13〜0.40 しか出ず
#   (正常なら 0.85〜0.95)、1 本のテロップの中でも前半と後半が別人に見えた。
#   さらに「配信者が多数派」という前提も崩れ、ゲーム側が代表になった。
#   そのため ONNX モデルを読む経路は外し、既定も "off" のままにしてある。
#   マイク単独トラックのような綺麗な音声へ移れば使える見込みはある。
#
# 安全側の方針:
#   ・既定は "flag" (印を付けるだけ)。自動では捨てない。
#     実測のとおり、音量だけの判定ではゲームの 92% を除ける一方で
#     配信者の声も 36% 失う (§12.3-3)。話者照合も誤るときは誤るため、
#     捨てる判断は利用者へ残せるようにしておく。
#   ・どこで失敗しても字幕は作り続ける (モデル無し・音声が取れない・torch 無しでも素通り)。
import math
import os
import subprocess
import wave

from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from . import ffmpeg_runner

_logger = get_logger(__name__)

# 声紋を取る音声の形式 (話者照合は 16kHz モノラルが標準)
_SAMPLE_RATE = 16000

# 代表の声を求める繰り返し回数 (刈り込み平均)
_CENTER_ITERATIONS = 4


# 設定 (subtitle セクション) を平坦化して返す
def speaker_config(subtitle_cfg):
    cfg = subtitle_cfg or {}
    return {
        # "off" = 何もしない / "flag" = 印を付ける / "drop" = 落とす
        "mode": str(cfg.get("speaker_filter", "off") or "off").strip().lower(),
        # 代表の声とのコサイン類似度の下限。これを下回る発話を「別人」とみなす
        "threshold": float(cfg.get("speaker_threshold", 0.45) or 0.0),
        # 代表の声を求めるときに残す割合 (配信者が占める想定の下限)
        "core_ratio": float(cfg.get("speaker_core_ratio", 0.5) or 0.5),
        # これより短い発話は声紋が安定しないため判定しない (素通りさせる)
        "min_sec": float(cfg.get("speaker_min_sec", 0.6) or 0.0),
    }


# 絞り込みを行うかどうか
def enabled(cfg):
    return cfg["mode"] in ("flag", "drop")


# ------------------------------------------------------------------
# 音声の取り出し
# ------------------------------------------------------------------

# 認識対象の音声を 16kHz モノラル WAV へ落とす (声紋用)
# 1 回だけ変換し、以降は読み込んだ波形を切り出して使う。
# 失敗したら None (呼び出し側は絞り込みを飛ばす)。
def extract_wave(input_path, output_path, ffmpeg_cfg):
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vn", "-ac", "1", "-ar", str(_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=no_window_creationflags())
    except FileNotFoundError:
        _logger.warning("FFmpeg が見つからないため話者照合を飛ばします")
        return None
    if result.returncode != 0 or not os.path.exists(output_path):
        _logger.warning("声紋用の音声を取り出せないため話者照合を飛ばします")
        return None
    return output_path


# WAV を 1 本のモノラル波形 (float のリスト) として読む
# 16bit PCM だけを扱う (extract_wave が必ずその形で書く)。
def load_wave(path):
    try:
        with wave.open(path, "rb") as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
                _logger.warning("想定外の WAV 形式のため話者照合を飛ばします")
                return None, 0
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except (OSError, wave.Error) as error:
        _logger.warning("声紋用の音声を読めないため話者照合を飛ばします: %s", error)
        return None, 0
    return raw, rate


# ------------------------------------------------------------------
# 声紋 (embedding)
# ------------------------------------------------------------------

# torch / torchaudio を遅延 import する (未導入でも起動する / blur と同じ作法)
def _import_torch():
    try:
        import torch                       # noqa: PLC0415 (遅延 import が目的)
        import torchaudio                   # noqa: PLC0415
    except Exception as error:              # noqa: BLE001
        _logger.warning("torchaudio を読み込めないため話者照合を飛ばします: %s", error)
        return None, None
    return torch, torchaudio


# MFCC の統計量 (平均・標準偏差) を声紋とする (方式 "mfcc")
#
# 学習済みの話者照合モデルほどの精度は無いが、
#   ・声道の形 (= 話者の個性) を表す係数であること
#   ・平均と標準偏差で発話長に依らない固定長になること
# から「同じ声か違う声か」の相対判定には使える。
# 0 次係数 (音量) は話者の違いではなくミックスの大小を表すため落とす。
def _mfcc_embedder(torch, torchaudio):
    transform = torchaudio.transforms.MFCC(
        sample_rate=_SAMPLE_RATE, n_mfcc=24,
        melkwargs={"n_fft": 400, "hop_length": 160, "n_mels": 40})

    def embed(samples):
        tensor = torch.tensor(samples, dtype=torch.float32).unsqueeze(0)
        coeffs = transform(tensor)[0]      # [n_mfcc, frames]
        if coeffs.shape[1] < 3:
            return None
        coeffs = coeffs[1:]                # 0 次 (音量) を落とす
        mean = coeffs.mean(dim=1)
        std = coeffs.std(dim=1)
        return torch.cat([mean, std]).tolist()
    return embed


# 声紋の計算関数を返す (torchaudio を読めなければ None)
#
# 実測ではこの方式でも分離できていない (§12.6)。
# 絞り込みを有効にしたときだけ呼ばれ、既定は "off" のため通常は通らない。
def build_embedder(cfg):
    torch, torchaudio = _import_torch()
    if torch is None:
        return None
    _logger.warning(
        "話者照合を有効にしましたが、実測ではゲーム音が乗った音声で分離できていません "
        "(docs/quality/resolve1.md §12.6・§12.7)。"
        "マイク単独トラックのような綺麗な音声でのみ期待できます")
    return _mfcc_embedder(torch, torchaudio)


# ------------------------------------------------------------------
# 代表の声と類似度
# ------------------------------------------------------------------

# 全体の平均を引いてから長さ 1 へそろえる (centering / §12.6)
# 共通成分 (常に乗っている背景音) を除き、話者の差だけを残すための前処理。
def _center(vectors):
    present = [v for v in vectors if v]
    if not present:
        return [None] * len(vectors)
    size = len(present[0])
    mean = [sum(v[k] for v in present) / len(present) for k in range(size)]
    return [_normalize([v[k] - mean[k] for k in range(size)]) if v else None
            for v in vectors]


# ベクトルを長さ 1 へそろえる (コサイン類似度のため)
def _normalize(vector):
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0:
        return None
    return [v / norm for v in vector]


# 正規化済みベクトルのコサイン類似度 (= 内積)
def similarity(a, b):
    return sum(x * y for x, y in zip(a, b))


# 最も多く喋っている話者の代表ベクトルを求める (刈り込み平均)
#
# 配信者が最も発話時間が長いという前提に立ち、
#   全体の平均 → 平均から近い上位 core_ratio だけで再平均
# を数回繰り返す。外れ (ゲーム内の別人の声) が平均から押し出され、
# 多数派 = 配信者の中心へ収束する。
# weights に発話長を渡すと「長く喋っている声」が重く効く。
def dominant_center(vectors, core_ratio=0.5, weights=None, iterations=_CENTER_ITERATIONS):
    usable = [v for v in vectors if v]
    if not usable:
        return None
    weights = weights or [1.0] * len(usable)
    indexes = list(range(len(usable)))
    center = _weighted_mean(usable, indexes, weights)
    keep = max(int(len(usable) * min(max(core_ratio, 0.1), 1.0)), 1)
    for _ in range(iterations):
        if center is None:
            return None
        ranked = sorted(indexes, key=lambda i: -similarity(usable[i], center))
        indexes = ranked[:keep]
        center = _weighted_mean(usable, indexes, weights)
    return center


# 指定した添字の重み付き平均を長さ 1 へそろえて返す
def _weighted_mean(vectors, indexes, weights):
    if not indexes:
        return None
    size = len(vectors[indexes[0]])
    total = [0.0] * size
    weight_sum = 0.0
    for i in indexes:
        w = float(weights[i]) if i < len(weights) else 1.0
        if w <= 0:
            continue
        for k in range(size):
            total[k] += vectors[i][k] * w
        weight_sum += w
    if weight_sum <= 0:
        return None
    return _normalize([v / weight_sum for v in total])


# ------------------------------------------------------------------
# 公開エントリ
# ------------------------------------------------------------------

# 認識結果 (素材時間) を話者で絞り込む (F15)
#
# items      : [{"start","end","text","words"?}] (素材時間)
# audio_path : items と同じ時間軸の音声を含むファイル (正規化後のクリップ等)
# 戻り値     : (items, 統計)
#   mode="flag" : items に "speaker_score" を足し、代表から遠いものへ
#                 "speaker_other"=True を立てる (落とさない)
#   mode="drop" : 代表から遠いものを除く
#
# どこで失敗しても items をそのまま返す (字幕を失わない)。
def apply(items, audio_path, subtitle_cfg, ffmpeg_cfg, work_dir=None):
    cfg = speaker_config(subtitle_cfg)
    stats = {"mode": cfg["mode"], "scored": 0, "other": 0, "dropped": 0}
    if not enabled(cfg) or not items:
        return items, stats

    embedder = build_embedder(cfg)
    if embedder is None:
        return items, stats

    folder = work_dir or os.path.dirname(os.path.abspath(audio_path))
    wave_path = os.path.join(folder, "speaker_16k.wav")
    try:
        if extract_wave(audio_path, wave_path, ffmpeg_cfg) is None:
            return items, stats
        raw, rate = load_wave(wave_path)
        if raw is None:
            return items, stats

        raw_vectors = []
        for item in items:
            samples = _slice(raw, rate, item, cfg["min_sec"])
            raw_vectors.append(embedder(samples) if samples else None)
        # 全体の平均を引いてから正規化する (centering)。
        # 背景のゲーム音が全区間に共通して乗るため、引かないとどのベクトルも
        # 同じ方向を向いてしまう (実測: コサイン類似度が全件 0.97〜0.99 / §12.6)。
        vectors = _center(raw_vectors)

        weights = [max(float(i.get("end", 0)) - float(i.get("start", 0)), 0.0)
                   for i in items]
        center = dominant_center(vectors, cfg["core_ratio"], weights)
        if center is None:
            _logger.info("代表の声を求められないため話者照合を飛ばします")
            return items, stats

        kept = []
        for item, vector in zip(items, vectors):
            if vector is None:
                # 声紋が取れない (短すぎる等) ものは素通りさせる (安全側)
                kept.append(item)
                continue
            score = similarity(vector, center)
            stats["scored"] += 1
            entry = {**item, "speaker_score": round(score, 4)}
            if score >= cfg["threshold"]:
                kept.append(entry)
                continue
            stats["other"] += 1
            if cfg["mode"] == "drop":
                stats["dropped"] += 1
                continue
            entry["speaker_other"] = True
            kept.append(entry)

        _logger.info(
            "話者照合 (%s): 判定 %d 件 / 別人とみなした %d 件 / 落とした %d 件 "
            "(しきい値 %.2f)",
            cfg["mode"], stats["scored"], stats["other"],
            stats["dropped"], cfg["threshold"])
        return kept, stats
    finally:
        if os.path.exists(wave_path):
            try:
                os.remove(wave_path)
            except OSError:
                _logger.warning("声紋用の音声を削除できませんでした: %s", wave_path)


# item の発話区間を波形から切り出す (float のリスト)
# 単語の時刻があれば「最初の語の頭〜最後の語の終わり」を使い、
# テロップの表示区間に混ざる無音を含めない。
def _slice(raw, rate, item, min_sec):
    start, end = _speech_span(item)
    if end - start < max(min_sec, 0.0):
        return None
    begin = max(int(start * rate), 0)
    finish = min(int(end * rate), len(raw) // 2)
    if finish - begin < int(rate * 0.1):
        return None
    chunk = raw[begin * 2:finish * 2]
    return _to_float(chunk)


# 発話の実時間 (単語があればその範囲)
def _speech_span(item):
    words = item.get("words") or []
    starts = [float(w["start"]) for w in words if w.get("start") is not None]
    ends = [float(w["end"]) for w in words if w.get("end") is not None]
    if starts and ends:
        return min(starts), max(ends)
    return float(item.get("start", 0.0)), float(item.get("end", 0.0))


# 16bit PCM のバイト列を -1.0〜1.0 の float へ直す
def _to_float(chunk):
    import array                             # noqa: PLC0415 (この関数だけで使う)

    values = array.array("h")
    values.frombytes(chunk[:len(chunk) // 2 * 2])
    return [v / 32768.0 for v in values]
