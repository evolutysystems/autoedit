# ver9 resolve15 — 縦動画プロジェクトにテロップの構成 (トラックの本数・色) と Timeline 上の配置を引き継ぐ

発端: 2026-10-31 のチャット要望 (`docs/request/ver9/request15.md`)
　　　＋同日の追加要望「Timeline 上のテロップの配置もアーカイブ用切り抜き画面と同じにする」
対象: `src/timeline/vertical_builder.py` (`_copy_subtitles`) / `src/timeline/crop.py` /
　　　`src/settings/setting.json` / `settings_window.py`
調査日: 2026-10-31 (main `111f234` / v1.7.1)
種別: **要望の設計書 (実装前)**。§1 は現行コードと実行結果で確認済み
前提: ver9 resolve13 (テロップ色のトラック指定) / ver9 resolve2 (テロップトラックの追加) /
　　　ver5 resolve9 §5.4 (縦プロジェクトの生成) / ver3 resolve11 §5.9 (書き出しでのトラック複製)

---

## 0. 先に結論

### 0.1 要望 (原文の要点)

| # | 要望 |
| --- | --- |
| R1 | アーカイブ切り抜きから縦動画プロジェクトを作るとき、テロップ本体はそのままでよい |
| R2 | **トラック数**を縦プロジェクトへ反映する |
| R3 | **トラックに設定した色**を縦プロジェクトへ反映する |
| R4 | **Timeline 上のテロップの配置**を、アーカイブ用切り抜きの Timeline 画面と同じにする (追加要望) |

### 0.2 4 行で

1. **縦プロジェクトの生成は、テロップトラックを S1 の 1 本へ潰している。**
   `vertical_builder._copy_subtitles()` が新しい `S1` を 1 本だけ作り、
   元の S1/S2/S3… のテロップを**全部そこへ流し込んでいる** (`vertical_builder.py:208`)。
   トラックの本数も、ヘッダーで決めた色 (`Track.color` / resolve13) も渡っていない。
2. **色だけでなく「時刻」も壊れている。** 1 本へ潰すと S1 と S2 のテロップが同じトラックで
   重なり、開き直したときの `_validate_overlaps` が後ろのものを右へ押し出す。
   実測で **コメントが 1.2 秒 → 3.0 秒へ、サブが 1.4 秒 → 5.0 秒へずれた** (§1.3)。
   要望の対応は、この副作用の修正も兼ねる。
3. **配置 (R4) も、クリップの境目でテロップが切り詰められている。** 同じ関数が
   「最初に重なったクリップの区間だけ採る」(`break`) ため、**無音カットの境目をまたぐ
   テロップが境目で打ち切られる**。実データ 3 本で測ると、全クリップを選んだ
   (= 画面とまったく同じ中身の) 場合でも **27 件 / 46.6 秒**、3 件 / 4.2 秒、
   4 件 / 10.4 秒 が失われていた (§1.6)。
4. **直し方は既にある。** アーカイブの書き出し (`archive/timeline_builder._build_group_timeline`)
   は同じ問題を ver3 resolve11 §5.9 / ver9 resolve13 §3.8 で解決済みで、
   **元のトラック構成を複製し、クリップを元のトラックへ振り分けている**。
   縦プロジェクトはこの対応から漏れていた。同じ流儀をそのまま持ち込む。

### 0.3 何が変わるか

元プロジェクト: S1 (色なし) / S2 Comment / S3 (#2ABCFF) / S4 (#FFEE00・空) の 4 本。

| | 現在 (v1.7.1) | 変更後 |
| --- | --- | --- |
| 縦プロジェクトのテロップトラック | **S1 の 1 本だけ** | S1 / S2 / S3 / S4 の **4 本** |
| トラックの色 | **すべて失われる** (`track_color_map` が空) | S3=#2ABCFF / S4=#FFEE00 を保持 |
| テロップの置き場 | 全部 S1 | 元と同じトラック (コメントは S2) |
| 焼き込み・プレビューの文字色 | 役割の色 (設定) へ戻る | **元と同じ色** |
| テロップの時刻 | 重なりで押し出されてずれる | 元のまま |
| 空のトラック | 作られない | **本数として引き継ぐ** (R2) |
| クリップの境目をまたぐテロップ | **境目で打ち切られる** (尻切れ) | 元の尺のまま (R4) |
| 選択が飛び飛びのとき | 先頭の区間ぶんだけ残る | 区間ごとに分けて、それぞれ正しい位置へ |

---

## 1. 現状の仕組み (調査結果)

### 1.1 1 本へ潰している箇所

```python
# src/timeline/vertical_builder.py:208
def _copy_subtitles(timeline, vertical, offsets, warnings):
    subtitle_track = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    vertical.tracks.append(subtitle_track)      # ← 新しい S1 を 1 本だけ作る

    for track in timeline.subtitle_tracks():    # 元は何本あってもよいが
        for clip in track.clips:
            ...
            subtitle_track.clips.append(moved)  # ← 行き先は常にこの 1 本
```

`Track(...)` に `color=` を渡していないため、トラックの既定色は空文字 = 未指定になる。
これは ver5 resolve9 §5.4 の時点のコードで、**テロップトラックが複数になる前 (ver9 resolve2)
にも、トラック色が入る前 (ver9 resolve13) にも書き換えられていない**。

### 1.2 色の解決は 1 か所に寄っているため、トラックを写せば直る

ver9 resolve13 §3.2 で文字色の優先順は `subtitle_style.resolve_color()` の 4 段
(`クリップ個別 → トラック → 役割(設定) → 既定`) に統一され、焼き込み (`compositor`)・
プレビュー (`preview_panel`)・Resolve 出力 (`resolve_export`)・プロキシ (`proxy_render`) が
すべて `subtitle_style.track_color_map(timeline)` を通っている。

つまり**失われているのは `Track.color` の値そのもの**であって、
下流の処理に手を入れる必要はない。トラックを写せば焼き込みもプレビューも揃う。

### 1.3 実測 (現行コードの振る舞い)

S1 (色なし) / S2 Comment / S3 (#2ABCFF) / S4 (#FFEE00・空) を持つ横 Timeline から
`vertical_builder.build()` を通した結果:

```
=== 縦 Timeline のトラック ===
V1 index=1 clips=[c1, c2]
A1 index=1 clips=[a1, a2]
S1 index=1 name='Subtitle 1' color='' clips=[s1(streamer), s2(comment), s3(streamer)]
track_color_map: {}                     ← トラック色はどこにも残らない

=== 保存 → 開き直し ===
WARNING トラック S1 でクリップ s2 が重複しているため 3.000 へ移動します
WARNING トラック S1 でクリップ s3 が重複しているため 5.000 へ移動します
S1 s1 1.0 2.0 本人
S1 s2 3.0 2.0 コメント      ← 元は 1.2 秒
S1 s3 5.0 2.0 サブ色        ← 元は 1.4 秒
```

- トラックは 4 本 → **1 本**。色は**全消失**。
- 同時刻に重ねていたコメント・サブが、**重なり解消で後ろへ押し出される**。
  テロップが 1 行ずつ直列に並ぶため、縦動画では「出るタイミングが違う」状態になる。

### 1.4 開き直すと本数だけは 3 本に見える (ので気づきにくい)

`TimelineEditorDialog.__init__` (`timeline_editor_dialog.py:121`) が
`commands.ensure_initial_subtitle_tracks()` を呼ぶため、縦プロジェクトを開くと
**S1/S2/S3 が空で並ぶ** (resolve13 §3.3 / 既定 `initial_tracks=3`)。
このとき S3 へ入るのは**設定の `subtitle.sub_subtitle_color`** で、
元プロジェクトで決めた色ではない。

そのため画面上は「トラックはある・色も付いている」ように見えるが、

- テロップは全部 S1 に積まれている
- S3 の色は設定の既定値で、元プロジェクトの指定ではない
- S4 以降は現れない

という状態になる。要望の「反映されていません」はこれを指している。

### 1.5 同じ問題を既に解いている前例

`src/archive/timeline_builder.py:469-505` (書き出し用に区間を切り出す処理):

```python
    # トラックの既定色も引き継ぐ (ver9 resolve13 §3.8)。写さないと
    # 書き出しだけテロップの色が設定の役割色へ戻ってしまう。
    base_subtitle = timeline.track_by_id(BASE_SUBTITLE_TRACK_ID)
    subtitle_track = Track(..., color=(base_subtitle.color if base_subtitle else ""))
    # 元の字幕トラック構成 (S1 / コメント用 S2 …) をそのまま複製する
    # (ver3 resolve11 §5.9)。1 本へ潰すと S1 と S2 の字幕が同じトラックで重なり、
    # 読み込み時に _validate_overlaps が片方を右へ押し出してしまう。
```

**縦プロジェクトだけがこの対応から漏れている。** 本設計は同じ考え方を移植する。

### 1.6 配置 (R4) — クリップの境目でテロップが打ち切られる

#### 1.6.1 原因は `break`

`_copy_subtitles()` は、テロップ 1 本を**選んだクリップ 1 本ずつと突き合わせ、
最初に重なったところで `break`** している。

```python
# src/timeline/vertical_builder.py:213-230
for old_start, new_start, duration in offsets:     # 選んだクリップを順に見る
    overlap_start = max(start, old_start)
    overlap_end = min(end, old_start + duration)
    if overlap_end - overlap_start < _MIN_SUBTITLE_SEC:
        continue
    moved = copy.deepcopy(clip)
    moved.timeline_start = new_start + (overlap_start - old_start)
    moved.duration = overlap_end - overlap_start   # ← クリップの端で打ち切る
    subtitle_track.clips.append(moved)
    break                                          # ← 続きのクリップを見ない
```

アーカイブ用の Timeline は**無音カットでクリップが細かく並ぶ**(実データで 43〜241 本)。
隣どうしは隙間なく繋がっているので、画面上はひと続きに見えるが、
テロップから見ると「境目」が無数にある。テロップはセクション単位でしか
打ち切られていない (`archive/timeline_builder.append_subtitles`) ため、
**境目をまたぐテロップはごく普通に存在する**。

なお `_renumber()` のコメントは「1 つの字幕が複数区間へまたがった場合」に ID を
振り直すと書いているが、`break` があるため**現状はまたがることがない** =
実装とコメントが食い違っている。§3.5 の修正で、このコメントどおりになる。

#### 1.6.2 実測 (現行コードの振る舞い)

隙間なく並ぶ c1(0〜5) / c2(5〜9) / c3(9〜12) に対し、境目をまたぐテロップを置いた:

```
=== 元 (アーカイブ用 Timeline) ===
  s2 4.00〜6.00 (2.00s) c1→c2 をまたぐ
  s3 8.00〜11.00 (3.00s) c2→c3 をまたぐ
=== 縦 (c1・c2・c3 をすべて選択 / close_gaps=True) ===
  s2 4.00〜5.00 (1.00s)      ← 後半 1.00 秒が消える
  s3 8.00〜 9.00 (1.00s)     ← 後半 2.00 秒が消える
```

**全クリップを選んでいる** = 画面とまったく同じ中身を作っているのに、
テロップだけが尻切れになる。

#### 1.6.3 実データでの件数

保存済みのアーカイブプロジェクト 3 本で、全クリップを選んで縦プロジェクトを
作った場合に失われるテロップを数えた (0.05 秒以上短くなるものを数える):

| プロジェクト | V1 クリップ | テロップ | 尻切れになる | 失われる表示時間 |
| --- | --- | --- | --- | --- |
| `vod_2884417212` | 241 | 272 | **27 件** | 計 46.6 秒 (平均 1.72 秒) |
| `vod_2881981221` | 73 | 295 | 3 件 | 計 4.2 秒 (平均 1.38 秒) |
| `vod_2877176496` | 43 | 219 | 4 件 | 計 10.4 秒 (平均 2.59 秒) |

平均 1.4〜2.6 秒 = **テロップ 1 本ぶんの表示時間がほぼ丸ごと消える**。
文の途中でテロップが消えるため、要望の「配置が違う」はここが実体と見てよい。

#### 1.6.4 飛び飛びに選んだ場合は現状でも正しい

選んでいないクリップを挟んで選択した場合 (c1 と c3)、その区間のテロップは
落ち、c3 側のテロップは詰めたあとの正しい位置へ移っていた。
**壊れているのは「繋がっているのに切る」ところだけ**で、区間の切れ目の扱いは合っている。

---

## 2. 設計方針

### 2.1 やること

1. **元のテロップトラック構成をそのまま複製する** (§3.1)。
   `id` / `index` / `name` / `color` / `enabled` を写し、**中身が空のトラックも作る**
   (R2 の「トラック数」はこれで満たす)。
2. **テロップを元のトラックへ振り分ける** (§3.2)。コメントは S2、サブ色のものは S3 と、
   元の置き場のまま縦プロジェクトへ渡る。重なりの押し出しも起きなくなる。
3. **1 本も無い横 Timeline からは、従来どおり S1 を 1 本作る** (§3.3)。
4. **繋がっているクリップの境目でテロップを切らない** (§3.5 / R4)。
   クリップ 1 本ずつではなく「詰めたあとも繋がっている区間」と突き合わせ、
   区間の中では元の尺のまま運ぶ。切れ目 (選んでいない区間) でだけ分ける。
5. **切り詰めた・落としたテロップを黙って捨てない** (§3.6)。件数を警告へ積む。

### 2.2 やらないこと

- **テロップの中身は変えない** (R1)。文字・個別色・役割・フォント・`origin`
  (認識結果や信頼度の控え) は現行どおり `copy.deepcopy` で運ぶ。
  変えるのは**時刻の決め方だけ**で、最短表示時間の規約 (`_MIN_SUBTITLE_SEC`) も据え置く。
- **クリップの詰め直しそのものは変えない。** `close_gaps=True` (既定) で選んだクリップを
  0 秒起点へ詰める仕様は ver5 resolve9 §5.4 のまま。映像が詰まるぶんテロップも一緒に
  動くのは正しい振る舞いで、「元と同じ配置」とは**映像との相対位置が同じ**ことを指す。
- **縦プロジェクト側でトラックを増やさない。** `ensure_initial_subtitle_tracks()` は
  開いたときに編集画面が呼ぶ (resolve13 §3.3)。生成時に先回りして呼ぶと、
  元が S1 だけのプロジェクトでも S2/S3 が**設定の色**で付いてしまい、
  「元の構成を写す」という本設計の筋と食い違う。
- **映像・音声トラックの構成は引き継がない。** 縦プロジェクトは V1/A1 の 1 本ずつに
  詰め直す仕様 (ver5 resolve9 §5.4)。オーバーレイ (V2 以降) は座標が合わないため
  従来どおり落として警告する (`_warn_dropped`)。
- **役割 (`clip.role`) をトラックから振り直さない。** テロップは元のトラックへ入るため、
  役割とトラックの対応は元のまま保たれる。`role_for_subtitle_track()` を通す必要はない。
- **`max_tracks` (既定 8) の判定を入れない。** 元プロジェクトが既にその上限の中で作られて
  おり、複製で本数が増えることはない。

---

## 3. 実装設計

### 3.1 トラック構成を複製する

`_copy_subtitles()` を書き換える。元のトラックを `index` 昇順で走り、同じ素性の
空トラックを縦 Timeline へ足してから、テロップを振り分ける。

```python
# src/timeline/vertical_builder.py

# 元のテロップトラック構成 (本数・名前・ヘッダーで決めた色) をそのまま複製する
# (ver9 resolve15 §3.1)
#
# 1 本へ潰していたため、(a) トラックの既定色 (resolve13) が消える
# (b) S1 と S2 のテロップが同じトラックで重なり、開き直すときの
# _validate_overlaps が後ろへ押し出す — の 2 つが起きていた。
# アーカイブの書き出し (archive/timeline_builder._build_group_timeline) と同じ流儀。
#
# 中身が空のトラックも作る。要望の「トラック数を反映する」はこれで満たす。
# 戻り値: 元のトラック ID → 縦 Timeline の対応トラック
def _copy_subtitle_tracks(timeline, vertical):
    mapping = {}
    for track in sorted(timeline.subtitle_tracks(), key=lambda t: t.index):
        copied = Track(track.id, TRACK_SUBTITLE, track.index, name=track.name,
                       enabled=track.enabled, color=track.color)
        vertical.tracks.append(copied)
        mapping[track.id] = copied
    if not mapping:
        # 字幕トラックが 1 本も無い横 Timeline (従来どおり S1 を用意する)
        base = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
        vertical.tracks.append(base)
        mapping[base.id] = base
    return mapping
```

写す項目と理由:

| 項目 | 写す | 理由 |
| --- | --- | --- |
| `id` | ○ | コメント用トラック (既定 `S2`) の判定は ID で行う (`comment_track_id`)。写さないと縦側でコメントの置き場が変わる |
| `index` | ○ | 画面の並び順 (`timeline_view.build_rows` は index 昇順)。番号が飛んでいても再現する |
| `name` | ○ | ヘッダーの表示名 (`Comment` / `Subtitle 3` など) |
| `color` | ○ | **R3 の本体** (resolve13 の `Track.color`) |
| `enabled` | ○ | 利用者が消していたトラックを勝手に復活させない |
| `locked` | × | 編集を止めるための一時的な印。新しいプロジェクトでは外した状態で始める (§4 Q1) |
| `kind` / `clips` | — | `kind` は固定、`clips` は §3.2 で入れる |

### 3.2 テロップを元のトラックへ振り分ける

```python
def _copy_subtitles(timeline, vertical, offsets, warnings):
    tracks = _copy_subtitle_tracks(timeline, vertical)
    fallback = min(tracks.values(), key=lambda t: t.index)

    for track in timeline.subtitle_tracks():
        # 元のトラックに対応する側へ入れる (役割ごとの重なりを保つ)
        target = tracks.get(track.id, fallback)
        for clip in track.clips:
            ...                       # 時刻の写し方は §3.5
            target.clips.append(moved)

    for target in tracks.values():
        target.clips.sort(key=lambda c: c.timeline_start)
        _renumber(vertical, target)
    ...
```

- 行き先を変えるだけで、テロップの中身 (文字・個別色・役割・`origin`) は触らない。
  時刻の決め方は §3.5 で合わせて直す (全体像は §3.5 のコードを参照)。
- `_renumber()` はトラックごとに呼ぶ。`vertical.next_id("s")` は
  全トラックの ID を見て払い出すため (`model._collect_ids`)、
  トラックをまたいだ ID 衝突は起きない。
- `fallback` は `tracks.get()` が外れたときの保険 (通常は起こらない)。

### 3.3 トラック本数の対応表

| 元プロジェクト | 生成される縦プロジェクト | 開いたときの画面 |
| --- | --- | --- |
| S1 のみ | S1 のみ | S1/S2/S3 (S2・S3 は編集画面が補う / 従来と同じ) |
| S1/S2/S3 | S1/S2/S3 (色つき) | そのまま |
| S1/S2/S3/S4 | S1/S2/S3/S4 (色つき) | そのまま |
| 字幕トラック無し | S1 (空) | S1/S2/S3 |
| S1/S2 (S3 を削除済み) | S1/S2 | S1/S2 + S3 (編集画面が補う) |

最後の行だけは「元より 1 本増える」が、これは縦プロジェクトに限らず**保存済みプロジェクトを
開き直したときと同じ振る舞い** (resolve13 §3.3) なので、本設計では触らない (§8)。

### 3.4 設定

従来どおり 1 本へ潰す経路も残す。既定は新しい振る舞いで、キーは切り戻し用。

| キー | 既定 | 意味 |
| --- | --- | --- |
| `vertical.crop.carry_subtitle_tracks` | `true` | `true` = テロップトラックの構成 (本数・名前・色・有効) を引き継ぐ / `false` = v1.7.1 以前と同じく S1 の 1 本へ潰す |

- 置き場所は `vertical.crop` (縦プロジェクト生成の設定が集まっている)。
  `crop.py` の `_DEFAULTS` と `crop.config()`、`settings_window` の既定値、
  `setting.json` の 3 か所へ足す (ハードコードしない / CLAUDE.md)。
- `build()` は既に `settings` を受けているため引数は増やさない
  (`crop.config(settings)["carry_subtitle_tracks"]` を読む)。
- 設定画面には出さない (`setting.json` 直編集)。`close_gaps` / `keep_media` と同じ扱い。

### 3.5 テロップの配置を元のままにする (R4)

`break` をやめ、**重なったクリップぶんの欠片をすべて作り、縦 Timeline 上で
繋がっている欠片どうしを 1 本へ結合する**。繋がっていないところ (選んでいない区間) で
だけ分かれるため、結果として「元の配置そのまま」になる。

```python
# src/timeline/vertical_builder.py

# クリップの境目は縫い目とみなす許容差 (秒)。
# フレーム量子化で 1 フレーム未満のずれ・重なりが残るため、
# project_io._OVERLAP_TOLERANCE_SEC と同じ値で「繋がっている」とみなす。
_SEAM_TOLERANCE_SEC = 0.0015


# テロップ 1 本を縦 Timeline の時刻へ写す (ver9 resolve15 §3.5)
#
# 従来は「最初に重なったクリップ」だけを採って端で打ち切っていたため、
# 無音カットの境目をまたぐテロップが尻切れになっていた (§1.6)。
# 重なったクリップぶんの欠片をすべて作り、縦 Timeline 上で隣り合うものを
# 1 本へ結合する。詰めても繋がっている区間では元の尺がそのまま残り、
# 選んでいない区間を挟んだところでだけ 2 本に分かれる。
#
# 欠片の時刻はクリップごとの差分 (new - old) で写す。区間の先頭の差分で
# まとめて写すと、クリップ間に残る 1 フレーム未満のずれが積もって終端がずれる。
# 戻り値: [(開始, 終了), ...] (縦 Timeline の時刻 / 時系列順)
def _map_spans(start, end, offsets):
    pieces = []
    for old_start, new_start, duration in offsets:      # 時系列順
        overlap_start = max(start, old_start)
        overlap_end = min(end, old_start + duration)
        if overlap_end <= overlap_start:
            continue
        shift = new_start - old_start
        piece = [overlap_start + shift, overlap_end + shift]
        if pieces and piece[0] - pieces[-1][1] <= _SEAM_TOLERANCE_SEC:
            pieces[-1][1] = max(pieces[-1][1], piece[1])    # 縫い目なので繋ぐ
        else:
            pieces.append(piece)
    # 切り詰めた結果これより短くなった欠片は捨てる (見えないため / 従来と同じ規約)
    return [(a, b) for a, b in pieces if b - a >= _MIN_SUBTITLE_SEC]
```

- 中間の欠片は `_MIN_SUBTITLE_SEC` で捨てない (繋ぐ前に捨てると穴が開く)。
  最短判定は**結合後の 1 本**に対して行う。
- クリップが時系列順であることは `_target_clips()` が保証している
  (`sorted(..., key=lambda c: c.timeline_start)`)。

`_copy_subtitles()` 側:

```python
def _copy_subtitles(timeline, vertical, offsets, warnings):
    tracks = _copy_subtitle_tracks(timeline, vertical)       # §3.1
    fallback = min(tracks.values(), key=lambda t: t.index)

    dropped = trimmed = 0
    for track in timeline.subtitle_tracks():
        target = tracks.get(track.id, fallback)              # §3.2
        for clip in track.clips:
            spans = _map_spans(clip.timeline_start, clip.timeline_end, offsets)
            if not spans:
                if clip.use:
                    dropped += 1
                continue
            carried = sum(b - a for a, b in spans)
            if carried < clip.duration - _MIN_SUBTITLE_SEC and clip.use:
                trimmed += 1
            for span_start, span_end in spans:
                moved = copy.deepcopy(clip)
                moved.timeline_start = span_start
                moved.duration = span_end - span_start
                target.clips.append(moved)

    for target in tracks.values():
        target.clips.sort(key=lambda c: c.timeline_start)
        _renumber(vertical, target)       # 分かれた欠片へ新しい ID を振る
    ...
```

### 3.6 落としたもの・切り詰めたものを知らせる

従来の「範囲の外にある字幕 N 件は引き継ぎませんでした」に加えて、
**一部だけ運ばれたもの**も伝える (黙って落とさない / ver5 resolve9 §3.6)。

```
・選んだ範囲の外にある字幕 3 件は引き継ぎませんでした。
・選んだ範囲の境目にかかる字幕 2 件は、はみ出した部分を切り詰めました。
```

切り詰めが起きるのは「選んでいないクリップを挟んで選んだ」場合だけで、
セクション全体を選ぶ通常の使い方では 0 件になる (§6 の実データ確認)。

---

## 4. 判断が要る点

| # | 項目 | 本設計の決め | 迷う点 |
| --- | --- | --- | --- |
| Q1 | `locked` を写すか | **写さない** (常に外した状態で作る) | 縦プロジェクトは作りたてで、編集を止める理由が引き継がれるとは考えにくい。ヘッダーに解除 UI が無く (`timeline_view` では判定のみ)、写すと**編集できない理由が分からない**トラックができる。要望にも含まれていない。写す指示があれば 1 行で変えられる |
| Q2 | `enabled=false` のトラックを写すか | **写す** (本数にも数える) | 「表示しない」と決めたものを復活させないため。空かつ無効なトラックが縦側に並ぶのが煩わしい場合は、無効なものを飛ばす選択もある |
| Q3 | 空のトラックを作るか | **作る** (R2 の本数そのもの) | 要望が「トラック数」と言っているため、中身の有無で本数が変わらないようにする |
| Q4 | 設定キーを足すか | **足す** (§3.4) | キーを増やさず常に引き継ぐだけでもよい。既定が新しい振る舞いなので実質は切り戻し用 |
| Q5 | 配置 (R4) にも切り戻し用のキーを足すか | **足さない** | §3.5 は要望への対応であると同時に**尻切れの修正**で、元の振る舞いへ戻す理由が無い。従来どおりに切り詰めたい運用は考えにくい |
| Q6 | 切り詰めの警告を出すか | **出す** (§3.6) | 通常の使い方では 0 件のため、出ても邪魔にならない。うるさければログだけにできる |

いずれも要望の本体 (R2 / R3 / R4) には影響しない。Q1 のみ、指示があれば変更する。

---

## 5. 変更ファイル

| ファイル | 内容 |
| --- | --- |
| `src/timeline/vertical_builder.py` | `_copy_subtitle_tracks()` / `_map_spans()` を追加・`_copy_subtitles()` の振り分けと時刻の写し方 (§3.1 / §3.2 / §3.5 / §3.6)・`_SEAM_TOLERANCE_SEC` |
| `src/timeline/crop.py` | `carry_subtitle_tracks` の既定と読み出し (§3.4) |
| `src/settings/setting.json` | `vertical.crop.carry_subtitle_tracks: true` |
| `src/settings/settings_window.py` | 同キーの既定値 (コメント付き) |
| `tests/test_vertical_subtitle_tracks.py` | **新規**。構成の複製・色・振り分け・時刻の保持 |
| `tests/test_vertical_subtitle_layout.py` | **新規**。境目をまたぐテロップ・飛び飛びの選択・警告 (§3.5 / §3.6) |
| `tests/test_vertical_builder.py` | 既存の `test_subtitles` は S1 のみ・境目をまたがないため**変更不要** (実装時に通ることを確認する) |
| `src/i18n/catalog_en.py` | §3.6 の切り詰め警告 1 文の対訳 |

既存の警告文 (「範囲の外にある字幕…」) は f-string のままで、本設計では触らない。
下流 (焼き込み・プレビュー・Resolve 出力) は `subtitle_style` 経由のため無改造 (§1.2)。

---

## 6. テスト計画

| 条件 | 期待 |
| --- | --- |
| S1/S2/S3/S4 を持つ横 Timeline | 縦プロジェクトも 4 本 (`index` / `name` / `enabled` が一致) |
| S3 に `#2ABCFF`、S4 に `#FFEE00` | 縦側の `Track.color` が一致 / `subtitle_style.track_color_map()` に両方載る |
| S2 のコメント・S3 のサブを同時刻に重ねる | それぞれ元のトラックへ入り、**時刻がずれない** |
| 保存 → 開き直し | `_validate_overlaps` の警告が出ない / 時刻が一致する |
| 中身が空の S4 | トラックとして存在する (本数に数える) |
| 字幕トラックが 1 本も無い | S1 が 1 本作られる (従来どおり) |
| 選択範囲の外のテロップ | 従来どおり落として警告 (件数も従来と一致) |
| `carry_subtitle_tracks=false` | S1 の 1 本へ潰す (v1.7.1 と同じ) |
| 元の Timeline | 変わらない (ver5 resolve9 §4-5 / `deepcopy` のまま) |

配置 (§3.5 / R4):

| 条件 | 期待 |
| --- | --- |
| 隙間なく並ぶ c1/c2/c3 を全選択・境目をまたぐテロップ | **元の尺のまま 1 本**で入る (現状は尻切れ) |
| 同上・テロップの開始時刻 | 先頭クリップぶんだけ前へ寄る (映像との相対位置が一致) |
| クリップ間に 1 フレーム未満のずれ・重なりがある | 縫い目とみなして繋ぐ (`_SEAM_TOLERANCE_SEC`) |
| c1 と c3 を選び、c2 をまたぐテロップ | **2 本に分かれ**、それぞれ正しい位置へ / ID が振り直される |
| 同上 | 「境目にかかる字幕 N 件を切り詰めました」の警告が出る |
| 分割後に `_MIN_SUBTITLE_SEC` 未満になる欠片 | 捨てる (従来と同じ規約) |
| `close_gaps=False` | 空白を残したまま、同じ規則で写る |
| 保存 → 開き直し | 重なり警告が出ない / 時刻が一致する |

手で確かめる: アーカイブ切り抜きで S2 にコメント・S3 にサブ色のテロップを置き、
縦動画プロジェクトを作成 → 開き直して **トラックの本数・ヘッダーの色・テロップの置き場と
時刻・尺が元と同じ**こと。プレビューの文字色が元と同じこと。書き出して焼き込みの色も
一致すること。

実データでの事前確認 (§3.5 のアルゴリズムを保存済みプロジェクト 3 本へ当てた結果):

| プロジェクト | 尻切れ | 落ち | 分割 |
| --- | --- | --- | --- |
| `vod_2884417212` (241 クリップ / 272 テロップ) | 27 件 → **0 件** | 0 件 | 0 件 |
| `vod_2881981221` (73 / 295) | 3 件 → **0 件** | 0 件 | 0 件 |
| `vod_2877176496` (43 / 219) | 4 件 → **0 件** | 0 件 | 0 件 |

---

## 7. 互換性

| 対象 | 扱い |
| --- | --- |
| 既に作った縦プロジェクト | 変わらない (S1 に積まれ、尻切れのまま)。作り直せば直る |
| 既存の縦プロジェクトの作り直し | テロップが長くなる (尻切れが直る) ため、焼き込み結果が変わる。意図した変更 |
| 元が S1 だけのプロジェクト | 生成結果は従来と同一 (S1 1 本) |
| クリップ用からの縦動画作成 | 同じ経路のため同じく改善される (要望はアーカイブ用だが区別しない) |
| プロジェクト形式 (JSON) | 変更なし。`Track.color` は指定があるときだけ載る既存仕様 (resolve13 §3.1) |
| `role_track_enabled=false` の運用 | トラックを写すだけなので影響なし (役割は触らない) |

---

## 8. 残課題

| 項目 | 状況 |
| --- | --- |
| 削除したトラックが開き直しで戻る | `ensure_initial_subtitle_tracks` が `initial_tracks` (既定 3) まで補うため、S3 を消したプロジェクトを開くと S3 が空で戻る。縦プロジェクトに限らない既存の振る舞いで、本設計では触らない |
| 既に作った縦プロジェクトの救済 | 自動では直せない (どのテロップがどのトラック由来か・どこで切られたかが縦側に残っていない)。作り直しが要る |
| オーバーレイ (V2 以降) | 従来どおり引き継がない (座標が縦で合わないため / `_warn_dropped` が警告) |
| 詰めたぶんのずれ | `close_gaps=True` では選んだクリップ間の空白が消えるため、テロップの絶対時刻は元と変わる (実データで最大 64ms)。映像も一緒に詰まるので**映像との相対位置は一致**しており、仕様どおり |
| 画面上 (映像フレーム内) の配置 | 本設計は「Timeline 上の配置」の話。縦では文字サイズ・余白が `vertical.*` の設定へ切り替わるため、画面内の見え方は横と同じにはならない (ver5 resolve9 の仕様)。ここも揃えたい場合は別途要望が要る |

---

## 10. 実装結果 (2026-10-31)

設計どおり実装した。**テスト 2071 件すべて成功** (実装前 2040 件 → 31 件追加)。

### 10.1 変更したファイル

| ファイル | 内容 |
| --- | --- |
| `src/timeline/vertical_builder.py` | `_copy_subtitle_tracks()` / `_map_spans()` を追加・`_SEAM_TOLERANCE_SEC`・`_copy_subtitles()` の全面書き換え |
| `src/timeline/crop.py` | `carry_subtitle_tracks` の既定と読み出し |
| `src/settings/setting.json` / `settings_window.py` | `vertical.crop.carry_subtitle_tracks: true` |
| `src/i18n/catalog_en.py` | 切り詰め警告 1 件の対訳 |
| `tests/test_vertical_subtitle_tracks.py` | **新規** 12 件 (構成・色・振り分け・開き直し・切り戻し) |
| `tests/test_vertical_subtitle_layout.py` | **新規** 19 件 (境目・縫い目・切れ目・警告・`_map_spans`) |

### 10.2 設計から変えた点

**欠片が縦 Timeline 上で隣り合うときは 1 本へ繋ぐ** (§3.5 のコードどおりの挙動だが、
§0.3 / §6 の表では「2 本に分かれる」と書いていた)。

`close_gaps=True` (既定) では選んだクリップが詰められるため、選んでいない区間を
またいだテロップも**縦 Timeline 上では隣り合う**。ここを 2 本に分けると、
同じ文字のテロップが隙間なく 2 つ並ぶだけで、見た目は 1 本と変わらず編集の手数だけ増える。
そのため結合する。分かれるのは `close_gaps=False` (空白を残す) のときだけになった。

| 選択 | close_gaps | 結果 |
| --- | --- | --- |
| c1・c3 (c2 を飛ばす) / テロップ 4.0〜10.0 | `True` (既定) | **1 本** 4.0〜6.0 (c1 側 1.0 秒 + c3 側 1.0 秒) |
| 同上 | `False` | **2 本** 4.0〜5.0 と 9.0〜10.0 |

どちらも「切り詰めた」警告は出る。

### 10.3 テストで分かった、設計に無かった前提

最初に書いたテストが、同じトラックに時間が重なるテロップ (8.0〜11.0 と 9.5〜10.5) を
置いていて落ちた。**同一トラックでの重なりは `project_io._validate_overlaps` が
押し出すため、実際のプロジェクトには存在しない**。テストの前提を実データに合わせて直した。

この性質は本修正の裏付けでもある: 尻切れを直してテロップが元の尺へ戻っても、
同じトラックの隣のテロップと衝突することはない (元の Timeline で重なっていないため)。

### 10.4 実データでの確認

保存済みのアーカイブプロジェクト 3 本で、全クリップを選んで縦プロジェクトを作り、
実装前 (v1.7.1) と比べた:

| プロジェクト | テロップトラック | テロップ件数 | 表示時間の合計 |
| --- | --- | --- | --- |
| `vod_2884417212` (V1 241) | 2 → **1 本**　⇒　2 → **2 本** | 272 → 272 | **−46.93 秒** ⇒ **−0.03 秒** |
| `vod_2881981221` (V1 73) | 2 → **1 本**　⇒　2 → **2 本** | 295 → 295 | **−4.19 秒** ⇒ **±0.00 秒** |
| `vod_2877176496` (V1 43) | 2 → **1 本**　⇒　2 → **2 本** | 219 → 219 | **−10.42 秒** ⇒ **±0.00 秒** |

(左が実装前 / 右が実装後。`git stash` で実装前のコードへ戻して同じ条件で測った)

残る −0.03 秒は、`vod_2884417212` にある本当の隙間 3 か所 (各 0.013〜0.018 秒) が
`close_gaps` で詰められるぶんで、仕様どおり。

トラックの名前・番号・有効/無効・色 (`Track.color`) も元と一致し、テロップは
元のトラック (S1 / S2 Comment) へ入ることを確認した。

### 10.5 要望との対応

| 要望 | 結果 |
| --- | --- |
| R2 トラック数 | 元のテロップトラックをすべて複製 (空のトラックも含む) |
| R3 トラックの色 | `Track.color` を複製。焼き込み・プレビュー・Resolve 出力は `subtitle_style` 経由のため無改造で一致 |
| R4 Timeline 上の配置 | 繋がっているクリップの境目では切らなくなった。実データで失われる表示時間が 46.9 秒 → 0.03 秒 |
| (併せて) 開き直すと時刻がずれる | テロップが元のトラックへ戻るため、重なりの押し出しが起きなくなった |
