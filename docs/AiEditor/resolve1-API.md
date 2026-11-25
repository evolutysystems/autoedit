# AiEditor resolve1-API — T3: Stretheus API 側の AI 代行の設計

要望: `docs/AiEditor/request1.md` (2026-11-20) の回答 Q2 (**ポイント制で利用者の負担を減らす**)
前提: `docs/AiEditor/resolve1.md` §6 (クライアントが依存する契約) / §17.3 (ポイント) / §22.2 T3
　　　`StretheusAPI README.md` (ASP.NET Core 9 / EF Core 9 / PostgreSQL / Azure App Service)
　　　`StretheusAPI docs/request/resolve8.md` (sec2 / sec3 に対するサーバー側の点検 / S-5)
　　　`docs/security/sec2.md` `sec3.md` (クライアントへ鍵を置けない根拠)
対象リポジトリ: **`StretheusAPI`** (本書は autoedit 側に置く設計書。実装は別リポジトリ)
対象: `src/Stretheus.Api/Controllers/AiController.cs` (新規) /
　　　`src/Stretheus.Application/{Abstractions,Dtos,Options,Services}` /
　　　`src/Stretheus.Infrastructure/{Anthropic,Persistence,Telemetry}` /
　　　`tests/Stretheus.Application.Tests` / `tests/Stretheus.Api.IntegrationTests`
状態: **未実装 (設計のみ)**

---

## 0. 先に結論

**サーバーがやることは「鍵を預かって Claude を呼び、使った量を記録し、ポイントに換算する」の 3 つだけ。**
判断の中身 (プロンプト・スキーマ・候補の絞り込み) はすべてクライアントが持つため、
サーバーは**中身を解釈しない通し役**である。これが最小かつ安全な分担になる。

| # | 作るもの | 置き場 |
| --- | --- | --- |
| (1) | エンドポイント 4 本 (`complete` / `jobs/{id}` / `count` / `config`) | `Stretheus.Api/Controllers/AiController.cs` |
| (2) | `IAiService` (役割→モデルの解決・上限の強制・記録・換算) | `Application/Services/AiService.cs` |
| (3) | `IClaudeClient` (公式 C# SDK `Anthropic` のラッパ 1 枚) | `Infrastructure/Anthropic/ClaudeClient.cs` |
| (4) | テーブル 2 つ (`AiUsages` / `AiJobs`) + EF 移行 | `Infrastructure/Persistence` |
| (5) | `AiOptions` (モデル許可リスト・価格・上限・換算) | `Application/Options/AiOptions.cs` |

**先に決めておくべき 4 点** (いずれも既存実装の実測から出た制約。§2 で詳述):

| # | 制約 | 対処 |
| --- | --- | --- |
| **C1** | **Azure App Service のリクエストは 230 秒で切られる。** Opus を高 `effort` で呼ぶと超えうる | `complete` は**同期 (200) と非同期 (202 + ポーリング) の両方**を返す。**クライアント側に追補 1 点が必要** (§9) |
| **C2** | **ボディ上限が 8MB** (`Program.cs:31`。動画を送らない前提の値) | AI 経路だけ 24MB へ上げ、画像は枚数・長辺・合計バイトで検証する (Claude 側の上限 32MB より下に収める) |
| **C3** | 既存のレート制限は `ingest` = 120/分。AI には粗すぎる | 専用ポリシー `ai` を足し、**同時実行数**も絞る (1 利用者 2 本まで) |
| **C4** | **費用の蛇口がサーバー側に開く。** クライアントの `ai.cost.*` は改変できる | **日次・プロジェクト単位の上限をサーバーで強制する** (§6)。クライアントの申告は一切信用しない |

---

## 1. 既存資産の再利用 (新規に作らないもの)

| 必要なこと | 既存 | 使い方 |
| --- | --- | --- |
| 認証 (JWT / sub からユーザー確定) | `AddApiAuthentication` / `User.GetUserId()` | `[Authorize]` を付けるだけ。**本文でユーザーを受け取らない** (`PointsController` と同じ) |
| 秘密情報 | Azure Key Vault (`Program.cs:19-23`) | `Anthropic:ApiKey` を Key Vault のシークレット `Anthropic--ApiKey` から読む |
| 失敗の返し方 | `ExceptionHandlingMiddleware` + `ProblemResponseWriter` + `ErrorCodes` | 新しい `code` を 6 つ足すだけ (§5.5) |
| 構成の検証 | `services.AddOptionsSection<T>(…, validateOnStart: true)` | `AiOptions` も同じ作法。**ただし未設定でも起動できること** (Stripe / Google と同じ縮退) |
| 外部 HTTP クライアント | `AddHttpClient<ITwitchClient, TwitchClient>` / Stripe は `AddScoped` | **Anthropic SDK は自前で HttpClient を持つため Stripe 側の作法に合わせる** (§4.2) |
| ポイント台帳 | `PointWallet` / `PointTransaction` / `PointsOptions` | 換算の受け皿。**当面は台帳を動かさない** (§7) |
| 計測の送出 | `IPointTelemetry` の 2 実装 (App Insights / ログ) | 同じ形で `IAiTelemetry` を作る |
| レート制限 | `AddApiRateLimiting` (`ApiServiceCollectionExtensions.cs:70`) | ポリシー `ai` を追加 |
| テスト | `Application.Tests/Fakes` + `Api.IntegrationTests` | `IClaudeClient` の Fake を置く。**テストでネットワークへ出ない** |

> 本 API は「動画ファイルを受け取らない」という原則 (README) を守る。
> AI 経路で増えるのは**テキストと数枚の静止画**だけで、動画・音声は依然として受け取らない。

---

## 2. 制約と設計判断

### 2.1 C1 — 230 秒の壁 (最重要)

Azure App Service は応答のないリクエストを 230 秒で切る。一方 Claude は
役割によって所要が大きく違う。

| 役割 | 想定の所要 | 同期で足りるか |
| --- | --- | --- |
| `sceneEval` (Sonnet / medium / 8 件) | 十数秒〜1 分 | 足りる |
| `classify` (Haiku / low) | 数秒 | 足りる |
| `plan` (Opus / high / 大きい入力) | **数分になりうる** | **足りない** |
| `review` (役ごと / 画像つき) | 1〜数分 | 際どい |

判断:

* `complete` は **`AiOptions.SyncBudgetSeconds` (既定 100 秒) 以内に終われば 200 で結果を返す。**
  超えた時点で **202 + `jobId`** を返し、処理はバックグラウンドで続ける。
  クライアントは `GET /api/ai/jobs/{id}` を `pollAfterMs` 間隔で見に行く。
* サーバー → Claude は**ストリーミングで受ける** (`client.Messages.CreateStreaming`)。
  SDK の HTTP タイムアウトと 128K 出力の両方に対する既定の作法であり、
  「無反応で切られる」を避けられる。
* 進捗の中身 (途中のテキスト) はクライアントへ流さない。**完成した JSON だけを渡す。**
  途中の文字を見せても編集には使えず、契約 (§6.2 の `result`) が複雑になるだけである。

> **代案の検討**: SSE でクライアントへ流し続ければ 230 秒を回避できるが、
> クライアントの HTTP は既存 `ApiClient` (`urllib` の単発 POST) であり、
> SSE を読むコードを**クライアントへ新規に書く**ことになる。
> 202 + ポーリングなら既存の POST / GET だけで足りるため、こちらを採る。

### 2.2 C2 — ボディ上限とスキーマの大きさ

* `Program.cs:31` の `maxRequestBodyBytes = 8MB` は全体の既定。
  AI 経路だけ `[RequestSizeLimit(24 * 1024 * 1024)]` で広げる (全体は 8MB のまま)。
* 画像は要求の `images[]` として受け取り、次を**サーバーで**検証する
  (クライアント側の上限 `ai.vision.*` は改変できるため、信用しない)。

| 検査 | 既定 | 超えたとき |
| --- | --- | --- |
| 枚数 | `MaxImagesPerRequest` = 16 | 400 `ai_payload_too_large` |
| 1 枚のバイト数 | `MaxImageBytes` = 1.5MB | 400 |
| 合計バイト数 | `MaxTotalImageBytes` = 16MB | 400 |
| 形式 | `image/jpeg` / `image/png` のみ | 400 |
| `schema` の文字数 | `MaxSchemaChars` = 20000 | 400 |
| `system` + `payload` の文字数 | `MaxPromptChars` = 400000 | 400 |

### 2.3 C3 — レート制限と同時実行

```csharp
// ApiServiceCollectionExtensions.AddApiRateLimiting に追加
options.AddPolicy("ai", context =>
    CreateFixedWindowPartition(GetPartitionKey(context), permitLimit: 30));
```

* さらに `AiService` が**利用者ごとの同時実行**を `MaxConcurrentPerUser` (既定 2) に絞る。
  超過は 429 `ai_busy` (`Retry-After` 付き)。
  実装は `SemaphoreSlim` のユーザー別辞書で足りる (単一インスタンス前提)。
  スケールアウトする段では `AiJobs` の `Pending` 件数で数える (§4.3 の注記)。
* `jobs/{id}` のポーリングは `ingest` ポリシー (120/分) で足りる。

### 2.4 C4 — 費用の上限はサーバーが持つ

クライアントの `ai.cost.max_calls_per_project` などは**利用者が書き換えられる**。
サーバーは次を独立に強制する。

| 上限 | キー | 既定 | 超えたとき |
| --- | --- | --- | --- |
| 1 利用者の 1 日の入力トークン | `Limits.MaxInputTokensPerDay` | 3,000,000 | 429 `ai_quota_exceeded` |
| 1 利用者の 1 日の呼び出し回数 | `Limits.MaxCallsPerDay` | 300 | 429 |
| 1 プロジェクトの呼び出し回数 | `Limits.MaxCallsPerProject` | 40 | 429 |
| 1 回の出力トークン | `Limits.MaxOutputTokens` | 16,000 | クランプ (エラーにしない) |
| 1 利用者の 1 日の概算費用 | `Limits.MaxUsdPerDay` | 5.00 | 429 |

* 集計は `AiUsages` を日付で数える (別テーブルを作らない)。
* **匿名 (端末 ID) ユーザーに AI を許すか**は要確認 (§10 Q-A)。
  既定は `RequireLogin = true` とし、匿名は 400 `login_required` (既存コード) を返す。
  実費が発生する経路を、使い捨ての端末 ID で無限に叩けてはならない。

---

## 3. エンドポイント

| Method | Path | 認証 | 概要 |
| --- | --- | --- | --- |
| POST | `/api/ai/complete` | 必要 | 1 回の判断。200 (結果) または 202 (受理 + `jobId`) |
| GET | `/api/ai/jobs/{id}` | 必要 | 202 で受けた処理の現況・結果 |
| POST | `/api/ai/count` | 必要 | 送る前のトークン見積り (ポイントの事前表示) |
| GET | `/api/ai/config` | 必要 | 使える役割・モデル・上限・ポイント換算・課金モード |

### 3.1 `POST /api/ai/complete`

要求 (`AiCompleteRequest`):

```json
{
  "requestId": "6f1c…",
  "role": "sceneEval",
  "modelHint": "claude-sonnet-5-5",
  "effortHint": "medium",
  "projectId": "…",
  "system": "…",
  "payload": { "…": "任意の JSON (サーバーは中身を解釈しない)" },
  "schema": { "…": "期待する JSON Schema" },
  "images": [ { "mediaType": "image/jpeg", "data": "<base64>" } ],
  "cachePrefixId": "vod-1234567890-bundle-v1",
  "maxOutputTokens": 8192
}
```

| 項目 | 必須 | 扱い |
| --- | --- | --- |
| `requestId` | ○ | **冪等キー** (§3.2)。クライアントが生成する GUID 相当 |
| `role` | ○ | 許可リスト外は 400 `validation_failed` |
| `modelHint` / `effortHint` | — | **希望**。許可リストに無ければ役割の既定へ落とす (エラーにしない) |
| `projectId` | — | あれば記録に紐づける。**所有者を検証する** (resolve8 S-5 の指摘を新規実装で繰り返さない) |
| `system` / `payload` | ○ | サーバーは解釈せず、そのまま Claude へ渡す |
| `schema` | — | 構造化出力に使う。無ければ「JSON だけを返せ」の指示を添える |
| `images` | — | §2.2 の検証を通す |
| `cachePrefixId` | — | キャッシュを掛ける単位の目印。記録とキャッシュ TTL の判断に使う |
| `maxOutputTokens` | — | `Limits.MaxOutputTokens` でクランプ |

応答 200 (`AiCompleteResponse`) — **`resolve1.md` §6.2 の形をそのまま返す**:

```json
{
  "result": { "…": "Claude が返した JSON" },
  "model": "claude-sonnet-5-5",
  "usage": { "inputTokens": 41203, "outputTokens": 2187,
             "cacheCreationInputTokens": 0, "cacheReadInputTokens": 38950 },
  "points": { "charged": 0, "estimated": 3 },
  "stopReason": "endTurn",
  "durationMs": 18432
}
```

応答 202 (`AiJobAcceptedResponse`):

```json
{ "jobId": "…", "status": "running", "pollAfterMs": 3000, "expiresAt": "2026-11-20T12:10:00Z" }
```

### 3.2 冪等性 (`requestId`)

AI の 1 回は**実費**である。クライアントがタイムアウトで再送したときに
二重で払わせない仕組みが必須になる。既存の `clientJobId` (ポイント予約) と同じ考え方。

* `(UserId, RequestId)` に一意制約を張る (`AiJobs`)。
* 同じ `requestId` の再送:
  * 処理中 → **202 を返す** (同じ `jobId`)。
  * 完了済み → **200 で保存済みの結果を返す** (Claude を呼び直さない)。
  * 失敗済み → 同じエラーを返す。再試行したい場合はクライアントが新しい `requestId` を振る。
* 結果の保持は `Jobs.ResultTtlMinutes` (既定 30 分)。期限切れは 410 `ai_result_expired`。
  クライアントは新しい `requestId` で取り直す。

### 3.3 `GET /api/ai/jobs/{id}`

```json
{ "jobId": "…", "status": "succeeded",
  "result": { "…": "完了時のみ" },
  "model": "claude-opus-5-5",
  "usage": { "…": "完了時のみ" },
  "points": { "charged": 0, "estimated": 11 },
  "stopReason": "endTurn", "durationMs": 142118,
  "error": null, "pollAfterMs": 0 }
```

| `status` | 意味 | HTTP |
| --- | --- | --- |
| `running` | 処理中 | 200 (`pollAfterMs` あり) |
| `succeeded` | 完了 | 200 |
| `failed` | 失敗 (`error.code` / `error.message`) | 200 — **ジョブの取得自体は成功している** |
| `refused` | Claude が安全側で断った | 200 (`error.code = ai_refused`) |

* 他人のジョブは 404 `not_found` (存在を漏らさない)。
* ジョブ行は `Jobs.RetentionHours` (既定 24) で消す。**結果本文は `ResultTtlMinutes` で先に消す** (§8)。

### 3.4 `POST /api/ai/count`

要求は `complete` と同じ形 (`schema` / `images` も含む)。応答:

```json
{ "inputTokens": 41203, "estimatedOutputTokens": 2187,
  "estimatedPoints": 3, "estimatedUsd": 0.0271, "model": "claude-sonnet-5-5" }
```

* 入力は Claude の `CountTokens` で**正確に**数える。出力は `AiOptions` の
  `Roles[role].ExpectedOutputTokens` で見込む (見積りであることを `estimated` の名で表す)。
* **この呼び出しもレート制限の対象**にする (見積りだけを無限に叩かせない)。
  ただしポイントは消費しない。

### 3.5 `GET /api/ai/config`

クライアントが**価格とモデルをハードコードしないため**の口 (`resolve1.md` §6.3)。

```json
{
  "enabled": true,
  "chargeMode": "measure",
  "requireLogin": true,
  "roles": {
    "sceneEval": { "model": "claude-sonnet-5-5", "effort": "medium", "maxOutputTokens": 8192 },
    "sceneScan": { "model": "claude-sonnet-5-5", "effort": "medium", "maxOutputTokens": 16000 },
    "plan":      { "model": "claude-opus-5-5",   "effort": "high",   "maxOutputTokens": 16000 }
  },
  "allowedModels": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
  "limits": { "maxCallsPerProject": 40, "maxImagesPerRequest": 16, "syncBudgetSeconds": 100 },
  "pricing": { "usdPerPoint": 0.01, "minChargePoints": 1 }
}
```

* クライアントは起動時または AI 工程の開始時に 1 回読み、`ai.models.*` の希望と突き合わせる。
* `enabled = false` のとき、クライアントは AI 工程を**丸ごと飛ばして既存の採点結果で続行**する
  (`resolve1.md` §17 の縮退と同じ)。

---

## 4. 実装の設計

### 4.1 層と依存 (既存の向きを崩さない)

```
Api            AiController                    [Authorize] / RequestSizeLimit / RateLimiting("ai")
                   │
Application    IAiService → AiService          役割解決・検証・上限・換算・記録
                   │        IAiUsageRepository / IAiJobRepository / IAiTelemetry
                   ▼
               IClaudeClient (Abstractions/External)
                   │
Infrastructure ClaudeClient                    公式 SDK `Anthropic` を使う唯一の場所
               AiUsageRepository / AiJobRepository / {Logging,AppInsights}AiTelemetry
```

* **公式 SDK を触るのは `Infrastructure/Anthropic/ClaudeClient.cs` だけ。**
  Application 層は `IClaudeClient` しか知らないため、テストはネットワークへ出ない。
* `Api → Infrastructure → Application → Domain` の既存の向き (README) を守る。

### 4.2 `IClaudeClient` と SDK の呼び方

```bash
dotnet add package Anthropic   # 公式 C# SDK
```

```csharp
// Application/Abstractions/External/IClaudeClient.cs
public interface IClaudeClient
{
    Task<ClaudeCompletion> CompleteJsonAsync(ClaudeRequest request, CancellationToken cancellationToken);
    Task<long> CountInputTokensAsync(ClaudeRequest request, CancellationToken cancellationToken);
}
```

`ClaudeClient` の中身 (要点のみ。型名は公式 SDK の C# 名):

```csharp
using Anthropic;
using Anthropic.Models.Messages;

var parameters = new MessageCreateParams
{
    Model = resolved.Model,                       // 例: "claude-sonnet-5-5"
    MaxTokens = resolved.MaxOutputTokens,
    // 役割のプロンプト (固定) → クライアントの system、の順に積み、末尾へキャッシュを掛ける
    System = new List<TextBlockParam>
    {
        new() { Text = rolePrompt },
        new() { Text = request.System, CacheControl = new CacheControlEphemeral { Ttl = Ttl.Ttl1h } },
    },
    Thinking = new ThinkingConfigAdaptive(),      // 既定で on。budget は渡さない
    OutputConfig = new OutputConfig
    {
        Effort = resolved.Effort,                 // Effort.Low / Medium / High / Max
        Format = new JsonOutputFormat { Schema = request.Schema },   // 構造化出力
    },
    Messages = [new UserMessageParam { Content = contentBlocks }],    // payload (+ images)
};

await foreach (RawMessageStreamEvent e in client.Messages.CreateStreaming(parameters))
{
    // テキスト差分を積むだけ。クライアントへは流さない (§2.1)
}
```

**SDK を使うときの決まり** (現行モデルの仕様。間違えると 400 になる):

| 決まり | 理由 |
| --- | --- |
| `Thinking = new ThinkingConfigAdaptive()` (または省略) | `ThinkingConfigEnabled { BudgetTokens = N }` は現行モデルで 400 |
| 深さは `OutputConfig.Effort` のみ | Opus 5.5 の既定は `medium`、Sonnet 5.5 は `high`。**役割ごとに明示する** |
| `temperature` / `topP` / `topK` を**送らない** | 現行の API バージョンでは 400 |
| JSON は `OutputConfig.Format = new JsonOutputFormat { … }` | `tool_choice` の強制 (`any` / `tool`) は現行モデルで 400。ツールで JSON を取る旧作法は使えない |
| アシスタントの prefill を**使わない** | 現行モデルで 400 |
| `MaxTokens` を小さくしすぎない | 途中で切れると JSON が壊れ、丸ごと払い直しになる |
| ストリーミングで受ける | 長い出力・長い所要での HTTP タイムアウトを避ける (§2.1) |
| `response.StopReason == "refusal"` を**必ず見る** | HTTP 200 で返る。`StopDetails.Category` を記録し、再試行しない |
| 使った量は `response.Usage` の 4 項目 | `InputTokens` / `OutputTokens` / `CacheCreationInputTokens` / `CacheReadInputTokens` |

**DI の登録** (既存の作法に合わせる):

```csharp
// Infrastructure/DependencyInjection.cs
// Anthropic SDK は自前で HttpClient を持つため AddHttpClient では登録しない (Stripe と同じ扱い)。
services.AddSingleton<AnthropicClient>(sp => new AnthropicClient
{
    ApiKey = sp.GetRequiredService<IOptions<AiOptions>>().Value.ApiKey,
});
services.AddScoped<IClaudeClient, ClaudeClient>();
```

* `AiOptions` は **`validateOnStart: true` にしない。** 鍵が未設定でも API は起動し、
  `/api/ai/config` が `enabled = false` を返すだけにする (Stripe / Google と同じ縮退)。

### 4.3 プロンプトキャッシュの掛け方 (費用に最も効く)

* 役割プロンプト (サーバー側の固定文) → クライアントの `system` の順に積み、
  **`system` の最後のブロックに `CacheControlEphemeral { Ttl = Ttl.Ttl1h }`** を付ける。
* キャッシュは**前方一致**なので、サーバーが前置きへ時刻・乱数・ユーザー名などを混ぜてはならない。
  `payload` の JSON も**キーの順序を保って**そのまま通す (再直列化で並びを変えない)。
* 効いているかは `CacheReadInputTokens` で分かる。
  **`cachePrefixId` が同じ呼び出しで 2 回目以降も 0 のままなら前置きが揺れている。**
  この異常は警告ログ 1 行として残す (§8)。
* 役割ごとに前置きが違うため、キャッシュも役割ごとに別になる。
  **モデルを分けるとキャッシュも分かれる**ため、`sceneEval` の連続呼び出しは
  同じモデル・同じ `cachePrefixId` で来ることをクライアント側が守る (`resolve1.md` §6.4)。

### 4.4 役割とモデルの解決

```
modelHint があり、かつ AllowedModels に含まれる  → それを使う
それ以外                                        → Roles[role].Model
Roles に無い role                                → 400 validation_failed
effortHint が許可値でない                        → Roles[role].Effort
maxOutputTokens                                 → min(要求値, Roles[role].MaxOutputTokens, Limits.MaxOutputTokens)
```

* **解決結果を応答の `model` に必ず入れる。** クライアントはログへそれを残す
  (希望どおりに通ったかを後から確かめられる)。
* 価格表に無いモデルは `AllowedModels` に入れない (換算できない呼び出しを作らない)。

---

## 5. DTO / エラー

### 5.1 DTO の置き場

`Application/Dtos/Ai/` に `AiCompleteRequest` / `AiCompleteResponse` / `AiJobResponse` /
`AiCountRequest` / `AiCountResponse` / `AiConfigResponse` / `AiUsageDto` / `AiPointsDto`。
既存の `Dtos/Points` と同じ粒度にする。

### 5.2 `payload` / `schema` の型

* `JsonElement` (または `JsonDocument`) で受け、**デシリアライズしない**。
  中身の形はクライアントの資産であり、サーバーが知る必要がない。
  これにより `resolve1.md` のスキーマ変更がサーバーの改修を呼ばない。
* 列挙 (`role` / `effortHint` / `stopReason`) は既存の `JsonStringEnumConverter`
  (camelCase / `allowIntegerValues: false`) に合わせる。
  → 契約上の値は `sceneEval` / `plan` / `review` / `profile` / `classify`、
  `endTurn` / `maxTokens` / `refusal` となる (`resolve1.md` の `scene_eval` 等は
  camelCase へ読み替える。**クライアント側の 1 行の対応表で済ませる** / §9)。

### 5.3 `images` の受け取り

`[{ "mediaType": "image/jpeg", "data": "<base64>" }]`。
サーバーは §2.2 を検証したうえで `ContentBlockParam` の画像ブロックとして積む。
**画像は保存しない** (§8)。

### 5.4 プロジェクトの所有者検証

`projectId` が付いているときは `IProjectRepository` で**呼び出し元の所有物か**を確かめる。
他人のものなら 404 `not_found`。
`StretheusAPI docs/request/resolve8.md` S-5 (`PointService` が所有者を検証していない) と
同じ穴を新規実装で再現しない。

### 5.5 追加する `ErrorCodes`

| code | HTTP | 意味 | クライアントの振る舞い (`resolve1.md` §17) |
| --- | --- | --- | --- |
| `ai_unavailable` | 503 | 鍵未設定・機能停止中 | AI 工程を飛ばして既存採点で続行 |
| `ai_refused` | 200 (ジョブ) / 422 (同期) | Claude が安全側で断った | 再試行しない。縮退 |
| `ai_payload_too_large` | 400 | §2.2 の上限超過 | 画像・候補を減らして再構成 |
| `ai_model_not_allowed` | 400 | 許可外のモデルを明示指定 | 希望を外して再送 |
| `ai_quota_exceeded` | 429 | §2.4 の上限超過 | その日は AI を使わず続行 |
| `ai_busy` | 429 | 同時実行超過 / Claude のレート制限 | `Retry-After` 後に再送 |
| `ai_result_expired` | 410 | 結果の保持期限切れ | 新しい `requestId` で取り直す |
| `insufficient_points` | 402 | 課金モードで残高不足 | AI 工程のみ縮退 (既存 `points` の作法に合わせる) |

* `429` は既存の作法どおり `Retry-After` を付ける。
* Claude 側の 5xx / タイムアウトは `ai_busy` に寄せる (クライアントに再試行させる)。
  SDK の例外は `Anthropic.Exceptions` の型で分岐する
  (`AnthropicRateLimitException` / `Anthropic5xxException` / `AnthropicApiException`)。

---

## 6. 上限の強制 (C4 の実装)

`AiService` が Claude を呼ぶ**前**に、この順で確かめる。

```
1. AiOptions.Enabled / ApiKey あり           → 無ければ 503 ai_unavailable
2. RequireLogin と匿名判定                   → 匿名なら 400 login_required
3. requestId の冪等チェック                  → 既存なら 200/202 を返して終わり
4. 入力の検証 (§2.2)                         → 400
5. 同時実行 (MaxConcurrentPerUser)           → 429 ai_busy
6. 日次・プロジェクト上限 (AiUsages を数える) → 429 ai_quota_exceeded
7. 課金モードなら残高の確認                   → 402 insufficient_points
8. 役割とモデルの解決 (§4.4)
9. Claude を呼ぶ
10. usage の記録 → 換算 → (課金モードなら) 台帳へ
```

* 6 の集計は `AiUsages` の当日分の `SUM(InputTokens)` と `COUNT(*)`。
  インデックス `(UserId, CreatedAt)` を張る。
* **上限は「止める」側に倒す。** 払うのはサーバーであり、クライアントの善意に依存できない。

---

## 7. ポイントへの換算 (回答 Q2 / Q3)

### 7.1 2 つのモード

`AiOptions.ChargeMode`:

| 値 | 振る舞い | 位置づけ |
| --- | --- | --- |
| `measure` | `AiUsages` に実測と**概算ポイント**を記録するだけ。台帳 (`PointTransactions`) は動かさない。応答は `charged = 0` | **当面の既定** (`resolve1.md` 回答 Q3 でポイント制自体が保留) |
| `charge` | 上に加えて確定残高から引き、`PointTransaction` を 1 行積む | 単価が決まってから有効化 |

* **クライアントはどちらでも壊れない** (`resolve1.md` §17.3 の表)。
  この 2 モードがあることで、単価が決まる前に本番で実測できる。

### 7.2 換算式

```
usd = (input  - cacheRead - cacheCreation) / 1e6 * InputUsdPerMTok
    + cacheCreation / 1e6 * InputUsdPerMTok * CacheWriteMultiplier
    + cacheRead     / 1e6 * CacheReadUsdPerMTok
    + output        / 1e6 * OutputUsdPerMTok

points = max(MinChargePoints, ceil(usd / UsdPerPoint))
```

* 価格は**構成値**。既定は 2026-11-20 時点の第一者 API 価格 (1M トークンあたり)。

| モデル | 入力 | 出力 | キャッシュ読み |
| --- | --- | --- | --- |
| `claude-opus-5-5` | $4.00 | $20.00 | $0.20 |
| `claude-sonnet-5-5` | $2.00 | $10.00 | $0.10 |
| `claude-haiku-5-5` | $0.10 | $0.50 | (構成で与える) |

* `CacheWriteMultiplier` の既定は 1.25 (キャッシュ書き込みは入力の割増)。
* **価格改定はアプリ設定の変更だけで済む。** 配布物にもコードにも価格を焼かない。
* `UsdPerPoint` が単価の実体。既存の単価 (Clip 25pt / Archive 100pt / 付与 150pt) との
  釣り合いは `measure` で集めた実測から決める (`resolve1.md` §22.2 T1)。

### 7.3 `charge` にするときの追加

* `PointTransactionKind` に `AiUsage` を足す (enum は**文字列で保存**されており
  `HasMaxLength(16)` に収まるため、既存行の意味は変わらない)。
* `PointTransaction.ReservationId` は null、`JobType` も null のまま。
  AI は「出力 1 回の予約」とは別軸であり、**予約の仕組みを流用しない** (混ぜると履歴が読めなくなる)。
* 残高不足は 402 `insufficient_points` で**止める** (予約のように透かしで続行させない。
  AI は成果物ではなく実費の発生だからである)。
* 履歴 (`GET /api/points/transactions`) には AI の消費も出る。表示名はクライアント側で引く。

---

## 8. 保存するもの / 保存しないもの

### 8.1 `AiUsages` (追記専用。実測の台帳)

| 列 | 型 | 備考 |
| --- | --- | --- |
| `Id` | guid | |
| `UserId` | guid | インデックス `(UserId, CreatedAt)` |
| `ProjectId` | guid? | 所有者検証済み |
| `RequestId` | string(64) | 冪等キー。`(UserId, RequestId)` に一意 |
| `Role` | string(16) | `HasConversion<string>` |
| `Model` | string(64) | **解決後**の値 |
| `Effort` | string(8) | |
| `InputTokens` / `OutputTokens` | int | |
| `CacheCreationInputTokens` / `CacheReadInputTokens` | int | キャッシュが効いた証拠 |
| `ImageCount` | int | |
| `DurationMs` | long | |
| `StopReason` | string(16) | `refusal` を含む |
| `Success` | bool | |
| `EstimatedUsd` | decimal(10,6) | §7.2 |
| `EstimatedPoints` / `ChargedPoints` | int | `measure` では `Charged = 0` |
| `CachePrefixId` | string(64)? | キャッシュの効きを後から追える |
| `CreatedAt` | datetime (UTC) | |

### 8.2 `AiJobs` (非同期の受け皿)

| 列 | 備考 |
| --- | --- |
| `Id` / `UserId` / `RequestId` | `(UserId, RequestId)` に一意 |
| `Status` | `Pending` / `Running` / `Succeeded` / `Failed` / `Refused` (string 保存) |
| `ResultJson` | **結果 JSON のみ。`ResultTtlMinutes` を過ぎたら null にする** |
| `ErrorCode` / `ErrorMessage` | 失敗時 |
| `UsageId` | `AiUsages` への参照 |
| `CreatedAt` / `CompletedAt` / `ExpiresAt` | `RetentionHours` を過ぎた行は掃除 |

* 掃除は既存の「読み出しの時点で回収する」作法 (`PointService` の期限切れ回収) に合わせ、
  `complete` / `jobs` の呼び出し時についでに片付ける。常駐ジョブを増やさない。
* **スケールアウトすると、受けたインスタンスとポーリング先が別になりうる。**
  結果を DB (`AiJobs.ResultJson`) に置くのはこのため。
  インメモリに持たせてはならない (App Service のインスタンスは増減する)。

### 8.3 保存しないもの (重要)

| 保存しない | 理由 |
| --- | --- |
| `system` / `payload` の本文 | **配信の文字起こしとコメント本文**であり、他人の発言を含む。`resolve1.md` §5.5 の方針をサーバー側でも守る |
| `result` の本文 (期限後) | 編集の中身。クライアントが持っている |
| `images` | 配信映像のフレーム |
| `schema` | 必要ない |

* ログにも本文を出さない (§9 の `AiTelemetry` は数値と識別子だけ)。
* これは「動画ファイルを受け取らない」(README) の精神を、テキストと画像にも広げたものである。
  万一 DB が漏れたときに、**配信内容が出ていかない**設計にしておく。

---

## 9. 監視・ログ

* Serilog へ 1 行: `role` / 解決モデル / `effort` / 入出力トークン / キャッシュ読み /
  所要 ms / `stopReason` / 概算 USD / 概算ポイント / `requestId` / `cachePrefixId`。
  **本文・鍵は出さない。**
* `IAiTelemetry` を `IPointTelemetry` と同じ形 (App Insights 実装 + ログ実装) で作り、
  接続文字列が無い環境ではログへ退避する (`Infrastructure/DependencyInjection.cs` の分岐を踏襲)。
* 監視したい異常 (いずれも警告 1 行):

| 兆候 | 意味 |
| --- | --- |
| `CacheReadInputTokens = 0` が同じ `cachePrefixId` で続く | 前置きが揺れている = 費用が数倍になる |
| `StopReason = maxTokens` | 出力が切れた = JSON が壊れて払い直しになる |
| `refusal` の増加 | プロンプトの作りが安全側の判定に触れている |
| 1 利用者が上限へ張り付く | 想定外の使い方、または不正利用 |

---

## 10. クライアント側 (`autoedit`) に必要な追補 2 点

本書の設計で、`resolve1.md` §6.2 の契約に**2 点だけ**足りないものが出た。
いずれも 1 行の追加で、他の節には影響しない。

| # | 追補 | 理由 |
| --- | --- | --- |
| **A1** | 要求に **`requestId`** (冪等キー) を足す | タイムアウト再送で二重に実費を払わないため (§3.2) |
| **A2** | `complete` の応答が **202 + `jobId`** のこともあると認めて、`GET /api/ai/jobs/{id}` をポーリングする | Azure App Service の 230 秒制限 (§2.1)。`plan` は同期で収まらない |

* A2 の実装は `ai/remote.py` の中だけで閉じる。`AiProvider` の外から見れば
  「`complete_json` が返ってくるのが遅いだけ」であり、`scene_finder` / `planner` /
  `reviewer` は 1 行も変わらない。
* あわせて、列挙値は**サーバー側が camelCase** になる (既存 `JsonStringEnumConverter` の設定)。
  クライアントの `scene_eval` ↔ `sceneEval` の読み替えは `ai/remote.py` の対応表 1 か所で済ませる。
* `ai.endpoint` に `jobs` を足す (`"/api/ai/jobs"`)。

> **`resolve1.md` 側はまだ直していない。** 上記 A1 / A2 を反映してよいか指示を待つ
> (「他は問題ない」との回答のため、勝手に書き換えない)。

---

## 11. テスト

**Claude へは一切繋がない。** `IClaudeClient` の Fake を `Application.Tests/Fakes` に置く。

| 置き場 | ファイル | 検証 |
| --- | --- | --- |
| Application.Tests | `AiServiceTests.cs` | 役割とモデルの解決 (§4.4) / `maxOutputTokens` のクランプ / 許可外モデルで 400 |
| | `AiIdempotencyTests.cs` | 同じ `requestId` で Claude を 2 回呼ばないこと / 完了済みは保存結果を返すこと |
| | `AiLimitTests.cs` | 日次・プロジェクト上限で 429 / 同時実行で `ai_busy` |
| | `AiPricingTests.cs` | §7.2 の式 (キャッシュ読み・書き込みを含む) / `MinChargePoints` / 価格改定が構成で効くこと |
| | `AiChargeModeTests.cs` | `measure` で台帳が動かないこと / `charge` で 1 行積むこと / 残高不足で 402 |
| | `AiValidationTests.cs` | 画像の枚数・サイズ・形式 / `schema` と本文の文字数 |
| | `AiPrivacyTests.cs` | **`AiUsages` と `AiJobs` に本文・画像が入らないこと** / 期限後に `ResultJson` が消えること |
| Api.IntegrationTests | `AiEndpointsTests.cs` | 401 / 匿名で `login_required` / 他人の `projectId` で 404 / 他人の `jobId` で 404 / 202 → ポーリング → 200 |
| | `AiRateLimitingTests.cs` | ポリシー `ai` が効くこと (既存 `RateLimitingTests.cs` に倣う) |
| | `AiConfigTests.cs` | 鍵未設定で `enabled = false` を返し、**起動は成功する**こと |

既存テストが 1 件も壊れないことを合わせて確認する。

---

## 12. 実装順

1. `AiOptions` + `/api/ai/config` (鍵なしでも起動 → `enabled = false`) — **ここで縮退が先に固まる**
2. `AiUsages` / `AiJobs` のエンティティ・構成・EF 移行 (`AddAiUsage`)
3. `IClaudeClient` の Fake + `AiService` (解決・検証・上限・換算) + 単体テスト
4. `AiController` の `complete` (同期のみ) / `count` + 結合テスト
5. `ClaudeClient` (公式 SDK。ストリーミング・キャッシュ・構造化出力)
6. 非同期経路 (202 + `jobs/{id}`) と冪等性
7. レート制限ポリシー `ai` + 同時実行 + 日次上限
8. `IAiTelemetry` と Serilog の 1 行
9. `measure` で実測 → 単価の決定 → `charge` の実装 (`PointTransactionKind.AiUsage`)

> 4 番までで**クライアントは Fake 相手に通し試験ができる**。
> 5 番以降は費用が発生するため、1〜4 を先に固めてから鍵を入れる。

---

## 13. 要確認 (推測で実装しない)

| # | 確認したいこと | 本書の既定 |
| --- | --- | --- |
| **Q-A** | **匿名 (端末 ID) の利用者に AI を使わせるか** | 使わせない (`RequireLogin = true`)。実費が出る経路を使い捨ての ID で叩かせないため |
| **Q-B** | サブスクリプション加入者と無料利用者で上限・単価を分けるか | 分けない (まず全員同じ上限)。`ILicenseService` で分けられる作りにはしておく |
| **Q-C** | `UsdPerPoint` の初期値 | 0.01 (1pt = $0.01) を仮に置く。`measure` の実測で決める |
| **Q-D** | 日次上限の初期値 ($5 / 300 回 / 300 万トークン) | §2.4 の表。1 本の VOD の実測が出たら見直す |
| **Q-E** | 非同期の結果の保持 30 分 / ジョブ行 24 時間 | その値。長く持つほど本文が残る時間が延びるため短めにしている |
| **Q-F** | AI の費用を分析ログ (`/api/analytics/*`) にも出すか | 出さない (`AiUsages` が一次資料)。重複した集計を 2 か所に作らない |
| **Q-G** | `/api/ai/*` を開発環境 (dev) 限定で先に出すか | dev で `ChargeMode = measure` のまま運用し、実測を集めてから本番へ出す |

---

## 付録 A. `AiOptions` の全体像

```json
"Ai": {
  "Enabled": false,
  "ApiKey": "",                         // Key Vault: Anthropic--ApiKey / 構成では空のまま
  "RequireLogin": true,
  "ChargeMode": "measure",              // measure | charge
  "SyncBudgetSeconds": 100,
  "AllowedModels": [ "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5" ],
  "Roles": {
    "sceneEval": { "Model": "claude-sonnet-5-5", "Effort": "medium", "MaxOutputTokens": 8192,  "ExpectedOutputTokens": 2500 },
    "plan":      { "Model": "claude-opus-5-5",   "Effort": "high",   "MaxOutputTokens": 16000, "ExpectedOutputTokens": 4000 },
    "review":    { "Model": "claude-sonnet-5-5", "Effort": "medium", "MaxOutputTokens": 8192,  "ExpectedOutputTokens": 2000 },
    "profile":   { "Model": "claude-sonnet-5-5", "Effort": "medium", "MaxOutputTokens": 8192,  "ExpectedOutputTokens": 2000 },
    "classify":  { "Model": "claude-haiku-5-5",  "Effort": "low",    "MaxOutputTokens": 4096,  "ExpectedOutputTokens": 800 }
  },
  "Prices": {
    "claude-opus-5-5":   { "InputUsdPerMTok": 4.00, "OutputUsdPerMTok": 20.00, "CacheReadUsdPerMTok": 0.20, "CacheWriteMultiplier": 1.25 },
    "claude-sonnet-5-5": { "InputUsdPerMTok": 2.00, "OutputUsdPerMTok": 10.00, "CacheReadUsdPerMTok": 0.10, "CacheWriteMultiplier": 1.25 },
    "claude-haiku-5-5":  { "InputUsdPerMTok": 0.10, "OutputUsdPerMTok": 0.50,  "CacheReadUsdPerMTok": 0.01, "CacheWriteMultiplier": 1.25 }
  },
  "Pricing": { "UsdPerPoint": 0.01, "MinChargePoints": 1 },
  "Limits": {
    "MaxCallsPerDay": 300, "MaxInputTokensPerDay": 3000000, "MaxUsdPerDay": 5.00,
    "MaxCallsPerProject": 40, "MaxConcurrentPerUser": 2, "MaxOutputTokens": 16000,
    "MaxImagesPerRequest": 16, "MaxImageBytes": 1572864, "MaxTotalImageBytes": 16777216,
    "MaxSchemaChars": 20000, "MaxPromptChars": 400000
  },
  "Jobs": { "ResultTtlMinutes": 30, "RetentionHours": 24 }
}
```

* 既定は `Enabled = false`。**鍵と構成を入れるまで、API の既存の振る舞いは 1 つも変わらない。**
* `Prices` の値は 2026-11-20 時点のもの。改定はアプリ設定の変更だけで済む
  (`Points__Costs__Clip` と同じく `Ai__Prices__claude-opus-5-5__InputUsdPerMTok` で上書きできる)。

## 付録 B. 要望・クライアント設計との対応

| 出どころ | 本書 |
| --- | --- |
| `resolve1.md` §6.1 (鍵はサーバー / SDK はサーバー) | §0 / §4.2 |
| `resolve1.md` §6.2 (エンドポイントと本文) | §3 (+ 追補 2 点 / §10) |
| `resolve1.md` §6.3 (モデルの使い分け) | §4.4 / 付録 A `Roles` |
| `resolve1.md` §6.4 (プロンプトキャッシュ) | §4.3 / §9 の監視 |
| `resolve1.md` §5.5 (送るものと送らないもの) | §8.3 (サーバー側でも保存しない) |
| `resolve1.md` §17.3 (計測のみ / 課金する) | §7.1 の 2 モード |
| `resolve1.md` §22.2 T1 (実測で単価を決める) | §7.2 / §12 の 9 番 |
| 回答 Q2 (ポイントで負担) | §7 |
| 回答 Q3 (ポイント制自体は保留) | §7.1 の既定 `measure` |
| 回答 Q5 (映像を見せる) | §2.2 / §5.3 / §8.3 |
