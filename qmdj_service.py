#!/usr/bin/env python3
"""QMDJ 奇门遁甲时空排盘打分服务（影子验证用，精确到分钟/秒）
时间层次:
  时柱(2小时) → 定局(日干×时支生克)
  刻(15分钟, 8刻/时辰) → 八门旺相微调
  分/秒 → 连续微调(让同一时辰内每笔交易有独特 score)
输入: Unix 时间戳(毫秒) → 输出 QMDJContext(干支+节气+刻+分钟秒+QMDJ_score)
用法: python3 qmdj_service.py <unix_ms> 或 --stdin
"""
import sys, json, time, math

TIAN_GAN = "甲乙丙丁戊己庚辛壬癸"
DI_ZHI = "子丑寅卯辰巳午未申酉戌亥"
JIE_QI = ["小寒","大寒","立春","雨水","惊蛰","春分","清明","谷雨","立夏","小满","芒种","夏至",
          "小暑","大暑","立秋","处暑","白露","秋分","寒露","霜降","立冬","小雪","大雪","冬至"]
# 八门(休生伤杜景死惊开) 每刻旺相权重: 吉门(休生开)+, 凶门(死惊伤)-, 平门(杜景)~
BA_MEN_WEIGHT = [0.04, 0.06, -0.04, -0.02, 0.01, -0.06, -0.05, 0.05]  # 休生伤杜景死惊开

def year_ganzhi(year):
    return TIAN_GAN[(year-4)%10] + DI_ZHI[(year-4)%12]

def day_ganzhi(unix_ms):
    days = unix_ms // 86400000
    anchor_days = 10957
    d = days - anchor_days
    return TIAN_GAN[(4+d)%10] + DI_ZHI[(6+d)%12]

def hour_zhi(unix_ms):
    h = (unix_ms // 3600000) % 24
    return DI_ZHI[((h+1)//2)%12]

def hour_ganzhi(day_gan, hour_zhi):
    day_gan_idx = TIAN_GAN.index(day_gan)
    hour_zhi_idx = DI_ZHI.index(hour_zhi)
    start = [0,2,4,6,8][day_gan_idx%5]
    return TIAN_GAN[(start+hour_zhi_idx)%10] + hour_zhi

def solar_term(unix_ms):
    lt = time.gmtime(unix_ms//1000)
    m, d = lt.tm_mon, lt.tm_mday
    idx = (m-1)*2
    if d >= 19:
        idx += 1
    return JIE_QI[idx%24]

def wuxing_of(gan):
    return {"甲":"木","乙":"木","丙":"火","丁":"火","戊":"土","己":"土",
            "庚":"金","辛":"金","壬":"水","癸":"水"}[gan]

def base_score(day_gz, hour_gz):
    """时柱基础分: 日干 vs 时干五行生克"""
    sheng = {"木":"火","火":"土","土":"金","金":"水","水":"木"}
    ke = {"木":"土","土":"水","水":"火","火":"金","金":"木"}
    dw, hw = wuxing_of(day_gz[0]), wuxing_of(hour_gz[0])
    if sheng[hw] == dw:   # 时生我 → 吉
        return 0.85
    if ke[hw] == dw:      # 时克我 → 凶
        return 0.25
    if sheng[dw] == hw:   # 我生时 → 泄
        return 0.60
    if ke[dw] == hw:      # 我克时
        return 0.45
    return 0.55

def compute(unix_ms):
    lt = time.gmtime(unix_ms//1000)
    year = lt.tm_year
    minute = lt.tm_min
    second = lt.tm_sec
    ygz = year_ganzhi(year)
    dgz = day_ganzhi(unix_ms)
    hz = hour_zhi(unix_ms)
    hgz = hour_ganzhi(dgz[0], hz)
    st = solar_term(unix_ms)
    # 刻: 每 15 分钟一刻(0-7), 对应八门旺相
    ke = minute // 15
    ke_weight = BA_MEN_WEIGHT[ke]
    # 分/秒连续微调: 用正弦做 0~1 连续因子, ±0.03
    frac = (minute*60 + second) / 7200.0  # 0~1 (一个时辰内)
    fine = 0.03 * math.sin(frac * 2 * math.pi)
    # 最终 score
    base = base_score(dgz, hgz)
    score = max(0.0, min(1.0, base + ke_weight + fine))
    pattern = "吉" if score >= 0.7 else ("凶" if score <= 0.35 else "平")
    return {
        "timestamp": unix_ms,
        "time": f"{year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d} {lt.tm_hour:02d}:{minute:02d}:{second:02d} UTC",
        "ganzhi": {"year": ygz, "day": dgz, "hour": hgz},
        "solar_term": st,
        "ke": ke,  # 刻(0-7)
        "minute": minute, "second": second,
        "qmdj_score": round(score, 4),
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
