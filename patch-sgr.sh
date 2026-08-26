#!/usr/bin/env bash
# patch-sgr.sh — claude-hud 升级后重打 SGR 白名单补丁（幂等）
# 作用: sanitize 透传 truecolor(38;2)/reset(0)/bold(1)/dim(2)/no-dim(22) 五种
#       SGR 码（glm-usage.py 进度条/周池着色依赖）；其余转义仍全剥（防注入不变）。
#       长度检查改为剔除 SGR 后计数（色码不占 50 字符预算）。
# 版本适配 (2026-08-23 v2):
#   0.8.0+ — sanitize 重构进 dist/utils/sanitize.js（sanitizeDisplayText 共享模块），
#            stash/unstash 打在共享模块，长度检查打在 dist/extra-cmd.js
#   ≤0.3.0 — sanitize 内联在 dist/extra-cmd.js（旧路径，保留兼容）
set -euo pipefail

# 自动定位最新版本 dist（版本号排序，勿用 mtime：升级后新旧目录 mtime 相同）
HUD_BIN=$(ls -d ~/.claude/plugins/cache/claude-hud/claude-hud/*/ 2>/dev/null | sort -V | tail -1)
[ -z "$HUD_BIN" ] && { echo "✗ 未找到 claude-hud 安装"; exit 1; }
echo "目标: $HUD_BIN"

if [ -f "${HUD_BIN}dist/utils/sanitize.js" ]; then
  # ---- 0.8.0+ 路径: 共享 sanitize 模块 + extra-cmd 长度检查 ----
  python3 - "${HUD_BIN}dist/utils/sanitize.js" << 'PYEOF'
import sys
p = sys.argv[1]
src = open(p).read()
if 'PATCH(2026-08-23-v2)' in src:
    print("✓ sanitize 补丁已是最新，跳过"); sys.exit(0)

old_head = """export function sanitizeDisplayText(input) {
    return input
        .replace(/\\x1B\\[[0-?]*[ -/]*[@-~]/g, '') // CSI sequences"""
new_head = """export function sanitizeDisplayText(input) {
    // PATCH(2026-08-23-v2): SGR 白名单透传（0.8.0+ 共享模块版）——truecolor 38;2 与
    // reset 0/bold 1/dim 2/no-dim 22 先摘出，其余 CSI/OSC/C0/bidi 仍全剥（防注入不变）。
    // 重打补丁: ~/.claude/plugins/claude-hud/patch-sgr.sh
    const kept = [];
    const stash = (m) => { kept.push(m); return `\\ue000${kept.length - 1}\\ue000`; };
    input = input.replace(/\\x1B\\[(?:38;2;\\d{1,3};\\d{1,3};\\d{1,3}|0|1|2|22)m/g, stash);
    return input
        .replace(/\\x1B\\[[0-?]*[ -/]*[@-~]/g, '') // CSI sequences"""
assert old_head in src, "sanitizeDisplayText 头部未匹配（claude-hud 结构可能又变，请人工核对）"
src = src.replace(old_head, new_head)

old_tail = """    .replace(CONTROL_AND_BIDI_PATTERN, ''); // control + bidi chars
}"""
new_tail = """    .replace(CONTROL_AND_BIDI_PATTERN, '') // control + bidi chars
        .replace(/\\ue000(\\d+)\\ue000/g, (_, i) => kept[+i]);
}"""
assert old_tail in src, "sanitizeDisplayText 尾部未匹配"
src = src.replace(old_tail, new_tail)
open(p, 'w').write(src)
print("✓ sanitize.js 补丁完成")
PYEOF

  python3 - "${HUD_BIN}dist/extra-cmd.js" << 'PYEOF'
import sys
p = sys.argv[1]
src = open(p).read()
if 'PATCH(2026-08-23-v2)' in src:
    print("✓ 长度检查补丁已是最新，跳过"); sys.exit(0)

old_len = """        if (label.length > MAX_LABEL_LENGTH) {"""
new_len = """        // PATCH(2026-08-23-v2): 可见长度计数（白名单 SGR 不占 50 预算）
        if (label.replace(/\\x1B\\[[0-9;]*m/g, '').length > MAX_LABEL_LENGTH) {"""
assert old_len in src, "长度检查未匹配（extra-cmd 结构可能又变，请人工核对）"
src = src.replace(old_len, new_len)
open(p, 'w').write(src)
print("✓ extra-cmd.js 长度补丁完成")
PYEOF

  # ---- PATCH(2026-08-26-v3): label 预算扩容 50→150（独立幂等） ----
  # glm-usage.py 追加 📈/🗓 预测段（速率/余量/还能用/结论）需 ~60-100 单元，
  # 50 上限装不下。render 层无 extraLabel 截断（已核实 0.8.0），仅此一处。
  python3 - "${HUD_BIN}dist/extra-cmd.js" << 'PYEOF'
import sys
p = sys.argv[1]
src = open(p).read()
OLD = "const MAX_LABEL_LENGTH = 50;"
NEW = "const MAX_LABEL_LENGTH = 150; // PATCH(2026-08-26-v3): 预测段扩容（patch-sgr.sh）"
if "PATCH(2026-08-26-v3)" in src:
    print("✓ MAX_LABEL_LENGTH 扩容已是最新，跳过"); sys.exit(0)
assert OLD in src, "MAX_LABEL_LENGTH = 50 未匹配（结构可能又变，请人工核对）"
src = src.replace(OLD, NEW)
open(p, 'w').write(src)
print("✓ extra-cmd.js label 预算 50→150 完成")
PYEOF

  HUD_TARGET="${HUD_BIN}dist/utils/sanitize.js" node --input-type=module -e "
const { sanitizeDisplayText } = await import(process.env.HUD_TARGET);
const t = sanitizeDisplayText('\x1b[38;2;1;2;3mA\x1b[0m\x1b[1m\x1b[22mB\x1b]0;x\x07C');
(t.includes('38;2;1;2;3m') && t.includes('\x1b[1m') && !t.includes('x\x07')) ? console.log('✓ 验证通过（truecolor/bold 透传，OSC 仍剥）') : (console.log('✗ 验证失败: ' + JSON.stringify(t)), process.exit(1));
"
else
  # ---- ≤0.3.0 旧路径: sanitize 内联在 dist/extra-cmd.js ----
  TARGET="${HUD_BIN}dist/extra-cmd.js"
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
fi
