#!/usr/bin/env python3
"""glm-quota-hud — GLM Coding Plan 用量监控，Claude HUD / CLI 双模式。

在 claude-hud 状态栏显示 GLM 双账号（5h 窗口 + 周/月积分池）用量：
  V1 5h ██░░░ 1%-3:55·V3 5h ██░░░ 40%-3:56 [绿]周29%-4d
（进度条与读数按用量分档变色 <70% 绿 / 70-85% 黄 / ≥85% 红，Catppuccin Mocha）

两种用法:
  1. HUD 模式（默认）: 作为 claude-hud --extra-cmd 输出 JSON label
  2. CLI 模式: python3 glm_quota_hud.py --mode cli [--refresh]
     终端直读彩色总览，配合 CC Switch 切账号前后查看各窗口余量

配置: providers.json（从 providers.example.json 复制）；token 一律走环境变量
（推荐 ~/.zshrc.secrets），永不写入配置文件。

HUD 彩色依赖 claude-hud dist 的 SGR 白名单补丁（patch-sgr.sh，幂等可重打）。

Version: 1.0.0 | License: MIT
"""

import argparse
import json
import os
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "providers.json")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".quota-cache.json")
CACHE_TTL = 120      # 2 minutes; --refresh bypasses
FETCH_TIMEOUT = 2    # per-request; claude-hud kills extraCmd at 3s

# ---------- 配置 ----------

DEFAULT_DISPLAY = {
    "bar_width_single": 10,
    "bar_width_multi": 5,
    "thresholds": {"yellow": 70, "red": 85},
    "colors": {"green": "#A6E3A1", "yellow": "#F9E2AF", "red": "#F38BA8",
               "purple": "#CBA6F7", "slot": "#45475A"},
}


def load_config():
    cfg = {"providers": {}, "display": dict(DEFAULT_DISPLAY)}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            user = json.load(f)
        cfg["providers"] = user.get("providers", {})
        cfg["display"].update(user.get("display", {}))
    else:
        # 无配置时回退到 GLM 默认（开箱即用，token 从 env）
        cfg["providers"] = {"glm": {
            "name": "GLM Coding Plan",
            "api_base": "https://open.bigmodel.cn",
            "endpoint": "/api/monitor/usage/quota/limit",
            "accounts": [
                {"label": "V1", "display": "V1", "token_env": "GLM_V1_TOKEN", "plan": "window"},
                {"label": "V3", "display": "V3", "token_env": "GLM_V3_TOKEN", "plan": "credit"},
            ],
            "session_token_env": "ANTHROPIC_AUTH_TOKEN",
        }}
    # colors hex → SGR truecolor
    c = cfg["display"]["colors"]
    cfg["sgr"] = {k: hex_to_sgr(v) for k, v in c.items()}
    return cfg


def hex_to_sgr(hexstr):
    r, g, b = (int(hexstr[i:i + 2], 16) for i in (1, 3, 5))
    return f"\x1b[38;2;{r};{g};{b}m"


# ---------- 渲染原语 ----------

BOLD = "\x1b[1m"
NO_DIM = "\x1b[22m"          # 解除外层 DIM 压暗（HUD 里 label() 整块包 DIM）
RESET_DIM = "\x1b[0m\x1b[2m"  # reset 后补 dim，恢复整块基调


def dyn_color(pct, sgr, thresholds):
    if pct >= thresholds["red"]:
        return sgr["red"]
    if pct >= thresholds["yellow"]:
        return sgr["yellow"]
    return sgr["green"]


def bar(pct, width, sgr, thresholds):
    """实心█按档变色 / 空块░固定槽色。"""
    filled = round(min(max(pct, 0), 100) / 100 * width)
    return f"{NO_DIM}{dyn_color(pct, sgr, thresholds)}{'█' * filled}{sgr['slot']}{'░' * (width - filled)}{RESET_DIM}"


# ---------- GLM provider 解析 ----------
# 响应结构: data.limits[] = {type, percentage, nextResetTime?, number?, unit?}
#   TOKENS_LIMIT            → 5h 窗口（V1 套餐）
#   TIME_LIMIT              → MCP 周池（V1 套餐）
#   CREDIT_LIMIT 带 nrt <6h → 5h 积分窗口（V3 套餐，与 V1 同步开窗）
#   CREDIT_LIMIT 带 nrt ≥6h → 周积分池（V3 套餐）
#   CREDIT_LIMIT 不带 nrt   → 月积分池（不展示）
# 账号类型判定: 含 CREDIT_LIMIT → 积分制(V3)，否则 5h 窗口制(V1)

def parse_glm(data):
    """返回 {account_kind, pools:[{kind, pct, next_reset_ms}]}。"""
    limits = data.get("data", {}).get("limits", [])
    kinds = {lim.get("type") for lim in limits}
    kind = "credit" if "CREDIT_LIMIT" in kinds else "window"
    pools = []
    now = time.time()
    for lim in limits:
        pct = lim.get("percentage", 0)
        nrt = lim.get("nextResetTime")
        t = lim.get("type")
        remain_s = (nrt / 1000 - now) if nrt else None
        if t == "TOKENS_LIMIT":
            pools.append({"kind": "5h", "pct": pct, "reset_ms": nrt})
        elif t == "TIME_LIMIT":
            pools.append({"kind": "mcp", "pct": pct, "reset_ms": nrt})
        elif t == "CREDIT_LIMIT" and nrt:
            if remain_s < 6 * 3600:  # 5h 积分窗口（周池到期前 <6h 内可能误判，倒计时显示相同）
                pools.append({"kind": "5h", "pct": pct, "reset_ms": nrt})
            else:
                pools.append({"kind": "weekly", "pct": pct, "reset_ms": nrt})
    order = {"5h": 0, "weekly": 1, "mcp": 2}
    pools.sort(key=lambda x: order.get(x["kind"], 9))
    return {"account_kind": kind, "pools": pools}


# ---------- 时钟 ----------

def remaining_clock(reset_ms):
    """H:MM 倒计时（5h 窗口恒一位小时）；过期/缺失返回空。"""
    if not reset_ms:
        return ""
    secs = reset_ms / 1000 - time.time()
    if secs <= 0:
        return ""
    return f"{int(secs // 3600)}:{int((secs % 3600) // 60):02d}"


def long_clock(reset_ms):
    """跨天池倒计时，天级粒度 (Xd)。"""
    if not reset_ms:
        return ""
    secs = reset_ms / 1000 - time.time()
    if secs <= 0:
        return ""
    return f"{int(secs // 86400)}d"


# ---------- 缓存 ----------

def load_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(accounts, current_label):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "accounts": accounts, "current": current_label}, f)
    except OSError:
        pass


# ---------- 探测 ----------

def fetch_api(api_key, base, endpoint):
    req = urllib.request.Request(
        f"{base}{endpoint}",
        headers={"Authorization": api_key, "Content-Type": "application/json",
                 "Accept-Language": "en-US,en"},
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def probe_all(provider_cfg, force=False):
    """并行探测全部账号。返回 (accounts{label: parsed}, current_label)。"""
    base, endpoint = provider_cfg["api_base"], provider_cfg["endpoint"]
    env_session = os.environ.get(provider_cfg.get("session_token_env", ""), "")
    tokens = []  # [(token, is_session)]
    for acc in provider_cfg.get("accounts", []):
        t = os.environ.get(acc["token_env"], "")
        if t:
            tokens.append((t, False, acc))
    if env_session:
        tokens.insert(0, (env_session, True, None))
    # dedup by token, 保留 session 优先
    seen, uniq = set(), []
    for t, is_s, acc in tokens:
        if t not in seen:
            seen.add(t)
            uniq.append((t, is_s, acc))

    with ThreadPoolExecutor(max_workers=max(len(uniq), 1)) as pool:
        results = list(pool.map(lambda x: fetch_api(x[0], base, endpoint), uniq))

    accounts, current = {}, ""
    for (t, is_s, acc), fetched in zip(uniq, results):
        if not (fetched and fetched.get("success")):
            continue
        parsed = parse_glm(fetched)
        kind = parsed["account_kind"]
        if acc and acc.get("plan") == kind:
            label = acc["label"]                       # 配置声明了 plan，直接信任
        else:
            same = [a["label"] for a in provider_cfg.get("accounts", []) if a.get("plan") == kind]
            idx = sum(1 for p in accounts.values() if p["account_kind"] == kind)
            label = same[idx] if idx < len(same) else f"{kind}{idx}"
        parsed["label"] = label
        accounts[label] = parsed
        if is_s:
            current = label
    return accounts, current


# ---------- HUD 模式 ----------

def render_hud(accounts, current, sgr, thresholds, bar_w):
    parts = []
    for label in sorted(accounts):
        acc = accounts[label]
        seg = []
        for pool in acc["pools"]:
            pct = pool["pct"]
            color = dyn_color(pct, sgr, thresholds)
            if pool["kind"] == "5h":
                clock = remaining_clock(pool["reset_ms"])
                dash = f"-{clock}" if clock else ""
                seg.append(f"{NO_DIM}{color}5h {bar(pct, bar_w, sgr, thresholds)}{BOLD}{dyn_color(pct, sgr, thresholds)}{pct}%{dash}{RESET_DIM}")
            elif pool["kind"] == "weekly":
                clock = long_clock(pool["reset_ms"])
                dash = f"-{clock}" if clock else ""
                seg.append(f"{NO_DIM}{BOLD}{color}周{pct}%{dash}{RESET_DIM}")
            elif pool["kind"] == "mcp" and pct >= 5:
                seg.append(f"mcp{pct}%")
        parts.append(f"{label} {' '.join(seg)}".rstrip())
    return "·".join(parts)


# ---------- CLI 模式（CC Switch 配合：切账号前后直读） ----------

def render_cli(accounts, current, provider_name, sgr, thresholds):
    R, G, Y, SLOT = sgr["red"], sgr["green"], sgr["yellow"], sgr["slot"]
    lines = [f"{BOLD}{sgr['purple']}◆ {provider_name} 配额总览{RESET_DIM}  {time.strftime('%m-%d %H:%M')}", "─" * 52]
    kind_names = {"5h": "5h ", "weekly": "周池", "mcp": "mcp"}
    for label in sorted(accounts):
        acc = accounts[label]
        mark = " ← 当前会话" if label == current else ""
        plan = "积分套餐" if acc["account_kind"] == "credit" else "5h窗口套餐"
        lines.append(f"{BOLD}{label}{RESET_DIM} ({plan}){mark}")
        for pool in acc["pools"]:
            pct = pool["pct"]
            color = dyn_color(pct, sgr, thresholds)
            name = kind_names.get(pool["kind"], pool["kind"])
            filled = round(min(max(pct, 0), 100) / 100 * 20)
            barstr = f"{color}{'█' * filled}{SLOT}{'░' * (20 - filled)}{RESET_DIM}"
            reset_ms = pool["reset_ms"]
            if pool["kind"] == "5h":
                clock = remaining_clock(reset_ms)
                tail = f"{color}{clock} 后重置{RESET_DIM}" if clock else "已过期，下个请求开新窗口"
            elif reset_ms:
                days = long_clock(reset_ms)
                local = time.strftime("%m-%d %H:%M", time.localtime(reset_ms / 1000))
                tail = f"{color}{days} 后（{local}）重置{RESET_DIM}"
            else:
                tail = ""
            lines.append(f"  {name:<4}{barstr} {BOLD}{color}{pct:>3}%{RESET_DIM}  {tail}")
    lines.append("─" * 52)
    lines.append("提示: --refresh 强刷缓存 | 配合 CC Switch 切换账号前后查看")
    return "\n".join(lines)


# ---------- 主入口 ----------

def main():
    ap = argparse.ArgumentParser(description="GLM Coding Plan 用量监控 (HUD/CLI)")
    ap.add_argument("--mode", choices=["hud", "cli"], default="hud",
                    help="hud=claude-hud extra-cmd JSON（默认）; cli=终端彩色直读")
    ap.add_argument("--refresh", action="store_true", help="跳过缓存强制探测")
    ap.add_argument("--show", choices=["current", "all"], default=None,
                    help="HUD 模式账号范围（env GLM_HUD_SHOW 可设默认; cli 恒为 all）")
    args = ap.parse_args()

    cfg = load_config()
    provider = cfg["providers"].get("glm")
    if not provider:
        print(json.dumps({"label": "quota-hud: no glm provider config"}))
        return

    cache = load_cache() if not args.refresh else {}
    fresh = not args.refresh and (time.time() - cache.get("ts", 0)) < CACHE_TTL
    if fresh:
        accounts = {k: v for k, v in cache.get("accounts", {}).items()}
        current = cache.get("current", "")
    else:
        accounts, current = probe_all(provider, force=args.refresh)
        if accounts:
            save_cache(accounts, current)

    if args.mode == "cli":
        print(render_cli(accounts, current, provider["name"], cfg["sgr"], cfg["display"]["thresholds"]))
        return

    # HUD 模式
    show = args.show or os.environ.get("GLM_HUD_SHOW", "current")
    d = cfg["display"]
    if show == "all" or not current:
        shown = accounts
        bar_w = d["bar_width_multi"] if len(accounts) > 1 else d["bar_width_single"]
    else:
        shown = {current: accounts[current]} if current in accounts else accounts
        bar_w = d["bar_width_single"]
    label = render_hud(shown, current, cfg["sgr"], d["thresholds"], bar_w) or "GLM: fetch failed"
    print(json.dumps({"label": label}))


if __name__ == "__main__":
    main()
