# ver9 resolve16 — 縦プロジェクトのトリミング・ぼかし背景を全クリップへ当てる

発端: 2026-10-31 のチャット要望 (`docs/request/ver9/request16.md`)
　　　「縦プロジェクトから動画生成すると、場面によってトリミングが効かず上下が真っ黒になる」
　　　＋同日の追加報告「解消されていない / クリップをコピーして前へ持ってきた箇所が怪しい /
　　　これは新規縦プロジェクト作成時にこうなっているのか」(`vod_2912229276_edited_2.mp4`)
対象: `src/timeline/crop.py` (`is_valid`) / `src/timeline/compositor.py` (`crop_chain`) /
　　　`src/timeline/proxy_render.py` / `src/gui/timeline/preview_panel.py` /
　　　`src/settings/setting.json` / `settings_window.py`
調査日: 2026-10-31 (main `111f234` / v1.7.1 + 作業中の resolve15)
種別: **不具合の原因調査と改善案 → 実装完了 (クローズ)**。§1 は現行コードと実測で確認済み
状態: **2026-10-31 クローズ**。実装・テスト・実データ確認とも完了し、利用者の実機確認で
　　　「完了しました」と確認を得た (§11)
前提: ver5 resolve9 §5.5 (切り抜きの出力) / ver6 resolve3 §3.3 (プロキシとの相似) /
　　　ver9 resolve14 §3.2 (素材の持ち方 `media_mode`) / ver9 resolve6 §3.6 (拡大率との併用禁止)

---

## 0. 先に結論

### 0.1 要望 (原文の要点)

| # | 要望 |
| --- | --- |
| R1 | 一部の場面では、作成時に決めた**トリミングが使われていない** (上下の余白が真っ黒) |
| R2 | **トリミング・上下のぼかしが全てのクリップに反映**されて動画が出力されるようにする |

### 0.2 3 行で

1. **切り抜きの指定は「素材 1 本」に紐づいている。**
   `crop.is_valid()` が **layout の `media_id` とクリップの素材 ID の一致**を条件にしており
   (`crop.py:234-236`)、一致しない素材のクリップは `compositor.crop_chain()` が
   `None` を返して**従来の正規化 (`scale` + `pad=…:black`) へ落ちる** (`compositor.py:302-304`)。
   これが「上下が真っ黒」の正体である。
2. **アーカイブ用切り抜きの Timeline は素材を 1 本ではなく「セクションごとに 1 本」持つ。**
   実データでは 241 クリップ / **素材 11 本**、73 クリップ / 6 本、43 クリップ / 5 本。
   layout が紐づくのは**先頭クリップの素材だけ**なので、トリミングが当たるのは
   全体の **5.2% / 9.5% / 8.1%** (尺比) にすぎない (§1.4)。残りは全部黒帯になる。
3. **v1.7.1 (ver9 resolve14) の既定変更で表に出た回帰である。**
   ID 一致の検査は 2026-09-25 (`53fa617` / クリップ用プロジェクト = 素材 1 本の前提) のもの。
   2026-10-30 (`41c4b72` / resolve14) に素材の持ち方の既定が `rebase` (元 VOD 1 本へ張り替え =
   素材が 1 本に畳まれる) から `copy` (素材をそのまま複製 = **N 本のまま**) へ変わったため、
   前提が崩れたまま検査だけが残った (§1.5)。

### 0.3 何が変わるか

対象: アーカイブ切り抜き (素材 11 本) から全クリップを選んで作った縦プロジェクト。

| | 現在 (v1.7.1) | 改善後 |
| --- | --- | --- |
| トリミングが当たるクリップ | 先頭素材のぶんだけ (**尺で 5〜10%**) | **全部** |
| 他のクリップの見え方 | `scale` + `pad=black` = **上下が真っ黒** | 作成時に決めた枠 + ぼかし背景 |
| 寸法が違う素材 (将来・再正規化など) | 真っ黒 | **枠の縦横比を保ったまま換算して当てる** (黒帯なし) |
| 書き出しログ | クリップごとに WARNING (241 行) | **素材ごとに 1 行** |
| 画面の簡易プレビュー | 全フレームへ一律に枠を当てる (出力と不一致) | 出力と同じ解決を通す |
| 既存の縦プロジェクト (素材 1 本 / 張り替え方式) | — | **フィルタ文字列は 1 文字も変わらない** |

### 0.4 追加報告への回答 (2026-10-31)

| 問い | 回答 |
| --- | --- |
| 解消されていない | **本書はまだ設計だけで、実装していない** (作業ツリーに `crop.py` / `compositor.py` の修正は入っていない)。いまのビルドで直っていないのは想定どおり |
| コピーして前へ持ってきた箇所が怪しい | **コピー操作そのものは原因ではない。** 貼り付けは同じプロジェクト内なら**素材をパスで引き当てて同じ `media_id` を再利用**するため、コピーしたクリップは**コピー元と同じ扱い**になる。黒帯になるかは「コピーしたか」ではなく**そのクリップがどのセクション (素材) 由来か**で決まる。別セクション由来のクリップを先頭へ持ってきたため、冒頭から黒帯が出て目立った、というのが実体 (§1.8) |
| 新規縦プロジェクト作成時にこうなっているのか | **いいえ。** 作成時に決まるのは `source["crop"]` (枠と、枠を決めた素材の ID) だけで、**黒帯になるかどうかは書き出しのたびにクリップ 1 件ずつ判定**している (`compositor.crop_chain`)。したがって作り直しても結果は同じで、逆に**修正後は既存の縦プロジェクトを作り直さずに直る** |

---

## 1. 現状の仕組み (調査結果)

### 1.1 落ちている箇所

```python
# src/timeline/crop.py:228-236  (is_valid)
    media_id = str(layout.get("media_id") or "")
    if media_id and media_id != str(getattr(media, "id", "") or ""):
        return False            # ← 別素材のクリップは「指定が噛み合わない」
```

```python
# src/timeline/compositor.py:298-311  (crop_chain)
def crop_chain(timeline, media, profile, settings=None):
    layout = crop.load(timeline)
    if layout is None:
        return None
    if not crop.is_valid(layout, media, profile.width, profile.height):
        _logger.warning("切り抜きの指定が素材と噛み合わないため、従来の正規化で出力します")
        return None             # ← ここで黒帯の正規化へ落ちる
```

`crop_chain()` が `None` を返すと `compositor.still_filters()` は従来の正規化へ進む。

```python
# src/timeline/compositor.py:163-169  (still_filters の末尾)
    return [
        f"scale={profile.width}:{profile.height}:force_original_aspect_ratio=decrease",
        f"pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2",   # ← 黒で埋める
        ...
    ]
```

`pad` の色は省略時 **黒**。960x540 を 1080x1920 へ収めると上下に 647px ずつ黒が入る。
要望の「上下の余白が真っ黒」と完全に一致する。

### 1.2 経路はここ 1 本に寄っている (直す場所は 1 か所)

| 経路 | 入口 | 切り抜きの解決 |
| --- | --- | --- |
| 本番の動画生成 | `renderer._video_filters` → `compositor.base_filters` | `crop_chain` |
| プロキシ (再生用) | `proxy_render._build_command` → `compositor.base_filters` | `crop_chain` |
| 高精度プレビュー (停止中) | `preview_panel` → `compositor.still_filters` | `crop_chain` |
| 簡易プレビュー (1 コマ描画) | `preview_panel._apply_crop` | `crop.is_valid` + `crop.preview_rects` |

クリップ 1 件ずつ `media` を渡して呼ぶのは上の 3 つで、いずれも `crop_chain` 1 か所を通る。
**`crop_chain` を直せば生成・プロキシ・高精度プレビューが同時に直る。**

簡易プレビューだけは `timeline.media_by_id(layout["media_id"])` = **layout 側の素材**で
検査し、合格したら**全フレームへ一律に枠を当てている** (`preview_panel.py:794-800`)。
そのため**画面では全クリップにトリミングが効いて見え、出力だけが黒帯になる**。
利用者が作成直後に気づけなかった理由でもある。

### 1.3 アーカイブ用切り抜きは素材を「セクションごとに 1 本」持つ

```python
# src/archive/timeline_builder.py:95-101  (build_archive_timeline)
    for entry in prepared or []:
        media = media_probe.probe(
            entry["normalized_path"], timeline.next_id("m"), clip_settings, cfg["media"])
        ...
        timeline.media_pool.append(media)        # ← セクションごとに m1, m2, m3, …
```

採点で選ばれた区間ごとに `normalized.mp4` を作るため、素材は必ず複数になる。
その 1 本ごとが無音カットで何十個ものクリップへ割れる。

縦プロジェクトを作るとき、枠を決める素材は**先頭クリップのもの 1 本だけ**である。

```python
# src/gui/timeline/vertical_project_dialog.py:195-200  (_resolve_media)
    # 対象クリップが参照している素材 (先頭のものを使う)
    def _resolve_media(self):
        for clip in self._clips:
            media = self._timeline.media_by_id(getattr(clip, "media_id", ""))
            if media is not None:
                return media
```

この素材の ID が `crop.make_layout()` で `layout["media_id"]` へ入り、以後その 1 本にしか
当たらなくなる。

### 1.4 実データでの影響 (`output/*.archive.timeline.json`)

全クリップを選んで縦プロジェクトを作った場合の「トリミングが当たる割合」。

| プロジェクト | クリップ | 素材 | 枠が紐づく素材 | トリミングが当たる | 残り (黒帯) |
| --- | --- | --- | --- | --- | --- |
| `vod_2884417212` | 241 件 / 569.7s | **11 本** | m1 (960x540) | 8 件 / 29.3s = **5.2%** | 94.8% |
| `vod_2881981221` | 73 件 / 953.0s | **6 本** | m1 (960x540) | 9 件 / 90.8s = **9.5%** | 90.5% |
| `vod_2877176496` | 43 件 / 832.0s | **5 本** | m8 (960x540) | 3 件 / 67.5s = **8.1%** | 91.9% |

**3 本とも、素材の寸法はすべて 960x540 で完全に一致している** (`Counter({(960, 540): 241})`)。
つまり枠 (ソース px の矩形) は**幾何学的にはそのまま当たる**。
弾いているのは寸法ではなく **ID の不一致だけ**である。

### 1.5 現行コードでの再現

素材 3 本 (m1/m2 は 960x540、m3 は 1200x750 の画像) の縦 Timeline で `crop_chain` を呼んだ結果。

```
c1 m1 960x540   → crop (当たる)
c2 m2 960x540   → 従来の正規化 (黒帯)
   ['scale=1080:1920:force_original_aspect_ratio=decrease',
    'pad=1080:1920:(ow-iw)/2:(oh-ih)/2', 'setsar=1', 'fps=60', 'format=yuv420p']
c3 m3 1200x750  → 従来の正規化 (黒帯)
```

`m2` は **m1 と寸法が同じ**にもかかわらず弾かれている。これが実データで起きている形である。

### 1.6 いつ壊れたか

| 日付 | コミット | 出来事 |
| --- | --- | --- |
| 2026-09-25 | `53fa617` | ver5 resolve9。`is_valid` に ID 一致の検査を入れる。当時の縦プロジェクトは**素材 1 本**が前提で、検査は正しかった |
| 2026-10-29 | ver5 resolve10 | アーカイブからも作れるようにする。素材は**元 VOD 1 本へ張り替え** (`rebase`) → 畳まれるので ID は 1 つ。問題は出ない |
| 2026-10-30 | `41c4b72` | ver9 resolve14。既定を **`media_mode="copy"`** (素材を N 本のまま複製) へ変更。**ID 一致の前提が崩れる** |

現在の `setting.json` は `vertical.crop.media_mode = "copy"` なので、既定の操作で必ず踏む。
`media_mode="rebase"` へ戻せば回避できるが、それは resolve14 が解いた「元 VOD が消えると
開けない」問題へ戻ることになるため、回避策として採らない。

### 1.7 付随して分かった 2 点

- **警告がクリップごとに出る。** `crop_chain` の `_logger.warning` は呼ばれるたびに記録する。
  241 クリップなら本番 + プロキシで数百行になり、本当に見たいログが埋まる。
  `_warn_normalize_once` のような「1 回だけ」の仕組みがここには無い。
- **プロキシのキャッシュが残る。** `proxy_render.tile_key()` は `crop.load(timeline)` の JSON を
  鍵へ入れている (`proxy_render.py:129`) が、**計算 (コード) の版は `_KEY_VERSION` だけ**である
  (`proxy_render.py:32` = `"proxy-tile-1"`)。直しても鍵が変わらないので、黒帯のまま作られた
  タイルが再生され続ける。

### 1.8 追加報告の調査 — 「コピーして前へ持ってきた」クリップ

貼り付け (`commands.PasteClips`) が素材をどう決めるかを読むと、素材は**パスで引き当てて
使い回す**。新しい素材が増えるのは、パスが一致するものがプールに無いときだけである。

```python
# src/timeline/commands.py:1396-1412  (PasteClips._resolve_media)
        path = str(data.get("path") or "")
        existing = timeline.media_by_path(path) if path else None
        if existing is not None:
            return existing.id          # ← 同じプロジェクト内ならここ (ID は変わらない)
        ...
        media = MediaRef.from_dict(data)
        media.id = timeline.next_id("m")    # ← 別プロジェクトから貼るとここ (ID が増える)
```

現行コードで実際に試した結果 (素材 2 本・枠は m1 で決めた縦 Timeline):

```
① コピー前
  c1  media=m1  start= 0.00 → トリミング有り
  c2  media=m2  start= 5.00 → 黒帯 (従来の正規化)
② 同じプロジェクト内で c2 をコピー → 先頭へ貼り付け
  c3  media=m2  start= 0.00 → 黒帯 (従来の正規化)    ← 貼ったものも m2 のまま
  c1  media=m1  start= 5.00 → トリミング有り
  c2  media=m2  start=10.00 → 黒帯 (従来の正規化)
  media_pool は増えない
③ 別プロジェクト (パス不一致) から貼り付け
  media_pool: m1, m2, m3 ← 新しい素材として増える
  c3  media=m3 → 黒帯 (従来の正規化)
```

読み取れること。

1. **コピーは状態を変えていない。** 貼ったクリップはコピー元と同じ素材を指すので、
   コピー元が黒帯なら貼り先も黒帯、コピー元にトリミングが効いていれば貼り先も効く。
   「コピーして前に持ってきた箇所が怪しい」という見立ては、**症状の出る場所**としては
   正しく (先頭に来るので真っ先に目に入る)、**原因**としては §1.1 と同じものである。
2. **別の編集画面からの貼り付けは、素材を 1 本増やす** (③)。縦プロジェクトの素材は
   作成時に `<プロジェクト>.media/m3_normalized.mp4` のように改名して複製されるため
   (resolve14 §3.1)、アーカイブ用画面のクリップ (`…\media_m3\normalized.mp4`) を
   コピーして貼るとパスが一致せず、**必ず新しい素材 ID が付く** = 必ず黒帯になる。
   素材 1 本で作った (張り替え方式の) 縦プロジェクトでも、この経路だけは黒帯が出る。
3. 1. も 2. も**寸法は同じ 960x540** なので、§3.1 (ID で弾くのをやめる) だけで両方とも直る。
   `PasteClips` 側に手を入れる必要は無い。

#### 1.8.1 紛らわしい別件 — V1 以外へ貼った素材

貼り付け先がベース映像トラック (V1) でない場合、そのクリップは**オーバーレイとして合成**され、
切り抜きの対象にならない (`compositor.overlay_chain` / 枠は V1 にしか掛からない)。
さらに V1 にクリップが無い区間は `renderer._render_gap` が**黒い映像**を作る。

そのため「画像を上に重ねた場面だけ上下が黒い」ように見えることがあるが、これは
本書の不具合 (R1) とは別物で、修正の対象ではない。見分け方:

| 見え方 | 正体 |
| --- | --- |
| 映像が中央に小さく出て、上下が真っ黒 | 本書の不具合 (V1 のクリップに枠が当たっていない) |
| 画像・別素材の帯が出て、その上下が真っ黒 | V1 に何も無い区間 (ギャップの黒) + オーバーレイ |

---

## 2. 改善方針

### 2.1 やること

| # | 方針 | ねらい |
| --- | --- | --- |
| A | **`is_valid` の ID 一致検査をやめ、寸法一致で判断する** | R2 の本命。実データ 3 本はこれだけで 100% 直る |
| B | 寸法が違う素材へは**枠の縦横比を保ったまま換算**して当てる | 将来、別解像度へ再正規化した素材が混ざっても黒帯にしない |
| C | 枠を換算した旨・当てられなかった旨の警告を**素材ごとに 1 回**へ | ログを読める状態に保つ (§1.7) |
| D | `proxy_render._KEY_VERSION` を上げる | 黒帯のまま作られたタイルを一斉に捨てる (§1.7) |
| E | 簡易プレビューも**出力と同じ解決**を通す | 画面と出力を一致させる (§1.2) |

A が本体で、B は「寸法が違っても黒帯にしない」ための保険である。
**素材の全面をぼかし背景で収める逃げ道は作らない** (§2.3)。

### 2.2 なぜ ID 検査を外してよいか

- 枠は **ソース px の矩形**で持っている (`layout["frames"][n]["src"]`)。同じ寸法の素材に対しては
  意味も見え方も同じで、ID が違うことは幾何に影響しない。
- 縦プロジェクトの V1 は「**同じ正規化プロファイルで作った複数の素材**」を正当に持つ
  (resolve14 の `copy` 方式)。ID で弾くのは、この構造と矛盾している。
- 「別素材へ誤って当たる」心配について: 寸法一致の検査 (`layout["source"] == (媒体 w, h)`) は
  残すため、無関係な寸法の素材には当たらない。また V1 (ベース映像トラック) へ後から別素材を
  置く経路は無い (`commands.AddMediaClip._resolve_track` はベーストラックを必ず避け、
  オーバーレイ用トラックへ回す / `commands.py:1065-1082`)。
- そもそも**縦プロジェクトにおける切り抜きは「このプロジェクト全体の画づくり」**であり、
  素材ごとに変える指定ではない。要望 R2 そのものがこの解釈を求めている。

### 2.3 やらないこと

- **クリップごと・素材ごとに枠を持たせる**… 要望は「全クリップに同じトリミング」であり、
  指定を増やすと画面 (枠決め UI) まで作り直しになる。`source["crop"]` 1 つの構造は変えない。
- **素材を 1 本へ統合する (rebase へ戻す)**… resolve14 が解いた問題へ逆戻りになる (§1.6)。
- **`pad` の色を設定可能にする**… 黒帯そのものを出さないのが要望であり、色の選択は
  問題のすり替えになる。`pad` を使う経路 (横向きプロジェクト) の既定も変えない。
- **当てられない素材を「全面ぼかし背景」で収める**… 一度は案に入れたが採らない。
  それは**トリミングが効いていない絵**を、ぼかしが付くぶん黒帯よりましな形で出すだけで、
  場面ごとに見え方がばらつくという訴え自体は残る。寸法が違う素材にも
  §3.2 の換算で**枠そのものを当てる**ことで応える。
- **V1 の拡大率との併用**… 切り抜きがあるときは拡大率を使わない規約 (resolve6 §3.6) を守る。

---

## 3. 実装設計

### 3.1 `crop.is_valid` — ID ではなく寸法で判断する

```python
# src/timeline/crop.py (is_valid)

    # 素材 ID は「どの素材の絵で枠を決めたか」の控えであり、**合否の条件にはしない**
    # (ver9 resolve16 §2.2)。縦プロジェクトの V1 は、同じ正規化プロファイルで作った
    # 複数の素材 (アーカイブのセクションごとの normalized.mp4) を正当に持つため、
    # ID で弾くと 9 割のクリップが黒帯の正規化へ落ちていた (§1.4)。
    # 枠はソース px の矩形なので、寸法が一致していれば同じ意味で当たる。

    width = int(getattr(media, "width", 0) or 0)
    height = int(getattr(media, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return False

    # 枠を決めたときと素材の寸法が変わっていたら、そのままでは当てられない
    source = layout.get("source") or []
    if len(source) == 2 and (int(source[0] or 0), int(source[1] or 0)) != (width, height):
        return False
```

削除するのは `media_id` の 2 行だけで、他の検査 (版・mode と枠数の対応・枠が素材内に収まるか・
キャンバス寸法) はそのまま残す。

### 3.2 `crop.resolve_layout` — 素材 1 本ぶんの枠を決める (新規)

A/B を 1 か所に集める。**枠の計算を crop.py だけに持たせる**という本モジュールの約束
(`crop.py:6-8`) を崩さないため、compositor 側には判断を置かない。

```python
# src/timeline/crop.py

# 素材 1 本へ実際に当てる枠を決める (ver9 resolve16 §3.2)
#
#   ① 寸法が一致する          → layout をそのまま返す (素材 ID は問わない)
#   ② 寸法が違う              → 枠を素材の寸法へ換算して返す
#   ③ 素材が不明・寸法が 0 /
#      設定で ② を切っている  → None (従来どおりの正規化へ落ちる)
#
# 戻り値: 当てる layout / 当てられないなら None
def resolve_layout(layout, media, canvas_width, canvas_height, cfg):
    if not layout or media is None:
        return None
    if is_valid(layout, media, canvas_width, canvas_height):
        return layout                                   # ①
    if not cfg.get("apply_to_all_media", True):
        return None                                     # ③ (v1.7.1 と同じ振る舞い)

    fitted = _fitted_layout(layout, media)
    if fitted is not None and is_valid(fitted, media, canvas_width, canvas_height):
        return fitted                                   # ②
    return None                                         # ③


# 枠を素材の寸法へ換算する (ver9 resolve16 §3.2 ②)
#
# **枠の縦横比は変えない。** 幅と高さを別々の比率で伸ばすと
#   ・single … 前景の形が変わる (人物が縦長・横長になる)
#   ・split  … 枠と置き場所の縦横比がずれ、scale で引き伸ばされる
# ため、縦横比を保ったまま「素材に収まる最大」へ合わせ、**同じ相対位置**へ置く。
#
#   倍率 = min(素材幅 / 枠を決めた素材の幅, 素材高 / 枠を決めた素材の高さ)
#   中心 = 元の枠の中心 (素材の幅・高さに対する比) をそのまま使う
#   丸め = 幅・高さは _even (yuv420p)、位置は素材内へ収める
#
# 倍率は min を採るため、換算後の枠は必ず素材の中へ収まる (is_valid を通る)。
def _fitted_layout(layout, media): ...


# 枠 1 つを素材の寸法へ換算する (上の規約の実体)
def _fit_rect(rect, source_size, media_size): ...
```

### 3.3 `compositor.crop_chain` — 解決を通し、警告は素材ごとに 1 回

```python
# src/timeline/compositor.py

def crop_chain(timeline, media, profile, settings=None):
    layout = crop.load(timeline)
    if layout is None:
        return None

    # 背景の種類は指定に入っている (作ったときの見え方を保つ)。
    # ぼかしの強さだけは設定から読む。
    cfg = crop.config(settings or {})
    cfg["background"] = str(layout.get("background") or cfg["background"])

    # 素材ごとに当てる枠を決める (ver9 resolve16 §3.2)。
    # 同じ寸法の素材なら layout がそのまま返るため、素材 1 本の既存プロジェクトでは
    # フィルタ文字列が 1 文字も変わらない。
    applied = crop.resolve_layout(layout, media, profile.width, profile.height, cfg)
    if applied is None:
        _warn_crop_once(media, "切り抜きの指定が素材と噛み合わないため、従来の正規化で出力します")
        return None
    if applied is not layout:
        _warn_crop_once(media, "切り抜きの枠を素材の寸法へ換算しました")
    return crop.filter_chain(applied, profile.width, profile.height, profile.fps, cfg,
                             scale=profile.scale)
```

`_warn_crop_once` は `_warn_normalize_once` と同じ流儀 (`(media.id, 文言)` を集合で覚える)。

### 3.4 フィルタ文字列の対比 (960x540 → 1080x1920 / single + blur)

| | 文字列 |
| --- | --- |
| 当たる素材 (現在も改善後も同じ) | `split=2[cbg][cfg];[cbg]crop=540:492:210:24,scale=1080:1920:…increase,crop=1080:1920,boxblur=20:2[cbgo];[cfg]crop=540:492:210:24,scale=…decrease[cfgo];[cbgo][cfgo]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=60,format=yuv420p` |
| 他の素材 (現在) | `scale=1080:1920:…decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60,format=yuv420p` ← **黒帯** |
| 他の素材 (改善後 / 寸法一致) | **上段と完全に同じ** |
| 他の素材 (改善後 / 寸法違い・②) | `…crop=674:614:263:68,…,boxblur=20:2…` ← 枠を換算 (縦横比 1.098 を保つ) |

② の数値は 1200x750 の素材へ換算した場合。倍率 `min(1200/960, 750/540) = 1.25`、
枠 `540x492` → `674x614` (偶数丸め)、中心は相対位置 (0.5, 0.5) のまま。

§3.2 の規約どおりに計算した検算 (枠 `540x492+210+24` / 決めた素材 960x540):

| 素材 | 換算後の枠 | 縦横比 (元 1.0976) | 素材内 | 偶数 |
| --- | --- | --- | --- | --- |
| 960x540 (一致) | `540:492:210:24` | 1.0976 | ✓ | ✓ |
| 1920x1080 | `1080:984:420:48` | 1.0976 | ✓ | ✓ |
| 1200x750 | `674:614:263:68` | 1.0977 | ✓ | ✓ |
| 640x360 | `360:328:140:16` | 1.0976 | ✓ | ✓ |

`split` の 2 枠でも置き場所の比 (1.6875 / 0.8442) は保たれる (1200x750 で 1.6867 / 0.8452)。
ずれは**偶数丸めの 1px ぶん**で、`scale` が吸収する (最大 0.05%)。

### 3.5 簡易プレビューも同じ解決を通す

```python
# src/gui/timeline/preview_panel.py (_apply_crop)
#
# 枠は「再生ヘッド位置のクリップの素材」に対して解決する (ver9 resolve16 §3.5)。
# layout 側の素材で検査すると、出力では当たらない素材のクリップでも画面には
# 枠が当たって見え、画面と出力が食い違う (§1.2)。
```

`crop.resolve_layout()` の戻り値を `crop.preview_rects()` へ渡す形に替えるだけで、
描画のしかた (`drawPixmap`) は変えない。換算が入った素材では換算後の枠が使われるため、
画面もその素材の実際の出力と同じ切り口になる。

### 3.6 プロキシのキャッシュを無効化する

```python
# src/timeline/proxy_render.py:32
# 鍵の書式の版。計算を変えたら上げる = 既存のキャッシュが一斉に無効になる
_KEY_VERSION = "proxy-tile-2"      # ver9 resolve16: 切り抜きの解決を素材ごとに変更
```

`vertical.crop` 一式は既に `_settings_signature()` の対象 (`proxy_render.py:181`) なので、
新しい設定キーは自動で鍵へ入る。

### 3.7 設定 (`setting.json` / `settings_window.py`)

`vertical.crop` へ 1 つ追加する。**既定は「当てる」**で、v1.7.1 と同じ振る舞いへ
戻す逃げ道として用意する (ハードコード禁止 / 設定可能な値は setting.json)。

```json
"crop": {
  "background": "blur",
  "blur_radius": 20,
  "blur_power": 2,
  "max_scale": 2.0,
  "project_suffix": "_vertical",
  "close_gaps": true,
  "keyframe_window_sec": 30.0,
  "media_mode": "copy",
  "keep_media": true,
  "carry_subtitle_tracks": true,
  "apply_to_all_media": true
}
```

| キー | 既定 | 意味 |
| --- | --- | --- |
| `apply_to_all_media` | `true` | 素材 ID が違っても (寸法が違っても) 枠を当てる (§3.1 / §3.2 ②)。`false` で v1.7.1 と同じく枠を決めた素材だけ |

`crop.config()` では既存の読み方に合わせて `bool(section.get(…))` で読む。
換算の規約 (倍率の採り方・偶数丸め) は**計算そのもの**であり設定値ではないため
`crop.py` の中に置く (resolve9 からの `_even` / `_int_rect` と同じ扱い)。

### 3.8 判断の表 (素材 1 本ごと)

| 素材の寸法 | ID | `apply_to_all_media` | 結果 |
| --- | --- | --- | --- |
| 枠と一致 | 一致 | — | ① そのまま当てる (現在もここ) |
| 枠と一致 | **不一致** | `true` | ① そのまま当てる ← **今回の修正点** |
| 枠と一致 | 不一致 | `false` | ③ 従来の正規化 (黒帯) |
| 違う (比率が同じ・違うを問わない) | — | `true` | ② 枠を換算して当てる |
| 違う | — | `false` | ③ 従来の正規化 (黒帯) |
| 素材が不明 (`None`) / 寸法が 0 | — | — | ③ 従来の正規化 |

③ が残るのは「幾何を計算する材料が無い」場合だけで、実データ (§1.4) では起きない。

---

## 4. 判断が要る点

| # | 論点 | 本書の案 |
| --- | --- | --- |
| Q1 | ID 一致の検査を外すと「別素材へ枠が当たる」余地が生まれる | 外す。寸法一致を条件に残し、V1 へ別素材が入る経路が無いことを確認済み (§2.2) |
| Q2 | 寸法が違う素材をどうするか (全面をぼかし背景で収める案もあった) | **全面ぼかしは採らない** (§2.3)。枠の縦横比を保って同じ相対位置へ換算し、枠そのものを当てる |
| Q3 | `split` (上下 2 枠) の指定で寸法が違う素材をどうするか | ② で足りる。縦横比を保って換算するので枠と置き場所の比が崩れず、引き伸ばしも `vstack` の崩れも起きない |
| Q4 | 作成時 (縦プロジェクト作成ダイアログ) に「枠を換算した素材がある」と伝えるべきか | **必須ではない** (黒帯は出ない)。伝える案は §8 の残課題とする |
| Q5 | 枠を決める絵は先頭クリップのものだけ (§1.3)。場面ごとに構図は違う | 変えない。要望は「全クリップに同じトリミング」であり、同寸法なら幾何は同一。場面ごとの枠は別要望として扱う |
| Q6 | `_KEY_VERSION` を上げるとプロキシが全部作り直しになる (開き直しで数分) | 上げる。上げないと黒帯のタイルが残り、直ったかどうかの確認ができない |
| Q7 | 既存の縦プロジェクト (`rebase` 方式 / 素材 1 本) への影響 | 無し。①を通るためフィルタ文字列は 1 文字も変わらない (§6 の回帰テストで固定する) |

---

## 5. 変更ファイル

| ファイル | 変更 |
| --- | --- |
| `src/timeline/crop.py` | `is_valid` から ID 一致検査を外す / `resolve_layout` `_fitted_layout` `_fit_rect` を追加 / `_DEFAULTS` と `config()` へ 1 キー |
| `src/timeline/compositor.py` | `crop_chain` を `resolve_layout` 経由へ / `_warn_crop_once` を追加 |
| `src/timeline/proxy_render.py` | `_KEY_VERSION` を `proxy-tile-2` へ |
| `src/gui/timeline/preview_panel.py` | `_apply_crop` を再生ヘッド位置のクリップの素材で解決する |
| `src/timeline/vertical_builder.py` | `_rebase_layout` のコメント修正 (「`is_valid` は素材 ID を見る」が事実でなくなる。張り替え時に `media_id` を更新する処理自体は控えとして残す) |
| `src/settings/setting.json` | `vertical.crop` へ `apply_to_all_media` |
| `src/settings/settings_window.py` | 同じキーを既定へ (日本語コメント付き) |
| `tests/test_crop_layout.py` | `test_other_media` の意図を反転 (別 ID・同寸法は**当たる**) |
| `tests/test_vertical_render.py` | `test_source_size_changed_falls_back` → `test_source_size_changed_is_fitted` (寸法が変わった素材は**換算して当てる**) |
| `tests/test_crop_all_media.py` | 新規 (§6) |

---

## 6. テスト計画

### 6.1 新規 `tests/test_crop_all_media.py`

| # | 内容 | 期待 |
| --- | --- | --- |
| 1 | 素材 3 本 (m1/m2 同寸法・m3 別寸法) の縦 Timeline で全クリップの `crop_chain` | **全部に `crop=` が付く** (黒帯の `pad=…` が 1 件も無い) |
| 2 | 同寸法・別 ID の素材の文字列 | 枠を決めた素材の文字列と**完全一致** |
| 3 | 寸法 2 倍・比率同じ (1920x1080) の素材 | 枠が 2 倍へ換算される (`crop=1080:984:420:48`) |
| 4 | 比率が違う素材 (1200x750) | `crop=674:614:263:68` (倍率 1.25 / **縦横比 1.098 を保つ**) |
| 5 | 換算後の枠が必ず素材の中へ収まること (比率違いを何通りか) | `is_valid` が通る / 幅・高さが偶数 |
| 6 | `apply_to_all_media=false` + 別 ID 同寸法 | 従来の正規化 (v1.7.1 と同じ) |
| 7 | `split` の layout + 寸法違い | 2 枠とも換算され、各枠の縦横比が置き場所 (1080x640 / 1080x1280) の比と一致する |
| 8 | 素材の寸法が 0 / 素材が `None` | 従来の正規化 (壊れた `crop=` を作らない) |
| 9 | 警告が素材ごとに 1 回だけ記録されること | `assertLogs` で件数を固定 |
| 10 | `crop.config` の既定 (`apply_to_all_media`) | `True` |
| 11 | クリップをコピーして先頭へ貼った縦 Timeline (§1.8 ②) | 貼ったクリップにも枠が当たる |
| 12 | 別プロジェクトから貼って素材が 1 本増えた縦 Timeline (§1.8 ③) | 増えた素材のクリップにも枠が当たる |

### 6.2 回帰 (既存の振る舞いを固定する)

| # | 内容 | 期待 |
| --- | --- | --- |
| 11 | 素材 1 本の縦プロジェクト (既存の `test_crop_layout` / `test_vertical_render`) | フィルタ文字列が現在と 1 文字も変わらない |
| 12 | 切り抜き指定が無い Timeline | `crop_chain` は `None` (従来の正規化) |
| 13 | 横向きプロジェクト | 変化なし (黒帯の `pad` はそのまま) |
| 14 | プロキシ (scale=0.5) での `boxblur` 半径換算 | 既存の相似検査がそのまま通る |

### 6.3 実データでの確認 (実装後)

- `output/*.archive.timeline.json` 3 本から縦プロジェクトを組み、全クリップについて
  `crop=` が付くことを数える (§1.4 の表を「改善後」で埋める)。
  ※ 中間ファイル (`normalized.mp4`) は一時ファイルで既に消えているため、
  JSON から Timeline を組んでフィルタ文字列だけを数える形で測る。
- 実際の動画生成は 1 本短いもので行い、冒頭・中盤・終盤のフレームを目視で確認する。

---

## 7. 互換性

- **既存の縦プロジェクト (素材 1 本)**: ①を通るため出力は完全に同一。
- **`media_mode="rebase"` で作ったもの**: 素材が 1 本に畳まれているため同じく同一。
- **保存形式**: `source["crop"]` の書式 (`version: 1`) は変えない。読み書きの往復も変えない。
- **v1.7.1 の振る舞いへ戻す**: `apply_to_all_media=false` (枠を決めた素材だけに当てる)。
- **プロキシ**: 鍵の版を上げるため、開き直したときに作り直しが走る (1 回だけ)。

---

## 8. 残課題

| # | 内容 |
| --- | --- |
| 1 | **枠を決める絵は先頭クリップのもの** (§1.3 / Q5)。場面ごとに人物の位置が違うため、「この区間だけ枠を変えたい」という要望が来る余地がある。クリップ区間ごとの枠は構造 (`source["crop"]` 1 つ) から変える話になるので別要望とする |
| 2 | 作成ダイアログの警告 (Q4)。寸法が違う素材があるときだけ「一部のクリップは枠を換算して当てます」と積む |
| 3 | `vertical_project_dialog._resolve_media` は先頭素材しか見ないため、**寸法が混在していても画面では気づけない**。混在を検知して伝えるかは Q4 と同じ扱い |
| 4 | `pad` の黒は横向きプロジェクトでは依然そのまま。縦以外で余白が出る場面 (縦素材を横キャンバスへ) の既定をどうするかは本書の対象外 |

---

## 9. 要望との対応

| 要望 | 対応 |
| --- | --- |
| R1 一部の場面でトリミングが効かず上下が真っ黒 | 原因は `is_valid` の素材 ID 一致検査 (§1.1)。`media_mode="copy"` で素材が N 本になったことが引き金 (§1.6) |
| R2 トリミング・上下のぼかしを全クリップへ反映 | §3.1 (ID 検査を外す) + §3.2 (寸法が違う素材は枠を換算して当てる) + §3.3 (解決を 1 か所へ)。実データ 3 本は §3.1 だけで 100% 当たる (§1.4)。**全面をぼかしで収める逃げ道は作らない** (§2.3) |

---

## 10. 実装記録 (2026-10-31)

### 10.1 設計からの詰め 2 点

| # | 内容 | 理由 |
| --- | --- | --- |
| 1 | `apply_to_all_media=false` のときの**素材 ID の照合は `resolve_layout` の中で行う** | `is_valid` からは ID 検査を外した (§3.1) ため、逃げ道を「枠を決めた素材だけに当てる」(§3.8 の表) にするには、照合をもう 1 か所に置く必要があった。`is_valid` は寸法だけを見る関数のままにしてある |
| 2 | **枠の換算 (②) は「寸法が違う」ときだけ**に限る | 寸法が同じなのに噛み合わないのは、指定そのものが壊れている場合 (mode と枠数の不一致・枠が素材の外を指す) である。ここで枠を動かすと、壊れた指定を黙って別の絵へ読み替えることになるため、従来の正規化へ落とす判断 (resolve9 §4-4) をそのまま残した (`crop._source_size_differs`) |

### 10.2 実データでの結果 (§6.3 / §1.4 の「改善後」)

`output/*.archive.timeline.json` から V1 のクリップを組み、`compositor.crop_chain` が
枠を返す件数を数えた (中間ファイルは消えているため文字列だけを測る方式)。

| プロジェクト | 改善前 (尺) | 改善後 (尺) | クリップ | 素材 |
| --- | --- | --- | --- | --- |
| `vod_2877176496` | 9.5% | **100.0%** (832.0/832.0s) | 43/43 | 6 本 |
| `vod_2881981221` | 8.1% | **100.0%** (953.0/953.0s) | 73/73 | 7 本 |
| `vod_2884417212` | 5.2% | **100.0%** (569.7/569.7s) | 241/241 | 31 本 |

### 10.3 テスト

- 新規 `tests/test_crop_all_media.py` 12 項目すべて通過 (§6.1 の表どおり)。
- 既存の全テスト **2083 件 通過** (`python -m unittest discover -s tests`)。
  素材 1 本の縦プロジェクト (`test_vertical_render` / `test_crop_layout` /
  `test_base_zoom` の相似検査) はフィルタ文字列が従来と同一のまま通っている (§7)。

### 10.4 残しているもの

- 作成ダイアログでの通知 (§8 の 2・3) は未実装。黒帯が出なくなったため急がない。
- プロキシは `_KEY_VERSION = "proxy-tile-2"` により、**開き直したときに 1 回だけ**
  作り直しが走る (Q6 のとおり)。

---

## 11. クローズ (2026-10-31)

| 項目 | 内容 |
| --- | --- |
| 状態 | **クローズ (対応完了)** |
| 要望 R1 / R2 | ともに充足。トリミングと上下のぼかしが**全クリップ**へ当たる |
| 実装 | §5 の表どおり (`crop.py` / `compositor.py` / `proxy_render.py` / `preview_panel.py` / `vertical_builder.py` のコメント / `setting.json` / `settings_window.py`) |
| 設計からの詰め | §10.1 の 2 点 (逃げ道での ID 照合位置 / 換算は寸法違いのときだけ) |
| テスト | 新規 12 項目 + 既存全 2083 件 通過 (§10.3) |
| 実データ | 3 本とも 100% のクリップに枠が当たることを確認 (§10.2) |
| 実機確認 | 利用者が動画生成で確認し「完了しました」との報告を受領 |
| 持ち越し | §8 の残課題 (作成ダイアログでの通知・区間ごとの枠) は**別要望**として扱う |

以後、本書は記録として扱い、追加の変更は新しい request / resolve で起こす。
