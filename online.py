# -*- coding: utf-8 -*-
"""
在线计算管道（对应需求2/3）
============================
需求2（纯在线）：调API实时拿 -> 直接计算 -> 统计 -> 出结果
需求3（大数据在线）：今日实时统计 + 离线层预先算好并存储的近29日历史统计
                    -> 百分位融合 -> 秒级出结果（无需重算历史）

流程：
  实时快照 -> 今日因子快照 -> 与库中29日历史分布融合 -> 量化评分
          -> 金融大模型语义门控 -> 风控否决 -> 最终决策 + 报告落盘
"""
import json
import os
import time

from config import CONFIG
import data_source as ds
import store
import factors
import llm_analyst
import risk


def run_online(cfg=CONFIG, conn=None, verbose: bool = True) -> dict:
    own = conn is None
    conn = conn or store.connect()
    t0 = time.time()
    report = {"ts": ds.now_cn().strftime("%Y-%m-%d %H:%M:%S"),
              "market_open": ds.market_open(), "stocks": []}

    # 一次批量请求拿全池实时行情（抗限流；失败时循环内逐票兜底）
    if verbose:
        print(f"[online] 开始运行，共 {len(cfg.universe)} 只；正在批量获取行情...", flush=True)
    try:
        rt_map = ds.fetch_realtime_batch(list(cfg.universe.keys()))
        if verbose:
            print(f"[online] 批量行情已返回 {len(rt_map)}/{len(cfg.universe)} 只", flush=True)
    except Exception as e:  # noqa: BLE001
        rt_map = {}
        if verbose:
            print(f"[online] 批量行情获取失败，将逐只重试：{e}", flush=True)

    for index, (secid, name) in enumerate(cfg.universe.items(), start=1):
        if verbose:
            print(f"[online] ({index}/{len(cfg.universe)}) 正在处理 {name}...", flush=True)
        try:
            hist_klines = store.get_klines(conn, secid, limit=cfg.stat_window + 25)
            if len(hist_klines) < factors.min_bars_needed():
                error = f"历史数据不足({len(hist_klines)}条)，先跑 offline"
                report["stocks"].append({"name": name, "error": error})
                if verbose:
                    print(f"[online] {name} 跳过：{error}", flush=True)
                continue
            if secid not in rt_map and verbose:
                print(f"[online] {name}：批量行情缺失，正在单独获取...", flush=True)
            rt = rt_map.get(secid) or ds.fetch_realtime(secid)
            snap = factors.snapshot_from_realtime(hist_klines, rt)
            hist_factors = store.get_factor_history(conn, secid, cfg.stat_window)
            fused = factors.fuse_score(snap, hist_factors)

            high20 = max(r["high"] for r in hist_klines[-20:])
            if verbose and cfg.llm.enabled:
                print(f"[online] {name}：正在请求分析模型（超时上限 {cfg.llm.timeout}s）...", flush=True)
            llm_out = llm_analyst.analyze(name, rt["code"], rt, snap, fused, cfg)
            decision = risk.gate(fused["score"], llm_out, high20, rt["price"],
                                 snap.get("amplitude"), cfg)

            report["stocks"].append({
                "name": name, "code": rt["code"], "secid": secid,
                "price": rt["price"], "change_pct": rt["change_pct"],
                "ts": rt.get("ts", ""),
                "quant_score": fused["score"],
                "factor_detail": fused["parts"],
                "missing_factors": fused["missing"],
                "llm": {k: v for k, v in llm_out.items() if k != "_amplitude"},
                "decision": {"action": decision.action, "weight": decision.weight,
                             "reasons": decision.reasons},
            })
            if verbose:
                print(f"[online] {name}({rt['code']}) 价 {rt['price']} 涨跌 {rt['change_pct']}% "
                      f"| 评分 {fused['score']:.3f} | {llm_out['engine']}:{llm_out['signal']}({llm_out['confidence']}) "
                      f"| 决策 {decision.action} 仓位{decision.weight*100:.1f}%"
                      + (f" | 模型回退原因: {llm_out['llm_error']}" if "llm_error" in llm_out else ""),
                      flush=True)
        except Exception as e:  # noqa: BLE001
            report["stocks"].append({"name": name, "secid": secid, "error": str(e)[:200]})
            if verbose:
                print(f"[online] {name} 失败: {e}", flush=True)

    report["elapsed_sec"] = round(time.time() - t0, 2)
    store.log_run(conn, "online", f"{len(report['stocks'])}只, {report['elapsed_sec']}s")
    if own:
        conn.close()

    os.makedirs(cfg_report_dir(), exist_ok=True)
    out = os.path.join(cfg_report_dir(),
                       f"online_{ds.now_cn().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    report["_report_path"] = out
    if verbose:
        print(f"[online] 全部处理完成，耗时 {report['elapsed_sec']} 秒；报告已保存：{out}", flush=True)
    return report


def cfg_report_dir() -> str:
    from config import REPORT_DIR
    return REPORT_DIR


if __name__ == "__main__":
    run_online()
