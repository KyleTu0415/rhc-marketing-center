# -*- coding: utf-8 -*-
"""P0 首仗实测结果固化（沙箱 DNS+可达站深挖；线上出站全开后命中率只会更高）。
tier: A=官网实抓真实邮箱  B=域名+MX推断(未验证)  C=待人工/线上重试  N=非公司主体噪声
"""
ROWS=[
# 7 条带官网字段
 dict(co="Vale Veterinary Equipment",cc="UK",web="valevetequipment.co.uk",em="sales@valevetequipment.co.uk",src="已知反爬登记(sgcaptcha)",tier="A",note="机房IP被反爬，邮箱来自既有登记表，待Hunter验活"),
 dict(co="Bupo Animal Health",cc="南非",web="bupoanimalhealth.com",em="pharma@bupoah.co.za",src="首页+contact+about实抓",tier="A",note="官网实抓；注意邮箱域bupoah.co.za与网站域不同但同页出现，疑为同集团"),
 dict(co="Merge Companion (Thailand)",cc="泰国",web="vetproductsgroup.com",em="info/sales/contact@vetproductsgroup.com",src="域名推断(未验证)",tier="B",note="7个子页均无明文邮箱；域有MX，仅给推断候选，必须Hunter验证"),
 dict(co="Drstoystore",cc="未知",web="drstoystore.com",em="info@drstoystore.com",src="域名推断(未验证)",tier="C",note="电商货架，且非器械买家；建议直接判废不发"),
 dict(co="Mms Mckesson",cc="未知",web="mms.mckesson.com",em="",src="—",tier="C",note="官网字段是巨头平台内页，首页不可达，脏数据，判废"),
 dict(co="Duraprohealth",cc="未知",web="duraprohealth.com",em="",src="—",tier="C",note="403内页，非企业独立站，判废/人工"),
 dict(co="Vetpoultry",cc="未知",web="vetpoultry.com",em="info@vetpoultry.com",src="域名推断(未验证)",tier="B",note="首页247KB及contact均无明文；有MX，仅推断候选"),
# 无官网字段，DNS+首页MATCH确认的真实企业站
 dict(co="Burtons",cc="UK",web="burtonsveterinary.com",em="",src="已知反爬(机房IP 403)",tier="C",note="真实头部兽医经销商；沙箱403，线上需换出口/人工；注意burtons.co.uk是同名误配已剔除"),
 dict(co="SelfiMed UK",cc="UK",web="selfimed.co.uk",em="info@selfimed.com",src="首页实抓",tier="A",note="首页明文，1.2MB真实企业站MATCH"),
 dict(co="Photon Surgical Systems",cc="UK",web="photonsurgicalsystems.co.uk",em="info@photonsurgicalsystems.co.uk",src="首页实抓",tier="A",note="472KB真实站MATCH"),
 dict(co="Afrimedics",cc="南非",web="afrimedics.co.za",em="sales@afrimedics.co.za; capetown@afrimedics.co.za; info@afrimedics.co.za",src="首页实抓(3个职能邮箱)",tier="A",note="2MB真实站MATCH；另afrimedics.com是114字节停放页，已正确剔除"),
 dict(co="TMHS Group",cc="坦桑尼亚",web="tmhs.co.tz",em="info@tmhsgroup.com; info@tmhstz.com",src="站内实抓(清洗www拼接后)",tier="A",note="tmhs.co.uk是同名误配已剔除；真站是.co.tz，站内含tmhsgroup/tmhstz两个邮箱"),
 dict(co="Anudha Limited",cc="坦桑尼亚",web="anudha.com",em="anudha@anudha.com",src="首页实抓",tier="A",note="78KB真实站MATCH"),
 dict(co="Cairo Medical",cc="埃及",web="cairomedical.org",em="info@cairomedical.org",src="首页实抓",tier="A",note="160KB站MATCH；.com是114字节停放页已剔除；待确认是否兽用器械买家"),
 dict(co="Farm Vet Supplies",cc="UK",web="farmvetsupplies.com",em="farmvetsupplies@gmail.com",src="首页实抓",tier="A",note="Gmail个人邮箱可发但优先级低；页面夹带的defra政府邮箱已自动过滤"),
 dict(co="Intriquip",cc="未知",web="intriquip.com",em="info@intriquip.com",src="域名推断(未验证)",tier="B",note="域有MX但沙箱抓不到首页，无法MATCH，仅推断待验"),
# 未命中/误配/噪声
 dict(co="Reliance Poultry Equipment",cc="南非",web="",em="",src="DNS未命中",tier="C",note="常见TLD组合均无A/MX；线上用搜索引擎再找"),
 dict(co="Agrifarmacysa Za",cc="南非",web="",em="",src="DNS未命中",tier="C",note="公司名本身是抓取乱码，需回原文链接人工辨认真名"),
 dict(co="Distributor International",cc="埃及",web="",em="",src="通用商号误配拦截",tier="N",note="所有匹配域首页不可达或不匹配，无法定位唯一主体"),
 dict(co="Veterinary instruments Germany",cc="德国",web="",em="",src="描述句/无主体",tier="N",note="是描述不是公司名，回原文重提取"),
 dict(co="Pattersonvet",cc="未知",web="(patterson.org已剔除)",em="",src="同名误配拦截",tier="N",note="patterson.org是美国教会站；真·Patterson兽医批发pattersonvet.com需线上再找"),
]
# 16条纯页面标题噪声（非公司主体）
NOISE=["Veterinary Equipment & Service","Authorized Veterinary Distributors","Veterinary Equipment Auctions",
 "OUR DEALERS","Distributors","Distributors(重复2条)","Distributors Archive","Veterinary Distributors(2条)",
 "Veterinary Anesthesia Machine and Parts » Robert's Repair","Animal Health Pharmaceutical Ingredient",
 "Veterinary Equipment & Medical Supply Company located in Melbourne","Our Distributors",
 "Used Vet & Medical Equipment in Canada","Veterinary and instruments Distributor Germany(标题句)"]
