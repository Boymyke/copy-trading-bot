#!/usr/bin/env python3
"""SONIC check: compare the 162 SONIC gold trades with real Exness 1-minute prices, describe how
they were entered/exited, and backtest simple rules that might reproduce them.
Reads /root/gold_bars.tar.gz (made by export-gold.sh). Read-only; prints a text report.
   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/<ref>/vps/sonic-check.py | python3 -
"""
import bisect, calendar, csv, io, statistics as st, sys, tarfile
from collections import defaultdict

BARS = sys.argv[1] if len(sys.argv) > 1 else "/root/gold_bars.tar.gz"
# side, open price, close price, close date (2026, month.day), close time (TMFinancials server time)
TRADES = """
s 4498.93 4494.99 06.01 11:28:55
s 4472.54 4466.94 06.01 16:41:39
s 4515.09 4509.94 06.02 16:30:32
s 4476.49 4471.85 06.03 07:50:55
s 4446.07 4440.55 06.03 18:46:02
s 4472.10 4475.03 06.04 09:03:13
s 4469.47 4464.16 06.04 10:13:26
s 4442.42 4439.91 06.05 07:19:28
s 4470.45 4468.09 06.05 15:07:21
b 4279.91 4282.96 06.08 08:42:07
b 4324.16 4332.41 06.08 18:22:58
s 4326.45 4326.34 06.09 10:40:24
s 4177.67 4175.58 06.10 08:06:00
b 4086.19 4094.38 06.11 09:57:10
s 4185.65 4187.95 06.12 08:27:33
b 4184.03 4190.00 06.12 17:29:50
s 4312.83 4310.22 06.15 08:45:22
b 4355.08 4365.25 06.15 16:41:37
s 4344.69 4346.29 06.16 13:22:06
b 4339.92 4343.29 06.16 15:11:57
s 4332.93 4325.91 06.17 08:58:07
s 4253.60 4245.90 06.18 18:17:36
s 4137.68 4132.54 06.19 08:41:30
b 4189.21 4196.13 06.22 08:39:30
b 4135.04 4139.12 06.23 06:48:57
s 4127.31 4122.67 06.23 21:32:06
b 4088.70 4085.42 06.24 10:15:20
b 4007.36 4009.83 06.24 16:46:25
b 3990.00 3994.32 06.25 07:30:21
b 4025.80 4034.67 06.25 20:27:18
s 4051.97 4047.32 06.26 14:49:42
b 4037.87 4046.64 06.29 15:08:30
b 3972.50 3977.79 06.30 07:25:58
s 4093.49 4084.76 07.01 17:03:41
b 4124.15 4120.96 07.02 16:15:41
b 4179.46 4181.51 07.03 06:35:00
s 4180.61 4175.71 07.03 08:22:23
s 4167.97 4162.75 07.06 07:23:13
s 4127.24 4122.10 07.07 08:33:52
s 4128.54 4131.18 07.07 10:43:52
s 4130.41 4126.80 07.07 11:57:37
s 4131.49 4124.75 07.08 08:25:20
s 4053.41 4047.59 07.08 14:34:03
s 4063.53 4059.49 07.09 07:43:07
b 4101.86 4108.30 07.09 10:37:26
b 4111.13 4116.62 07.10 09:08:48
s 4066.64 4058.35 07.13 08:29:25
s 4063.50 4066.94 07.13 15:49:24
s 4017.70 4019.29 07.14 07:55:55
b 4024.24 4031.42 07.14 09:08:09
b 4025.35 4032.65 07.15 08:41:16
b 4036.97 4038.43 07.15 15:24:38
s 4032.12 4029.38 07.16 08:57:20
s 4003.55 3998.12 07.17 10:48:29
b 4020.21 4025.69 07.20 11:19:04
s 4010.12 4006.31 07.20 17:59:42
b 4075.28 4080.12 07.21 09:54:41
s 4074.65 4072.23 07.21 19:44:14
s 4130.33 4128.13 07.22 08:02:01
b 4129.52 4130.76 07.22 23:03:30
b 4123.62 4121.62 07.23 08:07:30
s 4096.12 4093.06 07.23 13:30:45
s 4028.48 4024.09 07.24 09:00:10
s 4052.29 4048.41 07.24 11:26:33
b 4087.56 4093.24 07.27 08:11:22
b 4078.44 4087.83 07.27 16:45:17
s 4046.03 4041.86 07.28 08:28:37
b 4022.45 4025.40 07.29 07:53:59
b 4047.81 4051.78 07.29 23:08:41
b 4062.11 4067.49 07.30 12:36:57
b 4076.89 4081.75 07.31 08:24:09
s 4063.33 4057.73 08.03 08:16:22
s 4061.67 4058.91 08.04 08:17:10
s 4076.22 4080.12 08.04 21:59:40
b 4133.53 4137.67 08.05 08:20:38
s 4165.21 4161.88 08.05 11:52:17
b 4258.33 4259.53 08.06 08:04:52
b 4239.76 4245.46 08.06 20:55:52
b 4263.68 4268.52 08.07 08:17:20
b 4328.99 4336.55 08.10 07:22:20
s 4405.96 4409.44 08.11 07:59:58
s 4375.16 4368.36 08.11 12:01:18
s 4412.26 4408.62 08.12 15:07:11
b 4402.60 4404.94 08.13 08:15:27
s 4324.79 4318.65 08.14 08:19:37
b 4326.97 4333.77 08.14 10:10:43
s 4395.88 4393.35 08.17 08:04:50
s 4399.36 4392.34 08.18 08:14:35
s 4343.53 4339.43 08.19 08:14:58
s 4490.48 4485.73 08.20 08:27:21
b 4536.82 4537.94 08.21 08:15:14
b 4635.03 4641.85 08.24 08:07:05
b 4629.55 4635.58 08.25 08:10:16
b 4640.95 4639.96 08.25 20:35:19
b 4640.61 4643.24 08.26 08:09:09
s 4604.68 4601.92 08.27 11:30:12
b 4580.67 4583.52 08.28 08:25:37
s 4425.37 4419.78 08.31 08:09:37
s 4440.12 4438.12 09.01 09:05:54
b 4319.11 4320.49 09.02 11:50:28
b 4366.67 4369.27 09.02 18:25:56
s 4434.66 4438.31 09.03 08:52:15
s 4434.76 4427.47 09.03 12:57:10
s 4401.75 4383.98 09.04 15:48:05
b 4394.74 4399.23 09.07 10:05:53
s 4399.59 4395.21 09.08 20:29:05
b 4404.63 4412.51 09.09 16:15:54
s 4329.05 4323.24 09.10 23:02:43
b 4333.59 4328.72 09.11 08:02:21
b 4363.75 4370.49 09.11 18:30:18
s 4298.08 4292.18 09.15 08:58:55
b 4325.50 4332.34 09.16 10:00:07
b 4279.11 4284.41 09.17 06:21:35
s 4306.96 4308.30 09.17 09:22:53
s 4373.27 4366.64 09.17 16:29:43
b 4336.10 4344.46 09.18 06:33:14
s 4355.56 4351.66 09.21 11:56:26
b 4310.18 4313.76 09.22 10:37:01
s 4340.52 4336.37 09.22 15:55:48
s 4336.26 4332.89 09.23 09:56:42
s 4290.27 4287.13 09.24 08:34:28
s 4277.38 4267.57 09.24 11:24:43
b 4273.76 4278.27 09.25 10:38:59
s 4172.15 4165.37 09.28 10:04:52
s 4143.11 4145.66 09.29 12:44:07
s 4160.48 4158.78 09.29 14:32:16
s 4153.35 4148.05 09.29 20:05:18
b 4193.92 4194.56 09.30 10:18:11
b 4153.25 4159.92 09.30 18:49:34
b 4154.46 4159.80 10.01 12:23:18
b 4191.99 4194.09 10.02 16:35:12
b 4134.37 4142.92 10.05 09:04:23
b 4176.23 4173.82 10.06 22:18:42
b 4129.90 4133.22 10.07 10:26:34
s 4123.73 4118.59 10.08 08:58:25
s 4194.87 4196.66 10.09 09:55:40
s 4185.73 4183.08 10.09 13:57:48
s 4203.54 4196.52 10.12 07:59:13
b 4186.63 4191.26 10.13 07:21:09
s 4156.50 4147.79 10.14 09:04:12
s 4136.66 4133.71 10.15 09:09:15
s 4179.79 4175.28 10.16 18:26:07
b 4172.99 4174.44 10.19 09:46:05
s 4124.74 4126.83 10.20 12:22:15
b 4118.25 4123.43 10.21 10:29:35
s 4090.36 4084.08 10.22 11:10:52
b 4012.54 4019.66 10.23 10:34:24
b 4004.08 4006.67 10.26 10:07:42
s 4005.95 3995.59 10.27 11:26:58
b 4034.02 4040.43 10.28 10:35:04
s 3990.97 3979.89 10.30 13:43:01
s 3901.44 3898.24 11.02 11:53:13
s 3926.12 3914.38 11.03 08:55:39
s 4017.92 4021.74 11.04 09:12:38
b 4133.93 4140.82 11.05 06:38:50
b 4114.38 4129.41 11.09 04:24:39
s 4116.67 4107.84 11.09 19:10:40
s 4116.10 4114.36 11.10 06:54:15
s 4083.03 4075.51 11.10 12:57:23
s 4045.10 4047.83 11.11 05:45:53
s 4046.16 4041.01 11.11 15:08:05
b 3971.49 3977.29 11.12 07:39:04
"""
KNOWN_OPEN = {1: "06.01 11:07:05", 133: "10.06 22:11:42", 134: "10.07 10:19:55", 135: "10.08 08:47:20",
              136: "10.09 09:54:11", 137: "10.09 13:27:08", 138: "10.12 07:00:12", 139: "10.13 06:55:09",
              140: "10.14 08:52:58", 141: "10.15 09:05:21"}

def ep(s):
    d, t = s.split()
    mo, da = d.split(".")
    h, mi, se = t.split(":")
    return calendar.timegm((2026, int(mo), int(da), int(h), int(mi), int(se)))

def load(name):
    with tarfile.open(BARS) as tf:
        txt = tf.extractfile(name).read().decode("ascii", "replace")
    e, o, h, l, c, sp = [], [], [], [], [], []
    for r in csv.DictReader(io.StringIO(txt)):
        e.append(int(r["epoch"])); o.append(float(r["open"])); h.append(float(r["high"]))
        l.append(float(r["low"])); c.append(float(r["close"]))
        s = float(r["spread_points"]) * 0.001
        sp.append(s if 0 < s < 5 else 0.25)
    return e, o, h, l, c, sp

E1, O1, H1, L1, C1, SP1 = load("gold_M1.csv")
E5, O5, H5, L5, C5, SP5 = load("gold_M5.csv")
N1, N5 = len(E1), len(E5)
print(f"Loaded {N1} M1 bars and {N5} M5 bars. Typical Exness spread ${st.median(SP1):.2f}")

def idx1(t):
    """index of the M1 bar containing epoch t, or None if there is no bar within 3 minutes"""
    i = bisect.bisect_right(E1, t) - 1
    if i < 0 or t - E1[i] > 180: return None
    return i

def dist(price, i, w=1):
    lo = min(L1[max(0, i - w):i + w + 1]); hi = max(H1[max(0, i - w):i + w + 1]) + SP1[i]
    return 0.0 if lo <= price <= hi else min(abs(price - lo), abs(price - hi))

T = []
for n, line in enumerate([x for x in TRADES.strip().split("\n")], 1):
    s, o, c, d, t = line.split()
    T.append(dict(n=n, dir=1 if s == "b" else -1, o=float(o), c=float(c), tc=ep(d + " " + t), d=d, t=t,
                  to=ep(KNOWN_OPEN[n]) if n in KNOWN_OPEN else None))
for x in T: x["win"] = x["dir"] * (x["c"] - x["o"]) > 0

# ---------------------------------------------------------------- 1. clock offset + do the prices exist?
print("\n=== 1. Do SONIC's prices match real gold prices at those times? ===")
print("Median gap ($) between SONIC's CLOSE price and the Exness 1-minute bar, for each clock offset (hours):")
bymonth = defaultdict(list)
for x in T: bymonth[x["d"][:2]].append(x)
OFF = {}
for m in sorted(bymonth):
    row = []
    for off in (0, 1, 2, 3, 4):
        ds = []
        for x in bymonth[m]:
            i = idx1(x["tc"] - off * 3600)
            ds.append(dist(x["c"], i) if i is not None else 99.0)
        row.append((st.median(ds), off))
    OFF[m] = min(row)[1]
    print(f"  month {m}: " + "  ".join(f"off{o}={v:6.2f}" for v, o in row) + f"   -> using {OFF[m]}h")
# refine per ISO week for the DST change
byweek = defaultdict(list)
for x in T:
    y, mo, da = 2026, int(x["d"][:2]), int(x["d"][3:])
    byweek[calendar.timegm((y, mo, da, 0, 0, 0)) // (7 * 86400)].append(x)
for wk, xs in byweek.items():
    best = None
    for off in (OFF[xs[0]["d"][:2]], 2, 3):
        ds = [dist(x["c"], i) if (i := idx1(x["tc"] - off * 3600)) is not None else 99.0 for x in xs]
        v = st.median(ds)
        if best is None or v < best[0] - 0.3: best = (v, off)
    for x in xs: x["off"] = best[1]

for x in T:
    x["ic"] = idx1(x["tc"] - x["off"] * 3600)
    x["dc"] = dist(x["c"], x["ic"]) if x["ic"] is not None else None
have = [x for x in T if x["ic"] is not None]
dcs = [x["dc"] for x in have]
print(f"Trades with Exness data at the close time: {len(have)}/{len(T)}")
print(f"Close price inside the real bar (within $0.50): {sum(d <= 0.5 for d in dcs)}/{len(have)};"
      f" within $1.50: {sum(d <= 1.5 for d in dcs)}/{len(have)}; median gap ${st.median(dcs):.2f}; worst ${max(dcs):.2f}")

# ---------------------------------------------------------------- 2. entry time
TOL = 0.6
for x in have:
    x["known"] = x["to"] is not None
    ie = None
    if x["known"]:
        ie = idx1(x["to"] - x["off"] * 3600)
        x["do"] = dist(x["o"], ie) if ie is not None else None
    if ie is None:
        for j in range(x["ic"], max(0, x["ic"] - 240), -1):
            if L1[j] - TOL <= x["o"] <= H1[j] + SP1[j] + TOL:
                ie = j; break
    x["ie"] = ie
    # same inference for known ones, to see how good the guess is
    g = None
    for j in range(x["ic"], max(0, x["ic"] - 240), -1):
        if L1[j] - TOL <= x["o"] <= H1[j] + SP1[j] + TOL:
            g = j; break
    x["ig"] = g
print("\n=== 2. Entries ===")
kn = [x for x in have if x["known"] and x["ie"] is not None]
print("Trades with a known open time (from your screenshots): gap between SONIC's OPEN price and the real bar:")
for x in kn:
    print(f"  #{x['n']:3d} {x['d']} open gap ${x['do']:.2f}  close gap ${x['dc']:.2f}  real hold {(x['ic']-x['ie'])}min"
          f"  guessed hold {(x['ic']-x['ig']) if x['ig'] is not None else 'n/a'}min")
found = [x for x in have if x["ie"] is not None]
print(f"Open price found in real prices within the 4 hours before the close: {len(found)}/{len(have)}")
holds = [x["ic"] - x["ie"] for x in found]
print(f"Estimated hold (minutes): median {st.median(holds)}, 25%-75% {sorted(holds)[len(holds)//4]}-{sorted(holds)[3*len(holds)//4]}, max {max(holds)}")

# ---------------------------------------------------------------- indicators on M5
def ema(v, n):
    k = 2 / (n + 1); out = [v[0]]
    for a in v[1:]: out.append(out[-1] + k * (a - out[-1]))
    return out
def rsi(v, n=14):
    out = [50.0] * len(v); ag = al = 0.0
    for i in range(1, len(v)):
        ch = v[i] - v[i - 1]; g = max(ch, 0); lo = max(-ch, 0)
        if i <= n: ag += g / n; al += lo / n
        else: ag = (ag * (n - 1) + g) / n; al = (al * (n - 1) + lo) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out
EM20, EM50, EM200, RSI5 = ema(C5, 20), ema(C5, 50), ema(C5, 200), rsi(C5)
TR = [H5[0] - L5[0]] + [max(H5[i] - L5[i], abs(H5[i] - C5[i - 1]), abs(L5[i] - C5[i - 1])) for i in range(1, N5)]
ATR5 = ema(TR, 14)
def i5_before(t):
    """last M5 bar fully closed before epoch t"""
    return bisect.bisect_right(E5, t - 300) - 1

# ---------------------------------------------------------------- 3. what was the market doing at entry?
for x in found:
    ie, ic, d = x["ie"], x["ic"], x["dir"]
    seg_h, seg_l = H1[ie:ic + 1], L1[ie:ic + 1]
    x["mfe"] = (max(seg_h) - x["o"]) if d > 0 else (x["o"] - min(seg_l))
    x["mae"] = (x["o"] - min(seg_l)) if d > 0 else (max(seg_h) - x["o"])
    x["mae"] = max(x["mae"], 0.0); x["mfe"] = max(x["mfe"], 0.0)
    for k in (5, 15, 60, 240):
        x[f"m{k}"] = d * (C1[ie - 1] - C1[ie - 1 - k]) if ie - 1 - k >= 0 else 0.0
    hi, lo = max(H1[max(0, ie - 240):ie]), min(L1[max(0, ie - 240):ie])
    p = (x["o"] - lo) / (hi - lo) if hi > lo else 0.5
    x["rp"] = p if d > 0 else 1 - p
    j = i5_before(E1[ie])
    x["rsi"] = RSI5[j] if d > 0 else 100 - RSI5[j]
    x["trend"] = d * (EM50[j] - EM200[j]) > 0
    x["hr"] = (E1[ie] // 3600) % 24
    for k in (15, 60):
        x[f"p{k}"] = d * (C1[min(N1 - 1, ic + k)] - x["c"])
    x["gain"] = d * (x["c"] - x["o"])

def pct(xs, f): return f"{100 * sum(1 for x in xs if f(x)) / max(1, len(xs)):.0f}%"
def med(xs, k): return st.median(x[k] for x in xs) if xs else 0
W = [x for x in found if x["win"]]; L = [x for x in found if not x["win"]]
print("\n=== 3. What was gold doing when SONIC entered? (direction-adjusted: + means the market was already moving SONIC's way) ===")
for k in (5, 15, 60, 240):
    print(f"  move in the {k:3d} min before entry: with the trade in {pct(found, lambda x: x[f'm{k}'] > 0)} of trades, median ${med(found, f'm{k}'):+.2f}")
print(f"  entry position in the previous 4h range (0 = bought the low/sold the high, 1 = bought the high/sold the low): median {med(found,'rp'):.2f};"
      f" bottom fifth {pct(found, lambda x: x['rp'] < 0.2)}, top fifth {pct(found, lambda x: x['rp'] > 0.8)}")
print(f"  RSI(14, 5-min) in trade direction: median {med(found,'rsi'):.0f}; below 30: {pct(found, lambda x: x['rsi'] < 30)}; above 70: {pct(found, lambda x: x['rsi'] > 70)}")
print(f"  with the 5-min EMA50/200 trend: {pct(found, lambda x: x['trend'])}")
hrs = defaultdict(int)
for x in found: hrs[x["hr"]] += 1
print("  entry hour (UTC; Lagos = UTC+1): " + " ".join(f"{h:02d}h:{hrs[h]}" for h in sorted(hrs)))
print("\n=== 4. How good were the entries and exits? ===")
print(f"  winners: median gain ${med(W,'gain'):.2f}; worst point against them (MAE) median ${med(W,'mae'):.2f};"
      f" never more than $0.50 against: {pct(W, lambda x: x['mae'] <= 0.5)}, $1: {pct(W, lambda x: x['mae'] <= 1.0)}, over $3: {pct(W, lambda x: x['mae'] > 3)}; max ${max([x['mae'] for x in W] or [0]):.2f}")
print(f"  losers:  median loss ${-med(L,'gain'):.2f}; best point in their favour (MFE) median ${med(L,'mfe'):.2f}; MAE median ${med(L,'mae'):.2f}")
print(f"  winners closed within $1 of the best price reached: {pct(W, lambda x: x['mfe'] - x['gain'] <= 1.0)}")
print(f"  15 min after exit, price had gone further SONIC's way in {pct(found, lambda x: x['p15'] > 0)} of trades (median ${med(found,'p15'):+.2f}); 60 min: {pct(found, lambda x: x['p60'] > 0)} (median ${med(found,'p60'):+.2f})")

# ---------------------------------------------------------------- 5. backtests of simple rules
def sig_break(n):
    s = [0] * N5
    for i in range(n, N5):
        if C5[i] > max(H5[i - n:i]): s[i] = 1
        elif C5[i] < min(L5[i - n:i]): s[i] = -1
    return s
def sig_rsi(lo=30, hi=70):
    return [1 if RSI5[i] < lo else (-1 if RSI5[i] > hi else 0) for i in range(N5)]
def sig_pull():
    s = [0] * N5
    for i in range(200, N5):
        if EM50[i] > EM200[i] and L5[i] <= EM20[i] < C5[i] and C5[i] > O5[i]: s[i] = 1
        elif EM50[i] < EM200[i] and H5[i] >= EM20[i] > C5[i] and C5[i] < O5[i]: s[i] = -1
    return s
def sig_big(mult=1.5):
    return [0] + [(1 if C5[i] > O5[i] else -1) if abs(C5[i] - O5[i]) > mult * ATR5[i - 1] else 0 for i in range(1, N5)]
def neg(s): return [-a for a in s]
SIGS = [("break 1h high/low, follow", sig_break(12)), ("break 1h high/low, fade", neg(sig_break(12))),
        ("break 3h high/low, follow", sig_break(36)), ("break 3h high/low, fade", neg(sig_break(36))),
        ("break 6h high/low, follow", sig_break(72)), ("break 6h high/low, fade", neg(sig_break(72))),
        ("RSI<30 buy / >70 sell", sig_rsi()), ("RSI>70 buy / <30 sell", neg(sig_rsi())),
        ("trend pullback to EMA20", sig_pull()),
        ("big 5-min candle, follow", sig_big()), ("big 5-min candle, fade", neg(sig_big()))]

def sim(sig, tp, sl, h0, h1, maxhold=90, perday=2):
    out = []; free = 0; day = None; cnt = 0
    for i in range(250, N5 - 1):
        if sig[i] == 0: continue
        t = E5[i] + 300
        if t < free: continue
        hr = (t // 3600) % 24
        if not (h0 <= hr < h1): continue
        dd = t // 86400
        if dd != day: day, cnt = dd, 0
        if cnt >= perday: continue
        k = bisect.bisect_left(E1, t)
        if k >= N1 or E1[k] - t > 600: continue
        d = sig[i]; entry = O1[k] + SP1[k] if d > 0 else O1[k]
        pnl = None; j = k
        while j < N1 and E1[j] - E1[k] <= maxhold * 60:
            if d > 0:
                if L1[j] <= entry - sl: pnl = -sl; break
                if H1[j] >= entry + tp: pnl = tp; break
            else:
                if H1[j] + SP1[j] >= entry + sl: pnl = -sl; break
                if L1[j] + SP1[j] <= entry - tp: pnl = tp; break
            j += 1
        if pnl is None:
            j = min(j, N1 - 1)
            pnl = (C1[j] - entry) if d > 0 else (entry - C1[j] - SP1[j])
        out.append((t, d, pnl)); cnt += 1; free = E1[min(j, N1 - 1)] + 60
    return out

MID = calendar.timegm((2026, 8, 22, 0, 0, 0))
def stats(tr):
    if not tr: return "no trades"
    w = sum(1 for x in tr if x[2] > 0); gp = sum(x[2] for x in tr if x[2] > 0); gl = -sum(x[2] for x in tr if x[2] < 0)
    a = sum(x[2] for x in tr if x[0] < MID); b = sum(x[2] for x in tr if x[0] >= MID)
    return f"{len(tr):4d} trades  win {100*w/len(tr):3.0f}%  net ${gp-gl:+8.1f}/oz  PF {gp/gl if gl else 9.99:4.2f}  first half ${a:+7.1f}  second half ${b:+7.1f}"

ent = [(E1[x["ie"]], x["dir"]) for x in found]
def explained(sig):
    n = 0
    for te, d in ent:
        j = bisect.bisect_right(E5, te) - 1
        if any(0 <= q < N5 and sig[q] == d for q in range(j - 3, j + 1)): n += 1
    return n
print("\n=== 5. Backtests of simple rules on the same months (per 1 oz; SONIC-like exits: target $5.2, stop $2.7, max 90 min, max 2 trades/day) ===")
print("'explains' = how many of SONIC's entries had this signal in the same direction in the 20 minutes before")
rows = []
for name, s in SIGS:
    e = explained(s)
    per_day = sum(1 for a in s if a) / max(1, (E5[-1] - E5[0]) / 86400 * 5 / 7)
    for (tp, sl) in ((5.2, 2.7), (4.0, 4.0)):
        for (h0, h1, hl) in ((0, 24, "all day"), (4, 10, "04-10 UTC")):
            tr = sim(s, tp, sl, h0, h1)
            gp = sum(x[2] for x in tr if x[2] > 0); gl = -sum(x[2] for x in tr if x[2] < 0)
            rows.append((gp / gl if gl else 0, f"  {name:27s} tp{tp}/sl{sl} {hl:9s}: {stats(tr)}  explains {e}/{len(ent)} ({per_day:.0f} signals/day)"))
for _, r in sorted(rows, reverse=True): print(r)
sonic = [(E1[x["ie"]], x["dir"], x["gain"]) for x in found]
print(f"  {'SONIC as recorded':27s} {'':22s}: {stats(sonic)}")

# ---------------------------------------------------------------- 6. per-trade table
print("\n=== 6. Per-trade table ===")
print("n dir date close_time off closegap hold mae mfe gain m5 m15 m60 m240 rpos rsi trend hourUTC post15 post60")
for x in T:
    if x.get("ie") is None:
        print(f"{x['n']} {'B' if x['dir']>0 else 'S'} {x['d']} {x['t']} off{x.get('off','?')} no-price-match"); continue
    print(f"{x['n']} {'B' if x['dir']>0 else 'S'} {x['d']} {x['t'][:5]} {x['off']} {x['dc']:.2f} {x['ic']-x['ie']}{'k' if x['known'] else ''} "
          f"{x['mae']:.2f} {x['mfe']:.2f} {x['gain']:+.2f} {x['m5']:+.1f} {x['m15']:+.1f} {x['m60']:+.1f} {x['m240']:+.1f} {x['rp']:.2f} "
          f"{x['rsi']:.0f} {'T' if x['trend'] else 'C'} {x['hr']} {x['p15']:+.1f} {x['p60']:+.1f}")
print("\n=== END - copy everything from 'Loaded' to here and send it to Claude ===")
