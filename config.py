# -*- coding: utf-8 -*-
"""
QuantFusion 全局配置
====================
三层架构：
  L1 离线层（offline）  定时任务批量拉历史数据 -> 因子统计 -> 落库（对应需求1）
  L2 在线层（online）   实时API拿今日数据 + 历史统计融合 -> 快速评分（对应需求2/3）
  L3 大模型层（llm）    金融开源大模型语义门控分析（对应需求4）+ 风控否决
"""
import os
from dataclasses import dataclass, field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "quantfusion.db")
REPORT_DIR = os.path.join(BASE_DIR, "reports")


def _load_env(path: str) -> None:
    """极简 .env 加载器（不覆盖已有环境变量）。.env 不入库，见 .gitignore。"""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_env(os.path.join(BASE_DIR, ".env"))


@dataclass
class LLMConfig:
    """OpenAI 兼容协议配置 —— 可接火山方舟 / OpenRouter(Ling-3.0-flash-Fin) / DeepSeek 等。

    三个环境变量全部就绪时走真实大模型；任一缺失自动降级为规则分析师（同样输出结构）。
    """
    # 从 .env / 环境变量读取（三者齐备走真实大模型，否则规则兜底）
    base_url: str = field(default_factory=lambda: os.getenv("QF_LLM_BASE_URL", ""))
    api_key: str = field(default_factory=lambda: os.getenv("QF_LLM_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("QF_LLM_MODEL", ""))
    timeout: int = 60  # doubao-seed-2.1-pro 为思考模型，放宽超时
    temperature: float = 0.2

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass
class RiskConfig:
    max_position_pct: float = 0.20     # 单票最大仓位（占总资金）
    min_llm_confidence: float = 0.50   # LLM 置信度低于该值 -> 降级为 WAIT
    amplitude_breaker: float = 0.08    # 单日振幅超过 8% -> 熔断
    drawdown_breaker: float = 0.15     # 距 20 日高点回撤超过 15% -> 熔断
    commission_bps: float = 10.0       # 回测双边成本（bps/边）


@dataclass
class AppConfig:
    # 默认股票池：secid 规则 1.=沪 0.=深
    universe: dict = field(default_factory=lambda: {
        "1.600519": "贵州茅台",
        "1.601318": "中国平安",
        "0.000858": "五粮液",
        "0.300750": "宁德时代",
        "1.688981": "中芯国际",
    })
    history_days: int = 60        # 离线层拉取天数（>= 窗口 + 统计缓冲）
    stat_window: int = 29         # 历史统计窗口（需求3的"前29天"）
    forward_days: int = 5         # 回测持有期
    llm: LLMConfig = field(default_factory=LLMConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)


CONFIG = AppConfig()

# 量化评分阈值
SCORE_BULL = 0.70
SCORE_BEAR = 0.30
