#!/usr/bin/env python3
"""临时清理接口：备份全量线索 → 删除所有线索"""

import json, os, time
from datetime import datetime, timezone, timedelta

# 从 main.py 导入飞书 API 函数
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def cleanup_all_leads():
    """备份并删除所有线索"""
    from backend.app.main import _feishu_api, _ensure_leads_table, _invalidate_leads_cache
    
    timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    backup_file = f"RHC 运维备份/线索全量备份_{timestamp}.json"
    
    print("=" * 60)
    print("RHC 线索全量清理")
    print("=" * 60)
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"备份文件：{backup_file}")
    print()
    
    # 步骤 1：确保线索表存在
    print("[1/4] 获取线索表 ID...")
    tid = _ensure_leads_table()
    print(f"   线索表 ID: {tid}")
    print()
    
    # 步骤 2：拉取全量线索
    print("[2/4] 拉取全量线索...")
    all_records = []
    page_token = ""
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        try:
            resp = _feishu_api("GET", f"/bitable/v1/apps/{os.environ.get('FEISHU_ATK')}/tables/{tid}/records", params=params)
            items = resp.get("data", {}).get("items", [])
            all_records.extend(items)
            print(f"   已拉取 {len(all_records)} 条...")
            has_more = resp.get("data", {}).get("has_more", False)
            page_token = resp.get("data", {}).get("page_token", "")
            if not has_more:
                break
        except Exception as e:
            print(f"   ❌ 拉取失败：{e}")
            return False
    
    print(f"   ✅ 共 {len(all_records)} 条线索")
    print()
    
    # 步骤 3：备份到本地
    print("[3/4] 备份到本地...")
    os.makedirs("RHC 运维备份", exist_ok=True)
    with open(backup_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": timestamp,
            "total": len(all_records),
            "records": all_records
        }, f, ensure_ascii=False, indent=2)
    print(f"   ✅ 备份完成：{backup_file}")
    print()
    
    # 步骤 4：批量删除
    print("[4/4] 删除所有线索...")
    record_ids = [r["record_id"] for r in all_records if "record_id" in r]
    print(f"   待删除：{len(record_ids)} 条")
    
    # 批量删除（每次最多 500 条）
    deleted = 0
    for i in range(0, len(record_ids), 500):
        batch = record_ids[i:i+500]
        try:
            resp = _feishu_api(
                "POST",
                f"/bitable/v1/apps/{os.environ.get('FEISHU_ATK')}/tables/{tid}/records/batch_delete",
                {"records": batch}
            )
            deleted += len(batch)
            print(f"   已删除 {deleted}/{len(record_ids)} 条...")
        except Exception as e:
            print(f"   ❌ 删除失败：{e}")
            return False
    
    print()
    print("=" * 60)
    print(f"✅ 清理完成！")
    print(f"   删除：{deleted} 条")
    print(f"   备份：{backup_file}")
    print("=" * 60)
    
    # 清除缓存
    _invalidate_leads_cache()
    print("   缓存已清除")
    
    return True

if __name__ == "__main__":
    cleanup_all_leads()
