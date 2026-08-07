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
    def __init__(self, timeline, settings, work_dir, initial_audio_path=None):
        self._timeline = timeline
        self._settings = settings or {}
        self._ffmpeg_cfg = self._settings.get("ffmpeg", {})
        self._work_dir = work_dir
        self._sequence = 0
        # 生成済みチャンク: (path, start_sec, length_sec)
        self._chunk = None
        self._generated = []
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
        if self._chunk is None:
            return None
        path, chunk_start, chunk_len = self._chunk
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
        out_path = os.path.join(self._work_dir, f"preview_audio_{self._sequence:04d}.m4a")
        part_paths = []
        try:
            for index, piece in enumerate(pieces):
                part = os.path.join(
                    self._work_dir, f"preview_audio_{self._sequence:04d}_{index:03d}.m4a")
                self._render_piece(piece, part)
                part_paths.append(part)
            if len(part_paths) == 1:
                os.replace(part_paths[0], out_path)
                part_paths = []
            else:
                silence_cutter.concat_files(part_paths, out_path, self._ffmpeg_cfg)
        except Exception:  # noqa: BLE001 (音声生成の失敗で編集を止めない / §10)
            _logger.exception("プレビュー音声の生成に失敗しました")
            self._cleanup_paths(part_paths)
            return None
        finally:
            self._cleanup_paths(part_paths)

        # 古いチャンクを片づけてから差し替える
        self._drop_current_chunk()
        self._chunk = (out_path, start, length)
        self._generated.append(out_path)
        _logger.debug("音声チャンク生成: %.1fs から %.1fs", start, length)
        return out_path, start

    # Timeline が編集されたら生成済みを捨てる (以降はオンデマンド生成に切り替わる)
    def invalidate(self):
        self._initial_valid = False
        self._drop_current_chunk()

    # 生成した一時ファイルをすべて削除する (画面クローズ時)
    def cleanup(self):
        self._chunk = None
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

    # 断片 1 つを音声ファイルとして書き出す
    def _render_piece(self, piece, out_path):
        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(self._ffmpeg_cfg)
        sample_rate = self._ffmpeg_cfg.get("audio_sample_rate", 48000)
        codec = self._ffmpeg_cfg.get("audio_codec", "aac")
        duration = piece["duration"]

        if piece["kind"] == "silence":
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
                "-c:a", codec, "-ar", str(sample_rate), "-ac", "2", out_path,
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
            cmd += ["-c:a", codec, "-ar", str(sample_rate), "-ac", "2", out_path]

        ffmpeg_runner.execute(
            cmd, progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(self._ffmpeg_cfg))

    def _drop_current_chunk(self):
        if self._chunk is None:
            return
        path = self._chunk[0]
        self._chunk = None
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
