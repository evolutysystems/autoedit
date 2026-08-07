# resolve（ver3） — Timeline 型クリップ編集画面（プレビュー＋ノンリニア Timeline）要望設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md` の「いきなり実装を開始しない」に従う）。
* 記載の判断はすべて既存コードの実測に基づく。実測できなかった点・仕様として決めきれない点は
  推測実装せず **§15 確認事項** に列挙した（回答済みの項目は §1.1 に反映済み）。
* 本機能は既存クリップ編集フローの **置き換え** だが、現行の字幕編集画面（`SubtitleEditorDialog`）は
  コードごと残し、設定で切り替える。既定は新 UI、`timeline.enabled=false` で完全に従来動作へ戻す。

---

## 1. 要望（request.md 要約）

| ID | 要望 | 本書での扱い |
|---|---|---|
| R1 | 現在の字幕編集画面を出すタイミングで、新しい編集画面（上=プレビュー / 下=Timeline）を表示する | §5.1 / §6.1 |
| R2 | 現字幕編集画面は削除せず残す。ただし新 UI 実装後は表示しない | §5.1 / §10 |
| R3 | 無音カットを「実カット」から「編集点を JSON 保存」方式へ変更する | §4 / §6.2 / §7 |
| R4 | Timeline 表示時は JSON を読み、カット済みとして表示する | §6.2 / §6.3 |
| R5 | 最終動画生成時は Timeline 情報を参照して書き出す | §8 |
| R6 | プレビューは再生ヘッドのフレームを表示し、操作にリアルタイム追従する | §6.4 |
| R7 | プレビュー上で画像・字幕をクリック選択／ドラッグ移動できる | §6.5 |
| R8 | プレビューの右クリックメニュー: 削除 / レイヤー（最前面・前面・背面・最背面） | §6.5 |
| R9 | Video / Audio / Subtitle の 3 トラックを持つ | §6.3 |
| R10 | クリップの左右端ドラッグで尺変更、ドラッグで開始位置変更、編集点追加、削除 | §6.6 |
| R11 | Timeline の拡大・縮小 / 横スクロール / 再生ヘッド移動 | §6.7 |
| R12 | ローカルの画像・動画を Timeline へ D&D で読み込み、必要に応じて新 Video Track を作る | §6.8 |
| R13 | 画面下部の「決定」ボタンで Timeline の内容を反映した動画を書き出す | §8 |
| R14 | 既存コードを可能な限り流用する / UI と動画編集処理を分離する / Timeline を唯一のソースとする | §4 / §5 |

### 1.1 レビュー回答による追加要件（2026-08-07）

§15 の確認事項に対する回答を反映し、以下を要件へ格上げした。

| ID | 追加要件 | 本書での扱い |
|---|---|---|
| R15 | **オープニング / エンディングを使用する場合、Timeline 上で編集可能にする**（Q1） | §4-7 / §6.9 / §8 |
| R16 | **プレビューで音を聞けるようにする**（Q4） | §3-5 / §6.4-5 |
| R17 | 削除は空白を残すのが既定。**リップル削除も明示的に選べる**こと（Q5） | §6.6 |
| R18 | **Audio は Video と連動し、Video 側の編集（カット等）を Audio Track にも適用する**（Q3） | §4-9 / §6.3.4 |

回答により確定した仕様（設計変更を伴わないもの）:

* **Q2** 字幕は V1 の編集に追従しない（一旦追従不要）。§6.3.1 の設計どおり。
* **Q6** プロジェクト JSON は**常に作り直す**（既存ファイルは読み戻さない）。
  プロジェクト単位の保存・再編集は将来拡張とする。§6.2.1 / §13。
* **Q8** 画像の既定表示尺は 5.0 秒で妥当。
* **Q9** オーバーレイのトランジションは未対応でよい。将来拡張に留める。§13。
* **Q10** 後日再編集のための中間ファイル永続化は不要。従来どおり実行完了で破棄する。§12。

未回答のまま残っている論点は §15 に 3 件（Q3 / Q7 / Q11）。

---

## 2. 現状分析（既存コードの実測）

### 2.1 現在のクリップ用パイプライン

`src/pipeline/pipeline_runner.py:34 run_pipeline()` が全工程を順に呼ぶ（`total_steps=5`）。

```
入力動画
 ⓪ 音声解析・正規化    loudness_normalizer.run()      ← 映像は -c:v copy（時間軸不変）
 ─ 音量解析・閾値確認   volume_analyzer + ダイアログ    ← 確定 dB を volume_analysis.last_cut_db へ
 ① 無音カット          silence_cutter.run()           ← 検出 ＋ 実カット ＋ 再エンコード
 ② フルテロップ生成     subtitle_generator.run()       ← 音声認識 → 字幕編集画面 → ASS 焼き込み
 ③ オープニング結合     concat_processor.run(opening)
 ④ エンディング結合     concat_processor.run(ending)
 ─ 出力                output_writer.run()
```

各工程は `PipelineContext`（`src/pipeline/pipeline_context.py:12`）を介して
`current_video_path()` を差し替えながらバケツリレーする。

### 2.2 無音カットの実装（R3 の変更対象）

`src/modules/silence_cutter.py:396 run()` は次の 3 段構成になっている。

| 段 | 関数 | 内容 | R3 後の扱い |
|---|---|---|---|
| 検出 | `detect_silence()` (`:23`) | `silencedetect` を実行し無音区間を返す | **そのまま流用** |
| 算出 | `build_keep_segments()` (`:77`) | 無音の補集合＝残す有音区間 `[(start,end),…]` | **そのまま流用** |
| 実カット | `cut_and_concat_seek()` (`:223`) / `cut_and_concat_batched()` (`:270`) | 区間抽出→連結（重い再エンコード） | 編集点モードでは**呼ばない**（従来モード用に温存） |

重要な既存資産として、`:448` で算出済みの区間を `context.set_keep_segments()` へ保持している
（`pipeline_context.py:54`、resolve20 で Resolve 出力用に追加されたもの）。
**つまり「編集点」という概念と、それを保持する配線は既に存在する。** 本要望はこれを
一時データから**永続データ（JSON）＋ Timeline の起点**へ昇格させるものと位置づけられる。

さらに `src/export/resolve_export.py:309 _clips_from_segments()` は
`keep_segments` を「クリップ列（start / duration / name）」へ変換済みで、
Timeline のクリップ列とほぼ同型のデータを既に作っている。

### 2.3 字幕編集画面まわり（R1・R2 の接続点）

```
subtitle_generator.run()            src/modules/subtitle_generator.py:909
  └ _review_timeline()                              :855
      └ context.subtitle_review_callback(items)     ← GUI 経路のみ注入
          = SubtitleReviewBridge.__call__()   src/gui/main_window.py:137
              └ review_requested.emit()   （Qt::QueuedConnection でメインスレッドへ）
                  └ _on_review_requested()                :155
                      └ SubtitleEditorDialog.exec()  src/gui/subtitle_editor_dialog.py:480
```

* ワーカースレッド（`PipelineWorker`）→ メインスレッドの橋渡しは
  `threading.Event` によるブロッキング同期で確立済み（`main_window.py:130`）。
  **新画面もこの橋渡し機構をそのまま再利用できる。**
* `SubtitleReviewBridge` は既に `set_export_context()` / `set_preview_context()` という
  「付随情報の後注入」口を持つ（`:147` / `:151`）。同じ形で Timeline を注入できる。
* 字幕 1 件のデータ形は全画面共通で
  `{"start","end","text","use","role","font","font_size"}`（`subtitle_editor_dialog.py:412`）。

### 2.4 プレビューの現状（R6 の基準）

`src/gui/subtitle_preview_widget.py:107 SubtitlePreviewWidget` は
**「選択行の時刻から `preview.segment_sec` 秒を ffmpeg で焼き込み生成 → QtMultimedia で再生」** 方式。

* 生成はワーカースレッド（`_PreviewWorker`, `:62`）で行い、最新ジョブ以外は破棄する（`:414`）。
* 1 回の生成に ffmpeg プロセス起動＋エンコードが走るため、**再生ヘッドのスクラブ追従は不可能**。
* R6 の「Timeline 操作にリアルタイム追従」を満たすには別方式が要る（§3 で選定）。

### 2.5 焼き込み・レンダリング資産（R5 の基盤）

| 資産 | 位置 | 用途 |
|---|---|---|
| `run_ffmpeg_progress()` | `src/utils/progress.py:38` | 進捗管理（`claude.md` 指定。**必ず経由する**） |
| `ffmpeg_runner.execute()` | `src/modules/ffmpeg_runner.py:205` | 上記のラッパ＋失敗時 `FFmpegError` |
| `extract_segment()` | `src/modules/silence_cutter.py:175` | 入力シークによる区間抽出（CFR 固定・fade 対応） |
| `_concat_demux()` | `src/modules/silence_cutter.py:338` | 区間ファイル群の連結（`-fflags +genpts` 等の既知対策込み） |
| `build_subtitle_file()` | `src/modules/subtitle_generator.py:523` | 役割別 Style 付き ASS 生成 |
| `burn_subtitle()` | `src/modules/subtitle_generator.py:600` | ASS 焼き込み（A/V 同期・CFR 化の既知対策込み） |
| `build_font_profile()` | `src/modules/subtitle_generator.py:423` | 設定 → `FontProfile` |
| `resolve_output_profile()` | `src/modules/output_profile.py:48` | 縦/横判定とキャンバス寸法 |
| `concat_processor.run()` | `src/modules/concat_processor.py:80` | OP/ED 結合 |
| `output_writer.run()` | `src/modules/output_writer.py:47` | 最終配置・連番衝突回避 |

これらは **Timeline レンダラからそのまま再利用する**（R14「既存コードを可能な限り流用」）。
特に `extract_segment` / `_concat_demux` / `burn_subtitle` には
VFR・PTS 破損・A/V 同期の実障害対策がコメント付きで積み上がっており（`docs/error/20260630`,
`docs/error/20260719`）、**新規に書き直すと同じ不具合を踏み直す。**

### 2.6 利用可能なライブラリ（実測）

| ライブラリ | 版 | 確認方法 | 備考 |
|---|---|---|---|
| PySide6 | 6.11.0 | `import PySide6` | Qt Widgets / QtCharts / QtMultimedia 同梱済み（`src/main_window.spec`） |
| PyAV (`av`) | 17.0.0 | `import av` | faster-whisper の依存として**既に導入済み・配布物にも同梱済み**（`_internal/av`） |
| BudouX | 導入済 | `subtitle.wrap_engine` | 改行 |

**PyAV が既に入っている点が本設計の鍵**（§3-1）。新規ライブラリ追加は不要
（`claude.md`「不要なライブラリを追加しない」を満たす）。

---

## 3. 方式選定

### 3-1. プレビューのフレーム取得方式（R6 の中核）

| 方式 | 追従速度 | 忠実性 | 追加依存 | 評価 |
|---|---|---|---|---|
| A. 現行踏襲（ffmpeg で区間 mp4 生成→再生） | ×（秒オーダー） | ◎ | なし | スクラブ不可。R6 を満たせない |
| B. ffmpeg で 1 フレーム PNG 抽出 | △（100〜400ms／プロセス起動） | ◎ | なし | デバウンスすれば実用域だがドラッグ追従は苦しい |
| C. **PyAV で in-process デコード＋Qt で合成** | ◎（数〜数十 ms） | △（字幕描画が libass と別実装） | **なし（導入済）** | **採用** |

**採用: C（フォールバックとして B を併設）**

* ベース映像フレームは PyAV でシーク＋デコードして `QImage` 化する。プロセス起動が無いため
  再生ヘッドのドラッグに追従できる。
* オーバーレイ（画像・字幕）は **Qt 側の `QGraphicsItem` として合成** する。
  これにより R7（クリック選択・ドラッグ移動）と R8（右クリックメニュー・レイヤー変更）が
  Qt の標準機能で素直に実装でき、**プレビューが編集 UI そのものになる**。
* PyAV が使えない環境（import 失敗・コーデック非対応）は方式 B（ffmpeg 単フレーム抽出）へ
  自動フォールバックする。`SubtitlePreviewWidget` が QtMultimedia 不在時に静止画へ落ちるのと
  同じ防御方針（`subtitle_preview_widget.py:25`）。
* **忠実性の差は仕様として明記する**（§6.4-4）。Qt の文字描画は libass と完全一致しないため、
  最終確認用に「この 1 フレームを本番と同じ ASS で描画し直す」高精度プレビューを
  ボタンで用意する（既存 `build_subtitle_file` を再利用）。

### 3-2. 音声認識の実行対象（R3 の副作用）

無音カットが実カットでなくなると、**現行が音声認識に渡していた「無音カット後の動画」が存在しなくなる。**

| 方式 | 認識コスト | タイムスタンプ | 評価 |
|---|---|---|---|
| A. 元動画（全長）を認識 | **増大**（無音区間ぶん長い） | ソース時間で直接得られる | 処理時間が悪化する |
| B. 実カット動画を従来どおり作って認識 | 現状比 同等 | 要マッピング | 重い映像再エンコードが残り R3 の意義が薄れる |
| C. **残す区間の「音声のみ」を連結した一時ファイルを作って認識** | **現状より軽い**（映像エンコード無し） | Timeline 時間で直接得られる | **採用** |

**採用: C**

* `-vn -map 0:a` で音声のみを区間抽出・連結するため、現行の「映像込み再エンコード」より**軽い**。
* 得られるタイムスタンプは「無音を詰めた後の時間軸」なので、**字幕クリップの初期配置に
  区間ごとの写像計算が要らない**。OP を配置する場合だけ、その尺ぶんの一定オフセットを
  一律で足せばよい（§6.9-2）。
* 併せて `source_start` / `source_end`（元動画時間）も `TimeMap` で逆算して保持し、
  将来の再同期・Resolve 出力に使う。

### 3-3. 時刻の保持単位

resolve21 §5.3 で「整数フレーム管理による丸め誤差の非累積化」を採った経緯がある。
一方、既存資産（`keep_segments` / whisper items / ASS / `probe_duration`）はすべて秒基準である。

**採用: JSON・モデルともに秒（float）で保持し、フレーム量子化はレンダリング直前にのみ行う。**
量子化は各時刻の**絶対値に対して独立に** `round(sec * fps)` を適用し、
差分を足し込まない（＝誤差を累積させない）。これで resolve21 の教訓を満たしつつ、
既存資産との変換層を増やさない。

### 3-4. OP / ED の Timeline 化方式（R15）

回答 Q1 により、OP/ED は「後段で結合する固定要素」から「Timeline 上の編集対象」へ変わる。

| 方式 | 編集性 | 実装量 | 評価 |
|---|---|---|---|
| A. 従来どおり `concat_processor` で後段結合 | ×（編集不可） | 0 | R15 を満たせない |
| B. OP/ED 専用トラックを新設する | ○ | 中（トラック種別が増え、レンダラの合流が複雑） | 本編と同じ「並べて連結する」性質なのに別トラックにするのは不自然 |
| C. **V1 の先頭・末尾に通常のクリップとして置く** | ◎（移動・トリム・分割・削除がそのまま効く） | 小 | **採用** |

**採用: C**

* OP/ED は本編と同じ「時間軸上に並ぶ映像」であり、V1 に置けば §6.6 の編集操作が**追加実装なしで**すべて効く。
* `origin.type` を `"opening"` / `"ending"` にしておけば、UI 上で色分け・ラベル表示ができ、
  レンダラも通常クリップと同じ経路で処理できる。
* 素材の解像度・フレームレートが本編と異なりうる点だけは対処が要る。
  現行 `concat_processor.concat()`（`src/modules/concat_processor.py:25`）が
  `scale → pad → setsar → fps → format` で正規化しているのと**同一のチェーン**を
  レンダラのクリップ抽出時に適用する（§8.2）。
* `concat_processor` モジュール自体は従来モード（`silence_cut.mode="physical"`）用に**無改変で存置**する。

### 3-5. プレビュー音声の再生方式（R16）

回答 Q4 により、編集中に音を聞けることが要件になった。
PyAV でデコードした映像フレームと音声をどう同期させるかが論点。

| 方式 | 同期精度 | 応答性 | 実装量 | 評価 |
|---|---|---|---|---|
| A. PyAV で音声もデコードし自前でバッファ再生 | ◎ | ◎ | 大（音声デバイス制御・リサンプル・バッファ管理を自作） | 過剰 |
| B. 全長の Timeline 音声を先に 1 本生成し `QMediaPlayer` で再生 | ◎ | △（編集のたびに全長を作り直す） | 小 | 長尺で作り直しが重い |
| C. **再生ヘッド位置から一定秒ぶんの音声をオンデマンド生成し `QMediaPlayer` で再生** | ◎ | ○（初回のみ生成待ち） | 小〜中 | **採用** |

**採用: C**

* `QMediaPlayer` を**マスタークロック**にする。`positionChanged` を受けて再生ヘッドを進め、
  その時刻のフレームを PyAV から取得して表示する。フレーム取得が間に合わない場合は
  **音声を優先してフレームを間引く**（映像がカクついても音は途切れない）。
* 音声チャンクは `timeline.preview.audio_chunk_sec`（既定 30 秒）ぶんを ffmpeg で生成する。
  再生が残り `audio_prefetch_sec` を切ったら次チャンクを先読み生成し、途切れずにつなぐ。
* **初回チャンクの生成を省ける経路がある。** §7.3 で音声認識用に作る
  「残す区間の音声のみを連結した一時ファイル」は、編集前の Timeline 音声そのものである。
  画面を開いた直後（未編集）はこれをそのまま再生ソースに使えるため、**待ち時間ゼロで再生できる**。
  編集が入った時点で無効化し、以降はオンデマンド生成に切り替える。
* QtMultimedia が使えない環境では再生ボタンを無効化し、フレーム送り（無音）のみとする
  （`SubtitlePreviewWidget` の防御方針 `src/gui/subtitle_preview_widget.py:25` と同じ）。

---

## 4. 設計方針（全体）

1. **Timeline を編集情報の唯一のソースにする（R14）。**
   無音カット結果・字幕・追加メディア・レイヤー順のすべてを `Timeline` に集約し、
   動画生成は `Timeline` だけを入力に取る。パイプラインの他工程は Timeline を読まない。
2. **UI と編集処理を完全に分離する（R14）。**
   `src/timeline/`（Qt 非依存の純 Python：モデル・JSON・編集操作・レンダラ）と
   `src/gui/timeline/`（PySide6 の表示・入力）に分ける。
   モデルは Qt シグナルを持たず、変更通知は GUI 側の `TimelineController` が担う。
   これにより CLI／テストからも Timeline を生成・レンダリングできる。
3. **既存の重い処理は書き直さず呼び出す（R14）。**
   区間抽出・連結・ASS 生成・焼き込み・出力配置は §2.5 の既存関数を使う。
   進捗は必ず `run_ffmpeg_progress` を経由する（`claude.md` 指定）。
   OP/ED だけは Timeline クリップ化に伴い `concat_processor` を経由しなくなるが、
   同モジュールが持つ**解像度・SAR・fps の正規化チェーンは同じ内容をレンダラへ移植**し、
   モジュール自体は従来モード用に無改変で残す（§3-4）。
4. **追加のみ・既存キー不変。**
   `setting.json` は `timeline` 節の新規追加と `silence_cut.mode` の 1 キー追加に留める。
   既存キーの意味は変えない（`claude.md`「既存設定との互換性を失わない」）。
5. **旧画面は残して切り替える（R2）。**
   `SubtitleEditorDialog` / `SubtitlePreviewWidget` は無改変で残す。
   `timeline.enabled=false` で従来フロー（実カット＋旧字幕編集画面）へ完全に戻せる。
   アーカイブ切り抜き経路（`ArchiveResultWindow`）は**本要望の対象外**で無改変とする。
6. **ハードコード禁止。** ズーム段階・スナップ量・既定画像尺・プレビュー解像度・
   音声チャンク長など調整余地のある値はすべて `setting.json` から読む。
7. **OP/ED も Timeline 上の素材として扱う（R15）。**
   `general.opening_video` / `ending_video` と各 `*_enabled` フラグは
   「Timeline を組むときの初期配置の指示」という位置づけに変わる。配置後はユーザーが
   移動・トリム・削除でき、削除すれば出力にも含まれない。設定値は書き換えない
   （`claude.md`「推測で setting.json を変更しない」）。
8. **プレビューは編集画面の一部であり、再生機でもある（R16）。**
   フレーム表示・オーバーレイ編集・音声再生を 1 つの `PreviewPanel` に集約し、
   再生中は編集操作を受け付けない（再生停止後に編集可能）ことで状態の競合を避ける。
9. **音声は映像から派生させ、ズレを構造的に起こせなくする（R18）。**
   「V1 を編集したら A1 にも同じ編集を伝播させる」のではなく、
   **A1 クリップに固有の時刻を持たせない**（`link_clip` の参照だけを持つ）。
   時刻は常にリンク先 V1 クリップから導出するため、伝播漏れによる音ズレが原理的に起こらない。
   詳細は §6.3.4。

---

## 5. 画面構成

### 5.1 画面遷移

```
メイン画面（クリップ用タブ）── ▶ 実行
        │
        ├ 音声解析・正規化
        ├ 音量解析 → 閾値確認ダイアログ（従来どおり）
        ├ 無音検出（編集点のみ・実カットしない）
        ├ 音声認識（残す区間の音声のみ）
        ├ Timeline 構築（OP → 本編 → ED を V1 へ配置）→ プロジェクト JSON 保存
        │
        ├─▶ ★ TimelineEditorDialog（新規・モーダル）
        │      ├ 上部: プレビュー（フレーム＋音声再生＋オーバーレイ編集）
        │      └ 下部: Timeline（V／A／S トラック。OP/ED も編集可）
        │      「決定」→ レンダリングへ / 「キャンセル」→ PipelineCancelled
        │
        ├ Timeline レンダリング（OP/ED を含む）
        └ 出力（従来どおり）
```

OP/ED の結合工程が独立した工程でなくなり、Timeline レンダリングに吸収される（R15 / §3-4）。

`timeline.enabled=false` のときは ★ の位置に従来の `SubtitleEditorDialog` が出る
（無音カットも従来の実カットに戻る）。

### 5.2 `TimelineEditorDialog` のレイアウト

```
┌──────────────────────────────────────────────────────────────┐
│  ┌──────────────────────────────────────────────────┐  ┌──────────┐ │
│  │                                                  │  │ インスペクタ │ │  ← QSplitter(Horizontal)
│  │              プレビュー                            │  │  （任意表示）│ │
│  │   ・再生ヘッド位置のフレーム                          │  │  選択要素の  │ │
│  │   ・画像／字幕オーバーレイ（選択・ドラッグ可）           │  │  文字/色/    │ │
│  │   ・右クリック → 削除 / レイヤー                      │  │  位置/尺     │ │
│  │                                                  │  └──────────┘ │
│  └──────────────────────────────────────────────────┘             │
│  [▶] [⏮] [⏭]  00:01:23.45 / 00:12:34.56  [🔊]━━━━  [高精度プレビュー] │
├──────────────────────────────────────────────────────────────┤  ← QSplitter(Vertical)
│  ｜0:00      0:30      1:00      1:30      2:00   ← タイムルーラ      │
│  ┌────┬──────────────────────────────────────────────────┐ │
│  │ S1 │      ▭字幕 ▭字幕  ▭字幕     ▭字幕                   │ │
│  │ V2 │           ▭画像A                    ▭動画B          │ │
│  │ V1 │ ▨OP ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬ ▨ED  │ │
│  │ A1 │ ▨   ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬ ▨    │ │
│  └────┴──────────────────────────────────────────────────┘ │
│   ヘッダ           ▲再生ヘッド                                     │
│  [－][＋] ズーム   [◀ ────────── ▶] 横スクロール                    │
├──────────────────────────────────────────────────────────────┤
│                                      [DaVinci Resolve 出力] [決定] [キャンセル] │
└──────────────────────────────────────────────────────────────┘
```

* 上下比は `QSplitter(Qt.Vertical)` でユーザー可変。既定比は `timeline.ui.split_ratio`。
* `▨OP` / `▨ED` は設定の OP/ED 素材から自動配置されたクリップ（R15）。
  他のクリップと同じく移動・トリム・削除でき、色とラベルで区別できるようにする。
* トランスポート列にはミュートボタンと音量スライダーを置く（R16 / §6.4-5）。
  QtMultimedia が使えない環境では再生ボタンごと無効化する。
* 「DaVinci Resolve 出力」は既存ボタン（`subtitle_editor_dialog.py:79`
  `RESOLVE_EXPORT_BUTTON_TEXT` / `run_resolve_export()` `:85`）をそのまま流用する。
  出力材料は Timeline から組み直す（§8.5）。
* インスペクタ欄は Phase 6 で追加する任意ペイン（§14）。それ以前は非表示。

---

## 6. 詳細設計

### 6.1 クラス構成

#### 6.1.1 新規ファイル一覧

```
src/timeline/                          ← Qt 非依存（純 Python）
    __init__.py
    model.py            Timeline / Track / Clip / SubtitleClip / MediaRef / Transform
    timemap.py          TimeMap（ソース時間 ↔ Timeline 時間の写像）
    project_io.py       プロジェクト JSON の読み書き・検証・マイグレーション
    builder.py          編集点＋字幕 items → Timeline 構築
    commands.py         編集操作（split / trim / move / delete / reorder）＋ Undo/Redo
    renderer.py         Timeline → FFmpeg → 動画生成
    frame_source.py     フレーム取得（PyAV／ffmpeg フォールバック）
    audio_source.py     プレビュー用の音声チャンク生成（R16 / §6.4-5）
    media_probe.py      D&D されたメディアの種別・尺・寸法の判定

src/gui/timeline/                      ← PySide6（表示・入力のみ）
    __init__.py
    timeline_editor_dialog.py   TimelineEditorDialog（画面全体・決定/キャンセル）
    timeline_controller.py      TimelineController（QObject：モデル ↔ UI・シグナル・Undo）
    timeline_view.py            TimelineView（QGraphicsView：トラック・ルーラ・再生ヘッド）
    timeline_items.py           ClipItem / TrimHandleItem / PlayheadItem / RulerItem
    track_header_widget.py      TrackHeaderWidget（V1/A1/S1 のヘッダ列）
    preview_panel.py            PreviewPanel（フレーム表示＋トランスポート）
    preview_items.py            ImageOverlayItem / SubtitleOverlayItem（選択・ドラッグ）
```

#### 6.1.2 変更ファイル一覧（すべて追加的変更）

| ファイル | 変更内容 |
|---|---|
| `src/modules/silence_cutter.py` | `detect_edit_points(context)` を**追加**。既存 `run()` と実カット関数群は無改変で温存 |
| `src/pipeline/pipeline_runner.py` | `timeline.enabled` による工程分岐を追加。Timeline モードの工程は「音声解析・正規化／無音検出／音声認識／Timeline レンダリング」の 4 つ（OP/ED 結合はレンダリングに吸収されるため独立工程でなくなる）。従来モードは `total_steps=5` のまま不変 |
| `src/modules/concat_processor.py` | **無改変**。Timeline モードでは呼ばれない。正規化チェーン（`:47`〜`:57`）は同内容をレンダラへ移植する |
| `src/pipeline/pipeline_context.py` | `timeline` / `project_path` の保持と `cleanup()` での破棄を追加 |
| `src/modules/subtitle_generator.py` | 音声のみ認識用 `extract_audio_segments()` と Timeline レビュー分岐を追加。`build_subtitle_file()` に `\pos` 対応を追加（既存呼び出しは挙動不変） |
| `src/gui/main_window.py` | `TimelineReviewBridge` を追加（既存 `SubtitleReviewBridge` は無改変で併存） |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` に `timeline` 節と `silence_cut.mode` を追加 |
| `src/export/resolve_export.py` | Timeline から spec を作る `build_timeline_spec()` を追加（既存 2 経路は無改変） |
| `src/main_window.spec` | `hiddenimports` に `av` を追加（遅延 import のため明示） |

#### 6.1.3 クラス責務

| クラス | 責務 | Qt 依存 |
|---|---|---|
| `Timeline` | fps・キャンバス寸法・メディアプール・トラック列を保持。**編集情報の唯一のソース** | なし |
| `Track` | 種別（video/audio/subtitle）・index・クリップ列・有効/ロック | なし |
| `Clip` | メディア参照・Timeline 上の開始/尺・ソース内 in/out・`z_order`・`Transform` | なし |
| `SubtitleClip` | `Clip` の派生。text / role / font / font_size / use / 位置上書き | なし |
| `MediaRef` | メディア 1 件の実体（path・種別・尺・寸法・音声有無） | なし |
| `Transform` | x / y / scale / rotation / opacity（プレビューと `overlay` フィルタが共有） | なし |
| `TimeMap` | V1 クリップ列から作る双方向写像。`to_timeline()` / `to_source()` / `clip_at()` | なし |
| `TimelineProject` | JSON との相互変換・スキーマ検証・版差マイグレーション | なし |
| `TimelineBuilder` | 編集点 JSON ＋ 字幕 items → `Timeline` を組み立てる | なし |
| `TimelineCommand` 系 | 1 編集操作＝1 コマンド（`do()` / `undo()`）。`CommandStack` が履歴保持 | なし |
| `TimelineRenderer` | `Timeline` → FFmpeg 実行計画 → 動画生成。進捗は `run_ffmpeg_progress` 経由 | なし |
| `FrameSource` | 指定時刻のフレームを RGB バイト列で返す（PyAV / ffmpeg の 2 実装） | なし |
| `AudioChunkSource` | 指定 Timeline 区間の音声を 1 ファイルへ書き出す。編集で無効化されるキャッシュを持つ | なし |
| `TimelineController` | モデル操作の唯一の窓口。`CommandStack` を持ち、変更を Qt シグナルで放送 | あり |
| `TimelineEditorDialog` | 画面組み立て・決定/キャンセル・Resolve 出力ボタン | あり |
| `TimelineView` | Timeline の描画・ズーム・スクロール・再生ヘッド・D&D 受理 | あり |
| `PreviewPanel` | フレーム表示・音声再生・オーバーレイ合成・選択/ドラッグ・右クリックメニュー | あり |

`TimelineController` のシグナル（UI 更新の起点）:

```python
timeline_changed  = Signal()          # 構造変化（クリップ増減・トラック増減）
clip_changed      = Signal(str)       # clip_id 単位の更新（移動・トリム・レイヤー）
selection_changed = Signal(object)    # 選択要素（Clip / SubtitleClip / None）
playhead_moved    = Signal(float)     # 再生ヘッド秒
view_changed      = Signal()          # ズーム・スクロール
```

### 6.2 編集点 JSON の仕様

#### 6.2.1 位置づけと保存先

* 1 実行につき 1 ファイル。**編集点（無音カット結果）と Timeline 全体を同一ファイルに持つ。**
  「編集点だけの JSON」を分けない理由は、Timeline を唯一のソースとする方針（§4-1）に対し
  真実の在り処が 2 つになるのを避けるため。編集点は `edit_points` セクションとして
  **検出時の生データのまま**保持し、`tracks` はそれを起点にユーザー編集を反映した現在状態を持つ。
* 保存先: `timeline.project_dir`（空なら `general.output_directory`）配下の
  `<入力動画のstem><timeline.project_suffix>`。既定 `sample.timeline.json`。
* 保存タイミング: ① Timeline 構築直後（編集前の初期状態）、② 「決定」押下時（最終状態）。
* **既存ファイルは読み戻さない**（回答 Q6）。実行のたびに無音検出からやり直して Timeline を
  組み直し、同名ファイルは上書きする。前回の編集途中の状態が意図せず復活する事故を避けるため。
* したがって現時点の JSON は「実行内容の記録」と「外部連携（Resolve 出力・不具合解析）」のための
  出力であり、アプリが読み込む経路は持たない。読み込み側（`project_io.load()`）は
  Phase 0 で実装し単体テストで検証しておくが、実運用に入るのは**将来のプロジェクト再編集機能**
  （§13）が入ってからになる。
* 自動保存は読み戻し先が無いため**既定で無効**（`timeline.autosave_sec: 0`）とする。
  プロジェクト再編集機能の導入時に既定を見直す。

#### 6.2.2 スキーマ（`schema_version: 1`）

```jsonc
{
  "schema_version": 1,
  "generator": "Stretheus 1.4.0",
  "created_at": "2026-08-07T12:34:56",
  "updated_at": "2026-08-07T12:41:02",

  // ── タイムラインの諸元（出力プロファイルから解決 / output_profile.py:48）
  "timeline": {
    "fps": 60,
    "width": 1920,
    "height": 1080,
    "orientation": "landscape",     // "landscape" | "portrait"
    "duration_sec": 3421.883,       // 全トラックの最終終端（導出値・読み込み時に再計算）
    "playhead_sec": 0.0,
    "zoom_px_per_sec": 40
  },

  // ── 素材（元動画）
  "source": {
    "input_path": "D:/develop/autoedit/input/sample.mp4",   // ユーザーが選んだ元動画
    "media_path": "C:/Users/.../autoedit_xxxx/loudness.mp4", // 正規化後（映像は copy＝時間軸同一）
    "duration_sec": 3612.345,
    "width": 1920, "height": 1080, "fps": 60
  },

  // ── 編集点（無音カットの検出結果そのもの・元動画時間・R3/R4 の中核）
  "edit_points": {
    "detector": "silencedetect",
    "threshold_db": -23,            // volume_analysis.last_cut_db の確定値
    "min_silence_sec": 0.6,         // volume_analysis.cut_min_silence_sec
    "detected_at": "2026-08-07T12:34:50",
    "source_duration_sec": 3612.345,
    "keep_segments": [              // 残す（＝有音）区間。元動画時間・昇順・非重複
      [0.000, 12.480],
      [15.220, 48.900]
    ],
    "cut_segments": [               // 取り除く（＝無音）区間。keep の補集合（導出値・可読性のため併記）
      [12.480, 15.220]
    ]
  },

  // ── メディアプール（元動画＋ OP/ED 素材＋ D&D で追加した素材）
  "media_pool": [
    { "id": "m1", "kind": "video", "path": "C:/.../loudness.mp4",
      "duration_sec": 3612.345, "width": 1920, "height": 1080, "fps": 60, "has_audio": true },
    { "id": "m2", "kind": "image", "path": "D:/assets/logo.png",
      "duration_sec": null, "width": 800, "height": 600, "has_audio": false },
    { "id": "m3", "kind": "video", "path": "D:/StreamPipeline/dev/assets/op.mp4",
      "duration_sec": 4.000, "width": 1920, "height": 1080, "fps": 30, "has_audio": true },
    { "id": "m4", "kind": "video", "path": "D:/StreamPipeline/dev/assets/ed.mp4",
      "duration_sec": 6.500, "width": 1280, "height": 720, "fps": 30, "has_audio": true }
  ],

  // ── トラック（描画は index 昇順＝ V1 が最背面。実際の前後は z_order が決める）
  "tracks": [
    {
      "id": "V1", "kind": "video", "index": 1, "name": "Video 1",
      "enabled": true, "locked": false, "is_base": true,
      "clips": [
        // オープニング（general.opening_video から自動配置 / R15）
        { "id": "c0", "media_id": "m3",
          "timeline_start": 0.000, "duration": 4.000,
          "source_in": 0.000, "source_out": 4.000,
          "z_order": 0, "enabled": true,
          "transform": { "x": 0.0, "y": 0.0, "scale": 1.0, "rotation": 0.0, "opacity": 1.0 },
          "origin": { "type": "opening" } },
        // 本編（編集点 keep_segments[0] 由来）
        { "id": "c1", "media_id": "m1",
          "timeline_start": 4.000, "duration": 12.480,
          "source_in": 0.000, "source_out": 12.480,
          "z_order": 0, "enabled": true,
          "transform": { "x": 0.0, "y": 0.0, "scale": 1.0, "rotation": 0.0, "opacity": 1.0 },
          "origin": { "type": "silence_cut", "segment_index": 0 } },
        { "id": "c2", "media_id": "m1",
          "timeline_start": 16.480, "duration": 33.680,
          "source_in": 15.220, "source_out": 48.900,
          "z_order": 0, "enabled": true,
          "transform": { "x": 0.0, "y": 0.0, "scale": 1.0, "rotation": 0.0, "opacity": 1.0 },
          "origin": { "type": "silence_cut", "segment_index": 1 } },
        // エンディング（general.ending_video から自動配置 / R15）
        { "id": "c9", "media_id": "m4",
          "timeline_start": 50.160, "duration": 6.500,
          "source_in": 0.000, "source_out": 6.500,
          "z_order": 0, "enabled": true,
          "transform": { "x": 0.0, "y": 0.0, "scale": 1.0, "rotation": 0.0, "opacity": 1.0 },
          "origin": { "type": "ending" } }
      ]
    },
    {
      "id": "V2", "kind": "video", "index": 2, "name": "Video 2",
      "enabled": true, "locked": false, "is_base": false,
      "clips": [
        { "id": "c3", "media_id": "m2",
          "timeline_start": 5.000, "duration": 5.000,
          "source_in": 0.0, "source_out": 5.000,
          "z_order": 10, "enabled": true,
          "transform": { "x": 0.30, "y": -0.25, "scale": 0.4, "rotation": 0.0, "opacity": 1.0 },
          "origin": { "type": "user_media" } }
      ]
    },
    {
      "id": "A1", "kind": "audio", "index": 1, "name": "Audio 1",
      "enabled": true, "locked": false, "link_track": "V1",
      // A1 クリップは時刻を持たない。開始・尺・ソース位置はすべて link_clip 先の
      // V1 クリップから導出する（R18 / §6.3.4）。これにより音ズレが構造的に起こらない。
      // has_audio=false の V1 クリップ（例 c0 が無音素材の場合）には対応クリップを作らない。
      "clips": [
        { "id": "a0", "link_clip": "c0", "gain_db": 0.0, "muted": false },
        { "id": "a1", "link_clip": "c1", "gain_db": 0.0, "muted": false },
        { "id": "a2", "link_clip": "c2", "gain_db": 0.0, "muted": false },
        { "id": "a9", "link_clip": "c9", "gain_db": 0.0, "muted": false }
      ]
    },
    {
      "id": "S1", "kind": "subtitle", "index": 1, "name": "Subtitle 1",
      "enabled": true, "locked": false,
      "clips": [
        { "id": "s1",
          // Timeline 時間（OP の 4.0 秒ぶん後ろにずれている点に注意）
          "timeline_start": 4.350, "duration": 2.100,
          "text": "今日はこの話をします", "role": "streamer",
          "font": "", "font_size": null, "use": true,
          "z_order": 100,
          "transform": { "x": null, "y": null },   // null = 設定の alignment/margin に従う
          // origin は元動画時間（由来の記録。編集には使わない）
          "origin": { "type": "asr", "source_start": 0.350, "source_end": 2.450 } }
      ]
    }
  ]
}
```

#### 6.2.3 フィールド規約

| 規約 | 内容 |
|---|---|
| 時刻の単位 | すべて秒（float）。小数第 3 位まで（ミリ秒精度）で丸めて書き出す |
| 時刻の基準 | `edit_points.*` と `clips[].source_in/out` は**元動画時間**。`timeline_start` と字幕の時刻は**Timeline 時間** |
| 音声クリップ | `kind="audio"` のトラックのクリップは **時刻フィールドを持たない**（`link_clip` / `gain_db` / `muted` のみ）。時刻はリンク先から導出する（R18 / §6.3.4）。JSON に時刻が書かれていても読み込み時に無視する |
| `duration` | `source_out - source_in` と一致させる（速度変更は未対応。将来拡張で `speed` を追加） |
| `z_order` | 大きいほど前面。V1 のベースクリップは 0 固定。オーバーレイと字幕は 1 以上 |
| `transform.x/y` | キャンバス中心を原点とする**正規化座標**（-1.0〜1.0）。字幕の `null` は「設定の配置に従う」 |
| ID | `media_id` は `m<n>`、クリップは `c<n>` / `a<n>` / `s<n>`。プロジェクト内で一意 |
| `origin.type` | `"silence_cut"`（編集点由来）/ `"opening"` / `"ending"` / `"asr"`（音声認識由来）/ `"user_media"`（D&D 由来）。**由来の記録専用**で、編集やレンダリングの分岐には使わない（UI の色分け・ラベルにのみ使う） |
| 導出値 | `cut_segments` / `timeline.duration_sec` は読み込み時に再計算し、不一致なら再計算値を採る |
| 未知キー | 読み込み時に無視して保持しない（前方互換より単純さを優先。`schema_version` で判別する） |

#### 6.2.4 検証とマイグレーション

`project_io.load()` は次を検証し、違反は `TimelineError`（新規例外・`AutoEditError` 派生）にする。

* `schema_version` が既知か（未知＝より新しい版なら「このバージョンでは開けません」と明示して中止）
* `media_pool[].path` の実在（欠落メディアは該当クリップを無効化して WARNING を出し、処理は継続）
* `keep_segments` が昇順・非重複・`source_duration_sec` 内
* 各クリップの `duration > timeline.min_clip_sec`、`source_in < source_out`
* 同一トラック内でクリップが時間重複していないこと（重複時は後勝ちで詰めて WARNING）
* 音声クリップの `link_clip` が実在する映像クリップを指していること
  （欠落リンクは当該音声クリップを捨てて WARNING。映像側は無音として扱われる）

将来の版差は `_MIGRATIONS = {1: _migrate_1_to_2, ...}` の連鎖適用で吸収する
（`settings_window._normalize_legacy_values` と同じ「自己修復」方針）。

### 6.3 Timeline の管理方法

#### 6.3.1 トラック構成（R9）

| トラック | 内容 | 編集可否 |
|---|---|---|
| `V1`（ベース） | **OP → 元動画の残す区間 → ED** を時系列に並べたもの。本編は編集点 JSON の `keep_segments` から、OP/ED は `general` 設定から生成（R15） | 尺変更・移動・分割・削除 |
| `V2` 以降 | D&D で追加した画像・動画のオーバーレイ | 同上＋位置/拡大（プレビューから） |
| `A1` | `V1` にリンクした音声。`link_clip` で V1 クリップと 1:1 対応（R18） | ミュート・ゲインのみ。**尺・位置は V1 から導出**され、単独では動かせない（§6.3.4） |
| `S1` | 字幕クリップ | 尺変更・移動・分割・削除・テキスト編集・位置 |

* **OP/ED も V1 上の通常クリップ**であり、専用トラックも専用の編集規則も持たない（§3-4）。
  UI では `origin.type` を見て色とラベルで区別するだけにする。
* `A1` は **V1 リンク固定**（回答 Q3 / R18）。Video 側の編集はすべて Audio 側にも及ぶ。
  音声だけをずらす編集は A/V 同期の実障害（`docs/error/20260719`）を再発させるため、
  そもそも**できない構造**にする（§6.3.4）。BGM など独立音声は将来拡張（§13）。
* D&D した**動画**素材は「新規 `Vn` ＋ 対応する `An`」を対で作る（音声を持つ場合）。
* **字幕は V1 の編集に追従しない**（回答 Q2）。V1 のクリップをトリム・削除しても
  `S1` のクリップは動かない。由来の元動画時刻は `origin.source_start/source_end` に
  残してあるため、将来「字幕を映像に再同期する」機能を足せる（§13）。

#### 6.3.2 `TimeMap`（ソース時間 ↔ Timeline 時間）

V1 のクリップ列から都度構築する軽量オブジェクト。プレビューのフレーム取得と、
音声認識結果の逆写像に使う。

```python
class TimeMap:
    # V1 のクリップ列から (timeline_start, duration, source_in) の表を作る
    def __init__(self, base_clips): ...

    # Timeline 時間 → (media_id, ソース時間)。空白（クリップ無し）は None
    def to_source(self, timeline_sec): ...

    # ソース時間 → Timeline 時間。カット済み区間内なら None
    def to_timeline(self, source_sec): ...

    # ソース区間 [s,e] を Timeline 上の区間列へ分割する（カットを跨ぐ場合は複数返る）
    def split_interval(self, source_start, source_end): ...

    # Timeline 時間を含む V1 クリップを返す
    def clip_at(self, timeline_sec): ...
```

`split_interval()` がカットを跨ぐ区間を複数へ分割するのは、詰めた結果 Timeline 上では
**連続する**ため、同一テキストを分割配置しても表示は途切れない（ちらつかない）ことによる。
既存 `gate_timeline_by_speech(mode="split")`（`subtitle_generator.py:708`）と同じ考え方。

#### 6.3.3 編集操作＝コマンド（Undo/Redo）

すべての編集は `TimelineCommand` として実装し、`CommandStack` に積む。
これにより Undo/Redo（Ctrl+Z / Ctrl+Y）が全操作で一様に効き、
UI 側は「コマンドを積む」以外の方法でモデルを触らない（＝分離が崩れない）。

| コマンド | 対応要望 | 内容 |
|---|---|---|
| `MoveClip` | R10 | `timeline_start` 変更（スナップ・衝突解決込み） |
| `TrimClip` | R10 | 左端＝`source_in` と `timeline_start` を同時に、右端＝`source_out` を変更 |
| `SplitClip` | R10 | 再生ヘッド位置でクリップを 2 つに分割（＝編集点の追加） |
| `DeleteClip` | R10 / R8 | クリップ削除。`timeline.ripple_delete` で「空白を残す/詰める」を切替 |
| `AddMediaClip` | R12 | メディアプール登録＋クリップ配置（必要なら新トラック生成） |
| `ChangeZOrder` | R8 | 最前面 / 前面 / 背面 / 最背面 |
| `MoveOverlay` | R7 | `transform.x/y` 変更（プレビューのドラッグ） |
| `EditSubtitleText` | — | 字幕テキスト・role・font の変更 |
| `SetClipEnabled` | — | 使用 ON/OFF（旧画面の「使用」チェックに相当） |
| `SetAudioGain` | R18 | A1 クリップの `gain_db` 変更 |
| `SetAudioMuted` | R18 | A1 クリップの `muted` 切替 |

#### 6.3.4 Video ↔ Audio のリンク（R18 / 回答 Q3）

##### 基本方針: 伝播させるのではなく、派生させる

「V1 を編集したら A1 にも同じ編集を伝播させる」実装は、
**コマンドを 1 つ追加するたびに伝播処理の書き忘れが音ズレとして表面化する**。
本設計では A1 クリップに**固有の時刻を持たせない**（§6.2.2 のスキーマどおり
`link_clip` / `gain_db` / `muted` しか持たない）。

```python
# A1 クリップの時刻は「持っている」のではなく「毎回リンク先から求める」
class AudioClip:
    def timeline_start(self, timeline):
        return timeline.clip_by_id(self.link_clip).timeline_start
    def duration(self, timeline):
        return timeline.clip_by_id(self.link_clip).duration
    def source_in(self, timeline):
        return timeline.clip_by_id(self.link_clip).source_in
```

これにより、**どんな編集コマンドを追加しても音声が映像からずれることはない**。
リンク編集の正しさがコマンドの実装ではなくデータ構造で保証される。

##### コマンド別の作用

| コマンド | V1 クリップへの作用 | A1 への帰結 |
|---|---|---|
| `MoveClip` | `timeline_start` 変更 | 導出値が変わり、同じだけ移動する |
| `TrimClip`（左端） | `source_in` と `timeline_start` を同量変更 | 同じ位置・同じ長さにトリムされる |
| `TrimClip`（右端） | `source_out` 変更 | 同じ長さにトリムされる |
| `SplitClip` | 2 クリップへ分割 | **リンク側も 2 つへ分ける必要がある**（唯一の明示的な追随処理）。コマンド内で新 `AudioClip` を生成し `link_clip` を張り直す |
| `DeleteClip` | クリップ削除 | **リンクしている `AudioClip` も同時に削除する**（もう 1 つの明示的な追随処理） |
| `DeleteClip`（リップル） | 後続を詰める | 後続の A1 クリップも導出値が変わり、自動的に詰まる |
| `SetClipEnabled` | `enabled` 変更 | 導出され、映像とともに出力対象から外れる |
| `ChangeZOrder` / `MoveOverlay` | 描画順・位置 | **作用しない**（音声に対応する概念が無い） |
| `SetAudioGain` / `SetAudioMuted` | — | A1 クリップ固有の値。V1 には影響しない |

明示的な追随が要るのは **`SplitClip` と `DeleteClip` の 2 つだけ**（クリップの個数が変わる操作）。
残りはすべて導出で自動的に整合する。この 2 つには単体テストを置く。

##### UI 上の扱い

* `A1` トラックには、リンク先 V1 クリップと**同じ X 位置・同じ幅**でクリップを描画する。
  波形表示は Phase 1 では行わない（将来拡張）。
* `A1` クリップに対するドラッグ（移動・トリム）は**リンク先 V1 クリップへの操作として転送**する。
  結果として、ユーザーは V1 と A1 のどちらを掴んでも同じ編集ができる。
  「音声だけがずれる」操作は UI 上に存在しない。
* `A1` クリップの右クリックメニューには「ミュート」「音量…」を出す。
  「削除」は V1 の削除と同義になるため、誤操作防止として**メニューには出さない**
  （消したいときは V1 側から削除する）。

##### 音声を持たない映像クリップ

OP/ED 素材や D&D した画像・無音動画は `MediaRef.has_audio=false` になる。
この場合、対応する `AudioClip` を**作らない**（A1 上はギャップになる）。
レンダリング時は §8.2 の規則どおり `anullsrc` で同尺の無音を差し込むため、
連結後のタイムラインで映像と音声の長さが食い違うことはない。

##### 追加した動画素材（V2 以降）

D&D で追加した音声付き動画は「`Vn` ＋ `An`」を対で作る（§6.8）。
`An` は `Vn` に対して A1 と同じリンク規則で振る舞う。
最終的な音声は V1 系（本編）と Vn 系（追加素材）を `amix` で合成する。

### 6.4 プレビュー（R6）

#### 6.4-1 フレーム取得

```python
class FrameSource:                     # src/timeline/frame_source.py
    # 指定メディアの指定ソース時刻のフレームを (width, height, rgb_bytes) で返す
    def frame_at(self, media_ref, source_sec): ...

class PyAvFrameSource(FrameSource):    # 既定
    # container を media_id ごとにキャッシュし、seek(backward) → デコードして最近傍フレームを返す
    # 直近 timeline.preview.cache_frames 件を LRU キャッシュする

class FfmpegFrameSource(FrameSource):  # フォールバック（PyAV 不可時）
    # ffmpeg -ss <sec> -i <path> -frames:v 1 -f rawvideo で 1 フレーム取得
```

* 再生ヘッド移動時は `timeline.preview.update_debounce_ms`（既定 60ms）でデバウンスし、
  デコード中に届いた要求は最新 1 件だけを残す（`SubtitlePreviewWidget._on_worker_done`
  `:414` の「最新ジョブ以外は破棄」と同じ方針）。
* デコードはワーカースレッドで行い、結果を Qt シグナルでメインスレッドへ渡す。

#### 6.4-2 合成

`PreviewPanel` は `QGraphicsScene` を持ち、以下を重ねる。

| 要素 | アイテム | Z 値 |
|---|---|---|
| ベースフレーム | `QGraphicsPixmapItem` | `-1000`（固定・選択不可） |
| 画像/動画オーバーレイ | `ImageOverlayItem` | `clip.z_order` |
| 字幕 | `SubtitleOverlayItem` | `clip.z_order` |

シーン座標はキャンバス実寸（例 1920×1080）で持ち、ビューを `fitInView` で縮小表示する。
これにより `transform.x/y`（正規化座標）とレンダリング側の座標計算が一致する。

#### 6.4-3 リアルタイム更新の経路

```
TimelineView の再生ヘッドドラッグ
   → TimelineController.set_playhead(sec) → playhead_moved シグナル
       ├→ PreviewPanel: TimeMap.to_source(sec) → FrameSource.frame_at() → ベース差し替え
       └→ PreviewPanel: sec を含むオーバーレイ／字幕クリップだけを可視化（それ以外は hide）
```

上記は**停止中のスクラブ**の経路。再生中は音声がマスタークロックになるため経路が逆になる（§6.4-5）。

#### 6.4-4 本番との差異（明記事項）

resolve23 §5.5 と同じく、プレビューと最終出力の差を仕様として明記する。

| 項目 | プレビュー | 本番レンダリング |
|---|---|---|
| 字幕描画 | Qt（`QPainter`）による近似。アウトライン/影/BudouX 改行は再現するが libass と完全一致しない | libass（`ass` フィルタ） |
| 画像合成 | Qt の `QGraphicsItem` | FFmpeg `overlay` フィルタ |
| 色空間 | sRGB のまま | `yuv420p` へ変換 |
| フレーム | 最近傍フレーム（シーク精度） | CFR 量子化後の正確なフレーム |

差が問題になる場面のために「高精度プレビュー」ボタンを置き、
押下時のみ **現在の 1 フレームを本番と同じ経路**（`build_subtitle_file` → ffmpeg `ass` フィルタ）
で生成して表示する。既存 `SubtitlePreviewWidget._request_frame()` (`:389`) と同じ手法を流用する。

#### 6.4-5 音声再生（R16 / 回答 Q4）

##### 音声チャンクの生成

```python
class AudioChunkSource:                 # src/timeline/audio_source.py
    # Timeline 区間 [start, start+length] の音声を 1 ファイルへ書き出して返す。
    # 内部で V1/A1 のクリップ列を走査し、区間抽出（-vn -map 0:a）→ concat で連結する。
    # muted なクリップは anullsrc で無音に、gain_db は volume フィルタで反映する。
    def build(self, timeline, start_sec, length_sec, out_path, on_progress=None): ...

    # 直近に生成したチャンクを返す（範囲が足りていれば再利用）
    def cached(self, start_sec, length_sec): ...

    # Timeline が編集されたらキャッシュを捨てる（TimelineController の timeline_changed に接続）
    def invalidate(self): ...
```

* チャンク長は `timeline.preview.audio_chunk_sec`（既定 30 秒）。
* **初回だけは生成を省略できる。** §7.3 で音声認識用に作る「残す区間の音声を連結した一時ファイル」は
  編集前 Timeline の音声そのものなので、**未編集の間はこれを丸ごと再生ソースに使う**
  （オフセット 0＝Timeline 時間）。画面を開いた直後に待ち時間なく再生できる。
  ただし OP/ED が Timeline に載っている場合は先頭・末尾がずれるため、
  **OP/ED を含む Timeline では初回からオンデマンド生成にする**（判定は「V1 に
  `origin.type` が `opening`/`ending` のクリップが在るか」で行う）。
* 最初の編集コマンドが積まれた時点で `invalidate()` し、以降はオンデマンド生成に切り替える。

##### 再生時の同期

```
[▶] 押下
   → AudioChunkSource から再生ヘッド位置のチャンクを得る（無ければ生成 → 「準備中…」表示）
   → QMediaPlayer.setSource() → play()
        │
        └ positionChanged(ms)  ← これがマスタークロック
              → TimelineController.set_playhead(chunk_start + ms/1000)
                  ├→ TimelineView: 再生ヘッド描画を更新（可視域外なら自動スクロール）
                  └→ PreviewPanel: FrameSource からフレーム取得して表示
                       ※ 前フレームの取得が終わっていなければ**この回は描画を飛ばす**
                         （音声を止めない。上限は timeline.preview.play_fps）
   → 残り再生時間が audio_prefetch_sec を切ったら次チャンクを裏で生成しておく
```

* **音声を止めないことを最優先**にする。PyAV のデコードが間に合わない箇所では映像がカクつくが、
  音は連続する。編集中に「どこで喋っているか」を掴む用途にはこれで足りる。
* 再生中は Timeline とプレビューの**編集操作を無効化**する（§4-8）。
  編集したいときは一時停止してから行う。これで「再生位置が動いている最中にクリップが動く」
  という状態の競合を作らない。
* ミュートボタン・音量スライダーは `QAudioOutput.setMuted()` / `setVolume()` に直結する。
  既定値は `timeline.preview.audio_enabled` / `audio_volume`。

##### 利用不可時の扱い

| 状況 | 挙動 |
|---|---|
| QtMultimedia が import できない | 再生ボタン・音量 UI を無効化。フレーム送り（無音）のみ。INFO を 1 回出す |
| 音声チャンクの生成に失敗 | 「音声を再生できません」を状態表示に出し、無音で再生ヘッドだけを進める（`QTimer` 駆動） |
| 元動画に音声ストリームが無い | 生成を試みず、最初から無音再生モードにする |

### 6.5 プレビュー上の編集（R7・R8）

#### 6.5-1 選択とドラッグ（R7）

* `ImageOverlayItem` / `SubtitleOverlayItem` に `ItemIsSelectable | ItemIsMovable` を付与。
* ドラッグ確定（`mouseReleaseEvent`）で `MoveOverlay` コマンドを積む
  （ドラッグ中は毎フレーム積まない＝Undo 履歴を汚さない）。
* 選択は Timeline 側のクリップ選択と双方向同期する（`selection_changed` シグナル）。
* 字幕の位置上書きは ASS の `\pos(x,y)` として焼く（§8.3）。`transform.x/y` が `null` の間は
  従来どおり Style の `alignment` / `margin_*` に従い、**既存挙動と完全に一致する**。

#### 6.5-2 右クリックメニュー（R8）

`QMenu` を選択要素上に出す。

```
削除                        → DeleteClip（該当クリップを Timeline からも消す）
─────────────
レイヤー ▸ 最前面            → ChangeZOrder(TOP)
         前面               → ChangeZOrder(UP)
         背面               → ChangeZOrder(DOWN)
         最背面             → ChangeZOrder(BOTTOM)
```

`z_order` の再割り当ては「オーバーレイ要素（V2 以降のクリップ＋字幕クリップ）を
`z_order` 昇順に並べ、対象を移動させて 10 刻みで振り直す」方式にする。
10 刻みにするのは、間に挿入する将来操作で全体再計算を避けるため。
V1 のベースクリップは常に最背面（`z_order = 0` 固定）で、この操作の対象外とする。

### 6.6 Timeline のクリップ編集（R10）

`TimelineView` は `QGraphicsView`。X 軸＝時間（`zoom_px_per_sec` でスケール）、
Y 軸＝トラック（`timeline.ui.track_height_px`）。

| 操作 | 入力 | 挙動 |
|---|---|---|
| 尺変更 | クリップ左右端 ±`timeline.ui.trim_handle_px`（既定 6px）内をドラッグ | 左端＝`source_in` と `timeline_start` を同量、右端＝`source_out` を変更。`min_clip_sec` 未満にはできない。素材の範囲（`source_in ≥ 0`, `source_out ≤ media.duration`）を超えられない |
| 開始位置変更 | クリップ本体をドラッグ | `timeline_start` を変更。同一トラック内の他クリップと重なる位置には置けない（吸着で回避） |
| 編集点追加 | 再生ヘッド位置で `Ctrl+B`、またはクリップ右クリック →「分割」 | `SplitClip`。分割後の 2 クリップは `source_in/out` を分けて隙間なく並ぶ |
| 削除（空白を残す） | クリップ選択 → `Delete` / 右クリック →「削除」 | `DeleteClip(ripple=False)`。跡は空白として残る。**これが既定**（回答 Q5） |
| リップル削除（詰める） | `Shift+Delete` / 右クリック →「リップル削除」 | `DeleteClip(ripple=True)`。同一トラックの後続クリップを削除尺ぶん前へ詰める |
| 複数選択 | `Ctrl` クリック / ラバーバンド | 移動・削除をまとめて 1 コマンドで積む |

**削除の 2 種類は常に両方使える**（回答 Q5）。右クリックメニューにも
「削除」と「リップル削除」を並べて出し、キーボードだけ／マウスだけのどちらでも
両方に到達できるようにする。設定 `timeline.ripple_delete` は
**`Delete` キー単独がどちらに割り当たるか**を決めるだけで、既定は `false`（＝空白を残す）。
`Shift` 修飾は常にもう一方を意味する。

リップル削除の適用範囲は**同一トラックのみ**とする（全トラックを串刺しで詰める
「全体リップル」は、字幕が V1 に追従しない方針（Q2）と噛み合わないため採らない）。
`A1` はこの「同一トラック」の例外で、リンク元 `V1` と常に一体で動く（R18）。

OP/ED クリップも通常クリップと同じくこれらの操作の対象になる。
削除すれば出力に含まれず、`setting.json` の `general.opening_enabled` 等は書き換えない（§4-7）。

**スナップ**（`timeline.snap_enabled`）: ドラッグ中、再生ヘッド・他クリップの端・トラック先頭に
`timeline.snap_threshold_px`（既定 8px）以内で吸着する。`Alt` 押下中は無効化。

**A1 の連動（R18）**: 上記の全操作は Audio Track にもそのまま及ぶ。
A1 クリップは時刻を持たずリンク先 V1 クリップから導出するため、
`Move` / `Trim` / リップルの結果は**追随処理なしで**自動的に一致する。
クリップ個数が変わる `Split` / `Delete` のみコマンド内でリンクを張り直す。詳細は §6.3.4。
A1 側のクリップを掴んで操作した場合は、リンク先 V1 クリップへの操作として転送される。

### 6.7 Timeline の操作（R11）

| 操作 | 入力 | 実装 |
|---|---|---|
| 拡大・縮小 | `Ctrl+ホイール` / `[－][＋]` ボタン / `Ctrl +` `Ctrl -` | `zoom_px_per_sec` を `timeline.zoom_min/max_px_per_sec` の範囲で幾何級数的に変更。**マウス位置の時刻を固定点にする**（DaVinci と同じ挙動） |
| 横スクロール | 水平スクロールバー / `Shift+ホイール` / 中ボタンドラッグ | `QGraphicsView` の水平スクロール |
| 再生ヘッド移動 | ルーラのクリック／ドラッグ、`←` `→`（1 フレーム）、`Home` / `End` | `set_playhead()` → プレビュー更新 |
| 再生ヘッドへ追従 | 再生中 | 再生ヘッドが可視域外に出たら自動スクロール |

タイムルーラの目盛り間隔は `zoom_px_per_sec` から「1 目盛りが
`timeline.ui.ruler_min_label_px`（既定 60px）以上になる最小の刻み」を
`[1,2,5,10,15,30,60,120,300,600]` 秒から選んで決める（ハードコードした固定刻みにしない）。

### 6.8 メディアの追加（R12）

* `TimelineView.setAcceptDrops(True)`。`dragEnterEvent` で MIME の URL を検査し、
  `timeline.media.video_extensions` / `image_extensions` に一致するものだけ受理する
  （既存 `ClipTabWidget._dropped_video_path()` `main_window.py:469` と同じ判定方針）。
* ドロップ時の処理:
  1. `media_probe.probe(path)` で種別・尺・寸法・音声有無を判定（`ffmpeg_runner.probe_duration` /
     `probe_dimensions` を流用。画像は `Qt` の `QImageReader` で寸法のみ取得し尺は
     `timeline.media.default_image_duration_sec`）。
  2. `MediaRef` をメディアプールへ追加（同一パスは既存 ID を再利用）。
  3. ドロップ位置の X 座標を時刻へ、Y 座標をトラックへ変換する。
     * 既存トラックの空き領域 → そこへ配置
     * 既存クリップと重なる／ベーストラック（V1）上 → **新しい `Vn` を自動生成**して配置
       （`timeline.media.max_video_tracks` が上限。超過時は追加を拒否し理由をダイアログ表示）
  4. 動画で音声を持つ場合は対応する `An` も生成する。
  5. `AddMediaClip` コマンドとして積む（Undo で追加を取り消せる）。

### 6.9 OP / ED の Timeline 配置（R15）

#### 6.9-1 初期配置の判定

`TimelineBuilder` が Timeline を組む際、本編クリップ列の前後へ OP/ED クリップを置く。
**判定条件は現行 `concat_processor.run()`（`src/modules/concat_processor.py:80`）と完全に同一**にして、
「従来と同じ条件のときに、従来と同じ素材が同じ位置に入る」ことを保証する。

| 条件 | 判定 | 由来 |
|---|---|---|
| 出力プロファイルが縦（`is_portrait`） | **配置しない**（素材・フラグに関わらず） | `concat_processor.py:97`（request14 §8-2） |
| `general.opening_enabled` が false | OP を配置しない | `concat_processor.py:105` |
| `general.ending_enabled` が false | ED を配置しない | 同上 |
| 素材パスが空 / ファイルが存在しない | 配置しない（WARNING を 1 回） | `concat_processor.is_available()` `:15` |

縦動画で配置しないのは既存仕様の踏襲だが、**ユーザーが D&D で手動追加することは妨げない**。
「自動では入れないが、入れたければ入れられる」状態になる（従来は不可能だった）。

#### 6.9-2 配置後の座標

```
timeline_start:  0                op_dur           op_dur + body_dur
                 ├─ OP ──────────┼─ 本編（編集点由来のクリップ列）──┼─ ED ─┤
source_in/out:   OP 素材の 0〜全長      元動画の keep_segments        ED 素材の 0〜全長
```

* 本編クリップの `timeline_start` は OP の尺ぶん後ろへずれる。
* **字幕クリップも同じだけずらす。** 音声認識は「残す区間の音声のみ」に対して行うため
  （§3-2 方式 C）、得られる時刻は OP を含まない本編基準になる。
  `TimelineBuilder` が字幕を載せるときに `+op_duration` のオフセットを一律で加える。
* `TimeMap` は V1 全体から作るが、OP/ED クリップは元動画とは**別メディア**なので
  `to_source()` は `(media_id, source_sec)` の組で返す（§6.3.2 のシグネチャどおり）。
  プレビューはこれをそのまま `FrameSource.frame_at(media_ref, source_sec)` へ渡せる。

#### 6.9-3 素材の規格差

OP/ED 素材は本編と解像度・フレームレート・音声形式が異なりうる（例: 現行 `setting.json` の
既定素材は別プロジェクト由来）。レンダリング時の正規化は §8.2 で扱う。

プレビュー側では PyAV が素材の実寸でフレームを返すため、
`PreviewPanel` がキャンバス寸法へ **アスペクト比維持で縮小＋中央寄せ**して表示する
（レンダラの `scale`＋`pad` と同じ見え方になる）。

---

## 7. 無音カットの変更（R3）

### 7.1 モード切り替え

`silence_cut.mode` を新設する。

| 値 | 挙動 | 呼ばれる関数 |
|---|---|---|
| `"edit_points"`（既定） | 検出のみ。実カットしない | `silence_cutter.detect_edit_points(context)`（新規） |
| `"physical"` | 従来どおり実カット | `silence_cutter.run(context)`（**無改変**） |

`timeline.enabled=false` のときは値に関わらず `"physical"` として扱う
（Timeline が無ければ編集点を反映する先が無いため）。

### 7.2 `detect_edit_points()`（新規）

既存 `run()` の検出部分だけを抜き出した薄い関数で、**実カット関数は一切呼ばない**。

```python
# 無音区間を検出し「残す区間」を編集点として context へ保持する（実カットは行わない）
# 戻り値: keep_segments（元動画時間の [(start, end), ...]）
def detect_edit_points(context):
    settings = context.settings
    silence_cfg = settings.get("silence_cut", {})
    va_cfg = settings.get("volume_analysis", {})
    ffmpeg_cfg = settings.get("ffmpeg", {})

    input_path = context.current_video_path()
    total_duration = ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg)

    # 無音カット OFF のときは「全長 1 区間」を編集点とする（Timeline は成立させる）
    if not silence_cfg.get("enabled", True):
        keep = [(0.0, total_duration)]
    else:
        # 閾値は従来と同一（volume_analysis の確定値を単一閾値として使う / resolve7）
        silence_ranges = detect_silence(
            input_path,
            va_cfg.get("last_cut_db", -28),
            _coerce_float(va_cfg.get("cut_min_silence_sec"), default=0.6),
            ffmpeg_cfg,
        )
        keep = build_keep_segments(silence_ranges, total_duration)

    context.set_keep_segments(keep)     # 既存 API をそのまま使う
    return keep
```

* 閾値の決定ロジック（`volume_analysis.last_cut_db` / `cut_min_silence_sec`）は
  現行 `run()` (`:416`) と**完全に同一**にする。編集点の内容が現行の実カット結果と一致することが、
  「従来と同じ仕上がりが Timeline 経由でも得られる」保証になる。
* `silence_cut.fade_enabled` は編集点モードではレンダリング時に適用する（§8.2）。

### 7.3 音声認識への影響

現行は「実カット後の動画」を認識対象にしていた（`subtitle_generator.run()` `:942`）。
編集点モードではその動画が無いため、§3-2 の方式 C を実装する。

```python
# 残す区間の「音声のみ」を連結した一時ファイルを作る（映像エンコード無し＝高速）
# 戻り値: 一時音声ファイルのパス（時間軸は Timeline 時間と一致する）
def extract_audio_segments(input_path, keep_segments, out_path, ffmpeg_settings, on_progress=None):
    # 各区間を -vn -map 0:a で抽出 → concat デマルチプレクサで連結
    # 進捗は run_ffmpeg_progress 経由（ffmpeg_runner.execute）
```

得られたタイムスタンプは**本編を詰めた後の時間軸**そのものなので、字幕クリップへほぼ直接載せられる。
OP を配置する場合のみ、その尺ぶんのオフセットを一律で加える（§6.9-2）。
併せて `TimeMap.to_source()` で元動画時間を逆算し、`origin.source_start/source_end` に記録する。

この一時音声ファイルは**プレビューの初回再生ソースとしても再利用する**（§6.4-5）。
認識のためにどのみち生成するものなので、追加コストなしで「画面を開いた直後に音が出る」
状態を作れる。破棄は `PipelineContext.cleanup()` に任せる（他の中間ファイルと同じ寿命）。

---

## 8. 動画生成フロー（R5・R13）

### 8.1 全体

「決定」押下 → `TimelineEditorDialog.accept()` → ブリッジ経由でワーカースレッドへ Timeline を返す
→ `pipeline_runner` が `TimelineRenderer` を呼ぶ。

```
Timeline（唯一のソース）
  │
  ├ Step 1  ベース映像の構築  ← OP / 本編 / ED をまとめてここで処理する（R15）
  │    V1 の各クリップを extract_segment() で抽出（-ss 入力シーク・CFR 固定）
  │      ※ 素材の規格がキャンバスと異なるクリップ（OP/ED 等）は
  │        scale → pad → setsar → fps → format で正規化してから抽出する
  │    → _concat_demux() で連結                     [既存関数を流用]
  │
  ├ Step 2  オーバーレイ合成（V2 以降のクリップがある場合のみ）
  │    z_order 昇順に overlay フィルタを鎖状に連結
  │    各クリップは enable='between(t,start,end)' で表示区間を限定
  │
  ├ Step 3  字幕焼き込み（S1 に use=true のクリップがある場合のみ）
  │    build_subtitle_file() で ASS 生成 → burn_subtitle() で焼き込み  [既存関数を流用]
  │
  └ Step 4  出力配置           output_writer.run(context)                 [既存・無改変]
```

**OP/ED 結合が独立工程でなくなる**のが従来との最大の差（R15 / §3-4）。
`concat_processor.run()` は Timeline モードでは呼ばれない（従来モード用に無改変で存置）。

進捗は各 Step で `context.progress_subcallback(label)` を渡し、
FFmpeg 実行はすべて `ffmpeg_runner.execute()`（＝`run_ffmpeg_progress`）を経由する。

### 8.2 Step 1 の詳細

* V1 クリップを `timeline_start` 昇順に処理する。
* **規格の正規化**: クリップの素材（`MediaRef`）の寸法・fps がキャンバスと一致しない場合のみ、
  `extract_segment()` へ渡すフィルタに次を挟む。内容は現行
  `concat_processor.concat()`（`src/modules/concat_processor.py:47`〜`:57`）と同一にする。

  ```
  映像: scale=W:H:force_original_aspect_ratio=decrease,
        pad=W:H:(ow-iw)/2:(oh-ih)/2, setsar=1, fps=FPS, format=yuv420p
  音声: aformat=sample_rates=SR:channel_layouts=stereo, asetpts=N/SR/TB
  ```

  W/H は `timeline.width/height`、FPS は `ffmpeg.output_fps`、SR は `ffmpeg.audio_sample_rate`。
  **本編クリップは元動画がそのままキャンバス規格なので、このチェーンは挟まらず
  現行と完全に同じコマンドになる**（＝既存の再エンコード品質・挙動を変えない）。
  OP/ED や D&D 素材だけが正規化を通る。
* **空白（ギャップ）の扱い**: 「削除（空白を残す）」の跡は
  **黒フレーム＋無音として出力する**（`timeline.render.gap_policy` 既定 `"black"`）。
  `color` / `anullsrc` フィルタでギャップ尺ぶんのクリップを生成して差し込む。
  既定を `"black"` にしたのは、そうしないと「空白を残す削除」と「リップル削除」の
  結果が同じになってしまい、回答 Q5 の 2 種類を用意した意味が無くなるため。
  詰めたい場合はリップル削除を使う。`"close"` を選べば従来どおり詰める挙動にもできる。
* **音声（R18）**: 各 V1 クリップの抽出時に、対応する A1 クリップの設定を同じコマンドへ反映する。
  時刻は V1 から導出されるため、音声側に別の `-ss` / `-t` を渡すことはない（＝ズレようがない）。

  | A1 の状態 | 処理 |
  |---|---|
  | 通常（`muted=false` / `gain_db=0`） | 素材の音声をそのまま抽出（フィルタを挟まない＝現行と同一コマンド） |
  | `gain_db ≠ 0` | `volume=<n>dB` を挟む |
  | `muted=true` | `anullsrc` で同尺の無音に差し替える |
  | 対応する A1 クリップが無い（`has_audio=false` の素材） | 同上。`anullsrc` で同尺の無音を生成する |

  これにより、どのクリップも必ず「映像と同尺の音声」を持った状態で連結される。
* **追加素材の音声**: `V2` 以降に音声付き動画を置いた場合は、
  ベース音声と `amix=inputs=2:duration=first` で合成する（Step 2 と同じフィルタグラフ内）。
* `silence_cut.fade_enabled=true` のときは `extract_segment()` の
  `fade_enabled` 引数へそのまま渡す（既存シグネチャに手を入れない）。
* `enabled=false` のクリップは出力しない（ギャップとしても扱わず、詰める）。

### 8.3 Step 3 の詳細（字幕）

* 字幕クリップの時刻は既に Timeline 時間なので、Step 1 の出力に対して**変換せず**そのまま使える。
* `use=false` のクリップは除外する（旧画面の「使用」チェックと同じ意味）。
* `transform.x/y` が `null` でないクリップは、`build_subtitle_file()` が本文先頭へ
  `{\an5\pos(px,py)}` を前置する（正規化座標 → `PlayResX/Y` のピクセルへ変換）。
  `null` のときは何も付けず、**既存の生成結果とバイト単位で一致する**（後方互換）。
* 個別 `font` / `font_size` の `{\fn}` `{\fs}` タグは既存実装
  （`_inline_font_override()` `:507`）をそのまま使う。

### 8.4 z_order とフィルタ順序

字幕（`ass` フィルタ）と画像（`overlay` フィルタ）が `z_order` で入り混じる場合、
1 回の `ass` フィルタでは表現できない。そこで:

1. オーバーレイ要素（V2 以降のクリップ＋字幕クリップ）を `z_order` 昇順に並べる。
2. **連続する字幕群**をひとまとまりにして 1 つの ASS ファイルへ書き出す。
3. 並び順どおりにフィルタを鎖状につなぐ。

```
[base] → ass(group1.ass) → overlay(画像A) → ass(group2.ass) → overlay(動画B) → [out]
```

字幕群が 1 つだけ（＝画像より常に前面／背面）の一般的なケースでは
`ass` フィルタは 1 回だけになり、現行の `burn_subtitle()` と同じ経路を通る。

### 8.5 「DaVinci Resolve 出力」との関係

既存の Resolve 出力（resolve20 / 21）は `keep_segments` と字幕 items を材料にしている。
Timeline からは同じ材料を取り出せるため、`resolve_export.build_timeline_spec(timeline, settings)` を
**追加**し、内部で既存の `_clips_from_segments()` / `_captions_from_items()` /
`_titles_from_items()` を再利用する。既存 2 経路（`build_clip_spec` / `build_archive_spec`）は無改変。

---

## 9. setting.json 追加案（追加のみ・既存キー不変）

```jsonc
{
  "silence_cut": {
    // ── 既存キーはすべて不変。以下 1 キーのみ追加 ──
    "mode": "edit_points"        // "edit_points"=編集点のみ（新既定） / "physical"=従来の実カット
  },

  "timeline": {
    "enabled": true,             // false で従来 UI・従来フローへ完全に戻す
    "project_dir": "",           // 空 = general.output_directory
    "project_suffix": ".timeline.json",
    "autosave_sec": 0,           // 既定 0=無効（読み戻し経路が無いため / 回答 Q6）
    "keep_project_file": true,   // false なら「決定」後に削除する

    // 設定の OP/ED を Timeline へ自動配置するか（R15 / §6.9）
    // false でも D&D による手動追加は可能。配置条件は concat_processor と同一。
    "opening_ending": {
      "auto_place": true
    },

    "min_clip_sec": 0.05,        // これ未満へはトリムできない
    // Delete キー単独の割り当て。false=空白を残す（既定）/ true=リップル。
    // Shift+Delete は常にもう一方。両方とも右クリックメニューからも実行できる（回答 Q5）
    "ripple_delete": false,
    "snap_enabled": true,
    "snap_threshold_px": 8,
    "default_zoom_px_per_sec": 40,
    "zoom_min_px_per_sec": 2,
    "zoom_max_px_per_sec": 400,

    "ui": {
      "split_ratio": 0.55,       // 上（プレビュー）: 下（Timeline）の初期比
      "track_height_px": 56,
      "subtitle_track_height_px": 40,
      "header_width_px": 88,
      "trim_handle_px": 6,
      "ruler_min_label_px": 60,
      "window_width": 1280,
      "window_height": 820
    },

    "preview": {
      "backend": "pyav",         // "pyav"（既定） | "ffmpeg"（フォールバックを強制）
      "width": 960,              // プレビュー描画の基準幅（高さはアスペクト維持）
      "update_debounce_ms": 60,
      "cache_frames": 8,
      "high_quality_button": true, // 「高精度プレビュー」ボタンの表示

      // ── 音声再生（R16 / §6.4-5）
      "audio_enabled": true,     // 起動時のミュート状態（false=ミュートで開く）
      "audio_volume": 0.8,       // 0.0〜1.0
      "audio_chunk_sec": 30,     // 1 回に生成する音声チャンクの長さ（秒）
      "audio_prefetch_sec": 8,   // 残りがこの秒数を切ったら次チャンクを先読み生成する
      "play_fps": 15             // 再生中の映像更新レート上限（音声は常に途切れさせない）
    },

    "media": {
      "video_extensions": [".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv"],
      "image_extensions": [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"],
      "default_image_duration_sec": 5.0,
      "max_video_tracks": 8
    },

    "render": {
      // "black"=空白を黒＋無音として出力（既定 / 回答 Q5）/ "close"=空白を詰める
      "gap_policy": "black",
      "extract_mode": "seek",    // silence_cut.extract_mode と同義（seek 推奨）
      "concat_reencode": false,
      "overlay_enabled": true    // false でオーバーレイ合成を丸ごとスキップ（切り分け用）
    }
  }
}
```

`DEFAULT_SETTINGS`（`settings_window.py:117`）へ同内容を追加する。
既存の `_merge_with_defaults()` (`:446`) がユーザー値優先で補完し、
`load_settings()` (`:399`) が新規キーを 1 度だけ書き戻すため、**アップデート時の移行処理は不要**。

設定画面（`SettingsWindow`）への項目追加は、`preview` 節や `export` 節と同様に
**行わない**（`setting.json` で管理）。UI 追加の要否は §15-Q7 で確認する。

---

## 10. エラー処理・フォールバック方針

新規例外 `TimelineError(AutoEditError)` を `src/exceptions.py` へ追加する。

| 事象 | 挙動 |
|---|---|
| プロジェクト JSON の**書き出し**失敗 | WARNING に留めて処理を続行する。JSON は記録用であり編集・出力には使わない（回答 Q6）ため、これで実行を止めない |
| プロジェクト JSON の**読み込み**失敗（破損・未知 `schema_version`） | `TimelineError` を送出する。ただし現時点でこの経路は使われない（毎回作り直すため）。将来のプロジェクト再編集機能で「理由を明示して開かない」挙動になる |
| メディアの実体が見つからない | 該当クリップを `enabled=false` にして WARNING。編集は続行でき、レンダリング時は無視される |
| **OP/ED 素材が見つからない / 未設定** | 自動配置をスキップして WARNING を 1 回（現行 `concat_processor.is_available()` `:15` と同じ）。Timeline は本編のみで成立し、ユーザーが D&D で足すこともできる |
| **OP/ED 素材の規格が本編と異なる** | エラーにせず正規化して取り込む（§8.2）。正規化を適用したことを INFO で 1 回出す |
| PyAV の import 失敗 / デコード失敗 | `FfmpegFrameSource` へ自動フォールバック。INFO で 1 回だけ通知 |
| フレーム取得の失敗 | プレビュー領域に「フレームを表示できません」を表示。編集操作は続行可能 |
| **QtMultimedia が使えない** | 再生ボタン・音量 UI を無効化し、フレーム送りのみで継続。INFO を 1 回（§6.4-5） |
| **音声チャンクの生成失敗** | 「音声を再生できません」を状態表示に出し、無音で再生ヘッドだけを進める。編集は続行可能 |
| **再生中に映像が追いつかない** | エラーではない。フレームを間引いて音声を優先する（§6.4-5）。ログも出さない |
| 無音検出の失敗（`FFmpegError`） | 全長 1 クリップの Timeline を作って続行（編集は可能）。WARNING |
| 有音区間が 0 件 | 全長 1 クリップとして続行（現行は `InputError` で中断していたが、Timeline では手編集で救済できるため方針を変える） |
| 音声認識の失敗 | 字幕トラックを空にして続行（`SubtitleError` で中断しない）。WARNING |
| レンダリング中の FFmpeg 失敗 | 既存どおり `FFmpegError` を送出。`main_window._format_failure_message()` (`:242`) が stderr 末尾を併記 |
| 「キャンセル」押下 | 既存どおり `PipelineCancelled`（`subtitle_generator._review_timeline` `:882` と同じ扱い） |
| 画面クローズ時 | `FrameSource` の container を閉じ、一時ファイル・プロジェクト自動保存を停止する（`SubtitleEditorDialog.done()` `:578` と同じ寿命管理） |

**原則**: プレビュー・Timeline 表示系の失敗で編集不能にしない。
レンダリング（＝最終成果物）の失敗のみパイプラインを止める。

---

## 11. ログ出力方針

既存の `get_logger(__name__)`（`src/utils/logger.py`）を使う。レベルは `logging.level` に従う。

| タイミング | レベル | 内容 |
|---|---|---|
| 編集点検出 | INFO | `編集点検出: 無音 N 件 / 残す区間 M 件 (閾値=-23dB, 最小無音=0.60s, 総尺=3612.3s)` |
| 認識用音声抽出 | INFO | `認識用音声を抽出: M 区間 → 3421.9s (元 3612.3s / 圧縮率 94.7%)` |
| OP/ED 配置 | INFO | `オープニングを配置: op.mp4 (4.00s)` / `エンディングを配置: ed.mp4 (6.50s)` / `オープニング配置スキップ (縦動画のため)` |
| OP/ED の正規化 | INFO | `OP/ED 素材を正規化: 1280x720 30fps → 1920x1080 60fps` |
| Timeline 構築 | INFO | `Timeline 構築: V1 M クリップ (OP=有 / ED=有) / S1 K 字幕 / 全長 3432.4s / 60fps 1920x1080` |
| プロジェクト保存 | INFO | `プロジェクト保存: D:/.../sample.timeline.json` |
| 画面終了 | INFO | `Timeline 編集確定: V M クリップ / オーバーレイ P 件 / 字幕 K 件（使用 K' 件）/ ギャップ G 件` |
| レンダリング各 Step | INFO | `[1/4] ベース映像構築 開始` … 既存 `begin_step` / `end_step` の書式に揃える |
| フォールバック発生 | INFO | `PyAV が利用できないため ffmpeg フレーム抽出で動作します` / `QtMultimedia が利用できないためプレビュー音声を無効にします` |
| 音声チャンク生成 | DEBUG | `音声チャンク生成: 120.0s から 30.0s`（再生のたびに出るため INFO にしない） |
| 認識用音声の再利用 | INFO | `プレビュー音声に認識用音声を再利用します（未編集）` |
| メディア欠落・重複解決・区間補正 | WARNING | 発生ごと（多発時は 1 回に集約） |
| 編集操作の詳細（Move/Trim ごと） | DEBUG | `clip=c12 trim right 12.480 → 13.900` |

**編集操作を INFO で出さない**のは、ドラッグ 1 回で数十行出てログが実用に耐えなくなるため。

---

## 12. 影響範囲・後方互換

| 対象 | 影響 |
|---|---|
| クリップ用パイプライン | **変更あり**。`timeline.enabled=true`（既定）で新フローになる |
| アーカイブ切り抜きパイプライン | **影響なし**。`clip_writer` / `ArchiveResultWindow` は無改変 |
| `SubtitleEditorDialog` / `SubtitlePreviewWidget` | **無改変で存置**。`timeline.enabled=false` でのみ表示される |
| `silence_cutter` の実カット関数群 | **無改変で存置**。`silence_cut.mode="physical"` で従来どおり動作 |
| `concat_processor` | **無改変で存置**。Timeline モードでは呼ばれない（OP/ED はレンダラが扱う / R15）。正規化チェーンの内容は §8.2 でレンダラへ移植する |
| OP/ED の自動配置条件 | **従来と同一**（縦動画は付けない / `*_enabled` フラグ / 素材の実在）。違いは「配置後にユーザーが編集・削除できる」点だけ（§6.9-1） |
| `general.opening_video` 等の設定値 | **読むだけで書き換えない**。Timeline 上で OP を削除しても設定は変わらず、次回実行時にはまた配置される |
| DaVinci Resolve 出力（resolve20/21） | 既存 2 経路は無改変。Timeline 用の第 3 経路を追加 |
| 中間ファイルの寿命 | **従来どおり**。正規化後動画・認識用音声・プレビュー用一時ファイルは実行完了時に `PipelineContext.cleanup()` で破棄する（回答 Q10。後日再編集のための永続化は行わない） |
| `setting.json` | 追加のみ。既存キーの意味・既定値は不変 |
| CLI / ヘッドレス実行 | `subtitle_review_callback` 未注入時は Timeline 画面を出さず、**編集点をそのまま採用**して自動レンダリングする（現行の「全件使用」と同じ思想） |
| 既存テスト（`tests/`） | `test_fcpxml_builder` / `test_resolve_export` / `test_loudness_normalizer` は無影響 |
| 配布物サイズ | PyAV は既に同梱済みのため**増分なし** |

**切り戻し手順**: `setting.json` の `timeline.enabled` を `false` にする（再起動のみ）。
これで無音カットも実カットに戻り、v1.4.0 と同一の挙動になる。

---

## 13. 将来拡張を考慮した設計方針

| 拡張 | 本設計での備え |
|---|---|
| トランジション・オーバーレイのフェード（回答 Q9 で今回は見送り） | `Clip` に `transition_in` / `transition_out` を後付けできるよう、クリップ境界を `source_in/out` と `timeline_start` の 3 値で独立管理している。レンダリング側は `fade` / `xfade` フィルタで対応でき、プレビュー側は `QGraphicsItem` の opacity で近似できる |
| 速度変更（スロー・早送り） | `duration ≠ source_out - source_in` を許す拡張余地を §6.2.3 に明記。`speed` フィールド追加で対応 |
| キーフレームアニメーション | `Transform` を値オブジェクトとして独立させてあるため、`Transform` を「時刻→値」の列に置き換えれば済む |
| 音声の独立編集・BGM トラック | `Track.kind="audio"` と `link_track` を既に持つ。`link_track=null` のトラックを許可すれば独立編集になる |
| 字幕を映像へ再同期する | 字幕クリップは V1 に追従しない設計だが（回答 Q2）、`origin.source_start/source_end` に元動画時刻を残してある。`TimeMap.to_timeline()` を通し直すだけで「現在の V1 に合わせて字幕を貼り直す」機能を作れる |
| BGM・効果音の追加 | 回答 Q3 により今回は「Audio は Video と完全連動」に固定した（R18）。将来 BGM を足すなら `link_track=null` の**独立音声トラック**を別途許可すればよい。本編音声（A1）のリンク規則には手を入れずに共存できる |
| 音声波形の表示 | `A1` クリップは V1 と同じ矩形で描いているだけなので、背景に波形画像を敷くだけで足りる。PyAV で音声フレームを読み RMS 列を作れば生成できる（新規依存なし） |
| 縦動画（Shorts）対応 | `timeline.width/height` を `resolve_output_profile()` から解決しているため、縦でもそのまま成立する。字幕の縦用上書きは既存 `build_effective_subtitle_cfg()` を通す |
| アーカイブ切り抜きへの Timeline 適用 | `TimelineBuilder` が「編集点＋字幕 items」だけを入力に取るため、`clip_writer` からも同じ形で呼べる |
| 他 NLE への書き出し（EDL / OTIO） | `resolve_export` の spec 生成が Timeline を入力に取れるようになるため、フォーマットを足すだけで済む |
| **プロジェクト単位の保存・再編集**（回答 Q6 で将来要望として表明） | 今回は「毎回作り直し・JSON は記録用」に留めるが、必要な部品は Phase 0 で作り終える。`project_io.load()`・スキーマ検証・版マイグレーションは実装済みで単体テストも通っている状態にしておく。実運用に足りないのは次の 3 点だけ: ① 正規化後動画を破棄せず残す（回答 Q10 で今回は不要とされた部分）② 「プロジェクトを開く」導線 ③ 素材の相対パス化（プロジェクトを別 PC へ持ち運ぶ場合） |

**設計上の一貫した約束**: 「Timeline が唯一のソース」「モデルは Qt 非依存」「編集は必ずコマンド経由」
の 3 つを守る限り、上記の拡張はいずれも UI とレンダラの局所変更で収まる。

---

## 14. 実装手順（フェーズ分け）

各フェーズ終了時点で**アプリが動作する**（＝既存機能を壊さない）ことを条件にする。
`docs/HowToRelease.md` の手順に従い、リリースはフェーズ 8 完了後に 1 回行う。

| Ph | 内容 | 完了条件 | 既存への影響 |
|---|---|---|---|
| **0** | `src/timeline/` の骨格。`model` / `timemap` / `project_io` と単体テスト。`AudioClip` の時刻導出（R18 / §6.3.4）もここで作る | Timeline を組んで JSON 往復でき、`TimeMap` の写像と A1 の導出時刻がテストで通る | **なし**（誰も呼ばない） |
| **1** | `silence_cutter.detect_edit_points()` 追加、`silence_cut.mode` 追加、`builder` で Timeline 構築（**OP/ED 自動配置を含む** / R15）＋ JSON 保存 | `mode="edit_points"` で JSON が出力され、本編の区間が従来の実カット結果と一致し、OP/ED が §6.9-1 の条件どおり前後に載る | `mode="physical"` 既定なら挙動不変 |
| **2** | 音声のみ抽出（`extract_audio_segments`）と音声認識の接続。OP 尺ぶんの字幕オフセット | 認識結果が Timeline 時間で得られ、処理時間が現行以下 | 編集点モードのみ |
| **3** | `TimelineEditorDialog` の器＋`TimelineView`（表示・ズーム・スクロール・再生ヘッド）。編集はまだ不可 | Timeline が見え、OP/ED が色分け表示され、再生ヘッドを動かせる | `timeline.enabled=false` で従来 UI |
| **4** | `PreviewPanel`＋`FrameSource`（PyAV／ffmpeg）。再生ヘッド追従 | スクラブでフレームが追従する。OP/ED クリップ上でもその素材のフレームが出る。PyAV 不在環境でも表示できる | 同上 |
| **5** | **音声再生**（R16）: `AudioChunkSource`＋`QMediaPlayer` 同期・ミュート/音量・認識用音声の再利用 | 再生ボタンで音が出て映像が追従する。QtMultimedia 不在環境では無効化されるだけで落ちない | 同上 |
| **6** | クリップ編集（`commands`：移動・トリム・分割・**削除 2 種**）＋ Undo/Redo ＋スナップ＋ **A1 連動**（R10 / R17 / R18） | R10 の全操作が効き、「削除」と「リップル削除」が別結果になり、Undo で戻る。**全操作の後で A1 の時刻が V1 と一致している**（`Split` / `Delete` のリンク張り直しを単体テストで確認）。OP/ED も同じ操作で編集できる | 同上 |
| **7** | プレビュー上の要素編集（選択・ドラッグ・右クリック削除／レイヤー）＋インスペクタ（R7・R8） | R7・R8 が効く | 同上 |
| **8** | メディア D&D（R12）＋ `TimelineRenderer`（R5・R15）＋「決定」ボタン（R13） | Timeline どおりの動画が出力される。OP/ED が正規化されて連結され、ギャップが黒＋無音になり、進捗が `run_ffmpeg_progress` で表示される | 同上 |
| **9** | `timeline.enabled` 既定を `true` へ。Resolve 出力の Timeline 経路。`main_window.spec` 更新。ドキュメント更新・リリース | 新 UI が既定になり、`false` で従来へ戻せる | 切り替え発生 |

フェーズ 0〜2 は UI を伴わないため、`tests/` に単体テストを追加して検証する
（`test_timeline_model.py` / `test_timeline_project_io.py` / `test_timemap.py` /
`test_timeline_builder.py`）。特に Phase 1 では
**「従来の実カット結果と編集点由来のクリップ列が一致すること」** を回帰の基準にする。

フェーズ 8 の完了時点で初めて動画が出力できるようになるため、
それまでは `timeline.enabled=false`（既定）で従来フローを維持し、
開発者だけが `true` にして触れる状態にしておく。

---

## 15. 確認事項

### 15.1 回答反映済み（2026-08-07）

| # | 論点 | 回答 | 反映先 |
|---|---|---|---|
| Q1 | OP / ED の扱い | **使用する場合は Timeline 上で編集可能にする** | R15。§3-4 で方式 C（V1 の前後に通常クリップとして配置）を採用。§6.9 に詳細設計、§8.1〜8.2 にレンダリング、§12 に影響範囲 |
| Q2 | 字幕の V1 追従 | **一旦追従しなくてよい** | §6.3.1。当初案どおり。由来時刻は `origin` に残し、再同期は将来拡張（§13） |
| Q3 | Audio Track の編集範囲 | **Audio は Video と連動し、Video 側の編集（カット等）を Audio にも適用する** | R18。当初案（V1 リンク固定）の追認だが、確実性を上げるため設計を強化した。「編集を伝播させる」のではなく **A1 クリップに時刻を持たせず V1 から導出する**方式に変更し、音ズレをデータ構造で防ぐ（§4-9 / §6.3.4）。JSON の音声クリップから時刻フィールドを外し（§6.2.2 / §6.2.3）、レンダリングでも音声に独立したシークを渡さない（§8.2） |
| Q4 | プレビューの音声再生 | **音を聞く必要はある** | R16。§3-5 で方式 C（チャンク生成＋`QMediaPlayer` をマスタークロック）を採用。§6.4-5 に詳細設計。Phase 5 として工程を新設 |
| Q5 | 削除の挙動 | **空白を残す。ただしリップル削除もできるように** | R17。§6.6 で 2 種類を常に併設。`gap_policy` の既定を `"black"` に変更し、2 種類が実際に別結果になるようにした（§8.2） |
| Q6 | プロジェクト JSON が既に在るとき | **一旦常に作り直し。将来はプロジェクト単位で保存できるとよい** | §6.2.1。読み戻し経路は持たず、`autosave_sec` 既定を 0 に変更。将来拡張として §13 に必要な残作業 3 点を明記 |
| Q8 | 画像の既定表示尺 | **5.0 秒で妥当** | §9（変更なし） |
| Q9 | オーバーレイのトランジション | **まだ未対応でよい** | §13 の将来拡張に留置。実現手段（`fade`/`xfade`）だけ明記 |
| Q10 | 一時ファイルの寿命 | **一旦残さなくてよい** | §12。従来どおり実行完了で破棄。プロジェクト再編集を実装する際に見直す（§13） |

### 15.2 未回答（実装着手前に確定したい）

| # | 論点 | 現時点の案 | 確認したいこと |
|---|---|---|---|
| Q7 | **設定画面への項目追加** | `setting.json` のみで管理（`preview` / `export` 節と同じ扱い） | ズーム既定・スナップ・削除の既定などを `SettingsWindow` に出すべきか |
| Q11 | **音声認識の対象** | 残す区間の音声のみ（§3-2 方式 C） | 無音区間に小さく入っている発話を拾いたい要件はあるか |

---

## 16. 回答による設計変更の要約と、残る不確定要素

### 16.1 今回の回答で変わった点

| 変更 | 影響の大きさ | 理由 |
|---|---|---|
| **OP/ED を Timeline クリップ化**（Q1） | **中**。レンダリング工程が 6 → 4 になり、`concat_processor` を通らなくなる。素材の規格差を吸収する正規化チェーンをレンダラへ移植する必要が生じた（§8.2） | 専用トラックを作らず V1 に載せたため、編集操作・Undo・JSON スキーマは無変更で済んでいる |
| **プレビュー音声再生の追加**（Q4） | **中**。`AudioChunkSource` と再生同期が新規。工程が 1 つ増えた（Phase 5） | 認識用に作る音声を初回再生に流用できるため、実装量は当初想定より小さい |
| **ギャップの既定を「黒＋無音」へ**（Q5） | **小**。設定既定値の変更とレンダラの分岐 1 つ | 既定が `"close"` のままだと「空白を残す削除」と「リップル削除」の出力が同一になり、2 種類を用意した意味が消えるため |
| **JSON を読み戻さない**（Q6） | **小**。むしろ実装が減る。`autosave_sec` 既定を 0 へ | 読み込み側は Phase 0 で作りテストしておくので、将来の再編集機能へそのまま繋がる |
| **音声を V1 からの派生値にする**（Q3） | **小**。JSON から音声クリップの時刻フィールドが消え、実装はむしろ簡潔になる | 方針自体は当初案（リンク固定）と同じだが、「伝播処理を書く」実装だと**コマンドを追加するたびに書き忘れが音ズレとして表面化する**。時刻を持たせない構造にすれば、明示的な追随が要るのは `Split` と `Delete` の 2 つだけになり、残りは自動的に整合する |

Q2 / Q8 / Q9 / Q10 は当初案の追認であり、設計変更は生じていない。

### 16.2 残る不確定要素

* **Q7（設定画面への項目追加）** は UI の追加のみで、モデル・レンダラに影響しない。
  Phase 9 の直前までに決まればよい。
* **Q11（音声認識の対象）** だけは **§3-2 の方式選定そのものを覆す可能性がある。**
  無音区間の小さな発話も拾う必要があるなら方式 A（元動画全長を認識）へ切り替えることになり、
  ① 処理時間が増える ② 認識結果が元動画時間になるため `TimeMap.to_timeline()` を通した
  写像が必要になる ③ プレビュー初回音声の流用（§6.4-5）が成り立たなくなる、の 3 点が変わる。
  **Phase 2 に着手する前に確定させたい。**

---

## 17. 実装状況（2026-08-07）

Phase 0〜9 を実装済み。`timeline.enabled` は既定 `true`（新 UI が既定）。

### 17.1 新規ファイル

| ファイル | 内容 |
|---|---|
| `src/timeline/model.py` | Timeline / Track / Clip / SubtitleClip / AudioClip / MediaRef / Transform |
| `src/timeline/timemap.py` | 元動画時間 ↔ Timeline 時間の写像 |
| `src/timeline/project_io.py` | プロジェクト JSON の読み書き・検証・マイグレーション |
| `src/timeline/builder.py` | 編集点＋字幕 → Timeline 構築（OP/ED 自動配置を含む）・`timeline` 設定の既定補完 |
| `src/timeline/commands.py` | 編集操作コマンドと `CommandStack`（スナップショット方式の Undo/Redo） |
| `src/timeline/renderer.py` | Timeline → FFmpeg → 動画生成 |
| `src/timeline/frame_source.py` | PyAV / ffmpeg のフレーム取得 |
| `src/timeline/audio_source.py` | プレビュー用の音声チャンク生成 |
| `src/timeline/media_probe.py` | メディアの種別・尺・寸法・音声有無の判定 |
| `src/gui/timeline/timeline_controller.py` | モデル ↔ UI の唯一の窓口 |
| `src/gui/timeline/timeline_view.py` | ルーラ・トラックヘッダ・Timeline 本体・ズーム/スクロール |
| `src/gui/timeline/preview_items.py` | オーバーレイの `QGraphicsItem` |
| `src/gui/timeline/preview_panel.py` | プレビュー（フレーム表示・音声再生・オーバーレイ編集） |
| `src/gui/timeline/timeline_editor_dialog.py` | 編集画面本体・インスペクタ・決定/キャンセル |
| `tests/test_timeline_model.py` 他 3 本 | Phase 0/6 の単体テスト |

### 17.2 変更ファイル

| ファイル | 変更 |
|---|---|
| `src/exceptions.py` | `TimelineError` を追加 |
| `src/modules/silence_cutter.py` | `detect_edit_points()` / `extract_audio_segments()` / `concat_files()` を**追加**。既存の実カット関数群は無改変 |
| `src/modules/subtitle_generator.py` | `recognize_for_timeline()` を追加。`build_subtitle_file()` に `\pos` 対応を追加（位置未指定なら既存と同一出力） |
| `src/pipeline/pipeline_context.py` | `timeline` / `project_path` / `asr_audio_path` / `timeline_review_callback` を追加 |
| `src/pipeline/pipeline_runner.py` | `is_timeline_mode()` による分岐と `_run_timeline()` を追加。従来経路は `_run_legacy()` として不変 |
| `src/gui/main_window.py` | `TimelineReviewBridge` を追加。既存 `SubtitleReviewBridge` は無改変で併存 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` に `timeline` 節と `silence_cut.mode` を追加 |
| `src/export/resolve_export.py` | `build_timeline_spec()` / `export_timeline()` を追加。既存 2 経路は無改変 |
| `src/main_window.spec` | `hiddenimports` に `av` を追加 |

### 17.3 確認済みの動作

* 単体テスト 151 件が通る（従来分 41 件を含む。既存テストへの影響なし）
* 通し確認（GUI なし・編集点をそのまま採用）: 無音検出 → 編集点 → Timeline 構築 →
  レンダリング → 出力。OP（1280x720@30）と ED（854x480@25）が正規化されて連結され、
  出力尺が「残す区間の合計 + OP + ED」と一致することを実測
* ギャップ（削除で残した空白）が黒 + 無音として出力される（輝度 16 / 音量 -91dB）
* オーバーレイ画像と字幕が `z_order` 順に合成され、`\pos` 指定した字幕が指定位置へ描画される
* 音声クリップの `muted` が無音として反映される（-91dB）
* 従来モード（`timeline.enabled=false`）で v1.4.0 と同じ経路が動き、
  プロジェクト JSON も作られない
* 編集画面のオフスクリーン起動、再生ヘッド移動によるプレビュー更新、分割・リップル削除・
  Undo・字幕のドラッグ位置指定・ズーム

### 17.4 設計から変えた点

| 変更 | 理由 |
|---|---|
| プロジェクト JSON の重複検証に 1.5ms の許容を設けた | 時刻をミリ秒精度で丸めるため、正常に書き出したファイルを読み戻すだけで「重複」と誤判定され、警告と微修正が出ていた |
| 「決定」ボタンを既定ボタンにしない | Enter / Space の取りこぼしで長いレンダリングが意図せず始まるのを防ぐ |
| 編集済みでのキャンセルに確認を挟む | 誤操作でパイプラインごと中断させないため |
| インスペクタの字幕入力はフォーカスアウトで確定 | 1 打鍵ごとにコマンドを積むと Undo 履歴が使い物にならなくなるため |

### 17.5 未着手（要確認事項の回答待ち）

* **Q11**: 音声認識の対象。現状は §3-2 方式 C（残す区間の音声のみ）で実装済み
* **Q7**: 設定画面への項目追加。現状は `setting.json` のみで管理
* **Q3 の将来拡張**（BGM など独立音声トラック）、オーバーレイのトランジション（Q9）、
  プロジェクト単位の再編集（Q6）は §13 のとおり未実装
