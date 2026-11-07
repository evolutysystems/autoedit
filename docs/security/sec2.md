# sec2 — リリースビルド成果物のリバースエンジニアリング点検（v1.8.2）

## 0. 本書の位置づけ

* 対象: **v1.8.2 のリリースビルド成果物**
  * `src/dist/Stretheus/`（2026-11-07 に `pyinstaller main_window.spec --clean --noconfirm` で生成した onedir 出力）
  * `Stretheus.exe` 66,907,990 bytes / 配布フォルダ合計 約 1.5 GB（`_internal/` が約 1.4 GB）
  * `licenses/` を `tools/collect_licenses.py` の手順どおり同梱した状態（76 件・不足 0）
  * それらを生む `src/main_window.spec` / `installer/AutoEdit.iss` / `src/utils/updater.py`
* **主題は「配布物を解析したときに、情報漏洩に繋がる情報が取り出せるか」**である。
  sec1（v1.4.6）が配布物の構成と設定値を読む調査だったのに対し、本書は
  **exe の内部アーカイブを実際に展開して中身を復元する**ところまで行った（§2）。
* **本書は指摘と対策案のみ**。修正は本書のレビュー後に行う（`docs/CLAUDE.md`「実装前に設計を行うこと」）。
* **未実施**（§8 に再掲）:
  * zip 化・Inno Setup によるインストーラ生成（`dist/` までを対象とした）
  * 動的解析・ペネトレーションテスト・マルウェアスキャン
  * 依存パッケージの CVE 照合
  * クリーン PC での挙動確認（`HowToRelease.md §6`）
  * **サーバー側（StretheusAPI）の認可の検証**。本書は「クライアントから何が見えるか」までで、
    見えたエンドポイントが実際に守られているかは別リポジトリ側の点検が要る

---

## 1. 総評

**配布物そのものから開発者の秘密が漏れる状態ではない。** sec1 で指摘した
開発機パスの混入（S7）は解消しており、秘密鍵・API シークレットの類も入っていない（§6）。

問題は 2 種類に分かれる。

1. **配布物を読むと、利用者の端末から何を盗めばよいかが分かった**。
   Twitch トークンが平文で置かれており（S4）、解析者は保存先と形式を知ったうえで
   それを直接持ち出せた。Stretheus の JWT は DPAPI で守られているのに、
   **同じ仕組みが同じコードベースにありながら Twitch 側だけ使っていなかった**。
   同日の是正で DPAPI 化し、既存の平文からの移行も入れた。
2. **配布物の差し替えを止める手立てが乏しい**（S1 / S2）。
   漏洩そのものではないが、**漏洩を仕込む側の経路**として本書で最も重い。
   同日の是正で自動更新の検証を入れた（S1）ため、取得経路の改ざんは検知できるようになった。
   残るのは**インストール済みのファイルを後から差し替える**経路で、
   これは**コード署名でしか塞げない**（S2。証明書の調達が要る）。

> **初版（2026-11-07 午前）の訂正。** 本書は当初、配布 `setting.json` の接続先が
> dev を指していることを「リリース前の戻し忘れ」として**最上位の指摘（N1・高）**に挙げていた。
> これは誤りで、`HowToRelease.md §6` と `StretheusPlan/plan.md §7.8.4` のとおり
> **prod が未デプロイのため dev が実運用を担うという意図的な運用**である。
> 同日の是正で接続先の決まり方だけを直し、N1 は深刻度を下げて §4 に訂正の経緯を残した。

| # | 指摘 | 深刻度 | 初出 | 現況 |
|---|---|---|---|---|
| **N1** | 接続先が **3 か所**で決まり、そのときの値が配布物へ焼き付く | 中 | 本書 | **解消**（2026-11-07） |
| **S1** | 自動更新が取得した exe を**無検証で実行**する | **高** | sec1 | **解消**（2026-11-07） |
| **S2** | インストール先がユーザー書込可能／**実行ファイルが未署名** | **高** | sec1 | **一部**（署名は証明書待ち） |
| **S4** | Twitch アクセストークンを**平文 JSON** で保存 | 中 | sec1 | **解消**（2026-11-07） |
| **S5** | OAuth トークンが**ブラウザ履歴に残る** | 中 | sec1 | **未対応** |
| **S3** | `setting.json` の値が**そのまま外部プロセスとして実行**される | 中 | sec1 | **未対応** |
| **N2** | `points.json` の改ざんで**透かしを回避**できる | 中 | 本書 | **解消**（2026-11-07） |
| **S6** | 使っていない **torch(322MB)/transformers(39MB)** を同梱 | 中 | sec1 | **解消**（2026-11-07） |
| **N3** | 端末 ID の導出が決定論的で、**ソルトが配布物から読める** | 低 | 本書 | 未対応 |
| **N4** | 内部 API のエンドポイント一覧が配布物から**完全に読める** | 情報 | 本書 | 設計上は正常 |
| **S7** | 配布 `setting.json` に開発機の絶対パスが残る | — | sec1 | **解消**（§6） |

---

## 2. 解析手法と、実際に取り出せたもの

本章は「攻撃者と同じ手順を踏むと何が手に入るか」の記録である。
特別な道具は要らず、**Python 標準ライブラリだけで完結した**。

### 2.1 exe から内部アーカイブを取り出す

PyInstaller の onedir 出力でも、Python のモジュール群は `Stretheus.exe` 末尾の
CArchive（PKG）に収められている。末尾から `MEI\014\013\012\013\016` を探し、
続く 24 バイトのクッキーと TOC を読めば収録物が列挙できる。

```
pkglen=66,574,166  toc_off=66,573,438  toc_len=640  pyver=312  pylib=python312.dll
entries: 14
  struct / pyimod01_archive / pyimod02_importers / pyimod03_ctypes / pyimod04_pywin32
  pyiboot01_bootstrap / pyi_rth_inspect / pyi_rth_pkgutil / pyi_rth_multiprocessing
  pyi_rth_pyside6 / pyi_rth_cryptography_openssl
  main_window              type=s  usize=82,734
  PYZ.pyz                  type=z  usize=66,517,189
```

### 2.2 PYZ からモジュールを復元する

`PYZ.pyz` は **暗号化されていない**（PyInstaller 6 系は暗号化オプション自体を廃止している）。
先頭 12 バイトから TOC 位置を読み、各エントリを `zlib.decompress` → `marshal.loads` すれば
code object がそのまま得られる。

* **総モジュール数 8,259 / うち自前モジュール 136**（`src.gui.*` `src.services.*` `src.archive.*`
  `src.timeline.*` `src.blur.*` `src.export.*` などが名前ごと一覧できる）
* code object からは **関数名・引数名・分岐構造・全ての文字列定数**が取れる。
  Python 3.12 のバイトコードを元のソースへ戻す逆コンパイラは成熟していないが、
  `dis` で読めば処理の流れは十分に追える
* 日本語のログ文言・エラーメッセージもそのまま読めるため、**内部の設計意図まで分かる**

実例（`src.services.device_id` を復元したときに得られた文字列定数）:

```
stretheus-device-v1
SOFTWARE\Microsoft\Cryptography
MachineGuid
LOCALAPPDATA / Stretheus / device.dat
```

### 2.3 平文のまま置かれているデータ

`_internal/` 配下は展開済みであり、解析を要しない。

| 置き場所 | 中身 |
|---|---|
| `_internal/src/settings/setting.json` | 初期設定の全文（点検時は API 接続先・Twitch `client_id`・採点アルゴリズムの重み。接続先は N1 の是正で空になった） |
| `_internal/src/models/yolox_tiny.onnx` | ぼかし用モデル |
| `_internal/src/{t1,t2}.jpg` `watermark.png` `comment_icon.png` | 画面素材（点検したが利用者情報の写り込みなし） |
| `_internal/ffmpeg/{ffmpeg,ffprobe}.exe` | gyan.dev full build 2026-03-12-git-9dc44b43b2（`--enable-gpl --enable-version3`） |

### 2.4 探して**見つからなかった**もの

| 探索対象 | 結果 |
|---|---|
| `D:\develop` / `D:/develop` を含む文字列 | **0 件**（`_internal/` 全体・exe・PYZ 内 code object の `co_filename` すべて） |
| `C:\Users\<開発者>` 形式のパス | **0 件** |
| 開発者のメールアドレス・氏名・連絡先 | **0 件** |
| API シークレット・署名鍵・`client_secret` | **0 件** |

`co_filename` は `src\services\device_id.py` のような**リポジトリ相対パス**になっており、
ビルド機のディレクトリ構成は残っていない（PyInstaller 側の挙動 + `spec` のサニタイズの両方が効いている）。

### 2.5 再現手順

本点検に使った解析スクリプトはセッション限りの一時領域に置いたため残していない。
再現する場合は以下で足りる（いずれも標準ライブラリのみ）。

1. `Stretheus.exe` を末尾から `b'MEI\014\013\012\013\016'` で検索し、
   `struct.unpack('!8sIIII', …)` でクッキー（pkglen / toc_off / toc_len / pyvers）を読む
2. `pkg_start = cookie_pos + 88 - pkglen` を起点に TOC を走査し、各エントリを切り出す
   （エントリ構造: `!i` 長さ, `!IIIB` 位置・圧縮後・圧縮前・圧縮フラグ, 1 バイトの種別, 残りが名前）
3. 取り出した `PYZ.pyz` の先頭 `PYZ\0` を確認し、オフセット 8 の `!i` が指す位置から
   `marshal.load` で TOC（dict）を読む
4. 各値 `(typ, pos, length)` について `zlib.decompress` → `marshal.loads` で code object を得る
5. `co_consts` を再帰的に辿れば入れ子の code object・文字列定数がすべて得られる

> **この手順を妨げる手立ては、Python で配布する限り存在しない。**
> 難読化は時間稼ぎにしかならないため、**「クライアントに秘密を置かない」設計を守ること**で対処する。
> 現状その設計自体は守られている（§6）ので、崩さないことが要点である。

---

## 3. 高

### S1. 自動更新が取得した実行ファイルを無検証で起動する 【高・sec1 から継続】

#### 現況

`src/utils/updater.py` は sec1 の指摘当時のままである。

```python
def download_installer(url, on_progress=None):
    dest = os.path.join(tempfile.gettempdir(), _INSTALLER_ASSET)   # 固定パス
    urllib.request.urlretrieve(url, dest, reporthook=on_progress)  # タイムアウト無し
    return dest

def launch_installer(installer_path):
    subprocess.Popen([installer_path], close_fds=True)             # 無検証で実行
```

ハッシュ検証・署名確認・URL の検証（スキーム / ホスト）・ダウンロードのタイムアウトのいずれも無い。
インストーラ本体（`AutoEdit.iss` の `PrepareToInstall`）は payload を SHA-256 で検証しているのに、
**その検証をする側の exe が無検証で降ってくる**という連鎖の切れ方も変わっていない。

> v1.4.6 から v1.8.2 まで **9 回のリリースがこの経路を通って配布された**。
> 既存利用者は全員この無検証の経路で更新している。

#### 是正（2026-11-07・実施済み）

sec1 §2.1 の案に沿って `src/utils/updater.py` を書き換えた。

| 穴 | 対応 |
|---|---|
| a 完全性検証が無い | 取得しながら SHA-256 を計算し、期待値と一致しなければ**実行経路へ残さない**（一時フォルダごと削除） |
| b 保存先が固定・予測可能 | `tempfile.mkdtemp(prefix="stretheus_update_")` で毎回作る専用フォルダへ置く |
| c URL を検証しない | `https` + `_ALLOWED_HOSTS`（`github.com` / `objects.githubusercontent.com` / `release-assets.githubusercontent.com`）に限定。**リダイレクト先も同じ関数で確かめる** |
| d タイムアウトが無い | `_DOWNLOAD_TIMEOUT_SEC = 300` を設定 |

期待ハッシュは次の順で解決する（sec1 の案 A）。

1. GitHub API が返すアセットの `digest`（`sha256:<hex>`）。あれば追加の通信をしない
2. 併置した `StretheusSetup.exe.sha256` アセット

**どちらも得られないリリースへは更新しない**（fail closed）。検証できないものを実行しないためで、
利用者には何も出さず、ログに `WARNING` を残す。この運用が崩れると利用者が更新に気づけないため、
`HowToRelease.md §8.1` と `installer/README.md §3.1` に手順とチェック項目を足した。

`tests/test_updater_verify.py` に 16 件を追加（URL 許可判定、ハッシュ形式の正規化、
不一致時に取得物が残らないこと、ハッシュが無いときに見送ること）。

> **残る限界。** ハッシュも GitHub 経由で取るため、**配布元アカウントの侵害には無力**である。
> 守れるのは経路上の改ざん・取り違え・TOCTOU までで、
> 配布元の侵害まで塞げるのは S2 のコード署名を前提とした署名検証（sec1 の案 B）だけである。
>
> **移行**: v1.8.2 以前のアプリは古い updater で動いているため、検証が効くのは
> 「この仕組みを載せた版から、さらに次の版へ」の更新からである。

### S2. インストール先がユーザー書込可能で、実行ファイルが未署名 【高・sec1 から継続】

#### 現況（実測）

```
Get-AuthenticodeSignature Stretheus.exe  →  Status: NotSigned   Signer: (なし)
AutoEdit.iss  →  DefaultDirName={localappdata}\Programs\Stretheus
                 PrivilegesRequired=lowest
```

同一ユーザー権限で動くプロセスが `_internal/` の DLL や `Stretheus.exe` を差し替えられる。
署名が無いため、利用者にも差し替えを検知する手段が無い。

インストール先の `setting.json` を書き換えるだけで**接続先 API を攻撃者のサーバーへ向けられる**
点にも注意する（JWT はそのサーバーへ送られる）。N1 の是正で配布 `setting.json` の
`api.base_url` は空になったが、**利用者が後から書き足せば効いてしまう**ことは変わらない。

#### 対策と現況（2026-11-07 時点）

**本丸のコード署名は未実施のままである。証明書の調達が要るため、実装だけでは解消できない。**
今回入れたのは「証明書が手に入った日に、手順どおり実行すれば署名できる」状態までである。

| # | 対策 | 状況 |
|---|---|---|
| 1 | **Authenticode コード署名** | **未実施（証明書が無い）。** 土台のみ整備: `AutoEdit.iss` に `#ifdef SIGN` で `SignTool` / `SignedUninstaller` を追加し、`ISCC /DSIGN` で有効になるようにした。証明書が無い間は従来どおりビルドできる。手順は `HowToRelease.md §7.2` に具体化（signtool の登録、タイムスタンプ必須の理由、本体 exe と インストーラの両方への署名、検証コマンド） |
| 2 | 管理者インストールを選べるようにする | **未実施。** `PrivilegesRequired=dialog` にするだけでは `setting.json` の書き込み先（`{app}\_internal\src\settings\`）が書けなくなるため、対策 3 とセットでなければ壊れる |
| 3 | 設定・トークン・ログをインストール先から出す | **部分的。** 作業フォルダ・Twitch トークン・ポイントは `%LOCALAPPDATA%\Stretheus\` へ移っている。残るのは **`setting.json` 本体**（`_internal` 配下）と **ログ**（カレント相対 / S8） |
| 4 | 正規の入手元 URL と SHA-256 を明記する | **実施。** `installer/README.md §3.1` でハッシュの作成を手順化し、リリースへ `.sha256` を上げる形にした（S1 の是正と同じ作業） |

S1 のハッシュ検証が入ったことで、**未署名のままでも更新経路の改ざんは検知できる**ようになった。
ただし「インストール済みのファイルを後から差し替える」経路は署名でしか塞げない。

> 対策 2 と 3 は、設定ファイルの置き場所を変える改修になる。既存利用者の設定移行と、
> インストーラの退避・復元（`AutoEdit.iss`）の見直しを伴うため、本書では実施していない。
> **着手するなら設計から**（`docs/CLAUDE.md`「実装前に設計を行うこと」）。

---

## 4. 中

### N1. 接続先が 3 か所で決まり、そのときの値が配布物へ焼き付く 【中・新規・2026-11-07 解消】

#### 初版の指摘の訂正

本書は当初、配布 `setting.json` が dev を指すことを「リリース前の戻し忘れ」とみなし、
**最上位の指摘（高）**に挙げていた。これは誤りである。

* `HowToRelease.md §6`: 「本番 API をデプロイするまでは dev のままで配る（prod は未デプロイ）」
* `StretheusPlan/plan.md §7.8.4`: 「④ 配布クライアントの接続先（**prod を立てる場合のみ**）」
  「dev は live キーで動いている」

**dev を指しているのは意図的な運用**であり、`stretheusapi-dev` が実運用を担っている。
また接続先のホスト名は通信を観察すれば分かるため、配布物から読めること自体を
情報漏洩の指摘とするのは筋が悪い。この 2 点で初版の判断は成り立たない。

#### 残っていた実質的な問題

それでも次の 2 点は実際の事故リスクだった。

| # | 問題 |
|---|---|
| a | **接続先が 3 か所で決まる。** `setting.json`（新規インストール）/ `DEFAULT_SETTINGS`（設定が欠けたとき）/ `_normalize_legacy_values`（既存の利用者）。1 か所だけ直しても向き先が変わらず、`plan.md §7.8.4` 自身が「**最も見落としやすい**」と書いていた |
| b | **`setting.json` は git 管理下かつ開発端末で日常的に書き換わる。** そのときの接続先がリポジトリと配布物の両方へ焼き付く（sec1 S7 と同じ構図） |

#### 是正（2026-11-07・実施済み）

接続先を **`services/config.py` の `_DEFAULT_BASE_URL` 1 箇所**へ集約した。

| 箇所 | 変更 |
|---|---|
| `services/config.py` | `_DEFAULT_BASE_URL` を現運用（dev）に。優先順位を **環境変数 > `setting.json` > 既定** とし、環境変数 `STRETHEUS_API_BASE_URL` を追加。**凍結配布では環境変数を無視**する（配布物の向き先を外から変えさせない / S2 の緩和） |
| `settings/settings_window.py` | `DEFAULT_SETTINGS` の `api.base_url` を空に。`_MANAGED_API_BASE_URLS` を追加し、既定として配ったことのある URL が保存されていたら空へ寄せて既定に追従させる |
| `settings/setting.json` | `api.base_url` を空に（リポジトリから接続先が消えた） |
| `main_window.spec` | 同梱時に `api.base_url` を必ず空へ落とす（`general` の 4 キーと同じ安全柵） |
| `tests/test_api_base_url.py` | 15 件を追加。「どの環境が現運用か」は固定せず、`_DEFAULT_BASE_URL` へ倒れることだけを見る |

これにより **接続先の切り替えは 2 か所**で済む（`_DEFAULT_BASE_URL` を変え、
`_MANAGED_API_BASE_URLS` へ切り替え前の URL を足す）。開発端末で別環境を試すときは
環境変数を使うため、`setting.json` を汚さない。

> `plan.md §7.8.4` の「3 か所セット」手順は autoedit 側のこの変更に追従していない。
> 別リポジトリのため本書では直していない。**更新が要る。**

#### 残る論点（本書の範囲外）

**`dev` という名前の環境が、live 決済を含む実運用を担っている**（`plan.md §7.8.7`）。
技術的な欠陥ではないが、命名と環境の役割が食い違っている状態は事故のもとになる。
prod を立てるのか、dev を正式な環境として扱い直すのかは、運用側で決める話である。

### S4. Twitch アクセストークンを平文 JSON で保存している 【中・sec1 から継続】

#### 現況（実測）

```
C:\Users\<利用者>\AppData\Local\Stretheus\archive_work\twitch_token.json   106 bytes
  { "access_token": "<30 文字>", "scope": "user:read:broadcast", "token_type": "bearer" }
```

`src/archive/twitch_auth.py` の `_write_token_file` / `_read_token_file` は
`json.dump` / `json.load` のままである。

**同じコードベースに DPAPI の実装が既にある**（`src/services/auth_store.py` の
`protect()` / `unprotect()`）。`src/services/device_id.py` はそれを使って `device.dat` を保護しており、
Stretheus の JWT（`auth.dat`）も同様に守られている（§6 で実測）。**Twitch 側だけが取り残されている。**

#### 何が問題か

配布物を解析すれば保存先も形式も分かる（§2.2 で `archive_work` / `twitch_token.json` の文字列が
そのまま得られる）。利用者の端末で動く任意のプロセスが、このファイルを読むだけで
Twitch のユーザーアクセストークンを持ち出せる。`scope` に `user:read:broadcast` が付くため、
配信者のアカウントに対する読み取りが成立する。

#### 是正（2026-11-07・実施済み）

sec1 §3.2 の案どおり、`src/services/auth_store.py` の `protect` / `unprotect`
（DPAPI / CurrentUser）を `twitch_auth.py` から借りる形にした。
**同じ仕組みを 2 つ持たない**ため、新しい依存も新しい鍵管理も増えていない。

| 観点 | 対応 |
|---|---|
| 保存 | `protect()` で暗号化してバイナリで書く。復号できるのは同じ Windows ユーザーだけ |
| 暗号化できない環境 | **保存しない**（平文では書かない）。今回のセッションは使えるため、利用者には次回起動時の再ログインだけを求める |
| 既存の平文からの移行 | 読み込み時に復号を試し、失敗したら平文 JSON として読む。読めたら**その場で暗号化して置き換える**ので、平文は残らない |
| 他ユーザー・壊れたファイル | `None` を返して再ログインへ倒す（`auth_store` と同じ振る舞い） |

`tests/test_token_protection.py` に検証を追加した（平文が残らないこと、往復、
旧平文からの移行、暗号化できないときに保存しないこと、ログアウトでの削除）。

> **実データでの移行確認は行っていない。** 同じ形のダミーで検証している。
> 既にログイン済みの端末では、**次回アプリを起動した時点で自動的に暗号化へ置き換わる**。

### S5. OAuth トークンがブラウザ履歴に残る 【中・sec1 から継続】

`_RELAY_HTML` は現状も `window.location.replace('/capture?' + h)` でフラグメントをクエリへ
付け替えている。`replace` なので「戻る」履歴には積まれないが、**URL 自体は履歴に残り、
ローカルサーバのリクエストラインにも載る**。sec1 §3.3 の POST 化で解消する。

### S3. `setting.json` の値がそのまま外部プロセスとして実行される 【中・sec1 から継続】

`src/modules/ffmpeg_runner.py` の `_resolve_exe()` は現状も
「絶対パス指定はそのまま採用」で始まる。インストール先がユーザー書込可能（S2）なため、
**`setting.json` の `ffmpeg.executable` を書き換えるだけでアプリ起動後に任意の exe が動く**。
sec1 §3.1 の案（凍結配布では同梱物のみを認める）で塞ぐ。

### N2. `points.json` の改ざんで透かしを回避できる 【中・新規】

#### 該当（実測）

```
C:\Users\<利用者>\AppData\Local\Stretheus\points.json   69 bytes
  { "subscribed": true, "checkedAt": "2026-11-06T01:53:34.614983+00:00" }
```

`src/services/points.py` の `_write_json` は平文で書き出す。
`subscription_cache_hours` の既定は **72** である（配布 `setting.json`）。

#### 何が問題か

ポイント処理はサーバー主導で設計されており（予約の応答だけで透かしの要否を決める）、
その点は妥当である。ただし**オフライン時のサブスク救済だけはローカルの値を信じる**。
このファイルを `{"subscribed": true, "checkedAt": <現在時刻>}` に書き換え、
ネットワークを遮断すれば、最大 72 時間は透かし無しで出力できる。

情報漏洩ではなく**収益面のリスク**だが、配布物の解析でキャッシュの場所・キー名・
判定ロジックが読めるため、再現は容易である。

#### 是正（2026-11-07・実施済み）

完全に塞ぐことはできない（オフライン救済という仕様自体が「ローカルを信じる」ことを含むため）。
挙げた 3 段階のうち、1 と 2 を入れた。

| # | 対策 | 状況 |
|---|---|---|
| 1 | **DPAPI で保護する** | **実施。** `_read_state` / `_write_state` を `auth_store.protect` 経由にした（S4 と同じ仕組み）。手で書き換える手口は止まる。暗号化できない環境では保存せず、オフライン時は**透かしを入れる側（安全側）へ倒れる** |
| 2 | **救済の窓を狭める** | **実施。** 既定を **72 → 24 時間**へ。旧既定 72 を保存している利用者は読み込み時に 24 へ詰める（`_migrate_subscription_cache_hours`）。利用者が自分で変えた値は尊重する |
| 3 | サーバー署名を入れる | **未実施。** サーバー側の変更が要るため、本書では扱わない |

**送信待ちキュー（`pending_commits.json`）は平文のまま**にした。こちらは保存できないと
ポイントの確定・解放がサーバーへ届かず**利用者の不利益**になるため、どんな環境でも
必ず書けることを優先した。改ざんされてもサーバーが予約 ID を見て判断するため、得をする余地が小さい。

`tests/test_token_protection.py` に検証を追加した（平文が残らないこと、旧平文からの移行、
暗号化できないときに保存しないこと、キューが平文のままであること、72 → 24 の移行）。

> 解析して自分で暗号化し直す相手には、1 も 2 も効かない。そこまで塞ぐには 3 が要る。
> 本書は「手で書き換えるだけで回避できる」状態を解消したにとどまる。

### S6. 使っていない重量依存を同梱している 【中・sec1 から継続・2026-11-07 解消】

#### 指摘の訂正

sec1 と本書の初版は `torch` を「**使っていない**」としていたが、これは誤りである。
`src/modules/speaker_filter.py`（話者照合によるテロップの絞り込み）が
`torch` / `torchaudio` を遅延 import している。

ただし次の事情があり、**配布物から外す判断は変わらない**。

* `docs/quality/resolve1.md §12.6・§12.7` で「**実装したが不採用**」「この素材では成立しない」と
  結論済み（ONNX 実装は既に撤去されている）
* 既定は `"off"` で、**設定画面にも出さない**（`setting.json` を手で書く隠し設定）
* `torch` を読めないときは警告を出して**素通りする**フォールバックが実装済み（`_import_torch`）

`transformers` のほうは本当にソースから一切参照していない
（`faster-whisper` の conversion extra や `sentence-transformers` などが推移的に引いていただけ）。

#### 現況（是正前の実測・`_internal/` の容量上位）

```
426M  ffmpeg        322M  torch         125M  PySide6      102M  llvmlite
 65M  av.libs        60M  ctranslate2    54M  scipy         39M  transformers
 32M  onnxruntime    21M  src            21M  numpy.libs    20M  scipy.libs
```

容量だけでなく、**使っていないコードが攻撃面になる**点が問題である
（`transformers` はモデルの自動ダウンロード経路を持つ）。

#### 是正（2026-11-07・実施済み）

`main_window.spec` の `excludes` へ `torch` / `torchaudio` / `transformers` を追加した。

**配布フォルダは 1.5GB → 837MB（約 663MB 減）**。sec1 の見積もり（361MB）を上回ったのは、
`torch` を引いていた `llvmlite`(102M) / `scipy`(54M+20M) / `sklearn`(13M) も連鎖で落ちたためである。
除外後の上位は次のとおりで、`ffmpeg`(426M) と `PySide6`(125M) が大半を占める形になった。

```
426M  ffmpeg        125M  PySide6        65M  av.libs       60M  ctranslate2
 32M  onnxruntime    21M  src            21M  numpy.libs    13M  PIL
```

sec1 §3.4 が求めていた確認はすべて実施した。

| 確認 | 結果 |
|---|---|
| 主要モジュール 15 本の import（**配布物と同じ条件**＝消えた 12 パッケージを遮断） | すべて成功 |
| `faster_whisper` / `av` / `onnxruntime` / `numpy` / `PIL` の import | すべて成功 |
| `tests` 全件（同条件） | 2,319 件すべて合格 |
| `speaker_filter` のフォールバック | `_import_torch()` → `(None, None)`、`build_embedder()` → `None`。例外を漏らさず素通り |
| **音声認識の実走行**（`small` / 同条件） | 「This is a test of the speech-recognized system.」を認識 |
| **音声認識の実走行**（`large-v3` ＝配布設定 / 同条件） | 「This is a test of the Speech Reconnaissance System.」を認識。推論完了時点で torch 系の読み込み 0 件 |
| 配布 exe の起動 | GUI が立ち上がり、25 秒後も生存（初期化を完走） |

実走行の音声は Windows の音声合成（SAPI）で作り、モデルはローカルキャッシュのみを使った
（`local_files_only=True`。外部からは取得していない）。

> **話者照合は配布物では使えなくなった。** `speaker_filter.py` の冒頭にその旨を書いてある。
> マイク単独トラックなど綺麗な音声で試したくなったら、`excludes` から
> `torch` / `torchaudio` を外して再ビルドすれば戻る。
>
> **CLI 用の `main.spec` は変更していない**（`excludes` が空のまま）。
> 配布しているのは `main_window.spec` の成果物であり、CLI 版をビルドする場合は別途そろえること。

---

## 5. 低・情報

### N3. 端末 ID の導出が決定論的で、ソルトが配布物から読める 【低・新規】

```python
# src/services/device_id.py （PYZ から復元した文字列定数にそのまま出る）
_DERIVATION_SALT = "stretheus-device-v1"
digest = hashlib.sha256(f"{_DERIVATION_SALT}:{seed}".encode("utf-8")).digest()
```

`seed` は Windows の `MachineGuid`。ソルトが配布物から読めるため、
**ある端末の `MachineGuid` を知る立場なら、その端末の匿名セッション ID を計算できる**。

個人を直接特定する値ではなく（`MachineGuid` 自体は外部へ出さずハッシュだけを送る設計で、
プライバシー配慮としては妥当）、悪用には `MachineGuid` の入手が別途要る。
影響は「匿名セッションのなりすまし（未ログイン利用者のポイント消費）」に限られるため **低**とする。

対策を入れるならサーバー側で、匿名セッションの発行時に**サーバーが生成した乱数を混ぜて**
クライアントへ返し、以後はそれを使う（クライアントだけでは ID を決められないようにする）。
クライアント側の難読化では解決しない。

### N4. 内部 API のエンドポイント一覧が配布物から完全に読める 【情報】

PYZ の復元で、以下がそのまま一覧できる。

```
/api/auth/twitch/authorize-params   /api/auth/google/authorize-params
/api/auth/twitch/login              /api/auth/google/login
/api/auth/device   /api/auth/refresh   /api/auth/logout
/api/billing/config  /api/billing/checkout-session  /api/billing/portal-session
/api/billing/subscription            /api/points
/api/analytics/{sessions,projects,events,export-logs,processing-logs}   /api/errors
```

**これ自体は設計上避けられない**（クライアントが叩く以上、通信を見れば同じことが分かる）。
記録として残す趣旨であり、クライアント側の対応は不要である。

ただし前提として、**サーバー側がクライアントを信用していないこと**が要る。
本書ではサーバー側を点検していないため、`StretheusAPI` 側で
「全エンドポイントが JWT の検証と所有者チェックを持つか」「`/api/points` が
クライアントの申告額をそのまま信じていないか」を別途確認すること（§8）。

### N5. Twitch Client-ID が配布物に含まれる 【情報・対応不要】

`archive.auth.client_id` は公開クライアントの設計上、配布物に載るのが正常である
（sec1 S11 と同じ扱い）。`client_secret` は空であり、`spec` がビルド時に検査している。

---

## 6. 解消が確認できた点・守れている点

### 6.1 解消した指摘

| # | 指摘 | 現況（実測） |
|---|---|---|
| **N1** | 接続先が 3 か所で決まり、そのときの値が配布物へ焼き付く | **解消**（2026-11-07）。`config._DEFAULT_BASE_URL` 1 箇所へ集約し、環境変数での上書きと `spec` の安全柵を追加した。経緯と初版の訂正は §4 を見ること |
| **S4** | Twitch アクセストークンを平文 JSON で保存 | **解消**（2026-11-07）。`auth_store.protect`（DPAPI）で暗号化し、既存の平文は読み込み時に置き換える。詳細は §4 |
| **N2** | `points.json` の改ざんで透かしを回避できる | **解消**（2026-11-07）。サブスク判定を DPAPI 化し、救済の窓を 72 → 24 時間へ。送信待ちキューは平文のまま（理由は §4）|
| **S6** | 使っていない重量依存を同梱 | **解消**（2026-11-07）。`excludes` へ `torch` / `torchaudio` / `transformers` を追加し、**1.5GB → 837MB**。音声認識の実走行まで確認済み。詳細と指摘の訂正は §4 |
| **S7** | 配布 `setting.json` に開発機の絶対パスが残る | **解消**。`general` の 4 キーと `timeline.project.recent*` が空で出力され、配布物全体を検索しても絶対パスは 0 件。`spec` の `_build_shipped_settings()` が機械的に止める仕組みになっており、再発しにくい |
| **S8** | ログ出力先がカレント相対 | 部分的に改善。利用者データ（作業フォルダ・トークン・ポイント）は `%LOCALAPPDATA%\Stretheus\` へ移っている。`logging.log_dir` は `"logs"` のままなので、ログだけは引き続きカレント相対である |

### 6.2 維持すべき設計

| # | 守れている点 | 根拠（実測） |
|---|---|---|
| 1 | **配布物に秘密を置かない**。OAuth は認可コードフローで、`clientId` / `redirectUri` / `scopes` を**サーバーから受け取る** | `services/stretheus_auth.py`。Google 追加後も配布物側に秘密が増えていない |
| 2 | **Stretheus の JWT は DPAPI で保護**されている | `auth.dat`(984 bytes) の先頭が `01 00 00 00 D0 8C 9D DF 01 15 D1 11 8C 7A 00 C0` = DPAPI blob。平文では書かない実装（暗号化できなければ保存を諦める） |
| 3 | 端末 ID は `MachineGuid` を**そのまま送らず** SHA-256 で導出 | `services/device_id.py`。生の GUID は端末外へ出ない |
| 4 | **テレメトリ送信前にマスクする**（パス・UNC・URL クエリ・メール・32 文字以上のトークン風文字列・環境変数由来のユーザー名） | `services/log_payloads.py` の `_MASK_RULES` / `_mask_user_name`。送信本文に生のファイル名・パスは載らない |
| 5 | **ログにトークンを出さない** | 実ログを検索し、`access_token` / `Bearer` / JWT 本体の出力 0 件 |
| 6 | 透かし・ポイント消費の判断を**サーバーの応答だけで決める**（残高から推測しない） | `services/points.py`。クライアント側の計算を信じる作りになっていない |
| 7 | ビルド時に**機械的な安全柵**がある（開発機パス・`client_secret` でビルドを止め、API 接続先を空へ落とす） | `main_window.spec`。接続先の安全柵は N1 の是正で追加した |
| 8 | 通信は全て HTTPS、`certifi` を同梱 | `collect_data_files('certifi')` |
| 9 | UPX を使わない（AV 誤検知を避ける） | `spec` の `upx=False` |
| 10 | ライセンス表記を同梱し、不足を機械で確認できる | `tools/collect_licenses.py --check` が 76 件・不足 0。`--scan-bundle` も指摘なし |

---

## 7. 対応の順序（提案）

| 段 | 対象 | 内容 | 目安 |
|---|---|---|---|
| ~~1~~ | ~~**N1**~~ | **完了（2026-11-07）**。接続先を `config._DEFAULT_BASE_URL` 1 箇所へ集約し、環境変数での上書きと `spec` の安全柵を追加 | 済 |
| ~~1~~ | ~~**S1**~~ | **完了（2026-11-07）**。ハッシュ検証・URL 検証・専用一時フォルダ・タイムアウトを実装し、リリース手順へハッシュ添付を追加 | 済 |
| ~~2~~ | ~~**S4**~~ | **完了（2026-11-07）**。DPAPI 化と既存平文からの移行を実装 | 済 |
| **2** | **S3** | 実行ファイルパスの解決を「同梱 or 実在する絶対パス」に限定 | 小 |
| ~~2~~ | ~~**N2**~~ | **完了（2026-11-07）**。サブスク判定を DPAPI 化し、既定を 72 → 24 時間へ（旧既定の移行つき） | 済 |
| **3** | **S5** | relay を POST 化し、`Host` 検証と `no-store` を追加 | 小 |
| ~~3~~ | ~~**S6**~~ | **完了（2026-11-07）**。`torch` / `torchaudio` / `transformers` を除外し、**663MB 削減**（1.5GB → 837MB）。音声認識の実走行まで確認 | 済 |
| **4** | **S2** | **コード署名の導入（証明書調達）。** 土台（`.iss` の `/DSIGN`・手順書）は整備済みで、残るのは証明書そのもの。実現後に S1 へ署名検証を重ねる | 大 |
| **4** | **S2-2/3** | 管理者インストールを選べるようにする（`PrivilegesRequired=dialog`）。**`setting.json` の置き場所を `%LOCALAPPDATA%` へ移す改修とセット**。既存設定の移行とインストーラの退避・復元の見直しを伴うため、着手するなら設計から | 中〜大 |
| **—** | **N3 / N4 / N5** | 記録のみ。N3・N4 はサーバー側で担保する | — |

**2026-11-07 に 5 件を是正した**（N1 / S1 / S4 / N2 / S6）。残っているのは次の 3 件である。

| 残り | 内容 | 重さ |
|---|---|---|
| **S3** | 実行ファイルパスの解決を「同梱 or 実在する絶対パス」に限定 | 小 |
| **S5** | relay を POST 化し、`Host` 検証と `no-store` を追加 | 小 |
| **S2** | コード署名。**証明書の調達が前提で、実装では進められない** | 大 |

S3 と S5 は小さく、次の着手先として妥当である。
S2 は**証明書を買うかどうかの判断だけが残っている**状態で、
これが片付かない限り「インストール済みのファイルを後から差し替える」経路は開いたままになる。

---

## 8. 本書の限界（再掲）

* **サーバー側（`StretheusAPI`）を点検していない。** 本書は「クライアントから何が見えるか」までである。
  §5 の N4 で挙げたエンドポイントが実際に守られているかは、別リポジトリ側で確認が要る。
  特に `/api/points` と `/api/billing/*` が**クライアントの申告を信じていないこと**を確かめること
* **動的解析・侵入テスト・マルウェアスキャンは行っていない。** 静的な読み取りと、
  既存のローカル保存データの保護状態の実測のみである
* **依存パッケージの CVE 照合をしていない。** §4 の S6 は同梱の有無と容量の話であり、
  脆弱性の有無を示すものではない
* **zip 化・インストーラ生成・クリーン PC での挙動確認をしていない。** 対象は `src/dist/Stretheus/` である
* **本書は v1.8.2 のスナップショットである。** 同梱物・既定値・更新経路は版ごとに変わるため、
  件数・サイズ・パスをそのまま後の版へ引用してはならない
* 各対策のコード片は**方針を示すためのもの**で、そのままの動作確認はしていない。
  実装時は本書をもとに設計・テストを行うこと
