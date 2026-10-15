import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// GLM Coding Plan 额度 HUD —— pi coding agent footer 常驻扩展
// 复用本仓库 glm_quota_hud.py 的 CLI 模式；pi 的 footer 会剥离 ANSI，此处输出纯文本进度条
// 环境变量 GLM_HUD_CMD 可覆盖命令（如指向 glm-usage.py 深度定制版）
const HUD_CMD =
  process.env.GLM_HUD_CMD ||
  "python3 $HOME/.claude/plugins/claude-hud/glm_quota_hud.py --mode cli";
const REFRESH_MS = 5 * 60 * 1000; // 脚本自带 120s 缓存，5 分钟轮询足够

function stripAnsi(s: string): string {
  return s
    .replace(/\x1b\[[0-9;]*m/g, "")
    .replace(/\x1b\][^\x07]*(\x07|\x1b\\)/g, "");
}

type Ctx = Parameters<Parameters<ExtensionAPI["on"]>[1]>[1];

async function refresh(pi: ExtensionAPI, ctx: Ctx): Promise<void> {
  try {
    const res = await (pi as any).exec(HUD_CMD);
    const raw: string = typeof res === "string" ? res : String(res?.stdout ?? "");
    let text = stripAnsi(raw).trim();
    try {
      const j = JSON.parse(text);
      if (j && typeof j.label === "string") text = j.label;
    } catch {
      /* 非 JSON 就按纯文本用 */
    }
    if (text) ctx.ui.setStatus("glm", text);
  } catch {
    // 静默失败：HUD 不可用不应打断 TUI（缺 token / 网络失败等）
  }
}

export default function (pi: ExtensionAPI) {
  let lastCtx: Ctx | undefined;

  pi.on("session_start", async (_event, ctx) => {
    lastCtx = ctx;
    await refresh(pi, ctx);
  });

  const timer = setInterval(() => {
    if (lastCtx) refresh(pi, lastCtx);
  }, REFRESH_MS);
  // -p / rpc 模式下不让定时器阻止进程退出
  (timer as any)?.unref?.();

  // 手动刷新：/glmhud
  pi.registerCommand("glmhud", {
    description: "刷新 GLM 额度 HUD",
    handler: async (_args, ctx) => {
      await refresh(pi, ctx);
    },
  });

  return () => {
    if (timer) clearInterval(timer);
  };
}
