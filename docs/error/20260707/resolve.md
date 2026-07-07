# インストール後の起動時に setting.json の文字コード不整合で失敗する不具合 修正設計書

対象エラー: `docs/error/20260707/error.md`
対象ソース: `installer/AutoEdit.iss`（**主因**: `PatchOutputDir` が setting.json を ANSI(CP932) で保存し UTF-8 を破壊）/ `src/settings/settings_window.py`（**副因**: `load_settings` が `UnicodeDecodeError` を捕捉せず起動ごと即クラッシュ）
本書はレビュー用の**修正設計書**であり、実装は承認後に行う（`docs/CLAUDE.md` の方針に準拠）。

> 本不具合は **v1.0.0〜v1.0.2 の全リリースに存在**する（`PatchOutputDir` の実装が同一のため）。修正は次リリース **v1.0.3** で配布する。

---

## 1. 事象

Web からダウンロードしたインストーラ（`AutoEditSetup.exe`）で**新規インストール後、初回起動が必ず失敗**し、アプリが起動できない。

```
Failed to execute script 'main_window' due to unhandled exception:
'utf-8' codec can't decode byte 0x83 in position 594: invalid start byte

Traceback (most recent call last):
  File "main_window.py", line 490, in <module>
  File "main_window.py", line 482, in main
  File "main_window.py", line 256, in __init__
  File "src\settings\settings_window.py", line 219, in load_settings
  File "<frozen codecs>", line 322, in decode
UnicodeDecodeError: 'utf-8' codec can't decode byte 0x83 in position 594: invalid start byte
```

- クラッシュ点は `settings_window.py:219`（`content = f.read()`）で、`open(SETTINGS_FILE, encoding="utf-8")` の読み込み時に **`setting.json` が UTF-8 として不正**なため `UnicodeDecodeError` が送出される。
- `MainWindow.__init__`（L256）→ `load_settings()` は起動の最初期に呼ばれるため、例外が未捕捉で伝播し**アプリ全体が起動不能**になる。

---

## 2. 原因調査

### 2-1. `byte 0x83` の意味
`0x83` は **CP932(Shift_JIS) の全角文字リードバイト**（範囲 0x81–0x9F）。UTF-8 では継続バイトであり単独で文字を開始できないため「invalid start byte」となる。すなわち **`setting.json` の日本語部分が CP932 で書かれている**ことを示す。position 594 付近は `subtitle` セクションの日本語値（例: `"comment_label": "コメント："`）に対応する。

### 2-2. 主因: インストーラ `PatchOutputDir` が ANSI(CP932) で保存している
`installer/AutoEdit.iss` の `PatchOutputDir`（L195-249）は、新規インストール時に `output_directory` をユーザー選択値へ書き換えるが、その入出力に **ANSI 版の関数**を使用している。

```pascal
if not LoadStringsFromFile(SettingsFile, lines) then ...   { L208: ANSI 読み込み }
...
Result := SaveStringsToFile(SettingsFile, lines, False)     { L246: ANSI 保存 }
```

- 同梱される `setting.json` は **UTF-8**（日本語 `コメント：` 等を含む）。
- `LoadStringsFromFile` は **システム ANSI コードページ(日本語Windowsでは CP932)** で読み込むため、UTF-8 の日本語バイト列を**誤ってCP932の全角文字に再解釈**する（文字化け）。
- `SaveStringsToFile` は文字列配列を **ANSI(CP932) で書き戻す**。結果、`setting.json` の日本語部分が **CP932 バイト(0x83..)** になり、**UTF-8 として不正なファイル**が生成される。

> `output_directory` 行自体は ASCII のため置換は成功しているが、**保存工程でファイル全体が ANSI 化**され、日本語を含む他行が破壊される。これが根本原因。

### 2-3. 副因: `load_settings` が `UnicodeDecodeError` を捕捉していない
`settings_window.py` の `load_settings`（L214-224）は例外捕捉が `(json.JSONDecodeError, OSError)` に限定されている。

```python
with open(SETTINGS_FILE, "r", encoding="utf-8") as f:   # L218
    content = f.read().strip()                            # L219 ← ここで UnicodeDecodeError
...
except (json.JSONDecodeError, OSError):                   # UnicodeDecodeError は捕まらない
    return DEFAULT_SETTINGS.copy()
```

`UnicodeDecodeError` は `ValueError` のサブクラスであり、`OSError`/`JSONDecodeError` には該当しない。そのため**壊れた/別エンコードの `setting.json` でフォールバックが働かず、起動ごとにハードクラッシュ**する。堅牢性の欠陥であり、主因を直しても防御として修正すべき。

---

## 3. 修正方針

2層で修正する。

1. **【根本】インストーラを UTF-8 入出力に統一**（`PatchOutputDir`）。同梱 `setting.json` を UTF-8 のまま読み書きし、パッチ後もファイルが**有効な UTF-8** を保つ。
2. **【防御】アプリの読み込みを堅牢化**（`load_settings`）。UTF-8(BOM許容)で読み、失敗時は CP932 で回復を試み、それも不可なら既定へフォールバックして**起動不能を防ぐ**。既存の破損インストールも救済する。

---

## 4. 変更内容

### 4-1.【根本】`installer/AutoEdit.iss` `PatchOutputDir`
ANSI 版の配列関数（`LoadStringsFromFile`/`SaveStringsToFile`＝システムコードページ変換）をやめ、
**AnsiString による byte-exact 入出力 + `Utf8Decode`/`Utf8Encode`** で UTF-8 を保つ。

> 当初案の `LoadStringsFromUTF8File`/`SaveStringsToUTF8File` は、導入版の Inno Setup 6 に
> **`LoadStringsFromUTF8File` が存在しない**（コンパイル時 `Unknown identifier`）。実際に利用可能な
> `LoadStringFromFile`(AnsiString=生バイト) / `Utf8Decode` / `Utf8Encode` / `SaveStringToFile`(byte-exact)
> で同等以上を実現する（利用可否は ISCC で実測済み）。

```pascal
{ 変更後（要点） }
LoadStringFromFile(SettingsFile, raw);   { AnsiString: 生バイト読み込み(変換なし) }
content := Utf8Decode(raw);              { UTF-8 生バイト -> 正しい Unicode(日本語保持) }
{ ... content 上で "output_directory" の値(" と " の間)を jsonPath へ置換 ... }
SaveStringToFile(SettingsFile, Utf8Encode(content), False);  { UTF-8 バイトへ戻し byte-exact 保存 }
```

- ファイル全体を1つの Unicode 文字列として扱うため**改行も含め byte-exact**。日本語ラベルは保持され、
  **日本語を含む出力先パスも `Utf8Encode` で正しく UTF-8 化**される。
- 生成ファイルは **BOM 無しの有効な UTF-8**（`ISCC` コンパイル + 最小テストインストーラ実行で検証済み）。
- ロジック（`output_directory` の値置換）は不変。

### 4-2.【防御】`src/settings/settings_window.py` `load_settings`
読み込みを専用ヘルパへ分離し、複数エンコードで回復可能にする。

```python
# setting.json を読み込み JSON(dict) を返す。読めない/壊れている場合は None。
# UTF-8(BOM許容) を最優先し、失敗時は CP932 で回復を試みる
# (旧インストーラが ANSI 保存した破損ファイルの救済 / error 20260707)。
def _read_settings_file():
    for enc in ("utf-8-sig", "cp932"):
        try:
            with open(SETTINGS_FILE, "r", encoding=enc) as f:
                content = f.read().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not content:
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            continue
    return None


def load_settings(persist_new_keys=True):
    if not os.path.exists(SETTINGS_FILE):
        return DEFAULT_SETTINGS.copy()

    data = _read_settings_file()
    if data is None:
        return DEFAULT_SETTINGS.copy()

    merged = _merge_with_defaults(data)
    # 新規キー補完時、または CP932 で回復した(=UTF-8で読めなかった)場合に、
    # UTF-8 で正規化保存してファイルを修復する。既存値は上書きしない。
    if persist_new_keys and _has_new_keys(data, merged):
        try:
            save_settings(merged)   # save_settings は UTF-8 で書き出す(既存実装)
        except OSError:
            pass
    return merged
```

- **UTF-8 は `utf-8-sig` で読む**：BOM 付き/無しの双方に対応し、`json.loads` の BOM 起因失敗を防ぐ（新インストーラが UTF-8 化した後も安全）。
- **CP932 フォールバック**：旧インストーラで ANSI 保存された破損ファイルでも、CP932 で読めば JSON 構造は解釈でき、**`output_directory` 等の ASCII 値は正しく回復**する（日本語値 `comment_label` 等は文字化けし得るが、アプリは起動でき設定画面で再設定可能）。
- **自己修復**：`save_settings`（UTF-8 保存）で新規キー補完時に正規化保存されるため、次回以降は UTF-8 で健全に読める。CP932 で回復したケースも UTF-8 へ移行する。

> `save_settings` は現状 `open(..., "w", encoding="utf-8")`（L241 付近）で **UTF-8 保存**のため変更不要。書き込み側は既に正しい。

---

## 5. 既存の破損インストールの救済

| 対象 | v1.0.3 適用後の挙動 |
|---|---|
| 旧版で新規インストール済み(CP932 破損) | アプリの CP932 フォールバックで**起動可能**に回復。`output_directory` は保持。日本語値が化けていれば設定画面で再設定。 |
| 旧版から v1.0.3 へ更新 | 更新時は既存 setting.json を verbatim 復元（`request_autoupdate.md §8`）するため破損ファイルが残るが、**アプリ側フォールバックで起動可能**。以後 UTF-8 へ自己修復。 |
| v1.0.3 で新規インストール | インストーラが UTF-8 保存するため**破損しない**（根本解決）。 |

> 更新経路では旧 `AutoEditSetup.exe`(旧インストーラ)が使われる場合があるため、**アプリ側の防御(4-2)が回復の要**。根本(4-1)は新規インストールと将来の再パッチを守る。

---

## 6. 後方互換・影響
- `PatchOutputDir` のロジック(値置換)は不変。UTF-8 入出力化のみで、正常系(v1.0.3 新規インストール)は従来どおり `output_directory` を反映しつつ**ファイルは UTF-8 のまま**。
- `load_settings` の戻り値仕様・呼び出し側は不変。読み込み経路のみ堅牢化。
- 既存の正常な UTF-8 `setting.json`(開発環境含む)は `utf-8-sig` で問題なく読める。
- `_merge_with_defaults` / `_has_new_keys` / `save_settings` は変更なし。

---

## 7. 変更ファイル一覧
| ファイル | 変更内容 | 節 |
|---|---|---|
| `installer/AutoEdit.iss` | `PatchOutputDir` の `LoadStringsFromFile`→`LoadStringsFromUTF8File`、`SaveStringsToFile`→`SaveStringsToUTF8File`(2行) | §4-1 |
| `src/settings/settings_window.py` | `_read_settings_file`(新規: utf-8-sig→cp932 回復)を追加、`load_settings` を同ヘルパ経由へ変更 | §4-2 |

---

## 8. テスト観点（レビュー後の検証項目）
1. **v1.0.3 新規インストール**：出力先を選んでインストール → 起動できる。`{app}\_internal\src\settings\setting.json` が **UTF-8 のまま**で `output_directory` がユーザー値になっている。日本語値(`comment_label` 等)が化けていない。
2. **旧版 CP932 破損ファイルの回復**：CP932 で保存された `setting.json` を配置 → アプリが**クラッシュせず起動**し、`output_directory` が保持される。起動後に UTF-8 へ自己修復される。
3. **空/不正 JSON**：空ファイル・壊れた JSON でも既定にフォールバックして起動する。
4. **正常 UTF-8(BOM無)/UTF-8(BOM付)**：どちらも正しく読める(`utf-8-sig`)。
5. **開発(ソース)実行**：既存の UTF-8 `setting.json` が従来どおり読める。
6. **更新経路**：既存 setting.json 保持(verbatim)後もアプリが起動する(フォールバック有効)。
7. インストーラ再コンパイル後、`ISCC` が警告のみで成功する。

---

## 9. リリース手順への影響
- 修正後、**v1.0.3** として再ビルド・再リリースする（`installer/README.md` / `docs/HowToRelease.md` の手順）。`src/version.py` と `.iss` の `MyAppVersion` を 1.0.3 に更新し同期する。
- 本修正で v1.0.0〜v1.0.2 の起動不能が解消される旨をリリースノートへ明記する。

---

## 10. 未確定事項（要ユーザー確認）
- **CP932 回復時の日本語値の扱い**：本設計は「起動を最優先し、化けた日本語値は設定画面で再設定」とする。回復不能な値は既定へ戻す等の追加処理を望む場合は調整。
- **BOM**：実装は `SaveStringToFile(..., Utf8Encode(content))` で **BOM 無しの UTF-8** を書き出す（検証済み）。アプリ側も `utf-8-sig` で BOM 有無どちらも許容するため実害なし。
- **修正の即時リリース可否**：本書は設計。承認後に実装 → v1.0.3 ビルド・公開まで行うか。
