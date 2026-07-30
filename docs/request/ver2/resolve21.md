# resolve21 — Resolve 出力の字幕改善（タイミング修正／字幕トラック化／スタイル適用） 要望設計書

## 0. 本書の位置づけ
`docs/request/ver2/request21.md` に対する **設計書** です。resolve20（DaVinci Resolve プロジェクトファイル出力機能）で実装した FCPXML 出力に対する改善要望であり、CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要ライブラリを追加しない／後方互換を失わない／不明点は推測実装せず設計書へ記載する）に従い、本書レビュー後に実装します。

---

## 1. 要望（request21.md）

DaVinci Resolve のタイムラインへの取り込み（Import）は成功したが、以下の問題がある。

| # | 要望 | 補足 |
|---|---|---|
| P1 | **Text のタイミングがズレすぎている** | 字幕（title）がタイムライン上の正しい時刻に配置されない |
| P2 | **Text+ ではなく、字幕トラックに字幕として追加できないか** | Resolve のサブタイトル（字幕）トラックへ入れたい |
| P3 | **フォントのスタイルを字幕に適応したい** | setting.json の字幕スタイル（フォント/サイズ/色）を字幕トラック上の字幕へ反映したい |

---

## 2. 現状分析（原因の特定）

> 調査対象：`src/export/fcpxml_builder.py` / `src/export/resolve_export.py` / `src/export/config.py`（resolve20 実装）、FCPXML 仕様、Resolve マニュアル。行番号は調査時点。

### 2.1 P1: タイミングズレの主因 — **title のアンカリングが FCPXML 仕様外**

現行実装（`fcpxml_builder.py`）は title を次のように生成している：

- `build_fcpxml`（`:185-187`）… title を **`spine` の直下**へ追加している（`_append_title(spine, ...)`）。
- `_append_title`（`:205-213`）… `lane="1"` を付け、`offset` に**タイムライン時刻（連結後の秒）**をそのまま入れている（`resolve_export.py:159-170` の `base_offset + item.start` 由来）。

```xml
<spine>
  <asset-clip ref="r2" offset="300/60s" start="600/60s" duration="240/60s"/>  <!-- cutB -->
  <title lane="1" offset="310/60s" .../>   <!-- ← spine 直下・タイムライン時刻 (現行・仕様外) -->
</spine>
```

しかし **FCPXML の仕様**では、接続クリップ（connected clip / `lane` 付き要素）は：

1. **親となる本線クリップ（asset-clip）の「子要素」として入れ子にする**（spine 直下ではない）。
2. その `offset` は**親クリップのローカル時間＝ソース `start` 基準**で表す（タイムライン時刻ではない）。
   - 変換式：`接続要素の offset = 親の start + (タイムライン時刻 − 親の offset)`

つまり現行出力は「置く場所（spine 直下）」と「時間基準（タイムライン時刻）」の両方が仕様と異なり、Resolve の取り込み時の解釈は未定義（本線クリップ扱いになる／別の時間基準で解釈される等）。**ソースの in 点（`start`）が大きい後半のクリップほどズレが拡大**するため、「ズレすぎている」という症状と一致する。これが主因と判断する。

**例**：cutB（タイムライン `offset=300s`・ソース `start=600s`）上のタイムライン 310s の字幕は、正しくは cutB の子要素として `offset = 600 + (310 − 300) = 610s` と書く必要がある。現行の `310s` のままでは約 300 秒ズレて解釈され得る。

### 2.2 P1: 副次要因 — フレーム丸めの累積誤差

`build_fcpxml`（`:173-183`）はタイムライン位置 `cursor` を**丸め前の float 秒**で累積し、`offset`／`duration` を**個別に**最近傍フレームへ丸めている。このため

- 「クリップ N の offset」≠「クリップ 1〜N−1 の量子化済み duration の和」となり、クリップ間に **±1 フレームの隙間／重なり**が生じ得る。
- `format_duration`（`:51-61`）の「最低 1 フレーム」クランプも累積へ加算されない。

主因（§2.1）ほどのズレにはならないが、クリップ数が多いほど蓄積するため、**内部を整数フレームで管理する方式へ修正**する（§5.3）。

### 2.3 P2/P3: Resolve の字幕トラックの仕様（調査結果）

- Resolve は字幕を **`.srt` / `.vtt` / `.xml` / `.ttml`** 等から取り込め、`File > Import` または メディアプール右クリック「**Import Subtitle**」→タイムラインへ配置で**字幕トラック**に入る（Resolve 18 マニュアル）。
- Resolve は **FCPXML 内の `<caption>` 要素**（FCPXML 1.8+ のクローズドキャプション：ITT/CEA-608）もタイムライン取り込み時に**字幕トラックへ変換**できる（Rev/HappyScribe 各社の取り込みガイドが SRT と FCPXML の両対応を明記。ただし**対応の細部はバージョン依存のため実機確認とする**（§10-1））。
- 字幕トラックのスタイル（フォント/サイズ/色/位置）は **トラック単位**：字幕トラックヘッダー選択 → Inspector の **Track Style** で設定する。キャプション単体のスタイル上書きは Resolve 上で「Use Track Style」を外して行う。
- **含意**：字幕トラック方式では「役割別（配信者/サブ/コメント）の色分け」を per-字幕で完全再現することは保証できない（§5.5・§10-3）。フォント/サイズ等の**基本スタイルはトラックスタイルとして 1 回設定すれば全字幕へ一括適用**でき、これは P3 の「フォントのスタイルを字幕に適応したい」に対する Resolve 側の標準的な運用でもある。

### 2.4 現行実装の再利用資産

| 資産 | 再利用 |
|---|---|
| `resolve_export.build_clip_spec` / `build_archive_spec` | 編集点・字幕収集の枠組みはそのまま。title 生成部を caption 生成へ分岐拡張 |
| `_titles_from_items` / `_title_from_item`（`resolve_export.py:122-170`） | スタイル解決（item 優先→FontProfile 既定）のロジックを caption にも流用 |
| `fcpxml_builder.format_time` / `format_duration` / `hex_to_rgba` | 継続使用（内部フレーム管理化に伴い整理 §5.3） |
| `export_spec`（原子的書き出し・上書き確認） | SRT サイドカーの書き出しにも同方式を適用 |
| テーマ演出（`_theme_title`） | **字幕ではなく演出テキスト**のため Text+（title）のまま維持。ただし §5.2 のアンカリング修正を適用 |

---

## 3. 方式選定（P2: 字幕トラック化）

| 方式 | 字幕トラックに入るか | スタイル移送 | タイムラインとの一体性 | 判定 |
|---|---|---|---|---|
| **FCPXML `<caption>` 埋め込み（ITT）** | ○（取り込み時に字幕トラック化。※版依存・要実機確認） | △（ITT の text-style はフォント/色/太字/斜体など限定的。トラックスタイルが優先される可能性あり） | ◎（1 ファイルで編集点＋字幕） | **採用（第1候補）** |
| **SRT サイドカー併記** | ○（Import Subtitle で確実） | ✕（SRT はスタイル情報を持たない。スタイルは Resolve のトラックスタイルで設定） | △（別ファイル・手動 1 操作） | **採用（併記・フォールバック）** |
| Text+（title）継続 | ✕（ビデオトラックに載る） | ○ | ◎ | 設定で選択可として維持（後方互換） |
| Resolve Scripting API | ◎ | ◎ | ―（Resolve 常駐必須） | 対象外（resolve20 §10-6 と同判断） |

### 採用方針
- **既定＝FCPXML `<caption>`（ITT・言語 ja）で字幕トラックへ**。同時に **SRT サイドカーを併記出力**（設定で OFF 可）し、caption 取り込みが不調な環境でも「Import Subtitle」で確実に字幕トラックへ入れられる二段構えとする。
- 従来の **Text+（title）方式も設定で選択可**（`export.resolve.subtitle.mode`）。テーマ演出（中央テーマ/左上タグ）は字幕ではないため常に title のまま。
- **P1 のタイミング修正（§5.2/§5.3）は全方式に共通で適用**する。

---

## 4. 設計方針（全体）

1. **修正は `src/export/` に閉じる**。パイプライン・GUI のデータ受け渡し（resolve20 で構築済み）は不変。新規サードパーティ依存なし。
2. **FCPXML 仕様準拠のアンカリング**：接続要素（title/caption）は本線 asset-clip の子要素とし、offset は親ローカル時間へ変換する（§5.2）。
3. **時間は整数フレームで一元管理**：spec 構築後、builder 内部で全時刻をフレーム格子へ量子化してから累積し、丸め誤差を蓄積させない（§5.3）。
4. **字幕は caption（字幕トラック）を既定**とし、title（Text+）は設定で維持（§5.4）。
5. **スタイルは「トラックスタイル前提＋ベストエフォート移送」**：caption text-style へ FontProfile を可能な範囲で埋め込み、SRT 併記と Resolve 側トラックスタイル設定の運用手順を案内する（§5.5）。
6. **設定は setting.json 管理**：新設項目は `export.resolve.subtitle` 節に集約（§6）。既定値で新挙動、旧挙動（title のみ）へも戻せる。
7. **後方互換**：追加のみ。`_merge_with_defaults` による既定補完で旧 setting.json でも動作。既存テストは修正後の仕様に合わせて更新する。

---

## 5. 詳細設計

### 5.1 新規／変更ファイル一覧（予定）

| 種別 | ファイル | 内容 |
|---|---|---|
| 変更 | `src/export/fcpxml_builder.py` | **①アンカリング修正**（title/caption を asset-clip の子要素化・親ローカル時間変換）**②整数フレーム管理**（§5.3）**③ `<caption>` 生成対応**（§5.4） |
| 変更 | `src/export/resolve_export.py` | 字幕 mode 分岐（caption/title/both）、クリップ境界を跨ぐ字幕の分割（§5.2）、SRT サイドカー書き出し呼び出し |
| 新規 | `src/export/srt_writer.py` | SRT 文字列生成の純関数（items＋base_offset → SRT。`HH:MM:SS,mmm` 書式）。単体テスト可能に |
| 変更 | `src/export/config.py` | `export.resolve.subtitle` 節の平坦化（mode / caption_format / language / srt_sidecar） |
| 変更 | `src/settings/settings_window.py` | `DEFAULT_SETTINGS["export"]["resolve"]["subtitle"]` 追加（§6） |
| 変更 | `src/settings/setting.json` | 同上の既定値追加 |
| 変更 | `tests/test_fcpxml_builder.py` | アンカリング（入れ子・親ローカル時間）・整数フレーム累積・caption 生成の検証へ更新／追加 |
| 変更 | `tests/test_resolve_export.py` | mode 分岐・境界跨ぎ分割・SRT 生成の検証を追加 |

> GUI（`subtitle_editor_dialog.py` / `archive_result_window.py`）は変更不要（既存の完了メッセージに SRT パスを含める軽微修正のみ検討。エクスポート関数の戻り値へサイドカーのパスを足す）。

### 5.2 P1 修正①：接続要素のアンカリング（仕様準拠化）

**現行**（誤り）：`<spine>` 直下に `lane=1` の title を並べ、offset＝タイムライン時刻。

**修正後**：各字幕/テーマ text をタイムライン時刻から**担当する本線クリップを特定して入れ子化**し、offset を親ローカル時間へ変換する。

```
親クリップ i : タイムライン位置 offset_tl[i], ソース in 点 start_src[i], 尺 dur[i]
字幕 t0〜t1 (タイムライン時刻) が offset_tl[i] ≤ t0 < offset_tl[i]+dur[i] のとき
  → クリップ i の子要素として
     offset   = start_src[i] + (t0 − offset_tl[i])     … 親ローカル時間
     duration = min(t1, offset_tl[i]+dur[i]) − t0
```

```xml
<spine>
  <asset-clip ref="r2" name="cutB" offset="300/60s" start="600/60s" duration="240/60s">
    <title ref="r3" lane="1" offset="610/60s" duration="90/60s" name="字幕1">…</title>
    <caption role="iTT?captionFormat=ITT.ja" offset="610/60s" duration="90/60s" name="字幕1">…</caption>
  </asset-clip>
</spine>
```

- **クリップ境界を跨ぐ字幕**：`t1` が親クリップの終端を超える場合は**分割**し、残り（次クリップ相当分）を次クリップの子要素として続けて出力する（無音カット境界を跨いで発話が続くケース）。分割後の断片が **1 フレーム未満なら破棄**（DEBUG ログ）。
- **どのクリップにも載らない字幕**（タイムライン終端以降など、丸め誤差の端数）：最終クリップへクランプして配置し、WARNING を 1 回出す。
- テーマ演出 title（イントロ中央テーマ／左上タグ）も同じアンカリングで該当クリップの子要素にする（イントロ title はイントロクリップへ、タグ title は本編の各クリップへ跨ぎ分割）。

### 5.3 P1 修正②：整数フレーム管理（丸め誤差の非累積化）

builder 内部の時間管理を「float 秒」から「**整数フレーム**」へ変更する。

- spec 受領直後に全時刻（clips の start/duration、titles/captions の offset/duration）を `round(sec × fps)` でフレーム整数化する。
- タイムライン位置（cursor）は**量子化済み duration の累積（整数）**で計算する → クリップ間の隙間/重なりが構造的に発生しない。
- §5.2 の親特定・ローカル時間変換も整数フレームで行い、`format_time` は「フレーム整数 → `"N/fps s"`」の整形専用にする（既存の丸め二重適用を排除）。
- 字幕の t0/t1 も同じ格子上で量子化するため、resolve20 §10-4 の「speech_gate 等による端数」はフレーム精度で吸収される。

### 5.4 P2: `<caption>` 生成（字幕トラック化）

`export.resolve.subtitle.mode` により字幕 items の出力先を切り替える（テーマ演出は常に title）。

| mode | 出力 | 用途 |
|---|---|---|
| `"caption"`（**既定**） | `<caption>`（ITT）として本線クリップへアンカー → Resolve 取り込みで**字幕トラック** | P2 要望 |
| `"title"` | 従来どおり `<title>`（Text+ 相当）。ただし §5.2/§5.3 の修正適用 | 後方互換・Text+ で編集したい場合 |
| `"both"` | caption と title の両方を出力（Resolve 側で不要トラックを削除して使う） | 検証・移行用 |

**caption 要素の生成仕様**：

```xml
<caption role="iTT?captionFormat=ITT.ja" offset="610/60s" duration="90/60s" name="字幕1">
  <text placement="bottom">
    <text-style ref="ts1">こんにちは</text-style>
  </text>
  <text-style-def id="ts1">
    <text-style font="Yu Gothic UI" fontSize="130" fontColor="0.93 0 0.02 1" bold="1"/>
  </text-style-def>
</caption>
```

- `role`：`iTT?captionFormat=ITT.{language}`（language は設定・既定 `ja`）。ITT を採用する理由：CEA-608 は文字数・スタイル制約が強く日本語字幕に不向き。
- `placement`：FontProfile の `alignment`（ASS テンキー）から `bottom`／`top` へマッピング（1-3→bottom、7-9→top、中央系は bottom を既定）。ITT の placement は上下左右の 4 値のみで、px 単位の位置指定はできない（位置の詳細は Resolve のトラックスタイルで調整する運用）。
- 本文：ASS `\N` → 実改行。コメント役割の「コメント：」ラベル付与は現行（resolve20 §10-8）と同一。
- `lane` は付けない（caption は role でトラック分類される）。
- caption の text-style-def は **caption 要素内**に置く（title と同型）。

### 5.5 P3: フォントスタイルの適用

3 層のベストエフォートで適用する：

1. **caption text-style へ埋め込み**：`_title_from_item` と同じ解決順（item 個別 `font`/`font_size` → FontProfile 既定）で `font`/`fontSize`/`fontColor`（役割色）/`bold`/`italic`/`underline` を text-style に付与する。Resolve が取り込み時にどこまで反映するかは版依存（§10-2）。
2. **SRT サイドカー併記**（`srt_sidecar: true` 既定）：`{出力名}.srt` を FCPXML と同フォルダへ出力。時刻はタイムライン時刻（= §5.3 の量子化後の値をミリ秒へ変換）。スタイルは持たないため、Resolve 側で**トラックスタイル**（下記）を 1 回設定して適用する。
3. **運用案内（ドキュメント/ログ）**：Resolve では字幕トラックのスタイルは「字幕トラックヘッダー選択 → Inspector → Track Style」で一括設定する。Resolve 19 以降は字幕トラックのスタイルに **Text+ テンプレート**を指定でき、フォント表現の自由度が高い（要実機確認）。エクスポート完了ログに 1 回 INFO で案内を出す。

> **役割別の色分け（配信者/サブ/コメント）**：字幕トラックはトラック単位スタイルが基本のため、caption の per-item fontColor が無視される環境では色分けが失われる。実機確認（§10-3）の結果、色分け維持が必須なら「役割別に role（言語サブロール）を分けて複数字幕トラック化」を追加検討する（本書では確認事項に留め、推測実装しない）。

### 5.6 SRT サイドカー（`srt_writer.py`）

- 純関数 `build_srt(entries)`：`[{"start": 秒, "end": 秒, "text": str}, ...]` → SRT 文字列。連番は 1 起点、時刻書式 `HH:MM:SS,mmm`、テキストは実改行のまま。コメントラベル付与済みのテキストを受け取る（変換責務は resolve_export 側で caption と共通化）。
- `resolve_export` 側：mode に関わらず `srt_sidecar: true` なら出力（title mode でも SRT だけ字幕トラックに入れる使い方を許す）。書き出しは FCPXML と同じ「一時ファイル → rename」方式・上書き確認は FCPXML 側の確認と一体（同 stem のため 1 回のみ）。
- 出力パス：`default_output_path` の拡張子違い（`{prefix}_{stem}.srt`）。

### 5.7 エクスポート結果の返却

`export_spec` の戻り値を「出力パスの list（fcpxml＋srt）」へ拡張するか、`(fcpxml_path, srt_path|None)` のタプルとする。呼び出し側（両画面）の完了メッセージに SRT パスを併記する（表示のみの軽微修正）。

---

## 6. setting.json 追加案（**追加のみ**・既存キー不変）

`DEFAULT_SETTINGS["export"]["resolve"]` に `subtitle` 節を追加（`_merge_with_defaults` が欠落補完＝後方互換。`config.py` 側でも入れ子欠落を既定へフォールバック）。

```jsonc
"export": {
  "resolve": {
    // …既存キー (enabled/format/fcpxml_version/…) は不変…
    "subtitle": {
      "mode": "caption",        // "caption"(字幕トラック・既定) | "title"(Text+) | "both"
      "caption_format": "ITT",  // caption の書式 (実装は ITT のみ。将来 CEA-608)
      "language": "ja",         // caption ロールの言語コード (iTT?captionFormat=ITT.ja)
      "srt_sidecar": true       // SRT サイドカーを併せて出力する
    }
  }
}
```

- `mode` に不正値が指定された場合は WARNING を出して `"caption"` で出力（黙って別動作をしない）。
- `caption_format` は `"ITT"` のみ実装。他値は WARNING → ITT。
- **旧挙動へ戻す**には `mode: "title"`（タイミング修正は適用される）。

---

## 7. エラー処理・フォールバック方針

- **親クリップ未特定の字幕**（終端超え等）→ 最終クリップへクランプ配置＋WARNING（1 回）。エクスポートは中止しない。
- **分割後 1 フレーム未満の断片**→ 破棄（DEBUG）。
- **字幕 0 件**→ caption/SRT を出力しない（FCPXML はカット編集点のみ。現行 §7 と同じ）。
- **SRT 書き出し失敗**→ FCPXML が成功していれば FCPXML は残し、SRT の失敗を WARNING＋UI 通知（`ExportError` にはしない：主産物は FCPXML）。
- FCPXML 生成失敗・上書き確認・原子的書き出しは現行（resolve20 §7）を踏襲。

## 8. ログ出力方針

- 出力サマリ INFO に「字幕 mode／caption 件数／分割発生数／SRT パス」を追加。
- トラックスタイル運用案内（§5.5-3）を INFO で 1 回。
- 現行の位置近似 WARNING（`_warn_position_once`）は **title mode のときのみ**出す（caption では位置は placement＋トラックスタイル前提のため対象外）。

## 9. 影響範囲・後方互換

- **パイプライン・GUI 配線は不変**（`src/export/` 内部と設定既定の追加のみ。§5.7 の表示修正を除く）。
- **既定挙動の変化**：mode 既定を `"caption"` とするため、**既定では字幕が Text+ でなく字幕トラックに入る**（本要望 P2 の意図どおり）。Text+ 継続希望時は設定で `"title"`。
- **タイミング修正（§5.2/§5.3）は全 mode に適用**されるため、旧出力とは FCPXML の構造が変わる（title が asset-clip の子要素になる）。旧ファイルを再取り込みする運用はない想定のため互換問題なし。
- 既存テスト 27 件のうち title の spine 直下配置を前提とする検証は**仕様準拠の期待値へ更新**する（テストの網羅対象は §5.1）。
- 新規依存なし・spec 変更なし（`srt_writer` は通常 import で PyInstaller 同梱）。

## 10. 確認事項（実機確認が必要な点 — 推測実装しない）

| # | 項目 | 論点 | 既定の扱い |
|---|---|---|---|
| 1 | **Resolve の `<caption>` 取り込み可否** | 使用中の Resolve バージョンが FCPXML 内 caption を字幕トラックへ変換するか（版依存の報告あり） | 実機確認。不可なら SRT サイドカー（確実）を正とし、mode 既定を `"both"` か `"title"` へ見直す |
| 2 | **caption text-style の反映度** | フォント/色/太字が字幕トラックのクリップへ反映されるか、トラックスタイルで上書きされるか | 実機確認。反映されない場合はトラックスタイル運用（§5.5-3）を正式手順として README/ログで案内 |
| 3 | **役割別色分けの維持** | 字幕トラックで per-字幕色が失われる場合、役割別トラック（role 分割）へ拡張するか | 実機確認後に判断（必要なら次要望で対応） |
| 4 | **タイミング修正の実測** | §5.2/§5.3 適用後、Resolve 上で字幕がフレーム精度で一致するか（クリップ/アーカイブ両フロー） | 実機確認（受け入れ条件：全字幕が期待位置 ±1 フレーム） |
| 5 | **fps とソース実 fps の差** | timeline fps（`ffmpeg.output_fps`=60）とソース VOD の実 fps が異なる場合の挙動 | 実機確認。問題があれば asset 用 format を実 fps で別途宣言する改修を検討 |
| 6 | **Resolve 19 の Text+ トラックスタイル** | 字幕トラックのスタイルに Text+ を指定できる版か | 実機確認（案内文言の出し分けに使用） |

## 11. 段階実装（レビュー後）

1. **タイミング修正（P1）**：`fcpxml_builder` のアンカリング＋整数フレーム管理（§5.2/§5.3）。title mode のまま実機で ±1 フレーム一致を確認（§10-4）。**P2/P3 と独立に先行実装・検証可能**。
2. **caption 生成（P2）**：builder の caption 対応＋`resolve_export` の mode 分岐＋設定追加。実機で字幕トラック取り込みを確認（§10-1）。
3. **SRT サイドカー＋スタイル案内（P3）**：`srt_writer` 追加・併記出力・完了メッセージ/ログ整備。text-style 反映度を実機確認（§10-2/§10-3）。
4. **確認結果の反映**：§10 の結果に応じて mode 既定・役割別トラック等を最終調整。

実装着手は本設計書のレビュー後とする（CLAUDE.md「実装は設計書レビュー後に行う」遵守）。

---

## 12. 実装状況（2026-07-30）

本設計書に基づき段階 1〜3 を実装済み。段階 4（実機確認の反映）は §10 の確認待ち。

| 項目 | 実装 | 備考 |
|---|---|---|
| P1① アンカリング修正 | 済 | title/caption を担当 asset-clip の子要素化。`offset = 親start + (タイムライン時刻 − 親タイムライン位置)`。境界跨ぎは分割（枝番 `_1`/`_2`）、1 フレーム未満は破棄、終端超えは末尾クランプ＋WARNING |
| P1② 整数フレーム管理 | 済 | `_quantize_clips` で量子化済み尺の累積からタイムライン位置を確定（隙間/重なりを構造的に排除） |
| P2 caption 生成 | 済 | `<caption role="iTT?captionFormat=ITT.ja">`＋`<text placement>`。`export.resolve.subtitle.mode`（caption/title/both・既定 caption）で切替。テーマ演出は常に title |
| P3 スタイル適用 | 済 | caption text-style（font/fontSize/役割色/太字/斜体/下線）＋SRT サイドカー併記（`srt_sidecar` 既定 true）＋トラックスタイル運用案内（INFO ログ・完了ダイアログ） |
| 設定追加 | 済 | `DEFAULT_SETTINGS`／`setting.json` に `export.resolve.subtitle` 節（mode/caption_format/language/srt_sidecar）。追加のみ・既存キー不変 |
| 新規モジュール | 済 | `src/export/srt_writer.py`（純関数・新規依存なし） |
| GUI | 済 | `run_resolve_export` が複数出力パス（fcpxml＋srt）を表示し、SRT の取り込み手順とトラックスタイル設定手順を案内（表示のみの修正） |
| 単体テスト | 済 | 44 件（`python -m unittest discover -s tests`）全て成功。アンカリング/整数フレーム累積/分割/クランプ/caption/mode 分岐/SRT を網羅 |

**残作業（§10 実機確認）**：使用中の Resolve 版での caption 取り込み可否（§10-1）・text-style 反映度（§10-2）・役割別色分け（§10-3）・タイミング ±1 フレーム一致（§10-4）。確認結果に応じて mode 既定などを最終調整する。

---

## 参考（調査ソース）

- FCPXML の接続クリップ・時間属性の仕様（offset は親タイムライン基準／start はソース in 点／接続要素は親クリップへ入れ子・親ローカル時間へ変換）:
  [SpliceKit FCPXML Format Reference](https://github.com/elliotttate/SpliceKit/blob/main/docs/FCPXML_FORMAT_REFERENCE.md) / [FCPXML structure notes](https://gist.github.com/allenday/44cafa9d698ae6f94c5d55205f86f5b9)
- Resolve の字幕取り込み（Import Subtitle・対応形式・トラックスタイル）:
  [DaVinci Resolve 18 Manual — Importing Subtitles and Captions](https://www.steakunderwater.com/VFXPedia/__man/Resolve18-6/DaVinciResolve18_Manual_files/part1280.htm)
- SRT/FCPXML でのキャプション取り込みガイド:
  [Rev — Add Captions & Subtitles to DaVinci Resolve](https://www.rev.com/blog/how-to-add-captions-and-subtitles-to-davinci-resolve-studio) / [Happy Scribe — Adding subtitles in DaVinci Resolve](https://help.happyscribe.com/en/articles/6087119-adding-subtitles-and-captions-in-davinci-resolve)
