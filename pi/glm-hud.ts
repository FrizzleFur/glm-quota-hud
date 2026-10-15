import { exec } from "node:child_process";
import { promisify } from "node:util";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// GLM Coding Plan 额度 HUD —— pi coding agent footer 常驻扩展（通用版）
// 复用本仓库 glm_quota_hud.py 的 CLI 模式（install.sh 安装位置）；
// 环境变量 GLM_HUD_CMD 可覆盖命令（如指向 glm-usage.py 深度定制版）
const HUD_CMD =
  process.env.GLM_HUD_CMD ||
  "python3 $HOME/.claude/plugins/claude-hud/glm_quota_hud.py --mode cli";
const REFRESH_MS = 5 * 60 * 1000; // 脚本自带 120s 缓存，5 分钟轮询足够

// 执行外部命令用 node:child_process（pi 扩展 API 无 exec 方法）
// exec 走 shell，HUD_CMD 里的 $HOME 由 shell 展开
const execAsync = promisify(exec);

type Ctx = Parameters<Parameters<ExtensionAPI["on"]>[1]>[1];

function stripAnsi(s: string): string {
  return s
    .replace(/\x1b\[[0-9;]*m/g, "")
    .replace(/\x1b\][^\x07]*(\x07|\x1b\\)/g, "");
}

async function refresh(ctx: Ctx): Promise<void> {
  try {
    const { stdout } = await execAsync(HUD_CMD, { timeout: 15_000 });
    let text = stripAnsi(stdout).trim();
    try {
      const j = JSON.parse(text);
      // label 自带 ANSI 彩色码，parse 后需再剥一次
      if (j && typeof j.label === "string") text = stripAnsi(j.label);
    } catch {
      /* 非 JSON 就按纯文本用 */
    }
    if (text) ctx.ui.setStatus("glm", text);
  } catch (err) {
    // 失败要可见，不静默空白（踩坑：曾用不存在的 pi.exec，静默失败致 footer 永远空白）
    const msg = err instanceof Error ? err.message.split("\n")[0] : String(err);
    ctx.ui.setStatus("glm", `glm-hud 不可用: ${msg.slice(0, 60)}`);
  }
}

export default function (pi: ExtensionAPI) {
  let timer: ReturnType<typeof setInterval> | undefined;

  // 官方契约：定时器等长生命周期资源在 session_start 启动，不在 factory 里
  pi.on("session_start", async (_event, ctx) => {
    await refresh(ctx);
    if (!timer) {
      timer = setInterval(() => void refresh(ctx), REFRESH_MS);
      // -p / rpc 模式下不让定时器阻止进程退出
      (timer as any)?.unref?.();
    }
  });

  // 幂等清理（session_shutdown）
  pi.on("session_shutdown", async () => {
    if (timer) clearInterval(timer);
    timer = undefined;
  });

  // 手动刷新：/glmhud
  pi.registerCommand("glmhud", {
    description: "刷新 GLM 额度 HUD",
    handler: async (_args, ctx) => {
      await refresh(ctx);
    },
  });
}
