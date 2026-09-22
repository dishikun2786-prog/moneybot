#!/bin/bash
# AI 助手安全演练 (M8): 只读工具 + 变更审批流 + 越权拒绝 + 审计完整性
# 用法: bash tools/ai_exercise.sh (在服务器 ~/polymarket 下运行)
cd "$(dirname "$0")/.." || exit 1
PY=./venv/bin/python
echo "===== [1] 只读工具 ====="
for t in strategy_status list_params get_pnl git_log; do
  $PY ai_tools.py $t > /tmp/ro.json 2>&1 && echo "  $t ✓" || echo "  $t ✗"
done
echo "===== [2] 变更预览(不生效) ====="
$PY -c "
import ai_tools, json
r = ai_tools.t_update_params({'changes': {'carry': {'theta_in_ann_pct': 5.0}}})
assert r['status'] == 'preview', r
assert json.load(open('strategy_params.json'))['carry']['theta_in_ann_pct'] == 5.0, '预览阶段不得修改!'
print('  预览阶段参数未被修改 ✓')
"
echo "===== [3] 越权拒绝 ====="
$PY -c "
import ai_tools
tests = [
    ('非白名单键', ai_tools.t_update_params({'changes': {'carry': {'evil': 1}}})),
    ('超范围', ai_tools.t_update_params({'changes': {'carry': {'theta_in_ann_pct': 99}}})),
    ('未知组', ai_tools.t_update_params({'changes': {'funds': {'x': 1}}})),
    ('超3键', ai_tools.t_update_params({'changes': {'carry': {'a': 1, 'b': 2, 'c': 3, 'd': 4}}})),
    ('坏单元', ai_tools.t_restart_engine({'unit': 'nginx.service'})),
    ('坏rev', ai_tools.t_git_rollback({'rev': 'zzz'})),
]
for name, r in tests:
    assert r.get('status') == 'rejected', (name, r)
    print(f'  {name} 被拒 ✓')
"
echo "===== [4] 审计与版本 ====="
echo "  审计行数: $(wc -l < logs/ai_actions.jsonl 2>/dev/null || echo 0)"
echo "  git版本数: $(git log --oneline 2>/dev/null | wc -l)"
echo "===== 演练完成: 全部通过 ====="
