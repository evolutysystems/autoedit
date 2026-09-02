# resolve14（ver3） — コメント役割テロップのユーザーアイコン拡大 修正設計書

## 0. 本書の位置づけ

* 対象要望: `docs/request/ver3/request14.md`
* 本書は **設計のみ**。実装は本書のレビュー後に着手する（`docs/claude.md`「実装前に設計を行うこと」）。
* 調査は **2026-09-01 時点の実コード**を読み、
  **`comment_decor` の幾何計算を実際に走らせて数値を実測**した上で行った。
  本書の数値表はすべて実測値であり、手計算の見積りは含まない。
* 変更は **`setting.json` の既定値 4 つと、その移行処理 1 つだけ**。
  アイコンの描画コード（`comment_decor`）・焼き込み・プレビューには一切触れない。
* 追加する設定項目は無い（既存キーの既定値変更のみ / §7）。

---

## 1. 要望（request14.md）と要件 ID

| ID | 要望 | 種別 |
|---|---|---|
| **H1** | コメント役割テロップの**ユーザーアイコンを大きく**する | 改善 |
| **H2** | 横動画時：**1.5 倍** | 〃 |
| **H3** | 縦動画時：**2 倍** | 〃 |

要望文に無いが、実装に必ず要る論点:

| ID | 論点 | 理由 |
|---|---|---|
| **H4** | アイコンの**左余白を追従**させること | アイコンの位置は「文字の左端」から逆算される。大きさだけ変えると左へはみ出す（§2.4 で実測） |
| **H5** | **既存の `setting.json` への反映** | `_merge_with_defaults` は既存キーの値を温存するため、既定値を変えただけでは今使っている環境に反映されない（§2.7） |
| **H6** | 利用者が**独自に調整した値を壊さない**こと | 既定値の移行は「旧既定のときだけ」寄せるのが本プロジェクトの規約（`_migrate_comment_label` と同じ扱い） |
| **H7** | 背景ボックスへの**波及**の把握 | 箱の高さはアイコンと文字の大きい方で決まる。拡大で箱も変わる |

---

## 2. 現状分析

### 2.1 アイコンの大きさはどこで決まるか

`src/modules/comment_decor.py` の `icon_box()` が**唯一の計算元**である。

```python
# comment_decor.py:166-190
def icon_box(item, eff_cfg, canvas_w, canvas_h):
    ...
    size = _to_int((eff_cfg or {}).get("comment_icon_size_px"), 100)
    ...
    gap = max(_to_int((eff_cfg or {}).get("comment_icon_gap_px"), 50), 0)
    extent = text_extent(item, eff_cfg)
    text_left, text_mid = _text_anchor(item, eff_cfg, canvas_w, canvas_h, extent)
    x = text_left - gap - size          # ← 位置は「文字の左端」から逆算する
    y = text_mid - size / 2.0
```

`grep` で洗ったところ、アイコンを描く経路は 5 つあり、**すべてこの関数を通る**。

| 経路 | 呼び出し元 |
|---|---|
| Timeline 編集画面のプレビュー | `gui/timeline/preview_items.py:397` → `icon_box` |
| 高精度プレビュー | `gui/timeline/preview_panel.py:957` → `build_icon_chains` |
| 従来の字幕プレビュー | `gui/subtitle_preview_widget.py:370` → `build_icon_chains` |
| Timeline レンダリング（書き出し） | `timeline/renderer.py:462 / 519` → `build_icon_chains` |
| 字幕焼き込み（クリップ用） | `modules/subtitle_generator.py:1238` → `build_icon_chains` |

`build_icon_chains` も自前の寸法を持たず `icon_box(...)["size"]` をそのまま
ffmpeg の `scale=w={size}:h={size}` へ渡している（`comment_decor.py:337-341`）。

**したがって `comment_icon_size_px` を変えるだけで、画面と出力の両方が同時に変わる。**
描画コードへ手を入れる必要は無い。

### 2.2 アイコンの位置は「文字の左端」から逆算される（H4 の根拠）

§2.1 のとおり `x = text_left - gap - size` である。
`text_left` は位置指定の無いコメントでは `comment_margin_l` そのもの
（`_text_anchor` / `comment_decor.py:139-141`）。

つまり **アイコンの左端 = `comment_margin_l - gap - size`** であり、
`size` を増やすとアイコンは**左へ伸びる**。文字の位置は動かない。

### 2.3 現行の数値と「左端 40px」の由来

現行の既定値には、この逆算を前提とした設計意図が書かれている。

```python
# settings_window.py:190-198（subtitle / 横動画）
# 左余白 = margin_l(40) + アイコン幅(100) + 間隔(50)。アイコン左端 40px に揃う。
"comment_margin_l": 190,
"comment_icon_size_px": 100,
"comment_icon_gap_px": 50,
```

```python
# settings_window.py:605-613（vertical / 縦動画）
# アイコンを 50x50 へ縮め、左余白 = 40 + 50 + 50 = 140 とする
# (横と同じくアイコン左端 40px に揃える)。
"comment_margin_l": 140,
"comment_icon_size_px": 50,
"comment_icon_gap_px": 50,
```

**「アイコンの左端を画面端から 40px に揃える」が守るべき不変条件**であり、
`comment_margin_l` はその結果として決まる従属値である。

なお、縦動画の上書きは `_VERTICAL_OVERRIDE_KEYS`（`subtitle_generator.py:546-553`）に
`comment_icon_size_px` と `comment_margin_l` の両方が含まれているため、
**縦横で別々の値を持てる**。要望の「横 1.5 倍 / 縦 2 倍」はこの仕組みでそのまま表せる。

### 2.4 大きさだけ変えると画面外へ出る（実測）

現行の `comment_decor` に拡大値を与えて実際に計算させた結果:

| 条件 | アイコン左端 x | 一辺 | はみ出し詰め |
|---|---|---|---|
| 横 現行（icon=100 / margin_l=190） | **40** | 100 | なし |
| 横 icon=150 のみ（margin_l=190 据え置き） | **0** | 150 | **あり** |
| 横 icon=150 + margin_l=240 | **40** | 150 | なし |
| 縦 現行（icon=50 / margin_l=140） | **40** | 50 | なし |
| 縦 icon=100 のみ（margin_l=140 据え置き） | **0** | 100 | **あり** |
| 縦 icon=100 + margin_l=190 | **40** | 100 | なし |

大きさだけ変えると、いずれも要求位置が `x = -10` になって
`_clamp` で 0 へ詰められ、`icon_clamp` の警告が毎回出る
（`comment_decor.py:182-189`）。見た目も画面端へ張り付く。

**よって size と margin_l は必ず対で変える**（H4）。
増やす量は `size` の増分と同じ（横 +50 / 縦 +50）。

### 2.5 背景ボックスへの波及（H7）

`background_box` は中身の高さを
`half = max(文字高 / 2, アイコン / 2)`（`comment_decor.py:210`）で決める。
実測すると:

| | 文字高 | アイコン | 箱 w × h（現行 → 変更後） |
|---|---|---|---|
| 横 1920x1080（font 48・1 行） | 58 | 100 → 150 | 610×**148** → 660×**198** |
| 縦 1080x1920（font 90・1 行） | 108 | 50 → 100 | 780×**156** → 830×**156** |

* **横**はアイコンが高さを決めるようになり、箱が 50px 高くなる（1080 の 4.6%）。
* **縦**は文字（108）がアイコン（100）より高いままのため、**箱の高さは変わらない**。
* 幅はどちらも +50px（`comment_margin_l` の移動ぶん）。

最長行での収まりも確認した:

| | max_line_length | 箱の幅 | 詰め |
|---|---|---|---|
| 横 現行 | 20 | 947 | なし |
| 横 変更後 | 20 | 997 | なし |
| 縦 現行 | 15 | 1080 | **あり** |
| 縦 変更後 | 15 | 1080 | **あり** |

**縦動画の最長行はすでに現行で箱が画面幅に収まっていない。**
これは本要望とは独立の既存事象であり、変更後も悪化しない（どちらも詰めが起きる）。
本書では扱わず、§11 F2 として記録する。

### 2.6 アイコン素材は 300x300 px（拡大にはならない）

`src/comment_icon.png` は **300×300 px**。
`build_icon_chains` は `scale=...:force_original_aspect_ratio=decrease` で縮小して使う。

| 表示サイズ | 素材に対する倍率 | |
|---|---|---|
| 50px（縦 現行） | 0.17 | 縮小 |
| 100px（縦 変更後 / 横 現行） | 0.33 | 縮小 |
| 150px（横 変更後） | 0.50 | 縮小 |

**変更後もすべて縮小のため、拡大によるぼやけは起きない。**

### 2.7 既定値を変えただけでは今の環境に反映されない（H5）

`load_settings` は `_merge_with_defaults` で**不足キーだけ**を補い、
既存キーの値はそのまま温存する（`settings_window.py:788-800`）。
`src/settings/setting.json` には既に `comment_icon_size_px: 100` が書かれているため、
DEFAULT_SETTINGS を変えても**この環境では 100 のまま**になる。

本プロジェクトには、この状況のための仕組みが既にある。

```python
# settings_window.py:815-836
def _normalize_legacy_values(merged):
    ...
    if _migrate_comment_label(merged):   # 旧既定「コメント：」を空へ寄せる
        changed = True
```

`_migrate_comment_label`（`settings_window.py:847-856`）のコメントが方針を明示している。

> 既知の旧既定値のときだけ空へ寄せる。
> 利用者が独自の文言を入れている場合は上書きしない (その文言は尊重する)。

**同じ扱いでアイコンの寸法も移行する**（H5 / H6）。

---

## 3. 方式選定

### 3-1. 大きさの指定方法

| 案 | 内容 | 判定 |
|---|---|---|
| **A（採用）** | `comment_icon_size_px` の**既定値そのものを変える**（横 100→150 / 縦 50→100） | 設定はもともと px で持っている。描画コードを一切触らずに 5 経路すべてへ反映される（§2.1）。縦横の出し分けも既存の上書き機構で済む（§2.3） |
| B | `comment_icon_scale`（倍率）を新設し `icon_box` で掛ける | 「1.5 倍」という**要望の言い方をそのまま設定にしてしまう**案。基準値が別にあるため二重管理になり、`comment_margin_l` との整合も利用者が自力で取ることになる。設定項目も増える |
| C | `icon_box` の x 計算を「左余白固定」へ変え、size に依存しなくする | margin_l を追従させずに済むが、`gap`（文字とアイコンの間隔）が size に応じて勝手に変わる。§2.3 の設計意図（gap は間隔、margin_l は従属値）が崩れる |

**A を採用する。** 要望の「1.5 倍 / 2 倍」は**現行値に対する比**であり、
設定へ書くべきは比ではなく結果の px 値である。

* 横: 100 × 1.5 = **150**
* 縦: 50 × 2 = **100**

### 3-2. 左余白の追従（H4）

§2.4 の実測どおり、`comment_margin_l` を同じ増分だけ増やして
**「アイコン左端 40px」の不変条件を維持する**（§2.3）。

| | 現行 | 変更後 | 内訳 |
|---|---|---|---|
| 横 `comment_margin_l` | 190 | **240** | 40 + 150 + 50 |
| 縦 `comment_margin_l` | 140 | **190** | 40 + 100 + 50 |

### 3-3. 既存 setting.json の移行（H5 / H6）

| 案 | 判定 |
|---|---|
| **A（採用）** `_normalize_legacy_values` へ移行を足し、**旧既定値のままの環境だけ**新既定へ寄せる | `_migrate_comment_label` と同じ規約。独自調整を尊重しつつ、今の環境にも反映される |
| B | 何もしない（新規インストールのみ反映） | 要望を出した本人の環境に反映されない。実質「直っていない」 |
| C | 値に関わらず上書きする | 独自に調整した利用者の見た目を黙って壊す |

**A を採用する。** ただし移行の単位が問題になる。

`comment_icon_size_px` と `comment_margin_l` は §2.3 のとおり**対で意味を持つ**。
片方だけ寄せるとアイコン左端がずれる（§2.4 の「size のみ」の行がまさにそれ）。

したがって **2 つとも旧既定値のときだけ、2 つまとめて動かす**。
どちらか一方でも利用者が触っていれば**両方そのまま**にし、その旨をログへ残す。
中途半端に片方だけ寄せて見た目を壊すより、触らないほうが安全である（§9 Q2）。

---

## 4. 設計方針

1. **描画コードは触らない**。`comment_decor` は既に唯一の計算元として正しく、
   設定値を変えるだけで 5 経路すべてに反映される（§2.1）。
2. **`comment_icon_size_px` と `comment_margin_l` は必ず対で変える**（§3-2）。
   守る不変条件は「アイコン左端 = 画面端から 40px」。
3. 縦横の出し分けは **既存の `_VERTICAL_OVERRIDE_KEYS` の仕組みに乗せる**（§2.3）。
   新しい分岐は書かない。
4. **既存 setting.json は「旧既定のときだけ」対で移行する**（§3-3）。
5. 設定項目は増やさない（§7）。

---

## 5. 詳細設計

### 5.1 変更ファイル一覧

| ファイル | 変更内容 | 規模 |
|---|---|---|
| `src/settings/settings_window.py` | `DEFAULT_SETTINGS` の値 4 つ / 移行関数 `_migrate_comment_icon_size` の追加と登録 | 値 4 行 + 約 25 行 |
| `tests/test_comment_decor.py` | 既定値を前提にした期待値の更新（2 件） | 2 行 |
| `tests/test_comment_placement.py` | 縦上書きの期待値の更新（1 件） | 2 行 |
| `tests/test_settings_migration.py` | 移行のテストを追加 | 約 30 行 |

**`src/modules/comment_decor.py` は変更しない。**

### 5.2 DEFAULT_SETTINGS の差分

```python
# settings_window.py "subtitle" セクション（横動画）
-        # 左余白 = margin_l(40) + アイコン幅(100) + 間隔(50)。アイコン左端 40px に揃う。
-        "comment_margin_l": 190,
+        # 左余白 = margin_l(40) + アイコン幅(150) + 間隔(50)。アイコン左端 40px に揃う。
+        # アイコンは ver3 resolve14 で 1.5 倍 (100→150)。margin_l は従属値のため必ず対で直す。
+        "comment_margin_l": 240,
...
-        "comment_icon_size_px": 100,
+        "comment_icon_size_px": 150,
```

```python
# settings_window.py "vertical" セクション（縦動画）
-        # アイコンを 50x50 へ縮め、左余白 = 40 + 50 + 50 = 140 とする
-        # (横と同じくアイコン左端 40px に揃える)。
-        "comment_margin_l": 140,
+        # アイコンは 100x100 (ver3 resolve14 で 2 倍 / 50→100)。
+        # 左余白 = 40 + 100 + 50 = 190 とし、横と同じくアイコン左端 40px に揃える。
+        "comment_margin_l": 190,
...
-        "comment_icon_size_px": 50,
+        "comment_icon_size_px": 100,
```

`comment_icon_gap_px`（50）は縦横とも**変更しない**。
間隔は大きさと独立した見た目の要素であり、要望の対象でもない。

### 5.3 移行処理（`settings_window.py`）

```python
# コメントアイコンの拡大 (ver3 resolve14)。
# 背景: アイコンが小さいという要望で既定を横 1.5 倍 / 縦 2 倍にしたが、
#   _merge_with_defaults は既存のユーザー値を温存するため、旧 setting.json では
#   小さいままになる。_migrate_comment_label と同じ扱いで、既知の旧既定値のときだけ寄せる。
# 【重要】comment_icon_size_px と comment_margin_l は対で意味を持つ
#   (左余白 = 40 + アイコン + 間隔 / resolve14 §2.3)。片方だけ寄せるとアイコンが
#   画面端へはみ出すため、2 つとも旧既定のときだけ 2 つまとめて動かす。
_LEGACY_COMMENT_ICON = {
    "subtitle": {"comment_icon_size_px": 100, "comment_margin_l": 190},
    "vertical": {"comment_icon_size_px": 50, "comment_margin_l": 140},
}


def _migrate_comment_icon_size(merged):
    changed = False
    for section, legacy in _LEGACY_COMMENT_ICON.items():
        target = merged.get(section)
        if not isinstance(target, dict):
            continue
        # 1 つでも独自の値なら触らない (利用者の調整を尊重する)
        if any(target.get(key) != value for key, value in legacy.items()):
            continue
        for key in legacy:
            target[key] = DEFAULT_SETTINGS[section][key]
        changed = True
    if changed:
        _logger.info("コメントアイコンを拡大しました (横 1.5 倍 / 縦 2 倍)")
    return changed
```

`_normalize_legacy_values` へ登録する:

```python
     if _migrate_comment_label(merged):
         changed = True
+    if _migrate_comment_icon_size(merged):
+        changed = True
```

これで `load_settings` が起動時に一度だけ `setting.json` を書き戻す
（`settings_window.py:795-799` の既存経路）。

### 5.4 変わらないもの（意図的に触らない）

| 対象 | 理由 |
|---|---|
| `src/modules/comment_decor.py` | 設定値を変えるだけで反映される（§2.1）。幾何計算は正しい |
| `comment_icon_gap_px` | 間隔は大きさと独立。要望の対象外 |
| 背景ボックスの設定（`comment_bg_*`） | 箱は中身から自動で決まる（§2.5）。縦横で共通のまま |
| `src/comment_icon.png` | 300×300 px で足りる（§2.6）。差し替え不要 |
| 設定画面（`settings_window` の UI） | この 2 キーは UI に出しておらず `setting.json` 直編集の値。UI の変更は不要 |
| 焼き込み・プレビューの各経路 | 無改造 |

---

## 6. 実装手順

1. `DEFAULT_SETTINGS` の 4 値とコメントを更新する（§5.2）。
2. `_migrate_comment_icon_size` を追加し `_normalize_legacy_values` へ登録する（§5.3）。
3. 既存テストの期待値を更新する（§10-2）。
4. 移行のテストを追加する（§10-3）。
5. 実機で §10-1 を確認する。

---

## 7. setting.json 定義（追加分）

**追加項目は無し。** 既存キーの既定値を変えるだけである。

| セクション | キー | 現行 | 変更後 |
|---|---|---|---|
| `subtitle` | `comment_icon_size_px` | 100 | **150** |
| `subtitle` | `comment_margin_l` | 190 | **240** |
| `vertical` | `comment_icon_size_px` | 50 | **100** |
| `vertical` | `comment_margin_l` | 140 | **190** |

利用者が別の大きさにしたい場合は、従来どおりこの 2 つを対で編集する
（`comment_margin_l = 40 + comment_icon_size_px + comment_icon_gap_px`）。

---

## 8. 互換性・非破壊の担保

| 観点 | 影響 |
|---|---|
| コメント役割**以外**のテロップ | 影響なし。`icon_box` は `is_comment(item)` で弾く（`comment_decor.py:167`） |
| アイコン無効時（`comment_icon_enabled: false`） | 影響なし |
| 画面と出力の一致 | 保たれる。5 経路すべてが同じ `icon_box` を見る（§2.1） |
| アイコンのはみ出し | 起きない。左端 40px を維持（§2.4 実測） |
| 背景ボックス | 横のみ高さ +50px・幅 +50px。縦は高さ不変・幅 +50px（§2.5） |
| 既存の保存済みプロジェクト | 影響なし。字幕の位置・文言は Timeline 側にあり、アイコンは描画時に計算される |
| 独自に調整済みの `setting.json` | 変更しない（§3-3）。従来の見た目のまま |
| 画質 | 劣化しない。素材 300px からの縮小のまま（§2.6） |
| ドラッグで位置指定済みのコメント | 追従する。`_text_anchor` が `pos_x` を文字左端として扱い、アイコンはそこから逆算される |

---

## 9. 確認事項

| # | 内容 | 本書の判断 |
|---|---|---|
| **Q1** | 「1.5 倍 / 2 倍」は**現行の既定値**（横 100px / 縦 50px）に対する比という理解でよいか | その前提で 150px / 100px とした。別の基準（例：文字サイズ比）であれば数値が変わる |
| **Q2** | `comment_icon_size_px` か `comment_margin_l` を**独自に調整済み**の環境では、自動拡大を**行わない**方針でよいか | 行わない。片方だけ寄せるとアイコンがはみ出すため（§2.4）。必要なら手で 2 つを直してもらう |
| **Q3** | 横動画で**背景ボックスが 50px 高くなる**（148→198）が許容できるか | 許容する前提。アイコンを大きくする以上、箱がアイコンを包むのは避けられない |
| **Q4** | 文字とアイコンの**間隔（50px）**も一緒に広げるか | 広げない。要望はアイコンの大きさのみ。必要なら `comment_icon_gap_px` で個別に調整できる |
| **Q5** | 縦動画の最長行で背景ボックスが画面幅に収まらない件（§2.5）は本要望で直すか | 直さない。**現行から存在する別事象**で、本変更で悪化もしない。§11 F2 として記録する |

---

## 10. テスト計画

### 10-1. 実機確認

| # | 手順 | 期待 |
|---|---|---|
| 1 | 横動画で字幕の役割を「コメント」にし、Timeline プレビューを見る | アイコンが 1.5 倍（150px）で表示され、左端は画面端から 40px のまま |
| 2 | 同じ状態で書き出す | 焼き込み結果がプレビューと一致する |
| 3 | 縦動画で同じ確認 | アイコンが 2 倍（100px）、左端 40px |
| 4 | ログに `icon_clamp` の警告が出ていないこと | はみ出しの詰めが起きていない |
| 5 | 高精度プレビュー（Timeline 右クリック）を開く | 同じ大きさで出る |
| 6 | コメントを**ドラッグして位置を変えた**状態で確認 | アイコンが文字の左へ追従する |
| 7 | 役割が「配信者」のテロップ | 従来どおりアイコンも背景も出ない |
| 8 | 旧 `setting.json` のまま起動する | 起動時に 4 値が新既定へ書き換わり、以後そのまま |

### 10-2. 既存テストの期待値更新（必須）

既定値を前提に数値を直書きしているテストが **9 件**ある。**削除せず値を更新する**
（「設計書の数値がそのまま出るか」を確かめるテストであり、意味は変わらないため）。

> **実装時の補足**: 本節は当初 3 件と見積もっていたが、実際に走らせたところ 9 件だった。
> 見落としていた 6 件は、背景ボックスの座標・サイズや ffmpeg の overlay 座標を
> 直書きしているテストで、いずれも §2.5 で予測した「箱がアイコンに合わせて変わる」
> ことの現れであり、原因は同一である。以下は実測して確定した一覧。

| # | ファイル | テスト | 現行 | 変更後（実測） |
|---|---|---|---|---|
| 1 | `test_comment_decor.py` | `test_icon_box_landscape_defaults` | `(40.0, 490.0, 100)` | **`(40.0, 465.0, 150)`** |
| 2 | 〃 | `test_icon_box_portrait_defaults` | `(40.0, 935.0, 50)` | **`(40.0, 910.0, 100)`** |
| 3 | 〃 | `test_icon_uses_positioned_left_edge` | `(810.0, 490.0)` | **`(760.0, 465.0)`** |
| 4 | 〃 | `test_same_position_is_one_overlay` | `overlay=40.0:490.0` | **`overlay=40.0:465.0`** |
| 5 | 〃 | `test_background_box_landscape` | `y=458 / w=723 / h=164` | **`y=441 / w=773 / h=198`** |
| 6 | 〃 | `test_background_without_icon` | `x=166.0` | **`x=216.0`** |
| 7 | 〃 | `test_background_text_tags` | `\pos(16.0,458.0)` | **`\pos(16.0,441.0)`** |
| 8 | `test_comment_placement.py` | `test_comment_style_uses_own_placement` | MarginL `"190"` | **`"240"`** |
| 9 | 〃 | `test_vertical_override` | `140` / `50` / `140` | **`190` / `100` / `190`** |

`y` が変わるのは `y = 垂直中心 - size / 2` のため（横 540−75、縦 960−50）。
1〜4 はアイコンそのもの、5〜7 は §2.5 の背景ボックスの変化、8〜9 は左余白の移動が理由。
**いずれも `clamped` は `False` のまま**であること（はみ出していないことの担保）を併せて確認する。

### 10-3. 追加テスト（`tests/test_settings_migration.py`）

| # | テスト | 検証内容 |
|---|---|---|
| 1 | `test_comment_icon_size_migrated` | 旧既定（100/190・50/140）の設定が新既定（150/240・100/190）へ寄る |
| 2 | `test_custom_icon_size_is_kept` | `comment_icon_size_px` を独自値にしていれば **2 つとも**変更されない（H6 / §3-3） |
| 3 | `test_custom_margin_is_kept` | `comment_margin_l` だけ独自値でも **2 つとも**変更されない |
| 4 | `test_migration_is_idempotent` | 移行後にもう一度読み込んでも変化なし（`changed` が `False`） |
| 5 | `test_icon_stays_inside_canvas_after_migration` | 新既定で `icon_box` の `clamped` が横縦とも `False`（§2.4 の担保） |

### 10-4. 既存テストの回帰

```
python -m unittest discover -s tests
```

特に `tests/test_comment_decor.py`（背景ボックス・ffmpeg フィルタ）と
`tests/test_subtitle_comment_track.py` が通ること。

---

## 11. 将来拡張（本書では実装しない）

| # | 内容 | 備考 |
|---|---|---|
| **F1** | アイコンの大きさを**設定画面から**変えられるようにする | 現在は `setting.json` 直編集。`comment_margin_l` を自動追従させる入力欄にすれば、対で直す必要がなくなる |
| **F2** | 縦動画の最長行で背景ボックスが画面幅に収まらない件（§2.5 / §9 Q5） | `vertical.max_line_length`(15) × `font_size`(90) = 1350px が幅 1080px を超えているのが原因。文字数か文字サイズの見直しが要る |
| **F3** | アイコンを**文字サイズ比**で決める（例：フォントの 3 倍） | 縦横で別々の px を持たずに済むが、既存の px 指定との互換をどう取るか別途設計が要る |
