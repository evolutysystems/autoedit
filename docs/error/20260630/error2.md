# Error
2026-06-30 18:48:44,110 [ERROR] src.modules.ffmpeg_runner: FFmpeg 失敗 (code=4294967274): Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'C:\Users\evolu\AppData\Local\Temp\autoedit_kxtdl_58\silence_cut.mp4':
  Metadata:
    major_brand     : isom
    minor_version   : 512
    compatible_brands: isomiso2avc1mp41
    encoder         : Lavf62.13.100
  Duration: 00:00:11.74, start: 0.012000, bitrate: 6153 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 2560x1440 [SAR 1:1 DAR 16:9], 6018 kb/s, 58.86 fps, 240 tbr, 15360 tbn, start 0.033008 (default)
    Metadata:
      handler_name    : VideoHandler
      encoder         : Lavc62.29.100 libx264
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 132 kb/s, start 0.012000 (default)
    Metadata:
      handler_name    : SoundHandler
[Parsed_subtitles_0 @ 00000214c1b5ed00] Unable to parse "original_size" option value "/Users/evolu/AppData/Local/Temp/autoedit_kxtdl_58/subtitle.ass" as image size
[fc#-1 @ 00000214c1329dc0] Error applying option 'original_size' to filter 'subtitles': Invalid argument
Error opening output file C:\Users\evolu\AppData\Local\Temp\autoedit_kxtdl_58\subtitle.mp4.
Error opening output files: Invalid argument
2026-06-30 18:48:44,120 [ERROR] src.pipeline.pipeline_runner: パイプライン中断: 既知エラー
Traceback (most recent call last):
  File "D:\develop\autoedit\src\pipeline\pipeline_runner.py", line 57, in run_pipeline
    subtitle_generator.run(context)
  File "D:\develop\autoedit\src\modules\subtitle_generator.py", line 632, in run
    burn_subtitle(
  File "D:\develop\autoedit\src\modules\subtitle_generator.py", line 356, in burn_subtitle
    ffmpeg_runner.execute(cmd, total_duration=total_duration, on_progress=on_progress)
  File "D:\develop\autoedit\src\modules\ffmpeg_runner.py", line 94, in execute
    raise FFmpegError(
src.exceptions.FFmpegError: FFmpeg 実行失敗 (returncode=4294967274)
2026-06-30 18:48:44,120 [ERROR] __main__: パイプライン実行に失敗
Traceback (most recent call last):
  File "D:\develop\autoedit\src\gui\main_window.py", line 171, in run
    output = run_pipeline(
             ^^^^^^^^^^^^^
  File "D:\develop\autoedit\src\pipeline\pipeline_runner.py", line 57, in run_pipeline
    subtitle_generator.run(context)
  File "D:\develop\autoedit\src\modules\subtitle_generator.py", line 632, in run
    burn_subtitle(
  File "D:\develop\autoedit\src\modules\subtitle_generator.py", line 356, in burn_subtitle
    ffmpeg_runner.execute(cmd, total_duration=total_duration, on_progress=on_progress)
  File "D:\develop\autoedit\src\modules\ffmpeg_runner.py", line 94, in execute
    raise FFmpegError(
src.exceptions.FFmpegError: FFmpeg 実行失敗 (returncode=4294967274)

# Output
resolve2.mdに修正設計書を記載してください。
