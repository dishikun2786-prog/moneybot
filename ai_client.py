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
    "平台策略：现货×永续套利(Bybit，资金费率 carry)+现货波段。"
    "你可以调用工具: 查询系统用 strategy_status/list_params/get_pnl/git_log/run_backtest/get_micro; "
    "查询当前用户用 my_positions/my_balance/my_trades(优先用这三个回答用户自身问题)。"
    "修改参数用 update_params、回退用 git_rollback、重启用 restart_engine——"
    "这三个变更工具只生成【预览】并返回 action_id，【必须等用户在界面点击批准后才生效】。"
    "收到预览结果后要明确告诉用户: 修改了什么(旧值→新值)、影响哪个引擎(热加载下轮生效), 并提示用户点击批准。"
    "平台另有 Jev 快速决策层(TypeSafe System One): 每5分钟对行情+回测做批量开仓判断, "
    "决策全量留痕在 jev_decisions.jsonl (可用 get_jev_decisions 工具读最近N条)。"
    "你可以通过 update_params 调整 jev 组门控参数(与修改其他参数一样走预览→批准三闸): "
    "open_p=开仓概率门控(0.5-0.95, 越高越保守) / conf_min=置信度下限(0.5-0.9) / "
    "risk_pause=风险暂停线(score≥该值暂停新开仓建议, 1-4) / l1_and_mode=Jev否定时是否拦截L1开仓(0观测/1拦截)。"
    "巡检流程: 先 get_jev_decisions 看最近决策, 结合 my_positions 持仓和行情, 判断门控参数是否需要调整。"
    "铁律: ①涉及用户持仓/资金的问题必须先调用工具查实时数据，禁止凭空猜测; "
    "②不承诺收益，给建议必须带风险提示; ③数字带单位, 金额保留2位小数; "
    "④Jev门控参数调整同样必须走预览→用户批准, 绝不直接生效。"
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
