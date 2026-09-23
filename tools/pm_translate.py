#!/usr/bin/env python3
"""R11 P1: PM 标题/问题 DeepSeek 批量翻译 → logs/pm_zh.json
用法: ./venv/bin/python tools/pm_translate.py [--limit N]
- 输入: logs/pm_tokens.json (title/question 去重)
- 输出: logs/pm_zh.json {t: {title: zh}, q: {question: zh}} (增量, 原子写)
- 复用 .ai_secrets.json 的 deepseek_api_key; 批间限速; 失败重试2次
"""
import argparse
import json
import os
import sys
import time
import urllib.request

BASE = os.path.expanduser("~/polymarket")
TOK_FILE = f"{BASE}/logs/pm_tokens.json"
ZH_FILE = f"{BASE}/logs/pm_zh.json"
API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"
BATCH = 30

SYSTEM = (
    "你是专业的体育/政治/加密事件翻译助手。把英文标题翻译成简体中文，"
    "要求：1) 保留队伍名/人名缩写原文(如 NBA、MMA、Dota 2、FED)；"
    "2) 简洁口语化, 标题≤20字, 问题≤30字；3) 数字/百分比保留原文格式。"
    "输入是 JSON 数组, 只输出同序 JSON 数组, 每项 {\"s\":原文,\"z\":中文}, 不要输出其他内容。"
)


def _secrets():
    with open(f"{BASE}/.ai_secrets.json", encoding="utf-8") as f:
        return json.load(f)


def _ds_call(items, key):
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode())
    content = d["choices"][0]["message"]["content"] or ""
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


def translate_batch(items, key):
    """items: [原文字符串]; 返回 {原文: 中文}"""
    srcs = [{"s": s} for s in items]
    last_err = None
    for attempt in range(3):
        try:
            arr = _ds_call(srcs, key)
            out = {}
            for r in arr:
                s = str(r.get("s") or "")
                z = str(r.get("z") or "").strip()
                if s and z:
                    out[s] = z
            if out:
                return out
            last_err = "空结果"
        except Exception as e:
            last_err = str(e)[:100]
        time.sleep(2 * (attempt + 1))
    print(f"  ⚠ 批次失败(3次重试): {last_err}", flush=True)
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只翻前 N 批(调试)")
    args = ap.parse_args()

    with open(TOK_FILE, encoding="utf-8") as f:
        toks = json.load(f)

    titles = []
    seen_t = set()
    questions = []
    seen_q = set()
    for t in toks:
        title = (t.get("title") or "").strip()
        q = (t.get("question") or "").strip()
        if title and title not in seen_t:
            seen_t.add(title)
            titles.append(title)
        if q and q not in seen_q:
            seen_q.add(q)
            questions.append(q)

    zh = {"t": {}, "q": {}}
    if os.path.exists(ZH_FILE):
        try:
            with open(ZH_FILE, encoding="utf-8") as f:
                zh = json.load(f)
        except Exception:
            zh = {"t": {}, "q": {}}

    todo_t = [s for s in titles if not (zh["t"].get(s) or "").strip()]
    todo_q = [s for s in questions if not (zh["q"].get(s) or "").strip()]
    print(f"[pm-translate] 标题 {len(titles)} (待翻 {len(todo_t)}) | "
          f"问题 {len(questions)} (待翻 {len(todo_q)})", flush=True)

    key = _secrets()["deepseek_api_key"]
    done_n = 0
    n_batches = 0

    def run(items, bucket):
        nonlocal done_n, n_batches
        for i in range(0, len(items), BATCH):
            batch = items[i:i + BATCH]
            res = translate_batch(batch, key)
            zh[bucket].update(res)
            done_n += len(res)
            n_batches += 1
            time.sleep(0.5)
            if args.limit and n_batches >= args.limit:
                print(f"[pm-translate] 达到 --limit {args.limit} 批, 提前结束", flush=True)
                return False
            if n_batches % 20 == 0:
                _save(zh)
                print(f"[pm-translate] 进度: 已翻 {done_n} 条 ({n_batches} 批)", flush=True)
        return True

    if run(todo_t, "t") is False:
        _save(zh)
        return
    if run(todo_q, "q") is False:
        _save(zh)
        return
    _save(zh)
    cov_t = len([1 for s in titles if (zh["t"].get(s) or "").strip()]) / max(1, len(titles))
    cov_q = len([1 for s in questions if (zh["q"].get(s) or "").strip()]) / max(1, len(questions))
    print(f"[pm-translate] 完成: 标题覆盖率 {cov_t:.1%} | 问题覆盖率 {cov_q:.1%} "
          f"| 新增 {done_n} 条", flush=True)


def _save(zh):
    tmp = ZH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(zh, f, ensure_ascii=False)
    os.replace(tmp, ZH_FILE)


if __name__ == "__main__":
    main()
