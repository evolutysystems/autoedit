# AI Clip Studio (docs/AiEditor/resolve1.md)
#
# このパッケージは **Qt に依存しない** (CLI とテストから同じ判断を再現できる)。
# gui/ を import してはならず、timeline/ modules/ archive/ からは import されない
# (依存は一方通行 / §4)。
#
# `ai.enabled = false` のときは呼び出し側がこのパッケージを import しないため、
# ここでは重いモジュールを読み込まない (各モジュールを必要になった時点で import する)。
