# エラー調査報告 (error2)

## 1. 概要
| 項目 | 内容 |
| --- | --- |
| 発生日 | 2026-06-10 |
| 事象 | 例外は発生せず、**処理が無応答のまま停止 (ハングアップ)** |
| 最終ログ | `2026-06-10 13:41:41,355 [INFO] src.modules.silence_cutter: 検出された無音区間: 1159 件` |
| 停止箇所 (Python) | `src/utils/progress.py` 39行目 `for line in iter(process.stdout.readline, "")` で ffmpeg 子プロセスを待ち続けている |
| 起点 | `src/modules/silence_cutter.py` `run` → `cut_and_concat` → `ffmpeg_runner.execute` → `run_ffmpeg_progress` |
| 処理工程 | 無音カット (`cut_and_concat` の FFmpeg 実行中) |
| 入力 | 2時間超の配信アーカイブ / 検出された無音区間 **1159 件** |
| 前提 | 前回の `WinError 206` 対応 (`-filter_complex_script` 化, resolve.md) 適用済み。**起動エラーは解消し、ffmpeg は起動に成功している** |

> 前回 (error.md) は「ffmpeg を起動できずに即例外終了」。
> 今回は「ffmpeg の起動には成功したが、その後フレーム出力が始まらず無応答」という別フェーズの問題。
> resolve.md §6.1「巨大フィルタグラフの性能」で予見されていた懸念が顕在化したもの。

## 2. 処理がどこで止まっているか

ログ `検出された無音区間: 1159 件` (`silence_cutter.py:212`) 以降の流れは次の通り。

```
silence_cutter.run                       (silence_cutter.py:184)
  └─ build_keep_segments                 (:214)  ← 純Python・即時完了 (約1160区間を生成)
  └─ cut_and_concat                      (:218)
       └─ _write_filter_script           (:141)  ← 一時 .ffscript 書き出し・即時完了
       └─ ffmpeg_runner.execute          (:158)
            └─ run_ffmpeg_progress       (progress.py:19)
                 └─ subprocess.Popen     (progress.py:24)  ← ★起動成功 (206は解消済)
                 └─ process.stdout.readline ループ (progress.py:39)  ← ★ここで永久待機
```

- `build_keep_segments` / `_write_filter_script` は I/O・計算とも軽量で、停止要因にならない。
- 停止しているのは **`run_ffmpeg_progress` の stdout 読み取りループ (`progress.py:39`)**。
  ffmpeg 子プロセスが `-progress pipe:1` へ何も書かない (= 進捗が進まない) ため、
  `readline()` がブロックしたまま戻ってこない。

## 3. 原因

ハングの本質的な原因は重なっており、主因は **3.1 の巨大フィルタグラフ**。
**3.2 のパイプ構造**と **3.3 のタイムアウト不在**が「永久に固まる/復帰できない」状態を助長している。

### 3.1 【主因】1160分岐 split+trim+concat フィルタグラフの破綻

`cut_and_concat` は残す区間ごとに `[0:v]trim...`, `[0:a]atrim...` を生成し、
最後に `concat=n=1160:v=1:a=1` で1本に結合する (`silence_cutter.py:98-134`)。

```
[0:v]trim=...[v0]; [0:a]atrim=...[a0];
[0:v]trim=...[v1]; [0:a]atrim=...[a1];
...                                  ← 約1160区間ぶん
[v0][a0][v1][a1]...concat=n=1160:v=1:a=1[outv][outa]
```

このグラフは FFmpeg にとって病的な構成になる。

- `[0:v]` / `[0:a]` を **約1160回参照**するため、FFmpeg は入力を約1160出力に
  ファンアウトする `split` / `asplit` を自動挿入する。
- `concat` は入力を **先頭から順番に1区間ずつ**消費するが、`split` は
  入力フレームを **全分岐へ供給**する。後続区間 (まだ concat に消費されない) の
  分岐にフレームが滞留し、フレームキューが膨張する。
- 結果として、出力フレームを1枚も生成する前の **グラフ構成・初期バッファリング段階**で
  メモリを大量消費し、CPU を使い切ったまま事実上前進しなくなる。
  2時間超 × 1160分岐ではこの段階で実質フリーズに至る。

→ ffmpeg は「異常終了」も「正常進行」もせず、出力 (`out_time`) が 0 のまま停滞する。

### 3.2 【助長要因】stderr を並行ドレインしない構造 (progress.py の潜在バグ)

`run_ffmpeg_progress` (`progress.py:19-65`) は **実行中は stdout のみ**を読み、
stderr は stdout ループ終了後の `finally` で初めて読む (`progress.py:58`)。

```python
for line in iter(process.stdout.readline, ""):   # ← stdout だけを読む
    ...
finally:
    remaining = process.stderr.read() if process.stderr else ""  # ← 実行後にまとめて読む
```

- ffmpeg は警告・フィルタグラフ構成メッセージ等を **stderr** に出力する。
  1160区間ぶんの構成では起動直後に stderr 出力が膨らみやすい。
- Windows のパイプバッファ (概ね 64KB) が満杯になると、ffmpeg は stderr への
  書き込みで **ブロック**する。書き込みでブロックした ffmpeg は stdout への
  progress 出力も止める。
- 親プロセスは `process.stdout.readline()` で待ち続け、ffmpeg は stderr 書き込みで
  待ち続ける → **相互ブロック (デッドロック)**。
- これは「1スレッドが2本のパイプの片方だけを読む」典型的なデッドロックパターン。
  3.1 で進行が遅い状況と重なると、復帰不能の固着になる。

> 補足: `progress.py:21` で `-nostats` を付与しているため毎フレームの stats 行は
> 抑止される。ただし警告・エラー・グラフ構成メッセージは依然 stderr に出るため、
> デッドロックの可能性は残る (構造的欠陥)。

### 3.3 【助長要因】タイムアウト・ハートビート監視の不在

`run_ffmpeg_progress` / `execute` には **タイムアウトも無進捗検知もない**。
そのため 3.1・3.2 で ffmpeg が停滞した場合、アプリ全体が無期限に固まり、
ユーザーから見ると「ログが止まったきり応答しない」状態になる。

### 3.4 なぜ「ログが止まって見える」のか
進捗ログ/進捗バーは `on_progress` (= progress pipe の `progress=` 行) を契機に更新される
(`progress.py:47-53`, `pipeline_context.py:63-75`)。
ffmpeg が出力フレームを生成できず `progress=` 行を1度も送らないため、進捗は 0 のまま
更新されず、後続ログ (`無音カット完了` 等) にも到達しない。例外も出ないため
「停止」に見える。

## 4. 切り分け確認の指針 (推奨)
1. タスクマネージャで `ffmpeg.exe` の状態を確認する。
   - メモリが増え続ける / CPU 高止まり → 3.1 (巨大グラフ) が主因。
   - CPU ほぼ 0 で固着 → 3.2 (stderr デッドロック) の疑いが強い。
2. `cut_and_concat` が生成する `.ffscript` 一時ファイルのサイズ・区間数を確認する
   (約1160区間 = 数十万文字規模である想定)。
3. 同じ `.ffscript` を ffmpeg で手動実行し、stderr を直接ターミナルへ流して
   出力フレームが進むか・どこで止まるかを確認する。

## 5. 修正方針 (要点のみ。実装は設計レビュー後)

CLAUDE.md の方針に従い、本ファイルでは原因と方針の提示に留める。

### 5.1 【本命】巨大フィルタグラフの解消 — バッチ分割 + concat デマルチプレクサ
resolve.md §6.2 で「将来案」とした方式を本採用する。
- 残す区間を一定数ずつ (例: 50区間) のバッチに分け、各バッチを中間 mp4 にエンコード。
- 最後に concat デマルチプレクサ (ファイルリスト方式) で結合する。
- 1本のフィルタグラフあたりの規模を一定に保ち、メモリ・構成負荷を線形に抑える。
- バッチサイズは `setting.json` (例: `silence_cut.batch_size`) で調整可能にする
  (ハードコード禁止に準拠)。

### 5.2 【必須】stderr の並行ドレイン (progress.py のデッドロック解消)
- stderr を **別スレッドで並行して読み出す** (または stdout/stderr を同時に監視する)
  構造へ変更し、パイプバッファ満杯による相互ブロックを防ぐ。
- 出力結果には影響しない実行ラッパの内部修正であり、既存挙動を壊さない。

### 5.3 【推奨】無進捗タイムアウト / ハートビート監視
- 一定時間 `progress=` が更新されない場合に検知・ログ・中断できるようにし、
  無期限ハングを回避する。閾値は `setting.json` で調整可能にする。

## 6. 関連
- 前段の起動エラー: `docs/error/20260610/error.md` (`WinError 206`)
- その解決: `docs/error/20260610/resolve.md` (本件は §6.1 の予見が顕在化したもの)

## 7. 調査対象ファイル
- `src/modules/silence_cutter.py` (`cut_and_concat` / フィルタグラフ生成)
- `src/modules/ffmpeg_runner.py` (`execute`)
- `src/utils/progress.py` (`run_ffmpeg_progress` / パイプ読み取り)
- `src/pipeline/pipeline_runner.py`, `src/pipeline/pipeline_context.py` (進捗連携)
