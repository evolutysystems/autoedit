# sec3 — 是正後のリリースビルド再点検（v1.8.2 / 2026-11-07 是正版）

## 0. 本書の位置づけ

* 対象: **sec2 の是正を入れた後のリリースビルド成果物**
  * `src/dist/Stretheus/`（2026-11-07 に `pyinstaller main_window.spec --clean --noconfirm` で生成）
  * `Stretheus.exe` 12,819,794 bytes / 配布フォルダ合計 **837MB**
  * `licenses/` を同梱した状態（76 件・不足 0）
* **sec2 との関係**:
  * sec2 = v1.8.2 の点検と、同日に入れた是正（N1 / S1 / S4 / N2 / S6）の記録
  * **本書 = その是正が配布物に効いているかの確認と、改めての点検**
  * sec2 で「未対応」として残した指摘（S2 / S3 / S5 / S8 / S9 / N3 / N4）は §4 に持ち越す
* 調査方法は sec2 §2 と同じ（exe の CArchive 展開 → PYZ の marshal 復元 → 文字列と参照名の走査、
  配布物全体の文字列検索、ローカル保存データの実測）。加えて本書では
  **DPAPI の保護強度を実際に復号して確かめた**（§3 T2）。
* **未実施**（§8 に再掲）:
  * zip 化・Inno Setup によるインストーラ生成
  * 動的解析・ペネトレーションテスト・マルウェアスキャン
  * 依存パッケージの CVE 照合（§3 T3 で版の古さに触れるが、**CVE の断定はしていない**）
  * クリーン PC での挙動確認
  * サーバー側（StretheusAPI）の認可の検証

---

## 1. 総評

**sec2 の是正はすべて配布物に効いている**（§2）。配布サイズは 1.5GB → 837MB、
exe 内のモジュール数は 8,259 → 1,802 まで落ち、解析して得られる情報も減った。
**開発機の痕跡・秘密情報が配布物に無い**という状態も保たれている。

そのうえで、本書では新たに 6 件を挙げる。**性質が 2 つに分かれる。**

1. **アプリを消しても利用者の情報が残る**（T1）。
   アンインストールは `archive_work` しか消さないため、**Stretheus の JWT（`auth.dat`）と
   編集したファイルのパス一覧（`project_ids.json`）が端末に残り続ける**。
   本書で最も実害に近い。
2. **防御の「効く範囲」が思ったより狭い**（T2 / T5）。
   S4・N2 で入れた DPAPI は、**同じ Windows ユーザーで動くプロセスなら鍵を知らなくても復号できる**
   （実際に復号して確かめた）。S1 の検証も、検証から起動までの隙間は塞げていない。
   どちらも「平文よりはるかにまし」だが、**万能ではない**ことを記録しておく。

| # | 指摘 | 深刻度 | 初出 | 現況 |
|---|---|---|---|---|
| **T1** | アンインストールで **JWT と利用者データが残る** | **中** | 本書 | 未対応 |
| **T2** | DPAPI に**エントロピーを与えていない**（S4 / N2 の限界） | 中 | 本書 | 未対応 |
| **T3** | 同梱 FFmpeg が **full build**（446MB・不要機能多数・8 か月前） | 中 | 本書 | 未対応 |
| **T4** | 音声認識モデルを**実行時に外部から取得**する | 低 | 本書 | 未対応 |
| **T5** | 更新の**検証後から起動までの差し替え余地**（S1 の残存） | 低 | 本書 | 未対応 |
| **T6** | 利用者データ（チャット・タイムライン・パス一覧）が平文 | 情報 | 本書 | 設計上は妥当 |
| **S2** | インストール先がユーザー書込可能／**実行ファイルが未署名** | **高** | sec1 | **未対応**（証明書待ち） |
| **S3** | `setting.json` の値がそのまま外部プロセスとして実行される | 中 | sec1 | **未対応** |
| **S5** | OAuth トークンがブラウザ履歴に残る | 中 | sec1 | **未対応** |
| **S8** | ログ出力先がカレント相対 | 低 | sec1 | **未対応** |
| **S9** | 起動時に無同意で GitHub へ通信する | 低 | sec1 | **未対応** |
| **N3 / N4** | 端末 ID の導出が決定論的／API 一覧が読める | 低・情報 | sec2 | 記録のみ |

---

## 2. sec2 の是正が効いているかの確認

すべて**配布物を実測して**確かめた。

| 指摘 | 確認したこと | 結果 |
|---|---|---|
| **N1** 接続先が 3 か所 | 同梱 `setting.json` の `api.base_url` | `""`（空）。`config._DEFAULT_BASE_URL` 1 箇所へ倒れる |
| **S1** 更新が無検証 | exe 内の `updater` が参照する名前 | `sha256` / `mkdtemp` / `_is_allowed_url` / `hexdigest` をすべて確認 |
| **S4** Twitch トークンが平文 | `%LOCALAPPDATA%\Stretheus\archive_work\twitch_token.json` | **DPAPI blob**（`01 00 00 00 D0 8C 9D DF…` / 376 bytes） |
| **N2** サブスク判定が平文 | `%LOCALAPPDATA%\Stretheus\points.json` | **DPAPI blob**（344 bytes）。同梱設定の `subscription_cache_hours` は `24` |
| **S6** 重量依存の同梱 | `_internal/` の中身 | `torch` / `torchaudio` / `transformers` / `sklearn` / `scipy` / `llvmlite` が消えた |

> **S4 / N2 の移行は実データで動いた。** sec2 執筆時点では「ダミーでしか確認していない」と
> 断ってあったが、本書のために配布 exe を起動したところ、
> 端末に残っていた**平文の `twitch_token.json` と `points.json` が DPAPI blob へ置き換わった**。
> 移行経路が実際に機能することを確認できた。

### 2.1 解析して得られる情報の減り方

| 項目 | sec2 時点 | 本書 |
|---|---|---|
| 配布フォルダ | 1.5 GB | **837 MB** |
| `Stretheus.exe` | 66,907,990 bytes | **12,819,794 bytes** |
| PYZ の総モジュール数 | 8,259 | **1,802** |
| うち自前モジュール | 136 | 136（変わらず） |
| 開発機の絶対パス | 0 件 | **0 件**（維持） |
| 平文で読める接続先 | `setting.json` に dev URL | **0 件**（`config.py` の 1 箇所のみ・要 PYZ 展開） |

自前モジュール 136 本が名前ごと読め、`dis` で処理を追えること自体は変わらない（sec2 §2 のとおり）。
**Python で配布する限りこれは避けられず、「クライアントに秘密を置かない」設計で対処する**という
方針も変わらない。

---

## 3. 新規の指摘

### T1. アンインストールで JWT と利用者データが残る 【中・新規】

#### 該当

```
; installer/AutoEdit.iss の [UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{localappdata}\{#MyAppName}\archive_work"
Type: dirifempty;     Name: "{localappdata}\{#MyAppName}"
```

消えるのは `{app}` と `archive_work` だけである。
`%LOCALAPPDATA%\Stretheus\` の直下に残るものを実測した。

| 残るファイル | 中身 | 消えるべきか |
|---|---|---|
| `auth.dat`（984 bytes） | **Stretheus の JWT**（access_token / セッション期限 / ユーザー情報） | **消すべき** |
| `points.json`（344 bytes） | サブスク判定のキャッシュ | 消すべき |
| `pending_commits.json` | ポイントの送信待ちキュー | 消すべき（送信済みなら不要） |
| `project_ids.json`（3,613 bytes） | **編集したファイルの絶対パスと projectId の対応表** | 消すべき |
| `device.dat`（環境により） | 端末 ID の控え | 消すべき |

`dirifempty` は「空なら消す」ため、これらが残る限り `%LOCALAPPDATA%\Stretheus` ごと残る。

#### 何が問題か

* **アンインストールしたのに認証情報が端末に残る。** JWT はセッション期限まで有効で、
  `auth.dat` を持ち出せば（T2 のとおり同一ユーザーなら復号できる）そのアカウントとして API を叩ける
* **`project_ids.json` は「何を編集したか」の履歴**である。中身は絶対パスで、
  先頭を見ると `{"d:\\develop\\…` のように**利用者のファイル構成がそのまま入る**
* 中古 PC の譲渡・共用 PC・退職時の端末返却で、利用者は「消したつもり」になっている

> sec1 は「アンインストールでトークン・設定ごと `{app}` を削除する」を**守れている点**に挙げていた。
> これはデータが `{app}` にあった頃の話で、`docs/error/fromUser/resolve.md §6-6-6` で
> `%LOCALAPPDATA%` へ移したときに**この前提が崩れた**。`archive_work` だけは追従させたが、
> 認証情報は取り残されている。

#### 対策

`[UninstallDelete]` へ明示する。`archive_work` を足したときと同じ形で済む。

```
; 認証情報と利用者データもインストール先の外にあるため明示削除する (sec3 T1)。
; archive_work と同じ理由 (docs/error/fromUser/resolve.md §6-6-6 で外へ出したぶん、
; アンインストールでは自動で消えなくなった)。
Type: files;          Name: "{localappdata}\{#MyAppName}\auth.dat"
Type: files;          Name: "{localappdata}\{#MyAppName}\device.dat"
Type: files;          Name: "{localappdata}\{#MyAppName}\points.json"
Type: files;          Name: "{localappdata}\{#MyAppName}\pending_commits.json"
Type: files;          Name: "{localappdata}\{#MyAppName}\project_ids.json"
Type: filesandordirs; Name: "{localappdata}\{#MyAppName}\project_thumbs"
Type: dirifempty;     Name: "{localappdata}\{#MyAppName}"
```

**サーバー側のログアウトまでは行わない**点に注意する。ローカルの JWT を消すだけで、
サーバーのセッションは期限まで残る。そこまで求めるなら、アンインストール時に
`POST /api/auth/logout` を叩く必要があるが、オフラインのときに失敗するため
「消せるものはローカルで消す」で十分と考える。

### T2. DPAPI にエントロピーを与えていない 【中・新規 / S4・N2 の限界】

#### 該当

```python
# src/services/auth_store.py
ok = ctypes.windll.crypt32.CryptProtectData(
    ctypes.byref(source), _DESCRIPTION, None, None, None,
    #                                   ^^^^ pOptionalEntropy = None
    _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result))
```

#### 実証

アプリのコードを一切使わず、素の `ctypes` で `CryptUnprotectData` を呼ぶだけの
「第三者のツール」を書いて試した（対象は本点検で作ったダミーのみ）。

```
アプリと同じ方法で保護して保存した: 344 bytes
  平文が残っていないか: True
エントロピー無しで復号を試みた結果:
  **復号できた** -> {"access_token": "DUMMY-NOT-A-REAL-TOKEN", "scope": "user:read:broadcast"}
```

#### 何が問題か

`CRYPTPROTECT_LOCAL_MACHINE` を使っていない（＝ CurrentUser スコープ）ので
**他ユーザー・他端末では読めない**。ここは正しい。
しかし `pOptionalEntropy` が無いため、**同じ Windows ユーザーで動くプロセスなら、
アプリの鍵も知識も持たずに復号できる**。

つまり S4 / N2 で引き上がったのは次の一段だけである。

| | 是正前 | 是正後 |
|---|---|---|
| ファイルを開いて読む | **トークンが読める** | 暗号文で読めない |
| DPAPI を呼ぶツールで読む | （不要） | **読める** |
| 別ユーザー・別端末へコピー | 読める | 読めない |

ブラウザの Cookie 窃取ツールが DPAPI blob を無差別に復号して回るのと同じ手口が通る。
**「平文よりはるかにまし」だが、同一ユーザーのマルウェアは防げない。**

#### 対策

`pOptionalEntropy` にアプリ固有のバイト列を渡す。`protect` / `unprotect` の両方へ
同じ値を渡すだけで、**汎用の窃取ツールは復号できなくなる**。

```python
# アプリ固有のエントロピー (sec3 T2)。
# DPAPI blob を無差別に復号して回る汎用のツールは、このバイト列を知らないため通らない。
# **このアプリを狙った攻撃には効かない** (配布物から読めるため / sec2 §2)。
# 用途ごとに変えると、auth.dat の暗号文を points.json へ置いても復号できなくなる。
_ENTROPY_AUTH = b"Stretheus/auth/v1"
```

> **既存ファイルの移行が要る。** エントロピーを変えると今の暗号文は復号できなくなるため、
> 「エントロピー付きで試す → 失敗したらエントロピー無しで試す → 読めたら付きで書き直す」
> の順にすること。S4 で平文から移行したときと同じ形である。
>
> 効果は限定的で、**T1（そもそも消えない）のほうが先に手を付ける価値がある**。

### T3. 同梱 FFmpeg が full build で攻撃面が広い 【中・新規】

#### 該当（実測）

```
_internal/ffmpeg/ffmpeg.exe    223,322,624 bytes
_internal/ffmpeg/ffprobe.exe   223,116,800 bytes   → 合計 426MB (配布物の 51%)
ffmpeg version 2026-03-12-git-9dc44b43b2-full_build-www.gyan.dev
```

**配布物の過半を FFmpeg が占める**（S6 で torch を外した結果、相対的に最大になった）。
`full_build` のため、このアプリが使わない機能が多く入っている。

| 分類 | 有効になっている機能 | アプリでの用途 |
|---|---|---|
| **外部コードの読み込み** | `--enable-avisynth`（スクリプト実行）/ `--enable-frei0r`・`--enable-ladspa`（プラグイン DLL） | **無し** |
| **ネットワーク** | `--enable-libssh`（SFTP）/ `--enable-librist` / `--enable-libsrt` / `--enable-libzmq` | **無し**（入力はローカルファイルのみ） |
| 光学メディア | `--enable-libbluray` / `--enable-libdvdnav` / `--enable-libdvdread` / `--enable-libcdio` | 無し |
| その他 | `--enable-openal` / `--enable-libcaca` / `--enable-libgme` / `--enable-libmodplug` / `--enable-libopenmpt` / `--enable-chromaprint` | 無し |

アプリが実際に使うのは、無音検出（`silencedetect`）・音量（`loudnorm`）・
字幕焼き込み（`libass` / `freetype` / `fribidi` / `harfbuzz`）・
エンコード（`libx264` / `aac`）・スケール（`libzimg`）程度である。

#### 何が問題か

* **使わないデコーダ・プロトコル・プラグイン読み込み経路の分だけ攻撃面が広い。**
  FFmpeg の脆弱性は「細工した入力ファイルを解析させる」形で成立するものが多く、
  本アプリは**利用者が持ち込んだ動画や Twitch から取得した VOD を解析する**ため、
  入力が常に信頼できるとは限らない
* **ビルドが 2026-03-12 で、本書の時点から約 8 か月前。** その間の修正が入っていない
  （**本書では CVE の照合をしていない**。版が古い事実のみを挙げる）
* 容量面でも、`essentials_build` に替えれば 426MB の大半を削減できる見込みがある

#### 対策

| 優先 | 対策 | 補足 |
|---|---|---|
| 1 | **新しいビルドへ更新する** | まずこれ。`full_build` のままでも版を上げる価値がある |
| 2 | **`essentials_build` への差し替えを検討する** | `libass` を含むことの確認が要る（字幕焼き込みに必須）。差し替え後は**無音カット・字幕焼き込み・結合・縦動画・ぼかしの実走行**で確認する |
| 3 | 更新手順を決める | `HowToRelease.md §2.2` へ「同梱 FFmpeg の版と入手元・更新時期」を書き、版を固定して記録する |

> **ライセンスに注意。** 現在は GPL ビルド（`--enable-gpl --enable-version3`）で、
> `licenses/FFmpeg/` に全文とソース入手先を置いている（`HowToRelease.md §5.2.1`）。
> LGPL ビルドへ替えるなら `tools/license_manifest.json` も直すこと。

### T4. 音声認識モデルを実行時に外部から取得する 【低・新規】

#### 該当

```python
# src/modules/subtitle_generator.py:594,599
whisper_model_cls(model_name, device=device, compute_type=compute_type)
```

`download_root` も `local_files_only` も指定していないため、`faster-whisper` は
モデル名（配布既定は `large-v3`）を **HuggingFace Hub から取得**し、
`%USERPROFILE%\.cache\huggingface\hub` へ置く。配布物に入っているのは
VAD の `silero_vad_v6.onnx` だけで、認識モデル本体は入っていない
（転送用の `hf_xet` 8MB が同梱されているのはこのため）。

#### 何が問題か

* **配布物に無いデータを、初回利用時に外部から取り込む経路がある。**
  完全性は `huggingface_hub` の検証任せで、アプリ側では確かめていない
  （S1 でインストーラのハッシュを検証するようにしたのと対照的である）
* 取得先・取得量を利用者へ事前に説明していない（数 GB になる）
* オフライン環境では音声認識が使えない

モデルは実行コードではなく ctranslate2 のデータであるため、
差し替えられてもコード実行に直結するわけではない。深刻度は**低**とする。

#### 対策

1. `HowToRelease.md` と README に「**初回に HuggingFace からモデルを取得する**（モデル名と概算サイズ）」を明記する
2. 取得先を固定したいなら、モデルを配布物へ同梱して `local_files_only=True` で読む
   （`large-v3` は数 GB のため**配布サイズとの引き換え**になる。`small` などを選べるようにする手もある）

### T5. 更新の検証後から起動までの差し替え余地 【低・新規 / S1 の残存】

S1 の是正で、インストーラは専用の一時フォルダへ置き、SHA-256 が一致しなければ
実行経路へ残さないようにした。ただし**検証を終えてから `launch_installer` が
起動するまでの間**は、同一ユーザー権限のプロセスがファイルを差し替えられる。

```python
path = updater.download_installer(url, sha256)   # ここで検証が通る
...                                              # ← この隙間
updater.launch_installer(path)                   # ここで起動する
```

是正前（`%TEMP%\StretheusSetup.exe` 固定）より狙いにくくなった（毎回名前が変わる）が、
`%TEMP%` を列挙すれば見つかるため、**原理的には残っている**。
実際には「更新ダイアログで利用者が OK を押してから数秒」という短い窓である。

#### 対策

`launch_installer` の直前にもう一度ハッシュを確かめるのが最も簡単である。

```python
# 起動の直前にもう一度確かめる (sec3 T5)。
# 検証から起動までの間に差し替えられていないかを見る。
def launch_installer(installer_path, expected_sha256):
    if _file_sha256(installer_path) != _clean_sha256(expected_sha256):
        raise ValueError("更新プログラムが置き換わっています。更新を中止しました。")
    subprocess.Popen([installer_path], close_fds=True)
```

根本的には S2 のコード署名が入れば、OS 側が起動時に検証してくれる。

### T6. 利用者データが平文で残る 【情報】

`%LOCALAPPDATA%\Stretheus\` 配下を実測した。

| ファイル | 保護 |
|---|---|
| `auth.dat` / `twitch_token.json` / `points.json` | **DPAPI**（S4 / N2 の是正済み） |
| `chat_*.json`（Twitch のチャットログ・452KB） | 平文 |
| `vod_*.timeline.json`（編集内容・最大 701KB） | 平文 |
| `project_ids.json`（編集したファイルの絶対パス） | 平文 |

認証情報ではないため**暗号化までは要らない**と考える（サイズが大きく、
毎回の復号は編集操作の体感に響く）。本書は記録として残すにとどめる。
ただし `project_ids.json` は**消え残る**という別の問題があり、そちらは T1 で扱う。

---

## 4. sec2 から持ち越した指摘

内容は sec2 の各項を参照のこと。**いずれも本書の時点で未対応である。**

| # | 指摘 | 深刻度 | 補足 |
|---|---|---|---|
| **S2** | インストール先がユーザー書込可能／未署名 | **高** | 署名の土台（`.iss` の `/DSIGN`・手順書）は整備済み。**証明書の調達だけが残っている**。実測でも `NotSigned` |
| **S3** | `setting.json` の値がそのまま外部プロセスとして実行される | 中 | `_resolve_exe` が絶対パスを無検査で採用する点は変わらず |
| **S5** | OAuth トークンがブラウザ履歴に残る | 中 | `_RELAY_HTML` は現状のまま |
| **S8** | ログ出力先がカレント相対 | 低 | **本点検で実際に踏んだ**。配布 exe を起動した後、ログの出力先を特定できなかった |
| **S9** | 起動時に無同意で GitHub へ通信する | 低 | `auto_update_check` の既定は `true` のまま |
| **N3** | 端末 ID の導出が決定論的でソルトが読める | 低 | サーバー側で担保する話 |
| **N4** | 内部 API のエンドポイント一覧が読める | 情報 | 設計上は正常。サーバー側の認可が前提 |

---

## 5. 配布物の現況（実測）

```
Stretheus/                          837 MB
├─ Stretheus.exe                    12.8 MB   (未署名 / sec1 S2)
├─ _internal/
│   ├─ ffmpeg/                     426 MB    ← 配布物の 51% (sec3 T3)
│   ├─ PySide6/                    125 MB
│   ├─ av.libs/                     65 MB
│   ├─ ctranslate2/                 60 MB
│   ├─ onnxruntime/                 32 MB
│   ├─ src/                         21 MB    (同梱素材・yolox_tiny.onnx・setting.json)
│   ├─ numpy.libs/                  21 MB
│   ├─ PIL/                         13 MB
│   ├─ cryptography/                9.5 MB
│   └─ hf_xet/                      8.0 MB   ← モデル取得用 (sec3 T4)
└─ licenses/                        76 件・不足 0
```

| 項目 | 値 |
|---|---|
| PYZ の総モジュール数 | 1,802（うち自前 136） |
| 開発機の絶対パス | 0 件 |
| 平文で読める秘密情報 | 0 件 |
| 同梱 `setting.json` の `api.base_url` | `""`（空） |
| コード署名 | **なし**（`NotSigned`） |

---

## 6. 守れている点（維持すべき設計）

| # | 点 | 根拠（実測） |
|---|---|---|
| 1 | **配布物に秘密を置かない**（OAuth の `clientId` はサーバーから受け取る） | 配布物から秘密情報 0 件 |
| 2 | **開発機の痕跡を残さない** | `spec` のサニタイズ + `co_filename` の相対化で 0 件 |
| 3 | **接続先の出どころが 1 箇所**（N1 の是正） | `config._DEFAULT_BASE_URL` のみ。切り替えは 2 か所で済む |
| 4 | **認証情報を平文で置かない**（S4 / N2 の是正） | `auth.dat` / `twitch_token.json` / `points.json` が DPAPI blob |
| 5 | **更新の取得物を検証する**（S1 の是正） | URL 許可・SHA-256・専用一時フォルダ・タイムアウト |
| 6 | **検証できない更新は見送る**（fail closed） | 期待ハッシュを得られないリリースへは更新しない |
| 7 | **使っていないコードを配らない**（S6 の是正） | `torch` / `transformers` ほかを除外し 663MB 削減 |
| 8 | テレメトリ送信前にパス・ユーザー名・メール・トークン風文字列をマスク | `log_payloads._MASK_RULES` |
| 9 | ログにトークンを出さない | 実ログの検索で 0 件 |
| 10 | ビルド時の機械的な安全柵（開発機パス・`client_secret`・接続先） | `main_window.spec` がビルドを止める／空へ落とす |

---

## 7. 対応の順序（提案）

| 段 | 対象 | 内容 | 目安 |
|---|---|---|---|
| **1** | **T1** | `[UninstallDelete]` へ認証情報と利用者データを足す。**`.iss` に数行**で済む | 小 |
| **1** | **S3** | 実行ファイルパスの解決を「同梱 or 実在する絶対パス」に限定 | 小 |
| **2** | **S5** | relay を POST 化し、`Host` 検証と `no-store` を追加 | 小 |
| **2** | **T5** | `launch_installer` の直前にハッシュを再確認する | 小 |
| **2** | **T3-1** | 同梱 FFmpeg を**新しいビルドへ更新**する | 小 |
| **3** | **T2** | DPAPI へアプリ固有のエントロピーを与える（**既存ファイルの移行つき**） | 小〜中 |
| **3** | **T3-2** | `essentials_build` への差し替えを検討（**実走行での確認が要る**） | 中 |
| **3** | **T4** | モデルの取得先と概算サイズを文書へ明記する | 小 |
| **4** | **S2** | **コード署名**（証明書の調達）。実現後に更新へ署名検証を重ねる | 大 |
| — | **S8 / S9 / T6 / N3 / N4** | 記録のみ、またはサーバー側で担保 | — |

**段 1 の 2 件はどちらも小さく、次のリリースに入れられる。**
特に T1 は `.iss` へ数行足すだけで、「アンインストールしたのに認証情報が残る」という
利用者にとって分かりやすい問題が消える。

S2 は相変わらず**証明書を買うかどうかの判断だけが残っている**状態で、
これが片付かない限り「インストール済みのファイルを後から差し替える」経路は開いたままである。

---

## 8. 本書の限界

* **サーバー側（`StretheusAPI`）を点検していない。** 本書は「クライアントから何が見えるか」までである
* **動的解析・侵入テスト・マルウェアスキャンは行っていない。** 静的な読み取りと、
  ローカル保存データ・配布 exe の起動の実測のみである
* **依存パッケージの CVE 照合をしていない。** T3 は同梱 FFmpeg の**版が古い事実**を挙げたもので、
  特定の脆弱性の有無を示すものではない
* **zip 化・インストーラ生成・クリーン PC での挙動確認をしていない。** 対象は `src/dist/Stretheus/` である
* **T1 のアンインストール挙動は `.iss` の記述から判断した。** 実際にインストール →
  アンインストールして残存を確かめたわけではない
* **本書は 2026-11-07 時点のスナップショットである。** 件数・サイズ・パスをそのまま後の版へ引用してはならない
* 各対策のコード片は**方針を示すためのもの**で、そのままの動作確認はしていない
