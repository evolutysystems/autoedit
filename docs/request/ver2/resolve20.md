# resolve20 — DaVinci Resolve プロジェクトファイル出力機能 要望設計書

## 0. 本書の位置づけ
`docs/request/ver2/request20.md` に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要ライブラリを追加しない／後方互換を失わない／不明点は推測実装せず設計書へ記載する）に従い、本書レビュー後に実装します。**本書は「どう作るか」の設計であり、実装は含みません。**

---

## 1. 要望（request20.md）

「DaVinci Resolve ファイル出力」ボタンを **2 箇所**に実装する。

| # | 要望 | 補足 |
|---|---|---|
| R1 | **クリップ用**では字幕一覧画面（`SubtitleEditorDialog`）に「Davinci Resolve ファイル出力」ボタンを実装 | 現行クリップフロー |
| R2 | **アーカイブ切り抜き用**ではグラフ結果一覧画面（`ArchiveResultWindow`）に同ボタンを実装 | アーカイブフロー |
| R3 | DaVinci Resolve 用の **プロジェクトファイル**を作成する | ファイル形式は§3で選定 |
| R4 | ファイルを開くと **DaVinci Resolve でファイルを開ける** | 取り込み(import)で開く前提。§3 で詳述 |
| R5 | **カット編集点・字幕**を DaVinci Resolve で**再編集**できるようにする | 編集点＝元動画に対する区間 |
| R6 | 字幕は Resolve の**テロップ（Text+）**として、**フォント・フォントサイズ・記載場所を編集**できるように適応する | ASS スタイル → Text+ へ変換 |
| R7 | **（追記）無音カットの可否を `main_window` のアーカイブタブのチェックマークで選択**できるようにする | 既定は setting.json 準拠。実行中は操作不可（§5.8） |

---

## 2. 現状分析（既存コードの実測／再利用可否）

> 調査は `src/dist/`（PyInstaller 生成物）を除外して実施。行番号は調査時点。

### 2.1 UI の接続点（ボタン設置箇所）

| 画面 | クラス | ボタン列 | 保有データ |
|---|---|---|---|
| クリップ用 字幕一覧 | `src/gui/subtitle_editor_dialog.py` `SubtitleEditorDialog`（`:412`）/ `SubtitleEditorWidget`（`:158`） | `button_row`（`:441-450`、「字幕決定」「キャンセル」） | **字幕 items のみ**（後述）。**ソース動画パス・編集点は持たない** |
| アーカイブ用 結果画面 | `src/gui/archive_result_window.py` `ArchiveResultWindow`（`:52`） | `button_row`（`:152-161`、「完了（切り抜き＋字幕焼き込み）」「キャンセル」） | **元 VOD パス**（`self._source_path`）＋ **TOP-N クリップ区間 `[start,end]`（元 VOD 相対）**＋字幕 items＋テーマ＋settings |

**結論**：R2（アーカイブ）は結果画面が必要情報をほぼ保有しており実装容易。R1（クリップ用）は字幕画面が**ソースパスも編集点も持たない**ため、データ受け渡しの追加配線が必要（§5.3）。

### 2.2 字幕データ構造（両画面共通）

`SubtitleEditorWidget` の item / `result_items()`（`subtitle_editor_dialog.py:359-380`）:
```python
{"start": float, "end": float, "text": str,          # text は ASS 改行 \N を含む
 "use": bool, "role": "streamer"|"sub"|"comment",
 "font": str (""=既定), "font_size": int|None (None=既定)}
```
- 時間は編集対象動画に対する相対秒。クリップ用は**無音カット後の中間動画**基準、アーカイブ用は**各 prepared クリップ（無音カット済み）**基準（`_review_timeline` / `clip_writer`）。
- `role` により配信者/サブ/コメントの3系統に分かれ、色は `FontProfile.role_colors` で決まる。

### 2.3 スタイル（フォント/サイズ/位置/色）

`subtitle_generator.FontProfile`（`:73-104`）と `build_font_profile(subtitle_cfg)`（`:423-456`）が設定を集約:
- `family` / `size` / 役割別 `color_hex` / `outline_color` / `outline_width` / `back_color` / `bold`・`italic`・`underline`・`strikeout` / `spacing` / `angle` / `border_style` / **`alignment`（ASS テンキー配置：2=下中央, 5=中央, 7=左上）** / **`margin_l/r/v`**。
- 縦動画は `build_effective_subtitle_cfg`（`:470-480`）が `vertical` セクションで `font_size/alignment/margin_*` を上書き。
- **位置は「alignment＋margin」から決まり ASS Style 行に焼かれる**（`build_subtitle_file:523-581`）。DaVinci へはこの配置＋余白を Text+ の座標へ変換する（§5.5）。

### 2.4 カット編集点（R5 の核心・**現状は破棄されている**）

- `silence_cutter.build_keep_segments(silence_ranges, total_duration)`（`:77-86`）が **`[(keep_start, keep_end), …]`（元動画相対の残す区間）** を返す。これが DaVinci に必要な編集点そのもの。
- しかし `silence_cutter.run()`（`:396-466`）内で `keep_segments` は**ローカル変数**に留まり、動画を**物理的にカット・再エンコード連結**して `silence_cut.mp4` を作るのみ。`PipelineContext` は `_current_video_path` しか保持せず（`pipeline_context.py:42,52-57`）、**編集点はどこにも永続化されない**（リポジトリ全体で `build_keep_segments`/`keep_segments` は保存箇所なし）。
- アーカイブ用も同様に、各クリップの `_silence_cut`（`clip_writer.py:288-291`）は使い捨て `PipelineContext` で `silence_cutter.run` を呼び `prepared_path` のみ返す。**クリップ内の無音カット編集点は失われる**。ただし**クリップ単位の区間 `[start,end]`（元 VOD 相対）は `prepared` に残る**（`clip_writer.py:339-344`）。

> **含意**：編集点を Resolve へ渡すには「`keep_segments` を保持する」または「エクスポート時に `detect_silence`＋`build_keep_segments` を再計算する」必要がある（§5.2/§5.3）。

### 2.5 タイムライン諸元

- 解像度：`output_profile.resolve_output_profile(input_path, settings)`（`:48-74`）→ `{is_portrait, width, height}`（横 1920×1080 / 縦 1080×1920）。
- フレームレート：プロファイルに無く `ffmpeg.output_fps`（既定 **60**）を `ffmpeg_runner.get_output_fps` で取得。
- **タイムラインは width×height＝プロファイル値、fps＝`ffmpeg.output_fps` を用いる。**

### 2.6 設定・出力先

- `load_settings`／`_merge_with_defaults`（`settings_window.py:400-407`）は **`DEFAULT_SETTINGS` に定義された節のみ**マージ・永続化する。新規 `export` 節は `DEFAULT_SETTINGS` に追加必須（§6）。
- 出力先：`general.output_directory`（未設定なら入力ファイルのフォルダ）。アーカイブは `clip_writer._resolve_output_dir`（`:31-36`）。命名は `{prefix}_{stem}_…`（`clip_prefix` 既定 `"archive"`）。**Resolve プロジェクトファイルも同フォルダ・同命名規約で出力**する。

### 2.7 無音カット ON/OFF の現状（R7 の前提）

- **設定キーは既に存在**：`DEFAULT_SETTINGS["archive"]["clip_pipeline"]["silence_cut"]`（`settings_window.py:285-286`、既定 `True`）。
- **消費経路**：`clip_writer.run(...)` が `archive.clip_pipeline`（`clip_writer.py:461`）を読み、`_build_clip_settings` で **`clip_settings["silence_cut"]["enabled"]`** へ反映（`clip_writer.py:74`）。`_silence_cut`（`:287-291`）は `silence_cutter.run` を呼ぶが、`silence_cutter.run` 側が `silence_cut.enabled=false` で**スキップして元パスを返す**（`silence_cutter.py:398-403`）。
- **UI の現状**：`archive_tab.py` の `_build_ui`（`:206-291`）には**チェックボックスが1つも無い**（入力モード combo／パス入力／ログイン／採点開始ボタン／進捗のみ）。`archive.clip_pipeline.silence_cut` は**設定画面にも UI が無く**、`setting.json` 直編集でしか変えられない。
- **設定の読み込み**：`ArchiveTab._on_analyze`（`:390-391`）が実行ごとに `load_settings()` で最新化し、その dict を `ArchiveAnalyzeWorker`／`ArchiveClipWorker`／`ArchiveResultBridge` に渡す（`:414`, `:440-453`）。**この dict へ差し込めば UI 選択を1回の実行に反映できる**。
- **結論**：R7 は **UI の追加＋既存キーへの反映のみ**で成立し、**新規設定キー・パイプライン改変は不要**（§5.8）。

### 2.8 再利用できる資産

| 資産 | 用途 |
|---|---|
| `resolve_output_profile` / `get_output_fps` | タイムライン解像度・fps |
| `FontProfile` / `build_font_profile` / `build_effective_subtitle_cfg` | 字幕スタイル→Text+ 変換の入力 |
| `silence_cutter.detect_silence` / `build_keep_segments` | 編集点の（再）計算 |
| `_resolve_output_dir` / `clip_prefix` / 命名規約 | 出力先・ファイル名 |
| `SubtitleReviewBridge` / `ArchiveResultBridge` | ソースパス受け渡し経路（クリップ用は要拡張） |

---

## 3. 出力フォーマットの選定（重要）

「Resolve で開ける・編集点と字幕を再編集できる・字幕はフォント/サイズ/位置を編集可能」を、**Resolve 常駐なしに 1 ファイルで**満たす形式を選ぶ。

| 形式 | 生成可否（外部） | 編集点 | 字幕→編集可能タイトル | 「開く」体験 | 判定 |
|---|---|---|---|---|---|
| **.drp**（Resolve プロジェクト） | ✕（独自バイナリ・非公開） | ○ | ○ | ダブルクリックで開く | **不可**（外部生成できない） |
| **.drt**（Resolve タイムライン） | ✕（独自） | ○ | ○ | 取り込み | 不可 |
| **FCPXML(.fcpxml)** | ○（XML） | ○（クリップ列＝カット点） | **○（Text+ に変換、font/size/position/color を保持※）** | Resolve で **取り込み**（File > Import > Timeline / メディアプールへドラッグ） | **採用（第1候補）** |
| **EDL + SRT** | ○（テキスト） | ○（EDL） | △（SRT は字幕トラック。**per-字幕のフォント/位置は不可**） | 取り込み | 代替（字幕スタイル要件を満たさない） |
| **OTIO** | ○ | ○ | △（Text+ スタイル移送は限定的） | 取り込み | 代替 |
| **Resolve Scripting API** | ―（ファイルでない） | ◎ | ◎（Text+ を完全制御） | Resolve 常駐必須 | 将来オプション（§10-6） |

※ FCPXML のタイトルは Resolve 取り込み時に **Text+ 相当へ変換**され、**フォント・サイズ・色は概ね保持**されるが、**位置(Position)は中央へリセットされる事例が報告**されている（FCP↔Resolve の既知の相互運用差）。位置は「配置(alignment)＋余白」からの近似変換とし、完全一致は保証しない（§5.5・§10-2）。

### 採用方針
- **第1候補＝FCPXML（`.fcpxml`）**。理由：単一ファイルで「カット編集点（ソースを参照する複数クリップ）」と「フォント/サイズ/色を持つタイトル」を表現でき、Resolve が取り込める。追加の外部依存が不要（Python 標準の XML 生成で足りる）。
- **R4「ファイルを開くと開ける」の解釈（確定 2026-07-28・§10-1）**：`.drp` の「ダブルクリックでプロジェクトとして開く」は独自バイナリのため外部生成不可。**現実的な UX＝「`.fcpxml` を Resolve に取り込む（Import）」で確定**する（`.drp` ダブルクリック起動は要件としない）。
- EDL+SRT は「編集点の堅牢さ」を重視する場合の**任意併記出力**として設定で選べるようにする（§6）。

---

## 4. 設計方針（全体）

1. **既存フローは不変**。エクスポートは「読み取り専用の追加機能」。焼き込み・パイプライン・採点のロジックは一切変更しない。ボタンは各画面に**追加するだけ**。
2. **新規モジュール `src/export/`** に閉じる。`fcpxml_builder.py`（FCPXML 生成の純関数群）＋ `resolve_export.py`（両画面から呼ぶ橋渡し・データ収集）。**新規サードパーティ依存なし**（`xml.etree.ElementTree` / 文字列テンプレートで生成）。
3. **編集点はソース相対で表現**。タイムライン＝「残す区間(keep_segments)を連結した結果」とし、各区間を**元ソースを参照するクリップ**として並べる。これにより Resolve 上でカット点が編集点として再編集可能になる（§5.2）。
4. **字幕時間はタイムライン時間に一致**。字幕 items の時間は「カット後（連結後）動画」基準であり、これはタイムライン時間そのもの。よって**時間変換なしにタイトルを配置できる**（§5.2 の要）。
5. **スタイルは設定＋item から変換**。`FontProfile`＋item の font/font_size/role を Text+ の font/fontSize/color/位置へマッピング（§5.5）。ハードコード回避のため変換パラメータは `export` 設定に置く。
6. **設定は setting.json 管理**。出力形式・タイムライン名・タイトル既定・座標変換係数などを `export` 節へ（§6）。既定は FCPXML・現行スタイル準拠。
7. **後方互換**：`export` 節は追加のみ。未定義でも `_merge_with_defaults` が既定補完。ボタン以外の UI/挙動は不変。
8. **不明点は推測実装しない**：FCPXML の細部（スキーマ版・タイトル effect の UID・位置移送の可否）と R1 の編集点整合は§10 の確認事項として明示し、確定後に実装する。

---

## 5. 詳細設計

### 5.1 新規／変更ファイル一覧（予定）

| 種別 | ファイル | 内容 |
|---|---|---|
| 新規 | `src/export/__init__.py` | パッケージ |
| 新規 | `src/export/fcpxml_builder.py` | FCPXML 文字列生成（純関数：resources/format/asset、spine、asset-clip、title、text-style）。副作用なしで単体検証可能に |
| 新規 | `src/export/resolve_export.py` | 両画面共通のエクスポート・オーケストレーション（編集点・字幕・スタイル・諸元を収集 → builder 呼び出し → ファイル書き出し）。設定 `export` を読む |
| 新規 | `src/export/config.py` | `export` 設定の平坦化（archive/config.py と同型） |
| 変更 | `src/gui/subtitle_editor_dialog.py` | button_row に「DaVinci Resolve 出力」追加。**source_path / edit_points を受け取る引数を追加**（既定 None＝ボタン無効 or 非表示。後方互換） |
| 変更 | `src/gui/main_window.py` | `SubtitleReviewBridge` に source_path と（再計算した）編集点を渡す配線（§5.3） |
| 変更 | `src/gui/archive_result_window.py` | button_row に「DaVinci Resolve 出力」追加。保有する source_path＋クリップ区間＋items でエクスポート呼び出し（§5.4） |
| 変更 | `src/gui/archive_tab.py` | **（R7）「無音カット」チェックボックスを追加**し、実行時に `archive.clip_pipeline.silence_cut` へ反映（§5.8）。**新規設定キーなし** |
| 変更 | `src/settings/settings_window.py` | `DEFAULT_SETTINGS["export"]` 追加（§6） |
| 変更 | `src/settings/setting.json` | `export` 既定値追加 |
| 変更 | `src/modules/silence_cutter.py` / `src/pipeline/pipeline_context.py` | クリップ用の編集点整合のため `keep_segments`（元ソース相対）を `PipelineContext` に保持する（**§5.3 案B・確定**）。**全処理完了時／結果画面を閉じた時にクリアする**（§5.3 寿命管理・確定） |
| 変更 | `src/modules/subtitle_generator.py` | レビュー呼び出し直前にブリッジへ Resolve 出力用の付随情報（元入力パス/編集点/プロファイル）を渡す（`set_export_context` を持つブリッジのみ。CLI/テストは無影響） |
| 変更 | `src/archive/clip_writer.py` | `_silence_cut` がクリップ内 `keep_segments` を返し `prepared` へ引き継ぐ（§5.4 案①）。レビュー後にクリア |
| 変更 | `src/exceptions.py` | `ExportError`（エクスポート固有の失敗）を追加（§7） |
| 新規 | `tests/test_fcpxml_builder.py` / `tests/test_resolve_export.py` | builder（時刻量子化/色/spine/title）と spec 組み立て（編集点連結・スタイル変換・位置近似・書き出し）の単体テスト（§11-1。標準 `unittest` のみ使用＝新規依存なし） |

> **spec 変更は不要**（新規依存なし。`src/export` は通常 import のため PyInstaller の静的解析で同梱される）。

### 5.2 タイムライン・モデル（編集点と時間軸）

中核となる時間軸の考え方（両フロー共通）:

```
元ソース(VOD/入力)     : [.....無音....][ keepA ][..無音..][ keepB ][...]
残す区間 keep_segments :               [ keepA ]           [ keepB ]
Resolve タイムライン    : | keepA | keepB |            ← 連結（無音を詰めた結果）
字幕時間(items)         :   ↑ ここは「連結後動画」の時間 = タイムライン時間に一致
```

- タイムライン上の各クリップ = keep 区間。`asset-clip` の
  - `offset`（タイムライン位置）＝それ以前の keep 長の累積和、
  - `start`（ソース内イン点）＝ `keep_start`、
  - `duration` ＝ `keep_end − keep_start`。
- 字幕 items は連結後時間なので、`title` の `offset` に item.start を**そのまま**用いれば整合する（時間変換不要＝方針4）。
- **フレーム量子化**：FCPXML の時間は fps の分数（例 60fps→`frameDuration=1/60s`、各時刻は 1/60 の倍数）。全時刻を最近傍フレームへ丸める（builder が担当）。

この model により「カット編集点＝クリップ境界」「字幕＝タイトル」が Resolve で個別に再編集可能になる（R5/R6）。

### 5.3 クリップ用の出力（R1）— データ配線

**課題（§2.4）**：字幕画面はソースパスも編集点も持たず、字幕時間は無音カット後動画基準。

**方針（案B・確定 2026-07-28）**：`silence_cutter.run` が算出する `keep_segments`（元ソース相対）を `PipelineContext` に**保持**し、レビュー callback（`SubtitleReviewBridge`）経由で字幕画面／エクスポートへ渡す。あわせて**元入力パス**も `SubtitleReviewBridge` から渡す（`main_window` が保持）。再計算（旧案A）は行わない。

- 保持箇所：`PipelineContext` に `keep_segments`（と対応する元ソースパス）を格納するフィールド／setter/getter を追加。`silence_cutter.run` の末尾で set する。
- 受け渡し：`SubtitleReviewBridge` が context 由来の `keep_segments`＋source_path を保持し、エクスポート時に `resolve_export` へ供給。
- 長所：無音検出の再実行が不要。短所：共有パイプライン（`silence_cutter`/`PipelineContext`）へ**追加のみ**の軽微改変が入る（既存の出力・フローは不変＝後方互換、§9）。

**編集点の寿命管理（確定 2026-07-28）**：保持した `keep_segments` は**その回の処理専用の一時データ**とし、**「全処理の完了時」または「結果／字幕画面を閉じた時」にクリアする**（メモリに残さない）。
- 具体：`SubtitleReviewBridge`／`ArchiveResultBridge` はダイアログ終了（accept/reject/クローズ）時に保持中の `keep_segments`・source を破棄する。`PipelineContext.cleanup`／実行完了時にも context 側の保持をクリアする。
- 目的：次回実行への持ち越し・取り違えを防ぎ、機微でない中間データも溜めない（`cleanup` 思想と整合）。

**時間軸の注意**：`keep_segments` を用いると字幕 items の時間（無音カット後基準）＝タイムライン時間となり整合する（§5.2）。**ただし** `speech_gate`／表示保持等で字幕時間が微調整されている端数は許容誤差とする（§10-4）。

### 5.4 アーカイブ用の出力（R2）

`ArchiveResultWindow` は必要情報を保有（§2.1）。エクスポートは以下で構成:

- **ソース**＝ `self._source_path`（元 VOD）。
- **編集点**＝各 TOP クリップ区間 `prepared[i]["start"/"end"]`（元 VOD 相対）を**タイムライン上に順に連結**（TOP1, TOP2, … の順、`use=False` は除外）。
- **字幕**＝各クリップの編集済み items（`result_data()` の items）。
  - **整合上の注意（§2.4）**：items 時間は「prepared（クリップ内無音カット済み）」基準だが、クリップ区間 `[start,end]` は**生の VOD 区間**（無音を含む・長い）。このままでは字幕がずれる。対処案（§10-5）:
    - **案①（採用・確定 2026-07-28）**：クリップ内 `keep_segments` も**保持**し（§5.3 案B と同方式でクリップ単位に格納→ウィンドウ閉時にクリア）、§5.2 の model をクリップ単位で適用（無音を詰めたクリップをタイムライン化・字幕整合）。**整合が正確**で要望 R5 と一致。`clip_writer._silence_cut` が使い捨て context で捨てている keep をクリップ結果へ引き継ぐ。
    - （不採用）案②：メディアを prepared クリップ（無音カット済みファイル）にする方式は「元 VOD に対するカット点の再編集」から外れるため採らない。
- **テーマ／イントロカード**（resolve19）は Resolve 出力に**含める（確定 2026-07-28・§10-7）**。ただし FCPXML では次のとおり表現粒度に差が出る：
  - **含められるも**：イントロの**バッファ区間**（`intro_card.buffer_sec` 分を元 VOD から戻した区間）をタイムライン先頭クリップとして、**中央テーマ文字**と**左上タグ文字**を Text+ タイトルとして出力（フォント/サイズ/色は §5.5 と同方式で変換）。
  - **表現できないもの**：ブラー・黒帯オーバーレイ等の映像エフェクトは FCPXML で再現不可のため出力しない（Resolve 側で必要なら手動付与）。テキストと区間のみを移送するベストエフォートとする。この差分は §8 で一度 WARNING 周知。

### 5.5 字幕 → Resolve タイトル（Text+）変換（R6）

各字幕 item を 1 つの `title`（Resolve 取り込みで Text+ 相当）に変換する。

| 字幕側（FontProfile / item） | FCPXML `text-style` / title | 備考 |
|---|---|---|
| `item.font` or `FontProfile.family` | `font` | item 個別指定を優先、無ければ既定 |
| `item.font_size` or `FontProfile.size` | `fontSize` | 同上。縦動画は vertical 上書き値 |
| 役割色 `role_colors[role]`（#RRGGBB） | `fontColor`（RGBA 0–1 実数4値） | #RRGGBB→(r,g,b,1.0) 変換 |
| `outline_color`/`outline_width` | `strokeColor`/`strokeWidth` | ASS `&HAABBGGRR`→RGBA 変換 |
| `bold`/`italic`/`underline` | `bold`/`italic`/`underline` | フラグ移送 |
| **`alignment`（ASS テンキー 1–9）＋`margin_l/r/v`** | title の **位置(Position X/Y)＋テキスト整列** | §下記マッピング |
| `text`（`\N` 改行） | `<text>` 本文（改行を実改行へ） | **コメント役割は本文先頭に「コメント：」を含める（確定 2026-07-28・§10-8、現行焼き込みと一致）** |

**配置(alignment)→位置マッピング（近似）**：ASS のテンキー配置（1=左下…5=中央…9=右上）と余白 px を、タイムライン解像度（width×height）を基準に Text+ の中心座標へ換算する。
- 水平：左寄せ→`margin_l`、右寄せ→`width−margin_r`、中央→`width/2`。
- 垂直：下→`height−margin_v`、上→`margin_v`、中央→`height/2`。
- Resolve の座標系（中心原点・正規化 or px）へ変換する係数は `export.title` 設定に置く（ハードコード回避）。
- **位置は「できる限り近似」で出力（確定 2026-07-28・§10-2）**：§3 のとおり Resolve 取り込みで**位置が中央に戻る**ことがあり得るが、可能な限り alignment＋余白から座標を近似して付与する（`export.title` の変換係数で調整）。フォント・サイズ・色は比較的保持される。完全一致は保証しない。

### 5.6 FCPXML 構造（スキーマ概略・確定は§10-1）

生成する XML の骨子（`version` は 1.9 or 1.10 を§10-1 で確定）:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.9">
  <resources>
    <format id="r1" name="FFVideoFormat1080p60"
            frameDuration="1/60s" width="1920" height="1080"/>
    <asset id="r2" name="source" start="0s" hasVideo="1" hasAudio="1"
           format="r1" duration="&lt;元尺&gt;s">
      <media-rep kind="original-media" src="file:///&lt;絶対パス&gt;/source.mp4"/>
    </asset>
    <effect id="rTitle" name="Text" uid="&lt;Text+ or Basic Title の UID&gt;"/>
  </resources>
  <library>
    <event name="Stretheus">
      <project name="&lt;prefix&gt;_&lt;stem&gt;">
        <sequence format="r1" tcFormat="NDF" duration="&lt;合計&gt;s">
          <spine>
            <!-- keep 区間ごとにクリップ (offset=累積, start=keep_start, duration=長さ) -->
            <asset-clip ref="r2" offset="0s" start="120/60s" duration="300/60s" name="cutA"/>
            <asset-clip ref="r2" offset="300/60s" start="600/60s" duration="240/60s" name="cutB"/>
            <!-- 字幕タイトル (connected clip, lane=1) -->
            <title ref="rTitle" lane="1" offset="30/60s" duration="90/60s" name="字幕1">
              <text><text-style ref="ts1">こんにちは</text-style></text>
              <text-style-def id="ts1">
                <text-style font="Yu Gothic UI" fontSize="130" fontColor="0.93 0 0.02 1"
                            bold="1" alignment="center"/>
              </text-style-def>
              <!-- 位置パラメータ (近似) -->
              <param name="Position" value="0 -420"/>
            </title>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
```
- 時刻は全て `フレーム/60s` 等の有理数（フレーム量子化）。
- `title` は connected clip（`lane` 正値）として spine 上のクリップに重ねる。
- `effect` の `uid` は Resolve が Text+ として解釈できる値を用いる（§10-1 で確定。Basic Title 相当にフォールバック可）。

> 実装では ElementTree で厳密に生成し、DTD/スキーマ差異は§10-1 の確定に合わせる。**サンプルは概略**であり、属性名・階層は確定後に精緻化する。

### 5.7 出力先・ファイル名

- 出力先：`general.output_directory`（未設定は入力/VOD のフォルダ）。既存 `_resolve_output_dir` 同方針。
- ファイル名：`{prefix}_{stem}.fcpxml`（クリップ用 prefix は設定既定 `""` or `clip`、アーカイブは `archive`）。EDL/SRT 併記時は `.edl`/`.srt` を同名で。
- 上書き確認は UI（`QMessageBox`）で行う。

### 5.8 無音カット ON/OFF チェックボックス（R7・追記）

**設置場所**：`ArchiveTab._build_ui`（`archive_tab.py:206-291`）の **「採点開始」ボタンの直前**（入力グループの下）に「実行オプション」行として `QCheckBox` を1つ追加する。

```
[入力ソース: ローカル動画 ▼]
[ローカル / Twitch 入力グループ]
[x] 無音カット (クリップから無音区間を除去する)   ← 追加 (R7)
[          採点開始          ]
[進捗バー][ステータス]
```

| 項目 | 設計 |
|---|---|
| ウィジェット | `self.silence_cut_check = QCheckBox("無音カット")`（ツールチップで「各クリップから無音区間を除去します」を補足） |
| 初期値 | `self._settings["archive"]["clip_pipeline"]["silence_cut"]`（`config.clip_pipeline_config` 相当の既存参照経路。未定義時は `_merge_with_defaults` により `True`）。**ハードコードしない** |
| 反映先 | `_on_analyze` で `load_settings()` した dict に対し `settings["archive"]["clip_pipeline"]["silence_cut"] = self.silence_cut_check.isChecked()` を差し込む。以降は既存経路（`clip_writer.py:461,74` → `silence_cutter.py:398-403`）がそのまま処理する |
| 反映範囲 | **その実行のみ（in-memory）**。`setting.json` へは**書き戻さない**（設定画面の責務を侵さない・§10-10 で確定要） |
| 実行中の操作 | `_set_running(True/False)`（`archive_tab.py:483-`）で `setEnabled(not running)`。既存の入力欄／ボタン群と同じ扱い |
| `archive.enabled=false` 時 | `_build_disabled_ui` 側には追加しない（UI 無し）。属性は `None` 初期化し参照時に既存値へフォールバック |

**OFF 時の挙動（既存ロジックのまま）**：`silence_cutter.run` が「無音カットスキップ」を INFO ログに出し **元クリップをそのまま `prepared_path`** とする（`clip_writer.py:287-291`）。切り抜き・文字起こし・字幕焼き込み・結合は従来どおり動作する。

**Resolve 出力（R2/§5.4）との整合**：
- **ON**：従来どおり §5.4 案①（クリップ内 `keep_segments` を保持）でタイムライン化する。
- **OFF**：`keep_segments` が「クリップ全長 1 区間」に退化するため、**タイムライン = 生のクリップ区間 `[start,end]` そのまま**となり、字幕時間（= 生クリップ相対）と**完全に一致**する（§5.4 のズレ問題が発生しない）。
- よって `resolve_export` は「keep が空／未保持ならクリップ全長 1 区間として扱う」既定（§7「編集点が空」）で両ケースを分岐なしに処理できる。

**設定画面との関係**：`archive.clip_pipeline.silence_cut` は設定画面に UI が無い（§2.7）。本チェックボックスが**実質的な操作入口**となる。設定画面側への UI 追加は本要望の範囲外（必要なら別要望）。

---

## 6. setting.json 追加案（**追加のみ**・既存キー不変）

`DEFAULT_SETTINGS`（`settings_window.py:117`）に新規トップレベル `export` を追加（`_merge_with_defaults` が欠落補完＝後方互換）。

```jsonc
"export": {
  "resolve": {
    "enabled": true,               // 出力ボタンの表示 (false でボタン非表示=完全後方互換)
    "format": "fcpxml",            // 実装は "fcpxml" のみ (将来 "edl_srt"/"otio"。§11-5)
    "fcpxml_version": "1.9",       // 生成する FCPXML のスキーマ版
    "event_name": "Stretheus",     // FCPXML の event 名
    "title_effect_uid": "",        // Text+ として解釈させる effect UID (空=Basic Title 相当)
    "clip_edit_points": "context", // クリップ用の編集点取得: "context"(案B・確定) | "recompute"
    "include_intro_card": true,    // アーカイブのイントロ/タグ演出を出力に含めるか (確定=含める・§5.4)
    "clip_prefix": "clip",         // クリップ用出力の接頭辞 (空文字=接頭辞なし。§5.7 のハードコード回避)
    "title": {                     // 位置変換の係数 (ハードコード回避)
      "coord_space": "normalized", // "normalized" | "pixel"
      "origin": "center"           // Resolve 座標の原点
    }
  }
}
```
- `format` は **`"fcpxml"` のみ実装**。`"edl_srt"`/`"otio"` を指定した場合は WARNING を出して FCPXML で出力する（黙って別形式を装わない。§11-5 で追加）。
- 既存セクション（`general`/`subtitle`/`silence_cut`/`ffmpeg`/`vertical`/`archive`…）は**一切変更しない**。
- **R7（無音カット チェックボックス）は設定追加なし**：既存キー `archive.clip_pipeline.silence_cut`（`settings_window.py:285-286`、既定 `true`）をそのまま初期値／保存先の参照として使う。新規キー・既定値変更は行わない（§5.8）。
- パス解決・命名は既存方針（`SETTINGS_DIR` 相対 or 絶対、`_resolve_output_dir`、`clip_prefix`）に合わせる。

---

## 7. エラー処理・フォールバック方針

- **ソース未特定/存在しない**（クリップ用でパス未配線・ファイル移動）→ `InputError` で UI 通知しエクスポート中止。既存 `AutoEditError` 系を流用（新規例外は最小限、必要なら `ExportError`）。
- **編集点が空**（無音検出0・全体1区間）→ 全長 1 クリップとして出力（有効）。
- **字幕0件**→ カット編集点のみのタイムラインを出力（字幕タイトル無し）。
- **FCPXML 生成失敗**（想定外データ）→ 例外を UI へ集約通知、ファイルは書き出さない（部分ファイルを残さない：一時ファイル→成功時 rename）。
- **上書き**→ 既存ファイルがあれば確認ダイアログ。
- 例外は各画面の既存集約通知（`QMessageBox.critical`）に載せる。パイプライン本体には影響させない（読み取り専用機能）。

## 8. ログ出力方針

- エクスポート開始/完了（出力パス・クリップ数・字幕数・タイムライン尺）を INFO。
- 編集点の再計算（案A）実行と所要を INFO。位置変換の近似・Resolve 側で位置が戻り得る旨は一度 WARNING（利用者周知）。
- 生成失敗は ERROR（原因・データ要約）。
- 既存 `utils/logger.get_logger` を流用（`logging` 設定準拠）。機微情報は出さない。

## 9. 影響範囲・後方互換

- **現行クリップ／アーカイブ機能**：ボタン追加のみ。焼き込み・採点・パイプラインの**フロー/出力は不変**（回帰テスト＝現行動画で従来出力と一致）。
- **`SubtitleEditorDialog`**：追加引数は既定 None で**既存呼び出し非破壊**（`main_window` の既存 callback 経路はそのまま。source_path/編集点を渡さなければボタンは非表示 or 無効）。
- **`setting.json`**：`export` は追加のみ。旧設定も `_merge_with_defaults` で自動補完。
- **依存**：新規サードパーティ依存**なし**（標準 XML で生成）。CPU-only 方針・配布サイズに影響なし。
- **R7（アーカイブタブのチェックボックス）**：`archive_tab.py` への**ウィジェット追加のみ**。既定値は既存キーから読むため、**チェックを触らなければ従来と完全同一の挙動**。パイプライン（`clip_writer`/`silence_cutter`）は**無改変**（既存の `silence_cut.enabled` 経路を使うだけ）。`setting.json` へ書き戻さないので設定ファイルも不変。
- **案B（確定）**により `silence_cutter`/`PipelineContext` へ**追加のみ**の軽微改変が入る（`keep_segments` の保持フィールド＋set/getter＋クリア）。既存の出力・カット結果・フローは不変。保持データは全処理完了時／ウィンドウ閉時にクリアするため次回実行へ持ち越さない（§5.3）。

## 10. 確認事項 → **回答反映（2026-07-28）**

> ユーザー回答により #1/#2/#3/#5/#7/#8 を確定。#4/#6/#9 は既定を採用（#9 は実装時に対象 Resolve 版で最終決定）。

| # | 項目 | 論点 | **回答（確定 2026-07-28）** |
|---|---|---|---|
| 1 | **出力形式の最終確定** | 取り込み(import)で許容か／`.drp` 起動が必須か | **確定：FCPXML を「取り込み(import)」で開く方式**。`.drp` ダブルクリック起動は要件としない |
| 2 | **タイトル位置の再現度** | 位置はベストエフォートで良いか | **確定：できる限り近似**して出力（完全一致は保証しない。フォント/サイズ/色は保持） |
| 3 | **クリップ用の編集点取得** | 案A(再計算) or 案B(保持) | **確定：案B（`PipelineContext` に保持）**。かつ**全処理完了時／ウィンドウ閉時に編集点をクリア**（§5.3 寿命管理） |
| 4 | **字幕時間の基準ズレ許容** | speech_gate/表示保持の微調整端数 | 既定採用：フレーム丸めで許容 |
| 5 | **アーカイブの字幕整合** | 生 VOD 区間 vs prepared クリップ（§5.4 案①/②） | **確定：案①**（クリップ内 keep_segments を案B同様に保持・ウィンドウ閉時にクリア。要望 R5 と一致） |
| 6 | **Scripting API 方式の要否** | 完全再現／Resolve 常駐前提の要否 | 既定採用：当面は FCPXML のみ（未要望。将来オプション） |
| 7 | **イントロカード/テーマの扱い** | resolve19 の演出を Resolve 出力に含めるか | **確定：含める**。ただしテキスト（中央テーマ/左上タグ）と区間のみを Text+ で移送。ブラー等の映像効果は FCPXML で表現不可のため対象外（§5.4） |
| 8 | **コメント役割ラベル** | 「コメント：」を Text+ 本文に含めるか | **確定：含める**（Text 本文先頭。現行焼き込みと一致） |
| 9 | **FCPXML スキーマ版/Text+ UID** | 対象 Resolve バージョン、Text+ の effect UID | 未確定：実装時に対象 Resolve 版で確定（既定 1.9、UID 未確定時は Basic Title フォールバック） |
| 10 | **（追記 R7）チェック状態の永続化** | チェックの ON/OFF を **その実行のみ**に効かせるか、`setting.json` に**保存して次回も復元**するか | 既定採用：**その実行のみ（in-memory・setting.json 不変）**。初期値は毎回 setting.json から復元。次回起動へ引き継ぎたい場合は要指示（保存処理を追加） |

## 11. 段階実装（レビュー後）

1. **基盤**：`src/export/`（fcpxml_builder＋resolve_export＋config）と `export` 設定。builder は純関数で**単体テスト**（keep 区間→spine、item→title、スタイル変換、フレーム量子化）。
2. **アーカイブ用（R2）先行**：必要情報が揃う `ArchiveResultWindow` にボタン追加 → 実 VOD で Resolve 取り込み確認（§5.4）。字幕整合（§10-5）を検証。
3. **クリップ用（R1）**：`SubtitleReviewBridge`/`SubtitleEditorDialog` に source_path＋編集点(案A)を配線しボタン追加 → 実動画で確認。
4. **スタイル精緻化**：フォント/サイズ/色/位置の Resolve 再現度を実機確認し、位置変換係数を `export.title` で調整（§10-2）。
5. **（任意）EDL+SRT 併記**・**Scripting API 方式**（§10-6）を要望に応じ追加。

**R7（無音カット チェックボックス）は独立実装**：設定追加もパイプライン改変も無いため、上記 1〜5 と依存せず**単独で先行実装・確認できる**（§5.8）。確認観点＝①既定値が setting.json と一致、②OFF で「無音カットスキップ」ログが出て尺が縮まらない、③ON で従来どおり縮む、④実行中は操作不可、⑤`archive.enabled=false` でも例外なく起動。

実装着手は本設計書のレビュー後とする（CLAUDE.md「実装は設計書レビュー後に行う」遵守）。

---

## 12. 実装状況（2026-07-30）

本設計書に基づき実装済み。段階 1〜4 と R7 を反映し、5（EDL+SRT / Scripting API）は未実装（設定で指定された場合は WARNING を出して FCPXML で出力）。

| 項目 | 実装 | 備考 |
|---|---|---|
| R1 クリップ用の出力ボタン | 済 | `SubtitleEditorDialog(export_context=...)`。未注入なら**ボタン非表示**＝既存呼び出し非破壊 |
| R2 アーカイブ用の出力ボタン | 済 | `ArchiveResultWindow`。使用チェックしたクリップのみ TOP 順に連結 |
| R3/R4 プロジェクトファイル | 済 | `.fcpxml` を出力先へ生成（取り込みで開く。§3 採用方針どおり） |
| R5 カット編集点 | 済 | keep 区間ごとの `asset-clip`（offset=累積和 / start=元ソース内イン点） |
| R6 字幕＝Text+ | 済 | `title`＋`text-style`（font/fontSize/fontColor/stroke/装飾/整列）＋`param Position`（近似） |
| R7 無音カット チェック | 済 | アーカイブタブ。既存キーへ実行時反映・`setting.json` 不変・実行中は操作不可 |
| 編集点の寿命管理 | 済 | `PipelineContext.cleanup` / ブリッジのダイアログ終了時 / 結果画面 `done()` / レビュー後の `prepared` でクリア |
| 単体テスト | 済 | 27 件（`python -m unittest discover -s tests`）全て成功 |

**未確定のまま残る点（§10-9）**：Text+ の effect UID。既定は Basic Title 相当の UID をフォールバックとして埋め込み、`export.resolve.title_effect_uid` で差し替え可能にした。実機（対象 Resolve 版）での取り込み確認後に確定する。位置の再現度（§10-2）も実機確認で `export.resolve.title` を調整する。
