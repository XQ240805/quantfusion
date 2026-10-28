# -*- coding: utf-8 -*-
"""
数据源模块 —— 东方财富公开行情 API（免费、无需鉴权）
====================================================
- fetch_kline(secid, days)     日 K 线历史（前复权）
- fetch_realtime(secid)        实时快照（最新价/昨收/涨跌幅）
- 盘中半根 K 线自动剔除（离线层只存收盘确认的数据，今日数据由在线层实时拿）
"""
import json
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CN_TZ = timezone(timedelta(hours=8))  # 北京时间


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def today_str() -> str:
    return now_cn().strftime("%Y-%m-%d")


def market_open() -> bool:
    """粗略判断 A 股是否处于交易时段（周一至周五 09:15-15:05）。"""
    n = now_cn()
    if n.weekday() >= 5:
        return False
    hm = n.hour * 100 + n.minute
    return 915 <= hm <= 1505


class DataSourceError(Exception):
    pass


def _get_json(url: str, retries: int = 4, timeout: int = 10) -> dict:
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (QuantFusion)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(min(1.0 * (2 ** i), 8.0))  # 1s -> 2s -> 4s -> 8s 指数退避
    raise DataSourceError(f"请求失败: {url[:80]}... -> {last_err}")


def _parse_ts(ts: str) -> str:
    """行情时间戳 -> 'YYYY-MM-DD HH:MM'（北京时间）。兼容 Unix 秒与东财压缩格式。"""
    if not ts:
        return ""
    if ts.isdigit() and len(ts) == 10:  # Unix 秒
        return datetime.fromtimestamp(int(ts), CN_TZ).strftime("%Y-%m-%d %H:%M")
    if len(ts) >= 12 and ts.isdigit():
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}"
    return ts


def fetch_kline(secid: str, days: int) -> list[dict]:
    """拉取日 K 线。返回 [{date, open, close, high, low, volume, amount}, ...] 升序。

    主通道：东方财富 push2his；限流/失败时兜底：腾讯 fqkline。
    盘中（15:00 前）最后一天为未走完的半根 K 线，自动剔除，避免污染离线统计。
    """
    try:
        rows, name = _kline_eastmoney(secid, days)
    except DataSourceError:
        rows, name = _kline_tencent(secid, days)
    # 剔除今日半根K线
    if rows and rows[-1]["date"] == today_str() and not _bar_closed():
        rows = rows[:-1]
    return rows, name


def _kline_eastmoney(secid: str, days: int):
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
        f"&klt=101&fqt=1&end=20500101&lmt={days + 10}"
    )
    data = _get_json(url).get("data") or {}
    name = data.get("name", "")
    rows = []
    for line in data.get("klines", []):
        p = line.split(",")
        # f51日期, f52开, f53收, f54高, f55低, f56量(手), f57额
        rows.append({
            "date": p[0], "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]),
            "volume": float(p[5]), "amount": float(p[6]),
        })
    return rows, name


def _kline_tencent(secid: str, days: int):
    sym = _tencent_symbol(secid)
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={sym},day,,,{days + 10},qfq")
    d = _get_json(url).get("data", {}).get(sym, {})
    name = d.get("qt", {}).get(sym, ["", ""])[1] if d.get("qt") else ""
    raw = d.get("qfqday") or d.get("day") or []
    rows = []
    for p in raw:
        # [date, open, close, high, low, volume(手), ...]（腾讯无成交额，置 None）
        rows.append({
            "date": p[0], "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]),
            "volume": float(p[5]), "amount": None,
        })
    return rows, name


def _bar_closed() -> bool:
    n = now_cn()
    return n.hour >= 15


def _tencent_symbol(secid: str) -> str:
    mkt, code = secid.split(".")
    return ("sh" if mkt == "1" else "sz") + code


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _tencent_realtime_batch(secids: list[str]) -> dict[str, dict]:
    """腾讯行情兜底通道（qt.gtimg.cn，GBK 编码，容忍度高）。"""
    url = "https://qt.gtimg.cn/q=" + ",".join(_tencent_symbol(s) for s in secids)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (QuantFusion)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        text = resp.read().decode("gbk", "ignore")
    out = {}
    for m in re.finditer(r'v_(?:sh|sz)(\d{6})="([^"]*)"', text):
        code, payload = m.group(1), m.group(2).split("~")
        if len(payload) < 35:
            continue
        price = _f(payload[3])
        prev = _f(payload[4])
        if price is None or price <= 0:
            continue  # 停牌/休市
        chg = _f(payload[32])
        if chg is None and prev:
            chg = round((price / prev - 1) * 100, 2)
        secid = ("1." if code.startswith(("6", "9")) else "0.") + code
        out[secid] = {
            "code": code, "name": payload[1],
            "price": price, "prev_close": prev, "change_pct": chg or 0,
            "high_today": _f(payload[33]), "low_today": _f(payload[34]),
            "open_today": _f(payload[5]),
            "volume_today": _f(payload[6]),        # 手
            "amount_today": (_f(payload[37]) or 0) * 1e4,  # 万元 -> 元
            "ts": payload[30][:12] if len(payload[30]) >= 12 else payload[30],
            "market_open_now": market_open(),
        }
    return out


def fetch_realtime_batch(secids: list[str]) -> dict[str, dict]:
    """批量实时快照。通道顺序：东财 ulist 批量 -> 腾讯批量（只补缺失的票）。

    返回 {secid: snapshot}。字段与 fetch_realtime 一致；拿不到的票不出现在结果里。
    """
    out = {}
    try:
        sec_list = ",".join(secids)
        url = (
            "https://push2.eastmoney.com/api/qt/ulist.np/get"
            f"?secids={sec_list}"
            "&fields=f2,f3,f5,f6,f12,f14,f15,f16,f17,f18,f124&fltt=2"
        )
        d = (_get_json(url).get("data") or {})
        for it in (d.get("diff") or []):
            secid = ("1." if str(it.get("f12", "")).startswith(("6", "9")) else "0.") + str(it.get("f12"))
            price = it.get("f2")
            if not isinstance(price, (int, float)):
                continue  # 休市/停牌
            out[secid] = {
                "code": it.get("f12"),
                "name": it.get("f14", ""),
                "price": price,
                "prev_close": it.get("f18"),
                "change_pct": it.get("f3", 0),
                "high_today": it.get("f15"),
                "low_today": it.get("f16"),
                "open_today": it.get("f17"),
                "volume_today": it.get("f5"),     # 手
                "amount_today": it.get("f6"),     # 元
                "ts": _parse_ts(str(it.get("f124", ""))),
                "market_open_now": market_open(),
            }
    except DataSourceError:
        pass  # 东财限流，整体交给腾讯兜底
    missing = [s for s in secids if s not in out]
    if missing:
        try:
            out.update(_tencent_realtime_batch(missing))
        except Exception as e:  # noqa: BLE001
            if not out:
                raise DataSourceError(f"东财与腾讯通道均失败: {e}")
    return out


def fetch_realtime(secid: str) -> dict:
    """实时快照（单票）。优先走批量端点，失败再退回单票端点。"""
    try:
        batch = fetch_realtime_batch([secid])
        if secid in batch:
            return batch[secid]
    except DataSourceError:
        pass  # 批量端点限流时退回单票端点
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f86,f170"
    )
    d = (_get_json(url).get("data") or {})
    if not d:
        raise DataSourceError(f"实时行情无数据: {secid}")
    price = d.get("f43")
    # 休市/停牌时 f43 可能返回 "-"
    if price is None or not isinstance(price, (int, float)):
        raise DataSourceError(f"实时行情不可用（可能休市/停牌）: {secid}")

    def _val(x, scale=100.0):
        return x / scale if isinstance(x, (int, float)) else None

    return {
        "code": d.get("f57", secid.split(".")[1]),
        "name": d.get("f58", ""),
        "price": price / 100.0,
        "prev_close": _val(d.get("f60")),
        "change_pct": (d.get("f170") or 0) / 100.0,
        "high_today": _val(d.get("f44")),
        "low_today": _val(d.get("f45")),
        "open_today": _val(d.get("f46")),
        "volume_today": _val(d.get("f47"), 1.0),   # 手
        "amount_today": _val(d.get("f48"), 1.0),   # 元
        "ts": _parse_ts(str(d.get("f86", ""))),
        "market_open_now": market_open(),
    }


if __name__ == "__main__":
    rows, name = fetch_kline("1.600519", 5)
    print(name, len(rows), rows[-1] if rows else None)
    print(fetch_realtime("1.600519"))
