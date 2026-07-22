# resolve17 — Twitch アーカイブ切り抜き（採点式ハイライト抽出）要望設計書

## 0. 本書の位置づけ
`docs/request/request17.md` の要望に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要なライブラリを追加しない／後方互換を失わない／不明点は推測実装せず設計書へ記載する）に従い、本書レビュー後に実装します。

> **本要望は「機能追加」ではなく「新サブシステムの新設」です。** 現行アプリは実質 **CPU 前提・唯一の ML 依存が faster-whisper** という構成であり（§2）、request17 の採点（特に方式B）は 感情認識／笑い検知／OpenCV／MediaPipe／YOLO／Twitch API／ローカルLLM 等、**多数の重量級依存**を前提とします。よって本書は「全部を一度に作る」設計ではなく、**既存資産を最大再利用する土台 → 段階的にスコアラを足す**構成とし、重い依存・外部サービス・法務(ToS)論点は §8 の**確定待ち事項**として明示します。

---

## 1. 要望（request17.md 要約）

| # | 要望 | 補足 |
|---|---|---|
| R1 | 現行の自動編集は「**クリップ用**」として温存。今回は「**アーカイブ切り抜き用**」を追加し、**メイン画面をタブで切り分ける** | 現行機能は不変で残す |
| R2 | **Twitch アーカイブ(VOD) URL** を使って動作 | URL 入力 → 取得 |
| R3 | Twitch から**コメント（チャット）をダウンロード** | タイムスタンプ付き |
| R4 | 動画を**採点方式**にする。**5分窓を1分スライド**（方式A）／**30秒特徴量→5分窓積分**（方式B） | 2 方式が提示されている |
| R5-A | **方式A**：感情スコア（音量/ピッチ/話速/感情分類・加点表）＋ コメントスコア（ｗの数・急増ボーナス）。総合 = 感情×0.55 + コメント×0.45（100点満点）。**連続する高得点は1イベントに統合**し、**TOP5**を切り抜く | §4.5 |
| R5-B | **方式B**：30秒ごとに12項目の特徴量（重み付き）→ 時系列 → 5分窓で積分 → ランキング → **ローカルLLM最終判定** → TOP5切り抜き。積分 Total = Hook0.25+Emotion0.25+Comment0.20+Story0.20+Motion0.10 | §4.6 |
| R6 | 採点完了後、**専用画面**を表示：上半分＝**採点グラフ**（縦:点数／横:動画時間）、下半分左＝**字幕修正（現状の字幕修正画面と同じ要素）**、右＝**プレビュー（選択中の動画部分を再生）** | §4.7 |
| R7 | **完了ボタン**で切り抜き＋字幕焼き込み開始 | §4.8 |

---

## 2. 現状分析（既存コードの実測／再利用可否）

> 調査は `src\dist\`（PyInstaller 生成物）を除外して実施。

### 2.1 再利用できる既存資産（そのまま活かす）

| 資産 | 場所 | 本要望での用途 |
|---|---|---|
| **PipelineContext**（step/progress/一時ファイル/レビューコールバック基盤） | `src/pipeline/pipeline_context.py:12`（`begin_step`/`end_step`/`progress_subcallback`/`allocate_intermediate`/`cleanup`/`subtitle_review_callback`/`output_profile`） | アーカイブ採点パイプラインの土台に流用 |
| **Whisper 文字起こし**（faster-whisper） | `subtitle_generator.WhisperTextSource`（`subtitle_generator.py:290`、lazy import `:326`）。設定キー：`whisper_model/device/compute_type/beam_size` 等 | 話速・固有名詞・字幕・（LLM入力の）発話テキスト取得 |
| **音量/RMS 解析**（ffmpeg `volumedetect` ベース、ML 不要） | `volume_analyzer.measure_region_volume():24`（`(mean_db,max_db)` を返す）、`analyze_min_speech_db():70`、`silence_cutter.detect_silence` | 平均音量/ピーク/無音率スコアの実装基盤（**新規依存不要**） |
| **ASS 生成＋焼き込み** | `subtitle_generator.build_subtitle_file():484`、`burn_subtitle():561`（`fonts_dir`/`target_size` 対応）、`_format_ass_time():546`、`FontProfile:68` | 切り抜きクリップへの字幕焼き込み（R7）に流用 |
| **字幕修正 UI 要素** | `src/gui/subtitle_editor_dialog.py`（`SubtitleEditorDialog:155`、列＝時間/字幕/使用/配信者/サブ/コメント/フォント/サイズ、`result_items():343`） | R6 下半分左の「字幕修正」を**同要素**で実現（§4.7 で埋め込み化） |
| **縦/横プロファイル解決** | `output_profile.resolve_output_profile():48`（`{is_portrait,orientation,width,height}`） | 切り抜き出力の縦/横対応 |
| **設定の読込/保存・タブUI・フォント登録** | `settings_window.load_settings():272`/`save_settings():342`/`resolve_fonts_dir():350`/`register_fonts_in_dir():358`、`DEFAULT_SETTINGS:117`、`QTabWidget`（`_build_ui:455`、一般/字幕/縦動画の3タブ） | `archive` 設定セクション追加・設定タブ追加の型 |
| **ワーカースレッド＋橋渡し** | `main_window.py` の `PipelineWorker(QThread):183` / `SubtitleReviewBridge:74`（QueuedConnection でメインスレッドにダイアログ） | アーカイブ採点も別スレッド実行＋結果画面表示に流用 |
| **FFmpeg 実行/進捗** | `src/modules/ffmpeg_runner.py`、`utils/progress.run_ffmpeg_progress` | 取得・切り出し・焼き込みの実行 |

### 2.2 **存在しない**もの（新規実装が必要）

| 不足領域 | 現状 | 必要作業 |
|---|---|---|
| **Twitch ログイン/VOD 取得** | 無し（`yt_dlp`/`youtube_dl`/`requests` は非 dist src に**未使用**。ネットワークは `updater.py` の `urllib` のみ） | ログイン(OAuth)＋所有判定＋`twitch-dl download`（§4.3.0/4.3.1） |
| **Twitch コメント取得** | 無し（コード内の "comment" は字幕役割コメントのこと） | `twitch-dl chat json` を正規化（§4.3.2） |
| **ピッチ / 感情分類 / 笑い検知**（音声AI） | 無し（音声解析は ffmpeg `volumedetect` のみ、`librosa`/`transformers`/`torch` **未使用**） | 音声特徴スコアラ（§4.4、依存=§8-4） |
| **映像特徴**（Scene/Optical Flow/Motion/顔） | 無し（`cv2`/`mediapipe` **未使用**） | 映像スコアラ（方式B、依存=§8-5） |
| **ゲームイベント検知**（YOLO等） | 無し | 方式B・最重量（§8-5、原則スコープ外候補） |
| **ローカルLLM 判定** | 無し | LLM スコアラ（方式B、外部 Ollama 想定＝§8-6） |
| **採点/ランキング/イベント統合** | 無し（ハイライト抽出ロジックは皆無） | スコアラ集約・窓積分・統合・TOP5（§4.5/4.6） |
| **採点グラフ / プレビュー画面** | 無し（`QtCharts`/`QtMultimedia` は未使用） | 結果画面（§4.7） |
| **タブ化されたメイン画面** | `MainWindow` はフラットな `QWidget`（`main_window.py:275`、`_build_ui:296`） | メインを `QTabWidget` 化（§4.2） |

> **重要な構造的事実**：現行は「**CPU-only・依存最小**」を明確な設計方針としている（`main_window.spec` は CUDA を意図的に除外、hiddenimports は `faster_whisper`+`budoux` のみ）。request17 の全項目（特に方式B）を素直に実装すると **依存関係とビルドサイズが桁違いに増大**し、配布(PyInstaller)・CPU性能・実行時間・モデル配布に直結する。よって本設計は「**依存を増やさない中核**」と「**任意で足す重いスコアラ**」を**プラグイン境界**で分離する（§3-2, §4.4）。

---

## 3. 設計方針（全体）

1. **現行クリップ機能は完全温存**。メイン画面を `QTabWidget` 化し、①「クリップ用」（＝現行 `MainWindow._build_ui` の中身をそのまま移設）②「アーカイブ切り抜き用」（新規）に分割する（R1／§4.2）。**既存の実行フロー・シグナル・設定は不変**。
2. **スコアラ＝プラグイン境界**。採点は「特徴抽出 → 正規化スコア寄与 → 重み付き集約 → 窓積分 → ランキング → イベント統合 → TOP5」という**共通パイプライン**に統一し、各シグナル（音量・コメント・感情・笑い・映像・LLM…）を**個別スコアラ**として差し込む。**方式A と方式B はこの共通土台の上の“構成違い”**（窓幅・スライド幅・有効スコアラ・積分重み）として表現する（§4.4〜4.6）。これにより「まず軽い中核で動かし、重いスコアラは後から足す」段階実装が可能。
3. **中核は新規依存ゼロで成立させる**。中核スコア＝**コメント（ｗ数・急増）＋音量/無音（既存 `volume_analyzer`）＋話速/固有名詞（既存 Whisper）**。これらは既存資産のみで実装可能。ピッチ/感情/笑い/映像/LLM は**任意スコアラ**として、依存が用意された環境でのみ有効化（未導入なら重み0で自動スキップ＝結果は中核スコアで成立）。
4. **設定は setting.json 管理**。窓幅/スライド/重み/加点表/TOP件数/取得方式/LLM 接続先等はすべて `setting.json` の新規 `archive` セクションに置きハードコードを避ける（§4.9）。既定は方式Aの中核構成。
5. **字幕修正 UI は既存を再利用**。R6 の「字幕修正（現状と同じ要素）」は `SubtitleEditorDialog` の**テーブル部分を埋め込み可能ウィジェット化**して結果画面へ組み込む（§4.7.2）。列・戻り値仕様は既存踏襲。
6. **切り抜き＋焼き込みは既存経路を再利用**。TOP5 区間を ffmpeg で切り出し、各クリップに `build_subtitle_file`＋`burn_subtitle` で字幕焼き込み（R7／§4.8）。縦/横は `output_profile` に委譲。
7. **重量級依存・外部サービス・法務(ToS)は §8 で確定を取ってから着手**。特に「方式A/Bどちらを実装するか」「感情/笑い/映像/YOLO/LLM をどこまで入れるか」「Twitch取得の許諾」は**未確定のまま実装しない**（CLAUDE.md 準拠）。

---

## 4. 詳細設計

### 4.1 全体パイプライン（アーカイブモード）

```
[アーカイブタブ]
  Twitch VOD URL 入力 / コメントDL ON / 方式(A|B) 選択 / 「採点開始」
        │  (ワーカースレッド)
        ▼
  ① 取得        : VOD ダウンロード（§4.3）＋ コメントJSON ダウンロード（§4.3）
  ② 前処理      : 文字起こし(Whisper) / 音声抽出 / （方式B: 30秒グリッド or 方式A: 窓グリッド）
  ③ 特徴抽出    : 有効スコアラ群を区間ごとに実行（§4.4）→ 特徴時系列（メモリ/SQLite）
  ④ 積分・採点  : 窓ごとに重み付き集約（方式A=5分窓/1分スライド, 方式B=30秒×10積分）（§4.5/4.6）
  ⑤ ランキング  : 窓スコア降順 → イベント統合 → TOP5（§4.5）
 (⑤') LLM判定   : 方式Bのみ 上位候補をローカルLLMで最終採点/並べ替え（§4.6.3）
        ▼
  ⑥ 結果画面表示（§4.7）: 採点グラフ / 字幕修正 / プレビュー
        │  「完了」ボタン
        ▼
  ⑦ 切り抜き＋字幕焼き込み（§4.8）→ TOP5 クリップ出力
```

- ②〜⑤はワーカースレッド、⑥はメインスレッド（既存 `SubtitleReviewBridge` と同型の橋渡し）。
- 進捗は `PipelineContext.progress_callback` / スピナー（`main_window.py:337`）を流用。

### 4.2 メイン画面のタブ化（R1）

`src/gui/main_window.py` の `MainWindow` を **`QTabWidget` ホスト**へ再構成する。**既存 UI は削らず移設**する。

```
MainWindow(QWidget)
 └ QTabWidget
     ├ タブ1「クリップ用」 : 現行 _build_ui の中身をそのまま内包（ClipTabWidget へ切り出し）
     └ タブ2「アーカイブ切り抜き用」: ArchiveTabWidget（新規 / §4.2.1）
```

- **移設方針**：現行 `_build_ui()` が直接 `MainWindow` に組んでいるウィジェット群（入力欄・設定ボタン・実行・進捗・スピナー）を、新規 `ClipTabWidget(QWidget)` に**そのまま移動**。ドラッグ&ドロップ（`dragEnterEvent`/`dropEvent`）・自動更新（`start_update_check`）等の**現行挙動は不変**を維持（回帰しないことを実機確認）。
- **理由**：R1「現行はそのまま」を厳守しつつ、タブ内に隔離することで相互干渉を防ぐ。

#### 4.2.1 `ArchiveTabWidget`（新規）
| 要素 | ウィジェット | 備考 |
|---|---|---|
| **Twitch ログイン** | `QPushButton`「Twitch ログイン」＋状態ラベル（未ログイン/ログイン中: `<user>`） | タブ表示時に未ログインなら案内。ログインで所有判定・sub-only 取得が可能に（§4.3.0） |
| Twitch VOD URL | `QLineEdit` | 例 `https://www.twitch.tv/videos/xxxxxxxxx`。バリデーション（§4.3.1）。ログイン時は自分の VOD 一覧から選択も可 |
| コメント取得 | `QCheckBox`（既定ON） | R3 |
| 採点方式 | `QComboBox`（A / B） | 既定は設定 `archive.scoring.method`。**Bは依存が揃う環境のみ選択可**（未導入項目は注記） |
| 出力先 | 既存 `general.output_directory` を流用 | 個別化は §8-9 |
| 採点開始 | `QPushButton` | ワーカー起動 |
| 進捗/ステータス | `QProgressBar`＋`QLabel`＋スピナー | 既存流用 |

### 4.3 Twitch ログイン認証・取得（VOD＋コメント / R2・R3）

取得は新モジュール `src/archive/twitch_source.py`、認証は `src/archive/twitch_auth.py`。
**VOD もコメントも `twitch-dl`（ihabunek）に一本化する**（§8-2/§8-3 確定：調査で二役対応を実測確認）。
- VOD 取得：`twitch-dl download <URL|id>` → mp4。
- コメント取得：`twitch-dl chat json <URL|id>` → タイムスタンプ付き JSON（`chat ytt` の字幕形式出力も可）。
- sub-only/限定公開へのアクセスや所有確認は **`--auth-token <token>`**（公開 VOD は不要）。

> **簡素化**：本要望（ご指摘）により、当初案の yt-dlp / 非公式 GraphQL を**廃し twitch-dl に統一**。依存・実装面が一本化される。twitch-dl は Python パッケージ（`pipx`/`pip`）または単体実行ファイルで提供され、配布時は**外部 CLI として同梱 or 事前導入**を選べる（§8-2）。

#### 4.3.0 Twitch ログインと「自分/許諾済み」判定（ご要望反映）

**目的**：アーカイブタブに切り替えた際に Twitch ログインを行い、ログイン後に URL を実行することで **「自分のアーカイブか」を機械判定**する（R2 の運用ガード）。

**ログイン方式（`twitch_auth.py`）**：twitch-dl 自体に対話ログインは無く、**取得済み OAuth トークンを渡す**方式のため、ログインは本アプリ側で実装する。
- 既定案：**OAuth 認可コードフロー**。「Twitch ログイン」ボタンで既定ブラウザを開き、ローカル簡易 HTTP サーバ（`http.server`＝stdlib）で redirect を受け、トークンを取得。Client-ID は設定化（`archive.auth.client_id`）。
- 代替案（最小実装）：ユーザーが自分の Twitch **トークンを貼り付け**て保存（手動・低コスト）。

**所有判定フロー（ログイン時）**：
1. Helix `GET /users`（自分）→ `broadcaster_id`（自分の user id）を取得。
2. 入力 URL の video id から Helix `GET /videos?id=<id>` → その VOD の所有者 `user_id` を取得。
3. `VOD.user_id == 自分.user_id` なら **「自分のアーカイブ」＝実行許可**。
   - 併せて自分の VOD 一覧（Helix `GET /videos?user_id=<self>`）をタブに提示し、選択で URL 自動入力。

**他者 VOD の扱い（§8-1 確定：自分のみ許可）**：
- 本機能は **自分が所有する VOD のみダウンロード可**とする（他者 VOD 用の「公開＋同意」パスは設けない）。
- 所有判定（`VOD.user_id == 自分.user_id`）が**不成立の URL は実行不可**とし、明確なエラーで拒否する。
- これにより Twitch 利用規約・他者権利に関するリスクを最小化する（許諾は API で判定不能という限界を、運用を自分限定にすることで回避）。

**トークン種別の注意（実装時に要検証・§8-3b）**：twitch-dl のダウンロード用 `--auth-token` は **Web クライアントのトークン（`auth-token` クッキー相当）** で、Helix の OAuth トークンとは別物。自分限定運用（§8-1 確定）でも、**自分の sub-only/限定公開 VOD** を落とす場合は twitch-dl 用 Web トークンが要る。**1 回の OAuth ログインで「所有判定(Helix)」と「自 VOD の sub-only ダウンロード(twitch-dl)」を両立できるか**は §8-11 と同様にテストで確認する（不可なら公開/通常 VOD は OAuth のみで完結、sub-only 自 VOD はトークン貼付で補完）。

#### 4.3.1 VOD ダウンロード
- **入力**：Twitch VOD URL（`twitch.tv/videos/<id>`）。正規表現で video id を抽出、不正なら `InputError`。
- **取得**：`twitch-dl download` を `ffmpeg_runner` と同様のサブプロセス実行でラップ（進捗は twitch-dl の出力/ffmpeg 併用）。ログイン時は `--auth-token` を付与（sub-only 対応）。
- 取得先は `archive.download.work_dir`（`SETTINGS_DIR` 相対 or `general.output_directory` 配下、§4.9）。
- **既存ローカル mp4 指定**（`downloader="local"`）も残し、採点系を**取得無しで先行検証**できるようにする。
- 長尺 VOD（数時間）を想定し、**画質/範囲の設定**（`vod_format`、任意で開始/終了時刻）を設定化。

#### 4.3.2 コメント（チャット）ダウンロード
- `twitch-dl chat json <URL|id>` の出力を、以下の**内部正規化フォーマット**へ変換して採点に渡す（取得方式差を吸収）：
  ```json
  [{"offset_sec": 1234.5, "user": "name", "text": "ｗｗｗ", "emotes": ["Kappa"]}]
  ```
  `offset_sec` は VOD 先頭からの相対秒（twitch-dl の chat JSON はメッセージごとの相対オフセットを含む）。
- 取得失敗時は**コメントスコアを 0 として続行**（音声/映像スコアのみで採点）。全損させない（§5）。

> ⚠ **法務・ToS 注意（§8-1 確定）**：本機能は §4.3.0 のログイン所有判定により**自分が所有する VOD のみ**を対象とする（他者 VOD は実行不可）。これにより Twitch 利用規約・他者権利に関するリスクを最小化する。

### 4.4 特徴抽出＝スコアラ・プラグイン（§3-2 の中核）

共通インターフェース（新 `src/archive/scorers/base.py`）：

```
class Scorer:
    key: str                      # "comment" / "volume" / "emotion" ...
    enabled: bool                 # 依存未導入/設定OFF なら False → 集約時に自動スキップ
    def prepare(self, context): ... # 取得済み VOD/音声/文字起こし/コメントを受け取り前処理
    def score_segment(self, start, end) -> dict:
        # 例 {"raw": {...}, "points": <float>}  区間の寄与ポイント（正規化前）
```

- スコアラは `ScorerRegistry` で列挙。**依存が無いスコアラは `enabled=False`** となり、集約は生存スコアラの重みで実施（重み0扱い）。→ **中核だけで必ず完走**する。
- 区間粒度は方式で切替：方式A＝5分窓を直接採点、方式B＝30秒セグメントを採点し後段で積分（§4.6）。

#### 4.4.1 中核スコアラ（**新規依存なし**・既定 ON）

| スコアラ key | 実装 | 出典 |
|---|---|---|
| `comment` | 正規化コメント配列から、区間内の **「ｗ」「w」の総数を +1p/字**、さらに **その動画の平均コメント数/分 を上回る急増区間に +10p**（R5-A）。方式Bでは コメント数/平均文字数/草率/!?率/初見率 も算出（§4.6.1） | `twitch_source` 正規化配列 |
| `volume` | 区間 RMS 平均・ピークを `volume_analyzer.measure_region_volume()`（`:24`）で取得。**大声=+10p**（ピークが閾値超）等に写像 | 既存 `volume_analyzer` |
| `silence` | 無音率を `silence_cutter.detect_silence` から算出。**無言が長い=-8p**（R5-A）／方式B `無音率×-10` | 既存 `silence_cutter` |
| `speech_rate` | Whisper のワード時刻から発話速度(文字/秒)。方式B `話速×5` | 既存 Whisper（`word_timestamps=True`） |
| `proper_noun`（任意） | Whisper テキストから固有名詞抽出（辞書/簡易ヒューリスティック）。方式B `固有名詞×5` | 既存 Whisper（形態素は §8-7） |

#### 4.4.2 任意スコアラ（**要・追加依存**／既定 OFF・依存が揃えば ON）

| スコアラ key | 要望項目 | 想定手段 | 依存 | 確定 |
|---|---|---|---|---|
| `emotion` | 感情分類（喜怒哀驚）・感情変化 | 音声感情認識モデル（wav2vec2/speechbrain 等） | transformers/torch＋モデル | §8-4 |
| `laugh` | 大笑い/笑い検知 `+15p` / 方式B `笑い×20` | 音声イベント分類（YAMNet/PANNs）or ピッチ+音量ヒューリスティック | tf/torch or 軽量近似 | §8-4 |
| `pitch` | ピッチ（驚き/叫び判定の補助） | `librosa`/`parselmouth`/自前FFT | librosa 等 | §8-4 |
| `scene_motion` | 映像変化量/Optical Flow/Scene Change/Motion | OpenCV | `opencv-python` | §8-5 |
| `face_reaction` | 顔リアクション | MediaPipe | `mediapipe` | §8-5 |
| `game_event` | ゲームイベント | YOLO 等 | ultralytics/torch＋モデル | §8-5（原則スコープ外候補） |
| `llm` | 笑い/驚き/炎上/神プレイ/ストーリー/Short向け/続きが気になる(Hook) | ローカルLLM | Ollama 等・外部プロセス | §8-6 |

> **設計上の要点**：これら任意スコアラは「無ければ無いなりに動く」ことを保証（§3-3）。**CPU-only 方針を崩す判断（torch/ML同梱）は §8 の確定事項**であり、確定するまで**実装・同梱しない**。

### 4.5 方式A：5分窓/1分スライド 採点・イベント統合・TOP5（R5-A）

- **窓生成**：`window_sec=300` を `slide_sec=60` でスライド（0:00-5:00, 1:00-6:00, …）。設定化。
- **感情スコア**：`emotion`＋`laugh`＋`volume`＋`silence`＋`pitch` 系の寄与を **加点表**（`archive.scoring.method_a.emotion_points`）で合算し 0–100 に正規化。
  - 大笑い+15 / 大声+10 / 驚き+8 / 泣く+15 / 怒る+12 / 無言が長い-8（R5-A の表を設定値化）。
- **コメントスコア**：`comment`（ｗ数＋急増ボーナス）を 0–100 に正規化。
- **総合**：`Score = 感情×0.55 + コメント×0.45`（設定 `method_a.weights`）。
- **イベント統合（R5-A 明記の肝）**：スライド窓は隣接窓が大きく重なるため、**得点が近接して連続する窓群（例 91,90,92,89,88）は1イベントに統合**し、**そのピーク窓**を代表採用（例 2:00-7:00 のみ採用）。統合閾値 `event_merge_threshold`（点差）と最小間隔を設定化。
- **TOP5**：統合後イベントを総合点降順で 5 件（`top_n`）。各イベントの区間＝代表窓の `[start,end]`（±パディングは設定）。

擬似コード（方針）：
```
windows = slide_windows(duration, 300, 60)
scored  = [(w, aggregate_method_a(w)) for w in windows]     # 0-100
events  = merge_consecutive(scored, gap<=merge_threshold)   # 近接連続を1山に統合
top5    = sort_desc(events)[:top_n]
```

### 4.6 方式B：30秒特徴→時系列→5分積分→ランキング→LLM（R5-B）

#### 4.6.1 30秒特徴量（重み付き・R5-B表を設定化）
`segment_sec=30` グリッドで、区間ごとに以下を算出（重みは `method_b.feature_weights`）：
音量ピーク10(RMS) / 笑い20 / 感情変化20 / 話速5 / 無音率-10(VAD) / コメント数15 / コメント増加率15(平均との差) / エモート率10(草・Pog等) / 映像変化量10(OpenCV) / 顔リアクション15(MediaPipe) / ゲームイベント15(YOLO) / 固有名詞5。
- 音声特徴例：平均音量/最大音量/ピッチ/速度/笑い/叫び/無音率。
- コメント特徴：コメント数/平均文字数/草率/!?率/初見率。
- 映像特徴：Scene Change / Optical Flow / Motion / 人物移動 / UI変化。
- **時系列格納（§8-8 確定：メモリ）**：30秒粒度の特徴は**メモリ上の配列/辞書**で保持する（「Time Series Database」は本規模では不要＝新規依存回避。SQLite も使わない）。

#### 4.6.2 ローカルLLM特徴（30秒ごと・R5-B）
- 各30秒の発話テキスト（＋任意で映像サマリ）を LLM に渡し `Laugh/Shock/Story/Hook`（0–100）を返させ**この4値のみ保存**。
- LLM は **外部 Ollama 等（HTTP）**を既定とし（`llm.provider="ollama"`, `endpoint`）、**アプリに LLM を同梱しない**（配布肥大回避）。未接続なら `llm` スコアラ無効＝方式Bでも他特徴で成立（§8-6）。

#### 4.6.3 5分窓積分・ランキング・最終判定
- **5分窓＝30秒×10区間**を積分（合算）。カテゴリ集約：
  `Total = Hook×0.25 + Emotion×0.25 + Comment×0.20 + Story×0.20 + Motion×0.10`（`method_b.integ_weights`）。
- Total 降順でランキング → **上位候補のみ LLM 最終判定**で並べ替え/足切り（任意）→ **TOP5**。
- イベント統合（近接窓の1山化）は方式Aと共通ロジックを流用。

### 4.7 結果画面 `ArchiveResultWindow`（R6）

新規 `src/gui/archive_result_window.py`。1ウィンドウを**上下2分割・下段左右2分割**で構成。

```
┌───────────────────────────────────────────────┐
│  採点グラフ（縦:点数 / 横:動画時間, TOP5マーカ）   上半分  │
├───────────────────────┬───────────────────────┤
│  字幕修正（既存要素の埋込）      │  プレビュー(選択区間を再生)  │
│  下半分・左              │  下半分・右                │
└───────────────────────┴───────────────────────┘
        [ 完了（切り抜き＋字幕焼き込み） ]   [ キャンセル ]
```

#### 4.7.1 採点グラフ（上半分）
- 縦軸＝スコア、横軸＝動画時間、**TOP5区間をマーカ/ハイライト**表示。クリックで下段のプレビュー/字幕をその区間へ同期。
- **描画手段（§8-10 確定：QtCharts 採用）**：**`PySide6.QtCharts`（`QChart`＋`QLineSeries`＋TOP5用 `QScatterSeries`）**で折れ線＋マーカを描画する。QtCharts は Qt アドオンモジュールのため **PyInstaller の spec に `PySide6.QtCharts` を含める**（§4.10）。折れ線データは窓スコア列。データ供給を抽象化し描画実装は差替え可能に保つ。

#### 4.7.2 字幕修正（下半分・左）＝**既存 UI 要素の再利用**
- `SubtitleEditorDialog`（`subtitle_editor_dialog.py:155`）の**テーブル構築ロジックを埋め込み可能ウィジェット `SubtitleEditorWidget` にリファクタ**（列＝時間/字幕/使用/配信者/サブ/コメント/フォント/サイズ、`result_items()` 互換）。ダイアログ版は薄いラッパとして維持し**既存呼び出し(main_window)を壊さない**。
- 表示対象は**現在選択中のTOP5クリップに含まれる字幕**（Whisper 文字起こしを当該区間で切り出したもの）。R6「現状の字幕修正画面と同じ要素」を満たす。
- クリップ切替時にテーブルを差し替え、編集結果はクリップごとに保持。

#### 4.7.3 プレビュー（下半分・右）
- **`PySide6.QtMultimedia`（`QMediaPlayer`＋`QVideoWidget`）**で VOD をロードし、字幕修正で選択中の区間へ `setPosition()` でシーク再生（§8-11：QtMultimedia の再生バックエンド可用性を実機確認。不可なら静止フレーム＝ffmpeg 抽出画像にフォールバック）。
- 選択字幕行の `start` にシーク、区間終端で停止。

#### 4.7.4 メインスレッド橋渡し
- 既存 `SubtitleReviewBridge`（`main_window.py:74`）と同型の **`ArchiveResultBridge`** を新設し、ワーカー完了時にメインスレッドで本ウィンドウを開く。戻り値＝「完了(編集済みTOP5＋区間)」or「キャンセル」。

### 4.8 完了＝切り抜き＋字幕焼き込み（R7）

`src/archive/clip_writer.py`（新規）：
1. TOP5 各区間を ffmpeg で切り出し（`-ss/-to`、`extract_mode` は既存 `silence_cut.extract_mode` 思想を流用）。
2. 各クリップに対し `build_subtitle_file()`（`:484`）で当該区間の（編集済み）字幕から ASS 生成 → `burn_subtitle()`（`:561`）で焼き込み。縦/横は `output_profile.resolve_output_profile()` に委譲、`fonts_dir=resolve_fonts_dir(settings)` を渡す（追加フォント対応・resolve16 と整合）。
3. 出力名は **先頭に `archive` を付与**（§8-9 確定）：`archive_<VOD名>_clip{n}_{start}-{end}.mp4` 等（接頭辞・書式は設定化）。`general.output_directory` へ**共用配置**。
- **既存の焼き込み経路をそのまま使う**ため、色/役割/個別フォント/縦動画対応は現行仕様を自動継承。

### 4.9 setting.json 追加案（**追加のみ**・既存キーは不変）

`DEFAULT_SETTINGS`（`settings_window.py:117`）に **新規トップレベル `archive`** を追加（`_merge_with_defaults` が欠落補完＝後方互換）。既定は「方式A・中核スコアラのみ・任意スコアラ無効」。

```jsonc
"archive": {
  "enabled": true,
  "download": {
    "downloader": "twitch-dl",       // "twitch-dl"|"local" (yt-dlp/GQL は廃止)
    "twitch_dl_path": "twitch-dl",   // CLI パス (アプリに同梱 / §8-2 確定)
    "vod_format": "best",
    "chat_source": "twitch-dl",      // "twitch-dl chat json" に一本化
    "work_dir": "archive_work"       // 取得物の作業ディレクトリ
  },
  "auth": {                          // Twitch ログイン (§4.3.0)
    "client_id": "",                 // OAuth 認可コードフロー用 Client-ID
    "redirect_port": 3737,           // ローカル redirect 受信ポート
    "owner_only": true               // §8-1 確定: 自分が所有する VOD のみ許可
  },
  "output": {
    "clip_prefix": "archive"         // §8-9 確定: 出力ファイル名の先頭に付与
  },
  "scoring": {
    "method": "A",                   // "A" | "B"
    "top_n": 5,
    "event_merge_threshold": 5,      // 近接連続窓を1イベントに統合する点差
    "event_min_gap_sec": 60,
    "clip_pad_sec": 0,               // 採用区間の前後パディング
    "window_sec": 300,               // 方式A: 5分窓
    "slide_sec": 60,                 // 方式A: 1分スライド
    "segment_sec": 30,               // 方式B: 30秒グリッド
    "method_a": {
      "weights": { "emotion": 0.55, "comment": 0.45 },
      "emotion_points": {            // R5-A 加点表 (ハードコード回避)
        "big_laugh": 15, "loud": 10, "surprise": 8,
        "cry": 15, "anger": 12, "long_silence": -8
      },
      "comment": { "w_point_per_char": 1, "rate_spike_bonus": 10 }
    },
    "method_b": {
      "feature_weights": {           // R5-B 30秒特徴の重み
        "volume_peak": 10, "laugh": 20, "emotion": 20, "speech_rate": 5,
        "silence": -10, "comment_count": 15, "comment_rate": 15,
        "emote_rate": 10, "scene_motion": 10, "face_reaction": 15,
        "game_event": 15, "proper_noun": 5
      },
      "integ_weights": {             // 5分積分の重み
        "hook": 0.25, "emotion": 0.25, "comment": 0.20, "story": 0.20, "motion": 0.10
      }
    }
  },
  "scorers": {                       // 任意スコアラの有効化 (依存が無ければ実行時に自動無効)
    "emotion": false, "laugh": false, "pitch": false,
    "scene_motion": false, "face_reaction": false,
    "game_event": false, "llm": false
  },
  "llm": {                           // 方式B・任意
    "provider": "ollama",
    "endpoint": "http://localhost:11434",
    "model": ""
  }
}
```

- 既存セクション（`general`/`subtitle`/`silence_cut`/`volume_analysis`/`ffmpeg`/`vertical`/`logging`）は**一切変更しない**。
- 出力先・作業ディレクトリのパス解決は既存 `resolve_fonts_dir` と同じ **`SETTINGS_DIR` 相対 or 絶対指定**方針（ハードコード回避）。

### 4.10 変更・新規ファイル一覧（予定）

| 種別 | ファイル | 内容 |
|---|---|---|
| 変更 | `src/gui/main_window.py` | `QTabWidget` 化、現行UIを `ClipTabWidget` へ移設、`ArchiveTabWidget` 追加、`ArchiveWorker`/`ArchiveResultBridge` 追加 |
| 新規 | `src/gui/archive_tab.py` | アーカイブ入力タブ（URL/方式/開始） |
| 新規 | `src/gui/archive_result_window.py` | 結果画面（グラフ＋字幕修正埋込＋プレビュー） |
| 新規 | `src/gui/score_graph_widget.py` | 採点グラフ（`PySide6.QtCharts` 折れ線＋TOP5マーカ / §8-10） |
| 変更 | `src/gui/subtitle_editor_dialog.py` | テーブル部を `SubtitleEditorWidget` に切り出し（ダイアログは薄いラッパ化・**既存挙動不変**） |
| 新規 | `src/archive/__init__.py` / `archive_pipeline.py` | 採点オーケストレーション（PipelineContext 流用） |
| 新規 | `src/archive/twitch_source.py` | VOD＋コメント取得（`twitch-dl download` / `chat json` ラッパ） |
| 新規 | `src/archive/twitch_auth.py` | Twitch ログイン（OAuth）・所有判定（Helix `/users`・`/videos`）（§4.3.0） |
| 新規 | `src/archive/scorers/`（`base.py`, `comment.py`, `audio_volume.py`, `silence.py`, `speech.py` …） | スコアラ・プラグイン群（中核は新規依存なし） |
| 新規 | `src/archive/scorer_pipeline.py` | 窓生成・積分・ランキング・イベント統合・TOP5 |
| 新規 | `src/archive/clip_writer.py` | 切り抜き＋字幕焼き込み（既存 build/burn 流用） |
| 変更 | `src/settings/settings_window.py` | `DEFAULT_SETTINGS["archive"]` 追加、（任意）「アーカイブ」設定タブ追加 |
| 変更 | `src/settings/setting.json` | `archive` 既定値追加 |
| 変更 | `src/main_window.spec` | **twitch-dl CLI 同梱**（§8-2）、**`PySide6.QtCharts` 同梱**（§8-10）、プレビュー用 QtMultimedia。**CUDA/torch/YOLO 等は入れない**（§8-4/5/13＝CPU-only 維持） |

---

## 5. エラー処理・フォールバック方針

- **VOD URL 不正/取得失敗** → `InputError`/専用例外で中断し UI 通知。既存 `AutoEditError` 系を流用（新規例外は最小限）。
- **コメント取得失敗** → コメントスコア0で採点続行（全損回避）。
- **任意スコアラの依存未導入/初期化失敗** → 当該スコアラを `enabled=False` にしログ警告、中核スコアで採点続行。
- **LLM 未接続/タイムアウト** → `llm` スコアラ無効で続行（方式Bでも他特徴で成立）。
- **プレビュー再生不可（QtMultimedia バックエンド無し）** → ffmpeg 抽出静止画へフォールバック（§8-11）。
- **採点結果0件** → 「ハイライト候補なし」を通知し切り抜きをスキップ（`review_on_empty_skip` 思想と整合）。
- 例外は `main_window` の `_format_failure_message`（`:171`）と同様に UI へ集約通知。

## 6. ログ出力方針
- 取得：VOD/コメントの件数・所要時間・保存先を INFO。失敗は WARNING/ERROR。
- 採点：有効/無効スコアラ一覧、窓数、TOP5 の区間と点数を INFO。
- 任意スコアラのスキップ理由（依存無し等）を WARNING。
- LLM：呼出件数・平均レイテンシを INFO。
- 既存 `utils/logger.get_logger` を流用（`logging` 設定セクション準拠）。

## 7. 影響範囲・後方互換
- **現行クリップ機能**：タブ内へ移設するのみで**フロー・設定・焼き込み結果は不変**（回帰テスト＝現行動画で従来出力と一致確認）。
- **setting.json**：`archive` は**追加のみ**。既存キー不変・`_merge_with_defaults` で旧設定ファイルも自動補完。
- **`SubtitleEditorDialog`**：テーブル切り出しは**インターフェース非破壊**（`__init__`/`result_items()` 互換維持）。
- **依存（§8 確定反映）**：中核に追加するのは **twitch-dl（外部CLI同梱）** と **`PySide6.QtCharts`（Qtアドオン）** のみ。**torch/ML/OpenCV/MediaPipe/YOLO/CUDA は当面同梱しない**（§8-4/5/13）。CPU-only 方針は維持。

---

## 8. 確認事項 → **回答反映（answer17.md / 2026-07-22）**

| # | 項目 | 論点 | **回答（確定 2026-07-22）** |
|---|---|---|---|
| 1 | **Twitch 取得の許諾/ToS ＋ 所有判定** | 自分/他者の扱い | **確定：自分が所有する VOD のみダウンロード可**。所有判定（§4.3.0）不成立の URL は実行不可。他者 VOD の「公開＋同意」パスは設けない |
| 2 | **VOD＋コメント取得手段** | twitch-dl 一本化・配布形態 | **確定：`twitch-dl` に一本化し CLI をアプリに同梱**（PATH 非依存）。VOD=`download`／コメント=`chat json`。`local` 指定も残す |
| 3 | **ログイン方式** | OAuth or トークン貼付 | **確定：OAuth 認可コードフロー**（ブラウザ＋localhost redirect、Client-ID 設定化） |
| 3b | **トークン種別の両立** | Web トークン vs Helix トークン | **保留（実装時テスト）**：自 VOD が sub-only の場合の両立可否を検証。不可なら sub-only はトークン貼付で補完（§4.3.0） |
| 4 | **方式A の感情/笑い/ピッチ（ML/torch 容量）** | 質問への回答 | **回答＝§8Q-4**。torch 同梱は非常に重い（+0.5〜1GB超）。→ **方式A中核は torch 不要のヒューリスティックで実装し、torch/ML は同梱しない** |
| 5 | **方式B の映像/顔/YOLO（実行時間）** | 質問への回答 | **回答＝§8Q-5**。YOLO を外せば実行時間・依存とも大幅減。→ **YOLO は当面採用しない**（方式B自体も後回し） |
| 6 | **LLM** | 外部/ローカル | **確定：外部 LLM を使用**（HTTP。同梱しない。§4.6.2） |
| 7 | **固有名詞抽出** | 質問への回答 | **回答＝§8Q-7**。→ **当面は依存なしの軽量ヒューリスティック**（必要時に純Python軽量 janome を検討） |
| 8 | **時系列保持** | sqlite/メモリ | **確定：メモリで代替**（sqlite も使わない。§4.6.1） |
| 9 | **切り抜きの出力先/命名** | 共用/専用 | **確定：`general.output_directory` 共用。ファイル名先頭に `archive` を付与**（§4.8/4.9） |
| 10 | **採点グラフの描画** | QPainter/QtCharts | **確定：QtCharts（`PySide6.QtCharts`）を採用**。spec に同梱（§4.7.1/4.10） |
| 11 | **プレビュー再生** | QtMultimedia 可用性 | **確定：テストで確認**（不可時は ffmpeg 静止画へフォールバック。§4.7.3/§5） |
| 12 | **方式A/B 実装順** | A先/B先 | **確定：方式A から着手**（§9） |
| 13 | **配布(PyInstaller)** | CUDA/重依存 | **確定：CUDA は入れない**（CPU-only 維持）。torch/ML/YOLO も当面同梱しない |

### 8Q. 質問（#4/#5/#7）への回答

**§8Q-4：ML/torch を同梱すると容量は重い？ → 非常に重い。**
CPU 版 PyTorch だけで展開後 ~200MB、依存 DLL（MKL 等）込みで配布に **+0.5〜1GB 超**。感情モデル（wav2vec2 系で ~360MB 等）がさらに加算。現行の軽量 CPU ビルド（faster-whisper は CTranslate2 ベースで **torch 非依存**）は数十MB 規模のため、torch 同梱は桁が変わる。→ **方式A中核は torch 不要のヒューリスティック（音量/無音/コメント/話速）で実装し、torch/ML は同梱しない**（§8-4 確定）。

**§8Q-5：YOLO を外せば実行時間は減る？ → 大幅に減る。**
YOLO は毎フレーム物体検出で、CPU・長時間 VOD では最も重い処理（ultralytics+torch も抱える）。除外すれば依存・ビルドサイズ・実行時間の全てが大きく削減。YOLO は方式B専用のため、方式A優先の本方針では当面スコープ外（§8-5 確定）。

**§8Q-7：固有名詞抽出、形態素解析器 vs 簡易ヒューリスティックのメリデメ**

| | 形態素解析器（janome / SudachiPy 等） | 簡易ヒューリスティック（正規表現・カタカナ連続等） |
|---|---|---|
| 精度 | 高い（品詞判定で固有名詞を正しく抽出） | 低い（取りこぼし・誤検出あり） |
| 依存 | 追加（janome＝純Python軽量／SudachiPy＝辞書数十MB） | **なし** |
| 速度/保守 | やや重い・辞書更新等 | 軽量・単純 |

固有名詞は方式Bの重み5（低優先）。→ **当面は依存なしのヒューリスティック**、精度が要れば純Python軽量な janome を検討（§8-7 確定）。

---

> ✅ **確定反映（2026-07-22）**：第1弾は「**方式A・中核スコアラ（torch非依存ヒューリスティック）＋タブ化＋Twitchログイン(自分のみ)＋twitch-dl取得＋QtCharts結果画面＋切り抜き焼き込み**」。追加依存は **twitch-dl（同梱）** と **PySide6.QtCharts** のみ。**CUDA/torch/YOLO は入れない**。感情/映像/LLM 等の重いスコアラと方式Bは、本第1弾の完成後に段階追加する。本設計はレビュー後、実装フェーズへ移行する。

---

## 9. 段階実装（レビュー後）

1. **基盤**：メイン画面タブ化（現行を `ClipTabWidget` へ無改変移設＝回帰ゼロ確認）＋ `ArchiveTabWidget` 骨格（ログインボタン含む）。
2. **ログイン＋取得**：`twitch_auth`（OAuth・所有判定）→ `twitch_source`（`twitch-dl download`／`chat json` 正規化）。まず `local` 指定で採点系を先行検証、次いでログイン所有判定→取得を結線。
3. **中核採点**：`comment`/`volume`/`silence`/`speech_rate` スコアラ＋方式A窓/積分/イベント統合/TOP5（**新規依存なし**）。
4. **結果画面**：採点グラフ（`PySide6.QtCharts`）＋字幕修正埋込（`SubtitleEditorWidget` 切り出し）＋プレビュー（QtMultimedia、不可時は静止画）。
5. **切り抜き＋焼き込み**：`clip_writer`（既存 build/burn 流用、縦/横対応）。
6. **任意スコアラ（確定分のみ）**：感情/笑い/ピッチ → 映像/顔 → LLM（方式B）→（対象なら）YOLO。各々プラグイン追加＋設定＋spec更新。
7. 実素材で 方式A/B・縦/横・コメント有無・依存有無の各組合せを焼き込み確認。

実装着手は本設計書のレビュー後とする（CLAUDE.md「実装は設計書レビュー後に行う」遵守）。
