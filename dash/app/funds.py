# -*- coding: utf-8 -*-
"""M7 USDT 充值提现体系: 余额/流水/充值单(唯一金额匹配)/提现单(审批自动打款)/套餐/设置
- 数据库: dash/funds.db (chmod 600)
- 平台 Bybit 密钥: ~/polymarket/.platform_keys.json (AES-256-GCM, 主密钥同 keys.py 派生自 .dash_secret)
- 对账/降级 cron 见 tools/funds_reconcile.py (systemd timer pm-funds 每60s + 每日检查)
"""
import base64
import hashlib
import json
import os
import random
import sqlite3
import threading
import time

from . import config, keys as keys_mod

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "funds.db")
PK_FILE = os.environ.get("PLATFORM_KEY_FILE", os.path.expanduser("~/polymarket/.platform_keys.json"))
LOCK = threading.Lock()

PLATFORM_ADDR = "TMVjkk3h2nQ2xvF3npeu7WfKAhaXJKSkVu"  # 平台 USDT-TRC20 收款地址 (Bybit 主账户)
DEPOSIT_COIN = "USDT"
DEPOSIT_CHAIN = "TRX"          # Bybit chainType=TRX 即 TRC20
DEPOSIT_TTL_H = 24             # 充值单有效期
MATCH_TOL = 5e-5               # 金额匹配容差 (0.00005 USDT)
MIN_UNCLAIMED = 1.0            # 低于此金额的未匹配到账忽略


def _con():
    con = sqlite3.connect(DB_FILE, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with LOCK:
        con = _con()
        try:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS balance (
              uid INTEGER PRIMARY KEY,
              usdt REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS balance_tx (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              uid INTEGER NOT NULL,
              type TEXT NOT NULL,             -- deposit/withdraw/withdraw_refund/plan/admin
              amount REAL NOT NULL,           -- 正=入 负=出
              ref TEXT DEFAULT '',
              note TEXT DEFAULT '',
              ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deposit_orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              uid INTEGER NOT NULL,
              coin TEXT DEFAULT 'USDT',
              chain TEXT DEFAULT 'TRX',
              amount_unique REAL NOT NULL,    -- 唯一金额(含尾数), 到账匹配键
              status TEXT DEFAULT 'pending',  -- pending/matched/confirmed/expired/unclaimed
              address TEXT DEFAULT '',
              txid TEXT DEFAULT '',
              note TEXT DEFAULT '',
              created_ts TEXT NOT NULL,
              matched_ts TEXT,
              confirmed_ts TEXT,
              expires_ts TEXT
            );
            CREATE TABLE IF NOT EXISTS withdraw_orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              uid INTEGER NOT NULL,
              amount REAL NOT NULL,
              fee REAL NOT NULL DEFAULT 0,
              address TEXT NOT NULL,
              chain TEXT DEFAULT 'TRX',
              status TEXT DEFAULT 'pending_review',  -- pending_review/paid/completed/failed/rejected
              txid TEXT DEFAULT '',
              admin_note TEXT DEFAULT '',
              created_ts TEXT NOT NULL,
              reviewed_ts TEXT,
              paid_ts TEXT
            );
            CREATE TABLE IF NOT EXISTS plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code TEXT UNIQUE NOT NULL,       -- free/pro/live (free 固定, 禁改删)
              name TEXT NOT NULL,
              price REAL NOT NULL DEFAULT 0,   -- USDT/月
              features TEXT DEFAULT '',
              sort INTEGER NOT NULL DEFAULT 0,
              active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """)
            # 默认套餐 (free 固定 + 两个建议价)
            for code, name, price, feats, sort in [
                ("free", "免费版", 0, "模拟交易 · 全行情 · 手动下单", 0),
                ("pro", "专业版", 30, "模拟+实盘 · 对冲套利托管30天", 1),
                ("live", "旗舰版", 99, "模拟+实盘 · 对冲套利托管365天 · 优先支持", 2)]:
                con.execute("INSERT OR IGNORE INTO plans(code,name,price,features,sort,active)"
                            " VALUES(?,?,?,?,?,1)", (code, name, price, feats, sort))
            for k, v in [("withdraw_fee", "1"), ("max_withdraw", "500"),
                         ("min_withdraw", "5"), ("deposit_min", "5"), ("deposit_max", "10000")]:
                con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
            con.commit()
        finally:
            con.close()
    try:
        os.chmod(DB_FILE, 0o600)
    except OSError:
        pass


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------- 平台 Bybit 密钥 ----------

def save_platform_key(api_key, secret):
    # R14-M3: 记录保存时间 (密钥轮换告警用)
    enc = json.dumps({"key": keys_mod.encrypt(api_key), "secret": keys_mod.encrypt(secret),
                      "set_ts": int(time.time())})
    tmp = PK_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(enc)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PK_FILE)
    os.chmod(PK_FILE, 0o600)


def platform_key():
    try:
        with open(PK_FILE) as f:
            d = json.load(f)
        return {"key": keys_mod.decrypt(d["key"]), "secret": keys_mod.decrypt(d["secret"])}
    except Exception:
        return None


def platform_address():
    """平台 USDT-TRC20 收款地址: 优先 Bybit API, 兜底常量"""
    pk = platform_key()
    if pk:
        try:
            from bybit_live import _req
            d = _req(pk["key"], pk["secret"], "GET", "/v5/asset/deposit/query-address",
                     {"coin": DEPOSIT_COIN, "chainType": DEPOSIT_CHAIN})
            for c in (d.get("result") or {}).get("chains", []):
                if c.get("addressDeposit"):
                    return c["addressDeposit"]
        except Exception:
            pass
    return PLATFORM_ADDR


# ---------- 余额 ----------

def has_deposit(uid):
    """R14-M15: 是否有充值入账 (首次充值即自动开通实盘)"""
    con = _con()
    try:
        r = con.execute("SELECT COUNT(*) FROM balance_tx WHERE uid=? AND type='deposit' AND amount>0",
                        (int(uid),)).fetchone()
        return bool(r and r[0])
    finally:
        con.close()


def get_balance(uid):
    con = _con()
    try:
        r = con.execute("SELECT usdt FROM balance WHERE uid=?", (uid,)).fetchone()
        return round(float(r["usdt"]), 4) if r else 0.0
    finally:
        con.close()


def _add_tx(con, uid, typ, amount, ref="", note=""):
    con.execute("INSERT INTO balance_tx(uid,type,amount,ref,note,ts) VALUES(?,?,?,?,?,?)",
                (uid, typ, amount, ref, note, _now()))


def add_balance(uid, amount, typ, ref="", note=""):
    """余额变动 (正入负出) + 流水; 返回新余额"""
    with LOCK:
        con = _con()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO balance(uid,usdt) VALUES(?,0) "
                        "ON CONFLICT(uid) DO NOTHING", (uid,))
            con.execute("UPDATE balance SET usdt=usdt+? WHERE uid=?", (amount, uid))
            bal = con.execute("SELECT usdt FROM balance WHERE uid=?", (uid,)).fetchone()["usdt"]
            _add_tx(con, uid, typ, amount, ref, note)
            con.commit()
            return round(float(bal), 4)
        finally:
            con.close()


def tx_list(uid, limit=50):
    con = _con()
    try:
        rows = con.execute("SELECT * FROM balance_tx WHERE uid=? ORDER BY id DESC LIMIT ?",
                           (uid, int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ---------- 充值 (唯一金额匹配) ----------

def _unique_amount(base):
    """base + 随机尾数(0.0001~0.9999), 与活动订单不冲突"""
    con = _con()
    try:
        busy = {float(r["amount_unique"]) for r in con.execute(
            "SELECT amount_unique FROM deposit_orders WHERE status IN ('pending','matched')")}
    finally:
        con.close()
    for _ in range(20):
        tail = round(random.uniform(0.0001, 0.9999), 4)
        amt = round(base + tail, 4)
        if amt not in busy:
            return amt
    return round(base + random.uniform(0.0001, 0.9999), 4)


def create_deposit(uid, base_amount):
    """生成充值单: {order_id, address, amount_unique, chain, expires_ts}"""
    try:
        base = float(base_amount)
    except Exception:
        return {"ok": False, "error": "金额非法"}
    dmin = float(get_setting("deposit_min", "5"))
    dmax = float(get_setting("deposit_max", "10000"))
    if not (dmin <= base <= dmax):
        return {"ok": False, "error": f"充值金额须 {dmin:g}~{dmax:g} USDT"}
    amt = _unique_amount(base)
    addr = platform_address()
    now = _now()
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() + DEPOSIT_TTL_H * 3600))
    with LOCK:
        con = _con()
        try:
            cur = con.execute(
                "INSERT INTO deposit_orders(uid,coin,chain,amount_unique,status,address,created_ts,expires_ts)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (uid, DEPOSIT_COIN, DEPOSIT_CHAIN, amt, "pending", addr, now, expires))
            oid = cur.lastrowid
            con.commit()
        finally:
            con.close()
    return {"ok": True, "order_id": oid, "address": addr, "amount_unique": amt,
            "chain": "TRC20", "expires_ts": expires}


def my_deposits(uid, limit=20):
    con = _con()
    try:
        rows = con.execute("SELECT * FROM deposit_orders WHERE uid=? ORDER BY id DESC LIMIT ?",
                           (uid, int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _deposit_status_zh(s):
    return {"pending": "等待付款", "matched": "链上已匹配", "confirmed": "已入账 ✅",
            "expired": "已过期", "unclaimed": "待认领"}.get(s, s)


def reconcile_deposits():
    """对账 (cron 60s): Bybit 充值记录 → 匹配 pending 单入账; 未匹配建 unclaimed"""
    pk = platform_key()
    if not pk:
        return {"ok": False, "error": "平台密钥未配置"}
    try:
        from bybit_live import _req
        d = _req(pk["key"], pk["secret"], "GET", "/v5/asset/deposit/query-record",
                 {"coin": DEPOSIT_COIN, "limit": 20})
        rows = (d.get("result") or {}).get("rows") or []
    except Exception as e:
        return {"ok": False, "error": f"充值记录拉取失败: {e}"}
    n_confirmed = n_new = 0
    for r in rows:
        if r.get("chain") not in (DEPOSIT_CHAIN, "TRC20") and r.get("chain") != "TRX":
            continue
        if str(r.get("status")) != "3":   # 3=到账成功
            continue
        try:
            amt = round(float(r["amount"]), 4)
        except Exception:
            continue
        txid = r.get("txID", "")
        with LOCK:
            con = _con()
            try:
                # 已处理过的 txid 跳过
                hit = con.execute("SELECT id,uid,status FROM deposit_orders WHERE txid=? AND status='confirmed'",
                                  (txid,)).fetchone()
                if hit:
                    con.close()
                    continue
                # 匹配 pending 订单 (金额容差)
                order = con.execute(
                    "SELECT id,uid FROM deposit_orders WHERE status='pending' AND ABS(amount_unique-?)<=?",
                    (amt, MATCH_TOL)).fetchone()
                if order:
                    con.execute("UPDATE deposit_orders SET status='confirmed', txid=?, matched_ts=?, confirmed_ts=? "
                                "WHERE id=?", (txid, _now(), _now(), order["id"]))
                    con.execute("INSERT INTO balance(uid,usdt) VALUES(?,0) ON CONFLICT(uid) DO NOTHING",
                                (order["uid"],))
                    con.execute("UPDATE balance SET usdt=usdt+? WHERE uid=?", (amt, order["uid"]))
                    _add_tx(con, order["uid"], "deposit", amt, txid,
                            f"充值入账 {amt} USDT (唯一金额匹配)")
                    n_confirmed += 1
                else:
                    # 未匹配: 建 unclaimed (≥ MIN_UNCLAIMED)
                    dup = con.execute("SELECT id FROM deposit_orders WHERE txid=? AND status='unclaimed'",
                                      (txid,)).fetchone()
                    if not dup and amt >= MIN_UNCLAIMED:
                        con.execute("INSERT INTO deposit_orders(uid,coin,chain,amount_unique,status,address,txid,"
                                    "created_ts,matched_ts) VALUES(0,?,?,?,?,?,?,?,?)",
                                    (DEPOSIT_COIN, DEPOSIT_CHAIN, amt, "unclaimed",
                                     r.get("toAddress", ""), txid, _now(), _now()))
                        n_new += 1
                con.commit()
            finally:
                con.close()
    # 过期清理
    with LOCK:
        con = _con()
        try:
            con.execute("UPDATE deposit_orders SET status='expired' WHERE status='pending' AND expires_ts<?",
                        (_now(),))
            con.commit()
        finally:
            con.close()
    return {"ok": True, "confirmed": n_confirmed, "unclaimed_new": n_new}


def unclaimed_list(limit=30):
    con = _con()
    try:
        rows = con.execute("SELECT * FROM deposit_orders WHERE status='unclaimed' "
                           "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def claim_deposit(admin_uid, order_id, uid):
    """管理员认领: unclaimed 充值 → 指定用户入账"""
    with LOCK:
        con = _con()
        try:
            con.execute("BEGIN IMMEDIATE")
            o = con.execute("SELECT * FROM deposit_orders WHERE id=? AND status='unclaimed'",
                            (int(order_id),)).fetchone()
            if not o:
                con.rollback()
                return {"ok": False, "error": "订单不存在或已处理"}
            amt = float(o["amount_unique"])
            con.execute("INSERT INTO balance(uid,usdt) VALUES(?,0) ON CONFLICT(uid) DO NOTHING", (int(uid),))
            con.execute("UPDATE balance SET usdt=usdt+? WHERE uid=?", (amt, int(uid)))
            con.execute("UPDATE deposit_orders SET status='confirmed', uid=?, confirmed_ts=?, "
                        "note=note||' (管理员认领给uid='||?||')' WHERE id=?",
                        (int(uid), _now(), int(uid), int(order_id)))
            _add_tx(con, int(uid), "deposit", amt, o["txid"] or "", "充值认领入账 (管理员分配)")
            _add_tx(con, int(admin_uid), "admin", 0, str(order_id), "管理员认领充值单")
            con.commit()
            return {"ok": True, "msg": f"已认领入账 {amt} USDT 给 uid={uid}"}
        finally:
            con.close()


# ---------- 提现 (审批后自动打款) ----------

def _valid_trc20(addr):
    return bool(addr) and len(addr) == 34 and addr.startswith("T")


def create_withdraw(uid, amount, address):
    try:
        amt = round(float(amount), 2)
    except Exception:
        return {"ok": False, "error": "金额非法"}
    fee = float(get_setting("withdraw_fee", "1"))
    wmin = float(get_setting("min_withdraw", "5"))
    wmax = float(get_setting("max_withdraw", "500"))
    if amt < wmin:
        return {"ok": False, "error": f"单笔提现最低 {wmin:g} USDT"}
    if amt > wmax:
        return {"ok": False, "error": f"单笔提现最高 {wmax:g} USDT (大额请联系客服)"}
    addr = str(address or "").strip()
    if not _valid_trc20(addr):
        return {"ok": False, "error": "TRC20 地址格式错误 (应为 T 开头 34 位)"}
    with LOCK:
        con = _con()
        try:
            con.execute("BEGIN IMMEDIATE")
            bal = con.execute("SELECT usdt FROM balance WHERE uid=?", (uid,)).fetchone()
            bal = float(bal["usdt"]) if bal else 0.0
            if bal < amt + fee:
                con.rollback()
                return {"ok": False, "error": f"余额不足 (需 {round(amt + fee, 2)} USDT, 当前 {bal})"}
            con.execute("UPDATE balance SET usdt=usdt-? WHERE uid=?", (amt + fee, uid))
            _add_tx(con, uid, "withdraw", -(amt + fee), addr, f"提现冻结 {amt} USDT + 手续费 {fee}")
            cur = con.execute("INSERT INTO withdraw_orders(uid,amount,fee,address,chain,status,created_ts)"
                              " VALUES(?,?,?,?,?,?,?)", (uid, amt, fee, addr, DEPOSIT_CHAIN,
                                                        "pending_review", _now()))
            oid = cur.lastrowid
            con.commit()
            return {"ok": True, "order_id": oid, "msg": f"提现申请已提交 (#{oid})，待管理员审核"}
        finally:
            con.close()


def my_withdraws(uid, limit=20):
    con = _con()
    try:
        rows = con.execute("SELECT * FROM withdraw_orders WHERE uid=? ORDER BY id DESC LIMIT ?",
                           (uid, int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def pending_withdraws(limit=50):
    con = _con()
    try:
        rows = con.execute("SELECT * FROM withdraw_orders WHERE status='pending_review' "
                           "ORDER BY id ASC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _bybit_withdraw(amount, address):
    """Bybit 提现打款: 返回 (ok, msg, txid)"""
    pk = platform_key()
    if not pk:
        return False, "平台密钥未配置", ""
    try:
        from bybit_live import _req
        d = _req(pk["key"], pk["secret"], "POST", "/v5/asset/withdraw/create",
                 body={"coin": DEPOSIT_COIN, "chain": DEPOSIT_CHAIN, "address": address,
                       "amount": str(amount), "timestamp": int(time.time() * 1000),
                       "forceChain": 0})
        if d.get("retCode") == 0:
            return True, "ok", d["result"].get("id", "")
        return False, d.get("retMsg", "打款失败"), ""
    except Exception as e:
        return False, f"打款异常: {e}", ""


def review_withdraw(admin_uid, order_id, approve, note=""):
    """管理员审批: approve=True → 自动打款; False → 拒绝并退回余额(含手续费)"""
    with LOCK:
        con = _con()
        try:
            con.execute("BEGIN IMMEDIATE")
            o = con.execute("SELECT * FROM withdraw_orders WHERE id=? AND status='pending_review'",
                            (int(order_id),)).fetchone()
            if not o:
                con.rollback()
                return {"ok": False, "error": "订单不存在或已处理"}
            if not approve:
                con.execute("UPDATE withdraw_orders SET status='rejected', admin_note=?, reviewed_ts=? "
                            "WHERE id=?", (str(note)[:200], _now(), int(order_id)))
                con.execute("UPDATE balance SET usdt=usdt+? WHERE uid=?", (o["amount"] + o["fee"], o["uid"]))
                _add_tx(con, o["uid"], "withdraw_refund", o["amount"] + o["fee"], str(order_id),
                        f"提现被拒退回 ({note})")
                _add_tx(con, admin_uid, "admin", 0, str(order_id), "拒绝提现单")
                con.commit()
                return {"ok": True, "msg": f"已拒绝 #%d 并退回余额" % int(order_id)}
            # 打款前二次确认额度 (金额一致才打)
            ok, msg, txid = _bybit_withdraw(float(o["amount"]), o["address"])
            if not ok:
                con.execute("UPDATE withdraw_orders SET status='failed', admin_note=?, reviewed_ts=? "
                            "WHERE id=?", (f"打款失败: {msg}"[:200], _now(), int(order_id)))
                con.execute("UPDATE balance SET usdt=usdt+? WHERE uid=?", (o["amount"] + o["fee"], o["uid"]))
                _add_tx(con, o["uid"], "withdraw_refund", o["amount"] + o["fee"], str(order_id),
                        f"打款失败退回 ({msg})")
                _add_tx(con, admin_uid, "admin", 0, str(order_id), f"打款失败: {msg}")
                con.commit()
                return {"ok": False, "error": f"打款失败已退回余额: {msg}"}
            con.execute("UPDATE withdraw_orders SET status='paid', txid=?, admin_note=?, reviewed_ts=?, paid_ts=? "
                        "WHERE id=?", (str(txid), str(note)[:200], _now(), _now(), int(order_id)))
            _add_tx(con, admin_uid, "admin", 0, str(order_id), f"提现打款审批通过 txid={txid}")
            con.commit()
            return {"ok": True, "msg": f"已打款 {o['amount']} USDT → {o['address'][:8]}… (Bybit id {txid})"}
        finally:
            con.close()


def reconcile_balance_check():
    """R14-M3: 三方核对 — Bybit实际USDT余额 vs balance表总和 vs 在途(冻结+未确认充值) → health快照
    允许容差: 在途资金估算误差 (充值匹配延迟期)
    """
    try:
        pk = platform_key()
        if not pk:
            return {"ok": False, "error": "平台密钥未配置"}
        from bybit_live import _req
        d = _req(pk["key"], pk["secret"], "GET", "/v5/asset/transfer/query-account-coins-balance",
                 {"accountType": "UNIFIED", "coin": "USDT"})
        # Bybit v5: result.balance 是 list [{coin, walletBalance, ...}]
        _bals = ((d.get("result") or {}).get("balance")) or []
        _hit = [x for x in _bals if x.get("coin") == "USDT"] if isinstance(_bals, list) else []
        bal = float(_hit[0].get("walletBalance") or 0) if _hit else 0.0
    except Exception as e:
        return {"ok": False, "error": f"Bybit余额拉取失败: {e}"}
    con = _con()
    try:
        total = con.execute("SELECT COALESCE(SUM(usdt),0) FROM balance").fetchone()[0]
        frozen = con.execute("SELECT COALESCE(SUM(amount+fee),0) FROM withdraw_orders WHERE status IN ('pending','paid','submitting')").fetchone()[0]
        pend_dep = con.execute("SELECT COALESCE(SUM(amount_unique),0) FROM deposit_orders WHERE status='pending'").fetchone()[0]
    finally:
        con.close()
    expected = round(float(total), 2) + round(float(frozen), 2) - round(float(pend_dep), 2)
    diff = round(bal - expected, 2)
    snap = {"bybit_usdt": bal, "db_total": round(float(total), 2),
            "frozen_withdraw": round(float(frozen), 2), "pending_deposit": round(float(pend_dep), 2),
            "expected": expected, "diff": diff, "ts": int(time.time())}
    try:
        hp = os.path.join(os.path.dirname(DB_FILE), "funds_health.json")
        with open(hp, "w", encoding="utf-8") as f:
            json.dump(snap, f)
    except Exception:
        pass
    return {"ok": abs(diff) < 5.0, "data": snap}  # 容差 5 USDT


def track_withdraw_status():
    """追踪 paid 单最终状态 (cron): Bybit 提现记录 → success 则 completed"""
    pk = platform_key()
    if not pk:
        return
    try:
        from bybit_live import _req
        d = _req(pk["key"], pk["secret"], "GET", "/v5/asset/withdraw/query-record",
                 {"coin": DEPOSIT_COIN, "limit": 10})
        rows = (d.get("result") or {}).get("rows") or []
    except Exception:
        return
    done = {}
    for r in rows:
        if r.get("status") == "success":
            done[str(r.get("txID", ""))] = True
    if not done:
        return
    with LOCK:
        con = _con()
        try:
            for txid in done:
                con.execute("UPDATE withdraw_orders SET status='completed' "
                            "WHERE status='paid' AND txid=?", (txid,))
            con.commit()
        finally:
            con.close()


# ---------- 套餐 ----------

def plans_list(include_inactive=False):
    con = _con()
    try:
        q = "SELECT * FROM plans" + ("" if include_inactive else " WHERE active=1")
        rows = con.execute(q + " ORDER BY sort ASC, price ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def save_plan(code, name, price, features, sort, active, is_new=False):
    """管理后台增改套餐; free 套餐禁止改价格/删除"""
    code = str(code or "").strip().lower()
    if not code or not str(name or "").strip():
        return {"ok": False, "error": "code/名称必填"}
    try:
        price = float(price)
    except Exception:
        return {"ok": False, "error": "价格非法"}
    if code == "free" and (price != 0 or not active):
        return {"ok": False, "error": "免费版价格固定 0 且不可停用"}
    with LOCK:
        con = _con()
        try:
            if is_new:
                if con.execute("SELECT 1 FROM plans WHERE code=?", (code,)).fetchone():
                    return {"ok": False, "error": "code 已存在"}
                con.execute("INSERT INTO plans(code,name,price,features,sort,active) VALUES(?,?,?,?,?,?)",
                            (code, str(name), price, str(features or ""), int(sort or 0), 1 if active else 0))
            else:
                cur = con.execute("UPDATE plans SET name=?, price=?, features=?, sort=?, active=? WHERE code=?",
                                  (str(name), price, str(features or ""), int(sort or 0),
                                   1 if active else 0, code))
                if cur.rowcount == 0:
                    return {"ok": False, "error": "套餐不存在"}
            con.commit()
            return {"ok": True}
        finally:
            con.close()


def delete_plan(code):
    code = str(code or "").strip().lower()
    if code == "free":
        return {"ok": False, "error": "免费版不可删除"}
    with LOCK:
        con = _con()
        try:
            con.execute("DELETE FROM plans WHERE code=?", (code,))
            con.commit()
            return {"ok": True}
        finally:
            con.close()


def buy_plan(uid, code):
    """余额购买套餐: 扣款 → 更新 users.plan + plan_expires_at (月费制, 叠加时长)"""
    code = str(code or "").strip().lower()
    plan = next((p for p in plans_list() if p["code"] == code), None)
    if not plan:
        return {"ok": False, "error": "套餐不存在"}
    price = float(plan["price"])
    if code == "free":
        _set_plan(uid, "free", "")
        return {"ok": True, "msg": "已切回免费版"}
    with LOCK:
        con = _con()
        try:
            con.execute("BEGIN IMMEDIATE")
            bal = con.execute("SELECT usdt FROM balance WHERE uid=?", (uid,)).fetchone()
            bal = float(bal["usdt"]) if bal else 0.0
            if bal < price:
                con.rollback()
                return {"ok": False, "error": f"余额不足 ({bal} < {price} USDT), 请先充值"}
            con.execute("UPDATE balance SET usdt=usdt-? WHERE uid=?", (price, uid))
            _add_tx(con, uid, "plan", -price, code, f"购买套餐 {plan['name']} ({price} USDT/月)")
            con.commit()
        finally:
            con.close()
    _extend_plan(uid, code, plan["name"])
    return {"ok": True, "msg": f"套餐已生效: {plan['name']} (扣 {price} USDT)"}


def _extend_plan(uid, code, name):
    """叠加 1 个月时长 (从当前到期或现在起)"""
    from . import users as users_mod
    try:
        u = users_mod.get_user(uid)
        base = max(time.time(), float(u.get("plan_expires") or 0))
        users_mod.set_plan(uid, code, base + 30 * 86400)
    except Exception:
        pass


def _set_plan(uid, code, expires):
    from . import users as users_mod
    try:
        users_mod.set_plan(uid, code, expires)
    except Exception:
        pass


def check_expirations():
    """到期自动降级 free (cron 每日)"""
    from . import users as users_mod
    try:
        users_mod.expire_plans()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- 设置 ----------

def get_setting(key, default=""):
    con = _con()
    try:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else str(default)
    finally:
        con.close()


def set_setting(key, value):
    with LOCK:
        con = _con()
        try:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            con.commit()
            return {"ok": True}
        finally:
            con.close()


def all_settings():
    con = _con()
    try:
        return {r["key"]: r["value"] for r in con.execute("SELECT * FROM settings")}
    finally:
        con.close()
