# Stretheus (autoedit) 設計書

最終更新日: 2026-11-06
対象バージョン: **v1.8.2** (`src/version.py` / `installer/AutoEdit.iss` と一致)
対象: autoedit リポジトリ (配布名 **Stretheus**)

---

## 0. 本書の位置づけ

* 本書は **現行実装の設計書**である。`docs/` 配下で「今の仕様」を述べるのは本書と
  `docs/howto/`・`docs/layout/`・`docs/HowToRelease.md` だけであり、
  `docs/request/`・`docs/error/`・`docs/quality/`・`docs/security/`・`docs/reduce/` は
  **その時点の記録 (履歴)** である。文書の正の順序は次のとおり。

  1. `src/` の実装 (最終的な正)
  2. 本書 (`docs/design/design.md`) — 実装を要約した現行設計
  3. `docs/howto/` (利用者向け) / `docs/layout/` (画面) / `docs/HowToRelease.md` (配布)
  4. `docs/request/` 等の履歴文書 — **過去の意思決定の記録**。現行仕様の根拠として
     単独で引用してはならない

* 文書全体の索引と履歴文書の読み方は `docs/README.md` を参照。
* 開発の決まり (日本語コメント / ハードコード禁止 / 実装前に設計 など) は `docs/CLAUDE.md`。
* 本書は 2026-05-31 の初版 (無音カット + テロップ + OP/ED 結合の 4 工程だけを扱う設計) を
  全面改訂したものである。初版との差分は付録 A にまとめた。

---

## 1. システム概要

### 1.1 目的

配信アーカイブから YouTube 向けの**フルテロップ切り抜き動画**を作る工程を自動化する
Windows デスクトップアプリ。利用者の操作は「素材を選ぶ → 実行 → 編集画面で直す → 決定」の
4 手に収める。

### 1.2 提供する実行経路

アプリは 3 つの入口を持つ。いずれも最終工程 (Timeline レンダリング → 出力) を共有する。

| # | 経路 | 入口 | 入力 | 概要 |
|---|---|---|---|---|
| A | クリップ用 1 本編集 | メイン画面「クリップ用」タブ | 動画ファイル 1 本 | 正規化 → 無音検出 → 文字起こし → Timeline 編集 → 書き出し |
| B | アーカイブ切り抜き | メイン画面「アーカイブ切り抜き用」タブ | Twitch VOD (ログインして取得) またはローカル長尺動画 | 全長を採点して候補区間を選び、クリップ単位に前処理 → 一括編集 → 結合して 1 本出力 |
| C | 編集の再開 | 各タブの「続きから」 | 保存済みプロジェクト JSON | 素材を復旧して Timeline 編集 → 書き出し |

付随機能として **縦動画プロジェクトの作成** (経路 A/B の Timeline から派生) と
**DaVinci Resolve 出力** (`.fcpxml` + SRT サイドカー) がある。

### 1.3 想定実行環境

* OS: Windows 11 (開発・配布の基準)
* Python: 3.14 (`docs/CLAUDE.md`)。開発端末では PySide6 を入れた 3.12 でも動作する
* GUI: PySide6 (Qt Widgets)
* 外部バイナリ: `ffmpeg.exe` / `ffprobe.exe` — **同梱**し `ffmpeg.executable` で解決する
  (配布既定 `ffmpeg/ffmpeg.exe`。PATH には依存しない)
* 音声認識: faster-whisper (**CPU 実行に統一**。CUDA ランタイムは同梱しない)
* 任意の外部 CLI: `twitch-dl` (VOD / チャット取得)
* 任意のモデル: ONNX 人物検出 (`models/yolox_tiny.onnx`。ぼかしの追従補助)
* サーバー: Stretheus API (`api.base_url`) — ログイン・ポイント・課金・ログ送信

### 1.4 非機能要件

* **設定の外部化**: 可変値はすべて `src/settings/setting.json`。ハードコード禁止。
  既定値の唯一の出どころは `settings_window.DEFAULT_SETTINGS` (§6)
* **後方互換**: 欠落キーは起動時に既定で補完する。既存利用者の値は温存する (§6.4)
* **オフライン耐性**: API へ到達できなくても編集・書き出しは完走する。
  ログ送信・課金情報は縮退する (§7)
* **既定で軽い**: 機能が無効なら重い依存を import しない (例: `blur.enabled=false` なら
  onnxruntime を触らない)
* **進捗の可視化**: FFmpeg 実行はすべて `run_ffmpeg_progress` 経由 (§3.7)
* **依存を増やさない**: 追加ライブラリは原則入れない。ゴミ箱送り・DPAPI・HTTP は
  標準ライブラリ (`ctypes` / `urllib`) で実装する

---

## 2. 全体構成

### 2.1 パッケージ構成

```
src/
├── main.py                 CLI エントリ (1本/一括。GUI を伴わない実行)
├── version.py              アプリバージョン (.iss と一致させる)
├── exceptions.py           例外階層 (§8)
├── pipeline/               経路 A/C のオーケストレーション
│   ├── pipeline_runner.py    run_pipeline / run_from_project
│   └── pipeline_context.py   工程間で受け渡す状態 (中間パス・進捗・コールバック)
├── modules/                単機能の処理 (FFmpeg / Whisper を叩く層)
│   ├── ffmpeg_runner.py      FFmpeg 実行・ffprobe・可用性確認
│   ├── loudness_normalizer.py EBU R128 2 パス正規化
│   ├── volume_analyzer.py    発話区間の最低 dB 実測 (カット閾値の初期値)
│   ├── silence_cutter.py     無音検出 / 編集点算出 / 実カット
│   ├── subtitle_generator.py 音声認識 → ASS 生成 → 焼き込み
│   ├── speaker_filter.py     話者照合によるテロップの絞り込み
│   ├── comment_decor.py      コメント字幕の背景ボックス・アイコンの幾何
│   ├── font_advance.py       フォントの送り幅 (折り返し計算の実測)
│   ├── blur_overlay.py       ぼかしマスクの焼き込み
│   ├── watermark_overlay.py  透かしの焼き込み
│   ├── output_profile.py     出力キャンバス (縦横・寸法) の解決
│   ├── concat_processor.py   OP / ED 結合 (従来フロー用)
│   └── output_writer.py      最終ファイル配置・検証
├── timeline/               編集情報の唯一のソース (Qt 非依存)
│   ├── model.py              Timeline / Track / Clip / SubtitleClip / AudioClip
│   ├── project_io.py         プロジェクト JSON の読み書き・検証・移行
│   ├── builder.py            編集点 + 認識結果 → Timeline 構築
│   ├── vertical_builder.py   横 Timeline → 縦 Timeline
│   ├── commands.py           編集操作 (Command) と Undo/Redo
│   ├── clipboard.py          ノードのコピー/貼り付け
│   ├── compositor.py         合成フィルタ断片の唯一の計算元 (出力とプレビュー共通)
│   ├── renderer.py           Timeline → FFmpeg → 動画
│   ├── proxy_render.py       プロキシ (低解像度タイル) 生成
│   ├── crop.py               縦動画の切り抜き指定
│   ├── timemap.py            元動画時間 ↔ Timeline 時間の写像
│   ├── subtitle_style.py     テロップ見た目の優先順 (クリップ > 役割 > 既定)
│   ├── frame_source.py       プレビュー用フレーム取得 (PyAV / ffmpeg)
│   ├── audio_source.py       プレビュー用音声チャンク
│   ├── media_probe.py        素材の種別・尺・寸法判定
│   ├── media_recovery.py     保存済みプロジェクトの素材復旧
│   └── media_sidecar.py      正規化済み音声のサイドカー
├── archive/                経路 B (VOD 採点 → 切り抜き)
│   ├── twitch_auth.py        Twitch OAuth・所有判定・ストリームマーカー取得
│   ├── twitch_source.py      twitch-dl ラッパ (VOD / チャット)
│   ├── vod_range.py          VOD の一部区間だけの取り直し
│   ├── vod_retention.py      取得 VOD の寿命管理
│   ├── vod_rebase.py         Timeline 素材を元 VOD へ張り替え
│   ├── legacy_cache.py       旧版が残したキャッシュの回収
│   ├── features.py           採点用の特徴抽出 (音量 / 無音率)
│   ├── comment_source.py     チャットの正規化・セル集計
│   ├── marker_source.py      ストリームマーカー → 強制セクション
│   ├── scoring.py            方式 A 採点・窓積分・上位区間の選択
│   ├── pipeline.py           採点オーケストレーション
│   ├── clip_writer.py        クリップの前処理 → 一括レビュー → 焼き込み → 結合
│   ├── intro_source.py       イントロカードの素材 (開始前バッファ) 用意
│   ├── timeline_builder.py   複数クリップを 1 本の Timeline へ
│   ├── project_resume.py     アーカイブ用プロジェクトの再開
│   ├── cut_origin.py         切り出し素材 → 元 VOD の座標変換
│   └── config.py             archive セクションの読み出し
├── blur/                   トラッキングぼかし (指定・追従・マスク生成)
├── export/                 DaVinci Resolve 出力 (FCPXML / SRT)
├── services/               サーバー連携 (HTTP は標準ライブラリのみ)
│   ├── api_client.py         Stretheus API クライアント (401 リフレッシュ 1 回)
│   ├── stretheus_auth.py     ログイン (OAuth 認可コード) と JWT 管理
│   ├── auth_store.py         JWT のローカル保存 (Windows DPAPI)
│   ├── device_id.py          未ログイン端末の識別
│   ├── points.py             ポイントの予約 / 確定 / 取消・透かし要否
│   ├── billing.py            サブスクリプション (Stripe) のファサード
│   ├── telemetry.py          ログを積む唯一の入口
│   ├── log_queue.py          ローカルキュー (waiting → sending → sent)
│   ├── log_sender.py         溜まったログの送信
│   ├── log_payloads.py       送信本文の組み立てとマスク
│   ├── login_check.py        疎通確認 CLI
│   └── seed_points.py        dev 環境へのデータ投入 CLI
├── gui/                    PySide6 の画面 (表示と入力のみ)
│   ├── main_window.py       メイン画面 (2 タブ + 残高 + サブスク + 言語 + 自動更新)
│   ├── archive_tab.py       アーカイブ切り抜き用タブ
│   ├── archive_result_window.py / score_graph_widget.py  採点結果画面・採点グラフ
│   ├── subtitle_editor_dialog.py / subtitle_preview_widget.py  字幕一覧・プレビュー
│   ├── volume_threshold_dialog.py  カット閾値の確認
│   ├── project_library_dialog.py / project_resume_row.py / project_thumbnail.py
│   ├── account_tab.py / subscription_window.py / points_indicator.py / billing_workers.py
│   ├── theme.py             デザイントークンの唯一の出どころ (QSS 生成)
│   └── timeline/            Timeline 編集画面 (view / controller / preview / ぼかし指定 ほか)
├── settings/
│   ├── setting.json         設定の実体
│   └── settings_window.py   設定画面 + DEFAULT_SETTINGS + load/save
├── i18n/                   日本語原文キーの対訳 (ja / en)
└── utils/                  logger / progress / disk / trash / workspace / updater / proc / stage_timer
```

### 2.2 レイヤと依存の向き

```
        [ gui/*  (PySide6) ]        表示と入力だけ。編集ロジックを持たない
                │ 呼ぶ
                ▼
  [ pipeline/* ] [ archive/* ] [ export/* ]      オーケストレーション
                │
                ▼
  [ timeline/* ] [ modules/* ] [ blur/* ]        Qt 非依存の処理層
                │
                ▼
  [ services/* ] [ utils/* ] [ i18n ] [ settings ]   基盤 (HTTP / ログ / 設定 / 文言)
```

* **GUI は `timeline.commands` 経由でしかモデルを変更しない**。これにより Undo/Redo が
  全操作へ一様に効き、CLI / テストからも同じモデルを扱える。
* **合成の計算は `timeline/compositor.py` だけが持つ**。出力 (renderer)・プレビュー
  (proxy_render / 高精度プレビュー) が同じ計算を共有するため「画面と出力がズレる」を防ぐ。
* **設定の読みは各パッケージの `config.py`** (`archive/config.py`・`blur/config.py`・
  `export/config.py`) に閉じる。欠落キーはそこで既定へ落とす。

---

## 3. 処理フロー

### 3.1 経路 A: クリップ用 1 本編集 (既定 = Timeline モード)

`silence_cut.mode = "edit_points"` かつ `timeline.enabled = true` のとき本経路になる
(`pipeline_runner.is_timeline_mode`)。進捗は **4 工程**。

```
[実行]
  │  入力の事前確認: ffprobe で読めるか / 作業先と出力先の空き容量 (general.min_free_space_gb)
  │  出力キャンバスの解決 (output_profile) → 以降の全工程が同じ寸法を見る
  │  ログ送信の projectId 採番 (telemetry) → ポイント予約 (points.reserve)
  │  └ 透かしが入る場合は開始前に確認。やめれば消費せず中断
  ▼
[1] 音声解析・正規化        loudness_normalizer   EBU R128 2 パス (映像は -c:v copy)
  │
  ├─ 音量解析 + カット閾値の確認   volume_analyzer → GUI ダイアログ
  │     確定値は volume_analysis.last_cut_db へ保存し、無音検出の単一閾値とする
  ▼
[2] 無音検出 (実カットしない)  silence_cutter.detect_edit_points → 残す区間 (編集点)
  ▼
[3] 音声認識                 subtitle_generator.recognize_for_timeline
  │     残す区間の音声だけを faster-whisper へ渡す
  ▼
   Timeline 構築             timeline.builder.build
  │     V1: OP → 本編 (残す区間) → ED / A1: V1 にリンク / S1..: 役割別の字幕
  │     プロジェクト JSON を保存 (失敗しても実行は止めない)
  ▼
   Timeline 編集画面          TimelineEditorDialog (GUI 経路のみ)
  │     キャンセル → PipelineCancelled で中断 (異常終了ではない)
  │     CLI/ヘッドレスでは編集点をそのまま採用
  ▼
[4] Timeline レンダリング     timeline.renderer.render
  │     Step1 ベース映像の構築 (V1 抽出 → 連結。ギャップは render.gap_policy)
  │     Step2 オーバーレイ合成 (V2 以降を z_order 順に overlay / ぼかし / 透かし)
  │     Step3 字幕焼き込み (ASS 生成 → 焼き込み)
  ▼
   出力                      output_writer.run
        <元ファイル名>_edited.mp4 を general.output_directory へ (衝突時は連番)
        ラウドネスの検証・必要なら補正 (loudness.verify_output / correct_output)
  ▼
[完了]  ポイント確定 (points.commit) → 工程ごとの所要をログへ 1 行
```

### 3.2 経路 A': 従来フロー (実カット)

`silence_cut.mode = "physical"` または `timeline.enabled = false` のとき。進捗は **5 工程**。
Timeline を作らず、工程ごとに動画ファイルを作り替える。

```
[1] 音声解析・正規化 → (カット閾値の確認) → [2] 無音カット (実カット)
  → [3] フルテロップ生成 (認識 → ASS → 焼き込み。字幕一覧画面は subtitle.review_enabled)
  → [4] オープニング結合 → [5] エンディング結合 → 出力
```

* OP/ED は未設定 (`opening_enabled` / `ending_enabled` が false またはパス空) ならスキップ。
* 解像度/SAR 差は concat 前に scale/pad で揃える。

### 3.3 経路 C: 保存済みプロジェクトの再開

`pipeline_runner.run_from_project`。進捗は **2 工程** (素材の復旧 / レンダリング)。

```
プロジェクト JSON 読込 (検証は後回し)
  → 種別判定: アーカイブ用なら専用経路へ案内して中断
  → 素材の復旧 (media_recovery): 中間ファイルが消えていれば元動画から作り直す。
       見つからない素材は再リンク画面へ (timeline.project.missing_media_policy)
  → 検証 (project_io.validate)
  → Timeline 編集画面 (mode="resume") → レンダリング → 出力
```

* 自動保存 (`*.autosave.json`) が残っていれば復元を提案する。保存先は本体の
  プロジェクトファイルのまま。
* 再開でも出力は 1 本できるため、通常実行と同じくポイントを予約する。

### 3.4 経路 B: アーカイブ切り抜き

```
[入力]  Twitch VOD URL (ログイン → 所有判定 → twitch-dl download / chat json)
        またはローカル長尺動画 (+ 任意のチャット JSON)
  ▼
[採点]  archive.pipeline.analyze
        ① 特徴抽出 (features): セル (slide_sec) 単位の音量 / 無音率 — 全長 1 パス
        ② コメント集計 (comment_source): ｗ数・コメント数・急増ボーナス
        ③ 方式 A 採点 (scoring): 感情点 × 重み + コメント点 × 重み → 窓 (window_sec) 積分
        ④ ストリームマーカー (marker_source) を「必ず残す区間」として合流
        ⑤ 上位区間の選択 (archive.scoring.top_n。既定 10)
  ▼
[前処理] clip_writer.prepare_one_clip をクリップごとに
        区間切り出し (cut_region) → 正規化 → 無音カット/編集点 → 文字起こし
        → クリップ単位の Timeline を用意 (archive/timeline_builder)
  ▼
[一括レビュー] ArchiveResultWindow / ArchiveTimelineDialog
        採点グラフ・クリップ選択・字幕編集・テーマ (イントロカード) 入力・
        セクション追加 (元 VOD の区間を取り直して挿入) ・使用可否
  ▼
[書き出し] clip_writer.write_clips
        クリップごとに 字幕焼き込み → テーマ有りはイントロカード + 左上タグ
        → archive.combine.enabled なら 1 本へ結合 (規格が揃えばストリームコピー)
  ▼
[出力]  archive_<元名>_clip<N>_<開始>-<終了>.mp4 と結合 1 本
        ポイントは「1 回の出力操作につき 1 予約」(出力本数に依らない)
```

* 取得した VOD は既定 (`archive.download.keep_vod = false`) でプロジェクトが
  自己完結した時点でゴミ箱へ送る (1 本 10〜25GB を抱えさせない)。
  後から区間を足すときは `vod_range` が**必要な区間だけ**取り直す。

### 3.5 縦動画プロジェクト

Timeline の V1 クリップ右クリック →「縦動画プロジェクトを作成」。
選んだクリップを 0 秒起点へ詰め直し、縦キャンバス (既定 1080x1920 / `vertical.*`) の
**クリップ用プロジェクト**として保存する。切り抜きは動画を作らず
`crop` 指定として持ち、書き出し時に FFmpeg の crop で適用する。
素材は元動画へ張り替えるため、作成後も開き直せる。

### 3.6 DaVinci Resolve 出力

Timeline 編集画面 / 字幕一覧 / アーカイブ結果画面の「DaVinci Resolve 出力」から
`export/resolve_export.py` を呼ぶ。編集点・字幕・スタイルを集めて FCPXML
(`export.resolve.format` / `fcpxml_version`) を生成し、必要なら SRT サイドカーも書く。
読み取り専用の追加機能であり、失敗 (`ExportError`) はパイプライン本体へ影響させない。
ポイントの出力種別は `resolveProject`。

### 3.7 進捗表示

* FFmpeg は **必ず** `utils/progress.run_ffmpeg_progress` 経由で起動する
  (`modules/ffmpeg_runner.execute` が唯一の呼び出し元)。
  `-progress pipe:1` を読み、`total_duration` から比率を出す。
  無応答が `ffmpeg.progress_timeout_sec` を超えたら打ち切る。
* 工程の粒度は `PipelineContext.begin_step / end_step` が持ち、
  工程内の細かい進捗は `progress_subcallback` で加算する。
* 工程ごとの所要は `utils/stage_timer` が 1 行のログへまとめる。
* GUI (windowed) から子プロセスを起動するときはコンソール窓を出さない (`utils/proc`)。

---

## 4. モジュール責務一覧

### 4.1 パイプライン

| モジュール | 責務 |
|---|---|
| `pipeline_runner` | 経路 A/C の工程順、ポイント予約・確定、ログ送信、例外の集約 |
| `pipeline_context` | 入力/中間/出力パス、設定、進捗コールバック、Timeline、透かし要否の保持と後始末 |
| `output_profile` | 入力の表示寸法から縦横を判定し、出力キャンバスを 1 回だけ解決 |
| `output_writer` | 最終ファイル名の解決 (`_edited`、衝突時連番) と配置、ラウドネス検証 |

### 4.2 音声・映像処理

| モジュール | 責務 |
|---|---|
| `loudness_normalizer` | Integrated/TP/LRA を `loudness.*` の目標へ 2 パス正規化。映像はコピー |
| `volume_analyzer` | 発話候補区間を測り「発話区間の最低 dB」を出す (閾値ダイアログの初期値) |
| `silence_cutter` | `silencedetect` → 残す区間の算出。実カット (`extract_mode` / `batch_size`) と編集点算出の両方 |
| `subtitle_generator` | 音声認識 (faster-whisper) → テロップ単位の整形・折り返し → ASS 生成 → 焼き込み。モデルの保持と解放 |
| `speaker_filter` | 話者照合でゲーム内音声由来のテロップを落とす (`subtitle.speaker_filter`) |
| `comment_decor` | コメント字幕の背景ボックス・アイコンの幾何 (プレビューと焼き込みの唯一の計算元) |
| `font_advance` / `gui/timeline/font_metrics` | フォントファイルから送り幅・縦メトリクスを読み、libass と同じ折り返しを再現 |
| `blur_overlay` / `blur/*` | 囲んだ場所を追いかけてマスク動画を作り、`alphamerge` で焼き込む |
| `watermark_overlay` | 透かしの描画 (`watermark.*`)。入れるか否かはサーバーの応答だけで決まる |
| `concat_processor` | OP/ED の結合 (従来フロー)。解像度/SAR を揃えてから concat |
| `ffmpeg_runner` | コマンド組み立て・実行・ffprobe (寸法 / 尺)・同梱 FFmpeg の可用性確認 |

### 4.3 Timeline

| モジュール | 責務 |
|---|---|
| `model` | Timeline / Track / Clip / SubtitleClip / AudioClip / MediaRef / Transform |
| `project_io` | JSON の保存・読込・検証・移行 (`SCHEMA_VERSION = 1`)、種別判定 (clip / archive) |
| `builder` / `vertical_builder` / `archive/timeline_builder` | 編集点と認識結果から Timeline を組む (横 / 縦 / 複数クリップ) |
| `commands` | 全編集操作を Command として実装し CommandStack に積む (Undo/Redo) |
| `compositor` | クロップ・正規化・オーバーレイ・字幕・ぼかし・透かしの配置計算の唯一の置き場 |
| `renderer` | ベース連結 → 合成 → 焼き込みの 3 パスで書き出す。A/V 同期の実測対策を持つ |
| `proxy_render` / `gui/timeline/proxy_manager` | 確認用プロキシをタイル単位で作り、キャッシュする |
| `timemap` | 元動画時間 ↔ Timeline 時間の写像 (プレビューと字幕配置) |
| `media_recovery` / `media_sidecar` | 消えた中間ファイルの作り直しと、正規化済み音声の保存 |

### 4.4 サービス・基盤

| モジュール | 責務 |
|---|---|
| `services/api_client` | ベース URL 結合・JSON 送受信・Bearer 付与・401 で 1 回リフレッシュ・`ProblemDetails` の例外化 |
| `services/stretheus_auth` / `auth_store` / `device_id` | ログイン、JWT の DPAPI 保存、未ログイン端末の識別 |
| `services/points` | 出力 1 回につき予約 → 成果物完成で確定 / それ以外で取消。透かし要否はサーバーの応答のみ |
| `services/billing` | Stripe Checkout / カスタマーポータルの起動と加入状態の取得。カード情報は扱わない |
| `services/telemetry` → `log_payloads` → `log_queue` → `log_sender` | ログを積む / 本文を作る / ローカルキューに置く / 低優先度で送る |
| `utils/logger` | ファイル + コンソールのロガー (日付別・ローテート) |
| `utils/disk` / `workspace` / `trash` | 空き容量の判定、作業フォルダの残骸整理、ゴミ箱送り (SHFileOperationW) |
| `utils/updater` | GitHub Releases を見てインストーラを取得・起動 (失敗は静かにスキップ) |
| `i18n` | 日本語原文をキーとする `tr()`。`ui.language` で ja / en を切替 |
| `gui/theme` | デザイントークンの唯一の出どころ。QSS と自前描画の両方へ同じ値を供給 |

---

## 5. 主要クラス・データモデル

### 5.1 PipelineContext

工程間で受け渡す唯一の状態。

```
PipelineContext
 ├─ input_path / working_dir / output_path
 ├─ settings                    実行用にコピーした設定 (キャンバス解決済み)
 ├─ progress_callback(ratio, label) / total_steps / current_step
 ├─ begin_step() / end_step() / progress_subcallback
 ├─ current_video_path()        直前の工程が作った動画
 ├─ asr_audio_path() / build_asr_audio()   認識・プレビュー用の音声
 ├─ timeline / project_path / project_created_at
 ├─ output_profile              {is_portrait, orientation, width, height}
 ├─ project_id                  ログ送信の単位
 ├─ watermark_required          透かしを焼くか (サーバーの応答由来)
 └─ cleanup()                   中間ファイルの後始末
```

コールバック (GUI 経路のみ注入。未注入なら自動で進む):
`subtitle_review_callback` / `volume_analysis_callback` / `timeline_review_callback` /
`blur_failure_callback` / `watermark_confirm_callback` / `media_relink_callback`。

### 5.2 Timeline モデルとプロジェクト JSON

```
Timeline
 ├─ width / height / orientation      出力キャンバス
 ├─ tracks[]
 │    ├─ V1  ベース映像 (OP / 本編の残す区間 / ED)。最背面固定
 │    ├─ V2+ オーバーレイ映像・画像 (z_order)
 │    ├─ A1  V1 にリンクした音声 (独立した時刻を持たない)
 │    └─ S1.. 字幕 (役割別。既定 3 トラック / 最大 timeline.subtitle_tracks.max_tracks)
 ├─ source                            元動画・編集点メタ・ぼかし指定・crop 指定
 └─ 各 Clip: MediaRef (素材と素材内時刻) + Transform (位置・大きさ) + use フラグ
```

* プロジェクト JSON は `schema_version = 1`。未知の版は読まずに `TimelineError`。
  古い版は読込時に移行する。
* 種別はファイル名の接尾辞で決まる (`timeline.project.archive_suffix` = `.archive`)。
  クリップ用とアーカイブ用は復旧も書き出しも異なるため、取り違えはここで弾く。
* 追従ぼかしの結果は重いので JSON へ入れず、`blur/store.py` が別ファイルへ持つ
  (Undo のたびの deepcopy を避けるため)。

### 5.3 テロップの文字色の決定順 (`timeline/subtitle_style.py`)

焼き込み・プレビュー・Resolve 出力がすべてこの 1 か所から引く (式を 3 か所へ書くと
「プレビューだけ色が効く」食い違いが起きるため)。

1. **クリップ個別**の色 (`SubtitleClip.color`) — 利用者が 1 本だけ変えた色
2. **トラックの既定色** (`Track.color`) — トラックヘッダの色見本で決めた色 (ver9 resolve13)
3. **役割の色** (`subtitle.own_subtitle_color` / `sub_subtitle_color` / `comment_subtitle_color`)
4. フォールバック (配信者色)

役割そのものは**載っているトラックが決める**一方通行で、編集画面の「役割」コントロールは
既定で出さない (`timeline.subtitle_tracks.role_control_enabled = false`)。
フォント・サイズ・配置は `subtitle.*` を既定とし、縦動画では `vertical.*` が上書きする。

---

## 6. setting.json 定義

### 6.1 原則

* 可変値はすべて `src/settings/setting.json`。**既定値の唯一の出どころは
  `settings_window.DEFAULT_SETTINGS`** であり、`setting.json` だけに書いた新キーは
  `load_settings → save_settings` で消える。新しい設定を足すときは必ず両方を直す。
* `load_settings(persist_new_keys=True)` が起動時に欠落キーを既定で補完し、一度だけ書き戻す。
* 設定画面に出さない項目がある (モデルのパス・しきい値・互換フラグなど)。
  それらは `setting.json` を直接編集して調整する。

### 6.2 セクション一覧 (全 19)

| セクション | 内容 | 主な利用者 |
|---|---|---|
| `general` | 入出力フォルダ、OP/ED とその有効フラグ、自動更新、作業フォルダ、必要空き容量、残骸の掃除 | 全経路 |
| `subtitle` | 言語・役割色・フォント・折り返し・ASS スタイル・コメント装飾・Whisper パラメータ・テロップ分割結合・語彙/置換・話者照合 (約 100 キー) | 認識〜焼き込み |
| `silence_cut` | 有効/無効、`mode` (`edit_points` / `physical`)、閾値、最小無音長、フェード、抽出方式 | 無音カット |
| `volume_analysis` | 解析の有無・測り方 (`metric` / `percentile`)・確定カット閾値 `last_cut_db` | 閾値確認 |
| `loudness` | 目標 I / TP / LRA、2 パス、出力の検証と補正 | 正規化 |
| `preview` | 字幕プレビュー (尺・寸法・プリセット・音声) | 字幕一覧画面 |
| `timeline` | 編集画面の全設定 (プロジェクト・トラック・ショートカット・プレビュー・プロキシ・波形・UI 寸法・ヘルプ) | Timeline |
| `ffmpeg` | 実行ファイルのパス、コーデック、出力寸法/FPS、品質プロファイル、進捗タイムアウト | 全 FFmpeg 実行 |
| `vertical` | 縦動画のキャンバス・字幕配置・切り抜き (crop) の既定 | 縦動画 |
| `logging` | ログの出力先とレベル | 全体 |
| `archive` | 取得 (twitch-dl)・認証・採点 (方式 A の重み)・マーカー・区間追加・見積り・クリップ処理・結合・イントロカード | 経路 B |
| `export` | DaVinci Resolve 出力 (形式・字幕モード・座標系) | Resolve 出力 |
| `api` | Stretheus API のベース URL とタイムアウト | services |
| `watermark` | 透かしの大きさ・不透明度・位置 | 出力 |
| `points` | 残高の更新間隔・サブスク状態のキャッシュ | ポイント |
| `billing` | 課金設定のキャッシュ・反映待ちのポーリング | サブスク |
| `telemetry` | ログ送信の有無・保持日数・キュー上限・送信間隔 | ログ送信 |
| `blur` | 機能の有効/無効、UI 表示、検出モデル、追従、描画、編集画面 | ぼかし |
| `ui` | 言語・テーマ・アクセント・ガラスの不透明度・背景・メイン画面の寸法 | 画面全体 |

### 6.3 経路の分岐に効く代表キー

| キー | 既定 | 効果 |
|---|---|---|
| `timeline.enabled` | `true` | false で Timeline を作らず従来フローへ |
| `silence_cut.mode` | `edit_points` | `physical` で実カット (従来フロー) |
| `silence_cut.enabled` | `true` | false で無音カットと閾値確認をスキップ |
| `subtitle.enabled` | `true` | false でテロップ工程をスキップ |
| `subtitle.review_enabled` | `true` | 従来フローで字幕一覧画面を出すか |
| `subtitle.engine` | `whisper` | `none` または未導入で認識をスキップ (全体は継続) |
| `archive.clip_pipeline.*` | — | 切り抜きで無音カット / 字幕レビュー / 音量ダイアログを通すか |
| `archive.combine.enabled` | `true` | クリップを 1 本へ結合するか |
| `blur.enabled` / `blur.ui_enabled` | `true` / `false` | 機能そのものと、編集画面からの指定操作 |
| `export.resolve.enabled` | `true` | Resolve 出力ボタンを出すか |
| `telemetry.enabled` / `send_errors` | `true` / `false` | ログ送信の範囲 |

### 6.4 互換性方針

* `_merge_with_defaults` は**セクション単位の浅いマージ**で、既存のユーザー値を温存する。
  入れ子のキーは各 `config.py` が欠落時に既定へ落とす。
* 既定値を変えたいが既存利用者の値を温存したくない場合は、
  **移行フラグ**を足して 1 回だけ書き換える (`metric_migrated` / `ripple_delete_migrated` /
  `blackout_migrated` / `legacy_cache_checked` など)。
* 既存キーの意味・型は変えない。意味を変えるときは新しいキーを足す。

---

## 7. 外部連携

### 7.1 Stretheus API

* ベース URL は `api.base_url` のみが出どころ (リリース時に本番へ戻すことを確認する)。
* 認証は OAuth 認可コードフロー (Twitch / Google)。未ログイン利用者も
  `POST /api/auth/device` で匿名 JWT を得てポイント制の対象になる。
* JWT は `%LOCALAPPDATA%\Stretheus\auth.dat` へ **DPAPI (CurrentUser)** で保存する。
  他端末へコピーしても復号できない。トークンはログへ出さない。
* 401 (`reauth_required`) はリフレッシュで回復しないため、呼び出し側がログインを促す。
  5xx / 接続失敗は `ApiOfflineError` として**機能を止めずに縮退**する。

主なエンドポイント: `/api/auth/*`、`/api/points`、`/api/points/reservations[/{id}/commit|cancel]`、
`/api/billing/{config,checkout-session,portal-session,subscription}`、
`/api/analytics/{sessions,projects,processing-logs,export-logs,events}`、`/api/errors`。

### 7.2 ポイントと透かし

* 出力操作 1 回につき `reserve` → 成果物ができたら `commit` → 失敗・中断・キャンセルは `cancel`。
* **透かしを入れるかはサーバーの応答だけで決める** (残高から推測しない)。
  透かしが入ると分かった時点で開始前に確認し、やめるなら消費せず中断する。
* 出力本数が何本でも、1 回の操作に対する予約は 1 つ (アーカイブ切り抜きを含む)。

### 7.3 ログ送信

* アプリ各所が呼ぶのは `telemetry` だけで、ここは**ネットワークも認証も触らない**。
  ファイルを 1 本書くだけなのでオフラインでも必ず成功する。
* 送信は `log_sender` が低優先度で行う (利用者の操作を妨げない / 停止要求に 1 件ごとに応じる)。
* 検証に通らない本文は**積む前に捨てる**。個人情報はマスクする。

### 7.4 Twitch

* `twitch_auth` が OAuth で利用者アクセストークンを得て、対象 VOD が本人のものか判定する
  (`archive.auth.owner_only`)。
* VOD とチャットの取得は `twitch-dl` に一本化 (`download` / `chat json`)。
  同梱 CLI → PATH → `python -m twitchdl` の順に解決する。
* ストリームマーカーは Helix から取得し、採点には混ぜず「必ず残す区間」として合流させる。

### 7.5 自動更新

`general.auto_update_check` が true なら起動時に GitHub Releases を確認し、新版があれば
インストーラを取得して起動する (通信は `urllib` のみ、失敗は静かにスキップ)。

---

## 8. エラー処理方針

### 8.1 例外階層 (`src/exceptions.py`)

```
AutoEditError
 ├─ ConfigError            設定不整合
 ├─ InputError             入力動画・プロジェクトが無い / 読めない
 ├─ FFmpegError            command / stderr_tail / returncode を保持
 ├─ DiskSpaceError         path / free / required を保持
 ├─ SubtitleError          テロップ生成
 ├─ PipelineCancelled      利用者の意図による中断 (異常終了ではない)
 ├─ ExportError            Resolve 出力固有の失敗
 ├─ TwitchError            ログイン / 所有判定 / twitch-dl
 ├─ TimelineError          モデル・プロジェクト JSON・編集の前提違反
 └─ ApiError               status / code / retry_after を保持
      ├─ ReauthRequiredError   401。再ログインが必要
      └─ ApiOfflineError       到達不能。オフライン扱いで縮退
```

### 8.2 方針

| 区分 | 方針 |
|---|---|
| 入力不正 | 工程へ入る前に ffprobe で読めるかを確かめ、読めない理由を名指しして中断 |
| 容量不足 | 作業先と出力先の両方を開始前に確認。足りなければ `DiskSpaceError` で即中断 (握り潰さない) |
| FFmpeg 失敗 | 戻り値 != 0 を `FFmpegError` にし、コマンドと stderr 末尾をログへ |
| OP/ED 未設定 | 例外にせずスキップして継続 |
| 正規化・字幕・プロジェクト保存の失敗 | 記録して**続行**する (出力が作れなくなる失敗ではない) |
| 利用者のキャンセル | `PipelineCancelled`。ポイントを戻し「中断」として記録する |
| API 到達不能 | `ApiOfflineError` として縮退。編集・書き出しは止めない |
| 想定外例外 | 最上位で捕捉・記録して再送出。GUI は対象ファイルと理由を添えて表示する |
| 中間ファイル | 成否に関わらず `finally` で後始末。異常終了の残骸は次回起動時に `utils/workspace` が掃除 |

---

## 9. ログ出力方針

* ライブラリは Python 標準 `logging` のみ。
* 出力先: コンソール + `<logging.log_dir>/autoedit_YYYYMMDD.log`
  (5MB x 5 世代でローテート)。`log_dir` は**カレントディレクトリ相対**のため、
  配布時はショートカットの作業フォルダか `logging.log_dir` を運用に合わせる。
* 形式: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
* レベルは `logging.level` (既定 INFO)。FFmpeg コマンド全文は DEBUG。
* 例外は `logger.exception` でスタックトレースごと残す。
* 工程ごとの所要は `utils/stage_timer` が書き出しの最後に 1 行でまとめる。
* サーバーへ送るログ (§7.3) とローカルログは別系統である。

---

## 10. 多言語化

* `i18n.tr("日本語原文", **kwargs)` が唯一の表示文言の入口。**日本語の原文そのものがキー**で、
  英語表示のときだけ `catalog_en` を引く。
* 原文を 1 文字でも変えたら `catalog_en` も直す。`i18n.missing_keys()` で未対訳を確認できる。
* 対応言語は `ja` / `en` (`ui.language`)。メイン画面右上で切り替えると画面を作り直さずに反映する。

---

## 11. 配布・リリース

* PyInstaller の **onedir** で `Stretheus.exe` を作り (`src/main_window.spec`)、
  Inno Setup のダウンローダ型インストーラ (`installer/AutoEdit.iss`) で配る。
* バージョンは `src/version.py` と `installer/AutoEdit.iss` の `MyAppVersion` を**必ず一致**させる。
* FFmpeg / ffprobe は同梱する (PATH 非依存)。CUDA ランタイムは同梱しない (CPU 実行に統一)。
* 同梱物のライセンス表記は `tools/collect_licenses.py` で `licenses/` を作り直して同梱する。
* 詳細手順は `docs/HowToRelease.md` と `installer/README.md` (後者が正典)。

---

## 12. テスト方針

* フレームワークは**標準 `unittest`** のみ。実行は `python -m unittest discover -s tests`。
* ネットワーク・GUI の実操作・実際の FFmpeg 実行には依存させない
  (API クライアントや時間待ちは差し替え、FFmpeg はコマンド組み立ての検証に留める)。
* テストは「どの要望・どの障害に対する振る舞いか」をファイル先頭のコメントに書く。
* 設計とテストの対応、および不足している観点は `docs/design/test_matrix.md` を参照。

---

## 13. 既知の未解決事項

| # | 項目 | 現状 |
|---|---|---|
| 1 | `cut_region` のフレーム精度 | 切り出しは `-c copy` のためキーフレーム境界に丸められる。後回しと判断済み |
| 2 | 「TOP5」という呼称 | 既定は `archive.scoring.top_n = 10`。コード内コメントと一部履歴文書に TOP5 の呼称が残る |
| 3 | `subtitle.broadcast_style` | 初版設計にあった「ソロ / 複数人」は現行の設定に存在しない。役割色 + 話者照合が代替している |
| 4 | 設定画面に出ない設定 | モデルのパス・互換フラグなど、`setting.json` を直接編集する項目がある |
| 5 | `docs/third_party_licenses.md` | ver5 resolve2 §5.10 で計画されたが未作成。現状は `licenses/` と `tools/license_manifest.json` が実体 |

---

## 付録 A. 初版設計 (2026-05-31) からの主な変更

| 初版の記述 | 現行 |
|---|---|
| 4 工程 (無音カット → テロップ → OP → ED) の 1 経路のみ | 3 経路 (1 本編集 / アーカイブ切り抜き / 再開) + 縦動画 + Resolve 出力 |
| `modules/` 7 ファイル構成 | `pipeline` / `modules` / `timeline` / `archive` / `blur` / `export` / `services` / `gui` / `i18n` / `utils` の 10 パッケージ 140 ファイル |
| 無音カットは常に実カット | 既定は**編集点方式** (実カットせず Timeline の編集情報として持つ) |
| テロップは生成して焼くだけ | Timeline 編集画面で位置・色・役割・分割結合まで編集できる |
| 例外 4 種 (+ 基底 `AutoEditError`) | 12 種 (+ 基底 `AutoEditError`)。容量・キャンセル・API・Twitch・Timeline・Export を追加 |
| 設定 5 セクション | 19 セクション。既定値の出どころは `DEFAULT_SETTINGS` |
| GUI は「`settings_window.py` に実行ボタンを追加」 | `gui/main_window.py` が独立したメイン画面 (2 タブ構成) |
| `ffmpeg.executable = "ffmpeg"` (PATH 依存) | 同梱 `ffmpeg/ffmpeg.exe` を既定とし PATH に依存しない |
| 不明点: `run_ffmpeg_progress` の実体が不明 | `utils/progress.py` で実装済み。全 FFmpeg 実行が経由する |
| 不明点: 出力ファイル命名 / 一時ファイル配置 | `<元名>_edited.mp4` (衝突時連番) / `general.work_directory` (空なら %TEMP%) で確定 |
| サーバー連携なし | ログイン・ポイント・サブスク・ログ送信・自動更新を追加 |
