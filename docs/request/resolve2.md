# 設計書: 無音カット閾値と発話検出閾値の分離（テロップ表示タイミングの適正化）

`docs/request/request2.md` の要望に対する設計書。CLAUDE.md の方針に従い、本書のレビュー後に実装する。

---

## 1. 概要

現状はカット判定にもテロップ表示にも事実上 1 つの音量閾値しか作用しておらず、
「まだ発話していない区間（動画冒頭〜発話開始まで）にテロップが出続ける」問題が発生する。

これを解決するため、**2 種類の閾値を役割ごとに分離**する。

| 閾値 | 役割 | 目安 | 既存/新規 |
|---|---|---|---|
| **無音カット閾値**（`silence_cut.noise_threshold_db`） | 動画から**除去**する無音区間の判定 | -25dB | 既存（変更なし） |
| **発話検出閾値**（`subtitle.speech_threshold_db`） | テロップを**表示**する発話区間の判定 | -10dB 前後 | 新規 |

要望の例で言えば:

- 無音カット閾値 -25dB … 動画全体が -25dB を超えるためカットされない（＝現状維持で問題なし）。
- 発話検出閾値 -10dB … この閾値を超える区間（実際に喋っている 4〜6 秒・8〜9 秒）だけにテロップを限定する。

設計方針（CLAUDE.md 準拠）:

- ハードコードを排し、新閾値は `setting.json` から取得する。
- 既存キー・既存関数シグネチャを維持し後方互換を保つ。
- 発話区間の検出には**既存の `silence_cutter.detect_silence` / `build_keep_segments` を再利用**し、新規ライブラリ・重複実装を増やさない。
- 不足キーは `DEFAULT_SETTINGS` と `.get(key, 既定値)` で吸収する（旧 `setting.json` でも動作）。

---

## 2. 現状分析

### 2.1 音量閾値の現状（単一閾値）

`src/modules/silence_cutter.py:163` で `noise_threshold_db`（既定 -30 / 現 `setting.json` は -26）を読み取り、
`detect_silence`（`:21`）が FFmpeg `silencedetect=noise=<th>dB:d=<min>` で無音区間を抽出 → `build_keep_segments`（`:73`）で
**残す有音区間**を算出して結合している。これは「カットするか否か」の判定にのみ使われる。

### 2.2 テロップのタイミング決定の現状

`src/modules/subtitle_generator.py` のテロップ表示時刻は、**faster-whisper の認識セグメント `start`/`end` のみ**で決まる
（`WhisperTextSource.extract` `:128`、`build_subtitle_file` `:198`）。
音量による絞り込みは一切行っていない。

→ そのため Whisper が割り当てたセグメント区間がそのままテロップ表示区間になり、
**発話前の低音量区間（要望例の 0〜4 秒）にもテロップが表示され続ける**。
無音カット閾値を上げても「カットされる/されない」が変わるだけで、テロップ表示タイミングは制御できない。

### 2.3 既存の紛らわしいキー

`setting.json` の `subtitle.threshold`（"0.6"）/ `subtitle.min_speech_ignore_time`（"0.6"）は
参照元プロジェクトから引き継いだ値で、現コードでは `min_speech_ignore_time` のみが
`silence_cutter.run` の最小無音時間フォールバック（`:165`）として参照される。
**音量(dB)閾値ではない**ため、今回の発話検出閾値とは別物として扱う（§7 で確認事項に記載）。

---

## 3. setting.json 定義（subtitle セクション追加項目）

既存キーは維持し、以下を追加する。

| キー（新規） | 型 | 既定値 | 役割 |
|---|---|---|---|
| `speech_gate_enabled` | bool | `true` | 発話区間によるテロップ絞り込みの ON/OFF |
| `speech_threshold_db` | int | `-16` | 発話とみなす音量閾値(dB)。これ未満はテロップ対象外 |
| `speech_min_duration_sec` | float | `0.2` | 発話区間検出時の最小無音長(秒)。微小な息継ぎで区間が分断されるのを抑える |
| `speech_gate_mode` | str | `"trim"` | 絞り込み方式。`"trim"`=前後の無音だけ切詰め / `"split"`=発話小区間ごとに分割 |
| `speech_pad_sec` | float | `0.1` | 表示区間の前後パディング(秒)。語頭/語尾の切れ過ぎを防止 |

> **設計判断（閾値の大小関係）**
> 「発話検出閾値（-16dB 等）」は「無音カット閾値（-25dB 等）」より**大きい（＝より大きい音量）**であることが前提。
> `speech_threshold_db <= silence_cut.noise_threshold_db` の場合は絞り込みが意味を成さないため、
> 警告ログを出して**絞り込みをスキップ**（＝従来動作）する（§5.4）。

> **設計判断（既定 -16dB の根拠）**
> 要望例では発話 -10dB・無音 -25dB。両者の間に閾値を置くのが安全側のため既定を -16dB とした。
> 実素材で調整可能なよう `setting.json` 化する（ハードコード禁止方針）。

### setting.json 追記イメージ（subtitle セクション・抜粋）

```json
"subtitle": {
    "language": "ja",
    "min_speech_ignore_time": "0.6",
    "enabled": false,
    "speech_gate_enabled": true,
    "speech_threshold_db": -16,
    "speech_min_duration_sec": 0.2,
    "speech_gate_mode": "trim",
    "speech_pad_sec": 0.1,
    "engine": "whisper"
}
```

---

## 4. 処理フロー

```
[無音カット済み動画]                       ← context.current_video_path()
        │
        ├─(A) Whisper 認識 → タイムライン [{start,end,text}, ...]
        │
        ├─(B) 発話区間検出（新規）
        │     silencedetect(noise=speech_threshold_db, d=speech_min_duration_sec)
        │       → detect_silence()        ※silence_cutter を再利用
        │       → build_keep_segments()   ※「有音(=発話)区間」を取得
        │
        ├─(C) ゲート適用（新規）gate_timeline_by_speech(A, B)
        │     ・各 Whisper セグメントを発話区間と交差(intersect)
        │     ・交差なし → そのテロップは破棄
        │     ・trim: 区間内の最初の発話開始〜最後の発話終了に丸める
        │     ・split: 発話小区間ごとに分割（同一テキストを複数 Dialogue 化）
        │     ・speech_pad_sec で前後を微延長（隣接区間は重複させない）
        │
        └─(D) ASS 生成 → 焼き込み（既存 build_subtitle_file / burn_subtitle）
```

要望例の動作:

```
Whisper:        [0.0 - 6.0] "（4秒後の発話内容）"      ← 冒頭から表示されてしまう
発話区間(-16dB): [4.0 - 6.0], [8.0 - 9.0]
ゲート(trim)後:  [4.0 - 6.0] "（発話内容）"            ← 実発話時のみ表示
```

---

## 5. subtitle_generator.py 修正設計

### 5.1 発話区間検出関数の追加（既存資産の再利用）

`silence_cutter` の `detect_silence` / `build_keep_segments` を import して発話区間を求める。
**新たな FFmpeg 解析ロジックは書かず**、閾値だけ差し替えて呼び出す。

```python
# 発話とみなせる有音区間を検出する（テロップ表示の絞り込み用）
# silence_cutter を再利用し、無音カットより高い閾値(speech_threshold_db)で再解析する
def detect_speech_regions(input_path, total_duration, speech_threshold_db,
                          min_duration_sec, ffmpeg_settings):
    silence = silence_cutter.detect_silence(
        input_path, speech_threshold_db, min_duration_sec, ffmpeg_settings)
    # 無音の補集合 = 発話(有音)区間
    return silence_cutter.build_keep_segments(silence, total_duration)
```

### 5.2 ゲート関数の追加

```python
# Whisper タイムラインを発話区間で絞り込む
# mode="trim": 各セグメントを「区間内の最初の発話開始〜最後の発話終了」に丸める
# mode="split": 発話小区間ごとに同一テキストの Dialogue を生成する
def gate_timeline_by_speech(timeline, speech_regions, mode="trim", pad_sec=0.0):
    gated = []
    for seg in timeline:
        # セグメントと交差する発話小区間を抽出
        overlaps = [(max(seg["start"], s), min(seg["end"], e))
                    for (s, e) in speech_regions
                    if e > seg["start"] and s < seg["end"]]
        if not overlaps:
            continue  # 発話なし → テロップ非表示
        if mode == "split":
            for (s, e) in overlaps:
                gated.append({"start": s, "end": e, "text": seg["text"]})
        else:  # trim
            gated.append({"start": overlaps[0][0],
                          "end": overlaps[-1][1],
                          "text": seg["text"]})
    return _apply_padding(gated, pad_sec)  # 前後パディング＆重複/逆転防止
```

- `_apply_padding`: `pad_sec` を前後に加算しつつ、`start < 0` や直前イベントとの重なり・start≥end を補正する小ヘルパ。
- 既存 `_format_ass_time` / `build_subtitle_file` はタイムライン構造（`{start,end,text}`）を変えないためそのまま使える。

### 5.3 `run()` への組込み（`:259`）

`text_source.extract` でタイムライン取得後、ASS 生成前にゲートを挿入する。

```python
timeline = text_source.extract(input_path, subtitle_cfg.get("language", "ja"))
...
total_duration = ffmpeg_runner.probe_duration(input_path, ffmpeg_cfg)

# 発話区間によるテロップ絞り込み（新規）
if subtitle_cfg.get("speech_gate_enabled", True):
    timeline = _gate_with_settings(
        input_path, timeline, total_duration, subtitle_cfg, silence_cfg, ffmpeg_cfg)

build_subtitle_file(timeline, font_profile, subtitle_path)
```

`_gate_with_settings` 内で §5.4 の前提チェックを行い、検出→ゲートを実行する。

### 5.4 閾値前提チェック・安全側フォールバック

- `speech_threshold_db <= silence_cut.noise_threshold_db` の場合は、絞り込みが無意味なため
  **警告ログを出して元のタイムラインを返す**（従来動作維持）。
- 発話区間が 0 件（全区間が閾値未満）なら、誤って全テロップ消失するのを避けるため
  **警告ログを出してゲートをスキップ**する（実装上の安全弁）。
- ゲート後にタイムラインが空になった場合も同様に元タイムラインへフォールバックし、ログに残す。

---

## 6. settings_window.py 修正設計（字幕タブへの入力追加）

`resolve.md` で設計済みの「字幕」タブ（`QTabWidget` 化）に、以下を追加する。
タブ未導入の段階で実装する場合は既存 `QGridLayout` の字幕領域へ追加する。

| ラベル | ウィジェット | 保存キー |
|---|---|---|
| 発話検出を使う | `QCheckBox` | `speech_gate_enabled` |
| 発話検出閾値(dB) | `QLineEdit`（`QIntValidator`、負値可） | `speech_threshold_db` |
| 発話最小無音長(秒) | `QLineEdit`（数値） | `speech_min_duration_sec` |
| 絞り込み方式 | `QComboBox`（`trim` / `split`） | `speech_gate_mode` |
| 前後パディング(秒) | `QLineEdit`（数値） | `speech_pad_sec` |

- 負の dB を入力するため `QIntValidator(-100, 0)` 等で範囲を設ける。
- 読込は `_load_to_ui`、保存は `_on_save` の `settings["subtitle"].update({...})` に新規キーを追加（既存マージ保存方式を維持）。
- `DEFAULT_SETTINGS["subtitle"]` に §3 の既定値を追加する。

---

## 7. 後方互換・エラー処理

- 旧 `setting.json`（新規キー無し）でも `.get(key, 既定値)` により `speech_gate_enabled=true` 等で補完され動作する。
- 既存の `silence_cut` セクション・無音カット挙動には**一切変更を加えない**（閾値の役割分離のみ）。
- 発話区間検出の FFmpeg 失敗時は既存 `FFmpegError` が送出される。`run()` ではこれを捕捉し、
  **テロップ全体を失わないようゲートをスキップして従来タイムラインで継続**する方針とする（ログに警告）。
- `build_subtitle_file` / `burn_subtitle` のシグネチャは変更しない。

---

## 8. 変更対象ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/settings/setting.json` | `subtitle` に新規キー追加（§3） |
| `src/modules/subtitle_generator.py` | `detect_speech_regions` / `gate_timeline_by_speech` / `_apply_padding` 追加、`run()` にゲート工程組込み、`silence_cutter` を import |
| `src/settings/settings_window.py` | 字幕タブに発話検出系入力追加、`DEFAULT_SETTINGS`・`_load_to_ui`・`_on_save` 更新 |

> 既存の `silence_cutter.detect_silence` / `build_keep_segments` は**変更せず再利用**する（破壊しない）。

---

## 9. 不明点・確認事項（推測実装せず記載）

1. **絞り込み方式の既定**: `trim`（前後の無音だけ切詰め・テキストはまとめて表示）を既定とした。
   要望文面は「発話タイミングのみ表示」のため `trim` で要件を満たすと判断したが、
   息継ぎ単位で細かく出したい場合は `split` を既定にする選択肢もある。希望を確認したい。
2. **既存 `subtitle.threshold` / `min_speech_ignore_time` の扱い**: これらは dB 閾値ではないため
   今回の発話検出閾値とは別管理とした。役割整理（廃止・リネーム）を行うか確認したい。
3. **発話検出閾値の既定値(-16dB)**: 要望例（発話 -10dB / 無音 -25dB）の中間値とした。
   素材ごとの実測でチューニングが必要になる可能性がある。既定値の希望があれば反映する。
4. **代替案（Whisper VAD）**: faster-whisper の `vad_filter=True`（無音を VAD で除去）でも
   類似効果が得られる。ただし要望は「発話音量(dB)閾値」での制御を明示しているため、
   本設計は dB ベースのゲートを主案とし、VAD は将来オプションとして併記するに留めた。
   VAD 併用を希望する場合は別途設計する。

---

## 10. 実装ステップ（レビュー後）

1. `setting.json` に新規キー追加（§3）。
2. `subtitle_generator.py`: `silence_cutter` import → `detect_speech_regions` / `gate_timeline_by_speech` / `_apply_padding` 追加 → `run()` にゲート工程と前提チェックを組込み。
3. `settings_window.py`: `DEFAULT_SETTINGS` 更新 → 字幕タブへ入力追加 → `_load_to_ui` / `_on_save` 更新。
4. 要望例に相当する素材（冒頭無音＋途中発話）で、発話区間のみテロップ表示されることを確認。
5. 旧 `setting.json`（新規キー無し）でも例外なく動作し、ゲート無効時は従来挙動になることを確認。
