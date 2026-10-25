// CC Switch 额度脚本 —— GLM Coding Plan（智谱 open.bigmodel.cn）
// 形态：符号版（↻ 单色符号，非 emoji），单条目合并
//
// 用法：整段复制，粘贴到 CC Switch 该供应商的「额度/用量脚本」配置里，类型选「Custom（自定义）」，
// 语言 JavaScript。脚本不含任何密钥：{{apiKey}} 会被 CC Switch 在执行前自动替换为
// 该供应商配置里填写的 API Key，多个 GLM 账号粘贴同一段脚本即可，各卡片各用各的 key。
//
// 设计约束（v4.0.20 实测）：
// - 卡片对 ≥2 条额度强制折叠成「N 个套餐」摘要 → 本脚本只返回单条目（周池余量并入行尾）
// - label 列固定 72px 截断 → planName 用短文案（5h / 5h·积分 / 周·积分 / MCP）
// - token 制账号 5h 窗口闲置耗尽后接口隐藏该池 → 无 5h/周池时用 MCP 池（TIME_LIMIT）兜底
//
// 详细教程见同目录 README.md。

({
  request: {
    // 智谱官方额度监控接口，与 glm-quota-hud 状态栏查询的是同一个接口
    url: "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
    method: "GET",
    headers: {
      // 接口同时接受 "{{apiKey}}" 与 "Bearer {{apiKey}}" 两种写法；
      // 若 401 可先检查 Key 本身，再尝试加 Bearer 前缀
      "Authorization": "{{apiKey}}",
      "Content-Type": "application/json",
      "Accept-Language": "en-US,en",
    },
  },

  extractor: function (response) {
    // 响应结构: { data: { limits: [ { type, unit, percentage, nextResetTime, ... } ] } }
    // type 语义:
    //   TOKENS_LIMIT = token 制套餐（5h 窗口型）
    //   CREDIT_LIMIT = 积分制套餐（5h 窗口 + 周积分池，靠 unit 区分）
    //   TIME_LIMIT   = MCP 周池（5h/周池都未返回时的兜底行）
    // unit 语义: 3 = 5 小时滚动窗口；6 = 每周窗口
    // percentage = 已用百分比（0-100）
    // 注意：窗口闲置过期时接口不返回该池（接口隐藏规则），属正常现象
    var limits = (response.data && response.data.limits) || [];
    var fiveHour = null, weekly = null, mcp = null;

    function fmtReset(ms) {
      if (!ms) return null;
      var d = new Date(ms);
      var p = function (n) { return (n < 10 ? "0" : "") + n; };
      return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
    }

    for (var i = 0; i < limits.length; i++) {
      var it = limits[i];
      var t = it.type || "";
      var pct = it.percentage || 0;
      if (t === "TIME_LIMIT") {
        if (!mcp) mcp = { used: pct, reset: fmtReset(it.nextResetTime) };
        continue;
      }
      if (t !== "CREDIT_LIMIT" && t !== "TOKENS_LIMIT") continue;
      if (it.unit === 3 && !fiveHour) {
        fiveHour = { used: pct, reset: fmtReset(it.nextResetTime), credit: t === "CREDIT_LIMIT" };
      } else if (it.unit === 6 && !weekly) {
        weekly = { used: pct, reset: fmtReset(it.nextResetTime) };
      }
    }

    // 主行优先级：5h 池 > 周池 > MCP 池；低优先级信息并入行尾 extra 文字
    if (fiveHour) {
      var parts = [];
      if (fiveHour.reset) parts.push("↻ " + fiveHour.reset);
      if (weekly) {
        parts.push("周余" + (100 - weekly.used) + "%" + (weekly.reset ? " ↻" + weekly.reset : ""));
      }
      return [{
        planName: fiveHour.credit ? "5h·积分" : "5h",
        total: 100,
        used: fiveHour.used,
        remaining: 100 - fiveHour.used,
        unit: "%",
        isValid: true,
        invalidMessage: null,
        extra: parts.join(" · "),
      }];
    }

    if (weekly) {
      return [{
        planName: "周·积分",
        total: 100,
        used: weekly.used,
        remaining: 100 - weekly.used,
        unit: "%",
        isValid: true,
        invalidMessage: null,
        extra: weekly.reset ? "↻ " + weekly.reset : null,
      }];
    }

    if (mcp) {
      return [{
        planName: "MCP",
        total: 100,
        used: mcp.used,
        remaining: 100 - mcp.used,
        unit: "%",
        isValid: true,
        invalidMessage: null,
        extra: mcp.reset ? "↻ " + mcp.reset : null,
      }];
    }

    return [{
      planName: "GLM",
      total: null,
      used: null,
      remaining: null,
      unit: null,
      isValid: false,
      invalidMessage: "未返回额度池：检查 API Key 是否为 GLM Coding Plan 令牌，或所有窗口当前均未激活",
    }];
  },
})
