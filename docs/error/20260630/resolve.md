# テロップ焼き込み(burn_subtitle)が muxer エラー(-22)で失敗する不具合 修正設計書

対象エラー: `docs/error/20260630/error.md`
対象ソース: `src/modules/silence_cutter.py`（主因: 無音カット出力のタイムスタンプ破損）/ `src/modules/subtitle_generator.py`（顕在化点: 焼き込み再エンコード）/ `src/modules/ffmpeg_runner.py`・`src/gui/main_window.py`（診断補強）
本書はレビュー用の**修正設計書**であり、実装は承認後に行う（`docs/CLAUDE.md` の方針に準拠）。

> 直近の `docs/error/20260626〜0628` は音声認識（Whisper / CUDA）側を扱った。本書は音声認識成功後の「ASS 焼き込み」での FFmpeg 異常終了を扱う。

> **【重要・前回診断の訂正】** error.md に FFmpeg の完全な stderr が追記されたことで、当初疑った「`subtitles` フィルタのパスエスケープ不備」は**否定された**。ログ `fontselect: (Yu Gothic UI, 700, 0) -> YuGothicUI-Bold` の通り **ASS は正常に開かれフォントも解決済**で、焼き込みは**11分以上エンコードが進行**している。真因は別（後述）。

---

## 1. 事象

音声認識で字幕一覧を確定した後、フルテロップ生成（焼き込み）で FFmpeg が `returncode=4294967274`（= `−22` = `EINVAL`）で失敗する。stderr 末尾（`ffmpeg_runner.py:83` のログ）に決定的な手掛かりがある。

```
[Parsed_subtitles_0] fontselect: (Yu Gothic UI, 700, 0) -> YuGothicUI-Bold ...   ← 字幕は正常ロード
[mp4]  Application provided duration: 245404385355 in stream 0 is invalid          ← 異常な duration
[vost#0:0/libx264]  Error submitting a packet to the muxer: Invalid argument
[out#0/mp4]  Error muxing a packet
[out#0/mp4]  Task finished with error code: -22 (Invalid argument)
frame= 4525 fps=6.5 ... time=00:11:09.36 ... dup=0 drop=35074 speed=0.956x       ← 35074 フレーム脱落
```

注目点は2つ。
1. **`Application provided duration: 245404385355 in stream 0 is invalid`** … 映像(stream 0)のパケット duration が異常値で、mp4 muxer が `EINVAL` で拒否。
2. **`drop=35074`** … 出力フレーム `4525` に対し脱落が **35074**。総処理フレーム ≒ `4525 + 35074 ≒ 39600 ≒ 60fps × 660秒` で、**入力フレームの約9割が vsync で脱落**している。

> （補足）`subtitle:0KiB` は焼き込み(描画)方式では正常。字幕は映像へ焼かれ独立ストリームを持たない。

---

## 2. 原因調査

### 2-1. returncode の解読

`4294967274` は符号なし32bitで `−22` = `AVERROR(EINVAL)`。FFmpeg は muxer がパケットを受理できないと `EINVAL` を返し `Conversion failed!` で終了する。

### 2-2. 結論（主因）：無音カット出力 `silence_cut.mp4` のタイムスタンプが破損している

焼き込みは `silence_cut.mp4` を入力に再エンコードする（`subtitle_generator.py:574, 587, 620`）。この入力動画の**映像タイムスタンプ(PTS/DTS)が非単調・無効**であるため、

- 再エンコード時の vsync 変換で**大量フレームが脱落**（`drop=35074`）、
- 終盤で**異常な duration(245404385355) を持つパケット**が mp4 muxer に渡り `EINVAL` で停止する。

字幕焼き込み(`-vf subtitles`)自体は無関係（フォント解決済・11分進行）であり、**入力の TS 破損を再エンコードが顕在化させた**のが事象の本質。

### 2-3. なぜ `silence_cut.mp4` の TS が壊れるか（生成経路）

`setting.json` は `silence_cut.extract_mode="seek"`（既定）かつ `concat_reencode=false`。よって `silence_cutter.run` は **`cut_and_concat_seek`** 経路を通る（`silence_cutter.py:426-433`）。処理は2段。

1. **区間抽出 `extract_segment`（L172-209）**
   各有音区間を `-ss start -i input -t dur` で**再エンコード**して `silence_seg_xxxxx.mp4` を作る。ただし**CFR(固定フレームレート)を強制していない**（`-r` / `-fps_mode` なし）。`build_encode_options`（`ffmpeg_runner.py:94-100`）も `-c:v/-preset/-crf/-c:a` のみで**フレームレート指定を持たない**。
2. **連結 `_concat_demux`（L329-362）**
   抽出済み区間を concat デマルチプレクサで結合するが、`concat_reencode=false` のため **`-c copy`（ストリームコピー）**。各区間 mp4 の TS をそのまま継ぎ、`-fflags +genpts` や `-avoid_negative_ts` 等の**連結時 TS 正規化を行っていない**。

この組み合わせ（**VFR を含み得るソース → CFR 非強制の区間再エンコード → ストリームコピー連結**）は、区間境界で **TS の不連続・非単調・duration 異常**を生みやすい。ストリームコピー連結はパケットを検証せず通すため `silence_cut.mp4` 生成時には露見せず、**最初の全長再エンコードである焼き込み**で初めて muxer エラーとして顕在化する。

### 2-4. 寄与要因：ソースが 60fps の配信クリップ（VFR の可能性）

`ffmpeg.output_fps=60`、入力は `D:/StreamPipeline/dev` 由来の配信切り抜き。OBS 等の配信収録は **VFR(可変フレームレート)** であることが多い。VFR 区間を CFR 化せずに抽出・連結すると、上記 TS 破損が増幅し、再エンコードの CFR 変換で**大量フレーム脱落(drop=35074)**として現れる。これは観測ログと整合する。

### 2-5. 影響範囲（テロップ OFF でも危険）

`subtitle.enabled=false` 等でテロップ工程をスキップした場合、`silence_cut.mp4` は**そのまま OP/ED 結合（`concat_processor`）・最終出力へ流れる**。`concat_processor.concat` は再エンコードするため、そこでも同じ muxer エラーが起き得る。よって**根治は無音カット側（生成元）で行うのが本筋**で、焼き込み側は止血（防御）と位置付ける。

---

## 3. 修正方針

「焼き込み側で確実に止血（対策A）」＋「無音カット側で根治（対策B）」の二層。いずれも `docs/CLAUDE.md` の方針（既存破壊なし・不要ライブラリなし・値は `setting.json` 管理）に従う。

### 対策A（主・即時止血）：焼き込み再エンコードでタイムスタンプを再生成する

`burn_subtitle`（`subtitle_generator.py:328-345`）の再エンコードを、入力 TS に依存しない**クリーンな CFR 出力**に固定する。既に再エンコードしているため**追加コストはほぼ無し**。候補オプション（いずれも `setting.json` の `ffmpeg` 値を使用しハードコード回避）。

| 目的 | 追加オプション | 位置 |
|---|---|---|
| 欠落 PTS の再生成 | `-fflags +genpts` | 入力(`-i`)の前 |
| CFR 化で有効 duration を保証 | `-fps_mode cfr`（旧 `-vsync cfr`）/ `-r {ffmpeg.output_fps}` | 出力側 |
| （壊れ TS が残る場合の確実策）映像 PTS をフレーム番号から再構築 | `-vf "subtitles=...,setpts=N/FRAME_RATE/TB"` | フィルタ末尾 |
| 同・音声を再構築 | `-af asetpts=N/SR/TB`（または `-af aresample=async=1:first_pts=0`） | 出力側 |

- **最小案**：`-fflags +genpts` ＋ `-fps_mode cfr -r {output_fps}`。これで「invalid duration」と「大量 drop」の双方を解消できる見込み。
- **確実案**：上記に加え、フィルタ末尾 `setpts=N/FRAME_RATE/TB` ＋ 音声 `asetpts` で TS を完全再構築（VFR/壊れ TS に最も強い。CFR 前提）。
- どこまで付与するかは §6-1（実機検証）で確定する。`output_fps` は既存設定を参照する。

### 対策B（根治）：無音カット出力を健全な CFR 動画にする

`silence_cut.mp4` 自体を TS 健全・CFR で生成し、下流（焼き込み/OP・ED結合/出力）すべてを安定化する。候補（いずれかの組合せ）。

1. **区間抽出で CFR 強制**（推奨・根治の中心）
   `extract_segment`（L181-202）の出力に `-fps_mode cfr -r {ffmpeg.output_fps}`（または `-vsync cfr`）を付与し、各 `silence_seg_*.mp4` を 60fps CFR に固定。`build_encode_options` 側へ `-r`/`-fps_mode` を追加する案も可（ただし silencedetect 等への影響範囲を確認）。
2. **連結時の TS 正規化**
   `_concat_demux`（L344-353）のコピー経路に `-fflags +genpts`・`-avoid_negative_ts make_zero`・`-max_interleave_delta 0` を付与。あるいは `silence_cut.concat_reencode=true` を既定化（コスト増のためレビュー判断）。
3. **filter 方式への切替（回避策）**
   `extract_mode="filter"`（`cut_and_concat`/`cut_and_concat_batched`）はフィルタ内 `setpts=PTS-STARTPTS`＋`concat` で単一再エンコードするため TS が単調になりやすい。seek 方式固有の問題であれば運用回避として有効（既存実装で対応可）。

> 対策A だけでも本エラーは止まる見込みだが、テロップ OFF 経路（§2-5）まで守るには対策B が要る。**A を先行実装（即効）→ B を根治**の順を推奨。

### 対策C（補強・診断）：FFmpeg 実エラー行をユーザー通知へ
今回は `stderr_tail`（`progress.py:54`／`ffmpeg_runner.py:83`）がログに出ていたため原因特定できた。GUI 通知（`main_window.py:169` は returncode のみ）にも `FFmpegError.stderr_tail` の要点を併記し、再発時の切り分けを即時化する（前回方針踏襲）。根治ではないが運用価値が高い。

---

## 4. 変更対象

| ファイル | 変更概要 | 区分 |
|---|---|---|
| `src/modules/subtitle_generator.py` | `burn_subtitle` の再エンコードに TS 再生成/CFR 化オプションを付与（対策A）。`output_fps` 等は `ffmpeg` 設定から取得 | 主A |
| `src/modules/silence_cutter.py` | `extract_segment` の CFR 強制、`_concat_demux` の TS 正規化（対策B）。`extract_mode`/`concat_reencode` の扱いは現行キーを踏襲 | 根治B |
| `src/modules/ffmpeg_runner.py` | （任意）`build_encode_options` に `-r`/`-fps_mode` を含めるか、または fps 系を別ヘルパ化。`execute` の `FFmpegError` 文言へ `stderr_tail` 要点付加（対策C） | 補強 |
| `src/gui/main_window.py` | （任意）`failed.emit` に `stderr_tail` 併記（対策C） | 補強 |

- 設定キーの新規追加は基本不要（`ffmpeg.output_fps` 既存値を活用）。必要が生じた場合のみ `setting.json` と `DEFAULT_SETTINGS` へ同値追加し後方互換を保つ。
- `subtitles` フィルタの**パスエスケープ（`subtitle_generator.py:331-332`）は変更不要**（本事象の原因ではない／正常動作を確認）。

---

## 5. エラー処理・ログ方針

- 焼き込み・連結失敗時は従来どおり `FFmpegError` を上位へ送出（挙動不変）。対策C で実エラー行を可視化。
- 対策A/B 適用後、`drop`（脱落フレーム数）が大幅に減ることを INFO/デバッグで確認できるよう、必要なら進捗ログに `drop`/`dup` を出力（任意・過剰実装は避ける）。
- CFR 強制で**尺がわずかに変わる**可能性（VFR→CFR 補間）を許容範囲とし、音ズレが出ないこと（`asetpts`/`aresample` 併用）を検証で担保する。

---

## 6. 不明点・確認事項（レビュー対象）

1. **対策A の付与範囲** → `-fflags +genpts`＋`-fps_mode cfr -r {output_fps}` の「最小案」で解消するか、`setpts/asetpts` 再構築まで要する「確実案」が必要か。**実機 stderr で `drop` が消え muxer エラーが出ないことを確認して確定**。
2. **VFR 実態の確認** → 入力（`StreamPipeline/dev` の収録）が実際に VFR か、`ffprobe`（`r_frame_rate` vs `avg_frame_rate`、`vfr` 判定）で確認。VFR なら CFR 強制（対策B-1）を根治の中心に据える。
3. **CFR を入れる位置** → 区間抽出時（`extract_segment`）か、連結後一括か、焼き込み時か。最も上流（抽出時）が根治だが、`silencedetect`/進捗への副作用が無いか確認。
4. **`concat_reencode=true` 既定化の可否** → コスト増（連結も再エンコード）と安定性のトレードオフ。既定は現行 `false` 維持＋対策A/B-1 で足りるかを優先検討。
5. **対策C の採否** → ログのみで足りるか、GUI 通知にも実エラー行を出すか。
6. **「invalid duration 245404385355」の発生位置** → 終盤1パケットのみか全域か。`-fflags +genpts` 等で解消するか、特定区間（最終区間/極短区間）固有かを切り分け。

---

## 7. 検証方針

1. **再現**：同一素材（`silence_cut.mp4` 相当）で焼き込みを実行し、`returncode=4294967274`／`Application provided duration ... invalid`／`drop=35074` 規模を再現。
2. **対策A**：TS 再生成/CFR オプション付与後、焼き込みが**完走**し `drop` がほぼ 0、muxer エラーが消えることを確認。生成コマンドを `progress.py:41` のデバッグログで確認。
3. **対策B**：`silence_cut.mp4` を `ffprobe` で検査し、PTS 単調・CFR(60fps)・duration 正常であることを確認。
4. **音ズレ**：CFR 化後に映像/音声が同期していること（先頭・末尾・カット境界）を実視聴で確認。
5. **テロップ OFF 経路**：`subtitle.enabled=false` で `silence_cut.mp4`→OP/ED結合→出力が完走すること（§2-5）を確認。
6. **回帰**：無音カット結果・字幕表示タイミング・最終尺が従来想定どおりであることを確認。
