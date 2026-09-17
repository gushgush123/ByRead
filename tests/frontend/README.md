# 白读 · 前端回归测试（可选）

只测两个曾经真实出现过的**频道状态 bug**，用真实页面 + 真实 JS 跑，不是逻辑单测：

| bug | 现象 | 根因 |
|---|---|---|
| 1 | 进频道后点「全部」，列表还是只有那个频道的文章 | `selectView()` 没清 `state.channelId`，`load()` 仍然带着 `&channel=` |
| 2 | 快速切频道，A 高亮着、内容是 B 的（可稳定复现） | `load()` 开头 `if (state.loading) return` 把后一次点击**丢掉**了，而先发的响应回来后又无条件写进列表 |

## 怎么跑

需要 **Node 18+**（只在测试里用到，应用本身仍然只依赖 Python）：

```bash
cd tests/frontend
npm install                 # 只装 jsdom 一个包
node channel.test.js        # 4 个场景、9 条断言
node channel_monkey.test.js # 猴子测试：随机乱点 + 随机网络延迟
```

**前提：应用正在运行**（`python app.py`，默认 http://127.0.0.1:5000），
而且要至少有 **2 个频道**、且它们的内容不一样（否则测不出区别）。

测试会：
1. 用 jsdom 打开真实首页，把 `static/js/main.js` 内联进去（jsdom 29 没有 ResourceLoader，
   内联比拦请求更直接），其余资源（common.js、样式）仍从真服务加载；
2. 用 Node 的 fetch 顶替 jsdom 缺失的 `fetch`，并给 `/api/articles` 的响应加**人为延迟**——
   这样"旧响应晚回来"是确定性的，不靠手速；
3. 场景测试断言"点 A 出 A、点全部出全部、乱序切换后高亮与内容一致"；
4. 猴子测试每轮随机点 3~6 次（频道 / 全部 / 未读 / 星标 / 收藏夹），
   等所有请求落地后校验一条硬不变量：

> **最后发出的那个 `/api/articles` 请求，必须就是屏幕上显示的内容。**
> （顺带校验侧边栏高亮与最后一次请求的参数一致；在收藏夹里时按规则不高亮星标，这条不算违规。）

## 当初的实测结果（这就是修这个 bug 的依据）

| 版本 | 场景测试 | 猴子测试（30 轮随机乱点 + 随机延迟） |
|---|---|---|
| 修复前 | 4/9 通过（两个 bug 全部复现） | **29/30 轮不一致** |
| 修复后 | 9/9 通过 | **0/30 轮不一致**（60 轮也是 0） |

## 想 A/B 对比某次改动

可以把旧版本单独跑一遍（脚本自己去 git 里取，避免被终端重新编码）：

```bash
OLD_MAIN=HEAD:static/js/main.js node channel.test.js old        # 取 git HEAD 里那版
OLD_MAIN=<commit>:static/js/main.js node channel_monkey.test.js old
```

Windows 的 cmd 里用 `set OLD_MAIN=...`，PowerShell 里用 `$env:OLD_MAIN="..."`。
