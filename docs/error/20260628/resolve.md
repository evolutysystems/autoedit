# 配布版で CUDA(cublas64_12.dll) ロード失敗により音声認識が落ちる不具合 修正設計書

対象エラー: `docs/error/20260628/error.md`
対象ソース: `src/modules/subtitle_generator.py`（主因）/ `src/settings/setting.json`・`src/settings/settings_window.py`（既定値）/ `docs/HowToRelease.md`（配布注記）
本書はレビュー用の**修正設計書**であり、実装は承認後に行う（`docs/CLAUDE.md` の方針に準拠）。

---

## 1. 事象

PyInstaller でビルドした `src/dist` を zip 化し**別PCへ移行**して `AutoEdit.exe` を実行。動画選択→実行で**音量解析・無音カットまでは成功**したが、フルテロップ生成（音声認識）で失敗した。

```
処理に失敗しました。
テキストソース抽出に失敗: Library cublas64_12.dll is not found or cannot be loaded
```

`cublas64_12.dll` は **CUDA 12 系の cuBLAS**（GPU 行列演算ライブラリ）。GPU 推論時に ctranslate2（faster-whisper のバックエンド）が要求する。

---

## 2. 原因調査

### 2-1. 結論（主因）

**配布先PCで音声認識デバイスが GPU(`cuda`) に解決されたが、実行に必要な CUDA ランタイム（cuBLAS 等）が存在しない**ため、ctranslate2 が `cublas64_12.dll` をロードできずに失敗した。次の3点が重なっている。

#### (a) `whisper_device:"auto"` が GPU を選んでしまう

`setting.json` の `subtitle.whisper_device` は **`"auto"`**。`_resolve_device("auto")` は GPU があれば `"cuda"` を返す（`subtitle_generator.py` L126–L130）。

```python
def _resolve_device(configured_device):
    device = (configured_device or "auto").strip().lower()
    if device == "auto":
        return "cuda" if _cuda_device_count() > 0 else "cpu"
    return device
```

`_cuda_device_count()` は `ctranslate2.get_cuda_device_count()`（L115–L121）で、**GPU が存在すれば 1 以上**を返す。すなわち「**GPU の有無**」しか見ておらず、「**CUDA ランタイムが実際に使えるか**」は判定しない。
→ 移行先が「NVIDIA GPU 搭載だが CUDA Toolkit/cuDNN 未導入」のPCだと、`auto` が `cuda` に解決され、実行時に DLL ロードで破綻する。
（ビルド元PCは CUDA 整備済みのため再現せず、移行先で初めて顕在化した。）

#### (b) CUDA ライブラリが配布物に同梱されていない（確認済）

`dist/AutoEdit/_internal` を調査した結果、**CUDA 関連 DLL は一切含まれていない**。

| 確認内容 | 結果 |
|---|---|
| `_internal` 直下の `cublas*/cudnn*/cudart*` | **無し** |
| `ctranslate2/` 同梱 DLL | `ctranslate2.dll`, `libiomp5md.dll` のみ |
| `cublas64_12.dll` をパッケージ全体から検索 | **0 件** |

ctranslate2 の Windows wheel は **CUDA 本体ライブラリ（cuBLAS/cuDNN）を同梱しない**。GPU 実行はシステム側に CUDA Toolkit（cuBLAS）と cuDNN が導入されている前提。PyInstaller も収集対象にしていない。
→ GPU で動かすには別途 CUDA ランタイムが必要であり、現状の配布物は**GPU 実行を満たせない**。

#### (c) CPU フォールバックが「デコード時の失敗」を取りこぼす

`extract()` には GPU 初期化失敗時の CPU フォールバックがあるが、**`WhisperModel()` 構築時の例外しか捕捉していない**（L192–L202）。

```python
try:
    model = WhisperModel(self._model_name, device=device, compute_type=compute_type)
except Exception as e:
    if device == "cuda":
        ... # CPU へフォールバック
    else:
        raise
segments, _info = self._transcribe(model, input_path, language)  # ← generator を返すのみ

timeline = []
for seg in segments:        # ← 実デコードはここ(遅延評価)。cuBLAS ロードもこの時点で起きうる
    ...
```

faster-whisper の `transcribe()` は**ジェネレータ**を返し、実際の推論は `for seg in segments` で消費する**遅延評価時**に走る。cuBLAS のロードはこの時点で発生し得るため、**構築時 try/except を素通り**する。結果、例外は `extract()` の外へ伝播し、`run()` の

```python
except Exception as e:
    raise SubtitleError(f"テキストソース抽出に失敗: {e}") from e
```

で包まれ、エラー文「**テキストソース抽出に失敗: Library cublas64_12.dll ...**」として表示された。
→ 既存フォールバックは構築時のみで**不十分**。

### 2-2. なぜ「音量解析・無音カットは成功」したのか

音量解析・無音カットは **FFmpeg(CPU) 処理**であり CUDA に依存しない。CUDA を要するのは音声認識（ctranslate2）だけのため、そこまでは正常に進み、テロップ生成で初めて失敗した。事象の進行と符合する。

---

## 3. 修正方針

「**Web ダウンロードして実行**」（`docs/HowToRelease.md` の配布前提）では**移行先PCの CUDA 有無は不明**。よって既定で GPU を要求しないことを主対策とし、`auto`/`cuda` 指定時でも安全に CPU へ退避できるよう堅牢化する。

### 対策A（主）：配布既定デバイスを CPU に固定

`subtitle.whisper_device` の既定を **`"cpu"`** にする。

- `src/settings/setting.json`：`"auto"` → **`"cpu"`**（配布される初期設定）。
- `src/settings/settings_window.py` の `DEFAULT_SETTINGS["subtitle"]`：**既に `"cpu"`**（L137）。setting.json 側を合わせることで整合する。
- `whisper_compute_type` は現状 `"auto"`（CPU では `int8` に解決：L135–L139）で問題なし。CPU 既定と整合する。

> これだけで本エラーは解消する（`auto→cuda` が起きない）。**最小・確実**な対処。GPU を使いたい上級者は設定で明示的に `"cuda"` へ変更する（後述の CUDA 整備が前提）。

### 対策B（主）：デコード時の CUDA 失敗を捕捉して CPU 再試行

`auto`/`cuda` を将来使う場合に備え、**遅延評価（デコード）時の失敗まで含めて**フォールバックする。

- `extract()` の「`_transcribe()` 呼び出し ＋ `for seg in segments` のタイムライン構築」までを一つの処理単位として `try` で囲む。
- `device == "cuda"`（または CUDA 関連を示すエラー文：`cublas`/`cudnn`/`cuda`/`is not found or cannot be loaded` 等）で失敗した場合、**CPU で `WhisperModel` を再構築し、デコードを一度だけ再実行**して処理を継続する。
- 二重実行を避けるため、デコード（timeline 構築）を内部ヘルパへ切り出し、`device="cpu"` で 1 回だけリトライする構成にする。

```python
# 実装イメージ (擬似コード)
def extract(self, input_path, language):
    device = _resolve_device(self._device_setting)
    compute_type = _resolve_compute_type(self._compute_type_setting, device)
    try:
        return self._run_recognition(WhisperModel, input_path, language, device, compute_type)
    except Exception as e:
        if device == "cuda" or _looks_like_cuda_error(e):
            _logger.warning("GPU 実行に失敗したため CPU へフォールバックして再実行します: %s", e)
            return self._run_recognition(
                WhisperModel, input_path, language, "cpu",
                _resolve_compute_type(self._compute_type_setting, "cpu"),
            )
        raise
# _run_recognition: WhisperModel 構築 → _transcribe → for seg in segments で timeline 構築まで
```

> 既存の構築時フォールバック（L192–L202）は本リトライへ統合し、構築時・デコード時の両方を1経路で吸収する。

### 対策C（採用：A案）：GPU 実行のため CUDA ランタイムを同梱する

> **レビュー決定（2026-06-28）: A案（対策C＋対策B）を採用。** GPU 搭載PCでは GPU 実行し、
> CUDA が使えないPCでは対策B で CPU へ自動退避する。`whisper_device` は `"auto"` を維持する
> （対策A の CPU 固定は不採用＝GPU を活かすため）。

ctranslate2 は CUDA を**インポート表に出さず実行時に動的ロード**する（`ctranslate2.dll` の直接インポートは `libiomp5md.dll` のみ＝静的 CUDA 依存なし、を実機で確認）。よって**実行時に読まれる CUDA DLL 一式を配布物へ同梱**し、Windows の DLL 探索路（`_internal` 直下）へ載せる。

#### 必要ライブラリ（ctranslate2 4.7.1 → CUDA 12 / cuDNN 9）

pip wheel から取得して同梱する（クリーンな入手元）。

| パッケージ | 主な DLL |
|---|---|
| `nvidia-cublas-cu12` | `cublas64_12.dll`, `cublasLt64_12.dll` |
| `nvidia-cuda-runtime-cu12` | `cudart64_12.dll` |
| `nvidia-cudnn-cu12`（cuDNN 9） | `cudnn64_9.dll` ＋ `cudnn_ops/cnn/adv/graph/heuristic/engines_*64_9.dll` |
| `nvidia-cuda-nvrtc-cu12` | `nvrtc64_120_0.dll`, `nvrtc-builtins64_129.dll` |

- 実測同梱量：**15 DLL / 約 1.93GB**（`nvblas64_12.dll`・`*.alt.dll` は ctranslate2 未使用のため除外）。
  → 配布物総量は約 **3.3GB** 規模（zip 圧縮で縮む）。cuDNN の未使用サブライブラリ剪定は**実機GPU検証後**の最適化とする（無検証の剪定は GPU パスを壊すため見送り）。
- `cublas64_**12**` ＝ CUDA **12** 系、`cudnn*_**9**` ＝ cuDNN **9** 系。ctranslate2 のバージョン更新時は版整合を再確認する。

#### 残る前提（同梱しても回避できない）

- **NVIDIA ドライバ**：ターゲットPCのドライバが CUDA 12 対応（Windows R525 以降）である必要がある。ドライバ本体（`nvcuda.dll`／カーネルドライバ）は**再配布不可で同梱できない**。古いドライバでは同梱しても失敗する。
- **VRAM/世代**：`large-v3`（float16）はおよそ 3GB+ の VRAM を要する。不足時は decode で失敗。
- 上記いずれの失敗も**対策B（CPU フォールバック）が受け止める**ため、クラッシュせず CPU で完走する。**対策C は対策B との併用が前提**。

#### ビルド反映

`src/main_window.spec` に `_collect_cuda_binaries()` を追加し、上記 wheel の `bin/*.dll` を `Analysis(binaries=...)` で `_internal` 直下へ同梱する（未導入時はスキップして CPU 専用ビルドも壊さない）。

### 対策D（補強・任意）：CUDA 利用可否の厳格判定

`_cuda_device_count()` は「GPU の存在」しか保証しない。可能なら**実際に CUDA で極小の初期化を試すプローブ**を行い、失敗時は `cpu` とみなす案。ただし確実性とコストの面から、主対策は A＋B とし、D は任意（過剰実装を避ける）。

---

## 4. 変更対象と設定

**採用：A案（対策C＋対策B）。** 既定 `whisper_device` は `"auto"` を維持する。

| ファイル | 変更概要 |
|---|---|
| `src/modules/subtitle_generator.py` | `extract()` のフォールバックを**デコード時失敗まで**強化（対策B）。構築〜デコードを `_recognize()` へ内包し、GPU 失敗時は CPU で1回だけ再試行。CUDA エラー判定ヘルパ `_looks_like_cuda_error()` を追加 |
| `src/main_window.spec` | `_collect_cuda_binaries()` を追加し、CUDA ランタイム DLL を `_internal` 直下へ同梱（対策C） |
| `src/settings/setting.json` | **変更なし**（`whisper_device:"auto"` 維持。GPU 可なら GPU、不可なら対策B で CPU） |
| `docs/HowToRelease.md` | （任意）GPU 同梱・ドライバ要件・CPU フォールバックの注記を追記 |

- 設定キーは追加せず（後方互換）。ハードコードは増やさない（`docs/CLAUDE.md` 準拠）。
- ビルド環境に `nvidia-cublas-cu12 / nvidia-cuda-runtime-cu12 / nvidia-cudnn-cu12 / nvidia-cuda-nvrtc-cu12` を導入してからビルドする。

---

## 5. エラー処理・ログ方針

- CPU 再試行に**移行した旨**と**契機の例外**を `WARNING` で出力（GPU 不成立を運用で把握可能にする）。
- CPU 再試行も失敗した場合は従来どおり `SubtitleError` で上位へ。ユーザー向け文言は現状を踏襲。
- デバイス解決結果（`設定=auto → device=cpu` 等）は既存の `INFO` ログ（L187–L190）を踏襲。
- 対策Bのリトライは**最大1回**（無限ループ防止）。CPU でも失敗するケース（モデル未取得・ネット不通等）は別事象として通常のエラー経路へ。

---

## 6. 不明点・確認事項（レビュー対象）

1. **既定デバイス** → **【確定】`"auto"` 維持（A案）。** GPU 可なら GPU、不可なら対策B で CPU 退避。
2. **CPU 性能**：CPU フォールバック時、`whisper_model:"large-v3"` は**処理時間が大きく増える**。配布既定モデルの軽量化（例 `medium`/`small`）は別タスクで検討（本修正のスコープ外）。
3. **GPU 配布の要否**（対策C） → **【確定】GPU 同梱を実施（A案）。** CUDA 12/cuDNN 9 一式（約1.93GB）を同梱。ドライバ要件は残るため対策B 併用。
4. **CUDA エラー判定**（対策B `_looks_like_cuda_error`）：`device=="cuda"` ＋ CUDA 系語（`cublas`/`cublaslt`/`cudnn`/`cudart`/`cuda`）の文字列一致で CPU リトライ対象とする。過剰一致しても CPU 再試行は1回限りで害が小さい。
5. **対策D（厳格プローブ）**：`auto`＋対策B で実害が無くなるため**不採用**。
6. **cuDNN サブライブラリ剪定**：未使用 DLL（`cudnn_adv` 等）の除去でサイズ削減できるが、**実機GPUでの動作確認後**に行う（無検証の剪定は GPU パスを壊すリスク）。

---

## 7. 検証方針

1. **再現環境（GPU あり/CUDA なし）相当**で、`whisper_device:"cpu"`（対策A）にすれば音声認識が完走することを確認。
2. **対策B**：`whisper_device:"cuda"`（または `auto`）に設定した CUDA 欠落環境で、`cublas64_12.dll` 例外を捕捉して**CPU で自動継続**し、テロップが生成されることを確認（WARNING ログ出力も確認）。
3. CPU リトライも失敗する異常系（モデル未取得等）で、従来どおり `SubtitleError` が上がることを確認。
4. CUDA 整備済み環境で `cuda` 指定時に GPU 実行が従来どおり動くこと（リトライに入らない）を確認。
5. 配布物（zip）を**クリーンな別PC（CUDA なし・できれば GPU なし）**で実行し、音量解析→無音カット→**テロップ生成完走**→出力までを確認（`docs/HowToRelease.md §6` チェックリスト準拠）。
