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

    print("== 第四批：平台类按域名主体匹配（IT 重点：国别后缀全挡）==")
    for d in ["europages.co.uk", "europages.de", "europages.fr",
              "www.europages.co.uk", "m.europages.es",
              "kompass.co.uk", "thomasnet.de", "alibaba.cn",
              "www.machineseeker.co.uk", "amazon.co.uk", "yellowpages.com.au",
              "hotfrog.in"]:
        chk(f"平台挡[{d}]", ns["_is_junk_result_url"]("https://" + d + "/x"), True)
    print("== 第四批：平台主体匹配不得误伤同名企业官网 ==")
    for d in ["europages-vet-clinic.com", "mykompass-software.com", "amazonforestsupplies.com",
              "cylex-veterinary.com", "valevetequipment.co.uk", "burtonsveterinary.com"]:
        chk(f"企业留[{d}]", ns["_is_junk_result_url"]("https://" + d + "/"), False)
    print("== 第四批：竞品仍按精确串（不升级主体匹配）==")
    for d in ["mindrayanimal.com", "zoetis.com", "kruuse.com"]:
        chk(f"竞品挡[{d}]", ns["_is_junk_result_url"]("https://www." + d + "/p"), True)

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
    # 新闸门三档：有强联系(邮箱)才不封顶；有官网+经销商→59；四要素空→45
    chk("final 有邮箱不封顶", ns["_finalize_lead_score"](88, {"email_pattern": "b@x.com", "website": "http://x.com"}, "company", "distributor"), 88)
    chk("final 官网经销商封顶59", ns["_finalize_lead_score"](88, {"website": "http://x.com"}, "company", "distributor"), 59)
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
    chk("Vale带出已知邮箱", r.get("known_email"), "sales@valevetequipment.co.uk")
    rb2 = ns["_fetch_official_site_contacts"]("https://burtonsveterinary.com/")
    chk("Burtons为antibot", rb2.get("antibot"), True)
    chk("Burtons无已知邮箱", rb2.get("known_email"), "")

    print("== 第四批：存量评级回填口径（模拟 backfill_lead_grades 核心判定）==")
    def backfill_expected(score, cur):
        want = ns["_grade_from_score"](int(float(score)))
        return (want, (cur.strip().upper() == want))
    # 历史撕裂样本：必须被改写到与分数一致
    for score, cur, want in [("45", "A", "C"), ("10", "A", "C"), ("8", "A", "C"),
                             ("85", "C", "A"), ("62", "A", "B"), ("90", "A", "A"),
                             ("55", "B", "C")]:
        w, unchanged = backfill_expected(score, cur)
        chk(f"回填 {score}分 旧{cur} -> {w}", (w, unchanged), (want, cur == want))
    # B2B/政府站安全闸门：拒绝抓取
    for bad in ["https://www.machineseeker.co.uk/", "https://bizzmed.co.za/", "https://www.trade.gov/x"]:
        rb = ns["_fetch_official_site_contacts"](bad)
        chk(f"拒抓[{bad.split('/')[2]}]", any(rb.values()), False)

    print("== 第五批 a：竞品 host 规则层强制 competitor（不依赖 Coze）==")
    hfp = ns["_host_forced_page_type"]
    ovr = ns["_apply_host_page_override"]
    comp_urls = ["https://www.dreveterinary.com/", "http://dreveterinary.com/products"]
    for u in comp_urls:
        chk(f"竞品host强制competitor[{u}]", hfp(u), "competitor")
    # 即便 Coze 误判成 company/importer，host 覆盖也必须纠回 competitor
    pt2, bt2 = ovr("company", "importer", "https://www.dreveterinary.com/")
    chk("覆盖压过Coze误判(竞品)", (pt2, bt2), ("competitor", "importer"))
    fin = ns["_finalize_lead_score"](88, {}, "competitor", "importer")
    chk("竞品固定5分（即便粗分88）", fin, 5)

    print("== 第五批 b：国别黄页 host 强制 directory / 固定10分 ==")
    dir_urls = ["https://www.businesslist.co.ke/company/123",
                "https://businesslist.co.za/x", "https://www.europages.co.uk/x"]
    for u in dir_urls:
        chk(f"黄页host强制directory[{u.split('/')[2]}]", hfp(u), "directory")
    pt3, bt3 = ovr("company", "unknown", "https://www.businesslist.co.ke/company/1")
    chk("覆盖压过Coze误判(黄页)", pt3, "directory")
    fin2 = ns["_finalize_lead_score"](47, {}, "directory", "unknown")
    chk("黄页固定10分（即便粗分47）", fin2, 10)

    print("== 第五批 a/b：正常企业官网不得被强制分类 ==")
    for u in ["https://valevetequipment.co.uk/", "https://www.bupo.com/",
              "https://europages-vet-clinic.com/", "https://marivet.cl/contacto"]:
        chk(f"企业官网不强制分类[{u.split('/')[2]}]", hfp(u), "")

    print("== 第五批：可达性闸门三档（IT 定稿 59，不选 60）==")
    gate = ns["_apply_score_gate"]
    WEB = {"official_website": "https://www.goodvetdealer.com/"}
    # 档1：有强联系信号（邮箱/决策人/LinkedIn）→ 不封顶
    chk("有邮箱不封顶(78)", gate(78, {"email_pattern": "buyer@co.com"}, "company", "importer"), 78)
    chk("有决策人不封顶(85)", gate(85, {"decision_maker": "John"}, "company", "distributor"), 85)
    # 档2：无强联系 + 真官网 + company + 经销类买家 → 封顶 59
    for bt in ["importer", "distributor", "wholesaler", "dealer"]:
        chk(f"经销商官网封顶59[{bt}](88)", gate(88, dict(WEB), "company", bt), 59)
        chk(f"经销商官网封顶59[{bt}](55不破)", gate(55, dict(WEB), "company", bt), 55)
    # 59 仍算 C（B 线 60），保留"B=能联系上"
    chk("59评级仍为C", ns["_grade_from_score"](59), "C")
    chk("60评级才是B", ns["_grade_from_score"](60), "B")
    # 档3：四要素弱、非合格公司 → 仍封顶 45
    chk("无官网company经销商→45", gate(88, {}, "company", "importer"), 45)
    chk("有官网但manufacturer→45", gate(88, dict(WEB), "company", "manufacturer"), 45)
    chk("有官网但bt=unknown→45", gate(88, dict(WEB), "company", "unknown"), 45)
    chk("有官网但pt非company→45", gate(88, dict(WEB), "news_report", "importer"), 45)
    chk("四要素空非company→45", gate(88, {}, "", ""), 45)
    chk("低分不抬升(30→30)", gate(30, {}, "company", "importer"), 30)
    # 黄页 host 在进闸门前即被 host 覆盖强制 directory，finalize 整链固定10（不会进59档）
    _pt, _bt = ns["_apply_host_page_override"]("company", "importer", "https://www.europages.co.uk/x")
    chk("黄页host整链不进59档", ns["_finalize_lead_score"](88, {"website": "https://www.europages.co.uk/x"}, _pt, _bt), 10)
    # 搜索引擎结果页不算真官网，即便伪报 company+importer 也只能到45
    chk("搜索页URL不算官网→45",
        gate(88, {"website": "https://www.google.com/search?q=vet"}, "company", "importer"), 45)

    print("\n====", "ALL_OK" if not FAILS else f"{len(FAILS)} FAILURES", "====")
    if FAILS:
        print("\n".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
