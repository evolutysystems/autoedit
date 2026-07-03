# resolve8: インストーラー方式への移行（出力先指定・デスクトップショートカット）

`docs/request/request8.md` への要望設計書。

> ## 要望（原文）
> インストーラーを作成したい。
> - 現状はWebから直接zipファイルをダウンロードしている。
> - そうではなく、インストーラーをダウンロードして、インストーラーを経てシステムをダウンロードさせたい。
> - 以下はインストーラーでやりたいこと
>     ・編集した動画の出力先決定
>     ・デスクトップにショートカットを作成するかの可否
>
> **(追加)**
> Webページからダウンロードする際はGithub Releasesを使用する
> - リリース手順は "D:/develop/homepage/doc/error/resolve.md" を参照する

本書はレビュー用の設計書であり、**実装は本書の承認後に行う**（`docs/CLAUDE.md` の方針に準拠）。

---

## 1. 要件の整理

| # | 要件 | 解釈 / 補足 |
|---|---|---|
| R1 | 配布形態を「zip 直接DL」から**インストーラー方式**へ変更 | ユーザーは**インストーラー（小さい実行ファイル）**をダウンロードし、それを実行する。 |
| R2 | インストーラーが**システム本体をダウンロード**する | インストーラー実行中に、本体ペイロード（現行 `AutoEdit/` 一式）を Web から取得して展開・配置する（オンライン/ダウンローダ型インストーラー）。 |
| R3 | インストール時に**編集動画の出力先を決定**できる | インストーラーのカスタム画面でフォルダを選択し、`setting.json` の `general.output_directory` に反映する。 |
| R4 | **デスクトップショートカット作成の可否**を選べる | インストーラーのチェックボックスで選択。ON ならデスクトップに `AutoEdit.exe` へのショートカットを作成。 |
| R5（追加） | Web配布は **GitHub Releases** を使用 | 配布リポジトリ `evolutysystems/homepage`。homepage サイトの Dev ページの「ダウンロード」から、まず**インストーラー**を取得させる。リリース手順は `D:/develop/homepage/doc/error/resolve.md`（`gh release create/upload`）に準拠。 |

### 1.1 前提（現状の配布と GitHub Releases）

`docs/HowToRelease.md` の通り、現行は **PyInstaller(onedir, windowed) + FFmpeg 同梱**の `AutoEdit/` フォルダを **zip 化（`autoedit.zip`）**して配布し、「解凍 → `AutoEdit.exe` 実行」で起動する方式。

配布経路は別リポジトリ **`evolutysystems/homepage`**（Web サイト）で、`src/pages/dev/Dev.tsx` の「ダウンロード」ボタンが **GitHub Releases の直リンク URL** を開く（`D:/develop/homepage/doc/error/resolve.md`）。現状の URL 例：
`https://github.com/evolutysystems/homepage/releases/download/v1.0.0/autoedit.zip`

本要望は、この **「zip を直接DLして解凍」を「インストーラーをDLして実行」に置き換え**、出力先指定とショートカット作成を組み込む。

> **重要な制約（resolve.md §3 注意）**：**GitHub Releases の資産は1ファイル2GiB上限**。現行 `autoedit.zip` は約 **2.29GB**（CUDA 同梱なら約3.9GB）で**上限を超える**。この制約はインストーラー方式でも本体ペイロードに残るため、配布設計の中核論点になる（§4.2）。一方、**インストーラー本体（`AutoEditSetup.exe`）は数MB**で上限に余裕があり、Releases にそのまま置ける。

---

## 2. 現状調査（関連実装・配布物）

| 対象 | 内容（本要望に関係する箇所） |
|---|---|
| `docs/HowToRelease.md` | 現行の配布手順。onedir/windowed、FFmpeg 同梱、CUDA 同梱、配布レイアウト（§5.3）、SmartScreen/署名（§7）。**インストーラーが置き換える対象。** |
| `src/main_window.spec` | GUI 版ビルド設定（`dist/AutoEdit/` を生成）。**インストーラーが配布するペイロードの生成元（変更不要）。** |
| `src/settings/setting.json` | `general.output_directory`（現状 `D:/develop/autoedit/output`）を保持。**R3 でインストーラーが書き換える対象。** |
| `src/settings/settings_window.py` | `SETTINGS_DIR = dirname(abspath(__file__))` → `setting.json` を解決（L26-27）。`output_directory` の load/collect（L706/L804）。**凍結時の参照先＝`_internal/src/settings/setting.json`（HowToRelease §3.3）。R3 の書込先の特定に必要。** |
| 配布レイアウト（HowToRelease §5.3） | `AutoEdit.exe` / `_internal/`（または依存dll群）/ `settings/setting.json` / `ffmpeg/` / README / LICENSE。**インストーラーがこの構成を配置する。** |
| `D:/develop/homepage/doc/error/resolve.md` | 配布リポジトリ `evolutysystems/homepage`・`gh release create/upload` 手順・**Releases 2GiB 上限**・`Dev.tsx` のダウンロードボタン実装。**R5 のリリース手順の根拠。** |
| `D:/develop/homepage/src/pages/dev/Dev.tsx`（別リポジトリ） | `SYSTEMS[].downloadUrl` に Release URL を持ち、`handleDownload` がアンカー直リンクでDLさせる。**インストーラー配布に切替時、この URL を `AutoEditSetup.exe` に向ける（§4.3）。** |

**結論**：本体ビルド（`main_window.spec`）と本体コードは**変更不要**。インストーラーは「ペイロードのダウンロード → 配置 → `setting.json` のパッチ → ショートカット作成」を担う**新規の外付け工程**。配布面では (a) `AutoEditSetup.exe` を GitHub Releases に置き、(b) homepage の `Dev.tsx` のリンクをそれに向け、(c) 本体ペイロードの **2GiB 超え**を分割DL等で解決する（§4.2）。`setting.json` の凍結時の参照先（`_internal/src/settings/setting.json`）にも注意（§7）。

---

## 3. インストーラー技術の選定

`docs/CLAUDE.md`「不要なライブラリを追加しない」を尊重しつつ、**Python 実行時依存を増やさない**ものを選ぶ（インストーラーはビルド時ツールであり、配布物のランタイム依存には入らない）。

| 候補 | 長所 | 短所 | 評価 |
|---|---|---|---|
| **Inno Setup**（推奨） | 無料・実績豊富・日本語UI可・カスタムページ(Pascal Script)・**Inno Setup 6.1+ は `DownloadTemporaryFile` でダウンロード標準対応**（追加プラグイン不要）・ショートカット/レジストリ/アンインストーラ標準 | スクリプト(.iss)の学習コスト | **採用** |
| NSIS | 軽量・カスタマイズ自在 | スクリプトが低水準で記述量多い | 代替 |
| WiX(MSI) | 企業配布・GPO 向き | 学習コスト高・ダウンローダ型は手間 | 非採用（過剰） |
| PyInstaller onefile + 自作DL | 追加ツール不要 | §7 の `__file__` 問題・UI/アンインストールを自作になり工数大 | 非採用 |

> Inno Setup はビルド端末にのみ必要なツールで、配布物（ユーザー環境）には残らない。HowToRelease §7.3 でも「インストーラ（Inno Setup 等）を別途用意」が候補として既に挙がっており、方針整合。

---

## 4. 配布アーキテクチャ（GitHub Releases ＋ オンライン・ダウンローダ型）

要望「インストーラーをダウンロード→インストーラーを経てシステムをダウンロード」「Web配布は GitHub Releases」に従い、**配布チャネルを GitHub Releases（`evolutysystems/homepage`）に統一**したオンライン型を採用する。

```
[GitHub Releases: evolutysystems/homepage / tag v1.0.0]
  ├─ AutoEditSetup.exe              ← ユーザーが最初にDLする「小さいインストーラー」(数MB / 2GiB上限に余裕)
  ├─ AutoEdit-v1.0.0.zip.001        ← 本体ペイロードの分割 part1 (各 <2GiB ← 2GiB上限対策)
  ├─ AutoEdit-v1.0.0.zip.002        ← 同 part2 ...
  ├─ AutoEdit-v1.0.0.sha256         ← 結合後zipの改ざん検知ハッシュ
  └─ manifest.json (任意)           ← version / parts[] のURL・各SHA256・サイズ (将来の自動更新用)

[homepage サイト]
  src/pages/dev/Dev.tsx の Download ボタン
     → downloadUrl を AutoEditSetup.exe の Release URL に変更 (§4.3)

[ユーザー操作]
  1. homepage の Download ボタン → AutoEditSetup.exe をDL・実行
  2. インストーラーUI:
       - 言語/ようこそ
       - インストール先フォルダ選択 ({localappdata}\Programs\AutoEdit 既定推奨・§9-4)
       - 【カスタム】編集動画の出力先フォルダ選択 (R3)
       - 【カスタム】デスクトップにショートカットを作成する [✓] (R4)
       - 確認 → インストール
  3. インストーラー処理:
       a. payload の分割 part を順に DownloadTemporaryFile で取得 (R2/§4.2)
       b. part を結合し、SHA256 を検証
       c. インストール先へ展開・配置 (HowToRelease §5.3 のレイアウト)
       d. setting.json の output_directory をユーザー選択値にパッチ (R3)
       e. スタートメニュー(常時)・デスクトップ(任意)へショートカット作成 (R4)
       f. アンインストーラ登録
  4. 完了 → 「今すぐ起動」任意
```

### 4.2 本体ペイロードの 2GiB 超え対策（最重要）

resolve.md §3 の通り **GitHub Releases は1ファイル2GiB上限**。本体 zip（約2.29GB）はそのまま置けない。対策案：

| 案 | 内容 | 評価 |
|---|---|---|
| **B: CPU版で <2GiB（決定）** | **CUDA を不使用とし**（CUDA ランタイム約1.93GB を同梱しない）、CPU 実行版として配布。これにより payload を 2GiB 未満へ収め、**分割すら不要**にできる可能性が高い。 | **採用（決定）**。CUDA 削除は実装済み（§4.2.1）。最も単純で Releases 一本化に最適。 |
| A: 分割アップロード | `AutoEdit-v1.0.0.zip` を `.001/.002…`（各 <2GiB）へ分割。インストーラーが全 part を DL→**結合**→検証→展開。 | 予備案。CPU版でも 2GiB を超える場合に併用。 |
| C: 外部ストレージ | payload を S3/Cloudflare R2/Drive 等に置き URL からDL。 | 非採用（Releases 一本化が崩れる）。 |

> **決定（本タスクで実施）**：`docs/request/resolve8.md` に基づき **CUDA 不使用＝案B を採用**。CUDA ランタイム同梱（約1.93GB）を廃止し配布を軽量化する。**CPU版のサイズを実測**し、なお 2GiB を超える場合のみ案A（分割DL）を併用する。分割時の結合はインストーラー内（Pascal Script でバイナリ連結）で完結させ、ユーザーに手作業を求めない。

#### 4.2.1 CUDA 削除の実施内容（実装済み）

CUDA 不使用方針に伴い、以下を実装した（GPU=CUDA 由来の処理のみ除去。FFmpeg 等 CUDA 非依存の処理は不変）。

| ファイル | 変更内容 |
|---|---|
| `src/main_window.spec` | `_collect_cuda_binaries()`（`nvidia-*-cu12` の DLL を `_internal` へ同梱）を**削除**し `binaries=[]` に。約1.93GB の同梱を停止。 |
| `src/modules/subtitle_generator.py` | `_cuda_device_count()`・`_looks_like_cuda_error()` 等の **CUDA 検出/GPUフォールバックを削除**。`_resolve_device()` は常に `cpu` を返す（旧設定 `auto`/`cuda` は CPU へ正規化）。`float16` 指定は CPU 不可のため `int8` へ正規化。 |
| `src/settings/settings_window.py` | 実行デバイス選択肢を **CPU のみ**に、計算精度から GPU 専用 `float16` を除去。 |
| `src/settings/setting.json` | `whisper_device: "cpu"` / `whisper_compute_type: "int8"` へ更新。 |

> **音声認識は CPU 実行**となるため `large-v3` は処理が重い。必要に応じて軽量モデル（`medium`/`small`）への変更を README/設定で案内する（別途検討）。

### 4.3 homepage（`Dev.tsx`）側の変更

ダウンロードボタンの向き先を **zip から インストーラー**へ切り替える（別リポジトリ `evolutysystems/homepage` の改修）。

```ts
// SYSTEMS[].downloadUrl を zip → インストーラー exe に変更
downloadUrl:
  'https://github.com/evolutysystems/homepage/releases/download/v1.0.0/AutoEditSetup.exe',
```

`handleDownload` の実装（アンカー直リンク）はそのまま流用可。分割 part はユーザーに見せず、**ボタンはインストーラー1個だけを指す**（part の取得はインストーラーが担当）。

### 4.4 ペイロード生成・リリース手順（ビルド手順への追記）

現行手順（HowToRelease §5）で生成した `dist/AutoEdit/` を zip 化・分割し、`gh` で Releases へ上げる（resolve.md §2〜3 準拠）。

```powershell
# 1) 本体 zip 化
Compress-Archive -Path "src/dist/AutoEdit/*" -DestinationPath "$env:TEMP\AutoEdit-v1.0.0.zip"
(Get-FileHash "$env:TEMP\AutoEdit-v1.0.0.zip" -Algorithm SHA256).Hash | Out-File "$env:TEMP\AutoEdit-v1.0.0.sha256"

# 2) 2GiB 未満へ分割 (例: 1900MB 単位。実コマンドは確定時に決定・§9-2)
#    例) 7-Zip: 7z a -v1900m ... / または PowerShell でバイト分割

# 3) Releases へ: インストーラーと payload part・ハッシュを添付
gh release create v1.0.0 `
  "installer\Output\AutoEditSetup.exe" `
  "$env:TEMP\AutoEdit-v1.0.0.zip.001" "$env:TEMP\AutoEdit-v1.0.0.zip.002" `
  "$env:TEMP\AutoEdit-v1.0.0.sha256" `
  --repo evolutysystems/homepage --title "AutoEdit v1.0.0" --notes "インストーラー配布"
```

> オフライン型（payload をインストーラーに同梱）は、同梱した時点でインストーラー自身が2GiB超となり Releases に置けないため**不可**。よって本要望は**オンライン分割DL型が事実上必須**。

---

## 5. インストーラーのカスタムページ仕様

### 5.1 出力先フォルダ選択（R3）

```
┌─────────────────────────────────────────────┐
│ 編集した動画の出力先                            │
├─────────────────────────────────────────────┤
│ 自動編集した動画の保存先フォルダを選んでください。  │
│                                               │
│  [ C:\Users\<user>\Videos\AutoEdit      ] [参照]│
│                                               │
│  ※ 後からアプリの「設定」でも変更できます。       │
└─────────────────────────────────────────────┘
```

- 既定値：`{userdocs}\AutoEdit\output` 等の**書込可能なユーザー領域**を初期表示（`Program Files` 配下は権限の都合で避ける）。
- Inno Setup の `TInputDirWizardPage`（フォルダ選択ページ）を使用。
- 未存在フォルダは確定時に作成（`ForceDirectories`）。

### 5.2 デスクトップショートカットの可否（R4）

- Inno Setup 標準の **Tasks セクション**で実現（追加スクリプト不要）。
  ```ini
  [Tasks]
  Name: "desktopicon"; Description: "デスクトップにショートカットを作成する"; GroupDescription: "追加タスク:"
  ```
  ```ini
  [Icons]
  Name: "{group}\AutoEdit"; Filename: "{app}\AutoEdit.exe"; WorkingDir: "{app}"
  Name: "{autodesktop}\AutoEdit"; Filename: "{app}\AutoEdit.exe"; WorkingDir: "{app}"; Tasks: desktopicon
  ```
- `WorkingDir: "{app}"` を指定し、ログ等のカレント相対出力（HowToRelease §3.3）を**インストール先基準**に固定する（ショートカット起動時の作業フォルダ問題への対策）。

---

## 6. setting.json への出力先反映（R3）の方式

ユーザーが選んだ出力先を、配置後の `setting.json` の `general.output_directory` に書き込む。

### 6.1 書込先の特定（重要・要注意）

凍結（onedir）後、`settings_window.py` が参照する `setting.json` は **`{app}\_internal\src\settings\setting.json`**（HowToRelease §3.3 / §4 の `datas` 同梱先 `src/settings`）。インストーラーはこのファイルをパッチする。

### 6.2 パッチ方法（実装：output_directory の値のみ行置換）

JSON パーサをインストーラーに持ち込まず、**配置直前に文字列置換**する（堅牢・実装容易）。

- **採用（実装済み）**：`AutoEdit.iss` の `PatchOutputDir()` が `setting.json` を `LoadStringsFromFile` し、
  `"output_directory"` を含む行の**ダブルクォート間の値のみ**をユーザー選択パス（`\`→`/` 変換）へ置換して
  `SaveStringsToFile` する。**現在値がプレースホルダ `@@OUTPUT_DIR@@` でも実パスでも置換でき**、
  配布用テンプレートや `main_window.spec` の改変を不要にした（開発用 `setting.json` を変更しない＝
  `docs/CLAUDE.md` 遵守）。
- 失敗時（対象不在・書込不可）はログのみ出力しインストールは継続（既定値のまま・§10）。

> **整合性メモ**：`setting.json` を `_internal` 配下に置く現行方式は、HowToRelease §3.3 が「将来 `%APPDATA%\AutoEdit\` へ寄せる改修が望ましい」と注記している。**出力先をユーザー領域の設定ファイルへ寄せるか**は §9 で確認（寄せる場合、本体コード側の設定パス解決の改修が前提となり本要望の範囲を超える）。本書の既定は「現行の `_internal` 配下 `setting.json` をパッチ」。

---

## 7. 既知の制約・注意（HowToRelease からの引き継ぎ）

| 論点 | 内容 | インストーラーでの扱い |
|---|---|---|
| onedir 推奨 | onefile は `setting.json`/ログが一時展開先で消える（§3.3） | ペイロードは onedir 構成のまま配置（変更なし） |
| 設定の永続化 | `_internal` 配下 setting.json は `Program Files` だと書込権限の問題が出得る | インストール先を**ユーザー領域**にする／または設定を `%APPDATA%` へ寄せる改修（§9） |
| FFmpeg 同梱 | PATH 非依存のため同梱必須（§3.2） | payload zip に同梱済み前提（ビルド手順で配置） |
| CUDA 不使用（決定） | CUDA 同梱（約1.93GB）を**廃止**し CPU 実行へ統一（§4.2.1） | payload を大幅軽量化。音声認識は CPU 実行のため `large-v3` は重い点に留意（軽量モデル案内を検討） |
| SmartScreen/AV | 署名なし新規 exe は警告・誤検知（§7） | **インストーラー自身にもコード署名**を推奨（§8） |
| 設定画面「開始」ボタン | 凍結版で `src/main.py` 不在のため無効（§3.3 注記） | 既知事項。本要望では変更しない |

---

## 8. 信頼性・安全性（インストーラー特有）

- **コード署名**：`AutoEditSetup.exe` と本体 `AutoEdit.exe` を Authenticode 署名すると SmartScreen 警告を低減（HowToRelease §7.2）。インストーラーは新規 exe のため特に効果大。
- **ダウンロード検証**：分割 part を**結合した zip 全体**を SHA256 で検証してから展開（改ざん・破損検知）。可能なら part 個別の SHA256 も検証し、破損 part のみ再取得できるようにする。不一致時はインストール中止＋メッセージ。
- **HTTPS 配布**：payload URL は GitHub Releases（HTTPS）。
- **ネット不通時**：ダウンロード失敗を検知し、再試行/中止を選べるメッセージを表示（§10）。
- **UPX 無効**：本体は `upx=False` のまま（AV 誤検知回避・§4 既存方針）。

---

## 9. 不明点・確認事項（レビュー対象）

`docs/CLAUDE.md`「不明点は推測実装せず設計書へ記載」に従い明記する。

1. **配布サーバ/URL**：**GitHub Releases（`evolutysystems/homepage`）に確定**（R5）。インストーラー `AutoEditSetup.exe` と payload part・ハッシュを同一 Release タグ（例 `v1.0.0`）に添付する。タグ命名規則（バージョン）を確定する。
2. **2GiB 超え対策の確定**（§4.2・最重要）：分割DL（案A）/ CPU版で<2GiB に圧縮（案B）/ 外部ストレージ（案C）のいずれを採るか。本書は**案A（分割DL）を推奨**。採用時の**分割サイズと分割ツール**（7-Zip `-v` か独自バイナリ分割か）も確定する。
3. **GPU/CPU 版**：**CPU版に確定**（CUDA 不使用＝案B・§4.2.1 実装済み）。GPU版は配布しない。CPU版 payload のサイズ実測のうえ、2GiB 超なら案A（分割DL）併用を判断する。
4. **homepage 側改修の実施範囲**：`Dev.tsx` の `downloadUrl` をインストーラー exe に差し替える改修（§4.3）を本タスクで行うか、homepage 側で別途対応か（別リポジトリ `evolutysystems/homepage`）。
5. **インストール先の既定**：`{pf}\AutoEdit`（要管理者昇格・Program Files）か、`{localappdata}\Programs\AutoEdit`（昇格不要・ユーザー領域＝設定書込が楽）か。**後者を推奨**（§6/§7 の書込問題を回避）。
6. **出力先設定の保存場所**（§6.2 メモ）：現行 `_internal` 配下 `setting.json` をパッチで良いか、それとも `%APPDATA%\AutoEdit\setting.json` へ寄せる改修（本体コード側の設定パス解決変更）まで行うか。
7. **出力先の既定値**：初期表示するパス（例 `{userdocs}\AutoEdit\output`）。
8. **ショートカット名/アイコン**：表示名は「AutoEdit」で良いか。アイコン（`gui/app.ico`）は HowToRelease §4 時点で未配置。用意するか。
9. **多言語**：インストーラーUIは日本語のみで良いか（英語併記の要否）。
10. **自動更新**：将来の更新（manifest による版チェック）を今回スコープに含めるか（本書は**対象外**＝手動再インストール前提）。
11. **アンインストール時の出力動画**：ユーザーが指定した出力先フォルダ（生成済み動画）はアンインストールで**残す**想定（既定）。setting.json/ログの削除可否も確認。

---

## 10. エラー処理方針（インストーラー）

| 事象 | 方針 |
|---|---|
| 分割 part のダウンロード失敗（不通/タイムアウト） | 当該 part を再試行（数回）。最終的に失敗ならエラー表示し中止/再試行を選択可能に。中止時は取得済み一時ファイルを破棄。 |
| 結合後 SHA256 不一致（破損/改ざん） | インストール中止。「ファイルが破損しています。再実行してください」と表示。part 個別ハッシュがあれば破損 part のみ再取得。 |
| 展開先の容量不足/権限不足 | 事前に空き容量を確認（payload サイズ＋余裕）。権限不足は昇格要求 or ユーザー領域インストールへ誘導。 |
| setting.json パッチ失敗（対象不在/書込不可） | 警告ログ。デフォルト出力先のまま続行し、「アプリの設定で出力先を変更してください」と案内（インストール自体は継続）。 |
| ショートカット作成失敗 | 警告のみ。インストールは継続（スタートメニュー登録は維持）。 |

> インストーラー側のログは `{app}` または `%TEMP%` に出力（Inno Setup の `/LOG` 機能を活用）。本体ランタイムのログ方針（`utils/logger`）とは別系統。

---

## 11. 追加・変更ファイル

| 区分 | ファイル | 状態 | 変更概要 |
|---|---|---|---|
| 新規 | `installer/AutoEdit.iss` | **実装済み** | Inno Setup スクリプト。分割 part ダウンロード(`CreateDownloadPage`)・**結合**(`TFileStream`)・SHA256検証・展開(`tar.exe`/PowerShell)・出力先カスタムページ・ショートカット Tasks・setting.json パッチ・アンインストーラ。 |
| 新規 | `installer/README.md` | **実装済み** | インストーラーのビルド手順（ISCC コンパイル）＋ **GitHub Releases 公開手順**（zip 化・分割・`gh release create/upload`）・#define 設定・Dev.tsx 追従・検証チェックリスト。 |
| 変更 | `docs/HowToRelease.md` | **実装済み** | §7.3／§10 を「インストーラ＋GitHub Releases 配布」へ更新。詳細は `installer/README.md` を正典として参照。 |
| 不要 | `src/settings/setting.json`（配布用テンプレ） | **見送り** | §6.2 の値置換方式採用により**プレースホルダ化・別テンプレは不要**（開発用 `setting.json` は不変）。 |
| 追従（別リポジトリ・§9-4） | `evolutysystems/homepage` `src/pages/dev/Dev.tsx` | **未対応（別管轄）** | `SYSTEMS[].downloadUrl` を `AutoEditSetup.exe` の Release URL へ差し替え（手順は `installer/README.md §5`）。 |
| 任意 | `gui/app.ico` | **保留** | ショートカット/インストーラーのアイコン（§9-8）。用意でき次第 `.iss` の `SetupIconFile`/`[Icons]` に追加。 |

- **本体 Python コード（`main_window.py`/`settings_window.py` 等）は変更不要**（インストーラーは外付け工程）。
- 既存コードの**削除はしない**（`docs/CLAUDE.md`）。`evolutysystems/homepage` 側の `Dev.tsx` 差し替えは別リポジトリのため本リポジトリ外（`installer/README.md §5` に手順記載）。

### 11.1 実装時に確定した既定値（§9 の推奨を採用）

| 項目 | 確定値 | 出典 |
|---|---|---|
| インストール先 | `{localappdata}\Programs\AutoEdit`（`PrivilegesRequired=lowest`・昇格不要） | §9-5 |
| 出力先の既定 | `{userdocs}\AutoEdit\output` | §9-7 |
| setting.json 保存場所 | 現行 `_internal\src\settings\setting.json` をパッチ（`%APPDATA%` 化は別スコープ） | §9-6 |
| UI 言語 | 日本語（`compiler:Languages\Japanese.isl`） | §9-9 |
| ペイロード | 単一 zip 既定（`PayloadParts`）。2GiB 超のみ分割DL併用 | §4.2 |
| 整合性検証 | 結合後 zip の SHA-256（`PayloadSHA256` 設定時） | §8 |
| アンインストール | `{app}` 削除・出力先フォルダ（動画）は残置 | §9-11 |

---

## 12. 実装ステップ（承認後）

1. 配布方針の確定（§9-1〜6：Releaseタグ・**2GiB対策(分割/CPU版/外部)**・GPU/CPU・homepage改修範囲・インストール先・設定保存場所）。
2. `installer/AutoEdit.iss` 作成（**分割part DL→結合→SHA256検証**→展開→出力先ページ→ショートカット→パッチ→アンインストーラ）。
3. 配布用 `setting.json` のプレースホルダ化方式を確定（§6.2）。
4. payload の zip 化・分割・**`gh release` 公開手順**を `installer/README.md`・`docs/HowToRelease.md` に追記（§4.4）。
5. homepage `Dev.tsx` の `downloadUrl` をインストーラー exe へ差し替え（§4.3／別リポジトリ・§9-4 の合意後）。
6. クリーン PC で検証：homepageボタン→インストーラーDL→分割DL/結合→インストール→出力先反映→ショートカット起動→アプリ動作→アンインストール（HowToRelease §6 のチェックリストに準拠）。
7. （推奨）インストーラー・本体のコード署名（§8）。
