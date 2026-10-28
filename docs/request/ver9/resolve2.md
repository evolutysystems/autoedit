# ver9 resolve2 — テロップノードの右クリックから新規テロップトラックを追加する

対象: `autoedit` (Stretheus クライアント) / Timeline 編集画面
要望: `docs/request/ver9/request2.md`
関連: `docs/request/ver3/resolve11.md` (役割別の字幕トラック = コメント専用トラック S2),
      `docs/request/ver3/resolve10.md` §5.6 (右クリックメニューの規約),
      `docs/request/ver8/resolve6.md` §R5 (字幕メニューの追加例)

作成日: 2026-10-25 (main `115e257` / v1.6.1)
状態: **設計のみ。実装はレビュー後に行う** (CLAUDE.md「いきなり実装を開始しない」)

---

## 0. 要望と結論

> Timeline 編集画面で、テロップノードを右クリックして新規テロップトラックを追加する機能を
> 追加したいです。現在テロップの役割から「コメント」にすることで新規テロップトラックを
> 追加していますが、それと同じ機能を右クリックから行いたいです。
> コメントのようにユーザー画像、黒色の半透明の背景は必要ありません。
>
> **(追加)** 2 行目（2 トラック目）はコメント限定なので、テロップトラックを右クリックして
> 新規トラックを作成する場合は 3 行目という扱いからお願いします。

### 0.1 いま何が起きているのか

「コメントにするとトラックが増える」のは、**役割 (`role`) の変更に 2 つの働きが
同居している**ためです。

| 役割を `comment` にすると | 担当している場所 |
| --- | --- |
| ① 専用トラック (既定 `S2`) へ移る = 通常テロップと**同時刻に並べられる** | `commands.ChangeSubtitleRole` → `ensure_subtitle_track` |
| ② アイコン・黒の半透明背景・左中央寄せ・コメント色が付く | `role == "comment"` を見る `comment_decor` / `subtitle_generator` / `preview_items` |

要望は **① だけが欲しい / ② は不要** というものです。
① と ② は同じ `role` から分岐しているため、現状では ① だけを得る手段がありません。

### 0.2 結論 (やること)

**役割を一切変えず、字幕トラックだけを 1 本増やして、右クリックしたテロップを
そこへ移す操作**を新設します。

- 新トラックは `TRACK_SUBTITLE` の普通の字幕トラック (`S3`, `S4`, …)。
  コメント用トラック (`S2`) とは別物で、**コメントの印は何も持たない**。
- **2 トラック目 (index 2 / 既定 `S2`) はコメント専用として予約**し、
  右クリックからの新規トラックは **3 トラック目 (`S3`) から**払い出す (要望「(追加)」/ §R4)。
  `S2` がまだ存在しない状態でも `S2` は使わず、欠番のまま `S3` を作る。
- 移したテロップの `role` は**そのまま** (`streamer` / `sub`)。
  見た目 (色・配置・縁) を決めているのは `role` だけなので、
  **アイコンも背景も付かず、従来のテロップとまったく同じに焼かれます** (§1.3)。
- 出力 (ASS) はトラックを見ていないため、**レンダリング側の変更は不要** (§1.3)。

### 0.3 同時に直す既存の不具合 (この要望の前提)

「字幕トラックは S1 と S2 の 2 本だけ」という前提で書かれた推測が 2 箇所あり、
3 本目のトラックを足すと**それが勝手にコメント扱いされます**。
これを直さないと、要望どおり足した新トラックに
アイコンと背景が付いてしまいます (§1.5 問題B / §4.1 / §4.5)。

---

## 1. 現状分析

### 1.1 見た目を決めているのは `role` だけ (トラックは無関係)

| 要素 | 判定条件 | 場所 |
| --- | --- | --- |
| 塗り色 / 縁色 | `role` → `role_colors` / `role_outline_colors` | `subtitle_generator.FontProfile` |
| ASS Style 名 (`Streamer`/`Sub`/`Comment`) | `style_for_role(role)` | 同上 L198 |
| 配置・余白 (コメントは `\an4` + `margin_l 240`) | `placement_for_role(role)` | 同上 L186 |
| 「コメント：」ラベル | `role == "comment"` かつ `comment_label` が非空 | `subtitle_generator.generate_ass_file` L1057 |
| 黒の半透明背景 (角丸) | `role == "comment"` | 同上 L1064 / `comment_decor.build_background_text` |
| ユーザー画像 (アイコン) | `role == "comment"` | `comment_decor` / `preview_items._add_comment_decor` L421 |
| ASS Layer (背景→本文の重ね順) | `role == "comment"` のみ `comment_text_layer` | `subtitle_generator` L1091 |
| プレビューの基準点・行寄せ | `role == "comment"` | `preview_items` L431 / L610 |

→ **`role` を変えなければ装飾は一切付きません。** 要望の「アイコン・背景は不要」は
「役割を変えない」ことで自動的に満たされます。

### 1.2 トラックを決めているのは 2 つの関数だけ

```text
commands.ensure_subtitle_track(timeline, role, cfg)      # 無ければ作る
  └─ model.Timeline.subtitle_track_for_role(role, ...)   # 探すだけ
```

- `role == "comment"` かつ `role_track_enabled` → `cfg["comment_track_id"]` (既定 `S2`)
- それ以外 → `base_subtitle_track()` (= index 最小 = `S1`)

呼び出し元は `ChangeSubtitleRole` (役割変更時の移動) と
`AddSubtitleClip` / `PasteClips` (追加・貼り付け先の決定) の 3 つです。

### 1.3 トラックが出力に与える影響は「同時に出せるかどうか」だけ

書き出し・プレビュー・書き出し連携は、いずれも**トラックを辿らず
`Timeline.subtitle_clips()` で全字幕を時系列に集めるだけ**です。

| 経路 | 参照 |
| --- | --- |
| 焼き込み (ASS) | `timeline/renderer.py:445` |
| プレビュー | `gui/timeline/preview_panel.py:1401` |
| DaVinci Resolve 連携 | `export/resolve_export.py:598,610` |
| アーカイブ切り出し | `archive/timeline_builder.py:1112` (トラックは走査するが出力は role 依存) |

一方、**1 本のトラックの中では時間を重ねられません**
(`project_io._validate_overlaps` が重なりを右へ押し出す / `AddSubtitleClip` も
既存字幕の中には置かない)。「同時刻に 2 本のテロップを出す」唯一の手段が
トラックを分けることであり、これが `S2` を作った本来の理由です
(ver3 resolve11 §5.1)。

→ **トラックを増やすことは、出力の見た目を何も変えない。**
   変わるのは「同じ時刻に並べられる本数」だけです。

### 1.4 現状の右クリックメニュー (`timeline_view.contextMenuEvent`)

テロップノードを右クリックしたときの並び (上から):

```text
ここで分割
───
コピー / 再生ヘッドへ貼り付け
───
再生ヘッドより前をリップル削除 / 後ろをリップル削除
───
リップル削除 / 削除のみ (空白を残す)
───
レイヤー ▶
───
使用しない (使用する)
───                        ← _add_align_actions (字幕のときだけ)
テロップの結合 / ここでテロップを分割
───
カット頭へ揃える / すべての字幕をカット頭へ揃える
```

字幕固有の項目は `_add_align_actions(menu, clip)` に閉じており、
`isinstance(clip, SubtitleClip)` でなければ何も足しません。
**同じ流儀で 1 メソッド足すのが最小の変更になります。**

なお空き領域 (クリップ無し) を字幕トラック上で右クリックすると
「字幕追加」が出ます (`_add_subtitle`)。

### 1.5 既存の問題点

#### 問題A (要望そのもの)
役割を変えずにトラックを増やす操作が存在しない。
`AddSubtitleClip` も `ChangeSubtitleRole` も「役割 → トラック」の一方向しか扱わない。

#### 問題B (3 本目のトラックがコメント扱いされる)

「S1 以外の字幕トラック = コメント」という推測が 2 箇所にあります。

1. `gui/timeline/timeline_view.py:1056 _role_for_track()`
   ```python
   is_comment = (track.id == cfg["comment_track_id"]
                 or (base is not None and track.id != base.id))   # ← ここ
   return ROLE_COMMENT if is_comment else DEFAULT_ROLE
   ```
   新トラック `S3` の空き領域で「字幕追加」すると、**役割がコメントになり
   アイコンと背景が付きます** = 要望と正面から矛盾します。

2. `timeline/model.py:654 subtitle_track_for_role()`
   ```python
   # ID を設定で変えられている場合に備え「S1 以外の字幕トラック」で拾い直す
   for candidate in sorted(self.subtitle_tracks(), key=lambda t: t.index):
       if base is None or candidate.id != base.id:
           return candidate                                       # ← ここ
   ```
   `S2` が無く `S3` だけある状態でテロップをコメント化すると、
   **コメントが `S3` (通常テロップ用) へ流れ込みます**。

どちらも「2 本しかない」前提の救済措置です。3 本目を作れるようにする今回、
**先に直す必要があります** (§4.1 / §4.5)。

---

## 2. 設計方針

| # | 方針 | 理由 |
| --- | --- | --- |
| **D-1** | 役割 (`role`) は**変えない**。トラックだけ増やす | 装飾を決めるのは `role` だけ (§1.1)。触らなければアイコン・背景は付かない |
| **D-2** | 新トラックは「ただの字幕トラック」。コメント用トラックとは独立 | 役割と置き場所の対応を単純に保つ。`comment_track_id` の意味を変えない |
| **D-2b** | **2 トラック目 (index 2 / `S2`) はコメント専用に予約**し、通常テロップ用は 3 番目から払い出す | 要望「(追加)」。コメント用トラックは必要になって初めて生えるため (遅延生成)、番号を予約しないと `S1` だけの状態で新規トラックが `S2` を奪ってしまう |
| **D-3** | 操作は `Command` として実装し `CommandStack` へ積む | Undo/Redo が一様に効く (ver3 resolve.md §4-2 の規約)。スナップショット方式なので逆操作は不要 |
| **D-4** | トラックの ID / 名前 / 上限はすべて `setting.json` から取る | CLAUDE.md「ハードコードは禁止」 |
| **D-5** | 「S1 以外はコメント」という推測を消し、**コメントは `comment_track_id` だけ**で判定する | 3 本目以降を正しく通常テロップとして扱うため (§1.5 問題B) |
| **D-6** | 移した後の表示位置は変えない | 位置を自動でずらすと、既存プロジェクトの見え方を勝手に変えることになる。重なりが邪魔な場合はプレビューでドラッグして決める運用 (**確定 / §10.1 Q1**) |
| **D-7** | 空のトラックは作らない / 残さない | 1 本も移せないときにトラックだけ生える状態を避ける (§4.3 実装注意) |
| **D-8** | 機能は設定で OFF にできる | 既存の運用を壊さない。既定は ON |

---

## 3. 仕様

### R1 メニュー項目
テロップ (字幕) ノードを右クリックすると、字幕固有グループの末尾に項目を足す。

```text
使用しない
───
テロップの結合 / ここでテロップを分割
───
カット頭へ揃える / すべての字幕をカット頭へ揃える
───                                   ← 追加
新規テロップトラックへ移動            ← 追加 (R2)
```

ラベル (`tr()` 経由):
- 1 件: `新規テロップトラックへ移動`
- 複数選択: `選んだ {count} 件を新規テロップトラックへ移動`

### R2 動作
1. 新しい字幕トラックを 1 本作る (ID・名前は R4)。
2. 対象テロップをそのトラックへ移す。`clip.id` は変えない
   (選択・Undo・アーカイブ index の紐付けを切らないため / ver3 resolve11 の規約と同じ)。
3. `role` / `text` / `font` / 色 / `transform` / `origin` はすべて変更しない。
4. 移動後に Timeline を再描画。移したノードは選択状態を維持する。

### R3 対象ノード
- 右クリックしたノードが字幕のときだけ項目を出す (映像・音声・空き領域では出さない)。
- 複数選択中は、**選択中の字幕クリップすべて**が対象
  (設定 `new_track_moves_selection` が `false` なら右クリックした 1 件のみ)。
- 互いに時間が重なる組み合わせは 1 本のトラックへ入れられないため、
  **開始時刻の早い順に入れられるものだけ移す**。入らなかったものは元トラックに残し、
  件数をステータスへ出す (R9)。
- 元トラックが `locked` のクリップは対象外。

### R4 新トラックの ID / 名前 / 並び順 (要望「(追加)」)

`index` は次の 3 つを満たす最小の整数とする。

1. 既存字幕トラックの `index` 最大 + 1 以上
2. **`new_track_min_index` (既定 `3`) 以上** ← 2 トラック目はコメント用に空ける
3. **コメント用トラックの番号・ID を占有しない**
   (`comment_track_index` と等しい番号、`comment_track_id` と等しい ID は飛ばす)

- ID = `f"{new_track_id_prefix}{index}"` (既定 `S3`, `S4`, …)。
  既に同じ ID が使われている場合はさらに `index` を進める。
- `name` = `new_track_name_format.format(index=index)` (既定 `Subtitle 3`)
- 表示行は `build_rows()` が `index` 昇順で並べるため、
  `S1` → `S2`(Comment) → `S3` → `S4` … と上から順に並ぶ。**表示側の変更は不要**。

| 操作前の字幕トラック | 新トラック | 備考 |
| --- | --- | --- |
| `S1` のみ | **`S3`** (index 3) | `S2` は欠番のまま予約される (要望「(追加)」) |
| `S1`, `S2`(Comment) | `S3` (index 3) | |
| `S1`, `S3` | `S4` (index 4) | `S2` は引き続き予約 |
| `S1`, `S2`, `S3`, `S4` | `S5` (index 5) | |

**`S2` を空トラックとして先に作ることはしない** (D-7)。
`S1` + `S3` の状態では編集画面の行は 2 行に見えるが、
その後テロップをコメント化すると `S2` が生えて**必ず 2 行目へ入る**
(`build_rows()` が `index` 昇順で並べるため)。
→ 「2 行目はコメント限定」は番号の予約によって保証される (**確定 / §10.1 Q6**)。

### R5 上限
字幕トラックの本数が `max_tracks` (既定 8) に達している場合は実行せず、
メニュー項目を無効化し、理由をツールチップとステータスへ出す (R9)。

### R6 Undo / Redo
1 回の操作 = 履歴 1 手。Undo でトラックの新設とクリップの移動が同時に戻る
(`_snapshot` が `tracks` を丸ごと控えるため、追加実装は不要)。
履歴ボタンのラベルは `新規テロップトラックへ移動` (複数件は件数入り)。

### R7 出力・プレビュー
- 焼き込み / プレビュー / Resolve 連携は `subtitle_clips()` 経由で全字幕を集めるため、
  **新トラックのテロップも自動的に出力される** (§1.3)。
- `role` が変わらないので ASS Style も配置も従来どおり。
  **アイコン・背景・「コメント：」ラベルは付かない** (要望どおり)。
- `z_order` も変えない。字幕同士の重ね順は従来と同じ
  (`DEFAULT_SUBTITLE_Z_ORDER` のまま / レイヤー操作で個別変更は可能)。

### R8 コメント化との関係 (仕様として明記)
新トラック (`S3`) 上のテロップの役割を後からインスペクタで「コメント」に変えると、
従来どおりコメント用トラック (`S2`) へ移る。そこから「配信者」へ戻すと `S1` へ戻り、
**`S3` には戻らない** (`ensure_subtitle_track` は役割から置き場所を引くため)。
これは既存仕様の素直な帰結であり、変更しない。

### R9 ステータス通知 (プレビュー下のステータス行)

| 状況 | 文言 |
| --- | --- |
| 成功 (1 件) | `新しいテロップトラック({track})へ移しました` |
| 成功 (複数) | `{count} 件を新しいテロップトラック({track})へ移しました` |
| 一部が入らなかった | 上記 + ` ({count} 件は時間が重なるため残しました)` |
| 上限到達 | `テロップトラックはこれ以上追加できません (上限 {max} 本)` |
| 対象なし / ロック | `移動できるテロップがありません` |

### R10 空き領域メニュー (今回は対象外)
字幕トラックの空き領域から「空のトラックだけ足す」操作は要望に含まれないため作らない。
新トラックが欲しい場合は、テロップを 1 本移してから
そのトラックの空き領域で「字幕追加」する運用になる
(このとき追加される字幕の役割が **配信者** になるのが §4.5 の修正)。

### R11 機能 OFF
`new_track_enabled` が `false` のときはメニュー項目を出さない (完全に従来どおり)。

### R12 保存・読み込み
`Track.to_dict` / `from_dict` は種別非依存のため **プロジェクトのスキーマ変更は不要**。
`S3` 以降もそのまま往復する (既存の `S2` と同じ扱い)。

---

## 4. 実装設計

変更対象は 6 ファイル + テスト 1 ファイル。既存の関数は削除せず、追加と最小修正のみ。

```text
src/timeline/model.py                     ← ① ID 払い出し / ② コメント判定の厳格化
src/timeline/commands.py                  ← ③ 新コマンド + トラック生成ヘルパ
src/gui/timeline/timeline_controller.py   ← ④ UI から呼ぶ入口
src/gui/timeline/timeline_view.py         ← ⑤ メニュー項目 / ⑥ _role_for_track 修正
src/timeline/builder.py                   ← ⑦ 設定の検証・既定補完
src/settings/settings_window.py           ← ⑧ DEFAULT_SETTINGS へ新キー
src/i18n/catalog_en.py                    ← ⑨ 英訳
tests/test_subtitle_new_track.py          ← ⑩ 新規テスト
```

### 4.1 `src/timeline/model.py`

#### (a) 字幕トラック ID の払い出しを追加 (`next_video_track_id` と同じ形)

```python
    # 新しい字幕トラックの ID を払い出す (ver9 resolve2 §R4)
    # index は既存字幕トラックの最大 +1。ただし min_index 未満にはせず、
    # 予約された番号・ID (コメント用トラック) は飛ばす。
    # 【要望 (追加)】2 トラック目はコメント専用のため、通常テロップ用は
    # min_index=3 で呼ばれ、S1 だけの状態でも S2 を奪わず S3 から始まる。
    def next_subtitle_track_id(self, prefix="S", min_index=1,
                               reserved_indexes=(), reserved_ids=()):
        index = max([t.index for t in self.subtitle_tracks()] or [0]) + 1
        index = max(index, _to_int(min_index, 1))
        used_ids = {t.id for t in self.tracks} | {str(i) for i in reserved_ids if i}
        reserved = {_to_int(i, 0) for i in reserved_indexes}
        while index in reserved or f"{prefix}{index}" in used_ids:
            index += 1
        return f"{prefix}{index}", index
```

#### (b) コメント用トラックの拾い直しを厳格化 (§1.5 問題B-2)

```python
    def subtitle_track_for_role(self, role,
                                comment_track_id=COMMENT_SUBTITLE_TRACK_ID,
                                enabled=True):
        if not enabled or str(role or "") != ROLE_COMMENT:
            return self.base_subtitle_track()
        track = self.track_by_id(comment_track_id)
        if track is not None and track.is_subtitle():
            return track
        # ID を設定で変えられている場合の拾い直し (ver3 resolve11 §5.1)。
        # ver9 resolve2: 「S1 以外ならどれでも」は、通常テロップ用に増やした
        # トラック (S3 …) をコメント用と誤認するためやめる。
        # 中身がすべてコメント役割のトラックだけを候補にする (空トラックは除く)。
        base = self.base_subtitle_track()
        for candidate in sorted(self.subtitle_tracks(), key=lambda t: t.index):
            if base is not None and candidate.id == base.id:
                continue
            if candidate.clips and all(
                    str(c.role or "") == ROLE_COMMENT for c in candidate.clips):
                return candidate
        return None
```

`None` を返した場合は従来どおり `ensure_subtitle_track` が
`comment_track_id` のトラックを新規作成する (挙動は変わらない)。

### 4.2 `src/timeline/commands.py` — 設定既定と生成ヘルパ

```python
_DEFAULT_SUBTITLE_TRACKS_CFG = {
    "role_track_enabled": True,
    "comment_track_id": COMMENT_SUBTITLE_TRACK_ID,
    "comment_track_name": "Comment",
    "comment_track_index": 2,
    "auto_move_on_role_change": True,
    # ver9 resolve2: 右クリックから足す通常テロップ用トラック
    "new_track_enabled": True,
    "new_track_id_prefix": "S",
    "new_track_name_format": "Subtitle {index}",
    # 2 トラック目はコメント専用のため 3 から払い出す (要望 (追加))
    "new_track_min_index": 3,
    "max_tracks": 8,
    "new_track_moves_selection": True,
}


# 通常テロップ用の字幕トラックを 1 本作って返す (ver9 resolve2 §R4)
# コメント用トラックとは独立。役割の印は持たせない。
# コメント用の番号 (既定 index 2 / S2) は予約として飛ばす (要望 (追加))。
def create_subtitle_track(timeline, cfg=None):
    cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(cfg or {})}
    track_id, index = timeline.next_subtitle_track_id(
        prefix=cfg["new_track_id_prefix"],
        min_index=cfg["new_track_min_index"],
        reserved_indexes=(cfg["comment_track_index"],),
        reserved_ids=(cfg["comment_track_id"],),
    )
    try:
        name = str(cfg["new_track_name_format"]).format(index=index)
    except (KeyError, IndexError, ValueError):
        # 書式が壊れていても画面は止めない (ID をそのまま名前にする)
        _logger.warning("字幕トラック名の書式が不正です: %r",
                        cfg["new_track_name_format"])
        name = track_id
    track = Track(track_id, TRACK_SUBTITLE, index, name=name)
    timeline.tracks.append(track)
    _logger.info("テロップトラックを追加しました: %s (%s)", track_id, name)
    return track
```

### 4.3 `src/timeline/commands.py` — 新コマンド

```python
# 字幕を新しいテロップトラックへ移す (ver9 resolve2 §R2)
# 役割 (role) は変えない = 色・配置はそのままで、アイコンも背景も付かない。
# 「同じ時刻に並べられる本数」を増やすだけの操作であり、出力の見た目は変わらない。
class MoveSubtitlesToNewTrack(Command):

    def __init__(self, clip_ids, subtitle_tracks_cfg=None):
        self._clip_ids = [i for i in (clip_ids or []) if i]
        self._cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(subtitle_tracks_cfg or {})}
        # 呼び出し側の案内文言用
        self.created_track_id = None
        self.moved = 0
        self.skipped = 0
        self.limit_reached = False
        self.label = (tr("新規テロップトラックへ移動") if len(self._clip_ids) <= 1
                      else tr("新規テロップトラックへ移動（{count} 件）",
                              count=len(self._clip_ids)))

    def apply(self, timeline):
        # 【重要】CommandStack.push は apply が False を返しても状態を戻さない
        # (例外時だけ _restore する)。そのため「移せるか」を全部決めてから
        # トラックを作る。空トラックだけが残る状態を作らない (D-7)。
        targets = self._movable(timeline)
        if not targets:
            return False
        if len(timeline.subtitle_tracks()) >= int(self._cfg["max_tracks"]):
            self.limit_reached = True
            _logger.info("字幕トラックの上限 (%s) に達しています", self._cfg["max_tracks"])
            return False

        track = create_subtitle_track(timeline, self._cfg)
        self.created_track_id = track.id
        for clip in targets:
            source = timeline.track_of_clip(clip.id)
            if source is None:
                continue
            source.remove_clip(clip.id)
            track.clips.append(clip)
            self.moved += 1
        track.sort_clips(timeline)
        return self.moved > 0

    # 1 本のトラックへ入れられる字幕を開始時刻順に選ぶ。
    # 互いに重なるものは入れられないため、早いもの勝ちで落とす (§R3)。
    def _movable(self, timeline):
        clips = []
        for clip_id in self._clip_ids:
            clip = timeline.clip_by_id(clip_id)
            if not isinstance(clip, SubtitleClip):
                continue                      # 映像・音声の混在選択は黙って読み飛ばす
            source = timeline.track_of_clip(clip.id)
            if source is None or source.locked:
                self.skipped += 1
                continue
            clips.append(clip)
        chosen, cursor = [], None
        for clip in sorted(clips, key=lambda c: c.timeline_start):
            if cursor is not None and clip.timeline_start < cursor - _EPS:
                self.skipped += 1
                continue
            chosen.append(clip)
            cursor = clip.timeline_end
        return chosen
```

### 4.4 `src/gui/timeline/timeline_controller.py` — 入口

```python
    # 選択した字幕を新しいテロップトラックへ移す (ver9 resolve2 §R2)
    # 戻り値 {"moved": int, "skipped": int, "track": str|None, "limit": bool}
    def move_subtitles_to_new_track(self, clip_ids):
        command = commands.MoveSubtitlesToNewTrack(
            clip_ids, subtitle_tracks_cfg=self._cfg["subtitle_tracks"])
        changed = self.execute(command)
        return {
            "moved": command.moved if changed else 0,
            "skipped": command.skipped,
            "track": command.created_track_id if changed else None,
            "limit": command.limit_reached,
        }
```

移動後も `clip.id` は変わらないため、選択状態の作り直しは不要
(`_prune_selection` も走らない)。

### 4.5 `src/gui/timeline/timeline_view.py`

#### (a) メニュー項目 (`_add_align_actions` の直後に 1 行足す)

```python
        # 字幕の開始をカット頭へ揃える (ver8 resolve6 R5)
        self._add_align_actions(menu, clip)
        # 新しいテロップトラックへ移す (ver9 resolve2 §R1)
        self._add_subtitle_track_actions(menu, clip)
```

```python
    # 「新規テロップトラックへ移動」をメニューへ足す (ver9 resolve2 §R1)
    # 字幕以外では何も足さない。設定で OFF のときも足さない (§R11)。
    def _add_subtitle_track_actions(self, menu, clip):
        if not isinstance(clip, SubtitleClip):
            return
        cfg = self._controller.cfg["subtitle_tracks"]
        if not cfg["new_track_enabled"]:
            return
        ids = self._selected_subtitle_ids(clip) if cfg["new_track_moves_selection"] \
            else [clip.id]
        menu.addSeparator()
        label = (tr("新規テロップトラックへ移動") if len(ids) <= 1
                 else tr("選んだ {count} 件を新規テロップトラックへ移動", count=len(ids)))
        action = menu.addAction(label, lambda: self._move_to_new_subtitle_track(ids))
        # 上限に達していたら押せないようにし、理由をツールチップへ出す (§R5)
        limit = int(cfg["max_tracks"])
        if len(self._controller.timeline.subtitle_tracks()) >= limit:
            action.setEnabled(False)
            action.setToolTip(tr("テロップトラックはこれ以上追加できません (上限 {max} 本)",
                                 max=limit))

    # 選択中の字幕クリップ ID (無ければ右クリックしたもの 1 件)
    def _selected_subtitle_ids(self, clip):
        timeline = self._controller.timeline
        return [
            clip_id for clip_id in self._controller.selected_ids()
            if isinstance(timeline.clip_by_id(clip_id), SubtitleClip)
        ] or [clip.id]

    # 新しいテロップトラックへ移し、結果をステータスへ出す (§R9)
    def _move_to_new_subtitle_track(self, clip_ids):
        result = self._controller.move_subtitles_to_new_track(clip_ids)
        if result["limit"]:
            self.status_message.emit(
                tr("テロップトラックはこれ以上追加できません (上限 {max} 本)",
                   max=int(self._controller.cfg["subtitle_tracks"]["max_tracks"])))
            return
        if not result["moved"]:
            self.status_message.emit(tr("移動できるテロップがありません"))
            return
        message = (tr("新しいテロップトラック({track})へ移しました", track=result["track"])
                   if result["moved"] == 1
                   else tr("{count} 件を新しいテロップトラック({track})へ移しました",
                           count=result["moved"], track=result["track"]))
        if result["skipped"]:
            message += tr(" ({count} 件は時間が重なるため残しました)",
                          count=result["skipped"])
        self.status_message.emit(message)
```

`_add_align_actions` の `ids` 算出と重複するため、
既存側も `_selected_subtitle_ids()` を使う形へ寄せる (挙動は同一)。

#### (b) `_role_for_track` の修正 (§1.5 問題B-1)

```python
    # 字幕トラックに対応する役割を返す
    # コメント用トラック (設定 comment_track_id) だけがコメント役割。
    # ver9 resolve2: 以前は「S1 以外ならコメント」としていたため、通常テロップ用に
    # 増やしたトラック (S3 …) で字幕を足すとアイコンと背景が付いてしまっていた。
    def _role_for_track(self, track):
        cfg = self._controller.cfg["subtitle_tracks"]
        if not cfg["role_track_enabled"]:
            return DEFAULT_ROLE
        return ROLE_COMMENT if track.id == cfg["comment_track_id"] else DEFAULT_ROLE
```

### 4.6 `src/timeline/builder.py` — 設定の検証

```python
def _subtitle_tracks_config(values):
    values = values if isinstance(values, dict) else {}
    track_id = str(values.get("comment_track_id", "") or "").strip()
    return {
        "role_track_enabled": bool(values.get("role_track_enabled", True)),
        "comment_track_id": track_id or COMMENT_SUBTITLE_TRACK_ID,
        "comment_track_name": str(values.get("comment_track_name", "") or "Comment"),
        "comment_track_index": _positive_int(values.get("comment_track_index"), 2),
        "auto_move_on_role_change": bool(values.get("auto_move_on_role_change", True)),
        # ver9 resolve2: 右クリックから足す通常テロップ用トラック
        "new_track_enabled": bool(values.get("new_track_enabled", True)),
        "new_track_id_prefix": str(
            values.get("new_track_id_prefix", "") or "S").strip() or "S",
        "new_track_name_format": str(
            values.get("new_track_name_format", "") or "Subtitle {index}"),
        # 2 トラック目はコメント専用のため既定 3 から (要望 (追加))
        "new_track_min_index": _positive_int(values.get("new_track_min_index"), 3),
        "max_tracks": _positive_int(values.get("max_tracks"), 8),
        "new_track_moves_selection": bool(
            values.get("new_track_moves_selection", True)),
    }
```

### 4.7 `src/settings/settings_window.py`

`DEFAULT_SETTINGS["timeline"]["subtitle_tracks"]` へ 6 キーを追記する。
入れ子の欠落キーは `_fill_timeline_nested_defaults` → `_fill_dict_defaults` が
既存の `setting.json` へ自動で書き足すため、**移行処理は不要**。

```python
        "subtitle_tracks": {
            "role_track_enabled": True,
            "comment_track_id": "S2",
            "comment_track_name": "Comment",
            "comment_track_index": 2,
            "auto_move_on_role_change": True,
            # テロップノードの右クリックから通常テロップ用のトラックを足す
            # (ver9 resolve2)。コメントと違い、アイコンや背景は付かない。
            "new_track_enabled": True,
            # 新トラックの ID と名前 (index は既存字幕トラックの最大 +1)
            "new_track_id_prefix": "S",
            "new_track_name_format": "Subtitle {index}",
            # 払い出しを始める番号。2 トラック目 (S2) はコメント専用のため 3 から。
            # comment_track_index / comment_track_id は常に予約として飛ばす。
            "new_track_min_index": 3,
            # 字幕トラックの本数上限 (行が増えすぎて編集しづらくなるのを防ぐ)
            "max_tracks": 8,
            # 複数選択しているとき、選択中の字幕をまとめて移すか
            "new_track_moves_selection": True,
        },
```

設定画面への GUI 追加は行わない (既存の `subtitle_tracks` も `setting.json` 直編集
前提のため / 設定画面は役割色などの頻用項目に限っている)。

### 4.8 `src/i18n/catalog_en.py`

`tests/test_i18n.py` が `tr()` の原文と対訳の一致を検査するため、
追加した文言をすべて登録する。

| 原文 | 英訳 |
| --- | --- |
| `新規テロップトラックへ移動` | `Move to a new subtitle track` |
| `新規テロップトラックへ移動（{count} 件）` | `Move to a new subtitle track ({count})` |
| `選んだ {count} 件を新規テロップトラックへ移動` | `Move the {count} selected subtitles to a new track` |
| `新しいテロップトラック({track})へ移しました` | `Moved to the new subtitle track ({track})` |
| `{count} 件を新しいテロップトラック({track})へ移しました` | `Moved {count} subtitles to the new track ({track})` |
| ` ({count} 件は時間が重なるため残しました)` | ` ({count} kept in place because of overlap)` |
| `テロップトラックはこれ以上追加できません (上限 {max} 本)` | `No more subtitle tracks can be added (limit: {max})` |
| `移動できるテロップがありません` | `There are no subtitles that can be moved` |

---

## 5. setting.json 定義 (追加分)

| キー | 型 | 既定 | 意味 |
| --- | --- | --- | --- |
| `timeline.subtitle_tracks.new_track_enabled` | bool | `true` | 右クリックの「新規テロップトラックへ移動」を出すか |
| `timeline.subtitle_tracks.new_track_id_prefix` | str | `"S"` | 新トラック ID の接頭辞 (`S3`, `S4` …) |
| `timeline.subtitle_tracks.new_track_name_format` | str | `"Subtitle {index}"` | 新トラックの表示名の書式 (`{index}` が入る) |
| `timeline.subtitle_tracks.new_track_min_index` | int | `3` | 払い出しを始める番号。**2 トラック目をコメント専用に空けるための予約** (要望「(追加)」)。`comment_track_index` / `comment_track_id` は値に関わらず常に飛ばす |
| `timeline.subtitle_tracks.max_tracks` | int | `8` | 字幕トラックの本数上限 (S1・コメント用も数える) |
| `timeline.subtitle_tracks.new_track_moves_selection` | bool | `true` | 複数選択中に選択分をまとめて移すか (`false` で右クリックした 1 件のみ) |

既存キー (`role_track_enabled` / `comment_track_id` / `comment_track_name` /
`comment_track_index` / `auto_move_on_role_change`) は**変更しない**。

---

## 6. 既存挙動への影響と互換性

| 項目 | 影響 |
| --- | --- |
| 既存プロジェクトの読み込み | なし。スキーマ変更なし (§R12) |
| コメント用トラック (`S2`) の枠 | 新規トラックは `S2` / index 2 を**絶対に占有しない** (§R4)。コメント化はいつでも従来どおり `S2` を作れる |
| 既存の `setting.json` | 新キーが初回起動時に自動追記される (既存値は温存 / §4.7) |
| コメント化 (役割変更) の動作 | 変更なし。ただし `S2` が無く「中身が全部コメントのトラック」も無い場合は、従来 `S3` 等へ流れていたのが**`S2` を新規作成する**ようになる (本来の意図どおりの修正 / §4.1-b) |
| 字幕トラックの空き領域で「字幕追加」 | `S2` 以外 (= `S3` 以降) では役割が**コメント → 配信者**に変わる (§4.5-b)。コメント用トラック `S2` 上では従来どおりコメント |
| 焼き込み / プレビュー / Resolve 連携 | 変更なし (`subtitle_clips()` 経由 / §1.3) |
| アーカイブ切り抜き | 変更なし。`archive/timeline_builder.py` は元のトラック構成を ID ごと複製するため、`S3` 以降もそのまま運ばれる (L331-336) |
| リップル削除・貼り付け | 変更なし。`ripple_target_tracks` は音声以外の全トラックを対象にするため新トラックも追従する |
| コピー＆ペースト | `S3` のテロップをコピーして `S3` が無いプロジェクトへ貼ると `S1` (役割に対応するトラック) へ入る (`PasteClips._resolve_track` の既存挙動) |
| 旧バージョンのアプリで開いた場合 | `S3` は普通の字幕トラックとして読めるが、旧 `_role_for_track` / 旧 `subtitle_track_for_role` の推測により**コメント扱いされる**恐れがある。ダウングレードは非推奨 |

---

## 7. 既知の制約 (今回は変更しない)

### 7.1 縦動画プロジェクトの作成で字幕トラックが 1 本へ潰れる
`timeline/vertical_builder.py:205 _copy_subtitles` は、すべての字幕トラックの
クリップを新しい `S1` 1 本へまとめます。同時刻に重なるテロップがあると、
保存 → 読み込み時に `project_io._validate_overlaps` が後ろ側を右へ押し出します。

これは**コメント用トラック (`S2`) でも現状すでに起きている**挙動で、
今回の要望の範囲外のため変更しません。
直す場合は `archive/timeline_builder.py` と同じく元のトラック構成を複製する形へ寄せます
(→ §10 Q3)。

### 7.2 SRT 書き出しは重なりを表現できない
`export/srt_writer.py` / `resolve_export.py` は字幕を時系列の 1 列として書くため、
同時刻に 2 本あると SRT 上では連続した 2 エントリになります
(`S2` の既存挙動と同じ)。焼き込み (ASS) では正しく同時表示されます。

### 7.3 同じ位置に重なって描かれる
役割を変えない設計 (D-1) のため、`S1` と `S3` のテロップが同時刻にあると
**同じ配置 (既定: 下中央) に重なって**焼かれます。
位置はプレビューでドラッグして個別に決められます
(`clip.transform` → ASS の `\pos` / プレビューと焼き込みで同じ位置になる既存仕組み)。
自動でずらす案は検討のうえ**不採用**とした (§10.1 Q1)。この重なりは仕様として残す。

---

## 8. エラー処理・ログ方針

| 事象 | 扱い |
| --- | --- |
| 対象が字幕でない / 選択が空 | コマンドを積まず `False`。ステータスで通知 (R9)。ログは出さない |
| 元トラックがロック | その 1 件を飛ばして続行 (`skipped`)。`INFO` は出さずステータスのみ |
| 移動先で時間が重なる | 元トラックへ残す (`skipped`)。ステータスで件数を通知 |
| トラック上限到達 | `_logger.info("字幕トラックの上限 (%s) に達しています", …)` + ステータス |
| トラック名の書式が不正 | `_logger.warning` の上、ID を名前に使って続行 (画面は止めない) |
| コマンド内の予期せぬ例外 | 既存の `CommandStack.push` が `_logger.exception` + `_restore` で巻き戻す (追加実装なし) |
| トラック追加成功 | `_logger.info("テロップトラックを追加しました: %s (%s)", id, name)` |

ログは既存の `utils.logger.get_logger(__name__)` を使う。新しい出力先は作らない。

---

## 9. テスト計画

### 9.1 新規 `tests/test_subtitle_new_track.py` (Qt 非依存)

| # | 検証内容 |
| --- | --- |
| T1 | **`S1` だけの状態**で実行すると `S2` ではなく **`S3` (index 3)** が生え、対象クリップがそこへ移る (要望「(追加)」) |
| T2 | 移動後も `role` / `text` / `color` / `transform` / `clip.id` が変わらない (**装飾が付かないことの根拠**) |
| T3 | Undo 1 手でトラックの新設とクリップの移動が同時に戻る |
| T4 | `S2` (コメント) がある状態で足すと `S3` / `index=3` になり、行順が `S1 → S2 → S3` |
| T5 | `max_tracks` 到達時は `False` + `limit_reached` で、**トラックが増えない** |
| T6 | 元トラックがロックされているクリップは移らず、Timeline が変化しない |
| T7 | 互いに重なる 2 件を選ぶと早い 1 件だけ移り、もう 1 件は元トラックに残る (`skipped == 1`) |
| T8 | 1 件も移せないときは**空トラックが残らない** (D-7) |
| T9 | `next_subtitle_track_id` が既存 ID と衝突しない (`S3` 使用中なら `S4`) |
| T9b | `S1` + `S3` の状態で足すと `S4` になり、**`S2` は欠番のまま残る** (予約の維持) |
| T9c | T1 の後にテロップをコメント化すると `S2` が生え、行順が `S1 → S2 → S3` になる (2 行目がコメントになる) |
| T9d | `comment_track_id` / `comment_track_index` を設定で `S5` / `5` へ変えた場合、その番号・ID を飛ばして払い出す |
| T10 | 保存 → 読み込みで `S3` が往復し、`S1` と時間が重なっても押し出されない |
| T11 | `subtitle_track_for_role("comment")` が、通常テロップの入った `S3` を拾わない (§4.1-b) |
| T12 | 中身が全部コメントのトラックは従来どおり拾う (旧救済の維持) |

### 9.2 既存テストへの追加

| ファイル | 追加内容 |
| --- | --- |
| `tests/test_subtitle_comment_track.py` | `S2` が無く `S3` (通常テロップ入り) だけある状態でコメント化すると、`S3` ではなく `S2` が新規作成されることを確認 |
| `tests/test_settings_migration.py` | 旧 `setting.json` に新 6 キーが自動追記されること / 既存値が温存されること |
| `tests/test_i18n.py` | 追加文言が対訳表にそろっていること (既存の検査でカバー) |

### 9.3 ASS 出力の確認

`role` を変えないため、既存の `tests/test_comment_decor.py` /
`test_comment_bg_width.py` / `test_comment_placement.py` は無変更で通る想定。
加えて、`role="streamer"` のまま別トラックへ移した字幕が
**背景行 (`comment_bg_layer`) を 1 行も出さない**ことを
`generate_ass_file` の出力行で確認するケースを 1 つ足す (T2 の出力側の裏付け)。

### 9.4 手動確認手順

1. Timeline 編集画面でテロップを右クリック → 「新規テロップトラックへ移動」。
2. **`S2`(Comment) が無いプロジェクトでも `S3` 行として現れる**こと (要望「(追加)」)。
   その後そのテロップをコメント化すると `S2` が生え、行順が `S1 → S2 → S3` になること。
3. プレビューで**アイコン・黒半透明背景・「コメント：」が付かない**こと。
4. `S1` 側に同じ時刻のテロップを足し、**同時に 2 本表示される**こと。
5. Ctrl+Z 1 回でトラックごと元に戻ること。
6. 保存 → 再読込で `S3` が残ること。
7. 書き出し (焼き込み) で画面と同じ見え方になること。
8. 上限 (既定 8 本) に達するとメニューが押せなくなること。

```powershell
python -m unittest discover -s tests
```

---

## 10. 要確認事項 (推測実装せず確認したい点)

| # | 論点 | 設計の既定 | 代案 |
| --- | --- | --- | --- |
| ~~**Q1**~~ | 新トラックへ移したテロップの**表示位置**。`S1` のテロップと同時刻だと同じ場所 (下中央) に重なって出ます | **【確定 2026-10-25】位置は変えない** (D-6)。重なりが邪魔なときだけプレビューでドラッグして決める | (不採用) 設定 `new_track_offset_v_px` で段数 × px ぶん自動的に上へずらす案 — 設定とコードが増え、既存プロジェクトの見え方を勝手に変えるため採らない |
| **Q2** | メニューの**文言**。「新規テロップトラックへ移動」で意図は伝わるか | 上記 | 「テロップトラックを追加してここへ移す」「別のテロップトラックへ出す」など |
| **Q3** | 縦動画プロジェクト作成時に字幕トラック構成が 1 本へ潰れる既存挙動 (§7.1) を、この機会に直すか | 直さない (要望の範囲外) | `archive/timeline_builder.py` と同じくトラック構成を複製する (別要望として起票) |
| **Q4** | 既存トラックへの移動 (例: `S1` ⇄ `S3` を行き来) も要るか | 今回は「新規トラックへ移す」だけ | 「テロップトラックへ移動 ▶ S1 / S3 / 新規」のサブメニューに拡張 (R2 の実装をそのまま再利用できます) |
| **Q5** | 上限 8 本は妥当か (行が増えると編集画面が狭くなります) | 8 | `setting.json` で変更可能 (§5) |
| ~~**Q6**~~ | コメント用トラック (`S2`) は必要になって初めて生えるため、`S1` だけのプロジェクトで新規トラックを足すと `S3` ではあるが画面上は 2 行目に見える点をどう扱うか | **【確定 2026-10-25】番号だけ予約**し、空の `S2` は作らない (§R4 / D-7)。コメント化した時点で `S2` が生えて必ず 2 行目へ入る | (不採用) 新規トラック作成時に空のコメント用 `S2` も一緒に作る案 — 使っていないコメント行が常時 1 行占めるため採らない |

**見え方に関わる Q1・Q6 は確定済み (→ §10.1)。設計のまま実装に入れます。**
Q2〜Q5 は既定のまま進めても後から設定・サブメニューで足せます。

### 10.1 確定した判断

| 日付 | 項目 | 決定 |
| --- | --- | --- |
| 2026-10-25 | Q6 / 新規トラックの番号 | **番号 (index 2 / `S2`) を予約するだけ**とし、空のコメント用トラックは作らない。§R4・§4.1・§4.2 の設計がこれにあたる (変更なし) |
| 2026-10-25 | Q1 / 移動後の表示位置 | **変更しない**。`transform` を触らず、重なりはプレビューのドラッグで調整する運用。`new_track_offset_v_px` は**追加しない** (§5 の設定キーは 6 つのまま / §7.3 は仕様として残る) |

---

## 11. 実装手順 (レビュー後)

1. `model.py` : `next_subtitle_track_id()` 追加 / `subtitle_track_for_role()` 厳格化
2. `commands.py` : 既定 cfg へ 6 キー / `create_subtitle_track()` / `MoveSubtitlesToNewTrack`
3. `builder.py` : `_subtitle_tracks_config()` に検証追加
4. `settings_window.py` : `DEFAULT_SETTINGS` へ 6 キー
5. `timeline_controller.py` : `move_subtitles_to_new_track()`
6. `timeline_view.py` : メニュー項目 / `_role_for_track` 修正 / `_selected_subtitle_ids` 共通化
7. `catalog_en.py` : 英訳 8 件
8. `tests/test_subtitle_new_track.py` 新規 + 既存テスト追補
9. `python -m unittest discover -s tests` で全件確認 → 手動確認 (§9.4)

想定変更規模: 追加約 150 行 / 修正約 20 行 / テスト約 200 行。既存コードの削除はなし。
