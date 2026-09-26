#!/usr/bin/env python3
"""QMDJ 奇门遁甲时空排盘打分服务（影子验证用）
输入: Unix 时间戳(毫秒) → 输出 QMDJContext(干支历法+节气+局数+QMDJ_score)
简化版: 干支历法用 60 甲子循环, 节气按日期近似, QMDJ score 用确定性五行生克打分。
用途: 影子验证(不干预实际交易), 为后续权重融合提供客观数据。
用法: python3 qmdj_service.py <unix_ms>  或  --stdin 读 json
"""
import sys, json, time

TIAN_GAN = "甲乙丙丁戊己庚辛壬癸"
DI_ZHI = "子丑寅卯辰巳午未申酉戌亥"
SHENG_XIAO = "鼠牛虎兔龙蛇马羊猴鸡狗猪"
JIE_QI = ["小寒","大寒","立春","雨水","惊蛰","春分","清明","谷雨","立夏","小满","芒种","夏至",
          "小暑","大暑","立秋","处暑","白露","秋分","寒露","霜降","立冬","小雪","大雪","冬至"]

def year_ganzhi(year):
    return TIAN_GAN[(year-4)%10] + DI_ZHI[(year-4)%12]

def day_ganzhi(unix_ms):
    """日干支: 以 2000-01-01(甲子? 实际是戊午) 为锚, 用儒略日推算"""
    # 2000-01-01 日干支 = 戊午(索引: 干=4, 支=6)
    days = unix_ms // 86400000
    anchor_days = 10957  # 2000-01-01 的儒略日偏移(1970-01-01=0 到 2000-01-01)
    d = days - anchor_days
    gan = TIAN_GAN[(4 + d) % 10]
    zhi = DI_ZHI[(6 + d) % 12]
    return gan + zhi

def hour_zhi(unix_ms):
    """时支: 23-1=子, 1-3=丑, ..."""
    h = (unix_ms // 3600000) % 24
    idx = ((h + 1) // 2) % 12
    return DI_ZHI[idx]

def hour_ganzhi(day_gan, hour_zhi):
    """时干: 五鼠遁 (甲己日起甲子, 乙庚日起丙子...)"""
    day_gan_idx = TIAN_GAN.index(day_gan)
    hour_zhi_idx = DI_ZHI.index(hour_zhi)
    start = [0, 2, 4, 6, 8][day_gan_idx % 5]  # 甲己0 乙庚2 丙辛4 丁壬6 戊癸8
    gan = TIAN_GAN[(start + hour_zhi_idx) % 10]
    return gan + hour_zhi

def solar_term(unix_ms):
    """节气近似: 按月份日期(简化, 每月两个节气约 4-8 日和 19-23 日)"""
    lt = time.gmtime(unix_ms // 1000)
    m, d = lt.tm_mon, lt.tm_mday
    idx = (m - 1) * 2
    if d >= 19:
        idx += 1
    return JIE_QI[idx % 24]

def qmdj_score(day_gz, hour_gz):
    """QMDJ 综合时空置信度 0~1 (简化确定性打分)
    用日干五行 vs 时干五行生克: 生我/我生=吉(0.7-0.9), 克我=凶(0.2-0.4), 同=平(0.5)
    五行: 甲乙木 丙丁火 戊己土 庚辛金 壬癸水"""
    wuxing = {"甲":"木","乙":"木","丙":"火","丁":"火","戊":"土","己":"土",
              "庚":"金","辛":"金","壬":"水","癸":"水"}
    sheng = {"木":"火","火":"土","土":"金","金":"水","水":"木"}  # 我生
    ke = {"木":"土","土":"水","水":"火","火":"金","金":"木"}     # 我克
    dg = day_gz[0]; hg = hour_gz[0]
    dw, hw = wuxing[dg], wuxing[hg]
    if sheng[hw] == dw:      # 时生我 → 吉
        return 0.85
    if ke[hw] == dw:         # 时克我 → 凶
        return 0.25
    if sheng[dw] == hw:      # 我生时 → 泄, 平偏吉
        return 0.60
    if ke[dw] == hw:         # 我克时 → 平偏凶
        return 0.45
    return 0.55               # 同五行 → 平

def compute(unix_ms):
    year = time.gmtime(unix_ms // 1000).tm_year
    ygz = year_ganzhi(year)
    dgz = day_ganzhi(unix_ms)
    hz = hour_zhi(unix_ms)
    hgz = hour_ganzhi(dgz[0], hz)
    st = solar_term(unix_ms)
    score = qmdj_score(dgz, hgz)
    pattern = "吉" if score >= 0.7 else ("凶" if score <= 0.35 else "平")
    return {
        "timestamp": unix_ms,
        "ganzhi": {"year": ygz, "day": dgz, "hour": hgz},
        "solar_term": st,
        "qmdj_score": round(score, 2),
        "pattern_type": pattern,
    }

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--stdin":
        print(json.dumps(compute(int(sys.argv[1])), ensure_ascii=False))
    elif "--stdin" in sys.argv:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ts = d.get("timestamp") or d.get("ts") or 0
                print(json.dumps(compute(int(ts)), ensure_ascii=False))
            except Exception:
                pass
    else:
        print(json.dumps(compute(int(time.time()*1000)), ensure_ascii=False))
