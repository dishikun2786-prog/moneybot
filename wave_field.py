#!/usr/bin/env python3
"""M-D7: FDTD 波动方程盘口能量场 (Python 原型, 对齐 engine-go/internal/fdtd)

在订单簿价格-深度网格上求解一维受迫阻尼波动方程:
    ∂²u/∂t² = c(x)² ∂²u/∂x² − γ ∂u/∂t + F(x,t)
显式三点差分 (与 Go kernel 完全同构):
    u[n+1,i] = (2·u[n,i] − a2·u[n-1,i] + r[i]·(u[n,i-1]−2u[n,i]+u[n,i+1]) + Δt²·F[i]) / a0
    a0 = 1 + γΔt/2,  a2 = 1 − γΔt/2,  r[i] = (c[i]·Δt/Δx)²

物理映射 (Aether-HFT 思路 → 确定性特征):
    u(x)   = 归一化累计买卖不平衡 (压力场)
    c(x)   = c0·√(1 + 局部深度/均值深度)  (流动性越深处波速越快)
    F(x,t) = 当前压力场 (持续驱动)
输出: 波能通量 E_n、突破前压强 P_dense、平均波速 c_mean。

铁律: 代码只算特征、模型只做判断、代码按门控执行。零外部依赖(仅 numpy)。
"""
import numpy as np


class WaveGrid:
    """一维 FDTD 网格 (三缓冲轮转, 向量化)。"""

    def __init__(self, n, dx=1.0, dt=0.01, gamma=0.5):
        self.n = int(n)
        self.dx = float(dx)
        self.dt = float(dt)
        self.gamma = float(gamma)
        self.c2 = np.zeros(self.n)
        self.u = np.zeros(self.n)
        self.u_prev = np.zeros(self.n)
        self.u_next = np.zeros(self.n)
        self.F = np.zeros(self.n)

    def set_wave_speed(self, c):
        c = np.asarray(c, dtype=np.float64)
        self.c2[:len(c)] = (c * self.dt / self.dx) ** 2

    def step(self):
        a0 = 1.0 + 0.5 * self.gamma * self.dt
        a2 = 1.0 - 0.5 * self.gamma * self.dt
        dt2 = self.dt * self.dt
        n = self.n
        un = self.u_next
        u = self.u
        up = self.u_prev
        c2 = self.c2
        F = self.F
        un[1:n-1] = (2*u[1:n-1] - a2*up[1:n-1]
                     + c2[1:n-1] * (u[0:n-2] - 2*u[1:n-1] + u[2:n])
                     + dt2*F[1:n-1]) / a0
        un[0] = un[1]      # 零通量(反射)边界
        un[n-1] = un[n-2]
        self.u, self.u_prev, self.u_next = un, u, up

    def energy_flux(self):
        """E_n = Σ(Δu/Δt)² (动能, 正比于冲击剧烈度)。"""
        return float(np.sum((self.u - self.u_prev) ** 2)) / (self.dt ** 2)

    def total_energy(self):
        """总能量(动能+势能), 用于物理正确性检验(阻尼应使其单调衰减)。"""
        return float(np.sum((self.u - self.u_prev) ** 2) +
                     np.sum((self.u[1:] - self.u[:-1]) ** 2))

    def dense_pressure(self):
        """P_dense = max(du/dt) (关键价前的能量压缩)。"""
        return float(np.max(self.u - self.u_prev)) / self.dt


def imbalance_profile(bids, asks, n_grid=64):
    """由盘口构建归一化不平衡场 u(x) 与深度场 depth(x)。
    bids: [(price,size),...] 降序; asks: [(price,size),...] 升序。
    网格: 以中间价为 0, 均匀铺 n_grid 个归一化坐标 x∈[-1,1]。
    u(x) = (累计买深 − 累计卖深) / 总深; depth(x) = (累计买深 + 累计卖深) / 总深。
    """
    bids = sorted((float(p), float(s)) for p, s in bids if float(s) > 0)[::-1]
    asks = sorted((float(p), float(s)) for p, s in asks if float(s) > 0)
    if len(bids) < 3 or len(asks) < 3:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2.0
    span = max(mid - bids[-1][0], asks[-1][0] - mid) or (mid * 0.01)
    prices = mid + np.linspace(-1.0, 1.0, n_grid) * span
    total_bid = sum(s for _, s in bids)
    total_ask = sum(s for _, s in asks)
    tot = (total_bid + total_ask) or 1.0
    u = np.zeros(n_grid)
    depth = np.zeros(n_grid)
    for i, p in enumerate(prices):
        cb = sum(s for bp, s in bids if bp >= p)
        ca = sum(s for ap, s in asks if ap <= p)
        u[i] = (cb - ca) / tot
        depth[i] = (cb + ca) / tot
    return prices, u, depth


def liquidity_speed(depth, c0=1.0):
    """c(x) = c0·√(1 + depth/mean_depth)。"""
    depth = np.asarray(depth, dtype=np.float64)
    mean_d = float(depth.mean()) or 1.0
    return c0 * np.sqrt(1.0 + depth / mean_d)


def features_from_book(bids, asks, n_grid=64, n_steps=20, dt=0.01, gamma=0.5, c0=1.0):
    """盘口 → 波场特征 {E_n, P_dense, c_mean}。CFL 破坏时自适应降 dt 保证稳定。"""
    prof = imbalance_profile(bids, asks, n_grid)
    if prof is None:
        return None
    _, u0, depth = prof
    g = WaveGrid(n_grid, dx=1.0 / (n_grid - 1), dt=dt, gamma=gamma)
    g.u[:] = u0
    g.u_prev[:] = u0
    g.F[:] = u0
    c = liquidity_speed(depth, c0)
    cmax = float(c.max())
    if cmax * g.dt / g.dx >= 1.0:
        g.dt = 0.5 * g.dx / cmax  # 自适应降 dt 保 CFL
    g.set_wave_speed(c)
    for _ in range(int(n_steps)):
        g.step()
    return {"E_n": round(g.energy_flux(), 6),
            "P_dense": round(g.dense_pressure(), 6),
            "c_mean": round(float(c.mean()), 4)}


if __name__ == "__main__":
    # 自测: 合成盘口 → 波场特征
    bids = [(100.0 - 0.1 * i, 5.0 + i) for i in range(30)]
    asks = [(100.1 + 0.1 * i, 5.0 + i) for i in range(30)]
    f = features_from_book(bids, asks)
    print("合成盘口波场特征:", f)

    # 物理正确性: 阻尼衰减 / 驱动注入
    g = WaveGrid(256, dx=1.0, dt=0.01, gamma=0.5)
    x = np.arange(256) - 128
    g.u[:] = 0.5 * np.exp(-x * x / 200.0)
    g.u_prev[:] = g.u
    g.set_wave_speed(np.full(256, 0.9))
    e0 = g.total_energy()
    for _ in range(500):
        g.step()
    e1 = g.total_energy()
    print(f"阻尼衰减: e0={e0:.6f} e1={e1:.6f} {'OK' if e1 < e0 else 'FAIL'}")

    g2 = WaveGrid(256, dx=1.0, dt=0.01, gamma=0.1)
    g2.F[:] = 0.1
    g2.set_wave_speed(np.full(256, 0.9))
    f0 = g2.total_energy()
    for _ in range(200):
        g2.step()
    f1 = g2.total_energy()
    print(f"驱动注入: f0={f0:.6f} f1={f1:.6f} {'OK' if f1 > f0 else 'FAIL'}")


