# -*- coding: utf-8 -*-
"""
金融大模型分析层（对应需求4）—— 语义门控式 Alpha 筛选
======================================================
借鉴 Alpha-R1（财跃星辰）与 Ling-3.0-flash-Fin（蚂蚁）的公开思路：
  量化层给出因子统计快照 -> 大模型结合"市场状态描述"做语义匹配 ->
  只放行逻辑吻合的信号，并解释"为什么此刻该这样"。

工程约束：
  - OpenAI 兼容协议，base_url / api_key / model 全部可配置（环境变量），
    可接火山方舟、OpenRouter（Ling-3.0-flash-Fin:free）、DeepSeek、GLM 等。
  - 无 Key 或调用/解析失败 -> 自动降级为"规则分析师"（确定性逻辑，输出结构一致），
    保证全链路永远可用。
  - 大模型输出必须通过 JSON 校验；非法输出直接降级，不污染下游。
"""
import json
import re
import urllib.request

from config import CONFIG

SYSTEM_PROMPT = (
    "你是一名严谨的金融量化分析师，职责是对量化因子快照做语义门控（semantic gating）："
    "结合当前市场状态，判断哪些因子逻辑此刻成立。你只输出 JSON，不输出任何其他文字。"
    "JSON 字段：signal(buy|hold|avoid), confidence(0~1), selected_factors(数组), "
    "rationale(不超过120字的中文理由), risk_flags(数组，如 高波动/量能异常/接近熔断)。"
    "注意：因子统计只是证据，你需评估逻辑一致性；不确定时保守输出 hold。"
)


def _user_prompt(name: str, code: str, rt: dict, snap: dict, fused: dict) -> str:
    parts = "\n".join(
        f"  - {k}: 今日值 {v['value']}，位于近29日 {v['pct']*100:.0f} 分位，权重 {v['weight']}"
        for k, v in fused["parts"].items()
    )
    missing = "、".join(fused["missing"]) if fused["missing"] else "无"
    return f"""【标的】{name}（{code}）
【实时行情】最新价 {rt['price']}，涨跌幅 {rt['change_pct']}%，时间 {rt.get('ts','')}
【量化融合评分】{fused['score']:.3f}（0~1，越高越偏多）
【今日因子快照 vs 近29日历史分布】
{parts}
【数据缺失项】{missing}
【任务】判断此刻是否值得做多该标的，给出 signal / confidence / selected_factors / rationale / risk_flags，只输出 JSON。"""


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _valid(d: dict) -> bool:
    return (isinstance(d, dict) and d.get("signal") in ("buy", "hold", "avoid")
            and isinstance(d.get("confidence"), (int, float)) and 0 <= d["confidence"] <= 1)


def _call_llm(prompt: str, cfg) -> dict:
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": cfg.model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
        "temperature": cfg.temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg.api_key}"},
    )
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json(content)
    if not _valid(parsed):
        raise ValueError(f"LLM 输出校验失败: {content[:120]}")
    parsed["engine"] = "llm"
    parsed["model"] = cfg.model
    return parsed


def _rule_analyst(name: str, rt: dict, snap: dict, fused: dict) -> dict:
    """规则兜底分析师：确定性语义门控，结构与 LLM 输出完全一致。"""
    p = {k: v["pct"] for k, v in fused["parts"].items()}
    risk_flags = []
    score = fused["score"]

    if snap.get("amplitude") is not None and snap["amplitude"] > 0.06:
        risk_flags.append("盘中振幅偏大")
    if "vol20" in p and p["vol20"] > 0.8:
        risk_flags.append("波动率处于近月高位")
    if snap.get("vol_ratio") is not None and snap["vol_ratio"] > 2.5:
        risk_flags.append("量能异常放大")

    if score >= 0.70 and "波动率处于近月高位" not in risk_flags:
        signal, conf = "buy", min(0.55 + (score - 0.70) * 1.5, 0.85)
    elif score <= 0.30:
        signal, conf = "avoid", min(0.55 + (0.30 - score) * 1.5, 0.85)
    else:
        signal, conf = "hold", 0.5
    if risk_flags:
        conf = round(conf * 0.9, 2)

    selected = [k for k in ("mom5", "mom10", "close_pos", "vol_ratio", "ma_bias")
                if k in p and p[k] >= 0.7]
    regime = "偏强" if score >= 0.70 else ("偏弱" if score <= 0.30 else "中性")
    rationale = (f"融合评分 {score:.2f}（{regime}），"
                 f"动量与区间位置{'共振走强' if selected and score >= 0.70 else '无明确共振'}"
                 + (f"，但需注意{'、'.join(risk_flags)}" if risk_flags else "，未见明显风险信号"))
    return {"signal": signal, "confidence": round(conf, 2),
            "selected_factors": selected, "rationale": rationale,
            "risk_flags": risk_flags, "engine": "rule", "model": "rule-fallback"}


def analyze(name: str, code: str, rt: dict, snap: dict, fused: dict, cfg=CONFIG) -> dict:
    """主入口：优先真实大模型，失败/未配置则规则兜底。永不抛异常。"""
    prompt = _user_prompt(name, code, rt, snap, fused)
    if cfg.llm.enabled:
        try:
            return _call_llm(prompt, cfg.llm)
        except Exception as e:  # noqa: BLE001
            result = _rule_analyst(name, rt, snap, fused)
            result["llm_error"] = str(e)[:200]
            return result
    return _rule_analyst(name, rt, snap, fused)
