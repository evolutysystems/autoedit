# docs/request — 要望と設計書 (履歴)

このフォルダは **要望ごとの記録**であり、現行仕様書ではない。

* `verN/requestM.md` = 利用者・開発者からの要望
* `verN/resolveM.md` = その要望に対する設計書 (`docs/CLAUDE.md`「実装前に設計を行うこと」)
* `verN` は要望のまとまり。番号が大きいほど新しい。同じ主題でも**後の ver の決定が有効**

各 ver の主題は `docs/README.md` §3.1 の一覧にまとめてある。

> **読み方**: ここにある値・既定値・モジュール名は**その時点のもの**である。
> 現行仕様は `docs/design/design.md`、設定の既定値は
> `src/settings/settings_window.py` の `DEFAULT_SETTINGS` を見ること。
> 文書全体の索引と「新旧が反転した決定」の一覧は `docs/README.md` にある。

新しい要望を追加するときは `verN/requestM.md` + `verN/resolveM.md` を置き、
**実装後に現行仕様へ影響する分を `docs/design/design.md` へ反映する**こと。
