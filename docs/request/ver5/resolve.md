# resolve（ver5） — ポイント制 + watermark 詳細設計書

## 0. 本書の位置づけ

* 対象: `D:/develop/StretheusPlan/plan.md` の **P2「ポイント制 + watermark」**（基本設計）。本書はその詳細設計にあたる。
* サーバー側 (StretheusAPI) は **P1 で実装・dev デプロイ済み**である。
  API の契約は `D:/develop/StretheusAPI/docs/request/resolve2.md` §3 / §6.6 を正とし、本書はそれを前提に
  **autoedit 側の設計だけ**を扱う。API を変更する必要は無い。
* クライアントの認証部分 (`src/services/api_client.py` / `auth_store.py` / `stretheus_auth.py`) は
  **P1 Phase 4 で実装済み**であり、本書はその上に `points.py` と watermark を載せる。
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「実装前に設計を行うこと」）。
* 調査は **2026-09-15 時点の実コード**を読んで行った。plan.md の記述と実コードが食い違う箇所、
  および P1 で確定した内容によって plan.md の前提が変わった箇所は §2 で訂正している。
* 要望から一意に決められない論点は §9「確認事項」へ挙げた
  （`docs/claude.md`「不明点がある場合は推測実装せず設計書へ記載すること」）。

---

## 1. 要望と要件 ID

| ID | 要件 | 出典 |
| --- | --- | --- |
| R1 | 出力 1 回ごとにポイントを消費する。クリップ 25pt / アーカイブ 100pt | plan.md P2 仕様 |
| R2 | 消費対象は**動画の出力**と **DaVinci Resolve への書き出し**の 2 つだけ | resolve2 §3.0 |
| R3 | 消費の確定は成果物の完成時 (`output_writer.finalize` 成功後 / Resolve 書き出し成功後) | plan.md P2 / resolve2 §6.6 |
| R4 | 残高不足でも出力をブロックせず、watermark を焼き込む | plan.md P2 |
| R5 | サブスク会員は消費なし・watermark なし | plan.md P2 |
| R6 | 失敗・中断時は消費しない (cancel)。部分成功も失敗として扱う | resolve2 §3.0 |
| R7 | 同じ出力操作の再送で二重に消費しない (`clientJobId` による冪等性) | resolve2 §6.6 |
| R8 | watermark は出力解像度によらず同じ見え方にする (キャンバス幅に対する比率で配置) | plan.md P2 |
| R9 | watermark の有無はサーバーの応答だけで決める。クライアントは残高から推測しない | resolve2 §6.6 |
| R10 | watermark はユーザーが UI から無効化できない | plan.md P2 |
| R11 | 残高インジケータを常時表示する | plan.md P2 UI |
| R12 | 残高不足時は出力開始前に確認ダイアログを出す | plan.md P2 UI |
| R13 | 設定画面に「アカウント」タブ (ログイン状態・残高・履歴・サブスク導線) | plan.md P2 UI |
| R14 | commit を送れずにアプリが終了した場合、次回起動時に再送する | resolve2 §6.6 |

---

## 2. 現状分析

### 2.1 出力経路は 4 つある (plan.md の記述より多い)

plan.md は「クリップ側 = `pipeline_runner`」「アーカイブ側 = `archive_tab`」の 2 つを挙げているが、
実コードでは**動画出力が 2 経路 × 2 モード**、加えて **Resolve 書き出しが 3 入口**ある。

| # | 経路 | 実体 | 最終出力 |
| --- | --- | --- | --- |
| A | クリップ / Timeline モード | `pipeline_runner._run_timeline` → `renderer.render` → `output_writer.run` | 1 本 |
| B | クリップ / レガシーモード | `pipeline_runner._run_legacy` → `output_writer.run` | 1 本 |
| C | アーカイブ / Timeline モード | `clip_writer.finish_clips` → `_render_clips` → **クリップごとに `renderer.render`** → 結合 | 1〜N 本 |
| D | アーカイブ / レガシーモード | `clip_writer.finish_clips` → `_burn_clips` → `_burn_one` (`subtitle_generator.burn_subtitle`) → 結合 | 1〜N 本 |

Resolve 書き出しの入口 (`src/export/resolve_export.py`):

| 入口 | 呼び出し元 |
| --- | --- |
| `export_timeline` | Timeline 編集画面 |
| `export_clip_review` | `src/gui/subtitle_editor_dialog.py` |
| `export_archive_result` | `src/gui/archive_result_window.py` |

いずれも最終的に `export_spec` → `fcpxml_builder.build_fcpxml` を通る。**ここが 1 か所の合流点**になる。

### 2.2 `renderer.render` の最終工程は 3 つに分岐する

`src/timeline/renderer.py:410 _render_overlays_and_subtitles` は次の 3 通りに分かれる。

| 分岐 | 条件 | 再エンコード |
| --- | --- | --- |
| そのまま返す | オーバーレイも字幕も無い | なし |
| `_burn_subtitles_only` | 字幕のみ | 1 回 |
| `_composite` | オーバーレイあり | 1 回 |

plan.md は「既存の overlay 工程へ watermark を混ぜ込むのが最も安価」としているが、
**混ぜ込めるのは `_composite` だけ**である。残り 2 分岐と経路 B / D には合流点が無い。

### 2.3 クライアント側 SQLite は存在しない

plan.md と `StretheusAPI/docs/calude.md` は「クライアント側 SQLite」を前提に書かれているが、
**実コードに `sqlite3` の利用は 1 か所も無い** (`src/` 全体を検索して 0 件)。
R14 の commit キューのために SQLite を新規に導入するかどうかは方式選定 (§3.3) で決める。

### 2.4 認証まわりは実装済み

P1 Phase 4 で次が入っている。本書はこれらをそのまま使う。

```text
src/services/
    api_client.py       ベース URL 結合・Bearer 付与・401 時の再送・ProblemDetails の例外化
    auth_store.py       JWT の DPAPI 保存
    stretheus_auth.py   認可コードフローのログイン・リフレッシュ (直列化)・ログアウト
    config.py           setting.json の api セクション読み出しと StretheusAuth の生成
    login_check.py      手動疎通確認用 CLI
```

`setting.json` には `api.base_url` / `api.timeout_sec` が追加済み。
現在の既定は本番 (`https://stretheusapi.azurewebsites.net`) だが、
**本番は未デプロイ**のため、開発中は dev (`https://stretheusapi-dev.azurewebsites.net`) へ向ける必要がある (§9-5)。

### 2.5 plan.md から変わった前提 (P1 で確定済み)

| plan.md の記述 | 確定した内容 | 根拠 |
| --- | --- | --- |
| 消費は「動画が出力されたとき」 | 動画出力**と Resolve 書き出し**の 2 つ | resolve2 §3.0 |
| 未決 #2「アーカイブ 100pt の単位」 | **ジョブ単位で 100pt**。1 回の出力で N 本書き出しても 1 予約 | resolve2 §11.1 #5 |
| 未決 #3「部分成功の扱い」 | **扱わない。** 全体成功で commit、それ以外は cancel | resolve2 §11.1 #6 |
| 予約の期限 | 360 分。超過後の commit は遅延確定としてサーバーが受け付ける | resolve2 §3.5 |
| `POST /api/points/reservations` の応答 | `watermarkRequired` / `unlimited` / `expiresAt` / `wallet` を含む | resolve2 §3.9 |

### 2.6 watermark 素材

`src/watermark.png` は **2009 × 783 px / 735 KB** の PNG。現状どこからも参照されていない。
1920 幅のキャンバスに対して原寸では大きすぎるため、必ず縮小して使う (R8)。
アルファチャンネルの有無と視認性は未検証 (§9-2)。

---

## 3. 方式選定

### 3.1 watermark の焼き込み位置

| 案 | 内容 | 再エンコード | 実装箇所 | 判定 |
| --- | --- | --- | --- | --- |
| 案 1 | 完成した出力ファイルへ必ず 1 パス追加する | **+1 回** | `output_writer` と `clip_writer` の 2 か所 | 不採用 |
| 案 2 | `_composite` のフィルタチェーンへ混ぜ込む | +0 回 | `renderer` のみ | 不採用 (経路 B / D と他 2 分岐を覆えない) |
| **案 3** | **合成が走る経路は混ぜ込み、走らない経路だけ 1 パス追加する** | 多くの場合 +0 回 | `renderer` + 各経路の保険 | **採用** |

案 3 の要点は「**適用済みかどうかを `PipelineContext` の 1 つのフラグで管理し、未適用のまま出力させない**」ことである。

```text
renderer._render_overlays_and_subtitles
    ├─ _composite が走る        → フィルタチェーンの最後へ overlay を足す  → applied = True
    ├─ _burn_subtitles_only     → その後に単独パスを追加                    → applied = True
    └─ 何も無い                 → 単独パスのみ                              → applied = True

output_writer.run / clip_writer.finish_clips
    └─ applied が False なら単独パスを追加 (経路 B / D の保険)
```

これにより、**経路が増えても「出力の直前に必ず確認する」1 点で守れる**。
`_burn_subtitles_only` へ混ぜ込まないのは、この分岐が
`subtitle_generator.burn_subtitle` (A/V 同期対策を持つ既存の共有関数) をそのまま使っており、
ここへフィルタを差し込むと字幕焼き込み全体の挙動に影響するためである。

### 3.2 予約の単位と生成タイミング

| 出力操作 | `jobType` | `outputType` | 予約のタイミング | commit のタイミング |
| --- | --- | --- | --- | --- |
| クリップの動画出力 (経路 A / B) | `clip` | `video` | `run_pipeline` の開始時 | `output_writer.run` 成功後 |
| アーカイブの動画出力 (経路 C / D) | `archive` | `video` | `write_clips` の開始時 | `finish_clips` が出力を返した後 |
| Resolve 書き出し (クリップ) | `clip` | `resolveProject` | `export_*` の開始時 | `_atomic_write` 成功後 |
| Resolve 書き出し (アーカイブ) | `archive` | `resolveProject` | 同上 | 同上 |

`clientJobId` は**出力操作 1 回につき 1 つ** `uuid.uuid4()` で生成する。再送では同じ値を使う (R7)。

### 3.3 オフライン時の扱い (plan.md 未決 #1)

plan.md は (a) watermark 付きで出力 / (b) 72h のローカル猶予 / (c) ブロック の 3 案を挙げ、(b) を推奨している。
実装コストと安全性を評価した結果、**(a) を採用して (b) は将来対応とする**ことを提案する (§9-1 で確認)。

| 案 | 利用者から見た挙動 | 実装コスト | リスク |
| --- | --- | --- | --- |
| (a) watermark 付きで出力 | ネット断でも出力できる。ただし watermark が入る | 小 | サブスク会員がネット断で watermark 入りになる |
| (b) 72h 猶予 | ネット断でも通常どおり出力できる | **大** | ローカル台帳の署名・突き合わせ・不整合時の扱いを新設する必要がある |
| (c) ブロック | 出力できない | 小 | 配信者の現場で嫌われる |

(b) を推奨から外す理由:

* **ローカル台帳はサーバーの残高と必ずずれる。** 突き合わせの規則 (どちらを正とするか、
  猶予中に使い切った場合の事後処理) を決める必要があり、これは P2 の本題である
  「出力に watermark を入れる」より複雑になる。
* サーバーは既に**遅延確定**を持つ (期限切れ予約でも commit を受け付ける / resolve2 §3.5)。
  「予約だけ取れていれば後から確定できる」ため、猶予が要るのは**予約すら取れない場合**に限られる。
* オフラインでの出力は例外的な状況であり、そこで watermark が入ることは
  「回避を許さない」という R10 の方針とも整合する。

ただし (a) でも、**サブスク会員だけは救済する**。直近のサブスク判定結果を
`%LOCALAPPDATA%\Stretheus\points.json` にキャッシュし、オフライン時にこれが
有効期間内 (既定 72 時間) なら watermark 無しで出力する。
サブスク状態は残高と違い「消費して減る」ものではないため、キャッシュしても不整合が起きない。

### 3.4 commit の再送 (R14)

SQLite は導入せず、**JSON ファイルのキュー**とする。

* 保存先: `%LOCALAPPDATA%\Stretheus\pending_commits.json`
* 内容: `[{reservationId, clientJobId, jobType, outputType, completedAt}]`
* 契機: commit / cancel の送信に失敗したら追記し、**次回のアプリ起動時**と
  **次回の出力の予約直前**に再送する。commit / cancel は冪等なので何度送っても安全である。
* 予約の期限 (360 分) を過ぎていてもサーバーは遅延確定として受け付ける。

SQLite を使わない理由: 保存するのは高々数件のキューであり、検索も集計も不要である。
`sqlite3` は標準ライブラリだが、新しい永続化層を 1 つ増やすことに変わりはない
(`docs/claude.md`「不要なライブラリを追加しない」の趣旨)。
将来 Analytics のキューを作る際に SQLite が必要になったら、そのとき一緒に移す。

---

## 4. 設計方針

1. **watermark の有無はサーバーの応答だけで決める** (R9)。クライアントは残高から推測しない。
   予約の応答 `watermarkRequired` をそのまま `PipelineContext` へ載せる。
2. **未適用のまま出力しない。** 出力の直前に `watermark_applied` を確認し、未適用なら単独パスを追加する。
3. **ポイント処理で出力を壊さない。** API 呼び出しの失敗は出力を止めない (§3.3 の (a))。
   例外はすべて `points.py` の中で握り、呼び出し側へは「watermark が要るか」だけを返す。
4. **既存の出力経路の構造を変えない。** 予約・commit・cancel は各経路の入口と出口へ足すだけとし、
   レンダリングや結合のロジックには触らない。
5. **未ログインでも従来どおり使える。** ログインしていない場合はポイント API を呼ばず、
   watermark も入れない (現行ユーザーの体験を変えない)。課金の開始は別途アナウンスしてから行う (§9-4)。

---

## 5. 詳細設計

### 5.1 モジュール構成

```text
src/services/
    points.py              新規。残高照会・予約 / 確定 / 解放のファサード、サブスクキャッシュ、commit キュー
src/modules/
    watermark_overlay.py   新規。FFmpeg の overlay フィルタ生成と単独パス適用
src/gui/
    points_indicator.py    新規。残高インジケータ (R11)
```

既存方針どおり **標準ライブラリのみ**で実装する (`docs/claude.md`)。

### 5.2 `points.py`

```python
# 出力操作 1 回分のポイント予約を表す。watermark_required 以外はクライアントで判断に使わない。
class Reservation:
    reservation_id, client_job_id, job_type, output_type
    watermark_required, unlimited, amount, expires_at, offline

class PointsService:
    def __init__(self, auth, store_dir=None)

    # 残高を取得する。オフライン時は None を返す (UI は最後に取得した値を薄字で出す)。
    def balance(self, force=False) -> dict | None

    # 出力の開始時に予約する。API へ到達できない場合も Reservation を返す (offline=True)。
    def reserve(self, job_type, output_type, project_id=None) -> Reservation

    # 成果物の完成時。送信に失敗したらキューへ積む。
    def commit(self, reservation) -> None

    # 失敗・中断時。送信に失敗したらキューへ積む。
    def cancel(self, reservation) -> None

    # 起動時と予約の直前に呼ぶ。キューに残った commit / cancel を再送する。
    def flush_pending(self) -> None
```

`reserve` の戻り値の決め方:

| 状況 | `watermark_required` | `offline` | 備考 |
| --- | --- | --- | --- |
| 予約成功 | サーバーの応答のまま | False | |
| 未ログイン | **False** | True | 現行ユーザーの体験を変えない (§4-5) |
| オフライン + サブスクキャッシュが有効 | **False** | True | §3.3 |
| オフライン + キャッシュ無効 | **True** | True | §3.3 |
| API が 4xx を返した | True | False | 想定外。安全側 (watermark あり) に倒す |

`offline = True` の予約は `reservation_id` を持たないため、commit / cancel は何もしない。

### 5.3 `watermark_overlay.py`

```python
# 透かしを入れるか判定する (未適用かつ必要なとき True)
def is_required(context) -> bool

# _composite のフィルタチェーンへ足す入力とチェーンを返す (経路 A / C)
#   戻り値: (input_args, chain, out_label)
def build_chain(in_label, out_label, canvas_width, canvas_height, cfg) -> tuple

# 完成した動画へ単独パスで焼き込む (経路 B / D と、合成が走らない場合)
def apply(input_path, output_path, canvas_size, cfg, ffmpeg_cfg, on_progress=None) -> str

# Resolve 書き出し用。タイムライン全長へ配置する watermark クリップの仕様を返す
def resolve_clip_spec(canvas_width, canvas_height, duration_sec, cfg) -> dict
```

フィルタの組み立て (R8):

```text
[1:v] scale=<canvas_width * scale_ratio>:-1,
      format=rgba,colorchannelmixer=aa=<opacity>     [wm]
[<in>][wm] overlay=<x>:<y>                            [<out>]
```

* `scale_ratio` はキャンバス**幅**に対する比率。1920×1080 と 1080×1920 で同じ見え方になる。
* 位置は `position` (`bottom_right` 既定) と `margin_ratio` から算出する。
  余白もキャンバス幅比で計算する。
* 素材は `src/watermark.png`。凍結配布 (PyInstaller) でも参照できるよう、
  `main.spec` の `datas` に含まれているかを実装時に確認する (§8-3)。

### 5.4 動画出力への組み込み

#### 経路 A / B (クリップ)

```python
# src/pipeline/pipeline_runner.run_pipeline (経路 A / B の共通の入口)
reservation = points.reserve("clip", "video", project_id=...)
context.watermark_required = reservation.watermark_required
context.watermark_applied = False
try:
    output_path = _run_timeline(context) または _run_legacy(context)
except Exception:
    points.cancel(reservation)
    raise
points.commit(reservation)
```

`PipelineContext` へ `watermark_required` / `watermark_applied` の 2 フィールドを足す。
既定はどちらも False とし、**ポイント機能を通らない呼び出し (CLI / テスト) では従来と完全に同じ挙動**になる。

#### 経路 C / D (アーカイブ)

`clip_writer.write_clips` の入口で予約し、`finish_clips` の戻り値が空でなければ commit する。
キャンセル (結果画面で取り消し) と例外は cancel とする。
**出力本数によらず予約は 1 本** (§2.5)。

#### 出力直前の保険

```python
# src/modules/output_writer.run
if watermark_overlay.is_required(context):
    intermediate = watermark_overlay.apply(context.current_video_path(), ...)
    context.set_current_video_path(intermediate)
    context.watermark_applied = True
final = finalize(context.current_video_path(), output_path)
```

アーカイブ側 (`finish_clips`) も、個別出力・結合出力のそれぞれについて同じ確認を行う。

### 5.5 Resolve 書き出しへの組み込み

`export_spec` が唯一の合流点である (§2.1)。

```python
# src/export/resolve_export.export_spec
reservation = points.reserve(job_type, "resolveProject", project_id=...)
if reservation.watermark_required:
    spec = watermark_overlay.add_resolve_clip(spec)   # 最上位トラックへ全長のクリップを足す
try:
    _atomic_write(dest_path, build_fcpxml(spec))
except Exception:
    points.cancel(reservation)
    raise
points.commit(reservation)
```

* `job_type` は入口で決まる (`export_clip_review` → `clip` / `export_archive_result` → `archive`)。
  `export_timeline` は Timeline の出自で決める (§9-3)。
* Resolve 上で watermark クリップを削除できてしまうが、**これは許容する** (resolve2 §3.0)。
* 残高不足でも**書き出しをブロックしない** (R4)。

### 5.6 UI

| 要件 | 配置 | 挙動 |
| --- | --- | --- |
| R11 残高インジケータ | メイン画面の右上 (`src/gui/main_window.py`) | `残り 125 pt / 次回リセット 10/11`。未ログインは「ログイン」ボタン。オフラインは最後に取得した値を薄字で表示 |
| R12 確認ダイアログ | 出力ボタン押下時 | `watermark_required` が True のときだけ出す。「続行」「キャンセル」。**予約の応答を見てから**出すため、予約は確認の前に行う |
| R13 アカウントタブ | 設定画面 (`settings_window.py` の 5 番目のタブ) | ログイン / ログアウト・残高・次回リセット・履歴 (`GET /api/points/transactions`)・サブスク導線 (Twitch のサブスクページを開く) |

残高の取得は UI スレッドを止めないよう `QThread` で行い、失敗しても画面は開いたままにする。

---

## 6. 実装手順

| Phase | 内容 | 完了条件 |
| --- | --- | --- |
| 1 | `points.py` (予約 / 確定 / 解放・サブスクキャッシュ・commit キュー) | 単体テストが通る。dev API に対して予約 → commit が通る |
| 2 | `watermark_overlay.py` (チェーン生成・単独パス) | 1920×1080 と 1080×1920 で同じ見え方になることを実出力で確認 |
| 3 | 経路 A / B への組み込み (`pipeline_runner` / `output_writer` / `PipelineContext`) | クリップ出力で watermark が入る。ポイントが 25 減る |
| 4 | 経路 C / D への組み込み (`clip_writer`) | アーカイブ出力で watermark が入る。ポイントが 100 減る (本数によらず 1 回) |
| 5 | `renderer._composite` への混ぜ込み (再エンコードの削減) | 出力結果が Phase 3/4 と同じで、工程が 1 つ減る |
| 6 | Resolve 書き出しへの組み込み | FCPXML に watermark クリップが入り、Resolve で読める |
| 7 | UI (残高インジケータ・確認ダイアログ・アカウントタブ) | 手動確認 |
| 8 | 設定・ドキュメント更新 | `setting.json` の新セクション、`docs/` の更新 |

Phase 3 / 4 を先に単独パスで通し、Phase 5 で最適化する。**動くものを先に作り、速さは後から足す。**

---

## 7. setting.json 定義

```jsonc
"watermark": {
  "scale_ratio": 0.18,      // キャンバス幅に対する透かしの幅の比率
  "opacity": 0.75,          // 0.0〜1.0
  "position": "bottom_right",  // bottom_right | bottom_left | top_right | top_left
  "margin_ratio": 0.02      // キャンバス幅に対する余白の比率
},
"points": {
  "subscription_cache_hours": 72,   // オフライン時にサブスク判定を信用する時間 (§3.3)
  "balance_refresh_sec": 300        // 残高インジケータの更新間隔
}
```

* **`watermark` セクションは設定画面に出さない** (R10)。`DEFAULT_SETTINGS` には持たせるが UI からは編集できない。
* `points` セクションも同様に UI へ出さない。

---

## 8. 互換性・非破壊の担保

1. **未ログイン時は完全に従来どおり。** ポイント API を呼ばず、watermark も入れず、
   確認ダイアログも出さない。既存ユーザーの出力結果は 1 バイトも変わらない。
2. **`PipelineContext` の新フィールドは既定 False。** CLI / テストからの呼び出しは影響を受けない。
3. **凍結配布での素材参照。** `src/watermark.png` が PyInstaller の `datas` に含まれるか実装時に確認する。
   含まれていなければ `main.spec` へ追加する (現状 `datas` は `comment_icon.png` 等を列挙している)。
4. **既存テスト 643 件を壊さない。** ポイント処理はすべて既定 False の分岐の内側に入るため、
   既存の出力テストは経路が変わらない。
5. **API 障害で出力が失敗しない。** `points.py` は例外を外へ出さない (§4-3)。

---

## 9. 確認事項 — すべて確定 (2026-09-15)

| # | 論点 | **決定** | 反映先 |
| --- | --- | --- | --- |
| 1 | オフライン時の扱い (plan.md 未決 #1) | **(a) watermark 付きで出力する。** サブスク判定のみキャッシュで救済し、ローカル台帳は持たない | §3.3 |
| 2 | watermark のデザイン (plan.md 未決 #4) | **決定済み (下記)** | §5.3 / §7 |
| 3 | `export_timeline` の `jobType` | **Timeline の出自を持ち回る。** 生成元 (クリップ / アーカイブ) を Timeline またはエクスポート文脈へ保持する | §5.5 |
| 4 | 課金の開始時期 | **未ログインユーザーも従来どおり使える。** 課金対象にするのはログインした利用者だけで、ログインを必須にはしない | §4-5 |
| 5 | 開発中の接続先 | **dev (`https://stretheusapi-dev.azurewebsites.net`) へ変更する。** prod へデプロイするときだけ prod を指定する | §7 / §8-6 |
| 6 | アーカイブ結合時の watermark | **個別クリップと結合後の両方に入れる** | §5.4 |

### #2 の決定 — 透かしの既定値

実出力のフレームで 6 候補を比較して決めた (2026-09-15)。

```jsonc
"watermark": {
  "scale_ratio": 0.25,          // 横 1920 → 幅 480px / 縦 1080 → 幅 270px
  "opacity": 0.75,
  "position": "bottom_right",
  "margin_ratio": 0.02
}
```

* **素材は現行の `src/watermark.png` のまま進める。** 淡い水色で明るい背景では沈むが、
  白フチ付きなどへの作り直しは後日必要になったら行う。
* この大きさでは**字幕と重なることがある** (横動画は画面下中央に字幕が出るため)。
  透かしは無償利用の印であり、見えることに意味があるため許容する。
  気になる場合は `position` を `top_right` へ変えるだけで干渉しなくなる。

### #3 の補足 — Timeline の出自

`export_timeline` は Timeline 編集画面から呼ばれるが、その Timeline は
クリップ経路 (`pipeline_runner._run_timeline`) とアーカイブ経路
(`archive_timeline.build_archive_timeline`) の両方から来る。
単価が 25pt と 100pt で変わるため、**生成した側が出自を記録する**。

* `Timeline` に `origin` (`"clip"` / `"archive"`) を持たせ、生成時に設定する。
* プロジェクト JSON へ保存し、再開時にも失われないようにする
  (未設定の古いプロジェクトは `"clip"` とみなす = 安い側に倒す)。

### #5 の補足 — prod デプロイ時の戻し忘れを防ぐ

`api.base_url` は開発中 dev を指す。**prod へデプロイする際に戻す必要がある**ため、
リリース手順 (`docs/HowToRelease.md`) へ確認項目として追記する (Phase 8)。
