# Error Detail
2026-06-10 12:43:21,270 [INFO] autoedit: 処理対象: 1 件
2026-06-10 12:43:21,270 [INFO] autoedit: 処理開始: D:/develop/autoedit/input\stream.mp4
2026-06-10 12:43:21,270 [INFO] src.pipeline.pipeline_runner: ==================================================
2026-06-10 12:43:21,270 [INFO] src.pipeline.pipeline_runner: パイプライン開始: D:/develop/autoedit/input\stream.mp4
2026-06-10 12:43:21,279 [INFO] src.modules.silence_cutter: 無音カット開始: threshold=-26dB, min=0.600s, fade=False
2026-06-10 12:55:51,160 [INFO] src.modules.silence_cutter: 検出された無音区間: 1159 件
2026-06-10 12:55:51,188 [ERROR] src.pipeline.pipeline_runner: パイプライン中断: 想定外エラー
Traceback (most recent call last):
    File "D:\develop\autoedit\src\pipeline\pipeline_runner.py", line 38, in run_pipeline
    silence_cutter.run(context)
    File "D:\develop\autoedit\src\modules\silence_cutter.py", line 190, in run
    cut_and_concat(
    File "D:\develop\autoedit\src\modules\silence_cutter.py", line 151, in cut_and_concat
    ffmpeg_runner.execute(cmd, total_duration=effective_duration, on_progress=on_progress)
    File "D:\develop\autoedit\src\modules\ffmpeg_runner.py", line 67, in execute
    returncode, stderr_tail = run_ffmpeg_progress(
                              ^^^^^^^^^^^^^^^^^^^^
    File "D:\develop\autoedit\src\utils\progress.py", line 24, in run_ffmpeg_progress
    process = subprocess.Popen(
              ^^^^^^^^^^^^^^^^^
    File "C:\Users\evolu\AppData\Local\Programs\Python\Python312\Lib\subprocess.py", line 1026, in __init__
    self._execute_child(args, executable, preexec_fn, close_fds,
    File "C:\Users\evolu\AppData\Local\Programs\Python\Python312\Lib\subprocess.py", line 1538, in _execute_child
    hp, ht, pid, tid = _winapi.CreateProcess(executable, args,
                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
FileNotFoundError: [WinError 206] ファイル名または拡張子が長すぎます。

# Video Detail
- 2時間超えの配信アーカイブ

# OutPut
- resolve.md