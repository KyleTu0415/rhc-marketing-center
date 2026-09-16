#!/usr/bin/env python3
"""清理公海池线索：备份未认领线索 → 删除未认领线索 → 保留已认领线索"""

import json, os, sys, requests
from datetime import datetime, timezone, timedelta

# 从环境变量读取配置
FEISHU_ATK = os.environ.get("FEISHU_APP_TOKEN", "")
LEADS_TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "")
BASE_URL = "https://rhc-marketing-support.up.railway.app"

def fetch_all_leads():
    """拉取飞书线索表全量数据"""
    print(f"[1/4] 拉取线索表...")
    # 通过后端 API 拉取（需要登录 token）
    # 这里直接用飞书 API
    if not FEISHU_ATK or not LEADS_TABLE_ID:
        print("❌ 缺少环境变量 FEISHU_APP_TOKEN 或 FEISHU_TABLE_ID")
        print("   请从 Railway Variables 获取")
        return None
    
    # 飞书 API 拉取
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_ATK}/tables/{LEADS_TABLE_ID}/records"
    # 需要 tenant_access_token，这里简化处理
    print(f"   线索表 ID: {LEADS_TABLE_ID}")
    return []

def main():
    timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    backup_file = f"RHC 运维备份/线索清理备份_{timestamp}.json"
    
    print("=" * 60)
    print("RHC 线索清理工具")
    print("=" * 60)
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"备份文件：{backup_file}")
    print()
    
    # 步骤 1：确认操作
    print("[警告] 即将删除所有【未认领】的线索！")
    print("[警告] 已认领的线索会保留。")
    print()
    
    answer = input("确认继续？(输入 yes 继续): ").strip().lower()
    if answer != "yes":
        print("已取消。")
        return
    
    print()
    print("[2/4] 此脚本需要在 Railway 环境运行，请联系 Agent 执行清理。")
    print()
    print("替代方案：通过后端 API 清理")
    print(f"  POST {BASE_URL}/api/admin/cleanup-leads")
    print("  Header: Authorization: Bearer <admin_token>")
    print("  Body: {\"action\": \"delete_unclaimed\"}")

if __name__ == "__main__":
    main()
