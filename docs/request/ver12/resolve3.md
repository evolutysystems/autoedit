# ver12 resolve3 — アーカイブ切り抜き用タブをクリップ用タブと同じ形にする

要望: `docs/request/ver12/request3.md` (2026-11-18)
前提: `docs/request/ver3/resolve15.md` (クリップ用タブの D&D 化 = 今回そろえる相手)
　　　`docs/request/ver3/resolve9.md` §5.8-5.9 (編集の続き / 種別の取り違え防止)
　　　`docs/request/ver3/resolve16.md` §5.6-5.7 (マーカー権限と再ログイン)
　　　`docs/error/fromUser/resolve.md` §6-6 (取得データの使用量と回収)
　　　`docs/request/ver12/resolve1.md` (ver12 の現行作業)
　　　`docs/CLAUDE.md` (実装前に設計 / ハードコード禁止 / 既存実装を破壊しない)
対象: `src/gui/archive_tab.py` / `src/gui/main_window.py` / `src/gui/file_drop_area.py` /
　　　`src/gui/theme.py` / `src/i18n/catalog_en.py` / `tests/*` /
　　　`docs/layout/main_window.html` / `docs/design/design.md`
状態: **設計 (未実装)**

---

## 0. 先に結論

| # | 要望 | 本書の結論 |
| --- | --- | --- |
| **A** | ベースをクリップ用と同じに | **できる**。`FileDropArea` は種別を知らない作りで、アーカイブ用へ載せる前提で作られている (ver3 resolve15 §11 F1)。並び・幅・アイコンはクリップ用の値をそのまま使う |
| **B** | ログインステータス表示を削除 | **削除する**。常設ラベル (`login_status`) を無くし、**状態は「どの部品が出ているか」で示す**。途中の文言 (認可待ち・確認中・失敗) は画面共通の状態ラベルへ寄せる |
| **C** | ログイン時だけ URL 欄が D&D 領域の上部に出る | **出す**。未ログインではその位置に「Twitch ログイン」ボタンだけが出る (= ログインの入口はタブに残す。§9 の Q1) |

**要点は「選択はいつでも 1 つ」**。クリップ用タブと同じ考え方 (resolve15 §5.6-3) を
アーカイブ用へも持ち込み、**動画 / コメント付き動画 / VOD の URL / 保存したプロジェクト**の
どれが選ばれているかを D&D 領域 1 か所が表す。実行ボタンはその 1 つだけを見て経路を分ける。

採点・取得・切り抜き・再開の**処理側 (ワーカーと `src/archive/*`) は一切変えない**。
ワーカーへ渡す `job` 辞書 (`{"mode","input_path","url","chat_path"}`) も現状のままにする。

---

## 1. 新しい画面 (上から)

```
┌─ アーカイブ切り抜き用 ─────────────────────────────────┐
│ [Twitch 行]  ログイン済: [ https://www.twitch.tv/videos/…    ▼ ] │  ← C
│              未ログイン:          [ Twitch ログイン ]            │
│                                                                  │
│ ┌──────── D&D 領域 (タブページ高の 60%) ────────────────┐ │
│ │                        ⬇                                    │ │
│ │           ドラッグ＆ドロップでファイルを選択                │ │
│ │  配信動画 / コメントJSON / 保存したアーカイブ用プロジェクト  │ │
│ └──────────────────────────────────────────────┘ │
│   (入力パス欄 + 参照... / コメントJSON 欄 + 参照... は既定で非表示) │
│                                                                  │
│                      [   続きから   ]                            │  ← 幅 96px
│                      [      ▶      ]  ([ 中止 ] は実行中のみ)    │  ← 幅 96px・アクセント塗り
│                                                                  │
│ [███████████████░░░░░░░░░░░] 42%                                 │
│ ⠹ 文字起こし中…                                                  │
│                    (ここに縦の余りを捨てる)                      │
│ □ 無音カット   取得した配信データ: 23.4 GB / 2 件 [取得データを削除] │
└────────────────────────────────────────────────────┘
```

### 1.1 クリップ用から**そのまま持ってくる**もの

| 部品 | 根拠 |
| --- | --- |
| D&D 領域 (`FileDropArea`)。高さ = `ui.main_window.drop_zone_ratio` (0.60)、下限 `drop_zone_min_height_px` (140) | resolve15 C1 / C4。新しい設定キーは作らない |
| 隠した入力行 (`input_edit` + 「参照...」) を `ui.main_window.show_file_row` で出し戻せる | resolve15 C2。ダイアログで選ぶ道を消さない |
| 「続きから」ボタン (中央・幅 `theme.PRIMARY_ACTION_BUTTON_WIDTH_PX` = 96px・塗りなし) | resolve15 C6 / C8 |
| 実行ボタン (中央・幅 96px・アクセント塗り・`theme.RUN_GLYPH` の ▶・用途はツールチップ) | resolve3 §5.2-2 / resolve15 C7 |
| 進捗バー → 状態ラベル → `addStretch(1)` の並び | resolve15 §5.6-1 |
| 落としている最中の枠の色替え (`set_drop_active`) と、実行中は × を無効化 (`set_busy`) | resolve15 C3 |

### 1.2 アーカイブ用に**残す**もの (クリップ用には無い)

| 部品 | 置き場所 | 残す理由 |
| --- | --- | --- |
| **中止** ボタン | 実行ボタンと**同じ中央寄せの行**。待機中は `setVisible(False)` | 書き出しが数十分かかるため「やめる手段」は必須 (resolve3 R9-5)。隠れているあいだ行は実行ボタンだけを中央に置く (非表示のウィジェットは `QHBoxLayout` で場所を取らない) ので、待機中の見た目はクリップ用と同一になる |
| **無音カット** のチェック | 最下段のオプション行 (`addStretch(1)` の後) | 実行ごとに切り替える値 (resolve20 §5.8)。設定画面へ移すと「今回だけ外す」ができない |
| **取得データの使用量 + 削除** | 同じ最下段のオプション行 (右端) | 1 本 10〜25 GB の取得物を利用者が自分で回収できる唯一の入口 (`docs/error/fromUser/resolve.md` §6-6) |

> 最下段へ下げるのは、**上から「続きから・実行・進捗・状態」までをクリップ用と
> 同じ並びにする**ため。縦の余りは `addStretch(1)` が吸うので、
> オプション行はタブ下端に貼り付く。

### 1.3 **無くす**もの

| 今ある部品 | 扱い | 理由 |
| --- | --- | --- |
| 説明ラベル (`description_label`、2 行の説明文) | **削除** | クリップ用に無い。内容は D&D 領域の補足 1 行へ畳む |
| 入力ソース選択 (`mode_caption` + `mode_combo`) | **削除**。モードは**選ばれているもの**から決まる (§2.2) | 「ローカル or Twitch」を人に選ばせる必要が無くなる。URL が選ばれていれば Twitch、動画ならローカル |
| `local_group` / `twitch_group` の入れ物 | **削除** | 切り替える対象が無くなる |
| **ログイン状態ラベル (`login_status`)** | **削除** (要望 B) | §2.3 |
| 自分の VOD プルダウン (`vod_combo`) | URL 欄に**畳む** (編集できるプルダウン = 見た目は URL のテキストボックス + ▼) | 一覧そのものは有用 (運用の主入力は自分の VOD)。欄を 2 本並べない (§9 の Q2) |
| 「編集の続き」行 (`ProjectResumeRow`) | **削除**し「続きから」ボタン + D&D へ置き換え | resolve15 C5/C6 と同じ扱い。履歴は一覧画面 (`ProjectLibraryDialog`) が持つ |

---

## 2. 決めごと

### 2.1 選択は 1 つ (クリップ用と同じ規約 / resolve15 §5.6-3)

`self._selection = (種別, 値)` を持つ。種別は 3 つ:

| 種別 | 値 | D&D 領域の表示 | 補足 (hint) |
| --- | --- | --- | --- |
| `video` | 動画のパス | ファイル名 (太字) | `実行すると採点からはじまります` / コメントJSON も選ばれていれば `コメント採点: {name}` |
| `vod` | VOD の URL | VOD のタイトル (一覧から選んだとき) か `videos/1234567890` | `実行すると VOD の取得と採点からはじまります` |
| `project` | `*.timeline.json` のパス | ファイル名 (太字) | `実行すると編集の続きから書き出します` |

* **コメント JSON は「選択」ではなく動画への添付**として `self._chat_path` に持つ。
  D&D 領域の補足行に名前を出す。× (選択の取り消し) で動画と一緒に消える。
* `vod` を選ぶと `video` / `project` の選択は消え、逆も同じ。
  **URL 欄へ URL を入れる = vod を選ぶ操作**であり、× を押すと URL 欄も空になる。
* 種別が決まる経路は 4 つ (D&D / 「参照...」 / 「続きから」 / URL 欄) で、
  **どれも `_select_*()` を通す**。表示と選択がずれないようにする。

### 2.2 実行ボタンが分ける経路

```
_on_run():
  種別なし  → 「動画を落とすか、URL を貼るか、「続きから」で選んでください」と案内して終わり
  project   → パス存在確認 → _start_resume(path)              (現行の再開経路そのまま)
  video     → 存在確認 → job{"mode": local, input_path, chat_path} → ArchiveAnalyzeWorker
  vod       → extract_video_id で形だけ確認 → job{"mode": twitch, url} → ArchiveAnalyzeWorker
```

* `_current_mode()` は**選択から導く形**に変える (`mode_combo` を見ない)。
  `_MODE_LOCAL` / `_MODE_TWITCH` の値とワーカーの分岐は現状のまま。
* ツールチップは**クリップ用と同じ `実行`** にする (§9 の Q4)。
  「採点なのか再開なのか」は D&D 領域の補足行が答える。
* 「続きから」は一覧画面を開き、**選ぶところまで**で実行しない
  (現行は一覧で選ぶとそのまま再開が走る)。クリップ用と同じ約束にそろえる
  (resolve15 §3-6)。種別違いが選ばれたら今までどおり `switch_tab_requested` で
  相手のタブへ回す。

### 2.3 Twitch 行 (要望 B と C)

D&D 領域の**上**に行を 1 本だけ置き、状態で中身を入れ替える。

| 状態 | URL 欄 | ログインボタン | 画面共通の状態ラベル |
| --- | --- | --- | --- |
| 起動直後の確認中 (`TwitchRestoreWorker` 実行中) | 隠す | 出す・押せない (ツールチップ `ログイン状態を確認しています…`) | `ログイン状態を確認中…` |
| ログイン済み (マーカー権限あり) | **出す** | 隠す | 触らない |
| ログイン済み (マーカー権限なし / 旧トークン) | 出す | **出す** (押せる。ツールチップ `再ログインすると、ストリームマーカーを採点に使えるようになります。`) | 触らない |
| 未ログイン / 復元失敗 | 隠す (例外は下記) | 出す・押せる | `未ログイン` は**出さない** (何も書かない) |
| 認可待ち | 隠す | 押せない | `ブラウザで認可してください…` |
| ログイン失敗 | 隠す | 押せる | `ログインに失敗しました` + 現行どおりダイアログ |

* **「ログイン中: {名前}」は画面に出さない** (要望 B)。情報を失わないために
  **URL 欄のツールチップ** (`Twitch: {name} のアーカイブから選べます`) とログ
  (`_logger.info`) へ移す。マーカー未許可の注意もツールチップ側で伝える
  (ver3 resolve16 §5.6 の「再ログインで直る」案内は残す)。
* 例外: `archive.auth.owner_only = false` の設定では他人の公開 VOD も切れるため、
  **未ログインでも URL 欄を出す** (`ログイン済み or not owner_only`)。
  既定は `owner_only = true` なので、通常の利用では要望どおり
  「ログインしていれば出る」になる。
* URL 欄の実体は**編集できる `QComboBox`**:
  * `lineEdit()` のプレースホルダ = `VOD の URL を貼り付け (https://www.twitch.tv/videos/…)`
  * 項目 = `(自分のVODを選択)` + 自分の VOD 20 件 (現行と同じ `list_own_videos`)
  * 既存コードが触っている `self.url_edit` は `url_combo.lineEdit()` を指す属性として残す
    (`_on_analyze` / `_set_running` の変更を最小にする)
  * 一覧を入れた直後は `setCurrentIndex(-1)` + `clearEditText()` で**空に戻す**
    (編集できるコンボは項目を入れると先頭の文字が本文へ入ってしまう)
  * 項目を選んだら URL を本文へ入れ、`_select_vod(url, title)` を呼ぶ
* ログインボタンの文言・ワーカー (`TwitchLoginWorker` / `TwitchRestoreWorker`)・
  トークンの扱いは**変えない**。`_refresh_login_button()` を
  `_refresh_twitch_row()` に広げ、「どちらを出すか」までを 1 か所で決める。

### 2.4 ドラッグ&ドロップの受理 (アーカイブ用タブに新設)

`setAcceptDrops(True)` + `dragEnterEvent` / `dragLeaveEvent` / `dropEvent` を
クリップ用タブと同じ形で持つ。判定の順番が要点:

1. `timeline.project_suffix` (`.timeline.json`) で終わる → **プロジェクト**
2. 動画の拡張子 (`.mp4 .mov .avi .mkv .flv .wmv`) → **動画**
3. それ以外の `.json` → **コメント JSON** (動画が未選択なら補足行で「動画も選んでください」と促す)
4. どれでもない → `event.ignore()`

* プロジェクトは `confirm_project_kind(self, path, KIND_ARCHIVE)` を通し、
  種別違いなら `switch_tab_requested` で相手のタブへ回す (現行の約束どおり)。
* 実行中はドロップを受け付けない (クリップ用と同じ)。

### 2.5 共有化 (同じコードを 2 か所に置かない)

`archive_tab.py` から `main_window.py` は import できない (循環する) ため、
クリップ用タブが持っている 3 つを**共有できる場所へ移す**。移すだけで挙動は変えない。

| 今の置き場 | 移す先 | 呼び出し元 |
| --- | --- | --- |
| `main_window._centered(widget)` | `theme.centered_row(*widgets)` | 両タブ |
| `ClipTabWidget._apply_drop_zone_height()` の計算 | `file_drop_area.apply_zone_height(host, drop_area, settings)` | 両タブの `resizeEvent` |
| `main_window._VIDEO_EXTENSIONS` | `file_drop_area.VIDEO_EXTENSIONS` | 両タブ |

* `main_window` 側は**名前を残して委譲**する (`_centered` / `_VIDEO_EXTENSIONS` /
  `_apply_drop_zone_height` はそのまま使える)。既存テストと既存の呼び出しを壊さない。

---

## 3. 変更する関数 (`src/gui/archive_tab.py`)

| 関数 | 変更 |
| --- | --- |
| `_build_ui` | 上の §1 の並びに作り替える。説明ラベル・入力ソース・2 つのグループ・`ProjectResumeRow` を落とし、`FileDropArea`・Twitch 行・中央 2 ボタン・最下段オプション行を作る |
| `resizeEvent` (新) | `file_drop_area.apply_zone_height` を呼ぶ |
| `_select_video` / `_select_vod` / `_select_chat` / `select_project` / `_clear_selection` (新) | §2.1 の選択を 1 か所で作る。`select_project` はタブ共通の受け口 (`MainWindow._switch_to_kind` が呼ぶ) なので**公開名のまま維持** |
| `dragEnterEvent` / `dragLeaveEvent` / `dropEvent` / `_is_acceptable_drop` / `_dropped_*` (新) | §2.4 |
| `_on_run` (旧 `_on_analyze` から改名) | §2.2 の分岐。採点ワーカーの起動部分は現行のコードをそのまま使う |
| `_current_mode` | 選択の種別から導く |
| `_refresh_twitch_row` (旧 `_refresh_login_button`) | §2.3 の表の判断を 1 か所に集める |
| `_restore_session` / `_on_restored` / `_on_restore_failed` / `_on_login*` | `login_status` への代入を、状態ラベル (`status_label`) とツールチップへ置き換える |
| `_fill_own_vods` | `url_combo` へ入れる (§2.3 の「空に戻す」処理を追加) |
| `_open_library` / `_on_library_open` | 選ぶところまでにする (再開は実行ボタンから) |
| `_set_running` | 対象を差し替える (`run_button` / `cancel_button` の表示切替 / `url_combo` / `drop_area.set_busy` / `resume_button`)。`resume_row` 参照は無くす |
| `retranslate` | 無くなった部品の貼り替えを落とし、D&D 領域・2 ボタン・オプション行の文言を貼り替える |
| `refresh_theme` | `run_button` のアイコン + `drop_area.refresh_theme()` |

**残す名前**: `select_project` (タブ共通の受け口)、`running_changed` /
`switch_tab_requested` (親が繋ぐシグナル)、`_set_running` の通知。
`analyze_button` → `run_button` へ改名する (参照は `archive_tab.py` 内だけ。
クリップ用タブと同じ名前にして、両タブを同じ目で読めるようにする)。

---

## 4. 設定 (`setting.json`)

**新しいキーは追加しない。** 使うのは既存の 3 つ。

| キー | 用途 |
| --- | --- |
| `ui.main_window.drop_zone_ratio` (0.60) | D&D 領域の高さの比率 (両タブ共通) |
| `ui.main_window.drop_zone_min_height_px` (140) | 縮めたときの下限 |
| `ui.main_window.show_file_row` (false) | 隠した「入力パス + 参照...」「コメントJSON + 参照...」を出すか |

* アーカイブ用タブは部品が 2 行多い (Twitch 行・オプション行) が、
  `apply_zone_height` が**残りの部品の最小高を引いてから**比率を当てるため、
  同じキーで「実行ボタンが画面外へ出ない」が保たれる (resolve15 §3-4 / Q3)。
* `archive.auth.owner_only` (既存) は §2.3 の URL 欄の出し分けに読むだけ。書き換えない。

## 5. 文言 (i18n)

`src/i18n/catalog_en.py` へ追加する主なもの (日本語キー → 英語):

| 日本語 | 英語 |
| --- | --- |
| `配信動画 / コメントJSON / 保存したアーカイブ用プロジェクト` | `Stream video / chat JSON / saved archive project` |
| `実行すると採点からはじまります` | `Running starts from scoring` |
| `実行すると VOD の取得と採点からはじまります` | `Running downloads the VOD, then scores it` |
| `VOD の URL を貼り付け (https://www.twitch.tv/videos/…)` | `Paste a VOD URL (https://www.twitch.tv/videos/…)` |
| `コメント採点: {name}` | `Chat scoring: {name}` |
| `動画をドラッグ&ドロップするか、URL を貼るか、「続きから」でプロジェクトを選んでください。` | `Drop a video, paste a URL, or pick a project with "Resume".` |
| `コメントJSON を選びました (動画も選んでください)` | `Chat JSON selected (now choose a video)` |
| `Twitch: {name} のアーカイブから選べます` | `Twitch: pick from the archive of {name}` |

既にある `ドラッグ＆ドロップでファイルを選択` / `実行すると編集の続きから書き出します` /
`続きから` / `実行` / `(自分のVODを選択)` / `中止` / `無音カット` /
`取得データを削除` はそのまま使う。

## 6. テスト

| ファイル | 変更 |
| --- | --- |
| `tests/test_archive_login_restore.py` (既存) | `login_status.text()` を見ている 4 件を**部品の出し分け**の検査へ書き替える: 確認中 = ログインボタンが出て押せない・URL 欄は隠れている / 復元成功 = URL 欄が出てログインボタンが隠れ、一覧に VOD 2 件 / 失敗 = URL 欄が隠れてログインボタンが押せる / 未ログイン = 問い合わせに行かない |
| `tests/test_archive_tab_layout.py` (新規) | クリップ用の `test_clip_tab_selection.py` と同じ観点で: D&D 領域の高さ (比率・下限) / 「続きから」と実行ボタンの幅が 96px / 実行ボタンが D&D 領域より下にある / 動画のドロップ → 名前が出て × で戻る / `*.timeline.json` (アーカイブ種別) のドロップ → プロジェクト選択 / 種別違い → `switch_tab_requested` が飛ぶ / その他の `.json` → コメント添付 / URL 入力 → vod 選択になり × で URL 欄も空になる / 中止ボタンは待機中は隠れている / `mode_combo`・`login_status`・`resume_row` が無い / 何も選ばずに実行 → 案内のみでワーカーを起こさない |
| `tests/test_clip_tab_selection.py` (既存) | **変更しない** (共有化は委譲のため挙動が変わらないことを、このテストが通ることで示す) |

実行: `python -m unittest discover -s tests`

## 7. 文書

| 文書 | 更新内容 |
| --- | --- |
| `docs/layout/main_window.html` | アーカイブ用タブの枠を新しい並びへ描き替える (現在は旧構成のまま) |
| `docs/design/design.md` (+ `design.html`) | 画面構成の節で、アーカイブ用タブが D&D ベースになったことと選択の 1 本化を反映 |
| `docs/README.md` | ver12 の行へ `request3` / `resolve3` を追記 |

## 8. 影響範囲 (壊さないことの確認)

| 触らないもの | 理由 |
| --- | --- |
| `src/archive/*` (採点・取得・切り抜き・再開) | ワーカーへ渡す `job` と設定の形を変えないため |
| `ArchiveAnalyzeWorker` / `ArchiveClipWorker` / `ArchiveResumeWorker` | 起動の仕方も引数も現行どおり |
| `ArchiveResultBridge` / `MediaRelinkBridge` / `WatermarkConfirmBridge` | 生成箇所と引数は変えない |
| `ProjectResumeRow` (部品そのもの) | アーカイブ用タブから使わなくなるだけ。部品は残す (他から使う / 設定で戻す余地) |
| `MainWindow` の繋ぎ (`running_changed` / `switch_tab_requested` / ドロップの転送) | シグナル名と `select_project` を保つため無変更 |
| `archive.enabled = false` のときの画面 (`_build_disabled_ui`) | 無変更 |

## 9. 残る判断 (実装前に確認したい点)

| # | 論点 | 本書の案 | もう一つの案 |
| --- | --- | --- | --- |
| **Q1** | 要望 B は「ステータス表示の削除」なので、**ログインボタンはタブに残す**と解釈した | 未ログインのときだけ D&D 領域の上に出す (ログインの入口が他に無いため) | 設定画面 (アカウント系) へログインを移し、タブからは完全に消す |
| **Q2** | 自分の VOD 一覧 | URL 欄 (編集できるプルダウン) に畳む | 一覧を廃止し、素の `QLineEdit` にする (URL は毎回貼る) |
| **Q3** | 無音カット・取得データの使用量/削除 | タブ最下段のオプション行に残す | 設定画面へ移し、タブはクリップ用と完全に同じ部品だけにする |
| **Q4** | 実行ボタンのツールチップ | クリップ用と同じ `実行` | `採点開始` のまま (ただし再開でも押すボタンになる) |

Q1〜Q4 は**どれも本書の案で実装できる**。別案を採るときは §1.3 と §3 の該当行だけが変わる。
