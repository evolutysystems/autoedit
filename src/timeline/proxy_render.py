# プロキシ (確認用の低解像度動画) をタイル単位で作る (ver6 resolve3 §3.5 / §4.2)
#
# 1 タイル = 1 回の ffmpeg 実行。ベース連結・合成・焼き込みを分けず 1 パスで作る。
# 本番 (renderer) の多パス構成は A/V 同期の実測対策が積まれているため触らない (§2.2)。
#
# 断片ごとに -ss/-t 付きの入力を並べて concat する作りは audio_source._render_pieces
# の流儀をそのまま踏襲している (§1.6)。
#   【重要】1 入力 + trim にしてはならない。入力シークが効かず素材の先頭から
#   デコードするため、後方の区間では逆に数倍遅くなる (実測 7.62s / resolve6 §2.2)。
#
# 絵の作り方 (どこへどの大きさで置くか) は**一切ここに持たない**。すべて
# compositor を呼ぶ。そうしないと「プレビューにだけ実装が無い効果」が再発する (§1.1)。
import hashlib
import json
import os
import re
import subprocess
import time

from ..modules import ffmpeg_runner
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from . import compositor, crop

_logger = get_logger(__name__)

# これ未満の断片は作らない (ffmpeg が 0 フレームの入力を嫌う)
_MIN_PIECE_SEC = 0.02
# タイル長の下限 (秒)。短すぎるとプロセス起動ばかりになる
MIN_TILE_SEC = 0.5
# 鍵の書式の版。計算を変えたら上げる = 既存のキャッシュが一斉に無効になる
_KEY_VERSION = "proxy-tile-1"
# 生成の打ち切りを確認する間隔 (秒)
_CANCEL_POLL_SEC = 0.1


# 1 回のプロキシ生成で変わらない前提をまとめた入れ物
#
# ProxyManager が 1 つ作って使い回す。render_tile / tile_key が必要とするものを
# ここへ集めることで、両者が同じ前提を見ていることを型で示す。
#   profile             : プロキシ寸法の CanvasProfile
#   cfg                 : timeline.preview.proxy の設定 (builder が検証済み)
#   work_dir            : タイルと中間 ASS の置き場
#   eff_cfg/font_profile: 実効字幕設定と FontProfile (compositor.subtitle_profile 相当)
#   blur_mask_path      : ぼかしマスク (無ければ None)
#   blur_signature      : ぼかし判定の署名。変われば鍵が変わる (§3.5)
#   watermark_required  : 「今出力したら透かしが入るか」(§4.5)
class ProxyEnvironment:

    def __init__(self, settings, profile, cfg, work_dir,
                 eff_cfg=None, font_profile=None, fonts_dir=None,
                 blur_mask_path=None, blur_signature="",
                 watermark_required=False):
        self.settings = settings or {}
        self.profile = profile
        self.cfg = cfg or {}
        self.work_dir = work_dir
        self.eff_cfg = eff_cfg or {}
        self.font_profile = font_profile
        self.fonts_dir = fonts_dir
        self.blur_mask_path = blur_mask_path
        self.blur_signature = str(blur_signature or "")
        self.watermark_required = bool(watermark_required)

    @property
    def tile_sec(self):
        return max(float(self.cfg.get("tile_sec", 4.0) or 4.0), MIN_TILE_SEC)


# ------------------------------------------------------------------
# タイルの区切り
# ------------------------------------------------------------------

# タイル n の Timeline 範囲 (秒) を返す (§3.5)
#
# 境界は必ずフレーム境界へ乗せる。乗せないと、タイルの継ぎ目で 1 コマ重なる /
# 抜けるため「どのフレームに何が写っているか」が一致しなくなる (§2.1)。
# tile_sec が fps の整数倍でなくても、フレーム番号から逆算するので成立する。
def tile_range(timeline, tile_index, tile_sec, fps):
    fps = float(fps or timeline.fps or 1.0)
    tile_sec = max(float(tile_sec), MIN_TILE_SEC)
    index = max(int(tile_index), 0)
    total_frames = int(round(timeline.duration_sec() * fps))
    start = min(int(round(index * tile_sec * fps)), total_frames)
    end = min(int(round((index + 1) * tile_sec * fps)), total_frames)
    return (start / fps, end / fps)


# Timeline 全体を覆うのに必要なタイル数
def tile_count(timeline, tile_sec, fps):
    total = timeline.duration_sec()
    if total <= 0:
        return 0
    tile_sec = max(float(tile_sec), MIN_TILE_SEC)
    count = int(total / tile_sec)
    # 端数ぶんの 1 枚を足す (ちょうど割り切れる場合は足さない)
    if total - count * tile_sec > 1e-6:
        count += 1
    return count


# 時刻を含むタイル番号
def tile_index_at(sec, tile_sec):
    tile_sec = max(float(tile_sec), MIN_TILE_SEC)
    return max(int(float(sec) / tile_sec), 0)


# ------------------------------------------------------------------
# 内容ハッシュ (§3.5)
# ------------------------------------------------------------------

# タイルの絵を決める入力すべてから鍵を作る
#
# CommandStack はスナップショット方式で「このコマンドが汚した範囲」を取り出せない
# (§1.5)。そこで汚れ範囲を追跡せず、**内容が同じなら鍵が同じ**という形で
# キャッシュの正しさをデータ側に担保させる。Undo / Redo で元の状態へ戻れば
# 鍵も戻り、キャッシュが自然に効き直す。
#
# 追跡コードを書かないので「書き忘れによる古い絵の残留」が原理的に起きない。
def tile_key(timeline, env, tile_index):
    profile = env.profile
    start, end = tile_range(timeline, tile_index, env.tile_sec, profile.fps)
    parts = [
        _KEY_VERSION,
        f"canvas={profile.width}x{profile.height}@{profile.fps}:{profile.scale:.4f}",
        f"range={start:.3f}-{end:.3f}",
        f"watermark={int(env.watermark_required)}",
        f"blur={env.blur_signature}",
        f"crop={json.dumps(crop.load(timeline) or {}, sort_keys=True)}",
        f"settings={_settings_signature(env.settings)}",
    ]

    for clip in timeline.base_clips():
        if not _overlaps(clip.timeline_start, clip.timeline_end, start, end):
            continue
        audio = timeline.audio_clip_for(clip.id)
        # 拡大率と表示位置も絵を決める (ver9 resolve6 §4.2)。
        # ここへ足し忘れると、拡大率を変えても古いタイルが使われて絵が変わらない。
        transform = clip.transform
        parts.append("v1|" + "|".join([
            str(clip.media_id), f"{clip.source_in:.4f}",
            f"{clip.timeline_start:.4f}", f"{clip.duration:.4f}",
            f"{0.0 if audio is None else audio.gain_db:.3f}",
            str(int(bool(audio is not None and audio.muted))),
            f"{float(transform.scale or 1.0):.4f}",
            str(transform.x), str(transform.y),
        ]))

    for element in timeline.overlay_elements():
        if not _overlaps(element.timeline_start, element.timeline_end, start, end):
            continue
        transform = element.transform
        if hasattr(element, "text"):
            # 字幕 (文字・役割・フォント・色・位置・使用フラグ)
            parts.append("sub|" + "|".join([
                str(element.text), str(element.role), str(element.font),
                str(element.font_size), str(element.color),
                str(element.outline_color), str(int(bool(element.use))),
                f"{element.timeline_start:.4f}", f"{element.duration:.4f}",
                str(transform.x), str(transform.y), str(element.z_order),
            ]))
        else:
            parts.append("ov|" + "|".join([
                str(element.media_id), f"{element.source_in:.4f}",
                f"{element.timeline_start:.4f}", f"{element.duration:.4f}",
                str(transform.x), str(transform.y), str(transform.scale),
                str(transform.opacity), str(element.z_order),
            ]))

    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


# 絵に影響する設定だけを取り出して署名する
# 全部を入れると、無関係な設定変更でプロキシが全部作り直しになる。
def _settings_signature(settings):
    settings = settings if isinstance(settings, dict) else {}
    vertical = settings.get("vertical") or {}
    blur = settings.get("blur") or {}
    picked = {
        "subtitle": settings.get("subtitle") or {},
        "vertical_crop": vertical.get("crop") or {},
        "blur_enabled": bool(blur.get("enabled", False)),
        "blur_render": blur.get("render") or {},
        "watermark": settings.get("watermark") or {},
        "output_fps": (settings.get("ffmpeg") or {}).get("output_fps"),
    }
    return json.dumps(picked, sort_keys=True, ensure_ascii=False, default=str)


def _overlaps(start, end, window_start, window_end):
    return start < window_end - 1e-6 and end > window_start + 1e-6


# ------------------------------------------------------------------
# タイルの生成
# ------------------------------------------------------------------

# タイル 1 枚ぶんの ffmpeg コマンドを組み立てる。作る必要が無ければ None。
#
# 【重要】**Timeline を読むのはここだけ**にしてある。コマンドの組み立ては
# GUI スレッドで行い、ワーカースレッドは出来上がったコマンドを実行するだけにする。
# こうしないと、生成中に利用者が編集した Timeline を別スレッドから読むことになり、
# 中途半端な状態を拾う余地が残る (ProxyManager 側の約束 / §4.3)。
#   out_path : 書き出し先 (呼び出し側が鍵から決める)
def plan_tile(timeline, env, tile_index, out_path):
    profile = env.profile
    start, end = tile_range(timeline, tile_index, env.tile_sec, profile.fps)
    if end - start <= _MIN_PIECE_SEC:
        return None

    pieces = _collect_pieces(timeline, start, end)
    if not pieces:
        return None

    command, _labels = _build_command(timeline, env, tile_index, start, end,
                                      pieces, out_path)
    if command is None:
        return None
    _logger.debug("プロキシのタイルを組みました: #%d %.3f〜%.3fs (断片 %d 本)",
                  tile_index, start, end, len(pieces))
    return command


# 組み立てたコマンドを実行する。戻り値 (成功したか, 理由)
#   理由: "" (成功) / "missing" (ffmpeg が無い) / "failed" / "cancelled"
def run_tile(command, is_cancelled=None):
    return _run(command, is_cancelled)


# タイル 1 枚を作る (組み立て + 実行)。戻り値: 生成したパス (失敗・打ち切りは None)
# 1 スレッドで完結させたい呼び出し (テスト / CLI) 向け。
def render_tile(timeline, env, tile_index, out_path, is_cancelled=None):
    command = plan_tile(timeline, env, tile_index, out_path)
    if command is None:
        return None
    ok, _reason = run_tile(command, is_cancelled)
    if not ok:
        return None
    return out_path if os.path.exists(out_path) else None


# 区間 [start, end) を構成する V1 の断片を集める (§1.6 / audio_source と同じ流儀)
# ギャップは黒 + 無音の断片として入れる (renderer._render_gap と同じ扱い)。
def _collect_pieces(timeline, start, end):
    pieces = []
    cursor = start
    for clip in timeline.base_clips():
        if clip.timeline_end <= start + 1e-6:
            continue
        if clip.timeline_start >= end - 1e-6:
            break
        if clip.timeline_start > cursor + 1e-6:
            pieces.append({"kind": "gap",
                           "duration": min(clip.timeline_start, end) - cursor})
            cursor = min(clip.timeline_start, end)
        overlap_start = max(start, clip.timeline_start)
        overlap_end = min(end, clip.timeline_end)
        duration = overlap_end - overlap_start
        if duration <= _MIN_PIECE_SEC:
            continue
        media = timeline.media_by_id(clip.media_id)
        if media is None or not os.path.exists(media.path or ""):
            pieces.append({"kind": "gap", "duration": duration})
            cursor = overlap_end
            continue
        audio_clip = timeline.audio_clip_for(clip.id)
        muted = audio_clip is not None and audio_clip.muted
        pieces.append({
            "kind": "clip",
            "clip": clip,
            "media": media,
            # Timeline 上でこの断片が占める範囲 (フェードの判定に使う)
            "start": overlap_start,
            "end": overlap_end,
            # 素材の中の開始位置 (タイルの途中から始まる場合はそのぶん進める)
            "source_in": clip.source_in + (overlap_start - clip.timeline_start),
            "duration": duration,
            "gain_db": 0.0 if audio_clip is None else audio_clip.gain_db,
            # 音声を持たない / ミュート / 画像は無音を合成して映像と同尺にする
            "needs_silence": (media.is_image() or not media.has_audio
                              or muted or audio_clip is None),
        })
        cursor = overlap_end
    if end > cursor + 1e-6:
        pieces.append({"kind": "gap", "duration": end - cursor})
    return [p for p in pieces if p["duration"] > _MIN_PIECE_SEC]


# ffmpeg のコマンドを組み立てる。戻り値 (コマンド, ラベルの記録)
def _build_command(timeline, env, tile_index, start, end, pieces, out_path):
    profile = env.profile
    settings = env.settings
    cfg = env.cfg
    sample_rate = int((settings.get("ffmpeg") or {}).get("audio_sample_rate", 48000))
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(settings.get("ffmpeg") or {})

    input_args = []
    chains = []
    video_labels = []
    audio_labels = []
    index = 0

    # ── ① 断片ごとに base_filters を当て、concat で 1 本へ繋ぐ
    for order, piece in enumerate(pieces):
        duration = piece["duration"]
        if piece["kind"] == "gap":
            input_args += ["-f", "lavfi", "-t", f"{duration:.3f}",
                           "-i", (f"color=c=black:s={profile.width}x{profile.height}"
                                  f":r={profile.fps}")]
            chains.append(f"[{index}:v]setsar=1,format=yuv420p[pcv{order}]")
            video_labels.append(f"[pcv{order}]")
            index += 1
            input_args += ["-f", "lavfi", "-t", f"{duration:.3f}",
                           "-i", (f"anullsrc=channel_layout=stereo"
                                  f":sample_rate={sample_rate}")]
            audio_labels.append(f"[{index}:a]")
            index += 1
            continue

        input_args += ["-ss", f"{piece['source_in']:.3f}",
                       "-t", f"{duration:.3f}", "-i", piece["media"].path]
        filters = compositor.base_filters(
            timeline, piece["media"], piece["clip"], profile,
            fade_sec=_fade_sec(settings), duration=duration, settings=settings,
            # 断片がクリップのどこを切り出したかを渡す (フェードの判定に使う)
            window=(piece["start"], piece["end"]))
        # concat は入力の寸法・SAR・画素形式がそろっていることを要求する
        filters = list(filters) + ["setsar=1", "format=yuv420p"]
        chains.append(f"[{index}:v]{','.join(filters)}[pcv{order}]")
        video_labels.append(f"[pcv{order}]")
        video_index = index
        index += 1

        if piece["needs_silence"]:
            input_args += ["-f", "lavfi", "-t", f"{duration:.3f}",
                           "-i", (f"anullsrc=channel_layout=stereo"
                                  f":sample_rate={sample_rate}")]
            audio_labels.append(f"[{index}:a]")
            index += 1
        elif abs(piece["gain_db"]) > 1e-6:
            chains.append(f"[{video_index}:a]volume={piece['gain_db']:g}dB[pca{order}]")
            audio_labels.append(f"[pca{order}]")
        else:
            audio_labels.append(f"[{video_index}:a]")

    if len(pieces) > 1:
        chains.append("".join(
            v + a for v, a in zip(video_labels, audio_labels))
            + f"concat=n={len(pieces)}:v=1:a=1[cv][ca]")
        current = "[cv]"
        audio_map = "[ca]"
        mix_labels = ["[ca]"]
    else:
        current = video_labels[0]
        audio_map = audio_labels[0]
        mix_labels = [audio_labels[0]]

    # ── ② ぼかしはチェーンの先頭 (字幕・アイコン・オーバーレイをぼかさない / R8)
    # マスクはタイル開始ぶん進めて読む (§3.5)
    blur_chains, current = compositor.blur_chains(
        settings, env.blur_mask_path, profile, current, "[blout]",
        prefix="pbl", seek_point=start)
    chains.extend(blur_chains)

    # ── ③ オーバーレイと字幕を z_order 順に
    step = 0
    for element in timeline.overlay_elements():
        if not _overlaps(element.timeline_start, element.timeline_end, start, end):
            continue
        if hasattr(element, "text"):
            continue                    # 字幕はまとめて 1 つの ASS にする (下記)
        media = timeline.media_by_id(element.media_id)
        if media is None or not os.path.exists(media.path or ""):
            continue
        args, overlay_chains, current, step = compositor.overlay_chain(
            element, media, profile, current, index, step,
            time_offset=start, window=(start, end), prefix="p")
        input_args += args
        chains.extend(overlay_chains)
        overlay_index = index
        index += 1
        # 音声付きの動画オーバーレイはベース音声へ混ぜる
        if media.has_audio and not media.is_image():
            delay_ms = int(max(element.timeline_start - start, 0.0) * 1000)
            label = f"[pova{step}]"
            chains.append(
                f"[{overlay_index}:a]adelay={delay_ms}|{delay_ms},"
                f"aformat=sample_rates={sample_rate}:channel_layouts=stereo{label}")
            mix_labels.append(label)

    # ── ④ 字幕とコメントアイコン (時刻はタイル開始を 0 とみなして書き出す)
    items = _subtitle_items(timeline, start, end)
    if items:
        ass_path = os.path.join(env.work_dir, f"proxy_tile_{tile_index:05d}.ass")
        _path, option = compositor.subtitle_chain_for_items(
            timeline, items, env.eff_cfg, env.font_profile, ass_path, env.fonts_dir)
        chains.append(f"{current}{option}[psub]")
        current = "[psub]"
        icon_chains, _count, _groups = compositor.icon_chains(
            items, env.eff_cfg, profile, current, "[picon]", prefix="picic")
        if icon_chains:
            chains.extend(icon_chains)
            current = "[picon]"

    # ── ⑤ 透かしはチェーンの最後 (字幕・アイコンの上へ重ねる)
    wm_args, wm_chains, current = compositor.watermark_chain(
        settings, profile, current, "[pwm]", index,
        required=env.watermark_required)
    if wm_chains:
        input_args += wm_args
        chains.extend(wm_chains)
        index += 1

    # 音声の合流 (オーバーレイ音声があれば amix)
    if len(mix_labels) > 1:
        chains.append(
            f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:"
            f"duration=first:dropout_transition=0[paout]")
        audio_map = "[paout]"
    else:
        audio_map = _map_target(audio_map)

    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        *input_args,
        "-filter_complex", ";".join(chains),
        "-map", current,
        "-map", audio_map,
        # プロキシは 1 パスで速く作る。画質を上げても一致度は上がらない (§2.2)
        "-c:v", "libx264",
        "-preset", str(cfg.get("preset", "ultrafast") or "ultrafast"),
        "-crf", str(int(cfg.get("crf", 28) or 28)),
        "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr", "-r", str(profile.fps),
        "-c:a", "aac", "-b:a", str(cfg.get("audio_bitrate", "96k") or "96k"),
        "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-t", f"{end - start:.6f}",
        # 容器を明示する。呼び出し側は生成中のファイルへ .part を付けるため、
        # 拡張子からの推測に任せると muxer を決められず失敗する。
        "-f", "mp4",
        out_path,
    ]
    return command, {"video": current, "audio": audio_map}


# 入力ストリームの指定を -map の形へ直す
#
# filtergraph の出力ラベル ([ca] / [paout]) は括弧付きのまま渡すが、
# 入力ストリームは **括弧を外す** 必要がある (-map 0:a)。
# 断片が 1 本だけのタイルでは concat を通らず入力の音声を直接使うため、
# ここを間違えると「label '0:a' does not exist」で ffmpeg が起動に失敗する。
def _map_target(label):
    inner = str(label).strip("[]")
    return inner if re.fullmatch(r"\d+:[av]", inner) else label


# 無音カットのフェード秒 (renderer.render と同じ解決)
def _fade_sec(settings):
    silence_cfg = (settings or {}).get("silence_cut") or {}
    if not silence_cfg.get("fade_enabled", False):
        return 0.0
    return float(silence_cfg.get("fade_duration_sec", 0.05) or 0.0)


# タイルに重なる字幕を、タイル開始を 0 とした item 配列にする
#
# ASS は絶対時刻で書くため、タイルを単体の動画として作る本方式では
# タイル開始ぶん引く必要がある。はみ出した側は 0 / タイル尺で詰める。
def _subtitle_items(timeline, start, end):
    items = []
    for clip in timeline.subtitle_clips():
        if not _overlaps(clip.timeline_start, clip.timeline_end, start, end):
            continue
        item = clip.to_item()
        item["start"] = max(clip.timeline_start - start, 0.0)
        item["end"] = min(clip.timeline_end - start, end - start)
        items.append(item)
    return items


# ffmpeg を実行する。戻り値 (成功したか, 理由)
def _run(command, is_cancelled=None):
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=no_window_creationflags())
    except OSError as error:
        # ffmpeg が無い / 起動できない。呼び出し側が縮退を判断する (§7)
        _logger.warning("プロキシの生成を開始できませんでした: %s", error)
        return False, "missing"

    err = b""
    while True:
        try:
            _out, err = process.communicate(timeout=_CANCEL_POLL_SEC)
        except subprocess.TimeoutExpired:
            if is_cancelled is not None and is_cancelled():
                _terminate(process)
                _logger.debug("プロキシの生成を打ち切りました")
                return False, "cancelled"
            continue
        break

    if process.returncode != 0:
        # そのタイルだけ諦める。近似描画のまま編集は続けられる (§7)
        # どのタイルが失敗したのか分かるよう出力名を残す (tile_<番号>_<鍵>.mp4)。
        # 同じタイルが 3 回続けて失敗するとプロキシ自体を諦めるため、
        # 番号が分からないと後から原因を追えない。
        _logger.warning("プロキシのタイル生成に失敗しました (code=%s / %s): %s",
                        process.returncode, os.path.basename(command[-1]),
                        (err or b"").decode("utf-8", "replace").strip()[-500:])
        return False, "failed"
    return True, ""


def _terminate(process):
    try:
        process.terminate()
        deadline = time.monotonic() + 2.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            process.kill()
    except Exception:  # noqa: BLE001 (後片付けで落とさない)
        _logger.debug("プロキシの生成プロセスを止められませんでした")
