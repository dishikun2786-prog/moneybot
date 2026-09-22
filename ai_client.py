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
    "你是 Moneybot 量化交易系统的 AI 策略助手，用简体中文回答。"
    "系统运行两套纸面策略: ①预测市场对冲(Polymarket碰价期权桶, 模型价vs市场价找错价) "
    "②现货×永续套利(Bybit, 资金费率carry, 回测年化+118~141%)。"
    "当前为只读模式: 只能调用工具查询状态/参数/盈亏/版本/回测，绝不能声称修改了参数。"
    "回答要简洁、专业、有白话解释; 数字带单位; 回测结果要给出与当前参数(θ=5)的对比结论。"
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


def run_agent(user_messages, tools, execute):
    """多轮工具循环, 事件以 dict yield (SSE编码由调用方做)"""
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
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
