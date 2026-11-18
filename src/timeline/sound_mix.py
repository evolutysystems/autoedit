# SE (効果音) のミックス (docs/request/ver12/resolve1.md §9 / §10 / §11)
#
# Timeline の SE トラックに載ったノードを、1 本の音声へ混ぜ込む。
# 出力・プレビュー音声・プロキシタイルの 3 経路が同じ計算を使うため、
# 「どの素材のどこを、何秒の位置へ、何 dB で鳴らすか」はこのモジュールだけが持つ。
#
# 【出力での置き場所】renderer.render の最後 (透かしの後)。
#   合成 (_composite) の amix へ混ぜるとオーバーレイも字幕も無い案件で
#   合成自体が走らず、SE だけ鳴らなくなる (resolve1 §9.1)。
#   最後に 1 段置けば、どの経路を通っても必ず鳴る。
#
# 【画質】映像は -c:v copy で通すため、この段で画質は落ちない。
import os

from ..modules import ffmpeg_runner
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# これ未満の断片は作らない (ffmpeg が 0 秒入力で失敗するため)
_MIN_PIECE_SEC = 0.01

# 設定が渡されなかったときの既定 (builder._sound_tracks_config と同じ値)
_DEFAULT_CFG = {
    "mix_normalize": False,
    "max_mix_inputs": 32,
}


# timeline.sound_tracks の設定を既定で補完して返す
def config(settings=None, cfg=None):
    if cfg is None:
        cfg = ((settings or {}).get("timeline", {}) or {}).get("sound_tracks", {}) or {}
    return {**_DEFAULT_CFG, **(cfg or {})}


# ------------------------------------------------------------------
# 区間に重なる SE の洗い出し (3 経路の共通計算)
# ------------------------------------------------------------------

# [window_start, window_end) に鳴る SE を、その区間用の断片として返す
#
# 戻り値の各要素:
#   path        : 素材のパス
#   source_in   : 素材内の開始位置 (秒)
#   duration    : 鳴らす長さ (秒)
#   delay_sec   : 区間の先頭から何秒後に鳴らすか (0 以上)
#   gain_db     : 音量 (dB)
#
# 区間からはみ出した部分は切る。素材が見つからないノードは落とす
# (出力を止めない / resolve1 §9.4)。
def collect_pieces(timeline, window_start=0.0, window_end=None):
    start = max(float(window_start), 0.0)
    end = float(window_end) if window_end is not None else timeline.duration_sec()
    pieces = []
    for clip in timeline.sound_clips():
        overlap_start = max(start, clip.timeline_start)
        overlap_end = min(end, clip.timeline_end)
        duration = overlap_end - overlap_start
        if duration <= _MIN_PIECE_SEC:
            continue
        media = timeline.media_by_id(clip.media_id)
        if media is None or not os.path.exists(media.path or ""):
            _logger.warning("SE の素材が見つからないため鳴らしません: %s", clip.id)
            continue
        pieces.append({
            "clip_id": clip.id,
            "path": media.path,
            # 区間の途中から始まる場合は、素材側も同じだけ進める
            "source_in": clip.source_in + (overlap_start - clip.timeline_start),
            "duration": duration,
            "delay_sec": overlap_start - start,
            "gain_db": float(clip.gain_db),
        })
    return pieces


# その Timeline に鳴らす SE があるか (無ければ呼び出し側は 1 コマンドも足さない)
def has_sound(timeline, window_start=0.0, window_end=None):
    return bool(collect_pieces(timeline, window_start, window_end))


# ------------------------------------------------------------------
# filtergraph の部品 (プレビュー・プロキシが継ぎ足して使う)
# ------------------------------------------------------------------

# SE 断片を入力として足し、amix へ渡すラベルを作る
#
# index_base : 既に使っている入力の本数 (この番号から採番する)
# sample_rate: 出力のサンプリングレート
# prefix     : ラベルの接頭辞 (1 つの filtergraph で重複させないため)
# 戻り値: (入力引数, チェーン, ラベル列)
def input_chains(pieces, index_base, sample_rate, channels=2, prefix="se"):
    input_args = []
    chains = []
    labels = []
    layout = "mono" if int(channels or 2) == 1 else "stereo"
    for order, piece in enumerate(pieces):
        index = index_base + order
        input_args += ["-ss", f"{piece['source_in']:.3f}",
                       "-t", f"{piece['duration']:.3f}",
                       "-i", piece["path"]]
        filters = [f"aformat=sample_rates={sample_rate}:channel_layouts={layout}"]
        if abs(piece["gain_db"]) > 1e-6:
            filters.insert(0, f"volume={piece['gain_db']:g}dB")
        delay_ms = int(round(max(piece["delay_sec"], 0.0) * 1000))
        if delay_ms > 0:
            # all=1 で全チャンネルへ同じ遅延を掛ける (チャンネル数に依らない)
            filters.append(f"adelay={delay_ms}:all=1")
        label = f"[{prefix}{order}]"
        chains.append(f"[{index}:a]{','.join(filters)}{label}")
        labels.append(label)
    return input_args, chains, labels


# amix のフィルタ文字列を作る (ラベルが 1 本なら None = 混ぜる必要が無い)
# normalize=0 が既定。1 にすると入力数で割られ、SE を足すほど全体が小さくなる。
def amix_filter(labels, out_label="[aout]", normalize=False):
    if len(labels) <= 1:
        return None
    return (f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:"
            f"dropout_transition=0:normalize={1 if normalize else 0}{out_label}")


# ------------------------------------------------------------------
# 出力への適用 (renderer から呼ぶ)
# ------------------------------------------------------------------

# 完成した動画へ SE を混ぜ込んだ新しい動画を返す (resolve1 §9.3)
#
# SE が無ければ video_path をそのまま返す (ffmpeg は 1 回も起動しない)。
# 失敗しても例外は投げず元のパスを返す (SE のために出力そのものを失わない)。
def apply(timeline, context, video_path, ffmpeg_cfg=None, cfg=None):
    settings = getattr(context, "settings", {}) or {}
    ffmpeg_cfg = ffmpeg_cfg if ffmpeg_cfg is not None else settings.get("ffmpeg", {})
    cfg = config(settings, cfg)

    total = timeline.duration_sec()
    pieces = collect_pieces(timeline, 0.0, total)
    if not pieces:
        return video_path

    sample_rate = int(ffmpeg_cfg.get("audio_sample_rate", 48000) or 48000)
    work_dir = _work_dir(context, video_path)
    mix_path = None
    try:
        mix_path = _build_sound_bed(
            pieces, total, sample_rate, work_dir, ffmpeg_cfg, cfg, context)
        if mix_path is None:
            return video_path
        output_path = _allocate(context, "timeline_sound_mix.mp4")
        _merge_into_video(video_path, mix_path, output_path, sample_rate,
                          ffmpeg_cfg, cfg, context, total)
    except Exception:  # noqa: BLE001 (SE の失敗で出力を失わない / resolve1 §9.4)
        _logger.exception("SE のミックスに失敗しました (SE 無しで続行します)")
        return video_path
    finally:
        _remove(mix_path)

    _logger.info("SE を %d 件ミックスしました", len(pieces))
    return output_path


# SE だけを並べた音声 (サウンドベッド) を作る
# 本数が max_mix_inputs を超える場合は、束ごとに作ってから段階的に混ぜる。
def _build_sound_bed(pieces, total, sample_rate, work_dir, ffmpeg_cfg, cfg,
                     context=None):
    limit = max(int(cfg.get("max_mix_inputs", 32) or 32), 2)
    # 無音 1 本ぶんを入力に使うため、1 回に載せられる SE は limit - 1 件
    batch_size = max(limit - 1, 1)
    batches = [pieces[i:i + batch_size] for i in range(0, len(pieces), batch_size)]

    parts = []
    try:
        for order, batch in enumerate(batches):
            path = os.path.join(work_dir, f"timeline_sound_bed_{order:02d}.wav")
            _render_bed(batch, total, sample_rate, path, ffmpeg_cfg, cfg, context)
            parts.append(path)
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        # 束どうしをもう 1 回混ぜる (入力は束の数だけ)
        merged = os.path.join(work_dir, "timeline_sound_bed.wav")
        _merge_beds(parts, merged, sample_rate, ffmpeg_cfg, cfg)
        for path in parts:
            _remove(path)
        return merged
    except Exception:
        for path in parts:
            _remove(path)
        raise


# 無音を土台にして SE を並べた wav を 1 本書き出す
def _render_bed(pieces, total, sample_rate, out_path, ffmpeg_cfg, cfg, context=None):
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-t", f"{max(total, _MIN_PIECE_SEC):.3f}",
           "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}"]
    input_args, chains, labels = input_chains(pieces, 1, sample_rate, prefix="se")
    cmd += input_args
    # 土台の無音も混ぜる (SE の無い時間を 0 で埋め、尺を total にそろえる)
    mix = amix_filter(["[0:a]"] + labels, "[bed]",
                      normalize=bool(cfg.get("mix_normalize", False)))
    chains.append(mix)
    cmd += ["-filter_complex", ";".join(chains), "-map", "[bed]",
            "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "2",
            "-t", f"{max(total, _MIN_PIECE_SEC):.3f}", out_path]
    ffmpeg_runner.execute(
        cmd, progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
        cancel_cb=_cancel_cb(context))


# 束ごとのサウンドベッドを 1 本へ混ぜる
def _merge_beds(paths, out_path, sample_rate, ffmpeg_cfg, cfg):
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    labels = []
    for index, path in enumerate(paths):
        cmd += ["-i", path]
        labels.append(f"[{index}:a]")
    cmd += ["-filter_complex",
            amix_filter(labels, "[bed]",
                        normalize=bool(cfg.get("mix_normalize", False))),
            "-map", "[bed]", "-c:a", "pcm_s16le",
            "-ar", str(sample_rate), "-ac", "2", out_path]
    ffmpeg_runner.execute(
        cmd, progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg))


# 本編の音声とサウンドベッドを混ぜ、映像はコピーして新しい動画を書く
def _merge_into_video(video_path, bed_path, out_path, sample_rate, ffmpeg_cfg, cfg,
                      context=None, total_duration=0.0):
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    normalize = bool(cfg.get("mix_normalize", False))
    cmd = [ffmpeg, "-y", "-hide_banner", "-i", video_path, "-i", bed_path]
    if _has_audio_stream(video_path, ffmpeg_cfg):
        base = "[0:a]"
    else:
        # 本編に音声が無い場合の保険。無音を足してから混ぜる (入力は 1 本増える)
        cmd += ["-f", "lavfi",
                "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}"]
        base = "[2:a]"
    chains = [amix_filter([base, "[1:a]"], "[aout]", normalize=normalize)]
    cmd += [
        "-filter_complex", ";".join(chains),
        "-map", "0:v", "-c:v", "copy",
        "-map", "[aout]",
        "-c:a", ffmpeg_cfg.get("audio_codec", "aac"),
        *ffmpeg_runner.audio_bitrate_options(ffmpeg_cfg),
        "-ar", str(sample_rate), "-ac", "2",
        "-movflags", "+faststart",
        # 映像の尺で切る (本編より後ろへはみ出した SE は鳴らさない / resolve1 §4.3)
        "-shortest",
        out_path,
    ]
    on_progress = None
    if context is not None and hasattr(context, "progress_subcallback"):
        on_progress = context.progress_subcallback("SE のミックス")
    ffmpeg_runner.execute(
        cmd, total_duration=total_duration, on_progress=on_progress,
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_cfg),
        cancel_cb=_cancel_cb(context))


# 動画に音声ストリームがあるか (取得できないときは「ある」とみなす)
def _has_audio_stream(path, ffmpeg_cfg):
    try:
        from . import media_probe
        media = media_probe.probe(path, "", {"ffmpeg": ffmpeg_cfg})
        return bool(media.has_audio)
    except Exception:  # noqa: BLE001 (判定できなくても出力を止めない)
        _logger.warning("音声ストリームの有無を判定できませんでした: %s", path)
        return True


# 中間ファイルの置き場 (context があればそちら、無ければ動画と同じフォルダ)
def _work_dir(context, video_path):
    work_dir = getattr(context, "working_dir", None)
    if work_dir and os.path.isdir(work_dir):
        return work_dir
    return os.path.dirname(os.path.abspath(video_path)) or "."


# 出力先のパスを確保する (context があれば中間ファイルとして登録する)
def _allocate(context, filename):
    allocate = getattr(context, "allocate_intermediate", None)
    if callable(allocate):
        return allocate(filename)
    return os.path.join(_work_dir(context, filename), filename)


# 取り消しコールバック (renderer._cancel_cb と同じ規約)
def _cancel_cb(context):
    return getattr(context, "cancel_cb", None)


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
