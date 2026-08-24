# resolve10（ver3） — Timeline ノードのコピー＆ペースト（Ctrl+C / Ctrl+V）修正設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request10.md` および **2026-08-21 の追加指示（rev2）**
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/CLAUDE.md`「実装前に設計を行うこと」）。
* 記載した実装位置・挙動はすべて本リポジトリの実コードを読んで確認した（2026-08-21 時点）。
  決めきれない点は推測実装せず **§9 確認事項** に列挙した。
* **setting.json は本書では書き換えない**。追加キーは `builder.timeline_config()` の既定と
  `DEFAULT_SETTINGS`（`settings_window.py:459` 付近）へ足し、`load_settings()` の
  既定マージで自動補完させる（resolve7 / resolve9 と同じ方針）。
* ハードコード禁止（`docs/CLAUDE.md`）に従い、キー割り当てと衝突解決の方針は
  すべて `setting.json` から変えられる形にする（§7）。

### 0.1 rev2 での方針変更（本書はこの方針で書き直してある）

追加指示は 2 点。**初版（Ctrl+V＝短縮 / Ctrl+Shift+V＝挿入の 2 本立て）は破棄する。**

| # | 追加指示 | 本書の反映 |
|---|---|---|
| **R-1** | **Ctrl+V の動作をすべて Ctrl+Shift+V の動作（＝割り込ませる貼り付け）に置き換え、Ctrl+Shift+V のショートカットは無くす** | 貼り付けは **1 種類だけ**。`shortcuts.paste_insert` は作らない。「既存優先で短縮する」モード（初版の `PASTE_FIT` / `overlap_policy` / `free_fragments()`）は**設計ごと削除** |
| **R-2** | **右へズレるのは「そのトラック上だけ」の話。V2 に置いた画像をコピーして貼ったとき、V2 上の他ノードと干渉しないならそのまま貼る** | リップルは **① 干渉したトラックだけ ② 必要な分だけ** に限定する。干渉が無ければ**何も動かさない**（§3-4） |
| **R-3** | **ただし V1 へ貼る場合は S1（字幕）も一緒にズレるようにする** | 貼り付け先が **V1（ベース映像）のときだけ全トラック（音声以外）を同量ずらす**。V2 以降・字幕へ貼るときは R-2 どおりそのトラックだけ。設定 `paste.ripple_scope` の既定を **`base_syncs_all`** にする（§3-5） |

R-2 により、初版の「常に全トラックを `span_sec` ぶんずらす」は成立しない。
**ずらす量はトラックごとに計算する**（§3-4 / §5.2-2）のが本書の設計の中心になる。
R-3 は「どのトラックへ適用するか」だけの話で、**ずらす量の計算（§3-4）は変わらない**。

---

## 1. 要望と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **P1** | **Ctrl+C** で選択状態のノードをコピーする（Timeline 上の**全ノード**が対象） | 機能追加 |
| **P2** | **Ctrl+V** で**再生ヘッドの位置**へ貼り付ける | 機能追加 |
| **P3** | 貼り付けたノードが**優先**され、既存ノードと干渉する場合は**既存ノードが右へズレる・伸びる** | 機能追加（rev2 R-1） |
| **P4** | 右へズラすのは**干渉したトラックだけ**。干渉しないならそのまま貼る（他トラック・後続ノードは動かさない） | 機能追加（rev2 R-2） |
| **P14** | ただし **V1 へ貼るときは S1（字幕）も一緒にズレる** | 機能追加（rev2 R-3） |

「ノード」の定義（要望文に無いので本書で確定させる）:

| 画面上の見た目 | 実体 | コピーの扱い |
|---|---|---|
| V1 の本編クリップ | `Clip`（`model.py:206`） | **対象**（素材参照ごとコピー） |
| V2 以降のオーバーレイ（画像・動画） | `Clip` | **対象** |
| S1 の字幕 | `SubtitleClip`（`model.py:281`） | **対象** |
| A1 の音声 | `AudioClip`（`model.py:384`） | **単独ではコピーしない**。リンク元の V1 クリップへ読み替える（§3-6） |

要望文に無いが、実装に必ず要る論点:

| ID | 論点 | 理由 |
|---|---|---|
| **P5** | **複数選択のコピー**（相対位置の保持） | 選択は複数持てる（`timeline_controller.py:80`）。1 本しか扱えないと選択の意味が壊れる |
| **P6** | **貼り付け先トラックの決め方** | 「再生ヘッド位置」だけでは縦方向（どのトラックか）が決まらない |
| **P7** | **音声クリップの追随** | `AudioClip` は時刻を持たず V1 から導出する（R18）。貼り付けでリンクを作らないと**無音になる** |
| **P8** | **メディアプールの再登録** | 別画面の Timeline へ貼るとき `media_id` が存在しない。参照が切れると V1 が黒画面になる |
| **P9** | **アーカイブ用 `archive_clip_index` の扱い** | 書き出しは V1 の origin でクリップを分割する（`archive/timeline_builder.py:245`）。**貼り方によって出力ファイルが衝突する**（§2.6） |
| **P10** | **Undo・未保存判定** | 貼り付けは編集操作。履歴に載らないと戻せず、`*`（未保存）も出ない |
| **P11** | **文字入力欄との競合** | 字幕テキスト欄で Ctrl+C が「クリップのコピー」に奪われると文字がコピーできない |
| **P12** | **貼り付け後の再生ヘッド・選択** | 連続貼り付け（Ctrl+V 連打）でどうなるかを決めないと使い物にならない |
| **P13** | **字幕とのズレ** | V1 だけをずらすと、V1 に合わせて作った字幕（S1）が置いていかれる。**P14 でこれを回避する**（§3-5） |

---

## 2. 現状分析（実コードの確認結果）

### 2.1 コピー＆ペーストの資産は「まったく無い」

`src` 配下（`dist` を除く）に `QClipboard` を使う箇所は無く、`timeline.shortcuts` にも
`copy` / `paste` は存在しない（`builder.py:42-68` / `settings_window.py:459-484`）。
**完全な新規機能**であり、既存挙動を壊す箇所は原理的に無い（追加のみ）。

### 2.2 ショートカットの登録機構はそのまま使える

```python
# src/gui/timeline/timeline_editor_dialog.py:252
def _register_shortcuts(self):
    keys = self.controller.cfg["shortcuts"]
    ...
    def bind(action, handler):   # 空文字なら割り当てなし・重複は警告してから登録
```

* キー割り当ては `setting.json`（`timeline.shortcuts`）から読む。
* ただし **`_shortcut_config()`（`builder.py:71`）は既定辞書に無いキーを捨てる**。

  ```python
  for key, value in values.items():
      if key in merged:          # ← 既定に無い action は無視される
  ```

  つまり **`builder._DEFAULT_SHORTCUTS` への追記が必須**で、
  `settings_window.DEFAULT_SETTINGS` だけ足しても効かない（見落としやすい）。
* 登録したショートカットは `_ShortcutGuard`（`timeline_editor_dialog.py:701`）が
  **文字入力欄・コンボボックスにフォーカスがある間すべて無効化**する。
  → **P11 は追加実装なしで満たせる**（Ctrl+C / Ctrl+V を同じ `bind()` で登録するだけ）。
* `Ctrl+Shift+V` は**登録しない**（rev2 R-1）。既定辞書に項目を作らないため、
  `setting.json` に手で書いても `_shortcut_config()` が捨てる＝**復活しない**。

### 2.3 選択の実体は「ID のリスト」で、音声クリップは既に V1 へ読み替えている

```python
# src/gui/timeline/timeline_view.py:602 mousePressEvent
if isinstance(clip, AudioClip):
    linked = self._controller.timeline.clip_by_id(clip.link_clip)
    target = linked            # 選択は V1 クリップの ID になる
```

したがって `controller.selected_ids()` に `AudioClip` の ID が入ることは通常無い。
コピー側でも同じ読み替えを（防御的に）入れておけば P7 の入り口は塞がる。

### 2.4 「詰める」処理はあるが「割り込ませる」処理が無い

| 方向 | 関数 | 位置 |
|---|---|---|
| 区間を抜いて詰める | `ripple_remove_range(timeline, start, end, tracks, min_clip_sec)` | `commands.py:289` |
| 1 クリップへの適用規則（規則 1〜6） | `_apply_range_removal()` | `commands.py:232` |
| 対象トラックの決定（音声は常に除外） | `ripple_target_tracks()` | `commands.py:222` |
| **区間を割り込ませて広げる** | **存在しない** | — |

貼り付けはこの逆操作にあたる。ただし rev2 R-2 により **`ripple_remove_range()` の対称形にはならない**。

| | リップル削除 | 貼り付け（rev2） |
|---|---|---|
| 対象トラック | `ripple_sync_tracks` 設定（既定 `all` = 全トラック） | **干渉したトラックのみ**（既定 / §3-5 で設定可） |
| 動かす量 | 削除区間の長さ（全トラック共通） | **トラックごとに「必要な分だけ」計算**（§3-4） |
| 干渉が無いとき | — | **何もしない**（クリップは 1 つも動かない） |

音声トラックを対象外にする規約（`AudioClip` は V1 から導出するため自動追従する / R18）は**共通**。

### 2.5 クリップ生成まわりの既存規約

* ID は `timeline.next_id("c"/"s"/"a"/"m")`（`model.py:544`）で採番する。既存 ID とは必ず重複しない。
* `SplitClip`（`commands.py:438`）は分割時に **リンク音声も 2 本へ分ける**。
  貼り付け（新しい V1 クリップの生成）でも同じ後始末が要る（§3-6）。
* `AddMediaClip`（`commands.py:594`）は **重なる場合は別トラックへ逃がす**（`_resolve_track`）。
  今回の貼り付けは「押しのける」であり、**逃がしてはいけない**。
  よって `AddMediaClip` は再利用せず、専用コマンドを新設する（§3-3）。
* Undo/Redo は `CommandStack.push()` がコマンド適用前に全体スナップショットを取る方式
  （`commands.py:95`）。**コマンドとして実装しさえすれば P10 は自動的に満たされる**。

### 2.6 【重要】アーカイブ用の V1 は「貼り方によって出力が壊れる」

書き出しは V1 を `origin.archive_clip_index`（`archive/timeline_builder.py:42`）で区切る。

```python
# src/archive/timeline_builder.py:245 split_by_clip()
for clip in sorted(base.clips, key=lambda c: c.timeline_start):
    index = clip.origin.get(ORIGIN_ARCHIVE_INDEX)
    if current is None or current["index"] != index:   # index が変わったら別グループ
```

そして書き出し側は **index から作業フォルダ名と出力ファイル名を作る**。

```python
# src/archive/clip_writer.py:577
clip_dir = os.path.join(workdir, f"clip{clip_index}")
# _copy_individual / _write_individual も f"..._clip{clip['index']}_..." で命名する
```

つまり **同じ index のグループが 2 つできると、作業フォルダと出力ファイル名が衝突して
片方が上書きされる**。これは「clip2 の一部をコピーして clip1 の途中へ貼る」だけで起きる
（clip1 の並びが `1,1,2,1,1` に割れ、index=1 のグループが 2 つできる）。

一方で **素材の参照は問題ない**。レンダリングはクリップごとに `media.path` を引く
（`renderer.py:212` / `:500`）ため、clip2 由来の素材を clip1 のグループに混ぜても正しく出る。

→ **壊れるのは「グループの割れ方」だけ**。貼り付けた V1 クリップの `archive_clip_index` を
**貼り付け先の直前クリップから引き継ぐ**ようにすれば、グループは一切割れない（§3-8）。

### 2.7 その他、確認した前提

* `controller.execute()` → `timeline_changed` → `_on_timeline_changed()`（`timeline_editor_dialog.py:376`）で
  Undo ボタン・インスペクタ・タイトルの `*` が更新される。**貼り付けの後始末は追加不要**。
* 画面へ案内を出す経路は `TimelineView.status_message`（`timeline_view.py:293`）→
  `preview.set_status`（`timeline_editor_dialog.py:102`）。
* `min_clip_sec`（既定 0.05 / `builder.py:166`）が全編集の最小尺。
* `timeline_editor_dialog.py` は現状 `commands` を import していない（`model` のみ）。
  §5.5 の実装でも import の追加は不要（貼り付けモードの定数が無くなったため / rev2）。

---

## 3. 方式選定

### 3-1【P1】クリップボードはどこに置くか

| 案 | 内容 | 評価 |
|---|---|---|
| A. コントローラのメンバ | `TimelineController` に控える | ✕ 画面を閉じると消える。別画面（クリップ用↔アーカイブ用）へ貼れない |
| B. **プロセス内の共有モジュール** | `src/timeline/clipboard.py` に単一インスタンスを持つ | **◎ 採用**。Qt 非依存でテストでき、画面をまたいで貼れる |
| C. OS クリップボード（QClipboard + 独自 MIME） | 別プロセスとも共有できる | ✕ 今回の要望に対して過剰。Qt 依存が増えテストしづらい。将来 B の payload をそのまま載せられる |

**採用: 案 B**。ただし控える中身は **JSON 化できる素の辞書**（`clip.to_dict()` 互換）にしておき、
将来 C へ移す場合に payload をそのまま流用できる形にする。

**モデルオブジェクトの参照をそのまま持たない**のが要点。参照を持つと、コピー後に元クリップを
編集・削除・Undo した結果が貼り付け内容に混ざり込む（`_restore()` はクリップを作り直すため
古い参照が生き残る / `commands.py:72`）。辞書へ落として値で持てばこの事故は起きない。

### 3-2【P1 / P5】何をコピーするか（payload スキーマ）

複数選択をそのまま貼れるように、**最も早いノードの開始時刻を 0（アンカー）とした相対位置**で控える。

```python
{
  "version": 1,
  "span_sec": 12.34,          # 全体の尺 (最大終端 - アンカー)。案内文の表示にのみ使う
  "items": [
    {
      "kind": "video" | "subtitle",
      "offset_sec": 0.0,                                  # アンカーからの相対開始
      "track": {"id": "V1", "kind": "video", "index": 1, "is_base": True},
      "clip":  {...clip.to_dict()...},                    # id は貼り付け時に採番し直す
      "audio": {"gain_db": 0.0, "muted": False} | None,   # リンク音声の属性 (§3-6)
      "media": {...media.to_dict()...} | None             # 素材の再登録用 (§3-7)
    }, ...
  ]
}
```

* 相対位置で持つので、貼り付け先の再生ヘッドが違っても**選択したときの並びが崩れない**。
* `clip.to_dict()` をそのまま使うため、`transform` / `z_order` / `origin` / 字幕の色・フォントも
  自動的に運ばれる（今後クリップへ項目が増えても追従する）。
* **rev2 注記**: `span_sec` は「ずらす量」ではなくなった。
  ずらす量は**貼り付け先トラックごとに計算する**（§3-4）。payload には残すが、
  案内文以外には使わない。

### 3-3【P6】貼り付け先トラックの決め方

「再生ヘッド位置」は横方向しか決めない。縦方向は **コピー元のトラックを引き継ぐ** ことにする。

| コピー元 | 貼り付け先 |
|---|---|
| V1（ベース映像） | 貼り付け先 Timeline の **ベース映像トラック**（`base_video_track()`） |
| V2 以降 | **同じ ID のトラック**があればそこ。無ければ**映像トラックを新設**（上限 `media.max_video_tracks`） |
| S1 など字幕 | **同じ ID の字幕トラック**。無ければ既定の字幕トラック（無ければ S1 を作る） |
| ロック中のトラックが対象 | **貼り付けない・ずらさない**（`skipped` として案内を出す） |

**`AddMediaClip._resolve_track()` の「重なるなら別トラックへ逃がす」規約は使わない。**
要望は「押しのける」であり、勝手に別トラックへ逃げると違う結果になるため。

マウス位置のトラックへ貼る案（DaVinci の「対象トラック」）は、現行 UI に
「対象トラック」の概念自体が無いため採らない（§9 Q2 で確認）。

### 3-4【P3 / P4】**干渉したトラックだけ・必要な分だけ**ずらす（rev2 の中核）

貼り付け先トラック `T` に対し、その `T` へ貼る項目が占める区間を
`[start_T, end_T)`（= `再生ヘッド + 最小 offset` 〜 `再生ヘッド + 最大終端`）とする。

**手順**

1. `T` 上の既存クリップのうち **`timeline_end > start_T` のもの**（＝ `start_T` 以降に居る／跨いでいる）を集める。
2. その中で `[start_T, end_T)` に**実際に食い込んでいる**もの（動き出す位置が `end_T` より前）が
   **1 つも無ければ → ずらし量 0。何も動かさずそのまま貼る**（**要望 P4 そのもの**）。
3. 食い込みがある場合、**動かす側の開始位置の最小値** `earliest` を求め、
   **ずらし量 `shift = end_T - earliest`** とする（＝最小限）。
4. `T` 上の `timeline_end > start_T` のクリップを**まとめて `shift` だけ右へ**動かす
   （`start_T` を跨ぐクリップは `start_T` で分割し、後半だけ動かす / §5.2-2）。
5. 空いた `[start_T, end_T)` へ項目を置く。**構造上、衝突は起こり得ない**。

**「必要な分だけ」の意味**（初版の「常に span ぶんずらす」との違い）

```
既存 V2:   |----------[==A==]--------[==B==]
貼付予定:  [-----P-----]                        P = 10s, A は +4s から

初版  (span ぶん): P[0,10)  A[14,..)  B[..]     ← 4〜10 に穴が空く（不自然）
rev2 (必要分のみ): P[0,10)  A[10,..)  B[..]     ← shift = 10-4 = 6s。穴が空かない
```

* `end_T` 以降に居るクリップ（`B`）も**同じ `shift` だけ動かす**。
  動かさないと `A` が `B` に突っ込む。同量ずらすので**トラック内の間隔は完全に保たれる**。
* 食い込みが無ければ `shift = 0` なので、**`B` のような後続ノードも一切動かない**。
  「V2 に置いた画像を、他ノードに干渉しない位置へ貼る」は
  **Timeline 全体が無変化のままノードだけが増える**。
* **V1 は普通ぎっしり埋まっている**（無音カット後のクリップが連続する）ため、
  V1 への貼り付けは必ず `shift > 0` になる。要望の
  「V1 で動画の最初の方に貼ると V1 上が右にズレていく」はこの経路。

**複数項目が同じトラックへ乗る場合**は、その項目群の**外接区間**（最小開始〜最大終端）で
1 回だけ場所を空ける。コピー元にあった項目間の隙間は**隙間のまま**貼られる
（コピーした並びを崩さないため）。

### 3-5【P4 / P13 / P14】ずらす範囲は「そのトラックだけ」。ただし V1 は全体を連れて行く

rev2 R-2 のとおり、**リップルは原則として貼り付け先トラックに閉じる**。
V2 へ貼っても V1・S1 は動かない。

**例外は V1（ベース映像）へ貼るとき**（R-3 / P14）。
V1 だけを右へずらすと、V1 の映像に合わせて作った **S1 の字幕が置いていかれる**
（映像だけ後ろへ動き、字幕はその場に残る＝声と字幕がズレる）。
V2 以降のオーバーレイ（テロップ画像・ワイプ等）も V1 の内容に合わせて置いてあるため同じ理由でズレる。
よって **V1 へ貼るときは全トラック（音声以外）を同量ずらす**。

**`timeline.paste.ripple_scope`**

| 値 | 挙動 | 想定 |
|---|---|---|
| **`base_syncs_all`（既定）** | **V1（ベース映像）へ貼るときだけ**全トラック（音声以外）を同量ずらす。V2 以降・字幕へ貼るときは**そのトラックだけ** | **要望どおり**（R-2 + R-3）。V1 は字幕ごと後ろへ動き、オーバーレイの差し込みは軽い |
| `track` | 常に**干渉したトラックだけ**をずらす | V1 へ貼っても字幕を動かしたくない場合（字幕を後から作り直す運用など） |
| `all` | 常に全トラック（音声以外）を同量ずらす | resolve10 初版の挙動へ戻したい場合。V2 へ画像を貼るだけで Timeline 全体が伸びる |

**「V1 へ貼るとき」の判定**は「貼り付け先トラックの集合に `base_video_track()` が含まれるか」。
V1 と V2 へ同時に貼る（複数選択のコピー）場合も**含まれるので全トラック同期**になる。

**`base_syncs_all` / `all` のときの「同量」**は、**貼り付け先トラック群それぞれで求めた
`shift` の最大値**を採り、全トラックへ一律に適用する。
トラックごとに量が違うと縦の同期が壊れる（＝この設定を選んだ意味が無くなる）ため。

* **`shift = 0` なら全トラックが動かない**。V1 の再生ヘッド位置にちょうど貼れる隙間がある場合、
  字幕もオーバーレイも動かない（要望 P4 の原則は V1 でも生きている）。
* **音声トラックはどの値でも対象外**。`AudioClip` は時刻を持たず V1 から導出するため
  自動で追従する（R18 / `commands.py:222` と同じ理由）。ここで一緒に動かすと二重にずれる。
* **ロックされたトラックは同期対象でも動かさない**（`make_room_for_range()` が弾く / §5.2-2）。
  ロックした字幕トラックがあると **その字幕だけ V1 とズレる**。ロックの意味を優先する
  （黙って動かすとロックが無意味になる）ため、この非対称は仕様とする。
* 既存設定 `timeline.ripple_sync_tracks`（リップル削除用）は**貼り付けには使わない**。
  削除は「全トラック or 操作トラックのみ」の 2 値だが、貼り付けは
  「V1 のときだけ全トラック」という 3 つ目の値が要る。**別キーにする**のが本書の判断（§7）。

### 3-6【P7】音声クリップの扱い

* **コピー時**: 選択に `AudioClip` の ID が混じっていたらリンク元の映像クリップへ読み替える
  （`timeline_view.py:602` と同じ規約）。音声だけを単独でコピーすることはできない。
* **payload**: リンク音声の `gain_db` / `muted` を `item["audio"]` として持つ。
  音量を下げたクリップをコピーしたら、貼り付け先でも下がっているのが自然なため。
* **貼り付け時**: 生成した映像クリップに対し、貼り付け先トラックへリンクする音声トラックがあれば
  `AudioClip(next_id("a"), 新クリップID)` を足し、`gain_db` / `muted` を復元する。
  音声トラックが無ければ作る（`AddMediaClip` と同じ手順 / `commands.py:640` 付近）。
  素材に音声が無い（`media.has_audio == False`）場合は作らない。
* **割り込みで映像を分割したときはリンク音声も 2 本へ分ける**（`split_clip_at()` が面倒を見る / §5.2-1）。

### 3-7【P8】メディアプールの再登録

貼り付け先の Timeline に `media_id` が無い場合（＝別画面へ貼った場合）がある。

1. `timeline.media_by_path(payload の media.path)`（`model.py:579`）で**同じパスの素材を探す** → あれば再利用。
2. 無ければ `MediaRef.from_dict()` で復元し、`id` を `next_id("m")` で振り直して `media_pool` へ足す。
3. **実ファイルが存在しない場合は、その項目を貼らない**（黒画面のクリップを増やさない）。
   件数を数えて「N 件は貼れませんでした」と案内する。
   * アーカイブ用の素材はセッション中の一時ファイルなので、同一セッション内では必ず存在する。
   * 保存済みプロジェクトを開き直した場合は resolve9 §3-1 の復旧が済んだ後のパスになる。

**【重要】素材とトラックの解決は「場所を空ける前」に済ませる**（rev2）。
貼れない項目のためにトラックをずらしてしまうと、**空いた穴だけが残る**。
`PasteClips.apply()` は「① 全項目の貼り付け先トラックと素材を解決 → ② 場所を空ける → ③ 置く」
の 3 段構えにする（§5.3）。

### 3-8【P9】アーカイブ用 `archive_clip_index` の引き継ぎ

§2.6 のとおり、**index の付け方だけで出力が壊れる**。方針を設定で持たせる。

**`timeline.paste.archive_index_policy`**

| 値 | 挙動 | 結果 |
|---|---|---|
| **`inherit`（既定）** | 貼り付け先 V1 の**直前のクリップの index を引き継ぐ**（直前が無ければ直後、どちらも無ければ元の index） | グループの区切りが**増えない**。出力ファイル数・名前は貼り付け前と同じ |
| `keep` | コピー元の index をそのまま持つ | 忠実だが、**同じ index のグループが 2 つできると出力が上書き衝突する**（§2.6） |

* `inherit` なら「clip2 の名場面を clip1 の中へ差し込む」が**そのまま 1 本の clip1 として出力**される。
  素材はクリップごとに解決されるため（`renderer.py:212`）、中身は clip2 の映像で正しく出る。
* 「直前のクリップ」から引き継ぐため、**新しいグループ境界を作らない**ことが構造的に保証される。
* **rev2 注記**: 判定は「場所を空けた**後**」の V1 の並びに対して行う。
  ずらす前の並びで判定すると、割り込みで分割された直前クリップを取り違える。
* クリップ用（`source.archive` を持たない Timeline）では `origin` をそのままコピーするだけで、
  この処理は働かない。判定は resolve9 §3-4 の「`source.archive` の有無」を流用する。

### 3-9【P12】貼り付け後の再生ヘッドと選択

* 貼り付けたノードを**選択状態**にする（直後に移動・削除できる / `select_pasted`、既定 true）。
* 再生ヘッドを**貼った範囲の終端へ進める**（`move_playhead_to_end`、既定 true）。
  これで **Ctrl+V 連打が「順番に並べていく」操作**になる。
  1 つも貼れなかったときは動かさない。

### 3-10【P10】Undo・未保存

`PasteClips` を `Command` として実装するだけでよい（§2.5）。
コピー（Ctrl+C）は Timeline を変更しないので履歴に載せない＝ `*` も付かない。

### 3-11【P11】文字入力欄との競合

`_ShortcutGuard`（`timeline_editor_dialog.py:701`）が入力欄・コンボへのフォーカス中は
登録済みショートカットを一括で無効化するため、**字幕テキスト欄の Ctrl+C / Ctrl+V は文字操作のまま**。
`bind()` 経由で登録する限り追加実装は要らない（テストで担保する / §8）。

---

## 4. 変更ファイル一覧

| 種別 | ファイル | 内容 |
|---|---|---|
| **新規** | `src/timeline/clipboard.py` | プロセス内クリップボード（payload の生成・保持）。Qt 非依存 |
| 変更 | `src/timeline/commands.py` | `split_clip_at()` 抽出 / `required_shift()` / `make_room_for_range()` 追加 / `PasteClips` 追加 |
| 変更 | `src/gui/timeline/timeline_controller.py` | `copy_selected()` / `paste()` / `can_paste()` を追加 |
| 変更 | `src/gui/timeline/timeline_editor_dialog.py` | `copy` / `paste` の登録と案内表示 |
| 変更 | `src/gui/timeline/timeline_view.py` | 右クリックメニューへ「コピー」「貼り付け」を追加 |
| 変更 | `src/timeline/builder.py` | `_DEFAULT_SHORTCUTS` へ 2 キー追加 / `paste` セクションの既定と検証 |
| 変更 | `src/settings/settings_window.py` | `DEFAULT_SETTINGS` へ同じ既定を追加（`setting.json` へ自動補完させる） |
| **新規** | `tests/test_timeline_clipboard.py` | コピー＆ペーストの自動テスト（§8） |

アーカイブ用画面（`archive_timeline_dialog.py`）は **無改造**。基底のショートカット登録を
そのまま継承するため、Ctrl+C / Ctrl+V は自動的に効く。

---

## 5. 詳細設計

### 5.1 `src/timeline/clipboard.py`（新規）

```python
# Timeline ノードのクリップボード (ver3 resolve10 §3-1 / §3-2)
# コピーした内容はプロセス内で共有し、編集画面をまたいでも貼り付けられるようにする。
# モデルの参照ではなく辞書 (JSON 化できる形) で控える:
#   ・コピー後に元クリップを編集・削除・Undo しても貼り付け内容が変わらない
#   ・Qt に依存しないためテストから直接叩ける
#   ・将来 OS クリップボードへ載せ替える場合もこの payload をそのまま使える
from ..utils.logger import get_logger
from .model import AudioClip, SubtitleClip, round_sec

_logger = get_logger(__name__)

PAYLOAD_VERSION = 1
KIND_VIDEO = "video"
KIND_SUBTITLE = "subtitle"


# コピー対象を (トラック, クリップ) の一覧へ解決する
# 音声クリップはリンク元の映像クリップへ読み替える (§3-6)。重複は 1 件にまとめる。
def _resolve_targets(timeline, clip_ids):
    targets, seen = [], set()
    for clip_id in (clip_ids or []):
        clip = timeline.clip_by_id(clip_id)
        if isinstance(clip, AudioClip):
            clip = timeline.clip_by_id(clip.link_clip)
        if clip is None or clip.id in seen:
            continue
        track = timeline.track_of_clip(clip.id)
        if track is None:
            continue
        seen.add(clip.id)
        targets.append((track, clip))
    return targets


# クリップ 1 件を payload の項目へ変換する
def _build_item(timeline, track, clip, anchor):
    is_subtitle = isinstance(clip, SubtitleClip)
    item = {
        "kind": KIND_SUBTITLE if is_subtitle else KIND_VIDEO,
        "offset_sec": round_sec(clip.timeline_start - anchor),
        "track": {"id": track.id, "kind": track.kind,
                  "index": track.index, "is_base": bool(track.is_base)},
        "clip": clip.to_dict(),
        "audio": None,
        "media": None,
    }
    if not is_subtitle:
        media = timeline.media_by_id(clip.media_id)
        if media is not None:
            item["media"] = media.to_dict()          # 別 Timeline へ貼るときの再登録用
        linked = timeline.audio_clip_for(clip.id)
        if linked is not None:
            item["audio"] = {"gain_db": linked.gain_db, "muted": linked.muted}
    return item


# 選択中のクリップ群から payload を作る (§3-2)。作れなければ None。
def build_payload(timeline, clip_ids):
    targets = _resolve_targets(timeline, clip_ids)
    if not targets:
        return None
    anchor = min(clip.timeline_start for _track, clip in targets)
    span = max(clip.timeline_end for _track, clip in targets) - anchor
    items = [_build_item(timeline, track, clip, anchor)
             for track, clip in sorted(targets, key=lambda tc: tc[1].timeline_start)]
    return {"version": PAYLOAD_VERSION, "span_sec": round_sec(span), "items": items}


# プロセス内で共有する 1 つだけのクリップボード
class _Clipboard:

    def __init__(self):
        self._payload = None

    def set(self, payload):
        self._payload = payload

    def get(self):
        return self._payload

    def is_empty(self):
        return not (self._payload or {}).get("items")

    def clear(self):
        self._payload = None


_clipboard = _Clipboard()


# 選択中のノードをコピーする。控えた件数を返す (0 = コピーできるものが無い)
def copy_clips(timeline, clip_ids):
    payload = build_payload(timeline, clip_ids)
    if payload is None:
        return 0
    _clipboard.set(payload)
    _logger.info("Timeline のノードをコピーしました: %d 件", len(payload["items"]))
    return len(payload["items"])


def payload():
    return _clipboard.get()


def is_empty():
    return _clipboard.is_empty()


def clear():
    _clipboard.clear()
```

### 5.2 `commands.py` へ足す共通処理

#### 5.2-1 `split_clip_at()`（`SplitClip` から抽出）

分割は「編集点の追加（W）」と「貼り付けの割り込み（Ctrl+V）」の 2 箇所で要る。
規約が 2 つに分かれると必ずズレるため、**既存の `SplitClip.apply()` の中身を関数へ出し、
`SplitClip` はそれを呼ぶだけにする**（挙動は変えない）。

```python
# クリップを at_sec で 2 つに割り、後半のクリップを返す (割れなければ None)
# 両側が最小尺を満たさない位置では割らない。リンク音声も 2 本へ分ける (R18 §6.3.4)。
# ver3 resolve10 §5.2: SplitClip と貼り付けの割り込みで規約を共有するため関数へ出した。
def split_clip_at(timeline, track, clip, at_sec, min_clip_sec=0.05):
    offset = at_sec - clip.timeline_start
    if offset < min_clip_sec or (clip.duration - offset) < min_clip_sec:
        return None

    if isinstance(clip, SubtitleClip):
        new_clip = clip.copy()
        new_clip.id = timeline.next_id("s")
        new_clip.timeline_start = at_sec
        new_clip.duration = clip.duration - offset
        clip.duration = offset
        track.clips.append(new_clip)
        return new_clip

    new_clip = clip.copy()
    new_clip.id = timeline.next_id("c")
    new_clip.timeline_start = at_sec
    new_clip.duration = clip.duration - offset
    new_clip.source_in = clip.source_in + offset
    new_clip.source_out = clip.source_out
    clip.duration = offset
    clip.source_out = clip.source_in + offset
    track.clips.append(new_clip)

    audio_clip = timeline.audio_clip_for(clip.id)
    if audio_clip is not None:
        audio_track = timeline.track_of_clip(audio_clip.id)
        if audio_track is not None:
            new_audio = audio_clip.copy()
            new_audio.id = timeline.next_id("a")
            new_audio.link_clip = new_clip.id
            audio_track.clips.append(new_audio)
    return new_clip
```

#### 5.2-2 `make_room_for_range()`（新規 / rev2 の中核）

トラック 1 本に対し「`[start, end)` を空けるために**最小限**だけ右へずらす」処理。
**干渉が無ければ何もしない**（要望 P4）。

```python
# 割り込み時に跨ぎクリップをどう扱うか (ver3 resolve10 §3-4)
INSERT_SPLIT = "split"              # start で分割し後半だけずらす (既定)
INSERT_SHIFT_WHOLE = "shift_whole"  # 丸ごとずらす


# クリップ 1 件を [start, end) の外へ出す方法と「動き出す位置」を返す
#   (None,    None)        : 区間より前にいる。何もしない
#   ("move",  開始位置)     : 丸ごと右へずらす
#   ("split", start)       : start で分割し、後半だけ右へずらす
#   ("trim",  None)        : start へ終端を切り詰める (動かさない)
# ver3 resolve10 §3-4。ずらし量の計算と実際の適用で必ず同じ判定を使うため、
# 分岐をこの 1 関数へ寄せる (2 か所へ書くと必ずズレて重なりが残る)。
def _insert_action(clip, start, min_clip_sec, policy):
    if clip.timeline_end <= start + _EPS:
        return (None, None)                           # 区間より前 → 無関係
    if clip.timeline_start >= start - _EPS:
        return ("move", clip.timeline_start)          # start 以降 → 丸ごとずらす
    # ここから: start を跨ぐクリップ
    head = start - clip.timeline_start                # start より前に残る長さ
    tail = clip.timeline_end - start                  # start より後ろの長さ
    if policy == INSERT_SHIFT_WHOLE or head < min_clip_sec:
        return ("move", clip.timeline_start)          # 頭がごく短い → 丸ごと右へ
    if tail < min_clip_sec:
        return ("trim", None)                         # 尻がごく短い → start で切る
    return ("split", start)                           # start で分割し後半が動く


# トラック上で [start, end) を空けるのに必要なずらし量を求める (ver3 resolve10 §3-4)
# 干渉が無ければ 0.0 (＝ずらさずそのまま貼れる)。
def required_shift(track, start, end, min_clip_sec=0.05, policy=INSERT_SPLIT):
    shift = 0.0
    for clip in track.clips:
        _action, move_from = _insert_action(clip, start, min_clip_sec, policy)
        if move_from is None:
            continue
        if move_from < end - _EPS:                    # 貼り付け区間へ食い込んでいる
            shift = max(shift, end - move_from)
    return shift


# トラック上の [start, end) を空ける (ver3 resolve10 §3-4 / §5.2-2)
# shift を渡さなければ required_shift() で最小限を求める。
# 全トラックを同量ずらす場合 (ripple_scope=base_syncs_all / all) は呼び出し側が shift を指定する。
# 音声トラックは対象外: AudioClip は時刻を持たず V1 から導出するため自動で追従する (R18)。
# 戻り値: 実際にずらした量 (0.0 = 何も動かしていない)
def make_room_for_range(timeline, track, start, end, min_clip_sec=0.05,
                        policy=INSERT_SPLIT, shift=None):
    if track.is_audio() or track.locked:
        return 0.0
    if shift is None:
        shift = required_shift(track, start, end, min_clip_sec, policy)
    if shift <= _EPS:
        return 0.0

    moved = split = trimmed = 0
    # split_clip_at() が track.clips へ追加するため、走査は複製に対して行う
    for clip in list(track.clips):
        action, _move_from = _insert_action(clip, start, min_clip_sec, policy)
        if action is None:
            continue
        if action == "move":
            clip.timeline_start += shift
            moved += 1
        elif action == "trim":
            _trim_tail_to(clip, start)                # 最小尺未満の食い込みを潰す
            trimmed += 1
        else:                                          # split
            tail = split_clip_at(timeline, track, clip, start, min_clip_sec)
            if tail is None:                           # 念のため (通常は起きない)
                clip.timeline_start += shift
                moved += 1
            else:
                tail.timeline_start += shift
                split += 1

    _logger.info(
        "貼り付けの割り込み: %s の %.3fs へ %.2fs を確保 / 移動 %d 件・分割 %d 件・切詰 %d 件",
        track.id, start, shift, moved, split, trimmed)
    return shift


# クリップの終端を at へ切り詰める (最小尺未満の食い込みを潰すための後始末)
def _trim_tail_to(clip, at):
    duration = max(at - clip.timeline_start, 0.0)
    if isinstance(clip, Clip):
        clip.source_out = clip.source_in + duration
    clip.duration = duration
```

> **不変条件 1**: `make_room_for_range()` から戻った時点で `[start, end)` に重なる
> 既存クリップは 1 つも無い。跨ぎクリップは「分割」「丸ごと移動」「終端の切り詰め」の
> いずれかで必ず区間の外へ出る。
> **不変条件 2**: 干渉が無ければ **Timeline を 1 か所も変えない**（要望 P4）。
> **不変条件 3**: `start` 以降のクリップは**全て同じ `shift`** だけ動くため、
> トラック内の前後関係・間隔は保たれる（新しい重なりを作らない）。

### 5.3 `PasteClips` コマンド（新規）

```python
# ずらす範囲 (ver3 resolve10 §3-5)
SCOPE_BASE_SYNCS_ALL = "base_syncs_all"  # V1 へ貼るときだけ全トラック (既定 / 要望どおり)
SCOPE_TRACK = "track"                    # 常に干渉したトラックだけ
SCOPE_ALL = "all"                        # 常に全トラック

# アーカイブ用 V1 の archive_clip_index の扱い (§3-8)
ARCHIVE_INDEX_INHERIT = "inherit"
ARCHIVE_INDEX_KEEP = "keep"


# クリップボードの内容を at_sec へ貼り付ける (ver3 resolve10 §5.3)
# 貼り付けたノードが優先され、干渉した既存ノードは右へずれる (要望 P3)。
# ずれるのは干渉したトラックだけ・必要な分だけで、干渉が無ければ何も動かない (要望 P4)。
# ただし V1 (ベース映像) へ貼るときは字幕・オーバーレイも同量ずらす (要望 P14 / §3-5)。
# 【手順】順序に意味がある:
#   ① 解決 : 貼り付け先トラックと素材を先に確定する (貼れない項目のために場所を空けない / §3-7)
#   ② 確保 : トラックごとに必要な分だけ場所を空ける (§3-4)
#   ③ 配置 : 空いた区間へクリップを作って置く
class PasteClips(Command):

    def __init__(self, payload, at_sec, min_clip_sec=0.05,
                 insert_policy=INSERT_SPLIT, ripple_scope=SCOPE_BASE_SYNCS_ALL,
                 archive_index_policy=ARCHIVE_INDEX_INHERIT, max_video_tracks=8):
        self._payload = payload or {}
        self._at = max(float(at_sec), 0.0)
        self._min_clip_sec = float(min_clip_sec)
        self._insert_policy = insert_policy
        self._ripple_scope = ripple_scope
        self._archive_index_policy = archive_index_policy
        self._max_video_tracks = int(max_video_tracks)
        # 呼び出し側が選択・再生ヘッド・案内に使う結果
        self.created_clip_ids = []
        self.pasted_end_sec = None
        self.skipped = 0
        self.shifted_sec = 0.0        # 実際にずらした最大量 (0 = 何も動いていない)
        self.label = "貼り付け"

    def apply(self, timeline):
        items = self._payload.get("items") or []
        if not items:
            return False

        # ① 解決: 貼れる項目だけを (トラック, 項目, 素材ID, 区間) の形へ落とす
        plans = []
        for item in items:
            track = self._resolve_track(timeline, item)
            if track is None or track.locked:
                self.skipped += 1
                continue
            duration = float((item.get("clip") or {}).get("duration") or 0.0)
            if duration < self._min_clip_sec:
                self.skipped += 1
                continue
            media_id = None
            if item.get("kind") == clipboard.KIND_VIDEO:
                media_id = self._resolve_media(timeline, item)
                if media_id is None:        # 素材が見つからない → 貼らない (§3-7)
                    self.skipped += 1
                    continue
            start = self._at + float(item.get("offset_sec") or 0.0)
            plans.append({"track": track, "item": item, "media_id": media_id,
                          "start": start, "end": start + duration})
        if not plans:
            return False

        # ② 確保: トラックごとの外接区間で場所を空ける (§3-4)
        self._make_room(timeline, plans)

        # ③ 配置
        for plan in plans:
            self._place(timeline, plan)
        return bool(self.created_clip_ids) or self.shifted_sec > _EPS

    # トラックごとに必要な分だけ場所を空ける (§3-4 / §3-5)
    # 貼り付け先に V1 (ベース映像) が含まれる場合は、字幕・オーバーレイも同量ずらす
    # (既定 base_syncs_all / 要望 P14)。V1 だけ動かすと字幕が置いていかれるため。
    def _make_room(self, timeline, plans):
        # 同じトラックへ乗る項目は外接区間 (最小開始〜最大終端) でまとめて 1 回だけ空ける
        regions, tracks = {}, {}
        for plan in plans:
            track = plan["track"]
            tracks[track.id] = track
            low, high = regions.get(track.id, (plan["start"], plan["end"]))
            regions[track.id] = (min(low, plan["start"]), max(high, plan["end"]))

        base = timeline.base_video_track()
        sync_all = (self._ripple_scope == SCOPE_ALL) or (
            self._ripple_scope == SCOPE_BASE_SYNCS_ALL
            and base is not None and base.id in tracks)

        if not sync_all:
            # 干渉したトラックだけを、それぞれ必要な分だけずらす (要望 P4 / V2 などへの貼り付け)
            for track_id, (start, end) in regions.items():
                shift = make_room_for_range(
                    timeline, tracks[track_id], start, end,
                    self._min_clip_sec, self._insert_policy)
                self.shifted_sec = max(self.shifted_sec, shift)
            return

        # 全トラックを同量ずらす (縦の同期を保つため最大値へ揃える / §3-5)
        # make_room_for_range() が音声トラックとロック中のトラックを弾く。
        # shift が 0 (＝V1 に十分な隙間がある) なら 1 つも動かさない。
        shift = 0.0
        for track_id, (start, end) in regions.items():
            shift = max(shift, required_shift(tracks[track_id], start, end,
                                              self._min_clip_sec, self._insert_policy))
        if shift <= _EPS:
            return
        start = min(s for s, _e in regions.values())
        end = max(e for _s, e in regions.values())
        for track in timeline.tracks:
            make_room_for_range(timeline, track, start, end, self._min_clip_sec,
                                self._insert_policy, shift=shift)
        self.shifted_sec = shift

    # 項目 1 件を置く (場所は ② で空いているため衝突しない)
    def _place(self, timeline, plan):
        track, item = plan["track"], plan["item"]
        clip = self._instantiate(timeline, track, item, plan["media_id"],
                                 plan["start"], plan["end"] - plan["start"])
        track.clips.append(clip)
        self.created_clip_ids.append(clip.id)
        self._attach_audio(timeline, track, clip, item)
        end = clip.timeline_end
        self.pasted_end_sec = end if self.pasted_end_sec is None \
            else max(self.pasted_end_sec, end)

    # 新しいクリップを作る (ID は採番し直す。尺・素材位置はコピー元のまま)
    def _instantiate(self, timeline, track, item, media_id, start, duration):
        data = dict(item.get("clip") or {})
        if item.get("kind") == clipboard.KIND_SUBTITLE:
            clip = SubtitleClip.from_dict(data)
            clip.id = timeline.next_id("s")
            clip.timeline_start = start
            clip.duration = duration
            return clip

        clip = Clip.from_dict(data)
        clip.id = timeline.next_id("c")
        clip.media_id = media_id
        clip.timeline_start = start
        clip.duration = duration
        clip.origin = self._resolve_origin(timeline, track,
                                           dict(data.get("origin") or {}), start)
        return clip

    # アーカイブ用 V1 の archive_clip_index を決める (§3-8)
    # 直前のクリップの index を引き継ぐことで split_by_clip() のグループを割らない。
    # 場所を空けた後の並びに対して判定する (ずらす前だと直前クリップを取り違える)。
    def _resolve_origin(self, timeline, track, origin, start):
        if self._archive_index_policy != ARCHIVE_INDEX_INHERIT:
            return origin
        if not isinstance(timeline.source, dict) or not timeline.source.get("archive"):
            return origin                        # クリップ用 Timeline は対象外
        base = timeline.base_video_track()
        if base is None or track.id != base.id:
            return origin                        # V1 以外は書き出し分割に関与しない
        previous = following = None
        for clip in sorted(base.clips, key=lambda c: c.timeline_start):
            if clip.timeline_start <= start + _EPS:
                previous = clip
            elif following is None:
                following = clip
        neighbor = previous or following
        if neighbor is not None and "archive_clip_index" in neighbor.origin:
            origin["archive_clip_index"] = neighbor.origin["archive_clip_index"]
        return origin

    # 貼り付け先トラックを決める (§3-3)
    def _resolve_track(self, timeline, item):
        info = item.get("track") or {}
        if item.get("kind") == clipboard.KIND_SUBTITLE:
            track = timeline.track_by_id(str(info.get("id") or ""))
            if track is not None and track.is_subtitle():
                return track
            track = timeline.base_subtitle_track()
            if track is not None:
                return track
            track = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
            timeline.tracks.append(track)
            return track

        if info.get("is_base"):
            return timeline.base_video_track()
        track = timeline.track_by_id(str(info.get("id") or ""))
        if track is not None and track.is_video():
            return track
        if len(timeline.video_tracks()) >= self._max_video_tracks:
            _logger.warning("映像トラックの上限 (%d) に達しているため貼り付けません",
                            self._max_video_tracks)
            return None
        track_id, index = timeline.next_video_track_id()
        track = Track(track_id, TRACK_VIDEO, index, name=f"Video {index}")
        timeline.tracks.append(track)
        return track

    # 素材をプールへ再登録して media_id を返す (見つからなければ None / §3-7)
    def _resolve_media(self, timeline, item):
        data = item.get("media")
        clip_media_id = str((item.get("clip") or {}).get("media_id") or "")
        if not data:
            return clip_media_id if timeline.media_by_id(clip_media_id) else None
        path = str(data.get("path") or "")
        existing = timeline.media_by_path(path) if path else None
        if existing is not None:
            return existing.id
        if not path or not os.path.exists(path):
            _logger.warning("素材が見つからないため貼り付けません: %s", path)
            return None
        media = MediaRef.from_dict(data)
        media.id = timeline.next_id("m")
        timeline.media_pool.append(media)
        return media.id

    # リンク音声を作る (§3-6)。素材に音声が無ければ作らない。
    def _attach_audio(self, timeline, track, clip, item):
        if item.get("kind") != clipboard.KIND_VIDEO:
            return
        media = timeline.media_by_id(clip.media_id)
        if media is None or media.is_image() or not media.has_audio:
            return
        audio_track = timeline.audio_track_for(track.id)
        if audio_track is None:
            audio_id, audio_index = timeline.next_audio_track_id()
            audio_track = Track(audio_id, TRACK_AUDIO, audio_index,
                                name=f"Audio {audio_index}", link_track=track.id)
            timeline.tracks.append(audio_track)
        attrs = item.get("audio") or {}
        audio_track.clips.append(AudioClip(
            timeline.next_id("a"), clip.id,
            gain_db=attrs.get("gain_db", 0.0), muted=attrs.get("muted", False)))
```

> `import os` と `from .model import MediaRef`、`from . import clipboard` の追加が要る。
> `clipboard` は `commands` を import しないため循環参照にはならない。

> **`apply()` の戻り値に注意**: `CommandStack.push()` は `False` のときスナップショットへ
> 戻さないため（`commands.py:100`）、**場所を空けたのに 1 件も貼れなかった場合でも `True` を返す**
> 必要がある（`self.shifted_sec > _EPS` の判定がそれ）。さもないと Undo できない変更が残る。

### 5.4 `TimelineController` へ足す窓口

```python
    # ------------------------------------------------------------------
    # コピー＆ペースト (ver3 resolve10 §5.4)
    # ------------------------------------------------------------------

    # 選択中のノードをクリップボードへ控える。控えた件数を返す (0 = 対象なし)。
    # Timeline は変更しないため履歴には積まない (Undo の対象外)。
    def copy_selected(self):
        return clipboard.copy_clips(self._timeline, self._selected_ids)

    def can_paste(self):
        return not clipboard.is_empty()

    # クリップボードの内容を再生ヘッド位置へ貼り付ける (Ctrl+V)
    # 貼り付けたノードが優先され、干渉した既存ノードだけが必要な分だけ右へずれる。
    # 戻り値: {"pasted": 件数, "skipped": 件数, "shifted": ずらした秒数, "empty": bool}
    def paste(self):
        payload = clipboard.payload()
        if not payload:
            return {"pasted": 0, "skipped": 0, "shifted": 0.0, "empty": True}
        paste_cfg = self._cfg["paste"]
        command = commands.PasteClips(
            payload, self._playhead,
            min_clip_sec=self._cfg["min_clip_sec"],
            insert_policy=paste_cfg["insert_policy"],
            ripple_scope=paste_cfg["ripple_scope"],
            archive_index_policy=paste_cfg["archive_index_policy"],
            max_video_tracks=self._cfg["media"]["max_video_tracks"],
        )
        if not self.execute(command):
            return {"pasted": 0,
                    "skipped": command.skipped or len(payload.get("items") or []),
                    "shifted": 0.0, "empty": False}
        if paste_cfg["select_pasted"] and command.created_clip_ids:
            self.select(command.created_clip_ids)
        if paste_cfg["move_playhead_to_end"] and command.pasted_end_sec is not None:
            self.set_playhead(command.pasted_end_sec)
        return {"pasted": len(command.created_clip_ids), "skipped": command.skipped,
                "shifted": command.shifted_sec, "empty": False}
```

### 5.5 編集画面（ショートカットと案内）

```python
        # コピー＆ペースト (ver3 resolve10 §5.5)
        # 貼り付けは 1 種類だけ。Ctrl+Shift+V は割り当てない (rev2 R-1)。
        bind("copy", self._copy)
        bind("paste", self._paste)
```

```python
    # Ctrl+C: 選択中のノードをコピーする
    def _copy(self):
        count = self.controller.copy_selected()
        self.preview.set_status(
            f"{count} 件のノードをコピーしました" if count
            else "コピーするノードが選択されていません")

    # Ctrl+V: 再生ヘッド位置へ貼り付ける (干渉した既存ノードは右へずれる)
    def _paste(self):
        result = self.controller.paste()
        if result["empty"]:
            self.preview.set_status("コピーされたノードがありません")
            return
        if result["pasted"] == 0:
            self.preview.set_status(
                "貼り付けできませんでした (トラックのロックや素材の欠落を確認してください)")
            return
        message = f"{result['pasted']} 件を貼り付けました"
        if result["shifted"] > 0:
            message += f" (既存ノードを {result['shifted']:.2f} 秒ぶん右へ移動)"
        if result["skipped"]:
            message += f" ({result['skipped']} 件は貼れませんでした)"
        self.preview.set_status(message)
```

* 初版にあった「貼り付けられる空きがありません（Ctrl+Shift+V で…）」の案内は**不要になった**
  （空きが無くても押しのけて必ず貼れるため）。
* 案内は既存の `preview.set_status()` を使うため、画面レイアウトの変更は無い。

### 5.6 右クリックメニュー（`timeline_view.py`）

キーを知らなくても使えるように、既存メニューへ 2 項目を足す。

* **クリップを右クリック**（`contextMenuEvent` のクリップ分岐）: 「ここで分割」の直後へ
  * `コピー\tCtrl+C` → `self._controller.copy_selected()`
  * `再生ヘッドへ貼り付け\tCtrl+V` → `self._controller.paste()`
* **空き領域を右クリック**: 「ここへ再生ヘッドを移動」の下へ「再生ヘッドへ貼り付け」を足す。
* 貼り付け項目は `setEnabled(self._controller.can_paste())` で灰色にする。
* 音声クリップのメニュー（`_build_audio_menu`）には足さない（V1 側から操作する規約 / §3-6）。

貼り付け位置は**あくまで再生ヘッド**（メニューを開いた位置ではない）。規約を 1 つに保つため、
メニュー項目のラベルにも「再生ヘッドへ貼り付け」と明記する（§9 Q3）。

### 5.7 設定の追加（`builder.py` / `settings_window.py`）

```python
# src/timeline/builder.py  _DEFAULT_SHORTCUTS へ追記
    # ノードのコピー＆ペースト (ver3 resolve10 §3-1)
    # 貼り付けは 1 種類だけ。paste_insert (Ctrl+Shift+V) は作らない (rev2 R-1)
    "copy": "Ctrl+C",
    "paste": "Ctrl+V",
```

```python
# src/timeline/builder.py  timeline_config() へ追記
        # コピー＆ペースト (ver3 resolve10 §7)
        "paste": _paste_config(cfg.get("paste", {})),
```

```python
# 候補のいずれかへ寄せる (ver3 resolve10 §7)。既存の _dock_area() と同じ流儀の新規ヘルパ。
# 想定外の値は既定へ落とす。黙って別の挙動になると原因が追えないため警告を出す。
def _one_of(value, choices, default):
    text = str(value or default).strip().lower()
    if text in choices:
        return text
    _logger.warning("貼り付けの設定が不正なため既定 %s を使用します: %r", default, value)
    return default


# 貼り付けの方針を検証して既定へ寄せる (ver3 resolve10 §7)
def _paste_config(values):
    values = values if isinstance(values, dict) else {}
    return {
        "ripple_scope": _one_of(values.get("ripple_scope"),
                                ("base_syncs_all", "track", "all"), "base_syncs_all"),
        "insert_policy": _one_of(values.get("insert_policy"),
                                 ("split", "shift_whole"), "split"),
        "archive_index_policy": _one_of(values.get("archive_index_policy"),
                                        ("inherit", "keep"), "inherit"),
        "move_playhead_to_end": bool(values.get("move_playhead_to_end", True)),
        "select_pasted": bool(values.get("select_pasted", True)),
    }
```

`settings_window.DEFAULT_SETTINGS["timeline"]` へ同じ既定（`shortcuts` の 2 キーと
`paste` セクション）を足す。`_fill_timeline_nested_defaults()`（`settings_window.py:780`）が
入れ子 1 段を補完するため、**既存の `setting.json` にも自動で現れる**（既存値は温存）。

---

## 6. 受け入れ基準（動作表）

| # | 操作 | 期待 |
|---|---|---|
| 1 | V1 のクリップを選択 → Ctrl+C | 「1 件のノードをコピーしました」。Timeline は変わらない（`*` も付かない） |
| 2 | **V2 の画像をコピー → V2 上で他ノードに干渉しない位置へ Ctrl+V** | **そのまま貼られる。V2 の他ノードも V1・S1 も 1 つも動かない**（要望 P4） |
| 3 | 2 で貼り付け先が V2 の既存ノードに**部分的に**重なる | 重なった既存ノードだけが**必要な分だけ**右へずれる（穴は空かない）。V1・S1 は動かない |
| 4 | **隙間の無い V1 の先頭付近で Ctrl+V** | 割り込んで貼られ、**V1 上の以降のクリップがまとめて右へ**ずれる。V1 の全長が伸びる（要望 P3） |
| 5 | 4 で再生ヘッドがクリップの途中 | そのクリップが再生ヘッドで**分割**され、後半が右へずれる。リンク音声も 2 本になる |
| 6 | **4 の直後に S1 の字幕・V2 のオーバーレイを見る** | **V1 と同じ量だけ右へずれている**（声と字幕がズレない / 要望 P14）。跨いだ字幕は再生ヘッド位置で分割される |
| 7 | 字幕を選択 → Ctrl+C → Ctrl+V | 字幕トラックへ貼られる。**V1 は動かない**（V1 へ貼ったときだけ全体が動く / §3-5） |
| 8 | 複数選択（V1 の 2 本 + 字幕）→ Ctrl+C → Ctrl+V | **相対位置を保ったまま**貼られる。V1 が対象に含まれるので**全トラックが同量**ずれる |
| 9 | 貼り付け直後 | 貼ったノードが選択状態。再生ヘッドは貼った範囲の終端。もう一度 Ctrl+V で続けて置ける |
| 10 | 貼り付け後に Ctrl+Z | 貼り付け前へ完全に戻る（右へずれた分も戻る）。ボタンは「元に戻す: 貼り付け」 |
| 11 | 字幕テキスト欄にフォーカス → Ctrl+C / Ctrl+V | **文字のコピー＆ペースト**として働く（クリップは増えない） |
| 12 | アーカイブ用画面で clip2 の一部をコピー → clip1 の中へ Ctrl+V | clip1 の一部として書き出される。**出力ファイル数は増えない**（§3-8 `inherit`） |
| 13 | ロックしたトラックが貼り付け先 | 貼られず、**そのトラックは 1 つも動かない**。「N 件は貼れませんでした」と案内 |
| 14 | 何もコピーせずに Ctrl+V | 「コピーされたノードがありません」 |
| 15 | **Ctrl+Shift+V を押す** | **何も起きない**（割り当て無し / rev2 R-1）。`setting.json` へ手で書いても復活しない（§2.2） |

---

## 7. 追加する設定（`setting.json`）

```json
"timeline": {
  "shortcuts": {
    "copy": "Ctrl+C",
    "paste": "Ctrl+V"
  },
  "paste": {
    "ripple_scope": "base_syncs_all",
    "insert_policy": "split",
    "archive_index_policy": "inherit",
    "move_playhead_to_end": true,
    "select_pasted": true
  }
}
```

| キー | 既定 | 意味 |
|---|---|---|
| `shortcuts.copy` | `Ctrl+C` | コピー。空文字で無効化できる |
| `shortcuts.paste` | `Ctrl+V` | 貼り付け（貼り付け側が優先。干渉した既存ノードが右へずれる） |
| `paste.ripple_scope` | `base_syncs_all` | 右へずらす範囲（`base_syncs_all` = **V1 へ貼るときだけ全トラック**・それ以外はそのトラックだけ / `track` = 常に干渉したトラックだけ / `all` = 常に全トラック）（§3-5） |
| `paste.insert_policy` | `split` | 貼り付け位置を跨ぐクリップの扱い（`split` / `shift_whole`） |
| `paste.archive_index_policy` | `inherit` | アーカイブ用 V1 の index 引き継ぎ（`inherit` / `keep`） |
| `paste.move_playhead_to_end` | `true` | 貼り付け後に再生ヘッドを終端へ送る（連続貼り付け用） |
| `paste.select_pasted` | `true` | 貼り付けたノードを選択状態にする |

**既存設定との関係（注意点）**

* `timeline.ripple_sync_tracks`（リップル削除用 / 既定 `all`）は**貼り付けには使わない**。
  貼り付けには「V1 のときだけ全トラック」という 3 つ目の値が要るため、
  専用キー `paste.ripple_scope` を分ける（§3-5）。
* `timeline.min_clip_sec` は貼り付けにもそのまま効く（跨ぎクリップの分割可否・貼れる最小尺）。
* `timeline.media.max_video_tracks` は貼り付けでのトラック新設にも効く。
* **`shortcuts.paste_insert` は存在しない**。初版の設計から削除した（rev2 R-1）。

---

## 8. テスト計画（`tests/test_timeline_clipboard.py` 新規）

GUI 非依存の層（`clipboard.py` / `commands.py`）へ寄せた設計のため、
**要望の中身はすべて Qt 抜きで検証できる**。既存テスト（`test_timeline_commands.py`）と同じ流儀で書く。

| # | 観点 | 内容 |
|---|---|---|
| T1 | payload 生成 | 単一・複数選択で `offset_sec` / `span_sec` が正しい。音声クリップ ID を渡すと V1 へ読み替わる |
| T2 | 値のコピー | コピー後に元クリップを移動・削除しても payload が変わらない（§3-1） |
| T3 | 基本の貼り付け | 空きトラックへ貼ると位置・尺・`source_in/out` / `transform` / `z_order` / 字幕の色が一致する |
| T4 | **干渉なし（P4 / 最重要）** | V2 の空き位置へ貼ると **Timeline 全体で 1 クリップも `timeline_start` が変わらない**（V2 の後続ノードも V1 も S1 も）。`shifted_sec == 0` |
| T5 | **部分干渉（P3 / 最重要）** | `required_shift()` が `end - 最初に干渉するクリップの開始` に一致し、**穴が空かない**（貼り付け終端＝ずれた既存クリップの開始） |
| T6 | 後続ノードの同量移動 | 干渉クリップの後ろに居るクリップも同じ `shift` だけ動き、**トラック内の間隔が保たれる** |
| T7 | **トラック限定（P4）** | **V2 へ**貼ったとき V1・S1 のクリップが 1 つも動かない（既定 `base_syncs_all`） |
| T8 | **V1 は全体が同期（P14 / 最重要）** | **V1 へ**貼ったとき S1・V2 のクリップが **V1 と同じ量だけ**動く。字幕と映像の相対位置が貼り付け前後で変わらない。跨いだ字幕が分割される |
| T8b | `ripple_scope` の切替 | `track` にすると V1 へ貼っても S1 が動かない。`all` にすると V2 へ貼っただけで S1 も動く。ロックした字幕トラックは `base_syncs_all` でも動かない |
| T9 | 跨ぎクリップの分割 | 再生ヘッドがクリップの途中のとき分割され、後半が右へ動く。**リンク音声も 2 本になる** |
| T10 | 跨ぎの端（最小尺） | 跨ぎ位置が端から `min_clip_sec` 未満のとき、**重なりが残らない**（丸ごと移動 / 終端切り詰めのどちらか） |
| T11 | `insert_policy` | `shift_whole` で跨ぎクリップが分割されず丸ごと動く |
| T12 | 音声リンク | 貼り付けた V1 クリップに `AudioClip` が付き、`gain_db` / `muted` が復元される。画像・無音素材では付かない |
| T13 | 素材の再登録 | 別 Timeline へ貼ると `media_pool` へ 1 件増える。同じパスなら増えない。実ファイルが無ければ**貼らず、場所も空けない**（§3-7） |
| T14 | Undo | 1 回の Undo で完全に戻る（クリップ数・全長・音声リンク・ずらした位置） |
| T15 | ロック | ロックしたトラックには貼られず、**そのトラックのクリップも動かない** |
| T16 | **アーカイブ index** | `inherit` で `split_by_clip()` のグループ数が貼り付け前と同じ。`keep` では増えることも確認（挙動の明文化） |
| T17 | 設定の検証 | `_paste_config()` が不正値を既定へ寄せる。`_shortcut_config()` が `copy` / `paste` を返し、**`paste_insert` は返さない** |
| T18 | 変更したのに戻せない事故 | 場所は空いたが全項目が貼れなかった場合でも、Undo で元へ戻る（§5.3 の戻り値） |

GUI 側（`bind` の登録、`_ShortcutGuard` による無効化）は既存テストに倣い手動確認とする（§10 Phase 5）。

---

## 9. 確認事項（推測実装しない点）

| # | 確認したいこと | 本書の既定案 |
|---|---|---|
| **Q1** | **解決済み（rev2 R-3）**。V1 へ貼るときは S1・V2 も同量ずらす（`ripple_scope="base_syncs_all"`）。残る細部として、**ロックされた字幕トラックは同期対象でも動かさない**方針で良いか（動かすとロックの意味が無くなるが、その字幕だけ V1 とズレる） | ロックを優先して**動かさない**。ズレたくなければロックを外してから貼る（§3-5） |
| **Q2** | 貼り付け先トラックは**コピー元のトラック**で固定して良いか（マウス位置やクリック中のトラックへは貼らない） | 固定。現行 UI に「対象トラック」の概念が無いため |
| **Q3** | 右クリックメニューから貼り付けたとき、貼り付け位置は**再生ヘッド**で良いか（右クリックした位置ではない） | 再生ヘッド。規約を 1 つに保つため |
| **Q4** | 割り込みで**跨いだクリップを分割する**（既定 `split`）で良いか。字幕が分割されると同じ文が 2 つに割れる点は許容できるか | 分割。許容できなければ `shift_whole` へ変更可能 |
| **Q5** | ずらし量は**最小限**（干渉クリップの開始まで詰める / §3-4）で良いか。「貼り付けた尺ぶん必ずずらす」方が予測しやすい、という考え方もある | 最小限。穴が空かず、要望の「干渉しなければそのまま」と地続きのため |
| **Q6** | 貼り付け後に**再生ヘッドを終端へ送る**（既定 true）で良いか | 送る。Ctrl+V 連打で並べられるため |
| **Q7** | アーカイブ用で `archive_clip_index` を**直前クリップから引き継ぐ**（既定 `inherit`）で良いか | `inherit`。`keep` は出力ファイル名が衝突する（§2.6） |
| **Q8** | クリップボードを**画面をまたいで共有**（クリップ用↔アーカイブ用）して良いか | 共有する。素材が実在するときだけ貼るため壊れない |
| **Q9** | Ctrl+X（切り取り）も要るか。要望には無いため本書では**対象外**にしている | 対象外（`copy` + `delete` で代替できる） |

---

## 10. 実装フェーズ

**実装状況: Phase 1〜4・6・7 完了（2026-08-21）。Phase 5 のうちコード実装は完了、
画面上での目視確認（§6 の 1〜15）のみ未実施。**
自動テストは `tests/test_timeline_clipboard.py`（41 件）を追加し、既存を含め全 484 件が通る。

| Phase | 内容 | 完了条件 |
|---|---|---|
| 1 | `clipboard.py` 新規（payload 生成・保持） | T1・T2 が通る |
| 2 | `commands.split_clip_at()` 抽出＋`SplitClip` の委譲 | 既存 `test_timeline_commands.py` が無改造で通る |
| 3 | `commands.required_shift()` / `make_room_for_range()` | T4〜T6・T9〜T11 が通る |
| 4 | `commands.PasteClips` + `TimelineController.copy_selected/paste` | T3・T7・T8・T12〜T16・T18 が通る |
| 5 | ショートカット登録・案内・右クリックメニュー | 手動で §6 の 1〜15 を確認 |
| 6 | 設定の既定追加（`builder` / `settings_window`）とマイグレーション確認 | T17 と、既存 `setting.json` に 2 キー＋`paste` が現れること |
| 7 | 全自動テスト実行 | 既存テストを含めて全件通る |

---

## 11. 非対象・既知の割り切り

* **Ctrl+Shift+V は割り当てない**（rev2 R-1）。貼り付けは Ctrl+V の 1 種類だけ。
  初版にあった「既存優先で短縮する貼り付け」（`PASTE_FIT` / `overlap_policy` / `free_fragments()`）は
  **設計ごと削除した**。同じ結果が欲しい場合は、貼った後に手でトリムする。
* **Ctrl+X（切り取り）は対象外**（要望に無いため / Q9）。
* **トラック自体のコピー**（V2 をまるごと複製する等）は対象外。ノード単位のみ。
* **OS クリップボード経由の他アプリ連携**は対象外（§3-1 案 C）。payload の形だけ将来に備える。
* **ロックされたトラックは、V1 と同期する設定（既定 `base_syncs_all`）でも動かない**（§3-5 / Q1）。
  字幕トラックをロックしたまま V1 へ貼ると、その字幕だけ V1 とズレる。
* `ripple_scope="track"` を選んだ場合は **V1 へ貼ると字幕とのズレが生じる**（P13）。
  その運用では貼り付け後に字幕を調整する前提になる。
* V1 へ貼ると Timeline 全体が伸びるため、**書き出し時間もその分伸びる**。
* アーカイブ用で `archive_index_policy="keep"` を選んだ場合の出力衝突は**設定した人の責任**とし、
  実装では貼り付け時に警告ログを残すに留める。
