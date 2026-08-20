# アーカイブ用切り抜き Timeline のテロップずれ・重なり 修正設計書（初版・破棄）

> **本書は破棄扱い。正は `docs/error/20260812/resolve2.md`（改訂版）とする。**
> 初版は A案の処理時間を「+20〜30 分」と見積もっていたが、実測は **+7%** であり
> （resolve2.md §3-3）、その前提に基づく段階実装の推奨も取り下げた。
> 以下は経緯として残す。実装は resolve2.md に従うこと。

対象現象: `docs/error/20260812/error.md`
解析: `docs/error/20260812/Analyze.md`（症状 A・B・C の切り分けと実測）
決定事項: 同 §9-1（Q1 短縮 / Q2 禁止 / Q3 許容する / Q4 設定項目は不要 / Q5 再出力は不要 / Q6 クリップ用にも対応する）

本書は `docs/CLAUDE.md`「実装前に設計を行うこと」に従う **修正設計書** であり、実装は本書のレビュー後に行う。

---

## 1. 方針

解析で分離した 3 つの原因を、それぞれの発生箇所で断つ。

| 原因 | 対処 | 変更箇所 |
|---|---|---|
| C. 継ぎ接ぎ音声に対する認識で時刻が後方ほど遅れる | **素材そのまま（無音カット前）を認識し、`TimeMap` で「詰めた軸」へ写す** | `src/modules/subtitle_generator.py`（変更 1） |
| A. クリップ末尾のテロップが次クリップへはみ出して重なる | **クリップ範囲で打ち切る**（Q1 短縮 / Q2 重なりを作らない） | `src/archive/timeline_builder.py`（変更 2・3） |
| B. 移動距離 0 のクリックでテロップが飛ぶ | **クリックとドラッグを区別し、動いていなければコマンドを積まない**＋`MoveClip` 側の保険 | `src/gui/timeline/timeline_view.py`（変更 4）/ `src/timeline/commands.py`（変更 5） |

`setting.json` は変更しない（Q4）。既存出力の補正・移行は行わない（Q5）。

### 1-1. 全体像（変更後の流れ）

```
normalized.mp4（無音カット前・正規化済み）
 ├ detect_edit_points()          → keep_segments                     … 変更なし
 └ recognize_for_timeline()                                          … 変更 1
    ├ extract_audio_segments()   → asr_audio.m4a（プレビュー用のみ）  … 役割を限定
    ├ text_source.extract(normalized.mp4)  → items（★素材時間）      … 認識対象を変更
    ├ map_items_to_timeline()    → items（詰めた軸）                  … ★新規
    └ adjust_display_timing()    → items（詰めた軸・重なり無し）      … 順序を写像の後へ
 └ build_archive_timeline()
    └ _append_subtitles()        クリップ範囲で打ち切って載せる        … 変更 2
 └ split_by_clip()               グループ範囲で打ち切って振り分ける    … 変更 3
```

「`recognize_for_timeline()` は詰めた軸の items を返す」という **対外的な契約は変えない**。
そのため呼び出し側（`pipeline_runner._run_timeline` / `clip_writer._prepare_edit_points`）と
`timeline_builder` / `builder` の字幕配置は変更不要で、Q6（クリップ用にも対応）が自動的に満たされる。

---

## 2. 変更 1: 認識対象を素材そのままにし、時刻を `TimeMap` で写す

対象: `src/modules/subtitle_generator.py`

### 2-1. 新規関数 `map_items_to_timeline()`

`recognize_for_timeline()` の直前へ追加する。

```python
# 認識結果 (素材時間) を「残す区間を詰めた軸」= Timeline 時間へ写す
# (docs/error/20260812/resolve.md §2)
#
# 素材そのままの音声を認識すると時刻の精度が上がる代わりに、時刻は素材時間で返る。
# ここで TimeMap を通して詰めた軸へ写し直すことで、
# 「recognize_for_timeline は詰めた軸の items を返す」という契約を保つ。
#
# ・カットを跨ぐ字幕: 詰めた結果 Timeline 上では連続するため split_interval が 1 区間へまとめる
# ・一部がカット区間へかかる字幕: かかった分だけ短くなる
# ・全体がカット区間へ落ちた字幕: 載せない (無音区間への幻聴字幕もここで落ちる)
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

import を 1 行追加する。

```python
from ..timeline.timemap import TimeMap
```

> **循環 import の確認**: `src/timeline/__init__.py` は `.model` と `.timemap` のみを import し、
> `model.py` / `timemap.py` はどちらも他モジュールを import しない（純 Python）。
> よって `src/modules` → `src/timeline.timemap` の参照で循環は起きない。

### 2-2. `recognize_for_timeline()` の変更

```python
# 編集点モード用の音声認識 (docs/request/ver3/resolve.md §7.3 / 20260812 resolve.md §2)
#
# 認識は「素材そのまま (無音カット前) の音声」に対して行い、得られた素材時間を
# TimeMap で詰めた軸へ写す。残す区間だけを連結した音声を認識すると、
# 継ぎ接ぎの影響で単語タイムスタンプが後方ほど遅れる
# (docs/error/20260812/Analyze.md §3-3-1: 前半 +0.64s → 後半 +1.05s を実測)。
#
# 認識に失敗しても字幕を空にして続行する (パイプラインを止めない / §10)。
# 戻り値: (items, 実効字幕設定)
def recognize_for_timeline(context, keep_segments):
```

本体の差分は 3 か所。

**(a) 認識用音声の抽出は「プレビュー用」に役割を限定し、失敗しても続行する**

```python
    # ── プレビューの初回再生ソース用に、残す区間の音声を連結しておく (§6.4-5)
    # 認識には使わない (認識は素材そのままに対して行う / §2)。
    # 失敗してもプレビューが都度生成に切り替わるだけなので、字幕は作り続ける。
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
    # ── 音声認識 (素材そのまま = 無音カット前に対して行う / §2)
    try:
        timeline_items = text_source.extract(
            context.current_video_path(), eff_cfg.get("language", "ja"))
    except Exception:  # noqa: BLE001 (認識失敗で処理全体を止めない / §10)
        _logger.exception("音声認識に失敗したため字幕を作成しません")
        return [], eff_cfg
```

mp4 を渡す形は従来画面経路の `clip_writer._transcribe()` と同じで、既存の実績がある。

**(c) 表示タイミング整形の前に写像を挟む**

```python
    # ── 素材時間 → 詰めた軸へ写す (§2)
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
**表示上の時間**に対する指定という現在の意味を保てる。整形は
`disp_end <= next_start` を守るため、**写像後も字幕どうしは重ならない**（Q2）。

### 2-3. なぜ `TimeMap.split_interval()` で足りるか

`build_archive_timeline` / `builder.build` は残す区間を**隙間なく**積む。
`split_interval()` は分割した区間のうち Timeline 上で連続するものを `_merge_adjacent()` で
まとめるため、カットを何個跨いでも戻り値は 1 区間になる（`timemap.py:117-138, 170-177`）。
新しい写像ロジックを書き起こす必要はない。

---

## 3. 変更 2・3: クリップ範囲で字幕を打ち切る

対象: `src/archive/timeline_builder.py`

### 3-1. 変更 2: `_append_subtitles()` にクリップ尺を渡して打ち切る

呼び出し側（`build_archive_timeline()` 内）にクリップ尺を渡す 1 行を足す。

```python
        # 字幕はクリップの開始位置ぶんだけ後ろへずらし、クリップの尺で打ち切る
        _append_subtitles(timeline, subtitle_track, entry.get("items"),
                          clip_start, cursor - clip_start, segments,
                          media.id, entry["index"])
```

関数本体:

```python
# 字幕 items を字幕トラックへ載せる
# items の時刻は「残す区間を詰めた後」の時間軸 (recognize_for_timeline が素材時間から
# TimeMap で写すため、上の V1 の積み上げと同一の軸になる)。
# クリップの開始位置ぶんだけ offset を一律で加える。
#
# クリップ尺 (clip_duration) を越える表示は終端で打ち切る (20260812 resolve.md §3-1)。
# アーカイブはクリップを隙間なく連結するため、越えた分はそのまま次クリップの領域へ
# 重なって表示され、書き出しでも次クリップの冒頭に焼かれてしまう
# (docs/error/20260812/Analyze.md §4)。整形時の「発話末 +2 秒」がここに現れる。
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
        ...（以降は既存のまま。timeline_start=start + offset, duration=duration）
    if trimmed or dropped:
        _logger.info(
            "clip%s: クリップ終端で打ち切った字幕 %d 件 / 載せなかった字幕 %d 件",
            clip_index, trimmed, dropped)
```

* 打ち切り後に `display_min_duration_sec` は**適用しない**。クリップ境界では
  「次クリップの領域へ出ない」ことを優先する（Q1・Q2 の帰結）。
* しきい値は既存定数 `_MIN_SEGMENT_SEC`（0.01 秒）を再利用し、新しい定数・設定は作らない（Q4）。
* `timemap.to_source(clip_duration)` は終端ちょうどでも最後のエントリとして解決される
  （`timemap.py:85-89`）。

### 3-2. 変更 3: `split_by_clip()` の字幕振り分けを「範囲の交差＋打ち切り」へ

現状は開始時刻だけで振り分けるため、境界を跨ぐ字幕が丸ごと隣のグループへ移ってしまう
（`Analyze.md` §4-3）。変更 2 で構築時には跨がなくなるが、**編集で跨がせることは可能**なため、
書き出し側も範囲で扱う。

```python
    # 範囲に交差する字幕 (グループの外へ出る分は打ち切る)
    # 開始時刻だけで振り分けると、境界を跨ぐ字幕が丸ごと隣のクリップへ移ってしまう。
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
Timeline 上の見た目と書き出し結果が一致するため、これを正とする。

---

## 4. 変更 4・5: クリックでテロップを動かさない

### 4-1. 変更 4: ドラッグのしきい値（`src/gui/timeline/timeline_view.py`）

`mousePressEvent` は押した座標を覚え、「動いた」判定を持つ。

```python
        self._drag_clip_id = target.id
        self._drag_anchor_sec = self.x_to_sec(pos.x())
        self._drag_origin = span[0]
        self._press_pos = pos          # クリックとドラッグの区別に使う
        self._drag_moved = False       # しきい値を超えて実際に動かしたか
```

`mouseMoveEvent` はしきい値を超えるまで何もしない。

```python
        # クリックとドラッグの区別。しきい値は Qt のプラットフォーム値に任せる
        # (設定項目を増やさない / 20260812 resolve.md §4-1)
        if not self._drag_moved:
            if (pos - self._press_pos).manhattanLength() < QApplication.startDragDistance():
                return
            self._drag_moved = True
```

`mouseReleaseEvent` は動いていなければコマンドを積まない。

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

`__init__`（`timeline_view.py:286-310`）へ次の 2 つを追加する。

```python
        self._press_pos = QPoint()         # 押した座標 (クリック/ドラッグの判定に使う)
        self._drag_moved = False           # しきい値を超えて実際に動かしたか
```

import の追加も必要（現状はどちらも未 import）。

* `from PySide6.QtCore import QLine, QPoint, QRect, Qt, Signal` … `QPoint` を追加
* `from PySide6.QtWidgets import (..., QApplication, ...)` … `QApplication` を追加（アルファベット順で先頭）

移動・トリムの両方に同じ判定が効くため、端をクリックしただけで尺が変わることも無くなる。

### 4-2. 変更 5: `MoveClip` の no-op ガード（`src/timeline/commands.py`）

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

変更 4 が根本対処、変更 5 は他の呼び出し経路（コマンドを直接使うコード・将来の追加）に対する保険。
`_clamp_position()` 自体は実ドラッグ時の重なり回避として現状のまま残す（Q2: 重なりは禁止）。

---

## 5. `setting.json` 定義

**変更なし**（Q4）。

| 使う値 | 出所 |
|---|---|
| `subtitle.display_max_hold_sec` / `display_min_duration_sec` | 既存。意味も変えない |
| クリップ終端の打ち切りしきい値 | 既存定数 `timeline_builder._MIN_SEGMENT_SEC`（0.01 秒） |
| ドラッグ開始のしきい値 | `QApplication.startDragDistance()`（Qt のプラットフォーム値・既定 10px） |

---

## 6. エラー処理方針

| 事象 | 扱い |
|---|---|
| プレビュー用音声（`asr_audio.m4a`）の抽出失敗 | **字幕生成は続行**（従来は字幕を空にしていた）。プレビューが都度生成に切り替わるだけ |
| 音声認識の失敗 | 従来どおり空字幕で続行（クリップ全損を避ける） |
| 写像後に字幕が 0 件 | 警告ログを出して空字幕で続行 |
| `keep_segments` が全長 1 区間（無音カット無効） | 写像は恒等変換になる。分岐は設けない |
| クリップ終端の打ち切りで残らない字幕 | 件数をログに出して載せない（例外にしない） |

---

## 7. ログ出力方針

追加・変更するログ（いずれも INFO、既存の粒度に合わせる）。

```
字幕時刻を Timeline の軸へ写像: 49 → 47 件 (カット区間のため 2 件除外)
clip1: クリップ終端で打ち切った字幕 1 件 / 載せなかった字幕 1 件
```

既存の「テロップ表示タイミング整形: N → M 区間」「音声認識完了 (Timeline 用 N 件)」は残す。
これにより **認識 → 写像 → 整形 → 配置** の各段の件数がログだけで追える。

---

## 8. 影響範囲・互換性

| 箇所 | 影響 |
|---|---|
| アーカイブ用 Timeline | 症状 A・B・C が解消 |
| クリップ用 Timeline（`pipeline_runner._run_timeline`） | 変更 1 が効き、字幕時刻の精度が上がる（Q6）。契約は不変のため呼び出し側の変更なし |
| 従来画面経路（`timeline_review=false` / `_transcribe`） | **変更なし**。実カット後ファイルの軸で認識する現行仕様のまま |
| 処理時間 | 認識対象が「残す区間の合計」→「素材全長」になる。clip1 実測で 54.4s → 181.1s（約 3.3 倍）。今回の実行（clip あたり約 2.5 分）から **10 クリップで +20〜30 分**（Q3 で許容） |
| 幻聴字幕 | 無音を含む音声を認識するため増える方向だが、カット区間へ落ちた字幕は写像で除外されるため、**カットした無音への幻聴は構造的に落ちる**（`whisper_vad_filter=true` も継続） |
| プレビュー初回再生の再利用（§6.4-5） | 維持（`asr_audio.m4a` は作り続ける） |
| `origin.source_start` / `source_end` | 正しい時刻から逆算されるようになる |
| Resolve 出力（`to_export_entry`） | 打ち切り後の時刻がそのまま出る。処理の変更は不要 |
| 既存の出力済み動画 | 補正しない（Q5） |
| プロジェクト JSON | スキーマ変更なし。マイグレーション不要 |

---

## 9. テスト計画

### 9-1. 新規: `tests/test_subtitle_timeline_map.py`

`map_items_to_timeline()` の単体テスト（FFmpeg・Whisper を起こさない純関数）。

| # | 前提 | 期待 |
|---|---|---|
| 1 | `keep=[(0,30)]`（無音カット無効相当） | 時刻が変わらない（恒等） |
| 2 | `keep=[(0,5),(10,15)]`、item `(1,3)` | `(1,3)` のまま |
| 3 | 同上、item `(12,14)` | `(7,9)` へ写る |
| 4 | 同上、item `(4,11)`（カットを跨ぐ） | `(4,6)` の 1 区間へまとまる（分割されない） |
| 5 | 同上、item `(6,9)`（完全にカット区間） | 除外され `dropped=1` |
| 6 | 同上、item `(4,7)`（部分的にカット） | `(4,5)` へ短縮 |

### 9-2. 追記: `tests/test_archive_timeline.py`

| # | 内容 |
|---|---|
| 7 | クリップ尺 10 秒に対し item `(9.5, 12.0)` を与えると、字幕が `timeline_end == clip_end` で打ち切られる |
| 8 | item `(10.5, 12.0)`（完全に外）は載らない |
| 9 | 3 クリップぶんを構築したとき、**字幕トラック内に重なりが 1 件も無い**（Q2 の保証） |
| 10 | `split_by_clip()`: 境界を跨ぐ字幕が両グループへ、それぞれ見えている分だけ入る |
| 11 | 既存テスト（TOP 順・0 起点・enabled=False の除外・`to_export_entry`）が変わらないこと |

### 9-3. 追記: `tests/test_timeline_commands.py`

| # | 内容 |
|---|---|
| 12 | 重なった 2 件の字幕に対し `MoveClip(id, 現在の timeline_start)` が `False` を返し、位置が変わらない |
| 13 | 実際に別位置を指定した移動では従来どおり `_clamp_position` が働く（回帰） |

### 9-4. 回帰

```
python -m unittest discover -s tests
```

`tests/test_archive_prepare.py` は `recognize_for_timeline` をモックしているため影響を受けない見込みだが、
`_prepare_edit_points` の戻り値の形が変わらないことを確認する。

### 9-5. 手動確認

1. 同じ VOD（`2833484601`）でアーカイブ用切り抜きを実行する。
2. Timeline 編集画面で採点グラフのマーカから各クリップ境界へ飛び、
   **前クリップのテロップが次クリップの冒頭に残っていない**ことを確認する。
3. 重なりが無いこと、テロップを**クリックしても動かない**ことを確認する。
4. 再生して発話とテロップのタイミングが合っていること（特にクリップ後半）を確認する。
5. 書き出した動画で、クリップの先頭に前クリップのテロップが焼かれていないことを確認する。
6. ログで「写像: N → M 件」「打ち切った字幕 N 件」が出ていることを確認する。
7. クリップ用（通常の 1 本編集）でも字幕がずれていないことを確認する（Q6 の回帰）。

---

## 10. 実装手順

1. `src/modules/subtitle_generator.py`
   1. `from ..timeline.timemap import TimeMap` を追加。
   2. `map_items_to_timeline()` を追加（§2-1）。
   3. `recognize_for_timeline()` を §2-2 の (a)(b)(c) に従って変更し、関数コメントを更新。
2. `src/archive/timeline_builder.py`
   1. `_append_subtitles()` に `clip_duration` を追加し打ち切りを実装（§3-1）。
   2. 呼び出し側で `cursor - clip_start` を渡す。
   3. `split_by_clip()` の字幕振り分けを範囲交差へ変更（§3-2）。
3. `src/gui/timeline/timeline_view.py` にドラッグしきい値を実装（§4-1）。
4. `src/timeline/commands.py` の `MoveClip.apply()` へ no-op ガードを追加（§4-2）。
5. テストを追加・追記（§9-1〜9-3）し、`python -m unittest discover -s tests` を全件グリーンにする。
6. §9-5 の手動確認を実施する。

変更 1（症状 C）と変更 2〜5（症状 A・B）は独立しているため、
2 → 3 → 4 → 1 の順に分けて確認しても良い。処理時間が増える変更 1 を最後に回すと切り分けやすい。

---

## 11. 残存する既知の誤差・本件対象外の事項

| # | 事象 | 扱い |
|---|---|---|
| 1 | `asr_audio.m4a` 先頭の AAC プライミング約 21ms | **許容**（累積しない定数。プレビュー音声にしか使わなくなるため影響はさらに小さい） |
| 2 | `_cut_region()` の `-ss` + `-c copy` によるキーフレーム丸め（要求 180s に対し実尺 181.1s） | **本件対象外**。`docs/error/20260811/resolve.md` §10-2 のまま。採点グラフのマーカと実映像の対応がずれる別件 |
| 3 | 素材そのままの認識でも残る Whisper 自体の時刻誤差 | **許容**。参照側として最も精度が高い条件であり、これ以上は認識器の性能の問題 |
| 4 | 従来画面経路（`timeline_review=false`）の字幕時刻 | 変更しない。実カット後ファイルの軸とプレビューが一致しており、その経路では整合している |

---

## 12. 未確定事項（実装前に判断が必要なもの）

`docs/CLAUDE.md`「不明点がある場合は推測実装せず設計書へ記載すること」に従い記載する。
Q1〜Q6 の回答は `Analyze.md` §9-1 に反映済みで、残るのは下記のみ。

| # | 論点 | 本書の想定 | 確認したいこと |
|---|---|---|---|
| R1 | 一部がカット区間へかかる字幕の扱い | `split_interval()` の既定挙動に任せ、**かかった分だけ短縮**する（全体が落ちた場合のみ除外） | 「カットに少しでもかかる字幕は丸ごと落とす」方針にしたい場合は判定を追加する |
| R2 | 境界を跨ぐ字幕を書き出しで両クリップへ出すか | **両方へ、見えている分だけ出す**（Timeline の見た目と一致させる） | 「開始側のクリップにのみ出す」方針なら §3-2 を変えずに打ち切りだけ足す形にする |
| R3 | プレビュー用 `asr_audio.m4a` をアーカイブ用でも作り続けるか | **作り続ける**（`recognize_for_timeline` を分岐させず、実装を単純に保つ）。clip あたり約 5 秒 | アーカイブ用は初回再生の再利用をしていないため、クリップ 10 本で約 50 秒を削りたい場合は引数で切り替える |
