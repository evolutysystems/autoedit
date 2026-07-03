# エラー解決報告 (resolve2)

## 1. 概要
| 項目 | 内容 |
| --- | --- |
| 発生日 | 2026-06-02 |
| 例外種別 | `FFmpegError` (FFmpeg returncode=4294967274 = -22 / EINVAL) |
| 発生箇所 | `src/modules/concat_processor.py` `concat()` (37行目の `filter_spec`) |
| 処理工程 | オープニング結合 (`concat_processor.run` / `position="opening"`) |
| 影響 | concat フィルタ初期化に失敗し、結合動画が1フレームも出力されずパイプライン中断 |

## 2. FFmpeg のエラー核心
```
[Parsed_concat_0] Input link in0:v0 parameters (size 2560x1440, SAR 1:1)
  do not match the corresponding output link in0:v0 parameters (1920x1080, SAR 1:1)
[Parsed_concat_0] Failed to configure output pad on Parsed_concat_0
[fc#0] Error reinitializing filters!  (code -22 / Invalid argument)
[out#0/mp4] Nothing was written into output file ...
```
- 入力 #1 (本編側) の映像が **1920x1080**
- 入力 #0 (オープニング `op.mp4`) の映像が **2560x1440** ([SAR 1:1 DAR 16:9], 60fps)
- 後続の `aac`/`libx264` の "Could not open encoder before EOF" は、
  フィルタ初期化失敗によりパケットが1つも流れなかった**二次的影響**であり原因ではない。

## 3. 原因
`concat` フィルタ (`concat=n=N:v=1:a=1`) は、
**全セグメントの映像が同一の解像度・SAR (・pix_fmt・フレームレート)** であることを要求する。
仕様上、解像度が異なる入力を自動でスケーリングしない。

現行コードは入力を無加工のまま concat に流している。

```python
# src/modules/concat_processor.py:36-37
streams = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
filter_spec = f"{streams}concat=n={n}:v=1:a=1[outv][outa]"
```

オープニング(2560x1440)と本編(1920x1080)で解像度が食い違うため、
concat が出力パッドを構成できず `Failed to configure output pad` → `-22` で終了した。

> 補足: ファイル冒頭 (3行目) の
> 「解像度/コーデックの差異に強くするため concat **filter** を使用」というコメントは**誤認**。
> concat filter は demuxer と異なりコーデック差異は吸収するが、
> **解像度・SAR の差異は吸収しない**（一致が前提）。

## 4. 修正方針
concat へ渡す前に、**各入力映像をアスペクト比維持で目標解像度へ scale + pad し、
SAR・フレームレート・pix_fmt を統一**する。音声も sample_rate / channel_layout を統一する。
目標解像度はハードコードせず `setting.json` の `ffmpeg` セクションから取得する
（CLAUDE.md「ハードコード禁止」「設定可能な値は setting.json で管理」に準拠）。

### 4.1 setting.json への追加 (ffmpeg セクション)
```jsonc
"ffmpeg": {
    "executable": "ffmpeg",
    "ffprobe_executable": "ffprobe",
    "video_codec": "libx264",
    "audio_codec": "aac",
    "preset": "medium",
    "crf": 20,
    "output_width": 1920,      // 追加: 結合時の統一解像度(幅)
    "output_height": 1080,     // 追加: 結合時の統一解像度(高さ)
    "output_fps": 60,          // 追加: 統一フレームレート
    "audio_sample_rate": 48000 // 追加: 統一サンプリングレート
}
```
- 既存キーは変更しないため後方互換を維持。
- コード側は `.get(..., 既定値)` で取得し、setting.json 未更新でも動作する。

### 4.2 `concat()` の修正 (`src/modules/concat_processor.py`)

```python
# 修正後: concat 前に全入力を統一解像度/SAR/fps/音声形式へ正規化する
def concat(parts, output_path, ffmpeg_settings, total_duration=0.0, on_progress=None):
    if len(parts) < 2:
        raise InputError("結合対象が2つ未満です")

    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)

    inputs = []
    for path in parts:
        inputs.extend(["-i", path])

    n = len(parts)
    # 結合時の統一パラメータを setting.json から取得 (ハードコード回避)
    width = ffmpeg_settings.get("output_width", 1920)
    height = ffmpeg_settings.get("output_height", 1080)
    fps = ffmpeg_settings.get("output_fps", 60)
    sample_rate = ffmpeg_settings.get("audio_sample_rate", 48000)

    # 各入力を正規化: 映像はアスペクト比維持でscale→pad→SAR/fps/pix_fmt統一、
    # 音声はsample_rate/channel_layoutを統一する。concat は全入力同一形式が前提。
    chains = []
    concat_labels = ""
    for i in range(n):
        chains.append(
            f"[{i}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
        chains.append(
            f"[{i}:a:0]aformat=sample_rates={sample_rate}:channel_layouts=stereo,"
            f"asetpts=N/SR/TB[a{i}]"
        )
        concat_labels += f"[v{i}][a{i}]"

    filter_spec = (
        ";".join(chains)
        + f";{concat_labels}concat=n={n}:v=1:a=1[outv][outa]"
    )

    cmd = [
        ffmpeg, "-y", "-hide_banner",
        *inputs,
        "-filter_complex", filter_spec,
        "-map", "[outv]", "-map", "[outa]",
        *ffmpeg_runner.build_encode_options(ffmpeg_settings),
        output_path,
    ]
    ffmpeg_runner.execute(cmd, total_duration=total_duration, on_progress=on_progress)
    return output_path
```

### 4.3 各フィルタの役割
| フィルタ | 役割 |
| --- | --- |
| `scale=W:H:force_original_aspect_ratio=decrease` | アスペクト比を保ったまま枠内に収まる最大サイズへ縮小（歪み防止） |
| `pad=W:H:(ow-iw)/2:(oh-ih)/2` | 余白を黒で中央パディングし、全入力を厳密に W×H へ揃える |
| `setsar=1` | SAR(画素アスペクト比)を 1:1 に統一（SAR不一致でも concat は失敗するため） |
| `fps=60` | フレームレートを統一（混在時の同期ズレ防止） |
| `format=yuv420p` | pix_fmt を統一 |
| `aformat=sample_rates=48000:channel_layouts=stereo` | 音声サンプルレート/チャンネルを統一 |
| `asetpts=N/SR/TB` | 音声 PTS を再生成し結合境界での乱れを防止 |

## 5. 修正対象ファイル
- `src/modules/concat_processor.py` — `concat()` 関数 (映像/音声の正規化処理を追加)
- `src/modules/concat_processor.py` — 3行目コメントを実態に合わせ修正
  （「解像度差異に強い」→「解像度はscale/padで明示的に統一する」）
- `src/settings/setting.json` — `ffmpeg` に `output_width`/`output_height`/`output_fps`/`audio_sample_rate` を追加
- (任意) `src/settings/settings_window.py` — 上記4項目を設定画面へ反映

## 6. 確認手順
1. `op.mp4`(2560x1440) と本編(1920x1080) を用いて `python main.py` を再実行する。
2. オープニング結合工程がエラーなく完了し、`opening_merged.mp4` が生成されることを確認する。
3. 出力動画でオープニング部が黒帯付き(レターボックス/ピラーボックス)で
   1920x1080 に正規化され、本編と解像度が揃って連続再生されることを確認する。
4. 音声が結合境界で途切れず同期していることを確認する。
5. エンディング結合 (`position="ending"`) でも同様に動作することを確認する。

## 7. 再発防止
- concat フィルタ使用箇所では、入力の **解像度・SAR・fps・pix_fmt・音声形式** を
  事前に統一することを必須とする（差異は自動吸収されない）。
- 統一解像度は setting.json で一元管理し、素材差し替え時もコード変更不要とする。
- 将来、入力に音声トラックが無い素材が来た場合は `[i:a:0]` 参照で別エラーとなり得るため、
  `ffprobe` で音声有無を判定し `v=1:a=0` 分岐する設計を検討する
  （設計書のエラー処理方針へ追記候補。20260531 resolve.md の指摘と同様）。
