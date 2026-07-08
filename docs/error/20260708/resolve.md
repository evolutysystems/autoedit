# 「FFmpeg 実行ファイルが見つかりません: ffmpeg」で処理失敗する不具合 修正設計書

対象事象: 別PCでアプリを削除→再インストール後、編集実行時に「処理に失敗しました。FFmpeg 実行ファイルが見つかりません:ffmpeg」で失敗し処理が進まない。
対象ソース: `src/modules/ffmpeg_runner.py`（主因: 実行ファイル解決が PATH 依存）/ `src/settings/setting.json`・`src/settings/settings_window.py`（既定値が同梱物を指していない）/ `src/main_window.spec`（同梱漏れ: 再ビルドで FFmpeg が欠落）
本書は `docs/CLAUDE.md` の方針（既存破壊なし・不要ライブラリなし・値は `setting.json` 管理）に準拠する。

---

## 1. 事象

配布物（onedir）を **FFmpeg 未導入のPC** にインストールして編集を実行すると、パイプライン冒頭（`pipeline_runner.py:41` の `ensure_available`）で以下が送出され、処理が始まらない。

```
FFmpeg 実行ファイルが見つかりません: ffmpeg
```

開発PC・最初のテストPCでは再現しなかったが、これは **たまたま FFmpeg がシステム PATH に入っていた** ため。削除→再インストールした別PCは PATH に FFmpeg が無く失敗した。

---

## 2. 原因調査

### 2-1. 配布物には FFmpeg が同梱されている

現行 dist は次の構成で、`ffmpeg.exe` / `ffprobe.exe` を exe 直下に同梱している。

```
dist/AutoEdit/
├── AutoEdit.exe
├── ffmpeg/ffmpeg.exe      ← 同梱済み
├── ffmpeg/ffprobe.exe     ← 同梱済み
└── _internal/src/settings/setting.json
```

### 2-2. しかし設定が同梱物を指していない（主因）

同梱 `setting.json`（`_internal/src/settings/setting.json:80`）と既定値（`settings_window.py:185`／リポジトリ `setting.json:80`）は **裸の名前** のまま。

```json
"ffmpeg": {
    "executable": "ffmpeg",          ← "ffmpeg/ffmpeg.exe" ではない
    "ffprobe_executable": "ffprobe",
```

### 2-3. 検出処理が PATH のみを探す

`get_ffmpeg_exe()` は設定値をそのまま返し（`ffmpeg_runner.py:16-22`）、`ensure_available()` が `shutil.which()` で判定する（`ffmpeg_runner.py:44-47`）。

```python
def ensure_available(ffmpeg_settings):
    for exe in (get_ffmpeg_exe(ffmpeg_settings), get_ffprobe_exe(ffmpeg_settings)):
        if shutil.which(exe) is None:
            raise FFmpegError(f"FFmpeg 実行ファイルが見つかりません: {exe}")
```

`shutil.which("ffmpeg")` は **システム PATH（と CWD）しか探さず**、同梱の `ffmpeg/` フォルダを見ない。よって PATH に FFmpeg が無いPCでは `None` → 例外。**同梱した exe は設定が参照していないため一切使われない**。

### 2-4. 位置づけ

`docs/HowToRelease.md` §3.2「FFmpeg/ffprobe を PATH 非依存にする（必須）」で警告済みの事象。exe の同梱まではやったが「同梱 `setting.json` の値を同梱パスへ書き換える」対応が抜けた半端な状態だった。

### 2-5. 副次問題：`main_window.spec` が FFmpeg を同梱していない

`src/main_window.spec` は `binaries=[]`、`datas` に FFmpeg を含まない（`main_window.spec:17-20`）。現行 dist の `ffmpeg/` は **手動配置** と判断される。再ビルドすると FFmpeg が欠落し、恒久的に同じ事故が起きうる。

---

## 3. 修正方針

「設定・解決の PATH 非依存化（根治）」＋「spec への同梱（恒久化）」の二層。

### 対策A（主・根治）：実行ファイル解決を PATH 非依存にする

`ffmpeg_runner` の実行ファイル解決を、**凍結配布物では exe 隣接の同梱物を最優先で絶対パス解決** する方式へ改める。CWD・PATH に一切依存しない。

解決順（`_resolve_exe`）:
1. **絶対パス** … そのまま採用。
2. **凍結時（`sys.frozen`）の同梱物** … 候補基準ディレクトリ `sys._MEIPASS`（onedir では `_internal`）と `dirname(sys.executable)`（app 直下）の双方に対し、設定値（相対パス）を結合し、実在すれば絶対パスで採用。
   - 2つの基準を見るのは、`datas` 同梱（`_internal/ffmpeg/`）と手動配置（app 直下 `ffmpeg/`）の両レイアウトを吸収するため。既存 dist も新ビルドも動く。
3. **PATH フォールバック** … `shutil.which(値)`／`shutil.which(basename(値))` を試す（開発実行や PATH 導入環境向け）。
4. いずれも不発なら **設定値をそのまま返す**（`ensure_available` が明確なエラーを出す）。

`getattr(sys, "frozen", False)` による凍結判定は既存 `updater.py:71` と同方針。

### 対策B（設定）：同梱パスを既定値にする

`executable` / `ffprobe_executable` の既定値を同梱相対パスへ変更する。

```json
"executable": "ffmpeg/ffmpeg.exe",
"ffprobe_executable": "ffmpeg/ffprobe.exe",
```

- 対象: `src/settings/settings_window.py`（`DEFAULT_SETTINGS`）、`src/settings/setting.json`、既存 dist の `_internal/src/settings/setting.json`。
- 開発（非凍結）実行では対策A手順3の `which(basename)` により従来どおり PATH の `ffmpeg.exe` を解決するため後方互換。
- 更新インストール時は既存ユーザー設定が維持される（`AutoEdit.iss` §8.1）が、旧値 `"ffmpeg"` でも対策Aの手順3で PATH 解決を試みるため、PATH に FFmpeg があれば従来通り動作（新規/クリーン導入が根治対象）。

### 対策C（恒久化）：spec に FFmpeg を同梱する

再ビルドで欠落しないよう `main_window.spec` の `datas` に FFmpeg を追加する。ソース参照用に **リポジトリ内の正規配置** `src/ffmpeg/` を設ける（現行 dist からコピー）。

```python
datas=[
    ('settings/setting.json', 'src/settings'),
    ('ffmpeg/ffmpeg.exe', 'ffmpeg'),      # 同梱: 実行時に _internal/ffmpeg/ へ配置
    ('ffmpeg/ffprobe.exe', 'ffmpeg'),
] + collect_data_files('budoux'),
```

- onedir では `datas` は `_internal/` 配下に展開されるため、実行時パスは `_internal/ffmpeg/ffmpeg.exe`。対策A手順2の `sys._MEIPASS` 基準で解決される。
- 依存 DLL 走査を避けるため `binaries` ではなく `datas`（純コピー）を用いる（静的ビルドの ffmpeg は追加 DLL 不要）。

---

## 4. 変更対象

| ファイル | 変更概要 | 区分 |
|---|---|---|
| `src/modules/ffmpeg_runner.py` | `_resolve_exe` を追加し `get_ffmpeg_exe`/`get_ffprobe_exe` を PATH 非依存解決に変更。`ensure_available` を絶対パス実在も許容するよう補強 | 主A |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS.ffmpeg` の実行ファイル既定値を同梱相対パスへ | B |
| `src/settings/setting.json` | 同上（実値） | B |
| `src/dist/AutoEdit/_internal/src/settings/setting.json` | 既存 dist の同梱設定を同梱相対パスへ（再パッケージで即効） | B |
| `src/ffmpeg/ffmpeg.exe`・`ffprobe.exe` | ビルド用の正規同梱ソースを配置（dist からコピー） | C |
| `src/main_window.spec` | `datas` に FFmpeg を追加 | C |
| `docs/HowToRelease.md` | §3.2 の記述を実装済み状態へ追補（任意） | 文書 |

新規設定キーの追加は無し（既存キーの値変更のみ）で後方互換を保つ。

---

## 5. エラー処理・ログ方針

- 解決不能時は従来どおり `FFmpegError("FFmpeg 実行ファイルが見つかりません: …")` を送出（挙動不変）。メッセージには解決を試みた **設定値** を出す。
- 挙動を変えるのは「同梱物を正しく見つける」点のみで、既存の無音カット・字幕・結合の各処理へ副作用は無い（getter が絶対パスを返すだけ）。

---

## 6. 検証方針

1. **クリーン環境再現**：PATH に FFmpeg が無い状態を再現（一時的に PATH から除外）し、対策前は本エラー、対策後は解決されることを確認。
2. **凍結相当の単体確認**：`sys.frozen`/`sys._MEIPASS` を擬似設定し、`_resolve_exe("ffmpeg/ffmpeg.exe")` が同梱の絶対パスを返すことを単体で確認。
3. **開発実行の後方互換**：非凍結・PATH に ffmpeg 有りで従来通り解決・完走することを確認。
4. **既存 dist 動作**：`_internal/src/settings/setting.json` を新値へ更新した既存 dist を PATH 無し環境で起動し、app 直下 `ffmpeg/` を解決して完走することを確認。
5. **再ビルド同梱**：`main_window.spec` でビルドし直し、`_internal/ffmpeg/` に FFmpeg が入り、解決・完走することを確認。
6. **回帰**：無音カット→字幕→OP/ED 結合→出力の一連が従来どおり動作することを確認。
