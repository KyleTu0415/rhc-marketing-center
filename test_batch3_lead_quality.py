# -*- coding: utf-8 -*-
"""第三批线索质量修复离线回归测试。

运行：cd 仓库根目录 && python3 test_batch3_lead_quality.py
不依赖网络与飞书凭证：用 AST 抽取 main.py 的函数/常量到独立命名空间做纯函数断言，
仅"官网直取"部分在函数内做静态 HTML 解析，不发起真实请求。

覆盖：
- 变更1 二手/拍卖设备 + 二手平台域名：该挡/该留
- 变更2 厂家 SEO 标题（Manufacturer 结尾/重复）
- 变更3 评级统一出口 _grade_from_score + 非公司页固定分 + 分数收口
- 变更4 官网联系页解析：职能邮箱/电话/LinkedIn/联系人；反爬挑战页必须返回空（防时间戳误判回退）
"""
import ast
import re
import sys

SRC_PATH = "backend/app/main.py"


def load_namespace():
    src = open(SRC_PATH, "rb").read().decode("utf-8")
    tree = ast.parse(src)
    ns = {"re": re}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            try:
                exec(ast.get_source_segment(src, node), ns)
            except Exception:
                pass
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            try:
                exec(ast.get_source_segment(src, node), ns)
            except Exception:
                pass
    return ns


FAILS = []


def chk(name, got, exp):
    ok = got == exp
    if not ok:
        FAILS.append(f"{name}: got={got!r} exp={exp!r}")
    print(("PASS " if ok else "FAIL "), name, "=", got)


def main():
    ns = load_namespace()

    print("== 变更1 二手/拍卖：该挡 ==")
    block = [
        ("Used ventilation machines Machineseeker", "https://www.machineseeker.co.uk/Breathing-systems/ci-337"),
        ("Used breathing machine Machineseeker.com", "https://www.machineseeker.com/x"),
        ("Veterinary Equipment Auctions", "https://www.vetecom.co.uk/auction-practice"),
        ("Used medical equipment auction", "https://example.com/auctions/vet"),
        ("Refurbished ventilator for sale", "https://shop.example/used-ventilator"),
        ("dotmed listing", "https://www.dotmed.com/listing/123"),
        ("labx used lab gear", "https://www.labx.com/item/9"),
        ("kitmondo used machine", "https://www.kitmondo.com/machinery/5"),
        ("equipnet category", "https://www.equipnet.com/category/anesthesia/"),
    ]
    for t, u in block:
        chk(f"挡[{t[:28]}]", ns["_is_secondhand_result"](u, t) or ns["_is_junk_result_url"](u), True)

    print("== 变更1：该留 ==")
    keep = [
        ("Vale Veterinary Equipment Supplies", "https://www.valevetequipment.co.uk/"),
        ("Veterinary Practice News", "https://www.vettimes.co.uk/news/"),
        ("About our practice", "https://www.cityvet.co.uk/practice/"),
        ("Burton's Veterinary", "https://burtonsveterinary.com/"),
        ("Distributor of new equipment", "https://www.vet-uk.co.uk/distribution-network/"),
    ]
    for t, u in keep:
        got = (not ns["_is_secondhand_result"](u, t) and not ns["_is_junk_result_url"](u)
               and not ns["_is_seller_or_section_url"](u))
        chk(f"留[{t[:24]}]", got, True)

    print("== 变更2 厂家SEO标题 ==")
    for t in ["Veterinary Injection Manufacturer Manufacturer",
              "Veterinary syringe pump manufacturer",
              "Leading veterinary equipment manufacturer"]:
        chk(f"厂家[{t[:30]}]", ns["_is_manufacturer_title"](t), True)
    for t in ["Vale Veterinary Equipment Supplies",
              "Veterinary equipment distributor UK",
              "We are a distributor (not the manufacturer) of vet devices",
              "Animal Health Kenya"]:
        chk(f"留[{t[:30]}]", ns["_is_manufacturer_title"](t), False)

    print("== 变更3 评级与固定分 ==")
    for score, g in [(95, "A"), (80, "A"), (79, "B"), (60, "B"), (45, "C"), (8, "C")]:
        chk(f"grade {score}", ns["_grade_from_score"](score), g)
    for pt, exp in [("directory", 10), ("b2b_platform", 10), ("news_report", 10),
                    ("navigation", 10), ("competitor", 5), ("unknown", -1), ("company", -1)]:
        chk(f"fixed[{pt}]", ns["_excluded_page_score"](pt), exp)
    chk("final 可达不封顶", ns["_finalize_lead_score"](88, {"website": "http://x.com"}, "company", "distributor"), 88)
    chk("final 不可达封顶45", ns["_finalize_lead_score"](90, {}, "company", "distributor"), 45)
    chk("final competitor=5", ns["_finalize_lead_score"](90, {}, "company", "manufacturer"), 5)
    chk("final directory=10", ns["_finalize_lead_score"](90, {}, "directory", "unknown"), 10)

    print("== 变更4 联系页解析：真实页 ==")
    real_html = ('<footer><strong>Tel:</strong> +27 12 803 4376<br>'
                 '<a href="tel:+1 (415) 555-0132">call</a> '
                 '<a href="mailto:sales@acme-vet.co.uk">email</a> '
                 '<a href="https://www.linkedin.com/company/acme-vet">in</a> '
                 'John Smith / Managing Director</footer>')
    info = ns["_parse_contact_html"](real_html, "acme-vet.co.uk")
    chk("解析邮箱", info["email"], "sales@acme-vet.co.uk")
    chk("解析电话", info["phone"], "+1 (415) 555-0132")
    chk("解析LinkedIn", info["linkedin"], "https://www.linkedin.com/company/acme-vet")
    chk("解析联系人", info["decision_maker"], "John Smith")

    print("== 变更4 反爬挑战页：必须全空（防时间戳误判回退）==")
    challenge = ('<html><head><meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/'
                 '?r=%2Fcontact-us%2F&y=ipc:115.190.137.182:1789835651.180"></meta></head></html>')
    cinfo = ns["_parse_contact_html"](challenge, "valevetequipment.co.uk")
    chk("挑战页无电话(时间戳)", cinfo["phone"], "")
    chk("挑战页整体为空", any(cinfo.values()), False)
    # 裸时间戳/版本号不得被认成电话
    chk("JS时间戳非电话", ns["_parse_contact_html"]("<p>1789835651.180</p>", "x.com")["phone"], "")
    # 已知强反爬域名直取必须直接标记 antibot 且不抓取
    r = ns["_fetch_official_site_contacts"]("https://www.valevetequipment.co.uk/")
    chk("Vale登记为antibot", r.get("antibot"), True)
    chk("Vale不返回垃圾字段", any([r["email"], r["phone"], r["linkedin"], r["decision_maker"]]), False)
    # B2B/政府站安全闸门：拒绝抓取
    for bad in ["https://www.machineseeker.co.uk/", "https://bizzmed.co.za/", "https://www.trade.gov/x"]:
        rb = ns["_fetch_official_site_contacts"](bad)
        chk(f"拒抓[{bad.split('/')[2]}]", any(rb.values()), False)

    print("\n====", "ALL_OK" if not FAILS else f"{len(FAILS)} FAILURES", "====")
    if FAILS:
        print("\n".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
