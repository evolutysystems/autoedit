# Error
音声認識で字幕一覧を出力して決定した後に以下のエラーがでました。
FFmpeg 実行失敗(returncode = 4294967274)

logは以下です。
\autoedit_stswqgmz\silence_cut.mp4
2026-06-30 15:16:32,739 [INFO] src.modules.subtitle_generator: 音声認識開始 (model=large-v3, device=cpu, compute_type=int8, 設定=cpu)
2026-06-30 15:16:32,739 [INFO] src.modules.subtitle_generator: 音声認識実行 (device=cpu, compute_type=int8)
2026-06-30 15:16:42,365 [INFO] src.modules.subtitle_generator: VAD フィルタ有効 (min_silence=500ms, condition_on_previous_text=False)
2026-06-30 16:12:40,199 [INFO] src.modules.subtitle_generator: 音声認識完了 (998 セグメント)
2026-06-30 16:12:41,733 [INFO] src.modules.subtitle_generator: テロップ表示タイミング整形: 998 → 998 区間 (max_hold=2.0s, min_dur=0.5s)
2026-06-30 17:12:25,838 [ERROR] src.modules.ffmpeg_runner: FFmpeg 失敗 (code=4294967274): [Parsed_subtitles_0 @ 0000012b7f9918c0] Using font provider directwrite (with GDI)
[Parsed_subtitles_0 @ 0000012b7f9918c0] fontselect: (Yu Gothic UI, 700, 0) -> YuGothicUI-Bold, 1, YuGothicUI-Bold
[mp4 @ 0000012b7faa5d00] Application provided duration: 245404385355 in stream 0 is invalid
[vost#0:0/libx264 @ 0000012b7faa6600] Error submitting a packet to the muxer: Invalid argument
    Last message repeated 1 times
[out#0/mp4 @ 0000012b7faa5c00] Error muxing a packet
[out#0/mp4 @ 0000012b7faa5c00] Task finished with error code: -22 (Invalid argument)
[out#0/mp4 @ 0000012b7faa5c00] Terminating thread with return code -22 (Invalid argument)
[out#0/mp4 @ 0000012b7faa5c00] video:151305KiB audio:11056KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: 0.096804%
frame= 4525 fps=6.5 q=-1.0 Lsize=  162518KiB time=00:11:09.36 bitrate=1989.0kbits/s dup=0 drop=35074 speed=0.956x elapsed=0:11:40.51    
[libx264 @ 0000012b7f63b740] frame I:21    Avg QP:14.69  size:190871
[libx264 @ 0000012b7f63b740] frame P:1192  Avg QP:17.64  size: 67224
[libx264 @ 0000012b7f63b740] frame B:3388  Avg QP:19.28  size: 22016
[libx264 @ 0000012b7f63b740] consecutive B-frames:  1.0%  1.7%  1.9% 95.4%
[libx264 @ 0000012b7f63b740] mb I  I16..4: 13.7% 69.8% 16.4%
[libx264 @ 0000012b7f63b740] mb P  I16..4:  4.7% 14.8%  1.3%  P16..4: 41.4% 15.3%  7.5%  0.0%  0.0%    skip:15.0%
[libx264 @ 0000012b7f63b740] mb B  I16..4:  0.9%  2.0%  0.1%  B16..8: 39.9%  4.3%  0.5%  direct: 5.6%  skip:46.9%  L0:48.1% L1:48.7% BI: 3.2%
[libx264 @ 0000012b7f63b740] 8x8 transform intra:70.3% inter:84.1%
[libx264 @ 0000012b7f63b740] coded y,uvDC,uvAC intra: 39.9% 42.2% 10.5% inter: 16.5% 21.1% 0.3%
[libx264 @ 0000012b7f63b740] i16 v,h,dc,p: 34% 21% 14% 31%
[libx264 @ 0000012b7f63b740] i8 v,h,dc,ddl,ddr,vr,hd,vl,hu: 25% 16% 34%  4%  5%  6%  4%  4%  3%
[libx264 @ 0000012b7f63b740] i4 v,h,dc,ddl,ddr,vr,hd,vl,hu: 35% 22% 14%  4%  6%  6%  5%  4%  3%
[libx264 @ 0000012b7f63b740] i8c dc,h,v,p: 56% 19% 21%  4%
[libx264 @ 0000012b7f63b740] Weighted P-Frames: Y:9.9% UV:1.4%
[libx264 @ 0000012b7f63b740] ref P L0: 55.0% 10.3% 23.9% 10.3%  0.6%
[libx264 @ 0000012b7f63b740] ref B L0: 81.7% 14.5%  3.8%
[libx264 @ 0000012b7f63b740] ref B L1: 93.2%  6.8%
[libx264 @ 0000012b7f63b740] kb/s:1886.67
[aac @ 0000012b7fa83dc0] Qavg: 946.858

Conversion failed!
2026-06-30 17:12:25,838 [ERROR] src.pipeline.pipeline_runner: パイプライン中断: 既知エラー
Traceback (most recent call last):
  File "D:\develop\autoedit\src\pipeline\pipeline_runner.py", line 57, in run_pipeline
    subtitle_generator.run(context)
  File "D:\develop\autoedit\src\modules\subtitle_generator.py", line 620, in run
    burn_subtitle(
  File "D:\develop\autoedit\src\modules\subtitle_generator.py", line 344, in burn_subtitle
    ffmpeg_runner.execute(cmd, total_duration=total_duration, on_progress=on_progress)
  File "D:\develop\autoedit\src\modules\ffmpeg_runner.py", line 84, in execute
    raise FFmpegError(
src.exceptions.FFmpegError: FFmpeg 実行失敗 (returncode=4294967274)
2026-06-30 17:12:26,184 [ERROR] __main__: パイプライン実行に失敗
Traceback (most recent call last):
  File "D:\develop\autoedit\src\gui\main_window.py", line 157, in run
    output = run_pipeline(
             ^^^^^^^^^^^^^
  File "D:\develop\autoedit\src\pipeline\pipeline_runner.py", line 57, in run_pipeline
    subtitle_generator.run(context)
  File "D:\develop\autoedit\src\modules\subtitle_generator.py", line 620, in run
    burn_subtitle(
  File "D:\develop\autoedit\src\modules\subtitle_generator.py", line 344, in burn_subtitle
    ffmpeg_runner.execute(cmd, total_duration=total_duration, on_progress=on_progress)
  File "D:\develop\autoedit\src\modules\ffmpeg_runner.py", line 84, in execute
    raise FFmpegError(
src.exceptions.FFmpegError: FFmpeg 実行失敗 (returncode=4294967274)

# Output
resolve.mdを出力して修正設計書を作成してください。