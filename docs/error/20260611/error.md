# Error Detail
2026-06-10 18:05:39,043 [INFO] autoedit: 処理開始: D:/develop/autoedit/input\stream.mp4
2026-06-10 18:05:39,043 [INFO] src.pipeline.pipeline_runner: ==================================================
2026-06-10 18:05:39,043 [INFO] src.pipeline.pipeline_runner: パイプライン開始: D:/develop/autoedit/input\stream.mp4
2026-06-10 18:05:39,059 [INFO] src.modules.silence_cutter: 無音カット開始: threshold=-26dB, min=0.600s, fade=False
2026-06-10 18:18:53,597 [INFO] src.modules.silence_cutter: 検出された無音区間: 1159 件
2026-06-10 18:18:53,609 [INFO] src.modules.silence_cutter: 無音カットをバッチ分割: 1156 区間 → 24 バッチ (size=50)
2026-06-10 20:11:49,776 [ERROR] src.utils.progress: FFmpeg が 600 秒間 進捗を更新しなかったため強制終了しました
2026-06-10 20:11:49,776 [ERROR] src.modules.ffmpeg_runner: FFmpeg 失敗 (code=1):   Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  Stream #0:1 (aac) -> atrim:default
  concat -> Stream #0:0 (libx264)
  concat -> Stream #0:1 (aac)
Press [q] to stop, [?] for help
2026-06-10 20:11:49,941 [ERROR] src.pipeline.pipeline_runner: パイプライン中断: 既知エラー
Traceback (most recent call last):
  File "D:\develop\autoedit\src\pipeline\pipeline_runner.py", line 38, in run_pipeline
    silence_cutter.run(context)
  File "D:\develop\autoedit\src\modules\silence_cutter.py", line 329, in run
    cut_and_concat_batched(
  File "D:\develop\autoedit\src\modules\silence_cutter.py", line 214, in cut_and_concat_batched
    cut_and_concat(
  File "D:\develop\autoedit\src\modules\silence_cutter.py", line 158, in cut_and_concat
    ffmpeg_runner.execute(
  File "D:\develop\autoedit\src\modules\ffmpeg_runner.py", line 84, in execute
    raise FFmpegError(
src.exceptions.FFmpegError: FFmpeg 実行失敗 (returncode=1)
2026-06-10 20:11:49,941 [ERROR] autoedit: 処理失敗: D:/develop/autoedit/input\stream.mp4
Traceback (most recent call last):
  File "D:\develop\autoedit\src\main.py", line 81, in main
    output = run_pipeline(path, settings, progress_cb=_console_progress)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\develop\autoedit\src\pipeline\pipeline_runner.py", line 38, in run_pipeline
    silence_cutter.run(context)
  File "D:\develop\autoedit\src\modules\silence_cutter.py", line 329, in run
    cut_and_concat_batched(
  File "D:\develop\autoedit\src\modules\silence_cutter.py", line 214, in cut_and_concat_batched
    cut_and_concat(
  File "D:\develop\autoedit\src\modules\silence_cutter.py", line 158, in cut_and_concat
    ffmpeg_runner.execute(
  File "D:\develop\autoedit\src\modules\ffmpeg_runner.py", line 84, in execute
    raise FFmpegError(
src.exceptions.FFmpegError: FFmpeg 実行失敗 (returncode=1)

# Output
- resolve.md