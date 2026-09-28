# 订阅额度（Plus/Pro 窗口用量）接入研究 — 2026-09-28

本文件是竞争产品研究 + 接入方案的证据档（品牌政策豁免：`-RESEARCH.md` 记录外部
源码名与真实主机路径属研究证据，不是产品文案）。落地实现见
`pet/chatgpt_quota.py` 与 `tests/test_chatgpt_quota.py`、
`tests/test_subscription_quota_ui.py`。

## 1. 结论摘要

- 点击桌宠可同时查看 **DeepSeek 余额**与**订阅额度**，两个独立开关、默认关。
- 额度走**主动 HTTPS 查询**（`GET https://chatgpt.com/backend-api/wham/usage`），
  数据源是本机已登录订阅账户的凭据；不是照搬 clawd 的被动遥测。
- 展示：一个气泡（余额段 + 额度段合成）+ 灵动岛卡片一行常驻；阈值 85% 转告警色；
  窗口标签由服务端秒数生成（18000→「5 小时」、604800→「7 天」），绝不写死。

## 2. 上游 clawd-on-desk 到底怎么拿额度

研究对象：`github.com/Ste11er/clawd-on-desk` 浅克隆（`.scratch/clawd-study/clawd`）。

额度在该项目里是**纯被动遥测**，两条来源都不是"点击即查"：

| 来源 | 证据 | 形态 |
| --- | --- | --- |
| Claude Code statusline | `hooks/claude-rate-limits.js:6-8` | 宿主进程通过 stdin 喂 JSON |
| Antigravity | `hooks/antigravity-context-usage.js:52-56` | 同上 |
| Codex/本 CLI | `hooks/codex-rate-limits.js:5-11`、`agents/codex-log-monitor.js:1657-1676` | 轮询 tail rollout JSONL，取 `token_count.payload.rate_limits` |

数据流：`src/server-route-state.js:306-309,385-395` → `src/state-account-quota.js`
→ `src/session-hud.js:640-649` → `src/quota-ring-renderer.js`。

关键否证：

- 全仓搜索 **没有** `chatgpt.com` / `backend-api` / `wham` 字样，也没有
  `fetchQuota` / `refreshQuota` 之类的主动查询函数——它从不自己去问服务端。
- 点击额度环只做 `openDashboard()`（`src/quota-ring-renderer.js:414,452` →
  `preload-quota-ring.js:34` → `src/session-ipc.js:99`），即**打开面板**，
  不是查询。用户"点击即查"的印象来自面板本身在别处刷新。

因此：**照搬 clawd 得不到即时性**，它的新鲜度完全取决于宿主进程最近一次上报。

## 3. 本机实测：被动方案在本机会渲染不出任何东西

- 最近一次抓样时间：`2026-09-20T08:25Z`（研究时点距今约 190 小时）。
- 该抓样里 primary 重置于 `2026-09-20T13:23Z`、secondary 重置于
  `2026-09-26T09:28Z`——研究时点（2026-09-28）两个窗口**都已滚动过**。
- clawd 自己的规则会把这些数据全部丢掉：抓样最大年龄 10 分钟
  （`hooks/codex-rate-limits.js:34`）、`resetAt <= now` 直接丢弃
  （`src/state-account-quota.js:149-165`）。

结论：被动遥测在本机等于"永远没有数据"，所以本次接入选择了主动查询。

## 4. 主动端点与实测响应

```
GET https://chatgpt.com/backend-api/wham/usage
Authorization: Bearer <凭据文件里的 access_token>
chatgpt-account-id: <凭据文件里的 account_id>   # 缺失时省略该头
User-Agent: codex-cli/0.155.1（实测有效；实现里沿用仓库既有浏览器头构造）
```

实测 HTTP 200，响应结构（身份字段已替换为假值，其余为线上原样）：

```json
{
  "plan_type": "plus",
  "rate_limit": {
    "allowed": true, "limit_reached": false,
    "primary_window":   {"used_percent": 9, "limit_window_seconds": 18000,
                         "reset_after_seconds": 15187, "reset_at": 1790593179},
    "secondary_window": {"used_percent": 2, "limit_window_seconds": 604800,
                         "reset_after_seconds": 469892, "reset_at": 1791047884}
  },
  "credits": {"has_credits": true, "unlimited": false, "balance": "1035.28"},
  "model_usage": {"...": {"available": true}},
  "rate_limit_reset_credits": {"available_count": 1, "applicable_available_count": 1}
}
```

凭据文件：`~/.codex/auth.json`（`CODEX_HOME` 可覆盖目录），字段
`tokens.access_token`（JWT，`exp` 约 10 天）、`tokens.account_id`、
`last_refresh`。

## 5. 从 clawd 采纳的规则（与其实现对齐的部分）

1. 内部真相统一为 **used_percent 0-100**，界面显示"剩余"——阈值判定与展示解耦
   （`hooks/quota-bucket.js:5-6,25`）。
2. 重置时刻一律存**绝对 epoch 秒**，倒计时在渲染时算（`quota-bucket.js:12-19`）；
   只拿到 `reset_after_seconds` 时在解析阶段锚定成绝对时刻。
3. 窗口标签由服务端 `limit_window_seconds` 生成（`codex-rate-limits.js:8-11`），
   不写死 5h/7d。
4. 异常 `resetAt` 守卫：超过 45 天视为不可信，宁可显示"重置时间未知"
   （`state-account-quota.js:70`）。
5. 已重置窗口不假装还有剩余：标 `expired`、显示"已重置"（`state-account-quota.js:127-147`
   的"变暗但保留"语义在本项目里落成"明说已重置"）。
6. 客户不打折扣的隐私：**不存** account / email / user_id，也不进日志与异常文案。
7. 卡片行超过 2 分钟补「N 分钟前」，不把旧值当现值（clawd 的 freshness 语义）。

## 6. 与 clawd 不同的取舍

- **不代刷 token**：走本地 JWT `exp` 预检，过期就明确提示重新登录。理由是刷新会
  轮换 `refresh_token`——不回写会破坏 CLI 自己的登录态，回写又等于改写别人的凭据文件。
- 不做隐藏的 `info_mode` 槽位、不做二级 Linux 异常项（`_sync_mask`、行走时
  `move` 的 ~24Hz 回写），避免把额度接入变成顺带重构。
- 只展示两个窗口 + 套餐名；`credits.balance` / `approx_*_messages` / `model_usage`
  虽然响应里有，但本次不展示（口径先窄后宽）。

## 7. 设置项入场（本项目无代码级注册表）

`docs/SETTINGS-CHANGE-GATES.md` 的 12 字段声明只在 PR 报告里；代码侧真实清单是：
默认值、`reload()` 白名单、`RELOAD_WHITELIST_SNAPSHOT`、设置页一行。

- 键：`click_show_quota`（默认 `False`，"试验功能默认关闭"）。
- 位置：「互动 → 点击反馈」，紧随"点击显示余额"。
- `include_ai=False` 变体也提供该开关（额度不依赖聊天/API Key）。
- 行数预算：`modern_settings_dialog.py` 的写回由 2 行改为调用
  `settings_pet_controls.save_click_provider_toggles`，该文件净减 1 行，
  不需要上调 `MODERN_SETTINGS_DIALOG_PY_LINE_BUDGET`。

## 8. 已知边界

- 代理/VPN：与仓库其它联网功能同源风险，见
  `docs/NETWORK-PROXY-AND-VPN-2026-09-22.md`；失败一律降级成气泡一行提示，
  不阻塞点击。
- 额度刷新复用 `balance_refresh_minutes`（0 = 只在点击时查）。
- 30 秒内的重复点击命中内存缓存，不发第二次请求。

## 9. 实测记录（2026-09-28，本机）

命令：`.venv/bin/python .scratch/quota_live_check.py`（只读 GET；输出不含任何身份字段）

| 项 | 实测 |
| --- | --- |
| 真实请求往返 | 990 ms（HTTPS，证书校验开启） |
| 卡片行/气泡 | `Plus 额度 · 5 小时 剩 91% · 7 天 剩 98%`；窗口标签来自服务端 18000/604800 秒 |
| 凭据缺失 | `QuotaCredentialMissing: 未找到订阅凭据（请先登录本机 CLI）`，零请求 |
| 网络不可达 | `QuotaNetworkError: 网络连接失败`，9 ms（连接被拒） |
| 凭据过期 | `QuotaCredentialExpired`，0 ms（先本地预检 JWT `exp`，零请求） |
| 合成气泡 + 灵动岛行（离线 200 次） | 中位 2.12 ms / 均值 2.31 ms / 最大 4.50 ms；每次点击恰好 1 个气泡 |
| 全量测试 | `2949 passed, 21 skipped`（216 s，`QT_QPA_PLATFORM=offscreen`） |

30 秒内重复点击命中内存缓存（网络请求数保持 1）的回归断言在
`tests/test_subscription_quota_ui.py::test_info_worker_queries_once_then_serves_from_cache`。
