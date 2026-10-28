# ver9 resolve10 — 新規テロップトラックへ移したテロップが、役割変更で S1 へ戻る

報告: 2026-10-28 のチャット
> 以下のバグが発生しています。
> ①新規テロップトラックを作成し、新規テロップトラックにテロップを移す。
> ②役割やフォントの色等を変更する
> ③テロップがS1に戻る。

対象: `src/timeline/commands.py` (本体) / `src/timeline/model.py` /
`src/gui/timeline/timeline_editor_dialog.py` (引き金) /
`src/timeline/builder.py` + `src/settings/settings_window.py` + `src/settings/setting.json` (設定)
調査日: 2026-10-28 (main `115e257` / v1.6.1 + ver9 resolve2〜resolve9 実装後)
種別: **調査報告 + 修正設計書 + 実装記録**。§1・§2 の挙動はすべて実機で再現済み。
**実装済み** (2026-10-28 / §10 が記録)

---

## 0. 先に結論

### 0.1 3 行で

1. **原因は `ChangeSubtitleRole` が「テロップの置き場所を `role` だけで決め直す」こと。**
   `role` が「コメント」以外のときの行き先は `subtitle_track_for_role()` →
   `base_subtitle_track()` = **常に S1**。つまり利用者が S3 へ置いたテロップでも、
   役割を触った瞬間に「この役割の置き場は S1 だ」と判断して S1 へ引き戻します (§2.1)。
2. **「役割を変えていないつもり」でも起きる。** 役割コンボは `currentIndexChanged`
   で繋がっているため、インスペクタ上でマウスホイールを回した・矢印キーを押した
   だけでも役割変更コマンドが飛びます。隣のフォントコンボは同じ事故を避けるため
   `activated` にしてあるのに、役割だけ対策が入っていません (§2.4)。
3. **文字色・縁の色・フォント・サイズの変更自体は無罪** (`EditSubtitle(s)` は
   トラックを一切触らない)。実機で確認済み (§2.5)。保存・読み込みも無罪 (§2.6)。
   よって直すのは **役割変更の移動規約 1 点 + 引き金のコンボ 1 点** です。

### 0.2 再現結果 (実測)

S1 に置いたテロップ `c1` を「新規テロップトラックへ移動」で S3 へ移したあとの挙動。

| # | ②でした操作 | `c1` の居場所 | 判定 |
| --- | --- | --- | --- |
| A | 文字色を変える (`&H000000FF`) | **S3 のまま** | 正常 |
| B | フォントを変える | **S3 のまま** | 正常 |
| C | サイズ・文字寄せを変える | **S3 のまま** | 正常 |
| D | 役割を「配信者」→「サブ」 | **S1 へ戻る** | **不具合** |
| E | 役割を「配信者」→「配信者」(同じ役割の再選択) | **S1 へ戻る** | **不具合**。役割は変わっていないのに移動する |
| F | 役割を「配信者」→「コメント」 | S3 のまま | 別の不具合 (§2.7。S3 がコメント用トラックと誤認される) |
| G | D と同じ操作で、S1 の同時刻に別テロップが居る場合 | S3 のまま | 戻る / 戻らないが状況で変わる (§2.3) |
| H | 保存 → 読み込み | S3 のまま | 正常 |

### 0.3 対策 (この順で効く)

| # | 対策 | 効果 |
| --- | --- | --- |
| **1** | 役割変更にともなう自動移動を、**役割が面倒を見るトラック (S1 / コメント用 S2) に居るときだけ**行う。利用者が増やしたトラック (S3 以降) のテロップは動かさない | 報告の症状が止まる (D・E・G) |
| **2** | 役割が実際に変わっていないなら**何もしない** (移動も履歴積みもしない) | E が止まる。無意味な Undo 履歴も消える |
| **3** | 移動先の判定を **`clip.role` を書き換える前**に行う | F の誤認を半分直す |
| **4** | コメント用トラックの「拾い直し」を**コメント用の番号 (既定 index 2) に限る** | F の誤認を完全に直す |
| **5** | 役割コンボを `currentIndexChanged` → **`activated`** へ (フォントコンボと同じ流儀) | 「②で役割を触っていないのに戻る」事故が止まる |

対策 1 は設定 `timeline.subtitle_tracks.role_move_scope` で従来挙動へ戻せるようにします (§3.2)。

---

## 1. どう再現したか

GUI を含めた経路で再現しました (`QT_QPA_PLATFORM=offscreen`)。

```python
ctrl = TimelineController(timeline, {})          # S1 に c1 (role=streamer) だけ
panel = ted._InspectorPanel(ctrl)                # インスペクタ (役割・色・フォント)
ctrl.selection_changed.connect(panel.show_clip)
ctrl.timeline_changed.connect(panel.refresh)

ctrl.select(["c1"])
ctrl.move_subtitles_to_new_track(["c1"])         # ① → S3 が生えて c1 は S3 へ

panel._on_color_changed("&H000000FF")            # ② 文字色   → S3 のまま
panel._on_font_changed()                         # ② フォント → S3 のまま
panel.role_combo.setCurrentIndex(
    panel.role_combo.findData("sub"))            # ② 役割     → ★S1 へ戻る
```

実行結果:

```
移動前: S1 / combo = streamer
新規トラックへ移動: {'moved': 1, 'skipped': 0, 'track': 'S3', 'limit': False} -> S3
文字色変更後: S3   color = &H000000FF
フォント変更後: S3
役割をサブへ変更後: S1          ← ③ の症状
残トラック: ['S1', 'S3']        ← 空になった S3 だけが残る
```

コマンド単体でも同じです (`ChangeSubtitleRole` を直接積んだ場合)。

```
② 役割を 'sub'      へ -> 移動前 S3 / 移動後 S1 (moved=True)
② 役割を 'streamer' へ -> 移動前 S3 / 移動後 S1 (moved=True)   ← 役割は変わっていない
② 役割を 'comment'  へ -> 移動前 S3 / 移動後 S3 (moved=False)  ← §2.7
```

---

## 2. 何が起きているか

### 2.1 本体: 置き場所を `role` だけで決め直している

`src/timeline/commands.py:1577 ChangeSubtitleRole.apply()`

```python
changed = clip.role != self._role
clip.role = self._role
...
source = timeline.track_of_clip(clip.id)
target = ensure_subtitle_track(timeline, self._role, self._cfg)   # ← ここ
if source is None or target is None or target.id == source.id:
    return changed
...
source.remove_clip(clip.id)
target.clips.append(clip)                                         # ← S1 へ移る
```

`ensure_subtitle_track()` → `model.subtitle_track_for_role()` の最初の 2 行:

```python
def subtitle_track_for_role(self, role, comment_track_id=..., enabled=True):
    if not enabled or str(role or "") != ROLE_COMMENT:
        return self.base_subtitle_track()        # ← 非コメントは必ず S1
```

つまりこの関数は **「役割 → 置き場所」の片道写像**しか持っていません。
ver3 resolve11 で「コメントを S2 へ逃がす」ために作られた関数で、当時は
字幕トラックが **S1 と S2 の 2 本しかなかった**ため、「非コメント = S1」で
何も壊れませんでした。

ver9 resolve2 で **S3 以降を利用者が足せる**ようにしたとき、
`MoveSubtitlesToNewTrack` 側は「役割は変えない」という規約 (resolve2 §2 D-1) を
守りましたが、**逆向き (役割を変えたときに置き場所をどうするか) の規約が
更新されないまま残りました**。これが本件です。

> resolve2 §1.5 問題B で「S1 以外 = コメント」という推測を 2 箇所潰しましたが、
> 潰したのは**コメント化の行き先**だけで、**非コメント化の行き先が常に S1** という
> もう一方の決め打ちは手付かずでした。

### 2.2 なぜ「S1 へ戻る」という見え方になるのか

テロップの見た目 (色・縁・配置・アイコン・背景) を決めるのは `clip.role` だけで、
トラックは「同じ時刻に何本並べられるか」しか決めません (resolve2 §1.1 / §1.3)。
そのため利用者が S3 へ移す動機は **「同じ時刻に 2 本並べたい」** の一点です。

それが役割変更で S1 へ戻されると:

* S1 の同時刻が空いていれば → 黙って S1 へ移動し、**並べたかった 2 本が 1 本の列に戻る**
* S3 は空のまま残る (行は残るのでテロップだけ消えたように見える)
* 案内は「コメントトラックへ移しました」の文言しか無く、
  S1 へ引き戻したときは**何も出ない** (`_on_role_changed` は `moved` を
  コメント移動としてしか案内しない)

結果、利用者から見ると「勝手に S1 に戻った」になります。

### 2.3 戻るときと戻らないときがある

移動先 (S1) の同時刻が埋まっていると `_overlaps_any()` に引っかかり、
`move_blocked = True` で**役割だけ変わります** (実測 G)。

```
B S1 が埋まっている: S3 のまま  moved=False blocked=True
```

これは「S1 が空いているときだけ戻る」= 再現条件が分かりづらい形になっており、
報告の「役割やフォントの色等を変更する」という曖昧さの一因になっています。

### 2.4 引き金: 役割コンボが `currentIndexChanged` で繋がっている

`src/gui/timeline/timeline_editor_dialog.py:1535`

```python
self.role_combo = QComboBox()
for label, value in ((tr(t), v) for t, v in _ROLE_CHOICES):
    self.role_combo.addItem(label, value)
self.role_combo.currentIndexChanged.connect(self._on_role_changed)   # ← ここ
...
self.font_combo = QComboBox()
...
# activated = 利用者が選び直したときだけ。currentIndexChanged だと
# 一覧を矢印キーで見て回るだけで Undo 履歴が埋まってしまう。
self.font_combo.activated.connect(self._on_font_changed)             # ← 対策済み
```

**フォントコンボだけ対策が入っていて、役割コンボには入っていません。**
`QComboBox` は既定でホイールで選択が変わる (Windows の
`SH_ComboBox_AllowWheelScrolling` が有効) ため、

* インスペクタの上でホイールを回した
* コンボを開かずに矢印キーを押した

だけで役割変更コマンドが飛びます。フォントの場合は「Undo 履歴が増える」だけで
済みますが、**役割の場合はテロップが S1 へ移動する**ため被害が段違いです。

インスペクタは `QScrollArea` に入っておらず、ホイールを無効化する
イベントフィルタも入っていないため (`grep wheelEvent` の該当は
`timeline_view.py` の 1 箇所だけ)、この事故は起こり得ます。

報告の「②役割や**フォントの色等**を変更する」は、色やフォントを触っている最中に
ホイール・矢印でコンボの値が動いたケースを含んでいると考えられます。

### 2.5 文字色・フォント・サイズは無罪 (確認済み)

| 操作 | 経路 | トラックを触るか |
| --- | --- | --- |
| 字幕本文 | `_on_text_changed` → `EditSubtitle(text=)` | 触らない |
| フォント | `_on_font_changed` → `EditSubtitle(font=)` | 触らない |
| サイズ | `_on_size_changed` → `EditSubtitle(font_size=)` | 触らない |
| 文字色 / 縁の色 | `_on_color_changed` → `EditSubtitles(color=)` | 触らない |
| 文字寄せ | `_on_align_changed` → `EditSubtitles(align=)` | 触らない |

`EditSubtitle` / `EditSubtitles.apply()` は `setattr` だけで、
`remove_clip` / `clips.append` を一切呼びません
(クリップを別トラックへ移すコードは `commands.py` 以外に存在しない
= `grep remove_clip` で確認)。

`ColorField.set_value()` も `_updating` で囲われていて画面側からの流し込みでは
`color_committed` を出さず、`_InspectorPanel.show_clip()` も全体を `_updating` で
囲っているため、**再描画起因で編集コマンドが飛ぶことはありません**。

### 2.6 保存・読み込みも無罪 (確認済み)

S3 に載せたテロップを保存して読み直すと、トラック構成も居場所も色も保たれます。

```
保存→読込: ['S1', 'S3'] / c1 = S3 / color = &H000000FF
```

アーカイブ切り抜きの複製 (`archive/timeline_builder.py:455-488`) も
元のトラック構成をそのまま複製する実装になっており、無罪です。

### 2.7 副作用: S3 がコメント用トラックと誤認される (実測 F)

`ChangeSubtitleRole.apply()` は **`clip.role` を書き換えてから**行き先を探します。

```python
clip.role = self._role                       # ← 先に書き換える
...
target = ensure_subtitle_track(timeline, self._role, self._cfg)
```

`subtitle_track_for_role()` のコメント役割の拾い直しは
「中身が**すべて**コメント役割のトラック」を候補にします (resolve2 §4.1-b)。
S3 にテロップが 1 本だけ載っている状態でそれをコメント化すると、
**判定の時点で S3 の中身は全部コメント**になっているため、
S3 自身がコメント用トラックとして拾われます。

```
C コメント化: S3 / S2 = None / コメント用として拾われるトラック = S3
```

この時点で S2 は作られず、**以後 S1 のテロップをコメント化すると
通常テロップ用に作った S3 へ流れ込みます**。
resolve2 §1.5 問題B-2 で直したはずの症状が、別の入口から再発する形です。

---

## 3. 対策

### 3.1 方針

| # | 方針 | 理由 |
| --- | --- | --- |
| **D-1** | テロップの置き場所は、**最後に置き場所を指定した人の意思**を優先する | 利用者が S3 へ移したのは「同時刻に並べたい」という明示の意思。役割変更はそれを覆す理由にならない |
| **D-2** | 役割変更にともなう自動移動は**「S1 ↔ コメント用 S2」の往復に限る** | これが ver3 resolve11 で自動移動を入れた本来の目的 (コメントを通常テロップと同時に出す) |
| **D-3** | 役割が変わっていないなら**コマンドを成立させない** | `CommandStack.push` は `apply` が `False` を返せば履歴を汚さない。変化が無い操作で履歴と状態を動かさない |
| **D-4** | 挙動は設定で戻せるようにする | 既存プロジェクト・既存利用者の慣れを壊さない (CLAUDE.md「既存実装を破壊しない」) |

### 3.2 対策 1 (本命): 自動移動の範囲を限定する

`src/timeline/commands.py` に「役割が面倒を見るトラックか」の判定を足します。
これは既存の `is_removable_subtitle_track()` の裏返し
(= 消せないトラック = 役割が面倒を見るトラック) ですが、
`role_track_enabled=False` のときの扱いが違うため関数は分けます。

```python
# 役割変更の自動移動が面倒を見るテロップトラックか (ver9 resolve10 §3.2)
#
# 見るのは 2 本だけ:
#   ・既定トラック (index 最小 / 通常 S1) … コメントを解除したときの戻り先
#   ・コメント用トラック (既定 S2)        … コメント化したときの移動先
# 利用者が増やしたトラック (S3 以降) は「同時刻に並べたいから自分で置いた」
# 場所であり、役割を変えても動かさない (置き場所の指定を役割で上書きしない)。
def is_role_managed_subtitle_track(timeline, track, cfg=None):
    cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(cfg or {})}
    if track is None or not track.is_subtitle():
        return False
    base = timeline.base_subtitle_track()
    if base is not None and track.id == base.id:
        return True
    return (track.id == cfg["comment_track_id"]
            or track.index == _positive_int(cfg["comment_track_index"], 2))
```

設定キーを 1 つ足します (`timeline.subtitle_tracks`)。

| キー | 既定 | 意味 |
| --- | --- | --- |
| `role_move_scope` | `"role_tracks"` | `"role_tracks"` = S1 / コメント用トラックに居るテロップだけ自動移動する (新しい既定)。`"all"` = どのトラックに居ても移動する (v1.6.1 までの挙動) |

`auto_move_on_role_change: false` で自動移動を丸ごと切れる既存キーは残します
(`role_move_scope` はその内側の絞り込み)。

### 3.3 対策 2・3 を含めた `ChangeSubtitleRole` の書き換え

```python
class ChangeSubtitleRole(Command):

    label = "字幕の役割変更"

    def __init__(self, clip_id, role, subtitle_tracks_cfg=None):
        self._clip_id = clip_id
        self._role = str(role or DEFAULT_ROLE)
        self._cfg = {**_DEFAULT_SUBTITLE_TRACKS_CFG, **(subtitle_tracks_cfg or {})}
        self.moved = False
        self.move_blocked = False
        # ver9 resolve10: 置き場所を尊重して移動しなかったか (案内文言用)
        self.move_skipped = False

    def apply(self, timeline):
        clip = timeline.clip_by_id(self._clip_id)
        if not isinstance(clip, SubtitleClip):
            return False
        # ★対策2: 役割が変わらないなら何もしない。
        #   以前は「役割は同じだが移動はする」経路があり、コンボを選び直した
        #   だけでテロップが S1 へ動いていた (resolve10 §0.2 実測 E)。
        if clip.role == self._role:
            return False
        source = timeline.track_of_clip(clip.id)
        # ★対策3: 行き先は clip.role を書き換える前に決める。
        #   書き換えてから探すと、増やしたトラックに載っている 1 本を
        #   コメント化したときに「中身が全部コメント」の条件へ自分が
        #   引っかかり、そのトラックがコメント用と誤認される (§2.7)。
        target = self._move_target(timeline, clip, source)
        clip.role = self._role
        if source is None or target is None or target.id == source.id:
            return True
        if target.locked or _overlaps_any(
                target, clip.timeline_start, clip.timeline_end, exclude_id=clip.id):
            self.move_blocked = True
            _logger.info("移動先 %s が空いていないため役割だけ変更しました: %s",
                         target.id, clip.id)
            return True
        source.remove_clip(clip.id)
        target.clips.append(clip)
        target.sort_clips(timeline)
        self.moved = True
        return True

    # 役割変更にともなう移動先を返す (移動しないときは None)。
    # 【重要】clip.role を書き換える前に呼ぶ (§3.3 対策3)。
    def _move_target(self, timeline, clip, source):
        cfg = self._cfg
        if source is None:
            return None
        if not (cfg["role_track_enabled"] and cfg["auto_move_on_role_change"]):
            return None
        # ★対策1: 利用者が増やしたトラックに置かれたテロップは動かさない
        if (str(cfg["role_move_scope"]) == "role_tracks"
                and not is_role_managed_subtitle_track(timeline, source, cfg)):
            self.move_skipped = True
            _logger.info("%s は利用者が置いたトラックのため役割だけ変更します: %s",
                         source.id, clip.id)
            return None
        return ensure_subtitle_track(timeline, self._role, cfg)
```

**変えないこと** (既存コメントの意図を維持):

* 役割の書き換えは必ず通す。移動は best-effort のまま
  (出力の見た目を決めるのは `role` であって置き場所ではない)
* 移動しても `clip.id` は変えない (選択・Undo・アーカイブ index の紐付け)

### 3.4 対策 4: コメント用トラックの拾い直しを番号で絞る

`src/timeline/model.py:691 subtitle_track_for_role()` の救済措置に番号の条件を
足します。引数は省略可にして既定の挙動は変えません
(既存の呼び出し・テストを壊さない)。

```python
    # comment_track_index を渡すと、拾い直しの候補をその番号のトラックに限る
    # (ver9 resolve10 §3.4)。通常テロップ用に増やしたトラック (S3 以降) が
    # 「中身が全部コメント」になった瞬間にコメント用と誤認されるのを防ぐ。
    def subtitle_track_for_role(self, role,
                                comment_track_id=COMMENT_SUBTITLE_TRACK_ID,
                                enabled=True, comment_track_index=None):
        ...
        for candidate in sorted(self.subtitle_tracks(), key=lambda t: t.index):
            if base is not None and candidate.id == base.id:
                continue
            if (comment_track_index is not None
                    and candidate.index != int(comment_track_index)):
                continue                      # ← 追加
            if candidate.clips and all(
                    str(c.role or "") == ROLE_COMMENT for c in candidate.clips):
                return candidate
        return None
```

`commands.ensure_subtitle_track()` から `cfg["comment_track_index"]` を渡します。
拾い直しが空振りすれば、従来どおり設定どおりの ID / 番号でコメント用トラックが
新規作成されます (= 意図した形)。

### 3.5 対策 5: 役割コンボを `activated` へ

`src/gui/timeline/timeline_editor_dialog.py:1538`

```python
        # activated = 利用者が選び直したときだけ (フォントコンボと同じ流儀)。
        # currentIndexChanged だと、インスペクタ上でホイールを回した・矢印キーを
        # 押しただけで役割変更コマンドが飛び、テロップが移動してしまう
        # (ver9 resolve10 §2.4)。
        self.role_combo.activated.connect(self._on_role_changed)
```

`show_clip()` 側の `setCurrentIndex()` は `activated` を出さないため、
`_updating` ガードに依存しなくなる = 二重に安全になります。

あわせて案内文言を足します (`_on_role_changed`)。

```python
        if result["moved"]:
            ...                                   # 既存 (コメントトラックへ移した)
        elif result["blocked"]:
            ...                                   # 既存
        elif result["skipped"]:
            self.status_message.emit(
                tr("テロップの置き場所はそのままで役割だけ変更しました"))
```

### 3.6 採らなかった案

| 案 | 却下理由 |
| --- | --- |
| トラックに「役割」を持たせ、役割ごとに専用トラックへ振り分ける | resolve2 D-1 (見た目は `role` だけが決める) を崩す。利用者が「通常テロップを 3 本並べる」運用ができなくなる |
| 役割変更時の移動を完全にやめる (`auto_move_on_role_change` を既定 false へ) | コメント化で同時刻に並べられる、という ver3 resolve11 の価値を失う。S1 の同時刻が埋まっていればコメント化そのものが成立しなくなる |
| 「元いたトラックを覚えて、役割を戻したら帰す」 | 状態 (元トラック) をクリップへ持たせることになり、保存形式の変更・コピー&ペースト・アーカイブ複製すべてに波及する。得るものに対して高すぎる |
| 移動のたびに確認ダイアログを出す | 1 クリックで終わる操作に確認を挟むのはこのアプリの流儀に合わない (`RemoveSubtitleTrack` のような破壊操作だけ確認する) |

---

## 4. 変更する場所

| ファイル | 変更 |
| --- | --- |
| `src/timeline/commands.py` | `_DEFAULT_SUBTITLE_TRACKS_CFG` へ `role_move_scope` 追加 / `is_role_managed_subtitle_track()` 追加 / `ChangeSubtitleRole` 書き換え (§3.3) / `ensure_subtitle_track()` から `comment_track_index` を渡す |
| `src/timeline/model.py` | `subtitle_track_for_role()` へ `comment_track_index=None` を追加 (§3.4) |
| `src/timeline/builder.py` | `_subtitle_tracks_config()` へ `role_move_scope` の検証を追加 (未知の値は `"role_tracks"` へ寄せる) |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["timeline"]["subtitle_tracks"]` へ `role_move_scope` 追加 (既存 setting.json へは `_fill_timeline_nested_defaults` が補完する) |
| `src/settings/setting.json` | 同キーを追記 |
| `src/gui/timeline/timeline_editor_dialog.py` | 役割コンボを `activated` へ (§3.5) / 案内文言 1 本追加 |
| `src/gui/timeline/timeline_controller.py` | `change_subtitle_role()` の戻り値へ `"skipped"` を追加 |
| `src/i18n/catalog_en.py` | 追加した案内文言の英訳 1 件 |

設定画面への UI 露出は不要と判断しました
(`drag_between_tracks` / `remove_track_enabled` も setting.json 直編集のみで、
既存の流儀に合わせます)。

---

## 5. テスト計画

`tests/test_subtitle_new_track.py` へ追加 (既存の `_cfg()` / `_timeline()` を流用)。

| # | 内容 | 期待 |
| --- | --- | --- |
| T20 | S3 のテロップの役割を `streamer` → `sub` へ | `track_of_clip` は **S3 のまま** / `role == "sub"` |
| T21 | S3 のテロップの役割を `streamer` → `comment` へ | S3 のまま / `S2` は作られない / `role == "comment"` |
| T22 | T21 の後、S1 の別テロップをコメント化 | **`S2` が新規作成されて**そこへ移る (S3 へ流れ込まない / §3.4) |
| T23 | S1 のテロップをコメント化 → 解除 (`streamer`) | `S2` へ移る → `S1` へ戻る (従来の価値が残っている) |
| T24 | 同じ役割を選び直す (`streamer` → `streamer`) | `push` が `False` / 居場所も履歴も動かない |
| T25 | `role_move_scope="all"` のとき S3 → 役割変更 | S1 へ移る (従来互換の確認) |
| T26 | `auto_move_on_role_change=False` | どこに居ても移動しない (既存キーが効き続ける) |
| T27 | Undo / Redo | T20・T23 のあと Ctrl+Z で居場所も役割も元へ戻る |
| T28 | 設定補完 | 旧 setting.json (`role_move_scope` 無し) に既定が入る / 既存値は温存 (`tests/test_settings_migration.py`) |
| T29 | GUI: 役割コンボの `setCurrentIndex()` ではコマンドが飛ばない | `offscreen` でインスペクタを作り、`setCurrentIndex` 後に `track_of_clip` が変わらない |

既存テストへの影響: `tests/test_subtitle_comment_track.py` の
`ChangeSubtitleRoleTest` は S1 / S2 だけを使うため全件そのまま通る見込み
(S1 ↔ S2 の往復は対策後も従来どおり)。
`tests/test_subtitle_new_track.py` の T9c・T11 は
「S1 のテロップをコメント化 → S2 が生える」形なので影響なし。
ただし T11 は `subtitle_track_for_role("comment")` を**引数なし**で呼んでいるため、
§3.4 の既定 (`comment_track_index=None`) を変えないことが前提です。

---

## 6. 影響範囲とリスク

| 対象 | 影響 |
| --- | --- |
| 出力 (焼き込み) | **なし**。ASS は `timeline.subtitle_clips()` で全トラックのテロップを集め、色・配置は `role` で決まる (resolve2 §1.3)。置き場所が変わらなくなるだけ |
| プレビュー | **なし** (同上) |
| Resolve 書き出し | **なし**。トラック構成はそのまま写すだけ |
| 保存形式 | **なし**。新しいフィールドは増えない (設定キーが 1 つ増えるのみ) |
| Undo / Redo | スナップショット方式のため自動で追従。むしろ「変化のない役割変更」が履歴に積まれなくなる分きれいになる |
| アーカイブ切り抜き | **なし**。複製はトラック構成をそのまま写す |
| 既存プロジェクト | **なし**。読み込み時に居場所を直す処理は入れない (今 S1 に居るものは S1 のまま) |
| 慣れの変化 | S3 のテロップをコメント化しても S2 へ行かなくなる。アイコン・背景は `role` が決めるので**見た目は正しくコメントになる**が、「コメントは必ず 2 行目」という見え方ではなくなる → §7 Q1 で確認したい |

リスクは低。最大の論点は上の最後の行だけです。

---

## 7. 不明点・確認したいこと

| # | 問い | 既定としてこうする提案 |
| --- | --- | --- |
| **Q1** | S3 に置いたテロップを「コメント」にしたとき、**S2 へ移すべきか / S3 に留めるべきか** | **留める**。S3 へ移したのは「同時刻に並べたい」意思であり、S2 へ動かすとその意思を壊す。見た目 (アイコン・背景・色) は `role` が決めるので正しくコメントになる |
| **Q2** | S1 に置いたテロップのコメント化は従来どおり S2 へ移して良いか | **良い** (この自動移動が ver3 resolve11 の目的そのもの)。T23 で担保 |
| **Q3** | 役割コンボをホイールで変えられなくする (`activated` 化) と、ホイールで素早く切り替えていた操作ができなくなる。問題ないか | **問題ないと判断**。フォントコンボが既に `activated` で、同じ操作感に揃う |
| **Q4** | 既に S1 へ戻されてしまった既存プロジェクトを、読み込み時に S3 へ戻す処理は要るか | **要らない**。どのテロップがどこに居たかの記録が残っていないため推測実装になる (CLAUDE.md「推測実装はしない」)。手で戻せる (ドラッグ or 右クリック) |

---

## 8. 作業手順 (実装時)

1. `src/timeline/commands.py` — `role_move_scope` の既定追加 →
   `is_role_managed_subtitle_track()` 追加 → `ChangeSubtitleRole` 書き換え (§3.3)
2. `src/timeline/model.py` — `subtitle_track_for_role()` へ `comment_track_index` 追加 (§3.4) →
   `ensure_subtitle_track()` から渡す
3. `src/timeline/builder.py` / `settings_window.py` / `setting.json` — 設定キーの追加と検証
4. `tests/test_subtitle_new_track.py` — T20〜T27 追加 →
   `tests/test_settings_migration.py` — T28 追加
5. `src/gui/timeline/timeline_editor_dialog.py` — 役割コンボを `activated` へ / 案内文言 →
   `timeline_controller.py` の戻り値へ `"skipped"` → `catalog_en.py`
6. `tests/test_subtitle_comment_track.py` / `test_subtitle_new_track.py` /
   `test_settings_migration.py` を通す
7. 手動確認: ①新規トラックへ移す → ②役割・文字色・フォントを変える → ③S3 のまま /
   S1 のテロップをコメント化 → S2 へ移る / Ctrl+Z で戻る /
   インスペクタ上でホイールを回しても役割が変わらない

---

## 9. 付録: 本件に関わるコードの地図

| 場所 | 役割 | 本件との関係 |
| --- | --- | --- |
| `commands.ChangeSubtitleRole` | 役割変更 + 自動移動 | **原因** (§2.1) |
| `model.subtitle_track_for_role` | 役割 → トラックの片道写像 | **原因** (非コメント = S1) / 誤認 (§2.7) |
| `commands.ensure_subtitle_track` | 上記 + 無ければ作る | 経由地 |
| `commands.MoveSubtitlesToNewTrack` | ①の操作。役割は変えない | 正常 (resolve2) |
| `commands.MoveSubtitleToTrack` | ドラッグ移動。役割は変えない | 正常 |
| `commands.EditSubtitle` / `EditSubtitles` | 本文・フォント・サイズ・色・寄せ | **無罪** (§2.5) |
| `timeline_editor_dialog._InspectorPanel` | ②の入口 | 引き金 (§2.4) |
| `timeline_view.build_rows` | 行の並び | トラックを見るだけ。無罪 |
| `project_io.save` / `load_project` | 保存・読み込み | **無罪** (§2.6) |

---

## 10. 実装記録 (2026-10-28)

§3 の対策 1〜5 をすべて実装しました。設計との差分は下の「設計より具体化した点」の 3 点だけです。

### 10.1 変更したファイル

| ファイル | 変更内容 |
| --- | --- |
| `src/timeline/commands.py` | `_DEFAULT_SUBTITLE_TRACKS_CFG["role_move_scope"]` 追加 / `is_role_managed_subtitle_track()` 追加 / `ChangeSubtitleRole` を §3.3 のとおり書き換え (`move_skipped` / `_move_target()`) / `ensure_subtitle_track()` から `comment_track_index` を渡す |
| `src/timeline/model.py` | `subtitle_track_for_role()` へ `comment_track_index=None` を追加 (§3.4) |
| `src/timeline/builder.py` | `_role_move_scope()` 追加 + `_subtitle_tracks_config()` へ組み込み |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ `role_move_scope: "role_tracks"` |
| `src/settings/setting.json` | 同キーを追記 |
| `src/gui/timeline/timeline_controller.py` | `change_subtitle_role()` の戻り値へ `"skipped"` |
| `src/gui/timeline/timeline_editor_dialog.py` | 役割コンボを `activated` へ / `skipped` の案内文言 |
| `src/i18n/catalog_en.py` | 案内文言の英訳 1 件 |
| `tests/test_subtitle_role_track_keep.py` | **新規**。T20〜T27・T29 (+ 未知の設定値のフォールバック / `is_role_managed_subtitle_track` の単体) |
| `tests/test_settings_migration.py` | T28 (既定の補完 / `"all"` の温存) |

### 10.2 設計より具体化した点

1. **テストは新規ファイルへ**。`tests/test_subtitle_new_track.py` へ足す案 (§5) ではなく
   `tests/test_subtitle_role_track_keep.py` を新設しました。
   既存ファイルは「トラックを増やす操作」の担当で、本件は「増やしたあと役割を変える」
   別の関心事のため分けています。T29 (GUI) も同じファイルに同居させ、
   `PySide6` が無い環境ではクラスごとスキップします。
2. **`role_move_scope` の検証を関数へ出した**。`builder._role_move_scope()` が
   `"role_tracks"` / `"all"` 以外 (書き間違い・将来の未知の値) を既定へ寄せます。
   設定の綴り間違いで挙動が変わらないようにするためです。
3. **番号の読み取りは既存ヘルパへ寄せた**。`model.subtitle_track_for_role()` の
   `comment_track_index` は `int()` ではなく同ファイルの `_to_int()` を使います
   (設定から文字列が来ても落ちない)。

### 10.3 確認結果

再現スクリプト (§1 と同じもの) を修正後に再実行:

```
移動前: S1 / combo = streamer
新規トラックへ移動: {'moved': 1, 'skipped': 0, 'track': 'S3', 'limit': False} -> S3
文字色変更後: S3
フォント変更後: S3
役割をサブへ変更後: S3          ← ③ の症状が消えた (修正前は S1)
```

コマンド単体でも、役割をどれへ変えても S3 に留まります。

```
② 役割を 'sub'      へ -> 移動前 S3 / 移動後 S3 (moved=False)
② 役割を 'streamer' へ -> 移動前 S3 / 移動後 S3 (moved=False)  ← push 自体が False
② 役割を 'comment'  へ -> 移動前 S3 / 移動後 S3 (moved=False)
```

テスト: `python -m unittest discover -s tests` → **1959 件 OK** (新規 15 件を含む)。

### 10.4 既定を変えた点 (利用者から見た変化)

| 操作 | v1.6.1 まで | 今回以降 |
| --- | --- | --- |
| S3 以降のテロップの役割を変える | S1 へ移動 (S1 の同時刻が空いていれば) | **その場に残る** + 「テロップの置き場所はそのままで役割だけ変更しました」と案内 |
| S3 以降のテロップをコメントにする | S3 のまま (ただし S3 がコメント用と誤認される) | **S3 のまま** (誤認しない / 次のコメント化は S2 を作る) |
| S1 のテロップをコメントにする / 戻す | S2 へ / S1 へ | **変更なし** |
| コンボで同じ役割を選び直す | 移動 + 履歴 1 手 | **何も起きない** |
| インスペクタ上でホイール・矢印キー | 役割が変わりテロップが移動 | **何も起きない** |

従来挙動へ戻すには `setting.json` の
`timeline.subtitle_tracks.role_move_scope` を `"all"` にします。
