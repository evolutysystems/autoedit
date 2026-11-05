# ver9 resolve19 — Timeline 画面に「？」ボタンを足し、取り扱い説明の画像を開く

発端: 2026-11-05 のチャット要望 (`docs/request/ver9/request17.md`)
　　　「Timeline 画面で、**「？」マークのボタン**を押したら取り扱い説明を記載した画像を
　　　開くようにしたい。配置は**「元に戻す」ボタンの左側**。使用する画像は
　　　`autoedit/src` 下に配置した **t1.jpg と t2.jpg**。押下するとウィンドウが開き、
　　　そこに t1.jpg と t2.jpg が映っているイメージ。**スクロール**ができるようにしておく」
対象: `src/gui/timeline/help_dialog.py` (**新規**) /
　　　`src/gui/timeline/timeline_editor_dialog.py` (ボタン 1 個と開く処理) /
　　　`src/timeline/builder.py` (`timeline_config` に `help` を追加) /
　　　`src/settings/settings_window.py` (`DEFAULT_SETTINGS["timeline"]["help"]`) /
　　　`src/i18n/catalog_en.py` (新規文言の英訳) /
　　　`src/main_window.spec` · `src/main.spec` (画像の同梱)
調査日: 2026-11-05 (main `1f3fd48` / v1.8.1)
種別: **機能追加の設計**。§1 は現行コードで確認済み
状態: **2026-11-05 実装完了 (実機確認待ち)**。単体テスト 2,272 件 OK。実装記録は §9
前提: ver3 resolve11 §5.10 (同梱画像のパス解決) / ver3 resolve4 §5.3 (アイコンのみのボタン) /
　　　ver8 resolve §3 (i18n の対訳表)

> **番号について**: 要望では `resolve17.md` を指定されていたが、同名のファイルは
> 2026-11-02 に別テーマ (Stripe の加入反映) でクローズ済みで、`1f3fd48` でコミットされている。
> 上書きすると既存の設計記録が消えるため、ver9 の空き番号である **resolve19** で出した。
> 名前を合わせたい場合は指示があればリネームする。

---

## 0. 先に結論

### 0.1 要望 (原文の要点)

| # | 要望 | 対応 |
| --- | --- | --- |
| R1 | Timeline 画面に「？」マークのボタンを置く | §3.2 |
| R2 | 置く場所は「元に戻す」ボタンの**左側** | §3.2 |
| R3 | 押すと**ウィンドウが開く** | §3.3 |
| R4 | そのウィンドウに `src/t1.jpg` と `src/t2.jpg` が映っている | §3.3 / §3.4 |
| R5 | **スクロールできる** | §3.3 |

### 0.2 3 行で

1. **新しい画面を 1 枚足すだけで済む。** 既存の処理 (編集・書き出し・保存) には
   一切触らない。追加するのは `TimelineHelpDialog` (`help_dialog.py`) と、
   それを開くボタン 1 個である。
2. **画像は「縦に並べて `QScrollArea` に入れる」。** 素材は 1920×1080 なので
   等倍で貼ると画面に収まらない。**ビューポートの幅に合わせて縦横比を保って縮小**し、
   小さくしすぎると文字が読めなくなるため**最小幅**を設定で持つ。
   最小幅を下回る窓では横スクロールバーが出る (縮め続けて読めなくするより良い)。
3. **画像の場所は既存の同梱物と同じ方式で解決する。**
   非凍結は `src/`、凍結 (PyInstaller) は `sys._MEIPASS/src` を見る。
   これは `comment_decor.resolve_icon_path` / `blur.models._base_dir` と同じ考え方で、
   **両方の `.spec` に `datas` 行を足す**ことがセットで必要になる (§3.6)。

### 0.3 何が変わるか

| | 変更前 | 変更後 |
| --- | --- | --- |
| Timeline 画面の下部ボタン列 | `元に戻す` `やり直す` … | **`？`** `元に戻す` `やり直す` … |
| 取り扱い説明 | 画面内に無い (ツールチップのみ) | 「？」から画像 2 枚を見られる |
| アーカイブ用 Timeline 画面 | 同上 | **同じボタンが出る** (基底クラスで作るため / §3.2 注) |
| 書き出し・保存・編集 | — | **変化なし** |

---

## 1. 現状の仕組み (調査結果)

### 1.1 ボタン列はどこで組まれているか

`src/gui/timeline/timeline_editor_dialog.py` の `_build_ui` 内、
「下部ボタン」のコメント以下で `button_row` (`QHBoxLayout`) を組んでいる。

```python
# timeline_editor_dialog.py:219-229 (現行)
button_row = QHBoxLayout()
self.undo_button = QPushButton(tr("元に戻す"))
self.undo_button.setToolTip("Ctrl+Z")
self.undo_button.clicked.connect(self._undo)
self.redo_button = QPushButton(tr("やり直す"))
self.redo_button.setToolTip("Ctrl+Y")
self.redo_button.clicked.connect(self._redo)
button_row.addWidget(self.undo_button)
button_row.addWidget(self.redo_button)
button_row.addStretch(1)
```

以降は `addStretch(1)` の**右側**に、保存 / ぼかし / Resolve 出力 / 決定 / キャンセルが並ぶ。
したがって **R2 (「元に戻す」の左)** は `button_row.addWidget(self.undo_button)` の
**直前に 1 行入れるだけ**で満たせる。並びに関する他の分岐 (`if self._save_enabled` など) と
干渉しない。

### 1.2 この画面はアーカイブ用にも継承されている

`archive_timeline_dialog.py` が `TimelineEditorDialog` を継承し、
`_wrap_content` / `_decide_button_text` などのフックだけを差し替えている
(`timeline_editor_dialog.py:296-320` の「派生画面向けのフック」)。
**ボタン列は基底クラスで組まれる**ため、`_build_ui` に足せばアーカイブ用画面にも
自動的に「？」が出る。要望は「Timeline 画面」なので、これは望ましい挙動と判断した
(出し分けが必要なら §4 Q2)。

### 1.3 画像素材は既に置かれている

```
src/t1.jpg   1920×1080  RGB  234,263 byte   (2026-11-05 配置)
src/t2.jpg   1920×1080  RGB  240,803 byte
```

いずれも `src/` 直下。既存の同梱画像 (`src/comment_icon.png` / `src/watermark.png`) と
同じ階層で、パス解決の前例がそのまま使える。

### 1.4 同梱物のパス解決の前例

`src/blur/models.py:24-30`:

```python
# モデル置き場の基準ディレクトリを返す (comment_decor.resolve_icon_path と同じ考え方)
# 非凍結: src/            凍結: sys._MEIPASS/src (datas で同梱したもの)
def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "src")
    # src/blur/models.py → src
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
```

`comment_decor.resolve_icon_path` (`src/modules/comment_decor.py:85-95`) も同形で、
**見つからなければ `None` を返す**点まで共通している。本設計もこれに倣う。

対応する同梱指定は 2 ファイルにある。

* `src/main_window.spec:116-124` — 配布に使う windowed/onedir ビルド (`('comment_icon.png', 'src')` 等)
* `src/main.spec:12-15` — CLI 起点のビルド

### 1.5 `QScrollArea` はこのリポジトリで初使用になる

`src` 配下に `QScrollArea` の利用箇所は無い (`grep` で 0 件)。
一方で**スクロールバーの見た目は既にテーマ側に用意されている** (`theme.py:954-964`):

```
QScrollBar:vertical   { background: transparent; width: 10px; margin: 0px; }
QScrollBar::handle    { background: {glass.border}; … }
```

つまり `QScrollArea` を置けばスクロールバーはテーマに馴染む。
ただし **`QScrollArea` の viewport は既定で不透明に塗られる**ため、
ガラス背景 (`install_window_background`) と併せるなら viewport を透明にする必要がある (§3.3)。

### 1.6 ダイアログの作法

`volume_threshold_dialog.py` が最小形の前例:

* `self.setWindowTitle(tr("…"))`
* `theme.install_window_background(self)` — ガラス背景を取り付ける
* `QVBoxLayout` を root にして組む
* 閉じるボタンは `QPushButton` + `accept` / `reject`

子ウィンドウを開く既存処理 (`_open_blur_spec` / `timeline_editor_dialog.py:808-819`) は
`parent=self` を渡して `dialog.exec()` でモーダル表示している。

### 1.7 設定値の通り道 (ここが見落としやすい)

Timeline 画面は `self.controller.cfg[...]` から設定を読む。この `cfg` は
`src/timeline/builder.py` の **`timeline_config(settings)` が組み直した辞書**で、
**キーをホワイトリストで写している** (`builder.py:385-560`)。

```python
def timeline_config(settings):
    cfg = (settings or {}).get("timeline", {}) or {}
    ui = cfg.get("ui", {}) or {}
    …
    return { "enabled": …, "ui": { … }, "waveform": { … }, … }
```

したがって `setting.json` に `timeline.help` を書いただけでは
**`controller.cfg["help"]` は存在しない**。`timeline_config` に写しを足す必要がある。

なお `settings_window._merge_with_defaults` はセクション単位で既定を複製したうえで
利用者の値を重ねるため、**`timeline.help` を `DEFAULT_SETTINGS` に足せば
既存の `setting.json` にも自動で現れる** (`settings_window.py:1798-1820`)。
`_has_new_keys` が新規キーを検知して書き戻すので、移行コードは不要である。

---

## 2. 設計方針

### 2.1 やること

1. 取り扱い説明の画面を**独立したモジュール** (`src/gui/timeline/help_dialog.py`) にする。
   Timeline 画面はボタンを置いて呼ぶだけに留める (`docs/CLAUDE.md` 「モジュール化」)。
2. **画像のファイル名・並び順・窓の大きさを `setting.json` で持つ**
   (`docs/CLAUDE.md` 「ハードコードは禁止」)。将来ページを 3 枚に増やす、
   日本語版 / 英語版を差し替える、といった変更をコード改修なしで行えるようにする。
3. 画像が**見つからないときも画面を壊さない**。欠けたページは案内文へ置き換える (§3.5)。

### 2.2 やらないこと

| 見送るもの | 理由 |
| --- | --- |
| 画像の拡大縮小 (ズーム) ・ピンチ操作 | 要望はスクロールのみ。まず素のスクロールで出し、要望が来たら足す |
| 説明文の HTML / Markdown 化 | 要望は「画像を開く」。画像 1 本に絞ると差し替えが容易 |
| メイン画面 (`main_window`) への「？」追加 | 要望は Timeline 画面に限定 |
| `t1.jpg` / `t2.jpg` のリネーム | 要望がファイル名を明示している。並びは設定で持つため後から改名可能 (§4 Q1) |
| 窓の大きさ・位置の記憶 | 説明を読むだけの窓であり、`window_geometry` の仕組み (ver8 resolve3) を広げる価値が薄い |

---

## 3. 実装設計

### 3.1 全体像

```
  Timeline 編集画面 (timeline_editor_dialog.py)
    下部ボタン列  [ ？ ] [ 元に戻す ] [ やり直す ] … [ 決定 ] [ キャンセル ]
        │ clicked
        ▼
    _open_help()                      ← 遅延 import してダイアログを exec()
        │
        ▼
  TimelineHelpDialog (help_dialog.py / 新規)
    ┌──────────────────────────────┐
    │ QVBoxLayout (root)           │
    │  ┌────────────────────────┐  │
    │  │ QScrollArea            │  │ ← viewport 透明 / 縦横ともスクロール可
    │  │  └ QWidget (container) │  │
    │  │      QVBoxLayout       │  │
    │  │        _HelpPage(t1.jpg)│ │ ← QLabel 派生。幅に合わせて縮小して描く
    │  │        _HelpPage(t2.jpg)│ │
    │  └────────────────────────┘  │
    │  [ 閉じる ]                   │
    └──────────────────────────────┘
        │
        └─ 画像の実体パス解決 … resolve_image_path()
             非凍結: <repo>/src/<name>
             凍結  : sys._MEIPASS/src/<name>
```

### 3.2 ボタンを置く (R1 / R2)

`_build_ui` の `button_row` を組む箇所へ、**`undo_button` を足す前に**挿す。

```python
# 取り扱い説明 (ver9 resolve19 §3.2)。
# 置き場所は「元に戻す」の左 (要望 R2)。記号 1 文字のボタンなので
# iconOnly 扱いにして左右の余白を詰める (ver3 resolve4 §5.3)。
# help.enabled が False、または説明画像が 1 枚も見つからないときは出さない。
# 「押しても何も起きないボタン」を残さないため、ここで入口を閉じる。
self.help_button = None
if help_dialog.is_enabled(self.controller.cfg["help"]):
    self.help_button = QPushButton("？")
    self.help_button.setAutoDefault(False)
    self.help_button.setToolTip(tr("この画面の使い方を開きます"))
    self.help_button.setFixedWidth(
        int(self.controller.cfg["help"]["button_width_px"]))
    theme.mark_icon_button(self.help_button)
    self.help_button.clicked.connect(self._open_help)
    button_row.addWidget(self.help_button)

button_row.addWidget(self.undo_button)       # ← 既存行 (そのまま)
```

設計上の決め事:

* **ラベルは全角の `？`**。半角 `?` だと和文フォントの幅で細く見えるため。
  表示だけの記号で翻訳不要なので `tr()` は通さない (`？` は英語版でも同じで良い)。
* **`setAutoDefault(False)`** を立てる。既存の `save_button` / `blur_button` と同じで、
  Enter の取りこぼしで説明が開くのを防ぐ。
* **`_on_playing_changed` では無効化しない** (`timeline_editor_dialog.py:898-905`)。
  再生中に編集を止めているのは状態の競合を避けるためで、説明を読むのは競合しない。
  `undo_button` / `redo_button` の 2 行は現行どおり、`help_button` は触らない。
* **`_update_history_buttons` も触らない**。履歴の有無と関係しない。
* `theme.mark_icon_button` は `iconOnly` プロパティを立てて QSS の左右余白を詰める
  既存ヘルパ (`theme.py` / ver3 resolve4 §5.3)。ズームボタン (`－` `＋`) と同じ扱いにする。

> 注: 前述のとおりアーカイブ用画面にも同じボタンが出る (§1.2)。
> 同じ Timeline の操作説明なので、出すほうが自然と判断した。

`_open_help` は既存の `_open_blur_spec` と同じ形で置く。

```python
# 取り扱い説明の画面を開く (ver9 resolve19 §3.3)
def _open_help(self):
    from .help_dialog import TimelineHelpDialog      # noqa: PLC0415

    dialog = TimelineHelpDialog(self.controller.cfg["help"], parent=self)
    dialog.exec()
```

`exec()` でモーダルにする理由: Timeline 画面自体がモーダルなため、
非モーダルで開くと**親の背後へ隠れて行方不明になりやすい**。
説明を見ながら編集したい要望が出たら `show()` + 参照保持へ変える (§8)。

### 3.3 説明画面 (R3 / R5)

新規 `src/gui/timeline/help_dialog.py`。

| 要素 | 決め方 |
| --- | --- |
| 窓の大きさ | `help.window_width` / `help.window_height` (既定 980×760) を `resize` で初期値に。最小は `help.window_min_width_px` / `..._min_height_px` |
| 背景 | `theme.install_window_background(self)` (既存ダイアログと同じ) |
| スクロール | `QScrollArea`。縦横とも `ScrollBarAsNeeded` |
| viewport | `setFrameShape(QFrame.NoFrame)` + `viewport().setAutoFillBackground(False)` でガラス背景を透かす (§1.5) |
| 画像の並び | `QVBoxLayout` に設定の順で縦積み。間隔は `help.image_spacing_px` |
| 画像の幅 | ビューポート幅 − 余白に合わせ、**縦横比を保って縮小**。拡大はしない (等倍が上限) |
| 画像の最小幅 | `help.image_min_width_px` (既定 640)。これ未満には縮めず、横スクロールで見せる |
| 閉じるボタン | 右下に `閉じる`。`setDefault(True)` + `Esc` (QDialog 既定) で閉じられる |

要点となる 2 つの処理。

```python
# 説明画像 1 枚を「幅に合わせて縮めて」描くラベル (§3.3)
# 原寸 (1920x1080) のまま貼ると窓に収まらないため、与えられた幅に合わせて
# 縦横比を保って縮小する。拡大はしない (粗くなるだけで読みやすくならない)。
class _HelpPage(QLabel):

    def __init__(self, pixmap, min_width_px, parent=None):
        super().__init__(parent)
        self._source = pixmap
        self._min_width_px = max(int(min_width_px), 1)
        self._applied_width = 0
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setMinimumWidth(self._min_width_px)

    # 表示幅を決めて貼り直す。呼び出し側 (ダイアログ) が resize のたびに呼ぶ。
    # 同じ幅で呼ばれたら何もしない (resizeEvent の再入で無限に貼り直さないため)。
    def apply_width(self, width_px):
        width = max(int(width_px), self._min_width_px)
        width = min(width, self._source.width())          # 等倍を上限にする
        if width == self._applied_width:
            return
        self._applied_width = width
        scaled = self._source.scaledToWidth(width, Qt.SmoothTransformation)
        self.setPixmap(scaled)
        # 高さは縮小後の実寸に合わせる (縦スクロールの総量がこれで決まる)
        self.setFixedHeight(scaled.height())
```

```python
# 窓の幅が変わったら各ページを貼り直す (§3.3)
# QScrollArea の viewport 幅から左右の余白を除いた幅を配る。
def resizeEvent(self, event):
    super().resizeEvent(event)
    self._relayout_pages()

def _relayout_pages(self):
    width = self._scroll.viewport().width() - self._margin_px * 2
    for page in self._pages:
        page.apply_width(width)
```

### 3.4 画像のパス解決 (R4)

`help_dialog.py` に、§1.4 と同形の解決を置く。

```python
# 説明画像の置き場の基準ディレクトリを返す
# (comment_decor.resolve_icon_path / blur.models._base_dir と同じ考え方)
# 非凍結: src/                 … このファイルは src/gui/timeline/ なので 3 つ上
# 凍結  : sys._MEIPASS/src     … .spec の datas で同梱したもの (§3.6)
def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "src")
    here = os.path.dirname(os.path.abspath(__file__))        # src/gui/timeline
    return os.path.dirname(os.path.dirname(here))            # src


# 設定のファイル名 (src/ からの相対、または絶対パス) を実体パスへ直す。
# 見つからなければ None を返す (欠けたページは案内文に置き換える / §3.5)。
# 相対指定が src/ の外へ出るものは受け付けない (設定経由で任意のファイルを
# 開く口にしないため)。
def resolve_image_path(name):
    text = str(name or "").strip()
    if not text:
        return None
    if os.path.isabs(text):
        return text if os.path.exists(text) else None
    base = _base_dir()
    path = os.path.normpath(os.path.join(base, text))
    if os.path.commonpath([base, path]) != os.path.normpath(base):
        _logger.warning("説明画像の指定が src の外を指しています: %r", name)
        return None
    return path if os.path.exists(path) else None
```

`is_enabled(help_cfg)` も同じモジュールに置く。
**`enabled` が真でも、読める画像が 1 枚も無ければ `False`** を返す
— §3.2 のボタン表示判定とここを 1 本にまとめ、
「ボタンはあるが空の窓が開く」状態を作らない。

```python
# 「？」ボタンを出すか (§3.2)。設定の enabled と、読める画像が 1 枚以上あること。
def is_enabled(help_cfg):
    if not (help_cfg or {}).get("enabled", True):
        return False
    return any(resolve_image_path(name)
               for name in (help_cfg or {}).get("images", ()))
```

### 3.5 画像が読めないとき (エラー処理方針)

| 状況 | 振る舞い |
| --- | --- |
| `images` が空 / 全て見つからない | 「？」ボタンを**出さない** (§3.4 `is_enabled`) |
| 一部のページだけ見つからない | 見つかったページだけ並べ、欠けたぶんは `tr("説明画像が見つかりません: {name}")` のラベルを置く。ログへ `warning` |
| ファイルはあるが `QPixmap` が読めない (壊れている / 非対応形式) | 同上。`pixmap.isNull()` で判定 |
| `images` に `..` などが入っている | `src/` の外を指すものは捨てて `warning` (§3.4) |

既存方針 (`ver8 resolve §7` / 文言が英語にならないことはあっても画面は壊さない) に揃え、
**説明が出ないことはあっても編集作業は止めない**。例外は握って `_logger.exception` へ流し、
Timeline 画面側へは伝播させない。

ログは既存の `get_logger(__name__)` を使う (`docs/CLAUDE.md` のログ方針どおり、
モジュール単位のロガー)。出すのは「画像が見つからない」「読めない」「設定が不正」の 3 種のみで、
正常時は無言とする。

### 3.6 同梱 (ビルド)

両方の `.spec` の `datas` に足す。これを忘れると**開発環境では動き、配布版だけ
「？」が消える** (= `is_enabled` が偽になる) という気づきにくい差が出る。

`src/main_window.spec` (`('watermark.png', 'src')` の隣):

```python
# Timeline 画面の取り扱い説明 (ver9 resolve19 §3.6)。
# help_dialog.resolve_image_path が _internal/src/t1.jpg を解決する。
# setting.json の timeline.help.images で増やしたときは、ここにも足す。
('t1.jpg', 'src'),
('t2.jpg', 'src'),
```

`src/main.spec` にも同じ 2 行を足す。

> 枚数を増やすたびに `.spec` を直すのが面倒であれば、
> `*[(p, 'src') for p in glob('t*.jpg')]` のようなまとめ方もできる
> (`models/yolox_tiny.onnx` で前例あり)。ただし**無関係な `t*.jpg` を拾う危険**が
> あるため、本設計では**明示列挙**を採る。

### 3.7 設定 (`setting.json`)

`timeline` の下に `help` を新設する。`ui` へ混ぜない理由は、
`ui` が寸法の置き場であるのに対し、こちらは**素材の指定を含む 1 機能のまとまり**であり、
`waveform` / `paste` / `project` と同じ粒度だからである。

```jsonc
"timeline": {
    …
    "help": {
        // 「？」ボタンを出すか。false で機能ごと閉じる
        "enabled": true,
        // 説明画像。src/ からの相対パス (絶対パスも可)。この順に縦へ並ぶ。
        // 増やしたときは .spec の datas にも足す (§3.6)
        "images": ["t1.jpg", "t2.jpg"],
        // 「？」ボタンの幅 (記号 1 文字ぶん / ズームボタンと同じ考え方)
        "button_width_px": 32,
        // 説明の窓を開いたときの大きさ
        "window_width": 980,
        "window_height": 760,
        // 窓を縮められる下限
        "window_min_width_px": 480,
        "window_min_height_px": 360,
        // 画像をこれ未満には縮めない。下回る窓では横スクロールで見せる
        "image_min_width_px": 640,
        // 画像どうしの縦の間隔
        "image_spacing_px": 12,
        // 画像の左右・上下に取る余白
        "image_margin_px": 12
    }
}
```

同じ構造を 2 か所へ置く (既存の追加手順と同じ)。

1. `src/settings/settings_window.py` の `DEFAULT_SETTINGS["timeline"]` — 日本語コメント付き
2. `src/settings/setting.json` — 既存ファイルにも書く
   (`_merge_with_defaults` + `_has_new_keys` が自動で書き戻すので**無くても動く**が、
   手元の設定を読んだときに差が出ないよう明記する)

`src/timeline/builder.py` の `timeline_config` へ写しを足す (§1.7 が本題)。

```python
    help_cfg = cfg.get("help", {}) or {}
    …
    return {
        …
        # Timeline 画面の取り扱い説明 (ver9 resolve19 §3.7)
        "help": {
            "enabled": bool(help_cfg.get("enabled", True)),
            # 並べる画像。文字列以外・空文字・重複は捨てる
            "images": _help_images(help_cfg.get("images")),
            "button_width_px": _positive_int(help_cfg.get("button_width_px"), 32),
            "window_width": _positive_int(help_cfg.get("window_width"), 980),
            "window_height": _positive_int(help_cfg.get("window_height"), 760),
            "window_min_width_px": _positive_int(
                help_cfg.get("window_min_width_px"), 480),
            "window_min_height_px": _positive_int(
                help_cfg.get("window_min_height_px"), 360),
            "image_min_width_px": _positive_int(
                help_cfg.get("image_min_width_px"), 640),
            "image_spacing_px": _positive_int(help_cfg.get("image_spacing_px"), 12),
            "image_margin_px": _positive_int(help_cfg.get("image_margin_px"), 12),
        },
    }
```

```python
# 説明画像の一覧を正規化する (ver9 resolve19 §3.7)
# 文字列以外・空文字・重複を捨て、指定順を保つ。既定は t1.jpg → t2.jpg。
def _help_images(value):
    if value is None:
        return list(_HELP_IMAGES_DEFAULT)
    if not isinstance(value, (list, tuple)):
        _logger.warning("説明画像の設定が一覧ではないため既定を使用します: %r", value)
        return list(_HELP_IMAGES_DEFAULT)
    names = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in names:
            names.append(text)
    return names
```

既定値 (`_HELP_IMAGES_DEFAULT = ("t1.jpg", "t2.jpg")`) は `builder.py` の
モジュール定数にして 1 か所に寄せる。`DEFAULT_SETTINGS` 側にも同じ既定が出るが、
これは他のキー (`window_width` など) も同じ形であり、既存の作りに合わせた。

### 3.8 文言 (i18n)

`tr()` を通す新規文言は次の 4 つ。`src/i18n/catalog_en.py` へ原文順で追記する
(`tests/test_i18n.py` が漏れを検出する / ver8 resolve §3)。

| 原文 | 英訳 |
| --- | --- |
| `この画面の使い方を開きます` | `Open the guide for this screen` |
| `使い方` | `How to use` |
| `説明画像が見つかりません: {name}` | `Guide image not found: {name}` |
| `閉じる` | 既存キーがあれば流用。無ければ `Close` |

窓のタイトルは `tr("使い方")` とする (要望の「取り扱い説明」でもよい / §4 Q3)。

---

## 4. 判断が要る点

| # | 論点 | 本設計の既定 | 変えたい場合 |
| --- | --- | --- | --- |
| Q1 | `t1.jpg` / `t2.jpg` という名前のままにするか | **要望どおり維持**。並びは `timeline.help.images` で持つため、改名は設定 2 行と `.spec` 2 行の変更で済む | `help_timeline_1.jpg` のような名前にするなら、設定と `.spec` を合わせて直す |
| Q2 | アーカイブ用 Timeline 画面にも「？」を出すか | **出す** (基底クラスで作るため自然にそうなる) | 出し分けるなら `_help_enabled()` フックを `_wrap_content` 等と同じ並びに足す |
| Q3 | 窓のタイトル | `使い方` | 「取り扱い説明」を使うなら文言と対訳表を差し替える |
| Q4 | 説明画像を言語で切り替えるか | **切り替えない** (1 組のみ) | 切り替えるなら `images` を言語別の辞書にし、`i18n.language()` で引く |
| Q5 | 画像をクリックで等倍 / 拡大表示するか | **しない** (§2.2) | 要望があれば `_HelpPage` に `mousePressEvent` を足す |

いずれも**推測で実装せず、本節に記載した** (`docs/CLAUDE.md` 「不明点がある場合は推測実装せず設計書へ記載」)。
Q1〜Q3 は実装前に一言もらえれば手戻りなく反映できる。

---

## 5. 変更ファイル

| ファイル | 変更 | 概要 |
| --- | --- | --- |
| `src/gui/timeline/help_dialog.py` | **新規** | `TimelineHelpDialog` / `_HelpPage` / `is_enabled` / `resolve_image_path` / `_base_dir` |
| `src/gui/timeline/timeline_editor_dialog.py` | 変更 | `_build_ui` に「？」ボタン (約 12 行) と `_open_help` (約 5 行) |
| `src/timeline/builder.py` | 変更 | `timeline_config` に `help` ブロック / `_help_images` ヘルパ |
| `src/settings/settings_window.py` | 変更 | `DEFAULT_SETTINGS["timeline"]["help"]` を追加 |
| `src/settings/setting.json` | 変更 | 同じ `help` ブロックを追記 |
| `src/i18n/catalog_en.py` | 変更 | 新規文言 3〜4 件の英訳 (§3.8) |
| `src/main_window.spec` | 変更 | `datas` に `t1.jpg` / `t2.jpg` |
| `src/main.spec` | 変更 | 同上 |
| `tests/test_timeline_help.py` | **新規** | §6 のテスト |

**削除・置き換えは無い。** 既存の関数・クラスの振る舞いは変えない。

---

## 6. テスト計画

`tests/test_timeline_help.py` を新規に置く。GUI を触るため
`QT_QPA_PLATFORM=offscreen` を立てて `QApplication` を 1 つ用意する
(`tests/test_blur_dialog.py` と同じ作法)。実行は `python -m unittest discover -s tests`。

| # | 確認すること | 要望 |
| --- | --- | --- |
| T1 | 下部ボタン列で `help_button` が `undo_button` の**直前**にある (`indexOf` の比較) | R1 / R2 |
| T2 | 「？」を押すと `TimelineHelpDialog` が生成される (`exec` を差し替えて確認) | R3 |
| T3 | ダイアログの中に設定の順 (`t1.jpg` → `t2.jpg`) でページが並ぶ | R4 |
| T4 | `QScrollArea` があり、縦のスクロール方針が `ScrollBarAsNeeded` である | R5 |
| T5 | 窓を狭くすると画像幅が追従し、`image_min_width_px` 未満には縮まない。`apply_width` が同じ幅では貼り直さない | R5 |
| T6 | `resolve_image_path`: 相対名 → `src/` 下、絶対パス、存在しない名前 (`None`)、`..` で外へ出る名前 (`None`) | §3.4 |
| T7 | `is_enabled`: `enabled=false` で偽 / `images=[]` で偽 / 1 枚でも読めれば真 | §3.4 |
| T8 | 片方の画像が無いとき、残り 1 枚 + 案内ラベルで開ける (例外を出さない) | §3.5 |
| T9 | `timeline_config` が `help` を返し、壊れた値 (文字列・負数・`images` が辞書) でも既定へ落ちる | §3.7 |
| T10 | `t1.jpg` / `t2.jpg` が `src/` に実在し、両方の `.spec` の `datas` に書かれている | §3.6 |

既存テストへの影響:

* `tests/test_i18n.py` — 新規文言の対訳が必要 (§3.8)。これを入れ忘れると**既存テストが落ちる**
* `tests/test_settings_migration.py` — `timeline.help` の自動補完が既存の判定に乗るか確認する

実装前の基準は **2,237 件** (`ver9 resolve18 §9` 時点)。T1〜T10 ぶんが増える見込み。

---

## 7. 互換性

| 観点 | 影響 |
| --- | --- |
| 既存の `setting.json` | `timeline.help` は `_merge_with_defaults` が補完し、次回保存で書き戻される。移行コード不要 (§1.7) |
| `timeline.enabled=false` (旧字幕編集画面) | Timeline 画面を使わないため影響なし。「？」も出ない |
| 保存済みプロジェクトファイル | 触らない (画面の表示だけの変更) |
| 出力される動画 | 変化なし。書き出し経路に一切触らない |
| 配布サイズ | `t1.jpg` + `t2.jpg` で約 **0.46 MB** 増 |
| `help.enabled=false` にした場合 | 「？」が出ないだけで、他は現行と完全に同じ |
| 追加ライブラリ | **なし** (PySide6 の `QScrollArea` / `QPixmap` のみ) |

---

## 8. 残課題 (将来)

1. **同梱物のパス解決が 3 か所に分かれた** (`comment_decor` / `blur.models` / `help_dialog`)。
   `src/utils/` へ `resource_path(name)` として括り出すのが筋だが、
   既存 2 か所を触ると回帰の範囲が広がるため本設計では**同形で並べるだけ**に留めた。
2. 説明を**非モーダルで開く** (読みながら編集する) 要望が出たら、
   `show()` + 参照保持 + `WA_DeleteOnClose` へ切り替える。
   このとき `ver9 resolve17 §1` で踏んだ「閉じても `destroyed` が飛ばない」罠に注意する。
3. 画像の**ズーム / 等倍表示**、ページ間の目次、メイン画面からの参照 (§2.2 の見送り分)。
4. 説明画像の**多言語版** (Q4)。
5. 説明画像の**内容そのもの**は本設計の対象外。差し替えは `src/t1.jpg` / `src/t2.jpg` を
   置き換えるだけでよく、コード変更は要らない。

---

## 9. 実装結果 (2026-11-05)

### 9.1 変更したファイル

| ファイル | 変更 | 中身 |
| --- | --- | --- |
| `src/gui/timeline/help_dialog.py` | **新規 (約 220 行)** | `_base_dir` / `resolve_image_path` / `is_enabled` / `install_button` / `_HelpPage` / `TimelineHelpDialog` |
| `src/gui/timeline/timeline_editor_dialog.py` | 変更 (+11 行) | `help_dialog` の import / ボタン設置 1 文 / `_open_help` |
| `src/timeline/builder.py` | 変更 (+45 行) | `_HELP_IMAGES_DEFAULT` / `_help_images` / `timeline_config` の `help` ブロック |
| `src/settings/settings_window.py` | 変更 (+24 行) | `DEFAULT_SETTINGS["timeline"]["help"]` |
| `src/settings/setting.json` | 変更 (+15 行) | 同じ `help` ブロック (`ui` の直後) |
| `src/i18n/catalog_en.py` | 変更 (+3 行) | `この画面の使い方を開きます` / `使い方` / `説明画像が見つかりません: {name}` |
| `src/main_window.spec` · `src/main.spec` | 変更 (各 +4〜6 行) | `datas` に `t1.jpg` / `t2.jpg` |
| `tests/test_timeline_help.py` | **新規 (35 件)** | §6 の T1〜T10 + §9.3 の回帰 |

既存の関数・クラスの削除・置き換えは無い。`閉じる` は対訳表に既存のキーがあったため流用した
(設計 §3.8 の想定どおり)。

### 9.2 設計から変えた点

| # | 設計 | 実装 | 理由 |
| --- | --- | --- | --- |
| 1 | `_build_ui` の中でボタンを直接組む (§3.2 のコード片) | **`help_dialog.install_button(button_row, cfg, on_clicked, parent)` へ括り出した** | 画面側が 1 文で済み、**ボタンの並び順を単体テストで検査できる**ようになる。`timeline_editor_dialog.py` は「置いて呼ぶだけ」という §2.1 の方針にも合う |
| 2 | 窓の `resizeEvent` で貼り直す (§3.3) | **ビューポートへ `installEventFilter` して `QEvent.Resize` を拾う** | §9.3 の不具合。窓の `resizeEvent` ではビューポートの幅がまだ古い |
| 3 | 再入防止は `apply_width` の幅の覚えだけ | **`_relayout_pages` にも `_relayouting` の印を足した** | 貼り直すとスクロールバーの有無が変わってビューポートが動くため、幅が往復する形の入れ子を確実に断つ |

`enabled` / `images` / 寸法のキー構成、パス解決、エラー時の振る舞いは設計どおり。

### 9.3 実装中に見つかった不具合 (設計に無かった前提)

**ビューポートの幅が決まるのは、窓の `resizeEvent` が呼ばれた後である。**

設計 §3.3 のとおり窓側の `resizeEvent` で貼り直すと、**開いた直後の 1 回が
「まだ組み立て中のビューポート幅」で走る**。実測では窓が 980px のとき
ビューポートは **98px** しかなく、最小幅 640px に丸められたまま二度と更新されなかった
(窓の大きさが変わらない限り `resizeEvent` が来ないため)。

```
# 修正前
after init: dlg 980 viewport  98
0           dlg 980 viewport 944 applied [640, 640]   ← 944 あるのに 640 のまま
# 修正後
0           dlg 980 viewport 944 applied [920, 920]
resize 0    dlg 900 viewport 864 applied [840, 840]
```

ビューポート自身の `QEvent.Resize` を拾う形へ変えて解消した。
窓側の `resizeEvent` は**削除した** — 残すと古い幅で 1 度貼ってから正しい幅で貼り直すことになり、
1920px の `SmoothTransformation` が毎回 2 回走って無駄になるため。

回帰テストとして `test_pages_fill_viewport_on_open` / `test_pages_follow_resize` を追加した。

### 9.4 実機での確認 (オフスクリーン描画)

| 確認 | 結果 |
| --- | --- |
| ボタンの並び | `？` が index 0、その右に `元に戻す` `やり直す` |
| `？` の幅 | 全角 `？` の送り幅は **12px**、`iconOnly` の余白 6+6px と枠 2px で計 26px。設定の 32px に収まり見切れない (ズームボタンの `－` と同じ送り幅) |
| 説明の窓 (980×760) | t1.jpg → t2.jpg の順に表示。画像幅 844px、縦スクロール量 340px |
| 窓を 520×420 へ狭めたとき | 画像幅は最小幅 640px で止まり、横スクロール量 176px が出る (設計どおり) |
| 設定の読み出し | `timeline_config(load_settings())["help"]` が既定どおり返る |

### 9.5 テスト

```
python -m unittest discover -s tests
Ran 2272 tests in 25.121s
OK
```

実装前の基準は 2,237 件 (`ver9 resolve18 §9`)。**増えた 35 件が新規テスト**で、
既存テストの修正は不要だった (対訳表を同時に足したため `test_i18n` も通る)。

### 9.6 要望との対応

| # | 要望 | 結果 |
| --- | --- | --- |
| R1 | 「？」マークのボタンを置く | ✅ 下部ボタン列の左端 |
| R2 | 「元に戻す」の左側 | ✅ `test_button_comes_before_undo` / `test_editor_dialog_calls_before_undo` |
| R3 | 押すとウィンドウが開く | ✅ `TimelineHelpDialog` をモーダルで開く |
| R4 | t1.jpg と t2.jpg が映っている | ✅ 設定の順に縦へ並ぶ |
| R5 | スクロールできる | ✅ 縦横とも必要なときにバーが出る |

### 9.7 残っている確認 (実機)

* 実際に起動して「？」を押し、説明が読める大きさで出るか (オフスクリーン描画では
  和文フォントが無く文字が豆腐になるため、文字の見え方だけは実機で見たい)
* 配布ビルド (`main_window.spec`) で「？」が出るか — `datas` を足したので出るはずだが、
  §3.6 のとおり**ここだけは実際にビルドしないと分からない**
