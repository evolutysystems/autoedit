# AutoEdit インストーラー (ビルド & リリース手順)

`docs/request/resolve8.md` に基づくダウンローダ型インストーラー `AutoEditSetup.exe` のビルドと、
GitHub Releases への公開手順。

- 配布方式: **オンライン・ダウンローダ型**（小さいインストーラーをDL→実行中に本体をDL）
- 配布チャネル: **GitHub Releases**（`evolutysystems/homepage`）
- 本体: **CUDA 不使用の CPU 版**（`resolve8.md §4.2`。音声認識モデルは `large-v3` のまま）

インストーラーが行うこと:

1. GitHub Releases から本体ペイロード（zip / 必要時は分割zip）をダウンロード
2. 分割partを結合し、SHA-256 で破損検知
3. インストール先へ展開（PyInstaller onedir 構成をそのまま配置）
4. 「編集動画の出力先フォルダ」をユーザーに選ばせ `setting.json` に反映
5. 「デスクトップにショートカットを作成」の可否を選ばせる

---

## 0. 前提ツール

| ツール | 用途 | 備考 |
|---|---|---|
| **Inno Setup 6.1 以上** | `.iss` のコンパイル | `CreateDownloadPage` / `DownloadTemporaryFile` を使用するため 6.1+ 必須 |
| PyInstaller | 本体 `dist/AutoEdit/` の生成 | `docs/HowToRelease.md §5`（既存） |
| `gh` CLI | GitHub Releases へのアップロード | `D:/develop/homepage/doc/error/resolve.md` 準拠 |
| 7-Zip 等（任意） | payload が 2GiB 超のときの分割 | 単一 <2GiB なら不要 |

> Inno Setup / 7-Zip / gh はいずれも**ビルド端末のみ**で使用し、配布物（ユーザー環境）の
> ランタイム依存には含まれない（`docs/CLAUDE.md`「不要なライブラリを追加しない」と整合）。

---

## 1. 本体ペイロードのビルド & zip 化

`docs/HowToRelease.md §5` の手順で CPU 版本体を生成する（CUDA は同梱しない）。

```powershell
# リポジトリ直下・仮想環境を有効化済みとする
Set-Location src
pyinstaller main_window.spec --clean --noconfirm
# → src/dist/AutoEdit/ が生成される

# FFmpeg/ffprobe を同梱し setting.json のパスを合わせる (HowToRelease §5.2 / §3.2)
New-Item -ItemType Directory -Force "dist/AutoEdit/ffmpeg" | Out-Null
Copy-Item "<入手した>/ffmpeg.exe","<入手した>/ffprobe.exe" "dist/AutoEdit/ffmpeg/"
```

本体一式を zip 化し、ハッシュを取得する。

```powershell
# リポジトリ直下で実行
Compress-Archive -Path "src/dist/AutoEdit/*" -DestinationPath "$env:TEMP/AutoEdit-v1.0.0.zip"
$hash = (Get-FileHash "$env:TEMP/AutoEdit-v1.0.0.zip" -Algorithm SHA256).Hash
$hash    # ← この値を後述 #define PayloadSHA256 に設定する
```

### 1.1 2GiB を超える場合のみ：分割

GitHub Releases は **1ファイル 2GiB 上限**。CPU 版でも zip が 2GiB を超える場合のみ分割する。

```powershell
# 例) 7-Zip で 1900MB 単位に分割 (= AutoEdit-v1.0.0.zip.001, .002, ...)
& "C:\Program Files\7-Zip\7z.exe" a -tzip -v1900m "$env:TEMP/AutoEdit-v1.0.0.zip" ...
# ※ 上のハッシュは「結合後の単一zip」に対して取得すること。
#   分割は «単一zip を後から分割» する方式にして、結合後ハッシュと一致させる。
```

> 結合後のハッシュと一致させるため、**「単一 zip を作ってから分割」**するのが安全。
> インストーラーは全 part を取得後にバイナリ結合し、`PayloadSHA256` と照合する。

---

## 2. インストーラー設定（`AutoEdit.iss` の #define）

`installer/AutoEdit.iss` 冒頭の #define をリリース内容に合わせて更新する。

| #define | 設定値の例 | 説明 |
|---|---|---|
| `MyAppVersion` | `1.0.0` | バージョン。`ReleaseTag` は自動で `v1.0.0` になる |
| `PayloadParts` | 単一: `AutoEdit-v1.0.0.zip`<br>分割: `AutoEdit-v1.0.0.zip.001,AutoEdit-v1.0.0.zip.002` | ダウンロードする payload のファイル名（カンマ区切り） |
| `PayloadSHA256` | `1A2B...`（大文字16進） | 結合後 zip の SHA-256。空にすると検証スキップ（**本番は必ず設定**） |
| `RepoOwner` / `RepoName` | `evolutysystems` / `homepage` | Releases の配布元リポジトリ |

> URL は `https://github.com/{RepoOwner}/{RepoName}/releases/download/{ReleaseTag}/{part}` で組み立てられる。

---

## 3. インストーラーのコンパイル

```powershell
# ISCC のパス例 (環境に合わせて調整)
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "installer\AutoEdit.iss"
# → installer\Output\AutoEditSetup.exe が生成される
```

---

## 4. GitHub Releases への公開

インストーラーと payload（＋必要なら分割part）を**同一タグ**に添付する
（`D:/develop/homepage/doc/error/resolve.md §3` 準拠）。

```powershell
# 単一 payload の場合
gh release create v1.0.0 `
  "installer\Output\AutoEditSetup.exe" `
  "$env:TEMP\AutoEdit-v1.0.0.zip" `
  --repo evolutysystems/homepage `
  --title "AutoEdit v1.0.0" `
  --notes "インストーラー配布 (CPU版)"

# 分割 payload の場合は part を列挙して添付
gh release upload v1.0.0 `
  "$env:TEMP\AutoEdit-v1.0.0.zip.001" "$env:TEMP\AutoEdit-v1.0.0.zip.002" `
  --repo evolutysystems/homepage
```

> 既存 Release への差し替えは `gh release upload ... --clobber`。

---

## 5. homepage ダウンロードボタンの追従（別リポジトリ）

`evolutysystems/homepage` の `src/pages/dev/Dev.tsx` で、ダウンロード先を
**zip からインストーラー exe** へ変更する（`resolve8.md §4.3 / §9-4`）。

```ts
// SYSTEMS[].downloadUrl
downloadUrl:
  'https://github.com/evolutysystems/homepage/releases/download/v1.0.0/AutoEditSetup.exe',
```

> 本リポジトリ（autoedit）の管轄外のため、homepage 側で別途反映する。

---

## 6. 動作確認（クリーン PC 推奨）

`docs/HowToRelease.md §6` のチェックリストに加え、インストーラー固有項目を確認する。

- [ ] `AutoEditSetup.exe` 実行 → 言語/ようこそ → インストール先選択
- [ ] **出力先フォルダ選択ページ**が表示され、任意フォルダを指定できる
- [ ] **「デスクトップにショートカットを作成する」**チェックの ON/OFF が反映される
- [ ] ペイロードがダウンロードされ（分割時は結合され）、展開される
- [ ] インストール後、`{app}\_internal\src\settings\setting.json` の `output_directory` が
      指定フォルダ（`/` 区切り）に書き換わっている
- [ ] スタートメニュー／デスクトップのショートカットから `AutoEdit.exe` が起動する
- [ ] アプリで実行 → 指定した出力先に動画が出力される
- [ ] アンインストールで `{app}` が削除され、**出力先フォルダ（動画）は残る**
- [ ]（`PayloadSHA256` 設定時）破損ファイルで「ハッシュ不一致」になり中止される

---

## 7. 設計上の決定メモ（resolve8.md との対応）

| 項目 | 採用 | 根拠 |
|---|---|---|
| インストーラー技術 | Inno Setup 6.1+ | §3（追加ランタイム依存なし・DL標準対応） |
| 配布チャネル | GitHub Releases（evolutysystems/homepage） | R5/§4 |
| 2GiB 対策 | CPU版で <2GiB を基本、超過時のみ分割DL併用 | §4.2（案B、必要時A） |
| 展開方式 | `tar.exe`（標準同梱）優先、PowerShell `Expand-Archive` フォールバック | 追加依存なし |
| setting.json 反映 | `output_directory` の値のみ行置換（プレースホルダ非依存） | §6.2（堅牢・テンプレ不要） |
| インストール先既定 | `{localappdata}\Programs\AutoEdit`（昇格不要） | §9-5 |
| 出力先既定 | `{userdocs}\AutoEdit\output` | §9-7 |
| UI 言語 | 日本語 | §9-9 |
| アイコン | 未配置のため省略（用意でき次第 `SetupIconFile`/`[Icons]` に追加） | §9-8 |
| アンインストール | `{app}` を削除、出力先フォルダは残置 | §9-11 |
