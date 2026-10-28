# -*- coding: utf-8 -*-
"""
存储层 —— SQLite（对应需求3的"存储历史数据的结果"）
====================================================
三张表：
  kline_daily   原始日K线（离线层写入）
  factor_daily  每日因子值（离线层写入，在线层读取做百分位融合）
  run_log       管道运行审计日志
主键 (stock, date) upsert，天然幂等，定时任务重复跑不会写脏。
"""
import sqlite3
from contextlib import closing

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS kline_daily (
    stock   TEXT NOT NULL,
    date    TEXT NOT NULL,
    open REAL, close REAL, high REAL, low REAL,
    volume REAL, amount REAL,
    PRIMARY KEY (stock, date)
);
CREATE TABLE IF NOT EXISTS factor_daily (
    stock      TEXT NOT NULL,
    date       TEXT NOT NULL,
    mom5 REAL, mom10 REAL, vol20 REAL, vol_ratio REAL,
    amplitude REAL, rsi14 REAL, close_pos REAL, ma_bias REAL,
    PRIMARY KEY (stock, date)
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, stage TEXT, detail TEXT
);
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def upsert_klines(conn, stock: str, rows: list[dict]) -> int:
    with closing(conn.cursor()) as cur:
        cur.executemany(
            """INSERT INTO kline_daily(stock,date,open,close,high,low,volume,amount)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(stock,date) DO UPDATE SET
                 open=excluded.open, close=excluded.close, high=excluded.high,
                 low=excluded.low, volume=excluded.volume, amount=excluded.amount""",
            [(stock, r["date"], r["open"], r["close"], r["high"], r["low"],
              r["volume"], r["amount"]) for r in rows],
        )
    conn.commit()
    return len(rows)


def upsert_factors(conn, stock: str, rows: list[dict]) -> int:
    cols = ["mom5", "mom10", "vol20", "vol_ratio", "amplitude", "rsi14", "close_pos", "ma_bias"]
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
    sql = ("INSERT INTO factor_daily(stock,date," + ",".join(cols) + ") "
           "VALUES(" + ",".join(["?"] * 10) + ") "
           "ON CONFLICT(stock,date) DO UPDATE SET " + update_clause)
    with closing(conn.cursor()) as cur:
        cur.executemany(sql, [(stock, r["date"]) + tuple(r.get(c) for c in cols) for r in rows])
    conn.commit()
    return len(rows)


def get_klines(conn, stock: str, limit: int = 0) -> list[dict]:
    sql = "SELECT * FROM kline_daily WHERE stock=? ORDER BY date"
    if limit:
        sql += f" DESC LIMIT {int(limit)}"
        rows = conn.execute(sql, (stock,)).fetchall()[::-1]
    else:
        rows = conn.execute(sql, (stock,)).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM kline_daily LIMIT 0").description]
    return [dict(zip(cols, r)) for r in rows]


def get_factor_history(conn, stock: str, window: int) -> list[dict]:
    """取某只股票最近 window 条因子记录（升序），供在线层做百分位融合。"""
    cols = ["date", "mom5", "mom10", "vol20", "vol_ratio", "amplitude", "rsi14", "close_pos", "ma_bias"]
    rows = conn.execute(
        f"SELECT {','.join(cols)} FROM factor_daily WHERE stock=? ORDER BY date DESC LIMIT ?",
        (stock, window),
    ).fetchall()[::-1]
    return [dict(zip(cols, r)) for r in rows]


def last_factor_date(conn, stock: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) FROM factor_daily WHERE stock=?", (stock,)
    ).fetchone()
    return row[0] if row and row[0] else None


def log_run(conn, stage: str, detail: str):
    from data_source import now_cn
    conn.execute("INSERT INTO run_log(ts,stage,detail) VALUES(?,?,?)",
                 (now_cn().strftime("%Y-%m-%d %H:%M:%S"), stage, detail))
    conn.commit()
