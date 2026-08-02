# resolve23 — 字幕編集画面への動画プレビュー（字幕適用済み）追加 修正設計書

## 0. 本書の位置づけ
`docs/request/ver2/request23.md` に対する **修正設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要ライブラリを追加しない／後方互換を失わない／不明点は推測実装せず設計書へ記載する）に従い、本書レビュー後に実装します。

---

## 1. 要望（request23.md）

| # | 要望 | 補足 |
|---|---|---|
| A1 | **クリップ用・アーカイブ切り抜き用の字幕編集画面に動画のプレビューを出したい** | クリップ用=`SubtitleEditorDialog`（字幕一覧画面）、アーカイブ用=`ArchiveResultWindow`（結果画面） |
| A2 | **字幕も適応した状態で表示したい** | 編集中の字幕（テキスト・使用可否・役割色・個別フォント/サイズ）が反映された映像を確認できること |

---

## 2. 現状分析（既存コードの実測）

> 行番号は調査時点。

### 2.1 クリップ用 字幕一覧画面（プレビューなし）

- `SubtitleEditorDialog`（`subtitle_editor_dialog.py:466`）は編集テーブル本体 `SubtitleEditorWidget`（`:210`）を包む薄いラッパで、**映像表示は一切ない**（既定 720x520 のテーブル画面）。
- レビューは `subtitle_generator._review_timeline`（`subtitle_generator.py:839`）から `SubtitleReviewBridge`（`main_window.py:105`）経由で呼ばれる。呼び出し時点のパイプライン状態:
  - **無音カット済みの中間動画**が `context.current_video_path()` で取得できる（作業ディレクトリ内。レビュー中はワーカーがブロックされるため存在が保証される）。
  - items の時間は**無音カット後動画基準**であり、この中間動画とタイムスタンプが一致する（元入力動画とは一致しない）。
- ダイアログへの付随情報の注入は `_attach_export_context`（`subtitle_generator.py:820`）→ `SubtitleReviewBridge.set_export_context`（`main_window.py:144`）という既存機構がある（Resolve 出力用。`source_path`=元入力のため**プレビューには使えない**）。

### 2.2 アーカイブ用 結果画面（静止画プレビューのみ）

- `ArchiveResultWindow`（`archive_result_window.py:55`）の右ペインは**先頭フレーム1枚の静止画**（`_extract_preview` `:221` が ffmpeg で PNG 抽出 → QLabel 表示）。動画再生も字幕表示もない。
  - flow17 R2 では「プレビュー（QtMultimedia、選択区間シーク。不可環境は静止画フォールバック）」が計画されたが、実装は静止画のみで着地している（`:5` コメント「静止画」）。
- 静止画の抽出元は**元 VOD の clip.start 位置**（`:227`）であり、字幕 items の基準（無音カット後クリップ）とは時間軸が一致しない。
- 一方 `prepared` 各要素は `prepared_path`（切り出し→正規化→無音カット済みのクリップ動画 / `clip_writer.py` `_prepare_clips`）を保持しており、**items と時間軸が一致する再生用素材が既に画面へ渡っている**。
- 一時領域の管理（`_preview_dir` 作成 `:82` / `_cleanup` `:296` / `done` で掃除 `:307`）は流用可能。

### 2.3 焼き込み処理（プレビューの忠実性の基準）

- 実際の焼き込みは `subtitle_generator.build_subtitle_file`（ASS 生成: 役割色・個別フォント/サイズ・縦横キャンバス対応）＋ `burn_subtitle`（libass の `ass` フィルタ）で行われる。クリップ用・アーカイブ用（`clip_writer._burn_one` `clip_writer.py:419`）とも同一関数。
- 確定時には `wrap_lines` による改行再適用が入る（`_review_timeline` `subtitle_generator.py:873` / `clip_writer._finalize_timeline` `:399`）。**プレビューでも同じ整形を通せば見た目が焼き込み結果と一致**する。
- 追加フォントは `resolve_fonts_dir`（settings_window）→ `fontsdir` で libass に解決させている。

### 2.4 QtMultimedia の同梱状況

- `src/main_window.spec:36` の hiddenimports は `PySide6.QtCharts` のみ。**QtMultimedia / QtMultimediaWidgets は未同梱**。動画再生を出すにはリリース時に spec への追加が必要（flow17 R2 で計画済みの範囲であり、新規サードパーティ依存ではない）。

### 2.5 再利用できる資産

| 資産 | 用途 |
|---|---|
| `build_subtitle_file` / `build_font_profile` / `build_effective_subtitle_cfg` | プレビュー用 ASS を**本番と同一ロジック**で生成（役割色・個別フォント・縦横対応） |
| `wrap_lines` | 確定時と同じ改行整形をプレビューへ適用 |
| `ffmpeg_runner.get_ffmpeg_exe` / `probe_duration` | プレビュー動画生成コマンドの実行ファイル解決・尺取得 |
| `resolve_fonts_dir` | 追加フォントの fontsdir 解決 |
| `clip_writer._ass_filter_opt` 相当のエスケープ | `ass` フィルタのパスエスケープ（2段階）。共通化して再利用 |
| `ArchiveResultWindow` の一時領域管理・静止画抽出 | フォールバック表示・一時ファイル掃除の雛形 |
| `no_window_creationflags` | GUI 実行時にコンソール窓を出さない subprocess 実行 |

---

## 3. 方式選定（「字幕を適応した状態」の実現手段）

| 方式 | 忠実性 | 編集反映 | コスト | 判定 |
|---|---|---|---|---|
| **案B: 選択行周辺の短い区間だけ ffmpeg で字幕焼き込み → QtMultimedia で再生** | ◎（焼き込みと同一の libass 描画。役割色・個別フォント・縁取り・縦動画まで完全一致） | ○（「プレビュー更新」で再生成。区間が短いため数秒） | 区間長ぶんの再エンコードのみ（低解像度化で更に短縮） | **採用** |
| 案A: QVideoWidget の上へ Qt 描画で字幕を重畳（リアルタイム同期） | △（libass と Qt で描画が異なる。縁取り・改行幅・縦動画スケールの再現に個別実装が必要） | ◎（即時） | 実装量大・忠実性の検証負荷大 | 不採用（「字幕を適応した状態」の要望に対し見た目が本番とずれるリスク） |
| 案C: 動画全体を焼き込んでから再生 | ◎ | ×（毎回全長エンコード。長尺クリップ用で分単位） | 過大 | 不採用 |

### 採用方針
- **案B**: 字幕一覧で選択中の行の開始時刻から `preview.segment_sec`（既定 20 秒）を、プレビュー解像度（幅 `preview.width` 既定 640px）で**本番と同じ ASS**を焼き込んだ一時 mp4 として生成し、QtMultimedia（`QMediaPlayer`+`QAudioOutput`+`QVideoWidget`）で再生する。
- ASS は**フルキャンバス解像度（PlayResX/Y = 出力プロファイル）で生成し、`ass` フィルタ適用後に `scale` で縮小**する。libass の描画スケールが本番と同一比率になり、見た目が一致する。
- QtMultimedia が使えない環境（クリーン PC のバックエンド不備等）は、**現行アーカイブ結果画面と同じ静止画フォールバック**（先頭フレーム PNG）へ自動で落とす。この場合も**字幕焼き込み済みフレーム**を抽出する（`-frames:v 1` に同じフィルタを適用）。

---

## 4. 設計方針（全体）

1. **共通ウィジェット `SubtitlePreviewWidget` を新設**（`src/gui/subtitle_preview_widget.py`）し、クリップ用ダイアログとアーカイブ結果画面の双方へ埋め込む（`SubtitleEditorWidget` の共用と同型の構成）。
2. **プレビュー対象動画は「items と時間軸が一致する動画」**とする:
   - クリップ用 = 無音カット後の中間動画（`context.current_video_path()`）
   - アーカイブ用 = 各クリップの `prepared_path`
   元入力/元 VOD は使わない（時間ずれのため）。
3. **プレビュー生成は編集内容から都度組み立てる**: `result_items()` → `wrap_lines` 整形（確定時と同一）→ `build_subtitle_file` → 区間切り出し＋焼き込み。**自動再生成はせず「プレビュー更新」ボタンで明示的に再生成**する（1キーごとのエンコード暴発を防ぐ）。行選択の変更時は、生成済み区間内なら**シークのみ**、区間外なら再生成する。
4. **生成はワーカースレッドで実行**し、UI をブロックしない（生成中は「プレビュー生成中…」表示。連打は直前ジョブのキャンセル/破棄で対応）。
5. **設定は setting.json 新設 `preview` 節**で管理（ON/OFF・区間長・解像度・エンコード品質・音声）。ハードコード禁止。`_merge_with_defaults` により旧設定は自動補完。
6. **失敗時はフォールバックして継続**: プレビューは補助機能であり、生成失敗・再生不可で編集/焼き込みフローを止めない（静止画 → それも不可ならメッセージ表示のみ）。
7. **後方互換**:
   - `preview.enabled=false`、またはプレビュー用動画パスが注入されない既存呼び出し経路では**プレビュー欄を出さず、現行と完全同一のレイアウト・動作**とする。
   - `SubtitleEditorDialog` の引数追加はキーワード引数（既定 None）のみ。`result_items()`/`theme_value()` 等の I/F は不変。
8. **テーマ演出（イントロカード・左上タグ）はプレビュー対象外**とする（焼き込み後段の演出のため）。Resolve 出力の注意書きと同様に、画面上へ「テーマ演出はプレビューに含まれません」と明示する。

---

## 5. 詳細設計

### 5.1 新規／変更ファイル一覧（予定）

| ファイル | 区分 | 内容 |
|---|---|---|
| `src/gui/subtitle_preview_widget.py` | **新規** | プレビューウィジェット本体＋生成ワーカー |
| `src/gui/subtitle_editor_dialog.py` | 変更 | `SubtitleEditorDialog` へプレビュー欄を追加（`preview_context` 注入時のみ） |
| `src/gui/archive_result_window.py` | 変更 | 右ペインの静止画 QLabel を `SubtitlePreviewWidget` へ差し替え |
| `src/gui/main_window.py` | 変更 | `SubtitleReviewBridge` に `set_preview_context` を追加しダイアログへ引き渡し |
| `src/modules/subtitle_generator.py` | 変更 | `_attach_export_context` と同型の `_attach_preview_context` を追加（レビュー直前に注入） |
| `src/settings/settings_window.py` | 変更 | `DEFAULT_SETTINGS` へ `preview` 節を追加（UI 項目は出さず setting.json 管理 = volume_analysis と同方針） |
| `src/main_window.spec` | 変更 | hiddenimports へ `PySide6.QtMultimedia` / `PySide6.QtMultimediaWidgets` を追加（リリース時） |

### 5.2 `SubtitlePreviewWidget`（新規）

```
+--------------------------------------+
| プレビュー (字幕適用 / テーマ演出は含まれません) |
| +----------------------------------+ |
| |        QVideoWidget              | |  ← 不可環境では QLabel(静止画)
| +----------------------------------+ |
| [▶/⏸] [シークスライダー] [🔊/🔇]      |
| [プレビュー更新]  状態ラベル           |
+--------------------------------------+
```

公開 I/F（呼び出し側から見た契約）:

| メソッド | 役割 |
|---|---|
| `set_source(video_path, eff_cfg, profile, settings)` | プレビュー対象動画と実効字幕設定を設定（アーカイブのクリップ切替でも呼ぶ）。生成済みキャッシュは破棄 |
| `set_items_provider(callable)` | 現在の編集内容（`result_items()` 相当）を返す関数を登録。「プレビュー更新」時に呼ぶ |
| `request_preview(anchor_sec)` | anchor_sec（選択行の start。無選択は 0）を起点に区間プレビューを生成・再生 |
| `seek_to(sec)` | 生成済み区間内であればシーク、区間外なら `request_preview` |
| `shutdown()` | 再生停止・ソース解放（`setSource(QUrl())`）・一時ファイル削除（画面 close 時に必ず呼ぶ） |

内部処理（生成ジョブ / QThread ワーカー）:

1. `items_provider()` で編集結果を取得 → `use=True` のみ残し `wrap_lines`（`min_line_length`/`max_line_length`/`wrap_engine` は eff_cfg 値）で整形（= `_finalize_timeline` と同一整形。共通化のため同関数を `clip_writer` から `subtitle_generator` 側へ移すことはせず、**同じ整形ヘルパをウィジェット内に持たずに `clip_writer._finalize_timeline` をそのまま import して再利用**する）。
2. 区間決定: `seg_start = anchor_sec`、`seg_len = min(preview.segment_sec, 動画尺 - seg_start)`。items は区間と重なるものだけ残し、時間を `-seg_start` シフト（区間頭で切れる字幕は 0 起点へクランプ）。
3. `build_font_profile(eff_cfg)` → `build_subtitle_file(shifted_items, font_profile, ass_path, video_width=profile.width, video_height=profile.height)`（**本番と同一の ASS 生成**）。
4. ffmpeg 実行（進捗は不要のため `subprocess.run`＋`no_window_creationflags`。数秒ジョブであり `run_ffmpeg_progress` の進捗表示対象外とする）:
   ```
   ffmpeg -y -hide_banner -loglevel error
     -ss {seg_start} -t {seg_len} -i {video_path}
     -vf "[縦動画時: scale/pad で profile キャンバスへ正規化,]ass='{escaped}':fontsdir='{fonts}',scale={preview.width}:-2"
     -c:v libx264 -preset {preview.preset} -crf {preview.crf}
     -c:a aac -ac 2 out.mp4
   ```
   - 縦動画の正規化 chain は `burn_subtitle` の target_size 処理と同一内容とする（横動画は不要）。
   - `ass` フィルタのパスエスケープは既存実装（2段階エスケープ＋fontsdir）を共通関数化して使用。
5. 完了シグナルでメインスレッドへ復帰 → `QMediaPlayer.setSource` → 再生。
6. QtMultimedia import 失敗・再生エラー時: 同フィルタで `-frames:v 1` の PNG を抽出し QLabel 表示（静止画フォールバック）。それも失敗なら「プレビューを表示できません」。

一時ファイル: `tempfile.mkdtemp(prefix="subtitle_preview_")` 配下に生成し、`shutdown()` で削除。**削除前に必ず `stop()`＋`setSource(QUrl())` でファイルロックを解放**する（Windows のロック対策。失敗は握りつぶしてログのみ）。

### 5.3 クリップ用（`SubtitleEditorDialog`）への組み込み

- **注入経路**（export_context と同型・同寿命）:
  - `subtitle_generator._review_timeline` のレビュー呼び出し直前に `_attach_preview_context(review_cb, context)` を追加:
    ```python
    {"video_path": context.current_video_path(),   # 無音カット後の中間動画
     "eff_cfg": subtitle_cfg,                      # build_effective_subtitle_cfg 適用済み
     "profile": context.output_profile,
     "settings": context.settings}
    ```
  - `SubtitleReviewBridge.set_preview_context(payload)` で保持 → ダイアログ生成時に `preview_context=` として渡し、ダイアログ終了時に破棄（export_context と同じ寿命管理）。
- **レイアウト**: `preview_context` があり `preview.enabled` が真のときのみ、ルートを `QSplitter(Qt.Horizontal)` にして「左=説明＋テーブル（現行一式）/ 右=プレビュー」とする。既定サイズを 1120x560 へ拡大。**プレビュー無しのときは現行レイアウト・現行サイズのまま**（完全後方互換）。
- **行選択との連動**: テーブルの `currentCellChanged` で選択行の `start` を `seek_to` へ渡す（生成済み区間内はシークのみ）。
- 中間動画はレビュー中ワーカーがブロックしているため存在が保証される。ダイアログ終了時に `shutdown()` を呼び、以降パイプラインが再開して中間ファイルが削除されても参照しない。

### 5.4 アーカイブ用（`ArchiveResultWindow`）への組み込み

- 右ペインの `preview_label`（静止画）一式を `SubtitlePreviewWidget` へ差し替える。
- クリップ行変更（`_on_row_changed`）で `set_source(prepared["prepared_path"], prepared["eff_cfg"], prepared["profile"], self._settings)` を呼ぶ。**プレビュー対象を元 VOD から prepared_path へ変更**する（現状の「元 VOD の clip.start 静止画」は字幕と時間軸が合わないため）。
  - これに伴い `ArchiveResultBridge`/`write_clips` の変更は不要（`prepared` に `prepared_path`/`eff_cfg`/`profile` は既に含まれる）。
  - 静止画キャッシュ（`_preview_cache`/`_extract_preview`）はウィジェット内フォールバックへ役割を移し、窓側の実装は削除する。
- `done()` で `shutdown()` を呼ぶ（既存の `_cleanup` と同じタイミング。prepared_path は焼き込みで引き続き使うため**削除しない**。削除対象はプレビュー一時 mp4 のみ）。
- 字幕編集は共通 `SubtitleEditorWidget` のため、`set_items_provider` には `editor.result_items` を渡す。テーマ欄の値はプレビューに影響しない（§4-8）。

### 5.5 プレビューと本番焼き込みの差異（明記事項）

| 項目 | プレビュー | 本番 | 差異の扱い |
|---|---|---|---|
| 字幕描画 | libass（同一 ASS） | libass | **一致** |
| 解像度 | `preview.width`（縮小） | 出力設定 | ASS はフルキャンバスで生成後に縮小するため**比率一致** |
| 改行整形 | `_finalize_timeline`（wrap_lines） | 同左 | **一致** |
| テーマ演出（イントロ/タグ） | 含まない | 含む | 画面へ注記（§4-8） |
| OP/ED・結合 | 含まない | 含む | プレビューは単クリップ区間のみ |
| エンコード品質 | ultrafast/CRF 高 | 出力設定 | 画質のみの差。確認用途として許容 |

---

## 6. setting.json 追加定義

```jsonc
"preview": {
    "enabled": true,        // 字幕編集画面のプレビュー欄 ON/OFF (false で完全に従来 UI)
    "segment_sec": 20,      // 1回のプレビュー生成区間長 (秒)
    "width": 640,           // プレビュー解像度 (幅 px。高さはアスペクト維持)
    "preset": "ultrafast",  // プレビュー用 x264 preset
    "crf": 28,              // プレビュー用品質 (大=軽い)
    "audio_enabled": true   // プレビュー音声の既定 (ミュートボタンで切替可)
}
```

- `DEFAULT_SETTINGS`（settings_window.py）へ追加し、`_merge_with_defaults` で旧 setting.json を自動補完（resolve22 §7 と同じ後方互換機構）。
- 設定画面へ UI 項目は追加しない（volume_analysis と同じく setting.json 直接管理。要望があれば別リクエストで対応）。

---

## 7. エラー処理・フォールバック方針

| 事象 | 挙動 |
|---|---|
| QtMultimedia import 失敗（未同梱ビルド・環境不備） | 起動時に検出し静止画モードへ（ログ INFO）。編集機能は無影響 |
| `QMediaPlayer` の再生エラー | 静止画フォールバック（字幕焼き込み済み1フレーム） |
| プレビュー生成 ffmpeg 失敗 | 状態ラベルへ「プレビュー生成に失敗しました」＋WARNING ログ。再試行は「プレビュー更新」で可能。編集・焼き込みは継続 |
| プレビュー対象動画が消失（想定外） | 同上（生成失敗と同じ経路） |
| 一時ファイル削除失敗（Windows ロック） | 握りつぶして WARNING（`ignore_cleanup_errors` と同思想） |
| 生成中にダイアログを閉じる | ワーカーへキャンセル要求 → 完了通知は破棄（ダイアログ参照を持たない） |

いずれの場合も**「字幕決定」「完了」以降の既存フロー（焼き込み・結合・Resolve 出力）へは一切影響させない**。

---

## 8. 後方互換性

1. `preview_context` 未注入の `SubtitleEditorDialog` 呼び出し（既存テスト・他経路）は現行と完全同一（プレビュー欄なし・サイズ不変・I/F 不変）。
2. `preview.enabled=false` で両画面とも従来 UI（アーカイブ結果画面は現行どおり静止画すら出さない選択肢はとらず、**enabled=false 時は現行同等の先頭フレーム静止画を維持**する)。
3. `result_items()` / `result_data()` / `consume_theme()` / Resolve 出力の I/F・戻り値は不変。
4. 焼き込みパイプライン（`run_pipeline` / `write_clips`）への変更は `_attach_preview_context` の追加のみで、処理順序・中間ファイル・出力は不変。
5. spec への QtMultimedia 追加は同梱のみでユーザー操作不変（配布サイズ増は §9 で確認）。

---

## 9. 実装・リリース手順（flow17 準拠）

1. 実装順: `SubtitlePreviewWidget` 単体 → アーカイブ結果画面（prepared_path が既に画面にあり結線が小さい）→ クリップ用（注入経路の追加を含む）。
2. 受入基準:
   - [ ] クリップ用: 字幕一覧で行を選ぶと該当時刻からプレビューが再生され、テキスト・使用可否・役割色・個別フォント/サイズの編集が「プレビュー更新」で反映される。
   - [ ] アーカイブ用: クリップ切替でプレビューが切り替わり、同様に編集が反映される。字幕時刻と映像が一致する（prepared_path 基準）。
   - [ ] 縦動画（vertical プロファイル）でも字幕位置・サイズの比率が焼き込み結果と一致する。
   - [ ] `preview.enabled=false` および preview_context 未注入で現行と完全同一（回帰ゼロ）。
   - [ ] クリーン PC で再生成功 or 静止画フォールバックが機能する（flow17 R2 §8-11 と同じ検証）。
   - [ ] プレビュー一時ファイルが画面終了時に残らない（ロック時は次回 temp 掃除に委ねる）。
   - [ ] 現行動画でクリップ用の焼き込み結果が従来と一致（回帰ゼロ / HowToRelease §6）。
3. リリース: 次期マイナーリリースとして配布。`main_window.spec` へ QtMultimedia/QtMultimediaWidgets を追加し、配布サイズと GitHub Releases 上限（2GiB/ファイル）を確認。`version.py` と `AutoEdit.iss` を同時更新。

---

## 10. 不明点・レビューで確定したい事項（推測実装しない）

| # | 事項 | 提案（既定案） |
|---|---|---|
| Q1 | プレビュー区間長の既定値 | 20 秒（`preview.segment_sec`。長すぎると生成が遅く、短すぎると文脈が見えないバランス） |
| Q2 | 行選択時に自動再生するか | 生成済み区間内はシークして**自動再生継続**、区間外は自動生成せず「プレビュー更新」押下を待つ（無駄なエンコード防止） |
| Q3 | 音声の既定 | ON（`preview.audio_enabled`。ミュートボタンで切替） |
| Q4 | アーカイブ結果画面の静止画（現行機能）の扱い | プレビュー無効時・フォールバック時のみ表示に整理（§8-2） |
| Q5 | 編集変更の自動反映（キーストローク毎の再生成） | 行わない。「プレビュー更新」ボタン方式（§4-3）。将来要望があればデバウンス付き自動更新を別リクエストで検討 |
