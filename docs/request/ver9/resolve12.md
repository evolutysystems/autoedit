# ver9 resolve12 — テーマを入れてもイントロカード (黒帯カード) が出ない — 保存フックの未配線と、テーマ区間だけの取り直し

発端: 2026-10-29 のチャット報告「テーマを入力しても、以前は出力できていた黒いテーマカードが出てこなくなっている」
対象: `src/gui/timeline/timeline_editor_dialog.py` / `src/gui/timeline/archive_timeline_dialog.py` /
　　　`src/archive/project_resume.py` / `src/archive/clip_writer.py` / `src/archive/vod_range.py`
調査日: 2026-10-29 (main `4755926` / v1.7.0)
種別: **修正設計書 (実装前)**。§1 の調査は実データ (`vod_2912229276`) で確認済み
前提: ver9 resolve5 (区間だけの取り直し) / docs/error/fromUser/resolve.md §6-6・§9-7

---

## 0. 先に結論

### 0.1 3 行で

1. **テーマは正しく保存されている。** 欠けているのは**イントロカードの素** (クリップ開始の
   `buffer_sec` 秒**手前**の映像) で、プロジェクトの `.media/` にも元 VOD にも無い。
2. **原因は保存フックの未配線。** `ArchiveTimelineDialog._keep_extra_beside_project()` は
   10/23 の `d73aedb` で「元 VOD を手放してもイントロを作れるように」追加されたが、
   **`save_project()` から呼ぶ 1 行が入っていない**。追加された日から一度も動いていない。
3. **直し方は 2 本立て。** (a) 配線を直して元 VOD があるうちに控えを作る。
   (b) 既に元 VOD が無いプロジェクトのために、**テーマが入っている区間だけ** ver9 resolve5 の
   `vod_range.fetch_range` で取り直す。1 セクションあたり **3〜6MB** で済む。

### 0.2 なぜ「カードだけ」消えて、タグは出るのか

テーマ演出は `clip_writer._decorate_clip()` の 2 段構えである。

```python
# src/archive/clip_writer.py:383
def _decorate_clip(source, out, clip, theme, card, ...):
    if not theme or not card["enabled"]:
        return [out], out
    body = _build_tag_overlay(...)        # ① 左上タグ: 本編へ焼く → 素材は本編だけで足りる
    intro = _build_intro_card(...)        # ② イントロカード: クリップ開始の「手前」が要る
    parts = [intro, body] if intro else [body]
```

① は本編の映像に重ねるだけなので必ず出る。② だけが**クリップ素材に入っていない区間**
(開始の手前 `buffer_sec` 秒) を必要とし、素が無ければ `None` を返して黙って省略される。
「テーマは効いているのに黒いカードだけ出ない」という見え方はこの構造から来る。

---

## 1. 現象と原因 (調査結果)

### 1.1 テーマは保存されている

`C:\Users\evolu\AppData\Local\Stretheus\archive_work\vod_2912229276.archive.timeline.json`
の `source.archive.clips[]`:

| index | media_id | vod_start | vod_end | theme |
| --- | --- | --- | --- | --- |
| 1 | m1 | 140.0 | 540.0 | あり (6 文字) |
| 2 | m2 | 990.0 | 1170.0 | あり (6 文字) |
| 3 | m3 | 1210.0 | 1665.0 | あり (12 文字) |
| 4 | m4 | 1755.0 | 1935.0 | あり (5 文字) |
| 5 | m5 | 1980.0 | 2160.0 | — |
| 6 | m6 | 2475.0 | 2655.0 | — |
| 7 | m7 | 3780.0 | 3960.0 | あり (6 文字) |
| 8 | m8 | 4920.0 | 5265.0 | あり (10 文字) |
| 9〜11 | m12 / m14 / m15 | — | — | — |

UI (`theme_edit.editingFinished` → `SetArchiveClipTheme`) から Timeline への保存、
`result_data()` → `edited[].theme` → `_decorate_clip` の受け渡しはいずれも正常である。

### 1.2 イントロの素が 1 件も無い

`vod_2912229276.archive.media/` の中身は `m1_normalized.mp4`〜`m16_normalized.mp4` のみで、
**`<media_id>_intro.mp4` が 0 件**。控えを作る仕組み (resolve.md §9-7) が一度も走っていない。

### 1.3 元 VOD はゴミ箱にある

`archive_work\vod_2912229276.mp4` (2.4GB) はゴミ箱に実在する。
`archive_tab._sweep_old_sources()` → `vod_retention.sweep_unneeded()` が
**アーカイブタブを開いた時点で**自己完結プロジェクトの元 VOD を送ったものである (方針どおりの動作)。

結果、`_build_intro_card()` は

```python
# src/archive/clip_writer.py:300
if source and os.path.exists(source):      # ← 元 VOD: ゴミ箱なので偽
elif intro_source and os.path.exists(intro_source):   # ← 控え: 0 件なので偽
else:
    _logger.info("clip%d: 元 VOD もイントロ素材が無いためイントロカードを省略します …")
    return None
```

で必ず `None` を返す。**再編集して書き出すかぎり、テーマを何度入れ直してもカードは出ない。**

### 1.4 根本原因 — 保存フックが未配線

`_keep_extra_beside_project()` (`archive_timeline_dialog.py:220`) の呼び出し元を全探索した結果、
**定義以外の出現が無い**。基底の保存処理は次の 3 つしか呼んでいない。

```python
# src/gui/timeline/timeline_editor_dialog.py:891-901  (save_project)
if self._keep_media_for_kind():
    self._copy_media_beside_project(target)        # 素材の複製
elif self._project_cfg["keep_audio"]:
    self._export_audio_sidecars(target)            # 音声サイドカー
if self._project_cfg["keep_blur_cache"]:
    self._keep_blur_cache_beside_project(target)   # ぼかしの追従結果
# ← ここに _keep_extra_beside_project(target) が無い
```

`git log -S` でも、この関数が追加された `d73aedb` (2026-10-23)
で `timeline_editor_dialog.py` 側に入ったのは `recent_path` / `dedupe_recent` の修正だけで、
**呼び出し行は最初から存在しない**。設定 `archive_keep_intro_source` も既定 `true` のままで、
利用者側に落ち度は無い。

### 1.5 なぜテストで気づけなかったか

`tests/test_archive_intro_source.py:232` のコメントがそのまま答えである。

```
# 保存フック (_keep_extra_beside_project) だけを取り出して試す (Qt を起動しない)
```

`dialog._keep_extra_beside_project(...)` を**直接呼んで**検証しているため、
関数の中身は 6 件すべて緑でも、**`save_project()` から呼ばれるかどうかは一度も見ていない**。
「機能の中身」は試験され、「機能が繋がっているか」が抜けていた。

### 1.6 併発 — 自己完結の判定がイントロの控えを見ない

`vod_retention.is_self_contained()` は `.media/` に映像が 1 つでもあれば自己完結とみなし、
`_intro.mp4` は**意図的に数から除いている** (resolve.md §9-7: 「素材の複製に失敗したのに
イントロの控えだけ出来た」状態を自己完結と誤判定しないため)。

この判定は正しいが、**逆向きの保証が無い**。すなわち
「イントロの控えがまだ無くても元 VOD を捨ててよい」ことになっている。
§1.4 の配線漏れと重なって、取り返しのつかない状態 (素が両方無い) が成立していた。

### 1.7 併せて見つかった不整合 — 統合セクションでは控えの位置がずれる

控えは **media 単位** (`<media_id>_intro.mp4`) で作り、読み出しは **セクション単位** で行っている。
ところが統合されたセクションでは、両者の開始秒が一致しない。

| | 開始秒 | 根拠 |
| --- | --- | --- |
| セクション 1 (`clips[0]`) | **140.0** | `clips[].vod_start` |
| 代表 media (`m1`) | **180.0** | `media[].vod_start` |
| 実際に 140〜180 を持つ media | `m10` | 後から追加して統合された側 |

- 作る側 (`_keep_extra_beside_project`) は `archive_media()` を回るので、`m1` 用には **177〜180 秒**、
  `m10` 用には **137〜140 秒**の控えを作る。
- 読む側 (`project_resume.rebuild_prepared`) は `clips[].media_id` = `m1` を引くので、
  **177〜180 秒**の控えをイントロに使う。
- しかしイントロカードに要るのはセクション開始の手前 = **137〜140 秒**である。

つまり**配線を直しただけでは、統合済みのセクション (今回なら index 1 と 3) のカード背景が
無関係な位置の映像になる**。今は控えが 0 件なので表面化していないが、修正と同時に直す必要がある。

### 1.8 いつから出なくなったか

| 時期 | 状態 |
| --- | --- |
| 〜10/22 (v1.6.0 以前) | 元 VOD が作業フォルダに残り続けていたため、再編集でも元 VOD からカードを作れていた |
| 10/23 `d73aedb` (v1.6.1) | 元 VOD の自動整理を導入。控えの仕組みも入れたが**配線漏れ**。以後、保存→再起動で素が消える |
| 10/28〜29 (v1.7.0) | 報告のプロジェクトで発現。初回実行だけはカードが出る (元 VOD がその場にあるため) |

「以前は出ていた」は正しく、**v1.6.1 からの退行**である。

---

## 2. 設計方針

### 2.1 やること

1. **保存フックを配線する** (§3.1)。元 VOD があるうちに控えを作るのが第一の防衛線。
2. **控えの位置をセクション基準へ揃える** (§3.2)。§1.7 の不整合を同時に閉じる。
3. **テーマが入っている区間だけ取り直す** (§3.3〜§3.5)。元 VOD も控えも無いプロジェクトの救済。
   取得範囲はイントロカードの設計 (`[開始 - buffer_sec, 開始]`) にそのまま合わせる。
4. **省略したことが分かるようにする** (§3.7)。今回「黙って落ちた」ために成果物を見るまで気づけなかった。
5. **配線そのものを検査するテストを足す** (§6)。

### 2.2 やらないこと

- **元 VOD の自動整理をやめない。** 45GB 滞留を解消した方針 (resolve.md §6-6) は維持する。
  `keep_vod` の既定も `false` のまま変えない。
- **イントロの控えを自己完結の判定に入れない。** resolve.md §9-7 の判断を覆さない。
  代わりに「捨てる前に作る」(§3.1) と「無ければ取り直す」(§3.3) で塞ぐ。
- **テーマが空のセクションは取りに行かない。** 利用者の指示どおり、通信するのは
  **テーマが入っている区間だけ**とする。
- **元 VOD 全体の取り直しはしない。** ver9 resolve5 と同じく区間のみ。
- **採点・無音カット・字幕には触れない。** 本件はイントロカードの素だけの問題である。

---

## 3. 実装設計

### 3.1 保存フックの配線

基底に空のフックを置き、アーカイブ用だけが中身を持つ形にする
(`_keep_blur_cache_beside_project` と同じ作法)。

```python
# src/gui/timeline/timeline_editor_dialog.py  (save_project 内 / project_io.save の前)
if self._project_cfg["keep_blur_cache"]:
    self._keep_blur_cache_beside_project(target)
# 種別ごとの追加の控え (アーカイブ用はイントロカードの素 / ver9 resolve12 §3.1)
self._keep_extra_beside_project(target)

# 同ファイル内に既定の実装を置く (クリップ用は何もしない)
def _keep_extra_beside_project(self, project_path):
    return
```

- **自動保存 (`_on_autosave`) からは呼ばない。** 数秒ぶんとはいえ ffmpeg が走るため、
  60 秒ごとの自動保存に載せると編集中に引っかかる。保存は「本体の保存」のときだけでよい。
- `_save_after_confirm()` (`project_resume.py:402`) は `project_io.save` を直接呼ぶ経路で、
  ここも控えを作らない。**書き出し直前の補完 (§3.3) が走るためそのままでよい**。

### 3.2 控えの位置をセクション基準へ揃える (§1.7 の修正)

ファイル名の規約 (`<media_id>_intro.mp4`) は変えない。**どの media_id を選ぶか**を
作る側・読む側で 1 か所に寄せる。

```python
# src/archive/timeline_builder.py へ追加
# セクションの開始を担当する素材の media_id を返す (ver9 resolve12 §3.2)
#
# 統合されたセクションは clips[].media_id (代表) と、実際に開始秒を持つ素材が食い違う。
# イントロカードはセクション開始の手前から作るため、開始を担当する素材で控えを作らなければ
# 無関係な位置の映像がカードの背景になる。
# 選び方: media[] のうち vod_start がセクションの vod_start と最も近いもの。
#         media の控えが無い (旧プロジェクト) 場合は clips[].media_id へ落ちる。
def intro_media_id(timeline, clip_index):
    ...
```

- `_keep_extra_beside_project` は **`clips[]` を回る** (media ではなく) ように変える。
  テーマの有無・開始秒・media_id がすべてセクション側にあるため、ここが自然な単位である。
- `project_resume.rebuild_prepared` も同じ `intro_media_id()` で読む。
- 切り出し位置はどちらも `clips[].vod_start - buffer_sec` で統一する。

### 3.3 テーマ区間だけの取り直し — 走らせる場所

**書き出しの直前**に補完フェーズを置く。保存時ではなく書き出し時にする理由は §4.4。

```
project_resume._run_from_archive_project()
  ① 素材の復旧
  ② 編集画面  → edited (= [{"index","theme","use"}]) が確定する
  ③ 確定後の上書き保存
  ③' ★ イントロの素の補完 (新規)  ← ここ
  ④ clip_writer.finish_clips(...)
```

③' の判定順 (1 セクションごと):

1. `use=False` → 何もしない
2. `theme` が空 → 何もしない (**通信しない**)
3. `prepared["intro_source"]` が既にある → 何もしない
4. 元 VOD が手元にある → 何もしない (`_build_intro_card` が直接切るため)
5. `vod_source.video_id` が無い / `refetch.enabled=false` → 何もしない (従来どおり省略)
6. それ以外 → **取り直す**

初回実行 (`clip_writer._write_clips`) 経路は元 VOD がその場にあるため 4 で終わる。
セクション追加で取り直した素材は `_keep_intro_source` が控え済みで 3 で終わる。
**通信が起きるのは「元 VOD を手放したプロジェクトの、テーマ付きセクション」だけ**である。

### 3.4 取得範囲 — テーマカードの設計に合わせる

イントロカードが使う区間は `_build_intro_card` と同一にする。

```
要求区間 = [ clips[].vod_start - intro_card.buffer_sec , clips[].vod_start ]
```

`vod_range.fetch_range()` はこの要求区間に自前で余裕を足す
(`pad = max(section_add.refetch.pad_sec, intro_card.buffer_sec)` / `vod_range.py:214`)
ため、呼び側は**要求区間だけを渡せばよい**。
`plan_window()` が整数秒へ丸め、取得はセグメント (約 12 秒) 単位になる。

取得素材から控えを作るのは既存 API がそのまま使える。
`source_origin_sec` は ver9 resolve5 §4.5 で**まさにこの用途のために**用意された引数である。

```python
fetched = vod_range.fetch_range(
    video_id, start_sec=vod_start - buffer_sec, end_sec=vod_start, ...)
clip_writer.export_intro_source(
    fetched["path"], vod_start, buffer_sec, dest, ffmpeg_cfg,
    source_origin_sec=fetched["origin_sec"])     # ← 取得素材の 0 秒が VOD の何秒目か
```

`export_intro_source` は `-ss` を `-i` の前に置いた**再エンコード**で切るため、
元 VOD から切ったときと同じフレームが得られる (キーフレームスナップが起きない)。
位置誤差は resolve5 §2.5 の実測で ±0.0001 秒。

`vod_start < buffer_sec` のセクション (VOD の先頭付近) は手前を確保できない。
`export_intro_source` が `None` を返す既存動作のままとし、**取りに行く前に弾く**。

### 3.5 新規 API の形

アーカイブ側に置き、GUI には依存させない (CLI・テストからも通せるようにする)。

```python
# src/archive/intro_source.py  (新規) もしくは clip_writer へ追加
# テーマが入っているセクションのイントロ素材を、足りないぶんだけ用意する。
#
# prepared     : rebuild_prepared の結果 (intro_source を埋めて返す)
# edited       : 編集画面の結果 [{"index","theme","use"}] — テーマの有無はこちらが正
# timeline     : vod_source / media の控えを引くため
# media_dir    : <プロジェクト>.media/ (控えの置き場。残せば次回以降は取得しない)
# workdir      : 取得素材の一時置き場 (TemporaryDirectory)
# 戻り値: {"filled": 作れた件数, "fetched": 取得した件数, "skipped": [(index, 理由)]}
def ensure_intro_sources(prepared, edited, timeline, settings, media_dir, workdir,
                         ffmpeg_cfg=None, progress_cb=None, cancel_cb=None):
```

- 作れた控えは **`media_dir` に残す**。次回の書き出しでは §3.3 の 3 で止まり、再取得しない。
- `media_dir` が無い経路 (保存していないプロジェクト) では `workdir` へ置く。その実行かぎり。
- 取得は 1 セクションずつ。`cancel_cb` は resolve5 と同じく区間の切れ目で効く。

### 3.6 失敗時の扱い

**本編を失わないことを最優先**し、現在の握り潰し方針を維持する。

| 事象 | 扱い |
| --- | --- |
| 取得失敗 (通信 / 権限 / 期限切れ) | そのセクションだけカード無しで続行。WARNING + 画面へ 1 行 |
| 連結・切り出し失敗 | 同上 |
| 中止 (キャンセル) | 以後のセクションは取りに行かず、用意できたぶんだけ使う |
| 空き容量不足 | `disk.ensure_free` が既に `DiskSpaceError` を投げる。補完全体を諦め、書き出しは続行 |

### 3.7 省略を見えるようにする

今回「黙って落ちた」ことが発見を遅らせた。次の 2 点を足す。

- `_build_intro_card` の省略ログを **INFO → WARNING** へ上げる
  (ただし `theme` が入っているときだけ。テーマ無しは正常系なので従来どおり)。
- 書き出し完了時のメッセージに「テーマを入れたがカードを作れなかったクリップ」を列挙する。
  `finish_clips` の戻り値に件数を添えるか、`progress_cb` 経由で画面へ出すかは実装時に決める
  (**要判断** — §4.6)。

### 3.8 設定

**新しい設定項目は増やさない。** 既存の値で足りる。

| キー | 既定 | 本件での意味 |
| --- | --- | --- |
| `archive.intro_card.enabled` | `true` | false なら補完もしない |
| `archive.intro_card.buffer_sec` | `3.0` | 取得する長さ・切り出す長さ |
| `archive.section_add.refetch.enabled` | `true` | **false なら通信しない** (取り直し全体の親スイッチ) |
| `archive.section_add.refetch.pad_sec` | `3.0` | 取得範囲の余裕 |
| `archive.section_add.refetch.min_free_mb_per_min` | `60` | 空き容量の確認 |
| `timeline.project.archive_keep_intro_source` | `true` | 保存時に控えを作るか (§3.1) |
| `archive.download.keep_vod` | `false` | 変えない |

---

## 4. 判断が要る点

### 4.1 認証 (sub-only / 限定公開)

`vod_range.fetch_range` は `auth_token` を受けるが、セクション追加の経路は**渡していない**
(`archive_timeline_dialog.py:998`)。公開 VOD だけを想定した現状の割り切りである。
本件も同じ扱いとし、取れなければ §3.6 のとおりカード無しで続行する。
採点時に所有判定を通った VOD であれば、多くの場合は取得できる。

### 4.2 公開期限切れ・削除された VOD

取り直しは当然できない。`§3.6` の扱いに乗せ、文言は
「元動画が Twitch に残っていないため、イントロカードを作れませんでした」とする。
**プロジェクトの素材 (本編) は手元にあるので、書き出しそのものは成功する。**

### 4.3 黙って通信してよいか — **要判断**

書き出しの途中で数十 MB を落とすことになる。案は 2 つ。

| 案 | 内容 | 評価 |
| --- | --- | --- |
| A | 黙って取得し、進捗に「イントロ用の映像を取得中…」と出す | 手数が少ない。取得量は数十 MB で、書き出し本体の時間に比べれば短い |
| B | 書き出し開始前に「テーマ 6 件ぶんの映像 (約 40MB) を取得します」と確認する | 通信が意外に思われない。クリック 1 回増える |

**推奨は A。** 既にセクション追加が同じ条件で黙って取得しており (resolve5 §4.7)、
そこと作法を揃えるのが一貫する。ただし利用者が B を望むならそちらでよい。

### 4.4 なぜ保存時ではなく書き出し直前に取りに行くのか

- テーマは**書き出す直前まで変わる**。保存時に判定すると、後でテーマを足した区間は取り逃し、
  消した区間は無駄に取ることになる。
- 保存は編集中に何度も走る (手動保存・確定後の保存)。そのたびに通信すると編集が止まる。
- 書き出しは元々長時間かつ進捗表示がある工程なので、数十秒の取得が紛れ込んでも自然である。

ただし **§3.1 の配線は書き出し時の補完と別に必要**である。元 VOD があるうちに控えを作っておけば、
そもそも通信が発生しない。順序としては「保存で作る」が本線、「書き出しで取り直す」が救済。

### 4.5 元 VOD を捨てる条件を変えるか — **要判断**

§3.1 を入れれば「保存した時点で控えが揃う」ため、現行の判定のままでも穴はほぼ塞がる。
それでも「保存せずに閉じた」場合は控えが無いまま元 VOD が消え得る。

| 案 | 内容 |
| --- | --- |
| C | 変えない (§3.3 の取り直しで救済する) — **推奨** |
| D | `sweep_unneeded` の直前に、テーマ付きセクションの控えが無ければ元 VOD から作る |

D は「掃除」の処理が ffmpeg を動かすことになり、起動時の体感が悪くなる。
C を推奨し、D は「取り直しができないプロジェクト (ローカル動画由来) が実害を出したら」再検討する。

### 4.6 省略の通知をどこへ出すか — **要判断**

§3.7 の 2 点目。既存の完了ダイアログ (`archive_tab._on_clip_done`) は出力パスを並べるだけで、
「部分的にうまくいかなかったこと」を伝える口が無い。
`finish_clips` の戻り値を変えると呼び側 3 か所に影響するため、
`progress_cb` で最終メッセージに添える案を本命としたい。実装時に確定する。

---

## 5. 変更ファイルと段階

| 段階 | ファイル | 内容 |
| --- | --- | --- |
| **1 (必須・小)** | `src/gui/timeline/timeline_editor_dialog.py` | `save_project` からフックを呼ぶ + 既定の no-op |
| 1 | `tests/test_archive_intro_source.py` | **配線**の検査を追加 (§6.1) |
| **2 (必須・中)** | `src/archive/timeline_builder.py` | `intro_media_id()` 追加 |
| 2 | `src/gui/timeline/archive_timeline_dialog.py` | `_keep_extra_beside_project` を `clips[]` 基準へ |
| 2 | `src/archive/project_resume.py` | `rebuild_prepared` の控え引きを `intro_media_id()` 経由へ |
| **3 (救済・中)** | `src/archive/intro_source.py` (新規) | `ensure_intro_sources()` |
| 3 | `src/archive/project_resume.py` | ③' の補完フェーズを差し込む |
| 3 | `src/i18n/catalog_en.py` | 新規文言の対訳 |
| **4 (可視化・小)** | `src/archive/clip_writer.py` | 省略ログの格上げ (テーマ有りのときのみ) |
| 4 | `src/gui/archive_tab.py` | 完了時の通知 (§4.6 の決定後) |

段階 1 と 2 だけでも**これから作るプロジェクトは直る**。
段階 3 は**既に元 VOD を失ったプロジェクト** (報告の `vod_2912229276` を含む) の救済である。

---

## 6. テスト計画

### 6.1 配線の検査 (今回抜けていたもの)

- `save_project()` を呼ぶと `_keep_extra_beside_project` が**呼ばれる**こと (mock で確認)。
- 自動保存 (`_on_autosave`) では**呼ばれない**こと。
- クリップ用ダイアログでは no-op で例外にならないこと。

### 6.2 セクション基準の控え (§3.2)

- 統合セクション (`clips[].vod_start=140` / 代表 `m1` の `vod_start=180`) で、
  **137〜140 秒**を切る `export_intro_source` 呼び出しになること (ffmpeg は mock)。
- `media[]` を持たない旧プロジェクトでは `clips[].media_id` へ落ちること。

### 6.3 補完フェーズ (§3.3〜§3.5) — ネットワーク不要

`vod_range.fetch_range` を mock にして、呼ばれる条件だけを検査する。

| 条件 | 期待 |
| --- | --- |
| テーマ無し | 呼ばれない |
| `use=False` | 呼ばれない |
| 控えが既にある | 呼ばれない |
| 元 VOD が手元にある | 呼ばれない |
| `video_id` が無い | 呼ばれない / 理由が `skipped` に載る |
| `refetch.enabled=false` | 呼ばれない |
| 上記以外 | `start=vod_start-buffer_sec` / `end=vod_start` で 1 回だけ呼ばれる |
| `vod_start < buffer_sec` | 呼ばれない (手前を確保できない) |
| 取得が例外 | 他のセクションの処理が続き、書き出しは継続する |

### 6.4 手で確かめる (ネットワークあり)

1. `vod_2912229276` を開き直し、テーマを変えずに書き出す
   → 6 件ぶん取得され、全クリップにカードが出ること。
2. 2 回目の書き出しでは**取得が走らない**こと (控えの再利用)。
3. 取得したカードの背景が、元 VOD から作ったものと同じ位置であること
   (ゴミ箱から元 VOD を戻して突き合わせる。統合セクション index 1・3 を重点的に)。
4. 機内モード (通信遮断) で書き出し → カード無しで完走し、警告が出ること。

---

## 7. 容量と所要の見積り

resolve5 §8 の実測 (1080p で約 0.29MB/秒 / セグメント約 12 秒) から:

| 項目 | 値 |
| --- | --- |
| 要求区間 | 3.0 秒 (`buffer_sec`) |
| twitch-dl へ渡す範囲 | 要求区間 ± `pad` 3 秒 = 9 秒 |
| 実際に落ちる長さ | セグメント境界で 12〜24 秒 |
| 1 セクションあたり | **約 3〜6MB** |
| 報告のプロジェクト (テーマ 6 件) | **約 20〜40MB** / 所要 1〜2 分 |
| 控えとして残る量 | 3 秒 × 6 件 ≒ **5MB** (`.media/` へ) |

元 VOD 1 本 (2.4GB) を抱え続けるのに比べれば無視できる。

---

## 8. 手元のプロジェクトを今すぐ直す方法 (実装前の回避策)

ゴミ箱の `vod_2912229276.mp4` (2.4GB) を
`C:\Users\evolu\AppData\Local\Stretheus\archive_work` へ復元してから書き出せば、
`finish_clips` が `source.input_path` 経由で元 VOD を見つけ、カードは従来どおり焼かれる。

ただし `keep_vod=false` のままでは**アーカイブタブを開いた時点で再びゴミ箱へ送られる**。
一時的に戻すなら `setting.json` の `archive.download.keep_vod` を `true` にしてから復元すること
(resolve5 §9 と同じ注意)。

---

## 9. 残課題

| 項目 | 状況 |
| --- | --- |
| ローカル動画から採点したプロジェクト | `video_id` が無く取り直せない。元動画は利用者のファイルで消えないため、§3.1 の配線が入れば実害は無い見込み |
| `cut_region` のフレーム精度 | 利用者判断で後回し (resolve5 §8)。本件とは独立 |
| 控えの寿命 | `.media/` に残り続ける。1 件 3 秒で小さいため、当面は消さない |

---

## 10. 実装結果 (2026-10-29)

§4 の判断は**すべて推奨案**で確定した (A: 黙って取得し進捗に出す / C: 捨てる条件は変えない /
通知は `progress_cb` 経由)。段階 1〜4 をすべて実装した。

### 10.1 変更したファイル

| ファイル | 内容 |
| --- | --- |
| `src/archive/intro_source.py` | **新規**。`ensure_intro_sources()` — テーマ区間だけの取り直しと控えの作成 |
| `src/gui/timeline/timeline_editor_dialog.py` | `save_project` からフックを呼ぶ (**本件の主因**) + 既定の no-op |
| `src/gui/timeline/archive_timeline_dialog.py` | 控えを**セクション単位**で作るよう変更。`_copy_prepared_intro_source` の引数名と説明も合わせた |
| `src/archive/timeline_builder.py` | `intro_media_id()` / `recorded_vod_path()` を追加 |
| `src/archive/project_resume.py` | `rebuild_prepared` の控え引きを `intro_media_id()` 経由へ。書き出し直前に ③' 補完フェーズを追加。`recorded_vod_path` は timeline_builder へ委譲 |
| `src/archive/clip_writer.py` | 省略ログを WARNING へ。`clip_meta` に `theme`。完了ラベル `_finish_label()` |
| `src/gui/archive_tab.py` | 完了ダイアログへ「カードを作れなかったクリップ」を添える |
| `src/i18n/catalog_en.py` | 新規文言 3 件の対訳 |
| `tests/test_archive_intro_source.py` | セクション基準への追随 + **配線の検査** (`SaveProjectWiringTest` 4 件) ほか 7 件追加 |
| `tests/test_intro_source_refetch.py` | **新規** 14 件 (ネットワーク不要) |

設定ファイルは**変更していない** (§3.8 のとおり既存キーで足りる)。

テストは **1989 件すべて成功** (追加前 1953 件 → 36 件追加)。

### 10.2 設計から変えた点

- **`recorded_vod_path()` を `timeline_builder` へ移した。** `intro_source` から要るが、
  `project_resume` を import すると循環するため。`project_resume.recorded_vod_path` は
  委譲として残したので、GUI 側の呼び口は変わらない。
- **§4.6 の通知は「完了ラベル + 完了ダイアログ」に確定。** `finish_clips` の戻り値は変えず、
  最後の `progress_cb(1.0, …)` のラベルへ載せ、`archive_tab._on_clip_done` が
  「完了」以外ならダイアログへ添える形にした。呼び側 3 か所への影響は無い。

### 10.3 実データでの実測 (§6.4 の 1〜3 に相当)

報告のプロジェクト (`vod_2912229276` / 元 VOD はゴミ箱・控え 0 件) で、
**統合セクションである index 1** を実際に取り直した:

| 項目 | 結果 |
| --- | --- |
| 選ばれた控えの名前 | **`m10_intro.mp4`** (代表の `m1` ではない = §1.7 の修正が効いている) |
| 要求区間 | 137.0〜140.0 秒 (`vod_start 140.0 - buffer_sec 3.0`) |
| twitch-dl へ渡した範囲 | 134〜143 秒 (余裕 3 秒・整数秒) |
| 取得量 / セグメント | **約 7.3MB** / 2 件 |
| 作れた控え | 3.23MB / 尺 **3.000 秒** |
| 位置の確認 | 控えの**最終フレーム**と本編素材 `m10_normalized.mp4` の**先頭フレーム**が同じ場面で連続 (人物・看板・手の動きが繋がる) |
| `summary` | `{'filled': 1, 'fetched': 1, 'skipped': []}` |

取得元は記録 (`vod_source`) から解決され (`from_record: True`)、
全長の問い合わせも発生しなかった。

### 10.4 これで何が直るか

| 場面 | 修正後 |
| --- | --- |
| これから採点するプロジェクト | 保存した時点で `.media/` に控えが揃う (§3.1)。**通信は起きない** |
| 既に元 VOD を失ったプロジェクト | 書き出しの直前に、テーマが入っている区間だけ取り直す (§3.3)。2 回目以降は控えを再利用 |
| 統合したセクション | 開始を担当する素材で控えるため、カードの背景が正しい位置になる (§3.2) |
| どうしても作れないとき | WARNING をログへ残し、完了ダイアログに clip 番号を出す (§3.7)。本編は従来どおり出る |
