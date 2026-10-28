# -*- coding: utf-8 -*-
"""
离线计算管道（对应需求1：定时任务/批量拉历史数据 -> 统计 -> 落库）
==================================================================
设计为幂等：每次运行全量拉取最近 N 天 + 全量重算因子 + upsert 落库，
定时任务重复执行不会产生脏数据。生产上挂 cron 即可（见 README）。
"""
from config import CONFIG
import data_source as ds
import store
import factors


def run_offline(cfg=CONFIG, conn=None, verbose: bool = True) -> dict:
    own = conn is None
    conn = conn or store.connect()
    summary = {"stocks": {}, "ts": ds.now_cn().strftime("%Y-%m-%d %H:%M:%S")}

    for secid, name in cfg.universe.items():
        try:
            rows, _ = ds.fetch_kline(secid, cfg.history_days)
        except ds.DataSourceError as e:
            summary["stocks"][name] = {"error": str(e)}
            continue
        if not rows:
            summary["stocks"][name] = {"error": "K线为空"}
            continue

        n_kline = store.upsert_klines(conn, secid, rows)
        factor_rows = [f for f in factors.compute_factor_series(rows)
                       if all(f.get(c) is not None for c in factors.FACTOR_COLS)]
        n_factor = store.upsert_factors(conn, secid, factor_rows)
        summary["stocks"][name] = {
            "secid": secid, "klines": n_kline, "factors": n_factor,
            "last_date": rows[-1]["date"],
        }
        if verbose:
            print(f"[offline] {name}({secid}) K线{n_kline}条 因子{n_factor}条 最新{rows[-1]['date']}")

    store.log_run(conn, "offline", str(summary["stocks"]))
    if own:
        conn.close()
    return summary


if __name__ == "__main__":
    run_offline()
