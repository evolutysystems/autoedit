# resolve14 — 縦動画（YouTube Shorts / TikTok）対応 要望設計書

## 0. 本書の位置づけ
`docs/request/request14.md` の要望に対する **設計書** です。CLAUDE.md の方針（いきなり実装しない／既存実装を破壊しない／ハードコード禁止・設定は setting.json 管理／不要なライブラリを追加しない／後方互換を失わない）に従い、本書レビュー後に実装します。

---

## 1. 要望（request14.md 要約）
縦動画を選択した場合に以下を実現する。
1. **縦動画か横動画かを判定する処理**。
2. 縦動画なら **YouTube Shorts / TikTok に対応できるサイズ**（縦 9:16）にする。
3. **縦動画用のフォントサイズ**。
4. 縦動画なので **10〜15文字で改行**。
5. **YouTube Shorts / TikTok に対応できる字幕配置**。

---

## 2. 現状分析（関連コードの実測）

| 対象 | 現状 | 縦対応上の問題 |
|---|---|---|
| **向き判定** | **存在しない**。入力動画の幅・高さを取得する処理そのものが無い（`ffmpeg_runner` に `probe_duration` はあるが `probe_dimensions` は無い） | 縦/横を分岐できない |
| `setting.json` `ffmpeg.output_width/height` = **1920×1080 固定** | `settings_window.py` L195-196 の既定も 1920×1080 | 出力サイズが横専用 |
| `concat_processor.py` L37-52 | OP/本編/ED を `scale=W:H:force_original_aspect_ratio=decrease` + `pad=W:H` で **1920×1080 に正規化** | **縦動画が横キャンバスにレターボックス（左右黒帯）される** → Shorts/TikTok 不適合 |
| `subtitle_generator.py` `burn_subtitle` L497-532 | setpts→subtitles→CFR再エンコードのみ。**scale なし**（=入力解像度を維持） | 出力キャンバスを縦へ正規化する箇所が無い |
| `subtitle_generator.py` `build_subtitle_file` L442-461 | 引数 `video_width=1920, video_height=1080` を持つが、**呼び出し（L829）で渡していない** → ASS `PlayResX/PlayResY` が**常に 1920×1080 固定** | 縦動画では字幕座標系が実寸と不一致（フォント・余白が破綻） |
| `subtitle` フォント/改行/配置 | `font_size`(既定48/実setting130), `min_line_length`15 / `max_line_length`20, `alignment`2(下中央), `margin_v`60 | いずれも横1080高さ基準の単一値。縦用の別値を持てない |
| `silence_cutter.py` | スケールなし（CFR正規化のみ） | 影響小（解像度は維持） |

**結論**: 現状は**全工程が横1920×1080を暗黙前提**にしており、向き判定・縦キャンバス・縦用の字幕パラメータ（フォント/改行/配置）・ASS `PlayRes` の実寸連動が**すべて欠落**している。

**設計の要点**は「①入力の実表示寸法を測って向きを判定 → ②向きに応じた**出力プロファイル**（横=既存 / 縦=新規）を1回だけ解決して context に保持 → ③解像度正規化・ASS `PlayRes`・字幕パラメータを**全工程で同一プロファイルに揃える**」こと。既存の横挙動は既定プロファイルとして**完全維持**する。

---

## 3. 設計方針（全体）

1. **向き判定を追加**：`ffmpeg_runner.probe_dimensions()` を新設し、入力の**表示寸法（回転メタデータ適用後）**を取得。`height > width` を縦と判定する。
2. **出力プロファイルを導入**：`landscape`（既存＝1920×1080）と `portrait`（新規＝1080×1920）を定義。パイプライン開始時に**1回だけ**解決し、`PipelineContext` に保持して全工程が参照する（工程ごとの再判定・不整合を防ぐ）。
3. **設定は追加のみ（後方互換）**：既存 `ffmpeg.output_width/height` と `subtitle.*` は**横の既定値として温存**。縦専用の**上書き値だけ**を新規 `vertical` セクションに追加する。縦検出時のみ該当キーを上書きする（ハードコード禁止・setting.json 管理）。
4. **字幕をプロファイル連動**：`build_subtitle_file` に**実プロファイル寸法を渡す**（現状の未指定バグを解消）。縦時は `font_size` / `min_line_length` / `max_line_length` / `alignment` / `margin_*` を縦用値へ差し替える。
5. **解像度正規化を統一**：`burn_subtitle` に scale+pad を追加し、本編を**プロファイル寸法へ正規化**。`concat_processor` は既存の scale/pad の W/H をプロファイル寸法から取る。→ OP/ED 有無に関わらず最終出力が縦キャンバスに揃う。
6. **Shorts/TikTok セーフゾーン配置**：縦は下端の CTA/キャプション帯・右側ボタン列を避けるため、**下中央（alignment=2）＋大きめ `margin_v`** を既定にする（§4.6）。

---

## 4. 詳細設計

### 4.1 向き判定（`ffmpeg_runner.probe_dimensions`）
`probe_duration` と同じ流儀で ffprobe を用い、映像ストリームの `width`/`height` と**回転情報**を取得する。

```
# 追加関数（方針。実装はレビュー後）
# 戻り値: (disp_width, disp_height, orientation)  orientation ∈ {"landscape","portrait"}
def probe_dimensions(input_path, ffmpeg_settings):
    ffprobe -v error -select_streams v:0 \
      -show_entries stream=width,height:stream_side_data=rotation \
      -of json <input>
    # 回転(±90/±270)がある場合は width/height を入れ替えて「表示寸法」を得る
    # height > width → "portrait" / それ以外 → "landscape"
```

**回転メタデータの注意（重要）**: スマホ撮影動画は生の `width/height` が横のまま `rotation=-90` 等のサイド情報で縦表示されるケースが多い。**回転適用後の表示寸法**で判定しないと縦動画を横と誤判定する。`side_data_list` の `rotation`（または display matrix）を読み、±90/±270 のとき幅・高さを入れ替える。取得できない/解析失敗時は横として続行（フォールバック §6）。

**正方形（width==height）** は既定で横扱い（§8 で確認）。

### 4.2 出力プロファイルの解決と保持
- 新規ヘルパ（例：`pipeline_runner._resolve_output_profile(context)`）で、開始直後・無音カット前に `probe_dimensions` を実行。
- 縦対応が有効（`vertical.enabled=true`）かつ縦検出時は **portrait プロファイル**、そうでなければ **landscape プロファイル**（＝既存値）を選ぶ。
- 解決結果（`width/height` と字幕上書き値）を `PipelineContext` の新フィールド（例：`output_profile`）へ格納。以降 `subtitle_generator` / `concat_processor` は**この値のみ**を参照する。
- **判定基準は入力動画**（無音カットは解像度を変えないため、入力実寸で確定してよい）。

### 4.3 setting.json 追加案（追加のみ・既定は現行維持）
既存 `ffmpeg` / `subtitle` は**横の既定として不変**。縦専用の上書き値を新規セクションに集約する。

```jsonc
"vertical": {
  "enabled": true,          // 縦動画自動対応 ON/OFF（false で従来どおり横のみ）
  "output_width": 1080,     // 縦キャンバス幅（9:16 固定＝§8-1 確定）
  "output_height": 1920,    // 縦キャンバス高
  // 縦用・字幕上書き（未指定キーは subtitle の横既定を流用）
  "font_size": 90,          // 縦用フォント（幅1080・10〜15字前提の目安）
  "min_line_length": 10,    // 縦改行の下限
  "max_line_length": 15,    // 縦改行の上限（要望：10〜15文字）
  "alignment": 2,           // ASS numpad：2=下中央（§4.6）
  "margin_v": 320,          // 下端UI帯を避ける縦マージン（px, 1920基準）
  "margin_l": 40,
  "margin_r": 40
}
```
- 向き判定は **auto 固定**（入力の表示寸法から自動）。正方形(1:1)は **横扱い**（§8-3 確定）。
- フォント種類・色・アウトライン・装飾は字幕（横）設定を共有し、**縦タブでは上記の上書き値のみ**を持つ（§8-6）。
- `settings_window.py` の `DEFAULT_SETTINGS` にも同節を追加（`_merge_with_defaults` 相当で欠落補完し後方互換維持）。
- 既存環境（`vertical` 欠落）では `enabled` を **true 既定**にしても、横動画なら landscape が選ばれるため**挙動は不変**。縦を扱わない運用にしたい場合は `enabled:false` で完全無効化できる。

### 4.4 字幕パラメータのプロファイル連動（`subtitle_generator`）
1. `run()` L750-782 の `FontProfile` 構築で、**プロファイルが portrait のとき** `font_size` / `min_line_length` / `max_line_length` / `alignment` / `margin_v` / `margin_l` / `margin_r` を `vertical.*` で上書きする（色・アウトライン等は共通のまま）。
2. `WhisperTextSource` の改行下限/上限（L299-300）も**プロファイル値**を使う（縦は 10〜15）。※現在は `subtitle_cfg` 直読みのため、プロファイル解決値を渡す形へ小改修。
3. `build_subtitle_file(...)` 呼び出し（L829）で **`video_width`/`video_height` にプロファイル寸法を渡す** → ASS `PlayResX/PlayResY` が実キャンバスと一致（現状の固定バグを解消。横でも正しくなる）。

### 4.5 解像度正規化の適用箇所
| 箇所 | 変更 | 効果 |
|---|---|---|
| `burn_subtitle`（`subtitle_generator` L514 filter_spec） | `setpts=…` の後・`subtitles=…` の前に `scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2,setsar=1` を挿入（W/H=プロファイル寸法） | 本編を縦キャンバスへ正規化してから字幕を焼く。ASS `PlayRes` と一致 |
| `concat_processor.run`（縦時） | **縦検出時は OP/ED 結合をスキップ**（§8-2 確定：縦は OP/ED を付けない） | 縦専用素材を用意せず横潰れも発生させない |

- **縦（portrait）**：`burn_subtitle` の scale+pad のみで最終出力を 1080×1920 に揃える。**OP/ED は常にスキップ**（§8-2）。
- **テロップ無し & 縦**：正規化箇所を通らず**素通しを許可**（§8-5 確定。入力が 1080×1920 ならそのまま、異寸でも変換しない）。
- **横（landscape）**：`burn_subtitle` に scale を**追加しない**（`target_size=None`）ため従来と**完全に挙動不変**。OP/ED は従来どおり 1920×1080 で結合。

### 4.6 Shorts/TikTok に対応する字幕配置（要望5）
縦キャンバス 1080×1920 における各プラットフォームの**UI占有帯**を避ける。

- **TikTok**：右側にボタン列（いいね/コメント/シェア）、下部にキャプション・ユーザー名・進捗バー（概ね下部 15〜20%）。
- **YouTube Shorts**：下部にタイトル/チャンネル/操作、右側に高評価列（概ね下部 12〜15%）。

**既定方針（配信者テロップ）**：
- **`alignment = 2`（下中央）** を基本とし、**`margin_v ≈ 320`（px, 1920基準 ≒ 画面下 16.7%）** で下端UI帯より上へ持ち上げる。左右ボタンは中央寄せ字幕とほぼ干渉しないため `margin_l/r` は控えめ（40）。
- 画面中央に置きたい場合は **`alignment = 5`（中央中央）** を選べるよう設定化（`vertical.alignment`）。
- 文字は幅1080に対し **10〜15字/行**（§4.4）。フォントは太字＋アウトライン前提で **90px 目安**（要望3。実素材で微調整、§8-4）。

> ASS numpad 配置：1=左下 2=中下 3=右下 / 4=左中 5=中央 6=右中 / 7=左上 8=中上 9=右上。`margin_v` は alignment が下(1-3)なら下端から、上(7-9)なら上端からの距離。

---

## 5. 変更対象ファイル一覧（予定）
| ファイル | 変更概要 |
|---|---|
| `src/modules/ffmpeg_runner.py` | `probe_dimensions()` 新設（回転メタ対応・表示寸法/向き返却） |
| `src/pipeline/pipeline_runner.py` | 開始時に出力プロファイルを解決し context へ格納 |
| `src/pipeline/pipeline_context.py` | `output_profile`（寸法＋字幕上書き）フィールド追加 |
| `src/modules/subtitle_generator.py` | プロファイルで font/改行/alignment/margin 上書き、`build_subtitle_file` へ実寸伝播、`burn_subtitle` に scale+pad 追加 |
| `src/modules/concat_processor.py` | 結合 W/H をプロファイル寸法に変更 |
| `src/settings/setting.json` | `vertical` セクション追加 |
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS["vertical"]` 追加（＋必要なら設定UI） |

---

## 6. エラー処理・フォールバック方針
- `probe_dimensions` 失敗（ffprobe エラー/解析不能）→ **横（landscape）として続行**しログ警告（縦対応で全損させない。既存のフォールバック思想＝VAD/BudouX失敗時と同様）。
- 回転メタ取得不能 → 生 `width/height` で判定し警告。
- `vertical` セクション欠落 → `_merge_with_defaults` 相当で補完（既定値適用）。
- 例外階層は既存 `AutoEditError` 系を流用（新規例外は増やさない）。

---

## 7. 影響範囲・後方互換
- **横動画**：landscape プロファイル＝既存 1920×1080 と同値。`build_subtitle_file` へ実寸を渡す修正で ASS `PlayRes` が「実寸連動」に変わるが、対象が 1920×1080 なら結果は従来同一。
- **注意（横の非16:9ソース）**：`burn_subtitle` への scale+pad 追加により、従来「素通し」だった**非16:9の横ソースに pad（黒帯）が入る**挙動変化が生じ得る。出力解像度を常にプロファイルへ統一する利点はあるが、既存挙動を厳密維持したい場合は「横は従来どおり scale なし／縦のみ scale+pad」とする**オプション化**も可能（§8-5 で方針確認）。
- **新規依存なし**（ffprobe は既存同梱）。CLAUDE.md「不要なライブラリを追加しない」遵守。

---

## 8. 不明点・確認事項 → **回答確定（2026-07-14）**
| # | 項目 | 回答（確定） |
|---|---|---|
| 1 | 縦の出力サイズ | **1080×1920 固定**（下位解像度は当面不要。設定値としては保持し既定固定） |
| 2 | 縦の OP/ED 素材 | **縦動画では OP/ED を付けない**（縦検出時は結合工程をスキップ） |
| 3 | 正方形(1:1) | **横扱い**（`height > width` のみ縦。等値は横） |
| 4 | 縦のフォント/マージン既定値 | **その想定で採用**（`font_size=90` / `margin_v=320` / `alignment=2` / 改行 10〜15） |
| 5 | 正規化の適用範囲 | **素通しを許可**（テロップ無し＆縦は変換せず通す。横は従来どおり scale 追加なし） |
| 6 | 設定UI | `settings_window.py` に**「縦動画」タブを新設**し、縦フォント・改行・配置等の項目を追加する |

> 上記確定により本設計は実装フェーズへ移行する。

---

## 9. 段階実装（レビュー後）
1. `probe_dimensions`（回転対応）＋ プロファイル解決を追加（判定のみ・出力は横のまま）でログ検証。
2. `vertical` セクション追加＋ `concat`/`burn`/`build_subtitle_file` をプロファイル連動化（縦キャンバス出力）。
3. 縦用フォント/改行/配置の適用と実素材での微調整（§8-4/7 確定後）。
4. 必要なら設定UI追加（§8-6 確定後）。

実装着手は本設計書のレビュー後とする（CLAUDE.md「実装は設計書レビュー後に行う」遵守）。
