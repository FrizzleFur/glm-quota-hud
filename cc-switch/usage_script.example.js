// CC Switch 额度脚本 —— GLM Coding Plan（智谱 open.bigmodel.cn）
//
// 用法：整段复制，粘贴到 CC Switch 该供应商的「额度/用量脚本」配置里。
// 关键：脚本类型选「Token Plan / 编码计划」（供应商选 Zhipu/智谱），不要选 Custom——
//   Token Plan 渲染 = 每个额度池平铺一行（5小时一行、每周一行）；
//   Custom 渲染 = 明细折叠进悬停/展开，卡片上只剩一行。本脚本两处都兼容，但推荐前者。
//
// 脚本不含任何密钥：{{apiKey}} 会被 CC Switch 在执行前自动替换为
// 该供应商配置里填写的 API Key，多个 GLM 账号粘贴同一段脚本即可，各卡片各用各的 key。
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
    //   TIME_LIMIT   = MCP 周池（默认不展示；如需展示可仿照下方再建一档）
    // unit 语义: 3 = 5 小时滚动窗口；6 = 每周窗口
    // percentage = 已用百分比（0-100）
    // 注意：窗口闲置过期时接口不返回该池（接口隐藏规则），属正常现象
    var limits = (response.data && response.data.limits) || [];
    var fiveHour = null, weekly = null;

    function fmtReset(ms) {
      if (!ms) return null;
      var d = new Date(ms);
      var p = function (n) { return (n < 10 ? "0" : "") + n; };
      return "↻ 重置 " + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
    }

    for (var i = 0; i < limits.length; i++) {
      var it = limits[i];
      var t = it.type || "";
      if (t !== "CREDIT_LIMIT" && t !== "TOKENS_LIMIT") continue;
      var pct = it.percentage || 0;
      // emoji 语义与 glm-quota-hud 状态栏对齐：⚡ 5h 窗口 / 💰 积分池 / 📅 周池
      var tier = {
        planName: t === "CREDIT_LIMIT" ? "💰 5小时(积分)" : "⚡ 5小时",
        total: 100,
        used: pct,
        remaining: 100 - pct,
        unit: "%",
        isValid: true,
        invalidMessage: null,
        extra: fmtReset(it.nextResetTime),
      };
      if (it.unit === 3 && !fiveHour) {
        fiveHour = tier;
      } else if (it.unit === 6 && !weekly) {
        tier.planName = t === "CREDIT_LIMIT" ? "📅 每周(积分)" : "📅 每周额度";
        weekly = tier;
      }
    }

    var out = [];
    if (fiveHour) out.push(fiveHour);
    if (weekly) out.push(weekly);

    // 两个窗口都未返回时给出可读提示（直接返回空数组会被 CC Switch 判为脚本错误）
    if (out.length === 0) {
      return {
        planName: "⚠️ GLM 额度",
        total: null,
        used: null,
        remaining: null,
        unit: null,
        isValid: false,
        invalidMessage: "未返回额度池：检查 API Key 是否为 GLM Coding Plan 令牌，或所有窗口当前均未激活",
      };
    }
    return out;
  },
})
