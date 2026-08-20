# Nouse — 未使用コードの棚卸し（削除はしない調査報告）

## 0. 本書の位置づけ

* 目的: **現時点で使われていないファイル・関数・クラス・定数・設定キー**を洗い出し、
  それぞれを**削除した場合に何が起きるか**を明らかにする。
* 本書は**調査のみ**。削除・変更は一切行っていない（リポジトリのコードは無変更）。
* 調査日: **2026-08-19**（ver3 resolve7 Phase 1〜5 / resolve8 Phase 1 の実装直後の状態）
* 調査対象: `src/`（`src/dist` を除く 60 ファイル）と `tests/`（20 ファイル）
* 調査方法:
  1. Python の `ast` で「モジュール直下の定義」「クラスのメソッド」「`Signal` 宣言」
     「`self.X` への代入」を機械的に収集
  2. 収集した識別子を **`src/` + `tests/` の全文**（コメント・文字列リテラル込み）で照合。
     `getattr(obj, "name")` のような**文字列経由の参照も取りこぼさない**ようにした
  3. 機械判定の結果を **1 件ずつ手作業で確認**し、Qt / 標準ライブラリが名前で呼ぶ
     オーバーライド、設定次第で通る経路、GC 防止の参照保持を分離した（§7）
* 数え方: 「出現 1 回」= その定義自身だけで、他のどこからも呼ばれていないことを指す。

---

## 1. サマリ

| 分類 | 件数 | 概算行数 | 削除の安全度 |
|---|---:|---:|---|
| §2 完全に未使用（どこからも参照が無い） | 15 | 約 175 行 | **高**（挙動は変わらない） |
| §3 実装が無効化されているのに設定・UI が生きている（発話ゲート） | 1 系統 | 約 90 行 + 設定 5 キー + GUI 4 部品 | 中（**利用者から見える挙動が変わる**） |
| §4 設定キーだけ生きていて実装が無い／その逆 | 8 キー | — | 中（互換の考慮が要る） |
| §5 本体からは呼ばれずテストだけが参照 | 4 | 約 12 行 | 中（テスト側の修正が要る） |
| §6 到達可能だが既定では通らない経路 | 3 系統 | 約 400 行 | **低（消してはいけない）** |
| §7 見かけ上未使用だが必要 | 9 | — | **不可（消すと壊れる）** |
| 使われていない `import` | **0** | — | — |

* **合計で安全に削れるのは約 175 行（§2）**。プロジェクト全体（`src/` 約 20,000 行）の 1% 程度で、
  容量・実行速度への効果はほぼ無い。効果は「読む量が減る」「誤って呼ばれない」という保守性の面に限られる。
* いっぽう §3 は**行数より意味が重い**。「設定画面にあるのに効かないスイッチ」が 4 つあり、
  利用者から見て嘘になっている。ここは削除よりも**「消すか復活させるかを決める」**べき項目である。

---

## 2. 完全に未使用（どこからも参照されていない）

以下はすべて「定義はあるが、`src/` からも `tests/` からも 1 度も参照されていない」もの。
**削除しても実行時の挙動は変わらない**（＝出力される動画・保存ファイルは 1 バイトも変わらない）。

### 2.1 ファイル 1 件

| ファイル | 行数 | 内容 |
|---|---:|---|
| `src/gui/archive_result_dialog.py` | **116 行（全体）** | アーカイブ採点結果の**簡易**確認ダイアログ（flow17 R1） |

* 冒頭コメントに「R2 で採点グラフ・字幕修正・プレビューを備えた本画面
  （`ArchiveResultWindow`）に発展させる」と明記されており、**後継の
  `src/gui/archive_result_window.py`（335 行）に完全に置き換わっている**。
* `import` している箇所はゼロ（`archive_tab.py` は `archive_result_window` の
  `ArchiveResultBridge` を使う）。
* **削除した場合の影響**: 無し。実行経路から到達できないため、GUI・出力・テストのいずれにも変化なし。
  ただし `subtitle_generator._format_ass_time` を private のまま外部参照している唯一の箇所でもあるため、
  消すと「private 関数がパッケージ外から使われている」という設計上の傷も 1 つ消える。
* **推奨**: 削除して差し支えない。R1 → R2 の履歴は git に残る。

### 2.2 関数・クラス 5 件

| 場所 | 行 | 名前 | 現状 | 削除の影響 |
|---|---:|---|---|---|
| `src/exceptions.py` | 11-12 | `ConfigError` | 例外クラス。**raise も except も 1 箇所も無い** | 無し。ただし将来「設定不整合」を表す受け皿が消える |
| `src/modules/subtitle_generator.py` | 526-534 | `_inline_font_override` | テロップ個別フォント/サイズの ASS インラインタグ生成（resolve16 §4.4）。**呼び出し元が無い** | 無し。ただし**「行ごとのフォント上書き」機能の生成側が丸ごと消える**。字幕編集画面の「フォント」「サイズ」欄は現在 `SubtitleClip.font/font_size` 経由で ASS Style に反映されており、この関数は旧経路の残骸 |
| `src/timeline/builder.py` | 519-523 | `build_without_subtitles` | `build(..., [], ...)` を呼ぶだけの薄いラッパ | 無し |
| `src/timeline/media_probe.py` | 187-192 | `duration_of` | `ffmpeg_runner.probe_duration` の薄いラッパ | 無し |
| `src/timeline/project_io.py` | 429-437 | `ensure_base_audio_track` | 音声トラックが無い Timeline へ A1 を足す「読み込み時の保険」。`__all__` にも載っているが**誰も呼んでいない** | 無し。ただし**保険が無い状態が明示される**。現状 A1 が欠けた JSON を読むと音声が丸ごと落ちる（`_validate_audio_links` はリンク切れの除外しかしない）。削除より**呼び出しを足す**ほうが筋が良い |

> `_inline_font_override` と `ensure_base_audio_track` の 2 つは、「使われていない」ではなく
> **「使うつもりで書かれたが繋ぎ忘れている」**可能性がある。削除の前に §9 の判断が要る。

### 2.3 メソッド 5 件

| 場所 | 行 | 名前 | 削除の影響 |
|---|---:|---|---|
| `src/modules/subtitle_generator.py` | 142-143 | `FontProfile.to_ass_color` | 無し。コメントに「後方互換」とあるが、その後方互換の相手（旧 Style 生成）は既に `to_ass_style_named` へ移行済み |
| `src/timeline/timemap.py` | 71-73 | `TimeMap.from_timeline` | 無し。呼び出し側は全て `TimeMap.from_clips(timeline.base_clips(), body_media_id=…)` を直接使っている（`timeline_controller.py:96` 他） |
| `src/gui/timeline/preview_items.py` | 357-358 | `BaseFrameItem.bounding_canvas` | 無し。キャンバス矩形は各所で直接組んでいる |
| `src/gui/timeline/timeline_view.py` | 279-281 | `TrackHeaderWidget.rows_height` | 無し。高さは `build_rows()` を呼ぶ側がその場で計算している |
| `src/gui/subtitle_editor_dialog.py` | 255-261 | `SubtitleEditorWidget.description_text` | 無し。「埋め込み側が説明ラベルに使えるよう公開する」とあるが、埋め込み側（`ArchiveResultWindow`）は独自の説明文を出している。**削除すると、埋め込み画面の説明文が本家とずれたままであることが固定される** |

### 2.4 Qt シグナル 2 件

| 場所 | 名前 | 現状 | 削除の影響 |
|---|---|---|---|
| `src/gui/timeline/timeline_controller.py:19` | `clip_changed = Signal(str)` | **emit も connect も 1 度も無い**。クリップ単位の更新通知として用意されたが、実際は全て `timeline_changed`（全体再描画）で済ませている | 無し。ただし将来「1 クリップだけ再描画して軽くする」最適化の入口が消える |
| `src/gui/timeline/timeline_view.py:291` | `clip_activated = Signal(str)` | `mouseDoubleClickEvent`（:732-735）から **emit されているが、受け手が誰もいない** | 無し。**現状 Timeline 上のクリップをダブルクリックしても何も起きない**（コメントの「字幕編集などを開く」は未実装）。シグナルを消すならダブルクリック handler ごと消えることになる |

### 2.5 属性 4 件

| 場所 | 名前 | 現状 | 削除の影響 |
|---|---|---|---|
| `src/exceptions.py:26` | `FFmpegError.command` | 代入のみ。読む箇所が無い | 無し。ただし**失敗時に実行コマンドを表示する道が閉じる**。`stderr_tail` は `main_window._format_failure_message` が使っており（:352）、`command` だけが未使用 |
| `src/exceptions.py:28` | `FFmpegError.returncode` | 同上 | 同上 |
| `src/timeline/commands.py:603 / :704` | `AddMediaClip.created_track_id` / `AddSubtitleClip.created_track_id` | 本体は読まない。**`tests/test_timeline_commands.py:503,511` だけが参照** | 本体への影響は無いが**テスト 2 件が落ちる**（§5） |
| `src/gui/timeline/preview_items.py:284` | `SubtitleOverlayItem._canvas_size` | 代入のみ。字幕の配置計算は `_place()` の引数で受け取っている | 無し |
| `src/archive/twitch_auth.py:112` | `TwitchAuth._client_secret` | 代入のみ。**コメントに「インプリシットフローでは不要（後方互換で受け取るが未使用）」と明記** | 無し。意図的な未使用のため、消すなら引数 `client_secret` ごと消すことになり、呼び出し側の互換が切れる |

### 2.6 定数 1 件（※厳密には「使われていない」ではなく「使い忘れ」）

| 場所 | 名前 | 現状 |
|---|---|---|
| `src/timeline/model.py:22` | `ORIGIN_USER_MEDIA = "user_media"` | 本体は**定数を使わず文字列リテラルを直書き**している（`commands.py:591` の既定引数、`timeline_editor_dialog.py:904` の表示名テーブル）。参照しているのは `tests/test_archive_timeline.py` のみ |

* **削除の影響**: テスト 1 ファイルが `ImportError` になる。
* **推奨**: 削除ではなく**逆**。`commands.py` / `timeline_editor_dialog.py` の直書きを定数へ寄せる。
  他の `ORIGIN_*` 定数（`ORIGIN_SILENCE_CUT` 等）は本体でも使われており、これだけが例外になっている。

---

## 3. 実装が無効化されているのに設定・UI が生きている系統（最重要）

**発話ゲート（speech gate）**——「発話区間だけテロップを出す」機能。
resolve7 の要望で**呼び出しだけをコメントアウト**し、処理本体は温存されている。

```
src/modules/subtitle_generator.py:1072-1076   ← 唯一の呼び出しが 5 行まるごとコメント
    # if subtitle_cfg.get("speech_gate_enabled", True):
    #     timeline = _gate_with_settings(...)
```

そのため以下が「実行されないまま残っている」。

| 実体 | 行 | 行数 |
|---|---:|---:|
| `detect_speech_regions()` | 785-791 | 7 |
| `_apply_padding()` | 795-813 | 19 |
| `gate_timeline_by_speech()` | 819-839 | 21 |
| `_gate_with_settings()` | 844-886 | 43 |
| **計** | | **90 行** |

さらに、**設定と GUI は生きている**（＝利用者には効くように見える）。

| 設定キー | 既定 | 設定画面の部品 | 実際 |
|---|---|---|---|
| `subtitle.speech_gate_enabled` | `true` | チェックボックス「発話区間のみテロップを表示する」（`settings_window.py:1048`） | **効かない** |
| `subtitle.speech_threshold_db` | `-16` | 入力欄 | **効かない** |
| `subtitle.speech_min_duration_sec` | `0.2` | 入力欄 | **効かない** |
| `subtitle.speech_gate_mode` | `"trim"` | コンボボックス（`:1065`） | **効かない** |
| `subtitle.speech_pad_sec` | `0.1` | 入力欄 | **効かない** |

* **これは単なる未使用コードではなく、利用者から見える不具合**である。
  チェックを外しても入れても結果が変わらないスイッチが設定画面に 4 つ並んでいる。
* resolve8 §11 でも「実装が無効化されたまま設定だけ残っており、利用者からは効いて見える」として
  整理対象に挙げられている（本書執筆時点で未着手）。
* **削除した場合の影響**:
  * コードだけ消す → 挙動は変わらない（今も動いていないため）。ただし**復活の手数が上がる**。
  * 設定キーと GUI も消す → **設定画面の項目が 4 つ減る**。既存 `setting.json` に残ったキーは
    `_merge_with_defaults` が既定に無いキーを温存するため**ゴミとして残り続ける**（害は無い）。
  * 実装を復活させる（コメントを外す） → **テロップの表示件数が変わる**。resolve7 で
    「無音カットの単一閾値へ一本化する」と決めた方針の逆戻りになるため、方針判断が要る。
* **推奨**: 3 択のいずれかを**決める**こと。本書としては
  「コードは温存しつつ、GUI の 4 部品を隠して『効かない設定』を見せない」が影響最小と考える。

---

## 4. 設定キーだけ生きていて実装が無い／その逆

`DEFAULT_SETTINGS` の全 313 キーを実装側の参照と突き合わせた結果。

### 4.1 宣言されているが実装が一度も読まないキー

| キー | 既定 | 状況 | 削除の影響 |
|---|---|---|---|
| `timeline.keep_project_file` | `true` | `builder.timeline_config()` が `cfg["keep_project_file"]` へ**詰めているだけ**で、消費者がいない。コメントは「false なら『決定』後に削除する」だが**その実装が無い** | 無し。ただし「決定後にプロジェクト JSON を消す」機能が存在しないことが確定する。ver3 resolve7 で再編集の入口ができた今、**この機能はもう要らない**とも言える |
| `subtitle.review_on_empty_skip` | `true` | **どこからも読まれない**（設定画面にも欄が無い）。字幕が空のときレビューを飛ばす想定だったと思われる | 無し |
| `archive.scoring.method` | `"A"` | 方式は `method_a_config()` に固定。**分岐が存在しない** | 無し（方式 B が来たときに使う想定の予約席） |
| `archive.scoring.method_a.emotion_points.big_laugh` / `surprise` / `cry` / `anger` | 15 / 8 / 15 / 12 | `scoring.py:61-63` が読むのは `loud` と `long_silence` のみ。**コメントに「R1 は音声のみのため loud/long_silence を使用（他は ML 必要=後続）」と明記** | 無し。意図的な予約席 |
| `timeline.ripple_delete_migrated` | `false` | 1 度きりの移行フラグ。`settings_window.py:762-764` でのみ読み書き | 無し（消すと移行が再実行され、利用者が `false` へ戻した設定を勝手に `true` へ書き換える。**消してはいけない**） |
| `subtitle.custom_fonts_dir` | `"fonts"` | `resolve_fonts_dir()` が読む（`settings_window.py:806`）。**未使用ではない** | — |

### 4.2 逆に「実装は読むが `DEFAULT_SETTINGS` に無い」キー

| キー | 読む場所 | 状況 |
|---|---|---|
| `silence_cut.batch_size` | `silence_cutter.py:431` | 既存 `setting.json` にだけ存在（値 50）。`DEFAULT_SETTINGS` に無いため**新規インストールでは既定 0** となり、`cut_and_concat_batched` の**バッチ分割は永久に発動しない**（`:278` で一括処理へ落ちる） |
| `silence_cut.concat_reencode` | `silence_cutter.py` | 同上。新規インストールでは常に `false` |

* **影響**: 従来フロー（`silence_cut.mode="physical"`）でのみ効く値。ver3 の既定は
  `edit_points` なので現状ほぼ通らないが、**「ファイルにあるのに既定に無い」ため
  設定の自動補完から漏れ、環境によって挙動が変わる**状態になっている。
* **推奨**: 削除ではなく `DEFAULT_SETTINGS` へ追記して揃えるか、従来フローごと畳むかの判断。

---

## 5. 本体からは呼ばれず、テストだけが参照している定義

| 場所 | 名前 | 参照しているテスト | 削除の影響 |
|---|---|---|---|
| `src/export/fcpxml_builder.py:69-71` | `format_time` | `tests/test_fcpxml_builder.py:20-27` | 本体は全て `frames_to_text()` を直接呼ぶ。削除すると**テスト 1 件が落ちる**ため、テストも一緒に消す判断が要る |
| `src/export/fcpxml_builder.py:75-77` | `format_duration` | `tests/test_fcpxml_builder.py:30-32` | 同上（テスト 1 件） |
| `src/timeline/commands.py:603 / :704` | `created_track_id` | `tests/test_timeline_commands.py:503,511` | テスト 2 件が落ちる。**トラック自動生成の検証**に使われており、テストの価値は高い。消すならテストの書き換えが必要 |
| `src/timeline/model.py:22` | `ORIGIN_USER_MEDIA` | `tests/test_archive_timeline.py` | §2.6 のとおり、**本体側で使うのが正しい** |

* 「テストしか使っていない＝不要」とは限らない。`created_track_id` は仕様（空きが無ければ
  新しい映像トラックを作る）の**唯一の検証手段**であり、削除は実質テストの削除になる。

---

## 6. 到達可能だが既定では通らない経路（**消してはいけない**）

機械判定では「呼び出しが少ない」ように見えるが、**設定次第で確実に通る**。

| 系統 | 実体 | 通る条件 | 消した場合 |
|---|---|---|---|
| 従来フロー（実カット） | `silence_cutter.cut_and_concat` / `cut_and_concat_seek` / `cut_and_concat_batched`（計 約 250 行）、`concat_processor` の OP/ED 後段結合 | `silence_cut.mode="physical"` または `timeline.enabled=false` | **切り戻し手段が消える**。ver3 の Timeline に不具合が出たとき設定 1 つで戻す道が無くなる（resolve.md R2 の約束を破る） |
| 旧字幕編集画面 | `src/gui/subtitle_editor_dialog.py`（`SubtitleEditorDialog`） | 同上。加えて**アーカイブ経路の結果画面が部品を共用**している | 従来フローが動かなくなる。`SubtitleEditorWidget` / `SubtitlePreviewWidget` は現行画面も使用中 |
| PyAV 非搭載時のフレーム取得 | `frame_source.FfmpegFrameSource` | `PyAV` が無い環境、または `timeline.preview.backend="ffmpeg"` | PyAV が無い環境でプレビューが出なくなる |

---

## 7. 見かけ上未使用だが必要なもの（**消すと壊れる**）

機械判定が「参照 1 回」と出すが、**フレームワークが名前で呼ぶ**か、**参照保持が目的**のもの。

| 場所 | 名前 | 呼ぶ主体 |
|---|---|---|
| `src/archive/twitch_auth.py:60,99` | `_TokenHandler.do_GET` / `log_message` | `http.server.BaseHTTPRequestHandler`（メソッド名で呼ばれる） |
| `src/gui/subtitle_editor_dialog.py:150-173` | `_MultilineTextDelegate.createEditor` / `setEditorData` / `setModelData` / `updateEditorGeometry` | Qt（`QStyledItemDelegate` の仮想関数） |
| 各所 | `paintEvent` / `mousePressEvent` / `dropEvent` / `itemChange` ほか | Qt のイベントディスパッチ |
| `src/gui/timeline/timeline_editor_dialog.py:283` | `self._shortcut_guard` | **GC 防止の参照保持**。捨てると `QShortcut` の有効/無効切り替えが効かなくなる |
| `src/gui/score_graph_widget.py:35` | `self._highlight_bounds` | 同じく**参照保持用**（コメントに明記） |
| `src/pipeline/pipeline_context.py:26,30,35` | `subtitle_review_callback` / `volume_analysis_callback` / `timeline_review_callback` | `getattr(context, "…", None)` の**文字列経由**で読まれる |
| `src/exceptions.py:27` | `FFmpegError.stderr_tail` | 同じく `getattr` 経由（`main_window.py:352`） |

---

## 8. コード以外（参考）

| 対象 | 容量 | 状況 |
|---|---:|---|
| `src/dist/` | **約 1.4 GB** | PyInstaller のビルド生成物（torch 一式を含む）。`.gitignore` 済みで**リポジトリには入っていない**が、作業ツリーの容量の大半を占める。再ビルドで作り直せるため、ディスクを空けたいときは削除して差し支えない |
| `src/build/` / `src/ffmpeg/` / `output/` | — | 同じく `.gitignore` 済みの生成物・外部配置物 |
| `logs/` と `src/gui/logs/` | — | **同じ名前のログが 2 か所に出ている**（`logs/autoedit_YYYYMMDD.log` と `src/gui/logs/…`）。作業ディレクトリ依存でログ出力先が割れているだけで未使用ではないが、片方は実行のたびに増える |

---

## 9. 削除する場合の推奨順序

| 段階 | 対象 | リスク | 確認方法 |
|---|---|---|---|
| **1** | §2.3 メソッド 5 件、§2.4 シグナル 2 件、§2.5 属性（`command` / `returncode` / `_canvas_size`）、§2.2 の `build_without_subtitles` / `duration_of` / `ConfigError` | **ほぼ無し** | `python -m unittest discover -s tests`（現在 393 件）が緑のままであること |
| **2** | §2.1 `archive_result_dialog.py`（116 行） | 無し | 同上 + アーカイブ切り抜きを 1 本通す |
| **3** | §5 のテスト専用定義（`format_time` / `format_duration`） | テストも一緒に消す判断が要る | `tests/test_fcpxml_builder.py` の該当 2 件を削除 |
| **4** | §3 発話ゲート（コード 90 行 + 設定 5 キー + GUI 4 部品） | **利用者から見える変化あり** | 設定画面を開き、項目が消えていること／既存 `setting.json` を読んでもエラーにならないこと |
| **保留** | §2.2 `_inline_font_override` / `ensure_base_audio_track`、§4.1 `keep_project_file` | **「未使用」ではなく「繋ぎ忘れ」の可能性**。消す前に仕様判断が要る | §10 の未決事項 |
| **不可** | §6・§7 | 消すと壊れる | — |

* 段階 1〜3 を全て実施しても削減は**約 175 行（`src/` の 1% 未満）**で、
  ビルドサイズ・起動時間・実行速度への効果は測定できる水準に達しない。
  **効果は「読む量」と「誤用の芽」を減らすことに限られる**と理解した上で判断されたい。

---

## 10. 判断が要る未決事項

| # | 内容 | 選択肢 |
|---|---|---|
| **N1** | 発話ゲート（§3）をどうするか | (a) GUI だけ隠す（推奨・影響最小） / (b) コード・設定・GUI をすべて削除 / (c) 実装を復活させる（テロップ件数が変わる） |
| **N2** | `_inline_font_override`（§2.2）は削除か、繋ぎ直しか | 現行の行ごとフォント指定が `SubtitleClip.font/font_size` で足りているなら削除。ASS のインライン上書きが要る場面が残っているなら繋ぎ直し |
| **N3** | `ensure_base_audio_track`（§2.2）は削除か、`load_project` から呼ぶか | 音声トラック欠落 JSON への保険を残すなら**呼び出しを足す**ほうが良い |
| **N4** | `timeline.keep_project_file`（§4.1）| 再編集機能ができた今、「決定後に消す」機能はもう不要では。キーごと削除するか、実装するか |
| **N5** | `clip_activated`（§2.4）| ダブルクリックに機能（字幕編集を開く等）を与えるか、handler ごと削除するか |
| **N6** | 従来フロー（§6）を今後も維持するか | 維持するなら §4.2 の `batch_size` / `concat_reencode` を `DEFAULT_SETTINGS` へ揃える。畳むなら約 400 行がまとめて削減対象になる |

---

## 11. 再現手順

本書の一覧は以下の手順で再現できる（調査用スクリプトはリポジトリに含めていない）。

1. `ast` で `src/`（`dist` 除く）の全 `.py` からモジュール直下の定義・クラスのメソッド・
   `Signal` 宣言・`self.X` への代入を収集する
2. 収集した識別子を `src/` + `tests/` の全文から正規表現 `\b名前\b` で数える
3. 出現が 1 回（＝定義のみ）のものを候補として抽出する
4. 候補から次を手作業で除外する
   * Qt / `BaseHTTPRequestHandler` のオーバーライド（§7）
   * `getattr` など文字列経由で参照されるもの（§7）
   * 設定次第で通る経路（§6）
5. `DEFAULT_SETTINGS` の全キーを再帰的に展開し、`settings_window.py` 以外での
   文字列リテラル出現を数えて未参照キーを抽出する（§4）
