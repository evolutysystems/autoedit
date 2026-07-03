# 修正設計書: 実行環境に応じた GPU/CPU 自動選択

`docs/request/request5.md` の要望に対する修正設計書。CLAUDE.md の方針に従い、本書のレビュー後に実装する。

---

## 1. 概要

現状、音声認識（faster-whisper）の実行デバイスは `setting.json` の `subtitle.whisper_device`
（既定 `"cpu"`）で **CPU 固定**になっている。GPU 搭載環境でも CPU で動くため、音声認識が大きなボトルネックになる。

本改修では、**実行環境を参照して利用可能な GPU（CUDA）があれば GPU、なければ CPU を自動選択**する。
これにより GPU 環境では音声認識を大幅に高速化し、CPU 環境では従来どおり動作させる（実行時間短縮が目的）。

| 項目 | 現状 | 改修後 |
|---|---|---|
| `whisper_device` | `"cpu"` 固定 | `"auto"`（既定）/ `"cpu"` / `"cuda"` を選択可。`auto` 時は環境判定 |
| `whisper_compute_type` | `"int8"` 固定 | `"auto"`（既定）/ 明示値。`auto` 時はデバイスに応じて選択（GPU=`float16`／CPU=`int8`） |
| 判定対象 | なし | CUDA 対応 GPU の有無を起動時に検出 |
| 失敗時 | ― | GPU 初期化失敗時は **CPU へ自動フォールバック**して処理継続 |

### 1.1 設計方針（CLAUDE.md 準拠）

- **新規ライブラリを追加しない**。GPU 検出は faster-whisper のバックエンドである **CTranslate2** の
  `ctranslate2.get_cuda_device_count()` を用いる（faster-whisper が既に依存しているため追加依存ゼロ）。
- **ハードコードを避ける**。デバイス・計算精度は `setting.json` から取得し、`"auto"` という選択肢を追加する。
  既定値を `"auto"` にするが、ユーザーは `"cpu"`/`"cuda"` を明示して固定もできる。
- **既存実装を破壊しない**。`whisper_device` に従来どおり `"cpu"`/`"cuda"` を直接指定した場合は**その値を尊重**する。
  キー名・シグネチャは維持し、`"auto"` は新規の振る舞いとして上乗せする。
- **後方互換**。旧 `setting.json`（`whisper_device:"cpu"`）はそのまま CPU 動作。新規キーは `.get(key, 既定値)` で吸収。
- **堅牢性**。検出ミスや GPU ドライバ不整合に備え、GPU で `WhisperModel` 構築に失敗したら CPU で再試行する。

---

## 2. 現状分析

### 2.1 CPU 固定箇所（音声認識）

`src/modules/subtitle_generator.py` の `WhisperTextSource`：

```python
# __init__  (:120-121)
self._device = subtitle_settings.get("whisper_device", "cpu")          # ← CPU 固定の起点
self._compute_type = subtitle_settings.get("whisper_compute_type", "int8")

# extract() (:139-141)
model = WhisperModel(
    self._model_name, device=self._device, compute_type=self._compute_type
)
```

- `whisper_device` をそのまま `WhisperModel(device=...)` へ渡している。設定が `"cpu"` のため常に CPU。
- `whisper_compute_type` も `"int8"`（CPU 向け量子化）固定。GPU では `float16` 等が一般的で、ここも連動が必要。

### 2.2 既定値（`src/settings/settings_window.py`）

```python
# DEFAULT_SETTINGS["subtitle"]  (:129-130)
"whisper_device": "cpu",
"whisper_compute_type": "int8",
```

> 字幕タブの UI には Whisper のデバイス/精度の入力欄は**現状無い**（`DEFAULT_SETTINGS` と `setting.json` のみで管理）。
> 本改修は主にロジック側で完結する（UI 追加は §6 の任意項目）。

### 2.3 その他の CPU 処理（FFmpeg エンコード）

`src/modules/ffmpeg_runner.py` `build_encode_options()`（`:94`）は `video_codec` 既定 `"libx264"`（CPU エンコーダ）。
これも CPU 処理だが、GPU エンコード（NVENC 等）は**画質・対応コーデック・検出方法が CPU と異なる**ため、
本改修の主対象（音声認識）とは切り離し、§7 に**任意拡張**として整理する（要確認事項）。

---

## 3. デバイス判定設計

### 3.1 判定ロジック

`whisper_device` の値で分岐する。

| 設定値 | 挙動 |
|---|---|
| `"auto"`（新・既定） | CUDA GPU を検出 → 有れば `"cuda"`、無ければ `"cpu"` |
| `"cuda"` | GPU を明示指定（検出に関わらず使用。失敗時は §3.3 でフォールバック） |
| `"cpu"` | CPU を明示指定（従来どおり） |

### 3.2 GPU 検出ヘルパ（CTranslate2 利用・追加依存なし）

```python
# 利用可能な CUDA GPU 数を返す (検出不能・未導入時は 0)
# faster-whisper のバックエンド CTranslate2 を用い、追加依存を増やさない
def _cuda_device_count():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception:
        # ctranslate2 未導入 / CUDA ランタイム無し等は GPU 無しとみなす
        return 0


# whisper_device 設定から実際に使用するデバイスを解決する
# "auto": GPU があれば "cuda"、無ければ "cpu"
# "cuda"/"cpu": 明示指定を尊重する
def _resolve_device(configured_device):
    device = (configured_device or "auto").strip().lower()
    if device == "auto":
        return "cuda" if _cuda_device_count() > 0 else "cpu"
    return device
```

> `ctranslate2.get_cuda_device_count()` は CUDA ランタイムが無い環境でも例外/0 を返すため、
> `try/except` と合わせて「GPU 無し＝CPU」へ安全に倒れる。`torch` には依存しない。

### 3.3 計算精度（compute_type）の解決

`whisper_compute_type` が `"auto"`（新・既定）のとき、デバイスに応じた既定値を割り当てる。明示値はそのまま使う。

```python
# 計算精度を解決する
# "auto": GPU → float16 / CPU → int8。明示値はそのまま使用
def _resolve_compute_type(configured_compute_type, device):
    ct = (configured_compute_type or "auto").strip().lower()
    if ct != "auto":
        return ct
    return "float16" if device == "cuda" else "int8"
```

> **設計判断（既定精度）**
> GPU は `float16`、CPU は `int8` を既定とする（faster-whisper の一般的な推奨構成）。
> 画質/精度を固定したいユーザーは `whisper_compute_type` に明示値（例 `"float32"`）を入れて上書きできる。

---

## 4. subtitle_generator.py 修正設計

### 4.1 WhisperTextSource の初期化（`:118-121` 付近）

設定値は保持しつつ、`extract()` 実行時に「解決済みデバイス/精度」を確定する
（GPU 検出は import コストを伴うため、実際に音声認識する直前で行う）。

```python
def __init__(self, subtitle_settings):
    self._model_name = subtitle_settings.get("whisper_model", "large-v3")
    # 設定値を保持 (auto を含む)。実デバイスは extract 時に解決する
    self._device_setting = subtitle_settings.get("whisper_device", "auto")
    self._compute_type_setting = subtitle_settings.get("whisper_compute_type", "auto")
    self._beam_size = int(subtitle_settings.get("whisper_beam_size", 8))
    ...
```

### 4.2 extract() でのデバイス解決とフォールバック（`:138-141` 付近）

```python
# 実行環境に応じてデバイス/精度を解決する (auto 対応)
device = _resolve_device(self._device_setting)
compute_type = _resolve_compute_type(self._compute_type_setting, device)
_logger.info(
    "音声認識開始 (model=%s, device=%s, compute_type=%s, 設定=%s)",
    self._model_name, device, compute_type, self._device_setting,
)

try:
    model = WhisperModel(self._model_name, device=device, compute_type=compute_type)
except Exception as e:  # GPU 初期化失敗 (ドライバ/VRAM/CUDA 不整合等)
    if device == "cuda":
        # GPU で失敗したら CPU へフォールバックして処理を継続する
        _logger.warning("GPU 初期化に失敗したため CPU へフォールバックします: %s", e)
        device = "cpu"
        compute_type = _resolve_compute_type(self._compute_type_setting, device)
        model = WhisperModel(self._model_name, device=device, compute_type=compute_type)
    else:
        raise
```

- ヘルパ `_cuda_device_count` / `_resolve_device` / `_resolve_compute_type` はモジュール関数として追加
  （`_to_int` などの既存ユーティリティと同じ位置づけ）。
- フォールバックは `device == "cuda"` のときのみ。CPU 指定での失敗は従来どおり例外を上げる。
- `model.transcribe(...)` 以降は**変更なし**（戻り値・タイムライン構造は不変）。

---

## 5. setting.json / DEFAULT_SETTINGS 定義

既存キーは維持し、**値の意味に `"auto"` を追加**する。キー名の変更・追加は無い。

| キー | 型 | 旧既定 | 新既定 | 役割 |
|---|---|---|---|---|
| `whisper_device` | str | `"cpu"` | `"auto"` | `auto`/`cpu`/`cuda`。`auto` で環境判定 |
| `whisper_compute_type` | str | `"int8"` | `"auto"` | `auto`/明示値。`auto` でデバイスに応じ `float16`(GPU)/`int8`(CPU) |

### 5.1 DEFAULT_SETTINGS（`settings_window.py`）

```python
"whisper_device": "auto",          # cpu → auto
"whisper_compute_type": "auto",    # int8 → auto
```

### 5.2 既存 setting.json の扱い（後方互換）

- 旧 `setting.json` に `whisper_device:"cpu"` が保存済みなら**その値が優先**され、従来どおり CPU で動く
  （既定値が `auto` に変わっても、保存済みキーが勝つ）。GPU を使いたい場合は値を `"auto"` か `"cuda"` に変更する。
- キー自体が無い旧データは `_merge_with_defaults` / `.get` により新既定 `"auto"` が補完され、自動判定が効く。

> **設計判断（既定を auto にする是非）** → §8 の確認事項 1。既定 `auto` で「GPU があれば自動的に速くなる」体験を
> 標準にするか、互換重視で既定 `cpu` のままにし `auto` は明示選択にするかは要確認。

---

## 6. 設定 UI（任意・字幕タブ）

現状 UI には Whisper デバイス欄が無い。GUI から切替できると便利なため、**任意拡張**として字幕タブへ
ドロップダウン追加を提案する（実装するかは §8 確認事項 2）。

| ラベル | ウィジェット | 保存キー | 選択肢 |
|---|---|---|---|
| 実行デバイス | `QComboBox`（value/text 分離・既存 `_make_value_combo` 流用） | `whisper_device` | `auto`(自動) / `cpu` / `cuda`(GPU) |
| 計算精度 | `QComboBox` | `whisper_compute_type` | `auto` / `int8` / `float16` / `float32` |

- 既存の `_make_value_combo` / `_set_combo_data` / `_collect_settings` パターンに沿って追加するだけで実現可能。
- UI を追加しない場合でも、`setting.json` 直接編集で全機能を利用できる（ロジック側で完結するため）。

---

## 7. FFmpeg エンコードの GPU 化（任意拡張・要確認）

「処理がすべて CPU 固定」という観点では、FFmpeg の `libx264`（CPU エンコード）も対象になり得る。
ただし GPU エンコードは音声認識とは性質が異なり、慎重な扱いが必要なため**本改修の主対象からは外し**、
将来拡張として整理する。

- GPU エンコーダ（例 NVIDIA `h264_nvenc`）は **画質特性・対応オプションが `libx264` と異なる**
  （`-crf`/`-preset` の意味が変わる、`-cq`/`-rc` 等へ読み替えが必要）。`build_encode_options` の単純置換では
  画質・パラメータ整合が崩れるおそれがある。
- 利用可否の検出は「`ffmpeg -encoders` に該当エンコーダがあるか」「実際にエンコードが通るか」を見る必要があり、
  CTranslate2 ベースの GPU 検出とは別系統。
- したがって本改修では**音声認識のデバイス自動選択のみ**を確実に行い、FFmpeg の GPU 化は
  `video_codec` 設定で明示運用（現状どおり）とする。自動化が必要なら別タスクで設計する（§8 確認事項 3）。

---

## 8. 設計判断・確認事項

実装前に確認したい点（推測実装を避け、回答後に確定。CLAUDE.md 準拠）。

1. **既定値を `auto` にするか**
   既定を `whisper_device:"auto"` / `whisper_compute_type:"auto"` にして「GPU があれば自動で高速化」を
   標準体験にしたい。互換重視で既定を据え置き `auto` は明示選択のみにする案もある。どちらにするか。
2. **設定 UI への追加可否**（§6）
   字幕タブに「実行デバイス／計算精度」ドロップダウンを追加するか。不要ならロジックのみで実装する。
3. **FFmpeg の GPU エンコード対応**（§7）
   今回は音声認識のみを対象とし、FFmpeg は対象外とする方針でよいか。必要なら別タスク化する。
4. **対象 GPU の範囲**
   想定は **NVIDIA CUDA**（faster-whisper/CTranslate2 が対応する主経路）。AMD/Intel GPU や Apple Metal は
   faster-whisper の標準サポート外のため**今回は対象外**（CPU 動作）とする想定でよいか。

> 未確定時の既定方針: 1=auto を既定 / 2=UI 追加は任意で見送り可 / 3=今回は対象外 / 4=NVIDIA CUDA のみ。

---

## 9. 後方互換・エラー処理

- `whisper_device` に従来値 `"cpu"`/`"cuda"` を明示した場合は**その指定を尊重**（`auto` のときのみ自動判定）。
- GPU 検出は `try/except` で安全側（GPU 無し＝CPU）へ倒れる。`ctranslate2` 未導入環境でも例外にしない。
- GPU で `WhisperModel` 構築に失敗したら **CPU へ自動フォールバック**し、処理を止めない（§4.2）。
- `model.transcribe` 以降の出力（タイムライン構造）は不変。`build_subtitle_file`/`burn_subtitle` への影響なし。
- 既存例外機構（`SubtitleError`）を踏襲し、新規例外は追加しない。
- ログに「設定値・解決後デバイス・精度・フォールバック有無」を出力し、どの経路で動いたか追跡可能にする。

---

## 10. 変更対象ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/modules/subtitle_generator.py` | `_cuda_device_count` / `_resolve_device` / `_resolve_compute_type` を追加。`WhisperTextSource.__init__` で設定値保持、`extract()` でデバイス解決＋GPU 失敗時の CPU フォールバック（§3・§4） |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["subtitle"]` の `whisper_device` を `"auto"`、`whisper_compute_type` を `"auto"` に変更（§5.1）。（任意）字幕タブにデバイス/精度ドロップダウン追加（§6） |
| `src/settings/setting.json` | （任意）既存値を `"auto"` へ更新。未更新でも後方互換で動作（§5.2） |

> `model.transcribe` 呼び出し・タイムライン構造・FFmpeg 処理は**変更しない**（破壊しない）。

---

## 11. 実装ステップ（レビュー後）

1. `subtitle_generator.py` にヘルパ（`_cuda_device_count`/`_resolve_device`/`_resolve_compute_type`）を追加（§3）。
2. `WhisperTextSource.__init__`/`extract()` を改修し、デバイス解決＋GPU 失敗時 CPU フォールバックを実装（§4）。
3. `DEFAULT_SETTINGS` の既定を `auto` に更新（§5.1。確認事項 1 の結論を反映）。
4. （任意）字幕タブへデバイス/精度ドロップダウンを追加（§6。確認事項 2 の結論を反映）。
5. 動作確認:
   - GPU 環境: `auto` で `device=cuda` が選ばれ、CPU より短時間で完了すること（ログで確認）。
   - CPU 環境: `auto` で `device=cpu` に倒れ、従来どおり完了すること。
   - GPU 障害模擬（無効な CUDA 等）: CPU フォールバックで処理が継続すること。
   - 旧 `setting.json`（`whisper_device:"cpu"`）で従来どおり CPU 動作すること。
