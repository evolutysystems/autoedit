# セクション追加が「取得した区間のファイルが見つかりません」で失敗する 原因と修正設計書

現象の報告: `docs/error/20261118/error.md`
> 以下のVODを使用してセクション追加をしようとした際にエラーが発生し、セクション追加ができませんでした。
> https://www.twitch.tv/videos/2930581402
> エラー内容: セクションを準備できませんでした。／取得した区間のファイルが見つかりませんでした。
> 一番最後からセクションを追加しようとしたらエラーが発生した (0:51:10 ~ 1:42:20)。
> 他の区間でセクション追加はできていた。セクションのダウンロードはできている模様。
> しかしダウンロード完了直後にエラーが発生する。

本書は `docs/CLAUDE.md`「実装前に設計を行うこと」に従う **設計書** であり、
実装は本書のレビュー後に行う。

対象ログ: `%LOCALAPPDATA%\Programs\AutoEdit\logs\autoedit_20261118.log` (インストール版)
対象 VOD: `2930581402` (`broadcastType=HIGHLIGHT` / 6796.0s / source=1440p60)
同梱 twitch-dl: `3.3.1` (`src/dist/Stretheus/_internal/twitch_dl-3.3.1.dist-info`)

---

## 1. 結論（先に要点）

**この VOD のセグメントは `.ts` ではなく fMP4 (`.mp4`) である。**
区間取り直し (`vod_range`) は **セグメント名が `.ts` であることを前提に組んであり**、
取得そのものは成功しているのに「1 件も見つからない」と判定して失敗していた。

```python
# src/archive/twitch_source.py:459-461
# セグメント保存名 ("00020.ts")。twitch-dl は全体プレイリストでの並び順を
# {index:05d} で付ける (twitchdl/playlists.py enumerate_vods)。
_SEGMENT_NAME_RE = re.compile(r"^(\d+)\.ts$", re.IGNORECASE)
```

twitch-dl 側の保存名は `f"{index:05d}{ext}"` で、**`ext` は元 URI の拡張子**である。

```python
# twitchdl/playlists.py enumerate_vods()
_, ext = splitext(urlparse(segment.uri).path)
filename = f"{index:05d}{ext}"
```

この VOD の全体プレイリストは **681 セグメントすべてが `.mp4`** なので、
キャッシュには `00307.mp4` … `00614.mp4` が並ぶ。`^(\d+)\.ts$` は 1 件も当たらず、
`list_segments()` が空 → `twitch_source.py:547` で下記を送出していた。

```
取得した区間のファイルが見つかりません: …\vod_range_7\2930581402\chunked
```

修正は 1 行の正規表現では済まない。**fMP4 は連結方法も違う**（§4）。
あわせて **再試行が必ず別のエラーで落ちる**残骸問題もある（§5）。

| # | 原因 | 影響 |
|---|---|---|
| A | セグメント名を `.ts` 限定で判定している | fMP4 の VOD では区間取り直しが**常に**失敗 (§3) |
| B | 連結が concat デマルチプレクサ固定 | A を直しても fMP4 は連結できない (§4) |
| C | 取得後の検証で失敗したとき断片キャッシュを片づけない | 再試行が `FileExistsError` で落ちる (§5) |

---

## 2. ログに残っている事実

```
19:54:37 プロジェクトが自己完結したため元 VOD をゴミ箱へ送りました: vod_2930581402.mp4
19:54:37 元 VOD の整理で 16.7 GB を回収しました
  …
20:15:24 セクション準備開始: 1 区間 / 合計 3190.0s
20:15:24 空き容量 OK [区間取得]: …\archive_clips_jeyalmdw (空き 18.2 GB / 必要 6.2 GB)
20:15:24 区間取得: 2930581402 3067→6263 秒 (要求 3070.0→6260.0 / 余裕 3.0s / 画質 source)
20:15:25 twitch-dl 子プロセス開始: download 2930581402
20:16:08 セクションの準備に失敗しました
  File "src\archive\vod_range.py", line 231, in fetch_range
  File "src\archive\twitch_source.py", line 547, in download_vod_range
src.exceptions.TwitchError: 取得した区間のファイルが見つかりません:
  C:\Users\evolu\AppData\Local\Temp\archive_clips_jeyalmdw\vod_range_7\2930581402\chunked
```

読み取れること。

1. **19:54 に元 VOD がゴミ箱へ送られている** (`archive.download.keep_vod=false` / 既定)。
   以降のセクション追加は `vod_range` の**取り直し経路**になる。
2. `twitch_source.py:547` は `if not segments:` の側である。
   つまり **`chunked` フォルダ自体は見つかっている** (`_find_segment_dir` は通過した)。
   見つかっていないのは「`.ts` という名前のファイル」だけ。
3. 失敗までに 43 秒・3.4 GB を使っている。**取得は完走している**
   (報告の「ダウンロードはできている模様」と一致)。
4. 同じ操作を 20:13 / 20:14 / 20:15 / 20:24 と 4 回繰り返し、4 回とも失敗している。

> 注: 報告の区間は 0:51:10〜1:42:20 だが、ログの実値は 3070.0→6260.0 秒
> (= 0:51:10〜**1:44:20**) である。以降はログの値を用いる。
> どちらでも原因・対策は変わらない。

---

## 3. 原因 A: セグメント名を `.ts` 限定で判定している（決定的証拠）

対象 VOD の source プレイリストを実際に取得して数えた。

```
https://d3vd9lfkzbru3h.cloudfront.net/…/chunked/highlight-2930581402.m3u8
```

| 項目 | 実測 |
|---|---|
| セグメント総数 | 681 |
| 拡張子の内訳 | **`.mp4` 681 / `.ts` 0** |
| `#EXT-X-TWITCH-TOTAL-SECS` | 6796.0 |
| EXTINF 累積 | 6796.0（宣言と完全一致 = 位置計算の前提は健全） |
| `#EXT-X-MAP` (init セグメント) | 3 件 (`2930581402v0-init.mp4` / `init-0.mp4` / `2930581402v680-init.mp4`) |
| `#EXT-X-DISCONTINUITY` | 2 件 (先頭直後・末尾直前) |

プレイリストの冒頭。`#EXT-X-MAP` を持つ **fMP4 (CMAF)** である。

```
#EXT-X-TWITCH-TOTAL-SECS:6796.0
#EXT-X-MAP:URI="2930581402v0-init.mp4"
#EXTINF:5.000,
2930581402v0-549.mp4
#EXT-X-DISCONTINUITY
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10.000,
550.mp4
```

同じ引数で区間取得を再現した（画質だけ 360p に落として確認）。

```
twitch-dl download 2930581402 --quality 360p --format mp4 \
  --cache-dir <tmp>/{id}/{quality} --start 0:51:00 --end 0:51:30 \
  --keep --no-join --overwrite
```

キャッシュの中身（**`.ts` は 1 件も無い**）。

```
2930581402/360p30/00306.mp4                   ← セグメント本体
2930581402/360p30/00307.mp4
2930581402/360p30/00308.mp4
2930581402/360p30/00309.mp4
2930581402/360p30/init-0.mp4                  ← #EXT-X-MAP の init セグメント
2930581402/360p30/2930581402v0-init.mp4
2930581402/360p30/2930581402v680-init.mp4
2930581402/360p30/playlist.m3u8               ← VOD 全体 (位置計算に使う)
2930581402/360p30/playlist_downloaded.m3u8    ← 取得分だけ (今回の鍵 / §4)
2930581402/360p30/playlists.m3u8
2930581402/360p30/metadata.txt
```

`_SEGMENT_NAME_RE` が当たらないため `list_segments()` は `[]` を返す。これが報告のエラーである。
なお init セグメントは数字で始まらないため、拡張子を増やしても**セグメントとして誤認しない**。

### 3.1 ハイライトに限らない（追加実測）

当初は「`HIGHLIGHT` だから fMP4」と見ていたが、**同じチャンネルの通常アーカイブも fMP4 だった**。

| VOD | `broadcastType` | 尺 | `#EXT-X-MAP` | セグメント |
|---|---|---|---|---|
| `2930581402` (報告の VOD) | `HIGHLIGHT` | 6796.0s | 3 件 | **`.mp4`** 681 件 |
| `2931334324` (同チャンネル) | `ARCHIVE` | 9002.7s | 1 件 (`init-0.mp4`) | **`.mp4`** |

```
# 2931334324 / 360p30 index-dvr.m3u8
#EXT-X-TWITCH-TOTAL-SECS:9002.701
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10.000,
0.mp4
```

`ver9 resolve5` の実装・検証時 (2026-10) に使った VOD は MPEG-TS (`.ts`) だったが、
**Twitch はその後 fMP4 (CMAF) へ移っている**。
つまり `.ts` 前提は「当時の実測がそうだった」ことに由来し、**Twitch 側の保証ではない**。
現時点では**区間取り直しを使う VOD のほとんどが fMP4** と見るべきで、
この不具合は特定の VOD の問題ではなく**機能全体の停止**にあたる。

`.ts` 経路 (§8 案 2 で温存する concat デマルチプレクサ) は、
古い VOD と将来の仕様差に備えた**互換経路**として残す位置づけになる。

---

## 4. 原因 B: fMP4 は concat デマルチプレクサでは連結できない

原因 A を直して `00306.mp4` 以降を拾えるようにしても、次の連結で落ちる。
`concat_segments()` は concat デマルチプレクサに素のセグメントを並べるが、
fMP4 の断片は **`#EXT-X-MAP` の init セグメント（moov）と組にしないと読めない**。

実測（§3 のキャッシュに対して現行の組み立てをそのまま実行）。

```
ffmpeg -f concat -safe 0 -i concat.txt -c copy -avoid_negative_ts make_zero out.mp4
  → [mov,mp4,m4a,… @ …] error reading header
  → Impossible to open '…/00306.mp4'
  → Error opening input: Invalid data found when processing input
```

対して、twitch-dl が**取得分だけを書き出したプレイリスト**
`playlist_downloaded.m3u8` を HLS デマルチプレクサへ渡すと通る。

```
#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10,
00306.mp4
…
#EXT-X-ENDLIST
```

```
ffmpeg -y -i playlist_downloaded.m3u8 -c copy -avoid_negative_ts make_zero out.mp4
  → duration=40.000000 / start_time=0.000000 / h264 640x360 + aac
```

セグメント 4 本 × 10.0s = 40.000s と**完全に一致**し、先頭も 0 秒へ揃っている。
= `origin_sec`（EXTINF 累積）との突き合わせはそのまま成立する。

### 4.1 不連続点をまたぐ区間でも成立することを確認した

この VOD は末尾に `#EXT-X-DISCONTINUITY` と別の init セグメントがある。
最後をまたぐ区間 (1:52:40〜1:53:16 / セグメント 676〜680) で確認した。

`playlist_downloaded.m3u8` には **init が切り替わる位置で 2 本目の `#EXT-X-MAP`** が入る。

```
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10,
00676.mp4
… (00677 / 00678 / 00679)
#EXT-X-MAP:URI="2930581402v680-init.mp4"
#EXT-X-DISCONTINUITY
#EXTINF:1,
00680.mp4
```

| 項目 | 実測 |
|---|---|
| 入力の開始 PTS | 12312.033 (make_zero で 0 へ) |
| 出力 duration | 41.012333 (EXTINF 合計 41.0 と一致) |
| 映像 | h264 / `start_time=0.000000` / 1230 フレーム (= 41.0s × 30fps) |
| 音声 | aac / `start_time=0.010000` |

音声の 10ms 先行は既存の許容 (`_COVERAGE_TOLERANCE_SEC=0.5`) の内側であり、
`.ts` の concat 経路にも同程度の揺れがある。**追加の補正は要らない**。

### 4.2 `playlist.m3u8` との役割分担（取り違えないこと）

| ファイル | 中身 | 用途 |
|---|---|---|
| `playlist.m3u8` | **VOD 全体** (681 件 / `#EXT-X-TWITCH-TOTAL-SECS` 付き) | **位置の確定**。`parse_segment_starts` + `segment_origin` |
| `playlist_downloaded.m3u8` | **取得分だけ** (ローカル名へ書き換え / `#EXT-X-MAP` 保持) | **連結**。ffmpeg へ渡す |

`playlist_downloaded.m3u8` には総尺宣言が無いので、
**位置計算を `playlist_downloaded.m3u8` へ寄せてはならない**。
定数 `DOWNLOADED_PLAYLIST_NAME` は `twitch_source.py:465` に既にあるが未使用である。

---

## 5. 原因 C: 検証で失敗したとき断片キャッシュを片づけていない

20:14 の試行は、報告とは**別のエラー**で落ちていた。

```
20:14:47 区間取得: 2930581402 3057→6263 秒
  File "twitchdl\http.py", line 182, in _do_download_file
FileExistsError: [WinError 183] 既に存在するファイルを作成することはできません。:
  '…\chunked\init-0.mp4.tmp' -> '…\chunked\init-0.mp4'
20:14:51 断片キャッシュを削除しました: …\vod_range_7\2930581402 (3.4 GB 回収)
20:14:51 元動画の区間を取得できませんでした (指定画質: source / 利用可能: 取得できませんでした)。
```

経路はこうなっている。

1. 20:13 の試行が §3 の理由で失敗。この失敗は `twitch_source.py:541-552` の検証段で、
   **`_cleanup_cache()` を呼ばずに `raise` している**。3.4 GB と `init-0.mp4` が残る。
2. 20:14 の再試行で twitch-dl が init セグメントを落とし直す。
   `download_file()` は **既存ファイルの有無を見ず**、`.tmp` へ書いて `os.rename` する。
   Windows の `os.rename` は**宛先があると必ず失敗**する → `FileExistsError`。
3. 例外が `except TwitchError` へ入り、ここでは `_cleanup_cache()` が走って 3.4 GB が消える。
4. 20:15 の試行は空のキャッシュから始まるので、また §3 のエラーへ戻る。

> 補足: 番号付きセグメント本体は `download_with_retries()` が
> `if target.exists(): return` で飛ばすため衝突しない。
> **毎回落とし直す init セグメントだけが衝突する。**

結果として、利用者は**同じ 3.4 GB を 4 回**落とし、2 種類のエラーを交互に見ることになった。
「失敗したら片づける」方針 (`docs/error/fromUser/resolve.md §6-3-2`) が
**取得後の検証段には適用されていない**のが抜けである。

---

## 6. 切り分け: 「他の区間ではできていた」のはなぜか

報告の「他の区間でセクション追加はできていた」は、**元 VOD が手元にあった間の追加**である。

```
autoedit_20261117.log
14:45:34 セクション準備開始: 1 区間 / 合計 9.3s
14:45:54 セクション追加: clip7 を 26.91s へ挿入 (VOD 300.0-309.3 / 尺 1.07s)
15:11:15 セクション準備開始: 1 区間 / 合計 199.7s
15:15:15 セクション追加: clip7 を 127.83s へ挿入 (VOD 600.3-800.0 / 尺 121.72s)
   …（15:51 / 16:50 / 18:11 も同様に成功）
```

**これらに `区間取得:` のログが 1 件も無い。**
`_SectionPrepareWorker._source_for()` は `_has_local_vod()` が真なら元 VOD をそのまま使い、
取り直しに入らない。11-17 時点では `vod_2930581402.mp4` が残っていたため全部成功した。

11-18 19:54 に `vod_retention` が元 VOD をゴミ箱へ送った後の追加は**すべて失敗**している。
したがって切り分けは次のとおり。

- **「一番最後の区間だから」ではない。** 区間の位置・長さは無関係。
- **この VOD では、取り直し経路を通る追加が例外なく失敗する。**
- 採点・初回の切り抜きが成功していたのは、全 VOD 取得では
  **twitch-dl 自身が ffmpeg で連結している**ため、fMP4 でも問題にならないからである。

---

## 7. 影響範囲

| 対象 | 影響 |
|---|---|
| `vod_range.fetch_range()` (セクション追加の取り直し) | **fMP4 の VOD で常に失敗**。§3.1 の実測では HIGHLIGHT / ARCHIVE とも fMP4 で、**現行の VOD はほぼ全滅** |
| 全 VOD 取得 (`twitch_source.download_vod`) | 影響なし (連結は twitch-dl が行う) |
| 採点・初回の切り抜き・書き出し | 影響なし |
| 既に作成済みのプロジェクト | 影響なし (素材はプロジェクトの隣に複製済み) |
| 縦動画・ぼかし・Resolve 出力 | 影響なし |
| `.ts` の VOD でのセクション追加 | 影響なし (§8 でも経路を変えない / 現行の Twitch では出番が無い互換経路) |

副作用として、失敗するたび区間ぶんの通信量とディスク (今回 3.4 GB) を消費していた。

---

## 8. 対策案の比較

| 案 | 内容 | 判定 |
|---|---|---|
| 1 | 正規表現を `ts|mp4` に広げるだけ | **不可**。連結が `error reading header` で落ちる (§4) |
| 2 | fMP4 のときだけ `playlist_downloaded.m3u8` を ffmpeg へ渡す | **採用**。`.ts` の経路に手を入れないため非破壊 |
| 3 | 連結を常に `playlist_downloaded.m3u8` へ一本化 | 不採用。`.ts` の実績 (位置誤差 ±0.0001s / resolve5 §2.3) を作り直すことになる |
| 4 | `--no-join` を外し twitch-dl に連結させる | 不採用。整数秒の切り詰めで末尾が最大 1 秒欠ける (resolve5 §2.2) |
| 5 | init セグメントを自前に先頭連結する | 不採用。`#EXT-X-MAP` の切り替え (§4.1) を自前で追う必要があり、ffmpeg の HLS デマルチプレクサの再実装になる |

**案 2 を採る。** 既存の `.ts` 経路はそのまま残し、fMP4 のときだけ別の連結器を使う。

---

## 9. 詳細設計

### R1. セグメント名の判定を拡張する（原因 A の除去）

`src/archive/twitch_source.py`

```python
# セグメント保存名 ("00020.ts" / "00307.mp4")。twitch-dl は全体プレイリストでの
# 並び順を {index:05d} + **元 URI の拡張子**で付ける
# (twitchdl/playlists.py enumerate_vods: f"{index:05d}{ext}")。
# Twitch は配信アーカイブを MPEG-TS (.ts)、ハイライト等を fMP4 (.mp4) で配信するため
# 両方を拾う (error 20261118)。init セグメント ("init-0.mp4" 等) は
# 数字で始まらないため、ここには当たらない。
_SEGMENT_NAME_RE = re.compile(r"^(\d+)\.(ts|mp4|m4s)$", re.IGNORECASE)
```

`list_segments()` / `segment_index()` / `_find_segment_dir()` は
この正規表現を共有しているため、他に変更は要らない。
`.m4s` は Twitch では未観測だが、HLS/CMAF の標準的な拡張子であるため保険として含める
（当たっても扱いは fMP4 と同じ / §13 の未確定事項に残す）。

### R2. セグメントの種別を返す（連結器の選択材料）

```python
# セグメント群の種別を返す ("ts" = MPEG-TS / "fmp4" = 断片化 MP4 / "" = 不明)。
# fMP4 は #EXT-X-MAP の init セグメントと組にしないと単体では読めないため、
# 連結の方法を変えなければならない (error 20261118 §4)。
# 混在しているときは安全側の "fmp4" を返す (HLS デマルチプレクサは両方読める)。
def segment_format(segments):
    exts = {os.path.splitext(str(name))[1].lower() for name in (segments or [])}
    if not exts:
        return ""
    return "ts" if exts == {".ts"} else "fmp4"
```

### R3. `download_vod_range()` の戻り値に連結材料を加える

```python
    segment_dir = _find_segment_dir(cache_for_video)
    if not segment_dir:
        _fail(cache_for_video, keep_cache_on_error,
              tr("取得した区間のファイルが見つかりません: {path}", path=cache_for_video))
    segments = list_segments(segment_dir)
    if not segments:
        # 中身はあるのに名前が想定と違うときは、それが分かる文言にする (R6)
        _fail(segment_dir, keep_cache_on_error, _unreadable_message(segment_dir))
    playlist = os.path.join(segment_dir, PLAYLIST_NAME)
    if not os.path.isfile(playlist):
        _fail(segment_dir, keep_cache_on_error,
              tr("取得した区間の位置を確かめられません（プレイリストがありません）。"))

    fmt = segment_format(segments)
    downloaded = os.path.join(segment_dir, DOWNLOADED_PLAYLIST_NAME)
    if fmt != "ts":
        # fMP4 は取得分プレイリストと init セグメントが無いと連結できない
        if not os.path.isfile(downloaded):
            _fail(segment_dir, keep_cache_on_error,
                  tr("取得した区間をまとめられません（取得分のプレイリストがありません）。"))
        missing = _missing_init_sections(segment_dir, downloaded)
        if missing:
            _fail(segment_dir, keep_cache_on_error,
                  tr("取得した区間の先頭データがありません: {names}",
                     names=", ".join(missing)))

    _logger.info("区間取得完了: %s (%d セグメント / 形式 %s / 画質 %s)",
                 segment_dir, len(segments), fmt or "不明", chosen)
    return {"segment_dir": segment_dir, "segments": segments,
            "playlist": playlist,
            "downloaded_playlist": downloaded if os.path.isfile(downloaded) else "",
            "format": fmt, "quality": chosen}
```

補助関数（いずれも `twitch_source.py`）。

```python
# 取得後の検証で失敗したときの共通処理 (error 20261118 §5)。
# **断片キャッシュを引き取ってから送出する**。残すと、次の試行で twitch-dl が
# init セグメントを置き直せず FileExistsError (WinError 183) になる。
# keep は設定 keep_cache_on_error をそのまま渡す (残すと決めた意思は尊重し、
# 衝突のほうは R5 の事前掃除で解く)。
def _fail(cache_dir, keep, message):
    _cleanup_cache(cache_dir, keep)
    raise TwitchError(message)


# playlist_downloaded.m3u8 が参照する init セグメント (#EXT-X-MAP:URI="…") のうち、
# 実在しないものの名前を返す。空なら連結できる。
def _missing_init_sections(segment_dir, downloaded_playlist):
    ...
```

### R4. 連結を 2 系統にする（原因 B の除去）

`src/archive/vod_range.py`

```python
# 取得したセグメントを 1 本の mp4 へまとめる (再エンコードなし / error 20261118 §4)。
#
# MPEG-TS (.ts) は従来どおり concat デマルチプレクサで連結する
# (位置誤差 ±0.0001s を実測済み / resolve5 §2.3)。
# fMP4 (.mp4) は #EXT-X-MAP の init セグメントと組にしないと読めないため、
# twitch-dl が書き出した playlist_downloaded.m3u8 を HLS デマルチプレクサへ渡す。
# method: "auto" (既定) / "concat" / "playlist" (設定からの上書き用 / R7)
def join_range(fetched, dest, ffmpeg_cfg, method="auto"):
    use_playlist = (method == "playlist"
                    or (method != "concat" and fetched.get("format") != "ts"))
    if use_playlist:
        playlist = fetched.get("downloaded_playlist") or ""
        if not playlist:
            raise TwitchError(tr("取得した区間をまとめられませんでした。"))
        return join_by_playlist(playlist, dest, ffmpeg_cfg)
    return concat_segments(fetched["segment_dir"], fetched["segments"], dest, ffmpeg_cfg)


# 取得分のプレイリストを ffmpeg の HLS デマルチプレクサへ渡してまとめる。
# セグメント名はプレイリストと同じフォルダの相対名なので、入力はパスだけでよい。
# -allowed_extensions ALL は .m4s 等が来ても弾かれないようにするための保険。
# 先頭を 0 秒へ揃える -avoid_negative_ts make_zero は concat 側と同じ作法
# (原点は呼び側が origin_sec で持つ)。
def join_by_playlist(playlist_path, dest, ffmpeg_cfg):
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg)
    cmd = [
        ffmpeg, "-y",
        "-allowed_extensions", "ALL",
        "-i", playlist_path,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        dest,
    ]
    ffmpeg_runner.execute(cmd)
    return dest
```

`fetch_range()` の呼び出しを差し替える（`concat_segments` は残す = 既存テストも残る）。

```diff
-    concat_segments(fetched["segment_dir"], fetched["segments"], dest, ffmpeg_cfg)
+    join_range(fetched, dest, ffmpeg_cfg, method=cfg["segment_join"])
```

### R5. 取得前に「再試行を壊す残骸」を片づける（原因 C の除去）

`twitch_source.download_vod_range()` の `_run()` 呼び出し直前に入れる。

```python
# 前回の取得が残した「再試行を壊すファイル」だけを先に消す (error 20261118 §5)。
# twitch-dl の download_file() は init セグメント (#EXT-X-MAP) を毎回落とし直し、
# .tmp へ書いてから os.rename で置く。Windows の os.rename は宛先があると
# 必ず失敗する (WinError 183) ため、残っていると再試行が 100% 落ちる。
# **番号付きセグメント本体は残す** = download_with_retries() が既存分を飛ばすので、
# 再試行が前回の続きから進む (今回の 3.4 GB の落とし直しを避ける)。
# プレイリスト・metadata.txt は twitch-dl が open("w") で上書きするため消して構わない。
def _clear_broken_leftovers(cache_for_video):
    ...  # 各サブフォルダで「番号付きセグメントでないファイル」と "*.tmp" を削除
```

これにより `keep_cache_on_error=true` のままでも再試行が通る。

### R6. 失敗メッセージを原因が分かる文言にする

「ファイルが見つかりません」では、**中身はあるのに名前が想定と違う**状況を言い表せない。

```python
# セグメントとして読めなかったときの文言。中に何があるのかを添える。
# (error 20261118: fMP4 の .mp4 を .ts 前提で弾いたのに、利用者には
#  「見つかりません」としか出ず原因が分からなかった)
def _unreadable_message(segment_dir):
    names = sorted(os.listdir(segment_dir))[:5]
    if not names:
        return tr("取得した区間のファイルが見つかりません: {path}", path=segment_dir)
    return tr("取得した区間のファイルを読み取れませんでした"
              "（想定と違う名前です: {names}）。\n{path}",
              names=", ".join(names), path=segment_dir)
```

英語カタログ (`src/i18n/catalog_en.py`) にも同じキーを追加する。

### R7. 設定 (`setting.json`)

連結器の選択は自動 (`auto`) で足りるが、
将来 Twitch 側が変わったときに再ビルド無しで退避できるよう逃げ道を 1 つ置く。
ハードコードを避ける方針 (`docs/CLAUDE.md`) にも沿う。

```json
"section_add": {
  "refetch": {
    "enabled": true,
    "pad_sec": 3.0,
    "min_free_mb_per_min": 60,
    "allow_quality_fallback": false,
    "segment_join": "auto"
  }
}
```

`src/archive/config.py _section_refetch_config()`

```python
        # セグメントの連結方法。"auto" = 形式で判断 (.ts は concat / fMP4 はプレイリスト)。
        # "concat" / "playlist" で固定もできる (error 20261118 §8 案2 の逃げ道)。
        # 未知の値は "auto" へ丸める (既存設定との互換を崩さない)。
        "segment_join": _join_method(refetch.get("segment_join", "auto")),
```

`src/settings/settings_window.py` の `DEFAULT_SETTINGS` にも同じ既定を足す。
**設定画面への項目追加はしない**（利用者が触る値ではなく、障害時の退避用）。

---

## 10. 変更ファイル一覧（予定）

| ファイル | 変更 |
|---|---|
| `src/archive/twitch_source.py` | R1 正規表現 / R2 `segment_format()` / R3 戻り値と検証 / R5 残骸の事前掃除 / R6 文言。コメントの「セグメント (.ts)」表記も改める |
| `src/archive/vod_range.py` | R4 `join_range()` `join_by_playlist()` 追加、`fetch_range()` の連結呼び出し差し替え、冒頭コメントの `{index:05d}.ts` 表記を改める |
| `src/archive/config.py` | R7 `segment_join` |
| `src/settings/setting.json` | R7 既定値 |
| `src/settings/settings_window.py` | R7 `DEFAULT_SETTINGS` |
| `src/i18n/catalog_en.py` | R6 の新しい文言 |
| `tests/test_vod_range.py` | §11 のテスト追加 |
| `docs/design/design.md` | §4.2 の役割分担と「fMP4 / MPEG-TS の両対応」を追記 |
| `docs/README.md` | `error/20261118` を §3.2 の表へ登録 |

---

## 11. テスト計画

### 11.1 単体（ネットワーク不要 / `python -m unittest discover -s tests`）

| # | 内容 | 期待 |
|---|---|---|
| T1 | `segment_index("00307.mp4")` / `("00020.ts")` | `307` / `20` |
| T2 | `segment_index("init-0.mp4")` / `("2930581402v0-init.mp4")` | いずれも `None`（init を誤認しない） |
| T3 | `list_segments()` に `.mp4`・init・`*.tmp` を混ぜる | 番号順の `.mp4` だけが返る |
| T4 | `segment_format()` | `.ts` のみ→`"ts"` / `.mp4` 含む→`"fmp4"` / 空→`""` |
| T5 | `join_range()` が `format="fmp4"` で `join_by_playlist` を選ぶ | `ffmpeg_runner.execute` の引数が `playlist_downloaded.m3u8` を `-i` に取り、`concat.txt` を作らない |
| T6 | `join_range()` が `format="ts"` で従来経路を選ぶ | 現行 `ConcatTest` と同じ組み立て（**既存テストは変更しない**） |
| T7 | `segment_join="concat"` / `"playlist"` の強制 | 形式に関わらず指定どおりの経路。未知の値は `auto` |
| T8 | `download_vod_range()`: `.mp4` セグメントと `playlist_downloaded.m3u8` を置いた偽キャッシュ | `format="fmp4"` と `downloaded_playlist` が返る |
| T9 | 同: init セグメントが無い | `取得した区間の先頭データがありません` を送出し、キャッシュが消えている |
| T10 | 同: セグメント 0 件（別名ファイルだけ） | R6 の文言になり、`_cleanup_cache` が呼ばれている |
| T11 | `_clear_broken_leftovers()` | 番号付きセグメントは残り、init と `*.tmp` が消える |
| T12 | `fetch_range()` を `.mp4` セグメントのスタブで通す | `origin_sec` が EXTINF 累積どおり（`.ts` と同値） |

### 11.2 結合（実 VOD・手動）

| # | 手順 | 期待 |
|---|---|---|
| I1 | VOD `2930581402` のプロジェクトを開き、元 VOD が手元に無い状態で 0:51:10→1:44:20 を追加 | 取得→連結→追加まで完走 |
| I2 | I1 の素材を再生し、元 VOD の 0:51:10 以降と内容が一致するか確認 | 位置ずれなし（`origin_sec` の検証） |
| I3 | 末尾の不連続点をまたぐ区間 (1:52:40→1:53:16) を追加 | 完走（§4.1 の実測どおり） |
| I4 | `.ts` の VOD で同じ操作 | 従来どおり成功（**退行がないこと**）。§3.1 のとおり現行 Twitch では `.ts` の VOD が手に入らないため、`.ts` 経路の担保は単体 T6 と `segment_join="concat"` の強制 (T7) で行う |
| I5 | 取得中に中止 → 同じ区間をもう一度追加 | `FileExistsError` が出ず、前回の続きから進む |
| I6 | `keep_cache_on_error=true` で I5 | 同上 |

---

## 12. 既存実装への影響と互換性（非破壊の担保）

- **`.ts` の連結経路は 1 行も変えない。** `concat_segments()` は残し、既存テストもそのまま通る。
- `download_vod_range()` の戻り値は**キーの追加のみ**。既存の 4 キーは意味も型も変えない。
- `segment_join` は未設定時 `"auto"` = 現行と同じ判断。既存 `setting.json` のままで動く。
- 位置の確定は従来どおり `playlist.m3u8` の EXTINF 累積で行う。
  **`playlist_downloaded.m3u8` を位置計算に使わない**（総尺宣言が無く `verify_total` が成立しない）。
- 作成済みプロジェクト・採点結果・書き出しには触らない。
- 追加ライブラリは無い（HLS デマルチプレクサは同梱 ffmpeg の標準機能）。

---

## 13. 未確定事項（推測実装しない項目）

1. **`.m4s` の実在**: Twitch では未観測。R1 で拾うだけに留め、専用処理は書かない。
   実例が出たら `segment_format()` が `"fmp4"` を返し、そのまま連結経路へ乗る。
2. **`playlist_downloaded.m3u8` が無い twitch-dl 版**: 同梱の 3.3.1 では必ず書き出される
   （`--no-join` の早期 return より前）。同梱版を上げるときは T8 で担保する。
3. **音声の 10ms 先行 (§4.1)**: 既存許容の内側のため補正しない。
   取り直した素材と既存セクションの繋ぎで違和感が出た場合のみ、別途検討する。
4. **fail-fast**: 現在は区間を落とし終えてから検証している。
   取得前に `playlist.m3u8` だけ先読みして形式を判定することも可能だが、
   本修正で両形式を扱えるようになるため**今回は入れない**（API 呼び出しが増えるだけ）。

---

## 14. 実装記録（2026-11-18）

§9 の R1〜R7 をすべて実装した。§10 の変更ファイル一覧どおり。

設計との差分は 1 点のみ。

- **`_fail()` が片づける範囲**: 設計では `segment_dir`（`chunked` など画質フォルダ）を
  渡す形で書いていたが、**`cache_for_video`（動画 1 本ぶん）** に改めた。
  取得そのものが失敗したとき (`except TwitchError`) と同じ範囲になり、
  「どこで失敗しても残骸は残らない」が揃う。画質フォールバックで別フォルダが
  できていた場合も取りこぼさない。

### 14.1 検証結果

**単体**: `python -m unittest discover -s tests` → **2417 件 OK**
（`tests/test_vod_range.py` は 47 件。§11.1 の T1〜T12 を収容）。

**実機 (報告の VOD / fMP4)**: `vod_range.fetch_range('2930581402', 3070.0, 3100.0, …)`

| 項目 | 結果 |
|---|---|
| 取得 → 連結 → 検証 | **完走** (修正前は `取得した区間のファイルが見つかりません`) |
| `origin_sec` | **3065.0**（セグメント 307 の EXTINF 累積と一致） |
| `end_sec` | 3105.005（要求 3070.0→3100.0 を覆う） |
| セグメント数 / 取得量 | 4 件 / 3.2 MB (360p) |
| ログ | `区間取得完了: … (4 セグメント / 形式 fmp4 / 画質 360p)` |

**実機 (同チャンネルの ARCHIVE / これも fMP4)**:
`fetch_range('2931334324', 3070.0, 3100.0, …)` → `origin_sec=3060.0` /
`end_sec=3110.0` / 5 セグメント / 完走。

**再試行 (原因 C)**: 同じ `tag` で 2 回続けて実行。

```
前回の取得の残骸を片づけました: …\vod_range_t1\2930581402 (7 件)
区間取得完了: …\360p30 (4 セグメント / 形式 fmp4 / 画質 360p)
```

- `FileExistsError (WinError 183)` は再現しない。
- 消えたのは init セグメント 3 件・プレイリスト 3 件・`metadata.txt` の **7 件**で、
  **番号付きセグメント本体は残った**。
- 2 回目は約 3 秒で完了（1 回目は約 16 秒）= 落とし直していない。

### 14.2 残った既知事項

- `.m4s` は依然として未観測（§13-1）。拾うだけで専用処理は持たない。
- fail-fast は入れていない（§13-4）。両形式を扱えるようになったため不要と判断。
