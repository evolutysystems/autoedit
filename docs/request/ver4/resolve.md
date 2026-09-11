# resolve（ver4） — 画像クリップの左端トリム不具合修正 詳細設計書

## 0. 本書の位置づけ

* 対象: `D:/develop/StretheusPlan/plan.md` の **P0「画像クリップの左端トリム不具合修正」**
  （基本設計。本書はその詳細設計にあたる）
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「実装前に設計を行うこと」）。
* 調査は **2026-09-11 時点の実コード**を読んで行った。
  plan.md の記述と実コードが食い違う箇所が 1 つあり、§2.4 で訂正している
  （画像クリップの `source_in` は常に 0 ではない）。
* 要望から一意に決められない論点は §9「確認事項」へ挙げた
  （`docs/claude.md`「不明点がある場合は推測実装せず設計書へ記載すること」）。
* 変更は **`src/timeline/commands.py` の 1 ファイル + テスト**に閉じる。
  `setting.json` の変更は無い（§7）。

---

## 1. 要望と要件 ID

| ID | 内容 | 種別 |
|---|---|---|
| **T1** | 画像クリップの**左端**をドラッグして、Timeline の前方（0 秒側）へ**伸ばせる**ようにする | 不具合修正 |
| **T2** | 伸ばす場合も**直前クリップの終端**は越えない | 既存制約の維持 |
| **T3** | 縮める場合も**最小尺 `min_clip_sec`** は割らない | 既存制約の維持 |
| **T4** | 開始位置は **0 秒未満にならない** | 既存制約の維持 |
| **T5** | **動画クリップ**の左トリムは従来どおり**素材の先頭（`source_in >= 0`）で止まる** | 回帰防止 |

---

## 2. 現状分析

### 2.1 症状

* 画像クリップは**右端は自由に伸ばせる**が、**左端は前方へ伸ばせない**（縮めることはできる）。
* ドラッグ中はクリップが伸びて見えるのに、**マウスを離すと元の位置へ戻る**。
* 分割（編集点の追加）した後半の画像クリップだけは**少しだけ**左へ伸びる、という一貫しない挙動もある（§2.4）。

### 2.2 原因のコード

```python
# src/timeline/commands.py:578-599（TrimClip._trim_left）
def _trim_left(self, timeline, track, clip):
    target = float(self._new_value)
    # 直前クリップの終端より前へは伸ばせない
    previous_end = max(
        (c.timeline_end for c in track.clips
         if c.id != clip.id and c.timeline_end <= clip.timeline_start + _EPS),
        default=0.0,
    )
    target = max(target, previous_end)
    # 素材の先頭より前へは伸ばせない (字幕クリップは素材を持たないため無制限)
    if isinstance(clip, Clip):
        target = max(target, clip.timeline_start - clip.source_in)   # ← 原因
    # 最小尺を割らない
    target = min(target, clip.timeline_end - self._min_clip_sec)
    delta = target - clip.timeline_start
    if abs(delta) <= _EPS:
        return False
    clip.timeline_start = target
    clip.duration -= delta
    if isinstance(clip, Clip):
        clip.source_in += delta
    return True
```

* 画像クリップも `Clip` クラスである（`model.py:210`。映像と画像を型では分けていない）。
  そのため 588-589 行の「素材の先頭クランプ」が画像にも掛かる。
* 画像を置く `AddMediaClip` は `source_in=0.0` で生成する（`commands.py:789`）。
  このとき `clip.timeline_start - clip.source_in = clip.timeline_start` となり、
  **現在の開始位置より前へは 1 フレームも動かせない**。

具体例（画像クリップ: 開始 5.0 秒 / 尺 5.0 秒 / `source_in = 0.0`、左端を 2.0 秒へドラッグ）:

| 手順 | 値 |
|---|---|
| `target = 2.0` | 要求値 |
| `max(2.0, previous_end=0.0)` | 2.0 |
| `max(2.0, 5.0 - 0.0)` | **5.0**（ここで元の位置へ戻される） |
| `delta = 5.0 - 5.0 = 0` | `return False` → コマンドは積まれず、何も起きない |

### 2.3 右端との非対称

右端は素材の上限を `_source_limit()` から引いており、画像では上限が無い。

```python
# src/timeline/commands.py:242-247
# 素材のトリム上限 (画像は上限なし)
def _source_limit(timeline, clip):
    media = timeline.media_by_id(clip.media_id)
    if media is None:
        return None
    return media.max_source_sec()

# src/timeline/model.py:167-175
def is_image(self):
    return self.kind == MEDIA_IMAGE

def max_source_sec(self):
    if self.is_image():
        return None          # 画像は上限なし
    return self.duration_sec
```

**右端は「画像なら素材の制限なし」を判定しているのに、左端はしていない。**
これが「右は伸ばせるが左は伸ばせない」の正体である。plan.md の分析と一致する。

### 2.4 画像クリップの `source_in` は 0 とは限らない（plan.md の訂正）

plan.md は「画像クリップは `source_in=0.0` 固定」「修正後も `source_in` は常に 0 のまま据え置く」
としているが、**実コードでは画像クリップの `source_in` は 0 以外になり得る**。

| 経路 | 位置 | `source_in` の変化 |
|---|---|---|
| 分割（編集点の追加） | `commands.py:277` `new_clip.source_in = clip.source_in + offset` | 後半クリップは `offset` 秒になる |
| リップル削除（規則 5: 区間が尻だけかかる） | `commands.py:352-354` | `source_in += (end - clip_start)` |
| 左端を**縮める**トリム | `commands.py:597-598` | `source_in += delta` |

いずれも「種別（映像/画像/字幕）で処理を分けない」方針（`commands.py:313`）で書かれており、
画像クリップでも `source_in` を動かしている。

§2.2 の症状のうち「分割後の後半だけ少し伸びる」はこれで説明できる。
`source_in = 2.0` の画像クリップは、`timeline_start - 2.0` まで（= 2 秒ぶんだけ）左へ伸ばせてしまう。

**したがって「画像は `source_in` を 0 に固定する」前提は置けない。**
本書では「画像の `source_in` は**触らない（据え置く）**」と読み替える（§3-2）。

### 2.5 画像の `source_in` はどこからも使われていない

画像クリップの `source_in` を読む処理を全件確認した。**描画・出力のどちらも画像では参照していない。**

| 処理 | 位置 | 画像の扱い |
|---|---|---|
| 出力（V1 の抽出） | `renderer.py:230-233` | `-loop 1 -t <尺>` のループ入力。`-ss source_in` は**動画の分岐だけ** |
| 出力（V2 以降のオーバーレイ） | `renderer.py:537-539`, `562-563` | `-loop 1 -t <timeline_end>` + `enable=between(t,start,end)`。`source_in` は**動画の分岐だけ** |
| プレビューの描画 | `preview_panel.py:467-469` | `QPixmap(media.path)` をそのまま使う。`source_in` は**動画の分岐だけ** |
| プロジェクト読込時の検証 | `project_io.py:415-419` | `source_out <= source_in` のクリップを除外する。**差（= 尺）しか見ていない** |
| クリップ情報の表示 | `timeline_editor_dialog.py:1026` | 「素材内 x〜y 秒」を表示するだけ |

`timemap.py` / `audio_source.py` / `waveform.py` / `resolve_export.py` はベース映像（V1 の本編動画）か
音声付き動画だけを対象にしており、画像は通らない（`AddMediaClip._resolve_track` は画像を V1 へ置かない / `commands.py:810-827`）。

**結論: 画像クリップの `source_in` は、左トリムでどう扱っても出力結果・プレビューに影響しない。**
守るべき不変条件は `project_io` の検証が見ている **`source_out - source_in == duration`（> 0）** だけである。

### 2.6 ドラッグ中のプレビュー

```python
# src/gui/timeline/timeline_view.py:690-693（mouseMoveEvent / _DRAG_TRIM_LEFT）
elif self._drag == _DRAG_TRIM_LEFT:
    start = min(sec, clip.timeline_end - self._controller.cfg["min_clip_sec"])
    self._drag_preview = {"id": clip.id, "start": start,
                          "duration": clip.timeline_end - start}
```

プレビューは**最小尺しか見ておらず**、素材の先頭も直前クリップも見ていない。
マウスを離したとき（`timeline_view.py:727-728`）に初めて `TrimClip` が制約を掛ける。

* 画像の場合: 現状は「プレビューでは伸びる → 離すと戻る」。**修正後は一致する**（直前クリップを越えた場合を除く / §3-3）。
* 動画の場合: 現状どおり「素材の先頭を越えてプレビューが伸びる → 離すとそこで止まる」。本書では変えない。

UI 層（`timeline_view` → `timeline_controller.trim_clip` → `commands.TrimClip` / `timeline_controller.py:161-163`）は
値を素通しするだけで、**直すべき箇所は `TrimClip._trim_left` の 1 か所**である。

### 2.7 既存テストの状況

`tests/test_timeline_commands.py` の `TrimTest`（109-156 行）は**すべて V1 の動画クリップ**を対象にしている。
画像クリップのトリムを検証するテストは無い。

また、左トリムの回帰テスト `test_trim_left_stops_at_source_head`（138-140 行）は
`c1`（開始 0 秒 / `source_in = 0`）を使っており、
**「直前クリップ無し → 0 秒」と「素材の先頭 → 0 秒」が同じ値になる**。
このため「素材の先頭クランプ」を消しても通ってしまい、T5 の回帰テストとしては不十分である（§10-2 で補う）。

2026-09-11 時点で `python -m unittest tests.test_timeline_commands` は **79 件すべて成功**する。

---

## 3. 方式選定

### 3-1. 画像の判定方法

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | `timeline.media_by_id(clip.media_id)` で `MediaRef` を引き、`is_image()` で判定する | `_source_limit()` と同じ流儀。判定の根拠を素材（`MediaRef.kind`）1 か所に保つ |
| B | `_source_limit(timeline, clip) is None` を「素材先頭の制限なし」とみなす | 素材が見つからない（`media is None`）ときも `None` になり、**動画クリップの制限まで外れる**。意味が混ざる |
| C | `Clip` に `is_image` 属性を持たせる | 保存形式・`copy()`・`from_dict` まで波及する。修正の規模に見合わない |

**A を採用する。** 素材が見つからない（`media is None`）ときは**動画として扱い、従来の制限を残す**（安全側に倒す）。

### 3-2. 画像クリップの `source_in` の扱い

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | **`source_in` は動かさない**。`source_out = source_in + duration` だけ再計算する | §2.5 のとおり画像の `source_in` は誰も読まない。不変条件（`source_out - source_in == duration`）を保つ最小の変更 |
| B | `source_in = 0.0` にリセットする | 結果は同じだが、トリムのたびに**無関係な値まで書き換える**。分割・リップル側（§2.4）とも揃わない |
| C | 動画と同じく `source_in += delta` のまま | 左へ伸ばすと `source_in` が**負**になり、保存ファイルに負の素材時刻が残る。意味が壊れる |

**A を採用する。** plan.md の「`source_in` は常に 0 のまま据え置き」は、
§2.4 の事実を踏まえて「**`source_in` は据え置き（値は問わない）**」と読み替える。

副作用として、画像の左端を**縮める**ときも `source_in` が動かなくなる（現状は `+= delta`）。
§2.5 のとおり出力・プレビューには影響しない。表示上の違いは §8 に記す。

### 3-3. ドラッグ中のプレビュー側を直すか

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | **直さない**。`TrimClip` だけを修正する | 画像の「伸びて見えるのに戻る」は `TrimClip` 側の修正で解消する（§2.6）。直前クリップ越え・動画の素材先頭越えでプレビューと確定位置がずれるのは**動画・右端も含めた既存挙動**で、P0 の不具合とは別件 |
| B | 制約計算を関数へ出し、プレビューと `TrimClip` で共有する | 左右トリム・動画/画像/字幕すべての見た目が変わる。P0（XS）の範囲を超える（§11 F1 に残す） |

**A を採用する**（§9 Q2 で確認する）。

---

## 4. 設計方針

1. **原因の 1 行だけを直す**。画像クリップでは「素材の先頭クランプ」を掛けない（§3-1）。
2. **残す制約は 3 つ**: 直前クリップの終端（T2）/ 最小尺（T3）/ 0 秒以上（T4）。
   T4 は `previous_end` の既定値 `0.0` が既に担保しているため、新たなコードは要らない。
3. **動画・字幕・右端トリムの挙動は変えない**（T5 / `docs/claude.md`「既存実装を破壊しない」）。
4. 画像の `source_in` は動かさず、`source_out` だけを尺に合わせる（§3-2）。
5. 設定値は増やさない。誤った制約を外すだけであり、調整すべき値が無い（§7）。

---

## 5. 詳細設計

### 5.1 変更ファイル一覧

| ファイル | 変更内容 | 規模 |
|---|---|---|
| `src/timeline/commands.py` | `_is_still_image()` の新設、`TrimClip._trim_left` の分岐追加、コメント更新 | 約 15 行 |
| `tests/test_timeline_commands.py` | 画像クリップの左トリムテスト（新クラス）と、動画の回帰テストの追加 | 約 90 行 |

**UI（`timeline_view.py` / `timeline_controller.py`）・レンダラ・保存形式は変更しない。**

### 5.2 `src/timeline/commands.py`

#### (1) 静止画判定ヘルパの新設

`_source_limit()`（242-247 行）の直後に置く。判定の流儀を揃えるためである（§3-1）。

```python
# 静止画クリップか (素材内の時間を持たず、尺を前後どちらへも伸ばせる素材か)
# 素材が見つからないときは False (= 動画として扱い、素材の制限を残す安全側 / ver4 resolve §3-1)。
# 字幕クリップは素材を持たないため False。
def _is_still_image(timeline, clip):
    if not isinstance(clip, Clip):
        return False
    media = timeline.media_by_id(clip.media_id)
    return media is not None and media.is_image()
```

#### (2) `TrimClip._trim_left` の修正

```python
    # 左端: 新しい開始時刻へ寄せる。直前クリップの終端を超えない (0 秒未満にもならない)。
    # 動画は素材の先頭 (source_in>=0) も超えない。静止画は素材内の時間を持たないため
    # 素材先頭の制限を掛けない (右端で _source_limit が画像に None を返すのと対称 / ver4 resolve §2.3)。
    def _trim_left(self, timeline, track, clip):
        target = float(self._new_value)
        # 直前クリップの終端より前へは伸ばせない (直前が無ければ 0 秒)
        previous_end = max(
            (c.timeline_end for c in track.clips
             if c.id != clip.id and c.timeline_end <= clip.timeline_start + _EPS),
            default=0.0,
        )
        target = max(target, previous_end)
        still = _is_still_image(timeline, clip)
        # 素材の先頭より前へは伸ばせない (字幕・静止画は素材内の時間を持たないため無制限)
        if isinstance(clip, Clip) and not still:
            target = max(target, clip.timeline_start - clip.source_in)
        # 最小尺を割らない
        target = min(target, clip.timeline_end - self._min_clip_sec)
        delta = target - clip.timeline_start
        if abs(delta) <= _EPS:
            return False
        clip.timeline_start = target
        clip.duration -= delta
        if still:
            # 静止画は source_in を動かさず、source_out だけ尺に合わせる (ver4 resolve §3-2)。
            # source_in は描画・出力のどちらからも参照されない (§2.5)。
            clip.source_out = clip.source_in + clip.duration
        elif isinstance(clip, Clip):
            clip.source_in += delta
        return True
```

差分は次の 3 点だけである。

| # | 変更 | 目的 |
|---|---|---|
| 1 | `still = _is_still_image(timeline, clip)` を追加 | 判定を 1 回にまとめる |
| 2 | 素材先頭のクランプに `and not still` を追加 | **不具合の修正本体**（T1） |
| 3 | 末尾の `source_in` 更新を `still` で分岐 | 画像では `source_in` を据え置き、`source_out` を尺へ合わせる（§3-2） |

クラス冒頭のコメント（`commands.py:557`）も実態に合わせて更新する。

```python
# クリップの左右端をドラッグして尺を変える (R10)
# 左端: source_in と timeline_start を同量動かす (静止画は source_in を動かさない) / 右端: source_out を動かす
```

#### (3) 修正後の値の追跡

§2.2 と同じ例（画像クリップ: 開始 5.0 秒 / 尺 5.0 秒 / `source_in = 0.0`）で、左端を 2.0 秒へ:

| 手順 | 値 |
|---|---|
| `max(2.0, previous_end=0.0)` | 2.0 |
| 素材先頭クランプ | **静止画のため掛けない** |
| `min(2.0, 10.0 - 0.05)` | 2.0 |
| 結果 | 開始 2.0 / 尺 8.0 / `source_in = 0.0` / `source_out = 8.0` |

### 5.3 変わらないもの（意図的に触らない）

| 対象 | 理由 |
|---|---|
| `TrimClip._trim_right` | 画像は既に無制限（`_source_limit` → `None`）。`source_out = source_in + new_duration` も §3-2 と整合している |
| 動画クリップの左トリム | 素材先頭クランプ・`source_in += delta` とも従来どおり（T5） |
| 字幕クリップの左トリム | もともと `isinstance(clip, Clip)` の外で無制限。分岐の結果も同じ |
| `split_clip_at` / リップル（§2.4 の各経路） | 画像の `source_in` を動かしているが、§2.5 のとおり無害。P0 の症状とは無関係 |
| ドラッグ中のプレビュー（`timeline_view.py:690-693`） | §3-3 のとおり。§11 F1 |
| レンダラ・プレビュー描画 | 画像の `source_in` を参照しないため変更不要（§2.5） |
| 保存形式（`project_io`） | キー追加なし。既存プロジェクトはそのまま開ける |

---

## 6. 実装手順

1. `tests/test_timeline_commands.py` に §10-2 のテストを先に追加し、
   **画像の左伸ばし系（#1〜#3, #5）が失敗する**ことを確認する（不具合の再現）。
2. `commands.py` に `_is_still_image()` を追加し、`_trim_left` を §5.2(2) のとおり修正する。
3. `python -m unittest tests.test_timeline_commands` で新規・既存とも成功することを確認する。
4. `python -m unittest discover -s tests` で全体の回帰を確認する。
5. 実機で §10-1 を確認する。

---

## 7. setting.json 定義

**変更なし。**

本修正は「画像には存在しない素材の先頭」という誤った制約を外すものであり、利用者が調整すべき値は生まれない。
残す制約の最小尺は既存の `timeline.min_clip_sec`（`timeline_controller` 経由で `TrimClip` へ渡る）をそのまま使う。

---

## 8. 互換性・非破壊の担保

| 観点 | 影響 |
|---|---|
| 動画クリップの左トリム | **変化なし**。素材の先頭で止まる（§10-2 #7 で担保） |
| 字幕クリップの左トリム | **変化なし** |
| 右端トリム（全種別） | **変化なし** |
| 画像クリップの左端を**伸ばす** | **修正**。直前クリップの終端（無ければ 0 秒）まで伸ばせる |
| 画像クリップの左端を**縮める** | 見た目・出力は同じ。内部の `source_in` が動かなくなる（従来は `+= delta`）。クリップ情報欄の「素材内 x〜y 秒」の表示値だけが従来と異なる（§2.5 / §11 F2） |
| 既存の保存済みプロジェクト | そのまま開ける。`source_in > 0` の画像クリップも、以後は左へ自由に伸ばせる |
| `project_io` の読込検証 | `source_out - source_in == duration > 0` を保つため除外されない |
| Undo / Redo | `CommandStack` のスナップショット方式（`commands.py:95-123`）がそのまま吸収する。追加対応なし |
| リンク音声 | 画像は `has_audio = False` のため音声クリップを持たない（`commands.py:799`）。追従処理は発生しない |
| 素材が見つからないクリップ | 動画として扱い、従来の制限を残す（§3-1） |

---

## 9. 確認事項

| # | 内容 | 本書の判断 |
|---|---|---|
| **Q1** | plan.md の「画像の `source_in` は常に 0 のまま据え置く」は、実コードでは分割・リップルで 0 以外になる（§2.4）。「**値は問わず据え置く**」と読み替えてよいか | 読み替える（§3-2 案A）。0 へのリセット（案B）でも結果は同じだが、無関係な値を書き換えない方を採る |
| **Q2** | ドラッグ中のプレビューが**直前クリップを越えて伸びて見え、離すと直前クリップの終端で止まる**挙動（動画・右端も同様）を、本修正の範囲外としてよいか | 範囲外とする（§3-3 案A）。P0 は「画像が左へ伸びない」ことの修正に絞る。必要なら §11 F1 で別途対応 |
| **Q3** | 画像クリップのクリップ情報欄に出る「素材内 x〜y 秒」（`timeline_editor_dialog.py:1026`）は画像では意味を持たない。表示を変えるか | 本書では変えない（§11 F2） |

いずれも「本書の判断」のまま実装しても P0 の要件（T1〜T5）は満たせるため、**回答待ちで実装を止めない**。

---

## 10. テスト計画

### 10-1. 実機確認

| # | 手順 | 期待 |
|---|---|---|
| 1 | 編集画面で画像を D&D し（V2 に載る）、左端を 0 秒側へドラッグして離す | 離した位置まで伸びたまま残る（**戻らない**） |
| 2 | 同じ画像の左端を、同じトラックの直前の画像クリップより前までドラッグして離す | 直前クリップの終端で止まる |
| 3 | 左端を右へドラッグして縮める | 従来どおり縮む。最小尺より短くはならない |
| 4 | 画像を分割し、後半のクリップの左端を大きく左へドラッグ | 分割位置に関係なく伸ばせる（§2.4 の「少しだけ伸びる」が解消） |
| 5 | 1〜4 の後で Ctrl+Z / Ctrl+Y | 元の位置・尺へ戻り、やり直せる |
| 6 | 伸ばした状態で出力する | 画像が**伸ばした開始時刻から**表示される |
| 7 | 保存 → 開き直す | 伸ばした位置・尺が復元される。クリップが除外されない |
| 8 | V1 の本編動画クリップの左端を左へドラッグ | 従来どおり**素材の先頭で止まる**（回帰なし） |
| 9 | V2 に置いた動画オーバーレイの左端を左へドラッグ | 従来どおり素材の先頭で止まる（回帰なし） |

### 10-2. 追加テスト（`tests/test_timeline_commands.py`）

新クラス `ImageTrimTest` を `TrimTest` の直後に追加する。
`_build()` の Timeline へ画像素材 `m2`（`MediaRef("m2", "image", ..., None, 800, 600, 0, False)`）と
V2 トラックを足し、画像クリップ `i1`（開始 5.0 / 尺 5.0 / `source_in 0.0`〜`source_out 5.0`）を置いた状態から始める。
既存の `AddMediaTest` / `ResizeOverlayTest` と同じ組み立て方にする。

| # | テスト | 検証内容 |
|---|---|---|
| 1 | `test_image_trim_left_extends` | 左端を 2.0 へ → 開始 2.0 / 尺 8.0 / `source_in 0.0` / `source_out 8.0`（T1） |
| 2 | `test_image_trim_left_clamps_at_zero` | 左端を -3.0 へ → 開始 0.0 / 尺 10.0（T4） |
| 3 | `test_image_trim_left_stops_at_previous_clip` | V2 に画像 `i0`（0.0〜3.0）を置き、`i1` の左端を 1.0 へ → 開始 3.0（T2） |
| 4 | `test_image_trim_left_respects_min_clip_sec` | 左端を 9.999 へ → 尺が `_MIN` 以上（T3） |
| 5 | `test_image_trim_left_ignores_source_in` | `source_in = 2.0` の画像クリップ（分割後の後半を模す）の左端を 0.0 へ → 開始 0.0 まで伸び、`source_in` は 2.0 のまま、`source_out == source_in + duration`（§2.4 / §3-2） |
| 6 | `test_image_trim_left_shrink_keeps_source_in` | 左端を 7.0 へ（縮める）→ 開始 7.0 / 尺 3.0 / `source_in 0.0` のまま（§3-2 の副作用の固定） |
| 7 | `test_image_trim_left_undo` | #1 の後で Undo → 開始 5.0 / 尺 5.0 / `source_in 0.0` / `source_out 5.0` に戻る |

`TrimTest` へ回帰テストを 2 件追加する（§2.7 の不足を補う）。

| # | テスト | 検証内容 |
|---|---|---|
| 8 | `test_video_trim_left_stops_at_source_head_with_gap` | V2 に動画 `m1` のクリップ（開始 5.0 / `source_in 3.0`）を置き、左端を 0.0 へ → **開始 2.0 / `source_in 0.0`** で止まる。直前クリップが無く、素材の先頭クランプだけが効く配置にして T5 を確実に検証する |
| 9 | `test_trim_left_unknown_media_keeps_source_clamp` | メディアプールに無い `media_id` のクリップは動画として扱われ、素材の先頭で止まる（§3-1） |

### 10-3. 既存テストの回帰

```
python -m unittest discover -s tests
```

特に `tests/test_timeline_commands.py`（修正前 79 件成功）と、
Timeline を読み書きする `tests/test_timeline_clipboard.py` / `tests/test_media_recovery.py` が通ること。

---

## 11. 将来拡張（本書では実装しない）

| # | 内容 | 備考 |
|---|---|---|
| **F1** | ドラッグ中のプレビューに `TrimClip` と**同じ制約**を掛ける（直前・直後クリップ、動画の素材の先頭・終端） | 制約計算を `commands` 側の関数へ出し、`timeline_view.mouseMoveEvent` と共有する。左右・全種別の見た目が変わるため別途設計する（§3-3 / §9 Q2） |
| **F2** | 画像クリップのクリップ情報欄から「素材内 x〜y 秒」を外す | `timeline_editor_dialog.py:1026` で `media.is_image()` を見て出し分けるだけ（§9 Q3） |
| **F3** | 分割・リップルでも画像の `source_in` を動かさないよう揃える | §2.4 の各経路。無害なため現状は放置で問題ない。揃えるなら `_is_still_image()` を再利用できる |
