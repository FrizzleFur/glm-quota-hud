#!/usr/bin/env bash
# patch-sgr.sh — claude-hud 升级后重打 SGR 白名单补丁（幂等）
# 作用: dist/extra-cmd.js 的 sanitize 透传 truecolor(38;2)/reset(0)/dim(2) 三种
#       SGR 码（glm-usage.py 周池着色依赖）；其余转义仍全剥（防注入不变）。
#       长度检查改为剔除 SGR 后计数（色码不占 50 字符预算）。
set -euo pipefail

# 自动定位最新版本 dist
HUD_BIN=$(ls -d ~/.claude/plugins/cache/claude-hud/claude-hud/*/ 2>/dev/null | sort -V | tail -1)
[ -z "$HUD_BIN" ] && { echo "✗ 未找到 claude-hud 安装"; exit 1; }
TARGET="$HUD_BIN/dist/extra-cmd.js"
echo "目标: $TARGET"

python3 - "$TARGET" << 'PYEOF'
import sys
p = sys.argv[1]
src = open(p).read()
if '|0|1|2|22)m' in src:
    print("✓ 补丁已是最新（含 bold），跳过"); sys.exit(0)
if 'PATCH(2026-08-23)' in src:  # 旧补丁（无 bold）就地升级
    src = src.replace('|0|2)m', '|0|1|2|22)m'); src = src.replace('|0|1|2)m', '|0|1|2|22)m')
    open(p, 'w').write(src)
    print("✓ 旧补丁已升级（白名单 +bold）"); sys.exit(0)

old_san = """export function sanitize(input) {
    return input
        .replace(/\\x1B\\[[0-?]*[ -/]*[@-~]/g, '') // CSI sequences"""
new_san = """export function sanitize(input) {
    // PATCH(2026-08-23): SGR 白名单透传——truecolor 38;2 与 reset 0/dim 2 先摘出，
    // 其余 CSI/OSC/C0/bidi 仍全剥（防注入不变）。重打补丁:
    // ~/.claude/plugins/claude-hud/patch-sgr.sh
    const kept = [];
    const stash = (m) => { kept.push(m); return `\\ue000${kept.length - 1}\\ue000`; };
    input = input.replace(/\\x1B\\[(?:38;2;\\d{1,3};\\d{1,3};\\d{1,3}|0|1|2|22)m/g, stash);
    return input
        .replace(/\\x1B\\[[0-?]*[ -/]*[@-~]/g, '') // CSI sequences"""
assert old_san in src, "sanitize 未匹配（claude-hud 结构可能已变，请人工核对）"
src = src.replace(old_san, new_san)

old_bidi = ".replace(/[\\u061C\\u200E\\u200F\\u202A-\\u202E\\u2066-\\u2069\\u206A-\\u206F]/g, ''); // bidi\n}"
new_bidi = """g, '') // bidi
        .replace(/\\ue000(\\d+)\\ue000/g, (_, i) => kept[+i]);
}"""
assert old_bidi in src, "bidi 行未匹配"
src = src.replace(old_bidi, new_bidi)

old_len = """            let label = sanitize(data.label);
            if (label.length > MAX_LABEL_LENGTH) {"""
new_len = """            let label = sanitize(data.label);
            // PATCH(2026-08-23): 可见长度计数（白名单 SGR 不占 50 预算）
            if (label.replace(/\\x1B\\[[0-9;]*m/g, '').length > MAX_LABEL_LENGTH) {"""
assert old_len in src, "长度检查未匹配"
src = src.replace(old_len, new_len)

open(p, 'w').write(src)
print("✓ 补丁完成")
PYEOF
HUD_TARGET="$TARGET" node --input-type=module -e "
const { sanitize } = await import(process.env.HUD_TARGET);
const t = sanitize('\x1b[38;2;1;2;3mA\x1b[0m\x1b[2mB\x1b]0;x\x07C');
t.includes('38;2;1;2;3m') && !t.includes('x\x07') ? console.log('✓ 验证通过') : (console.log('✗ 验证失败'), process.exit(1));
"
