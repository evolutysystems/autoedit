# ver9 resolve5 — 元 VOD を手放したあとでも「セクション追加」を使えるようにする (必要区間だけ twitch-dl で取り直す)

発端: 2026-10-26 のチャット報告「Timeline 画面でのセクション追加ができなくなっている (`vod_2910070011`)」
対象: `src/archive/` / `src/gui/timeline/archive_timeline_dialog.py` / `src/archive/twitch_source.py`
調査・検証日: 2026-10-26 (main `115e257` / v1.6.1)
実装日: 2026-10-26 (§10 に実装結果と、設計から変えた点を記録)
種別: **修正設計書**。§2 の検証は実測済み (実データ `vod_2910070011` と削除済み元 VOD で確認)

---

## 0. 先に結論

### 0.1 3 行で

1. **不具合ではない。** 元 VOD (`vod_2910070011.mp4`) が `keep_vod=false` の既定でゴミ箱へ送られ、
   `_section_add_available()` の `os.path.exists(self._source_path)` が偽になってボタンが無効化されていた
   (`src/gui/timeline/archive_timeline_dialog.py:437`)。容量を利用者へ負担させない方針どおりの動作である。
2. **しかし機能が死ぬのは設計の穴。** 元 VOD を手放す方針と「後から区間を足せる」機能が両立していない。
   解決は要望どおり **「追加したい区間だけを twitch-dl で取り直す」**。180 秒なら **約 52MB** で済む
   (元 VOD は 3.93GB)。容量方針を崩さずに機能を戻せる。
3. **方式は実測で確定済み。** twitch-dl の `--start/--end` が内部で行う切り詰めは**整数秒へ切り捨てられ、
   末尾が最大 1 秒欠ける**ため使えない。代わりに `--no-join` でセグメント (.ts) のまま取り、
   **プレイリストの EXTINF 累積から「取得素材の VOD 絶対原点」を自前で確定**する。
   この方式の位置誤差は実測 **±0.0001 秒** (VOD 前半・後半とも / §2.5)。

### 0.2 なぜ「正しい位置に挿入」が難しいのか、そこをどう解いたか

セクション追加は「元動画の `vod_start`〜`vod_end`」という**絶対秒**で区間を管理している
(`source.archive.clips[].vod_start`)。この絶対秒の基準は**全セグメントを連結した元 VOD mp4 の先頭**である。
一部だけ取り直した素材は先頭が VOD の途中なので、**その素材の 0 秒が VOD の何秒目なのか**を
誤差なく知らなければ、切り出し位置がそのままずれる。

本設計はこれを **EXTINF 累積** で解く。twitch-dl のキャッシュには取得範囲に関わらず
**VOD 全体のプレイリスト (`playlist.m3u8`)** が残り、セグメントの保存名は
**全体プレイリストでの並び順 (`{index:05d}.ts`)** である (`twitchdl/playlists.py:119-125`)。
よって「取得できた先頭セグメント番号 N」から `EXTINF[0..N-1]` を足せば原点が決まる。

---

## 1. 現象と原因 (報告への回答)

### 1.1 ボタンが無効になる条件

```python
# src/gui/timeline/archive_timeline_dialog.py:437
def _section_add_available(self):
    return bool(self._section_cfg["enabled"]      # true (setting.json 確認済み)
                and self._clip_workdir            # OK (再開経路でも渡っている)
                and self._clip_settings           # OK
                and self._source_path             # 'C:\...\archive_work\vod_2910070011.mp4'
                and os.path.exists(self._source_path))   # ← False
```

`_section_add_blocked_by_missing_source()` (:446) が真になり、:280-288 の経路で
**押せないボタン + 理由のツールチップ**が出る。ボタン自体は見えている。

### 1.2 元 VOD が消えた経路

| 項目 | 値 |
| --- | --- |
| プロジェクトが指す元 VOD | `C:\Users\evolu\AppData\Local\Stretheus\archive_work\vod_2910070011.mp4` |
| 実在 | **なし** (ゴミ箱に `$RMSH09C.mp4` として残存 / 元の場所も一致) |
| 原因 | `archive.download.keep_vod = false` (既定) → `vod_retention.sweep_unneeded()` (`src/archive/vod_retention.py:96`) |
| 発火箇所 | `archive_tab._sweep_old_sources()` (`src/gui/archive_tab.py:363`)。アーカイブタブを開くたびに走る |
| 判定 | プロジェクトが自己完結 (= `<プロジェクト>.media/` に素材が揃った) |

`<プロジェクト>.media/` を持つため**編集は続けられる**。足りないのは「まだ素材が無い区間を新しく作る」ことだけである。

### 1.3 併せて見つかった不整合

無効時のツールチップは「設定の『元動画を残す』を有効にしたうえで」と案内するが、
**`keep_vod` には設定画面の UI が無い** (`src/settings/settings_window.py:998` の既定値辞書にあるだけ)。
現状は `setting.json` を直接編集するしか有効化手段がない。§6.5 で扱う。

---

## 2. 検証結果 (すべて実測)

検証対象: `video_id=2910070011` (公開 / 全長 8991 秒 / 825 セグメント)、
比較基準: ゴミ箱に残っていた削除前の元 VOD (3.93GB / 1080p)。

### 2.1 座標系の確認 — 元 VOD は「全長取得」だった

`source.duration_sec = 1808.165` は VOD 全長ではなく**完成 Timeline の尺** (9 クリップ合計 ≒ 1800 秒)。
`clips[9].vod_end = 8055` と `curve` 197 窓が VOD 全長 8991 秒と整合する。
**よって `vod_start` は Twitch VOD 上の絶対秒とそのまま対応する** (取得時の範囲オフセットは存在しない)。

> 注: GUI 経路は `download_vod` に `start/end` を渡していない (`src/gui/archive_tab.py:182`)。
> 現状 `download_vod` の `start/end` 引数はどこからも使われていない。

### 2.2 twitch-dl の `--start/--end` は使えない

| 事実 | 根拠 |
| --- | --- |
| 指定は **hh:mm[:ss] の整数秒のみ** | `validate_time` が `int(p)` でパース (`twitchdl/cli.py:60-77`)。小数秒は `BadParameter` |
| 内部の切り詰めは **ffmpeg の `-ss`/`-t` に整数秒で渡される** | `_join_vods` が `utils.format_time(crop)` を使う (`twitchdl/commands/download.py:97-106`) |
| その `format_time` が **`int()` で切り捨て** | `twitchdl/utils.py:57-67` |
| 実測: 30 秒を要求して **29.05 秒**しか得られない | 実行ログ `-ss 00:06 ... -t 00:29` / `time=00:00:29.05` |

→ **末尾が最大 1 秒欠ける。** 区間の末尾が切れるのは「正しい位置に挿入」と相容れないため、
この切り詰めには頼らない。

### 2.3 セグメントの命名と位置が自前で確定できる

```
#EXT-X-TWITCH-TOTAL-SECS:8991.436        ← キャッシュの playlist.m3u8 (VOD 全体)
#EXTINF:11.997,
20.ts                                     ← プレイリスト内の URI
```

- 保存名は URI 名ではなく **`{index:05d}.ts`** (= 全体プレイリストの並び順 / `twitchdl/playlists.py:119-125`)。
  実測で `20.ts` は `00020.ts` として保存された。
- `--start 0:03:45`(=225 秒) の指定で取得されたのは `00020.ts`〜`00023.ts`。
- EXTINF 累積で `index=20` の開始は **218.993 秒**。twitch-dl が算出した `crop_start = 6.007`
  (= 225 − 218.993) と**完全に一致**した。

### 2.4 EXTINF 累積は元 VOD の時間軸と一致する

| 測り方 | 値 |
| --- | --- |
| EXTINF 累積 (160p プレイリスト) | 8991.436 秒 |
| EXTINF 累積 (chunked プレイリスト) | 8991.436 秒 (**画質間で完全一致**) |
| 元 VOD mp4 の実測 duration | 8991.468 秒 |
| Twitch メタデータ `lengthSeconds` | 8991 |

全長での乖離は **0.032 秒 (3 万分の 1)**。画質を変えてもセグメント境界は同一なので、
記録画質が使えない場合でも位置計算は壊れない。

### 2.5 位置の一致を音声相関で厳密に確認 (本設計の根拠)

「再取得素材から相対位置で切り出したもの」と「元 VOD から絶対位置で切り出したもの」を、
16kHz モノラルへ落として相互相関にかけた結果:

| 位置 | 遅延 | 相関係数 |
| --- | --- | --- |
| VOD 前半 225 秒 | **−0.0001 秒** | 0.999 |
| VOD 後半 7875 秒 | **+0.0001 秒** | 0.992 |

**VOD の後半でも累積誤差は出ない。** 方式は成立する。

> 検証中の注意として記録する: フレームの MD5 比較は**当てにならない**。
> `-ss` + `-c copy` はキーフレーム境界へスナップし、`.ts` 単体と concat 済み mp4 では
> デコード経路の違いで先頭フレームの内容も変わる。実際に後半の MD5 は不一致だったが、
> 音声相関では一致していた。**位置の検証は音声相関で行うこと。**

### 2.6 容量と所要

| 項目 | 実測 |
| --- | --- |
| 30 秒 (1080p source) | 8.7MB / 4 セグメント / 数秒 |
| 180 秒の見込み | **約 52MB** |
| 元 VOD 全長 | 3.93GB |
| `--no-join` の挙動 | 「Skipping joining files...」でセグメントを残し正常終了 |
| セグメント連結 (`-f concat -c copy`) | 成功。`start_time=0` / 原点 = 先頭セグメント |

**要望の価値はここ。** 1 区間 52MB なら、元 VOD を抱えずに追加が成立する。

---

## 3. 設計方針

### 3.1 やること

> **元 VOD の代わりに「必要な区間だけの素材」を作り、その素材の VOD 絶対原点を添えて、
> 既存の準備処理 (`prepare_one_clip`) へそのまま流す。**

既存の処理を書き換えるのではなく、**切り出しの起点を 1 つの引数で平行移動させる**だけにする。
準備工程 (正規化・音量解析・編集点検出・文字起こし) と Timeline へ入れる処理は一切変えない。

### 3.2 やらないこと

| 項目 | 理由 |
| --- | --- |
| `cut_region` のフレーム精度の作り直し | 利用者判断で後回しと決まっている既知課題。本件と独立 |
| 元 VOD 全長の自動再取得 | 容量を負担させない方針に反する。本設計の目的そのものを損なう |
| 画質の勝手な切り替え | 既存素材と寸法が割れる (§5.4)。確認を取る |
| `keep_vod` 既定値の変更 | 既定で 3.93GB を抱える挙動へ戻ることになる |

---

## 4. 実装設計

### 4.1 新規モジュール `src/archive/vod_range.py`

VOD の一部区間を「切り出しに使える 1 本の mp4 + その絶対原点」として用意する。
twitch-dl の呼び出しそのものは `twitch_source` に残し、本モジュールは**位置計算と連結・検証**に徹する。

```python
# 指定区間を含む素材を用意する。
# 戻り値:
#   {"path"       : 連結済み mp4 の絶対パス
#    "origin_sec" : path の 0 秒が VOD の何秒目か (EXTINF 累積で確定)
#    "end_sec"    : origin_sec + 実測 duration
#    "quality"    : 実際に使った group_id ("chunked" など)
#    "bytes"      : 取得量}
def fetch_range(video_id, start_sec, end_sec, workdir, settings,
                quality="source", pad_sec=3.0, progress_cb=None, cancel_cb=None):
```

処理順:

1. **要求範囲をセグメント単位へ広げる。** `--start = floor(start_sec - pad)` / `--end = ceil(end_sec + pad)`
   を `hh:mm:ss` で渡す。`pad_sec` は後述 (§5.3)。
2. **空き容量を確かめる。** `disk.ensure_free` を使う。必要量は
   `区間秒 × refetch.min_free_mb_per_min / 60 × 2` (セグメント + 連結ぶん)。
3. `twitch_source.download_vod_range()` を呼ぶ (§4.2)。`--keep --no-join` を付け、
   **twitch-dl には切り詰めをさせない**。
4. キャッシュの `playlist.m3u8` (VOD 全体) を読み、EXTINF 累積表 `{index: start_sec}` を作る。
5. キャッシュの `*.ts` を番号順に並べ、**先頭番号 N** から `origin_sec = 累積[N]` を得る。
6. **検証** (どれかが崩れたら `TwitchError` にして Timeline は無変更で中止):
   - セグメント番号が**連番**であること (欠番 = 取得が不完全)
   - `origin_sec <= start_sec` かつ `end_sec <= origin_sec + 実測duration`
     (= 要求区間を本当に覆っているか)
   - 累積表の合計が `EXT-X-TWITCH-TOTAL-SECS` と整合すること
7. `ffmpeg -f concat -safe 0 -c copy` で 1 本の mp4 へ連結し、`ffprobe` で duration を実測。
8. 戻り値を返す。

ファイルはすべて `workdir` (= 画面の `clip_workdir` / `TemporaryDirectory`) 配下に置く。
**画面を閉じれば消える**ので、容量を抱え続けることはない。

### 4.2 `src/archive/twitch_source.py` への追加

既存 `download_vod` は触らない (全長取得の意味を変えない)。並びに低レベル関数を足す。

```python
# 指定範囲のセグメントをキャッシュへ落とすだけ行う (連結も切り詰めもしない)。
# 戻り値: {"segment_dir", "segments"(番号順のファイル名), "playlist_path"}
def download_vod_range(video_id, cache_root, start_hms, end_hms,
                       twitch_dl_path="twitch-dl", quality="source",
                       auth_token="", progress_cb=None, ffmpeg_dir=None,
                       quality_fallback=False, keep_cache_on_error=False):
```

- argv は `download <id> --quality <q> --start <hms> --end <hms> --cache-dir <root>/{id}/{quality}
  --keep --no-join --overwrite`。`--output` は使われない (連結しないため) が、
  twitch-dl が要求するので一時名を渡す。
- 既存の `_resolve_twitchdl` / `_twitchdl_env` / `_run` / `_cleanup_cache` をそのまま使う。
  **同梱 twitch-dl の解決と ffmpeg の PATH 注入を二重に持たない。**
- `quality_fallback` の既定は **False**。既存素材と画質が割れるのを黙って通さない (§5.4)。
- **出力ファイル名の衝突に注意。** `download_vod` の出力は `vod_{id}.mp4` 固定で、
  部分取得をこの名前で置くと「取得済みの全長 VOD」として再利用されてしまう
  (`download_vod:302` の `_is_usable_video` 判定は長さを見ない)。
  本経路は `work_dir` ではなく `workdir` 配下の別名のみを使い、**`archive_work` に部分 VOD を置かない**。

### 4.3 プロジェクトへの記録追加 (これが無いと再取得できない)

現状 `source.archive` は `vod_path / clips / media / curve` のみで、**`video_id` も画質も記録されていない**。
`source.archive.vod_source` を足す。

```json
"vod_source": {
  "provider": "twitch",
  "video_id": "2910070011",
  "quality": "chunked",
  "vod_duration_sec": 8991.468,
  "fetched_at": "2026-10-25T02:06:29Z"
}
```

| 欄 | 用途 |
| --- | --- |
| `video_id` | 再取得の対象。**これが無ければ再取得は不可** |
| `quality` | 同じ画質で取り直す (寸法と見た目を揃える) |
| `vod_duration_sec` | 追加ダイアログの上限 (元 VOD を ffprobe できないため) |
| `fetched_at` | ~~公開期限切れの説明に使う~~ → **実装では記録しない**。
プロジェクトの `created_at` で足りるため、取得時に info を 1 回余計に叩く理由がない |

- 記録の経路: `archive_tab._fetch_twitch` が持つ `video_id` と `resolve_quality` の結果を
  採点パイプラインへ運び、`archive_timeline.build_archive_timeline(..., vod_source=...)` で書く。
- **後方互換**: `vod_source` が無い既存プロジェクトは、`vod_path` のファイル名
  `vod_<digits>.mp4` から `video_id` を推測する (`twitch_source.extract_video_id` の裸 ID 規則を再利用)。
  推測できたら再取得を許し、できなければ従来どおりグレーアウトする。
  画質は設定の `vod_format` (既定 `source`) を使う。
  → `vod_2910070011` は**この後方互換で救われる** (ファイル名から ID が取れる)。

### 4.4 音量基準の継承 (併せて直す)

`cut_db_scope` / `loudness_scope` は既定で **`"vod"`** (VOD 全体で測った値を全クリップへ適用)。
ところが `_SectionPrepareWorker` は `analyze_vod` を通さず `vod_analysis` を渡していないため
(`archive_timeline_dialog.py:893-895`)、**追加セクションだけクリップ単位の測定値になる**。
元 VOD が無ければ VOD 全体はそもそも測れないので、**記録して再利用する**のが唯一の筋になる。

```json
"vod_analysis": {"cut_db": -34, "measured": {"input_i": ..., "input_tp": ..., ...}}
```

- 書く場所: 採点時に `clip_writer.analyze_vod` が返した値 (`src/archive/clip_writer.py:418`) を
  `source.archive` へ保存。
- 使う場所: `_SectionPrepareWorker` が `prepare_one_clip(..., vod_analysis=記録値)` を渡す。
- 記録が無い既存プロジェクトは従来どおり (クリップ単位)。**既存の挙動は変わらない。**

これは元 VOD があるときのセクション追加にも効く既存不具合の修正である。

### 4.5 `prepare_one_clip` に切り出し起点を足す

```python
# src/archive/clip_writer.py:589
def prepare_one_clip(input_path, settings, clip_settings, clip, ffmpeg_cfg, workdir,
                     use_timeline=False, progress_cb=None, saved_db=None,
                     vod_analysis=None, source_origin_sec=0.0):   # ← 追加
    ...
    cut_region(input_path,
               clip["start"] - source_origin_sec,     # ← 素材内の相対秒へ平行移動
               clip["end"] - source_origin_sec, raw, ffmpeg_cfg)
```

- `entry["start"] / entry["end"]` は **VOD 絶対秒のまま**にする。
  これらは `vod_start / vod_end` として Timeline・素材の控え・復元に使われる唯一のキーであり、
  相対秒を書くと復元 (`make_media_recover` → `cut_region`) が壊れる。
- 既定 `0.0` なので、`_prepare_clips` (初回構築) の呼び出しは**一切変わらない**。
- イントロカードの素は「クリップ開始の `buffer_sec` 手前」から作るため、
  `export_intro_source` にも同じ `source_origin_sec` を足して平行移動させる。

**ここには設計時に見落としていた問題がある。** 取り直した区間の素材は
`clip_workdir` (一時ディレクトリ) にあり、**画面を閉じると消える**。
ところがイントロの素を素材フォルダへ残すのは**保存時** (`_keep_extra_beside_project`) で、
その時点では元 VOD も取り直した素材も無い。つまり素を作る機会が一度も無かった。

→ 実装では **準備の最中に控える**ことにした:
1. `_SectionPrepareWorker` が区間を用意した直後、取り直した素材から
   イントロの素を `clip_workdir` へ書き出し、準備データへ `intro_source` として持たせる。
2. 保存時は、元 VOD があれば従来どおり切り出し、無ければこの控えを素材フォルダへ写す。
   突き合わせは `vod_start` で行う (素材 1 本と 1 対 1 で対応する値のため)。
3. 統合で足した差分の素材は `self._prepared` へは入らない (代表へ畳まれる) ため、
   `_prepared_by_index` を走査する。

### 4.6 セクション追加の有効条件と UI

```python
def _section_add_available(self):
    if not self._section_cfg["enabled"] or not (self._clip_workdir and self._clip_settings):
        return False
    return self._has_local_vod() or self._refetch_available()
```

`_refetch_available()`: `refetch.enabled` かつ `video_id` が判明 (記録 or ファイル名推測)。

> **設計から変えた点**: 所有判定 (`archive.auth.owner_only`) は条件に入れないことにした。
> 取り直しは「採点のときに所有判定を通した**同じ VOD** の一部をもう一度取る」操作で、
> 入口で一度確かめてある。加えて編集画面は `auth` を持たないため、条件に入れると
> 画面まで認証を引き回す必要があり、得るものより配線が増える。
> 公開 VOD は認証なしで取得できることを実測で確認した (§2)。
> sub-only などで取れない場合は取得を試みた時点で理由が出る (§5.2)。

| 状態 | ボタン | 文言 |
| --- | --- | --- |
| 元 VOD あり | 有効 | 従来どおり |
| 元 VOD なし / 再取得可 | **有効** | 「元動画は手元にありません。追加する区間だけを Twitch から取得します (180 秒で約 50MB)」 |
| 元 VOD なし / `video_id` 不明 | 無効 | 「元動画が無く、取得元も分からないため追加できません」 |
| 元 VOD なし / 設定で無効 | 無効 | 現行の文言 (設定の案内) |

(「未ログイン」の行は上記のとおり条件から外したため無くなった)

**追加ダイアログの上限** (`_vod_duration_sec`) の解決順を変える:
① 元 VOD を ffprobe → ② 記録 `vod_duration_sec` → ③ `twitch-dl info` の `lengthSeconds`
(ネットワーク 1 回・失敗は無視) → ④ 0 (上限チェックなし / 従来の退避)。

### 4.7 準備ワーカーの流れ

`_SectionPrepareWorker.run()` の各区間の先頭に「素材の用意」を挿む。

```
区間ごとに:
  元VODがある → src = 元VOD,  origin = 0.0            (従来と完全に同じ)
  元VODがない → r = vod_range.fetch_range(video_id, start, end, workdir, …)
                src = r["path"],  origin = r["origin_sec"]
  prepare_one_clip(src, …, job, …, vod_analysis=記録値, source_origin_sec=origin)
```

- 進捗は既存の 4 工程の前に「元動画の区間を取得中…」を足す。
  `_run` の `progress_cb` が twitch-dl の出力を流すので、既存の仕組みに乗る。
- **キャンセル**: 既存規約は「工程の切れ目でのみ効く」。取得工程は外部プロセスなので、
  `_run` に中断を渡せるようにし、ダウンロード中もキャンセルで `terminate()` できるようにする
  (数十 MB とはいえ待たせる工程であり、ここだけは途中で止められるべき)。
  中断時はキャッシュを片づける (`_cleanup_cache` と同じ扱い)。
- 失敗・中断時に Timeline を触らないのは現行どおり。`AddArchiveSection` は
  素材が揃ってから初めて実行される (`archive_timeline_dialog.py:612` 以降)。

### 4.8 設定

```json
"archive": {
  "section_add": {
    "refetch": {
      "enabled": true,
      "pad_sec": 3.0,
      "min_free_mb_per_min": 60,
      "allow_quality_fallback": false
    }
  }
}
```

`archive_config.section_add_config()` (`src/archive/config.py:277`) の戻り値へ平坦化して足す。
既存キーの意味は変えない。

---

## 5. 判断が要る点と、その決め方

### 5.1 公開期限切れ・削除された VOD

Twitch の VOD は保存期限 (通常 14 日 / サブスク対象 60 日) があり、配信者が消すこともある。
`twitch-dl info` が失敗した時点で切り分け、**取得を試みる前に**理由を出す。

> 「この配信アーカイブは Twitch 上に見つかりません (公開期限切れ、または削除された可能性があります)。
> 　追加する区間の素材を用意できないため、この操作はできません。
> 　編集中のセクションはプロジェクトに揃っているので、作業は続けられます。」

### 5.2 sub-only / 限定公開

現状 GUI は `--auth-token` を**あえて渡していない** (`archive_tab.py:177-178` の注記:
twitch-dl の `--auth-token` は Web クッキー系で Helix トークンとは別物)。
本経路も同じ制約を踏襲し、失敗時に上記と同じ案内を出す。
**ここで新しく認証方式を増やさない** (resolve17 の判断を動かさない)。

### 5.3 `pad_sec` の既定値

取得はセグメント単位 (約 12 秒) なので、要求区間の前後には元々余裕が付く。
`pad_sec` は「要求区間がセグメント境界と一致したとき」に備えた保険と、
イントロカードの素 (`intro_card.buffer_sec` 既定 3 秒) ぶんの確保を兼ねる。
→ **既定 3.0 秒**、実効値は `max(pad_sec, intro_card.buffer_sec)` とする。

### 5.4 記録画質が使えないとき

既存素材が 1080p で、追加だけ 720p になると `output_profile.resolve_output_profile` が
別プロファイルを返し、**Timeline 上で寸法が割れる**。
よって `allow_quality_fallback` の既定は **false** とし、使えないときは黙って落とさず確認を取る:

> 「記録された画質 (1080p) が、この VOD では利用できなくなっています。
> 　利用できる最高画質は 720p です。追加するセクションだけ画質が変わりますが、続けますか？」

§2.4 のとおり**セグメント境界は画質間で同一**なので、画質が変わっても位置計算は壊れない。

### 5.5 `keep_vod` の既定は変えない

再取得が入れば、元 VOD を抱える理由はさらに薄くなる。既定 `false` のままが正しい。
ただし §1.3 の不整合 (UI が無いのにツールチップが設定を案内する) は直す (§6.5)。

---

## 6. 変更ファイルと段階

| # | フェーズ | 変更 | ネットワーク |
| --- | --- | --- | --- |
| 1 | 取得の土台 | **新規** `src/archive/vod_range.py` / `twitch_source.download_vod_range` | 必要 |
| 2 | 記録 | `archive/timeline_builder.py` (`vod_source`・`vod_analysis` の書き込み) / `gui/archive_tab.py` (video_id・画質を運ぶ) / `settings/settings_window.py` (既定値) / マイグレーション | 不要 |
| 3 | 起点の平行移動 | `archive/clip_writer.py` (`source_origin_sec`) | 不要 |
| 4 | 画面の統合 | `gui/timeline/archive_timeline_dialog.py` (`_section_add_available` / `_vod_duration_sec` / `_SectionPrepareWorker`) / `gui/timeline/section_add_dialog.py` (上限と案内) | 必要 |
| 5 | 後始末 | 設定画面に「元動画を残す」を出す / `i18n/catalog_en.py` に新規文言 | 不要 |

フェーズ 1〜3 は画面の挙動を変えない (既定 `0.0` と記録の追加のみ) ため、
**途中で止めても現行の動作は壊れない**。

### 6.5 設定画面の「元動画を残す」(§1.3 の後始末)

`keep_vod` のチェックボックスを「取得データ」の欄へ出す。文言は実態に合わせる:

> ☐ 元動画 (VOD) を残す
> 　外すと、編集に必要な素材が揃った時点で元動画をゴミ箱へ送ります (1 本 10〜25GB の節約)。
> 　**セクション追加は、必要な区間だけを取得し直して使えます。**

---

## 7. テスト計画

### 7.1 ネットワーク不要なユニットテスト (新規 `tests/test_vod_range.py` / 32 件)

| 対象 | 内容 |
| --- | --- |
| EXTINF 累積 | fixture の `playlist.m3u8` から `{index: start}` を作り、既知値と一致するか |
| 原点の確定 | 先頭セグメント `00020.ts` → `218.993` (§2.3 の実測値をそのまま期待値にする) |
| 欠番検出 | `00020,00022` を置いて `TwitchError` になるか |
| 範囲の被覆検証 | 要求区間が素材外のとき失敗するか |
| ゼロ埋め照合 | プレイリストの `20.ts` と保存名 `00020.ts` が対応すること |
| 画質間の同一性 | 160p と chunked の fixture で累積が一致すること |

### 7.2 既存処理への影響 (新規 `tests/test_section_add_offset.py` / 21 件)

| 対象 | 内容 |
| --- | --- |
| `prepare_one_clip` | `cut_region` を差し替え、`source_origin_sec=218.993` で `start-origin` が渡ること |
| 同 | `entry["start"]/["end"]` は**絶対秒のまま**であること |
| 既定値 | `source_origin_sec` 省略時に従来と同一の引数で呼ばれること |
| `video_id` の後方互換 | `vod_2910070011.mp4` → `"2910070011"` / 非該当名 → 不可 |
| ボタンの可否 | 記録・ファイル名・ログイン状態の組み合わせで §4.6 の表どおりになること |
| `vod_analysis` の継承 | 記録があれば `prepare_one_clip` へ渡り、無ければ `None` のままであること |

### 7.3 手で確かめる (ネットワークあり)

1. `vod_2910070011` のプロジェクトを開く → **セクション追加ボタンが有効**であること。
2. 既存セクションの外 (例 `6000`〜`6180` 秒) を指定して追加 → 約 50MB の取得で完了すること。
3. 追加されたセクションの映像が**指定位置と一致**すること。
   検証は §2.5 の方法で行う (**音声相関。フレーム MD5 は使わない**)。
4. 追加セクションの音量が既存セクションと揃っていること (§4.4)。
5. 取得中のキャンセルで Timeline が無傷・履歴が汚れないこと。
6. 機内モード等でネットワークを切って追加 → 理由が出て Timeline 無変更で終わること。

---

## 8. 精度と制約 (正直に書いておく)

| 項目 | 値・条件 |
| --- | --- |
| 位置精度 | **±0.0001 秒** (§2.5 実測 / VOD 前半・後半とも) |
| 全長の座標系の乖離 | 0.032 秒 / 8991 秒 (§2.4) |
| 取得量 | 約 0.29MB/秒 (1080p 実測) → 180 秒で約 52MB |
| 取得単位 | セグメント (約 12 秒)。要求区間より前後に最大 12 秒ぶん多く落ちる |
| 残る既知課題 | `cut_region` のフレーム精度 (`-ss` + `-c copy` のキーフレームスナップ)。**本件とは独立**で、元 VOD があるときも同じように出る。利用者判断で後回し |
| 依存 | 同梱 twitch-dl 3.3.1 のキャッシュ構成 (`playlist.m3u8` が残る / 保存名が `{index:05d}`) に依存する。**バージョンを上げるときは §7.1 の fixture テストで必ず確認すること** |

---

## 10. 実装結果 (2026-10-26)

### 10.1 変更したファイル

| ファイル | 内容 |
| --- | --- |
| `src/archive/vod_range.py` | **新規**。位置の確定 (EXTINF 累積)・連結・検証・`fetch_range` |
| `src/archive/twitch_source.py` | `download_vod_range` / `video_length_sec` / `list_segments` / `segment_index` を追加。`download_vod` に `details`、`_run` に `cancel_cb`、`info --json` の取得を `_info_json` へ共通化 |
| `src/archive/timeline_builder.py` | `vod_source` / `vod_analysis` の記録と読み出し、`resolve_vod_source` / `guess_video_id` |
| `src/archive/clip_writer.py` | `prepare_one_clip` と `export_intro_source` に `source_origin_sec`、`_prepare_clips` が `(prepared, vod_analysis)` を返す、`write_clips` に `vod_source` |
| `src/archive/config.py` | `section_add.refetch` の読み出し |
| `src/gui/archive_tab.py` | 取得元の控えを採点→切り抜きへ運ぶ |
| `src/gui/timeline/archive_timeline_dialog.py` | 可否判定・文言・全長の解決・準備ワーカーの取得工程・イントロの控え |
| `src/settings/settings_window.py` | `refetch` の既定値、「元動画 (VOD) を残す」のチェックボックス |
| `src/settings/setting.json` | `archive.section_add.refetch` |
| `src/i18n/catalog_en.py` | 新規文言 19 件の対訳 |
| `tests/test_vod_range.py` | **新規** 32 件 |
| `tests/test_section_add_offset.py` | **新規** 21 件 |
| `tests/test_archive_prepare.py` / `test_watermark_archive.py` / `test_archive_intro_source.py` | API 変更への追随と、控えから戻す経路の検査を追加 |

テストは **1845 件すべて成功** (追加前 1792 件 → 53 件追加)。

### 10.2 実装経路での実測 (§7.3 の 1〜3 に相当)

`fetch_range` を実際の Twitch VOD (`2910070011`) で通し、削除前の元 VOD と突き合わせた:

| 項目 | 結果 |
| --- | --- |
| 要求区間 | 6000.0〜6030.0 秒 (これまでの検証で触っていない位置) |
| twitch-dl へ渡した範囲 | `0:59:57`〜`1:00:33` (余裕 3 秒・整数秒) |
| 取得量 / セグメント | **35.1MB** / 4 件 / 約 10 秒 |
| 確定した原点 | 5991.310 秒 (相対位置 8.690 秒) |
| 元 VOD との音声相関 | **遅延 +0.0004 秒 / 相関 0.996 → 一致** |

### 10.3 報告されたプロジェクトでの確認

`vod_2910070011.archive.timeline.json` は `vod_source` を持たない (実装前に作られた) が、
元 VOD のファイル名からの後方互換で救われることを確認した:

```
resolve_vod_source = {'video_id': '2910070011', 'quality': 'source', 'from_record': False}
→ セクション追加が使えるか: True
→ 追加ダイアログの上限 (Twitch へ問い合わせ): 8991.0 秒
```

`vod_analysis` は無いため、このプロジェクトの追加セクションは従来どおり
クリップ単位で音量を測る (§4.4 の恩恵は、これ以降に採点したプロジェクトから)。

### 10.4 残課題

| 項目 | 状況 |
| --- | --- |
| `cut_region` のフレーム精度 | 利用者判断で後回し (§8)。本件とは独立 |
| 既存プロジェクトの音量基準 | `vod_analysis` を持たないプロジェクトでは追加セクションの音量が既存と揃わない (従来と同じ挙動)。採点をやり直さずに埋める手段は無い |
| 画質フォールバックの確認ダイアログ | `allow_quality_fallback` は既定 false で**落とさない**ところまでを実装した。true にしたときに利用者へ確認を取る UI (§5.4) は入れていない。設定を変えた利用者だけが対象で、変えた本人が画質の変化を承知しているため |

---

## 9. 補足 — いま手元の `vod_2910070011` をすぐ使える状態に戻す方法

実装により**元 VOD を戻さなくてもセクション追加は使える**ようになった (§10.3)。
それでも元 VOD を手元に置きたい場合の手順を残しておく。

- ゴミ箱の `vod_2910070011.mp4` を元の場所
  (`C:\Users\evolu\AppData\Local\Stretheus\archive_work`) へ復元する。
- ただし `keep_vod=false` のままではアーカイブタブを開いた時点で**再びゴミ箱へ送られる**。
  一時的に戻すなら `setting.json` の `archive.download.keep_vod` を `true` にしてから復元する。
