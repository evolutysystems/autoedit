# エラー解決報告 (resolve2)

対象: `docs/error/20260610/error2.md` (無音カット中のハングアップ)

## 1. 概要
| 項目 | 内容 |
| --- | --- |
| 発生日 | 2026-06-10 |
| 事象 | 例外なしの **無応答ハング** (`検出された無音区間: 1159 件` 以降進まない) |
| 停止箇所 | `src/utils/progress.py:39` (ffmpeg 進捗待ちの stdout 読み取りループ) |
| 起点 | `src/modules/silence_cutter.py` `cut_and_concat` の FFmpeg 実行 |
| 入力 | 2時間超アーカイブ / 無音区間 **1159 件** (残す区間 約1160) |
| 前提 | `WinError 206` 対応 (`-filter_complex_script` 化, resolve.md) 適用済み |

error2.md で特定した原因は次の3層。本書はそれぞれに対する修正案を示す。

| # | 原因 | 区分 | 対応 |
| --- | --- | --- | --- |
| 3.1 | 1160分岐 split+trim+concat フィルタグラフの破綻 | 主因 | §3 バッチ分割 + concat デマルチプレクサ |
| 3.2 | stderr を並行ドレインせずパイプバッファ満杯でデッドロック | 助長 | §4 stderr 並行ドレイン |
| 3.3 | タイムアウト・無進捗検知が皆無 | 助長 | §5 無進捗タイムアウト監視 |

修正は **§3 → §4 → §5 の優先順**で適用する。§3 単体でハングは解消する見込みだが、
§4・§5 は同種の固着 (他工程含む) を防ぐ堅牢化として併せて実施することを推奨する。

すべて CLAUDE.md の制約 (日本語コメント / 関数コメント / 設定は setting.json /
既存破壊禁止 / 標準ライブラリのみ) に準拠する。

---

## 2. 設計方針

- **フィルタグラフ1本あたりの規模を一定に保つ**ことが本質的解決。
  1本に全区間を詰め込む現行方式をやめ、区間を小さなバッチに分けて段階的に処理する。
- 既存の `cut_and_concat` (trim+concat フィルタ) は **バッチ内**の結合に再利用する。
  バッチ間は **concat デマルチプレクサ** (再エンコードなしのファイル結合) でまとめる。
- 出力結果 (区間の切り出し内容・フェード挙動) は現行と同一に保つ。
- バッチサイズ等の閾値はハードコードせず `setting.json` で調整可能にする。

---

## 3. 【主因対応】バッチ分割 + concat デマルチプレクサ

### 3.1 設定値の追加 (`src/settings/setting.json`)
`silence_cut` セクションに以下を追加する (既存キーは変更しない)。

```jsonc
"silence_cut": {
  // ... 既存キー ...
  "batch_size": 50,                 // 1フィルタグラフあたりの最大区間数 (0以下で分割無効=従来動作)
  "concat_reencode": false          // バッチ結合時に再エンコードするか (既定: コピー結合)
}
```

- `batch_size` を超える区間数のときだけ分割パスに入る。小規模動画は従来経路のまま。
- `concat_reencode=false` の既定ではバッチ結合をストリームコピーで行い高速。
  コーデック不一致等で結合が不安定な環境向けに `true` で再エンコードも選べる。

### 3.2 `cut_and_concat` の分岐 (`src/modules/silence_cutter.py`)
既存の `cut_and_concat` は **シグネチャ・挙動を変えず**そのまま残し、
新たに区間数で経路を選ぶラッパ `cut_and_concat_batched` を追加して `run` から呼ぶ。

```python
# 残す区間を結合する。区間数が batch_size を超える場合はバッチ分割し、
# 各バッチを中間ファイル化したうえで concat デマルチプレクサで最終結合する。
# batch_size <= 0 のときは従来どおり一括 (cut_and_concat) で処理する。
def cut_and_concat_batched(input_path, keep_segments, output_path, ffmpeg_settings,
                           fade_enabled=False, fade_duration_sec=0.05,
                           total_duration=0.0, on_progress=None,
                           batch_size=0, concat_reencode=False):
    if not keep_segments:
        raise InputError("残すべき有音区間が存在しません (全区間が無音判定)")

    # 分割不要 (小規模 or 無効) なら従来経路へフォールバック
    if batch_size <= 0 or len(keep_segments) <= batch_size:
        return cut_and_concat(
            input_path, keep_segments, output_path, ffmpeg_settings,
            fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
            total_duration=total_duration, on_progress=on_progress,
        )

    # 区間を batch_size ずつのバッチへ分割する
    batches = [
        keep_segments[i:i + batch_size]
        for i in range(0, len(keep_segments), batch_size)
    ]
    _logger.info("無音カットをバッチ分割: %d 区間 → %d バッチ (size=%d)",
                 len(keep_segments), len(batches), batch_size)

    out_dir = os.path.dirname(output_path) or "."
    batch_paths = []
    # 進捗はバッチ尺の累積で按分する
    kept_total = sum((e - s) for s, e in keep_segments) or 1.0
    done_duration = 0.0
    try:
        for idx, batch in enumerate(batches):
            batch_path = os.path.join(out_dir, f"silence_batch_{idx:04d}.mp4")
            batch_dur = sum((e - s) for s, e in batch)

            # 各バッチ内の進捗を全体進捗へ写像するサブコールバック
            base = done_duration
            def _batch_progress(ratio_in_batch, _kv, _base=base, _dur=batch_dur):
                if on_progress is not None:
                    on_progress((_base + ratio_in_batch * _dur) / kept_total, _kv)

            # バッチ内結合は既存の trim+concat フィルタ (cut_and_concat) を再利用
            cut_and_concat(
                input_path, batch, batch_path, ffmpeg_settings,
                fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
                total_duration=batch_dur,
                on_progress=_batch_progress if on_progress else None,
            )
            batch_paths.append(batch_path)
            done_duration += batch_dur

        # バッチ群を concat デマルチプレクサで最終結合する
        _concat_demux(batch_paths, output_path, ffmpeg_settings,
                      reencode=concat_reencode)
        if on_progress is not None:
            on_progress(1.0, {})
    finally:
        # 中間バッチファイルを後始末する (成否に関わらず)
        for p in batch_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    _logger.warning("バッチ中間ファイル削除に失敗: %s", p)
    return output_path
```

### 3.3 concat デマルチプレクサ結合の追加 (`src/modules/silence_cutter.py`)

```python
# 複数の中間動画を concat デマルチプレクサ (ファイルリスト方式) で1本に結合する。
# reencode=False のときはストリームコピー (高速・劣化なし)。
def _concat_demux(batch_paths, output_path, ffmpeg_settings, reencode=False):
    if not batch_paths:
        raise InputError("結合対象のバッチが存在しません")

    out_dir = os.path.dirname(output_path) or "."
    # concat デマルチプレクサ用のファイルリストを書き出す
    fd, list_path = tempfile.mkstemp(suffix=".txt", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for p in batch_paths:
                # シングルクォートを含むパス対策でエスケープする
                safe = p.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
        cmd = [
            ffmpeg, "-y", "-hide_banner",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
        ]
        if reencode:
            cmd += ffmpeg_runner.build_encode_options(ffmpeg_settings)
        else:
            cmd += ["-c", "copy"]
        cmd += [output_path]

        ffmpeg_runner.execute(cmd)
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)
    return output_path
```

### 3.4 `run` からの呼び出し差し替え (`src/modules/silence_cutter.py`)
`run` の `cut_and_concat(...)` 呼び出し (現 `silence_cutter.py:218`) を
`cut_and_concat_batched(...)` に差し替え、設定値を渡す。

```python
    batch_size = int(silence_cfg.get("batch_size", 0))
    concat_reencode = bool(silence_cfg.get("concat_reencode", False))

    cut_and_concat_batched(
        input_path, keep_segments, output_path, ffmpeg_cfg,
        fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
        total_duration=total_duration,
        on_progress=context.progress_subcallback("無音カット"),
        batch_size=batch_size, concat_reencode=concat_reencode,
    )
```

> 効果: 1本のフィルタグラフは最大 `batch_size` 区間 (既定50) に収まり、
> split のファンアウトとフレームキュー膨張が線形に抑えられる。
> 3.1 のグラフ構成・バッファリング段階での固着が解消する。

---

## 4. 【堅牢化】stderr の並行ドレイン (`src/utils/progress.py`)

### 4.1 問題の再掲
現行 `run_ffmpeg_progress` は実行中 stdout だけを読み、stderr は終了後に読む
(`progress.py:39, 58`)。stderr のパイプバッファが満杯になると ffmpeg が
書き込みでブロックし、stdout も止まって相互デッドロックになる。

### 4.2 修正案: stderr を専用スレッドで並行読み出し
標準ライブラリ `threading` のみを使用する (新規依存なし)。

```python
import threading
from collections import deque

# stderr を別スレッドで読み続け、末尾 N 行だけを保持する。
# パイプバッファ満杯による ffmpeg 側ブロックを防ぐ (デッドロック対策)。
def _drain_stderr(stderr, sink, max_lines=30):
    if stderr is None:
        return
    for line in iter(stderr.readline, ""):
        sink.append(line.rstrip("\n"))  # deque(maxlen) が古い行を自動破棄
```

`run_ffmpeg_progress` 本体を次のように変更する。

```python
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=_READ_BUFSIZE,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
    )

    # stderr を並行ドレインする (満杯ブロック防止)
    stderr_tail_buf = deque(maxlen=30)
    stderr_thread = threading.Thread(
        target=_drain_stderr, args=(process.stderr, stderr_tail_buf), daemon=True
    )
    stderr_thread.start()

    progress_kv = {}
    try:
        for line in iter(process.stdout.readline, ""):
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            progress_kv[key] = value
            if key == "progress":
                ratio = _calc_ratio(progress_kv, total_duration)
                if on_progress is not None:
                    try:
                        on_progress(ratio, dict(progress_kv))
                    except Exception:
                        _logger.exception("on_progress コールバックで例外発生")
                if value == "end":
                    break
    finally:
        process.wait()
        stderr_thread.join(timeout=5)

    stderr_tail = "\n".join(stderr_tail_buf)
    return process.returncode, stderr_tail
```

> 効果: stderr が常に消費されるため ffmpeg が書き込みでブロックしなくなり、
> 3.2 のデッドロックが解消する。戻り値 (returncode, stderr_tail) の意味は不変。

---

## 5. 【堅牢化】無進捗タイムアウト監視 (`src/utils/progress.py`)

### 5.1 目的
3.1・3.2 を直しても、別工程や想定外要因で ffmpeg が停滞した際に
アプリが無期限ハングするのを避ける。一定時間 `progress=` が更新されなければ
プロセスを終了し、`FFmpegError` として上位へ通知する。

### 5.2 設定値の追加 (`src/settings/setting.json`)
```jsonc
"ffmpeg": {
  // ... 既存キー ...
  "progress_timeout_sec": 600   // この秒数 progress 更新が無ければ停滞と判断 (0で無効)
}
```

### 5.3 実装方針
- 進捗監視スレッド (または最終更新時刻の比較) で「最後に `progress=` を受けた時刻」を保持。
- `progress_timeout_sec` を超えて更新が無ければ `process.kill()` → `process.wait()` し、
  タイムアウトを示す `FFmpegError` を送出する。
- `run_ffmpeg_progress` に `progress_timeout_sec` 引数を追加し、
  `ffmpeg_runner.execute` → 各呼び出し元から `ffmpeg` 設定値を引き渡す。
- 既定や 0 のときは従来どおりタイムアウトなし (挙動不変)。

> 注: §3・§4 で本ハングは解消する見込みのため、§5 は安全網。
> §3・§4 を先に適用し、§5 は閾値検証のうえ段階導入してよい。

---

## 6. 修正対象ファイル
| ファイル | 変更内容 |
| --- | --- |
| `src/modules/silence_cutter.py` | `cut_and_concat_batched` / `_concat_demux` 追加、`run` の呼び出し差し替え。`cut_and_concat` は不変で再利用 |
| `src/utils/progress.py` | stderr 並行ドレイン (`_drain_stderr` + スレッド)、無進捗タイムアウト (引数追加) |
| `src/modules/ffmpeg_runner.py` | `execute` に `progress_timeout_sec` を中継する引数を追加 (任意) |
| `src/settings/setting.json` | `silence_cut.batch_size` / `silence_cut.concat_reencode` / `ffmpeg.progress_timeout_sec` 追加 |

## 7. 既存実装への影響
- `cut_and_concat` のロジック・出力は不変。小規模動画 (区間数 ≤ batch_size) は従来経路のまま。
- `run_ffmpeg_progress` の戻り値仕様は不変 (内部のパイプ読み取り方法のみ変更)。
- 追加ライブラリなし (`threading` / `collections` は標準)。
- 設定値はすべて既定値で従来動作を維持 (`batch_size` 既定の扱いに注意。§9 参照)。

## 8. 確認方針
1. 2時間超 (無音区間1000件超) のアーカイブで `batch_size=50` 設定時にハングせず完走すること。
2. 出力動画の尺・内容が一括処理時と一致すること (フェード有無含む)。
3. `silence_batch_*.mp4` / `.txt` / `.ffscript` 一時ファイルが処理後に残らないこと。
4. `batch_size=0` で従来 (一括) 経路に分岐し、短尺動画が従来どおり成功すること。
5. (任意) `progress_timeout_sec` を小さく設定し、停滞時に `FFmpegError` で中断されること。
6. stderr 大量出力ケースでデッドロックせず stderr_tail がエラーログに残ること。

## 9. 留意点 / 推奨デフォルト
- **`batch_size` の既定**: 本番の主目的は長尺動画の救済なので、既定を `50` 等の
  有効値にして「標準でバッチ分割が効く」状態を推奨する。`0` (=従来一括) を既定にすると
  本ハングが再発しうるため非推奨。
- **バッチ境界とフェード**: バッチ間結合は concat デマルチプレクサ (コピー結合) のため、
  バッチ境界に追加のフェードは入らない。各区間のフェード (`fade_enabled`) は
  バッチ内 `cut_and_concat` でこれまで通り付与される。挙動差は生じない。
- **コピー結合の前提**: 全バッチが同一コーデック/解像度/fps で生成されるため
  `-c copy` で安全に結合できる。万一環境差で結合エラーが出る場合は
  `concat_reencode=true` で再エンコード結合に切り替える。
- **将来拡張**: バッチ自体を並列エンコードすればさらに高速化できるが、
  CPU/メモリ負荷とのトレードオフがあるため別タスクとして設計推奨。

## 10. 関連
- `docs/error/20260610/error2.md` (本件の調査報告)
- `docs/error/20260610/error.md` / `resolve.md` (前段の `WinError 206` とその解決。
  本書 §3 は resolve.md §6.2 の「将来案」を本採用したもの)
