#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0补邮箱离线原型v2：发现官网→增强深挖→MX推断。只读,不写飞书不改main.py。
用法 deep7 / clean17 / all35"""
import sys,os,re,json,time,socket,struct,random
sys.path.insert(0,"/Coze/Drive/RHC智能工作平台/rhc-marketing-assistant/backend")
os.environ.setdefault("FEISHU_APP_ID","x");os.environ.setdefault("FEISHU_APP_SECRET","x")
from urllib.parse import urlparse,urljoin
import html as _h
from app import main as m
CORE=json.load(open("/tmp/core35.json",encoding="utf-8"))
# 页面标题噪声（非真实公司主体）
NOISE_EXACT={'distributors','distributor','our dealers','our distributor','our distributors',
 'authorized veterinary distributors','veterinary distributors','distributors archive',
 'animal health pharmaceutical ingredient','veterinary equipment auctions',
 'veterinary equipment & service','veterinary equipment & medical supply co'}
NOISE_RE=re.compile(r'(distributor|distributors|located in|in canada|medical supply company|pharmaceutical ingredient|machine and parts|»|’s|supplies in tanzania)',re.I)
def is_noise(name):
    n=name.strip()
    if n.lower() in NOISE_EXACT:return True
    # 标题句特征：含多词叙述或地区尾巴，且不像一个可搜索商号（<=3词的专有名不杀）
    words=re.sub(r'[^A-Za-z0-9& ]',' ',n).split()
    if NOISE_RE.search(n) and len(words)>=4:return True
    return False

_CF_RE=re.compile(r'<a[^>]+class="__cf_email__"[^>]*data-cfemail="([0-9a-fA-F]+)"',re.I)
def _cf(h):
    try:
        b=bytes.fromhex(h);r=b[0];return ''.join(chr(x^r) for x in b[1:])
    except Exception:return ""
_AT=re.compile(r'([A-Za-z0-9._%+\-]{2,})\s*(?:\(|\[|\{)?\s*(?:@|\bat\b|\[at\]|\(at\)|\{at\})\s*(?:\)|\]|\})?\s*([A-Za-z0-9.\-]{3,}\.[A-Za-z]{2,})',re.I)
def ex_emails(html,host=""):
    out,seen=[],set()
    def add(e):
        e=e.strip().strip(".,;:)'\"").lower()
        if e in seen or not m._is_valid_email(e):return
        if any(b in e for b in m._CONTACT_EMAIL_BLOCK):return
        if e.endswith((".png",".jpg",".jpeg",".gif",".webp",".svg")):return
        seen.add(e);out.append(e)
    for x in _CF_RE.finditer(html or ""):
        d=_cf(x.group(1));
        if "@" in d:add(d)
    t=_h.unescape(html or "")
    for x in m._EMAIL_RE.finditer(t):add(x.group(0))
    for x in _AT.finditer(t):add(f"{x.group(1)}@{x.group(2)}")
    return out

CP=["/contact","/contact-us","/contactus","/contacts","/en/contact","/en/contact-us","/about/contact",
 "/get-in-touch","/reach-us","/about","/about-us","/aboutus","/about/company","/company/contact",
 "/imprint","/impressum","/kontakt","/contatti","/contacto","/contactez-nous"]
_HREF=re.compile(r'href=["\']([^"\']+)["\']',re.I)
_CW=re.compile(r'contact|about|imprint|impressum|kontakt|get[- ]in[- ]touch|reach|contatt|contacto',re.I)
def nav_links(home,root):
    L,S=[],{root+"/",root}
    for x in _HREF.finditer(home or ""):
        h=x.group(1).strip().split("#")[0].lower()
        if h.startswith(("mailto:","tel:","javascript:","#")) or not _CW.search(h):continue
        f=urljoin(root+"/",x.group(1).strip());p=urlparse(f)
        if p.scheme not in("http","https"):continue
        if m._reg_host(p.netloc)!=m._reg_host(urlparse(root).netloc):continue
        if f.rstrip("/") in S:continue
        S.add(f.rstrip("/"));L.append(f)
        if len(L)>=8:break
    return L

def scan(url,deadline):
    """返回 dict(root,pages,emails,ok,antibot,bad,known)"""
    R={"root":"","pages":0,"emails":[],"ok":False,"antibot":False,"bad":False}
    if not url:return R
    root=m._site_root(url) if "://" in url else "https://"+url
    R["root"]=root
    if not root or not m._real_website(root+"/") or m._is_junk_result_url(root+"/") or m._is_gov_edu_result(root+"/"):
        R["bad"]=True;return R
    host=urlparse(root).netloc.lower()
    for d,(note,known) in m._KNOWN_ANTIBOT_DOMAINS.items():
        if host==d or host.endswith("."+d):
            R["antibot"]=True
            if known:R["emails"]=[(known,root+"#known")]
            return R
    seen=set();em=[];es=set()
    def fetch(u):
        if time.time()>deadline:return ""
        try:return m._http_get(u,timeout=8) or ""
        except Exception:return ""
    def collect(h,u):
        R["pages"]+=1
        if len(h)<1200 and m._ANTIBOT_CHALLENGE_RE.search(h):R["antibot"]=True;return
        for e in ex_emails(h,host):
            if e not in es:es.add(e);em.append((e,u))
    home=fetch(root+"/");nav=nav_links(home,root) if home else []
    if home:R["ok"]=True;collect(home,root+"/")
    c=[]
    for u in nav+[root+p for p in CP]:
        k=u.rstrip("/")
        if k in seen or k==root.rstrip("/"):continue
        seen.add(k);c.append(u)
    for u in c[:9]:
        if time.time()>deadline or len(em)>=6:break
        h=fetch(u)
        if h:collect(h,u)
    R["emails"]=em[:6];return R

def dns_mx(dom,ip="8.8.8.8"):
    try:
        tid=random.randrange(65536);p=struct.pack("!HHHHHH",tid,0x0100,1,0,0,0)
        for part in dom.rstrip('.').split('.'):
            b=part.encode();p+=bytes([len(b)])+b
        p+=b"\x00"+struct.pack("!HH",15,1)
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(4)
        s.sendto(p,(ip,53));d,_=s.recvfrom(1024);s.close()
        rc=d[3]&0xF;an=struct.unpack("!H",d[6:8])[0]
        return False if rc==3 else (an>0 if rc==0 else None)
    except Exception:return None
GU=["info","sales","contact","export","office","admin","hello","mail","enquiries","inquiry"]

def discover(company,deadline):
    try:
        hosts,_=m._discover_official_hosts(company,deadline)
        return hosts or []
    except Exception:return []

def run_one(c):
    o=dict(c);o.update(emails=[],guess=[],mx=None,pages=0,antibot=False,note="",method="",discovered="")
    if is_noise(c["company"]):o["note"]="页面标题噪声,非公司主体";return o
    t=time.time();web=c["website"];host=None;scanres=None
    if web:
        scanres=scan(web,t+32)
    else:
        hosts=discover(c["company"],t+18)
        for h in hosts[:3]:
            r=scan("https://"+h,t+40)
            if r["ok"] or r["emails"] or r["antibot"]:
                scanres=r;o["discovered"]=h;break
        if scanres is None and hosts:
            scanres=scan("https://"+hosts[0],t+40);o["discovered"]=hosts[0]
    if scanres:
        o["pages"]=scanres["pages"];o["antibot"]=scanres["antibot"]
        if scanres["bad"]:o["note"]="官网字段疑似平台/非企业站"
        if scanres["emails"]:
            o["emails"]=scanres["emails"];o["method"]="官网深挖"
        reg=m._reg_host(urlparse(scanres["root"]).netloc) if scanres["root"] else ""
        # 只有确认是可达实质企业站(ok)且未抓到邮箱时才做MX推断
        if reg and not scanres["emails"] and scanres["ok"]:
            mx=dns_mx(reg);o["mx"]=mx
            if mx:o["guess"]=[f"{p}@{reg}" for p in GU];o["method"]="域名推断(未验证)"
        elif not scanres["ok"] and not scanres["emails"]:
            o["note"]=(o["note"]+" 首页不可达/无实质").strip()
    else:
        o["note"]="未发现可达官网"
    return o

if __name__=="__main__":
    mode=sys.argv[1] if len(sys.argv)>1 else "all35"
    if mode=="deep7":items=[c for c in CORE if c["website"]]
    elif mode=="clean17":items=[c for c in CORE if not c["website"] and not is_noise(c["company"])]
    else:items=CORE
    R=[];t0=time.time()
    for i,c in enumerate(items):
        r=run_one(c);R.append(r)
        em=";".join(e for e,_ in r["emails"]) or ("GU:"+",".join(r["guess"][:2]) if r["guess"] else "")
        print(f"[{i+1:>2}/{len(items)}] {('D:'+r['discovered'])[:28]:28} mx={str(r['mx']):5} pg={r['pages']:2} {c['company'][:26]:26} | {em} {r['note']}")
    json.dump(R,open("/tmp/p0_results.json","w"),ensure_ascii=False,indent=1)
    real=[r for r in R if r["note"]!="页面标题噪声,非公司主体"]
    got=sum(1 for r in R if r["emails"]);gz=sum(1 for r in R if r["guess"]);nv=len(R)-len(real)
    print(f"\n== 总{len(R)} 噪声{nv} 实抓{got} 可推断{gz} 耗时{time.time()-t0:.0f}s ==")
