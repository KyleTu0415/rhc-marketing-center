# -*- coding: utf-8 -*-
"""一次性运维脚本：按「综合评分」回填飞书线索表所有历史线索的「评级」列。

背景：评级口径统一为 _grade_from_score（>=80=A, >=60=B, 其余 C）。
历史数据存在"45分 A级 / 10分 A级"的撕裂（旧记录评级停留在初评）。
本脚本只重刷「评级」字段，不改综合评分、系统排除等任何其他列。

用法（在已配置飞书环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET/FEISHU_APP_TOKEN 的环境运行）：
    # 1) 先干跑，只打印将要修改的差异，不写库
    python scripts/backfill_lead_grades.py
    # 2) 核对无误后真正写库
    python scripts/backfill_lead_grades.py --apply

说明：生产凭证在 Railway，本地通常无凭证；部署后也可由管理员调用
POST /api/admin/leads/backfill-grades（body {} 干跑 / {"apply": true} 执行）达到同样效果。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def main():
    apply = "--apply" in sys.argv
    # 延迟导入，确保带上 backend 路径；缺飞书凭证时 import 仅告警不报错，真正取数才失败
    from app.main import backfill_lead_grades

    print(f"模式：{'写库(APPLY)' if apply else '干跑(DRY-RUN，不写库)'}")
    r = backfill_lead_grades(apply=apply)
    print(f"总记录 {r['total']} | 需修改 {r['to_change']} | 已一致 {r['unchanged']} | 错误 {r['errors']}")
    for c in r["changes"][:200]:
        print(f"  {c['score']:>3}分  {c['old_grade'] or '(空)'} -> {c['new_grade']}  {c['company']}")
    if len(r["changes"]) > 200:
        print(f"  …其余 {len(r['changes']) - 200} 条略")
    if not apply and r["to_change"]:
        print("\n干跑结束，确认无误后加 --apply 执行写库。")


if __name__ == "__main__":
    main()
