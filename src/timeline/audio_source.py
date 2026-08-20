# プレビュー用の音声チャンク生成 (docs/request/ver3/resolve.md §6.4-5 / R16)
# 再生ヘッド位置から一定秒ぶんの「Timeline 音声」をオンデマンドで生成し、
# QMediaPlayer の再生ソースにする。QMediaPlayer をマスタークロックにすることで
# 映像フレームの取得が間に合わない場合でも音声が途切れない。
#
# 未編集かつ OP/ED を含まない Timeline に限り、音声認識用に作った一時ファイル
# (残す区間の音声を連結したもの) をそのまま初回再生ソースとして再利用でき、
# 画面を開いた直後に待ち時間なく再生できる。
import os

from ..modules import ffmpeg_runner, silence_cutter
from ..utils.logger import get_logger
from .model import ORIGIN_ENDING, ORIGIN_OPENING

_logger = get_logger(__name__)

# 生成しない極短区間
_MIN_CHUNK_SEC = 0.05


class AudioChunkSource:

    # timeline           : 対象 Timeline (編集のたびに invalidate される)
    # settings           : setting.json
    # work_dir           : 一時ファイルの置き場 (PipelineContext.working_dir)
    # initial_audio_path : 音声認識用に生成済みの音声 (再利用できる場合のみ渡す)
    # preview_cfg        : timeline.preview の設定 (生成方式・品質・保持本数 / resolve6 §5.6)
    def __init__(self, timeline, settings, work_dir, initial_audio_path=None,
                 preview_cfg=None):
        self._timeline = timeline
        self._settings = settings or {}
        self._ffmpeg_cfg = self._settings.get("ffmpeg", {})
        self._work_dir = work_dir
        self._sequence = 0
        # 生成済みチャンク: [(path, start_sec, length_sec), …] (新しいものが末尾)
        # 1 本だけ持つと「少し戻して再生」で毎回作り直しになるため複数持つ (resolve6 §3-6 R5)
        self._chunks = []
        self._generated = []
        cfg = preview_cfg or {}
        # 断片ごとに ffmpeg を起動する従来方式へ戻すための切り替え (resolve6 §7)
        self._build_mode = str(cfg.get("audio_build_mode", "single_process") or
                               "single_process")
        self._chunk_limit = max(int(cfg.get("audio_chunk_cache", 3) or 1), 1)
        # プレビュー音声の品質。0 / 空文字なら ffmpeg セクションの値を使う。
        self._sample_rate = (int(cfg.get("audio_sample_rate", 0) or 0)
                             or int(self._ffmpeg_cfg.get("audio_sample_rate", 48000)))
        self._channels = max(int(cfg.get("audio_channels", 0) or 0), 0) or 2
        self._codec = (str(cfg.get("audio_codec", "") or "")
                       or str(self._ffmpeg_cfg.get("audio_codec", "aac")))
        # 認識用音声を再利用できるか (§6.4-5)
        self._initial_audio_path = initial_audio_path
        self._initial_valid = self._can_reuse_initial(timeline, initial_audio_path)
        if self._initial_valid:
            _logger.info("プレビュー音声に認識用音声を再利用します (未編集)")

    # ------------------------------------------------------------------
    # 初回音声の再利用判定
    # ------------------------------------------------------------------

    # 認識用音声をそのまま使えるか
    # 条件: ファイルが在る / V1 に OP・ED が無い (在ると先頭・末尾がずれるため)
    def _can_reuse_initial(self, timeline, path):
        if not path or not os.path.exists(path):
            return False
        for clip in timeline.base_clips():
            if clip.origin_type() in (ORIGIN_OPENING, ORIGIN_ENDING):
                return False
        return True

    # ------------------------------------------------------------------
    # 公開 I/F
    # ------------------------------------------------------------------

    # 指定区間をまかなえる生成済み音声があれば (パス, チャンク開始秒) を返す
    def cached(self, start_sec, length_sec):
        start = max(float(start_sec), 0.0)
        end = start + float(length_sec)
        if self._initial_valid:
            # 認識用音声は Timeline 全長 (OP/ED 無し) をカバーする
            return self._initial_audio_path, 0.0
        # 新しいものから順に見る (直近に作ったチャンクが当たりやすい)
        for path, chunk_start, chunk_len in reversed(self._chunks):
            if chunk_start - 1e-6 <= start and end <= chunk_start + chunk_len + 1e-6:
                return path, chunk_start
        return None

    # 指定区間の音声を生成して (パス, チャンク開始秒) を返す (ワーカースレッドから呼ぶ)
    def build(self, start_sec, length_sec):
        hit = self.cached(start_sec, length_sec)
        if hit is not None:
            return hit

        start = max(float(start_sec), 0.0)
        length = max(float(length_sec), _MIN_CHUNK_SEC)
        total = self._timeline.duration_sec()
        if start >= total - _MIN_CHUNK_SEC:
            return None
        length = min(length, total - start)

        pieces = self._collect_pieces(start, start + length)
        if not pieces:
            return None

        self._sequence += 1
        extension = "wav" if self._codec.startswith("pcm_") else "m4a"
        out_path = os.path.join(
            self._work_dir, f"preview_audio_{self._sequence:04d}.{extension}")
        try:
            if self._build_mode == "per_piece":
                self._render_pieces_each(pieces, out_path, extension)
            else:
                self._render_pieces(pieces, out_path)
        except Exception:  # noqa: BLE001 (音声生成の失敗で編集を止めない / §10)
            _logger.exception("プレビュー音声の生成に失敗しました")
            self._cleanup_paths([out_path])
            return None

        self._chunks.append((out_path, start, length))
        self._generated.append(out_path)
        self._trim_chunks()
        _logger.debug("音声チャンク生成: %.1fs から %.1fs (断片 %d 本)",
                      start, length, len(pieces))
        return out_path, start

    # Timeline が編集されたら生成済みを捨てる (以降はオンデマンド生成に切り替わる)
    def invalidate(self):
        self._initial_valid = False
        self._drop_chunks()

    # 生成した一時ファイルをすべて削除する (画面クローズ時)
    def cleanup(self):
        self._chunks = []
        self._cleanup_paths(self._generated)
        self._generated = []

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    # 区間 [start, end] を構成する断片 (クリップの一部 / 無音) の列を作る
    def _collect_pieces(self, start, end):
        pieces = []
        cursor = start
        for clip in self._timeline.base_clips():
            if clip.timeline_end <= start + 1e-6:
                continue
            if clip.timeline_start >= end - 1e-6:
                break
            # クリップより手前に空白があれば無音を挟む
            if clip.timeline_start > cursor + 1e-6:
                pieces.append({"kind": "silence",
                               "duration": min(clip.timeline_start, end) - cursor})
                cursor = min(clip.timeline_start, end)
            overlap_start = max(start, clip.timeline_start)
            overlap_end = min(end, clip.timeline_end)
            duration = overlap_end - overlap_start
            if duration <= _MIN_CHUNK_SEC:
                continue
            audio_clip = self._timeline.audio_clip_for(clip.id)
            media = self._timeline.media_by_id(clip.media_id)
            muted = audio_clip is not None and audio_clip.muted
            has_audio = (media is not None and media.has_audio
                         and not media.is_image() and audio_clip is not None)
            if not has_audio or muted:
                pieces.append({"kind": "silence", "duration": duration})
            else:
                pieces.append({
                    "kind": "clip",
                    "path": media.path,
                    "source_in": clip.source_in + (overlap_start - clip.timeline_start),
                    "duration": duration,
                    "gain_db": audio_clip.gain_db,
                })
            cursor = overlap_end
        if end > cursor + 1e-6:
            pieces.append({"kind": "silence", "duration": end - cursor})
        return [p for p in pieces if p["duration"] > _MIN_CHUNK_SEC]

    # 断片列を 1 回の ffmpeg 実行で書き出す (ver3 resolve6 §3-6 R2 / §5.6)
    # 断片ごとに -ss/-t 付きの入力を並べ、concat フィルタで 1 本へ繋ぐ。
    # 【重要】1 入力 + atrim にしてはならない。入力シークが効かず素材の先頭から
    #         デコードするため、後方の区間では逆に数倍遅くなる (実測 7.62s / resolve6 §2.2)。
    def _render_pieces(self, pieces, out_path):
        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(self._ffmpeg_cfg)
        layout = "mono" if self._channels == 1 else "stereo"
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        filters = []
        labels = []
        for index, piece in enumerate(pieces):
            if piece["kind"] == "silence":
                cmd += ["-f", "lavfi", "-t", f"{piece['duration']:.3f}",
                        "-i", f"anullsrc=channel_layout={layout}:"
                              f"sample_rate={self._sample_rate}"]
            else:
                cmd += ["-ss", f"{piece['source_in']:.3f}",
                        "-t", f"{piece['duration']:.3f}",
                        "-i", piece["path"]]
            label = f"{index}:a"
            # ゲイン指定のある断片だけ concat の手前で音量を掛ける
            if abs(piece.get("gain_db", 0.0)) > 1e-6:
                filters.append(f"[{index}:a]volume={piece['gain_db']:g}dB[g{index}]")
                label = f"g{index}"
            labels.append(f"[{label}]")
        filters.append("".join(labels) + f"concat=n={len(pieces)}:v=0:a=1[out]")
        cmd += ["-filter_complex", ";".join(filters), "-map", "[out]",
                "-c:a", self._codec, "-ar", str(self._sample_rate),
                "-ac", str(self._channels), out_path]
        ffmpeg_runner.execute(
            cmd,
            progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(self._ffmpeg_cfg))

    # 断片ごとに ffmpeg を起動して連結する従来方式 (audio_build_mode="per_piece")
    # 切り戻し用に残す。1 プロセス方式で問題が出たときの逃げ道 (resolve6 §4-2)。
    def _render_pieces_each(self, pieces, out_path, extension):
        part_paths = []
        try:
            for index, piece in enumerate(pieces):
                part = os.path.join(
                    self._work_dir,
                    f"preview_audio_{self._sequence:04d}_{index:03d}.{extension}")
                self._render_piece(piece, part)
                part_paths.append(part)
            if len(part_paths) == 1:
                os.replace(part_paths[0], out_path)
                part_paths = []
            else:
                silence_cutter.concat_files(part_paths, out_path, self._ffmpeg_cfg)
        finally:
            self._cleanup_paths(part_paths)

    # 断片 1 つを音声ファイルとして書き出す (per_piece 方式で使う)
    def _render_piece(self, piece, out_path):
        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(self._ffmpeg_cfg)
        sample_rate = self._sample_rate
        codec = self._codec
        channels = self._channels
        layout = "mono" if channels == 1 else "stereo"
        duration = piece["duration"]

        if piece["kind"] == "silence":
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", f"anullsrc=channel_layout={layout}:sample_rate={sample_rate}",
                "-c:a", codec, "-ar", str(sample_rate), "-ac", str(channels), out_path,
            ]
        else:
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{piece['source_in']:.3f}",
                "-i", piece["path"],
                "-t", f"{duration:.3f}",
                "-vn", "-map", "0:a:0",
            ]
            if abs(piece.get("gain_db", 0.0)) > 1e-6:
                cmd += ["-af", f"volume={piece['gain_db']:g}dB"]
            cmd += ["-c:a", codec, "-ar", str(sample_rate),
                    "-ac", str(channels), out_path]

        ffmpeg_runner.execute(
            cmd, progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(self._ffmpeg_cfg))

    # 保持本数を超えたぶんを古い順に捨てる (resolve6 §3-6 R5)
    def _trim_chunks(self):
        while len(self._chunks) > self._chunk_limit:
            path, _start, _length = self._chunks.pop(0)
            self._remove_chunk_file(path)

    def _drop_chunks(self):
        chunks, self._chunks = self._chunks, []
        for path, _start, _length in chunks:
            self._remove_chunk_file(path)

    def _remove_chunk_file(self, path):
        # 再生中はロックされ得るため、削除失敗は cleanup へ委ねる
        try:
            if os.path.exists(path):
                os.remove(path)
                if path in self._generated:
                    self._generated.remove(path)
        except OSError:
            pass

    def _cleanup_paths(self, paths):
        for path in list(paths):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
