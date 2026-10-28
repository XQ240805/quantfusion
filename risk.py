# -*- coding: utf-8 -*-
"""
风控层 —— 拥有绝对否决权（借鉴 TradingAgents 的 CRO 熔断设计）
================================================================
量化评分与大模型意见都只是"建议"，风控一票否决后才产生最终决策。
规则：
  R1 单票仓位上限
  R2 大模型置信度不足 -> 降级为 WAIT
  R3 单日振幅超过阈值 -> 熔断 AVOID
  R4 距 20 日高点回撤超过阈值 -> 熔断 AVOID
"""
from dataclasses import dataclass, field


@dataclass
class Decision:
    action: str            # BUY / WAIT / AVOID
    weight: float          # 建议仓位（占总资金比例）
    reasons: list = field(default_factory=list)


def gate(quant_score: float, llm: dict, high20: float | None, price: float,
         amplitude: float | None, cfg) -> Decision:
    """quant_score: 融合评分 0~1；llm: llm_analyst 输出；
    high20: 20日最高价；amplitude: 今日振幅(未走完则为盘中振幅)。"""
    reasons = []
    risk_cfg = cfg.risk

    # R3 振幅熔断
    if amplitude is not None and amplitude >= risk_cfg.amplitude_breaker:
        return Decision("AVOID", 0.0,
                        [f"今日振幅 {amplitude*100:.1f}% ≥ {risk_cfg.amplitude_breaker*100:.0f}%，触发振幅熔断"])

    # R4 回撤熔断
    if high20 and price and high20 > 0:
        dd = 1 - price / high20
        if dd >= risk_cfg.drawdown_breaker:
            return Decision("AVOID", 0.0,
                            [f"距20日高点回撤 {dd*100:.1f}% ≥ {risk_cfg.drawdown_breaker*100:.0f}%，触发回撤熔断"])

    signal = llm.get("signal", "hold")
    conf = float(llm.get("confidence", 0.5))

    # R2 置信度不足降级
    if conf < risk_cfg.min_llm_confidence and signal == "buy":
        reasons.append(f"大模型置信度 {conf} < {risk_cfg.min_llm_confidence}，降级为 WAIT")
        signal = "hold"

    if signal == "avoid":
        return Decision("AVOID", 0.0, reasons + [f"分析层输出 avoid: {llm.get('rationale', '')}"])
    if signal == "hold":
        return Decision("WAIT", 0.0, reasons + [f"分析层输出 hold: {llm.get('rationale', '')}"])

    # R1 仓位上限：按评分线性缩放，但不超上限
    weight = round(min(risk_cfg.max_position_pct * quant_score / 0.9, risk_cfg.max_position_pct), 4)
    reasons.append(f"量化评分 {quant_score:.2f} + 分析层 buy(置信 {conf})，"
                   f"按上限 {risk_cfg.max_position_pct*100:.0f}% 线性给仓 {weight*100:.1f}%")
    return Decision("BUY", weight, reasons)
