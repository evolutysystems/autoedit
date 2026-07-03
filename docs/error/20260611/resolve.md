# 無音カット FFmpeg 強制終了エラー 修正案（案A 実装計画）

対象ログ: `docs/error/20260611/error.md`
対象ソース: `src/modules/silence_cutter.py` / `src/modules/ffmpeg_runner.py` / `src/utils/progress.py`
方針: **案A（入力シーク `-ss` による区間抽出方式）で実装する**

---

## 1. 原因（要約）

`cut_and_concat()` が使う `trim`/`atrim` フィルタは **入力をシークしない**。
そのため FFmpeg は区間位置に関わらず **入力をフレーム0から全デコード**し、

- 後半区間を含むバッチは最初の出力フレームが出るまで純デコードを続け、その間 `-progress` が更新されず、`progress_timeout_sec=600` の無進捗 watchdog（`utils/progress.py`）に `kill` される。
- 24バッチ = 入力を実質24回フル再デコードし、`batch_size` 分割がむしろ処理量を増やしている。

→ 切り出し方式そのものを **入力シーク方式** に変える（案A）。

---

## 2. 案A 実装方針

`trim` フィルタを廃し、**区間ごとに `-ss`（入力シーク）で該当箇所だけをデコード**して中間ファイル化し、最後に既存の `_concat_demux()` で結合する。

- 既に再エンコード前提（`libx264`）のため、`-ss` を `-i` の前に置いてもフレーム精度が得られる（直前キーフレームからデコードし開始点までを内部破棄）。
- 各 ffmpeg 呼び出しは「その区間の尺」だけを処理するため、進捗が常時更新され watchdog kill が原理的に起きない。
- 全長フルデコードの繰り返しが消え、処理量が「残す尺の合計」に比例する。

互換性: 既存の `cut_and_concat()` / `cut_and_concat_batched()` は残し、`setting.json` の `extract_mode`（既定 `"seek"`）で経路を切り替える。これにより「既存実装を破壊しない」制約を満たす。

---

## 3. 具体的なコード変更（`src/modules/silence_cutter.py`）

### 3-1.【新規】区間抽出関数 `extract_segment()`

`cut_and_concat()` の近傍に追加する。

```python
# 1区間を入力シーク (-ss) で抽出し中間ファイル化する
# trim フィルタと異なり入力をシークするため、その区間付近のみをデコードする。
# 再エンコード前提のため -ss を -i の前に置いてもフレーム精度が得られる。
def extract_segment(input_path, start, end, seg_path, ffmpeg_settings,
                    fade_enabled=False, fade_duration_sec=0.05,
                    on_progress=None):
    seg_duration = end - start
    if seg_duration <= 0:
        raise InputError(f"区間長が不正です: start={start}, end={end}")

    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_settings)
    # -ss を -i の前に置き、対象区間付近のみをデコードする (高速シーク)
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-ss", f"{start:.3f}",
        "-i", input_path,
        "-t", f"{seg_duration:.3f}",   # 区間長で停止 (基準が曖昧な -to は使わない)
    ]
    # フェード指定時は区間内で完結する fade/afade を付与する (trim 不要)
    if fade_enabled and seg_duration > fade_duration_sec * 2:
        fade_out_start = seg_duration - fade_duration_sec
        cmd += [
            "-vf",
            f"fade=t=in:st=0:d={fade_duration_sec},"
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_duration_sec}",
            "-af",
            f"afade=t=in:st=0:d={fade_duration_sec},"
            f"afade=t=out:st={fade_out_start:.3f}:d={fade_duration_sec}",
        ]
    cmd += [
        *ffmpeg_runner.build_encode_options(ffmpeg_settings),
        "-avoid_negative_ts", "make_zero",  # 連結時の負PTS/音ズレ対策
        seg_path,
    ]

    # 区間尺を total_duration にすることで進捗率を 0-1 に正規化する
    ffmpeg_runner.execute(
        cmd, total_duration=seg_duration, on_progress=on_progress,
        progress_timeout_sec=ffmpeg_runner.get_progress_timeout_sec(ffmpeg_settings),
    )
    return seg_path
```

### 3-2.【新規】案A オーケストレータ `cut_and_concat_seek()`

`cut_and_concat_batched()` の近傍に追加する。区間ごとに `extract_segment()` を呼び、`_concat_demux()` で結合する。進捗・中間ファイル後始末は既存の按分／クリーンアップ実装を踏襲する。

```python
# 入力シーク方式で全区間を抽出し concat デマルチプレクサで結合する (案A)
# trim フィルタによる全長再デコードを排し、watchdog 停滞 kill を回避する。
def cut_and_concat_seek(input_path, keep_segments, output_path, ffmpeg_settings,
                        fade_enabled=False, fade_duration_sec=0.05,
                        on_progress=None, concat_reencode=False):
    if not keep_segments:
        raise InputError("残すべき有音区間が存在しません (全区間が無音判定)")

    out_dir = os.path.dirname(output_path) or "."
    kept_total = sum((e - s) for s, e in keep_segments) or 1.0
    seg_paths = []
    done_duration = 0.0
    try:
        for idx, (start, end) in enumerate(keep_segments):
            seg_path = os.path.join(out_dir, f"silence_seg_{idx:05d}.mp4")
            seg_dur = end - start

            # 区間内進捗 (0-1) を全体進捗へ写像するサブコールバック
            def _seg_progress(ratio_in_seg, _kv, _base=done_duration, _dur=seg_dur):
                if on_progress is not None:
                    on_progress((_base + ratio_in_seg * _dur) / kept_total, _kv)

            extract_segment(
                input_path, start, end, seg_path, ffmpeg_settings,
                fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
                on_progress=_seg_progress if on_progress else None,
            )
            seg_paths.append(seg_path)
            done_duration += seg_dur

        # 抽出済み区間群を concat デマルチプレクサで最終結合する (既存関数を再利用)
        _concat_demux(seg_paths, output_path, ffmpeg_settings, reencode=concat_reencode)
        if on_progress is not None:
            on_progress(1.0, {})
    finally:
        # 中間区間ファイルを後始末する (成否に関わらず)
        for p in seg_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    _logger.warning("区間中間ファイル削除に失敗: %s", p)
    return output_path
```

### 3-3.【変更】`run()` の経路切替

`run()` 内の設定読み出しと `cut_and_concat_batched(...)` 呼び出しを、`extract_mode` で分岐させる。

設定読み出しに1行追加:

```python
    extract_mode = silence_cfg.get("extract_mode", "seek")  # "seek"=案A / "filter"=従来
```

呼び出し箇所（現 329 行付近）を差し替え:

```python
    if extract_mode == "seek":
        # 案A: 入力シーク方式 (推奨)
        cut_and_concat_seek(
            input_path, keep_segments, output_path, ffmpeg_cfg,
            fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
            on_progress=context.progress_subcallback("無音カット"),
            concat_reencode=concat_reencode,
        )
    else:
        # 従来: trim+concat フィルタ方式 (互換維持)
        cut_and_concat_batched(
            input_path, keep_segments, output_path, ffmpeg_cfg,
            fade_enabled=fade_enabled, fade_duration_sec=fade_duration_sec,
            total_duration=total_duration,
            on_progress=context.progress_subcallback("無音カット"),
            batch_size=batch_size, concat_reencode=concat_reencode,
        )
```

※ `cut_and_concat()` / `cut_and_concat_batched()` / `_write_filter_script()` は削除せず温存（`extract_mode="filter"` で利用可能）。`batch_size` は seek 方式では未使用となる。

---

## 4. `setting.json` 変更（`silence_cut` セクション）

```jsonc
"silence_cut": {
    "noise_threshold_db": -26,
    "min_silence_duration_sec": 0.6,
    "fade_enabled": false,
    "fade_duration_sec": 0.05,
    "extract_mode": "seek",   // 追加: 区間抽出方式 ("seek"=案A / "filter"=従来)
    "batch_size": 50,         // 互換のため残置 (filter 方式でのみ使用)
    "concat_reencode": false
}
```

- ハードコード禁止方針に従い、方式切替は `extract_mode` で設定可能化する。
- `ffmpeg.progress_timeout_sec` はセーフティネットとして現状維持（600）で問題ないが、極端な長区間がある場合に備え必要なら引き上げる。

---

## 5. 互換性・影響

- 既存の `filter` 経路コードは全て温存するため、後方互換を維持（破壊的変更なし）。
- 出力フォーマットは従来同様 `build_encode_options()` による再エンコード結果で一致。
- 中間ファイルは `silence_seg_XXXXX.mp4` を出力ディレクトリに一時生成し、`finally` で必ず削除。
- トレードオフ: 区間数（最大1156）分の ffmpeg プロセス起動が発生するが、各々が軽量・短時間のため総処理時間は現行（全長×24回デコード）より大幅短縮される見込み。プロセス数をさらに抑えたい場合は、別途「微小ギャップ隣接区間のマージ」を追加検討する（本実装の範囲外）。

---

## 6. 検証方針

1. `extract_mode="seek"` で当該 `stream.mp4` を実行し、**progress が継続更新され watchdog kill が発生しない**ことをログで確認。
2. 出力総尺 ≒ 有音区間合計尺（`kept_total`）であること、A/V 同期ズレがないことを確認。
3. 短尺サンプル（区間数少）で `seek` / `filter` 両経路の出力が同等になる回帰確認。
4. `fade_enabled=true` 時に各区間境界へフェードが付与されることを確認。
5. 異常系: 区間長0・全区間無音時に `InputError` が送出され、中間ファイルが残らないこと（`finally` 後始末）を確認。
