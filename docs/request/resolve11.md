# resolve11 — BudouX 導入によるテロップ改行の文節最適化（15〜20文字） 要望設計書

## 0. 本書の位置づけ
`docs/request/request11.md` の要望に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要なライブラリを追加しない＝必要最小限に留める）に従い、本書レビュー後に実装します。

---

## 1. 要望（request11.md 要約）
1. 改行処理最適化のため **BudouX** ライブラリを導入する。
2. 改行処理を変更する。
   - 現状：**15文字で機械的に改行**（文字数のみ）。
   - 変更後：**15〜20文字の範囲で、BudouX による最適な文節境界で改行**する。

> BudouX とは：Google 製の日本語分節（文節分割）ライブラリ。純 Python・小型モデル同梱・オフライン動作・追加のML実行基盤（torch 等）不要で、`pip install budoux` のみで導入でき、PyInstaller にも同梱しやすい。本プロジェクトの配布形態（誰でもダウンロードして任意環境で実行）と整合する。

---

## 2. 現状分析（関連コードの実測）

| 対象 | 現状 |
|---|---|
| `src/modules/subtitle_generator.py` L194-212 `wrap_by_length(text, max_line_length)` | 手動改行 `\N` を尊重しつつ、各区間を **`max_line_length` 文字ごとに機械的にスライス**（`s[:max_line_length]`）して `\N` で連結。文節・単語境界を無視。 |
| 同 L338-340 `WhisperTextSource._split_lines` | 音声認識直後に `wrap_by_length(text, self._max_line_length)` を適用。 |
| 同 L226 | `self._max_line_length = int(subtitle_settings.get("max_line_length", 15))`。 |
| 同 L627-636 `_review_timeline` | 字幕確定（レビュー）時に `wrap_by_length(e["text"], max_len)` を再適用（手動改行 `\N` は尊重）。 |
| `src/settings/setting.json` L26 | `subtitle.max_line_length: 15`（単一キー。下限の概念なし）。 |
| `src/settings/settings_window.py` L120 / L486-488 / L775 / L873 | DEFAULT_SETTINGS・UI（「1行最大文字数」整数入力）・load・collect に `max_line_length` が存在。下限・改行エンジンの概念なし。 |
| `src/main.spec` / `src/main_window.spec` | `datas=[]` / `hiddenimports=[]`。追加データ同梱の設定なし。 |
| 依存管理 | `requirements*.txt` は不在。依存は各自導入 + PyInstaller で `_internal` に同梱（`src/dist/AutoEdit/_internal` 実績あり）。 |

**結論**：改行の**発生箇所は `wrap_by_length` の1関数に集約**されており、そこを文節ベースへ差し替えれば全経路（認識直後・レビュー確定）に一括反映できる。`wrap_by_length` は**フォールバック用に温存**し、BudouX 版を新設して既存関数を置換せず**上位ディスパッチで切替**える方式が既存構造・後方互換と親和的。

---

## 3. 設計方針

1. **既存 `wrap_by_length` は削除・改変しない**（BudouX 未導入環境・エンジン無効時のフォールバックとして温存）。
2. **BudouX は任意依存として遅延 import**（faster-whisper の既存パターン L249-255 に倣う）。未導入なら警告ログを出して**文字数改行へ自動フォールバック**し、パイプラインは継続する。
3. **改行の下限・上限を setting.json で管理**（ハードコード禁止）。
   - `min_line_length`（下限, 既定 15）
   - `max_line_length`（上限, 既存キーを流用。既定を 20 とする提案）
4. 改行エンジンを `subtitle.wrap_engine` で選択可能にする（`"budoux"` 既定／`"length"` 従来動作）。将来のエンジン差替に対応。
5. 手動改行 `\N` は現状同様に**強制改行として尊重**する。

---

## 4. 改行アルゴリズム設計（BudouX 版）

### 4.1 概要
1. テキストを手動改行 `\N` で分割（強制改行を尊重）。
2. 各区間を BudouX で**文節リスト**へ分割。
3. 文節を貪欲にパックし、**下限 `min_line_length` を満たしつつ上限 `max_line_length` を超えない**位置（＝文節境界）で改行する。
4. 単一文節が上限を超える場合のみ、その行を従来の `wrap_by_length` で**文字数ハード分割**（保険。文節内は物理的に割るしかないため）。
5. 結果を `\N` で連結して返す（従来と同じ出力形式・呼び出し側変更不要）。

### 4.2 疑似コード
```python
# 文節境界で 15〜20 文字を目安に改行する。手動改行 \N は尊重。
def wrap_by_budoux(text, min_line_length, max_line_length, parser):
    if not text:
        return text
    out_lines = []
    for segment in text.split("\\N"):        # 手動/強制改行を尊重
        if not segment:
            out_lines.append(segment)
            continue
        phrases = parser.parse(segment)       # BudouX による文節分割 -> list[str]
        line = ""
        for ph in phrases:
            if not line:
                line = ph
            elif len(line) < min_line_length:
                # 下限未満: 文節を足して下限に近づける(下限優先=短すぎる行を避ける)
                line += ph
            elif len(line) + len(ph) <= max_line_length:
                # 下限達成 かつ 上限内: 同一行へ追加
                line += ph
            else:
                # 下限達成 かつ 上限超過: 文節境界で改行
                out_lines.append(line)
                line = ph
        if line:
            out_lines.append(line)
    # 単一文節が上限超過の行のみ、従来ロジックで文字数ハード分割(保険)
    result = []
    for ln in out_lines:
        if len(ln) > max_line_length:
            result.extend(wrap_by_length(ln, max_line_length).split("\\N"))
        else:
            result.append(ln)
    return "\\N".join(result)
```

### 4.3 パラメータの意味（15〜20 の実現）
- `max_line_length`（=20, 上限）：**文節の組み合わせで超えないように改行**する主制御。
- `min_line_length`（=15, 下限）：**下限未満での改行を避ける**（＝行が短くなりすぎないようにする）。下限に満たない間は文節を足し続けるため、行長は概ね **15〜20 に収束**する（文節が短いため）。
- 文節が短く下限到達時にちょうど上限も超える等の境界では、上限を数文字超えることがあり得るが、**文節を割らないことを優先**する（要望「最適な文節で改行」に合致）。上限の厳密遵守が必要な場合は §12 未確定事項で調整。

### 4.4 BudouX 呼び出し
- 既定日本語パーサを**モジュールレベルで遅延生成・シングルトン保持**（毎回ロードするとコスト増）。
  ```python
  _budoux_parser = None  # 遅延初期化シングルトン

  def _get_budoux_parser():
      global _budoux_parser
      if _budoux_parser is None:
          import budoux  # 任意依存: 未導入時は ImportError -> 呼び出し側でフォールバック
          _budoux_parser = budoux.load_default_japanese_parser()
      return _budoux_parser
  ```

---

## 5. ディスパッチ（改行エンジンの切替）

改行呼び出し箇所（`_split_lines`・`_review_timeline`）を、エンジン設定に応じて分岐する共通ヘルパ `wrap_lines` に集約する。

```python
# 設定に応じて改行方式を選択する。BudouX 未導入/失敗時は文字数改行へフォールバック。
def wrap_lines(text, min_line_length, max_line_length, engine="budoux"):
    if engine == "budoux":
        try:
            parser = _get_budoux_parser()
            return wrap_by_budoux(text, min_line_length, max_line_length, parser)
        except ImportError:
            _logger.warning(
                "budoux 未導入のため文字数改行にフォールバックします "
                "(pip install budoux)。subtitle.wrap_engine を 'length' にすると警告を抑止できます。"
            )
        except Exception as e:  # noqa: BLE001 (分割失敗時もテロップ全損を避ける)
            _logger.warning("BudouX 改行に失敗したため文字数改行にフォールバック: %s", e)
    return wrap_by_length(text, max_line_length)
```

### 5.1 呼び出し側の変更
- `WhisperTextSource.__init__`（L220-244 付近）に下限・エンジンを追加取り込み：
  ```python
  self._min_line_length = int(subtitle_settings.get("min_line_length", 15))
  self._max_line_length = int(subtitle_settings.get("max_line_length", 20))
  self._wrap_engine = str(subtitle_settings.get("wrap_engine", "budoux")).strip().lower()
  ```
- `_split_lines`（L338-340）：
  ```python
  def _split_lines(self, text):
      return wrap_lines(text, self._min_line_length, self._max_line_length, self._wrap_engine)
  ```
- `_review_timeline`（L627-636）：
  ```python
  min_len = int(subtitle_cfg.get("min_line_length", 15))
  max_len = int(subtitle_cfg.get("max_line_length", 20))
  engine = str(subtitle_cfg.get("wrap_engine", "budoux")).strip().lower()
  used = [
      {"start": e["start"], "end": e["end"],
       "text": wrap_lines(e["text"], min_len, max_len, engine),
       "role": e.get("role", _DEFAULT_ROLE)}
      for e in edited if e.get("use", True)
  ]
  ```

---

## 6. 設定（setting.json / DEFAULT_SETTINGS）

### 6.1 追加・変更キー（`subtitle` セクション）
```jsonc
"wrap_engine":     "budoux",   // 新規: 改行方式 "budoux"(既定) | "length"(従来=文字数)
"min_line_length": 15,          // 新規: 1行下限(BudouX時のみ有効)
"max_line_length": 20,          // 既存: 1行上限。既定を 15 -> 20 へ変更(提案)
```
- `DEFAULT_SETTINGS["subtitle"]`（settings_window.py L99- 付近）に **新規2キー**（`wrap_engine`・`min_line_length`）を追加し、`max_line_length` の既定を **20** に更新する。
- 既存 `setting.json` に新キーが無くても `_merge_with_defaults`（L204-211 相当）が**自動補完**するため、旧設定ファイルはそのまま動作する。
- **実 `setting.json` の `max_line_length: 15 -> 20` 書き換え**は、CLAUDE.md「推測で setting.json を変更しない」に配慮し、既定はコード側 DEFAULT_SETTINGS で定義し、**実ファイルの変更は要望確定後**に行う（現行 15 のままでも BudouX は文節改行として機能する）。

---

## 7. 設定画面（settings_window.py）

既存「1行最大文字数」の近傍に **下限入力** と **改行方式トグル** を追加する（`max_line_length` に UI がある既存方針に合わせる）。

### 7.1 UI 追加（`_build_subtitle_tab` L485-489 付近）
```
改行方式        [BudouX(文節) ▼]   ← 新規 wrap_engine_combo ("budoux"/"length")
1行下限文字数   [__15__]           ← 新規 min_line_length_edit (int)
1行最大文字数   [__20__]           ← 既存 max_line_length_edit
```
- `self.wrap_engine_combo`（QComboBox, 表示「BudouX(文節)」→ value `"budoux"` /「文字数」→ value `"length"`。既存の value/text 分離方針に倣う）を新設。
- `self.min_line_length_edit = self._make_int_edit()` を新設（`max_line_length_edit` と同じ生成ヘルパを流用）。
- `__init__` のウィジェット参照初期化（L272 付近）に `self.min_line_length_edit = None` / `self.wrap_engine_combo = None` を追加。

### 7.2 読込・保存
- `_load_to_ui`（L775 付近）：
  ```python
  self.min_line_length_edit.setText(str(subtitle.get("min_line_length", 15)))
  self.max_line_length_edit.setText(str(subtitle.get("max_line_length", 20)))
  # wrap_engine_combo は value="budoux"/"length" を選択状態へ反映
  ```
- `_collect_settings`（L873 付近, `subtitle` update 辞書）：
  ```python
  "wrap_engine": <combo の選択 value>,           # "budoux" | "length"
  "min_line_length": self._to_int(self.min_line_length_edit.text(), 15),
  "max_line_length": self._to_int(self.max_line_length_edit.text(), 20),
  ```

> UI 追加を避け setting.json 管理のみとする選択肢もある（§12 参照）。本設計は `max_line_length` に既存 UI がある整合性から **UI 追加を既定**とする。

---

## 8. ライブラリ導入・配布（PyInstaller）

BudouX は既定パーサ用の**モデル JSON をパッケージ内 data として同梱**しているため、PyInstaller のワンファイル/ワンフォルダ化で**データファイルの収集が必要**。

### 8.1 依存の追加
- 導入コマンド：`pip install budoux`
- `requirements*.txt` は不在のため、`docs/HowToRelease.md`（リリース手順）へ `budoux` の導入を追記する。requirements.txt を新設する場合も**最小限**に留める（CLAUDE.md「不要なライブラリを追加しない」）。

### 8.2 .spec 変更（`src/main.spec` / `src/main_window.spec`）
```python
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ['main.py'],
    ...
    datas=collect_data_files('budoux'),          # モデルJSON等のデータを同梱
    hiddenimports=collect_submodules('budoux'),  # 動的 import 対策(保険)
    ...
)
```
- これにより**オフライン・任意環境で BudouX が動作**する（配布アプリ要件と整合）。
- 同梱に失敗しても §5 のフォールバックにより文字数改行で継続するため、**改行が壊れて全損することはない**。

---

## 9. データフロー（改行の流れ）
```
setting.json(wrap_engine / min_line_length / max_line_length)
  │
音声認識 timeline{start,end,text}
  └► WhisperTextSource._split_lines
        └► wrap_lines(text, min, max, engine)
              ├─ engine="budoux" & budoux導入 → wrap_by_budoux(文節境界で15〜20)
              └─ それ以外/失敗          → wrap_by_length(文字数, 従来)
  └► _review_timeline (字幕確定時)
        └► wrap_lines(text, min, max, engine)   … 手動改行 \N は尊重
  └► build_subtitle_file → burn_subtitle (焼き込み)
```

---

## 10. 後方互換・エラー処理方針
- **既存 `wrap_by_length` は不変**（削除・改変なし）。BudouX 無効・未導入・失敗時のフォールバック先。
- **BudouX 未導入環境**：`wrap_lines` が ImportError を捕捉し警告ログ + 文字数改行へフォールバック。パイプラインは継続（テロップ全損なし）。
- **`wrap_engine="length"`**：従来の文字数改行を明示選択（BudouX を使わない）。
- **旧 setting.json（新キー無し）**：`_merge_with_defaults` が `wrap_engine="budoux"` / `min_line_length=15` を補完。`max_line_length` は既存値（現行 15）を尊重（DEFAULT は 20、実ファイルは未確定）。
- **手動改行 `\N`**：現状同様に強制改行として尊重（BudouX 版でも先に `\N` で分割）。
- **単一文節が上限超過**：`wrap_by_length` で文字数ハード分割（上限保険）。行が上限を大きく超えて表示崩れすることを防ぐ。
- **不正値**：`min_line_length`/`max_line_length` は `_to_int` / `int(...)` で数値化（既存踏襲）。`min > max` の異常設定でも、下限優先ロジックにより極端な破綻はしない（下限を満たすまで足す→上限超過で改行）。必要なら実装時に `min = min(min, max)` の正規化を1行入れる。

---

## 11. 変更ファイル一覧
| ファイル | 変更内容 | 節 |
|---|---|---|
| `src/modules/subtitle_generator.py` | `_get_budoux_parser`（遅延シングルトン）／`wrap_by_budoux`（文節改行）／`wrap_lines`（エンジン分岐・フォールバック）を新設。`WhisperTextSource` に `min_line_length`/`wrap_engine` 取り込み・`_split_lines` を `wrap_lines` へ。`_review_timeline` を `wrap_lines` へ。**`wrap_by_length` は温存**。 | §4,5 |
| `src/settings/settings_window.py` | DEFAULT_SETTINGS へ `wrap_engine`/`min_line_length` 追加・`max_line_length` 既定 20 化。改行方式コンボ＋下限入力 UI 追加。load/collect 拡張。ウィジェット参照2追加。 | §6,7 |
| `src/settings/setting.json` | `wrap_engine`/`min_line_length` 追加・`max_line_length` を 20 へ（**要望確定後**に反映）。 | §6 |
| `src/main.spec` / `src/main_window.spec` | `datas=collect_data_files('budoux')` ＋ `hiddenimports=collect_submodules('budoux')`。 | §8 |
| `docs/HowToRelease.md` | `pip install budoux` を導入手順へ追記。 | §8 |

---

## 12. テスト観点（レビュー後の検証項目）
1. BudouX 導入済み環境で、句をまたぐ長文テロップが **文節境界で改行**され、各行が概ね **15〜20 文字**に収まる（従来の「15文字で語中を割る」現象が解消）。
2. 手動改行 `\N` を含むテキストが、`\N` 位置を保持しつつ各区間で文節改行される。
3. 単一で 20 文字を超える固有名詞等（分割不能な1文節）は、上限で文字数ハード分割され表示崩れしない。
4. `wrap_engine="length"` 設定時は **従来と同一の文字数改行**になる（後方互換）。
5. BudouX 未導入環境で起動 → 警告ログ後、文字数改行で正常動作（クラッシュ・テロップ全損なし）。
6. 旧 setting.json（新キー無し）で起動 → 既定補完で動作、エラーなし。
7. 設定画面で改行方式・下限・上限を変更・保存 → setting.json に反映され、焼き込み結果に反映される。
8. PyInstaller ビルド（`_internal` 同梱）後の実行ファイルで BudouX が動作する（`collect_data_files` 済み）。未同梱でもフォールバックで動作。
9. `min > max` 等の異常設定でも致命的な破綻・無限ループが起きない。

---

## 13. 将来拡張（要望外・任意）
- 英数字・記号の**行頭・行末禁則**処理（BudouX は文節分割のみ。禁則は別途）。
- 表示幅ベース（全角/半角の幅換算）での上限判定（現状は文字数）。
- BudouX の HTML/大規模モデル・他言語パーサ差替（`wrap_engine` の値拡張で対応可能）。

---

## 14. 未確定事項（要ユーザー確認）
- **`max_line_length` 既定値**：本書提案は **20**（現行 setting.json は 15）。実 `setting.json` を 20 へ書き換えてよいか（DEFAULT は 20 で用意、実ファイルは確認後）。
- **`min_line_length` 既定値**：本書提案 **15**（＝要望の下限）。
- **上限の厳密性**：本設計は「文節を割らないことを優先」し、境界条件で上限を数文字超え得る。上限を**厳密遵守**したい場合は、超過時に直近文節を次行へ送る（下限割れ許容）ロジックへ変更する。
- **UI 追加の要否**：改行方式トグル・下限入力を設定画面に出すか（本書は既存 `max_line_length` UI との整合で**追加**を既定）。setting.json 管理のみに留める選択も可。
