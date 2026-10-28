# -*- coding: utf-8 -*-
"""
QuantFusion 命令行入口
=======================
  python3 main.py offline   # 离线管道：批量拉历史 -> 因子统计 -> 落库   (需求1)
  python3 main.py backtest  # 回测验证：历史信号 -> 前向收益 -> 绩效
  python3 main.py online    # 在线管道：实时快照 + 历史融合 + LLM + 风控 (需求2/3/4)
  python3 main.py report    # 用最新一份 online JSON 渲染 HTML 决策日报
  python3 main.py all       # 依次执行以上三步（首次部署用）
  python3 main.py test      # 单元测试
"""
import argparse
import json
import sys

from config import CONFIG


def main():
    p = argparse.ArgumentParser(description="QuantFusion 量化融合引擎")
    p.add_argument("cmd", choices=["offline", "online", "backtest", "report", "all", "test"])
    args = p.parse_args()

    if args.cmd == "test":
        import unittest
        sys.exit(not unittest.main(
            module="tests", argv=[sys.argv[0]], exit=False, verbosity=2
        ).result.wasSuccessful())

    if args.cmd in ("offline", "all"):
        import offline
        offline.run_offline(CONFIG)

    if args.cmd in ("backtest", "all"):
        import backtest
        backtest.run_backtest(CONFIG)

    if args.cmd == "report":
        import report_html
        path = report_html.render_latest()
        print(f"日报已生成: {path}")
        return

    if args.cmd in ("online", "all"):
        import online
        report = online.run_online(CONFIG)
        print("\n===== 在线决策摘要 =====")
        for s in report["stocks"]:
            if "error" in s:
                print(f"{s['name']}: 失败 - {s['error']}")
                continue
            d = s["decision"]
            print(f"{s['name']}({s['code']}): {d['action']} 仓位{d['weight']*100:.1f}% "
                  f"评分{s['quant_score']} [{s['llm']['engine']}] {'；'.join(d['reasons'])}")
        print(f"\n完整报告: {report.get('_report_path', '-')}")
        print(json.dumps({"耗时秒": report["elapsed_sec"], "盘中": report["market_open"]},
                         ensure_ascii=False))
        try:
            import report_html
            daily = report_html.render(report)
            print(f"HTML 日报: {daily}")
        except Exception as e:  # noqa: BLE001 日报失败不影响管道
            print(f"HTML 日报生成失败: {e}")


if __name__ == "__main__":
    main()
