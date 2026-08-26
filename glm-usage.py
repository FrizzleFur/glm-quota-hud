#!/usr/bin/env python3
"""GLM Coding Plan usage monitor for Claude HUD (dual-account).

Queries the BigModel.cn API for quota limits of both GLM Coding Plan
accounts and outputs JSON compatible with ClaudeHUD's --extra-cmd.

ClaudeHUD runs extraCmd with a hard 3s timeout, so both accounts are
fetched in parallel with a 2s per-request budget; percentages respect
the 120s cache TTL but the 5h countdown clock is recomputed per print.

Account plans differ (label is derived from the API response, not the
token position, so it survives provider switching):
  Glm5.3-V1: 5h window (TOKENS_LIMIT) + MCP weekly pool (TIME_LIMIT)
  Glm5.3-V3: credit-based — 5h credit window + weekly credit pool +
             monthly pool (three CREDIT_LIMITs; monthly has no
             nextResetTime and is not displayed). The 5h window and the
             weekly pool are told apart by time-to-reset (< 6h = window)

Format: "V1 ⚡谷5h ██░░░░░░░░ 1%↻4:52  🛠mcp8%  📈9.0/h 余99%≈11.0h ✅"
        "V3 ⚡谷5h █░░░░ 7%↻4:33  📅周23%↻4d→08/30  🗓5.3/d 余77%≈14.5d"
(bar: purple, turns red at 85%; single-account mode widens to 10 cells;
 forecast segments 📈/🗓 need dist patch MAX_LABEL_LENGTH=150 — patch-sgr.sh)
Quota warning: any pool at/above GLM_HUD_WARN (default 90) swaps its icon to
🔥, appends "!<eta>" (only when usage will exhaust BEFORE reset), and fires a
one-shot macOS notification carrying the full forecast (rate, ETA, verdict).
Forecast data lives in .glm-usage-history.json (percentage time series,
reset-segmented, throttled appends); notification dedup latch in
.glm-notify-state.json. Both are lazily created — delete to roll back.
"""

import json
import os
import re
import subprocess
import time
import datetime
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_SCRIPT_DIR, ".glm-usage-cache.json")
HISTORY_FILE = os.path.join(_SCRIPT_DIR, ".glm-usage-history.json")
STATE_FILE = os.path.join(_SCRIPT_DIR, ".glm-notify-state.json")
CACHE_TTL = 120      # 2 minutes
FETCH_TIMEOUT = 2    # per-request; HUD kills extraCmd at 3s

try:
    WARN_THRESHOLD = int(os.environ.get("GLM_HUD_WARN", "90"))
except ValueError:
    WARN_THRESHOLD = 90

API_BASE = os.environ.get("GLM_API_BASE", "https://open.bigmodel.cn")
API_ENDPOINT = f"{API_BASE}/api/monitor/usage/quota/limit"

# GLM 信息配色（Catppuccin Mocha 官方色板，与 tmux 主题同族）：
#   进度条实心█与读数同档变色（<70% Green / 70-85% Yellow / ≥85% Red），
#   空块░中性槽色 surface1 #45475A；整段 SGR 22 解除外层 DIM 压暗保持亮色
# 依赖 dist/extra-cmd.js 的 SGR 白名单补丁（透传 38;2/0/1/2/22 五种码，其余转义仍剥），
# claude-hud 升级后需重跑 patch-sgr.sh。段尾 reset 后补 dim，恢复整块 DIM 基调
BOLD = "\x1b[1m"
NO_DIM = "\x1b[22m"
RESET_DIM = "\x1b[0m\x1b[2m"
PEAK_GRAY = "\x1b[38;2;108;112;134m"   # overlay0，高峰标注灰
FORECAST_GRAY = "\x1b[38;2;166;173;200m"  # subtext0 #A6ADC8，预测段信息层
                                          # （overlay0 太暗且曾叠 DIM 双重压暗）
BAR_PURPLE = "\x1b[38;2;203;166;247m"   # Catppuccin Mocha mauve #CBA6F7
BAR_SLOT = "\x1b[38;2;69;71;90m"        # 空块槽色 surface1 #45475A
GREEN = "\x1b[38;2;166;227;161m"        # #A6E3A1
YELLOW = "\x1b[38;2;249;226;175m"       # #F9E2AF
RED = "\x1b[38;2;243;139;168m"          # #F38BA8
BAR_RED = RED


def dyn_color(pct):
    """读数动态分档（Context 同款阈值）。"""
    if pct >= 85:
        return RED
    if pct >= 70:
        return YELLOW
    return GREEN


def bar_color(pct):
    return dyn_color(pct)


def bar(pct, width):
    """实心█分档色 / 空块░暗紫（同族）。"""
    filled = round(min(max(pct, 0), 100) / 100 * width)
    return f"{NO_DIM}{bar_color(pct)}{'█' * filled}{BAR_SLOT}{'░' * (width - filled)}{RESET_DIM}"

# 双账号 token 均从环境变量读取（值存 ~/.zshrc.secrets，密钥永不进 git/备份仓库）：
#   GLM_V1_TOKEN → 5h 窗口 + MCP 周池账号；GLM_V3_TOKEN → 积分制账号
# 另含当前会话 token（ANTHROPIC_AUTH_TOKEN），三来源去重后并行探测
V1_TOKEN = os.environ.get("GLM_V1_TOKEN", "")
V3_TOKEN = os.environ.get("GLM_V3_TOKEN", "")


def token_list():
    env_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    tokens = [t for t in (env_key, V1_TOKEN, V3_TOKEN) if t]
    return list(dict.fromkeys(tokens))  # dedup, keep order


def label_for(data):
    """积分制套餐 → V3，5h 窗口套餐 → V1。"""
    types = {lim.get("type") for lim in data.get("data", {}).get("limits", [])}
    return "Glm5.3-V3" if "CREDIT_LIMIT" in types else "Glm5.3-V1"



def remaining_clock(timestamp_ms):
    """Format reset time as absolute H:MM (prefix 明 when it lands tomorrow);
    empty if already past. Absolute clock reads faster than a countdown."""
    if not timestamp_ms:
        return ""
    dt = datetime.datetime.fromtimestamp(timestamp_ms / 1000)
    if dt.timestamp() <= time.time():
        return ""
    if dt.date() != datetime.date.today():
        return f"明{dt.hour}:{dt.minute:02d}"
    return f"{dt.hour}:{dt.minute:02d}"


def long_clock(timestamp_ms):
    """Reset countdown for multi-day pools, day granularity (Xd)."""
    if not timestamp_ms:
        return ""
    secs = (timestamp_ms / 1000) - time.time()
    if secs <= 0:
        return ""
    return f"{int(secs // 86400)}d"


def reset_date(timestamp_ms):
    """Reset absolute date MM/DD for multi-day pools（↻1d→08/31：天数倒计时
    配绝对日期，跨天池更直观；亦用于 ⚠️ 耗尽日标注）。"""
    if not timestamp_ms:
        return ""
    dt = datetime.datetime.fromtimestamp(timestamp_ms / 1000)
    if dt.timestamp() <= time.time():
        return ""
    return f"{dt.month:02d}/{dt.day:02d}"


def peak_word():
    """谷/峰内联进 5h 池标签（⚡谷5h / ⚡峰5h）：仅工作日 14-18 点为峰
    （1x 积分），其余皆谷（0.5x）。原 peak_suffix 灰标注已移除——峰谷
    并入池标签后冗余，时段边界由 crontab 通知（13:50/17:55）提醒。"""
    now = datetime.datetime.now()
    return "峰" if (now.weekday() < 5 and 14 <= now.hour < 18) else "谷"


def load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(data_map, current_label=""):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "data": data_map, "current": current_label}, f)
    except OSError:
        pass


# ===================== 预测与预警（2026-08-26 增补） =====================
# 伴生数据文件（惰性创建，删除即回滚到无预测/无通知行为）：
#   HISTORY_FILE: {"V1:5h": [[ts, pct], ...]}  percentage 时间序列
#   STATE_FILE:   {"V1:5h": {"notified": true, "ts": ...}}  通知去重 latch

def pool_key(account, lim):
    """稳定池标识。V1 两池类型即稳定；V3 双 CREDIT_LIMIT 用 API 稳定字段
    (number, unit) 判别（5h 窗口 5/3，周池 1/6）。禁用显示层的「剩余<6h」
    时间启发式作 key——周池到期前最后 6h 会被误灌 V3:5h，每周污染一次
    速率序列（显示启发式在 format_usage 中保留不动，仅此处换源）。"""
    lim_type = lim.get("type")
    if lim_type == "TOKENS_LIMIT":
        return f"{account}:5h"
    if lim_type == "TIME_LIMIT":
        return f"{account}:mcp"
    if lim_type == "CREDIT_LIMIT":
        if (lim.get("number"), lim.get("unit")) == (5, 3):
            return f"{account}:5h"
        if (lim.get("number"), lim.get("unit")) == (1, 6):
            return f"{account}:weekly"
    return None  # 月池等未识别 CREDIT_LIMIT：不入历史与预警


def extract_pools(account, data):
    """[(pool_key, pct, nextResetTime_ms)]，供历史/预警/渲染共用。"""
    out = []
    for lim in data.get("data", {}).get("limits", []):
        key = pool_key(account, lim)
        if key:
            out.append((key, lim.get("percentage", 0), lim.get("nextResetTime")))
    return out


def load_side_file(path):
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_side_file(path, obj):
    """原子写：tmp 与目标同目录（os.replace 仅同文件系统原子），并发坏档
    靠 load 容错自愈。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except OSError:
        pass


def append_history(history, pools, now):
    """fetch 成功后追加各池快照点（append 必须先于速率计算，新点入段）：
    - pct 回落 = 窗口重置 → 弃旧段重新累积（速率只在同窗口内差分）
    - 末点同 pct 且 <30min 跳过（周池 8 天×每 120s fetch 会膨胀到 5.7k
      点，节流后上限约 400 点/池）
    - prune：5h 池留 6h、周池/MCP 留 8 天"""
    for key, pct, _reset in pools:
        pts = [list(p) for p in history.get(key, [])]
        if pts:
            last_ts, last_pct = pts[-1]
            if pct < last_pct:
                pts = []
            elif pct == last_pct and now - last_ts < 1800:
                continue
        pts.append([now, pct])
        span = 6 * 3600 if key.endswith(":5h") else 8 * 86400
        history[key] = [p for p in pts if now - p[0] <= span]


def _slope(seg):
    """首尾差分 %/h；<2 点、跨度 <5min（整数 pct 量化下 5min 内 1 点变化
    =12%/h 粒度，噪声偏大但对 ETA 决策参考可接受；10min 下限在重度消耗
    场景全程够不到——实测 94→100% 仅 7min，预估会整程缺席）或非上升段
    返回 None。新点持续进入 recent 窗口，粗 ETA 会自我修正。"""
    if len(seg) < 2:
        return None
    (t0, p0), (t1, p1) = seg[0], seg[-1]
    dt = t1 - t0
    if dt < 300 or p1 <= p0:
        return None
    return (p1 - p0) / dt * 3600.0


def compute_rate(history, key, now):
    """%/h 三级回退：① 5h 池取最近 ≤45min 子窗口（反映当前燃烧速率，
    避免「空闲 2h + 突发 30min」整段均值低估 ETA）② 整段首尾差分（长
    周期池均值合理，子窗口仅对 5h 池启用）③ None（冷启动静默）。"""
    pts = history.get(key, [])
    if key.endswith(":5h"):
        recent = [p for p in pts if now - p[0] <= 2700]
        rate = _slope(recent)
        if rate:
            return rate
    return _slope(pts)


def assess(key, pct, reset_ms, rate, now):
    """撑得到判断。返回 (verdict, eta_ms)：
    unknown=无速率（通知文案降级） flat=无消耗（撑得到，不做除法）
    ok=撑得到重置 short=撑不到（预计耗尽早于重置） eta_unknown=有速率
    但无重置时刻（MCP 池 API 可能不返回 nextResetTime）。"""
    if rate is None:
        return "unknown", None
    if rate <= 0:
        return "flat", None
    # eta_ms 是毫秒时间戳：先在秒域加时长再 ×1000（直接 now + 毫秒增量
    # 会把 1970 年当 ETA，2026-08-26 实跑暴露）
    eta_ms = (now + (100 - pct) / rate * 3600) * 1000
    if not reset_ms:
        return "eta_unknown", eta_ms
    reset_left_h = (reset_ms / 1000 - now) / 3600
    return ("ok" if (eta_ms / 1000 - now) / 3600 >= reset_left_h else "short"), eta_ms


def check_notify(state, pools_info, now):
    """level+latch：pct >= 阈值且未 notified → 触发；pct 回落 → 清除标记
    （下周期可再触发）。首装即已达标会弹，是有意行为：信息性通知不依赖
    「上次值」。返回 (triggered, dirty)，latch 由调用方先落盘再发通知。"""
    triggered = []
    dirty = False
    for info in pools_info:
        key, pct = info["key"], info["pct"]
        rec = state.get(key, {})
        if pct >= WARN_THRESHOLD:
            if not rec.get("notified"):
                triggered.append(info)
                state[key] = {"notified": True, "ts": now}
                dirty = True
        elif rec.get("notified"):
            state.pop(key)
            dirty = True
    return triggered, dirty


def _eta_suffix(key, eta_ms):
    """ETA 时刻/时长：5h 池 !H.h 小数小时（!2.1h），周池/MCP !D.d 天。
    （通知里用绝对时刻 remaining_clock，label 用时长省字符。）eta 已过
    或为零（pct=100 已耗尽）时仅显示 "!"，避免 !0.0h 丑形态。"""
    if not eta_ms:
        return "!"
    span_s = (eta_ms / 1000) - time.time()
    if span_s <= 0:
        return "!"
    return f"!{span_s / 3600:.1f}h" if key.endswith(":5h") else f"!{span_s / 86400:.1f}d"


def _notify_line(info):
    """单池通知文案：无速率降级为行动建议；有速率给完整预报
    （速率/预计耗尽/重置对比/结论）。周池速率换算 %/天 展示。"""
    key, pct = info["key"], info["pct"]
    name = key.replace(":", " ")
    rate, verdict = info["rate"], info["verdict"]
    if rate is None:
        return f"{name} 已达 {pct}%，建议收敛用量或切换账号"
    hourly = key.endswith(":5h")
    rate_txt = f"{rate:.0f}%/h" if hourly else f"{rate * 24:.1f}%/天"
    reset_str = remaining_clock(info["reset"]) if info.get("reset") else ""
    eta_str = remaining_clock(info["eta"]) if info.get("eta") else ""
    head = f"{name} 已达 {pct}%（消耗 {rate_txt}"
    if verdict == "short":
        return head + f"，预计耗尽 {eta_str} 早于重置 {reset_str}，撑不到，建议收敛或切换账号）"
    if verdict == "ok" or verdict == "flat":
        return head + f"，重置于 {reset_str}，撑得到重置）"
    if verdict == "eta_unknown":
        return head + f"，预计耗尽 {eta_str}）"
    return head + "，建议收敛用量或切换账号）"


def _emergency_label(forecast):
    """50 单元最后防线：极端超限时降级为无 SGR 纯文本（池名+pct+预警后缀），
    保核心信息且避免色码被上游截断劈开。"""
    segs = []
    for key, info in forecast.items():
        suffix = _warn_suffix(key, info) if info["pct"] >= WARN_THRESHOLD else ""
        segs.append(f"{key.replace(':', ' ')}{info['pct']}%{suffix}")
    return ("·".join(segs) or "GLM")[:49] + "…"


def send_notification(triggered):
    """多池合并一条通知（禁止 N 条串行 osascript）。latch 已由调用方先行
    落盘（写盘毫秒级；若先通知后落盘、进程在间隙被杀会重复弹）。Popen
    fire-and-forget：不等待（osascript 实测 0.3-0.8s，fetch 慢路径 2s+
    串行会压穿 HUD 3s 总超时），start_new_session 脱离进程组规避 HUD 按
    进程组 kill。tradeoff：通知失败/被杀时 latch 已置 → 该次通知永久
    丢失（宁漏勿扰）。"""
    body = " · ".join(_notify_line(i) for i in triggered)
    body = body.replace("\\", "\\\\").replace('"', '\\"')  # AppleScript 层转义
    script = f'display notification "{body}" with title "GLM 额度预警"'
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def fetch_api(pair):
    """鉴权链：API key 优先（服务恢复后继续可用），空 body 时回退该账号的
    网页登录 JWT。2026-08-24 起 monitor 端点收紧为仅认 JWT——API key 请求
    返回 200 空 body（非 401）。JWT 从已登录账号浏览器 cookie
    bigmodel_token_production 提取（HS512，payload 无 exp，长效但可被吊销）。"""
    api_key, jwt = pair
    for auth in (api_key, jwt):
        if not auth:
            continue
        req = urllib.request.Request(
            API_ENDPOINT,
            headers={
                "Authorization": auth,
                "Content-Type": "application/json",
                "Accept-Language": "en-US,en",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                body = resp.read().decode("utf-8").strip()
            if body:
                return json.loads(body)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            continue
    return None


def peak_suffix():
    """[已废弃 2026-08-26] 峰谷标注内联进 5h 池标签（⚡谷5h/⚡峰5h，见
    peak_word），本函数保留仅供历史参考，main 不再调用。"""
    return ""


def _warn_suffix(key, info):
    """预警池 !ETA 后缀：short/eta_unknown/unknown（无速率）显示——unknown
    时无 ETA 仅"!"；ok/flat 撑得到无需行动则隐藏。"""
    if info.get("verdict") in ("ok", "flat"):
        return ""
    return _eta_suffix(key, info.get("eta"))


def _render_5h(icon, pct, w, suffix, clock):
    """⚡谷5h/🔥峰5h 统一渲染（V1 TOKENS_LIMIT 与 V3 5h 积分窗口共用样式）。"""
    color = dyn_color(pct)
    dash = f" ↻{clock}" if clock else ""
    return f"{NO_DIM}{color}{icon}{peak_word()}5h {bar(pct, w)}{BOLD}{color}{pct}%{suffix}{dash}{RESET_DIM}"


def _forecast_seg(key, info):
    """📈/🗓 预测段（label 扩容补丁 MAX_LABEL_LENGTH=150 后的空间，需
    patch-sgr.sh 已打 v3）：速率/余量/还能用/结论。5h 池 📈%/h（近 45min
    子窗口速率）+ ✅撑得到/⚠️耗于H:MM；周池与 MCP 🗓%/天（跨天均值）+
    ⚠️MM/DD 耗尽日。无速率（冷启动）/无消耗（平台期）/已耗尽（100%!
    已表达）返回空。"""
    pct = info["pct"]
    rate = info.get("rate")
    verdict = info.get("verdict")
    if rate is None or rate <= 0 or pct >= 100:
        return ""
    remain = 100 - pct
    eta_s = (info.get("eta", 0) / 1000 - time.time()) if info.get("eta") else 0
    if eta_s <= 0:
        return ""  # ETA 已过/为零：数据异常防御（正常仅 pct=100 触达，上方已挡）
    hourly = key.endswith(":5h")
    mark = ""
    if verdict == "short":
        clock = remaining_clock(info["eta"]) if hourly else reset_date(info["eta"])
        mark = f"{RED}⚠️{clock}{FORECAST_GRAY}"
    elif verdict == "ok":
        mark = f"{GREEN}✅{FORECAST_GRAY}"
    if hourly:
        return f"{NO_DIM}{FORECAST_GRAY}📈 {rate:.1f}%/h 余{remain}%≈{eta_s / 3600:.1f}h {mark}{RESET_DIM}"
    return f"{NO_DIM}{FORECAST_GRAY}🗓 {rate * 24:.1f}%/d 余{remain}%≈{eta_s / 86400:.1f}d {mark}{RESET_DIM}"


def format_usage(data, bar_width=10, account="", forecast=None, compact=False):
    """V1: ⚡谷5h bar + 🛠mcp%; V3: ⚡谷5h bar + 📅周%%↻Nd→MM/DD。
    预警池（pct >= GLM_HUD_WARN，默认 90）：图标换 🔥、追加 !ETA 后缀、
    bar 收窄 5 格释放预算。📈/🗓 预测段（速率/余量/还能用/✅⚠️）随各池
    追加——依赖 dist 补丁 MAX_LABEL_LENGTH=150（patch-sgr.sh v3）。
    compact（show=all 双账号）：非预警池 bar 3 格且去 ↻ 时钟、预测段
    省略，预警池信息优先。forecast = {pool_key: info} 由 main 注入（缓存
    路径亦有，rate 取持久化 history）。emoji UTF-16 计数：⚡=1 ↻=1 📅🛠🔥=2。"""
    forecast = forecast or {}
    limits = data.get("data", {}).get("limits", [])
    five_h = ""
    mcp = ""
    weekly = ""
    fc_parts = []
    for lim in limits:
        pct = lim.get("percentage", 0)
        lim_type = lim.get("type")
        key = pool_key(account, lim)
        info = forecast.get(key) or {"pct": pct, "verdict": None, "eta": None}
        warn = pct >= WARN_THRESHOLD
        suffix = _warn_suffix(key, info) if warn else ""
        if lim_type == "TOKENS_LIMIT":
            clock = remaining_clock(lim.get("nextResetTime"))
            if compact and not warn:
                clock = ""
            five_h = _render_5h(
                "🔥" if warn else "⚡", pct,
                5 if warn else (3 if compact else bar_width),
                suffix, clock,
            )
        elif lim_type == "TIME_LIMIT":
            # mcp 周池始终显示（用户需要随时看到调用剩余）；预警态红色并
            # 压缩去 mcp 字样省预算（平时 🛠mcp8% = 7 单元，预警 🛠91% = 5）
            if warn:
                mcp = f"{NO_DIM}{RED}🛠{pct}%{suffix}{RESET_DIM}"
            else:
                mcp = f"🛠mcp{pct}%"
        elif lim_type == "CREDIT_LIMIT" and lim.get("nextResetTime"):
            # CREDIT_LIMIT 按周期分两种：5h 积分窗口（API number=5/unit=3，与 V1 的
            # TOKENS_LIMIT 同步开窗）按 5h 样式加粗；周积分池（number=1/unit=6）显示周。
            # 用剩余时长区分（5h 窗口恒 < 5h）；周池到期前最后 <6h 内会被误标为 5h，
            # 但两者倒计时显示相同，代价仅为前缀字样。不带 nextResetTime 的月池不展示
            # （pool_key 用 number/unit 稳定字段，与此显示启发式解耦，见 pool_key 注释）
            remain_s = (lim["nextResetTime"] / 1000) - time.time()
            if remain_s < 6 * 3600:
                clock = remaining_clock(lim.get("nextResetTime"))
                if compact and not warn:
                    clock = ""
                five_h = _render_5h(
                    "🔥" if warn else "⚡", pct,
                    5 if warn else (3 if compact else bar_width),
                    suffix, clock,
                )
            else:
                clock = long_clock(lim.get("nextResetTime"))
                if compact and not warn:
                    clock = ""
                # 周池天数倒计时配绝对日期（↻1d→08/31），跨天池更直观
                dash = f" ↻{clock}→{reset_date(lim.get('nextResetTime'))}" if clock else ""
                color = dyn_color(pct)
                weekly = f"{NO_DIM}{BOLD}{color}{'🔥' if warn else '📅'}周{pct}%{suffix}{dash}{RESET_DIM}"
        # 预测段随池追加（compact 双账号仅预警池显示，控总长）
        if not compact or warn:
            seg = _forecast_seg(key, info)
            if seg:
                fc_parts.append(seg)
    # 段间双空格：emoji 与相邻段视觉上更透气（单空格时 🛠 贴 ↻时钟过紧）
    return "  ".join(p for p in (five_h, mcp, weekly, *fc_parts) if p)


def main():
    env_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    tokens = token_list()
    if not tokens:
        print(json.dumps({"label": "GLM: no API key"}))
        return

    cache = load_cache()
    data_map = dict(cache.get("data", {}))
    # 当前会话账号 = env token 自己的账号类型（由 API 响应判定），缓存持久化。
    # 不用 token 字符串对比判定——GLM_V3_TOKEN 未注入的会话里会误判导致 fetch failed
    current_label = cache.get("current", "")
    cache_fresh = (time.time() - cache.get("ts", 0)) < CACHE_TTL
    now = time.time()
    history = load_side_file(HISTORY_FILE)  # 无条件载入：缓存路径 forecast 亦需

    updated = False
    if not cache_fresh:
        # API key → 同账号 JWT 的鉴权链（见 fetch_api 注释）
        jwt_map = {t: j for t, j in (
            (V1_TOKEN, os.environ.get("GLM_V1_JWT", "")),
            (V3_TOKEN, os.environ.get("GLM_V3_JWT", "")),
        ) if t}
        pairs = [(t, jwt_map.get(t, "")) for t in tokens]
        with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
            results = list(pool.map(fetch_api, pairs))
        for tok, fetched in zip(tokens, results):
            if fetched and fetched.get("success"):
                data_map[label_for(fetched)] = fetched
                updated = True
                if tok == env_key:
                    current_label = label_for(fetched)
        if updated:
            save_cache(data_map, current_label)
            # fetch 成功：append 历史快照（append 先于下方速率计算，新点入段）
            for lbl, resp in data_map.items():
                append_history(history, extract_pools(lbl.replace("Glm5.3-", ""), resp), now)
            save_side_file(HISTORY_FILE, history)

    # ---- 预测/预警编排：pct/reset 取 data_map（缓存或新数据均可），rate 取
    # 持久化 history——缓存命中路径亦有完整预测显示；预警 level+latch 仅
    # fetch 路径判断（缓存命中数据未变；漏窗最多一个 TTL 120s，睡眠/离线
    # 后首个成功 fetch 补上，无结构性漏窗）----
    forecast = {}
    for lbl, resp in data_map.items():
        for key, pct, reset in extract_pools(lbl.replace("Glm5.3-", ""), resp):
            rate = compute_rate(history, key, now)
            verdict, eta = assess(key, pct, reset, rate, now)
            forecast[key] = {"key": key, "pct": pct, "reset": reset,
                             "rate": rate, "verdict": verdict, "eta": eta}
    if updated:
        state = load_side_file(STATE_FILE)
        triggered, dirty = check_notify(state, list(forecast.values()), now)
        if dirty:
            save_side_file(STATE_FILE, state)  # latch 先落盘（毫秒级），再 fire-and-forget
        if triggered:
            send_notification(triggered)

    # 显示模式：current=只显示当前会话账号（默认），all=全部账号
    show = os.environ.get("GLM_HUD_SHOW", "current")
    if show == "all" or not current_label or current_label not in data_map:
        # 无法判定当前账号时：唯一账号直接显示，多账号全显（优于误报 fetch failed）
        names = sorted(data_map)
    else:
        names = [current_label]
    refreshed = {label_for(v) for v in data_map.values()} if not cache_fresh else set()
    parts = []
    for name in names:
        data = data_map.get(name)
        if not data:
            continue
        mark = "?" if (not cache_fresh and name not in refreshed) else ""
        # 显示名去掉 "Glm5.3-" 前缀：claude-hud label 上限 50 字符（JS UTF-16 计），
        # 双账号 + 倒计时的完整形态需压缩以避免截断；进度条宽度随账号数自适应
        # （单账号 10 格与 Context 同宽，双账号 compact 模式 3 格控预算）
        display = name.replace("Glm5.3-", "")
        parts.append(f"{display} {format_usage(data, 10 if len(names) == 1 else 5, display, forecast, compact=(len(names) > 1))}{mark}")
    if not parts:
        parts = ["GLM: fetch failed"]
    label = "·".join(parts)
    # 150 单元守护（剥 SGR 后按 UTF-16 计，与 dist 补丁 v3 的
    # MAX_LABEL_LENGTH=150 同口径；未打补丁时上游 50 截断——升级后需重跑
    # patch-sgr.sh）：极端超限时降级为无色纯文本形态，避免上游截断劈开
    # SGR 色码导致后续段颜色错乱
    plain = re.sub(r"\x1b\[[0-9;]*m", "", label)
    if len(plain.encode("utf-16-le")) // 2 > 150:
        label = _emergency_label(forecast)
    print(json.dumps({"label": label}))


if __name__ == "__main__":
    main()
