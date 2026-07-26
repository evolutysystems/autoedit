# resolve19 — アーカイブ切り抜き専用「テーマ・イントロカード」機能 要望設計書

## 0. 本書の位置づけ
`docs/request/ver2/request19.md` に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要ライブラリを追加しない／後方互換を失わない／不明点は推測実装せず設計書へ記載する）に従い、本書レビュー後に実装します。本機能は **「アーカイブ切り抜き用」のみに適用**し、通常のクリップ用フロー（`main_window` → `run_pipeline`）には一切影響を与えません。

---

## 1. 要望（request19.md）

字幕一覧画面（`SubtitleEditorDialog`）に **テーマ入力欄**を追加し、アーカイブ切り抜きのクリップに「イントロカード（導入演出）」を付ける。

1. 字幕一覧画面の**上部にテキストボックス**を追加（placeholder =「(任意)テーマを決めてください」）。
2. **未入力なら何もしない**（従来どおりのクリップ出力）。
3. **入力があれば**、そのクリップに対して以下を行う：
   - **クリップ開始前の 1.5 秒分をバッファとして戻す**（＝切り抜きで削られた直前 1.5 秒を復元する）。
   - その **1.5 秒間**、次の演出を重ねる：
     - **画面全体をブラー**する。
     - **横幅いっぱい・縦は上下にそれぞれ 300px の余白を残した黒色要素**を、ブラーの上に乗せる。
     - **画面中央にテーマ文字列**を表示する。
     - **1.5 秒経過でブラー・黒色要素・中央テキストが消える**。
   - テーマ文字列は、**そのクリップの間じゅう左上に小さく**表示し続ける。

---

## 2. 現状分析（関連実装）

| 対象 | 現状 | 本要望との関係 |
|---|---|---|
| `src/gui/subtitle_editor_dialog.py` `SubtitleEditorDialog` | 字幕一覧の編集ダイアログ。**通常クリップ用とアーカイブ用で共有**（`SubtitleReviewBridge` 経由）。上部にテキストボックスは無い | ここに**テーマ欄を追加**するが、**アーカイブ経路でのみ表示**する必要がある（共有クラスのため後方互換に注意） |
| `src/gui/main_window.py` `SubtitleReviewBridge` | ワーカー→メインスレッドで編集ダイアログを開く橋渡し。`__call__(items)` が編集結果 `list`／`None` を返す（`run_pipeline` の `subtitle_review_callback` 契約） | テーマ文字列を**呼び出し側（clip_writer）へ戻す**経路が無い。契約（戻り値=list/None）は変えたくない |
| `src/pipeline/pipeline_runner.py` `run_pipeline` | 音量→無音カット→フルテロップ＋**編集画面**→OP/ED→出力。共有の中核 | **改変しない**。テーマ演出は archive 側の後処理で行う |
| `src/modules/subtitle_generator.py` `_review_timeline` / `burn_subtitle` | 編集画面フックの呼び出しと ASS 焼き込み。編集は**このクリップを既に切り出した後**に行われる | テーマ入力は**切り出し後**に判明するため、「開始前 1.5 秒の復元」は**元 VOD から別途切り出す**必要がある（§4.1 参照） |
| `src/archive/clip_writer.py` `write_clips` / `_cut_region` / `_build_combine_parts` | 各クリップを `_cut_region`→`run_pipeline`→一時領域に集約→`concat` で 1 本に結合。`input_path`（元 VOD）と `clip["start"]` を保持 | **元 VOD と元 start を持つ唯一の場所**。イントロ 1.5 秒の切り出し・演出・結合はここで行うのが自然（archive 限定を保てる） |
| `src/archive/config.py` | `archive` セクションの設定読み出し | テーマ演出用の設定（`archive.intro_card`）を追加する窓口 |

### 2.1 設計上の最重要ポイント（順序の問題）
テーマ欄は **字幕一覧画面**（=`run_pipeline` 内・クリップ切り出し後）で入力される。一方「開始前 1.5 秒の復元」は **元 VOD からの切り出し**であり、時系列上は run_pipeline より前の操作。この“ニワトリと卵”を解くため、本設計では以下を採る：

- **1.5 秒バッファは run_pipeline に含めない**（無音カット・文字起こしを通さない）。ブラーで覆う導入素材のため、字幕対象にすると採録字幕が混入して不適切。
- テーマは **`run_pipeline` 完了後**に `clip_writer` が受け取り、**元 VOD の `[start-1.5s, start]`** を別途切り出して演出し、**本編クリップの前に連結**する。→ 既存の共有パイプラインを一切変えずに実現できる（§3-1）。

---

## 3. 設計方針（全体）

1. **共有パイプラインは不変**。テーマ演出は **archive 配下（`clip_writer` / `config` / `subtitle_editor_dialog` の追加表示 / `SubtitleReviewBridge` のテーマ受け渡し）** に閉じる。`run_pipeline`・`subtitle_generator`・`concat_processor` のロジックは変更しない。
2. **テーマ欄は archive 経路でのみ表示**。`SubtitleEditorDialog` に `show_theme_field=False`（既定）を追加し、archive の `SubtitleReviewBridge` からのみ `True` を渡す。通常クリップ用は欄を出さず**従来と完全同一**。
3. **テーマの戻し方**は callback 契約を壊さない。`SubtitleReviewBridge.__call__(items)` は従来どおり編集結果 `list`／`None` を返しつつ、**入力テーマを内部属性へ保存**する。`clip_writer` は run_pipeline 完了後に `bridge.consume_theme()` で**そのクリップのテーマ**を取り出す（処理は 1 クリップずつ直列のため取り違えなし）。
4. **未入力＝何もしない**。テーマ空文字なら §4.1 の全演出をスキップし、出力は現行どおり（バッファ復元もタグも無し）。
5. **イントロ 1.5 秒の生成**は元 VOD から `[max(0,start-buffer), start]` を切り出し、`ffmpeg -vf` で「ブラー → 黒帯（全幅・上下 300px 余白）→ 中央テーマ」を焼いた `intro.mp4` を作る（§4.4）。長さは設定 `buffer_sec`（既定 1.5）。
6. **左上タグ**は本編クリップ（run_pipeline 出力）全長へ小さく焼く（§4.5）。
7. **結合**は既存 `concat_processor` を流用し、`parts = [OP?] + (intro_i? + clip_i) × K + [ED?]` の順に 1 本へ（§4.6）。イントロ・本編・OP/ED の解像度差は concat の正規化（`ffmpeg.output_width/height/fps`）で吸収。
8. **設定は setting.json 管理**（ハードコード禁止）。`archive.intro_card` を新設し、バッファ秒数・ブラー強度・余白 px・色・フォントサイズ等を可変にする（§5）。既定は要望値（1.5 秒 / 上下 300px）。
9. **後方互換**：`archive.intro_card` 未定義でも `DEFAULT_SETTINGS` のマージで既定補完。テーマ未入力時・非 archive 経路は挙動不変。

---

## 4. 詳細設計

### 4.1 変更後のアーカイブ・クリップ処理フロー（1 クリップぶん）

```
[アーカイブタブ] 採点 → TOP5 選択 → 「完了」
  clip_writer.write_clips: 各クリップ n について
    ① _cut_region(元VOD, start, end) → raw.mp4              （現行どおり／バッファは含めない）
    ② run_pipeline(raw, clip設定, subtitle_review_callback=bridge, volume_analysis_callback=None)
         └ 内部: 無音カット → 文字起こし → 字幕一覧画面（★テーマ欄を表示）→ ASS 焼き込み → out.mp4
         └ bridge が「字幕決定」時に入力テーマを内部保存
    ③ theme = bridge.consume_theme()                        （このクリップのテーマ／空なら演出なし）
    ④ theme が空でない場合のみ:
        (a) intro.mp4 = 元VODの [max(0,start-buffer_sec), start] を切り出し、
              ブラー + 黒帯(全幅・上下300px) + 中央テーマ を焼く          （§4.4）
        (b) out_tagged.mp4 = out.mp4 の全長に「左上・小テーマタグ」を焼く   （§4.5）
        → clip_parts_n = [intro.mp4, out_tagged.mp4]
       theme が空の場合:
        → clip_parts_n = [out.mp4]                          （＝現行と同一）
  全クリップ後:
    parts = [Opening?] + clip_parts_1 + clip_parts_2 + … + [Ending?]     （§4.6）
    concat_processor.concat(parts, final)  → archive_<元名>_combined.mp4
```

- テーマ未入力クリップは **intro もタグも付かず現行と同一**。混在（あるクリップだけテーマ有り）も自然に扱える。
- イントロは**元 VOD の実素材（ブラー済み）**であり、無音カット・字幕は通さない（§2.1）。
- 「開始前 1.5 秒」は**元 start 基準**（無音カット後の start ではなく、TOP5 で確定した元区間の start）。素材先頭に近く `start-buffer_sec < 0` の場合は 0 でクランプし、確保できた分だけ（≤1.5秒）演出する（§7-4）。

### 4.2 `SubtitleEditorDialog` へのテーマ欄追加（archive 経路のみ表示）

```
# __init__ に追加（既定 False で後方互換）
def __init__(self, items, parent=None, default_font="", default_size=None,
             font_families=None, show_theme_field=False, theme_placeholder="", theme_text=""):
    ...
    self._show_theme_field = bool(show_theme_field)
```

- `_build_ui` の**説明ラベル直後**に、`show_theme_field=True` のときだけ 1 行の `QLineEdit`（`self.theme_edit`）を配置する。
  - placeholder は **request19 指定の固定文言**「(任意)テーマを決めてください」。設定項目にはせず、コード内定数として持ち、呼び出し側から `theme_placeholder` で渡す（archive 経路のみ）。
  - `show_theme_field=False`（通常クリップ用の既定）では欄自体を生成せず、**レイアウト・戻り値ともに従来と完全同一**。
- 取得メソッド `theme_value()` を追加：欄が無ければ `""`、有れば `self.theme_edit.text().strip()` を返す。
- `result_items()` は**変更しない**（字幕行の戻り値契約は不変）。テーマは別メソッドで取得する。

### 4.3 `SubtitleReviewBridge` によるテーマの受け渡し（契約非破壊）

```
class SubtitleReviewBridge(QObject):
    def __init__(self, ..., show_theme_field=False, theme_placeholder=""):
        ...
        self._show_theme_field = show_theme_field
        self._theme_placeholder = theme_placeholder
        self._last_theme = ""            # 直近クリップで入力されたテーマ

    def _on_review_requested(self, items):
        dialog = SubtitleEditorDialog(items, ...,
                    show_theme_field=self._show_theme_field,
                    theme_placeholder=self._theme_placeholder)
        if dialog.exec() == Accepted:
            self._result = dialog.result_items()
            self._last_theme = dialog.theme_value()     # ★テーマを保存
        else:
            self._result = None
            self._last_theme = ""                       # キャンセル時はテーマ無効
        self._event.set()

    def consume_theme(self):
        """直近クリップのテーマを返して空にする（1回消費）。取り違え防止に消費式にする。"""
        t = self._last_theme
        self._last_theme = ""
        return t
```

- `__call__(items)` の戻り値契約（`list`／`None`）は**不変**＝`run_pipeline` からは従来どおり見える。
- **通常クリップ用**（`main_window`）は `show_theme_field` を渡さない（既定 False）ため、テーマ欄も `consume_theme` 利用も無く**完全に従来どおり**。
- run_pipeline は **1 クリップずつ同期実行**（resolve18 §4.1）のため、`consume_theme()` は必ず「直前に処理したクリップ」の値を返す（並行なし）。

### 4.4 イントロカード生成（1.5 秒・元 VOD から）

元 VOD の `[max(0,start-buffer_sec), start]` を切り出し、1 パスの `ffmpeg -vf` で演出を焼く。ffmpeg 実行は既存 `ffmpeg_runner`（`get_ffmpeg_exe` / `build_encode_options` / `execute`）を再利用する。

```
# フィルタ構成（値は archive.intro_card 設定から。W,H = ffmpeg.output_width/height）
scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2,setsar=1,   # 出力キャンバスへ正規化
gblur=sigma={blur_sigma},                                                              # 画面全体ブラー
drawbox=x=0:y={margin_top_px}:w=iw:h=ih-{margin_top_px}-{margin_bottom_px}:color={box_color}@{box_opacity}:t=fill,  # 全幅・上下余白の黒帯
ass='intro_card.ass'                                                                    # 中央テーマ（libass=日本語/フォント安定）
```

- **中央テーマ文字列**は `drawtext` ではなく **一時 ASS（`\an5` 中央・大サイズ）＋ `ass` フィルタ**で焼く。理由：既存のフォント運用（`fonts_dir`／`subtitle.font_family`）と整合し、日本語・改行・縁取りを安定描画できる（`burn_subtitle` と同じ思想）。**フォント種別・サイズ・色は `archive.intro_card.title_*`**（`title_font_family` は空文字なら `subtitle.font_family` を使用）。フォント解決のため `ass` フィルタへ `fontsdir`（`resolve_fonts_dir`）を付与する。
- 黒帯は `drawbox`（全幅 `w=iw`、`y=margin_top_px`、`h=ih-上-下`）。既定 300/300px は 1080p で高さ 480px の帯。
- 尺は実切り出し長（`start<buffer_sec` の端では 1.5 秒未満）。音声は元 VOD の該当 1.5 秒をそのまま採用（ブラー映像に合わせ違和感なし。無音化オプションは §7-5）。
- 出力は一時領域（最終結合まで出力先を汚さない／resolve18 と同一方針）。

> **再現イメージ（本設計の実フィルタで f1.mp4 のフレームを加工）**：`docs/request/ver2/images/resolve19_intro_card.png`
> ブラー済み全画面の上に、上下 300px を残した黒帯と中央テーマが乗る。

### 4.5 左上テーマタグ（本編クリップ全長）

`run_pipeline` 出力 `out.mp4` の**全長**に、左上へ小さくテーマを焼く。生成は一時 ASS（`\an7` 左上・小サイズ・半透明背景）＋ `ass` フィルタの 1 パス。

```
# タグ用 ASS（例）
Fontname={tag_font_family or subtitle.font_family}, Fontsize={tag_font_size}, PrimaryColour={tag_color},
Alignment=7（左上）, MarginL={tag_margin_l}, MarginV={tag_margin_v}, BackColour で半透明背景（可読性確保）
Dialogue 開始=0:00:00.00, 終了=<out.mp4 の実尺（ffprobe で取得）>
```

- **フォントは `tag_font_family`**（空文字なら `subtitle.font_family`）で指定でき、`ass` フィルタへ `fontsdir` を付与して追加フォントも解決する。
- タグ尺は `ffmpeg_runner.probe_duration(out.mp4)` で本編全長に一致させる（無音カット後の実尺）。
- 本編の再エンコードが 1 回増える（`out.mp4`→`out_tagged.mp4`）。トレードオフは §7-3。

> **再現イメージ**：`docs/request/ver2/images/resolve19_clip_tag.png`（左上に小テーマタグ、本編は素の映像）。1.5 秒経過後は中央カード/黒帯/ブラーが消え、この状態がクリップ終端まで続く。

### 4.6 結合（イントロ＋本編を時系列で 1 本に）

resolve18 の結合を拡張し、クリップごとに **[intro?] + [本編]** を並べる。

```
parts = []
if OP有効かつ素材有: parts.append(opening)
for clip in used:
    if clip.theme: parts += [intro_clip.mp4, clip_tagged.mp4]
    else:          parts += [clip_out.mp4]
if ED有効かつ素材有: parts.append(ending)
concat_processor.concat(parts, final, ffmpeg_cfg)   # 全 part を出力プロファイルへ正規化
```

- `concat_processor.concat` が全 part を `ffmpeg.output_width/height/fps/audio_sample_rate` へ正規化するため、intro（元 VOD 解像度由来）と本編（正規化済み）の差は吸収される。
- OP/ED は resolve18 どおり**結合 1 本に 1 回だけ**（`archive.combine.opening_ending` ＋ general 設定）。

### 4.7 変更対象ファイル一覧（予定）

| ファイル | 変更概要 |
|---|---|
| `src/gui/subtitle_editor_dialog.py` | `__init__` に `show_theme_field`/`theme_placeholder`/`theme_text` を追加（既定で従来同一）。`show_theme_field=True` 時のみ上部に `QLineEdit` を配置。`theme_value()` 追加。`result_items()` は不変 |
| `src/gui/main_window.py` | `SubtitleReviewBridge` に `show_theme_field`/`theme_placeholder`/`_last_theme`/`consume_theme()` を追加。**通常クリップ用の生成箇所は引数を渡さない**（挙動不変） |
| `src/gui/archive_tab.py` | `SubtitleReviewBridge` 生成時に `show_theme_field=True` と placeholder（固定文言の定数）を渡す |
| `src/archive/clip_writer.py` | `_process_clips` で run_pipeline 後に `bridge.consume_theme()` を取得。テーマ有りクリップは §4.4/§4.5 の intro/tag を生成し `clip_parts` を組む。`_build_combine_parts` を intro 挿入対応に拡張。intro/tag 生成関数を追加（`ffmpeg_runner` 再利用） |
| `src/archive/config.py` | `intro_card_config(settings)` を追加（`archive.intro_card` を安全に既定フォールバックで返す） |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["archive"]` に `intro_card` を追加（`_merge_with_defaults` が欠落補完＝後方互換）。**設定画面に「アーカイブ」タブを新設し、`intro_card` の全項目を編集 UI として露出**（`_build_archive_tab` / `_load_to_ui` / `_collect_settings`。フォントは「(字幕フォントに従う)＝空」を含む任意フォントコンボ、秒数/不透明度は小数入力、色は HTML カラーピッカー） |
| `src/settings/setting.json` | `archive.intro_card` 既定値を追加 |

> 設定画面（`settings_window` の UI）へテーマ演出項目を**露出済み**（「アーカイブ」タブ）。`_collect_settings` は `archive` の他キー（download/scoring/output/clip_pipeline/combine）を base から引き継ぎ、`intro_card` サブ辞書のみ UI 値で差し替える。

---

## 5. setting.json 追加案（追加のみ・既定は要望値）

`archive` に `intro_card` を新設する。テーマ入力時のみ発火するが、演出パラメータをハードコードしないための可変設定。

```jsonc
"archive": {
  // …既存(enabled/download/scoring/output/clip_pipeline/combine)は不変…
  "intro_card": {
    "enabled": true,               // 機能全体のスイッチ（false で完全に無効＝テーマ欄も出さない）
    "buffer_sec": 1.5,             // 開始前に戻すバッファ秒数（要望: 1.5）
    "blur_sigma": 18,              // 画面全体ブラーの強さ（gblur sigma）
    "margin_top_px": 300,          // 黒帯の上余白（要望: 300）
    "margin_bottom_px": 300,       // 黒帯の下余白（要望: 300）
    "box_color": "black",          // 黒色要素の色
    "box_opacity": 1.0,            // 黒色要素の不透明度（1.0=完全不透明）
    "title_font_family": "",       // 中央テーマのフォント（空文字=subtitle.font_family を使用）
    "title_font_size": 112,        // 中央テーマの文字サイズ（1080p 目安）
    "title_color": "#FFFFFF",      // 中央テーマの文字色
    "tag_font_family": "",         // 左上タグのフォント（空文字=subtitle.font_family を使用）
    "tag_font_size": 40,           // 左上タグの文字サイズ
    "tag_color": "#FFFFFF",        // 左上タグの文字色
    "tag_bg_opacity": 0.45,        // 左上タグ背景の不透明度（可読性用・0で無効）
    "tag_margin_l": 40,            // 左上タグの左マージン
    "tag_margin_v": 30,            // 左上タグの上マージン
    "mute_intro": false            // イントロ1.5秒の音声を無音化するか（§7-5）
  }
}
```

- **フォントは中央テーマ・左上タグそれぞれで変更可能**（`title_font_family` / `tag_font_family`）。**空文字なら `subtitle.font_family` を既定として使用**する（＝従来どおり）。指定名は追加フォント（`fonts_dir`）も含めて解決するため、`burn_subtitle` と同様に `ass` フィルタへ `fontsdir` を付与する（未導入フォント指定時は libass の既定へフォールバック）。`title_*`/`tag_*` はフォント種別・サイズ・色を上書きできる。
- **テーマ欄のプレースホルダは固定文言**「(任意)テーマを決めてください」（request19 指定）とし、設定項目にはしない（コード内定数）。
- `enabled=false` で本機能を完全停止（テーマ欄も非表示）。テーマを入力しても空扱いにはせず、欄自体を出さない。

---

## 6. エラー処理・フォールバック方針
- **テーマ空文字** → 全演出スキップ（バッファ復元・intro・タグを行わず現行出力）。
- **編集画面キャンセル**（`PipelineCancelled`）→ resolve18 §4.4 どおり当該クリップのみスキップ。テーマも `consume_theme` で空を返す（キャンセル時 `_last_theme=""`）。
- **intro 切り出し失敗**（`start-buffer_sec` 付近の端・元 VOD 読み取り失敗）→ intro をスキップし**本編＋タグのみ**で継続（クリップ全損を避ける）。`start<=0` 相当で確保 0 秒なら intro 無し。
- **intro/tag の ffmpeg 失敗** → 該当演出をスキップし素の本編で継続。ログに WARNING。演出失敗で結合自体は止めない。
- 例外集約は既存 `AutoEditError` 系・`ArchiveClipWorker.failed` を流用。

## 7. 確認事項（レビューで確定したい点）

| # | 項目 | 既定（推奨） | 補足 |
|---|---|---|---|
| 1 | 中央テーマの描画方式 | **一時 ASS＋ass フィルタ**（drawtext でなく） | 既存フォント運用（fonts_dir/font_family）と整合。drawtext 希望なら変更可 |
| 2 | 左上タグの表示範囲 | **本編クリップ全長**（intro 1.5 秒には出さない／intro は中央テーマのみ） | 「クリップの間」を本編と解釈。intro からも出すなら intro 側 ASS にも追加 |
| 3 | 本編タグの再エンコード | **後処理で 1 パス追加**（out.mp4→out_tagged.mp4） | archive 限定を保つ代償。共有 `subtitle_generator` にテーマ焼きを載せれば 1 パス削減できるが、shared 改変が必要 |
| 4 | 素材端で 1.5 秒未満しか戻せない場合 | **確保できた分だけ演出**（0 秒なら intro 無し） | 常に 1.5 秒確保したい等の要望あれば要検討 |
| 5 | イントロ 1.5 秒の音声 | **元 VOD の音声をそのまま**（`mute_intro=false`） | ブラー映像に実音が乗る。無音化したい場合 `mute_intro=true` |
| 6 | ブラー強度・黒帯不透明度・中央/タグの**フォント種別**・サイズ・色 | 既定値（§5）。フォントは空文字で `subtitle.font_family` を使用 | `title_font_family`/`tag_font_family` で個別変更可。プレビューで微調整想定（1080p 前提の目安値） |
| 7 | 縦動画クリップ | 横キャンバス正規化に従う（resolve18 §8-5 と同様） | 300px 余白は 1080p 基準。縦出力時は px の見え方が変わる点を仕様として明記 |
| 8 | テーマの既定・記憶 | 空（毎回入力） | 直前テーマを初期表示する等の要望あれば `theme_text` で対応可 |

## 8. 影響範囲・後方互換
- **共有パイプライン（`run_pipeline`／`subtitle_generator`／`concat_processor`）は不変**。変更は archive 配下＋共有 GUI クラスへの**加算的**な引数追加のみ（既定で従来同一）。
- **通常クリップ用フロー不変**：`SubtitleEditorDialog`／`SubtitleReviewBridge` は `show_theme_field` 未指定で従来と完全同一（テーマ欄なし・consume_theme 未使用）。
- **新規依存なし**：ブラー（gblur）・drawbox・ass・concat はすべて既存 ffmpeg／libass の機能。
- `settings_window` UI に「アーカイブ」タブを新設し、`intro_card` の全項目を編集可能に露出済み（§4.7）。

---

## 9. 表示イメージ（f1.mp4 を実フィルタで加工した再現）

request19 の「f1.mp4 の一部を画像化し、どのような表示になるか再現」に対応。`f1.mp4`（2560×1440/60fps）から 1 フレームを抽出し **1920×1080（出力キャンバス）へ正規化**した上で、本設計の実フィルタ（ブラー／黒帯／中央テーマ／左上タグ）を適用した。サンプルのテーマ文字列は「**ベルギーGP 決勝ハイライト**」。

| フェーズ | 画像 | 内容 |
|---|---|---|
| ① 導入 0.0–1.5 秒（イントロカード） | `docs/request/ver2/images/resolve19_intro_card.png` | 画面全体ブラー＋上下 300px を残した全幅の黒帯＋中央にテーマ |
| ② 1.5 秒経過後〜クリップ終端（本編） | `docs/request/ver2/images/resolve19_clip_tag.png` | 黒帯・中央テーマ・ブラーが消え、左上に小さくテーマタグが残る |
| （参考）加工前フレーム | `docs/request/ver2/images/resolve19_base_frame.png` | 抽出元の 1920×1080 フレーム |

- 再現は実際に `ffmpeg -vf "gblur=sigma=18, drawbox=x=0:y=300:w=iw:h=ih-600:color=black@1.0:t=fill, drawtext(中央)"`（中央テキストは本番では ass フィルタに置換）で生成しており、**本番の見え方と概ね一致**する。

---

## 10. 段階実装（レビュー後）
1. `SubtitleEditorDialog` にテーマ欄（`show_theme_field`/`theme_value()`）を加算（既定で従来同一を確認）。
2. `SubtitleReviewBridge` にテーマ保存・`consume_theme()` を追加。archive の生成箇所で `show_theme_field=True`＋固定 placeholder を渡す。通常クリップ用は不変を確認。
3. `clip_writer`：run_pipeline 後に `consume_theme()`。テーマ有りで intro（§4.4）＋タグ（§4.5）生成、`_build_combine_parts` を intro 挿入対応に。失敗時フォールバック（§6）。
4. `config.intro_card_config` と `DEFAULT_SETTINGS["archive"]["intro_card"]`／`setting.json` を追加（§5）。
5. 検証：テーマ入力クリップで「開始前 1.5 秒バッファ＋ブラー＋上下300px黒帯＋中央テーマ（1.5秒）→ 消える → 左上タグが終端まで」を確認。未入力クリップは現行どおり。混在・素材端（<1.5秒）・キャンセル・OP/ED 付き結合順（時系列）を確認。

> 本機能はアーカイブ切り抜き専用の追加演出であり、リリース時は R1（v1.2.x）の追補として扱う。
