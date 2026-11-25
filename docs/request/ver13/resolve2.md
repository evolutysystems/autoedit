# ver13 resolve2 — 無音カット無しの追加で範囲を連続に戻す / 切り出し原点の記録 設計

要望: `docs/request/ver13/request2.md` / 前提: `docs/request/ver13/resolve1.md`

本書は `docs/CLAUDE.md`「実装前に設計を行うこと」に従う **設計書** である。

---

## 1. 結論

| # | 直し方 |
|---|---|
| A | **無音カットを外して追加したとき、指定範囲にある既存セクションの穴を「既存素材から」埋める。** 取り直し (ダウンロード・ffmpeg) は行わない。無音カットは編集点方式であり、**素材には切った区間がそのまま残っている**ため、V1 へクリップを足すだけで戻せる |
| C | **`cut_region` の原点を `cut_origin.measure` で実測し、素材の VOD 原点として記録する。** あわせてセクション追加では要求位置より手前の余りを `source_in` で落とし、隣のクリップと内容が重ならないようにする |

A は再取得しないため**一瞬で終わる**。今回の 1.4 秒の追加は取得だけで 33 秒かかっていた。

## 2. A — 穴を既存素材から埋める

### 2.1 なぜ取り直しが要らないか

無音カットは `silence_cut.mode = "edit_points"` (編集点方式) である。
V1 に載るのは「残す区間」だけだが、**素材 (`<project>.media/clipN/normalized.mp4`) は
無音カット前の全長**である (`timeline_builder` の `normalized_path` / ver3 resolve5 §3-2)。

つまり穴は「素材の中にあるが V1 に置いていない区間」であり、
`source_in` / `source_out` を指定したクリップを足すだけで戻る。

```
素材 m5 (VOD 3240.0 始まり / 180 秒)
 V1: [====]    [==]      [===]        ← 残した区間
     ↑穴↑  ↑ 穴 ↑   ↑ 穴 ↑          ← ここを足す (同じ素材を参照するだけ)
```

**隣のクリップの `source_out` と次のクリップの `source_in` をそのまま使う**ため、
座標変換が一切要らず、継ぎ目がずれようがない。

### 2.2 どこを穴とみなすか

素材 1 本ごとに「その素材が V1 で使っている区間」の**補集合**を `[0, 素材の尺]` の中で取る。
`used_vod_ranges` と違い、**穴の統合 (`used_gap_merge_sec`) は行わない**。
あれは「追加ダイアログで取り直す価値のある穴か」を決める値であり、
取り直さない本経路では短い穴も戻せる (要望 B がここで解消する)。

* 先頭・末尾の穴も対象にする (冒頭と末尾の無音も切られているため)。
* 複数のセクションが同じ素材を使っていることがある (統合後)。
  足したクリップの `archive_clip_index` は**隣のクリップのもの**を継ぐ。
* 素材が Timeline に無い (媒体を手放した) 場合は対象外。従来どおり取り直しへ回す。

### 2.3 テロップ

戻した区間に**テロップは付かない**。音声認識は「残す区間」に対してだけ走っており
(`subtitle_generator.recognize_for_timeline(ctx, keep_segments)`)、穴の中の発話は
認識していないためである。

穴は「`noise_threshold_db` を `cut_min_silence_sec` 以上下回った区間」であり、
原則として発話が無い。小声が混じる場合に備えて**穴を戻した件数と合計秒をログと画面に出す**。
戻した区間の再認識は本書の範囲外とする (§6 P1)。

### 2.4 取り直しとの役割分担

無音カットを**外した**追加では、指定範囲を次の 2 つに分ける。

| 指定範囲の内訳 | 扱い |
|---|---|
| 既存セクションの素材が覆っている所 (= 穴) | **既存素材から戻す** (取得なし) |
| どのセクションも覆っていない所 | 従来どおり取り直して用意する |

`plan_section_add` が返す `ranges` から、戻せる穴のぶんを差し引いてから取得へ回す。
無音カットを**入れた**追加は従来どおり (穴埋めは行わない)。

### 2.5 実装

| 追加するもの | 場所 |
|---|---|
| `plan_gap_fill(timeline, start, end)` → 戻せる穴の一覧 | `archive/timeline_builder.py` |
| `RestoreArchiveGaps` コマンド (スナップショット方式。`AddArchiveSection` と同じ作り) | `archive/timeline_builder.py` |
| 呼び出し (無音カット OFF のときだけ) と件数の案内 | `gui/timeline/archive_timeline_dialog.py` |

穴の表現:

```python
{"media_id": "m5", "clip_index": 5,
 "source_in": 53.6, "source_out": 70.2,      # 素材内の秒
 "vod_start": 3293.6, "vod_end": 3310.2,     # 案内とログ用
 "after_clip_id": "c41"}                     # この V1 クリップの直後へ入れる
```

挿入は `AddArchiveSection._make_room` と同じく `commands.make_room_for_range` で
場所を空けてから行う。音声クリップも併せて足す。

## 3. C — 切り出し原点を実測して記録する

### 3.1 何が間違っているか

```python
# archive/clip_writer.py:77
cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", input_path, "-t", f"{duration:.3f}",
       "-c", "copy", "-avoid_negative_ts", "make_zero", dest]
```

`-c copy` はシーク点と要求位置の間を捨てられないため、**素材の先頭は要求位置ではなく
その手前 (Twitch VOD なら最大 1 GOP ≒ 2 秒)** になる。これは
`archive/cut_origin.py` の冒頭が明記している既知の性質であり、
Resolve 出力 (`export/resolve_export.py:371`) と素材の張り替え (`archive/vod_rebase.py:57`)
は `cut_origin.measure` で実測して補正している。

ところが素材の復元情報を書く 2 か所は**要求位置をそのまま記録している**。

| 場所 | 現在 |
|---|---|
| `timeline_builder.build_archive_timeline` (`media_meta`) | `vod_start = entry["start"]` |
| `timeline_builder.AddArchiveSection._insert_one` (`put_archive_media`) | 同上 |

`media_vod_ranges` のコメント「`cut_region` は `-c copy` のため素材の先頭 = 切り出し開始位置と
一致する」は誤りであり、`cut_origin.py` の記述と矛盾している。**`cut_origin.py` が正しい。**

### 3.2 影響

| 使う側 | 影響 |
|---|---|
| `plan_section_add` / `used_vod_ranges` | 使用中の区間が最大 2 秒ずれる。穴の位置がずれる |
| `ai/plan_applier._vod_offset` | **AI のテロップを置く基準がずれる**。2026-11-25 のテロップずれの一因 (実測で最大 1.05 秒) |
| Resolve 出力 / 素材の張り替え | 影響なし (独自に実測している) |
| 素材の中身・テロップの時間軸 | **影響なし**。素材内の秒で完結しているため |

### 3.3 直し方

**測って記録するだけ。素材もテロップの軸も変えない。**

1. `clip_writer.prepare_one_clip` が `cut_region` の直後に実測する。

```python
# -c copy の切り出しは要求位置の手前から始まる (archive/cut_origin.py)。
# 素材内の秒 → VOD の秒 に使う原点はここで実測した値が唯一正しい。
measured = cut_origin.measure(input_path, clip["start"] - origin, ffmpeg_cfg)
entry["media_vod_start"] = origin + measured
```

   * `measure` は測れなければ要求位置をそのまま返す (処理は止まらない)。
   * ffprobe 1 回 (`-read_intervals` 0.3 秒ぶん) で、クリップあたり数百ミリ秒。

2. 記録する 2 か所が `entry["media_vod_start"]` を使う。無い場合 (古い呼び出し・テスト) は
   `entry["start"]` へ落とす。`vod_end` は要求どおりの終端のままにする
   (素材が覆うのは `[実測原点, 要求の終端]` である)。

3. `media_vod_ranges` の誤ったコメントを直す。

### 3.4 セクション追加だけは余りを落とす

記録を直しても、追加した素材は要求位置より手前の映像を含んだままである。
単独のセクションなら「数秒の助走」で済むが、**既存セクションの穴へ入れる場合は
隣のクリップと内容が重なり、同じ場面が 2 回流れる**。

`AddArchiveSection._insert_one` で、素材内の秒 `head = entry["start"] - media_vod_start`
より前を落とす。

```python
segments = [(max(s, head), e) for s, e in segments if e - max(s, head) > _MIN_SEGMENT_SEC]
```

`segments` は `append_subtitles` の `TimeMap` にもそのまま渡るため、
**クリップと字幕が同じ区間列を見る**という 20261124 resolve §4 R4 の決まりを守れる。

`build_archive_timeline` (通常の切り抜き) では落とさない。既存の仕上がりを変えないため。

## 4. 採らなかった案

| 案 | 却下理由 |
|---|---|
| A: 穴も取り直して素材を作る | ダウンロードと ffmpeg が穴の数だけ走る。今回の例では 19 箇所。既存素材に同じ映像があるのに取り直す意味が無い |
| A: 「穴を埋める」を独立のメニューにする | 要望は「無音カットを外して追加したら連続になってほしい」。操作を増やさない |
| A: 無音カット ON でも穴を埋める | ON は「切ってよい」という指定である。埋めると指定と逆になる |
| C: `-c copy` をやめて再エンコードで正確に切る | 180 秒のセクションで数十秒かかるようになる。原点を測れば足りる |
| C: `build_archive_timeline` でも余りを落とす | 既存の切り抜きの尺が全クリップ数十〜百ミリ秒変わる。要望と関係しない所を動かさない |
| C: `media_vod_ranges` の側で補正する | 補正の責務が散る。記録する側を正しくするのが筋 |

## 5. 変更するファイル

| ファイル | 変更 |
|---|---|
| `src/archive/clip_writer.py` | `prepare_one_clip` が原点を実測し `entry["media_vod_start"]` へ入れる |
| `src/archive/timeline_builder.py` | `plan_gap_fill` / `RestoreArchiveGaps` 新設 / `media_meta` と `put_archive_media` が実測原点を使う / `_insert_one` が余りを落とす / 誤ったコメントの訂正 |
| `src/gui/timeline/archive_timeline_dialog.py` | 無音カット OFF のとき穴を埋める。取り直す範囲から穴を差し引く。件数の案内 |
| `src/i18n/catalog_en.py` | 新しい文言の英訳 |
| `tests/test_ver13_gap_restore.py` | **新規**。下記 |

設定の追加は無い。

## 6. テスト

| # | 確認 |
|---|---|
| T1 | `plan_gap_fill` が V1 の穴を素材内の秒で返す (先頭・中間・末尾) |
| T2 | 指定範囲の外の穴は返さない。またがる穴は指定範囲で切り取る |
| T3 | 復元情報の無い素材は対象外 |
| T4 | `RestoreArchiveGaps` が V1 と A1 へクリップを足し、尺のぶんだけ後ろをずらす |
| T5 | 穴を埋めたあと、その素材の使用区間が連続になる (穴が無くなる) |
| T6 | 字幕は増えない (穴に認識結果が無いため) |
| T7 | Undo で元へ戻る (スナップショット方式) |
| T8 | `prepare_one_clip` が `media_vod_start` を入れる。測れないときは要求位置 |
| T9 | `build_archive_timeline` の `media` 行が `media_vod_start` を使う。無ければ `start` (回帰) |
| T10 | `AddArchiveSection` が要求位置より手前の余りを落とす |
| T11 | 余りが無い (実測 = 要求) ときは従来と同じ区間になる (回帰) |
| T12 | 無音カット ON の追加では穴を埋めない |

## 7. 設計だけ (実装は保留)

| 項目 | 内容 |
|---|---|
| P1 | 戻した穴の再認識。穴は原則として無音だが、小声が混じることがある。「戻した区間だけ文字起こしし直す」を別途設計する |
| P2 | `build_archive_timeline` 側の余りの扱い。AI 編集の素材も同じ助走を持つため、AI のテロップ配置を詰めるときに再検討する |
