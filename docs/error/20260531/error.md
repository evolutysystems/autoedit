# Error
以下のエラーが発生した。ログから抜粋。
2026-05-31 14:28:19,505 [ERROR] src.modules.ffmpeg_runner: FFmpeg 失敗 (code=4294967274): [Parsed_asetpts_3 @ 000001c8ffc34140] Media type mismatch between the 'Parsed_asetpts_3' filter output pad 0 (audio) and the 'Parsed_concat_40' filter input pad 10 (video)
[AVFilterGraph @ 000001c8ffc24cc0] Error linking filters
Error : Invalid argument

# 使用した動画詳細
- 解像度(2560 * 1080)
- 50秒の動画

# 実行方法
- コマンドプロンプトからpython main.pyを実行