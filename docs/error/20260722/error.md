# Error
以前の対応で動画前半のズレはなくなりますが、30分の動画であれば8分経過あたりから徐々にズレ始めていきます。
今回は映像と音声はズレていませんが、テロップがずれていきます。

以下の対策を既に行っているのか、また有効なのか教えてください。
- -ssの位置
    ・悪い例
        ffmpeg -ss 00:10:00 -i input.mp4 ...
    入力前に-ssを書くと高速ですが、キーフレーム単位になるため数百ms～数秒ずれることがあります。
    ・正確
        ffmpeg -i input.mp4 -ss 00:10:00 -to 00:11:00 ...
    入力後に書くことでフレーム精度になります。

- 毎回エンコードしない
    例えば
        元動画→カット→結合→字幕→リサイズ→エンコード
    このように5回エンコードすると
    PTS
    DTS
    Timebase
    が少しずつ崩れる可能性がある。
    理想は
    元動画→全編集(Filter)→最後に1回だけエンコード

- setpts / asetpts
    映像だけ setpts=PTS-STARTPTS して
    音声を asetpts していないケース。
    例えば
        -vf setpts=PTS-STARTPTS
    だけだと音がずれる。
    必ず
        -vf setpts=PTS-STARTPTS
        -af asetpts=PTS-STARTPTS
    をセットで。

- concat demuxer
    動画を結合するとき
        concat:
    ではなく
    concat demuxer
    を使います。
        file part1.mp4
        file part2.mp4
        file part3.mp4
        ffmpeg -f concat -safe 0 -i list.txt -c copy output.mp4

- -copyts
    PTSを維持したいなら
    -copyts
    が有効な場合があります

- -avoid_negative_ts
    -avoid_negative_ts make_zero
    これだけで改善するケースがあります。

# Output
resolve.mdを対策設計書として出力してください。