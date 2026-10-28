# -*- coding: utf-8 -*-
"""
因子引擎（纯函数，零依赖，可独立单测）
========================================
8 个日频因子（全部只用 T 日及之前数据，无前视偏差）：
  mom5/mom10   5/10 日动量
  vol20        20 日收益率标准差（低波动加分）
  vol_ratio    量比 = 当日成交量 / 前 20 日均量
  amplitude    日振幅 = (高-低)/昨收
  rsi14        Cutler RSI（简单均值法，确定性强可测）
  close_pos    收盘价在 20 日高低区间中的位置
  ma_bias      均线乖离 = (MA5-MA20)/MA20

核心融合逻辑（对应需求3）：
  在线层拿到今日快照后，与库中近 29 日历史因子分布做百分位对比，
  把"今天的实时统计 + 历史的统计结果"合成 0~1 综合评分。
"""
from statistics import mean, stdev

FACTOR_COLS = ["mom5", "mom10", "vol20", "vol_ratio", "amplitude", "rsi14", "close_pos", "ma_bias"]

# 评分权重（方向已编码：vol20 用 1-pct，即波动越低分越高）
SCORE_WEIGHTS = {
    "mom5": 0.30, "mom10": 0.20, "close_pos": 0.15, "vol_ratio": 0.15,
    "vol20_inv": 0.10, "ma_bias": 0.10,
}


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return mean(values[-n:])


def pct_rank(value: float | None, history: list[float]) -> float | None:
    """value 在 history 中的百分位（0~1）。history 为空返回 0.5 中性。"""
    if value is None:
        return None
    if not history:
        return 0.5
    return sum(1 for h in history if h < value) / len(history)


def rsi14(closes: list[float], i: int) -> float | None:
    """Cutler RSI：截至 i（含）的 14 周期。"""
    if i < 14:
        return None
    gains, losses = [], []
    for k in range(i - 13, i + 1):
        chg = closes[k] - closes[k - 1]
        gains.append(max(chg, 0))
        losses.append(max(-chg, 0))
    avg_gain, avg_loss = mean(gains), mean(losses)
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def compute_factor_series(rows: list[dict]) -> list[dict]:
    """对升序 K 线逐日计算因子。数据不足的日期返回 None 值，调用方过滤。

    rows: [{date, open, close, high, low, volume, amount}, ...]
    """
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    out = []
    for i, r in enumerate(rows):
        f = {"date": r["date"]}
        f["mom5"] = closes[i] / closes[i - 5] - 1 if i >= 5 else None
        f["mom10"] = closes[i] / closes[i - 10] - 1 if i >= 10 else None
        if i >= 20:
            rets = [closes[k] / closes[k - 1] - 1 for k in range(i - 19, i + 1)]
            f["vol20"] = stdev(rets) if len(rets) >= 2 else None
        else:
            f["vol20"] = None
        if i >= 21:
            f["vol_ratio"] = vols[i] / mean(vols[i - 20:i])
        else:
            f["vol_ratio"] = None
        f["amplitude"] = (r["high"] - r["low"]) / closes[i - 1] if i >= 1 and closes[i - 1] else None
        f["rsi14"] = rsi14(closes, i)
        if i >= 19:
            lo, hi = min(r["low"] for r in rows[i - 19:i + 1]), max(r["high"] for r in rows[i - 19:i + 1])
            f["close_pos"] = (closes[i] - lo) / (hi - lo) if hi > lo else 0.5
        else:
            f["close_pos"] = None
        if i >= 19:
            ma5, ma20 = sma(closes[: i + 1], 5), sma(closes[: i + 1], 20)
            f["ma_bias"] = (ma5 - ma20) / ma20 if ma20 else None
        else:
            f["ma_bias"] = None
        out.append(f)
    return out


def min_bars_needed() -> int:
    """完整因子所需的最早数据条数（vol20 需 21 根）。"""
    return 21


def snapshot_from_realtime(history_rows: list[dict], rt: dict) -> dict:
    """在线层专用：用今日实时行情拼出"今日因子快照"。

    history_rows: 库中已确认的历史K线（升序，不含今日）
    rt: fetch_realtime() 输出（含 price/prev_close + 可选盘中高低量）
    返回 {factor: value or None, ...}，None 表示盘中数据不足暂缺。
    """
    if not history_rows:
        raise ValueError("历史K线为空，无法构建今日快照（请先跑离线管道）")
    closes = [r["close"] for r in history_rows]
    vols = [r["volume"] for r in history_rows]
    highs = [r["high"] for r in history_rows]
    lows = [r["low"] for r in history_rows]
    price = rt["price"]
    snap: dict = {}

    snap["mom5"] = price / closes[-5] - 1 if len(closes) >= 5 else None
    snap["mom10"] = price / closes[-10] - 1 if len(closes) >= 10 else None

    rets = [closes[k] / closes[k - 1] - 1 for k in range(max(1, len(closes) - 19), len(closes))]
    snap["vol20"] = stdev(rets) if len(rets) >= 2 else None

    # 盘中量比：当日成交量按已交易时长折算全天预估
    if "volume_today" in rt and rt["volume_today"]:
        elapsed = _elapsed_fraction(rt)
        if elapsed and elapsed >= 0.05:
            est_full_vol = rt["volume_today"] / elapsed
            snap["vol_ratio"] = est_full_vol / mean(vols[-20:]) if len(vols) >= 20 else None
        else:
            snap["vol_ratio"] = None  # 开盘初数据太薄，弃用并后续降权
    else:
        snap["vol_ratio"] = None

    prev_close = rt.get("prev_close") or closes[-1]
    if rt.get("high_today") and rt.get("low_today"):
        snap["amplitude"] = (rt["high_today"] - rt["low_today"]) / prev_close
    else:
        snap["amplitude"] = None

    rsi_closes = closes + [price]
    snap["rsi14"] = rsi14(rsi_closes, len(rsi_closes) - 1)

    lo20, hi20 = min(lows[-20:]), max(highs[-20:])
    snap["close_pos"] = (price - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5

    ma5 = mean(closes[-4:] + [price]) if len(closes) >= 4 else None
    ma20 = mean(closes[-19:] + [price]) if len(closes) >= 19 else None
    snap["ma_bias"] = (ma5 - ma20) / ma20 if ma5 and ma20 else None
    return snap


def _elapsed_fraction(rt: dict) -> float | None:
    """根据实时时间戳估算今日已交易时长占比（用于盘中量比折算）。"""
    from data_source import now_cn, CN_TZ
    from datetime import datetime
    ts = rt.get("ts")
    if not ts or len(ts) < 16:
        return None
    try:
        t = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=CN_TZ)
    except ValueError:
        return None
    minutes = t.hour * 60 + t.minute
    # 上午 9:30-11:30 + 下午 13:00-15:00 = 240 分钟
    if minutes <= 11 * 60 + 30:
        traded = max(0, minutes - 570)
    else:
        traded = 120 + max(0, minutes - 780)
    return min(traded / 240.0, 1.0)


def fuse_score(snapshot: dict, hist: list[dict]) -> dict:
    """今日快照 vs 历史因子分布 -> 综合评分（需求3的核心融合）。

    hist: 近 29 日 factor_daily 记录（升序）
    返回 {score, parts, missing}
    """
    parts, missing = {}, []
    for key, w in SCORE_WEIGHTS.items():
        base = "vol20" if key == "vol20_inv" else key
        val = snapshot.get(base)
        hvals = [h[base] for h in hist if h.get(base) is not None]
        if val is None:
            missing.append(base)
            continue
        p = pct_rank(val, hvals)
        parts[key] = {"pct": round(p, 4), "weight": w, "value": round(val, 6) if val is not None else None,
                      "contrib": round((1 - p if key == "vol20_inv" else p) * w, 4)}
    total_w = sum(v["weight"] for v in parts.values())
    score = sum(v["contrib"] for v in parts.values()) / total_w if total_w else 0.5
    return {"score": round(score, 4), "parts": parts, "missing": missing}
