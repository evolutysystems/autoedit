# Split Sound 実験ハーネス

設計書: `docs/request/ver10/resolve_ss1.md` (要望は `docs/request/ver10/ss1.md`)

「話者の声とその他の音を AI で分け、分離音を使って自動編集パイプラインを回すと
**無音カットと文字起こしの精度が上がるのか / どれだけ遅くなるのか**」を測るための道具。

> **`src/` は 1 行も変更しない。** 既存関数 (`silence_cutter.detect_silence` /
> `build_keep_segments` / `volume_analyzer.analyze_min_speech_db` /
> `subtitle_generator.resolve_text_source`) を import して再利用するだけ。
> 設定 (`src/settings/setting.json`) は**読むだけ**で、保存はしない。
> 配布物 (`src/dist`) には一切入らない。

## 1. 用意するもの

| | 入れ方 | 状態 (2026-11-09) |
| --- | --- | --- |
| Demucs (htdemucs) | `pip install demucs` | **導入済み** (demucs 4.1.0)。重みは初回実行時に HuggingFace から自動取得 |
| DeepFilterNet (DF3) | **prebuilt の `deep-filter` 実行ファイル**を使う | **未導入** (§4 参照) |
| torch / torchaudio / numpy | 既に開発環境にある | torch 2.11.0+cpu / torchaudio 2.11.0 |

`pip install demucs` が追加したのは `demucs einops julius lameenc sphn` の 5 つだけで、
**torch は再インストールされていない** (事前に `--dry-run` で確認済み)。

### DeepFilterNet について (重要)

`pip install deepfilternet` は依存の `deepfilterlib` が **Rust (cargo) を要求**し、
Python 3.12 / Windows 向けの wheel が無いため**ビルドできない**。
そのため C2 / C4 は **DeepFilterNet が公式配布している単体実行ファイル
(`deep-filter`)** を使う形にしてある (ffmpeg と同じ「外部 exe を呼ぶ」扱い)。

```bash
# 実行ファイルの場所を渡す
python tools/splitsound/separate.py --materials S1 --conditions c2_df3 \
    --df-bin D:/app/bin/deep-filter.exe
# もしくは環境変数
set SPLITSOUND_DF_BIN=D:\app\bin\deep-filter.exe
```

実行ファイルを置かないまま C2 を指定すると、その条件だけが失敗して先へ進む
(他の条件は測れる)。

## 2. 手順

```bash
# ① 素材の下見 (どれを S1/S2/S3 にするかの手掛かりを出す)
python tools/splitsound/prepare.py --survey --min-duration 300

# ② 共通入力を作る (切り出し → ラウドネス正規化 → PCM WAV 化)
python tools/splitsound/prepare.py --cut                # materials.json の全部
python tools/splitsound/prepare.py --cut --only S1      # 1 本だけ

# ③ 分離 (解析用音声を作る)
python tools/splitsound/separate.py --materials S1,S2,S3 --conditions c1_htdemucs

# ④ 閾値 → 無音検出 → 文字起こし
python tools/splitsound/run_conditions.py --materials S1,S2,S3 \
    --conditions c0_baseline,c1_htdemucs
#   --no-asr を付けると文字起こしを飛ばす (無音カットだけ先に見るとき)

# ⑤ 指標の計算 (正解データがあれば M1〜M6 が埋まる)
python tools/splitsound/evaluate.py --materials S1,S2,S3
```

正解データの作り方は `truth_schema.md`。
置き場は `docs/request/ver10/ss1_truth/<素材ID>.jsonl`。

## 3. 条件 (設計書 §3.1)

| 条件 | 中身 |
| --- | --- |
| `c0_baseline` | 現状。正規化後の音声そのまま |
| `c1_htdemucs` | htdemucs の `vocals` stem |
| `c2_df3` | DeepFilterNet DF3 の強調音 (`--df-bin` が必要) |
| `c3_gate` | `c1` の結果に、元音声とのレベル差でゲートを掛ける (先に c1 を回す) |
| `c4_df3_demucs` | DF3 → htdemucs の 2 段 |

閾値は条件ごとに 2 通り測る。
**T1** = その音声で測り直した値 / **T2** = `c0_baseline` の値を流用。

## 4. 置き場

```text
output/_splitsound/
    survey.json                     素材の下見の結果
    prepared.json                   共通入力を作った記録
    metrics.csv                     条件 × 素材 × 指標の生表 (evaluate.py)
    <素材ID>/
        source_cut.m4a              切り出したそのまま
        normalized.m4a              ラウドネス正規化後
        analysis_source.wav         **全条件の共通入力** (48kHz ステレオ PCM)
        <条件>/
            voice.wav               解析用音声 (分離結果)
            separate.json           分離の検査結果 (長さ・ずれ・所要・RTF)
            result.json             閾値・残す区間・テロップ・所要
```

`output/` は `.gitignore` 済みなのでコミットされない。

## 5. 時間軸について (ここだけは要注意)

分離音の時刻は、本番では**そのまま映像のカット点**になる。1 サンプルのずれが
映像のずれになるため、`separate.py` は毎回次を検査し、通らない結果は「失敗」にする
(設計書 §4.2-2 / §4.2-4)。

| 検査 | 合格条件 |
| --- | --- |
| 長さ | 総サンプル数が入力と**完全一致** |
| 無音でない | 全長 RMS が -60dBFS より大きい |
| 位置ずれ | 入力との相互相関のピークが 0 サンプル (±1) |

**共通入力を PCM WAV にしてあるのはこの検査のため**である。
手元の m4a (AAC) には 325 秒付近にタイムスタンプの不連続があり、
「`-ss` で頭から捨てて読む (タイムスタンプ基準)」と「先頭から流し読みする
(サンプル数基準)」で**同じ時刻の内容が 13 サンプルずれた** (2026-11-09 実測)。
WAV には編集リストや欠落が無いため、どちらの読み方でも同じ位置になる。
