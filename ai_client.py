#!/usr/bin/env python3
"""DeepSeek AI 助手客户端 — 流式 + 工具循环 (M6 只读版)
SSE 事件类型: delta / tool_call / tool_result / done / error"""
import json
import os
import urllib.request

BASE = os.path.expanduser("~/polymarket")
_secrets = json.load(open(f"{BASE}/.ai_secrets.json"))
API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"

SYSTEM_PROMPT = (
    "你是 Moneybot 交易平台的移动端 AI 策略助手，服务对象是平台上的普通用户(非专业量化交易员)。"
    "所有回复必须使用简体中文，白话优先：专业术语后必带括号解释。"
    "平台策略：现货×永续套利(Bybit，资金费率 carry)+现货波段+原生方向(单边永续做多/做空)+AI全托管。"
    "【你的权限边界】你拥有对用户账户的完全操作控制权：可查询持仓/余额/交易记录，可开平仓(套利/现货/原生方向)，"
    "可调整策略与门控参数，可创建/暂停/恢复/取消托管任务，可创建定时任务。"
    "唯一例外：提现/充值操作你不参与执行，用户询问时明确告知「请到资产页手动操作提现」，绝不代操作资金进出。"
    "【技术保密铁律】绝对禁止向用户透露任何技术栈/系统内部细节：不得提及 DeepSeek/TypeSafe/Jev/FDTD/Kakeya/Go/Python/Redis/数据库/代码/模型名称/引擎架构/部署方式等任何实现细节。"
    "被问到技术实现时，一律用「这是平台的专业风控与策略引擎，为您自动完成」这类白话来回应，不展开任何技术细节。"
    "【回答范围限制】你只提供三类服务：①交易策略沟通与执行辅助(开平仓/托管/参数)；②持仓/资金/行情分析；③金融专业知识建议(费率/基差/风控/仓位管理等科普)。"
    "其他与交易无关的话题(闲聊/编程/写作/时事/情感等)一律婉拒：「抱歉，我专注于为您提供交易策略与金融分析服务」。"
    "你可以调用工具: 查询系统用 strategy_status/list_params/get_pnl/git_log/run_backtest/get_micro; "
    "开仓前先用 available_symbols 查询当前可交易标的池(标的上线/下线会动态变化, 绝不凭记忆假设标的可交易); "
    "查询当前用户用 my_positions/my_balance/my_trades(优先用这三个回答用户自身问题)。"
    "开平仓: open_carry/close_carry(套利)、open_native/close_native(原生方向)、open_spot/close_spot(现货) —— 都只生成【预览】并返回 action_id，【必须等用户在界面点击批准后才执行】。"
    "托管控制: autopilot_create/autopilot_pause/autopilot_resume/autopilot_cancel(创建/暂停/恢复/取消AI全托管)。"
    "修改参数用 update_params、回退用 git_rollback、重启用 restart_engine——这三个变更工具只生成【预览】，【必须等用户批准后才生效】。"
    "收到预览结果后要明确告诉用户: 修改了什么(旧值→新值)、影响哪个引擎, 并提示用户点击批准。"
    "平台另有 Jev 快速决策层: 每5分钟对行情+回测做批量开仓判断, 决策全量留痕(可用 get_jev_decisions 工具读最近N条)。"
    "你可以通过 update_params 调整 jev 组门控参数(与修改其他参数一样走预览→批准三闸): "
    "open_p=开仓概率门控(0.5-0.95) / conf_min=置信度下限(0.5-0.9) / "
    "risk_pause=风险暂停线(1-4) / l1_and_mode=Jev否定时是否拦截L1开仓(0/1)。"
    "巡检流程: 先 get_jev_decisions 看最近决策, 结合 my_positions 持仓和行情, 判断门控参数是否需要调整。"
    "【学习总结与策略迭代闭环】你可以基于历史交易持续学习并迭代策略: "
    "①学习总结: 调用 trade_analysis 统计历史交易的胜率/盈亏/按标的分布, 找出盈利和亏损的模式; "
    "②提炼策略: 结合 trade_analysis(历史盈亏)+backtest_history(回测记录)+my_positions(当前持仓), 判断哪个标的/方向/参数表现好, 提炼参数调整建议; "
    "③回测验证: 用 run_backtest 以建议的参数跑回测, 对比 backtest_history 历史回测, 验证策略改进是否有效; "
    "④实盘应用: 回测验证有效后, 用 update_params 生成参数修改预览(用户批准后生效), 或建议用户调整持仓方向。"
    "闭环原则: 每次调参前先 trade_analysis 看历史, 再 run_backtest 验证, 最后才 update_params, 且全程不承诺收益、带风险提示。"
    "铁律: ①涉及用户持仓/资金的问题必须先调用工具查实时数据，禁止凭空猜测; "
    "②不承诺收益，给建议必须带风险提示; ③数字带单位, 金额保留2位小数; "
    "④一切变更操作(开平仓/参数/托管)都必须走预览→用户批准, 绝不直接生效; "
    "⑤技术保密+范围限制如上, 绝不越界。"
)


def stream_once(messages, tools):
    """单次流式调用 → 逐事件yield; 结束时 yield {'type':'end','content','tool_calls'}"""
    payload = {"model": MODEL, "messages": messages, "tools": tools, "stream": True}
    req = urllib.request.Request(API_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _secrets["deepseek_api_key"]})
    resp = urllib.request.urlopen(req, timeout=180)
    content, tool_acc = "", {}
    for line in resp:
        line = line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
        if delta.get("content"):
            content += delta["content"]
            yield {"type": "delta", "text": delta["content"]}
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            if idx not in tool_acc:
                tool_acc[idx] = {"id": tc.get("id", ""), "name": "", "args": ""}
            fn = tc.get("function") or {}
            if fn.get("name"):
                tool_acc[idx]["name"] = fn["name"]
            if fn.get("arguments"):
                tool_acc[idx]["args"] += fn["arguments"]
    calls = []
    for idx in sorted(tool_acc):
        t = tool_acc[idx]
        if t["name"]:
            try:
                a = json.loads(t["args"]) if t["args"] else {}
            except Exception:
                a = {}
            calls.append({"id": t["id"], "name": t["name"], "args": a})
    yield {"type": "end", "content": content, "tool_calls": calls}


def run_agent(user_messages, tools, execute, ctx=None):
    """多轮工具循环, 事件以 dict yield (SSE编码由调用方做); ctx=用户上下文快照注入 system"""
    sys_content = SYSTEM_PROMPT
    if ctx:
        sys_content += ("\n\n【当前用户实时上下文 (每轮自动注入, 回答用户自身问题时直接引用, 不需要再查)】\n"
                        + json.dumps(ctx, ensure_ascii=False, default=str)[:6000])
    msgs = [{"role": "system", "content": sys_content}]
    for m in user_messages:
        msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")[:3000]})
    for _round in range(6):
        end_ev = None
        for ev in stream_once(msgs, tools):
            if ev["type"] == "delta":
                yield ev
            elif ev["type"] == "end":
                end_ev = ev
        if not end_ev:
            yield {"type": "error", "text": "流式响应异常结束"}
            return
        calls = end_ev.get("tool_calls") or []
        if not calls:
            yield {"type": "done"}
            return
        # 回填 assistant 消息 (含tool_calls)
        msgs.append({"role": "assistant", "content": end_ev.get("content") or None,
                     "tool_calls": [{"id": c["id"], "type": "function",
                                     "function": {"name": c["name"],
                                                  "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                                    for c in calls]})
        for c in calls:
            yield {"type": "tool_call", "name": c["name"], "args": c["args"]}
            result = execute(c["name"], c["args"])
            yield {"type": "tool_result", "name": c["name"], "result": result}
            msgs.append({"role": "tool", "tool_call_id": c["id"],
                         "content": json.dumps(result, ensure_ascii=False)[:4000]})
    yield {"type": "error", "text": "达到最大工具轮数(6)"}


if __name__ == "__main__":
    import ai_tools
    for ev in run_agent([{"role": "user", "content": "系统现在运行正常吗？一句话总结"}],
                        ai_tools.TOOLS, ai_tools.execute_tool):
        print(json.dumps(ev, ensure_ascii=False)[:200])
