# アーカイブ用切り抜き Timeline のテロップずれ・重なり 修正設計書（改訂版）

対象現象: `docs/error/20260812/error.md`
解析結果: `docs/error/20260812/Analyze.md`（症状 A・B・C の切り分けと実測）
決定事項: 同 §9-1（Q1 短縮 / Q2 禁止 / Q3 許容する / Q4 設定項目は不要 / Q5 再出力は不要 / Q6 クリップ用にも対応する）

本書は `docs/error/20260812/resolve.md`（初版）の**改訂版であり、以降はこちらを正とする**。
初版からの主な違いは §3-3 の**処理時間の実測**（初版の見積り「+20〜30 分」は誤りで、実測は **+7%**）と、
それに伴う実装順序の見直し、および §2 の前提検証・§9 の不変条件の追加である。

`docs/CLAUDE.md`「実装前に設計を行うこと」に従う修正設計書であり、実装は本書のレビュー後に行う。

---

## 1. 結論（先に要点）

解析で分離した 3 つの原因を、それぞれの発生箇所で断つ。

| 原因（Analyze.md） | 対処 | 変更箇所 |
|---|---|---|
| **C.** 残す区間だけを連結した継ぎ接ぎ音声に対する認識で、時刻が後方ほど遅れる（前半 +0.64s → 後半 +1.05s） | **素材そのまま（無音カット前）を認識し、`TimeMap` で「詰めた軸」へ写す** | `src/modules/subtitle_generator.py`（変更 1） |
| **A.** クリップ末尾のテロップが「発話末 +2 秒」まで表示され、次クリップへはみ出して重なる（clip1 で +2.596s） | **クリップ範囲で打ち切る**（Q1 短縮 / Q2 重なりを作らない） | `src/archive/timeline_builder.py`（変更 2・3） |
| **B.** 移動距離 0 のクリックでも `MoveClip` が走り、重なり解消のためテロップが飛ぶ（+2.116s を再現） | **クリックとドラッグを区別し、動いていなければコマンドを積まない**＋`MoveClip` 側の保険 | `src/gui/timeline/timeline_view.py`（変更 4）/ `src/timeline/commands.py`（変更 5） |

`setting.json` は変更しない（Q4）。既存出力の補正・移行は行わない（Q5）。

### 1-1. 全体像（変更後の流れ）

```
normalized.mp4（無音カット前・正規化済み）
 ├ detect_edit_points()                  → keep_segments                  … 変更なし
 └ recognize_for_timeline()                                               … 変更 1
    ├ extract_audio_segments()           → asr_audio.m4a（プレビュー用のみ）… 役割を限定
    ├ text_source.extract(素材そのまま)  → items（★素材時間）
    ├ map_items_to_timeline()            → items（詰めた軸）               … ★新規
    └ adjust_display_timing()            → items（詰めた軸・重なり無し）   … 写像の後へ移動
 └ build_archive_timeline()
    └ _append_subtitles()                クリップ範囲で打ち切って載せる     … 変更 2
 └ split_by_clip()                       グループ範囲で打ち切って振り分ける … 変更 3
```

**`recognize_for_timeline()` が「詰めた軸の items を返す」という対外契約は変えない。**
そのため呼び出し側（`pipeline_runner._run_timeline` / `clip_writer._prepare_edit_points`）と
`timeline_builder` / `builder` の字幕配置は変更不要で、Q6（クリップ用にも対応）が自動的に満たされる。

---

## 2. 設計の前提（実コードで確認済み）

改訂版では、設計が依拠する事実をすべて実コードで確認した。

| # | 前提 | 確認箇所 |
|---|---|---|
| P1 | 認識時点の `context.current_video_path()` は `keep_segments` を求めたファイルと同一（間に差し替えが無い） | `pipeline_runner.py:153-171`（`loudness_normalizer.run` → `detect_edit_points` → `recognize_for_timeline` の順で、途中に `set_current_video_path` が無い）。アーカイブ用は `clip_writer.py:348-355` で `normalized` 固定 |
| P2 | `PipelineContext` に `allocate_intermediate` / `progress_subcallback` / `set_asr_audio_path` / `current_video_path` がある | `pipeline_context.py:69,93,97,114` |
| P3 | `TimeMap.split_interval()` は、詰めた結果 Timeline 上で連続する区間を 1 つへ結合する | `timemap.py:117-138` と `_merge_adjacent()`（同 `:170-177`） |
| P4 | `src/modules` → `src/timeline.timemap` の import で循環しない | `src/timeline/__init__.py` は `.model` と `.timemap` のみ import。`model.py` / `timemap.py` は他モジュールを import しない（純 Python） |
| P5 | `SubtitleClip` は `duration` が可変で `copy()` を持つ（変更 3 で尺を詰められる） | `model.py:274-305` |
| P6 | `_clamp_position()` は「重なっていれば近い側の境界へ寄せる」ため、現在位置の指定でも動く | `commands.py:145-163`。`MoveClip` の早期 return は clamp の**後**（同 `:321-323`） |
| P7 | `Timeline.duration_sec()` は字幕トラックも含む（ログの全長 770.6s に字幕のはみ出しが乗っていた） | `model.py:629-636` |
| P8 | 打ち切りのしきい値に使える既存定数がある（新規定数を作らない） | `timeline_builder.py:39`（`_MIN_SEGMENT_SEC = 0.01`） |
| P9 | `speech_gate` は現状コメントアウトされており、本変更の影響を受けない | `subtitle_generator.py:993` |

---

## 3. 修正方針の比較と選定（症状 C）

症状 A・B は原因が一意なので対処も一意（§5・§6）。症状 C は選択肢があるため比較する。

| 案 | 内容 | 時刻精度 | 処理時間 | 影響範囲 | 判定 |
|---|---|---|---|---|---|
| **A案** | **素材そのままを認識し `TimeMap` で詰めた軸へ写す** | **最良**（Analyze.md §3-3-1 の参照側そのもの） | **+7%**（§3-3 で実測） | `recognize_for_timeline` 内に収まる。契約不変 | **採用** |
| B案 | 残す区間ごとに認識し、区間の開始位置を足す | 悪化。1〜2 秒の断片では文脈が失われ認識自体が落ちる（clip1 なら 38 回） | 大幅増（モデル呼び出しが区間数ぶん） | 同上 | 却下 |
| C案 | 認識はそのままにし、`whisper_vad_filter` / `word_timestamps` の設定だけ見直す | 不明（継ぎ接ぎ音声である根本は残る） | 変化なし | 設定のみ | 却下（Q4 で設定追加は不要とされ、根本解決にならない） |
| D案 | 症状 C は放置し、A・B のみ直す | 1 クリップあたり最大約 1 秒の遅れが残る | 変化なし | 小 | 却下（Q3 で「許容する」と決定済み） |

初版では A案の処理時間を「+20〜30 分」と見積もっていたため段階実装を勧めていたが、
**実測で +7% と判明したため、その前提は取り下げる**（§3-3・§11）。

### 3-3. 実測: A案の処理時間増は +7%

同じ clip1 の音声を、`setting.json` の設定（`large-v3` / `cpu` / `int8` / `beam_size=8` /
`whisper_vad_filter=true`）そのままで 2 通り認識し、認識開始〜完了のログ時刻を比較した。

| 認識対象 | 音声の長さ | 認識時間 | 備考 |
|---|---|---|---|
| 詰めた音声（現行・`asr_audio.m4a`） | 54.38s | **141.0s** | 20:57:30.8 → 20:59:51.8 |
| 素材そのまま（A案・`normalized.mp4`） | 181.10s | **150.7s** | 21:03:54.6 → 21:06:25.3 |
| 差 | ×3.33 | **+9.7s（+6.9%）** | — |

**音声の長さは 3.33 倍になるのに、認識時間は 7% しか増えない。**
`whisper_vad_filter=true` により Silero VAD が無音を落としてから認識するため、
認識本体の負荷は「発話量」でほぼ決まり、増えるのは音声デコードと VAD 走査のぶんだけである。

今回の実行における認識時間の合計は約 27 分（10 クリップ・ログの認識開始〜完了の合計）なので、
**全体で +2〜3 分程度**の増加に収まる見込み。素材／残す区間の比はクリップごとに 1.79〜3.33 倍
（合計 1816.0s / 767.56s = 2.37 倍）で、上記の測定はその中で最も比が大きいケースである。

---

## 4. 変更 1: 認識対象を素材そのままにし、時刻を `TimeMap` で写す

対象: `src/modules/subtitle_generator.py`

### 4-1. import の追加

```python
from ..timeline.timemap import TimeMap
```

循環 import が起きないことは P4 で確認済み。

### 4-2. 新規関数 `map_items_to_timeline()`

`recognize_for_timeline()` の直前へ追加する。

```python
# 認識結果 (素材時間) を「残す区間を詰めた軸」= Timeline 時間へ写す
# (docs/error/20260812/resolve2.md §4)
#
# 素材そのままの音声を認識すると時刻の精度が上がる代わりに、時刻は素材時間で返る。
# ここで TimeMap を通して詰めた軸へ写し直すことで、
# 「recognize_for_timeline は詰めた軸の items を返す」という契約を保つ。
#
# ・カットを跨ぐ字幕     : 詰めた結果 Timeline 上では連続するため 1 区間へまとまる
# ・一部がカットへかかる : かかった分だけ短くなる
# ・全体がカットへ落ちる : 載せない (カットした無音への幻聴字幕もここで落ちる)
#
# 戻り値: (写像後の timeline, 落とした件数)
def map_items_to_timeline(timeline_items, keep_segments):
    timemap = TimeMap.from_segments(keep_segments)
    mapped = []
    dropped = 0
    for entry in timeline_items or []:
        pieces = timemap.split_interval(entry["start"], entry["end"])
        if not pieces:
            dropped += 1
            continue
        # 先頭の開始〜末尾の終了で 1 区間にする (連続する区間は split_interval が結合済み)
        mapped.append({**entry, "start": pieces[0][0], "end": pieces[-1][1]})
    return mapped, dropped
```

`TimeMap.from_segments()` は `media_id` を省略すると全エントリが `None` になり、
`split_interval()` の絞り込みも無効化されるため、素材が 1 本の本用途ではこれで足りる
（`timemap.py:46-55, 119-138`）。

### 4-3. `recognize_for_timeline()` の変更（3 か所）

関数コメントを差し替える。

```python
# 編集点モード用の音声認識 (docs/request/ver3/resolve.md §7.3 / 20260812 resolve2.md §4)
#
# 認識は「素材そのまま (無音カット前) の音声」に対して行い、得られた素材時間を
# TimeMap で詰めた軸へ写す。残す区間だけを連結した音声を認識すると、継ぎ接ぎの影響で
# 単語タイムスタンプが後方ほど遅れる
# (docs/error/20260812/Analyze.md §3-3-1: 前半 +0.64s → 後半 +1.05s を実測)。
#
# 認識に失敗しても字幕を空にして続行する (パイプラインを止めない / §10)。
# 戻り値: (items, 実効字幕設定)
#   items = [{"start","end","text","use","role","font","font_size"}] (詰めた軸)
def recognize_for_timeline(context, keep_segments):
```

**(a) 区間音声の抽出は「プレビュー用」に役割を限定し、失敗しても字幕を作り続ける**

変更前は抽出失敗で `return [], eff_cfg`（字幕が全損）だったが、
認識に使わなくなるため続行できる。

```python
    # ── プレビューの初回再生ソース用に、残す区間の音声を連結しておく (§6.4-5)
    # 認識には使わない (認識は素材そのままに対して行う / §4)。
    # 失敗してもプレビューが都度生成へ切り替わるだけなので、字幕は作り続ける。
    audio_path = context.allocate_intermediate("asr_audio.m4a")
    try:
        silence_cutter.extract_audio_segments(
            context.current_video_path(), keep_segments, audio_path, ffmpeg_cfg,
            on_progress=context.progress_subcallback("プレビュー音声を準備中…"),
        )
    except AutoEditError:
        _logger.exception("プレビュー用音声の抽出に失敗しました (字幕生成は続行します)")
    else:
        setter = getattr(context, "set_asr_audio_path", None)
        if callable(setter):
            setter(audio_path)
```

**(b) 認識対象を `context.current_video_path()` にする**

```python
    # ── 音声認識 (素材そのまま = 無音カット前に対して行う / §4)
    try:
        timeline_items = text_source.extract(
            context.current_video_path(), eff_cfg.get("language", "ja"))
    except Exception:  # noqa: BLE001 (認識失敗で処理全体を止めない / §10)
        _logger.exception("音声認識に失敗したため字幕を作成しません")
        return [], eff_cfg
```

mp4 を直接渡す形は従来画面経路の `clip_writer._transcribe()` と同じで、既存の実績がある。
追加の音声抽出は行わない（デコードは faster-whisper 側が音声ストリームのみ行う）。

**(c) 表示タイミング整形の前に写像を挟む**

```python
    # ── 素材時間 → 詰めた軸へ写す (§4)
    before_map = len(timeline_items)
    timeline_items, dropped = map_items_to_timeline(timeline_items, keep_segments)
    _logger.info(
        "字幕時刻を Timeline の軸へ写像: %d → %d 件 (カット区間のため %d 件除外)",
        before_map, len(timeline_items), dropped,
    )
    if not timeline_items:
        _logger.warning("写像後の字幕が 0 件のため字幕を作成しません")
        return [], eff_cfg

    # ── 表示タイミング整形 (詰めた軸の上で行う。整形の意味を変えないため写像の後)
    ...（既存のまま）
```

整形を写像の後に置くことで、`display_max_hold_sec` / `display_min_duration_sec` が
**表示上の時間**に対する指定という現在の意味を保てる。
整形は `disp_end <= next_start` を守るため、**写像後も字幕どうしは重ならない**（Q2 / §9 の I3）。

---

## 5. 変更 2・3: クリップ範囲で字幕を打ち切る（症状 A）

対象: `src/archive/timeline_builder.py`

### 5-1. 変更 2: `_append_subtitles()` にクリップ尺を渡して打ち切る

呼び出し側（`build_archive_timeline()` 内・現行 `:107-109`）へクリップ尺を渡す。

```python
        # 字幕はクリップの開始位置ぶんだけ後ろへずらし、クリップの尺で打ち切る
        _append_subtitles(timeline, subtitle_track, entry.get("items"),
                          clip_start, cursor - clip_start, segments,
                          media.id, entry["index"])
```

`cursor` はこの時点でこのクリップぶんを積み終えているため、`cursor - clip_start` が
そのクリップの Timeline 上の尺になる。

関数本体（現行 `:150-176`）:

```python
# 字幕 items を字幕トラックへ載せる
# items の時刻は「残す区間を詰めた後」の時間軸 (recognize_for_timeline が素材時間から
# TimeMap で写すため、上の V1 の積み上げと同一の軸になる)。
# クリップの開始位置ぶんだけ offset を一律で加える。
#
# クリップ尺 (clip_duration) を越える表示は終端で打ち切る (20260812 resolve2.md §5-1)。
# アーカイブはクリップを隙間なく連結するため、越えた分はそのまま次クリップの領域へ
# 重なって表示され、書き出しでも次クリップの冒頭に焼かれてしまう
# (docs/error/20260812/Analyze.md §4)。整形の「発話末 +2 秒」がここに現れる。
#
# 由来の素材内時刻は TimeMap で逆算して origin へ残す (クリップ用と同じ扱い)。
def _append_subtitles(timeline, subtitle_track, items, offset, clip_duration,
                      segments, media_id, clip_index):
    timemap = TimeMap.from_segments(segments, media_id=media_id)
    trimmed = 0
    dropped = 0
    for item in (items or []):
        start = float(item.get("start", 0.0))
        end = float(item.get("end", 0.0))
        # クリップ終端で打ち切る (Q1: 短縮)
        if end > clip_duration:
            end = clip_duration
            trimmed += 1
        duration = end - start
        if duration <= _MIN_SEGMENT_SEC:
            # 打ち切った結果ほぼ残らない = 実質クリップの外側 → 載せない
            dropped += 1
            continue
        origin = {"type": ORIGIN_ASR, ORIGIN_ARCHIVE_INDEX: clip_index}
        source_start = timemap.to_source(start)
        source_end = timemap.to_source(end)
        ...（以降は現行のまま。timeline_start=start + offset, duration=duration）
    if trimmed or dropped:
        _logger.info(
            "clip%s: クリップ終端で打ち切った字幕 %d 件 / 載せなかった字幕 %d 件",
            clip_index, trimmed, dropped)
```

* 打ち切り後に `display_min_duration_sec` は**適用しない**。クリップ境界では
  「次クリップの領域へ出ない」ことを優先する（Q1・Q2 の帰結）。
* しきい値は既存定数 `_MIN_SEGMENT_SEC`（0.01 秒 / P8）を再利用し、新規定数・設定は作らない（Q4）。
* `timemap.to_source(clip_duration)` は終端ちょうどでも最後のエントリとして解決される
  （`timemap.py:85-89`）。
* `start` が既に `clip_duration` を超えている場合は `duration` が負になり、
  同じ `<= _MIN_SEGMENT_SEC` の判定で落ちる（追加の分岐は不要）。

### 5-2. 変更 3: `split_by_clip()` の字幕振り分けを「範囲の交差＋打ち切り」へ

現行（`:243-249`）は開始時刻だけで振り分けるため、境界を跨ぐ字幕が丸ごと隣のグループへ移る
（Analyze.md §4-3）。変更 2 で構築時には跨がなくなるが、**編集で跨がせることは可能**なため、
書き出し側も範囲で扱う。

```python
    # 範囲に交差する字幕 (グループの外へ出る分は打ち切る)
    # 開始時刻だけで振り分けると、境界を跨ぐ字幕が丸ごと隣のクリップへ移ってしまう
    # (docs/error/20260812/Analyze.md §4-3)
    for track in timeline.subtitle_tracks():
        for subtitle in track.clips:
            start = max(subtitle.timeline_start, offset)
            stop = min(subtitle.timeline_end, end)
            if stop - start <= _MIN_SEGMENT_SEC:
                continue
            moved = subtitle.copy()
            moved.timeline_start = start - offset
            moved.duration = stop - start
            subtitle_track.clips.append(moved)
```

境界を跨ぐ字幕は**両方のクリップに、それぞれ見えている分だけ**出る。
Timeline 上の見た目と書き出し結果が一致するため、これを正とする（→ §12 R2）。

---

## 6. 変更 4・5: クリックでテロップを動かさない（症状 B）

### 6-1. 変更 4: ドラッグのしきい値（`src/gui/timeline/timeline_view.py`）

import を 2 つ追加する（現状どちらも未 import）。

* `from PySide6.QtCore import QLine, QPoint, QRect, Qt, Signal` … `QPoint` を追加
* `from PySide6.QtWidgets import (QAbstractSlider, QApplication, ...)` … `QApplication` を追加

`__init__`（`:286-310`）へ状態を 2 つ追加する。

```python
        self._press_pos = QPoint()         # 押した座標 (クリック/ドラッグの判定に使う)
        self._drag_moved = False           # しきい値を超えて実際に動かしたか
```

`mousePressEvent`（`:591-635`）の末尾、`_drag_preview` を作る箇所へ追記する。

```python
        self._drag_preview = {"id": target.id, "start": span[0], "duration": span[1]}
        self._press_pos = pos
        self._drag_moved = False
```

`mouseMoveEvent`（`:637-671`）は、しきい値を超えるまで何もしない。
クリップを取得した直後（`clip is None` の判定の後）へ入れる。

```python
        # クリックとドラッグの区別。しきい値は Qt のプラットフォーム値に任せる
        # (設定項目を増やさない / 20260812 resolve2.md §6-1)
        if not self._drag_moved:
            if (pos - self._press_pos).manhattanLength() < QApplication.startDragDistance():
                return
            self._drag_moved = True
```

`mouseReleaseEvent`（`:673-695`）は、動いていなければコマンドを積まない。

```python
        drag = self._drag
        clip_id = self._drag_clip_id
        preview = self._drag_preview
        moved = self._drag_moved
        self._drag = _DRAG_NONE
        self._drag_clip_id = None
        self._drag_preview = None
        self._drag_moved = False

        if drag == _DRAG_PLAYHEAD or clip_id is None or preview is None:
            self.update()
            return

        # 動かしていない = 単なるクリック → 選択だけで終える。
        # ここでコマンドを積むと、重なっている字幕が重なり解消のため別の位置へ飛ぶ
        # (docs/error/20260812/Analyze.md §5)
        if not moved:
            self.update()
            return
```

しきい値を超えるまで `_drag_preview` を書き換えないため、描画（`_clip_span` が
`_drag_preview` を優先して返す）も押した時点の位置のままで、見た目は変わらない。
移動・トリムの両方に効くので、端をクリックしただけで尺が変わることも無くなる。

### 6-2. 変更 5: `MoveClip` の no-op ガード（`src/timeline/commands.py`）

```python
    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        track = timeline.track_of_clip(self._clip_id)
        if clip is None or track is None or track.locked:
            return False
        # 要求位置が現在位置と同じなら何もしない。
        # clamp を先に通すと、すでに他クリップと重なっている場合に
        # 「重なりの解消」として別の位置へ動いてしまう
        # (docs/error/20260812/Analyze.md §5-2)
        if abs(float(self._new_start) - clip.timeline_start) <= _EPS:
            return False
        target = _clamp_position(track, clip, self._new_start, self._min_clip_sec)
        if abs(target - clip.timeline_start) <= _EPS:
            return False
        clip.timeline_start = target
        return True
```

変更 4 が根本対処、変更 5 はコマンドを直接使う経路・将来の追加に対する保険。
`_clamp_position()` 自体は実ドラッグ時の重なり回避として現状のまま残す（Q2: 重なりは禁止）。

---

## 7. `setting.json` 定義

**変更なし**（Q4）。

| 使う値 | 出所 |
|---|---|
| `subtitle.display_max_hold_sec` / `display_min_duration_sec` | 既存。意味も変えない |
| クリップ終端の打ち切りしきい値 | 既存定数 `timeline_builder._MIN_SEGMENT_SEC`（0.01 秒） |
| ドラッグ開始のしきい値 | `QApplication.startDragDistance()`（Qt のプラットフォーム値・既定 10px） |

---

## 8. エラー処理方針 / ログ出力方針

### 8-1. エラー処理

| 事象 | 扱い |
|---|---|
| プレビュー用音声（`asr_audio.m4a`）の抽出失敗 | **字幕生成は続行**（従来は字幕を空にしていた）。プレビューが都度生成へ切り替わるだけ |
| 音声認識の失敗 | 従来どおり空字幕で続行（クリップ全損を避ける） |
| 写像後に字幕が 0 件 | 警告ログを出して空字幕で続行 |
| `keep_segments` が全長 1 区間（無音カット無効） | 写像は恒等変換になる。分岐は設けない |
| `keep_segments` が空（防御的なケース） | `TimeMap` が空になり全件 `dropped` → 上記「0 件」と同じ扱い。`detect_edit_points` は必ず 1 区間以上返すため通常は起きない |
| クリップ終端の打ち切りで残らない字幕 | 件数をログに出して載せない（例外にしない） |

### 8-2. ログ

追加・変更するログ（いずれも INFO・既存の粒度に合わせる）。

```
字幕時刻を Timeline の軸へ写像: 49 → 47 件 (カット区間のため 2 件除外)
clip1: クリップ終端で打ち切った字幕 1 件 / 載せなかった字幕 1 件
```

既存の「テロップ表示タイミング整形: N → M 区間」「音声認識完了 (Timeline 用 N 件)」は残す。
これで **認識 → 写像 → 整形 → 配置** の各段の件数がログだけで追える。

---

## 9. 不変条件（テストで固定する性質）

改訂版で明文化する。これらが崩れると症状が再発する。

| # | 不変条件 |
|---|---|
| I1 | `recognize_for_timeline()` の戻り値は「残す区間を詰めた軸」の時刻である（対外契約） |
| I2 | すべての字幕は `0 <= start` かつ `end <= 所属クリップの終端` に収まる |
| I3 | 同一字幕トラック内で字幕どうしが重ならない（Q2） |
| I4 | `split_by_clip()` の各グループの字幕は `0 <= start` かつ `end <= グループ尺` に収まる |
| I5 | 移動距離 0 の `MoveClip` は Timeline を変更しない（`False` を返す） |

---

## 10. テスト計画

### 10-1. 新規: `tests/test_subtitle_timeline_map.py`

`map_items_to_timeline()` の単体テスト（FFmpeg・Whisper を起こさない純関数）。

| # | 前提 | 期待 |
|---|---|---|
| 1 | `keep=[(0,30)]`（無音カット無効相当） | 時刻が変わらない（恒等） |
| 2 | `keep=[(0,5),(10,15)]`、item `(1,3)` | `(1,3)` のまま |
| 3 | 同上、item `(12,14)` | `(7,9)` へ写る |
| 4 | 同上、item `(4,11)`（カットを跨ぐ） | `(4,6)` の 1 区間へまとまる（分割されない / I1） |
| 5 | 同上、item `(6,9)`（完全にカット区間） | 除外され `dropped=1` |
| 6 | 同上、item `(4,7)`（部分的にカット） | `(4,5)` へ短縮 |
| 7 | `keep=[]` | 全件除外・例外にならない |

### 10-2. 追記: `tests/test_archive_timeline.py`

既存のヘルパー（`_prepared()` / `media_probe` のモック）を流用する。

| # | 内容 |
|---|---|
| 8 | クリップ尺 10 秒に対し item `(9.5, 12.0)` を与えると `timeline_end == クリップ終端` で打ち切られる（I2） |
| 9 | item `(10.5, 12.0)`（完全に外）は載らない |
| 10 | 3 クリップぶんを構築したとき、字幕トラック内に重なりが 1 件も無い（I3） |
| 11 | `split_by_clip()`: 境界を跨ぐ字幕が両グループへ、それぞれ見えている分だけ入る（I4） |
| 12 | 既存テスト（TOP 順・0 起点・`enabled=False` の除外・`to_export_entry`）が変わらない |

### 10-3. 追記: `tests/test_timeline_commands.py`

| # | 内容 |
|---|---|
| 13 | 重なった 2 件の字幕に対し `MoveClip(id, 現在の timeline_start)` が `False` を返し位置が変わらない（I5） |
| 14 | 別位置を指定した移動では従来どおり `_clamp_position` が働く（回帰） |

### 10-4. 回帰

```
python -m unittest discover -s tests
```

`tests/test_archive_prepare.py` は `recognize_for_timeline` をモックしているため影響を受けない見込みだが、
`_prepare_edit_points` の戻り値の形が変わらないことを確認する。

### 10-5. 手動確認

1. 同じ VOD（`2833484601`）でアーカイブ用切り抜きを実行する。
2. 採点グラフのマーカから各クリップ境界へ飛び、**前クリップのテロップが次クリップの冒頭に残っていない**ことを確認する。
3. テロップの重なりが無いこと、**クリックしても動かない**ことを確認する（ドラッグは従来どおり動く）。
4. 再生して発話とテロップのタイミングが合っていること（特にクリップ後半）を確認する。
5. 書き出した動画で、クリップの先頭に前クリップのテロップが焼かれていないことを確認する。
6. ログで「写像: N → M 件」「打ち切った字幕 N 件」が出ていること、
   認識時間が従来比 +10% 程度に収まっていること（§3-3 の実測どおりか）を確認する。
7. クリップ用（通常の 1 本編集）でも字幕がずれていないことを確認する（Q6 の回帰）。

---

## 11. 実装手順

1. `src/modules/subtitle_generator.py`
   1. `from ..timeline.timemap import TimeMap` を追加。
   2. `map_items_to_timeline()` を追加（§4-2）。
   3. `recognize_for_timeline()` を §4-3 の (a)(b)(c) に従って変更し、関数コメントを更新。
2. `src/archive/timeline_builder.py`
   1. `_append_subtitles()` に `clip_duration` を追加して打ち切りを実装（§5-1）。
   2. 呼び出し側で `cursor - clip_start` を渡す。
   3. `split_by_clip()` の字幕振り分けを範囲交差へ変更（§5-2）。
3. `src/gui/timeline/timeline_view.py` にドラッグしきい値を実装（§6-1）。
4. `src/timeline/commands.py` の `MoveClip.apply()` へ no-op ガードを追加（§6-2）。
5. テストを追加・追記（§10-1〜10-3）し、`python -m unittest discover -s tests` を全件グリーンにする。
6. §10-5 の手動確認を実施する。

処理時間の増加が +7%（§3-3）と小さいことが分かったため、
**初版で勧めていた「変更 1 を最後に回す段階実装」は不要**とし、上記の順で一度に入れる。
1〜4 はそれぞれ独立しているため、切り戻しは変更単位で可能。

---

## 12. 残存する既知の誤差・本件対象外の事項

| # | 事象 | 扱い |
|---|---|---|
| 1 | `asr_audio.m4a` 先頭の AAC プライミング約 21ms | **許容**（累積しない定数。プレビュー音声にしか使わなくなるため影響はさらに小さい） |
| 2 | `_cut_region()` の `-ss` + `-c copy` によるキーフレーム丸め（要求 180s に対し実尺 181.1s） | **本件対象外**。`docs/error/20260811/resolve.md` §10-2 のまま。採点グラフのマーカと実映像の対応がずれる別件 |
| 3 | 素材そのままの認識でも残る Whisper 自体の時刻誤差 | **許容**。参照側として最も精度が高い条件であり、これ以上は認識器の性能の問題 |
| 4 | 従来画面経路（`timeline_review=false` / `_transcribe`） | 変更しない。実カット後ファイルの軸とプレビューが一致しており、その経路では整合している |
| 5 | 無音を含む音声を認識することによる幻聴字幕 | `whisper_vad_filter=true` は継続し、さらにカット区間へ落ちた字幕は写像で除外されるため、**カットした無音への幻聴は構造的に落ちる**。増加が観測された場合は §10-5 の手動確認で検知する |

---

## 13. 未確定事項（実装前に判断が必要なもの）

`docs/CLAUDE.md`「不明点がある場合は推測実装せず設計書へ記載すること」に従い記載する。
Q1〜Q6 は `Analyze.md` §9-1 で回答済みで、残るのは下記のみ。いずれも本書の想定で実装可能。

| # | 論点 | 本書の想定 | 変える場合 |
|---|---|---|---|
| R1 | 一部がカット区間へかかる字幕の扱い | `split_interval()` の既定挙動に任せ、**かかった分だけ短縮**する（全体が落ちた場合のみ除外） | 「カットに少しでもかかる字幕は丸ごと落とす」なら §4-2 に判定を追加 |
| R2 | 境界を跨ぐ字幕を書き出しで両クリップへ出すか | **両方へ、見えている分だけ出す**（Timeline の見た目と一致させる） | 「開始側のクリップにのみ出す」なら §5-2 を現行の開始時刻判定のまま、打ち切りだけ足す |
| R3 | プレビュー用 `asr_audio.m4a` をアーカイブ用でも作り続けるか | **作り続ける**（`recognize_for_timeline` を分岐させず実装を単純に保つ）。clip あたり約 5 秒 | アーカイブ用は初回再生の再利用をしていないため、10 クリップで約 50 秒を削るなら引数で切り替える |
