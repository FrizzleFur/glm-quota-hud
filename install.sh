#!/usr/bin/env bash
# install.sh — glm-quota-hud 安装器
# 1) 复制脚本到 ~/.claude/plugins/claude-hud/
# 2) 对已安装的 claude-hud dist 打 SGR 白名单补丁（幂等）
# 3) 打印 statusLine 配置示例（不自动改 settings.json——用户自己贴）
set -euo pipefail

HUD_DIR="$HOME/.claude/plugins/claude-hud"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$HUD_DIR"
cp "$SRC_DIR/glm_quota_hud.py" "$SRC_DIR/patch-sgr.sh" "$HUD_DIR/"
[ -f "$SRC_DIR/providers.json" ] && cp "$SRC_DIR/providers.json" "$HUD_DIR/" \
  || cp "$SRC_DIR/providers.example.json" "$HUD_DIR/providers.example.json"
echo "✓ 脚本已复制到 $HUD_DIR"

# 找 claude-hud 安装（cache 目录版本号自动探测）并打补丁
if [ -d "$HOME/.claude/plugins/cache/claude-hud/claude-hud" ]; then
  bash "$HUD_DIR/patch-sgr.sh"
else
  echo "⚠ 未检测到 claude-hud 插件（~/.claude/plugins/cache/claude-hud/）"
  echo "  请先安装 claude-hud 再重跑本脚本，或只用 CLI 模式（无需 HUD）"
fi

echo ""
echo "=== 下一步：配置 statusLine ==="
echo "在 ~/.claude/settings.json 的 statusLine.command 中追加："
echo '  --extra-cmd "python3 '"$HUD_DIR"'/glm_quota_hud.py"'
echo ""
echo "完整示例："
echo '  "statusLine": {'
echo '    "type": "command",'
echo '    "command": "node ~/.claude/plugins/cache/claude-hud/claude-hud/0.0.12/dist/index.js --extra-cmd \"python3 '"$HUD_DIR"'/glm_quota_hud.py\""'
echo '  }'
echo ""
echo "token 环境变量（加到 ~/.zshrc.secrets，勿写进任何 git 仓库）："
echo '  export GLM_V1_TOKEN="..."   # 5h 窗口套餐 token'
echo '  export GLM_V3_TOKEN="..."   # 积分套餐 token'
echo ""
echo "✓ 安装完成。CLI 预览: python3 $HUD_DIR/glm_quota_hud.py --mode cli --refresh"
