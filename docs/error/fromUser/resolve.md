# 利用者報告エラー（2026-10-22）原因と修正 設計書

対象現象: `docs/error/fromUser/20261022.md` / 画面写真 `docs/error/fromUser/Error.jpg`

1. `Failed to execute script 'main_window' due to unhandled exception : [Errno 28] No space left on device`
2. `処理に失敗しました。ffprobeが失敗。`

対象バージョン: **v1.5.8**（`3f15736` / `src/version.py` `__version__ = "1.5.8"`）

解析対象ソース:
`src/gui/main_window.py` / `src/gui/archive_tab.py` / `src/archive/twitch_source.py` /
`src/archive/config.py` / `src/archive/features.py` /
`src/pipeline/pipeline_context.py` / `src/pipeline/pipeline_runner.py` /
`src/timeline/renderer.py` / `src/timeline/media_recovery.py` /
`src/modules/ffmpeg_runner.py` / `src/modules/loudness_normalizer.py` /
`src/modules/output_writer.py` / `src/settings/settings_window.py` / `installer/AutoEdit.iss`
外部: `twitchdl 3.3.1` / `click 8.3.2`

本書は `docs/CLAUDE.md`「いきなり実装を開始しない」「不明点がある場合は推測実装せず設計書へ記載する」に従い、
**解析と修正設計のみ**をまとめたものである。実装は本書のレビュー後に行う。

---

## 1. 結論（先に要点）

**Error.jpg のトレースバックにより、1 件目は発生箇所まで確定した。**

| # | 症状 | 原因 | 確度 |
|---|---|---|---|
| **A** | `[Errno 28] No space left on device` | **Twitch VOD 取得（twitch-dl）の最中に C: が枯渇**した。落ちたのは GUI ではなく、本体 exe を multi-call で再起動した**twitch-dl 子プロセス**（`src/gui/main_window.py:1525` の `cli()`）。VOD の断片は `%LOCALAPPDATA%\twitch-dl` へ、結合後の mp4 は `archive_work` へ置かれ、**どちらもインストールドライブ(C:)で、どちらも自動削除されない** | **確定**（トレースバック + 実測） |
| **A'** | 枯渇の実体 | 開発端末での滞留実測: `archive_work` **45.59 GB / VOD 29 本**、`%LOCALAPPDATA%\twitch-dl` **48.38 GB / 4527 ファイル（VOD 1 本ぶん・未完成）**。合計 **約 94 GB** | **確定**（実測） |
| **B** | 容量不足が「クラッシュダイアログ」になる | `main()` の `__twitchdl__` 分岐（`main_window.py:1522-1526`）に例外の受けが無い。`click` は **EPIPE 以外の `OSError` を意図的に再送出する**（`click/core.py:1425-1430`）ため、`ENOSPC` は素通りして PyInstaller のダイアログになる | **確定**（コード） |
| **C** | `ffprobeが失敗` | **同じ枯渇の後始末**が原因である可能性が高い。`download_vod` は `--skip-existing` を付けているため、**前回の失敗で残った中途半端な `vod_<id>.mp4` を「取得済み」として素通り**させ、採点の最初の `probe_duration`（`features.py:33`）で落ちる | **高（要ログ確認 / §8-1）** |
| — | 空き容量の事前確認 | `shutil.disk_usage` / `ENOSPC` 判定は**コード全体に 1 箇所も無い**（grep 0 件） | **確定** |

A と C は**同じ原因（C: の枯渇と、その後始末の不在）から出た 2 つの症状**とみてよい。

---

## 2. Error.jpg の解析

### 2-1. 読み取った内容

```
Unhandled exception in script
Failed to execute script 'main_window' due to unhandled exception:
[Errno 28] No space left on device

Traceback (most recent call last):
  File "main_window.py", line 1559, in <module>
  File "main_window.py", line 1525, in main
  File "click\core.py", line 1485, in __call__
  File "click\core.py", line 1406, in main
  File "click\core.py", line 1873, in invoke
  File "click\core.py", line 1269, in invoke
  File "click\core.py", line 824, in invoke
  File "twitchdl\cli.py", line 397, in download
  File "twitchdl\commands\download.py", line 52, in download
  File "twitchdl\commands\download.py", line 61, in download_one
  File "twitchdl\commands\download.py", line 342, in _download_vi…(_download_video)
  File "asyncio\runners.py", line 195, in run
  File "asyncio\runners.py", line 118, in run
  File "asyncio\base_events.py", line 691, in run_until_complete
  （以降はダイアログ外）
```

### 2-2. 判明したこと

**落ちたのは GUI プロセスではない。** `main_window.py:1525` は次の分岐である。

```python
# src/gui/main_window.py:1517-1527
def main():
    started_at = time.monotonic()
    if len(sys.argv) > 1 and sys.argv[1] == "__twitchdl__":
        from twitchdl.cli import cli
        sys.argv = ["twitch-dl", *sys.argv[2:]]
        cli()                                  # ← 1525 行目。ここで落ちた
        return
    app = QApplication(sys.argv)               # ここまで到達していない
```

凍結配布では twitch-dl を別バイナリにせず、本体 exe を
`Stretheus.exe __twitchdl__ download …` として再起動する multi-call 方式を採っている（flow17 R3）。
つまりこのダイアログは **VOD をダウンロードしている子プロセス**が出したものであり、
`QApplication` すら作られていない。Qt も GUI も一切関係しない。

さらに `twitchdl/commands/download.py:342` は

```python
asyncio.run(
    download_all(zip(sources, targets), args.max_workers, …)
)
```

で、**VOD の断片（.ts）をダウンロードして書き出している最中**である。
結合（join）より前の段階で、書き込み先の空きが尽きた。

### 2-3. なぜ握られずにダイアログまで出たか

`click` は `standalone_mode=True` でも **EPIPE 以外の `OSError` を再送出する**。

```python
# click/core.py:1425-1430
except OSError as e:
    if e.errno == errno.EPIPE:
        …
        sys.exit(1)
    else:
        raise          # ← ENOSPC(28) はここを通る
```

`main_window.py` 側の `cli()` 呼び出しには `try` が無いため、
例外はそのままスクリプト最上位（`:1559`）まで抜け、
PyInstaller（onedir / `console=False` / `disable_windowed_traceback=False`）が
`Unhandled exception in script` ダイアログを出して終了した。

### 2-4. 利用者から見た挙動

* ダイアログは**モーダル**で、利用者が `Close` を押すまで子プロセスは終われない
* 親（GUI）は `_run()` で子の stdout を読みながら待っているため、
  その間「VOD取得中…」のまま**止まって見える**
* 子が終わると非 0 終了 → `TwitchError`（`twitch_source.py:281-285`）
  → 「VOD の取得に失敗しました」
* つまり利用者には **「見慣れない英語のクラッシュ窓」→「取得に失敗」** の順で出る。
  どちらにも「ディスクの空きが無い」とは書かれていない

---

## 3. 原因A: ディスク枯渇（`[Errno 28]`）

### 3-1. 重い成果物の置き場が 5 つ、すべて C: 固定

| # | 置き場 | 既定値 | 決めている箇所 | 設定画面から変更 | 自動削除 |
|---|---|---|---|---|---|
| ① | **VOD 断片（.ts）のキャッシュ** | `%LOCALAPPDATA%\twitch-dl\videos\{id}\{quality}` | twitch-dl の `--cache-dir` 既定（`twitchdl/cli.py:336-339`）。**アプリ側は指定していない**（`twitch_source.py:263-269`） | **不可**（存在すら見えない） | **成功時のみ** |
| ② | **結合後の VOD / チャット** | `<インストール先>\_internal\src\settings\archive_work` | `src/archive/config.py:82-87`（相対値は `src/settings` 基準） | **不可**（`setting.json` 手編集のみ） | **無し** |
| ③ | パイプライン中間ファイル | `%TEMP%\autoedit_xxxxxxxx` | `src/pipeline/pipeline_context.py:47`（`working_dir` を渡す呼び出しが**存在しない**） | **不可** | 正常終了時のみ |
| ④ | 音声認識モデル | `%USERPROFILE%\.cache\huggingface`（faster-whisper 既定） | `src/modules/subtitle_generator.py:415`（`download_root` 未指定・同梱もしていない） | **不可** | 無し（仕様上は妥当） |
| ⑤ | 出力動画 | `{userdocs}\Stretheus\output` | `installer/AutoEdit.iss:269` | 可 | 無し（仕様） |

インストール先は `installer/AutoEdit.iss:52` で `{localappdata}\Programs\Stretheus` に固定されている。
つまり **①〜④ は利用者が何をしても C: に置かれる**。⑤ だけが選べる。

### 3-2. VOD 取得は「VOD 1 本分の 2 倍」を C: に要求する

`twitchdl/commands/download.py` の流れは

1. 断片を **① のキャッシュ**へ全部落とす（= VOD 1 本分）
2. ffmpeg で結合し **② の `vod_<id>.mp4`** を作る（= さらに VOD 1 本分）
3. 成功したら `cache.delete()` でキャッシュを消す（`download.py:378-381`）

したがって**ピークで VOD 1 本の約 2 倍**が C: に必要になる。
`vod_format` の既定は `"source"`（`src/archive/config.py:74`）= 無劣化の最高画質であり、
4 時間配信で 10〜25 GB。**ピーク 20〜50 GB を C: に要求する**。

そして **③ の `cache.delete()` は成功時にしか通らない**。
今回のように断片のダウンロード中（`download.py:342`）で落ちると、
**落ちるまでに書いた断片が丸ごと残り続ける**。

### 3-3. 滞留の実測（開発端末）

**① twitch-dl キャッシュ** — `%LOCALAPPDATA%\twitch-dl`

```
videos\2844948409\   48.38 GB / 4527 ファイル   最終更新 2026/08/20
合計                 48.38 GB
```

この VOD ID は `archive_work` の完成品一覧に**存在しない**。
つまり **完了しなかった取得のキャッシュが 2 か月間 48 GB 居座っていた**。

**② archive_work** — `src/settings/archive_work`

```
vod_2854706666.mp4   11802.32 MB   2026/08/24
vod_2847676012.mp4    6901.34 MB   2026/08/16
vod_2833484601.mp4    6498.37 MB   2026/08/10
vod_2844372771.mp4    5906.30 MB   2026/08/13
vod_2866841954.mp4    5020.37 MB   2026/09/07
…（vod 計 11 本 + chat 計 11 本 + 他）
合計                  45.59 GB / 29 ファイル
```

取得物を削除するコードは `src/archive/` に無い
（`grep -n "os.remove" src/archive/*.py` のヒットは `twitch_auth.py:169` のトークン削除のみ）。

**合計 約 94 GB** が、利用者からは見えない場所に、回収手段も無いまま残っている。
これが枯渇の実体である。

### 3-4. 中間ファイル（③）も工程間で消えない

`PipelineContext.cleanup()`（`src/pipeline/pipeline_context.py:171-194`）は
**パイプライン終了時に一括削除**する作りで、工程ごとの解放が無い。

| 生成物 | 生成箇所 | 1 時間素材の概算 | 解放時期 |
|---|---|---|---|
| `normalized.mp4` | `loudness_normalizer.py:246` | 入力とほぼ同じ（映像 `-c:v copy`） | 終了時 |
| `tl_part_00000..N.mov` | `renderer.py:_render_base` | 映像再エンコード + **PCM 音声**（`pcm_s16le` = 691 MB/時） | **連結直後に削除（`renderer.py:244-250`）** ✓ |
| `timeline_base.mov` | `renderer.py:206` | 同上（PCM のまま） | 終了時 |
| `timeline_subtitle.mp4` または `timeline_composite.mp4` | `renderer.py:481` / `548` | 出力とほぼ同じ | 終了時 |
| `blur_mask.mkv` / `blur_applied.mp4` / `watermark_applied.mp4` | ぼかし・透かし有効時のみ | 各 1 本分 | 終了時 |

ピークは**概ね素材 3 本分**。1 時間 / 1080p60 / crf20 なら **15〜20 GB を %TEMP% に要求する**。
アーカイブ切り抜きでは §3-2 の 20〜50 GB の**後に**これが積まれる。

### 3-5. 異常終了すると ③ も永久に残る

`tempfile.TemporaryDirectory` はプロセスが死ぬと後始末が走らない。
**起動時に古い `autoedit_*` を掃除する処理は無い**（`grep -rn "rmtree" src` に該当なし）。
開発端末の `%TEMP%` には `autoedit_*` が **85 件**残っていた。
「容量不足で落ちる → 残骸が増える → 次回はさらに空きが少ない」という**悪循環**になる。

### 3-6. 後始末の持ち主が居ない一時フォルダ（潜在）

`src/timeline/media_recovery.py:188-191`

```python
def _work_path(context):
    if context is not None and hasattr(context, "allocate_intermediate"):
        return context.allocate_intermediate(_RECOVERED_NAME)
    return os.path.join(tempfile.mkdtemp(prefix="autoedit_recover_"), _RECOVERED_NAME)
```

`context=None` の経路（`src/archive/project_resume.py:286-287`）では
**`mkdtemp` で作った素材 1 本ぶんのフォルダを誰も消さない**。
アーカイブ用プロジェクトは `source.media_id` が空で `body_id` 判定（`media_recovery.py:77`）に
入らないことが多いため常時発生ではないが、**設計として後始末の持ち主が居ない**のは明確な欠陥である。

### 3-7. 空き容量を見ている箇所が 1 つも無い

`disk_usage` / `free_space` / `ENOSPC` / `errno` の grep は `src` 配下で **0 件**。
その結果、

* 始める前に「足りない」と言えない
* 枯渇したときに「容量が足りません」と言えない（素の `[Errno 28]` が出る）
* 枯渇で落ちたのか別の理由で落ちたのか区別できない

### 3-8. 出力配置も部分ファイルを残す

`output_writer.finalize()`（`src/modules/output_writer.py:36-45`）は `shutil.move` のみ。
出力先が別ドライブだと copy+remove になり、**失敗時に中途半端な出力ファイルが残る**
（削除もログも無い）。

---

## 4. 原因B: 容量不足が「クラッシュ」になる

### 4-1. 今回の送出点（確定）

§2-3 のとおり `main_window.py:1525` の `cli()` に受けが無い。
**ここが唯一の、しかも 1 行で塞げる送出点**である。

### 4-2. GUI 側にも同じ穴がある（別件だが同時に塞ぐべき）

`main()` の GUI 経路（`:1528-1555`）にも `try/except` も `sys.excepthook` も無い。
起動処理中に `OSError` が出れば、今回と同じダイアログになる。

あわせて PySide6 6.11 の挙動を実測した
（`QT_QPA_PLATFORM=offscreen` で `QTimer.singleShot` から `OSError(28)` を送出）。

```
OSError: [Errno 28] No space left on device   ← stderr へ出るだけ
EXEC RETURNED: 0                              ← exec() は正常に戻る
```

**スロット内の例外は `app.exec()` の外へ出ない**。
windowed ビルドでは `sys.stderr` が `None` なので、
**GUI スレッドで容量不足が起きた場合は何の痕跡も残らず黙って無視される**。
今回のダイアログとは逆向きの問題だが、原因究明を妨げる点では同じである。

### 4-3. 既存コードは「自前の書き込み」はよく守れている

`save_settings` の呼び出し側、`telemetry._write_json`、`log_queue.enqueue`、
`auth_store.save`、`points._write_json`、`device_id._write`、`blur/store.save`、
`theme._indicator_assets`、`project_thumbnail.ensure`、
`timeline_editor_dialog._on_autosave` / `_copy_media_beside_project` / `_export_audio_sidecars`
は、いずれも `OSError` を捕まえて継続する作りになっている。

**守れていないのは「外部ライブラリに処理を丸ごと預けている箇所」**＝
今回の `cli()` であり、ここが抜けていた。

### 4-4. 被害

* 子プロセスのモーダルダイアログで**親の処理が止まる**（§2-4）
* `logs/autoedit_YYYYMMDD.log` に何も残らない（子プロセスはロガーを初期化していない）
* 48 GB のキャッシュが**消されないまま残る**（`cache.delete()` に到達しない）

---

## 5. 原因C: `ffprobe が失敗`

### 5-1. 最有力: 中途半端な `vod_<id>.mp4` を「取得済み」として通している

`src/archive/twitch_source.py:263-285`

```python
argv += ["download", str(video_id),
         "--quality", chosen,
         "--format", "mp4",
         "--output", out_template,
         "--skip-existing"]          # ← ここ
…
if not os.path.isfile(expected):     # ← 存在するかしか見ていない
    raise TwitchError(…)
```

twitch-dl 側の `--skip-existing` は**存在するかどうかしか見ない**。

```python
# twitchdl/commands/download.py:238-242
if target.exists():
    if args.skip_existing:
        click.echo(f"Video already downloaded: {green(target)}")
        return                        # ← 正常終了 (exit 0)
```

したがって、**結合（join）の途中で容量が尽きて中途半端な `vod_<id>.mp4` が残っていると**、
次の実行は

1. twitch-dl が「Video already downloaded」と言って **exit 0**
2. `os.path.isfile(expected)` も通る
3. `ArchiveAnalyzeWorker` が `pipeline.analyze(input_path, …)` へ進む（`archive_tab.py:131`）
4. 採点の最初の処理 `features.extract_cells()` が
   `ffmpeg_runner.probe_duration(input_path, …)` を呼ぶ（`src/archive/features.py:33`）
5. 壊れた mp4 なので **`FFmpegError("ffprobe が失敗")`**
6. `self.failed.emit(str(e))` → `archive_tab.py:834`
   → **「処理に失敗しました。\nffprobe が失敗」**

という経路をたどる。報告された 2 件目の文言と**完全に一致する**。

### 5-2. 文言に情報が無い理由（確定）

アーカイブタブの失敗通知は `str(e)` をそのまま使っている（`archive_tab.py:131-132` → `:834`）。
クリップ用タブが使っている `_format_failure_message`（`main_window.py:406-415`）とは**別実装**で、
`FFmpegError` が持っている `command`（= 対象パス）・`returncode`・`stderr_tail` を
**1 つも拾っていない**。

さらに `ffprobe` は `-v error` で起動しているため、
壊れたファイルでも stderr が短く、仮に拾っても十分な情報にならないことがある。
**対象パスとファイルサイズが出ないことが、切り分けを不可能にしている。**

### 5-3. 代替候補（ログで否定できるまで残す）

| 候補 | 内容 | 確定に必要な証拠 |
|---|---|---|
| C-2 | 入力動画そのものを `ffprobe` が開けない（ローカル動画モード、OneDrive の実体なしファイル、ネットワークドライブ、録画途中のファイル） | `ffprobe 実行: …`（DEBUG）行の対象パス |
| C-3 | `ffprobe.exe` が起動できない（セキュリティソフトによる隔離、同梱物の欠落） | returncode が異常値（`0xC0000135` 等） |

### 5-4. 上流の失敗を握り潰す構造も切り分けを妨げている

`loudness_normalizer.normalize_file()`（`src/modules/loudness_normalizer.py:221-229`）は
**FFmpeg の失敗理由を問わず**「元音声のまま継続」する。
音が正規化されないだけなら継続は正しいが、**容量不足で落ちた場合も継続してしまう**ため、
処理は空きの無いドライブへ書き続け、**最初に見える症状は原因と無関係な工程で出る**。
同じ構造は `probe_duration` 失敗（`:175`）、`has_audio_stream` 失敗（`:119-121`）にもある。

---

## 6. 修正設計

方針は 3 本立て。**(I) 容量を使い切らせない / (II) 使い切ったら必ず分かる / (III) 落とさない**。
既存実装は壊さず、既定値は現行の挙動を保つ（`docs/CLAUDE.md`「既存実装を破壊しない」「既存設定との互換性を失わない」）。

### 6-1. 【最優先】`__twitchdl__` 分岐を落とさない（原因B の直接の修正）

`src/gui/main_window.py:1522-1526`

```python
if len(sys.argv) > 1 and sys.argv[1] == "__twitchdl__":
    from twitchdl.cli import cli
    sys.argv = ["twitch-dl", *sys.argv[2:]]
    try:
        cli()                                   # click は standalone_mode で内部 sys.exit する
    except SystemExit:
        raise                                   # click の正常な終了経路は邪魔しない
    except BaseException as error:              # ダイアログを出さずに親へ伝える
        # 親は stdout を読んでいる。機械判読できる 1 行と、人が読む 1 行を出す。
        sys.stdout.write(f"__TWITCHDL_ERROR__ {type(error).__name__}: {error}\n")
        sys.stdout.flush()
        sys.exit(_EXIT_DISK_FULL if disk.is_no_space(error) else _EXIT_ERROR)
    return
```

* **ダイアログを出させない**ことが要点。子プロセスはコンソールもログも持たないため、
  親が読んでいる stdout に出すのが唯一の伝達経路である。
* `_EXIT_DISK_FULL`（例: 28）を親が見て、`TwitchError` ではなく
  **`DiskSpaceError` として「空き容量が足りません」**を出す（`twitch_source._run` / `download_vod`）。
* ついでに**子プロセスでもログを初期化**し、`logs/` に残るようにする
  （現状、子プロセスの出来事は 1 行もログに残らない）。

### 6-2. 新規: `src/utils/disk.py`（空き容量ユーティリティ）

標準ライブラリ `shutil.disk_usage` のみを使う（ライブラリ追加なし）。

```python
# 空き容量の確認と ENOSPC の判定 (error fromUser/20261022 resolve §6-2)
def free_bytes(path)                             # 直近の存在する親まで遡って空き容量を返す (取得不能なら None)
def ensure_free(path, required_bytes, label="")  # 不足なら DiskSpaceError を送出
def is_no_space(error)                           # OSError.errno == ENOSPC / winerror 112・39 を判定
def looks_like_no_space(stderr_tail)             # FFmpeg の stderr に "No space left" 等を含むか
def human(bytes_)                                # "12.3 GB" 形式 (ログと文言で共用)
```

`src/exceptions.py` へ `DiskSpaceError(AutoEditError)` を追加する
（`AutoEditError` 派生なので既存の `except AutoEditError` 経路がそのまま効く）。
Windows の `winerror 112 (ERROR_DISK_FULL)` も拾う（`shutil.copy2` 経由では `errno` が
`ENOSPC` にならない場合があるため）。

### 6-3. twitch-dl のキャッシュを管理下に置く

`src/archive/twitch_source.py:263-269`

1. **`--cache-dir` を明示する。**
   `os.path.join(work_dir, "cache", "{id}", "{quality}")` を渡す。
   これだけで **① が `archive_work` 配下へ移り、置き場が 1 つ減る**
   （`work_dir` を別ドライブへ移せば断片も一緒に移る）。
2. **失敗時にキャッシュを掃除する。**
   twitch-dl は成功時しか消さないため、`download_vod` が非 0 終了を受けたら
   当該 `cache/{id}` を削除する（再試行で続きから落とす運用にしたい場合は
   `keep_cache_on_error` 設定で選べるようにする / §8-2 7-6）。
3. **取得前に空きを確認する。**
   `list_qualities` で画質一覧は既に取っているため、
   「断片 + 結合後」= **VOD 1 本の 2 倍**を要求値として `disk.ensure_free` を呼ぶ。
   VOD の正確なサイズは事前に分からないため、既定は `min_free_space_gb` を使う（§8-2 7-5）。

### 6-4. `--skip-existing` をやめ、使える mp4 かどうかで判断する（原因C の修正）

`--skip-existing` は「存在すれば成功」であり、**壊れたファイルを素通りさせる**。

1. `download_vod` の冒頭で `expected` の状態を自分で確かめる。
   * 無い → そのまま取得
   * ある → **`probe_duration` が通り、尺が 0 より大きいか**を確認する
     * 通る → 取得をスキップ（従来の `--skip-existing` と同じ利点を維持）
     * 通らない → **壊れた残骸として削除**し、取得し直す（ログに ERROR で理由を残す）
2. twitch-dl へ渡すのは `--overwrite` とし、判断はアプリ側に一本化する。
3. 取得完了後も `os.path.isfile` だけでなく**同じ検証**を行い、
   壊れたまま採点へ進ませない。

これにより、**「ffprobeが失敗」という分かりにくい症状の代わりに
「前回の取得が壊れていたため取得し直します」という自明な挙動**になる。

### 6-5. 置き場を設定可能にする（`setting.json`）

`general` に 3 キー、`archive.download` に 2 キーを**追加**する。
既定値は「現行と同じ場所」とし、**追加だけで挙動が変わらない**ようにする
（`_merge_with_defaults` が新規キーを補完し `load_settings` が一度だけ書き戻す / `settings_window.py:1077-1096`）。

| キー | 既定 | 意味 |
|---|---|---|
| `general.work_directory` | `""` | パイプライン中間ファイルの置き場。空 = 従来どおり `%TEMP%` |
| `general.min_free_space_gb` | `20` | 開始前に要求する空き容量(GB)。`0` で確認しない |
| `general.cleanup_stale_work_hours` | `24` | 起動時に掃除する `autoedit_*` の経過時間。`0` で掃除しない |
| `archive.download.keep_source_days` | `0` | `archive_work` の取得物の保持日数。`0` = 無期限（§9-1 により既定を変更） |
| `archive.download.keep_cache_on_error` | `false` | 取得失敗時に断片キャッシュを残すか（再試行を速くしたい人向け） |
| `archive.download.keep_vod` | `false` | 元 VOD を残し続けるか。`true` で「セクション追加」を使い続けられる（§6-6-3） |
| `timeline.project.archive_keep_media` | `true` | アーカイブ用プロジェクトの隣へ**映像も**置く（§6-6-1） |
| `archive.download.legacy_cache_checked` | `false` | 旧データの回収を尋ねたか。一度答えたら二度と尋ねない（§6-6-6） |

設定画面（`src/settings/settings_window.py`）には
`output_dir_edit` と同じ `_make_dir_picker` / `_make_column_label` を使って行を足す。

* 一般タブ: 「作業フォルダ」（`:1586` の出力ディレクトリの直後）、「開始前に必要な空き容量(GB)」
* アーカイブタブ: `archive.download.work_dir`（**現在は手編集しか手段が無い**）、「取得した配信の保持日数」
* 読み込み `_load_settings`（`:2390` 付近）/ 収集 `_collect_settings`（`:2647` 付近）へ対応行を追加

さらに**空き容量と現在の使用量を設定画面に表示**する
（例: `D:\work （空き 312.4 GB） / 取得物 45.6 GB`）。
「どこに何 GB 置かれているか」を利用者が知る手段が今は無いことが、94 GB の滞留を招いている。

### 6-6. 元 VOD の寿命を「プロジェクト」に紐づける

#### 6-6-1. 考え方

滞留の本質は容量ではなく**所有者の不在**である。
`vod_<id>.mp4` は誰のものでもないまま作業フォルダに置かれ続けていた。
残っていた理由は 1 つだけで、**アーカイブ用プロジェクトを開き直すとき、
映像を元 VOD から切り直していた**ためである（`project_resume.py:89`
`clip_writer.cut_region(vod, …)`）。保存の既定が音声だけ（`keep_audio=True` /
`keep_media=False`）だったことによる。

そこで方針を次のとおりにする。

> **「続きから編集するのに要るものは、プロジェクトのフォルダへまとめて置く。
> 揃ったら元 VOD は手放す。」**

この入れ物 `<プロジェクト>.media/` は既に存在し動いている（実測:
`vod_2904967242.archive.media/` に `m1〜m8_audio.m4a` が 42.8 MB）。
足りないのは中身が音声だけであることなので、**映像も置く**ようにする。

| | 従来 | 本方針 |
|---|---|---|
| プロジェクトの隣に置くもの | 音声サイドカーのみ | **映像 + 音声**（自己完結） |
| 開き直しの手順 | 元 VOD から切り直す → 音声を貼り直す | **そのまま使う**（復旧処理が不要） |
| 元 VOD | 永久に残る | 自己完結したら**ゴミ箱へ** |
| 置き場 | `archive_work`（システムドライブ固定） | プロジェクト置き場（利用者が選べる） |

#### 6-6-2. 容量の実測（削減率は一定ではない）

| 項目 | 実測 |
|---|---|
| `vod_2904967242.mp4` | 45.0 分 / 549 MB（12.2 MB/分） |
| 同プロジェクトのクリップ合計 | **30.2 分 = VOD の 67%** |
| 映像も保存した場合 | 約 369 MB（**削減は 33% のみ**） |

**「プロジェクトの隣に置くほうが必ず小さい」は成り立たない。**
長めに切り出す使い方では 67% に達する。
4 時間配信から TOP10 を 2 分ずつ取るような使い方なら 8% 程度になり、振れ幅が大きい。

それでも採用する理由は容量ではなく**寿命**である。

* プロジェクトを消せば一緒に消える（孤児が出ない）
* 置き場が利用者の選んだフォルダになる（システムドライブを圧迫しない）
* 二重持ち（元 VOD + クリップ）が無くなる
* 開き直しが速くなる（切り直しの工程が丸ごと不要になる）

#### 6-6-3. 失うもの: セクション追加

採点で選ばれなかった区間を後から足す機能（`archive_timeline_dialog._section_add_available`）は
**元 VOD 全体を必要とする**ため、元 VOD を手放すと使えなくなる。

* 既定は「手放す」。`archive.download.keep_vod = true` で残す選択もできる
* 元 VOD が無い状態で開いたときは、**ボタンを押せない状態で出し**、
  使えない理由と戻し方をツールチップで伝える（黙って消さない）
* 編集そのものは素材がプロジェクト側にあるため続けられる

#### 6-6-4. 判定と後始末

元 VOD を捨てるのは **「それを元にした自己完結プロジェクトが在る」** ときだけとする。

* 自己完結 = `<プロジェクト>.media/` に**映像ファイル**が在る
  （音声サイドカーしか無い場合は切り直しが要るため**まだ必要**）
* プロジェクトの場所は編集画面と同じ決まりで求める
  （`<置き場>/<VOD名><archive_suffix><project_suffix>`）
* 捨て方は**ゴミ箱**。ゴミ箱を使えない環境では何もしない
* 実行はアーカイブ画面を開いたとき（`src/archive/vod_retention.py`）

#### 6-6-5. 作業フォルダ自体の回収手段

* アーカイブタブに**現在の使用量と「取得データを削除」ボタン**を置く（確認あり・ゴミ箱行き）
* `keep_source_days` による日数での自動削除も持つが、**既定は 0（無期限 = 何もしない）**
  （理由は §9-1）
* 削除対象は `vod_<数字>.mp4` / `chat_<数字>.json` の**完全一致**に限る。
  前方一致にすると同じフォルダの `vod_<id>.archive.json`（**取り直せない編集結果**）を
  巻き込む。これは実装中に実際に作り込んでいた欠陥である

### 6-6-6. 置き場の移動と旧データの回収（7-1 = a / 7-7 = a で決定）

#### 置き場をインストールフォルダの外へ出す（7-1 = a）

`archive.download.work_dir` が相対値のとき、`src/settings` 基準ではなく
**`%LOCALAPPDATA%\Stretheus\`** を基準にする（`config.work_base_dir`）。

* 取得物は 1 本 10〜25 GB になるため、**アプリの置き場に利用者データを置かない**
* インストール先は常にシステムドライブ（`AutoEdit.iss:52`）なので、
  移さない限り C: 固定からは逃れられない
* **既存の取得物は移さない**（起動時に GB 級のコピーを走らせない）。
  旧い場所は後述の「旧データの回収」で利用者へ知らせ、回収してもらう
* **Twitch のログイン情報だけは引き継ぐ**（`config._migrate_token`）。
  トークンは作業フォルダ配下にあり、移さないと**ログアウトしてしまう**。
  小さく、移さないと利用者が確実に困る唯一のファイルである
* 絶対パスで指定している利用者は対象外（自分で決めた場所を尊重する）

**アンインストールの扱い**: 外へ出したぶん `{app}` の削除では消えなくなるため、
`[UninstallDelete]` へ `{localappdata}\{#MyAppName}\archive_work` を明示する
（`installer/AutoEdit.iss`）。残すと「アンインストールしたのに容量が戻らない」ことになる。
編集結果と出力動画は利用者が選んだ別フォルダにあるため対象外。

#### 旧データの回収（7-7 = a）

旧版が残した次の 2 か所を、**起動時に 1 度だけ**尋ねて回収する
（`src/archive/legacy_cache.py`）。

| 対象 | 開発端末での実測 |
|---|---|
| `%LOCALAPPDATA%\twitch-dl`（旧い断片キャッシュ） | 48.38 GB |
| 旧い `archive_work`（インストールフォルダ配下） | 45.59 GB |

決め事:

* **実在するときだけ**対象にする。他ツールと共用しうる場所のため、
  無いものを作らず、存在も仮定しない
* **1 GB 未満なら尋ねない**（起動のたびに煩わせない）
* **無断では消さない**。必ず尋ね、**ゴミ箱へ送る**。ゴミ箱を使えない環境では何もしない
* 「いいえ」と答えた場合も覚える（`archive.download.legacy_cache_checked`）。
  毎回尋ねられる方が煩わしい

※ インストール先のフォルダ名は利用者によって `AutoEdit` と `Stretheus` の 2 通りがある
（v1.0.8 で `AppId` 据え置きのまま改名したため / §8-1）。
旧い場所は `src/archive/config.py` 自身の位置から解決するため、どちらでも正しく当たる。

### 6-7. 中間ファイルの置き場採用と早期解放（`PipelineContext`）

1. `_prepare_context`（`pipeline_runner.py:260-270`）で
   `working_dir=general.work_directory` を渡す。空なら従来どおり `%TEMP%`。
   明示指定時は `autoedit_<日時>_<pid>` のサブフォルダを掘り、**終了時にフォルダごと削除**する。
2. `release_intermediate(filename)` を追加し、
   「次の工程の出力が完成した時点で前段を捨てる」。`cleanup()` と削除処理を共有する。
3. `renderer.render()` で段をまたぐたびに前段を解放する
   （`timeline_base` → 焼き込み完了直後 / `normalized.mp4` → `timeline_base` 完成直後。
   後者は **§8-2 7-2 の確認が必要**）。ピークが素材 3 本分から **2 本分**へ下がる。
4. `cleanup()` は現状どおり最後に全削除（二重削除は `os.path.exists` で無害）。

### 6-8. 開始前・工程前の空き容量チェック

| 位置 | 内容 |
|---|---|
| `twitch_source.download_vod` の冒頭 | **断片 + 結合後の 2 本分**を確認（§6-3-3） |
| `pipeline_runner.run_pipeline`（`:68` の存在確認の直後） | 入力サイズと `min_free_space_gb` から作業ドライブ・出力ドライブの空きを確認。不足なら `DiskSpaceError` で**1 バイトも書かずに**中止 |
| `renderer._render_base` の冒頭 | part 合計 + base の概算で再確認 |
| `output_writer.run` の冒頭 | 中間ファイルサイズ + 余裕を出力ドライブで確認 |

文言（`tr` へ追加 / `src/i18n/catalog_en.py` にも対訳を追加）:

```
空き容量が足りません。
  場所 : {path}
  空き : {free}
  必要 : {required}
不要なファイルを削除するか、設定画面で作業フォルダを別のドライブへ変更してください。
```

### 6-9. 枯渇の検出と文言統一

* `ffmpeg_runner.execute()`: 失敗時に `disk.looks_like_no_space(stderr_tail)` が真なら
  `FFmpegError` ではなく **`DiskSpaceError`** を送出する。
* `loudness_normalizer.normalize_file()`: `except FFmpegError` の中で
  `DiskSpaceError` は**握り潰さず再送出**する。
  同じ扱いを `subtitle_generator.py:1044`、`media_sidecar.py:62/96` にも適用する。
* `output_writer.finalize()`: `shutil.move` を `try/except OSError` で囲み、
  `disk.is_no_space` なら**書きかけの出力を削除**してから `DiskSpaceError` を送出する。
* `twitch_source._run()`: 子プロセスの `_EXIT_DISK_FULL` / `__TWITCHDL_ERROR__` 行を見て
  `DiskSpaceError` へ変換する（§6-1）。

### 6-10. 起動時の掃除（新規: `src/utils/workspace.py`）

```python
def sweep_stale(base_dir=None, prefixes=("autoedit_", "autoedit_recover_"), max_age_hours=24)
```

* `main()` で `MainWindow` を作る**前**に呼ぶ。例外は内部で握って必ず戻る。
* `max_age_hours` 以内のものには触らない（多重起動時に走っている実行を壊さないため）。
* 削除したフォルダ数と回収した容量を INFO でログへ残す。
* `media_recovery._work_path` の `mkdtemp` はこの掃除の対象に含め、あわせて
  `project_resume.py:286` では `workdir`（既に `TemporaryDirectory` 管理下）配下を使うよう引数を追加し、
  **持ち主の居ない一時フォルダを新たに作らない**。

### 6-11. GUI 側の最後の砦

```python
def main():
    sys.excepthook = _show_fatal_error      # threading.excepthook も設定する
    try:
        ...                                  # 既存の起動処理
        sys.exit(app.exec())
    except SystemExit:
        raise
    except BaseException as error:
        _log_fatal(error)                    # logs へトレースバック
        _show_fatal_error(type(error), error, error.__traceback__)
        sys.exit(1)
```

* `disk.is_no_space(error)` が真なら、素の `[Errno 28]` ではなく §6-8 の文言を出す。
* 書き出し先が無いこともあるため、`_log_fatal` 自体も `try` で囲む。
* windowed ビルドで Qt が黙って捨てるスロット内例外（§4-2）もロガーへ流す。
* 編集内容の保全として `timeline.autosave_sec` の既定を `0` → `60` にする（**7-3 = a で決定**）。
  落ちると編集内容がすべて失われるため、最後の砦と対で入れる。
  書くのはプロジェクト JSON 1 本（実測 0.5 MB 前後）で、容量方針とは衝突しない。
  あわせて**「保存せずに閉じる」を選んだときは自動保存ファイルを消す**。
  自動保存は異常終了に備えるものであり、意図して捨てた編集を次回
  「復元しますか」と差し出すのは誤りで、置き場にも残り続けるため。

### 6-12. `ffprobe` の診断強化

1. **`probe_duration` / `probe_dimensions` の事前確認**（`src/modules/ffmpeg_runner.py`）

   | 状態 | 文言 |
   |---|---|
   | 存在しない | `対象ファイルがありません: {path}` |
   | 0 バイト | `対象ファイルが空です (0 バイト): {path}` |
   | 開けない | `対象ファイルを開けません: {path} ({error})` |

2. **失敗時の情報を増やす**

   ```
   ffprobe が失敗しました (returncode={code})
     対象 : {path}
     容量 : {size}
   ```

3. **`archive_tab.py:834` を `_format_failure_message` へ寄せる。**
   現在 `str(e)` のみで、`FFmpegError` の情報を 1 つも拾っていない（§5-2）。
   整形関数を `src/gui/` の共通処理へ移し、クリップ用・アーカイブ用で同じ表示にする。
   あわせて `returncode` と `command` の**対象パスのみ**を文言へ載せる
   （コマンド全体はログへ DEBUG で、画面には対象だけ）。
4. **`ffprobe` 自体の疎通確認**を `ensure_available`（`:93-99`）へ追加する。
   現在は `os.path.isfile` だけで「起動できるか」を見ていない。
   `ffprobe -version` を 1 回実行し、returncode が異常なら
   `FFmpeg を起動できません（セキュリティソフトに隔離されていないか確認してください）` を出す。
   これで候補 C-3 を即座に切り分けられる。

### 6-13. ログ出力方針（追加分）

| タイミング | レベル | 内容 |
|---|---|---|
| 子プロセス（`__twitchdl__`）開始・終了 | INFO | 引数の要約と終了コード（**現在は 1 行も残らない**） |
| VOD 取得前 | INFO | キャッシュ先・結合先・各ドライブの空き容量 |
| VOD 取得後 | INFO | 取得サイズ、キャッシュ削除の可否と回収容量 |
| 既存 mp4 の再利用判断 | INFO / ERROR | 「取得済みを再利用」/「壊れていたため取得し直し」 |
| パイプライン開始 | INFO | 入力サイズ / 作業ドライブ・出力ドライブの空き容量 |
| 各中間ファイル生成後・解放時 | INFO | パスとサイズ、残り空き、回収容量 |
| 起動時の掃除 | INFO | 削除したフォルダ数と回収容量 |
| 容量不足の検出 | ERROR | 場所・空き・必要量 |
| `ffprobe` 失敗 | ERROR | 対象パス・サイズ・returncode・stderr（全文） |
| 致命的例外 | CRITICAL | トレースバック全文（`logs/` と `crash_*.log` の両方） |

---

## 7. テスト方針

既存のテスト構成（`tests/test_*.py` / 標準 `unittest`）に合わせる。

`tests/test_disk_space.py`
* `free_bytes`: 存在しない深いパスでも直近の親まで遡って値を返す
* `is_no_space`: `OSError(errno.ENOSPC)` / `winerror 112` を真、`EACCES` を偽
* `looks_like_no_space`: FFmpeg の実文言サンプルで真、無関係な stderr で偽
* `ensure_free`: 不足で `DiskSpaceError`、`min_free_space_gb=0` で確認しない

`tests/test_twitchdl_child.py`（**今回の再発防止の本体**）
* `__twitchdl__` 分岐で `cli()` が `OSError(ENOSPC)` を送出しても
  **例外が外へ出ず**、`_EXIT_DISK_FULL` で終了する
* `cli()` が `SystemExit(0)` を出す正常系は従来どおり素通りする
* `_run` が `_EXIT_DISK_FULL` を受けたとき `TwitchError` ではなく `DiskSpaceError` になる

`tests/test_vod_reuse.py`（**原因C の再発防止**）
* 壊れた（probe が通らない）`vod_<id>.mp4` がある状態で `download_vod` を呼ぶと
  **削除して取得し直す**（「ffprobe が失敗」まで進まない）
* 健全な `vod_<id>.mp4` があれば取得をスキップして同じパスを返す
* 取得後の検証に失敗したら `TwitchError` になり、採点へ進まない

`tests/test_workspace_sweep.py`
* 古い `autoedit_*` を消し、新しいものを残す / 対象外の名前に触らない /
  削除できないフォルダがあっても例外を外へ出さない

`tests/test_pipeline_disk_guard.py`
* `shutil.disk_usage` をモックして空きゼロにし、`run_pipeline` が
  **中間ファイルを 1 つも作らずに** `DiskSpaceError` を送出する
* `general.work_directory` 指定時に中間ファイルがそこへ作られ、終了時に消える
* `release_intermediate` 後に前段ファイルが実在しない
* `loudness_normalizer.normalize_file` が `DiskSpaceError` を握り潰さず再送出する

既存テストへの追加
* `tests/test_settings_migration.py`: 新規 5 キーが補完され、既存値が壊れない
* `ffmpeg_runner`: 0 バイト / 存在しないパスで**それぞれ別の文言**になる

---

## 8. 影響範囲・互換性と確認事項

### 8-1. まだ確認したいこと

1 件目は Error.jpg で確定したため、残るは **2 件目の裏取り**である。

1. `<インストール先>\_internal\src\gui\logs\autoedit_<日付>.log`（および `logs\`）の該当時刻付近。
   ※ **インストール先のフォルダ名は利用者によって異なる**。アプリ名は v1.0.8（`bdb9569` /
   2026-07-18）で `AutoEdit` → `Stretheus` へ変更したが、`AppId`（`AutoEdit.iss:47`）を据え置き
   `UsePreviousAppDir` も未指定（既定 yes）のため、**v1.0.8 より前からの利用者は
   `%LOCALAPPDATA%\Programs\AutoEdit\` のまま**で、新規インストールのみ `…\Programs\Stretheus\` になる。
   案内するときはパスを直書きせず「ショートカットを右クリック →ファイルの場所を開く」で辿らせる。
   **「VOD 取得中…」の直後に `ffprobe 実行: …` が出ていれば §5-1 で確定**する。
   ローカル動画の採点だった場合は C-2 の線になる。
2. 2 つのエラーの**前後関係**（同じ VOD で続けて起きたのか）。
3. `<インストール先>\_internal\src\settings\archive_work` に残っている `vod_*.mp4` のサイズ一覧。
   **極端に小さい / 0 バイトのものがあれば §5-1 の物証**になる。
4. `%LOCALAPPDATA%\twitch-dl` の容量（開発端末では未完成 1 本で 48 GB あった）。
5. C: ドライブの空き容量。

※ 1〜4 はいずれも**利用者に操作を頼まずともログで足りる**ものが多い。
3・4 はフォルダを開いて容量を見てもらうだけで済む。

### 8-2. 設計判断として確認したいこと

| # | 論点 | 選択肢 | 備考 |
|---|---|---|---|
| **7-1** | `archive_work` の既定位置を `%LOCALAPPDATA%\Stretheus\archive_work` へ移すか | (a) 移す / (b) 位置は変えず UI と自動削除だけ | インストールフォルダ配下はアンインストールで消える利点がある。(a) ならアンインストーラへ削除処理が要る |
| **7-2** | `normalized.mp4` を `timeline_base` 完成直後に解放してよいか | (a) 解放する / (b) 残す | 編集画面プレビューや `keep_audio` のサイドカー保存が本編素材を参照しうる |
| **7-3** | `timeline.autosave_sec` の既定を `0` → `60` にするか | (a) する / (b) しない | クラッシュ時の保全に効くが、周期的に書くため容量方針と相反しうる |
| **7-4** | 中間音声を `pcm_s16le`/`.mov` から変える余地はあるか | (a) 変えない / (b) `flac` 等を検討 | A/V 同期対策（20260812 resolve3）で選ばれた値のため**独断では変更しない** |
| **7-5** | VOD 取得前の必要容量をどう見積もるか | (a) `min_free_space_gb` をそのまま使う / (b) 画質一覧の解像度から概算する | 尺は `list_qualities` では取れない。(b) は別途 API 呼び出しが要る |
| **7-6** | 取得失敗時に断片キャッシュを消すか残すか | (a) 既定で消す（`keep_cache_on_error=false`） / (b) 既定で残す | 残すと再試行が速いが、今回のように 48 GB が居座る。**(a) を推す** |
| **7-7** | 旧版が残した `%LOCALAPPDATA%\twitch-dl` を掃除するか | (a) 起動時に尋ねて消す / (b) 案内だけ出す / (c) 触らない | 他ツールと共用しうるフォルダのため**無断削除はしない**前提 |

### 8-3. 互換性

| 変更 | 互換性 |
|---|---|
| `__twitchdl__` 分岐の例外受け | 正常系（`SystemExit`）は素通りさせるため挙動不変 |
| `--skip-existing` → 自前検証 + `--overwrite` | **壊れたファイルだけ**挙動が変わる。健全なら従来どおりスキップ |
| `--cache-dir` の明示 | 断片の置き場が変わる。旧キャッシュは §6-6 で別途案内 |
| `setting.json` へ 5 キー追加 | 追加のみ。既定値は現行動作と同一 |
| `DiskSpaceError` 新設 | `AutoEditError` 派生のため既存の捕捉経路がそのまま効く |
| 中間ファイルの早期解放 | 削除タイミングのみ変更。出力は不変（§8-2 7-2 の確認が前提） |
| `archive_work` の既定位置変更 | **既存利用者に影響**（§8-2 7-1 の確認が前提） |
| `archive_tab` の失敗文言 | 情報が増えるのみ |

---

## 9. 実装状況（2026-10-22 時点）

本書 §9 の順に実装した。全 1477 件のテストが通ることを確認済み。

| # | 項目 | 状態 | 主な変更先 |
|---|---|---|---|
| 1 | §6-1 `__twitchdl__` 分岐の例外受け | **完了** | `src/gui/main_window.py` (`_run_twitchdl_child`) / `src/archive/twitch_source.py` (`_run`) |
| 2 | §6-2 空き容量ユーティリティ | **完了** | `src/utils/disk.py` (新規) / `src/exceptions.py` (`DiskSpaceError`) |
| 3 | §6-4 壊れた取得物の検出と取り直し | **完了** | `src/archive/twitch_source.py` (`_is_usable_video`) |
| 4 | §6-12 ffprobe の診断強化・文言統一 | **完了** | `src/modules/ffmpeg_runner.py` / `src/gui/failure_message.py` (新規) |
| 5 | §6-3 キャッシュの引き取りと後始末 | **完了** | `src/archive/twitch_source.py` (`--cache-dir` / `_cleanup_cache`) |
| 6 | §6-6 元 VOD の寿命管理・置き場の移動・旧データ回収 | **完了** | `src/archive/vod_retention.py` (新規) / `config.py` / `archive_tab.py` / `timeline_editor_dialog.py` / `archive_timeline_dialog.py`。`legacy_cache.py` (新規) / `installer/AutoEdit.iss`。削除方針は §9-1 のとおり設計から変更 |
| 7 | §6-5 設定追加 + 設定画面 | **完了** | `src/settings/settings_window.py`（一般タブに 2 行 + 空き容量の実測表示） |
| 8 | §6-8 / §6-9 事前チェックと文言統一 | **完了** | `pipeline_runner` / `output_writer` / `loudness_normalizer` / `subtitle_generator` / `media_sidecar` |
| 9 | §6-11 GUI 側の最後の砦 | **完了** | `src/gui/main_window.py` (`main` / `_main` / `sys.excepthook`) |
| 10 | §6-7 早期解放 / §6-10 起動時の掃除 | **一部完了** | `pipeline_context` / `renderer` / `src/utils/workspace.py` (新規)。`normalized.mp4` の解放は**見送り**（§8-2 7-2） |

### 9-1. 設計からの変更: 取得物の削除方針（事故を受けて）

§6-6 では `keep_source_days` を **既定 7 日**とし、アーカイブ画面を開いたときに
過ぎたものを `os.remove` で削除する設計にしていた。実装後の動作確認で
**画面を 1 つ作っただけで開発端末の取得物 45 GB が消え、復旧できなかった**。

取得物は**利用者が何時間もかけて取得した素材**であり、
「画面を開いただけで」「取り消せない形で」消えるのは、容量不足そのものより損害が大きい。
次の 2 点へ改めた。

| 項目 | 設計 (§6-6) | 実装 |
|---|---|---|
| `keep_source_days` の既定 | `7`（自動削除が既定で有効） | **`0` = 無期限**。自動削除は利用者が明示的に有効化したときだけ働く |
| 削除の方法 | `os.remove`（完全削除） | **ゴミ箱へ送る**（`src/utils/trash.py`）。ゴミ箱を使えない環境では**何もしない** |

回収の主な手段は、画面の「取得データを削除」ボタン（使用量の表示 + 確認ダイアログ + ゴミ箱行き）とする。
**見えるようにすること**が本来の目的であり、黙って消すことではなかった。

この不変条件は `tests/test_archive_source_retention.py` が守る
（既定では何も消さない / `os.remove` を呼んだら失敗する）。

新規テスト（88 件）: `tests/test_disk_space.py` / `tests/test_workspace_sweep.py` /
`tests/test_vod_reuse.py` / `tests/test_pipeline_disk_guard.py` /
`tests/test_archive_source_retention.py` / `tests/test_vod_retention.py` /
`tests/test_legacy_cache.py` / `tests/test_autosave_default.py` /
`tests/test_section_progress.py` / `tests/test_points_indicator_shutdown.py` /
`tests/test_project_media_copy.py`。
`src/i18n/catalog_en.py` へ 25 件の対訳を追加。

既存テストの修正: `tests/test_watermark_integration.py` は入力に `__file__`（.py）を
渡しているため、新設した入力の事前検査と空き容量確認を当該テストでは無効化した。

**§8-2 の判断事項（すべて決定済み）**:

* ~~**7-1** `archive_work` の既定位置の変更~~ → **(a) で決定・実装済み**（§6-6-6）
* ~~**7-3** `timeline.autosave_sec` の既定変更~~ → **(a) で決定・実装済み**（0 → 60 秒）
* ~~**7-4** 中間音声コーデックの見直し~~ → **(a) で決定: 変えない**（§9-2）
* ~~**7-7** 旧版が残した取得キャッシュの掃除~~ → **(a) で決定・実装済み**（§6-6-6）
* **7-2** `normalized.mp4` の早期解放（Timeline の素材として参照され続けるため見送り）

### 9-2. 中間音声コーデックは変えない（7-4 = a で決定）

`timeline.render.intermediate_audio_codec = "pcm_s16le"` /
`intermediate_container = ".mov"` は**現状維持**とする。

この値は A/V 同期対策として選ばれたものである（`docs/error/20260812/resolve3.md`）。
部品を AAC で書くとエンコーダ遅延（1024 サンプル = 21.3ms）が 1 部品ごとに尺へ乗り、
concat での連結で部品数ぶん累積してテロップが先行する。非圧縮にすることでこれを断っている。

容量だけを見れば可逆圧縮（`flac` 等）で 1 時間あたり 0.7 GB → 0.3 GB 程度に下げられる
見込みはあるが、**実測で同期が崩れないことを確かめずに触ってよい場所ではない**。
容量問題は §6-7（中間ファイルの早期解放）と §6-3〜§6-6（置き場と寿命）で対処済みであり、
同期という「直しにくい不具合」の再発と引き換えにするだけの利得がない。

将来見直す場合は、本書とは別に
「部品数を変えた対照実験で尺の誤差が累積しないこと」を示す検証を先に行うこととする。

### 9-3. 実機確認で見つかった不具合（セクション追加 / 2026-10-22）

#### 症状

「セクション追加」を押すと準備は最後まで成功するのに、直後に
`AttributeError: 'NoneType' object has no attribute 'setLabelText'` が出た。

#### 原因（既存の不具合。本対応が“見える化”した）

モーダルの `QProgressDialog.setValue()` は**内部で `processEvents()` を回す**。
その最中にワーカーの完了通知が届き、
`_on_section_ready` → `_close_section_progress` が走って
`self._section_progress` が `None` になる。
`setValue()` から戻った**次の行**で `None` を触って落ちていた
（`archive_timeline_dialog._on_section_progress`）。

```python
if self._section_progress is not None:
    self._section_progress.setValue(...)      # ← この中で再入し、None にされる
    self._section_progress.setLabelText(...)  # ← 落ちる
```

本対応より前から存在していたが、PySide6 はスロット内の例外を `exec()` の外へ出さず、
windowed ビルドでは `sys.stderr` が `None` のため**痕跡すら残らなかった**（§4-2）。
§6-11 の `sys.excepthook` を入れたことで初めて表に出た。

#### 修正

**値の更新を最後に呼ぶ**。再入から戻った先に触るものが無くなる。

```python
dialog = self._section_progress
if dialog is None:
    return
dialog.setLabelText(label)
dialog.setValue(...)        # 最後に呼ぶ
```

回帰テスト: `tests/test_section_progress.py`
（`setValue()` の中で進捗ダイアログを捨てる偽ダイアログで再入を再現する。Qt は起動しない）

#### あわせて直した文言の誤り

受けそこねた例外に対して「**予期しないエラーで終了します**」と出していたが、
スロット内の例外では **PySide6 は処理を続けるため、アプリは終了しない**。
実際この報告でも終了していない。案内が嘘になっていたため、
「終わる場合」と「続けられる場合」で文言を分けた
（`main_window._show_error_dialog(error, fatal)`）。

### 9-4. 実機確認で見つかった不具合（終了時 / 2026-10-22）

#### 症状

セクションを追加 → Timeline 画面を閉じる → メイン画面を閉じる、の順で操作すると

```
予期しないエラーが発生しました。この操作は完了できませんでしたが、作業は続けられます。
libshiboken: Internal C++ object (PySide6.QtWidgets.QPushButton) already deleted
```

※ セクション追加そのものは成功している（ログ上は挿入・保存とも完了）。
**終了時にだけ**起こる別件。

#### 原因（既存の不具合。§6-11 が“見える化”した）

```
File "src/gui/main_window.py", line 1288, in <lambda>
    lambda *_a: self.points_indicator.reload())
File "src/gui/points_indicator.py", line 194, in _apply_state
    self.login_button.setVisible(not logged_in)
RuntimeError: libshiboken: Internal C++ object
    (PySide6.QtWidgets.QPushButton) already deleted.
```

設定画面 / サブスクリプション画面を閉じたら残高を取り直すため、
`destroyed` へ繋いでいた（ver5 resolve §5.6 / ver6 resolve2 §4.2）。

`destroyed` は **C++ 側の破棄時に飛ぶ通知**であり、**アプリ終了時にも飛ぶ**。
そのときメイン画面は既に破棄されていて、残高インジケータの子である
「ログイン」ボタンの C++ 側も解放済み。そこへ `setVisible()` を投げて落ちていた。

これも §9-3 と同じく**元からあった不具合**で、
windowed ビルドでは `sys.stderr` が `None` のため痕跡が残らず、
§6-11 の `sys.excepthook` を入れて初めて表に出た。

#### 修正

`PointsIndicator.reload()` の入口で **C++ 側が生きているかを確認**し、
解放済みなら何もしない（終了中に残高を取りに行く意味も無い）。

```python
def reload(self):
    if not isValid(self) or not isValid(self.login_button):
        return
    self._apply_state()
    self.refresh(force=True)
```

`shiboken6.isValid` は PySide6 に同梱の判定関数で、
`src/dist/Stretheus/_internal/shiboken6` として既に配布物へ入っている
（ライブラリの追加には当たらない）。

親の破棄で子ボタンも同時に無効になることは実機で確認した
（`shiboken6.delete(parent)` 後、親・子とも `isValid` が False になり、
`setVisible()` が報告と同一の文言で `RuntimeError` を出す）。

回帰テスト: `tests/test_points_indicator_shutdown.py`
（`isValid` を差し替え、解放済み / ボタンだけ解放済み / 生存中の 3 系統。Qt は起動しない）

#### 設計書へ残す確認事項（推測実装しない）

`destroyed` は「閉じた時」ではなく「Python の参照が切れた時」に飛ぶため、
**設定画面でログイン / ログアウトしても、閉じた直後には残高表示が切り替わらない**
（次に設定画面を開いた時点で切り替わる）。

今回の落ちる不具合とは別の話で、直すなら
`SettingsWindow` / `SubscriptionWindow` に `closed` シグナルを持たせて
`closeEvent` で発火させる形になる。**利用者への影響が小さく、
挙動の変更を伴う**ため、今回は触らずここへ記録するだけとする。

### 9-5. 実機確認で見つかった不具合（保存のたびに素材が増える / 2026-10-22）

#### 症状

アーカイブプロジェクトを 2 回保存したあとの `.media` フォルダ:

```
m1_normalized.mp4        36.9 MB   ← 1 回目の複製（もう誰も参照していない）
m1_m1_normalized.mp4     36.9 MB   ← 2 回目の複製（プロジェクトはこちらを指す）
```

実測: 合計 **803.4 MB** のうち **397.2 MB（9 ファイル）が孤児**。
保存 1 回につき素材 1 本ぶん（数百 MB）が増え続ける。
§6-6 で「VOD を消す代わりに素材を隣へ置く」とした狙いが台無しになる。

#### 原因

`_copy_media_beside_project` は複製後に `media.path` を**複製先へ張り替える**。
次の保存では `media.path` が既に `.media` の中を指しているため、
そこから組み立てた複製先の名前に ID 接頭辞がもう一度付く。

```python
dest = os.path.join(folder, f"{media.id}_{os.path.basename(media.path)}")
#                             m1_      +  m1_normalized.mp4  →  m1_m1_normalized.mp4
```

名前が変わるので「同じ内容が既にあるなら複製し直さない」判定（サイズ比較）も空振りする。

#### 修正

**既に複製先フォルダの中を指しているなら複製しない**（名前も組み直さない）。

```python
if _in_folder(media.path, folder):
    dest = media.path       # 前回の保存で複製済み
else:
    dest = os.path.join(folder, f"{media.id}_{os.path.basename(media.path)}")
    ...                     # 従来どおり複製する
```

回帰テスト: `tests/test_project_media_copy.py`
（1 回目は複製する / 2 回目は複製し直さない / 5 回保存しても 1 件のまま。Qt は起動しない）

#### 既に出来てしまった孤児の始末（2026-10-22 実施）

利用者の確認を得て、**9 ファイル / 397.2 MB をゴミ箱へ送った**（§9-1 の方針どおり
`src/utils/trash.py` 経由。完全削除はしない）。

実施前に「プロジェクトを読み直して参照されている名前を集め、そこに無いものだけ」を
対象にし、実施後に `media_pool` の全パスが実在することを確認した（参照切れ 0 件）。

```
.media 合計:  803.4 MB → 406.3 MB（10 件）
```

**参照されていない `.media` 内のファイルを自動で片付ける仕組みは入れていない。**
上記の修正で孤児は新たに生まれないため、現時点では不要と判断した
（必要になれば「保存時にゴミ箱へ送る」形になるが、削除を伴うため改めて確認を取る）。

### 9-6. 実機確認で見つかった不具合（自己完結プロジェクトが開けない / 2026-10-22）

#### 症状

アーカイブ切り抜きを**新規に実行して保存**し、あらためて「編集の続き」から
同じプロジェクトを開くと「**素材が見つかりません**」の画面が出て先へ進めない。

利用者が「参照...」で確かめたところ、素材はすべて揃っていた:

```
%LOCALAPPDATA%\Stretheus\archive_work\
  vod_2905853277.archive.timeline.json          287 KB
  vod_2905853277.archive.media\
    m1_normalized.mp4   39.0 MB
    m2_normalized.mp4  115.9 MB
    m3_normalized.mp4   38.7 MB
    m4_normalized.mp4   77.3 MB
    m5_normalized.mp4   38.8 MB
    m6_normalized.mp4   38.8 MB
    m7_normalized.mp4   38.7 MB        ← m1〜m7 すべて実在
```

プロジェクト JSON を読み直して突き合わせると、`media_pool` の 7 件は
**全件そのままのパスで実在する**（参照切れ 0 件）。
無いのは元 VOD 1 本だけだった:

```
source.input_path       = …\archive_work\vod_2905853277.mp4   → 存在しない
source.archive.vod_path = …\archive_work\vod_2905853277.mp4   → 存在しない
```

#### 原因（§6-6 の副作用。素材の問題ではない）

`archive/project_resume.py` は、**素材を復旧する前に元 VOD を必須にしている**。

```python
# _run_from_archive_project
timeline, meta = project_io.load_project(load_path, …, validate_timeline=False)
…
# 元 VOD が見つからなければ差し替えを尋ねる (無いと映像を復元できない)
_ensure_vod(timeline, relink_callback)        # ← ここで止まる
…
media_recovery.recover(timeline, …)           # ← ここまで到達しない
```

`_ensure_vod` は VOD が無いと `relink_callback` を呼ぶ。GUI では
`MediaRelinkBridge` → `MissingMediaDialog` で、この画面の見出しが
「素材が見つかりません」である。つまり**素材ではなく VOD を尋ねていた**のに、
文言のうえでは素材が足りないように見えていた。

VOD が消えているのは不具合ではなく、§6-6 でそう決めたとおりの動きである。

1. 保存時に `archive_keep_media`（既定 `true`）が `.media/` へ映像素材を複製する
2. `vod_retention.is_self_contained()` が「`.media/` に映像がある = 自己完結」と判定する
3. 次の起動で `archive_tab._sweep_old_sources()` が元 VOD をゴミ箱へ送る

```
# 判定の根拠は .media/ の中身（音声サイドカーだけでは不足）
return any(name.lower().endswith(_VIDEO_SUFFIXES) for name in names)
```

**「元 VOD を手放せるようにするために素材を隣へ置く」のに、
置いた素材を見る前に元 VOD を要求していた**ため、§6-6 を入れた環境では
新規実行 → 保存 → 再起動 → 「編集の続き」が必ずこの画面で止まる。

`_ensure_vod` が書かれた時点（ver3 resolve9 §5.7）の既定は `keep_audio`
（音声だけ保存・映像は開くときに VOD から切り直す）だったため、
「VOD が無ければ何も復元できない」は当時は正しかった。
`archive_keep_media` を既定にした §6-6 で前提が変わったが、ここが追従していない。

#### 被害

- 自己完結プロジェクト（= §6-6 の狙いどおりに運用した場合）が**一切開けない**
- 画面に出るのは「素材が見つかりません」で、利用者には理由が分からない
- `MissingMediaDialog` は実在する任意の動画を受け付けるため、
  ここで `.media\m1_normalized.mp4` を選ぶと**それを元 VOD として切り直す**
  （区間が合わず、別のクリップの中身が入った素材が出来る）。
  利用者の「参照すると .media の中にファイルがある」という確認は、
  まさにこの誤選択の一歩手前だった。

#### 修正: 元 VOD は「要るときだけ」尋ねる

開く前の必須チェックをやめ、**実際に VOD から切り直す素材が出てきた時点で**
初めて解決する（遅延評価 + 記憶）。判定順は `media_recovery.recover` が既に持っている:

```
① media.path がそのまま在る        → .media/ の複製はここで終わる（VOD 不要）
② プロジェクトからの相対パスで在る → フォルダごと移した場合もここ（VOD 不要）
③' source_recover (= 本件)         → ここで初めて VOD が要る
```

`project_resume.py`:

- `_run_from_archive_project` から `_ensure_vod` の事前呼び出しを外す
- `make_vod_resolver(timeline, relink_callback)` を追加する
  （1 回目の呼び出しで `_ensure_vod` を通し、結果を覚える。
  解決できなければ従来と同じ `InputError` を投げる）
- `make_media_recover(..., vod_resolver=None)` を追加し、
  `recover_one` の中で `_vod_path(source)` の代わりに呼ぶ
  （`vod_resolver` 未指定なら従来どおり。CLI・テストの既存呼び出しは無改造で動く）

```python
def recover_one(media, source):
    entry = archive_timeline.archive_media_entry(timeline, media.id)
    if entry is None:
        return None                 # アーカイブのクリップ素材ではない
    vod = vod_resolver() if vod_resolver is not None else _vod_path(source)
```

これで

- 自己完結プロジェクト（`archive_keep_media`）→ VOD を尋ねずに開ける
- `keep_audio` だけのプロジェクト → 従来どおり VOD を尋ね、無ければ
  「元の配信アーカイブ（VOD）が見つかりません」で止まる（文言も従来どおり）

#### あわせて直す: VOD が無いときのイントロカード

テーマ付きクリップのイントロカードは、クリップ開始の `buffer_sec` 秒**手前**の
映像から作る（`clip_writer._build_intro_card`）。この区間は素材（`vod_start` 始まり）
には含まれないため、**元 VOD が無いと作れない**。

現状は ffmpeg が失敗し `_decorate_clip` が例外を握りつぶすため、
ログにはスタックトレースだけが残り、理由が読み取れない。
入力が無いことを先に見て、理由の分かる INFO を出して省略する。

本編・左上テーマタグ・字幕・結合はすべて素材だけで作れるため、
**失われるのはイントロカードだけ**である。

> **追記（2026-10-22）**: 利用者の指示により、**保存済みプロジェクトでも
> イントロカードを作れるようにした**（→ §9-7）。保存時にイントロの素
> （クリップ開始の直前 `buffer_sec` 秒 / 約 3 MB）だけを `.media/` へ控え、
> 元 VOD が無ければそちらを使う。省略へ落ちるのは、控えも元 VOD も無い
> （この修正より前に保存した）プロジェクトだけになった。

#### 残る確認事項（推測実装しない）

- ~~イントロカードを保存済みプロジェクトでも作りたい場合~~
  → **§9-7 で対応済み**（保存時にイントロ用の数秒だけ別ファイルで残す案を採った）。
- `MissingMediaDialog` で「元 VOD」を尋ねるとき、素材用の文言のままでよいか
  （本修正でこの画面が VOD のために出るのは `keep_audio` 運用だけになる）。

#### 回帰テスト

`tests/test_archive_resume_without_vod.py`

- `.media/` に素材が揃っていれば、元 VOD が無くても開ける（再リンクを 1 度も尋ねない）
- 素材が無いプロジェクトは従来どおり VOD を尋ね、答えが無ければ `InputError`
- 尋ねた答えは `source.input_path` と `archive.vod_path` の両方へ書き戻る（回答 Q7 のまま）

### 9-7. 保存済みプロジェクトでもイントロカードを作る（2026-10-22）

#### §9-6 で先送りした「残る確認事項」への回答

利用者の指示: **保存済みプロジェクトでもイントロカードを作れるようにする。**

あわせて「イントロカードは Timeline 画面の決定ボタン押下後に作っていたのでは?」
という確認を受けた。**その認識は正しい。** 作るのは決定ボタンの後である:

```
result_callback (= Timeline 画面) が返る            ← 決定ボタン
  → clip_writer.finish_clips(input_path, …)
      → _render_clips / _burn_clips
          → _decorate_clip(input_path, …)
              → _build_intro_card(source=input_path, clip_start=clip["start"], …)
                    ffmpeg -ss (clip_start - buffer_sec) -t buffer_sec -i <input_path>
```

§9-6 で問題にしたのは**タイミングではなく素 (入力)** である。
`source` は `input_path` = **元 VOD** で、`clip_start` は **VOD 内の絶対秒**。
つまりイントロカードは「VOD のクリップ開始より手前」を切って作っており、
その区間は**クリップ素材に 1 フレームも入っていない**。

```
VOD    ───────────┬──────────────────────┬───────────────
                  │                      │
         vod_start - buffer_sec      vod_start            vod_end
                  └─ イントロの素 ──────┘└─ クリップ素材 (cut_region) ──┘
                     (素材には入らない)
```

そのため、決定ボタンの後に作っていても、元 VOD を手放した時点で素が無くなる。

#### 方針: 保存時に「その数秒だけ」控えておく

音声サイドカー（§3-1 案D）と同じ考え方で、**イントロの素だけ**を
`<プロジェクト名>.media/<media_id>_intro.mp4` へ残す。
既定 `buffer_sec = 3.0` 秒ぶんなので、素材 1 本（39〜116 MB の実測）に対して
**3 MB 程度**で済む。

採らなかった案:

- **切り出しを `buffer_sec` 秒手前からにする**（素材の先頭にイントロ区間を含める）
  → `archive.media.vod_start` は素材の先頭 = VOD の時刻という前提で、
  `media_vod_ranges` / 無音カットの区間 / ぼかし / 字幕がすべてこれに乗っている。
  全素材の時間軸が `buffer_sec` ずれるため、保存済みプロジェクトとの互換も壊れる。
- **保存時に完成したイントロカードを作る**
  → テーマは保存後にも変えられるため、作り直しの口が無くなる。

#### 実装

**① 控えを作る（`src/archive/clip_writer.py`）**

```python
INTRO_SOURCE_SUFFIX = config.INTRO_SOURCE_SUFFIX     # "_intro.mp4"

def intro_source_path(media_dir, media_id): …        # 置き場を組む
def existing_intro_source(media_dir, media_id): …    # 既に在れば返す (0 バイトは無効)
def export_intro_source(vod_path, clip_start, buffer_sec, dest, ffmpeg_cfg): …
```

`export_intro_source` は `cut_region`（`-c copy`）ではなく**再エンコード**する。
`-c copy` はキーフレームまでしか戻れず、開始位置と尺が前後して
「クリップ開始の直前 `buffer_sec` 秒」にならない。
`-ss` を `-i` の前に置いた再エンコードはキーフレームから復号して捨てるため、
元 VOD から直接切るのと**同じ区間がフレーム正確に**得られる。

実測（`testsrc` 12 秒 / GOP 60 / clip_start=5.0 / buffer_sec=3.0）:

```
控えの尺                       : 3.000 s (期待 3.000)
控えの先頭 vs VOD の 2.0 秒     : PSNR 54.99 dB   ← 一致 (差は再エンコードぶんだけ)
控えの先頭 vs VOD の 2.5 秒     : PSNR 20.52 dB   ← 別フレーム
控えの 2.9 秒 vs VOD の 4.9 秒  : PSNR 50.11 dB   ← 一致
控えの 2.9 秒 vs VOD の 5.4 秒  : PSNR 18.84 dB   ← 別フレーム
```

**② 保存時に残す（`src/gui/timeline/`）**

`save_project` に種別ごとのフックを足す（既定は何もしない）:

```python
if self._keep_media_for_kind():
    self._copy_media_beside_project(target)
    self._keep_extra_beside_project(target)      # ← 追加
```

`ArchiveTimelineDialog._keep_extra_beside_project` が素材 1 本ごとに
`archive.media` の `vod_start` を引いて控えを作る。
既に在るものは作り直さない（保存のたびに ffmpeg を回さない / §9-5 と同じ考え方）。
元 VOD が既に無いときは何もしない（既存の控えはそのまま使う）。

**③ 書き出しで使う（`src/archive/`）**

```python
# _build_intro_card: 素は ① 元 VOD → ② 保存しておいた控え の順
if source and os.path.exists(source):
    input_args = ["-ss", f"{start_buf:.3f}", "-i", source, "-t", f"{length:.3f}"]
elif intro_source and os.path.exists(intro_source):
    input_args = ["-i", intro_source, "-t", f"{length:.3f}"]   # 既にその区間だけ
else:
    …省く (§9-6 の文言つき INFO)
```

控えのパスは `rebuild_prepared(timeline, media_dir=…)` が
`prepared["intro_source"]` へ載せ、`clip_meta` 経由で `_decorate_clip` へ渡る。
**新規実行では元 VOD が在るため従来どおり元 VOD から切る**（挙動は変えない）。

**④ 自己完結の判定から除く（`src/archive/vod_retention.py`）**

`is_self_contained` は `.media/` に `.mp4` が 1 つでもあれば自己完結と判定していた。
`_intro.mp4` もこれに当たるため、**素材の複製が失敗してイントロの控えだけが
出来た状態**を自己完結と誤判定し、まだ要る元 VOD を捨てる危険があった。
`_intro.mp4` は数えないようにする。

#### 設定

`timeline.project.archive_keep_intro_source`（既定 `true`）。
`archive_keep_media` が `false` のときは何もしない（元 VOD が残るため要らない）。
`archive.intro_card.enabled` が `false` / `buffer_sec` が 0 のときも作らない。

#### 既にある 2 つのプロジェクトについて

`vod_2904967242` / `vod_2905853277` は、この修正より前に保存したため控えが無い。
元 VOD は**ゴミ箱にある**ので、戻してから一度上書き保存すれば控えが作られる
（戻さない場合、この 2 つのイントロカードだけは作れない。本編・左上テーマタグ・
字幕・結合は素材だけで作れるため影響しない）。

#### 残る確認事項（推測実装しない）

- セクションを削除しても `.media/<media_id>_intro.mp4` は残る（1 件 3 MB 程度）。
  §9-5 と同じ方針で、参照されていない `.media` 内のファイルを自動で片付ける
  仕組みは入れていない（削除を伴うため、必要になれば改めて確認する）。

#### 回帰テスト

`tests/test_archive_intro_source.py`（18 件 / ffmpeg はモック）

- 置き場の組み立てと「0 バイトは無効」
- クリップ開始の直前 `buffer_sec` 秒を**再エンコードで**取ること
  （`-ss` の値 / `-c copy` でないこと）
- VOD の先頭でバッファが取れない・`buffer_sec` が 0・元 VOD が無い場合は作らないこと
- ffmpeg が失敗しても保存を止めず、書きかけを残さないこと
- `_build_intro_card` が ①元 VOD → ②控え の順で素を選び、どちらも無ければ省くこと
- イントロの控えだけを自己完結と誤判定しないこと
- 保存フックが素材 1 本ごとに控えを作り、2 回目の保存では作り直さないこと

`tests/test_archive_resume_without_vod.py` に 2 件追加
（控えがあれば `prepared["intro_source"]` へ載る / `media_dir` 省略でも壊れない）

---

### 9-8. 「編集の続き」の履歴に同じプロジェクトが 2 行並ぶ（2026-10-22）

#### 症状

`setting.json` の `timeline.project.recent_archive` に同じプロジェクトが 2 行:

```json
"recent_archive": [
    "C:/Users/evolu/AppData/Local/Stretheus/archive_work/vod_2905853277.archive.timeline.json",
    "C:\Users\evolu\AppData\Local\Stretheus\archive_work\vod_2905853277.archive.timeline.json",
    "C:\Users\evolu\AppData\Local\Stretheus\archive_work\vod_2904967242.archive.timeline.json"
]
```

「編集の続き」のコンボ（`project_resume_row._recent`）は設定を直に読むため、
**同じ名前が 2 行並ぶ**。履歴の枠（`recent_limit`、既定 10）も 1 つ無駄に埋まる。

#### 原因

`QFileDialog` は Windows でも **"/" 区切り**のパスを返す。
`save_project(ask=True)`（「名前を付けて保存」）で選んだパスはそのまま
`_remember_recent` へ渡り、突き合わせは**文字列の完全一致**だった。

```python
recent = [p for p in (project_cfg.get(key) or []) if p and p != path]
```

パイプラインが `os.path.join` で組んだ "\" 区切りのパスとは別物に見えるため、
同じプロジェクトが 2 行積まれる。

#### 修正

突き合わせ用の助けを `src/timeline/builder.py` へ置く
（`project_io` は `builder` を読む側のため、共通の置き場は `builder` になる）。

```python
def recent_path(path):   # 履歴へ積む形 (normpath + abspath)
def recent_key(path):    # 突き合わせの鍵 (normcase。Windows は大文字小文字を区別しない)
def dedupe_recent(entries):  # 重複を落として書き方をそろえる (並び順はそのまま)
```

適用先:

| 場所 | 変更 |
|---|---|
| `save_project` | 保存先を `recent_path()` でそろえる（"/" を持ち込ませない） |
| `_remember_recent` | `dedupe_recent([path] + 既存)` で積む（**既存の重複もここで畳まれる**） |
| `timeline_config` | `recent` / `recent_archive` を読む時点で畳む |
| `project_resume_row._recent` | 生の設定ではなく `timeline_config` 経由で読む |
| `project_io.forget_recent` | 同じ鍵で突き合わせ、リネーム先も `recent_path()` で書く |

`setting.json` に既に入っていた重複 1 件は、確認のうえ削除した（3 行 → 2 行）。

#### 回帰テスト

`tests/test_recent_project_paths.py`（8 件）

- 区切り文字・大文字小文字の違いを同じプロジェクトとして畳むこと
- 先に出てきたものを残すこと（新しい順の並びを崩さない）
- `timeline_config` が読む時点で畳むこと
- `forget_recent` が区切り文字をまたいで突き合わせ、正規化したパスを書くこと

---

## 10. 実装順序（レビュー後）

1. **§6-1 `__twitchdl__` 分岐の例外受け**（今回の死因そのもの。1 ファイルで塞げる）
2. **§6-2 `src/utils/disk.py` + `DiskSpaceError`**（以降すべての土台）
3. **§6-4 壊れた `vod_*.mp4` の検出と取り直し**（2 件目の直接の修正）
4. **§6-12 `ffprobe` の診断強化 / `archive_tab` の文言統一**（切り分け手段）
5. **§6-3 キャッシュを `work_dir` 配下へ + 失敗時の掃除**（48 GB 級の回収）
6. **§6-6 `archive_work` の可視化と削除手段**（45 GB 級の回収）
7. **§6-5 設定追加 + 設定画面**（C: から逃がせるようにする）
8. **§6-8 / §6-9 事前チェックと文言統一**
9. **§6-11 GUI 側の最後の砦**
10. **§6-7 中間ファイルの早期解放**（§8-2 7-2 の確認後）／**§6-10 起動時の掃除**

1〜4 は**原因が確定済み、または原因未確定でも単独で価値がある**ため、
§8-1 の回答を待たずに着手できる。5〜10 は確認事項の回答を反映してから進める。
