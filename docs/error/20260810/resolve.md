# VOD 取得が「Source quality not found」で失敗する不具合 調査・修正設計書

対象エラー: `docs/error/20260810/error.md`
主対象ソース: `src/archive/twitch_source.py`（`download_vod`）／`src/gui/archive_tab.py`（`_fetch_twitch`）
関連ソース: `src/archive/config.py`（`download_config`）／`src/settings/settings_window.py`（`DEFAULT_SETTINGS`）
外部依存: `twitch-dl 3.3.1`（`twitchdl/playlists.py`・`twitchdl/twitch.py`）
本書は `docs/CLAUDE.md` 方針に準拠した**調査・修正設計書**であり、実装は承認後に行う。

---

## 1. 結論（先に要点）

**アプリの不具合ではなく、「この VOD には source 画質が存在しない」ことに対して
アプリが `--quality source` を固定で渡しているため落ちている。**

* 実測（§2-2）: 対象 VOD `2833484601` が持つ画質は
  **1080p60 / 720p60 / 360p / Audio Only の 4 つだけで、source（`chunked`）が無い**。
* twitch-dl は `--quality source` を「`group_id == "chunked"` の playlist」と定義しており、
  無ければ即エラーで終了する（§2-1）。
* アプリ側は `archive.download.vod_format`（既定 `"source"`）を**そのまま渡すだけ**で、
  取得できないときの代替を持たない（§2-3）。

したがって修正は **「希望画質が無ければ、利用できる最高画質へ自動で落とす」** ことになる。

---

## 2. 原因の特定

### 2-1. twitch-dl 側の仕様（コードで確認）

`twitchdl/playlists.py`:

```python
def select_playlist_by_name(playlists, quality):
    if quality == "source":
        for playlist in playlists:
            if playlist.is_source:
                return playlist
        raise click.ClickException("Source quality not found, please report an issue on github.")

    for playlist in playlists:
        if playlist.name == quality or playlist.group_id == quality:
            return playlist
    available = ", ".join([p.name for p in playlists])
    raise click.ClickException(f"Quality '{quality}' not found. Available qualities are: {available}")
```

```python
def _parse_playlist(playlist):
    media = playlist.media[0]
    is_source = media.group_id == "chunked"      # ← source の定義はこれだけ
```

* `"source"` は**エイリアスではなく `chunked` グループの完全一致**。
  「最高画質を選ぶ」という意味ではないため、`chunked` が無い VOD では必ず失敗する。
* 一方、`"source"` 以外の指定は `name` か `group_id` の一致で選ばれ、
  失敗時のメッセージに**利用可能な画質一覧が出る**（この違いが §5-3 の実装で効く）。
* なお `twitch.get_playlists()` は `allow_source=true` / `include_unavailable=true` を付けて
  取得しており、**隠れた（地域・ログイン制限の）playlist も拾ったうえで `chunked` が無い**。
  つまり「取り方が悪くて見えていない」のではない。

### 2-2. 対象 VOD の実測（決定的証拠）

`twitch-dl info 2833484601 --json` の結果（2026-08-10 実行）:

| name | group_id | resolution | is_source |
|---|---|---|---|
| 1080p60 | 1080p60 | 1920x1080 | **False** |
| 720p60 | 720p60 | 1280x720 | **False** |
| 360p | 360p30 | 640x360 | **False** |
| Audio Only | audio_only | (なし) | **False** |

（VOD: `配信確認配信` / 7095 秒 / creator `tatsumic` / 2026-07-31 公開）

**`chunked` が 1 つも無い**。error.md のログが
`Fetching playlists...` の直後に落ちているのと完全に一致する。

> 「なぜ source が無いのか」は Twitch 側の都合であり、本アプリからは制御できない。
> 一般に、配信時のトランスコード構成・チャンネルの種別・VOD の保存期間などで
> source（無変換）レンディションが提供されないことがある。**断定は避け、
> 「source が常に在るとは限らない」という前提でアプリを作り直す**のが本書の立場。

### 2-3. アプリ側の実装

`src/archive/twitch_source.py:128-139`:

```python
def download_vod(video_id, work_dir, twitch_dl_path="twitch-dl", quality="source", ...):
    argv += ["download", str(video_id),
             "--quality", quality or "source",     # ← 設定値をそのまま渡すだけ
             ...]
```

`src/archive/config.py:53` / `settings_window.py:402`:

```python
"vod_format": str(dl.get("vod_format", "source") or "source"),   # 既定 "source"
"vod_format": "source",      # 取得画質 (source=最高)   ← コメントの認識も誤り
```

* 既定が `"source"` 固定で、**代替画質へ落ちる経路が無い**。
* 設定コメントの「source=最高」は誤り。正しくは「source=無変換（chunked）。無い VOD もある」。
* 失敗時の画面表示は `_run()` の汎用文言
  「twitch-dl の実行に失敗しました (code 1)」＋末尾 5 行のため、
  **利用者には「どうすれば直るか」が分からない**。

### 2-4. 切り分け（誤解を招きやすい点）

| 疑い | 判定 | 根拠 |
|---|---|---|
| 認証（sub-only VOD）で playlist が取れていない | **✗** | `AuthRequiredError` ではなく playlist 取得後の画質選択で失敗している |
| twitch-dl が古い／壊れている | **✗** | 3.3.1 で仕様どおりの動作。他の画質なら取得できる |
| ffmpeg が見つからない | **✗** | 結合前（playlist 選択時）に落ちている |
| ネットワーク・所有判定 | **✗** | `Fetching chapters/access token/playlists` まで成功している |
| 出力先やファイル名 | **✗** | `Target:` 表示は失敗前の情報表示 |

---

## 3. 影響範囲

* **Twitch から取得する経路のみ**。ローカル動画を選ぶ経路（`downloader=local`）は無関係。
* `source` を持たない VOD すべてで再現する（この配信者・この VOD 固有ではない）。
* コメント取得（`chat json`）は画質と無関係のため影響なし。

---

## 4. 対策案の比較

| | 案A: 取得前に画質一覧を調べて選ぶ | 案B: 失敗したら別画質で再実行 | 案C: 設定で固定画質にする | 案D: twitch-dl の更新を待つ |
|---|---|---|---|---|
| 効き目 | ◎ 常に最良の 1 本を選べる | ○ 直るが 1 回無駄に走る | △ VOD ごとに手直しが要る | ✗ 仕様どおりの動作なので直らない |
| 利用者の手間 | 無し | 無し | **毎回必要** | — |
| 追加コスト | `info` 1 回（数秒） | 失敗分の待ち時間 | 無し | — |
| 実装の素直さ | ◎ 判断材料が揃ってから決められる | △ エラー文言の解析に依存 | ○ | — |

**採用: 案A**（案B は `info` が失敗したときの保険として一部を流用する / §5-3）。

理由:

1. `twitch-dl info --json` が `playlists[]` を機械可読で返すため、**文言解析に頼らず**決められる。
2. 「希望画質 → 無ければ最高画質」の判断をアプリ側に持てるので、
   ログに「source が無いので 1080p60 を使う」と残せる（利用者が状況を理解できる）。
3. 追加コストは API 2 回ぶん（数秒）で、数十分かかる VOD 取得全体から見れば無視できる。

---

## 5. 詳細設計

### 5-1. 画質一覧の取得（`twitch_source.list_qualities()` 新規）

```python
# VOD で利用できる画質の一覧を返す (ver3 20260810)。
# 戻り値: [{"name","group_id","resolution","is_source"}]。取得できないときは [] を返す
# (呼び出し側は従来どおり希望画質で実行してよい = 挙動を悪化させない)。
def list_qualities(video_id, twitch_dl_path="twitch-dl", ffmpeg_dir=None):
    argv = list(_resolve_twitchdl(twitch_dl_path)) + ["info", str(video_id), "--json"]
    ...
```

**実装上の要点**（実測で確認した twitch-dl の挙動）:

* `info --json` は **JSON を stdout、進捗ログ（`Fetching video...` 等）を stderr** へ出す
  （`twitchdl/output.py`: `print_json`→`click.echo` / `print_log`→`click.secho(err=True)`）。
  既存の `_run()` は `stderr=STDOUT` へ**合流させている**ため、そのままでは JSON を壊す。
  → `list_qualities` は `_run()` を使わず、**stdout だけを取る専用の実行**にする。
* 失敗（非0終了・JSON 不正・ネットワーク断・sub-only の認証要求）は
  **例外にせず空リスト**を返す。取得できないときに VOD 取得ごと止めては本末転倒のため。
* 凍結配布物でも `__twitchdl__` マルチコールで `info` を呼べる
  （`main_window.py:810-814` が twitch-dl の CLI 全体をディスパッチしている）。

### 5-2. 画質の決定（`twitch_source.resolve_quality()` 新規）

```python
# 映像を持たない配信 (音声のみ) は編集できないため候補から外す。
# twitch-dl の group_id 定数であり利用者が変える値ではないので定数で持つ。
_AUDIO_ONLY_GROUP = "audio_only"


# 希望画質 preferred が使えるならそれを、無ければ利用できる最高画質を返す。
# playlists が空 (一覧を取れなかった) なら preferred をそのまま返す。
def resolve_quality(playlists, preferred="source"):
    ...
```

**選び方の規則**（上から順に判定）:

1. `audio_only` は常に除外する。
2. `preferred == "source"` … `is_source` が真のものがあればそれを採用。
3. `preferred` が `name` または `group_id` に一致すればそれを採用
   （利用者が `"1080p60"` のように指定している場合）。
4. どれにも当たらなければ **利用できる最高画質**を採用する。
   並べ替えは **解像度の高さ（縦画素数）→ フレームレート**の順で降順。
   フレームレートは `name` 末尾の数字（`1080p60` → 60、`1080p` → 30 相当）から読む。
5. 候補が 1 つも無ければ `preferred` をそのまま返す（従来どおりの失敗に委ねる）。

> 4 の並べ替えで `resolution` が `None` の要素（Audio Only）は 1 で除外済み。
> `360p` のように `name` と `group_id`（`360p30`）が違うものがあるため、
> **twitch-dl へ渡すのは `name`** とする（`select_playlist_by_name` は両方を見るが、
> 一覧表示と一致する `name` の方が利用者にとって分かりやすい）。

### 5-3. `download_vod()` の変更

```python
def download_vod(video_id, work_dir, twitch_dl_path="twitch-dl", quality="source",
                 auth_token="", start=None, end=None, progress_cb=None, ffmpeg_dir=None,
                 quality_fallback=True):
    ...
    if quality_fallback:
        playlists = list_qualities(video_id, twitch_dl_path, ffmpeg_dir)
        chosen = resolve_quality(playlists, quality or "source")
        if playlists and chosen != (quality or "source"):
            message = (f"{quality} 画質が無いため {chosen} で取得します "
                       f"(利用可能: {', '.join(p['name'] for p in playlists)})")
            _logger.info(message)
            if progress_cb:
                progress_cb(message)      # 画面にも出して利用者へ知らせる
    else:
        chosen = quality or "source"
    argv += ["download", str(video_id), "--quality", chosen, ...]
```

* `quality_fallback=False` を渡せば**従来と完全に同じ挙動**になる（切り戻し用）。
* `list_qualities()` が空を返したときは `resolve_quality()` が `preferred` を返すため、
  **現行と同じコマンドが組まれる**（＝この修正で新たに壊れる経路が無い）。

### 5-4. 失敗時メッセージの改善（`_run()` は変えない）

画質が原因で落ちた場合に限り、`download_vod` 側で分かりやすい文言へ包み直す。

```python
try:
    _run(argv, progress_cb, label="VOD取得中…", env=_twitchdl_env(ffmpeg_dir))
except TwitchError as e:
    names = ", ".join(p["name"] for p in playlists) if playlists else "取得できませんでした"
    raise TwitchError(
        f"VOD の取得に失敗しました (指定画質: {chosen} / 利用可能: {names})。\n{e}") from e
```

### 5-5. 呼び出し側（`archive_tab._fetch_twitch`）

```python
input_path = twitch_source.download_vod(
    video_id, work_dir, dl["twitch_dl_path"], dl["vod_format"],
    progress_cb=..., ffmpeg_dir=ffmpeg_dir,
    quality_fallback=dl["vod_quality_fallback"])
```

### 5-6. 設定（`setting.json` / `download_config`）

```jsonc
"archive": {
    "download": {
        // 希望画質。"source"=無変換(chunked)。VOD によっては存在しないことがある
        "vod_format": "source",
        // 追加: 希望画質が無いとき、利用できる最高画質へ自動で落とす (既定 true)
        "vod_quality_fallback": true
    }
}
```

* `download_config()` へ `"vod_quality_fallback": bool(dl.get("vod_quality_fallback", True))` を追加。
* `settings_window.DEFAULT_SETTINGS` の `vod_format` のコメントを
  **「source=最高」→「source=無変換(chunked)。無い VOD もある」**へ訂正する。
* 設定画面への項目追加は行わない（現状も `vod_format` は UI 非公開のため）。

---

## 6. 変更ファイル一覧（予定）

| 種別 | ファイル | 内容 |
|---|---|---|
| 変更 | `src/archive/twitch_source.py` | `list_qualities()` / `resolve_quality()` 追加、`download_vod()` に fallback と文言改善 |
| 変更 | `src/archive/config.py` | `download_config` に `vod_quality_fallback` を追加 |
| 変更 | `src/gui/archive_tab.py` | `download_vod()` へ `quality_fallback` を渡す |
| 変更 | `src/settings/settings_window.py` | 既定値へ `vod_quality_fallback` 追加・`vod_format` のコメント訂正 |
| 変更 | `src/settings/setting.json` | 同上（キー追加） |
| 新規 | `tests/test_twitch_quality.py` | `resolve_quality()` の単体テスト（§8-1） |

---

## 7. 影響・非破壊の担保

| 対象 | 保証内容 |
|---|---|
| ローカル動画の経路 | 変更なし |
| コメント取得 | 変更なし |
| source を持つ VOD | `resolve_quality` が `source` を返すため**従来と同一のコマンド**になる |
| `info` が失敗する環境 | 空リスト → 希望画質のまま実行 = 従来と同一の挙動 |
| 既存 setting.json | 追加キーは既定 `true` で補完。未記載でも動く |
| 切り戻し | `vod_quality_fallback=false` で完全に従来動作へ戻せる |

---

## 8. テスト計画

### 8-1. 単体（ネットワーク不要）

`resolve_quality()` に固定の playlists を与えて検証する。

* source がある → `source` 指定で source が選ばれる
* **source が無い**（今回の実測データ: 1080p60 / 720p60 / 360p / audio_only）
  → **1080p60** が選ばれる
* `preferred="720p60"` が実在 → そのまま 720p60
* `preferred="1440p"` が不在 → 最高画質へ落ちる
* 候補が **audio_only のみ** → 何も選べず `preferred` を返す（後段で従来のエラー）
* `playlists=[]`（一覧を取れなかった）→ `preferred` をそのまま返す
* 解像度が同じで fps 違い（`1080p60` と `1080p`）→ **60 が優先**される

### 8-2. 結合（実 VOD・手動）

* [ ] 今回の VOD `2833484601` で取得が **1080p60 で完走**する
* [ ] 画面に「source 画質が無いため 1080p60 で取得します」が出る
* [ ] source を持つ VOD では従来どおり source で取得される（ログで確認）
* [ ] `vod_quality_fallback=false` で従来どおり失敗する（切り戻しの確認）
* [ ] 取得後の採点 → 切り抜き → 書き出しまで通る

---

## 9. 確認事項

### 9.1 回答により確定（2026-08-10）

| # | 内容 | 回答 |
|---|---|---|
| **Q1** | 自動で画質を落とす方針でよいか | **自動で落とす**（source が無ければ利用できる最高画質で続行） |
| **Q2** | 落とす下限を設けるか（例: 720p 以下なら中止） | **設けない**（どの画質でも続行する） |

いずれも本書の前提どおりのため、§5 の設計に変更は無い。

### 9.2 未回答（この前提で進める）

| # | 内容 | 本書の前提 |
|---|---|---|
| **Q3** | sub-only VOD 対応（twitch-dl 3.3.1 の `--sub-only`）も併せて入れるか | 本書の範囲外。別件として扱う |
| **Q4** | `info` の追加実行（数秒）を常に行ってよいか | 行う。`vod_quality_fallback=false` で無効化できる |
