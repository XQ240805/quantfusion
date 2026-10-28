# -*- coding: utf-8 -*-
"""
回测验证 —— 检验"离线因子 -> 评分 -> 信号"在历史上是否有效
============================================================
规则（无前视偏差）：
  T 日收盘产生信号（只用 <=T 数据）-> T+1 开盘买入 -> T+5 收盘卖出
  双边各扣 commission_bps；评分 >= BULL 才做多，<= BEAR 观望（不做空）
输出：交易次数 / 胜率 / 平均单笔收益 / 年化夏普 / 最大回撤 / 对比买入持有
"""
import os
from statistics import mean, stdev

from config import CONFIG, SCORE_BULL
import store
import factors
import data_source as ds


def _max_drawdown(cum: list[float]) -> float:
    peak, mdd = 1.0, 0.0
    for v in cum:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    return mdd


def run_backtest(cfg=CONFIG, conn=None, verbose: bool = True) -> dict:
    own = conn is None
    conn = conn or store.connect()
    fd = cfg.forward_days
    cost = cfg.risk.commission_bps / 10000.0
    result = {"forward_days": fd, "threshold": SCORE_BULL, "stocks": {}}

    for secid, name in cfg.universe.items():
        rows = store.get_klines(conn, secid)
        if len(rows) < factors.min_bars_needed() + fd + 1:
            result["stocks"][name] = {"error": "历史数据不足"}
            continue
        series = factors.compute_factor_series(rows)
        trades, equity = [], [1.0]

        for t in range(len(rows) - fd - 1):
            snap = {c: series[t].get(c) for c in factors.FACTOR_COLS}
            if any(v is None for v in snap.values()):
                continue
            hist = [series[k] for k in range(max(0, t - cfg.stat_window), t)
                    if all(series[k].get(c) is not None for c in factors.FACTOR_COLS)][-cfg.stat_window:]
            fused = factors.fuse_score(snap, hist)
            if fused["score"] >= SCORE_BULL:
                entry, exit_ = rows[t + 1]["open"], rows[t + fd]["close"]
                ret = exit_ / entry - 1 - 2 * cost
                trades.append(ret)
                equity.append(equity[-1] * (1 + ret))

        if trades:
            win = sum(1 for r in trades if r > 0) / len(trades)
            avg = mean(trades)
            sharpe = (avg / stdev(trades) * (252 / fd) ** 0.5) if len(trades) > 1 and stdev(trades) > 0 else 0.0
            bh = rows[-1]["close"] / rows[factors.min_bars_needed()]["close"] - 1
            result["stocks"][name] = {
                "trades": len(trades), "win_rate": round(win, 3),
                "avg_ret": round(avg, 4), "sharpe_ann": round(sharpe, 2),
                "max_drawdown": round(_max_drawdown(equity), 3),
                "buy_hold_ret": round(bh, 4),
            }
            if verbose:
                s = result["stocks"][name]
                print(f"[backtest] {name}: {s['trades']}笔 胜率{s['win_rate']*100:.0f}% "
                      f"均收益{s['avg_ret']*100:+.2f}% 夏普{s['sharpe_ann']} "
                      f"MDD {s['max_drawdown']*100:.1f}% | 买入持有 {s['buy_hold_ret']*100:+.2f}%")
        else:
            result["stocks"][name] = {"trades": 0, "note": "窗口内无达到阈值的信号"}

    if own:
        conn.close()
    _save_md(result, cfg)
    return result


def _save_md(result: dict, cfg):
    os.makedirs(cfg_report_dir(), exist_ok=True)
    path = os.path.join(cfg_report_dir(),
                        f"backtest_{ds.now_cn().strftime('%Y%m%d_%H%M%S')}.md")
    lines = [f"# QuantFusion 回测报告（{result['ts'] if 'ts' in result else ds.today_str()}）",
             f"- 持有期: T+{result['forward_days']}，信号阈值: {result['threshold']}",
             f"- 成本: 双边各 {cfg.risk.commission_bps} bps",
             "", "| 标的 | 信号数 | 胜率 | 平均收益 | 年化夏普 | 最大回撤 | 买入持有 |",
             "|---|---|---|---|---|---|---|"]
    for name, s in result["stocks"].items():
        if "error" in s:
            lines.append(f"| {name} | 数据不足 | - | - | - | - | - |")
        elif s.get("trades", 0) == 0:
            lines.append(f"| {name} | 0 | - | - | - | - | - |")
        else:
            lines.append(f"| {name} | {s['trades']} | {s['win_rate']*100:.0f}% | "
                         f"{s['avg_ret']*100:+.2f}% | {s['sharpe_ann']} | "
                         f"{s['max_drawdown']*100:.1f}% | {s['buy_hold_ret']*100:+.2f}% |")
    lines += ["", "> 样本仅约两个月日线数据，统计意义有限，仅用于验证管道正确性，不构成投资建议。"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    result["_report_path"] = path


def cfg_report_dir() -> str:
    from config import REPORT_DIR
    return REPORT_DIR


if __name__ == "__main__":
    run_backtest()
