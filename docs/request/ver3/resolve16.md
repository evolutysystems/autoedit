# resolve16（ver3） — アーカイブ採点へのストリームマーカー反映 設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request16.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「実装前に設計を行うこと」）。
* 調査は **2026-09-07 時点の実コード**を読んで行った。推測で書いた箇所は無く、
  要望文から一意に決められない論点はすべて §9「確認事項」へ挙げた
  （`docs/claude.md`「不明点がある場合は推測実装せず設計書へ記載すること」）。
* **§9 の Q1〜Q7 は 2026-09-07 に回答を得て確定した**（いずれも本書の判断どおり）。
  実装はこの確定内容に従う。未確定は Q8 のみで、これは異常系の扱いのため実装を止めない。
* **現状、ストリームマーカーを扱う処理はコード上に一切存在しない**（§2.1）。
  本要望は「既存処理の修正」ではなく「新しい入力源の追加」である。
* 既存の**採点（方式A）のロジックには手を入れない**。マーカーは
  採点結果の**後段**で「必ず残すセクション」として合流させる（§3-2）。
* 追加する設定値は `setting.json` の `archive.markers` と `archive.auth.scopes` へ置く
  （`docs/claude.md`「ハードコードは禁止」／§7）。
* 本書内の「resolve16」はすべて **ver3** の本書を指す。
  `docs/request/ver1/resolve16.md`（フォント登録）とは無関係である。

---

## 1. 要望（request16.md）と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **I1** | アーカイブ用の採点処理で**ストリームマーカーを使う** | 新機能 |
| **I2** | マーカーの位置から**前後 2 分を必ずセクションとして追加**する | 〃 |
| **I3** | 採点結果がマーカーの前後 2 分と**被るならマージ**する | 〃 |

要望文に無いが、実装に必ず要る論点:

| ID | 論点 | 理由 |
|---|---|---|
| **I4** | マーカーを**どこから取るか** | 現状どこにも取得処理が無い。Twitch Helix の `streams/markers` が唯一の一次情報（§2.5） |
| **I5** | マーカー取得には**追加スコープ**が要る | 現在のログインは**スコープ無し**で認可している（`twitch_auth.py:116`）。保存済みトークンでは 401/403 になる（§2.5） |
| **I6** | **ローカル動画モード**ではマーカーが取れない | 入力ソースは local / twitch の 2 つ（`archive_tab.py:63-64`）。local には VOD ID が無い |
| **I7** | マーカー由来セクションは **`top_n`（TOP10）の枠を消費するか** | 「必ず追加」である以上、上限で落ちてはならない（§3-4） |
| **I8** | マージの**規約をどれに合わせるか** | 既存に「接触も統合」という規約が 1 つある（`scoring._merge_time_sections` / `timeline_builder.plan_section_add`）。3 つ目を作ると結果が食い違う（§3-3） |
| **I9** | マーカー由来セクションの**スコア**をどう決めるか | 採点を通っていない。クリップバー・採点グラフが `score` を参照する（§2.2） |
| **I10** | 取得失敗時の**フォールバック** | コメント取得は失敗しても採点を続行する規約になっている（`archive_tab.py:153-155`）。マーカーだけ致命にはできない |
| **I11** | セクション数が**増えることの負荷** | 1 セクションごとに 切り出し→正規化→音量解析→文字起こし が走る（`clip_writer.prepare_one_clip`）。TOP10 + マーカー N 件になる（§2.7） |

---

## 2. 現状分析

### 2.1 ストリームマーカーを扱う処理は存在しない

`src/dist`（同梱ライブラリ）を除く全ファイルを `marker` / `マーカー` で検索した結果、
該当は **`src/gui/score_graph_widget.py:112` の `setMarkerSize(14.0)`（グラフの点の大きさ）だけ**だった。
`docs/` 配下にも「ストリームマーカー」の記述は無い。

Twitch API の呼び出しも 3 か所しか無く、いずれもマーカーとは無関係である。

| 呼び出し | 位置 | 用途 |
|---|---|---|
| `helix/users` | `twitch_auth.py:254` | ログインユーザー情報 |
| `helix/videos?id=` | `twitch_auth.py:263` | VOD 所有者の判定 |
| `helix/videos?user_id=` | `twitch_auth.py:277` | 自分の VOD 一覧 |

VOD 本体とコメントは Helix ではなく twitch-dl 経由である
（`twitch_source.download_vod` / `download_chat`）。

**つまり本要望は、取得・正規化・採点への合流をすべて新規に作る話である。**

### 2.2 採点からセクションが決まるまで

```
gui/archive_tab.ArchiveAnalyzeWorker.run()            archive_tab.py:98
  ├─ _prepare_source()  → (input_path, comments)      archive_tab.py:109
  └─ pipeline.analyze(input_path, settings, comments) pipeline.py:15
       ├─ features.extract_cells()                    features.py:26   ← 音量/無音のセル特徴
       ├─ comment_source.aggregate_into_cells()       pipeline.py:40   ← コメントをセルへ
       ├─ scoring.score_window() × 窓数               pipeline.py:53   ← curve（窓スコア列）
       └─ scoring.select_top_events(curve, top_n, …)  pipeline.py:57   ← clips（= セクション）
            ↓
gui/archive_tab._on_analyze_done()                    archive_tab.py:543
  └─ clip_writer.write_clips(input, settings, clips)  clip_writer.py:661
       └─ prepare_one_clip() × セクション数           clip_writer.py:409
```

`select_top_events` の戻りが**そのままセクション**であり、
`{"index","start","end","score","emotion","comment","use"}` を持つ（`scoring.py:160-173`）。
`start/end` は **VOD 先頭からの秒**である。

呼び出し元は `pipeline.py:57` の 1 か所だけで、テストからも呼ばれていない。
**この関数の入口を広げるのが、影響範囲が最も狭い変更点である。**

### 2.3 「重なりをマージする」規約は既に 1 つある（I8）

`scoring._merge_time_sections`（`scoring.py:114`）が、
開始昇順のセクション列に対して **接触（`next.start <= cur.end`）も重なりとみなして**統合し、
区間を和集合へ広げ、代表スコアは `total` 最大のものを採る。

編集画面のセクション追加（ver3 resolve13）も、わざわざ同じ規約に揃えている。

```python
# timeline_builder.py:524-528（plan_section_add）
# 接触も重なりとみなす (scoring._merge_time_sections と同規約)
if start <= entry_end + _SECTION_EPS and entry_start <= end + _SECTION_EPS:
```

**要望 I3 の「マージ」は、この既存規約をそのまま適用すれば満たせる。**
新しい重なり判定を書く必要は無い（§3-3）。

### 2.4 Twitch モードの取得経路（I4 / I10）

```python
# archive_tab.py:121-156（_fetch_twitch の骨格）
video_id = twitch_source.extract_video_id(self._job["url"])
if auth_cfg["owner_only"]:
    ... self._auth.is_own_video(video_id) で自分の VOD 以外を弾く
input_path = twitch_source.download_vod(...)      # VOD 全長を取得（start/end を渡していない）
try:
    chat_path = twitch_source.download_chat(...)
    comments = comment_source.load_comments(chat_path)
except TwitchError as e:
    _logger.warning("コメント取得に失敗 (コメント無しで継続): %s", e)
    comments = None                                # ← 取得失敗は致命にしない規約
```

ここで押さえるべき事実が 3 つある。

1. **`video_id` はこの関数の中に既にある**。マーカー取得に必要な引数は追加取得不要。
2. **VOD は全長を取得している**（`download_vod` に `start/end` を渡していない）。
   したがって **VOD の 0 秒 = 配信開始**であり、Helix の `position_seconds`
   （配信開始からの相対秒）と**同じ時間軸**になる。コメントの `contentOffsetSeconds`
   （`comment_source.py:53`）が既にこの前提で使われているのと同じである。
3. **取得失敗はワーニングで握りつぶして続行する**規約が既にある（I10 はこれに倣えばよい）。

### 2.5 Helix のマーカー API と、いま足りていないもの（I5）

* エンドポイント: `GET https://api.twitch.tv/helix/streams/markers?video_id=<id>`
* 必要スコープ: **`user:read:broadcast`**
* 取得できるのは**アクセストークンの持ち主が配信者である VOD のみ**
* 応答（抜粋）:

```json
{"data":[{"user_id":"123","videos":[{"video_id":"456","markers":[
  {"id":"106b8d…","created_at":"2026-09-01T20:10:03Z",
   "description":"神回","position_seconds":244}]}]}],
 "pagination":{"cursor":"eyJi…"}}
```

現状のログインは**スコープを 1 つも要求していない**。

```python
# twitch_auth.py:114-116
# 所有判定・自VOD一覧は公開情報のため追加スコープ不要 (空スコープでよい)。
self._scopes = scopes if scopes is not None else []
```

`archive_tab.py:414` の `TwitchAuth(...)` も `scopes` を渡していないため、
**既に保存されているトークンの `scope` は空文字**である
（トークンは `{"access_token","scope","token_type"}` で保存される / `twitch_auth.py:219-223`）。

**したがって、既存ログインのままではマーカーを取得できない。**
`_helix_get` は 401 だけを特別扱いしており（`twitch_auth.py:245-248`）、
スコープ不足の 403 は「HTTP 403」という素っ気ないメッセージになる。

### 2.6 設定の補完規則（新しいキーがどこまで届くか）

`_merge_with_defaults`（`settings_window.py:984`）は**セクション単位の浅いマージ**で、
`archive` の**直下キー**は「利用者の setting.json に無ければ既定が入る」。

```python
merged[section] = copy.deepcopy(defaults)          # archive の既定を丸ごと複製
for key, value in data[section].items():           # 利用者が持つキーだけ上書き
    merged[section][key] = value
```

一方、**既存サブセクションの中に足したキーは届かない**。
`archive.auth` は利用者ファイルに存在するため、利用者の `auth` 辞書がまるごと採用され、
そこに `scopes` は無い。入れ子の補完は `timeline` と `ui` にしか適用されていない
（`_fill_timeline_nested_defaults` / `_fill_ui_nested_defaults` / `settings_window.py:835-838`）。

| 追加先 | 既存 setting.json に反映されるか |
|---|---|
| `archive.markers`（**新設サブセクション**） | **される**（archive 直下キーのため既定が入る） |
| `archive.auth.scopes`（既存サブセクション内） | **されない**（コード側の既定にフォールバックするだけ） |

これは §3-5 の方式選定に直結する。

### 2.7 セクションが増えることのコスト（I11）

1 セクションあたり `prepare_one_clip`（`clip_writer.py:409`）が
**切り出し → ラウドネス正規化 → 音量解析 → 編集点検出 → 文字起こし**を行う。
現行の既定は `top_n = 10`（`setting.json` の `archive.scoring.top_n`）。

マーカーを N 個打った配信では、最大で **10 + N セクション**になる
（重なったぶんはマージで減る）。処理時間と出力尺は**セクション数に比例して伸びる**。

これは要望どおりの挙動（「必ず追加」）だが、利用者が予期しない待ち時間になり得るため、
§5.5 のログと §8 に明記する。

---

## 3. 方式選定

### 3-1. マーカーの取得元（I4）

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | **Helix `streams/markers`** を `TwitchAuth` から呼ぶ | マーカーの一次情報はここにしか無い。`TwitchAuth` は既に Helix GET・トークン管理・401 処理を持っており（`twitch_auth.py:232`）、メソッドを 1 つ足すだけで済む |
| B | twitch-dl から取る | **twitch-dl にマーカー取得コマンドは無い**（`download` / `chat` のみ）。実現できない |
| C | 利用者が JSON を用意する | 手作業が要る。ただし local モード救済としては有効なため §11 F2 に残す |

**A を採用する。**

### 3-2. マーカーをセクションにする位置（I2）

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | `scoring.select_top_events` に**強制区間 `forced` を渡し**、採点結果と合流させてから既存のマージを通す | 採点そのものは変えない（要望は「必ずセクションにする」であって「スコアを上げる」ではない）。マージ規約が 1 つのまま保たれる。後続（`clip_writer` 以降）は**セクションが増えたことに気付く必要すら無い** |
| B | セル特徴へマーカー加点し、採点で TOP 入りさせる | 「必ず」を満たせない。加点しても他区間の点次第で落ちる。要望の文言と一致しない |
| C | 編集画面で `plan_section_add` を使って後から足す | 追加のたびに単独で切り出し・文字起こしが走り（resolve13 §3-4）、初回構築のバッチ処理を活かせない。編集画面を開かない従来画面経路では機能しない |

**A を採用する。** `select_top_events` の呼び出し元は `pipeline.py:57` の 1 か所であり
（§2.2）、引数を増やしても既定 `None` なら現行と完全に同じ結果になる。

### 3-3. マージ規約（I3 / I8）

**既存の `_merge_time_sections` をそのまま使う。**
判定を新設しない。理由は §2.3 のとおりで、採点由来の統合・手動追加の統合・
マーカーの統合の 3 つが同じ規約で動くことが、結果の一貫性そのものだからである。

強制区間も採点区間と同じ `{"start","end","total","emotion","comment"}` の形に揃えて
同じリストへ入れ、開始昇順に並べてから 1 回だけマージする。

副作用として、**マーカー同士が 4 分以内に並んでいれば 1 セクションに統合される**。
これは「前後 2 分を必ず含む」を満たしたままであり、**§9 Q3 で統合してよいと確定した**。

### 3-4. `top_n` の扱い（I7）

**マーカー由来セクションは `top_n` の枠を消費しない。**
採点は現状どおり `top_n` 件を貪欲選択し、その**後で**強制区間を合流させる。

要望文の「採点の結果、もしストリームマーカーの前後 2 分と被るようであればマージ」は、
**採点は従来どおり走る**ことを前提にした書き方であり、この順序と一致する。

なお「マーカーに重なる窓を貪欲選択から除外し、`top_n` をマーカー以外に使い切る」案も
あり得る（マーカーと重なった窓が 1 枠を消費して総セクション数が減るのを避けられる）。
これは §9 Q1 で「**枠を消費してよい**」と確定したため**採用しない**。
採点側の貪欲選択（`scoring.py:140-147`）には手を入れない。

### 3-5. スコープの追加と既存トークンの扱い（I5）

`archive.auth.scopes` は既存サブセクション内のため、**既存の setting.json には現れない**（§2.6）。
選択肢は 2 つある。

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | コード側（`config.auth_config`）の既定を `["user:read:broadcast"]` とし、setting.json への出現は求めない | `config.py` の他のキーと同じ扱い（`config.py` は全キーにコード既定を持つ）。既存ファイルを触らず、追加の移行処理も要らない |
| B | `_fill_archive_nested_defaults` を新設して `archive` も入れ子補完する | `timeline`/`ui` と同じ仕組みで整合するが、`download.vod_quality_fallback` など**今回と無関係なキーまで**既存ファイルへ書き足す。要望の範囲を超える（§11 F3 に残す） |

**A を採用する。**

保存済みトークン（`scope: ""`）については:

| 案 | 判定 |
|---|---|
| **A（採用）** | スコープ不足を**検出してマーカー取得だけを飛ばす**。ログインは維持し、画面に「マーカー未許可」と出して再ログインを促す | 既存機能（所有判定・VOD 一覧）はスコープ無しで動く。勝手にログアウトさせない（`docs/claude.md`「既存実装を破壊しない」） |
| B | 起動時に強制ログアウトして再ログインさせる | 今まで動いていた機能まで一時的に使えなくなる。利用者から見ると「壊れた」 |

**A を採用する**（**§9 Q7 で「利用者に再ログインしてもらう運用でよい」と確定**）。
再ログイン後は `scope` に `user:read:broadcast` が入り、自動的に有効になる。

---

## 4. 設計方針

1. **採点（方式A）のロジックには触らない**。マーカーは採点の**後段**で合流させる（§3-2）。
2. **マージ規約は既存の 1 つだけ**を使う。新しい重なり判定を書かない（§3-3）。
3. **取得できなくても必ず完走する**。マーカー取得の失敗・未ログイン・スコープ不足・
   ローカルモードは、いずれも「マーカー無しで従来どおり採点」へ落ちる（§3-5 / I10）。
4. **取得（ネットワーク）と変換（純粋関数）を分ける**。`twitch_auth` が取り、
   `marker_source` が正規化する。`comment_source` と同じ構成にする。
5. 前後の秒数・有効無効は `setting.json` の `archive.markers` へ置く（ハードコード禁止）。
6. マーカー由来かどうかの印はデータに残すが、**この変更では UI を変えない**（§5.8 / §11 F1）。

---

## 5. 詳細設計

### 5.1 変更ファイル一覧

| ファイル | 変更内容 | 規模 |
|---|---|---|
| `src/archive/marker_source.py` | **新規**。マーカーの正規化と区間化（純粋関数） | 約 90 行 |
| `src/archive/twitch_auth.py` | `has_scope()` / `get_video_markers()` の追加、403 のメッセージ改善 | 約 45 行 |
| `src/archive/scoring.py` | `select_top_events` に `forced` を追加、`best_window_for_range` の新設、`_merge_time_sections` の印の引き継ぎ | 約 45 行 |
| `src/archive/pipeline.py` | `analyze` に `markers` を追加し強制区間を組む | 約 12 行 |
| `src/archive/config.py` | `marker_config()` の新設、`auth_config` へ `scopes` | 約 12 行 |
| `src/gui/archive_tab.py` | マーカー取得の呼び出し、`scopes` 引き渡し、スコープ不足の表示 | 約 25 行 |
| `src/archive/clip_writer.py` | 印を prepared へ引き継ぐ | 2 行 |
| `src/archive/timeline_builder.py` | 印を clip_meta へ引き継ぐ / `estimate_section_score` を薄いラッパへ | 約 6 行 |
| `src/archive/project_resume.py` | 印を復元する | 1 行 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["archive"]["markers"]` の追加 | 約 10 行 |
| `tests/test_archive_markers.py` | **新規**。正規化・区間化・マージのテスト | 約 130 行 |

**採点の中核（`features.py` / `scoring.score_window`）は変更しない。**

### 5.2 `src/archive/marker_source.py`（新規）

`comment_source` と同じ構成にする。ネットワークに触れず、単体で検証できる純粋関数だけを置く。

```python
# ストリームマーカーの正規化・セクション化 (ver3 resolve16)
# Helix `GET /helix/streams/markers` の応答、または内部正規化フォーマットのどちらでも
# 受け付け、採点結果へ必ず足す「強制セクション」の区間へ変換する。
# 取得(ネットワーク)は twitch_auth.get_video_markers が担い、ここは変換だけを行う
# (comment_source と同じ分担 / resolve17 §4.3.2 と同方針)。
#
# 内部正規化フォーマット:
#   [{"offset_sec": 1234.0, "description": "神回", "id": "106b8d…"}]
#   offset_sec = VOD 先頭からの相対秒。Helix の position_seconds と同じ基準
#   (VOD は全長を取得しているため 0 秒 = 配信開始 / resolve16 §2.4)。

# 極短の区間はセクションにしない (timeline_builder._MIN_SEGMENT_SEC と同値)
_MIN_SECTION_SEC = 0.01

# 説明が空のマーカーに使う既定ラベル (ログとセクションの印に使う)
_DEFAULT_LABEL = "マーカー"


# Helix 応答 (data[].videos[].markers[]) から marker 配列を取り出す
def _markers_of_helix(payload):
    out = []
    for user in (payload.get("data") or []):
        for video in (user.get("videos") or []):
            out.extend(video.get("markers") or [])
    return out


# 生データ (Helix 応答 dict / marker 配列 / 内部正規化配列) を内部正規化配列へ統一する。
# position_seconds を持てば Helix 形式、offset_sec を持てば内部形式として扱う。
# 壊れた要素は捨て、offset_sec 昇順にソートして返す。
def normalize_markers(raw):
    ...


# マーカーから「必ず残す区間」を作る (要望 I2: 前後 2 分)。
# duration_sec でクランプし、0 秒未満・動画尾を越える指定を丸める。
# ここでは重なりの統合を行わない。統合は scoring._merge_time_sections が
# 採点結果と一緒に 1 回だけ行う (規約を 2 つ持たないため / resolve16 §3-3)。
# 戻り値: [{"start","end","label"}] (start 昇順)
def to_sections(markers, before_sec, after_sec, duration_sec):
    ...
```

`to_sections` の規則（before/after = 120、duration = 7200 の場合）:

| 入力 | 出力 | 理由 |
|---|---|---|
| `offset_sec = 300` | `(180.0, 420.0)` | 通常 |
| `offset_sec = 30` | `(0.0, 150.0)` | 前側をクランプ |
| `offset_sec = 7190` | `(7070.0, 7200.0)` | 後側をクランプ |
| `offset_sec < 0` / `>= duration` | 捨てる | VOD 外 |
| クランプ後の尺 <= `_MIN_SECTION_SEC` | 捨てる | セクションにならない |

`label` は Helix の `description`（空なら `"マーカー"`）。ログとデータの印に使う（§5.8）。

### 5.3 `src/archive/twitch_auth.py`

```python
# ストリームマーカー取得に必要なスコープ (Helix streams/markers / ver3 resolve16)
MARKER_SCOPE = "user:read:broadcast"

# マーカー取得の 1 ページあたり件数 (Helix の上限は 100)
_MARKER_PAGE_SIZE = 100
# 暴走防止のページ数上限 (100 件 × 20 ページ = 2000 マーカー)
_MARKER_MAX_PAGES = 20


# 保存済みトークンが指定スコープを持つか (ver3 resolve16 §3-5)
# 旧バージョンでログイン済みのトークンは scope が空文字のため False になる。
# 呼び出し側はこれを見て、マーカー取得を飛ばすか再ログインを促すかを決める。
def has_scope(self, scope):
    granted = str((self._token or {}).get("scope", "") or "").split()
    return scope in granted


# VOD のストリームマーカーを取得する (ver3 resolve16 §3-1)
# 戻り値: [{"offset_sec","description","id"}] (marker_source.normalize_markers 済み)
# 前提: user:read:broadcast スコープ + 自分が配信者である VOD であること。
#   条件を満たさないときは Helix が 401/403 を返すため TwitchError になる。
#   呼び出し側は握りつぶしてマーカー無しで続行してよい (§5.6)。
def get_video_markers(self, video_id):
    from . import marker_source
    collected = []
    cursor = None
    for _ in range(_MARKER_MAX_PAGES):
        query = {"video_id": str(video_id), "first": _MARKER_PAGE_SIZE}
        if cursor:
            query["after"] = cursor
        payload = self._helix_get("streams/markers", query)
        collected.extend(marker_source.normalize_markers(payload))
        cursor = (payload.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    return sorted(collected, key=lambda m: m["offset_sec"])
```

あわせて `_helix_get` の例外整形へ 403 を足す（`twitch_auth.py:245` の 401 分岐の隣）。
現状は「Twitch API 呼び出しに失敗しました (HTTP 403)」としか出ず、
原因（スコープ不足）が分からないためである。

```python
if e.code == 403:
    raise TwitchError(
        "Twitch API の権限が不足しています。一度ログアウトして再ログインしてください。")
```

### 5.4 `src/archive/scoring.py`

#### (1) 区間に重なる最良の窓を返す（I9）

同じ規則が既に `timeline_builder.estimate_section_score`（`timeline_builder.py:552`）にある。
**規則を 2 か所に置かない**ため、規則本体を `scoring` へ移し、既存関数はラッパにする。

```python
# 区間 [start,end] に重なる窓のうち total が最大のものを返す (無ければ None)
# 追加セクションのスコア推定に使う (ver3 resolve13 §5.4 / resolve16 §5.4)。
def best_window_for_range(scored, start_sec, end_sec):
    best = None
    for w in (scored or []):
        if not w:
            continue
        if (float(w.get("start", 0.0)) < float(end_sec)
                and float(start_sec) < float(w.get("end", 0.0))):
            if best is None or float(w.get("total", 0.0)) > float(best.get("total", 0.0)):
                best = w
    return best


# 区間のスコア (重なる窓の total 最大 / 重なりが無ければ 0.0)
def score_for_range(scored, start_sec, end_sec):
    best = best_window_for_range(scored, start_sec, end_sec)
    return round(float(best.get("total", 0.0)), 1) if best else 0.0
```

`timeline_builder.estimate_section_score` は
`return scoring.score_for_range(curve, start_sec, end_sec)` へ差し替える
（`archive.scoring` は他モジュールを import しないため循環参照は起きない）。
既存の呼び出し元（`archive_timeline_dialog.py:399`）とテスト
（`tests/test_archive_section_add.py:270-279`）はそのまま通る。

#### (2) 強制区間の合流

```python
# 採点済み窓から重なりを避けて上位 top_n をイベントとして選び、
# その中で時間が連続 or 重なるものは 1 つのクリップ(セクション)へ統合して返す。
# forced : 必ず残す区間 [{"start","end","label"}] (ver3 resolve16 / 要望 I2)。
#   ストリームマーカーの前後 N 分がこれにあたる。top_n の枠は消費せず、採点で
#   選ばれた区間と合流させてから同じマージ規約に通す (resolve16 §3-3・§3-4)。
#   スコアは採点し直さず、重なる窓の最大値を代表として採る (§5.4)。
def select_top_events(scored, top_n, clip_pad_sec, duration, forced=None):
    ...
    # 採点由来（従来どおり。印は False で揃える）
    padded.append({..., "marker": False, "labels": []})

    # 強制区間を合流させる
    for f in (forced or []):
        best = best_window_for_range(scored, f["start"], f["end"])
        padded.append({
            "start": float(f["start"]), "end": float(f["end"]),
            "total": round(float(best["total"]), 1) if best else 0.0,
            "emotion": float(best["emotion"]) if best else 0.0,
            "comment": float(best["comment"]) if best else 0.0,
            "marker": True,
            "labels": [f["label"]] if f.get("label") else [],
        })
    padded.sort(key=lambda w: w["start"])

    merged = _merge_time_sections(padded)
```

`_merge_time_sections` は統合時に印を引き継ぐ（`scoring.py:114` の既存ループへ 3 行）。

```python
cur["marker"] = bool(cur.get("marker")) or bool(c.get("marker"))
for label in (c.get("labels") or []):
    if label not in cur["labels"]:
        cur["labels"].append(label)
```

**代表スコアの選び方（`total` 最大を採る）は変えない。**
スコア 0 のマーカー区間が高得点区間と統合されても、代表点は高いほうが残る。

戻りのセクション辞書へ 2 キーを足す（`scoring.py:166-173`）。

```python
clips.append({
    "index": idx, "start": m["start"], "end": m["end"],
    "score": m["total"], "emotion": m["emotion"], "comment": m["comment"],
    "use": True,
    # ストリームマーカー由来を含むか / そのマーカーの説明 (ver3 resolve16 §5.8)
    "marker": bool(m.get("marker")),
    "marker_labels": list(m.get("labels") or []),
})
```

`forced` が空（= `None` を渡した従来呼び出し）なら、結果は**現行と同一**である
（追加 2 キーを除く）。

### 5.5 `src/archive/pipeline.py`

```python
def analyze(input_path, settings, progress_cb=None, comments=None, markers=None):
```

```python
    # ストリームマーカーを「必ず残す区間」へ変換する (ver3 resolve16 / 要望 I2)。
    # 採点には混ぜない。採点結果と合流させ、重なりは select_top_events が統合する。
    forced = []
    marker_cfg = config.marker_config(settings)
    if markers and marker_cfg["enabled"] and duration > 0:
        forced = marker_source.to_sections(
            markers, marker_cfg["before_sec"], marker_cfg["after_sec"], duration)
        _logger.info("ストリームマーカー: %d件 → 強制セクション %d件 (前%.0fs/後%.0fs)",
                     len(markers), len(forced),
                     marker_cfg["before_sec"], marker_cfg["after_sec"])

    clips = scoring.select_top_events(
        curve, cfg["top_n"], cfg["clip_pad_sec"], duration, forced=forced)
    _logger.info("採点完了: 窓%d件 → %d セクション (うちマーカー由来 %d)",
                 len(curve), len(clips), sum(1 for c in clips if c.get("marker")))
```

セクションごとのログ（`pipeline.py:59-62`）にもマーカー印を足し、
利用者が「なぜこの区間が出たか」をログで追えるようにする。

> **注記（早期 return との関係）**: `pipeline.py:33` の `if not cells:` は
> `features.extract_cells` が `duration <= 0`（尺を読めない）のときにだけ通る
> （`features.py:31-33`。尺が読めれば `n_cells >= 1`）。
> この場合はクランプの基準となる尺が無く、マーカー区間も作れないため、
> **早期 return は現状のままとする**。「マーカーがあるのにセクションが 0 件」に
> なるのは、動画そのものが読めていないときだけである。

### 5.6 `src/gui/archive_tab.py`

`_prepare_source` の戻りを 3 要素へ広げる（呼び出し元は `run()` の 1 か所）。

```python
    def run(self):
        try:
            input_path, comments, markers = self._prepare_source()
            result = pipeline.analyze(input_path, self._settings,
                                      progress_cb=self._emit,
                                      comments=comments, markers=markers)
```

`_fetch_twitch` の末尾（コメント取得の直後）へマーカー取得を足す。

```python
        # ストリームマーカー取得 (ver3 resolve16)。失敗してもマーカー無しで続行する
        # (コメント取得と同じ規約 / resolve17 §5)。
        markers = None
        marker_cfg = config.marker_config(self._settings)
        if marker_cfg["enabled"]:
            if not (self._auth and self._auth.is_logged_in()):
                _logger.info("未ログインのためストリームマーカーは使いません")
            elif not self._auth.has_scope(twitch_auth.MARKER_SCOPE):
                _logger.warning(
                    "ストリームマーカーの権限がありません。一度ログアウトして"
                    "再ログインすると使えるようになります (%s)", twitch_auth.MARKER_SCOPE)
            else:
                self.progress.emit(0.0, "マーカー取得中…")
                try:
                    markers = self._auth.get_video_markers(video_id)
                    _logger.info("ストリームマーカー取得: %d件", len(markers))
                except TwitchError as e:
                    _logger.warning("マーカー取得に失敗 (マーカー無しで継続): %s", e)
                    markers = None
        return input_path, comments, markers
```

local モードは `return input_path, comments, None`（マーカー無し）。
**local の挙動は一切変わらない**（I6）。

`_init_auth`（`archive_tab.py:412`）でスコープを渡す。

```python
        self._auth = TwitchAuth(
            client_id=auth_cfg["client_id"],
            ...
            scopes=auth_cfg["scopes"],       # ver3 resolve16: マーカー取得に user:read:broadcast
        )
```

`_set_logged_in`（`archive_tab.py:484`）に一言添える（スコープ不足を可視化する）。

```python
        suffix = "" if self._auth.has_scope(twitch_auth.MARKER_SCOPE) else "（マーカー未許可）"
        self.login_status.setText(f"ログイン中: {me['display_name']}{suffix}")
```

ツールチップに「再ログインするとストリームマーカーを採点に使えます」を設定する。

### 5.7 `src/archive/config.py`

```python
# ストリームマーカーの設定を平坦化して返す (ver3 resolve16 §7)
def marker_config(settings):
    archive = settings.get("archive", {}) if isinstance(settings, dict) else {}
    mk = archive.get("markers", {})
    return {
        "enabled": bool(mk.get("enabled", True)),
        # マーカー位置の前後に必ず含める秒数 (要望: 前後 2 分)
        "before_sec": float(mk.get("before_sec", 120)),
        "after_sec": float(mk.get("after_sec", 120)),
    }
```

`auth_config` へ 1 行足す（既存ファイルには現れないためコード既定が本体 / §3-5）。

```python
        # マーカー取得に必要なスコープ。既存 setting.json には無いためコード既定が効く
        # (resolve16 §2.6・§3-5)。空リストにすると従来どおりスコープ無しで認可する。
        "scopes": list(auth.get("scopes", ["user:read:broadcast"])),
```

### 5.8 マーカー印の引き回し（データのみ／UI は変えない）

`marker` / `marker_labels` を保存対象まで運ぶ。**表示は変えない**（§11 F1）。

| ファイル | 変更 |
|---|---|
| `clip_writer.prepare_one_clip`（`clip_writer.py:459` の `entry`） | `"marker": bool(clip.get("marker")), "marker_labels": list(clip.get("marker_labels") or [])` |
| `timeline_builder.build_archive_timeline`（`timeline_builder.py:117` の `clip_meta`） | 同じ 2 キーを `entry.get(...)` から積む |
| `project_resume`（`project_resume.py:150` の prepared 復元） | 同じ 2 キーを `entry.get(...)` から戻す |

いずれも `.get()` の既定付きで読むため、**この 2 キーを持たない既存プロジェクト JSON も
そのまま開ける**（`project_resume` の他キーと同じ扱い）。

### 5.9 変わらないもの（意図的に触らない）

| 対象 | 理由 |
|---|---|
| `features.py`（セル特徴） | マーカーは特徴ではなく「必ず残す区間」。採点に混ぜない（§3-2 案B を不採用） |
| `scoring.score_window`（採点式） | 同上。重み・加点は一切変えない |
| `_merge_time_sections` の統合規約 | 既存規約をそのまま使う（§3-3）。印の引き継ぎ 3 行だけ足す |
| `clip_writer` 以降の書き出し経路 | セクションが増えるだけ。1 件ずつの処理は同一 |
| Timeline 編集画面・採点グラフ | 表示は現行のまま（§9 Q6 で確定 / §11 F1） |
| local モードの入力 UI | マーカーは Twitch モード限定（I6 / §9 Q4 で確定） |
| `top_n` の既定（10） | 変えない。マーカーは枠を消費しない（§3-4） |

---

## 6. 実装手順

1. `marker_source.py` を新規作成する（§5.2）。単体テストを先に通す。
2. `scoring.py` へ `best_window_for_range` / `score_for_range` を足し、
   `timeline_builder.estimate_section_score` をラッパへ差し替える（§5.4(1)）。
   ここまでで既存テストが通ることを確認する。
3. `select_top_events` へ `forced` を足す（§5.4(2)）。`forced=None` で現行同値を確認する。
4. `config.marker_config` / `auth_config.scopes` を足す（§5.7）。
5. `DEFAULT_SETTINGS["archive"]["markers"]` を足す（§7）。
6. `twitch_auth` へ `has_scope` / `get_video_markers` / 403 メッセージを足す（§5.3）。
7. `pipeline.analyze` へ `markers` を足す（§5.5）。
8. `archive_tab` の取得・引き渡し・表示（§5.6）。
9. 印の引き回し（§5.8）。
10. 実機で §10-1 を確認する。

2〜3 と 6〜8 は独立しているため、**マーカー取得が無くても（`markers` を手で与えれば）
採点側だけを先に検証できる**。

---

## 7. setting.json 定義（追加分）

```json
"archive": {
    "markers": {
        "enabled": true,
        "before_sec": 120,
        "after_sec": 120
    }
}
```

| キー | 既定 | 意味 |
|---|---|---|
| `archive.markers.enabled` | `true` | `false` でマーカーを取得も反映もしない（完全に従来動作） |
| `archive.markers.before_sec` | `120` | マーカー位置の**前**に必ず含める秒数（要望: 2 分 / §9 Q2 で確定） |
| `archive.markers.after_sec` | `120` | マーカー位置の**後**に必ず含める秒数（要望: 2 分 / §9 Q2 で確定） |

`archive.markers` は `archive` の直下キーのため、
**既存 setting.json にも次回起動時にそのまま書き足される**（§2.6 / `_has_new_keys` が
新規キーを検出して保存する）。移行処理は不要。

`archive.auth.scopes`（既定 `["user:read:broadcast"]`）は既存ファイルには現れず、
コード側の既定が効く（§3-5 案A）。明示したい場合は `archive.auth` へ手で書けば尊重される。

---

## 8. 互換性・非破壊の担保

| 観点 | 影響 |
|---|---|
| ローカル動画モード | **影響なし**。マーカーは常に `None`（I6） |
| マーカーが 0 件の VOD | **影響なし**。`forced` が空 → `select_top_events` は現行と同一結果 |
| `markers.enabled: false` | 取得もしない。完全に従来動作 |
| 未ログイン / スコープ不足 / API 失敗 | ワーニングを出してマーカー無しで続行（コメント取得と同じ規約） |
| 既存の Twitch ログイン | 維持される。ログアウトさせない（§3-5）。マーカーだけが使えない状態になる |
| 既存の保存済みプロジェクト | 開ける。`marker` / `marker_labels` は `.get()` 既定で補われる（§5.8） |
| `select_top_events` の既存呼び出し | `forced` は既定 `None`。呼び出し元は `pipeline.py:57` の 1 か所のみ |
| `estimate_section_score` の既存呼び出し | ラッパとして残すため無改造で通る（§5.4(1)） |
| **セクション数と処理時間** | **増える**。TOP10 + マーカー由来（重なりぶんは統合で相殺）。1 件あたり 切り出し→正規化→文字起こしが走る（§2.7）。要望どおりの挙動だが、待ち時間は伸びる |
| 出力尺 | 増える。マーカー 1 個 = 最大 4 分のセクション |
| マーカーが密集した配信 | 4 分以内の隣接マーカーは 1 セクションへ統合され、長い 1 本になる（§9 Q3） |
| VOD とマーカーの時間軸 | 一致する。VOD は全長取得で 0 秒 = 配信開始（§2.4）。配信断で VOD が分割された回は、後半のマーカーが後半 VOD 側に付くため**個別に処理する**（§9 Q5 で確定） |

---

## 9. 確認事項（Q1〜Q7 は 2026-09-07 に確定）

**回答はすべて本書の判断と一致した。§3〜§5 の設計に変更は無く、そのまま実装する。**

| # | 内容 | 本書の判断 | 回答 |
|---|---|---|---|
| **Q1** | マーカーに重なった採点区間は、`top_n`（TOP10）の枠を**消費してよい**か | 消費する（§3-4）。採点は現状のまま走り、その後で合流させる | **確定: 消費する。** 採点側の貪欲選択には手を入れない |
| **Q2** | 「前後 2 分」は**マーカー位置を中心に前 2 分・後 2 分（計 4 分）**という理解でよいか | その前提（`before_sec`/`after_sec` = 120 / 計 240 秒） | **確定: この認識でよい。** `markers.before_sec = markers.after_sec = 120`（§7） |
| **Q3** | マーカーが 4 分以内に連続して打たれている場合、**1 つの長いセクション**に統合されてよいか | 統合する（§3-3）。分けるには 2 つ目のマージ規約が要り、既存の一貫性を崩す | **確定: 統合する。** マージ規約は `_merge_time_sections` の 1 つだけを保つ |
| **Q4** | **ローカル動画モード**でもマーカーを使いたいか | 本書では対象外（Twitch モード限定） | **確定: 対象外。** local の挙動は一切変えない（§5.6 / I6）。将来必要なら §11 F2 |
| **Q5** | 配信断で VOD が複数に分かれた回の扱い | 各 VOD を個別に処理する現行のまま | **確定: 個別に処理する。** マーカーはその VOD に属するものだけを使う |
| **Q6** | マーカーの `description` を**テーマ欄（イントロカード）へ自動入力**するか | しない（要望外）。印としてデータには残す | **確定: しない。** `marker_labels` は保持のみ。自動入力は §11 F1 |
| **Q7** | スコープ追加のため利用者に**再ログイン**してもらう運用でよいか | よい。強制ログアウトはせず画面表示で促す | **確定: よい。** §3-5 案A（ログイン維持＋「（マーカー未許可）」表示）で実装する |
| **Q8** | `owner_only: false` に変更している環境 | 他人の VOD ではマーカー API が 401/403 を返すため、マーカー無しで続行する。エラーにはしない | 未回答（異常系の扱いのため本書の判断で実装し、実装を止めない） |

---

## 10. テスト計画

### 10-1. 実機確認

| # | 手順 | 期待 |
|---|---|---|
| 1 | マーカーを打った自分の VOD で、旧トークンのまま採点開始 | ログイン表示に「（マーカー未許可）」。ログに権限不足のワーニング。**採点は従来どおり完走** |
| 2 | 一度ログアウト → 再ログイン | Twitch の認可画面に配信情報の閲覧許可が出る。表示から「（マーカー未許可）」が消える |
| 3 | 同じ VOD で採点開始 | 進捗に「マーカー取得中…」。ログに `ストリームマーカー取得: N件` |
| 4 | 編集画面のセクション一覧 | マーカー位置の前後 2 分が**必ず**セクションとして存在する |
| 5 | 高得点区間と重なる位置にマーカーがある VOD | 2 つに割れず **1 セクション**になり、区間が和集合へ広がっている |
| 6 | 4 分以内に 2 つマーカーがある VOD | 1 セクションへ統合される（§9 Q3） |
| 7 | 配信開始 30 秒の位置にマーカーがある VOD | セクションが 0 秒から始まる（前側クランプ） |
| 8 | マーカーが 1 つも無い VOD | 従来と同じ TOP10。ログは `マーカー: 0件` |
| 9 | `markers.enabled: false` | マーカー取得の進捗もログも出ない。完全に従来動作 |
| 10 | ローカル動画モード | 何も変わらない |
| 11 | 保存したプロジェクトを開き直す | セクションが復元される（`marker` 印も含む） |

### 10-2. 追加テスト（`tests/test_archive_markers.py` / 新規）

ネットワークは使わず、Helix 応答を**固定 JSON で与えて**判定だけを見る
（`tests/test_twitch_quality.py` と同方針）。

| # | テスト | 検証内容 |
|---|---|---|
| 1 | `test_normalize_helix_payload` | `data[].videos[].markers[]` の入れ子から `offset_sec/description/id` を取り出す |
| 2 | `test_normalize_flat_list` | marker 配列を直接渡しても同じ結果になる |
| 3 | `test_normalize_internal_format` | `offset_sec` を持つ内部形式をそのまま受け付ける |
| 4 | `test_normalize_sorts_by_offset` | 順不同の入力が `offset_sec` 昇順で返る |
| 5 | `test_normalize_ignores_broken` | `position_seconds` 欠落・非数値・非 dict を捨てる |
| 6 | `test_sections_are_before_after` | `offset=300` → `(180, 420)`（前後 120 秒） |
| 7 | `test_sections_clamp_head_and_tail` | `offset=30` → `(0, 150)` / 尺 7200 で `offset=7190` → `(7070, 7200)` |
| 8 | `test_sections_drop_out_of_range` | 負値・尺超過のマーカーを捨てる |
| 9 | `test_forced_section_is_always_kept` | `top_n=1` でも強制区間がセクションとして残る（要望 I2） |
| 10 | `test_forced_merges_with_overlapping_score` | 重なる採点区間と **1 セクション**になり、区間が和集合、`score` は高いほうが残る（要望 I3） |
| 11 | `test_forced_merges_on_touch` | 端が接するだけでも統合される（`_merge_time_sections` と同規約 / §3-3） |
| 12 | `test_forced_adjacent_markers_merge` | 4 分以内の 2 マーカーが 1 セクションになる |
| 13 | `test_forced_does_not_consume_top_n` | `top_n=2` + マーカー 1 件 → 重ならなければ **3 セクション**（§3-4） |
| 14 | `test_forced_none_keeps_current_result` | `forced=None` の結果が従来の期待値と一致する（非破壊の担保） |
| 15 | `test_marker_flag_and_labels` | 統合後のセクションに `marker=True` と説明が残る（§5.8） |
| 16 | `test_score_for_range_matches_estimate` | `scoring.score_for_range` と `timeline_builder.estimate_section_score` が同値（§5.4(1)） |
| 17 | `test_has_scope` | `scope: ""` のトークンで `False` / `"user:read:broadcast"` で `True`（§5.3） |

### 10-3. 既存テストの回帰

```
python -m unittest discover -s tests
```

特に `tests/test_archive_section_add.py`（`estimate_section_score` をラッパへ差し替えるため）
と `tests/test_archive_prepare.py` / `tests/test_archive_timeline.py` が通ること。

---

## 11. 将来拡張（本書では実装しない）

| # | 内容 | 備考 |
|---|---|---|
| **F1** | マーカー由来セクションを**画面に見せる**（クリップバーへ印、採点グラフにマーカー線、`description` をテーマ欄の初期値に） | データ側（`marker` / `marker_labels`）は §5.8 で既に運んでいるため、表示を足すだけで済む（§9 Q6） |
| **F2** | **ローカルモードのマーカー JSON 指定**（chat json と同じくファイル選択欄を足す） | `marker_source.normalize_markers` が内部形式も受けるため、`load_markers` を 1 つ足せば繋がる（§9 Q4） |
| **F3** | `archive` セクションの**入れ子既定補完**（`_fill_archive_nested_defaults`） | `timeline`/`ui` と同じ仕組み。`archive.auth.scopes` のような入れ子キーが setting.json に現れるようになる（§3-5 案B） |
| **F4** | マーカーを**採点にも加点**する（区間内の窓へボーナス） | 「必ず追加」とは別軸の話。導入するなら `method_a` の重みと並べて設計し直す |
| **F5** | 前後秒数の**マーカーごとの上書き**（`description` に `[3m]` と書いたら 3 分にする等） | 運用が固まってから。現状は全マーカー一律 |
