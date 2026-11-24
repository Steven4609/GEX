# GEX 小报 · GitHub 版

SPY / QQQ 和自选股的伽马敞口（GEX）小报。**每个交易日美东 18:10 左右自动更新一次**，网页上也可以随时手动更新。
算法与 Claude 定时任务里的 GEX 小结完全一致（同一套墙位、零伽马位、触发位和几率规则），两边可以互相对照。

- 网页：`https://<你的用户名>.github.io/GEX/`（顶部两个按钮切换「SPY / QQQ」和「自选股」两页；右上角可切换中文 / English）
- 数据：[`data/`](data/) 文件夹；跟踪表 [`data/tracking.csv`](data/tracking.csv) 可直接用 Excel 打开

## 文件说明

| 文件 | 内容 |
|---|---|
| `index.html` | 网页 |
| `config/watchlist.txt` | 自选股清单（网页上改了自选股并点“更新”时，会自动改写） |
| `data/latest.json` | 最新一份报告的全部数据 |
| `data/reports/*.json` | 历史报告（保留 30 天），网页右上角下拉菜单可切换最近 5 份 |
| `data/translate_cache.json` | 新闻标题的中文翻译缓存 |
| `data/tracking.csv` | 触发位跟踪表：每份报告的触发位、预测几率，盘后自动回填实际结果 |
| `scripts/run.py` | 主脚本：判断交易日和时段、抓期权链、写结果、更新跟踪表 |
| `scripts/compute.mjs` | 计算核心（与 Claude 定时任务的算法逐行一致） |
| `scripts/events.py` | 事件栏（经济数据、联储讲话、财报、异动股新闻）和自选股要闻 |
| `scripts/probe.py` | 数据源连通测试（由 probe.yml 调用） |
| `.github/workflows/update.yml` | 定时和手动更新任务 |
| `.github/workflows/probe.yml` | 一次性测试：GitHub 服务器能否连上各数据源 |

## 数据源

- **期权链**：CBOE 延迟行情（约 15 分钟延迟）。CBOE 连不上时自动改用 Yahoo Finance：Yahoo 不提供 gamma，
  脚本用 Black-Scholes 按 Yahoo 的隐含波动率自算；Yahoo 盘后的隐含波动率有时失真，所以卡片会标明数据源。
- **事件**：Nasdaq 经济日历和财报日历（有实际值和预期值）；取不到时用 ForexFactory 周历（只有预期值）。
  异动股（大型科技股和自选股中涨跌超过 3% 的）附 Yahoo Finance 最新一条英文标题。
  没有 AI 判断，政策类突发新闻不收；这部分仍以 Claude 定时任务为准。
- **自选股要闻**：近 3 天 CNBC、Bloomberg、Reuters、WSJ、MarketWatch、Barron's、FT、Yahoo Finance 等的标题和链接
  （经 Google News 和 Yahoo Finance RSS），按标题关键词打标签；财报信息来自 Nasdaq 和 Yahoo。
- **标题翻译**：Google 翻译网页接口（机器翻译，偶有错误；网页同时显示英文原标题）。

## 日常怎么用

- **看更新是否成功**：仓库顶部 **Actions** 标签，绿勾成功、红叉失败（点进去看原因）。
- **网页手动更新**：第一次需要在网页“设置”里填一个只授权给本仓库的 GitHub 令牌（网页上有步骤说明），令牌只存在你这台设备的浏览器里。
  之后改自选股、点“更新”，1–3 分钟后网页自动刷新（更新过程中关掉或刷新网页都没关系）。
- **不用令牌也能手动更新**：Actions → 左侧 **GEX update** → 右侧 **Run workflow**，可填自选股代码。
- 周末和休市日不会自动更新；这时手动更新得到的是最近一个交易日收盘后的数据。

## 口径（与 Claude 定时任务相同）

- 净伽马 = 各合约 gamma × 未平仓量 × 100 × 现价² × 1%，call 为正、put 为负（假设做市商多 call、空 put）。
- 零伽马位 = 现价 ±7% 内逐档用 Black-Scholes 重算净伽马，离现价最近的过零点。
- Call 墙 = 现价及上方 8% 内 call 伽马最大的行权价；Put 墙 = 现价及下方 8% 内 put 伽马最大的行权价。
- 几率 = 零漂移随机游走，波动率取目标日平值期权隐含波动率，按交易时间折算；盘中碰到 = 2 × 收在外侧。
- 杠杆 ETF（SPXL / SPXS / TQQQ / SQQQ）价格只对 SPY / QQQ 计算。

不构成投资建议。
