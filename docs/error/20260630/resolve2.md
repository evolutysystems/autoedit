# 焼き込みで subtitles フィルタが original_size 解析エラー(EINVAL)になる不具合 修正設計書

対象エラー: `docs/error/20260630/error2.md`
対象ソース: `src/modules/subtitle_generator.py`（`burn_subtitle` の `subtitles` パスエスケープ）
本書はレビュー用の**修正設計書**であり、実装は承認後に行う（`docs/CLAUDE.md` の方針に準拠）。

> 本書は `docs/error/20260630/resolve.md`（無音カット出力の TS 破損対策／対策A・B・C を実装済）の**続き**。resolve.md の TS 対策で「muxer の invalid duration(-22)」は解消に向かったが、**resolve.md 実装時に同時に加えたパスエスケープ変更が回帰(リグレッション)を起こした**。本書はその是正を扱う。

---

## 1. 事象

無音カット後のテロップ焼き込みで、FFmpeg が `returncode=4294967274`（= `−22` = `EINVAL`）で即失敗する。stderr の決定的な行は次の通り。

```
[Parsed_subtitles_0] Unable to parse "original_size" option value
        "/Users/evolu/AppData/Local/Temp/autoedit_kxtdl_58/subtitle.ass" as image size
[fc#-1] Error applying option 'original_size' to filter 'subtitles': Invalid argument
Error opening output file ...\subtitle.mp4.
Error opening output files: Invalid argument
```

- 前回(`error.md`)は字幕ロード後に**11分エンコードして muxer で**落ちていた。今回は**フィルタ初期化の段階で即失敗**しており、別事象。
- 入力 `silence_cut.mp4` は正常に開けており（`Duration: 00:00:11.74`）、resolve.md の TS 対策によって**前回の muxer エラーは再発していない**（今回はそこまで到達していない）。

---

## 2. 原因調査

### 2-1. 結論（主因）：`subtitles` パスのコロンエスケープを誤って撤去した（回帰）

エラーの本質は **「`subtitles` フィルタがパス中の `:`(ドライブレター区切り)をオプション区切りと誤解釈した」** こと。`subtitles` フィルタのオプションは `:` 区切りで、

- 第1オプション = `filename`
- 第2オプション = `original_size`

である。現状の生成値（`subtitle_generator.py:337-339`）は

```python
safe_path = subtitle_path.replace("\\", "/").replace("'", r"\'")
filter_spec = f"subtitles='{safe_path}',setpts=N/FRAME_RATE/TB"
# → subtitles='C:/Users/evolu/.../subtitle.ass',setpts=...
```

これを FFmpeg は **`filename=C`**, **`original_size=/Users/evolu/.../subtitle.ass`** と解釈した。すなわち `C` の直後の `:` がオプション区切りとして働き、後半が `original_size`（画像サイズ指定）へ渡って「画像サイズとして解釈できない」→ `EINVAL` となった。

→ つまり **ドライブレターの `:` が `\:` にエスケープされていない**ことが直接原因。

### 2-2. なぜこうなったか：resolve.md 実装時の変更が誤りだった

`docs/error/20260630/resolve.md` の実装で、TS 対策(`setpts`/CFR/genpts)と**同時に**、当時の仮説（「シングルクォートで囲めばコロンエスケープは不要／二重エスケープがパスを壊す」）に基づき、**コロンエスケープ `.replace(":", "\\:")` を撤去**した。これが誤りだった。

**実機の事実（エスケープ挙動の確定）**

| 形 | 結果 |
|---|---|
| `subtitles='C\:/...ass'`（コロンエスケープ**あり**＋クォート）＝**元の実装** | **正常**（`error.md` でフォント解決・11分エンコード到達） |
| `subtitles='C:/...ass'`（コロンエスケープ**なし**＋クォート）＝**現状** | **失敗**（`original_size` 解析エラー・本事象） |

この対比から、**FFmpeg のエスケープは2段階**であることが確定する。

1. **フィルタグラフ階層**：チェーンを `,`・`;` で区切る。シングルクォート `'...'` はここで効き、空白・`,` を保護し、内側の `\` を**そのまま下位へ通す**。
2. **フィルタ内オプション階層**：`subtitles` のオプションを `:` で区切る。**この階層で `:` はなお区切り文字**であり、リテラルにするには `\:` が必要。

シングルクォート（階層1）は `:` の分割（階層2）を**防がない**。したがって `:` は `\:` でエスケープする必要があり、**元の `'C\:/...'`（クォート＋コロンエスケープ）が正しい**。「二重エスケープ」という前回仮説は誤りで、両者は別階層に作用するため**併用が正解**だった。

> 補足：このエスケープ自体は本来の事象（`error.md` の muxer エラー）とは無関係であり、resolve.md 実装で**触るべきではなかった**。TS 対策のみ残し、エスケープは原状へ戻すのが正しい。

---

## 3. 修正方針

### 対策A（主・確実）：コロンエスケープを復元する

`burn_subtitle` のパス整形を、実機で動作実績のある形へ戻す。`setpts` 等の TS 対策（resolve.md 対策A）は**維持**する。

```python
# 修正案 (subtitle_generator.py burn_subtitle 内)
# subtitles フィルタは2段階エスケープ:
#   ・シングルクォートでフィルタグラフ階層(空白/カンマ)を保護し、内側の '\' を下位へ通す
#   ・フィルタ内オプション階層では ':' がなお区切り文字のため '\:' でエスケープする
# 両者は別階層に作用するため併用が正しい (error2.md で実証)。
safe_path = subtitle_path.replace("\\", "/").replace(":", "\\:")
filter_spec = f"subtitles='{safe_path}',setpts=N/FRAME_RATE/TB"
# → subtitles='C\:/Users/evolu/.../subtitle.ass',setpts=...
```

- 変更は `subtitle_generator.py:337` の1行（`.replace("'", r"\'")` を `.replace(":", "\\:")` へ戻す）。コメント(334-336)も是正する。
- `-fflags +genpts` / `-af aresample` / `-fps_mode cfr` / `-r {fps}`（resolve.md 対策A）は**そのまま維持**。

### 対策B（任意・恒久的堅牢化）：相対パス化でドライブレターを排除

将来この種のエスケープ事故を根絶する案として、**FFmpeg を ASS のあるディレクトリ(cwd)で起動し、フィルタはベース名のみ参照**する。

```
cwd = os.path.dirname(subtitle_path)             # = working_dir
filter_spec = f"subtitles=subtitle.ass,setpts=N/FRAME_RATE/TB"
```

- ドライブレター `:` もバックスラッシュも消えるため、エスケープ自体が不要になる。`-i`/出力は絶対パスのまま（cwd 非依存）。中間名は固定 `subtitle.ass`（特殊文字なし）。
- ただし `Popen` へ `cwd` を渡す配線（`execute` → `run_ffmpeg_progress` に `cwd` 引数追加）が必要で変更面が広い。**既定は対策A（最小・実証済）**とし、対策B は将来の任意改善に留める。

---

## 4. 変更対象

| ファイル | 変更概要 | 区分 |
|---|---|---|
| `src/modules/subtitle_generator.py` | `burn_subtitle`（L337）のパス整形を `.replace(":", "\\:")` へ復元。コメント(L334-336)を2段階エスケープの説明へ是正。`setpts`/CFR/genpts は維持 | 主A |
| （任意）`src/modules/ffmpeg_runner.py`・`src/utils/progress.py` | 対策B採用時のみ `cwd` 配線を追加 | 任意B |

- 設定追加なし・新規ライブラリなし（後方互換）。
- resolve.md で入れた無音カット側 TS 対策（`silence_cutter.py`）・診断補強（`main_window.py`）は**変更しない**。

---

## 5. エラー処理・ログ方針

- 焼き込み失敗時は従来どおり `FFmpegError` を送出。resolve.md 対策C により GUI 通知へ stderr 末尾が併記され、今回の `Unable to parse "original_size" ...` も画面で確認可能（実際に本切り分けに寄与）。
- 生成コマンドは `progress.py` のデバッグログ（`FFmpeg 実行:`）で確認し、`-vf` が `subtitles='C\:/.../subtitle.ass',setpts=...` になっていることを検証する。

---

## 6. 不明点・確認事項（レビュー対象）

1. **対策A で確定してよいか** → 実機実績のある `'C\:/...'` 形へ戻す最小修正。**推奨：確定**。
2. **対策B（相対パス＋cwd）の採否** → 既定は不採用（変更面が広い）。恒久対策として将来検討でよいか。
3. **TS 対策の有効性の再確認**（重要）→ 今回はフィルタ初期化前に失敗したため、resolve.md の muxer/`drop` 対策が**実際に効いているかは未確認**。対策A 適用後に焼き込みを完走させ、`error.md` の `Application provided duration ... invalid` と `drop=35074` が再発しないことを併せて確認する。
4. **入力 `silence_cut.mp4` の fps 実態** → error2.md のヘッダは `58.86 fps, 240 tbr`。CFR 強制(対策B/extract)後も完全な 60fps 一定になっていない可能性があり、§7 で `ffprobe` 確認する。

---

## 7. 検証方針

1. **本修正**：日本語/ドライブレター付き絶対パス環境で焼き込みが**フィルタ初期化を通過**し、テロップ付き動画が出力されること。
2. **コマンド確認**：`-vf` が `subtitles='C\:/.../subtitle.ass',setpts=N/FRAME_RATE/TB` になっていること（`original_size` へパスが流れないこと）。
3. **TS 対策の回帰確認**（§6-3）：焼き込みが最後まで完走し、`invalid duration` / 大量 `drop` が出ないこと。出力の `ffprobe` で PTS 単調・CFR を確認。
4. **空白を含むパス**：作業ディレクトリに空白を含む場合も（シングルクォートにより）成功すること。
5. **回帰**：無音カット・OP/ED 結合・最終出力が従来どおり動作すること。
