# Error Detail
オープニ ング結合2026-06-02 09:38:30,949 [ERROR] src.modules.ffmpeg_runner: FFmpeg 失敗 (code=4294967274):   Duration: 00:00:13.30, start: 0.000000, bitrate: 7100 kb/s
  Stream #1:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 2560x1440 [SAR 1:1 DAR 16:9], 6953 kb/s, 59.85 fps, 60 tbr, 15360 tbn (default)
    Metadata:
      handler_name    : VideoHandler
      encoder         : Lavc62.29.100 libx264     
  Stream #1:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 132 kb/s (default)
    Metadata:
      handler_name    : SoundHandler
Stream mapping:
  Stream #0:0 (h264) -> concat
  Stream #0:1 (aac) -> concat
  Stream #1:0 (h264) -> concat
  Stream #1:1 (aac) -> concat
  concat -> Stream #0:0 (libx264)
  concat -> Stream #0:1 (aac)
Press [q] to stop, [?] for help
[Parsed_concat_0 @ 000002c3d0460c40] Input link in0:v0 parameters (size 2560x1440, SAR 1:1) do not match the corresponding output link in0:v0 parameters (1920x1080, SAR 1:1)
[Parsed_concat_0 @ 000002c3d0460c40] Failed to configure output pad on Parsed_concat_0
[fc#0 @ 000002c3cf6486c0] Error reinitializing filters!
[fc#0 @ 000002c3cf6486c0] Task finished with error code: -22 (Invalid argument)
[fc#0 @ 000002c3cf6486c0] Terminating thread with return code -22 (Invalid argument)
[aost#0:1/aac @ 000002c3d0280480] [enc:aac @ 000002c3cf8da700] Could not open encoder before EOF    
[aost#0:1/aac @ 000002c3d0280480] Task finished with error code: -22 (Invalid argument)
[aost#0:1/aac @ 000002c3d0280480] Terminating thread with return code -22 (Invalid argument)        
[vost#0:0/libx264 @ 000002c3cfd129c0] [enc:libx264 @ 000002c3cf8dbe40] Could not open encoder before EOF
[vost#0:0/libx264 @ 000002c3cfd129c0] Task finished with error code: -22 (Invalid argument)
[vost#0:0/libx264 @ 000002c3cfd129c0] Terminating thread with return code -22 (Invalid argument)    
[out#0/mp4 @ 000002c3cfd8ae80] Nothing was written into output file, because at least one of its streams received no packets.
frame=    0 fps=0.0 q=0.0 Lsize=       0KiB time=N/A bitrate=N/A speed=N/A elapsed=0:00:00.08       
Conversion failed!

# Output
- resolve2.md
