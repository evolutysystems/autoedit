# request_autoupdate — 自動アップデート機能（setting.json 既存値を保持したまま更新） 要望設計書

## 0. 本書の位置づけ
自動アップデート機能追加の要望に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要なライブラリを追加しない）に従い、本書レビュー後に実装します。

---

## 1. 要望
1. **自動アップデート機能を追加する**。GitHub Releases に新バージョンを公開したら、ユーザーのアプリが更新を検知して更新できるようにする。
2. **更新時に `setting.json` の既存の値を上書きしない**。バージョンが異なる更新を行う際、ユーザーが設定した既存値（出力先・色・閾値・改行設定等）を保持したまま更新する（新バージョンで追加された項目のみ補完する）。

---

## 2. 現状分析（実測）

### 2.1 自動更新は存在しない
- アプリ本体（`src/**/*.py`）に **バージョン確認・HTTP通信・GitHub 参照のコードは一切無い**（`requests`/`urllib`/`__version__`/update-check いずれも不在）。アプリは Releases を見に行かない。
- 配布は**ダウンローダ型インストーラ** `AutoEditSetup.exe`（Inno Setup / `installer/AutoEdit.iss`）。`MyAppVersion "1.0.1"` が**ハードコード**され、`v1.0.1` タグの `AutoEdit-v1.0.1.zip` を固定取得する（＝「最新」を探す動作ではない）。
- 配布元リポジトリは **public の `evolutysystems/autoedit`**（`.iss` L27-30。匿名DL可）。

**結論**：新バージョンを Releases に上げても既存インストールは自動更新されない。更新チェック主体をアプリへ新設する必要がある。

### 2.2 setting.json の経路と「更新で消える」問題
| 対象 | 現状 |
|---|---|
| `settings_window.py` L25-26 | `SETTINGS_FILE = <__file__ のディレクトリ>/setting.json`。インストール版では `{app}\_internal\src\settings\setting.json`。 |
| `load_settings()` L209-220 → `_merge_with_defaults()` L224-231 | 読込時に **DEFAULT_SETTINGS に data を上書きマージ**。＝**欠落キーは既定補完・既存値はユーザー値優先**（メモリ上）。ただし**ディスクへ書き戻さない**（保存操作時のみ永続化）。 |
| `save_settings()` L235-238 | 全設定を setting.json へ丸ごと保存。 |
| `AutoEdit.iss` `PrepareToInstall` L315-366 | 更新でも **`ExtractZip(payload, appDir)` で setting.json を既定値で丸ごと上書き** → その後 `PatchOutputDir` で出力先を(再表示した)選択ページ値に書換。**＝ユーザー設定が全消失し出力先もリセットされる**（要望2で解消すべき現状の欠陥）。 |

**結論**：更新時に既存 `setting.json` を保護する仕組みが無い。**「更新前の setting.json を保持し、新バージョンの新規キーのみ補完」**を、インストーラ（ファイル保護）＋アプリ（新規キー補完）の2層で実現する。

---

## 3. 設計方針
1. **更新チェック主体はアプリ**：起動時に GitHub Releases API で最新版を確認し、新版があればユーザーに更新を提案 → 同意時に新 `AutoEditSetup.exe` を取得・起動してアプリ終了（インストーラが本体を上書き更新）。
2. **通信は標準ライブラリ `urllib` のみ**（新規依存を増やさない）。失敗時は静かにスキップ（オフライン利用者に影響させない）。
3. **凍結ビルド時のみ有効**：`sys.frozen`（PyInstaller）でない開発実行では更新チェックをスキップ。
4. **setting.json 既存値の保護（要望2）**：
   - **インストーラ**：更新時は既存 `setting.json` を退避→展開後に**そのまま復元**（既存値を1バイトも上書きしない）。更新モードでは**出力先選択ページをスキップ**（既存 `output_directory` を維持）。
   - **アプリ**：起動時に `_merge_with_defaults` が**新バージョンで増えた新規キーのみ補完**。差分があれば一度だけ `save_settings()` で永続化。
5. **設定でON/OFF可能**：`general.auto_update_check`（既定 true）。ハードコード禁止に配慮。

---

## 4. バージョン管理

### 4.1 バージョン定数（新規 `src/version.py`）
```python
# アプリバージョン (installer/AutoEdit.iss の MyAppVersion と一致させる)
# リリース毎に .iss と本ファイルの両方を更新する。
__version__ = "1.0.1"
```
- `.iss` の `MyAppVersion` と**同期**（リリース手順に「両方更新」を明記）。
- 将来的に `.iss` 側から生成する自動同期も可能（§13）。

### 4.2 バージョン比較
`x.y.z` を整数タプル化して比較（`v` 接頭辞・前後空白を除去。数値化不能な部分は 0 扱い）。
```python
def _parse_version(text):
    text = (text or "").strip().lstrip("vV")
    parts = []
    for p in text.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)

def is_newer(remote, local):
    return _parse_version(remote) > _parse_version(local)
```

---

## 5. 更新チェック・ダウンロード・再実行（新規 `src/utils/updater.py`）

### 5.1 定数（`.iss` と一致）
```python
_REPO_OWNER = "evolutysystems"
_REPO_NAME = "autoedit"
_INSTALLER_ASSET = "AutoEditSetup.exe"
_API_LATEST = f"https://api.github.com/repos/{_REPO_OWNER}/{_REPO_NAME}/releases/latest"
_HTTP_TIMEOUT_SEC = 5
```

### 5.2 最新リリース取得
```python
# 最新リリースの (tag, インストーラDL URL) を返す。失敗時は None (呼び出し側でスキップ)。
def check_latest():
    import json, urllib.request
    req = urllib.request.Request(
        _API_LATEST,
        headers={"User-Agent": "AutoEdit-Updater", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as res:
            data = json.loads(res.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 (通信失敗時は更新をスキップ)
        _logger.info("更新チェックをスキップ (取得失敗): %s", e)
        return None
    tag = data.get("tag_name", "")
    # アセットから AutoEditSetup.exe を探す。無ければタグからURLを組み立てる。
    url = None
    for asset in data.get("assets", []):
        if asset.get("name") == _INSTALLER_ASSET:
            url = asset.get("browser_download_url")
            break
    if not url and tag:
        url = (f"https://github.com/{_REPO_OWNER}/{_REPO_NAME}"
               f"/releases/download/{tag}/{_INSTALLER_ASSET}")
    return {"tag": tag, "installer_url": url}
```

### 5.3 判定
```python
# 更新が必要なら latest 情報を返す。凍結ビルドでない/最新/失敗時は None。
def find_update(current_version):
    if not getattr(sys, "frozen", False):
        return None  # 開発(ソース)実行では更新しない
    latest = check_latest()
    if not latest or not latest.get("installer_url"):
        return None
    if is_newer(latest["tag"], current_version):
        return latest
    return None
```

### 5.4 ダウンロードと起動
```python
# インストーラを一時フォルダへDLし、そのパスを返す (進捗コールバック対応)。
def download_installer(url, on_progress=None):
    import tempfile, urllib.request
    dest = os.path.join(tempfile.gettempdir(), _INSTALLER_ASSET)
    # urlretrieve でDL (on_progress: (block, block_size, total))
    urllib.request.urlretrieve(url, dest,
                               reporthook=on_progress if on_progress else None)
    return dest

# インストーラを起動しアプリを終了する。インストーラが本体を上書き更新する。
def launch_installer_and_exit(installer_path):
    import subprocess
    # サイレント更新にはしない (出力先維持・確認のため通常UIで実行)。
    subprocess.Popen([installer_path], close_fds=True)
    # 呼び出し側 (GUI) が QApplication.quit() 等で終了する
```
> **注**：本体ファイルを上書きするため、アプリ実行中は一部ファイルがロックされ得る。インストーラは新プロセスで起動し、**アプリを終了してから**展開させる（起動直後に GUI 側で終了）。

---

## 6. GUI 統合（`src/gui/main_window.py`）

### 6.1 起動時チェック（非ブロッキング）
- メインウィンドウ表示後、`general.auto_update_check` が true のとき、**バックグラウンド `QThread`** で `find_update(__version__)` を実行（UIを固めない・失敗は無視）。
- 新版検知時のみメインスレッドへ通知し、確認ダイアログを表示：
  ```
  新しいバージョン v1.0.2 が公開されています。
  今すぐ更新しますか？（更新中はアプリが再起動されます）
             [今すぐ更新]   [後で]
  ```
- 「今すぐ更新」→ `download_installer`（進捗ダイアログ）→ `launch_installer_and_exit` → `QApplication.quit()`。
- 「後で」→ 何もしない（次回起動時に再チェック）。

### 6.2 手動チェック（任意）
- 設定画面またはメニューに「更新を確認」ボタンを設け、`find_update` を即時実行。最新なら「最新版です」表示。要望外だが利便性のため §13 に将来拡張として記載（本設計では起動時自動チェックを主とする）。

---

## 7. 設定（setting.json / DEFAULT_SETTINGS）

### 7.1 追加キー（`general` セクション）
```jsonc
"auto_update_check": true   // 新規: 起動時の自動更新チェック ON/OFF
```
- `DEFAULT_SETTINGS["general"]`（settings_window.py L94-101 付近）へ追加。
- 旧 setting.json に無くても `_merge_with_defaults` が自動補完（既存動作）。
- 設定画面 UI にチェックボックス「起動時に更新を確認する」を追加（`general` タブ）。UI 追加を避け setting.json 管理のみとする選択も可（§14）。

---

## 8. setting.json 既存値の保護（★要望2の核心）

**方針**：更新時に「更新前のユーザー setting.json を保持（既存値を上書きしない）」＋「新バージョンの新規キーのみ補完」を、**インストーラ（ファイル保護）とアプリ（新規キー補完）の2層**で担保する。

### 8.1 インストーラ側（`AutoEdit.iss` の `PrepareToInstall`）
更新（既存インストール）検知時に、既存 `setting.json` を**退避→復元**する。展開でユーザー設定が消えないようにする。

```pascal
{ 追加: 既存 setting.json のパスと退避先 }
settingsPath := appDir + '\_internal\src\settings\setting.json';
backupPath   := ExpandConstant('{tmp}\setting.user.json');
isUpdate     := FileExists(settingsPath);   { 展開前に判定 }

{ 1) 既存があれば退避 (更新時のみ) }
if isUpdate then
  FileCopy(settingsPath, backupPath, False);

{ 2) 既存の展開処理 (ExtractZip) … payload の既定 setting.json で上書きされる }
if not ExtractZip(mergedZip, appDir) then ...;

{ 3) 更新時は退避した既存 setting.json を「そのまま復元」= 既存値を1バイトも変えない }
if isUpdate then
begin
  FileCopy(backupPath, settingsPath, False);   { verbatim 復元 }
  { 更新モードでは出力先パッチをスキップ (既存 output_directory を維持) }
end
else
  { 新規インストール時のみ: 従来どおり出力先をパッチ }
  PatchOutputDir(settingsPath, OutputDirPage.Values[0]);
```

- **更新モードでは出力先選択ページを非表示**にする（`ShouldSkipPage` で `OutputDirPage` を `isUpdate` 時にスキップ）。既存の `output_directory`（＝ユーザー値）を維持する。
- 新規インストール時は現状どおり（出力先選択→`PatchOutputDir`）。
- `isUpdate` の判定は `settingsPath` の存在で行う（AppId 固定のため同一インストール先に上書きされる）。

### 8.2 アプリ側（新規キー補完・`settings_window.py`）
- 更新後の初回起動で、`_merge_with_defaults`（既存 L224-231）が**新バージョンで増えた新規キー**をメモリ上で補完する（既存値はユーザー値優先で不変）。
- **新規キーをディスクへも反映**するため、起動時に「マージ結果とディスクに差分がある場合のみ一度保存」する軽量正規化を追加する：
  ```python
  # 読込時、新規キー補完で内容が変化した場合のみ setting.json を更新する。
  # 既存値は _merge_with_defaults がユーザー値優先で保持するため上書きされない。
  def load_settings(persist_new_keys=True):
      if not os.path.exists(SETTINGS_FILE):
          return DEFAULT_SETTINGS.copy()
      try:
          with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
              content = f.read().strip()
          if not content:
              return DEFAULT_SETTINGS.copy()
          data = json.loads(content)
      except (json.JSONDecodeError, OSError):
          return DEFAULT_SETTINGS.copy()
      merged = _merge_with_defaults(data)
      # 新規キーが増えていれば (data とキー構成が異なれば) 一度だけ永続化
      if persist_new_keys and _has_new_keys(data, merged):
          try:
              save_settings(merged)
          except OSError:
              pass  # 保存失敗しても実行は継続 (メモリ上は補完済み)
      return merged
  ```
  - `_has_new_keys(data, merged)`：`merged` に `data` へ無いキーが1つでもあれば True（＝新規キー追加時のみ書き戻す）。**既存値は変更しないため上書きにはならない**。

### 8.3 保護の保証（要望2の充足）
| ケース | 挙動 |
|---|---|
| ユーザーが変更した既存キー（色・出力先・閾値等） | インストーラが**verbatim 復元** → **完全保持**（上書きされない）。 |
| 新バージョンで**追加**された新規キー | アプリ `_merge_with_defaults` が既定値で補完し、初回起動で永続化。 |
| 既定値が変わった既存キー（例 max_line_length 15→20） | ユーザー setting.json の値（15）を**保持**（要望「既存値は上書きしない」に合致）。新既定を強制しない。 |
| 新バージョンで削除/改名されたキー | 旧キーは残置（無害・未参照）＋新キーは補完。実害なし。 |

---

## 9. データフロー（更新の流れ）
```
アプリ起動 (frozen)
  └► general.auto_update_check=true なら QThread で find_update(__version__)
        └► GitHub Releases API (latest) 取得 → タグ比較
              ├─ 最新/失敗/開発実行 → 何もしない
              └─ 新版あり → 確認ダイアログ
                    └─「今すぐ更新」→ download_installer(AutoEditSetup.exe)
                          └► インストーラ起動 + アプリ終了
                                └► [インストーラ] 既存 setting.json を退避
                                      └► payload 展開 (既定 setting.json で上書き)
                                      └► 退避した既存 setting.json を verbatim 復元
                                      └► (更新時は出力先ページ・パッチをスキップ)
                                └► 新バージョンで再起動
                                      └► load_settings(): 新規キー補完 → 差分あれば保存
                                            (既存値は保持されたまま)
```

---

## 10. 後方互換・エラー処理方針
- **オフライン/API失敗/レート制限**：`check_latest` が例外を握り潰し `None` を返す → 更新スキップ、通常起動。ユーザー体験を阻害しない。
- **開発(ソース)実行**：`sys.frozen` が False → 更新チェックをスキップ（誤って自身を更新しない）。
- **`auto_update_check=false`**：チェック自体を行わない。
- **DL失敗/中断**：進捗ダイアログでエラー表示し、アプリは通常起動継続（更新は次回に持ち越し）。
- **旧 setting.json（新キー無し）**：`_merge_with_defaults` が `auto_update_check` 等を補完。エラーなし。
- **setting.json 保存失敗（読み取り専用等）**：メモリ上は補完済みのため実行継続（§8.2）。
- **インストーラでの退避失敗**：`FileCopy` 失敗時はログ出力し、最悪でも従来挙動（既定で上書き）に留める。要望上は退避成功を前提とするが、失敗しても更新自体は完了させ、設定画面で再設定可能とする。
- **GitHub API 認証**：public リポジトリのため匿名で取得可（未認証60req/h。起動時1回のため十分）。`User-Agent` ヘッダ必須（GitHub 要件）。

---

## 11. 変更ファイル一覧
| ファイル | 変更内容 | 節 |
|---|---|---|
| `src/version.py` | **新規**：`__version__`（`.iss` の MyAppVersion と同期）。 | §4 |
| `src/utils/updater.py` | **新規**：`check_latest`/`find_update`/`download_installer`/`launch_installer_and_exit`/`is_newer`/`_parse_version`（urllib のみ）。 | §5 |
| `src/gui/main_window.py` | 起動時に QThread で更新チェック、確認ダイアログ・進捗・再起動。`auto_update_check` 参照。 | §6 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["general"]` に `auto_update_check` 追加。`load_settings` に新規キー永続化（`_has_new_keys`）追加。設定画面に「起動時に更新を確認する」チェック追加。 | §7,8.2 |
| `src/settings/setting.json` | `general.auto_update_check: true` 追加。 | §7 |
| `installer/AutoEdit.iss` | 更新検知（既存 setting.json 存在）時に退避→verbatim 復元、出力先ページ/パッチをスキップ。 | §8.1 |
| `installer/README.md` / `docs/HowToRelease.md` | リリース手順に「`__version__` と `MyAppVersion` を両方更新」「更新時 setting.json 保持仕様」を追記。 | §4,13 |

---

## 12. テスト観点（レビュー後の検証項目）
1. 凍結ビルドで、Releases に新タグを用意 → 起動時に更新ダイアログが出る。「今すぐ更新」でインストーラDL→起動→アプリ終了→新版で再起動する。
2. オフライン/API 失敗時に更新チェックが静かにスキップされ、通常起動する（エラーやフリーズなし）。
3. 開発(ソース)実行では更新チェックが走らない。
4. `auto_update_check=false` でチェックが行われない。
5. **（要望2）更新前に色・出力先・閾値・改行設定等を変更 → 更新実行 → 更新後もそれらの既存値が完全に保持される**（インストーラの verbatim 復元）。
6. **（要望2）更新後、新バージョンで追加された新規キーが setting.json に補完されている**（既存値は不変）。
7. **（要望2）既定値が変わった既存キー（例 max_line_length）が、ユーザー値のまま保持される**（新既定に勝手に変わらない）。
8. 新規インストール（既存 setting.json 無し）では従来どおり出力先選択ページが出て `output_directory` が反映される。更新時はページがスキップされ既存出力先が維持される。
9. setting.json が読み取り専用でも更新後にクラッシュしない（メモリ補完で継続）。
10. `__version__` と `.iss` の `MyAppVersion` が一致している（リリース手順チェック）。

---

## 13. 将来拡張（要望外・任意）
- **手動「更新を確認」ボタン**（設定画面/メニュー）。
- **サイレント/バックグラウンド更新**（ダウンロードのみ先行、再起動時適用）。
- **`__version__` の自動同期**（`.iss` の `MyAppVersion` からビルド時生成）。
- **インストーラ署名/チェックサム検証**（DL した `AutoEditSetup.exe` の真正性確認。現状は HTTPS+GitHub 依存。payload 自体は `.iss` が SHA-256 検証済み）。
- **プレリリース/チャネル対応**（beta 版のオプトイン）。
- **差分更新**（現状はフルインストーラ再取得）。

---

## 14. 確定事項・未確定事項

### 14.1 確定事項（ユーザー確認済み 2026-07-07）
- **チェック契機 = 起動時に確認**：アプリ起動時に自動でチェックする（`general.auto_update_check` で ON/OFF）。§6.1 のとおり実装する。
- **設定画面に出す**：`general` タブに「起動時に更新を確認する」チェックボックスを追加する（§7.1）。setting.json 管理のみには**しない**。
- **更新の強制度 = 任意ダイアログ**：新版検知時は確認ダイアログで「今すぐ更新／後で」を選ばせる（§6.1）。必須更新（スキップ不可）にはしない。

### 14.2 未確定事項（要ユーザー確認）
- **バージョン定義の場所**：`src/version.py` に定数を置き `.iss` と手動同期する前提（将来 §13 で自動化可）。
- **インストーラ退避先**：本書は `{tmp}\setting.user.json`。より堅牢にするなら `{localappdata}\AutoEdit\` 等の永続領域も選択可。
