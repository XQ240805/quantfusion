# -*- coding: utf-8 -*-
"""
HTML 决策日报生成器
===================
把 reports/online_*.json（+ 最新回测报告）渲染成一张单文件 HTML 日报：
  - 顶部：日期 / 盘中状态 / BUY-WAIT-AVOID 计数 / 平均分
  - 决策矩阵：一眼扫完所有标的
  - 个股卡片：评分环 + 因子强弱条 + 因子雷达 + LLM 理由 + 风控原因
  - 回测摘要：T+5 胜率 / 收益 / 夏普 / 回撤
零依赖、内联 CSS/SVG，可直接双击打开或发给别人。
"""
import glob
import html
import json
import os
import re

from config import REPORT_DIR

# 因子中文名与格式：(显示名, 格式类型)
FACTOR_META = {
    "mom5": ("5日动量", "pct"),
    "mom10": ("10日动量", "pct"),
    "close_pos": ("20日区间位置", "ratio"),
    "vol20_inv": ("低波动（逆）", "pct"),
    "vol20": ("20日波动率", "pct"),
    "ma_bias": ("均线乖离", "pct"),
    "vol_ratio": ("量比", "num"),
    "rsi14": ("RSI14", "num1"),
    "amplitude": ("日内振幅", "pct"),
}

ACTION_STYLE = {
    "BUY": ("买入", "#c62828", "#fdeaea"),
    "WAIT": ("观望", "#b26a00", "#fff6e5"),
    "AVOID": ("回避", "#2e7d32", "#e8f5e9"),
}


def _fmt(key: str, val) -> str:
    if val is None:
        return "—"
    kind = FACTOR_META.get(key, ("", "num1"))[1]
    try:
        if kind == "pct":
            return f"{val * 100:+.2f}%"
        if kind == "ratio":
            return f"{val * 100:.0f}%"
        if kind == "num":
            return f"{val:.2f}"
        return f"{val:.1f}"
    except (TypeError, ValueError):
        return str(val)


def _color(favor: float) -> str:
    """多空友好度 -> 颜色（A 股习惯：偏多红、偏空绿）。"""
    if favor >= 0.66:
        return "#c62828"
    if favor >= 0.45:
        return "#8a8f98"
    return "#2e7d32"


def _score_ring(score: float) -> str:
    r, c = 34, 2 * 3.14159 * 34
    filled = max(0.0, min(1.0, score)) * c
    col = "#c62828" if score >= 0.7 else ("#b26a00" if score >= 0.45 else "#2e7d32")
    return f"""<svg class="ring" viewBox="0 0 84 84" width="84" height="84" aria-label="评分 {score:.2f}">
<circle cx="42" cy="42" r="34" fill="none" stroke="#eceef1" stroke-width="8"/>
<circle cx="42" cy="42" r="34" fill="none" stroke="{col}" stroke-width="8" stroke-linecap="round"
        stroke-dasharray="{filled:.1f} {c:.1f}" transform="rotate(-90 42 42)"/>
<text x="42" y="40" text-anchor="middle" font-size="19" font-weight="700" fill="{col}">{score:.2f}</text>
<text x="42" y="56" text-anchor="middle" font-size="9" fill="#8a8f98">融合评分</text></svg>"""


RADAR_LABEL = {"mom5": "5日动量", "mom10": "10日动量", "close_pos": "区间位置",
               "vol20_inv": "低波动", "ma_bias": "乖离"}


def _radar(parts: dict) -> str:
    """5 维因子雷达（用 贡献/权重 作为多空友好度）。"""
    keys = [k for k in ("mom5", "mom10", "close_pos", "vol20_inv", "ma_bias") if k in parts]
    if len(keys) < 3:
        return ""
    import math
    cx = cy = 66
    rmax = 40
    n = len(keys)
    rings, spokes = "", ""
    for frac in (0.33, 0.66, 1.0):
        pts = []
        for i in range(n):
            a = -math.pi / 2 + 2 * math.pi * i / n
            pts.append(f"{cx + rmax * frac * math.cos(a):.1f},{cy + rmax * frac * math.sin(a):.1f}")
        rings += f'<polygon points="{" ".join(pts)}" fill="none" stroke="#e8eaee" stroke-width="1"/>'
    labels, poly = "", []
    for i, k in enumerate(keys):
        a = -math.pi / 2 + 2 * math.pi * i / n
        x, y = cx + rmax * math.cos(a), cy + rmax * math.sin(a)
        spokes += f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#e8eaee"/>'
        p = parts[k]
        favor = (p["contrib"] / p["weight"]) if p["weight"] else 0.5
        rr = rmax * max(0.08, min(1.0, favor))
        poly.append(f"{cx + rr * math.cos(a):.1f},{cy + rr * math.sin(a):.1f}")
        lx, ly = cx + (rmax + 12) * math.cos(a), cy + (rmax + 12) * math.sin(a)
        labels += (f'<text x="{lx:.1f}" y="{ly + 3:.1f}" text-anchor="middle" font-size="9" '
                   f'fill="#8a8f98">{RADAR_LABEL[k]}</text>')
    return f"""<svg class="radar" viewBox="0 0 132 132" width="132" height="132">
{rings}{spokes}
<polygon points="{' '.join(poly)}" fill="rgba(198,40,40,.16)" stroke="#c62828" stroke-width="1.6"/>
{labels}</svg>"""


def _factor_bars(parts: dict, missing: list) -> str:
    rows = []
    for k, p in parts.items():
        favor = (p["contrib"] / p["weight"]) if p["weight"] else 0.5
        w = max(2, round(favor * 100))
        col = _color(favor)
        rows.append(f"""<div class="frow">
  <span class="fname">{FACTOR_META.get(k, (k,))[0]}<em>权重 {p['weight'] * 100:.0f}%</em></span>
  <span class="fbar"><i style="width:{w}%;background:{col}"></i></span>
  <span class="fval" style="color:{col}">{_fmt(k, p['value'])}</span>
  <span class="fpct">分位 {p['pct'] * 100:.0f}</span>
</div>""")
    if missing:
        names = "、".join(FACTOR_META.get(m, (m,))[0] for m in missing)
        rows.append(f'<div class="fmiss">⚠ 缺失因子：{html.escape(names)}（权重已重归一化）</div>')
    return "".join(rows)


def _stock_card(s: dict) -> str:
    name = html.escape(s.get("name", ""))
    code = html.escape(str(s.get("code", "")))
    price = s.get("price")
    chg = s.get("change_pct")
    up = (chg or 0) >= 0
    ccol = "#c62828" if up else "#2e7d32"
    score = s.get("quant_score") or 0
    d = s.get("decision") or {}
    action = d.get("action", "WAIT")
    label, acol, abg = ACTION_STYLE.get(action, ACTION_STYLE["WAIT"])
    weight = (d.get("weight") or 0) * 100
    llm = s.get("llm") or {}
    engine = "大模型" if llm.get("engine") == "llm" else "规则兜底"
    reasons = "；".join(d.get("reasons") or [])
    flags = "".join(f'<span class="chip warn">{html.escape(str(f))}</span>'
                    for f in (llm.get("risk_flags") or []))
    sig = llm.get("signal", "-")
    conf = llm.get("confidence")
    conf_txt = f"{conf * 100:.0f}%" if isinstance(conf, (int, float)) else "—"

    return f"""<article class="card">
  <div class="chead">
    <div class="cname">{name}<span class="ccode">{code}</span></div>
    <div class="cbadge" style="color:{acol};background:{abg}">{label}
      <b>{weight:.1f}%</b></div>
  </div>
  <div class="cprice" style="color:{ccol}">{price if price is not None else '—'}
    <span class="cpct">{'+' if up else ''}{chg if chg is not None else '—'}%</span>
    <span class="cts">{html.escape(str(s.get('ts', '')))}</span>
  </div>
  <div class="cbody">
    {_score_ring(score)}
    <div class="fbars">{_factor_bars(s.get('factor_detail') or {}, s.get('missing_factors') or [])}</div>
    {_radar(s.get('factor_detail') or {})}
  </div>
  <div class="llm">
    <div class="llmhead">
      <span class="tag">{engine}</span>
      <span class="sig">{html.escape(str(sig)).upper()}</span>
      <span class="conf">置信 {conf_txt}</span>
      <span class="model">{html.escape(str(llm.get('model', '')))}</span>
    </div>
    <p class="rationale">{html.escape(str(llm.get('rationale', '') or '—'))}</p>
    {f'<div class="chips">{flags}</div>' if flags else ''}
  </div>
  <div class="reason">风控判定：{html.escape(reasons or '—')}</div>
</article>"""


def _matrix(stocks: list) -> str:
    rows = []
    for s in stocks:
        d = s.get("decision") or {}
        action = d.get("action", "-")
        label, acol, abg = ACTION_STYLE.get(action, ("—", "#8a8f98", "#f2f3f5"))
        chg = s.get("change_pct") or 0
        ccol = "#c62828" if chg >= 0 else "#2e7d32"
        rows.append(f"""<tr>
  <td class="tl">{html.escape(s.get('name', ''))}</td>
  <td class="mono">{html.escape(str(s.get('code', '')))}</td>
  <td class="mono">{s.get('price', '—')}</td>
  <td class="mono" style="color:{ccol}">{'+' if chg >= 0 else ''}{chg}%</td>
  <td class="mono"><b>{(s.get('quant_score') or 0):.2f}</b></td>
  <td><span class="pill" style="color:{acol};background:{abg}">{label}</span></td>
  <td class="mono">{((d.get('weight') or 0) * 100):.1f}%</td>
</tr>""")
    return f"""<table class="matrix">
<thead><tr><th>标的</th><th>代码</th><th>最新价</th><th>涨跌</th><th>评分</th><th>决策</th><th>建议仓位</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>"""


def _backtest_section() -> str:
    files = sorted(glob.glob(os.path.join(REPORT_DIR, "backtest_*.md")))
    if not files:
        return ""
    try:
        with open(files[-1], encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 7 or cells[0] in ("标的",):
            continue
        ret = cells[3]
        col = "#c62828" if ret.startswith("+") else "#2e7d32"
        rows.append(f"""<tr><td class="tl">{html.escape(cells[0])}</td>
<td class="mono">{cells[1]}</td><td class="mono">{cells[2]}</td>
<td class="mono" style="color:{col}">{html.escape(ret)}</td>
<td class="mono">{cells[4]}</td><td class="mono">{cells[5]}</td>
<td class="mono">{cells[6]}</td></tr>""")
    if not rows:
        return ""
    head = ""
    for line in text.splitlines():
        if line.startswith("- 持有期"):
            head = html.escape(line.lstrip("- "))
    pct = re.findall(r"信号阈值:\s*([\d.]+)", text)
    return f"""<section class="bt">
  <h2>回测验证 <span class="hint">{head}{' · 阈值 ' + pct[0] if pct else ''}</span></h2>
  <table class="matrix">
  <thead><tr><th>标的</th><th>信号数</th><th>胜率</th><th>平均收益</th><th>夏普</th><th>最大回撤</th><th>买入持有</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table>
  <p class="note">⚠ 样本约两个月日线，统计意义有限，仅用于验证管道正确性，不构成投资建议。</p>
</section>"""


def render(report: dict, out_path: str | None = None) -> str:
    stocks = [s for s in report.get("stocks", []) if "error" not in s]
    errors = [s for s in report.get("stocks", []) if "error" in s]
    counts = {"BUY": 0, "WAIT": 0, "AVOID": 0}
    for s in stocks:
        counts[(s.get("decision") or {}).get("action", "WAIT")] = \
            counts.get((s.get("decision") or {}).get("action", "WAIT"), 0) + 1
    avg = sum(s.get("quant_score") or 0 for s in stocks) / len(stocks) if stocks else 0
    ts = report.get("ts", "")
    day = ts[:10] or ""
    mkt = "盘中" if report.get("market_open") else "收盘"
    elapsed = report.get("elapsed_sec", "-")

    stat_cards = "".join(
        f'<div class="stat"><span class="sl">{ACTION_STYLE[k][0]}</span>'
        f'<b style="color:{ACTION_STYLE[k][1]}">{counts.get(k, 0)}</b>'
        f'<span class="su">只</span></div>' for k in ("BUY", "WAIT", "AVOID"))
    err_html = ""
    if errors:
        items = "".join(f'<li>{html.escape(e.get("name", ""))}：{html.escape(e.get("error", ""))}</li>'
                        for e in errors)
        err_html = f'<div class="errbox">以下标的本次未取到数据：<ul>{items}</ul></div>'

    body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>QuantFusion 决策日报 · {html.escape(day)}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f6f7f9;color:#1f2328;
 font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px 20px 60px}}
header{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;
 flex-wrap:wrap;padding-bottom:16px;border-bottom:2px solid #1f2328}}
.brand{{font-size:22px;font-weight:800;letter-spacing:-.3px}}
.brand .sub{{font-size:13px;font-weight:500;color:#6b7280;margin-left:10px;letter-spacing:0}}
.meta{{font-size:12.5px;color:#6b7280;font-variant-numeric:tabular-nums}}
.meta b{{color:#1f2328}}
.stats{{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0 24px}}
.stat{{background:#fff;border:1px solid #e6e8ec;border-radius:12px;padding:14px 18px;
 min-width:104px;display:flex;align-items:baseline;gap:6px;box-shadow:0 1px 2px rgba(16,24,40,.04)}}
.stat .sl{{font-size:13px;color:#4b5563}}
.stat b{{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums}}
.stat .su{{font-size:12px;color:#9aa0a6}}
h2{{font-size:15px;margin:0 0 10px;font-weight:700}}
.hint{{font-size:12px;font-weight:400;color:#8a8f98;margin-left:8px}}
table.matrix{{width:100%;border-collapse:separate;border-spacing:0;background:#fff;
 border:1px solid #e6e8ec;border-radius:12px;overflow:hidden;box-shadow:0 1px 2px rgba(16,24,40,.04)}}
table.matrix th{{background:#fafbfc;font-size:12px;color:#6b7280;font-weight:600;
 text-align:right;padding:10px 14px;border-bottom:1px solid #e6e8ec}}
table.matrix td{{padding:11px 14px;border-bottom:1px solid #f0f1f3;text-align:right;font-size:13.5px}}
table.matrix tr:last-child td{{border-bottom:0}}
table.matrix th:first-child,table.matrix td.tl{{text-align:left;font-weight:600}}
.mono{{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}}
.pill{{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700}}
section{{margin-top:28px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(520px,1fr));gap:16px;margin-top:14px}}
.card{{background:#fff;border:1px solid #e6e8ec;border-radius:14px;padding:18px 20px 16px;
 box-shadow:0 1px 3px rgba(16,24,40,.05)}}
.chead{{display:flex;justify-content:space-between;align-items:center}}
.cname{{font-size:17px;font-weight:750}}
.ccode{{font-size:12px;color:#9aa0a6;font-weight:400;margin-left:8px}}
.cbadge{{font-size:12.5px;font-weight:700;padding:5px 12px;border-radius:999px;
 display:flex;align-items:center;gap:7px}}
.cbadge b{{font-size:15px;font-variant-numeric:tabular-nums}}
.cprice{{margin-top:6px;font-size:22px;font-weight:750;font-variant-numeric:tabular-nums;
 display:flex;align-items:baseline;gap:8px}}
.cpct{{font-size:14px;font-weight:600}}
.cts{{font-size:11.5px;color:#a8adb5;font-weight:400;margin-left:auto}}
.cbody{{display:flex;gap:16px;align-items:center;margin:14px 0 4px}}
.ring{{flex:0 0 auto}}
.fbars{{flex:1;min-width:0}}
.frow{{display:grid;grid-template-columns:104px 1fr 66px 60px;gap:8px;align-items:center;
 font-size:12px;padding:3px 0}}
.fname em{{display:block;font-style:normal;font-size:10.5px;color:#a8adb5}}
.fbar{{height:7px;background:#f0f1f3;border-radius:999px;overflow:hidden}}
.fbar i{{display:block;height:100%;border-radius:999px}}
.fval{{text-align:right;font-weight:700;font-variant-numeric:tabular-nums}}
.fpct{{text-align:right;color:#9aa0a6;font-size:11px;font-variant-numeric:tabular-nums}}
.fmiss{{font-size:11.5px;color:#b26a00;margin-top:6px}}
.radar{{flex:0 0 auto}}
.llm{{margin-top:12px;padding:12px 14px;background:#fafbfc;border:1px solid #eef0f3;border-radius:10px}}
.llmhead{{display:flex;align-items:center;gap:10px;font-size:12px;color:#6b7280}}
.tag{{background:#eef2ff;color:#3b4cca;padding:1px 8px;border-radius:6px;font-weight:700}}
.sig{{font-weight:800;color:#1f2328;letter-spacing:.4px}}
.model{{margin-left:auto;color:#a8adb5;font-size:11px}}
.rationale{{margin:7px 0 0;font-size:13px;line-height:1.7;color:#33383f}}
.chips{{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}}
.chip{{font-size:11.5px;padding:1px 8px;border-radius:999px}}
.chip.warn{{background:#fff4e5;color:#b26a00}}
.reason{{margin-top:10px;font-size:12.5px;color:#4b5563;padding-top:9px;border-top:1px dashed #e6e8ec}}
.note{{font-size:12px;color:#8a8f98;margin:10px 0 0}}
.errbox{{margin-top:16px;padding:12px 14px;background:#fff6e5;border:1px solid #ffe2b8;
 border-radius:10px;font-size:12.5px;color:#8a5a00}}
.errbox ul{{margin:6px 0 0;padding-left:18px}}
footer{{margin-top:36px;padding-top:14px;border-top:1px solid #e6e8ec;
 font-size:11.5px;color:#9aa0a6;line-height:1.8}}
@media(max-width:600px){{
 .cards{{grid-template-columns:1fr}}
 .cbody{{flex-wrap:wrap}}
 .frow{{grid-template-columns:90px 1fr 60px}}
 .fpct{{display:none}}
}}
</style></head><body><div class="wrap">
<header>
  <div class="brand">QuantFusion<span class="sub">量化融合决策日报</span></div>
  <div class="meta"><b>{html.escape(ts)}</b> · {mkt} · 覆盖 <b>{len(stocks)}</b> 只 · 耗时 <b>{elapsed}s</b> ·
    数据源 东方财富/腾讯 · 模型 doubao-seed-2.1-pro</div>
</header>

<div class="stats">
  {stat_cards}
  <div class="stat"><span class="sl">平均评分</span><b>{avg:.2f}</b></div>
</div>
{err_html}

<section>
  <h2>决策矩阵</h2>
  {_matrix(stocks)}
</section>

<section>
  <h2>个股明细 <span class="hint">评分 = 昨日因子分位融合 · 条形长度 = 该因子多空友好度</span></h2>
  <div class="cards">{''.join(_stock_card(s) for s in stocks)}</div>
</section>

{_backtest_section()}

<footer>
  生成时间 {html.escape(ts)} · QuantFusion 量化融合引擎（离线统计 + 在线融合 + 大模型语义门控 + 风控否决）<br/>
  评分口径：8 个技术因子在近 29 日历史分布中的分位，按权重（动量 30/20%、区间位置 15%、低波动 10%、均线乖离 10%）加权；
  BUY/WAIT/AVOID 由风控层最终裁定，仓位上限 20%。<br/>
  <b>免责声明：</b>本报告由程序基于公开行情数据自动生成，仅供研究学习，不构成投资建议。市场有风险，投资需谨慎。
</footer>
</div></body></html>"""

    out_path = out_path or os.path.join(
        REPORT_DIR, f"daily_{(day or 'latest').replace('-', '')}_{ts[11:16].replace(':', '')}.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)
    return out_path


def render_latest() -> str:
    """取最新一份 online_*.json 渲染 HTML。"""
    files = sorted(glob.glob(os.path.join(REPORT_DIR, "online_*.json")))
    if not files:
        raise FileNotFoundError("没有找到 online_*.json，先运行 python3 main.py online")
    with open(files[-1], encoding="utf-8") as f:
        report = json.load(f)
    return render(report)


if __name__ == "__main__":
    path = render_latest()
    print(f"日报已生成: {path}")
