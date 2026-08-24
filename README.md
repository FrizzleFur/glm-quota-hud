# glm-quota-hud

> 让 Claude Code 状态栏一眼看清 GLM Coding Plan 双账号额度 —— 5h 窗口 / 周积分池彩色进度条 + 倒计时

[English](README_EN.md) | 中文

## 效果

Claude Code 状态栏（GLM 额度段与 Context 进度条同框，用量分档变色）:

![状态栏效果](assets/hud-glm.jpg)

```
V1 5h ░░░░░ 1%-3:55 · V3 5h ██░░░ 40%-3:56 周29%-4d
      ↑紫槽   ↑绿(健康)         ↑随用量变色: <70%绿 / 70-85%黄 / ≥85%红
```

CLI 直读模式（配合 [CC Switch](#配合-cc-switch-切换账号) 使用）:

```
◆ GLM Coding Plan 配额总览  08-24 08:17
────────────────────────────────────────────────────
V1 (5h窗口套餐)
  5h  ░░░░░░░░░░░░░░░░░░░░   1%  4:55 后重置
  mcp ░░░░░░░░░░░░░░░░░░░░   1%  7d 后（08-31 10:32）重置
V3 (积分套餐) ← 当前会话
  周池 ███████░░░░░░░░░░░░░  33%  3d 后（08-27 17:00）重置
────────────────────────────────────────────────────
```

## 特性

- **双账号并行探测** — 5h 窗口套餐 + 积分制套餐同时显示，120s 缓存零压力
- **三池语义识别** — 5h 窗口 / 周积分池 / MCP 周池自动分类（含 API 隐藏规则：窗口未激活时不返回）
- **用量分档变色** — Catppuccin Mocha 配色（可自定义），进度条与读数随用量绿→黄→红
- **精确倒计时** — 5h 窗口到分钟、周池到天 + 绝对重置时刻
- **CLI 直读模式** — 不装 HUD 也能用；CC Switch 切账号前后一条命令看清所有窗口余量
- **零依赖** — 纯 Python 标准库，单文件
- **安全** — token 只走环境变量（`~/.zshrc.secrets`），永不入库；SGR 补丁保留 sanitize 防注入能力

## 快速开始

```bash
git clone https://github.com/FrizzleFur/glm-quota-hud.git
cd glm-quota-hud
bash install.sh
```

然后把 token 写进 `~/.zshrc.secrets`（不进 git）:

```bash
export GLM_V1_TOKEN="你的5h窗口套餐token"
export GLM_V3_TOKEN="你的积分套餐token"
```

最后按 `install.sh` 输出的示例，把 `--extra-cmd "python3 ~/.claude/plugins/claude-hud/glm_quota_hud.py"`
加进 `~/.claude/settings.json` 的 statusLine。重开终端完成。

> 单账号用户只需配一个 token，另一个账号自动忽略。

## 配合 CC Switch 切换账号

用 [cc-switch](https://github.com/farion1231/cc-switch) 管理多个 GLM 账号时，最疼的问题是：
**切过去才发现 5h 窗口已经用完了**。CLI 模式就是为这个场景做的：

![CC Switch 账号管理](assets/cc-switch.jpg)

> CC Switch 负责账号切换与总额度卡片，glm-quota-hud 把额度搬进状态栏常驻监控——两者组成完整工作流。

```bash
# 切换前：看清各账号窗口余量
python3 ~/.claude/plugins/claude-hud/glm_quota_hud.py --mode cli --refresh

# ...在 CC Switch 里切换到余量最健康的账号...

# 切换后：确认当前会话指向（← 当前会话 标记）
python3 ~/.claude/plugins/claude-hud/glm_quota_hud.py --mode cli
```

`--refresh` 跳过缓存强刷（120s TTL 内默认读缓存，保护 API）。

## 配置

复制 `providers.example.json` 为 `providers.json`（install.sh 已代劳），可调：

| 配置项 | 说明 | 默认 |
|---|---|---|
| `accounts[].token_env` | 每个账号的 token 环境变量名 | GLM_V1/V3_TOKEN |
| `accounts[].plan` | `window`（5h 窗口制）/ `credit`（积分制），用于账号配对 | - |
| `display.thresholds` | 变色阈值 | 黄 70 / 红 85 |
| `display.colors` | 全部颜色（Catppuccin Mocha hex） | 见 example |
| `display.bar_width_*` | 进度条宽度（单账号/多账号） | 10 / 5 |

HUD 显示模式：`GLM_HUD_SHOW=all` 双账号同显（默认 `current` 只显示当前会话账号）。

## 彩色原理（SGR 白名单补丁）

claude-hud 对 `--extra-cmd` 输出做 sanitize，**剥离一切 ANSI 转义**（防终端注入的安全设计），
因此 extra 段天生只能灰色。`patch-sgr.sh` 给它的 dist 打一个最小补丁：白名单放行
`38;2`（truecolor 前景）/ `0`（reset）/ `1`（bold）/ `2`（dim）/ `22`（解除 dim）五种 SGR 码，
**OSC / C0 / bidi / 其他转义照剥**——防注入能力不放松。补丁幂等可重打：

```bash
bash ~/.claude/plugins/claude-hud/patch-sgr.sh   # claude-hud 升级后再跑一次即可
```

不想要彩色？不打补丁即可，脚本输出会退化为纯文本（功能不受影响）。

## monitor 接口故障的 JWT 回退（2026-08-24 经验）

2026-08-24 15:32-16:2x 智谱 monitor 接口对 API key 返回 200 空 body（约 1 小时后自愈）。
脚本内置回退链：API key 失败（空响应）时自动改用网页登录 JWT 查询。
配置：浏览器登录 open.bigmodel.cn → F12 Application→Cookies 复制 `bigmodel_token_production` 值 →
`export GLM_V1_JWT="..."`（或 GLM_V3_JWT）进 ~/.zshrc.secrets。JWT 无 exp 字段较长效，失效重登重提。

## FAQ

**Q: 为什么我的 V3 积分账号有时没有 5h 窗口显示？**
GLM 只在 5h 窗口激活（近期有请求）时才返回该池。窗口过期且未产生新请求时看不到是正常的——
发一次对话后再 `--refresh` 就会出现。

**Q: V1 和 V3 的周池重置时间为什么不一样？**
V3 周积分池是固定整点（如周四 17:00）；V1 的 MCP 周池锚定订阅激活时刻滚动（如 8.31 10:32）。

**Q: 支持 GLM 之外的服务商吗？**
架构上把「探测 + 解析 + 渲染」分离了（见 `parse_glm`），新 provider 只需实现响应解析函数。
首版只内置 GLM——需要的话欢迎 issue / PR。

## License

MIT
