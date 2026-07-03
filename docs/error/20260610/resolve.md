# エラー解決報告 (resolve)

## 1. 概要
| 項目 | 内容 |
| --- | --- |
| 発生日 | 2026-06-10 |
| 例外種別 | `FileNotFoundError: [WinError 206] ファイル名または拡張子が長すぎます` |
| 発生箇所 | `src/utils/progress.py` 24行目 (`subprocess.Popen`) |
| 起点 | `src/modules/silence_cutter.py` 151行目 (`cut_and_concat`) |
| 処理工程 | 無音カット (`silence_cutter.run` → `cut_and_concat`) |
| 入力 | 2時間超の配信アーカイブ / 検出された無音区間 **1159 件** |
| 影響 | `-filter_complex` 文字列が肥大化しコマンドライン長上限を超過。プロセス起動に失敗しパイプライン中断 |

## 2. 原因

### 2.1 直接原因
`WinError 206` は Windows の `CreateProcess` に渡すコマンドライン文字列が
**上限 32,767 文字** を超えた場合に発生する。
FFmpeg 実行ファイルが見つからない訳ではなく、「コマンドラインが長すぎて
プロセスを生成できない」という意味のエラーである。

### 2.2 肥大化のメカニズム
無音区間が 1159 件検出されると、残す有音区間 (`keep_segments`) はほぼ同数
(約 1160 区間) になる。`cut_and_concat` は各区間ごとに映像・音声 2 本の
フィルタチェーンを `-filter_complex` 文字列として組み立てる。

```python
# src/modules/silence_cutter.py:102 付近 (1区間あたりの生成例)
v_chain = f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}]"
a_chain = f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]"
```

- 1 区間あたり概算 100〜130 文字 × 2 本 = 約 250 文字
- 末尾の `concat=n=...` 連結部 `[v0][a0][v1][a1]...` も区間数に比例して増加
- 1160 区間 × 約 250 文字 + 連結部 ≒ **30 万文字超**

この巨大な文字列を `-filter_complex` の **1 引数** として
`subprocess.Popen` に渡すため、コマンドライン全体が
Windows の上限 (32,767 文字) を大幅に超過し、プロセス起動前に失敗する。

### 2.3 補足
- 短い動画 (無音区間が数十件) では文字列が上限内に収まるため再現しない。
  本件は「2時間超 = 区間数が桁違いに多い」入力で初めて顕在化した。
- `silencedetect` 自体は正常完了 (ログ `検出された無音区間: 1159 件`) しており、
  検出ロジックには問題はない。問題は連結フィルタの **渡し方** にある。

## 3. 修正方針

FFmpeg はフィルタグラフを**ファイルから読み込む** `-filter_complex_script`
オプションを備えている。これを用いれば、肥大化したフィルタ文字列を
コマンドラインに載せず、コマンドライン長の上限を回避できる。

> `-filter_complex` (インライン文字列) → `-filter_complex_script <ファイルパス>`

### 3.1 修正案 (推奨): `-filter_complex_script` への切り替え

`cut_and_concat` で組み立てた `filter_complex` を一時ファイルへ UTF-8 で
書き出し、コマンドにはファイルパスのみを渡す。

```python
# src/modules/silence_cutter.py cut_and_concat 内
# 修正前
cmd = [
    ffmpeg,
    "-y",
    "-hide_banner",
    "-i", input_path,
    "-filter_complex", filter_complex,        # ← 巨大文字列を直接渡している
    "-map", "[outv]",
    "-map", "[outa]",
    *ffmpeg_runner.build_encode_options(ffmpeg_settings),
    output_path,
]

# 修正後
# フィルタグラフを一時ファイルへ書き出し、パスのみをコマンドに渡す
# (コマンドライン長の上限 32,767 文字を回避する)
filter_script_path = _write_filter_script(filter_complex, output_path)
cmd = [
    ffmpeg,
    "-y",
    "-hide_banner",
    "-i", input_path,
    "-filter_complex_script", filter_script_path,   # ← ファイル経由で渡す
    "-map", "[outv]",
    "-map", "[outa]",
    *ffmpeg_runner.build_encode_options(ffmpeg_settings),
    output_path,
]
```

一時ファイルを生成・後始末するヘルパを追加する。

```python
import os
import tempfile

# フィルタグラフを一時ファイルへ書き出しパスを返す
# 出力先と同じディレクトリに置き、文字コードは UTF-8 で統一する
def _write_filter_script(filter_complex, output_path):
    out_dir = os.path.dirname(output_path) or "."
    fd, path = tempfile.mkstemp(suffix=".ffscript", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(filter_complex)
    except Exception:
        os.remove(path)
        raise
    return path
```

- 一時ファイルは FFmpeg 実行完了後 (成功・失敗いずれも) に削除する。
  `cut_and_concat` 末尾を `try/finally` で囲み確実に後始末する。
- 配置先を中間ファイルと同じディレクトリにすることで、
  一時ディレクトリの権限差異やパス長問題を避ける。

### 3.2 修正後の `cut_and_concat` 末尾 (後始末込み)

```python
    filter_script_path = _write_filter_script(filter_complex, output_path)
    try:
        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
        cmd = [
            ffmpeg, "-y", "-hide_banner",
            "-i", input_path,
            "-filter_complex_script", filter_script_path,
            "-map", "[outv]", "-map", "[outa]",
            *ffmpeg_runner.build_encode_options(ffmpeg_settings),
            output_path,
        ]
        kept_total = sum((e - s) for s, e in keep_segments)
        effective_duration = kept_total if total_duration <= 0 else total_duration
        ffmpeg_runner.execute(
            cmd, total_duration=effective_duration, on_progress=on_progress
        )
    finally:
        # 成否に関わらず一時スクリプトを削除する
        if os.path.exists(filter_script_path):
            os.remove(filter_script_path)
    return output_path
```

## 4. 修正対象ファイル
- `src/modules/silence_cutter.py`
  - `cut_and_concat` … `-filter_complex` を `-filter_complex_script` へ変更、後始末追加
  - `_write_filter_script` … 新規ヘルパ関数を追加
  - 先頭の import に `tempfile` を追加 (`os` は既存)

## 5. 既存実装への影響
- フィルタグラフの**内容**は一切変更しない (渡し方のみ変更) ため、
  出力結果は従来と同一。
- 短い動画での挙動は不変 (これまで通り正常動作)。
- 他工程 (テロップ生成・結合) のコマンド組み立てには変更なし。
- CLAUDE.md の制約「既存実装を破壊しない」「不要なライブラリを追加しない」を遵守
  (`tempfile` は標準ライブラリ)。

## 6. 留意点 / 今後の検討事項
1. **巨大フィルタグラフの性能**
   本修正で起動エラーは解消するが、1160 区間を 1 本のフィルタグラフで
   `concat` 処理するため、メモリ・CPU 負荷とエンコード時間は依然として大きい。
   2 時間超の入力では処理時間が長くなる点に留意。

2. **更なる堅牢化 (将来拡張案)**
   区間数が極端に多い場合に備え、以下も将来的に検討する余地がある。
   - 区間を一定数ずつのバッチに分割して中間ファイル化し、
     最後に `concat` デマルチプレクサ (ファイルリスト方式) で結合する。
   - これによりフィルタグラフ 1 本あたりの規模を一定に保てる。
   - バッチサイズは setting.json で調整可能にする (ハードコード禁止に準拠)。
   ※ 本対応は今回の即時修正には含めず、別タスクとして設計推奨。

3. **設定値**
   今回の修正に新規の設定値追加は不要 (挙動を変えないため)。
   将来のバッチ分割を導入する際に `silence_cut.batch_size` 等を
   setting.json へ追加する想定。

## 7. 確認方針
- 2 時間超 (無音区間 1000 件超) のアーカイブで `WinError 206` が再現しないこと。
- 一時 `.ffscript` ファイルが処理完了後に残らないこと。
- 短尺動画でも従来通り無音カットが成功し、出力が変わらないこと。
