# AiEditor resolve1 — AI Clip Studio (AI 動画制作機能) の設計

要望: `docs/AiEditor/request1.md` (2026-11-20)
前提: `docs/design/design.md` (現行設計書。経路 A/B/C・Timeline モデル・設定方針)
　　　`docs/request/ver2/resolve17.md` (方式 A 採点) / `docs/request/ver3/resolve.md` (Timeline 本体)
　　　`docs/request/ver12/resolve1.md` (SE) / `resolve2.md` (テロップアニメーション)
　　　`docs/security/sec2.md` `sec3.md` (配布物から読み取れる情報 = 鍵を同梱できない根拠)
　　　`docs/CLAUDE.md` (実装前に設計 / ハードコード禁止 / 既存実装を破壊しない / 不要なライブラリを追加しない)
対象: `src/ai/*` (新規) / `src/pipeline/*` / `src/archive/*` / `src/timeline/*` /
　　　`src/settings/*` / `src/exceptions.py` / `tests/*`
　　　+ **別リポジトリ `StretheusAPI`** (`/api/ai/*` の実装 / §6.2 / §22.2 T3)
状態: **未実装 (設計のみ)**。**2026-11-20 の利用者回答 (Q1〜Q12) を反映済み** (§22)。
　　　画面は変更しない (§18)

---

## 0. 先に結論

**既存に無いのは「判断」だけである。素材取得・解析・文字起こし・編集・合成・出力はすべて既存にある。**
したがって新規開発は次の 3 点に絞る。

1. **`src/ai/` 1 パッケージ**(Qt 非依存・既存を一切変更しない層)。
2. **AI と既存編集エンジンの境界を `EditPlan` という JSON 1 枚に固定する。**
   これだけがモデル非依存の契約であり、**使うモデルを差し替えても既存エンジン側は 1 行も変わらない**
   (要望 §6.4)。モデルは Claude を使い、**役割ごとに使い分ける** (回答 Q1 / §6.3)。
3. **新しい実行経路 D「AI Clip Studio」** を `pipeline/` へ足す。経路 B (アーカイブ切り抜き) の
   採点・前処理をそのまま呼び、最終工程 (Timeline レンダリング → 出力) は経路 A/B/C と共有する。

採用する重要な判断は次の 4 つ。

| # | 判断 | 理由 |
| --- | --- | --- |
| (1) | **EditPlan は Timeline を直接組まず、既存の `timeline/commands.py` を叩いて組む** | 検証・Undo/Redo・保存・自動保存・プレビュー一致が無料で付く。AI の出力が「人間の編集操作と同じ道」を通るため、§14 の差分解析が**同じ語彙**で書ける |
| (2) | **AI へ渡す時間座標は常に「元 VOD の秒」に正規化する** | 現状は VOD 時間 / 切り出し素材時間 / Timeline 時間の 3 系統があり (`archive/cut_origin.py` / `timeline/timemap.py`)、ここを曖昧にすると「AI の指定した 1250.5 秒」がどこを指すか決まらない |
| (3) | **全長の文字起こしはしない。候補区間だけ文字起こしして AI へ渡す** | 現状の文字起こしはクリップ単位 (`archive/clip_writer.py:634`) であり、全長 ASR は実測コストが読めない。候補 (既定 30 件 × 数分) に絞れば現行の切り抜きと同程度で収まる |
| (4) | **学習 (要望 §12) は「保存済みプロジェクト JSON の差分」から始める。新しい操作ログ機構を先に作らない** | 編集操作の履歴は永続化されていない (`timeline/commands.py:357` の `CommandStack` はメモリのみ) が、**人間が作った完成プロジェクトは既に資産として残っている** (`timeline/project_io.py:527 scan_projects`)。これを読むだけで Phase 1 が成立する |
| (5) | **AI の呼び出しは Stretheus API が代行する。クライアントへ鍵も SDK も置かない** (2026-11-20 回答 Q2) | 鍵を同梱できない (sec2 / sec3) うえ、ポイント制で負担を減らすなら利用量の判定はサーバーが持つしかない。クライアントは既存 `services/api_client.py` の上に乗るだけで**追加ライブラリが 0**になる |
| (6) | **役割ごとにモデルを使い分ける** (回答 Q1)。構成は Opus、量の多い評価は Sonnet、分類は Haiku | 判断の質が効く工程と、量で効く工程が分かれている。ただし**プロンプトキャッシュはモデル単位**のため、同じ入力を共有する工程は 1 モデルへ寄せる (§6.3〜§6.4) |

MVP (要望 §9.1) の範囲は **「候補抽出 → AI 評価 → AI 構成 → EditPlan → Timeline → 既存レンダリング → 利用者確認」** まで。
品質レビュー (§12)・レビュー PDF (§12.6)・自動修正・学習 (§13〜§15)・投稿分析は後続とする。

**画面は作らない** (回答 Q12)。AI を含む編集結果は既存の Timeline 編集画面にそのまま出る (§18)。

---

## 1. 現状 — 既に在るもの / 無いもの

### 1.1 要望の各領域に対して既存が何を返すか (調査結果)

| 要望の領域 (§2) | 既存の担い手 | 入口 | 戻り値・成果物 |
| --- | --- | --- | --- |
| VOD 取り込み | `archive/twitch_source.py` (twitch-dl ラッパ) | ログイン → 所有判定 → download / chat json | ローカル mp4 + チャット JSON |
| チャットの正規化 | `archive/comment_source.py:127 normalize_comments` | 生 JSON | `[{"offset_sec", "user", "text", "emotes"}]` (昇順) |
| 音声の全長解析 | `archive/features.py:279 extract_cells` | 動画 + `slide_sec` | `duration`, セル列 `[{"start","end","max_db","mean_db","silence_ratio",…}]`, `audio_scan` |
| コメントのセル集計 | `comment_source.py:170 aggregate_into_cells` | 正規化コメント + セル | セルへ `w_count` / `laugh_word_count` / `laugh_emote_count` / `comment_count` を加算 |
| 基礎的なシーン抽出 | `archive/scoring.py:54 score_window` / `:204 select_top_events` | セル列 | 窓スコア列 `curve` と候補 `clips[{"index","start","end","score","emotion","comment","marker",…}]` |
| 採点の総括 | `archive/pipeline.py:18 analyze` | 動画 + 設定 + コメント + マーカー | `{"duration","curve","clips","audio_scan"}` |
| 区間切り出し | `archive/clip_writer.py:77 cut_region` | VOD + 開始/終了 | 切り出し素材 (ストリームコピー) |
| 素材 → VOD の座標戻し | `archive/cut_origin.py:47 measure` | 素材 + 要求位置 | 切り出しの原点 (秒)。**足し算だけでは最大 1 GOP ずれる** |
| 正規化 / 無音検出 | `modules/loudness_normalizer.py` / `modules/silence_cutter.py` | 素材 | EBU R128 済み素材 / 残す区間 (編集点) |
| 文字起こし | `modules/subtitle_generator.py:2739 recognize_for_timeline` | 素材 + 残す区間 | `[{"start","end","text"(,"words")}]` (素材時間) |
| Timeline 構築 | `timeline/builder.py:867 build` / `archive/timeline_builder.py:72 build_archive_timeline` | 編集点 + 認識結果 | `Timeline` (V1/A1/S1..) |
| 編集操作 | `timeline/commands.py` (36 コマンド) | `CommandStack.push` | Undo/Redo 付きのモデル変更 |
| 検証 | `timeline/project_io.py:390 validate` | Timeline | 素材・編集点・クリップ・音声リンク・重なりの検査 |
| 合成・字幕焼き込み・出力 | `timeline/compositor.py` / `timeline/renderer.py:50 render` | Timeline + Context | 出力 mp4 |
| 1 区間の取り直し | `archive/vod_range.py:203 fetch_range` | video_id + 区間 | 必要な区間だけの素材 (`.ts` / fMP4 両対応) |
| 保存・再開 | `timeline/project_io.py` (`SCHEMA_VERSION = 1`) | プロジェクト JSON | 保存 / 読込 / 移行 / 一覧 / サムネイル |
| ポイント・課金・ログ送信 | `services/*` | `PointsService` / `telemetry` | 予約→確定、分析ログ |

**要望 §2 の表で「既存 Stretheus」と書かれた領域は、上記のとおり全て実装済みである。**
新規開発はこの表に**無い列だけ**でよい。

### 1.2 無いもの (= 本当に作るもの)

| # | 無いもの | 現状 |
| --- | --- | --- |
| (1) | **会話内容を見る判断** | 採点は音量・無音率・ｗ数・笑い語・エモート・コメント急増だけ (`scoring.py:54`)。テキストは採点に 1 文字も入っていない |
| (2) | **動画の企画・構成** | 候補は常に**時系列順**に並ぶ (`scoring.py:204` の `chosen.sort(key=start)`)。並べ替え・役割付け・導入/オチの概念が無い |
| (3) | **プラットフォーム別の作り分け** | 縦は「横 Timeline のクリップを縦キャンバスへ移す」だけ (`timeline/vertical_builder.py`)。構成は横のまま |
| (4) | **外部 AI への通信経路** | HTTP は `services/api_client.py:38` の Stretheus API 用 1 本のみ。標準ライブラリ (`urllib`) 実装で、AI プロバイダーの口は無い |

### 1.3 要望と現状のズレ (設計で吸収する事実)

| # | 事実 | 設計への反映 |
| --- | --- | --- |
| Z1 | 文字起こしは**クリップ単位**。全長 ASR は存在しない | §7.1 候補を広く取り、**候補だけ**文字起こしする二段構え |
| Z2 | 時間座標が 3 系統 (VOD / 切り出し素材 / Timeline) | §9.2 EditPlan の時刻は**必ず VOD 時間**。変換は `cut_origin` と `TimeMap` だけが行う |
| Z3 | `cut_region` はストリームコピーのため切り出し頭が最大 1 GOP (≒2 秒) 手前にずれる | §10 検証で原点を実測し、EditPlan の時刻は**素材側で解決してから**使う (フレーム精度の課題そのものは後回し。`docs/README.md` の既知事項と同じ扱い) |
| Z4 | 編集操作の履歴は永続化されない (`CommandStack` はメモリ) | §13・§14 はプロジェクト JSON の差分で成立させる。操作ログ (§15) は任意・後付け |
| Z5 | トランジション・映像エフェクトという概念が Timeline に無い (あるのは crop / zoom / ぼかし / 透かし / オーバーレイ / テロップアニメ / SE) | §9.3 EditPlan は**既存が受け取れる項目だけ**を持つ。要望 §6.3 のうち未対応項目は §19 対象外へ明記 |
| Z6 | 配布物からは文字列が読める (sec2 / sec3) | §6.1 AI の鍵は**クライアントへ置かない**。呼び出しは Stretheus API 経由 |
| Z7 | 追加ライブラリは原則入れない (非機能要件) | §6.1 クライアントは既存 `ApiClient` (`urllib`) のまま。公式 Anthropic SDK を使うのは**サーバー側 (別リポジトリ)** |

---

## 2. 方針の比較

### 2.1 AI 層の置き場所

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | `archive/` の中へ AI 判断を足す | **不採用**。`archive/` は「採点して切り出す」責務で既に 20 モジュールある。AI の失敗・コスト・鍵管理が採点経路へ混ざり、`archive.enabled` 相当の切り分けができない |
| B | **`src/ai/` を新設し、経路 D として `pipeline/` から呼ぶ** | **採用**。既存は 1 行も変えずに動く。`ai.enabled=false` で丸ごと休止でき、「既定で軽い」(非機能要件) を守れる |
| C | 外部ツール / 別プロセスにする | **不採用**。設定・ログ・ポイント・i18n・進捗の全てを二重に持つことになる |

### 2.2 EditPlan の適用先

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | `Timeline` オブジェクトを AI 層が直接組む | **不採用**。`builder.py` と同じ構築規約を 2 か所に書くことになり、「OP/ED の配置条件は `concat_processor.run()` と完全に同一」(builder.py 冒頭) という保証が崩れる |
| B | **既存 `commands.py` を `CommandStack.push` 経由で叩いて組む** | **採用**。トリム・移動・分割・テロップ追加・SE 追加・音量が既存コマンドのまま効き、Undo/Redo と検証が付く。人間の修正と AI の編集が**同じ操作語彙**になり §14 が成立する |
| C | 採点結果の `clips` 配列へ流し込むだけ | **不採用**。並べ替え・テロップ・SE を表現できない (要望 §5 が満たせない) |

> 初回の骨組み (V1 / A1 / S1) だけは既存 `builder.build` / `archive/timeline_builder.build_archive_timeline` に作らせ、
> **そのあとの「AI の意思」をコマンドで載せる**。ゼロから組まないのが要点である。

### 2.3 AI の呼び出し口と鍵 (回答 Q2 により確定)

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | 利用者が自分の API キーを入れ、`%LOCALAPPDATA%` に DPAPI 保管 | **開発用のみ**。`services/auth_store.py:80 protect` / `:84 unprotect` をそのまま使えるので、サーバーが未実装の間の検証経路として残す (既定では使わない) |
| B | **Stretheus API が AI 呼び出しを代行し、ポイントで負担する** | **採用**。鍵を配らず利用量を一元管理でき、利用者は鍵を用意しなくてよい。サーバー側 (`StretheusAPI`) の実装が必要 (§22 T3) |
| C | アプリへ鍵を同梱 | **不採用**。`docs/security/sec2.md` / `sec3.md` のとおり配布物から文字列は読める。即失効する |

> 案 B を採ると、クライアントは**追加ライブラリ 0** で済む (既存 `ApiClient` の上に乗るだけ)。
> 公式 Anthropic SDK はサーバー側に入る。これは `docs/CLAUDE.md`「不要なライブラリを追加しない」と、
> 「Claude を叩くなら公式 SDK」という作法の両方を同時に満たす唯一の形である。

### 2.4 学習 (要望 §12) の入口

| 案 | 内容 | 判定 |
| --- | --- | --- |
| A | 操作ログ機構を先に作り、以後の操作を全部記録する | **不採用 (先行させない)**。過去資産が使えず、データが溜まるまで何も学べない |
| B | **保存済みプロジェクト JSON (人間が作った完成品) を解析する** | **採用 (Phase 1)**。`project_io.scan_projects` で既存プロジェクトが列挙でき、カット位置・尺・テロップ・アニメ・SE・構成は全部 JSON に入っている |
| C | B に加えて操作ログを任意で足す | **採用 (Phase 2 以降 / §15)**。`CommandStack` に観測口を 1 つ足すだけ。既定オフ |

---

## 3. 対策一覧

| # | 内容 | 置き場 |
| --- | --- | --- |
| **R1** | `src/ai/` パッケージと依存の向き | `src/ai/` (新規) |
| **R2** | 解析データの集約 (`AnalysisBundle`) と候補だけの文字起こし | `ai/context_builder.py` |
| **R3** | AI 呼び出しの口 (Stretheus API 経由 / 鍵はサーバー) とモデルの使い分け | `ai/provider.py` / `ai/remote.py` + **`StretheusAPI` 側** |
| **R4** | 面白シーン抽出 3 段階 (候補 → 評価 → 統合・ランキング) | `ai/scene_finder.py` |
| **R5** | 動画企画・構成 (`VideoPlan`。プラットフォーム別) | `ai/planner.py` |
| **R6** | **`EditPlan` スキーマ (AI と既存エンジンの契約)** | `ai/plan_schema.py` |
| **R7** | EditPlan の検証 (素材・範囲・整合性) | `ai/plan_validator.py` |
| **R8** | EditPlan → Timeline 適用 (既存コマンド経由) | `ai/plan_applier.py` |
| **R9** | 経路 D のオーケストレーション | `pipeline/ai_runner.py` |
| **R10** | 品質レビュー (5 役) と自動修正の上限 | `ai/reviewer.py` |
| **R11** | 人間の編集の解析と編集プロファイル | `ai/edit_analyzer.py` / `ai/profile.py` |
| **R12** | AI 編集 / 人間修正の差分解析 | `ai/plan_diff.py` |
| **R13** | 操作ログ (任意・既定オフ) | `timeline/commands.py` の観測口 + `ai/edit_log.py` |
| **R14** | 設定 `ai` セクション / 例外 / ログ / ポイント / テレメトリ | `settings/settings_window.py` / `exceptions.py` / `services/*` |
| **R15** | **動画全体のレビュー PDF** (SE の提案を含む / 回答 Q9) | `ai/review_report.py` + PDF 書き出し (§12.6) |

---

## 4. R1 パッケージ構成と依存の向き

```
src/ai/
├── config.py            ai セクションの読み出し (欠落キーは既定へ)
├── provider.py          呼び出しの抽象 (StretheusProvider / mock) (R3)
├── remote.py            Stretheus API 経由の呼び出し (既存 ApiClient の上) (R3)
├── key_store.py         開発用: 直接呼び出しの鍵を DPAPI 保管 (既定では使わない / R3)
├── prompts.py           役割ごとのプロンプト雛形 (Python 定数。文言は i18n 対象外)
│                        (実装では .md を同梱しない。main.spec の datas を触らずに済み、
│                         プロンプトは日本語固定のため外に出す利点が無い / §21 の 9 番)
├── context_builder.py   AnalysisBundle の組み立て (R2)
├── scene_finder.py      面白シーン抽出 3 段階 (R4)
├── planner.py           企画・構成 (R5)
├── plan_schema.py       EditPlan / SceneEval / VideoPlan / Review の形と既定 (R6)
├── plan_validator.py    検証 (R7)
├── plan_applier.py      Timeline への適用 (R8)
├── reviewer.py          品質レビューと修正指示 (R10)
├── review_report.py     レビュー PDF の本文組み立て (Qt 非依存 / R15)
├── edit_analyzer.py     保存済みプロジェクトの解析 (R11)
├── profile.py           編集プロファイルの読み書き・更新履歴 (R11)
├── plan_diff.py         AI 版 / 人間修正版の差分 (R12)
├── edit_log.py          操作ログ (任意 / R13)
└── store.py             学習データ・プロファイルの置き場とユーザー分離
```

依存の向き (`docs/design/design.md` §2.2 の図へ 1 層足す):

```
        [ gui/*  (PySide6) ]
                │ 呼ぶ
                ▼
  [ pipeline/* ] [ archive/* ] [ export/* ]      オーケストレーション
                │                 │
                │                 ▼
                │          [ ai/* ]              AI の判断 (Qt 非依存)
                ▼                 │
  [ timeline/* ] [ modules/* ] [ blur/* ]  ◀────┘ ai は読む & コマンドを push するだけ
                │
                ▼
  [ services/* ] [ utils/* ] [ i18n ] [ settings ]
```

守る規約:

* **`ai/` は `gui/` を import しない** (Qt 非依存。CLI / テストから同じ判断を再現できる)。
* **`timeline/` と `modules/` と `archive/` は `ai/` を import しない** (一方通行)。
  これにより `ai.enabled=false` のとき AI 層は 1 行も読まれず、既存経路は現状と完全に同一になる。
* `ai/` は `archive/pipeline.analyze` などを**呼ぶ側**であり、採点アルゴリズムを再実装しない。
* 設定の読みは `ai/config.py` に閉じる (既存の `archive/config.py` / `blur/config.py` と同じ作法)。

---

## 5. R2 解析データの集約 (`ai/context_builder.py`)

### 5.1 `AnalysisBundle` — AI へ渡す唯一の入力

AI には**解析済みの構造化データだけ**を渡す。音声・映像そのものは送らない (コスト・容量・プライバシー)。

```
AnalysisBundle
 ├─ source        {"video_id", "title", "game", "duration_sec", "width", "height", "fps"}
 ├─ cells[]       {"start","end","max_db","mean_db","silence_ratio",
 │                 "comment_count","w_count","laugh_word_count","laugh_emote_count"}
 │                   ← archive/features.extract_cells + comment_source.aggregate_into_cells の結果
 ├─ curve[]       {"start","end","emotion","comment","total"}   ← scoring.score_window の結果
 ├─ candidates[]  {"index","start","end","score","emotion","comment","marker","marker_labels"}
 │                   ← scoring.select_top_events の結果 (VOD 時間)
 ├─ transcript[]  {"start","end","text"}        候補区間だけ。VOD 時間へ戻してある (§5.3)
 ├─ comments[]    {"offset_sec","user","text"}  間引き済み (§5.4)
 └─ profile       編集プロファイルの要約 (§13。無ければ None)
```

* `cells` / `curve` は**全長分**あるため、そのまま送るとトークンを食う。
  AI へ渡すのは §5.4 の縮約後の形であり、`AnalysisBundle` 自体は**ローカルの完全版**として保持する
  (検証・差分・ログで使う)。

### 5.2 既存呼び出しの順序 (再実装しない)

```
[入力] VOD (twitch_source) または ローカル長尺動画
  ▼
comment_source.load_comments / normalize_comments            ← 既存
  ▼
archive.pipeline.analyze(input, settings, cb, comments, markers)   ← 既存をそのまま 1 回
      戻り値 duration / curve / clips / audio_scan をすべて保持する
  ▼
候補の本数だけ ai.candidates.top_n へ差し替える (§7.1)
  ▼
候補ごとに clip_writer.cut_region → cut_origin.measure → _transcribe   ← 既存
  ▼
AnalysisBundle を組む
```

* `analyze` は `archive.scoring.top_n` (現行 10) を使うため、AI 用に広く取りたい場合は
  **`cfg["top_n"]` を上書きした設定コピーを渡す** (設定ファイルは書き換えない)。
  `archive/config.py:69 method_a_config` が読む値を実行用コピーで差し替える形にし、
  `docs/design/design.md` §6.1 の「実行時にだけ決まる値は保存しない」に従う。
* `audio_scan` は書き出しへ引き継ぐ全長 1 パス解析の結果である (`pipeline.py` の注記)。
  経路 D でも**同じ測定を 2 度流さない**ため、そのまま `clip_writer.analyze_vod` へ渡す。

### 5.3 候補だけ文字起こしする (Z1 への対処)

| 手順 | 内容 | 既存 |
| --- | --- | --- |
| 1 | 候補 `i` の区間 (前後 `ai.candidates.transcribe_pad_sec` 秒を足す) を切り出す | `clip_writer.cut_region` |
| 2 | 切り出しの原点を実測する | `cut_origin.measure` |
| 3 | 文字起こしする (正規化・無音カットは**しない**。テキストが欲しいだけ) | `clip_writer._transcribe` と同じ経路 |
| 4 | 認識結果の `start`/`end` に原点を足して **VOD 時間へ戻す** | 本モジュール |

* 手順 3 の設定は `ai.transcribe.*` で上書きできるようにする。
  **話者照合 (`speaker_filter`) は既存の `subtitle.speaker_filter` に従う** (実装時の判断)。
  当初は「AI 用に既定で有効」と書いたが、既定が `"off"` なのは**実測で成立しなかったため**
  である (ゲーム音が常時乗る VOD では声紋が話者を表さない / `docs/quality/resolve1.md` §12.6)。
  成立しない方法で候補を落とすほうが害が大きいため、AI 用に勝手に `"drop"` へ上げない。
  利用者が有効にしていれば AI 用の文字起こしにも効く。
* 候補が重なる場合は区間を統合してから 1 回だけ流す (`scoring._merge_time_sections` と同じ考え方)。
* 文字起こしに失敗した候補は `transcript` を持たないまま評価へ回す。
  **AI は「テキスト無し」を理由に落とさず、音量・コメントだけで評価する** (縮退)。

### 5.4 トークン予算と縮約

| 対象 | 縮約方法 | 設定キー |
| --- | --- | --- |
| `cells` | 候補区間に重なるセルだけ。±`context_pad_sec` 秒 | `ai.budget.cell_pad_sec` |
| `curve` | 候補の前後 N 窓だけ。小数 1 桁 | `ai.budget.curve_window_span` |
| `comments` | 候補区間内のみ。1 セルあたり `max_comments_per_cell` 件まで (ｗ・笑い語・エモートを含むものを優先) | `ai.budget.max_comments_per_cell` |
| `transcript` | 候補区間内のみ。連続する短文は結合 | `ai.budget.merge_transcript_gap_sec` |
| 全体 | 概算トークンが `ai.budget.max_input_tokens` を超えたら候補を分割して複数回に分けて評価する | `ai.budget.max_input_tokens` |

* **正確なトークン数はサーバーが数える** (`POST /api/ai/count`。サーバー側で Claude の
  `messages.count_tokens` を呼ぶ)。クライアントにトークナイザを入れない (依存を増やさない)。
  クライアント側の概算は「文字数 ÷ `ai.budget.chars_per_token`」で分割の判断にだけ使い、
  **ポイントの見積りと実績はサーバーの数値を正とする** (§17.3)。
* 時刻は常に**秒 (float, 小数 1 桁)**。`"12:34"` のような表記は使わない (パースの揺れを作らない)。
* `AnalysisBundle` の共通部分 (source / profile / 全候補の要約) は**プロンプトの先頭に固定**し、
  候補ごとの可変部分を後ろへ置く。プロンプトキャッシュは前方一致のため、この順序を崩すと
  キャッシュが効かなくなる (§6.4)。

### 5.5 送るものと送らないもの (プライバシー)

| 送る | 送らない |
| --- | --- |
| 数値の解析結果 (dB・無音率・スコア) | 音声・映像・サムネイル |
| 候補区間の文字起こし本文 | VOD 全長の文字起こし |
| コメント本文 (間引き済み) | **コメント投稿者名** (既定で落とす。`ai.privacy.include_comment_user=false`) |
| VOD タイトル・ゲーム名 | ファイルの絶対パス・端末名・利用者のメールアドレス・JWT |

* 送信本文のマスクは `services/log_payloads.py` と同じ考え方で `ai/` 側に持つ。
* 「どこへ何を送るか」を利用者が事前に確認できるよう、**初回の AI 利用時に 1 度だけ同意を取る**
  (`ai.consent.accepted_version`。文面が変わったら再確認)。
  実装は `ai/consent.py` (文面の組み立てまで) と `gui/archive_tab.py` (確認と保存) に分けた。
  文面は**設定から組み立てる**ため、実際の挙動と食い違わない
  (投稿者名を送る設定ならそう書き、学習が無効なら学習の行を出さない)。
  **同意しなければ AI を使わず、従来どおりの切り抜きで続行する** (機能ごと止めない)。

---

## 6. R3 AI 呼び出しの口 (`ai/provider.py` / `ai/remote.py`)

### 6.1 構成 — 呼ぶのはサーバー、鍵はサーバーだけが持つ

```
[クライアント] ai/scene_finder・planner・reviewer
                   │ 知っているのは AiProvider だけ
                   ▼
               ai/provider.py (抽象)
                   ├ StretheusProvider → ai/remote.py → services/api_client.ApiClient (既存 / urllib)
                   ├ DirectProvider     開発用。ai/key_store.py の鍵で直接叩く (既定では使わない)
                   └ MockProvider       テスト用。固定 JSON を返し、ネットワークへ出ない
                                            │  POST /api/ai/complete
[サーバー]                                  ▼
               StretheusAPI  → 公式 Anthropic SDK (anthropic) → Claude
                   鍵の保管 / モデルの選択 / 利用量の記録 / ポイント判定
```

| 決まり | 理由 |
| --- | --- |
| クライアントへ AI の鍵を置かない | 配布物から文字列は読める (sec2 / sec3) |
| クライアントへ AI の SDK を入れない | 「不要なライブラリを追加しない」。通信は既存 `ApiClient` の上に乗る |
| **モデル名の最終決定権はサーバー** | 価格・モデルの入れ替えに配布更新を伴わせない。クライアントの `ai.models.*` は**希望**として送り、サーバーが許可した範囲で解決する |
| 利用量 (トークン) の正は `response.usage` | クライアントの概算は分割判断にだけ使う (§5.4) |

> サーバー側 (`StretheusAPI`) の実装は本書の範囲外。必要なエンドポイントと本文の形だけ
> §6.2 で決め、実装は `StretheusAPI` 側の設計に委ねる (§22 T3)。

### 6.2 エンドポイントと本文 (クライアントが依存する契約)

| メソッド | パス | 用途 |
| --- | --- | --- |
| POST | `/api/ai/complete` | 1 回の判断を求める (JSON を返させる) |
| POST | `/api/ai/count` | 送る前のトークン見積り (ポイントの事前表示用) |
| GET | `/api/ai/config` | 使えるモデル・役割ごとの既定・1 回の上限・残ポイント換算 |

要求 (クライアント → サーバー):

```json
{
  "role": "scene_eval",
  "model_hint": "claude-sonnet-5-5",
  "project_id": "…",
  "system": "…",
  "payload": { "…": "AnalysisBundle の縮約" },
  "schema": { "…": "期待する JSON の形" },
  "cache_prefix_id": "vod-1234567890-bundle-v1",
  "max_output_tokens": 8192
}
```

応答 (サーバー → クライアント):

```json
{
  "result": { "…": "JSON そのもの" },
  "model": "claude-sonnet-5-5",
  "usage": { "input_tokens": 41203, "output_tokens": 2187,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 38950 },
  "points": { "charged": 0, "estimated": 3 },
  "stop_reason": "end_turn"
}
```

* **JSON は構造化出力で取る。** サーバーは Claude の `output_config.format` に `schema` を渡す。
  現行モデル (Claude Opus 5.5 / Sonnet 5.5) では `tool_choice` の強制 (`any` / `tool`) が
  400 になるため、「ツールを強制して JSON を得る」古い作法は使えない。
* `cache_prefix_id` は「同じ前置きを使い回す」という**クライアント側の意思表示**。
  サーバーはこれが同じ呼び出しに同じプロンプト前置きを使い、`cache_control` を掛ける (§6.4)。
* `stop_reason` が `refusal` のときは `result` が無い。クライアントは `AiPlanError` と同じ扱いで
  縮退する (安全側の拒否を再試行で押し通さない)。サーバー側の退避 (別モデルへの引き継ぎ) は
  サーバーの責務とする。
* リトライはタイムアウト / 5xx / レート制限のみ `ai.retry.max_attempts` 回、指数バックオフ。
  既存 `api_client` の `_is_busy` / `_retry_delay` と同じ考え方。
* `ApiOfflineError` (到達できない) は `AiOfflineError` へ変換し、**既存の採点結果へ縮退**する (§17)。

> **実装時の確定 (2026-11-20 / `src/ai/remote.py`)**
>
> サーバー側 (`StretheusAPI`) の実装に合わせ、**線の上の名前は camelCase** である
> (`requestId` / `modelHint` / `maxOutputTokens` / `usage.inputTokens` / `stopReason`、
> 列挙は `sceneEval` / `endTurn`)。上の JSON は本書を書いた時点の表記であり、
> 読み替えは `ai/remote.py` の対応表 1 か所に閉じている (クライアント内は snake_case)。
>
> あわせて、`StretheusAPI docs/AiEditor/resolve1.md` §10 の追補 3 点を実装した。
>
> | # | 追補 | 実装 |
> | --- | --- | --- |
> | A1 | 要求に冪等キー `requestId` を付ける | 呼び出しごとに採番。到達できなかった再送は**同じキー**、混雑・中断は**新しいキー** |
> | A2 | `complete` が 202 + `jobId` を返すことがある | `GET /api/ai/jobs/{id}` を `pollAfterMs` 間隔で見に行く (`ai.jobs.wait_sec` / `poll_min_ms`) |
> | A3 | `ai_invalid_output` (422) を縮退の対象に加える | 1 回だけ作り直し、2 回目は諦めて呼び出し側へ返す |
>
> 本書に無かったものを 3 つ足した。いずれも A2 のための器である。
>
> * `ai.endpoint.jobs` (`/api/ai/jobs`) / `ai.jobs.wait_sec` (900) / `ai.jobs.poll_min_ms` (1000)
> * `ai_job_timeout` — 受理された処理が待ち時間に収まらなかった。**投げ直さない**
>   (サーバー側で処理が続いており、送り直すと二重に払う)。縮退の対象とする
> * `ai_budget_exceeded` — `ai.cost.*` でクライアント側が打ち切った (サーバーの上限とは別)
>
> `ai_model_not_allowed` を受けたときは希望モデルを外して 1 回だけ送り直す
> (役割の既定で通る)。`ai_payload_too_large` は呼び出し側が入力を小さくして送り直す。

### 6.3 モデルの使い分け (回答 Q1)

Claude の現行世代の価格は次のとおり (第一者 API / 1M トークンあたり。**価格は変わるため、
確定値はサーバー側で持ち、クライアントへは `/api/ai/config` で伝える**)。

| モデル | ID | 入力 | 出力 | キャッシュ読み |
| --- | --- | --- | --- | --- |
| Claude Opus 5.5 | `claude-opus-5-5` | $4.00 | $20.00 | $0.20 |
| Claude Sonnet 5.5 | `claude-sonnet-5-5` | $2.00 | $10.00 | $0.10 |
| Claude Haiku 5.5 | `claude-haiku-5-5` | $0.10 | $0.50 | — |

役割ごとの既定の割り当て:

| 役割 (`role`) | モデル | effort | 理由 |
| --- | --- | --- | --- |
| `scene_eval` (候補 30 件をバッチで) | `claude-sonnet-5-5` | `medium` | **呼び出し回数が最も多い工程**。入力の大半は共通前置きでキャッシュが効く |
| `plan` (企画・構成) | `claude-opus-5-5` | `high` | 1 回しか呼ばないのに動画全体の出来を決める。ここを削ると他の工程の価値が消える |
| `review` の `critic` | `claude-opus-5-5` | `medium` | 面白さの判断は質が効く |
| `review` の `director` / `viewer` / `platform` | `claude-sonnet-5-5` | `medium` | 観点が明確で量もある |
| `profile` (傾向の言語化) | `claude-sonnet-5-5` | `medium` | 統計の要約。数値は機械が出している (§13.2) |
| `classify` (差分の分類・テロップの整形) | `claude-haiku-5-5` | `low` | 判断ではなく分類 |

決まりと注意:

* **`thinking` は `{"type": "adaptive"}` 固定** (または省略)。深さは `output_config.effort` で決める。
  `budget_tokens` は現行モデルでは 400 になる。`temperature` / `top_p` / `top_k` は送らない。
* Claude Opus 5.5 の `effort` の既定は `medium`、Sonnet 5.5 は `high` である。
  **既定に任せず役割ごとに明示する** (上の表)。
* 最上位の `claude-fable-5-2` ($8 / $40) は採らない。本件の判断は Opus 5.5 で足りる。
* **モデルを分けるとプロンプトキャッシュを共有できない** (キャッシュはモデル単位)。
  そのため `scene_eval` の全バッチは**同じモデルへ寄せる**。
* 割り当ては**測ってから確定する**。まず全工程を `claude-opus-5-5` の低 `effort` で 1 本通し、
  役割ごとの入出力トークンと結果の質を記録してから上の表を見直す
  (新世代の低 `effort` が旧世代の高 `effort` を上回ることがあり、1 モデルに寄せた方が
  キャッシュが効いて安くなる場合がある)。これは §22 T1 の測定項目とする。

### 6.4 プロンプトキャッシュ (効きが最も大きい節約)

* `scene_eval` は「共通の前置き (役割の指示 + `AnalysisBundle` の共通部分)」+「候補 8 件ぶんの可変部」で
  組む。前置きを**1 バイトも変えずに**30 件ぶんのバッチを回すと、2 回目以降の入力が
  キャッシュ読み (Sonnet 5.5 なら $0.10 / 1M) になる。
* キャッシュは**前方一致**なので、前置きに時刻・乱数・順序の揺れる JSON を入れてはならない。
  `AnalysisBundle` を JSON へ落とすときは**キーの順序を固定**する。
* 1 本の VOD を処理する間 (レビューの往復を含む) 同じ前置きを使うため、サーバー側では
  長めの保持 (`ttl: "1h"`) を選べるようにする。
* 効いているかは `usage.cache_read_input_tokens` で分かる。**0 が続いたら前置きが揺れている。**
  これをログの 1 行に残す (§17.2)。
* 将来の節約手段: `scene_eval` は即時性が低いので、サーバー側で Message Batches
  (非同期・50% の費用) へ回せる。MVP では同期で作り、必要になったら切り替える。

### 6.5 開発用の直接呼び出し (`ai/key_store.py`)

サーバーが未実装の間 (および開発端末) のための経路。**既定では使わない。**

* 置き場は `%LOCALAPPDATA%\Stretheus\ai_keys.dat`。
  `services/auth_store.py:80 protect` / `:84 unprotect` (DPAPI / CurrentUser) をそのまま使う。
* **`setting.json` には鍵を書かない。** 配布 `setting.json` に鍵欄を持たせない (sec2 N1 と同じ理由)。
* `ai.direct_mode = true` のときだけ有効。この経路を使うときは公式 SDK (`anthropic`) を
  **開発環境にだけ** 入れる (`requirements` の配布対象には入れない)。
* 鍵・プロンプト全文・応答全文は**ログへ出さない** (§17.2)。

---

## 7. R4 面白シーン抽出 (`ai/scene_finder.py`)

要望 §4 の 3 段階をそのまま実装する。

### 7.1 第 1 段階: 候補抽出 (既存の採点をそのまま使う)

* 計算は `archive/scoring.py` のまま。**変更しない。**
* 本数だけ分ける。切り抜き (経路 B) の `archive.scoring.top_n` = 10 は「出力するクリップ数」であり、
  AI の候補数とは別物である。経路 D は `ai.candidates.top_n` (既定 **30**) を使う。
* 取りこぼしを減らす追加の緩め方 (すべて設定で、既定は現行と同じ挙動):

| 緩め方 | キー | 既定 |
| --- | --- | --- |
| 窓の重なりを許す (現行は `_overlaps` で弾く) | `ai.candidates.allow_overlap` | `false` |
| スコア下限で切る (上位 N でなく閾値方式) | `ai.candidates.min_score` | `0` (無効) |
| 窓長を短くして細かい候補を拾う | `ai.candidates.window_sec` | `0` (= `archive` の値を使う) |

* ストリームマーカー由来の強制区間は現行どおり枠を消費しない (`select_top_events` の `forced`)。

### 7.2 第 2 段階: AI による面白さの評価

1 回の呼び出しで候補を `ai.scene_eval.batch_size` 件ずつ評価する
(既定 8 件 × 4 回程度。モデルは `claude-sonnet-5-5` / `effort = medium`。
**全バッチを同じモデルへ寄せ、共通前置きをキャッシュへ乗せる** / §6.4)。期待する応答:

```json
{
  "scenes": [
    {
      "candidate_index": 7,
      "start": 1250.5,
      "end": 1278.2,
      "scores": {
        "funny": 82, "surprise": 74, "context_dependency": 35,
        "first_time_clarity": 68, "excitement": 79,
        "shorts_fit": 88, "long_fit": 55, "standalone": 81
      },
      "summary": "視聴者の指摘に配信者が気付いて慌てる",
      "reason": "直前の自信ある発言と結果が食い違い、コメントのｗが急増している",
      "needed_context": { "start": 1240.0, "end": 1250.5, "why": "何に自信があったかの説明" },
      "recommended_use": ["shorts", "long_hook"],
      "peak": 1268.0
    }
  ]
}
```

| 項目 | 型・範囲 | 対応する要望 §4.2 |
| --- | --- | --- |
| `scores.funny` | 0..100 | 面白さ |
| `scores.surprise` | 0..100 | 意外性 |
| `scores.context_dependency` | 0..100 (高い = 前後が必要) | 文脈依存度 |
| `scores.first_time_clarity` | 0..100 | 初見理解度 |
| `scores.excitement` | 0..100 | 盛り上がり |
| `scores.shorts_fit` | 0..100 | ショート適性 |
| `scores.long_fit` | 0..100 | 長尺適性 |
| `scores.standalone` | 0..100 | 独立性 |
| `summary` / `reason` | 文字列 (`ai.scene_eval.max_reason_chars` で上限) | シーンの概要 / 理由 |
| `needed_context` | 区間 + 理由 (任意) | 必要な前後の文脈 |
| `recommended_use` | `shorts` / `long_hook` / `long_body` / `long_climax` / `skip` | 推奨する利用方法 |
| `peak` | 秒 (任意) | 短尺のフックに使う山の位置 |

* **単一の「面白さ」を聞かない** (要望 §4.3)。評価軸を分けることで再現性が出て、
  §13 の学習にも使える数値が残る。
* 範囲外・欠落・未知の値は `plan_validator` が既定へ丸める (`clamp` して落とさない)。
* 上の形は `schema` としてサーバーへ送り、**構造化出力で受け取る** (§6.2)。
  それでも欠けた場合に備えて丸めは残す (モデルの出力を信用しきらない)。
* 評価結果は `<project>.ai.json` へ保存し、再実行時に使い回せるようにする (呼び出し回数を減らす)。

### 7.3 第 3 段階: 統合・ランキング

* 重複統合は既存 `scoring._merge_time_sections` と同じ規則を使う (区間が重なる / 連続するものを 1 つに)。
* 総合順位は**設定の重み**で決める (AI に順位そのものを作らせない = 再現性のため):

```
rank_score = Σ ( scores[k] * ai.scene_eval.weights[k] )
```

既定の重み (用途別に 2 組持つ):

| 用途 | funny | surprise | first_time_clarity | excitement | shorts_fit | long_fit | standalone | context_dependency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `long` | 0.30 | 0.15 | 0.15 | 0.20 | 0.00 | 0.20 | 0.00 | -0.10 |
| `shorts` | 0.30 | 0.20 | 0.15 | 0.10 | 0.25 | 0.00 | 0.20 | -0.20 |

* 既存の採点 (`total`) も混ぜる (`ai.scene_eval.base_score_weight`、既定 0.2)。
  AI が落としても音量・コメントが極端に高い区間は残る = **AI の誤判断で面白い場面を失わない**。

### 7.4 「既存が候補化しなかったシーン」の追加発見 (要望 §4.2 末尾)

音量もコメントも動かない「静かに面白い会話」は第 1 段階で落ちる。これを拾う経路を**任意機能**として設計する。

* `ai.sweep.enabled` (既定 `false`)。有効時は全長を `ai.sweep.chunk_sec` (既定 600) ごとに区切り、
  **その区間の文字起こしだけ**を AI へ渡して「面白そうな時刻」を列挙させ、候補へ合流する。
* 全長の文字起こしが必要なためコストが跳ねる。**既定オフ**とし、実測してから既定を見直す (§22 Q4)。
* 合流後は第 2 段階以降と同じ扱い。重複は §7.3 で統合される。

---

## 8. R5 動画企画・構成 (`ai/planner.py`)

### 8.1 `VideoPlan` (プラットフォームごとに 1 つ)

```json
{
  "target": "youtube_long",
  "title": "自信満々だったのに",
  "concept": "配信者の自信が崩れる瞬間を 3 段で積む",
  "duration_sec": 480,
  "sections": [
    { "role": "opening",  "scene_ref": 7,  "start": 1262.0, "end": 1272.0, "reason": "最も意外な一言だけを先に見せる" },
    { "role": "intro",    "scene_ref": 3,  "start": 845.0,  "end": 872.0,  "reason": "何をしている配信かを説明する" },
    { "role": "body",     "scene_ref": 11, "start": 2110.0, "end": 2190.0, "reason": "同じ失敗の繰り返しで笑いを積む" },
    { "role": "climax",   "scene_ref": 7,  "start": 1240.0, "end": 1290.0, "reason": "オープニングの続きを full で見せる" },
    { "role": "ending",   "scene_ref": 15, "start": 3300.0, "end": 3318.0, "reason": "本人の自己申告で締める" }
  ],
  "direction": {
    "subtitle": "full",
    "emphasis_scenes": [7, 11],
    "sound_effects": "light",
    "bgm": "none",
    "vertical_crop": false
  }
}
```

| `target` | `role` の既定並び | 根拠 |
| --- | --- | --- |
| `youtube_long` | `opening` → `intro` → `body`(複数) → `climax` → `ending` | 要望 §5.2 |
| `youtube_shorts` / `tiktok` | `hook` → `context` → `body` → `punchline` | 要望 §5.3 |

* **長尺と短尺は別々に呼び出す** (要望 §5.4)。同じ `VideoPlan` を使い回さない。
  Shorts と TikTok も独立に呼ぶ (既定 `ai.plan.targets = ["youtube_long","youtube_shorts"]`。
  TikTok は `ai.plan.targets` へ足すだけで増える)。
* 尺の上限・下限は設定 (`ai.plan.<target>.min_sec` / `max_sec`)。AI にハードコードさせない。
* `scene_ref` は §7 の候補 index。**AI が新しい時刻を勝手に発明しても、`start`/`end` は
  必ず VOD の範囲と素材の存在で検証する** (§10)。
* オープニングで使った区間をクライマックスで再使用するような**重複利用は許す** (要望 §5.2 の構成例)。
  検証では「同じ区間の二重使用」をエラーにしない。

### 8.2 編集プロファイルの注入

`AnalysisBundle.profile` があれば、プロンプトへ「この利用者の傾向」として添える (§13.3 の要約)。
無ければ一般的な方針で組む。**プロファイルは指示ではなく参考**として渡し、
矛盾したときは尺・上限などの設定値を優先する。

---

## 9. R6 `EditPlan` スキーマ (`ai/plan_schema.py`) — AI と既存エンジンの契約

### 9.1 全体

```json
{
  "plan_version": 1,
  "generated": { "provider": "claude", "model": "claude-opus-5-5", "at": "2026-11-20T12:00:00+09:00" },
  "project": {
    "target": "youtube_shorts",
    "source": { "kind": "vod", "video_id": "1234567890", "path": "" },
    "canvas": { "orientation": "portrait" },
    "duration_sec": 45
  },
  "scenes": [
    {
      "id": "sc1",
      "source_start": 1262.0,
      "source_end": 1272.5,
      "role": "hook",
      "order": 1,
      "reason": "意外な発言で興味を引く",
      "crop": { "mode": "center", "scale": 1.0 },
      "zoom": null,
      "gain_db": 0.0
    }
  ],
  "subtitles": [
    {
      "scene_id": "sc1",
      "source_start": 1263.2,
      "source_end": 1265.0,
      "text": "いや そんなわけないでしょ",
      "track_role": "own",
      "animation": "tsukkomi",
      "fade_in": false,
      "fade_out": false,
      "color": "",
      "align": "",
      "reason": "ここが一番の山なので動きを付ける"
    }
  ],
  "sounds": [
    { "scene_id": "sc1", "offset_sec": 0.4, "preset": "surprise", "gain_db": -3.0, "reason": "気付いた瞬間を立てる" }
  ],
  "editing": { "subtitle": true, "vertical_crop": true, "sound_effects": true, "watermark": null },
  "notes": "オチの直前に 0.3 秒の間を残した"
}
```

### 9.2 時間座標の規約 (Z2 への対処 / **最重要**)

| 項目 | 座標系 | 変換の担い手 |
| --- | --- | --- |
| `scenes[].source_start` / `source_end` | **元 VOD の秒** | `plan_applier` が素材へ写す |
| `subtitles[].source_start` / `source_end` | **元 VOD の秒** | 同上 |
| `sounds[].offset_sec` | **そのシーン先頭からの相対秒** | 同上 |
| 素材内の秒 | 切り出し素材の時刻 | `cut_origin.measure` の原点を引く / 足す |
| Timeline の秒 | 編集後の時刻 | `timeline/timemap.TimeMap` (`to_source` / `to_timeline` / `split_interval`) |

* **AI は Timeline 時間を一切知らない。** 無音カットで何が消えるか・OP が何秒かを AI に考えさせない。
* `sounds` だけ相対秒にするのは、シーンの並べ替えで SE が付いて回るようにするためである
  (絶対の VOD 時間で書くと、並べ替えたときにどのシーンの SE か分からなくなる)。

### 9.3 各項目の受け手 (既存のどこが実行するか)

| EditPlan の項目 | 受け手 | 備考 |
| --- | --- | --- |
| `scenes[]` の区間と順番 | `timeline/builder.build` + `commands.MoveClip` / `TrimClip` / `DeleteClip` | V1 を組み替える |
| `scenes[].gain_db` | `commands.SetAudioGain` | A1 のリンク音声 |
| `scenes[].crop` | `timeline/crop.py` + `vertical.crop.*` | 縦の切り抜き指定 (動画は作らない) |
| `scenes[].zoom` | `commands.ZoomBaseClip` / `MoveBaseClip` | 既存の拡大・位置 |
| `subtitles[]` | `commands.AddSubtitleClip` / `EditSubtitle` | 本文・役割・色・寄せ |
| `subtitles[].animation` | `SubtitleClip.animation` (`modules/subtitle_animation.py`) | 値は現行 12 種のみ (§9.4) |
| `sounds[]` | `commands.AddSoundClip` / `SetAudioGain` | SE トラック (`timeline.sound_tracks`) |
| `editing.subtitle` | `subtitle.enabled` 相当 | 実行用設定コピーへ反映 |
| `editing.vertical_crop` | `output_profile` / `vertical.*` | キャンバスの向き |
| `project.canvas.orientation` | `modules/output_profile.py` | 解決値は保存しない (§6.1 の原則) |

### 9.4 値の制約 (AI に自由文を書かせない項目)

| 項目 | 許す値 |
| --- | --- |
| `project.target` | `youtube_long` / `youtube_shorts` / `tiktok` |
| `scenes[].role` | `opening` / `intro` / `body` / `climax` / `ending` / `hook` / `context` / `punchline` |
| `subtitles[].track_role` | `own` / `sub` / `comment` |
| `subtitles[].animation` | `""` / `shake` / `glow_in` / `tsukkomi` / `glow_blur` / `shock1` / `shock2` / `shine` / `poyon` / `dive_tsukkomi` / `double_jump` / `black_blur_zoom_in` / `black_blur_zoom_out` (`docs/design/design.md` §5.4 と同一。廃止値は `""` へ) |
| `sounds[].preset` | `ai.sounds.presets` のキーのみ (**パスを AI に書かせない**。プリセット名 → ファイルの対応は設定側が持つ) |
| `scenes[].crop.mode` | `center` / `left` / `right` / `face` (`face` は `blur` の検出が有効なときだけ) |

* `track_role` が `comment` のテロップは**フェードしか効かない** (背景の箱とアイコンが動きに追従できない /
  `design.md` §5.4)。`animation` が付いていても検証で `""` に落とす。
* `preset` 方式にするのは安全のためでもある。AI が任意のローカルパスを書けると、
  検証漏れがそのままファイル読み出しになる。

### 9.5 バージョン

* `plan_version = 1`。未知の版は読まずに `AiPlanError`。
  プロジェクト JSON (`project_io.SCHEMA_VERSION = 1`) と同じ作法に合わせる。
* EditPlan はプロジェクトの隣へ `<project>.plan.json` として保存する
  (§14 の差分解析で「AI が何を意図したか」の原本になるため、必ず残す)。

---

## 10. R7 EditPlan の検証 (`ai/plan_validator.py`)

**AI の出力をそのまま実行しない** (要望 §10.2)。検証は 2 段で行う。

### 10.1 形の検証 (スキーマ)

| # | 検査 | 違反時 |
| --- | --- | --- |
| V1 | 必須キーの有無・型 | `AiPlanError` (この 1 件は致命) |
| V2 | `plan_version` が既知か | `AiPlanError` |
| V3 | 列挙値 (§9.4) が許す値か | 既定へ丸める (`role` 不正 → `body`、`animation` 不正 → `""`) |
| V4 | 文字列長 (`text` は `subtitle.max_line_length` × `subtitle_max_lines` を超えないか) | 既存の折り返し (`wrap_lines`) を通してから載せる |
| V5 | 数値の範囲 (`gain_db` は `timeline.sound_tracks.gain_min_db`〜`gain_max_db`) | clamp |

### 10.2 中身の検証 (素材と整合するか)

| # | 検査 | 違反時 |
| --- | --- | --- |
| V6 | `source_start < source_end` | その要素を落とす |
| V7 | 区間が VOD の尺 (`duration_sec`) に収まるか | 収まる範囲へ切る |
| V8 | 区間の素材が手元にあるか (候補として切り出したか) | 無ければ `vod_range.fetch_range` で取り直す。取れなければ落とす |
| V9 | 最短尺 (`project_io._DEFAULT_MIN_CLIP_SEC` 相当) を下回らないか | 落とす |
| V10 | シーン合計尺が `ai.plan.<target>.max_sec` を超えないか | 後ろの `body` から削る (`opening` / `climax` は残す) |
| V11 | テロップが属するシーンの区間内にあるか | はみ出し分を切る。完全に外なら落とす |
| V12 | テロップ同士が重なっていないか | 後ろをずらす (既存の 1ms 境界規約 `SubtitleClip.to_dict` を壊さない) |
| V13 | SE が `max_mix_inputs` (既定 32) を超えないか | 超過分を落とす |
| V14 | SE のプリセットが存在し、ファイルが実在するか | 落とす |
| V15 | 適用後の Timeline が `project_io.validate` を通るか | 通らなければ適用前へ巻き戻し、`AiPlanError` |

* **落とす (黙って崩さない)** の原則は既存のアニメーション設計と同じ (`design.md` §5.4)。
  落とした件数と理由は 1 行のログに残し、画面に「AI の指示のうち N 件を適用できませんでした」と出す。
* V15 は適用の最後に 1 回。`CommandStack` の `_snapshot` / `_restore` を使うため、
  失敗しても**編集画面は壊れない**。

---

## 11. R8 EditPlan → Timeline (`ai/plan_applier.py`) と経路 D

### 11.1 適用の順番

```
① 骨組みを作る
     clip_writer.prepare_one_clip でシーンごとに素材を用意 (正規化 / 編集点 / 文字起こし)
     archive/timeline_builder.build_archive_timeline で Timeline を組む   ← 既存
     put_archive_media で素材の VOD 範囲 (vod_start/vod_end) を記録       ← 既存
② 並べ替え    scenes[].order のとおり commands.MoveClip で V1 を並べる
③ 不要の削除  候補として用意したが plan に無いシーンは commands.DeleteClip
④ 端の調整    plan の区間と素材の差分を commands.TrimClip で詰める
⑤ テロップ    自動生成済みのテロップを基に EditSubtitle、plan 固有のものは AddSubtitleClip
⑥ 動き        SubtitleClip.animation / fade_in / fade_out を EditSubtitle で設定
⑦ SE          commands.AddSoundClip + SetAudioGain
⑧ 縦          縦ターゲットなら timeline/vertical_builder で縦プロジェクトへ (crop は指定として持つ)
⑨ 検証        project_io.validate → 保存 (project_io.save) + EditPlan を隣へ保存
```

* **すべて `CommandStack.push` 経由**。これで Undo/Redo が AI の編集にも効き、
  利用者は「AI の 1 手だけ戻す」ことができる。
* ⑤ が「自動生成済みのテロップを基に」なのが重要である。テロップ本文は既存の文字起こしが正であり、
  AI には**直すべきものだけ**を指示させる (全文を AI に書かせると認識の正確さを失う)。

### 11.2 経路 D の処理フロー (進捗 6 工程)

```
[実行]  入力 (VOD URL / ローカル長尺) + ターゲット (長尺 / Shorts / TikTok)
  │  事前確認: ffprobe / 空き容量 (general.min_free_space_gb) / AI 鍵の有無 / 同意
  │  projectId 採番 (telemetry) → ポイント予約 (points.reserve)
  ▼
[1] 取得・解析        twitch_source / comment_source / archive.pipeline.analyze    ← 既存
  ▼
[2] 候補の文字起こし   cut_region → cut_origin → _transcribe (候補のみ)            ← 既存
  ▼
[3] AI シーン評価      ai/scene_finder (バッチで複数回。失敗時は既存採点へ縮退)
  ▼
[4] AI 企画・構成      ai/planner → VideoPlan → EditPlan (ターゲットごと)
  ▼
[5] 適用              ai/plan_validator → ai/plan_applier → Timeline → 保存
  ▼
   編集画面            TimelineEditorDialog (既存)。AI の意図を併記 (§18)
  │     キャンセル → PipelineCancelled (異常終了ではない)
  ▼
[6] レンダリング・出力  timeline/renderer.render → output_writer                    ← 既存
  ▼
[完了]  ポイント確定 → 工程ごとの所要をログへ 1 行 (utils/stage_timer)
```

* **工程 [6] より後ろは経路 A/B/C と完全に同一**である。出力・透かし・ラウドネス検証・
  ポイント確定の規約は一切変えない。
* ターゲットが複数のときは [4][5] をターゲットごとに回し、**プロジェクトを複数作る**。
  出力は利用者が選んだものだけ行う (ポイントは「1 回の出力操作につき 1 予約」の現行規約のまま)。
* AI の工程 ([3][4]) が失敗した場合: **経路 B と同じ結果** (採点順のクリップ) まで落として続行する。
  AI 機能の失敗で「何も出てこない」状態にはしない。

---

## 12. R10 品質レビューと自動修正 (`ai/reviewer.py`)

### 12.1 レビュー担当 (要望 §8.2)

役ごとに 1 回ずつ呼ぶ (`ai.review.roles` で選べる。既定は 3 役のみ有効 = コスト抑制)。

| 役 | 見るもの | 渡すもの | 既定 |
| --- | --- | --- | --- |
| `director` | 映像の見せ方・テンポ・演出 | EditPlan + 各シーンの尺・順番 | 有効 |
| `critic` | 面白さ・笑い・盛り上がり | EditPlan + 文字起こし + シーン評価 | 有効 |
| `viewer` | 初見で分かるか | EditPlan + 文字起こし (**シーン評価は渡さない** = 予備知識なしで読ませる) | 有効 |
| `platform` | 各プラットフォームへの適性 | EditPlan + target + 尺 | 有効 (長尺 / Shorts / TikTok の 3 本立てになったため) |
| `qa` | 音声・映像・字幕の技術的な問題 | **機械の検査結果**を渡す (§12.2) | 無効 |

モデルは §6.3 のとおり `critic` だけ `claude-opus-5-5`、他は `claude-sonnet-5-5`。
レビューは役ごとに同じ EditPlan を見るため、**前置き (EditPlan + 解析の要約) を共通化して
キャッシュへ乗せる**。役ごとの指示だけを後ろに置く (§6.4)。

### 12.2 `qa` 役は AI に任せない部分がある

音量・尺・字幕の重なり・テロップのはみ出しは**プログラムで測れる**ので測る
(`modules/loudness_normalizer` の検証、`project_io._validate_overlaps`、
`modules/font_advance` の実測幅)。AI へ渡すのは「測った結果の意味付け」だけにする。
要望 §10.2 の責任範囲 (時間計算・検証はプログラム) に合わせるためである。

### 12.3 レビュー結果 (要望 §8.3)

```json
{
  "issues": [
    {
      "id": "iss1",
      "at": { "start": 12.0, "end": 18.5, "coord": "timeline" },
      "scene_id": "sc2",
      "kind": "pacing",
      "severity": "major",
      "detail": "状況説明が長く、オチまでに 12 秒かかっている",
      "reason": "Shorts では冒頭 3 秒で興味を失う",
      "fix": { "op": "trim", "target": "sc2", "source_start": 848.0, "source_end": 860.0 },
      "expected": "オチまでが 7 秒になる",
      "priority": 1
    }
  ]
}
```

| 項目 | 値 |
| --- | --- |
| `kind` | `pacing` / `clarity` / `funny` / `subtitle` / `audio` / `visual` / `platform` |
| `severity` | `minor` / `major` / `critical` |
| `fix.op` | `trim` / `drop` / `reorder` / `edit_subtitle` / `add_subtitle` / `add_sound` / `set_animation` / `none` |
| `at.coord` | `timeline` / `source` (どちらの時間か明示。曖昧さを残さない) |

### 12.4 自動修正ループの上限 (要望 §8.4)

```
EditPlan v1 → 適用 → レビュー → 修正指示 → EditPlan v2 → 適用 → レビュー → …
```

| 上限 | キー | 既定 |
| --- | --- | --- |
| 修正の往復回数 | `ai.review.max_rounds` | **2** |
| 1 回のレビューで採る指摘数 | `ai.review.max_issues_per_round` | 5 |
| 採る重大度の下限 | `ai.review.min_severity` | `major` |
| 1 プロジェクトの AI 呼び出し総数 | `ai.cost.max_calls_per_project` | 20 |
| 1 プロジェクトの概算入力トークン総数 | `ai.cost.max_input_tokens_per_project` | 設定値 (§16) |

* 上限に達したら**そこで止めて利用者へ渡す**。無制限に回さない。
* `severity = critical` の指摘が残っている場合は、画面に「未解決の指摘」として出す (隠さない)。
* **最終承認は必ず人間** (要望 §8.4 末尾)。レビューが通ったからといって自動で出力しない。

### 12.5 映像を見せるレビュー (回答 Q5 で許可)

Claude は画像を受け取れる。**動画そのものは送れない**ため、フレームを抜いて送る。

| 項目 | 決め方 | キー |
| --- | --- | --- |
| どこを抜くか | シーンごとに「先頭 / 中央 / `peak` 付近」。`peak` があればそれを優先 | `ai.vision.points` |
| 1 シーンあたり何枚 | 既定 **1 枚**。`director` 役のみ最大 3 枚 | `ai.vision.frames_per_scene` |
| 1 回の呼び出しの上限 | 既定 **12 枚** (超えたらスコアの高いシーンを優先) | `ai.vision.max_frames` |
| 大きさ | 長辺 `ai.vision.max_edge_px` (既定 768) まで縮める。**トークンは面積に比例する** | `ai.vision.max_edge_px` |
| 抜き方 | 既存 `timeline/frame_source.py` (プレビュー用のフレーム取得) をそのまま使う。無ければ ffmpeg で 1 枚書き出す | — |
| いつ抜くか | **レンダリング後ではなく Timeline から**。出力を待たずにレビューできる (縦の crop は `timeline/crop.py` の指定を適用した絵を使う) | — |
| 既定 | `ai.vision.enabled = true` (回答 Q5)。ただし `frames_per_scene = 1` で始める | — |

* 送るのはフレーム画像だけで、**音声は送らない**。音の問題は機械の測定で拾う (§12.2)。
* フレームは**作業フォルダへ一時出力し、送信後に消す** (`PipelineContext.cleanup` と同じ扱い)。
* 画像の実トークンは `/api/ai/count` で測る (§5.4)。枚数の既定は測定結果で見直す (§22 T1)。
* 映像を見せると分かるようになるもの: テロップが顔に重なる・暗転だけの区間・
  クロップで人物が切れている・画面の情報が読めない。これらは数値では拾えない。

### 12.6 R15 レビュー PDF (回答 Q9)

**「この動画をどう見たか」を 1 枚の読み物として書き出す。** AI の指摘・評価・提案の置き場であり、
画面を変えずに (回答 Q12) AI の意図を利用者へ届ける唯一の経路になる。

| 節 | 内容 | 出どころ |
| --- | --- | --- |
| 表紙 | 元 VOD・ターゲット・尺・生成日時・使ったモデル | EditPlan `generated` |
| 総評 | 3〜5 行。何が良くて何が足りないか | `critic` / `director` |
| 構成の表 | 役割 → 区間 → 尺 → 採用理由 | VideoPlan |
| シーン評価 | 8 軸のスコアと一言 (表 + 簡単な棒) | SceneEval |
| 指摘一覧 | 時刻・重大度・内容・修正案・期待される効果 | Review `issues` |
| **効果音の提案** | 「この時刻にこういう音が欲しい」理由つき。手元に無い音は**プリセット名ではなく説明**で書く | `reviewer` の `add_sound` 指摘 |
| テロップの提案 | 文言・動き・強調の提案 | `edit_subtitle` / `set_animation` 指摘 |
| 技術的な注意 | 音量・字幕の重なり・はみ出し (機械の測定) | §12.2 |
| 適用できなかった AI の指示 | 件数と理由 (§10) | plan_validator |
| 参考画像 | §12.5 で抜いたフレーム (あれば) | frame_source |

実装の決まり:

* **本文の組み立ては `ai/review_report.py` (Qt 非依存)** で、出力は HTML 1 枚。
  見出し・表・画像だけを使い、CSS は最小限にする (`docs/quality/resolve1.html` と同じ作り方)。
* **PDF 化は PySide6 の `QTextDocument` + `QPdfWriter`** で行う (`gui` 側)。
  PySide6 は既に依存にあるため、**PDF ライブラリを追加しない**。
  フォントは既存の設定 (`subtitle.font` ほか) ではなく、PDF 用に `ai.report.font` を持つ。
* HTML のまま保存もできる (`ai.report.format` = `pdf` / `html` / `both`)。
  PDF にできない環境でも内容は失われない。
* 置き場は出力フォルダの `<出力名>_review.pdf` (`general.output_directory`)。
* **レビューを回していなくても出せる。** その場合は「構成の表 + シーン評価 + 適用できなかった指示」
  だけの PDF になる (= AI が何を考えて組んだかの記録)。これが Q12 の答えでもある。
* 既定は `ai.report.enabled = true`、`auto_open = false` (勝手に PDF ビューアを開かない)。

---

## 13. R11 人間の編集の解析と編集プロファイル (`ai/edit_analyzer.py` / `ai/profile.py`)

要望 §12 の本体。**Phase 1 (要望 §12.7) をここで完結させる。**

### 13.1 入力は「保存済みプロジェクト JSON」

`project_io.scan_projects(settings, kind=...)` が列挙するものをそのまま読む。
プロジェクト JSON には次がすべて入っている (= 新しいログ機構なしで解析できる)。

| 要望 §12.3 の解析対象 | プロジェクト JSON のどこから測るか |
| --- | --- |
| カット位置・頻度・不要な間の削除 | `source.keep_segments` と V1 クリップの `source_in` / `duration` の連なり |
| テンポ (シーンごとの尺・展開速度) | V1 クリップの尺分布 (中央値・四分位) |
| テロップの表示タイミング・文字数・位置・装飾 | `SubtitleClip` の `timeline_start` / `duration` / `text` / `transform` / `color` / `align` / `font_size` |
| テロップアニメーション | `SubtitleClip.animation` / `fade_in` / `fade_out` の使用率と種類の分布 |
| エフェクト | `source.blur` 指定 / `Transform` (zoom・位置) の使用率 |
| 音声 | `AudioClip.gain_db` / `muted`、`SoundClip` の本数・`gain_db`・配置間隔 |
| シーン構成 | V1 クリップの並びと、`archive` 用なら `clip_index` の順 |
| 強調表現 | 大きいフォント・個別色・動き付きテロップの出現位置 (ピーク付近かどうか) |
| 演出 (ズーム・トランジション) | `ZoomBaseClip` の結果 (`Transform.scale`)。トランジションは現状存在しない (§19) |
| 動画全体 (導入・展開・オチ・締め) | 先頭 / 末尾の尺の偏り、最長クリップの位置 |

### 13.2 二段構え: 測る → 言語化する

| 段 | 担い手 | 出力 |
| --- | --- | --- |
| ① **統計 (AI 不要)** | `ai/edit_analyzer.py` | 分布・率・中央値・ヒストグラム (数値だけ。**再現性がある**) |
| ② **言語化 (AI)** | `ai/profile.py` + `prompts/profile.md` | 「オチの直前に 0.3 秒前後の間を残す傾向」のような**仮説つきの文章** |

* ① は AI を呼ばない。**AI を呼べない環境でもプロファイルの骨格は作れる。**
  入口は `ai/profile.refresh(settings, paths, provider=None)` 1 つで、
  `provider=None` なら統計だけで完結する。保存済みプロジェクトの一覧は呼び出し側が
  `project_io.scan_projects` で集めて渡す (`ai/` は `project_io` を import しない)。
  手で回すときは `python tools/ai_smoke.py --learn` (実測のみ / ログイン時は言語化まで)。
* ② で得た文章は `hypothesis` として保存し、**確定事実として扱わない** (要望 §12.8)。

### 13.3 プロファイル JSON

```json
{
  "profile_version": 1,
  "name": "ゲーム実況 / 長尺",
  "scope": { "target": "youtube_long", "content": "game" },
  "updated_at": "2026-11-20T12:00:00+09:00",
  "sample": { "projects": 23, "clips": 412, "subtitles": 5120, "from": "2026-06-01", "to": "2026-11-18" },
  "stats": {
    "clip_sec": { "median": 8.4, "p25": 4.1, "p75": 15.2 },
    "cut_gap_sec": { "median": 0.22 },
    "subtitle_chars": { "median": 14, "p90": 22 },
    "animation_rate": 0.08,
    "animation_top": [["tsukkomi", 0.41], ["shock1", 0.22]],
    "se_per_min": 1.6,
    "zoom_rate": 0.04
  },
  "hypotheses": [
    { "id": "h1", "text": "面白い発言の直前は 0.2 秒以下まで間を詰める", "confidence": "medium", "evidence": 37, "status": "unconfirmed" }
  ],
  "avoid": [
    { "id": "a1", "text": "コメント役割のテロップに動きを付けない", "source": "rule" }
  ],
  "history": [
    { "at": "2026-11-20T12:00:00+09:00", "change": "created", "projects": 23 }
  ]
}
```

* 置き場は `%LOCALAPPDATA%\Stretheus\ai\profiles\<slug>.json` (`ai/store.py`)。
  **プロジェクトの隣には置かない** (プロジェクトを配っても学習内容が付いて回らないようにする)。
* **複数プロファイル** (要望 §12.5): `scope` で用途を分け、経路 D は
  `ai.profile.active.<target>` で選ぶ。未設定なら使わない。
* `history` に更新履歴を必ず積む (要望 §12.8)。利用者が「いつ何が変わったか」を見られる。
* `status`: `unconfirmed` / `confirmed` / `rejected`。**利用者が訂正できる** (要望 §12.4 末尾)。
* `avoid` の `source: "rule"` は機械由来の制約 (コメント役割の動きなど)。AI が覆せない。

### 13.4 ユーザー分離と学習の節度 (要望 §12.8)

| 決まり | 実装 |
| --- | --- |
| 利用者ごとのデータを分ける | 置き場を `%LOCALAPPDATA%` (Windows ユーザー単位) にする。JWT の sub では分けない (ログイン前も使えるため) |
| 他人の編集ログを無断で学習しない | **サーバーへ送らない** (`ai.learning.upload = false` 固定。有効化は将来の明示的な設計で) |
| 抽出した情報と実データを区別する | `stats` (実測) と `hypotheses` (推定) を別のキーに分ける |
| 推定を事実として扱わない | `confidence` / `status` を必ず持たせ、プロンプトでも「参考」と添える |
| 一時的な修正と継続的な傾向を区別する | `evidence` (何件に基づくか) が `ai.learning.min_evidence` (既定 10) 未満の仮説はプロンプトへ入れない |
| いつでも確認・調整できる | プロファイル JSON をそのまま画面で見せ、仮説ごとに「採用 / 却下」を切れる (§18) |
| 不適切な修正を無条件に学習しない | §14.4 の除外規則 |

既定 (回答 Q7):

* **`ai.learning.enabled = true`** — 既定で学習する。溜まらないと何も改善しないため。
* **`ai.learning.keep_days = 180`** (半年) — それより古い差分データは起動時の掃除で捨てる
  (既存の残骸整理と同じ作法。`utils/trash` でゴミ箱へ送る)。
  **プロファイル本体と更新履歴は捨てない** (捨てるのは生の差分データだけ)。
* `ai.learning.upload = false` は**固定**。学習は端末内で完結し、サーバーへは送らない (回答 Q8 と同じ方針)。
* 既定で学習するため、**初回の AI 利用時の同意 (§5.5) に「編集の差分を端末内に記録する」ことを含める。**
  黙って記録しない。利用者はいつでも `ai.learning.enabled = false` にできる。

---

## 14. R12 差分解析 (`ai/plan_diff.py`) — AI 編集 vs 人間の修正

### 14.1 何と何を比べるか

| 比較対象 | 出どころ |
| --- | --- |
| **AI 版** | `plan_applier` の適用直後に保存したプロジェクト JSON (`<project>.ai-base.json`) + `<project>.plan.json` |
| **人間版** | 利用者が編集画面で保存した本体のプロジェクト JSON |

* AI 版のスナップショットを**適用直後に 1 回だけ**取るのが要点。これが無いと「人間が直したのか
  AI がそう作ったのか」が永久に分からない。
* 差分は**保存時**に取る (編集中は取らない)。`CommandStack.is_clean` が立つ瞬間が機会である。

### 14.2 差分の分類 (要望 §12.4 の表を実装語へ)

| 分類 | 検出方法 | 記録する値 |
| --- | --- | --- |
| `cut_shorten` / `cut_extend` | V1 クリップの `source_in` / `duration` の変化 | 頭・尾それぞれの秒差 |
| `scene_drop` | AI 版にあった区間が人間版に無い | 区間とシーン役割 |
| `scene_add` | 人間版にだけある区間 | 区間 |
| `reorder` | `timeline_start` 順の並びが変わった | 旧順 → 新順 |
| `subtitle_text` | 同じ時刻のテロップ本文が変わった | 編集距離・前後の文字列 |
| `subtitle_move` | `transform` / `align` / `font_size` が変わった | 変化量 |
| `subtitle_style` | `color` / `outline_color` が変わった | 旧値 → 新値 |
| `animation_add` / `animation_remove` / `animation_change` | `animation` の変化 | 旧値 → 新値 |
| `se_add` / `se_remove` / `se_gain` | `SoundClip` の増減・`gain_db` | 位置と量 |
| `audio_gain` | `AudioClip.gain_db` / `muted` | 量 |
| `emphasis_add` | 人間がフォント拡大 / 個別色 / 動きを足した | 位置 |

* 時刻の突き合わせは **VOD 時間へ戻してから**行う (`TimeMap` + `put_archive_media` の `vod_start`)。
  Timeline 時間のまま比べると、1 か所のカットで以降が全部「変更された」ことになってしまう。
* 一致判定の許容は `ai.learning.match_tolerance_sec` (既定 0.3)。

### 14.3 差分 1 件の記録 (要望 §12.4 の例をそのまま)

```json
{
  "kind": "cut_shorten",
  "scene_id": "sc1",
  "vod_range": { "start": 1262.0, "end": 1272.5 },
  "ai": { "head": 1262.0, "sec": 10.5 },
  "human": { "head": 1265.0, "sec": 7.5 },
  "delta": { "head_sec": 3.0, "tail_sec": 0.0 },
  "context": {
    "transcript": "…そんなわけないでしょ…",
    "role": "hook",
    "comment_rate": 2.4,
    "max_db": -8.1
  },
  "hypothesis": { "text": "hook では導入を短くした方がよい可能性がある", "confidence": "low", "status": "unconfirmed" },
  "user_note": ""
}
```

* **`context` を必ず付ける** (要望 §12.6 末尾)。編集操作だけを溜めても
  「どういう状況でその編集をしたか」が学べない。
* `hypothesis` は AI が推定し、`user_note` で利用者が訂正できる。確定事実にしない。

### 14.4 学習に取り込まない差分 (要望 §12.8 末尾)

| 除外する差分 | 理由 |
| --- | --- |
| 1 プロジェクトで 1 回しか現れない種類 | 一時的な修正の可能性が高い |
| `delta` が許容 (`match_tolerance_sec`) 未満 | 操作のぶれ |
| 素材が壊れていた / 復旧できなかったことに起因する修正 | 編集の意図ではない |
| 検証で落とした項目を人間が入れ直した分 | **AI の意図は通っていた**。落としたのは Stretheus 側 |
| 利用者が「学習しない」と指定したプロジェクト | 明示 (`ai.learning.exclude` にパスを持つ) |

---

## 15. R13 操作ログ (任意・既定オフ)

差分 (§14) は「結果」しか分からない。「どういう手順で直したか」まで見たい場合のための後付け。

* `timeline/commands.py:357 CommandStack.__init__` に **観測口を 1 つだけ**足す:
  `CommandStack(limit=100, observer=None)`。`push` が成功したとき
  `observer(label, command)` を呼ぶだけ (失敗時・Undo/Redo では呼ばない)。
* 既定は `None` = **現行と完全に同じ挙動**。GUI が `ai.learning.operation_log = true` のときだけ
  `ai/edit_log.py` を繋ぐ。
* 書き出しは `<project>.editlog.jsonl` (追記のみ)。1 行 = `{"at","label","kind","params"}`。
  **スナップショットは書かない** (`_snapshot` は Timeline 全体の deepcopy であり、
  毎回ファイルへ落とすと現実的でない)。
* 各 `Command` に `to_log()` を足す案は**採らない** (36 クラスへ手を入れることになる)。
  `observer` 側でクラス名と公開属性から拾う。拾えないものは `label` だけ残す。

---

## 16. R14-a 設定 (`settings/settings_window.py` の `DEFAULT_SETTINGS`)

`ai` セクションを新設する (20 番目)。**既定値の出どころは `DEFAULT_SETTINGS` 1 か所**であり、
`setting.json` にも同じ内容を入れる (`docs/design/design.md` §6.1)。

```
"ai": {
  "enabled": false,                      # 実装が揃うまで既定オフ (有効化まで ai/ は import されない)
  "direct_mode": false,                  # true は開発用の直接呼び出し (§6.5)
  "endpoint": {                          # Stretheus API 側のパス (接続先は api.base_url を共有)
    "complete": "/api/ai/complete",
    "count":    "/api/ai/count",
    "config":   "/api/ai/config"
  },
  "models": {                            # 希望として送る。最終決定はサーバー (§6.1)
    "scene_eval": "claude-sonnet-5-5",
    "plan":       "claude-opus-5-5",
    "review":     "claude-sonnet-5-5",
    "review_critic": "claude-opus-5-5",
    "profile":    "claude-sonnet-5-5",
    "classify":   "claude-haiku-5-5"
  },
  "effort": {                            # 深さ (low/medium/high/xhigh/max)。既定に任せない
    "scene_eval": "medium", "plan": "high", "review": "medium",
    "profile": "medium", "classify": "low"
  },
  "max_output_tokens": 8192,
  "timeout_sec": 180,
  "retry":   { "max_attempts": 3, "backoff_sec": 2.0 },
  "consent": { "accepted_version": 0 },
  "privacy": { "include_comment_user": false, "include_transcript": true },
  "candidates": {
    "top_n": 30, "allow_overlap": false, "min_score": 0,
    "window_sec": 0, "transcribe_pad_sec": 15.0
  },
  "transcribe": { "speaker_filter": true, "reuse_existing": true },
  "sweep":   { "enabled": false, "chunk_sec": 600 },   # 回答 Q4: 一旦 OFF。要実測 (§22 T2)
  "scene_eval": {
    "batch_size": 8, "max_reason_chars": 120, "base_score_weight": 0.2,
    "weights": {
      "long":   { "funny": 0.30, "surprise": 0.15, "first_time_clarity": 0.15,
                  "excitement": 0.20, "shorts_fit": 0.0, "long_fit": 0.20,
                  "standalone": 0.0,  "context_dependency": -0.10 },
      "shorts": { "funny": 0.30, "surprise": 0.20, "first_time_clarity": 0.15,
                  "excitement": 0.10, "shorts_fit": 0.25, "long_fit": 0.0,
                  "standalone": 0.20, "context_dependency": -0.20 }
    }
  },
  "plan": {
    "targets": ["youtube_long", "youtube_shorts", "tiktok"],   # 回答 Q6
    "youtube_long":   { "min_sec": 180, "max_sec": 900, "max_scenes": 12 },  # 回答 Q10: 15 分
    "youtube_shorts": { "min_sec": 15,  "max_sec": 60,  "max_scenes": 5 },
    "tiktok":         { "min_sec": 15,  "max_sec": 60,  "max_scenes": 5 },
    # 2026-11-25 変更: "full" -> "key" (docs/error/20261125c/resolve.md §2.5)
    # 文字起こしは既存の処理が作る。AI は見た目だけ付ける (足さない / 本文を変えない)
    "subtitle_mode": "key",
    "variants": 1,                       # 回答 Q11: 1 案
    "allow_reuse_scene": true
  },
  "sounds": { "enabled": true, "presets": {} },     # 回答 Q9: 既定は空 (登録まで適用しない)
  "review": {
    "enabled": false,                    # Phase 2 で有効化
    "roles": { "director": true, "critic": true, "viewer": true,
               "platform": true, "qa": false },
    "max_rounds": 2, "max_issues_per_round": 5, "min_severity": "major"
  },
  "vision": {                            # 回答 Q5: 映像を見せてよい
    "enabled": true, "frames_per_scene": 1, "max_frames": 12,
    "max_edge_px": 768, "points": ["peak", "head"]
  },
  "report": {                            # 回答 Q9: レビュー PDF
    "enabled": true, "format": "pdf", "auto_open": false,
    "font": "", "include_frames": true, "name_suffix": "_review"
  },
  "cost": { "max_calls_per_project": 20, "max_input_tokens_per_project": 300000,
            "confirm_over_points": 0 },  # 0 = 事前確認なし (ポイント制の決定後に見直す)
  "budget": {
    "max_input_tokens": 60000, "chars_per_token": 2.0, "cell_pad_sec": 60,
    "curve_window_span": 2, "max_comments_per_cell": 20, "merge_transcript_gap_sec": 1.0,
    "cache_prefix": true                 # 共通前置きをキャッシュへ乗せる (§6.4)
  },
  "learning": {
    "enabled": true, "upload": false, "operation_log": false,   # 回答 Q7: 既定で学習
    "min_evidence": 10, "match_tolerance_sec": 0.3,
    "keep_days": 180, "exclude": []                             # 回答 Q7: 半年
  },
  "profile": { "active": { "youtube_long": "", "youtube_shorts": "", "tiktok": "" } },
  "debug":   { "dump_payloads": false }
}
```

決まり:

* **鍵はここに入れない** (§6.1 / §6.5)。`setting.json` に入るのはパスと希望モデル名だけ。
* `models.*` / `effort.*` は**希望**であり、サーバーが許可した範囲で解決される。
  サーバーが別のモデルを選んだ場合は応答の `model` が正であり、ログもそれを残す。
* `ai.enabled=false` のとき `ai/` は import されない (既定で軽い)。
* `_merge_with_defaults` はセクション単位の浅いマージであるため、
  `ai` の入れ子キーは `ai/config.py` が欠落時に既定へ落とす (既存の `archive/config.py` と同じ)。
* 実行時にだけ決まる値 (解決済みキャンバス等) は従来どおり保存しない
  (`output_profile.without_runtime_canvas` の守りはそのまま効く)。
* `api.base_url` は既存キーを共有する。AI 用に別の接続先を持たせない (出どころを 2 つにしない)。

## 17. R14-b 例外・ログ・ポイント・テレメトリ

### 17.1 例外 (`src/exceptions.py` へ追加)

```
AutoEditError
 └─ AiError              AI 機能の失敗の親
     ├─ AiProviderError  通信・認証・レート制限 (status / code を持つ。api_client の作法に合わせる)
     ├─ AiOfflineError   到達できない (5xx / 接続失敗 / タイムアウト) → 縮退の判断材料
     └─ AiPlanError      応答が JSON でない / スキーマ不正 / 検証で致命
```

* **既存の `except AutoEditError` 経路はそのまま効く** (`DiskSpaceError` と同じ方針)。
* `PipelineCancelled` と `DiskSpaceError` は AI 層でも握り潰さず貫通させる
  (`subtitle_generator.recognize_for_timeline` と同じ扱い)。

### 17.2 ログ

* 既存の `utils/logger.get_logger(__name__)` を使う。
* 残すもの: 呼び出した役割・モデル名・所要 ms・概算トークン・応答の件数・
  検証で落とした件数と理由・リトライ回数。
* **残さないもの: API キー・プロンプト全文・応答全文・コメント本文。**
  `ai.debug.dump_payloads = true` のときだけ作業フォルダへ別ファイルで落とす (既定オフ)。
* 工程ごとの所要は `utils/stage_timer` に 1 行でまとめる (既存と同じ)。
* **利用量は VOD 1 本ごとに 2 種類の行で残す** (`ai/usage_log.py`)。これが T1 の一次資料である。
  * `AI 合計: calls=… input=… output=… cache_read=… cache_create=… points=…/… roles=…`
  * `AI 内訳: role=… calls=… input=… output=… cache_read=… cache_create=… points=…/… models=…`
    (**1 行 1 工程**。ログから表へ写すだけで §6.3 の表を上書きできる)
  * 画面の経路 D (`gui/archive_tab.py` の `ArchiveAiWorker`) は**中止・失敗でも**最後に残す
    (そこまでに使った分は実費として出ているため)。

### 17.3 ポイント・課金 (回答 Q2 / Q3)

* **出力 1 回につき 1 予約という現行規約は変えない** (`points.reserve` → `commit` / `cancel`)。
  AI の利用は**この予約とは別の軸**として数える (出力しなくても AI は呼ばれるため)。
* **AI の費用はポイントで負担する** (回答 Q2)。判定はサーバーが行い、クライアントは
  応答の `points.charged` / `points.estimated` を受け取って表示するだけにする
  (残高から推測しない = 透かしと同じ作法)。
* ただし**既存のポイント制そのものの位置づけが保留** (回答 Q3) であるため、
  クライアントは次の 2 つの状態を**どちらも壊さずに通せる**作りにする。

| 状態 | サーバーの応答 | クライアントの振る舞い |
| --- | --- | --- |
| **計測のみ** (当面の既定) | `points.charged = 0` / `estimated` は入る | 消費しない。利用量だけログへ残す |
| **課金する** (決定後) | `points.charged > 0` | 残高不足はサーバーが拒否し、クライアントは理由を出して AI 工程だけ縮退する (既存の採点結果で続行) |

* **ポイントとトークンの換算は実測で決める** (回答 Q2)。そのために次を必ず記録する。

| 記録する値 | 出どころ |
| --- | --- |
| `role` / 解決されたモデル / `effort` | 応答の `model` と要求値 |
| `input_tokens` / `output_tokens` | `usage` |
| `cache_creation_input_tokens` / `cache_read_input_tokens` | `usage` (キャッシュが効いたかの証拠) |
| 画像の枚数と長辺 | クライアント (§12.5) |
| 1 プロジェクトの合計と内訳 (工程別) | クライアントが集計し 1 行のログへ |

  これを**同じ VOD で設定を変えて複数回**流し、「1 本あたりの実費」と「ポイント単価」を決める
  (§22 T1 の測定項目)。クライアント側は単価をハードコードせず、
  `/api/ai/config` から受け取る (価格改定で配布を作り直さないため)。
* 事前の見積りは `/api/ai/count` で出す。`ai.cost.confirm_over_points` を超える見積りのときだけ
  開始前に確認する (既定 0 = 確認しない。課金を始める段で有効にする)。
* 透かしの要否は従来どおりサーバーの応答だけで決める。

### 17.4 テレメトリ

* `telemetry.event("ai_scene_eval", duration_ms, project_id)` のように**既存の口**を使う。
  新しいテーブルを足さない。
* 送るのは所要・件数・成否・モデル名まで。**本文・プロンプト・コメントは送らない。**

---

## 18. 画面 — 新しい画面は作らない (回答 Q12)

**AI を含む編集結果は、既存の Timeline 編集画面にそのまま出る。** 新しいタブ・新しい画面・
インスペクタの追記はいずれも行わない。経路 D は**既存の画面の流れに乗る**。

### 18.1 どこから始めて、どこに出るか

```
既存「アーカイブ切り抜き用」タブ (gui/archive_tab.py)
  │   入力と実行は今のまま。ai.enabled=true のとき経路 D を通る
  ▼
既存の採点結果画面 (ArchiveResultWindow)
  │   クリップ一覧・採点グラフ・使用可否 — 画面はそのまま。
  │   並びと採否が AI の構成で決まっている点だけが従来と違う
  ▼
既存の Timeline 編集画面 (ArchiveTimelineDialog / TimelineEditorDialog)
  │   AI が組んだ並び・テロップ・動き・SE が**最初から載っている**。
  │   利用者はいつもの操作で直す (AI の 1 手も Undo で戻せる / §11.1)
  ▼
既存の書き出し → 出力 + レビュー PDF (§12.6)
```

### 18.2 画面を変えずに AI の意図を伝える経路

画面に理由を出さない代わりに、次の 3 つで伝える。

| 伝えるもの | 置き場 |
| --- | --- |
| 何をどういう理由で採ったか / 構成・指摘・SE の提案 | **レビュー PDF** (§12.6)。これが主な読み物 |
| AI の出力そのもの (再現・検証用) | `<project>.plan.json` / `<project>.ai.json` |
| 適用できなかった指示の件数と理由 | ログ 1 行 (§10 / §17.2)。既存の進捗ラベルの範囲で済ませる |

* **設定画面にも新しいページを足さない。** `ai` セクションは `setting.json` を直接編集して調整する
  (`docs/design/design.md` §6.1 の「設定画面に出さない項目がある」と同じ扱い)。
  利用者へ出す必要が生じた段で、改めて画面の設計を行う。
* 進捗ラベル (工程名) だけは i18n へ足す必要がある (`src/i18n` の ja / en)。
  **これは画面の変更ではなく文言の追加**として扱う。モデル名は訳さない。

### 18.3 将来 (指示が出たときのための覚書)

次のものは「あると良い」が、今回は作らない。必要になったら改めて設計する。

* 候補一覧に AI の 8 軸スコアを出す (既存 `ArchiveResultWindow` + `score_graph_widget` の拡張)。
* インスペクタに AI の理由 (`reason`) を 1 行出す。
* 編集プロファイルの確認・仮説の採用/却下の画面 (今は JSON を直接見る / §13.3)。

> 画面デザインの変更は設計書で止める方針 (Figma で吟味してから指示) に合わせ、
> **本書では配置も含めて何も変えない**。

---

## 19. 対象外 (今回やらない)

| # | 項目 | 理由 |
| --- | --- | --- |
| 1 | **投稿・分析 (要望 §2 / §9 の 8)** | 外部 API 連携とサーバー設計が別件。本書は投稿まで含めない |
| 2 | **独自モデルの追加学習 (要望 §12.7 Phase 4)** | 学習データが貯まってから判断する。本書は Phase 1〜2 の器まで |
| 3 | **トランジション・映像エフェクト** | Timeline に概念が無い (Z5)。EditPlan に項目を作ると「受け手の無い指示」になる |
| 4 | **BGM** | SE トラックで音は置けるが、長尺 BGM のループ・ダッキングは未実装。`EditPlan.direction.bgm` は受け口だけ作り、適用しない。**PDF での提案は行う** (§12.6) |
| 5 | **新しい画面・タブ・インスペクタの追記** | 回答 Q12。既存 Timeline 画面に結果が出るだけ (§18) |
| 6 | **AI によるテロップ本文の全文生成** | 文字起こしが正。AI は修正指示のみ (§11.1 ⑤) |
| 7 | **`cut_region` のフレーム精度の改善** | 既知の後回し課題。本書は `cut_origin.measure` で現状どおり吸収する |
| 8 | **学習データのサーバー送信** | `ai.learning.upload = false` 固定 (回答 Q8 と同じ方針)。有効化は別途設計 (要望 §12.8) |
| 9 | **プロンプトの多言語化** | 画面文言は i18n だが、AI へのプロンプトは日本語で固定する (出力言語は `subtitle.language` に従わせる) |
| 10 | **Claude 以外のプロバイダーの実装** | 回答 Q1 により Claude で進める。`AiProvider` の抽象は残すので、差し替えはサーバー側の作業になる |
| 11 | **音声そのものを AI へ送ること** | フレーム画像までとする (§12.5)。音の問題は機械の測定で拾う |

---

## 20. テスト (`tests/`)

**AI プロバイダーへは一切繋がない。** `mock` プロバイダーと固定 JSON で検証する。

| ファイル | 検証内容 |
| --- | --- |
| `test_ai_config.py` | `ai` セクションの既定補完 / 不正値の丸め / `enabled=false` で `ai` を import しないこと |
| `test_ai_context_builder.py` | `AnalysisBundle` の縮約 (セル・コメント・文字起こしの間引き) / 投稿者名が落ちること / 原点を足して VOD 秒へ戻ること / 重なる候補を 1 回で流すこと |
| `test_ai_scene_finder.py` | 8 軸が候補へ重なること / 区間が候補の中に収まること / 重みで順位が決まること / 失敗したバッチが既存の採点へ落ちること / 評価が `<project>.ai.json` へ残り使い回されること |
| `test_ai_planner.py` | 2 段 (構成 → 編集指示) で呼ぶこと / project をこちらで埋めること / 各段の失敗が手元の案へ落ちること / 尺と本数の上限が効くこと |
| `test_ai_plan_schema.py` | EditPlan の必須キー / 未知 `plan_version` / 列挙値の丸め / `order` のとおり並ぶこと・同じ区間の二重使用が通ること (`test_ai_applier_order.py` は作らずここへ入れた) |
| `test_ai_plan_validator.py` | V1〜V15 の各違反が「落とす / 丸める / 例外」のどれになるか |
| `test_ai_plan_applier.py` | 適用後の Timeline が `project_io.validate` を通る / 既存コマンド経由で Undo できる / VOD↔素材↔Timeline の時刻が無音カットを跨いでも合う (`test_ai_time_coord.py` の内容はここへ入れた) |
| `test_ai_runner.py` | 手書きの EditPlan から プロジェクトと `<project>.plan.json` ができる / 素材が plan の順で用意される / 中止が効く / 縦プロジェクトができる / `run_ai` が解析〜プロジェクトまで通る (`test_ai_fallback.py` の「プロバイダー失敗で縮退して完走」もここ) |
| `test_ai_reviewer.py` | 役ごとに 1 回ずつ呼ぶこと / 前置きが共通であること / viewer に評価を渡さないこと / 修正の当て方 / 往復の上限 / フレームの枚数と削除 (`test_ai_vision_frames.py` の内容もここ) |
| `test_ai_consent.py` | 初回に同意を求めること / 版が上がったら再確認すること / 文面が設定と食い違わないこと |
| `test_ai_plan_diff.py` | §14.2 の各分類が検出されること / 許容内のぶれを差分にしないこと / 1 回だけの種類を学習しないこと |
| `test_ai_learning.py` | 既存プロジェクト JSON から統計が出ること (AI 不要の段) / プロファイルの読み書き・更新履歴・`min_evidence` 未満の仮説を使わないこと / 置き場が `%LOCALAPPDATA%` であること (`test_ai_edit_analyzer.py` と `test_ai_profile.py` をまとめた) |
| `test_ai_key_store.py` | 鍵が平文で保存されないこと / `setting.json` に書かれないこと |
| `test_ai_remote.py` | 要求本文の形 (role / schema / cache_prefix_id) / `ApiOfflineError` → `AiOfflineError` / `stop_reason="refusal"` を再試行しないこと |
| `test_ai_usage_log.py` | `usage` の 4 項目と解決モデルが 1 行のログへ残ること / キャッシュ 0 が続いたら警告すること |
| `test_ai_points_metering.py` | `charged=0` (計測のみ) と `charged>0` (課金) の**両方で完走**すること / 残高不足で AI 工程だけ縮退すること |
| `test_ai_vision_frames.py` | 枚数・長辺の上限 / 抜いたフレームを送信後に消すこと |
| `test_ai_report.py` | レビュー未実施でも PDF が出ること / PDF 不可の環境で HTML へ落ちること / 新規ライブラリを import しないこと |
| `test_command_stack_observer.py` | `observer=None` で現行と同じ / 失敗時と Undo では呼ばれないこと |

`test_ai_subtitle_animation.py` (コメント役割に動きが付かない / 廃止値が `""` になる) は
`test_ai_plan_schema.py` と `test_ai_plan_applier.py` へ、
`test_ai_fallback.py` (プロバイダー失敗で経路 B 相当まで縮退して完走する) は
`test_ai_scene_finder.py` / `test_ai_planner.py` / `test_ai_runner.py` へ、
`test_ai_cost_guard.py` (回数・トークン上限で打ち切る) は
`test_ai_usage_log.py` / `test_ai_points_metering.py` へ入れた。

既存テストが 1 件も壊れないことを合わせて確認する
(2026-11-21 時点で全 2900 件。`test_updater_verify` の 1 件だけは `%TEMP%` に
前のインストーラーが残っている環境で落ちる既知の件であり、AI とは無関係)。

---

## 21. 実装順

> **実装状況 (2026-11-21)** — **Phase 0〜3 のクライアント側が入った** (12 番の実測だけ残る)。
> サーバー側 (§22 T3) は実装済みであり、クライアントは `/api/ai/*` を呼べる状態にある
> (疎通の確認は `python tools/ai_smoke.py --enable --complete`)。
> `pipeline/ai_runner.run_plan` は手書きの EditPlan から、`run_ai` は
> **解析 → 候補の文字起こし → AI のシーン評価 → 構成 → 編集指示 → プロジェクト**まで通る。
> Phase 2 (レビュー・フレーム・読み物) と Phase 3 (統計・プロファイル・差分・操作ログ) も入り、
> 既存のアーカイブタブから `ai.enabled = true` で経路 D が通る (§18.1)。
> 残るのは 12 番 (トークンと費用の実測 → モデル割り当てとポイント単価の確定 / §22 T1) で、
> これは**サーバーをデプロイして実際に流さないと決められない**。
> 開ける手順 (鍵 → `Ai:Enabled` → 1 本流して突き合わせ) は
> `StretheusAPI docs/AiEditor/resolve1.md` の §12 末尾にある。測るための出口
> (工程ごとの `usage` 4 項目を 1 行 1 工程で残すログ) はこちら側に用意済み (§17.2)。

### Phase 0 — 器 (AI を呼ばずに動くところまで)

1. `exceptions.py` へ `AiError` 系を追加 — **済**
   (図では兄弟だった `AiOfflineError` は、既存の `ApiOfflineError(ApiError)` と同じく
   `AiProviderError` の派生にした。`status` / `code` をそのまま持てるため)
2. `settings` へ `ai` セクション (`DEFAULT_SETTINGS` + `setting.json`) と `ai/config.py` — **済**
3. `ai/provider.py` (抽象 + `mock`) / `ai/key_store.py` (開発用) — **済**
   (`DirectProvider` は実装を持たない。サーバーの口ができた今は役目が無く、
   配布物へ公式 SDK を入れない決まりもあるため、`ai.direct_mode = true` のときに
   分かる形で失敗させる。鍵の保管だけは測り直しのために残した / §6.5)
4. `ai/plan_schema.py` / `ai/plan_validator.py` — **済** (2026-11-21)
   (**ここまでで EditPlan を手書きしてテストできる**。実装時に決めたこと:
   ・`subtitles[].track_role` は線の上では `own`/`sub`/`comment` で、Timeline のモデルでは
     `streamer`/`sub`/`comment` である。写しは `plan_schema.model_track_role` 1 か所に閉じ、
     既存の値と食い違わないことを `test_ai_plan_schema` が `timeline.model` と突き合わせる
   ・V4 の折り返しは `modules/subtitle_generator.wrap_lines` を使わない。同モジュールは
     `settings_window` 経由で Qt を引くため、`ai/` の Qt 非依存 (§4) を壊す。
     文字数の上限 (`max_line_length × subtitle_max_lines`) だけをここで守り、
     実際の行分けは既存の経路に任せる
   ・V3 は「書かれていない」を記録しない。既定の採用は直しではないため
   ・V8 の素材確認は `plan_validator.Materials` へ注入する (`vod_range.fetch_range` を
     直接呼ばない)。ranges も ensure も無ければ素材を問わず通すので、手書きの plan が通る
   ・V10 は合計尺に加えて `max_scenes` も見る。落とすのは後ろの `body` からで、
     `opening`/`climax`/`hook`/`punchline` は残す。落とせる役割が尽きたら最後の終わりを詰める
   ・V15 は `plan_applier` が持つ。`CommandStack` のスナップショットで巻き戻せる場所でしか
     判定できないため、検証側には置かない
   ・シーン評価の欠けた軸は 0 ではなく中立 (50) へ。1 軸の欠落で面白い場面を沈ませない)

### Phase 1 — MVP (要望 §9.1)

5. `ai/context_builder.py` (既存 `analyze` + 候補の文字起こし) — **済** (2026-11-21)
   (`analysis_settings` / `build` / `transcribe_candidates` / `for_prompt` / `batches`。
   実装時に決めたこと:
   ・`ai.candidates.allow_overlap` は**効かない**。窓の重なりを弾くのは
     `archive/scoring.select_top_events` の内部 (`_overlaps`) で、設定で外せない。
     採点側を変えずには実現できないため、有効にしたときは「未対応」を 1 行ログへ出す
   ・`ai.transcribe.speaker_filter` は「既存の `subtitle.speaker_filter` に従う」に落とした。
     既定が `"off"` なのは**実測で成立しなかったため** (ゲーム音が乗る VOD では声紋が
     話者を表さない / `docs/quality/resolve1.md` §12.6)。AI 用に勝手に `"drop"` へ上げない
   ・`cells` は `analyze` の戻り値に無いため、渡されたときだけ持つ
     (`features.extract_cells` を直接呼ぶ経路のため)。無くても評価は音量以外の材料で通る
   ・切り出しと文字起こしの既定実装は `archive/clip_writer` (Qt を引く) なので
     **関数の中で import する**。差し替えればテストは ffmpeg も Qt も無しで通る)
6. `ai/plan_applier.py` (既存コマンド経由の適用) — **済** (2026-11-21)
   (§11.1 の工程のうち ③〜⑦ と ⑨ を持つ。実装時に決めたこと:
   ・**② 並べ替えは行わない。** `MoveClip` は同一トラックの重なりを避けて位置を丸める
     (`commands._clamp_position`) ため並べ替えの手段にならず、テロップ・SE も一緒に
     動かす必要があって既存コマンドの範囲を超える。代わりに **`ai_runner` が
     「plan が使うシーンだけを plan の順で」素材を用意する**ことで並びを満たす。
     渡された Timeline が plan と違う並びのときは ③ (リップル削除) と ④ で寄せる
   ・③ は `DeleteClip(ripple=True)`、④ は `RippleTrimToPlayhead` を使う。
     どちらも全トラックを詰めるため、テロップと SE がシーンに付いて回る
   ・① 骨組み (素材の用意と Timeline の構築) は `ai_runner` が持つ。ffmpeg を回す工程であり、
     進捗とキャンセルを持つ側に置くのが筋である
   ・⑤ は「同じ場所に既存のテロップがあれば直す / 無ければ足す」。本文は文字起こしが正 (§19 の 6)
   ・V15 は `project_io.validate` が例外を投げない (直して続ける) 実装のため、
     **クリップが消えたか**で判定する。消えたら積んだ手を全て `stack.undo` で戻して `AiPlanError`。
     素材のファイルがまだ無いだけのときは無効化されるだけなので巻き戻さない
   ・ここは Qt 非依存にならない。`timeline/project_io` → `builder` → `compositor` →
     `modules/subtitle_generator` → `settings.settings_window` が PySide6 を引くためである。
     判断系 (`config` / `provider` / `remote` / `plan_schema` / `plan_validator` / `usage_log`)
     は Qt を引かないままにしてあり、§4 の「`gui/` を import しない」は全モジュールで守っている)
7. `pipeline/ai_runner.py` (経路 D。この時点では**手書きの EditPlan** を食わせて通す) — **済** (2026-11-21)
   (`run_plan(input, settings, plan, target=…)` で ①事前確認 → ②検証 → ③素材 → ④Timeline →
   ⑤適用 → ⑥保存 → ⑦縦 まで通り、`<project>.timeline.json` と `<project>.plan.json` ができる。
   実装時に決めたこと:
   ・**検証は素材より先**に行う。使えない指示のために ffmpeg を回さない
   ・素材は `<プロジェクト名>.media` へ置く (`project_io.media_dir_path` の既存規約)。
     一時フォルダへ置くと保存したプロジェクトを開き直せない
   ・⑦ 縦は `vertical_builder.build` を既定の `media_mode="copy"` で呼ぶ (元 VOD は要らない)。
     切り抜きの枠は素材 1 件ぶんしか持てないため、plan のシーンごとの `crop.mode` は
     **最も多いものへ寄せる**。`face` は `blur` の検出が要るので当面 `center` と同じ扱い。
     縦が作れなくても横のプロジェクトは完成しており、後から作り直せる
   ・レンダリング・出力 ([6]) は行わない。保存したプロジェクトを既存の経路が開いて書き出す
     (§11.2 の「工程 [6] より後ろは経路 A/B/C と完全に同一」))
8. `ai/remote.py` + `DirectProvider` (開発用の直接呼び出しで先に判断の質を見る) — **済**
   (サーバーの口が先に出来たため、直接呼び出しは作らずサーバー経由へ寄せた。
   利用量の記録と上限は `ai/usage_log.py`)
9. `ai/scene_finder.py` (3 段階) — **済** (2026-11-21)
   (`evaluate` / `rank` / `merge_overlaps` / `top_scenes` / `save_eval`。実装時に決めたこと:
   ・AI が返した区間は**候補の範囲内へ収める**。外へ伸ばした分は切り、
     区間が壊れていれば候補の範囲へ戻す (素材が無い時刻を選ばせない)
   ・評価できなかった候補は落とさず、8 軸を中立 (50) にして既存の採点だけで順位へ乗せる。
     バッチ単位で失敗したときも他のバッチは続ける (§11.2 の縮退)
   ・評価は `<project>.ai.json` へ残す。候補の時刻が 60 秒以上動いていたら
     古い評価は使わない (候補の作り方を変えたら作り直す)
   ・プロンプトは `ai/prompts.py` の Python 定数にした。設計 §4 では `ai/prompts/*.md`
     だったが、PyInstaller で .md を同梱するには `main.spec` の datas を触る必要があり、
     文言は日本語固定で i18n の対象外 (§19 の 9) なため外に出す利点が無い)
10. `ai/planner.py` (長尺 / Shorts / TikTok) — **済** (2026-11-21)
   (`build` が 2 段で呼ぶ: ① VideoPlan (§8.1) → ② EditPlan (§9)。実装時に決めたこと:
   ・**どちらの段も、失敗したら手元で組んだ案へ落として完走する** (§11.2)。
     ② が落ちた場合は「シーンの並びだけ」の EditPlan になり、テロップは
     既存の自動生成がそのまま残る (本文は文字起こしが正 / §19 の 6)
   ・EditPlan の `project` は**こちらで埋める**。モデルに素材の情報 (video_id・パス・
     キャンバス) を作らせない
   ・section が指すシーンが無いときは時刻の近いシーンへ寄せ、区間はシーンの範囲へ収める
   ・長尺と短尺は別々に呼ぶ。重みの組 (`long` / `shorts`) は `planner.use_of(target)` が決める)
11. **`StretheusAPI` 側の `/api/ai/*`** (公式 SDK・鍵・利用量の記録・計測のみのポイント応答) — **済**
    (`StretheusAPI docs/AiEditor/resolve1.md`。既定は `Ai:Enabled = false` / `ChargeMode = measure`)
12. トークンと費用の実測 → 役割ごとのモデル割り当てとポイント単価の確定 (§22 T1)

### Phase 2 — レビューと PDF

13. `ai/reviewer.py` + 修正ループと上限 — **済** (2026-11-21)
    (5 役・機械の検査・採否・修正の当て方。実装時に決めたこと:
    ・**レビューは EditPlan に対して回し、Timeline への適用は最後に 1 回**にした。
      設計 §12.4 のループは「適用 → レビュー → 修正 → 適用」だが、1 往復ごとに
      素材を作り直す (ffmpeg) コストが現実的でない。フレームは元 VOD から抜くので
      適用前でもレビューに回せる
    ・修正は決定的な操作だけを当てる。当てられない指摘 (伸ばす trim・未登録の
      プリセット・コメント役割への動き) は**当てずに記録へ残す**
    ・`qa` は機械の測定結果だけを渡す。測るのは時間と本数に限り、音量の実測は
      書き出し時の既存の検証に任せる)
14. `ai/vision.py` (フレームの抜き出し / §12.5) — **済** (2026-11-21)
    (抜く元は**元 VOD** である。設計 §12.5 は「Timeline から」としていたが、
    レビューが適用前のため Timeline がまだ無い。縦の crop を当てた絵は作れないので
    「クロップで人物が切れている」は当面この経路では拾えない。
    既存 `timeline/frame_source` ではなく ffmpeg で 1 枚書き出す
    (送信用に base64 で読み、読んだら消すため)。)
15. `ai/review_report.py` + PDF 書き出し (§12.6) — **済** (2026-11-21)
    (本文は `ai/review_report.py` が HTML 1 枚として組み (Qt 非依存)、
    PDF 化は `gui/ai_report.py` が PySide6 の QTextDocument + QPdfWriter で行う。
    PDF ライブラリは増やしていない。PDF にできない環境では HTML を残す。
    構成の表は VideoPlan ではなく EditPlan から作る (planner の中で VideoPlan が
    消えるため。役割・区間・理由は EditPlan にも載っている)。)

### Phase 3 — 学習 (要望 §12)

16. `ai/edit_analyzer.py` (統計だけ。AI 不要) — **済** (2026-11-21)
    (プロジェクト JSON を `project_io` を通さず**生の JSON として読む**。
    統計に素材は要らず、素材が消えた古いプロジェクトも数えたいためである。)
17. `ai/profile.py` (プロファイル + 履歴 + 利用者の訂正) — **済** (2026-11-21)
    (置き場は `ai/store.py` = `%LOCALAPPDATA%\Stretheus\ai\profiles`。
    仮説の `evidence` は **AI の自己申告を信用せず差分の実数を上限にする**。
    `min_evidence` 未満と `rejected` はプロンプトへ入れない。)
18. `ai/plan_diff.py` (AI 版スナップショットの保存を含む) — **済** (2026-11-21)
    (スナップショットは `run_plan` が適用直後に `<project>.ai-base.json` へ 1 回だけ保存する。
    差分は**保存の瞬間**に `TimelineEditorDialog.save_project` から
    `plan_diff.record_for_project` 1 回で取る。VOD 時間へ戻して突き合わせるため、
    1 か所のカットで以降が全部「変更された」ことにならない。)
19. `ai/edit_log.py` + `CommandStack` の観測口 (任意) — **済** (2026-11-21)
    (`CommandStack(limit, observer=None)` と `set_observer` を足した。既定は None =
    現行と完全に同じ挙動で、`push` が成功したときだけ呼ぶ。
    §15 の「公開属性から拾う」だけでは何も残らないコマンドが多いため、
    **先頭の下線を外した名前**で拾う (36 クラスへ `to_log()` は足していない)。)

### 画面の繋ぎ込み (§18.1) — **済** (2026-11-21)

* `gui/archive_tab.py` に `ArchiveAiWorker` を足し、採点が終わった時点で
  `ai.enabled = true` なら経路 D へ分岐する。**画面は増やしていない**。
  AI が保存したプロジェクトは既存の「編集の続き」経路 (`_start_resume`) が開くため、
  以降の編集・書き出しは経路 A/B/C と完全に同一である。
* レビューの読み物の PDF 化は画面側 (`gui/ai_report.py`) で行う
  (`pipeline` から `gui` を import しないため)。
* 起動時に `ai/store.cleanup` を呼び、`keep_days` より古い**差分だけ**を片付ける。

> Phase 1 の 7 番が要点である。**AI を呼ぶ前に「EditPlan を食わせれば動画ができる」状態**を作っておけば、
> AI 側の品質とエンジン側の不具合を切り分けられる。
>
> 8 番と 11 番の順序も意図的である。サーバーの口ができるまで待つと判断の質を測れないため、
> **開発端末では直接呼び出しで先に進め、サーバーの口ができた時点で既定を差し替える。**
> クライアントは `AiProvider` しか知らないため、この差し替えで他のモジュールは変わらない。

---

## 22. 確定事項と残件 (2026-11-20 回答)

### 22.1 確定した方針

| # | 確認したこと | 回答 | 反映先 |
| --- | --- | --- | --- |
| **Q1** | プロバイダーとモデル | **Claude を使う。複数のモデルを役割で使い分け、トークンを効率よく使う** | §6.3 (割り当て表) / §6.4 (キャッシュ) / §16 (`ai.models` / `ai.effort`) |
| **Q2** | 利用料の負担 | **Stretheus のポイント制にして利用者の負担を減らす。ポイントとトークン・課金額の釣り合いは複数回のテストで決める** | §2.3 案 B / §6.1〜§6.2 (サーバー代行) / §17.3 (計測と校正) |
| **Q3** | AI 機能のポイント消費 | **既存ポイント制の位置づけ自体が保留のため保留** | §17.3 (「計測のみ」と「課金する」の両方を壊さず通す) |
| **Q4** | 全長の文字起こし (§7.4) | **一旦 OFF。テストが必要なことを記録する** | §16 (`ai.sweep.enabled = false`) / §22.2 T2 |
| **Q5** | 映像を AI に見せるか | **見せてよい** | §12.5 (フレームの抜き方・枚数・大きさ) |
| **Q6** | TikTok | **有効にする** | §16 (`ai.plan.targets` に `tiktok`) / §12.1 (`platform` 役を有効) |
| **Q7** | 学習の既定と保持期間 | **既定で学習する。期間は半年** | §13.4 (`enabled = true` / `keep_days = 180`) |
| **Q8** | コメント投稿者名 | **送らない** | §5.5 (`privacy.include_comment_user = false`) |
| **Q9** | SE プリセットと**レビュー PDF** | **プリセットの既定は空。あわせて「AI 的にこういう SE が欲しい」を含む動画全体のレビュー PDF を作る** | §12.6 (R15) / §16 (`ai.report.*`) |
| **Q10** | 長尺の上限 | **一旦 15 分** | §16 (`youtube_long.max_sec = 900`) |
| **Q11** | 構成案の数 | **1 案でよい** | §16 (`ai.plan.variants = 1`) |
| **Q12** | 画面 | **画面を変更する予定はない。AI を含む編集結果は Timeline 画面に出る** | §18 (新しい画面を作らない。意図は PDF と JSON で伝える) |

### 22.2 残件 (実装の前後で必ず片付けるもの)

| # | 残件 | 扱い |
| --- | --- | --- |
| **T1** | **トークンと費用の実測 → 役割ごとのモデル割り当て・`effort`・ポイント単価の確定** (Q1 / Q2) | Phase 1 の最後 (実装順 12)。同じ VOD で設定を変えて複数回流し、`usage` の 4 項目を工程別に集計する。§6.3 の表は**測定結果で上書きする前提の初期値**である。集計の出口は用意済みで、ログの `AI 内訳` 行がそのまま工程別の表になる (§17.2)。**サーバーを `Ai:Enabled = true` でデプロイしないと測れない**ため、残っているのは測定そのものだけである |
| **T2** | **全長の文字起こし (`ai.sweep`) の実測** (Q4) | 時間・費用・拾えるシーンの質を測ってから既定を決める。**忘れないこと**。測るまで `false` のまま |
| **T3** | ~~**`StretheusAPI` 側の `/api/ai/*` の設計と実装** (Q2)~~ → **完了 (2026-11-20)** | `StretheusAPI docs/AiEditor/resolve1.md` に設計と実装。線の上の名前は camelCase で、追補 A1〜A3 を含む (§6.2 の実装時の確定)。既定は無効 (`Ai:Enabled = false`) で、単価の確定までは `measure` |
| **T4** | 映像レビューの枚数・大きさの既定 (Q5) | `frames_per_scene = 1` / `max_edge_px = 768` で始め、T1 と同時に実測して見直す |
| **T5** | レビュー PDF の見た目 | 本文の構成 (§12.6) は決めた。配色・組みは既存 `docs/quality/resolve1.html` に合わせる。凝るなら別途指示を待つ |
| **T6** | SE プリセットの中身 (Q9) | 既定は空。PDF の提案を見てから「よく欲しがる音」を登録する。音源の同梱可否は別件 |
| **T7** | ポイント制そのものの位置づけ (Q3) | 決まるまで AI 側は「計測のみ」で動かす。決まったら `ai.cost.confirm_over_points` と残高不足時の縮退を有効にする |

---

## 付録 A. 要望の各節と本書の対応

| 要望 | 本書 |
| --- | --- |
| §2 役割分担 | §1.1 (既存の担い手の実測) / §4 (層の分離) |
| §3 処理フロー | §11.2 (経路 D) |
| §4 面白シーン抽出 | §7 (3 段階) / §5 (入力の作り方) |
| §5 動画企画・構成 | §8 (VideoPlan。長尺と短尺を別呼び出し) |
| §6 編集指示生成 | §9 (EditPlan スキーマ) / §9.3 (受け手の対応表) |
| §7 既存編集エンジンとの連携 | §11 (既存コマンド経由) / §10 (検証) |
| §8 品質レビュー | §12 (5 役・結果スキーマ・上限) |
| §9 優先順位 / MVP | §21 (実装順) |
| §10 共通原則 | §4 (再利用と依存) / §6.1 (プロバイダー非依存) / §10 (必ず検証) |
| §11 最終目標 | §11.2 + §13 (プロファイル) |
| §12 編集ログ解析・学習 | §13 (解析とプロファイル) / §14 (差分) / §15 (操作ログ) |

要望に無く、2026-11-20 の回答で足したもの:

| 追加 | 本書 |
| --- | --- |
| Claude の複数モデルの使い分け (Q1) | §6.3 / §6.4 |
| ポイント制での負担 (Q2 / Q3) | §6.1〜§6.2 / §17.3 |
| 映像 (フレーム) を見せるレビュー (Q5) | §12.5 |
| **動画全体のレビュー PDF** (Q9) | §12.6 (R15) |
| 画面は変更しない (Q12) | §18 |
