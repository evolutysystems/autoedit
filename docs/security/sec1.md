# sec1 — リリースビルド成果物のセキュリティ点検（v1.4.6）

## 0. 本書の位置づけ

* 対象: **v1.4.6 のリリース成果物一式**
  * `installer/Output/StretheusSetup.exe`（ダウンローダ型インストーラー）
  * `Stretheus-v1.4.6.zip`（本体ペイロード / 554,507,937 bytes）
  * `src/dist/Stretheus/`（PyInstaller onedir 出力）
  * それらを生む `installer/AutoEdit.iss` / `src/main_window.spec` / 自動更新 `src/utils/updater.py`
* **本書は指摘と対策案のみ**。修正は本書のレビュー後に行う（`docs/claude.md`「実装前に設計を行うこと」）。
* 調査方法: **配布物と実コードの読み取り・実測**。
  配布 `setting.json` の値、`_internal/` の同梱パッケージ、`.iss` の `[Code]`、
  `updater.py` の通信・実行経路を実際に確認した。
* **未実施**（本書の限界。§7 に再掲）:
  * 動的解析・ペネトレーションテスト・マルウェアスキャン
  * 依存パッケージの脆弱性データベース照合（版数の一覧は §5 に出したが、
    **個別の CVE 断定はしていない**。照合は別途必要）
  * 実機・クリーン PC での挙動確認

---

## 1. 総評

**「配布物の中身」よりも「配布物をどう更新するか」の方に重い問題がある。**

インストーラー本体は SHA-256 検証・HTTPS・引数配列によるプロセス起動など、基本は押さえられている（§4）。
一方で、**アプリ内の自動更新だけが検証を一切持たない**（S1）。ここが最も優先度が高い。

次いで、**インストール先がユーザー書込可能**（S2）で、かつ
**`setting.json` に書かれた実行ファイルパスがそのまま外部プロセスとして起動される**（S3）。
この 2 つが噛み合うと「設定ファイルを書き換えるだけでアプリ起動時に任意コマンドが動く」形になる。

| # | 指摘 | 深刻度 | 主因 | 対策の重さ |
|---|---|---|---|---|
| **S1** | 自動更新が取得した exe を**無検証で実行**する | **高** | `updater.py` にハッシュ・署名検証が無い | 中（§2.1 の実装で完結） |
| **S2** | インストール先がユーザー書込可能／**実行ファイルが未署名** | **高** | `PrivilegesRequired=lowest` + 署名なし | 大（証明書の調達が要る） |
| **S3** | `setting.json` の値が**そのまま外部プロセスとして実行**される | 中 | `ffmpeg.executable` 等の解決が緩い | 小 |
| **S4** | Twitch アクセストークンを**平文 JSON**で保存 | 中 | `twitch_auth._write_token_file` | 小（DPAPI・標準ライブラリで可） |
| **S5** | OAuth トークンが**ブラウザ履歴に残る** | 中 | relay ページがフラグメント→クエリへ変換 | 小（JS 数行） |
| **S6** | 使っていない **torch(322MB)/transformers(39MB)** を同梱 | 中 | PyInstaller が推移的に収集 | 中（要動作確認） |
| **S7** | 配布 `setting.json` に**開発機の絶対パス**が残る | 低 | リリース前の値戻し漏れ | 小 |
| **S8** | ログ出力先が**カレント相対** | 低 | `logging.log_dir = "logs"` | 小 |
| **S9** | 起動時に GitHub へ**無同意で通信**する | 低 | `auto_update_check` 既定 true | 小 |
| **S10** | テーマの記号 PNG を**予測可能な %TEMP%** へ置く | 低 | v1.4.6 で追加した `_indicator_assets` | 小 |
| **S11** | Twitch **Client-ID** が配布物に含まれる | 情報 | 公開クライアントの設計上は正常 | 対応不要 |

---

## 2. 高

### S1. 自動更新が取得した実行ファイルを無検証で起動する 【高】

#### 該当

```python
# src/utils/updater.py:86-96
def download_installer(url, on_progress=None):
    dest = os.path.join(tempfile.gettempdir(), _INSTALLER_ASSET)   # 固定パス
    urllib.request.urlretrieve(url, dest, reporthook=on_progress)  # タイムアウト無し
    return dest


def launch_installer(installer_path):
    subprocess.Popen([installer_path], close_fds=True)             # 無検証で実行
```

呼び出し側（`main_window.py:1141-1153`）も、ダウンロード完了後そのまま起動する。

#### 何が問題か

インストーラー本体は payload を SHA-256 で検証している（`AutoEdit.iss` の `PrepareToInstall`）。
**その検証を行うはずの `StretheusSetup.exe` 自身が、何の検証もなく取得・実行される。**
検証の連鎖が根元で切れている。

具体的に 4 つの穴がある。

| # | 穴 | 詳細 |
|---|---|---|
| a | **完全性検証が無い** | ハッシュも Authenticode 署名も確認しない。取得した exe が正しいものかを判断する材料がゼロ |
| b | **保存先が固定・予測可能** | `%TEMP%\StretheusSetup.exe`。ダウンロード完了から `Popen` までの間に同一ユーザー権限のプロセスが差し替えられる（TOCTOU）。攻撃者は「更新ダイアログが出る瞬間」を待つ必要すらなく、ファイルを監視して置き換えればよい |
| c | **URL を検証しない** | `check_latest()` は GitHub API の `browser_download_url` をそのまま使う（`updater.py:53-65`）。スキームもホストも確認しないため、応答が意図しない値になった場合（アカウント侵害、社内 TLS 傍受プロキシ、将来の実装変更）に任意 URL——`http://` や `file://` すら——を取得して実行し得る |
| d | **タイムアウトが無い** | `check_latest` は 5 秒を設定しているが、`urlretrieve` には無い。応答を返さないサーバに当たると更新ダイアログのまま固まる |

> **深刻度の根拠**: b は「同一ユーザーで動く別プロセス」が前提のため単独では権限昇格にならない。
> しかし a と c は**遠隔の攻撃者**（配布元の侵害・経路上の攻撃）が直接効く。
> しかも結果が「任意の exe の実行」であり、被害の上限が最大である。

#### 対策

`updater.py` へ 4 点を入れる。**新規ライブラリは不要**（標準ライブラリのみ）。

```python
import hashlib
import shutil
import tempfile
import urllib.parse

# インストーラの取得を許可するホスト (GitHub Releases の実体と、そのリダイレクト先)
_ALLOWED_HOSTS = {"github.com", "objects.githubusercontent.com",
                  "release-assets.githubusercontent.com"}
_DOWNLOAD_TIMEOUT_SEC = 300


# 取得先 URL が想定どおりか確かめる (S1-c)。
# https 以外・想定外ホストは弾く。リダイレクト後の URL も同じ関数で確かめる。
def _is_allowed_url(url):
    parsed = urllib.parse.urlsplit(url or "")
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_HOSTS


# インストーラを取得し、期待ハッシュと一致したパスを返す (S1-a/b/d)。
# 置き場所は毎回作る専用の一時フォルダにし、固定パスの差し替えを避ける。
def download_installer(url, expected_sha256, on_progress=None):
    if not _is_allowed_url(url):
        raise ValueError(f"想定外の配布元 URL のため更新を中止します: {url}")
    work_dir = tempfile.mkdtemp(prefix="stretheus_update_")
    dest = os.path.join(work_dir, _INSTALLER_ASSET)
    digest = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "AutoEdit-Updater"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SEC) as res:
        if not _is_allowed_url(res.geturl()):     # リダイレクト先も確かめる
            raise ValueError("配布元 URL が想定外へリダイレクトされました。")
        total = int(res.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = res.read(1024 * 256)
                if not chunk:
                    break
                digest.update(chunk)
                f.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
    actual = digest.hexdigest().lower()
    if actual != (expected_sha256 or "").strip().lower():
        shutil.rmtree(work_dir, ignore_errors=True)
        raise ValueError("更新プログラムのハッシュが一致しません。更新を中止しました。")
    return dest
```

期待ハッシュの配り方は 2 案ある。

| 案 | 内容 | 判定 |
|---|---|---|
| **A（推奨）** | リリース本文（`gh release view --json body`）または専用アセット `StretheusSetup.exe.sha256` にハッシュを載せ、更新チェック時に取得して照合する | リリース手順に 1 行足すだけ。**署名が無くても改ざんを検知できる** |
| B | Authenticode 署名を検証する（`WinVerifyTrust` を ctypes で呼ぶ） | S2 のコード署名が前提。実施できるなら A と併用が最善 |
| C | 何もしない（現状） | 更新経路が丸ごと信頼の穴になる |

**A を先に入れ、S2 のコード署名が実現したら B を重ねる**のが現実的である。

> なお A のハッシュも GitHub 経由で取る以上、**配布元アカウントの侵害には無力**である。
> それを塞げるのは B（署名）だけなので、A は「経路上の改ざん・取り違えの検知」と位置づける。

---

### S2. インストール先がユーザー書込可能で、実行ファイルが未署名 【高】

#### 該当

```
; installer/AutoEdit.iss:51-54
DefaultDirName={localappdata}\Programs\{#MyAppName}
PrivilegesRequired=lowest
```

* インストール先: `%LOCALAPPDATA%\Programs\Stretheus`
* この配下に `Stretheus.exe` / `_internal\*.dll`（約 1.4GB）/ `_internal\src\settings\setting.json` が置かれる
* **exe・dll ともコード署名なし**（`docs/HowToRelease.md §7.2` は「推奨」のまま未実施）

#### 何が問題か

* インストール先は**同一ユーザーが自由に書き換えられる**。`Stretheus.exe` の差し替え、
  `_internal` への DLL 置き換え（PyInstaller は `_internal` から DLL を読む）が可能で、
  **署名が無いため差し替えられても気づけない**。
* デスクトップ／スタートメニューのショートカットは残るため、
  差し替えた exe が「正規のアプリ」として起動し続ける（**持続化**に使いやすい形）。
* 昇格が要らない代わりに「管理者が後でこのアプリを実行する」場面では、
  ユーザー書込可能なバイナリが管理者権限で動くことになる。
* SmartScreen 警告が毎回出るため、**利用者が「詳細情報 → 実行」を習慣化**してしまう。
  これは偽装版を掴まされる素地になる。

> ユーザー領域インストールは `resolve8.md §9-5` で「昇格不要」を狙って**意図的に選んだ**設計であり、
> 本書はその判断自体を覆すものではない。ただし**選択の代償**は上記のとおりで、
> 代償を埋める手段（署名）が未実施のまま残っている点を指摘する。

#### 対策

| 優先 | 対策 | 補足 |
|---|---|---|
| 1 | **Authenticode コード署名**（`Stretheus.exe` と `StretheusSetup.exe`） | SmartScreen 評価が改善し、S1-B の署名検証も可能になる。OV/EV 証明書の調達が要るため計画が要る |
| 2 | 「**管理者インストール**（`{autopf}\Stretheus`）」を選べるようにする | `PrivilegesRequired=lowest` を `dialog` にすると利用者が選べる。書込可能な場所に置きたくない利用者に選択肢を与える |
| 3 | 設定・トークン・ログを**インストール先から出す** | `%LOCALAPPDATA%\Stretheus\` へ寄せる（S4/S8 と同じ改修）。管理者インストールを選んだときに設定が書けなくなる問題も同時に解ける |
| 4 | README に**正規の入手元 URL と SHA-256** を明記する | 利用者が自力で真正性を確認できる導線 |

---

## 3. 中

### S3. `setting.json` の値がそのまま外部プロセスとして実行される 【中】

#### 該当

```python
# src/modules/ffmpeg_runner.py:44-60 (_resolve_exe)
    # 3) PATH 解決 (開発実行や PATH 導入環境)
    found = shutil.which(configured) or shutil.which(os.path.basename(configured))
    if found:
        return found
    # 4) 見つからず: 設定値をそのまま返す
    return configured
```

解決結果は `subprocess.run([exe, ...])` へ渡される（`ffmpeg_runner.py:112,172` ほか計 6 か所）。
同じ形が `archive/twitch_source.py:65-90` の `_resolve_twitchdl`（`archive.download.twitch_dl_path`）にもある。

#### 何が問題か

* `setting.json` の `ffmpeg.executable` / `ffprobe_executable` / `archive.download.twitch_dl_path` は
  **「実行される外部コマンドのパス」**である。値を書き換えれば、アプリの通常操作で任意の exe が起動する。
* `setting.json` は §S2 のとおり**ユーザー書込可能な場所**にある。
  つまり「ファイルを 1 行書き換える」だけで、次回のアプリ利用時にコードが実行される。
* 手順 4 は**存在確認をせずに設定値をそのまま返す**。Windows の `CreateProcess` は
  裸名を「実行ファイルのディレクトリ → **カレントディレクトリ** → System32 → PATH」の順で探すため、
  カレントに置かれた同名 exe を拾う余地がある。

> 現実の同梱構成では手順 2（同梱物）で必ず解決されるため、**既定状態でこの経路には入らない**。
> 問題は「設定を書き換えられたとき」「同梱が壊れたとき」に**素直に外部コマンドへ落ちる**設計にある。

#### 対策

```python
# 凍結配布では「同梱物」または「実在する絶対パス」だけを認める (sec1 S3)。
# PATH 探索・裸名フォールバックは、同梱を持たない開発実行のときだけ許す。
def _resolve_exe(configured):
    if os.path.isabs(configured):
        # 絶対パス指定でも実在と拡張子を確かめる (存在しない値を素通しさせない)
        if os.path.isfile(configured) and configured.lower().endswith(".exe"):
            return configured
        raise FFmpegError(f"指定された実行ファイルがありません: {configured}")
    for rel in _bundle_relpath_candidates(configured):
        for base in _bundle_base_dirs():
            candidate = os.path.normpath(os.path.join(base, rel))
            # 同梱ディレクトリの外へ出る値 (.. を含む等) を弾く
            if not _is_within(candidate, base):
                continue
            if os.path.isfile(candidate):
                return candidate
    if getattr(sys, "frozen", False):
        raise FFmpegError("同梱の FFmpeg が見つかりません。再インストールしてください。")
    found = shutil.which(configured)      # 開発実行のみ
    ...
```

併せて次を検討する。

* `setting.json` 読み込み時に、実行パス系のキーだけ**別扱いで検証**する
  （`settings_window._normalize_legacy_values` と同じ場所に検証を足す）。
* 設定ファイルを `%LOCALAPPDATA%\Stretheus\` へ移す（S2-3 と同じ改修）。
  書込可能性そのものは変わらないが、インストール先の実行ファイル群と設定を分離できる。

---

### S4. Twitch アクセストークンを平文 JSON で保存している 【中】

#### 該当

```python
# src/archive/twitch_auth.py:_write_token_file
with open(self._token_path, "w", encoding="utf-8") as f:
    json.dump(self._token, f)
```

保存先は `_internal/src/settings/archive_work/twitch_token.json`（`archive/config.py:72-76`）。

#### 何が問題か

* **暗号化されていない**ため、同一ユーザーで動く任意のプロセス（情報窃取型マルウェア含む）が
  ファイルを読むだけで Twitch のユーザーアクセストークンを取得できる。
* 保存先が**インストールディレクトリ配下**のため、バックアップやフォルダ共有に巻き込まれやすい。

> **緩和されている点**（正しく設計されている）:
> スコープが空 `[]`（`twitch_auth.py:_scopes` 既定）のため、
> 漏れても得られるのは「公開情報の参照」相当であり、配信の変更や DM 等はできない。
> トークンをログへ出していないことも確認済み（`access_token` を含むログ出力は 0 件）。

#### 対策

Windows の DPAPI を使えば、**標準ライブラリ（ctypes）だけ**でユーザー単位の暗号化ができる。

```python
# Twitch トークンを DPAPI (CryptProtectData) で暗号化して保存する (sec1 S4)。
# 鍵は Windows がユーザーアカウントに紐づけて管理するため、別ユーザー・別PCでは復号できない。
# 追加ライブラリは不要 (ctypes は標準)。失敗時は保存自体を諦める (平文で書かない)。
import ctypes
from ctypes import wintypes


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(raw: bytes) -> bytes:
    blob_in = _DATA_BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw),
                                               ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptProtectData に失敗しました")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
```

* 併せて、保存先を `%LOCALAPPDATA%\Stretheus\` 配下へ移す（S2-3）。
* 既存の平文トークンは、初回起動時に**読み込んで暗号化し直し、平文ファイルを削除**する移行を入れる。
* Windows 以外へ移植する予定があるなら、DPAPI が使えない環境では
  「保存しない（毎回ログイン）」へ落とすのが安全側である。

---

### S5. OAuth トークンがブラウザ履歴に残る 【中】

#### 該当

```python
# src/archive/twitch_auth.py:_RELAY_HTML
"var h = window.location.hash ? window.location.hash.substring(1) : '';"
"if (h) { window.location.replace('/capture?' + h); }"
```

インプリシットフローでトークンは URL **フラグメント**（`#access_token=...`）で返る。
フラグメントはサーバへ送られないため、この relay ページが**クエリ文字列へ付け替えて**サーバへ渡している。

#### 何が問題か

フラグメントは「サーバに送られない・履歴や中継機器に残りにくい」から選ばれている仕組みである。
それを**クエリ文字列に変換した時点で、その利点を捨てている**。

* `http://localhost:3737/capture?access_token=...` が**ブラウザの履歴エントリ**として残る
  （`location.replace` は履歴を増やさないが、現在エントリがこの URL に置き換わる）。
* リクエストラインに載るため、ブラウザ拡張機能や、ローカルを覗ける診断ツールから見える。
* サーバ側は `Cache-Control` を返しておらず、キャッシュ制御を指定していない。

> **緩和されている点**: ローカルサーバのアクセスログは `log_message` を潰して抑止済み
> （`twitch_auth.py`）。`state` による CSRF 照合もあり、他人のトークンを注入されることは防いでいる。

#### 対策

relay ページを **POST** に変える。変更は JS 数行とハンドラ 1 個で済む。

```python
# フラグメントを POST のボディで渡す。URL に載せないため履歴・リクエストラインに残らない。
_RELAY_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Stretheus</title></head>"
    "<body style='font-family:sans-serif;padding:2em'><p>ログイン処理中…</p>"
    "<script>"
    "var h = window.location.hash ? window.location.hash.substring(1) : '';"
    "if (h) {"
    "  fetch('/capture', {method:'POST', headers:{'Content-Type':"
    "    'application/x-www-form-urlencoded'}, body:h})"
    "   .then(function(){ history.replaceState(null,'','/done');"
    "                     document.body.innerHTML = '<p>完了しました。タブを閉じてください。</p>'; });"
    "} else { document.body.innerHTML = '<p>トークンが取得できませんでした。</p>'; }"
    "</script></body></html>"
)
```

`_TokenHandler` へ `do_POST` を足し、`_handle_capture` をボディのパース結果で呼ぶ。
併せて次も入れる。

* **`Host` ヘッダの検証**（`localhost:3737` / `127.0.0.1:3737` 以外は 400）。
  悪意あるサイトが DNS リバインディングでローカルサーバへ到達するのを防ぐ。
  現状も `state` 照合で乗っ取りは防げているが、多層で塞ぐ。
* レスポンスへ `Cache-Control: no-store` を付ける。

---

### S6. 使っていない重量依存を同梱している 【中】

#### 実測

| パッケージ | 同梱サイズ | 自前コードからの参照 |
|---|---|---|
| `torch` (2.11.0+cpu) | **322 MB** | **0 件** |
| `transformers` | **39 MB** | **0 件** |
| `torchaudio` (2.11.0) | 2 MB | **0 件** |

`grep -rn "import torch|from torch|import transformers|from transformers" src`（`src/dist` 除く）は **0 件**。
PyInstaller が推移的に収集したものである。

#### 何が問題か

* **攻撃面が増える**。`torch` は `torch.load` に代表される **pickle ベースの逆シリアライズ**を持ち、
  `transformers` は外部からモデル・設定を読む経路を多数含む。
  アプリが使っていなくても、配布物に存在すれば「攻撃者が使える部品」として数えられる。
* **配布物が肥大する**（本体 1.4GB のうち約 360MB がこの 3 つ）。
  ダウンロード時間・ディスク使用量・更新のたびの転送量に直接効く。
* **仕様書と実態が食い違っている**。`src/main_window.spec` のコメントは
  「CUDA/torch は従来どおり非同梱 (CPU-only 維持)」と書いているが、**実際には torch が入っている**。
  この乖離自体が、次の担当者の判断を誤らせるリスクになる。

#### 対策

```python
# src/main_window.spec
    excludes=[
        # 自前コードから参照が無く、faster-whisper (ctranslate2 バックエンド) も
        # 使わない重量依存を外す (sec1 S6)。攻撃面と配布サイズを同時に減らす。
        'torch', 'torchaudio', 'transformers',
    ],
```

**ただし外す前に確認が要る**。`faster-whisper` は tokenizer の一部経路で `transformers` を
参照することがあるため、除外後に**音声認識を実際に走らせて**確認する必要がある。
手順:

1. `excludes` を入れてビルド。
2. 実動画で「無音カット → 音声認識 → テロップ焼き込み」を通す。
3. 失敗したら `transformers` だけ戻し、`torch` / `torchaudio` の除外に留める
   （それでも 324MB 削減できる）。
4. 成功したら spec のコメントを**実態に合わせて書き直す**。

---

## 4. 低・情報

### S7. 配布 `setting.json` に開発機の絶対パスが残っている 【低】

配布された `_internal/src/settings/setting.json` の `general` セクション:

```json
"video_directory": "D:/develop/autoedit/input",
"opening_video":   "D:/StreamPipeline/dev/assets/op.mp4",
"ending_video":    "D:/StreamPipeline/dev/assets/ed.mp4",
"output_directory": "D:/develop/autoedit/output"
```

* インストーラーが書き換えるのは `output_directory` **だけ**（`PatchOutputDir`）。
  残り 3 つは**開発機のフォルダ構成をそのまま利用者へ配る**ことになる
  （別プロジェクト名 `StreamPipeline` まで露出している）。
* 情報漏えいとしては軽微だが、**機能面でも**新規インストール直後に存在しないパスを指す。

**対策**: リリース前に配布用の既定値へ戻す。v1.4.5 で `volume_analysis.last_cut_db` に対して
行った処置（開発機の実行値 → 既定値）と同じ扱いにする。手作業に頼らないよう、
`docs/HowToRelease.md §10` の手順へ検査を 1 つ足すのがよい。

```powershell
# リリース前チェック例: 配布 setting.json に開発機パスが残っていないか
Select-String -Path "src\dist\Stretheus\_internal\src\settings\setting.json" -Pattern "D:/develop|StreamPipeline"
```

### S8. ログ出力先がカレントディレクトリ相対 【低】

`logging.log_dir` の既定は `"logs"`（相対）。`utils/logger.py:34` が `os.makedirs(log_dir)` して書く。

* ショートカットは `WorkingDir={app}` を指定しているため**通常起動では問題ない**が、
  他の起動経路（コマンドライン、別のショートカット、関連付け起動）では任意の場所にログが作られる。
* ログには入出力ファイルの絶対パス・VOD ID などが残る。
  共有フォルダやクラウド同期フォルダがカレントだった場合、そこへ出る。

**対策**: 既定を `%LOCALAPPDATA%\Stretheus\logs` の絶対パスへ寄せる（S2-3 とまとめて実施）。

### S9. 起動時に無同意で GitHub へ通信する 【低】

`MainWindow.start_update_check` は `general.auto_update_check`（既定 `true`）で
起動のたびに `api.github.com` へアクセスする。IP アドレスと `User-Agent: AutoEdit-Updater` が送られる。

* **設定で無効化できる点は良い**。問題は「既定で有効かつ、初回に説明が無い」こと。

**対策**: 初回起動時に更新チェックの可否を尋ねるか、README とリリースノートへ明記する。

### S10. テーマの記号 PNG を予測可能な `%TEMP%` へ置く 【低 / v1.4.6 の新規コード】

```python
# src/gui/theme.py (v1.4.6 で追加)
directory = os.path.join(tempfile.gettempdir(), "stretheus_theme")
...
_QSS_ASSET_TEMPLATE = 'QCheckBox::indicator:checked { image: url("{asset.check}"); }'
```

* パスが決まっているため、同一ユーザーの他プロセスが PNG を差し替えられる。
* 影響は**表示のみ**（Qt が画像として読むだけで、コード実行にはならない）。
  チェックマークが別の絵に化ける程度で、深刻度は低い。
* `%TEMP%` は Windows ではユーザー単位（`C:\Users\<user>\AppData\Local\Temp`）で、
  既定 ACL は本人・SYSTEM・Administrators に限られる。他ユーザーからは書けない。

**対策（任意）**: 気にするなら `tempfile.mkdtemp()` でプロセス専用フォルダにし、終了時に消す。
ただし QSS はテーマ適用のたびに読み直されるため、寿命管理が要る。**現状維持でも許容範囲**と判断する。

### S11. Twitch Client-ID が配布物に含まれる 【情報・対応不要】

配布 `setting.json` に `archive.auth.client_id: "mcu1dwig8t6bmv87xp6s08y2s757r5"` がある。

* **これは秘密情報ではない**。OAuth の公開クライアントでは Client-ID は
  認可 URL に必ず現れるため、隠す前提の値ではない。
* `client_secret` は `""`（空）で正しい。インプリシットフローに secret は不要で、
  **同梱していないことが正しい設計**である。
* 残るリスクは「第三者が同じ Client-ID を使い、Twitch の同意画面に同じアプリ名を出せる」
  「API レート制限を共有する」程度。redirect_uri を Twitch 側で厳格登録している限り、
  トークンが第三者に渡ることはない。

**対策**: 不要。ただし**将来 `client_secret` が必要なフローへ変えるときは、
絶対に配布物へ含めない**こと（その時点で S11 は「情報」から「重大」へ変わる）。

---

## 5. 参考: 同梱している主要パッケージの版数

**本書では個別の脆弱性照合をしていない。** 別途 GitHub Advisory / NVD と突き合わせること。

| パッケージ | 版 | 備考 |
|---|---|---|
| PySide6 / Addons / Essentials | 6.11.0 | GUI |
| torch | 2.11.0+cpu | **未使用（S6）** |
| torchaudio | 2.11.0 | **未使用（S6）** |
| transformers | （dist-info なし・39MB 同梱） | **未使用（S6）** |
| tokenizers | 0.22.2 | faster-whisper 経由 |
| ctranslate2 | 4.7.1 | 音声認識バックエンド |
| faster-whisper | 1.2.1 | 音声認識 |
| av | 17.0.0 | Timeline のフレーム取得 |
| httpx / httpcore | 0.28.1 / 1.0.9 | twitch-dl |
| certifi | 2026.2.25 | HTTPS ルート証明書（同梱確認済み） |
| urllib3 / requests | 2.6.3 / 2.33.1 | 推移的依存 |
| m3u8 | 6.0.0 | twitch-dl |
| budoux | 0.8.4 | 文節改行 |
| twitch-dl | 3.3.1 | VOD 取得 |
| pillow | 12.3.0 | 推移的依存 |

**推奨**: `requirements.txt` へ版を固定し（`HowToRelease.md §2.2` は「推奨」のまま未整備）、
リリース前に `pip-audit` 相当の照合を手順へ組み込む。
版が固定されていないと「どの版で作った配布物か」を後から特定できず、
脆弱性が公表されたときに影響範囲を判断できない。

---

## 6. すでに守れている点（維持すべき設計）

指摘だけでなく、**現状の良い判断**も記録しておく。改修時に壊さないため。

| # | 内容 | 該当 |
|---|---|---|
| 1 | payload を SHA-256 で検証してから展開する | `AutoEdit.iss` `PrepareToInstall` |
| 2 | 外部プロセス起動が**すべて引数配列**で、`shell=True` が 1 件も無い | `subprocess.run` / `Popen` 全 15 か所を確認（`os.system` / `eval` / `exec` も 0 件） |
| 3 | 同梱 FFmpeg を **PATH より先に**解決する（PATH 乗っ取りを受けない） | `ffmpeg_runner._resolve_exe` の順序 |
| 4 | OAuth に `state` の CSRF 照合がある | `twitch_auth._handle_capture` |
| 5 | OAuth スコープが**空**（最小権限） | `twitch_auth._scopes` 既定 `[]` |
| 6 | `client_secret` を配布物に含めていない | 配布 `setting.json` は `""` |
| 7 | トークンをログへ出していない | `access_token` を含むログ出力 0 件 |
| 8 | ローカル OAuth サーバのアクセスログを抑止 | `_TokenHandler.log_message` |
| 9 | UPX 圧縮を使わない（AV 誤検知を避ける） | `main_window.spec` `upx=False` |
| 10 | 更新時にユーザーの `setting.json` を保持する | `AutoEdit.iss` の退避・復元 |
| 11 | アンインストールでトークン・設定ごと `{app}` を削除する | `[UninstallDelete]` |
| 12 | 通信が全て HTTPS で、`certifi` を同梱している | `updater` / `twitch_auth` / httpx |

---

## 7. 対応の順序（提案）

| 段 | 対象 | 内容 | 目安 |
|---|---|---|---|
| **1** | S1 | 更新の**ハッシュ検証**・専用一時フォルダ・URL 検証・タイムアウト。リリース手順へ `StretheusSetup.exe` のハッシュ公開を追加 | 小〜中 |
| **1** | S7 | 配布 `setting.json` の開発機パスを既定へ戻す。リリース前チェックを手順へ追加 | 小 |
| **2** | S3 | 実行ファイルパスの解決を「同梱 or 実在する絶対パス」に限定 | 小 |
| **2** | S5 | relay を POST 化。`Host` 検証と `no-store` を追加 | 小 |
| **2** | S4 | トークンを DPAPI で暗号化。既存平文からの移行を入れる | 小〜中 |
| **3** | S6 | `excludes` で torch / transformers を外し、**音声認識の実走行で確認** | 中 |
| **3** | S8 / S2-3 | 設定・トークン・ログを `%LOCALAPPDATA%\Stretheus\` へ寄せる | 中 |
| **4** | S2 | **コード署名**の導入（証明書調達を含む）。実現後に S1 へ署名検証を重ねる | 大 |
| **4** | S9 | 更新チェックの初回同意 or README 明記 | 小 |
| — | S10 / S11 | 現状維持（記録のみ） | — |

段 1 は**次のリリースまでに入れる**ことを推奨する。特に S1 は、
今回 v1.4.6 を公開したことで**既存利用者がこの経路を通って更新する**ため、
経路が検証を持たない状態が実際に使われることになる。

---

## 8. 本書の限界（再掲）

* **動的解析・侵入テスト・マルウェアスキャンは行っていない。** 配布物と実コードの静的な読み取りのみである。
* **依存パッケージの CVE 照合をしていない。** §5 は版数の一覧であり、脆弱性の有無を示すものではない。
* **クリーン PC での挙動確認をしていない**（`HowToRelease.md §6` のチェックリストは未実施）。
* 本書は**現在の配布物**を対象とする。`excludes` の変更・設定の移設などを行った場合は、
  その構成に対して改めて点検が要る。
* 各対策のコード片は**方針を示すためのもの**で、そのままの動作確認はしていない。
  実装時は本書をもとに設計・テストを行うこと。
