# リリース手順書 (HowToRelease)

`src/gui/main_window.py` をメイン画面とする GUI アプリケーションを、**Web からダウンロードして実行する**
配布物（Windows 向け実行ファイル）としてリリースするための手順書。

> 対象 OS は Windows 11（開発環境）を基準とする。Python は CLAUDE.md 準拠で **Python 3.14**。
> パッケージングには既存採用の **PyInstaller**（`src/main.spec` 実績あり）を用い、新規依存は増やさない。

> **【重要・CUDA 不使用方針（`docs/request/resolve8.md`）】**
> 音声認識は **CPU 実行に統一**し、**CUDA ランタイム（`nvidia-*-cu12`）の同梱は廃止**した
> （配布ペイロードを約1.93GB 削減し、GitHub Releases の 2GiB 上限へ収めやすくするため）。
> 本書中の **CUDA / GPU 同梱に関する記述（§2.2・§3.5・§4 の CUDA 収集・§6・§9 の該当行）は
> 歴史的経緯として残すが、現行ビルドには適用しない**。GPU 配布は行わない。

---

## 1. 配布物の全体像

ユーザーは「ダウンロード → 解凍 → exe をダブルクリック」で起動できることを目標とする。
GUI のメイン画面は `src/gui/main_window.py` の `MainWindow`（入力動画選択・実行・進捗・字幕編集の橋渡し）。

最終的に配布パッケージへ**同梱が必要なもの**は次のとおり。

| 区分 | 内容 | 理由・注意 |
|---|---|---|
| 実行ファイル | `Stretheus.exe`（PyInstaller 生成） | GUI ランチャ（`main_window.py`）を起点に起動 |
| Python ランタイム/依存 | PyInstaller が同梱（PySide6 等） | ユーザー環境に Python 不要にする |
| 外部バイナリ | `ffmpeg.exe` / `ffprobe.exe` | **PATH 非依存**にするため必ず同梱（§3.2） |
| 設定ファイル | `setting.json` | 初期設定。書込先の扱いに注意（§3.3） |
| 音声認識モデル | faster-whisper モデル（任意） | 同梱 or 初回起動時にダウンロード（§3.4） |
| 付属物 | `README` / ライセンス / チェックサム | Web 配布の信頼性確保（§7） |

---

## 2. 依存関係の確認と固定

### 2.1 ランタイム依存

| 依存 | 用途 | 取得 |
|---|---|---|
| PySide6 | GUI（`main_window.py` / `settings_window.py` / `subtitle_editor_dialog.py`） | pip |
| faster-whisper | 音声認識（`subtitle_generator.py` で**遅延 import**） | pip（任意。未導入でもモジュール読込は可能） |
| budoux | テロップ文節改行（`subtitle_generator.py` で**遅延 import** / request11） | pip（任意。未導入時は文字数改行へ自動フォールバック。モデルJSONは spec で同梱） |
| CUDA ランタイム（cuBLAS/cuDNN/cudart/nvrtc） | **GPU 実行時**に ctranslate2 が動的ロード | pip wheel（`nvidia-*-cu12`。GPU 同梱配布時のみ。§3.5） |
| FFmpeg / ffprobe | 無音カット・焼き込み・結合・尺取得 | 外部バイナリ（pip 不可） |
| PyInstaller | パッケージング（ビルド時のみ） | pip |

### 2.2 依存の固定（推奨）

リリースの再現性を担保するため、ビルドに用いた版を `requirements.txt`（無ければ新規作成）へ固定する。

```text
PySide6==<ビルドで使用した版>
faster-whisper==<ビルドで使用した版>
budoux==<ビルドで使用した版>   # テロップ文節改行 (request11)。spec で collect_data_files('budoux') を同梱すること
# GPU 同梱配布を行う場合のみ (CUDA ランタイムを wheel から同梱・§3.5/§4)
# ctranslate2 の要求版に合わせる (例: ct2 4.7.1 → CUDA 12 / cuDNN 9)
nvidia-cublas-cu12==<版>
nvidia-cuda-runtime-cu12==<版>
nvidia-cudnn-cu12==<版>
nvidia-cuda-nvrtc-cu12==<版>
# ビルド専用
pyinstaller==<ビルドで使用した版>
```

```powershell
# クリーンな仮想環境でビルドするのが望ましい
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> FFmpeg/ffprobe は pip では入らない。公式配布（または同等のビルド）から `ffmpeg.exe` / `ffprobe.exe` を入手する。
>
> **配置先（必須）**: 入手した `ffmpeg.exe` / `ffprobe.exe` を `src/ffmpeg/` に置く（git 管理外）。`main_window.spec` が
> この2ファイルを `datas` として同梱し、実行時は `_internal/ffmpeg/` に展開される。未配置だとビルドは通るが
> 同梱が欠落し、FFmpeg 未導入PCで「FFmpeg 実行ファイルが見つかりません」となる（error 20260708）。

---

## 3. パッケージング上の重要事項（事前設計）

ビルド前に必ず押さえるべき、**Web ダウンロード配布特有**の論点。

### 3.1 GUI エントリポイント

`src/gui/main_window.py` は `main()`（`QApplication` 起動 → `MainWindow` 表示）を持ち、
パッケージ実行/単独実行の両対応（先頭の `sys.path` 補完ブロック）を備える。PyInstaller の
スクリプトに**この `main_window.py` を直接指定**できる（既存 `main.spec` が `main.py` を指定するのと同方針）。

GUI のため **ウィンドウモード（`console=False`）** でビルドする（コンソール窓を出さない）。

### 3.2 FFmpeg/ffprobe を PATH 非依存にする（必須）

**【実装済み・error 20260708】** 以下は対応済み。`ffmpeg_runner._resolve_exe()` が実行ファイルを PATH 非依存で解決する
（絶対パス → 凍結配布物の同梱物(exe/`_internal` 隣接) → PATH → 設定値、の順）。既定値も同梱相対パスを指す。

- `setting.json` / `DEFAULT_SETTINGS` の既定値は同梱パス:
  ```json
  "ffmpeg": {
      "executable": "ffmpeg/ffmpeg.exe",
      "ffprobe_executable": "ffmpeg/ffprobe.exe"
  }
  ```
- `main_window.spec` の `datas` が `src/ffmpeg/` の2ファイルを同梱し、実行時 `_internal/ffmpeg/` に展開する。
- `_resolve_exe` は `sys._MEIPASS`(=`_internal`) と exe 直下の双方を探索するため、`datas` 同梱・手動配置いずれのレイアウトでも解決できる。
- 非凍結(開発)実行では `shutil.which("ffmpeg.exe")` により従来どおり PATH の FFmpeg を解決する（後方互換）。

> 教訓: 旧構成は exe を同梱しつつ `setting.json` の値が裸の `"ffmpeg"` のままで PATH 依存だったため、
> FFmpeg 未導入PCで起動失敗した。**同梱と設定値の両方**を揃えること。

### 3.3 setting.json・ログの書込先（凍結時の注意）

`settings_window.py` は設定ファイルの場所を `__file__` 基準で解決する
（`SETTINGS_DIR = dirname(abspath(__file__))` → `setting.json`）。**PyInstaller で凍結すると `__file__`
の位置が変わる**ため、配布形態により以下の注意がある。

- **onefile（単一 exe）**: 実行時に一時展開先（`_MEIxxxx`）へ `__file__` が向く。ここは**読み取り専用かつ
  実行終了で消える**ため、`save_settings()` の保存が**失われる/失敗**する。→ GUI で設定変更を保存できない。
- **onedir（フォルダ配布）＝推奨**: exe と同階層に依存一式が展開される。設定ファイルを exe 隣／書込可能な
  場所に置く運用にしやすく、トラブルが少ない。

**本手順では onedir を推奨**する。さらに堅牢にするなら、設定・ログを書込可能な領域
（例: `%APPDATA%\Stretheus\`）へ寄せる改修が望ましい（別タスク）。ログも `logger.get_logger(log_dir="logs")`
が**カレントディレクトリ相対**で出力するため、ショートカット起動時の作業フォルダによっては想定外の場所に出る。
配布時は exe のあるフォルダを作業ディレクトリにするショートカットを用意するか、`setting.json` の
`logging.log_dir` を運用に合う値にする。

> 既知の注意（§9）: `settings_window.py` の「開始」ボタンは `MAIN_SCRIPT`（`src/main.py`）を
> `sys.executable` で起動する実装のため、凍結環境ではスクリプトが存在せず機能しない。
> GUI のメイン処理は `main_window.py` のワーカースレッド実行（プロセス内）で完結するため、
> 通常利用には影響しないが、設定画面の当該ボタンは凍結版では無効になる点を把握しておく。

### 3.4 音声認識モデル（faster-whisper）

`subtitle_cfg.whisper_model`（既定 `large-v3`）は初回利用時に HuggingFace から自動ダウンロードされ、
ユーザーのキャッシュ（`%USERPROFILE%\.cache\huggingface`）に保存される。**初回はネット接続と相応の
ダウンロード時間/容量**が必要。配布方針を選ぶ。

- **既定（推奨・配布物軽量）**: モデルは初回起動時にダウンロード。README に「初回はネット必須・時間がかかる」旨を明記。
- **オフライン配布**: モデルを同梱し、`setting.json` で軽量モデル（例 `small`/`medium`）やローカルパスを指す。
  配布サイズが大きくなるためトレードオフを判断する。

### 3.5 GPU(CUDA) 実行と CUDA ランタイムの同梱（重要）

`subtitle.whisper_device` は既定 `"auto"`。NVIDIA GPU 搭載PCでは `cuda` に解決され、音声認識を GPU 実行する。
ただし ctranslate2（faster-whisper のバックエンド）は **CUDA ランタイム DLL を実行時に動的ロード**するため、
**配布物に CUDA ランタイムを同梱しないと、GPU 搭載PCで `cublas64_12.dll is not found` 等で失敗する**
（詳細・経緯: `docs/error/20260628/resolve.md`）。

配布方針を選ぶ。

- **GPU 同梱配布（A案・本手順の既定）**: CUDA ランタイム一式を wheel から取得して同梱する（§4 の spec が自動収集）。
  - 必要 wheel（ctranslate2 の要求版に整合。**ct2 4.7.1 → CUDA 12 / cuDNN 9**）:
    `nvidia-cublas-cu12` / `nvidia-cuda-runtime-cu12` / `nvidia-cudnn-cu12` / `nvidia-cuda-nvrtc-cu12`
  - 同梱量の目安: **15 DLL / 約 1.93GB**（`nvblas`・`*.alt.dll` は ct2 未使用のため除外）。配布物総量は **約 3.9GB**。
- **CPU のみ配布（軽量）**: 上記 wheel を導入しない。§4 の spec は未導入時 CUDA 収集を自動スキップする。
  `whisper_device` を `"cpu"` にすると GPU 判定自体を行わない（CPU は `large-v3` で処理が重い点に注意）。

> **同梱しても回避できない前提**（いずれも実行時に**自動で CPU フォールバック**＝対策B が受け止める）:
> - **NVIDIA ドライバ**: ターゲットPCのドライバが CUDA 12 対応（Windows R525 以降）であること。ドライバ本体（`nvcuda.dll`）は**再配布不可で同梱不可**。
> - **VRAM/世代**: `large-v3`(float16) は約 3GB+ の VRAM が必要。

#### CPU フォールバック（対策B）

`subtitle_generator.py` の `extract()` は、GPU 実行が**構築時・デコード時のいずれで失敗しても** CPU で 1 回だけ再試行する。
これにより、CUDA 未整備・古いドライバ・VRAM 不足のPCでも**クラッシュせず CPU で完走**する（`docs/error/20260628/resolve.md` 対策B）。

---

## 4. ビルド用 spec ファイル（GUI 版）

既存 `src/main.spec`（CLI・`console=True`）とは別に、GUI 版 spec を用意する。
`src/main_window.spec` として新規作成する例（onedir・windowed）。

実際の `src/main_window.spec`（**CUDA 不使用・CPU 版** / resolve8.md）は以下。

```python
# -*- mode: python ; coding: utf-8 -*-
# GUI ランチャ(main_window.py)を起点にした windowed/onedir ビルド設定
# (docs/HowToRelease.md §4 準拠)
# 注: アイコン(gui/app.ico)を EXE へ埋め込み、実行時用に datas でも同梱する(下記参照)。
#     setting.json の同梱先は settings_window.py が __file__ 基準で参照する
#     'src/settings' に合わせる(実行時に初期設定を解決できるようにするため)。
#
# CUDA 不使用方針 (docs/request/resolve8.md): 音声認識は CPU 実行に統一したため、
# CUDA ランタイム DLL (nvidia-*-cu12) の同梱は廃止した。これにより配布ペイロードを
# 大幅に削減し、GitHub Releases の 1ファイル 2GiB 上限に収めやすくする。

a = Analysis(
    ['gui/main_window.py'],          # GUI エントリポイント
    pathex=['..'],                   # 'from src.xxx' 解決のためリポジトリルートを追加
    binaries=[],                     # CUDA 同梱は廃止 (CPU 実行のため不要 / resolve8.md)
    datas=[
        ('settings/setting.json', 'src/settings'),   # 初期設定を実行時参照先へ同梱
    ],
    hiddenimports=[
        'faster_whisper',            # 遅延 import のため明示
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,           # onedir: バイナリは COLLECT 側へ
    name='Stretheus',                 # 生成される実行ファイル名
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # AV 誤検知を避けるため UPX は無効
    console=False,                   # GUI: コンソール窓を出さない
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Stretheus',                 # 出力フォルダ名 dist/Stretheus/
)
```

ポイント:
- `console=False`：GUI のためコンソール非表示。
- onedir（`exclude_binaries=True` + `COLLECT`）：§3.3 の理由で onefile より安定。
- `pathex=['..']`：エントリ `gui/main_window.py` 内の `from src.xxx` を解決するためリポジトリルートを追加（§3.1）。
- `datas` の `setting.json` 同梱先は **`src/settings`**：`settings_window.py` が `__file__` 基準で `_internal/src/settings/setting.json` を参照するため（`settings` 直下では実行時に見つからない）。FFmpeg は §5.3 で exe 隣へ手動配置。
- `binaries=[]`：**CUDA ランタイムの同梱は廃止（CUDA 不使用・CPU 版 / resolve8.md）**。`nvidia-*-cu12` は導入・同梱しない。音声認識は CPU で実行する。
- `upx=False`：圧縮は SmartScreen/AV 誤検知を招きやすいため無効（§7）。
- `icon='gui/app.ico'` を `EXE(...)` に指定し exe へアイコンを埋め込む。実行時のウィンドウ/タスクバー用に
  `datas` へ `('gui/app.ico', 'src/gui')` を同梱し、`main_window._resolve_app_icon_path` が `sys._MEIPASS` 基準で解決して
  `QApplication.setWindowIcon` する。アイコン素材 `src/gui/app.ico` はマルチ解像度(16/32/48/64/128/256)を内包した単一 `.ico`。
- `hiddenimports` の `faster_whisper` は遅延 import のため明示。

---

## 5. ビルド手順

### 5.1 クリーンビルド

```powershell
# リポジトリ直下で実施。仮想環境を有効化済みとする
# spec を src/ に置いた場合は src を作業フォルダにして実行
Set-Location src
pyinstaller main_window.spec --clean --noconfirm
```

成功すると `src/dist/Stretheus/` に `Stretheus.exe` と依存一式が生成される
（`src/build/` は中間生成物。配布対象外）。

### 5.2 動作前提の整備（FFmpeg 同梱）

`ffmpeg.exe` / `ffprobe.exe` を配布フォルダへ配置し、同梱 `setting.json` のパスを合わせる（§3.2）。

```powershell
# 例: dist/Stretheus/ffmpeg/ に配置
New-Item -ItemType Directory -Force "dist/Stretheus/ffmpeg" | Out-Null
Copy-Item "<入手した>/ffmpeg.exe","<入手した>/ffprobe.exe" "dist/Stretheus/ffmpeg/"
```

### 5.3 配布フォルダのレイアウト（例）

```
Stretheus/                      ← これを zip 化して配布
├─ Stretheus.exe                ← メイン画面(main_window.py)起点の実行ファイル
├─ _internal/ または依存 dll 群 ← PyInstaller 生成(変更不可)
├─ settings/
│   └─ setting.json            ← 初期設定(ffmpeg パス等を配布構成に合わせる)
├─ ffmpeg/
│   ├─ ffmpeg.exe
│   └─ ffprobe.exe
├─ README.txt                  ← 動作要件・初回DL・使い方・連絡先
└─ LICENSE / サードパーティライセンス表記
```

---

## 6. リリース前の動作確認チェックリスト

**ビルド環境ではなく、クリーンな別 PC（できれば Python 未導入）** で確認するのが理想。

- [ ] `Stretheus.exe` をダブルクリックでメイン画面（`MainWindow`）が表示される。
- [ ] 「参照...」で入力動画を選択できる。
- [ ] 「設定...」ボタンで設定画面（`SettingsWindow`）が開く。字幕タブのカラーピッカーが動作する。
- [ ] 「実行」でパイプラインが進捗バー付きで進む（無音カット→テロップ→OP/ED 結合→出力）。
- [ ] 字幕編集画面（レビュー）が出て、編集・字幕決定・キャンセルが想定どおり動く。
- [ ] FFmpeg/ffprobe が同梱パスで解決され、PATH 非依存で動作する。
- [ ] 出力ファイルが生成される。ログが想定の場所に出る（§3.3）。
- [ ] 音声認識が初回 DL（または同梱モデル）で機能する。
- [ ] GPU 搭載PCで CUDA 実行が**同梱ランタイム**で動作する（§3.5）。
- [ ] CUDA 未整備／古いドライバ／非 NVIDIA のPCで、音声認識が**CPU へ自動フォールバックして完走**する（対策B・§3.5）。
- [ ] 設定変更→保存→再起動で保持される（onedir 前提。§3.3 の制約を確認）。

---

## 7. Web 配布特有の注意（信頼性・安全性）

ダウンロード実行型は OS/ブラウザ/AV の保護機構に引っかかりやすい。次を考慮する。

### 7.1 Windows SmartScreen / Defender

- 署名なしの新規 exe は **SmartScreen 警告**（「WindowsによってPCが保護されました」）が出る。
  README に回避手順（詳細情報 → 実行）を記載するか、**コード署名**で軽減する。
- PyInstaller 製 exe は **AV 誤検知**が起きやすい。`upx=False`（§4）、不要な hiddenimports を避ける、
  署名する、で低減する。誤検知が出たら各ベンダーへ誤検知報告を行う。

### 7.2 コード署名（推奨）

- **Authenticode 証明書**（OV/EV）で `Stretheus.exe` と主要 dll に署名すると SmartScreen 評価が改善する
  （EV はより強力）。
  ```powershell
  signtool sign /fd SHA256 /tr http://timestamp.example /td SHA256 /a "dist/Stretheus/Stretheus.exe"
  ```
- 署名は外部公開を伴う作業のため、証明書の管理・運用ポリシーに従って実施する。

### 7.3 配布パッケージとチェックサム

- 配布は **インストーラ方式**（`StretheusSetup.exe`）を基本とする（`docs/request/resolve8.md`）。
  ユーザーは小さいインストーラーをDL→実行し、インストーラーが本体ペイロードを GitHub Releases から取得する。
  zip 直配布は後方互換の代替として残す。
- 改ざん検知用に **SHA-256** を公開する。
  ```powershell
  Compress-Archive -Path "dist/Stretheus/*" -DestinationPath "Stretheus-v1.0.0.zip"
  Get-FileHash "Stretheus-v1.0.0.zip" -Algorithm SHA256   # ← インストーラの PayloadSHA256 に設定
  ```
- ダウンロードページにバージョン・SHA-256・動作要件・変更点（リリースノート）を明記する。

> **インストーラ配布の詳細手順は `installer/README.md` を参照**（`.iss` の #define 設定、
> payload の zip 化/分割、`gh release create/upload` による GitHub Releases 公開、検証）。
> 配布チャネルは `evolutysystems/homepage` の Releases、ダウンロード導線は同サイト
> `src/pages/dev/Dev.tsx`（インストーラー URL へ差し替え）。

---

## 8. バージョニングとリリースノート

- セマンティックバージョニング（`vMAJOR.MINOR.PATCH`）を推奨。zip 名・exe バージョン情報・配布ページで一致させる。
- リリースノートに記載する項目: 新機能/修正、動作要件（OS/FFmpeg/ネット要否）、既知の不具合、SHA-256。
- 配布ファイル名例: `Stretheus-v1.0.0-win64.zip`。

---

## 9. 既知の注意点・トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| 設定を保存しても次回反映されない | onefile で `setting.json` が一時展開先に書かれ消える（§3.3） | onedir でビルドする／設定を書込可能領域へ寄せる改修 |
| FFmpeg 関連でエラー | PATH に FFmpeg が無い | `ffmpeg.exe`/`ffprobe.exe` を同梱し `setting.json` のパスを設定（§3.2） |
| 初回の字幕生成が遅い/失敗 | Whisper モデル未取得・ネット不通 | ネット接続を確認、または軽量/同梱モデルへ変更（§3.4） |
| 字幕生成が遅い | **CPU 実行**（CUDA 不使用方針 / resolve8.md）で `large-v3` を処理 | 仕様。必要に応じ軽量モデル（`medium`/`small`）へ変更（§3.4）。GPU(CUDA) 配布は廃止 |
| SmartScreen 警告 | 署名なし新規 exe | コード署名、または README に実行手順記載（§7） |
| AV に削除される | PyInstaller 製 exe の誤検知 | `upx=False`・署名・誤検知報告（§7.1） |
| 設定画面の「開始」が動かない | 凍結版に `src/main.py` が存在しない（§3.3 注記） | メイン画面(`main_window.py`)の「実行」を使う／当該ボタンの凍結対応は別途改修 |
| ログが見つからない | `log_dir` がカレント相対（§3.3） | exe フォルダを作業ディレクトリにするショートカット、または `logging.log_dir` を調整 |

---

## 10. リリース作業フロー（まとめ）

> CUDA は不使用（CPU 版）。配布は**インストーラ方式**（`installer/README.md` が正典手順）。

1. クリーン仮想環境を作成し依存をインストール（§2）。**`nvidia-*-cu12` は導入しない**（CPU 版 / resolve8.md）。
2. GUI 版 spec（`main_window.spec`）でビルド（§4・§5.1。`binaries=[]`＝CUDA 非同梱）。
3. FFmpeg を同梱し `setting.json` のパスを配布構成へ合わせる（§5.2・§3.2）。
4. 本体を zip 化し SHA-256 を取得（§7.3）。2GiB 超なら分割（`installer/README.md §1.1`）。
5. `installer/AutoEdit.iss` の #define（バージョン／`PayloadParts`／`PayloadSHA256`）を設定し ISCC でコンパイル（`installer/README.md §2-3`）。
6. `gh release create/upload` でインストーラー＋payload を GitHub Releases へ公開（`installer/README.md §4`）。
7. homepage `Dev.tsx` のダウンロード URL をインストーラーへ差し替え（別リポジトリ・`installer/README.md §5`）。
8. クリーン PC で動作確認（§6・`installer/README.md §6`）。
9. （推奨）インストーラー・本体のコード署名（§7.2）。
