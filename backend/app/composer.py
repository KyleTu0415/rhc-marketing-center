"""

RHC Medical - 图片合成引擎（Pillow 实现）

支持：背景图/渐变 + 产品图 + 动物图 + 文字卡片 多层合成

"""

import base64

import io

import math

import urllib.request

import urllib.parse

import json

from PIL import Image, ImageDraw, ImageFont, ImageFilter



UPLOAD_KEY = "6d207e02198a847aa98d0a2a901485a5"

UPLOAD_URL = "https://freeimage.host/api/1/upload"


def _upload_bytes_to_freeimage(img_bytes: bytes, filename: str = "image.png") -> str:
    """Upload image bytes directly to freeimage.host, return public URL."""
    import httpx
    files = {"source": (filename, img_bytes, "image/png")}
    data = {"key": UPLOAD_KEY, "type": "file", "action": "upload"}
    with httpx.Client(timeout=30) as client:
        r = client.post(UPLOAD_URL, data=data, files=files)
        r.raise_for_status()
        j = r.json()
        if j.get("status_code") == 200:
            return j["image"]["url"]
        raise RuntimeError(f"freeimage upload failed: {j}")



CANVAS_W = 1200

CANVAS_H = 800



# 位置锚点（相对坐标 0~1）

POS_MAP = {

    "nw":      (0.0, 0.0),

    "north":   (0.5, 0.0),

    "ne":      (1.0, 0.0),

    "west":    (0.0, 0.5),

    "center":  (0.5, 0.5),

    "east":    (1.0, 0.5),

    "sw":      (0.0, 1.0),

    "south":   (0.5, 1.0),

    "se":      (1.0, 1.0),

}



# 主题配色

THEMES = {

    "medical-teal": {

        "bg_start": (15, 46, 47),

        "bg_end":   (35, 85, 88),

        "accent":   (200, 168, 93),

        "card_bg":  (255, 255, 255, 235),

        "card_title": (15, 46, 47),

        "card_subtitle": (80, 80, 80),

    },

    "deep-blue": {

        "bg_start": (12, 35, 64),

        "bg_end":   (30, 80, 140),

        "accent":   (91, 192, 235),

        "card_bg":  (255, 255, 255, 235),

        "card_title": (12, 35, 64),

        "card_subtitle": (80, 80, 80),

    },

    "clean-white": {

        "bg_start": (240, 245, 248),

        "bg_end":   (255, 255, 255),

        "accent":   (15, 110, 110),

        "card_bg":  (15, 46, 47, 240),

        "card_title": (255, 255, 255),

        "card_subtitle": (200, 210, 210),

    },

    "warm-gold": {

        "bg_start": (45, 32, 22),

        "bg_end":   (90, 65, 40),

        "accent":   (220, 190, 110),

        "card_bg":  (255, 255, 255, 235),

        "card_title": (45, 32, 22),

        "card_subtitle": (80, 80, 80),

    },

}





def download_image(url: str) -> Image.Image:

    """下载图片并转为 RGBA。支持 http(s):// 和 file:///"""

    if not url:

        return None

    if url.startswith("file:///"):

        path = url[8:].lstrip("/")

        # Windows 路径修正: /C:/path -> C:/path

        if len(path) >= 3 and path[1] == ":":

            path = path[0].upper() + path[1:]

        return Image.open(path).convert("RGBA")

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    with urllib.request.urlopen(req, timeout=30) as r:

        data = r.read()

    return Image.open(io.BytesIO(data)).convert("RGBA")





def upload_image(img: Image.Image) -> str:

    """保存图片到本地 uploads 目录，返回本地 URL"""

    import os

    import time

    uploads_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")

    os.makedirs(uploads_dir, exist_ok=True)

    

    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)

    if has_alpha:

        filename = f"animal_{int(time.time()*1000)}.png"

        filepath = os.path.join(uploads_dir, filename)

        img.save(filepath, format="PNG")

    else:

        filename = f"bg_{int(time.time()*1000)}.jpg"

        filepath = os.path.join(uploads_dir, filename)

        img.convert("RGB").save(filepath, format="JPEG", quality=92)

    

    print(f"[info] saved to: {filepath}")

    # Return local URL that backend will serve

    return f"http://localhost:8000/uploads/{filename}"





def make_gradient_bg_fast(w: int, h: int, theme: dict) -> Image.Image:

    """快速生成渐变背景（numpy 对角渐变）"""

    import numpy as np

    start = theme["bg_start"]

    end = theme["bg_end"]



    x = np.linspace(0, 1, w, dtype=np.float32)

    y = np.linspace(0, 1, h, dtype=np.float32)

    xx, yy = np.meshgrid(x, y)

    t = (xx + yy) / 2.0



    arr = np.zeros((h, w, 4), dtype=np.uint8)

    for i in range(3):

        arr[:, :, i] = (start[i] + (end[i] - start[i]) * t).astype(np.uint8)

    arr[:, :, 3] = 255



    return Image.fromarray(arr, "RGBA")





def add_decorations(bg: Image.Image, theme: dict) -> Image.Image:

    """添加装饰元素：半透明圆形、线条等"""

    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 0))

    draw = ImageDraw.Draw(overlay)

    w, h = bg.size

    accent = theme["accent"]



    # 右上角大圆（半透明）

    draw.ellipse(

        [w - 300, -100, w + 100, 300],

        fill=(*accent, 25),

    )

    # 左下角小圆

    draw.ellipse(

        [-80, h - 200, 120, h + 80],

        fill=(*accent, 18),

    )

    # 左侧细线条

    for i in range(3):

        y = 120 + i * 60

        draw.line([(0, y), (60, y)], fill=(*accent, 60), width=2)



    bg = Image.alpha_composite(bg, overlay)

    return bg





def _get_font(size: int, bold: bool = False):

    """尝试加载字体，失败则用默认字体"""

    font_paths = [

        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",

        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",

        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",

    ]

    for fp in font_paths:

        try:

            return ImageFont.truetype(fp, size)

        except (OSError, IOError):

            continue

    return ImageFont.load_default()





def _wrap_text(text: str, font, max_width: int, draw) -> list:

    """将文本按宽度自动换行"""

    words = text.split()

    lines = []

    current = ""

    for word in words:

        test = (current + " " + word).strip()

        bbox = draw.textbbox((0, 0), test, font=font)

        if bbox[2] - bbox[0] <= max_width:

            current = test

        else:

            if current:

                lines.append(current)

            current = word

    if current:

        lines.append(current)

    return lines





def make_text_card(

    title: str,

    subtitle: str = "",

    theme_name: str = "medical-teal",

    max_width: int = 560,

) -> Image.Image:

    """生成文字卡片（自动宽度 + 自动换行）"""

    theme = THEMES.get(theme_name, THEMES["medical-teal"])

    title_font = _get_font(30, bold=True)

    sub_font = _get_font(16, bold=False)



    pad_x, pad_y = 28, 22



    # 先测量文字

    dummy = Image.new("RGBA", (10, 10))

    d = ImageDraw.Draw(dummy)



    # 标题换行

    text_area_w = max_width - pad_x * 2 - 8  # 8 for accent line

    title_lines = _wrap_text(title, title_font, text_area_w, d)



    # 计算标题高度

    title_line_h = 0

    for line in title_lines:

        bbox = d.textbbox((0, 0), line, font=title_font)

        title_line_h = max(title_line_h, bbox[3] - bbox[1])

    title_total_h = title_line_h * len(title_lines) + (len(title_lines) - 1) * 6



    # 副标题

    sub_h = 0

    if subtitle:

        sub_lines = _wrap_text(subtitle, sub_font, text_area_w, d)

        sub_line_h = 0

        for line in sub_lines:

            bbox = d.textbbox((0, 0), line, font=sub_font)

            sub_line_h = max(sub_line_h, bbox[3] - bbox[1])

        sub_h = sub_line_h * len(sub_lines) + (len(sub_lines) - 1) * 4

    else:

        sub_lines = []



    height = pad_y * 2 + title_total_h + sub_h + (12 if subtitle else 0)



    card = Image.new("RGBA", (max_width, height), (0, 0, 0, 0))

    draw = ImageDraw.Draw(card)



    # 圆角矩形背景

    radius = 14

    card_bg = theme["card_bg"]

    draw.rounded_rectangle(

        [(0, 0), (max_width - 1, height - 1)],

        radius=radius,

        fill=card_bg,

    )



    # 左侧强调竖线

    draw.rectangle(

        [(0, 12), (4, height - 12)],

        fill=theme["accent"],

    )



    # 绘制标题行

    ty = pad_y

    for line in title_lines:

        draw.text(

            (pad_x + 8, ty),

            line,

            fill=theme["card_title"],

            font=title_font,

        )

        ty += title_line_h + 6



    # 副标题

    if subtitle:

        ty += 6  # extra gap

        for line in sub_lines:

            draw.text(

                (pad_x + 8, ty),

                line,

                fill=theme["card_subtitle"],

                font=sub_font,

            )

            ty += sub_line_h + 4



    return card





def paste_layer(

    base: Image.Image,

    layer: Image.Image,

    location: str = "center",

    zoom_pct: int = 50,

    margin: int = 30,

) -> None:

    """将图层按位置和缩放比例贴到画布上（就地修改 base）"""

    anchor = POS_MAP.get(location, POS_MAP["center"])

    z = max(5, min(95, zoom_pct)) / 100.0



    # 缩放以画布宽度为基准，但保持图层宽高比

    target_w = int(base.width * z)

    ratio = target_w / layer.width

    target_h = int(layer.height * ratio)



    # 如果缩放后高度超过画布高度的 90%，改为以高度为基准

    if target_h > base.height * 0.9:

        target_h = int(base.height * 0.9)

        ratio = target_h / layer.height

        target_w = int(layer.width * ratio)



    layer = layer.resize((target_w, target_h), Image.LANCZOS)



    # 以锚点为中心定位

    x = int((base.width - target_w) * anchor[0])

    y = int((base.height - target_h) * anchor[1])



    # 边界检查

    x = max(margin, min(x, base.width - target_w - margin))

    y = max(margin, min(y, base.height - target_h - margin))



    base.paste(layer, (x, y), layer)





def generate_ai_background(prompt: str = "", style: str = "professional") -> dict:

    """Generate pure AI scene background via Coze workflow, return {image_url, debug_log}"""

    from app.config import settings

    log = []

    pat = settings.COZE_PAT

    workflow_id = settings.COZE_WORKFLOW_ID

    if not pat or not workflow_id:

        log.append("[error] Coze PAT or WORKFLOW_ID not configured")

        return {"image_url": "", "debug_log": "\n".join(log)}



    api_url = "https://api.coze.cn/v1/workflow/run"    # Pass user description + style to workflow; no product image needed

    effective_style = style

    if prompt and prompt.strip():

        effective_style = f"{style}, user scene description: {prompt.strip()}"

    payload = {

        "workflow_id": workflow_id,

        "parameters": {

            "style": effective_style,

        }

    }

    log.append(f"[info] calling Coze workflow {workflow_id}")

    log.append(f"[info] effective_style={effective_style[:80]}")



    try:

        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(

            api_url,

            data=body,

            headers={

                "Authorization": f"Bearer {pat}",

                "Content-Type": "application/json; charset=utf-8",

            },

            method="POST",

        )

        with urllib.request.urlopen(req, timeout=120) as resp:

            result = json.loads(resp.read().decode("utf-8"))

        code = result.get("code")

        if code != 0:

            log.append(f"[error] Coze workflow failed: code={code}, msg={result.get('msg','')}")

            return {"image_url": "", "debug_log": "\n".join(log)}



        # Parse output: data is a JSON string like {"output": ["https://..."]}

        data_str = result.get("data", "")

        if isinstance(data_str, str):

            data_obj = json.loads(data_str)

        else:

            data_obj = data_str



        output_list = data_obj.get("output", [])

        if output_list:

            img_url = output_list[0]

            log.append(f"[ok] AI background generated: {img_url}")

            return {"image_url": img_url, "debug_log": "\n".join(log)}

        else:

            log.append("[error] No output URL in workflow response")

            return {"image_url": "", "debug_log": "\n".join(log)}



    except Exception as e:

        log.append(f"[error] Coze workflow call failed: {type(e).__name__}: {e}")

        return {"image_url": "", "debug_log": "\n".join(log)}





def generate_background(theme: str = "medical-teal") -> dict:

    """生成纯净背景图（无文字、无产品），返回 {image_url, debug_log}"""

    log = []

    theme_cfg = THEMES.get(theme, THEMES["medical-teal"])

    bg = make_gradient_bg_fast(CANVAS_W, CANVAS_H, theme_cfg)

    bg = add_decorations(bg, theme_cfg)

    log.append(f"[ok] clean background generated ({theme}), no text")

    try:

        url = upload_image(bg)

        log.append(f"[ok] uploaded: {url}")

        return {"image_url": url, "debug_log": "\n".join(log)}

    except Exception as e:

        log.append(f"[error] upload failed: {e}")

        buf = io.BytesIO()

        bg.convert("RGB").save(buf, format="JPEG", quality=85)

        b64 = base64.b64encode(buf.getvalue()).decode()

        return {"image_url": f"data:image/jpeg;base64,{b64}", "image_base64": b64, "debug_log": "\n".join(log)}





def _translate_to_english(text: str) -> str:

    """调用 DeepSeek 将中文描述翻译为英文图片搜索词"""

    from app.config import settings

    api_key = settings.OPENAI_API_KEY

    base_url = settings.OPENAI_BASE_URL

    if not api_key:

        return text

    try:

        import httpx

        resp = httpx.post(

            f"{base_url}/chat/completions",

            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},

            json={

                "model": settings.OPENAI_TEXT_MODEL,

                "messages": [

                    {"role": "system", "content": "Translate the user's Chinese animal description into concise English keywords suitable for an image search engine. Output ONLY the English keywords, nothing else. Example input: '金毛，正对，坐着' -> 'golden retriever sitting facing camera'"},

                    {"role": "user", "content": text},

                ],

                "temperature": 0.2,

                "max_tokens": 60,

            },

            timeout=15,

        )

        resp.raise_for_status()

        result = resp.json()["choices"][0]["message"]["content"].strip()

        return result or text

    except Exception:

        return text





def _http_get_with_retry(url, params=None, timeout=60, retries=3, headers=None):

    """带重试的 HTTP GET，应对国内网络不稳定"""

    import httpx

    import time

    last_err = None

    for attempt in range(retries):

        try:

            resp = httpx.get(url, params=params, timeout=timeout,

                             follow_redirects=True, headers=headers,

                             verify=True)

            resp.raise_for_status()

            return resp

        except Exception as e:

            last_err = e

            print(f"[warn] HTTP attempt {attempt+1}/{retries} failed: {type(e).__name__}: {e}")

            if attempt < retries - 1:

                time.sleep(2 * (attempt + 1))

    raise last_err





def _search_animal_unsplash(query: str) -> list:

    """Search animal images via keyword-matched Unsplash direct URLs (no key needed)"""

    import urllib.parse

    query_lower = query.lower().strip()



    # Chinese keyword -> English keyword pre-mapping (for when translation fails)

    cn_to_en = {

        "金毛": "golden retriever", "拉布拉多": "labrador", "哈士奇": "husky",

        "柯基": "corgi", "贵宾": "poodle", "泰迪": "poodle",

        "比格": "beagle", "斗牛犬": "bulldog", "牧羊犬": "german shepherd",

        "狗": "dog", "犬": "dog",

        "猫": "cat", "小猫": "kitten", "波斯猫": "persian cat",

        "兔": "rabbit", "兔子": "rabbit",

        "鹦鹉": "parrot", "鸟": "bird",

        "马": "horse",

        "牛": "cow", "奶牛": "cow",

        "羊": "sheep",

        "猪": "pig",

        "仓鼠": "hamster",

        "龟": "turtle", "乌龟": "turtle",

        "蛇": "snake",

        "鸡": "chicken", "鸭": "duck",

        "狐狸": "fox", "鹿": "deer", "狮子": "lion", "老虎": "tiger",

        "熊": "bear", "熊猫": "panda", "大象": "elephant", "猴子": "monkey",

    }

    # Apply Chinese mapping: if any Chinese keyword found, use its English equivalent

    for cn_kw, en_kw in cn_to_en.items():

        if cn_kw in query_lower:

            query_lower = en_kw

            break



    # Common veterinary animal -> Unsplash direct image URL mapping

    animal_urls = {

        # Dogs

        "golden retriever": "https://images.unsplash.com/photo-1633722715463-d30f4f325e24?w=1200&q=80",

        "dog": "https://images.unsplash.com/photo-1587300003388-59208cc962cb?w=1200&q=80",

        "labrador": "https://images.unsplash.com/photo-1579213838770-ca50d594f892?w=1200&q=80",

        "husky": "https://images.unsplash.com/photo-1605568427561-40dd23c2acea?w=1200&q=80",

        "corgi": "https://images.unsplash.com/photo-1612536057832-2b78b096076e?w=1200&q=80",

        "poodle": "https://images.unsplash.com/photo-1616149562384-01890ea39022?w=1200&q=80",

        "beagle": "https://images.unsplash.com/photo-1505628346881-b72b27e84530?w=1200&q=80",

        "bulldog": "https://images.unsplash.com/photo-1583511655857-d19b40a7a54e?w=1200&q=80",

        "german shepherd": "https://images.unsplash.com/photo-1589941013453-ec89f33b5e95?w=1200&q=80",

        # Cats

        "cat": "https://images.unsplash.com/photo-1514888286974-6c03e2ca1dba?w=1200&q=80",

        "kitten": "https://images.unsplash.com/photo-1574158622682-e40e69881006?w=1200&q=80",

        "persian cat": "https://images.unsplash.com/photo-1573865526739-10659fec78a5?w=1200&q=80",

        # Rabbits

        "rabbit": "https://images.unsplash.com/photo-1585110396000-c9ffd4e4b308?w=1200&q=80",

        "bunny": "https://images.unsplash.com/photo-1535295972055-1c762f4483e5?w=1200&q=80",

        # Birds

        "parrot": "https://images.unsplash.com/photo-1552728089-57bdde30beb3?w=1200&q=80",

        "bird": "https://images.unsplash.com/photo-1444464666168-49d633b86797?w=1200&q=80",

        # Horses

        "horse": "https://images.unsplash.com/photo-1553284965-83fd3e82fa5a?w=1200&q=80",

        "pony": "https://images.unsplash.com/photo-1598974357801-cbca100e65d3?w=1200&q=80",

        # Cows/Cattle

        "cow": "https://images.unsplash.com/photo-1570042225831-d98fa7577f1e?w=1200&q=80",

        "cattle": "https://images.unsplash.com/photo-1546458656-83b86556a884?w=1200&q=80",

        # Sheep/Goats

        "sheep": "https://images.unsplash.com/photo-1484552078227-5a7285f66306?w=1200&q=80",

        "goat": "https://images.unsplash.com/photo-1524024973431-2ad916746881?w=1200&q=80",

        # Pigs

        "pig": "https://images.unsplash.com/photo-1516467508483-a7212febe31a?w=1200&q=80",

        # Hamsters/Guinea pigs

        "hamster": "https://images.unsplash.com/photo-1425082661507-d6d2f6a11844?w=1200&q=80",

        "guinea pig": "https://images.unsplash.com/photo-1548767797-d8c844163c4c?w=1200&q=80",

        # Exotic

        "ferret": "https://images.unsplash.com/photo-1615266895738-11f1371cd7e5?w=1200&q=80",

        "reptile": "https://images.unsplash.com/photo-1504450874802-0ba2bcd659e3?w=1200&q=80",

        "snake": "https://images.unsplash.com/photo-1531386816498-818d35661c93?w=1200&q=80",

        "lizard": "https://images.unsplash.com/photo-1504450874802-0ba2bcd659e3?w=1200&q=80",

        "turtle": "https://images.unsplash.com/photo-1437622645530-1885ce061ad6?w=1200&q=80",

        # Farm

        "chicken": "https://images.unsplash.com/photo-1548550023-2bdb3c5beed7?w=1200&q=80",

        "duck": "https://images.unsplash.com/photo-1555852095-64e7428df0fa?w=1200&q=80",

    }

    

    candidates = []

    

    # 1. Exact or partial match

    for keyword, url in animal_urls.items():

        if keyword in query_lower or query_lower in keyword:

            if url not in candidates:

                candidates.append(url)

    

    # 2. If no match, try individual word matching

    if not candidates:

        words = query_lower.split()

        for word in words:

            for keyword, url in animal_urls.items():

                if word in keyword or keyword in word:

                    if url not in candidates:

                        candidates.append(url)

    

    # 3. Fallback: try Unsplash source redirect (may work for some queries)

    if not candidates:

        try:

            encoded = urllib.parse.quote(query + " animal")

            fallback_url = f"https://source.unsplash.com/1200x800/?{encoded}"

            # Just add it, the download step will verify

            candidates.append(fallback_url)

        except Exception:

            pass

    

    print(f"[info] Unsplash search: {len(candidates)} candidates for '{query}'")

    return candidates







def _search_animal_pixabay(query: str) -> list:

    """Pixabay API 备用搜索（免费，需key，这里用公共demo key受限）"""

    # Pixabay 不需要key也能通过网页搜索，但API需要key

    # 改用开放的 Openverse API (WordPress)，无需key

    api_url = "https://api.openverse.org/v1/images/"

    params = {

        "q": f"{query} animal",

        "license_type": "all",

        "page_size": 10,

        "mature": "false",

    }

    try:

        resp = _http_get_with_retry(api_url, params=params, timeout=30, retries=2,

                                    headers={"User-Agent": "RHC-Marketing/1.0"})

        data = resp.json()

        candidates = []

        for item in data.get("results", []):

            url = item.get("url") or item.get("thumbnail")

            if url and item.get("width", 0) >= 400:

                candidates.append(url)

        return candidates

    except Exception as e:

        print(f"[warn] Openverse search failed: {e}")

        return []





def search_animal_image(prompt: str) -> bytes:

    """搜索动物图片并下载，返回二进制；Wikimedia为主，Openverse为备"""

    # 先翻译为英文搜索词

    query = _translate_to_english(prompt)

    print(f"[info] searching animal (query='{query}')")



    candidates = []

    # 尝试 Wikimedia（3次重试）

    try:

        candidates = _search_animal_unsplash(query)

        print(f"[info] Unsplash: {len(candidates)} candidates")

    except Exception as e:

        print(f"[warn] Unsplash search failed: {e}")



    # Wikimedia 无结果时尝试 Openverse

    if not candidates:

        candidates = _search_animal_pixabay(query)

        print(f"[info] Openverse: {len(candidates)} candidates")



    if not candidates:

        raise RuntimeError(f"No images found for: {query} (both sources failed)")



    # 逐个尝试下载（用 urllib 绕过国内对 httpx 的连接限制）

    import urllib.request as _urllib

    for img_url in candidates[:5]:

        try:

            print(f"[info] downloading from: {img_url[:80]}")

            req = _urllib.Request(img_url, headers={

                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",

                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",

                "Referer": "https://unsplash.com/",

            })

            resp = _urllib.urlopen(req, timeout=30)

            data = resp.read()

            print(f"[info] downloaded {len(data)} bytes from {img_url[:60]}")

            if len(data) > 5000:

                return data

        except Exception as e:

            print(f"[warn] download failed for {img_url[:60]}: {type(e).__name__}: {e}")

            continue



    raise RuntimeError(f"Failed to download any image for: {query}")





def _call_coze_animal_workflow(prompt: str) -> str:
    """调用 Coze 动物素材工作流（生成纯白影棚底动物图），返回图片URL。"""
    from app.config import settings
    pat = settings.COZE_PAT
    workflow_id = getattr(settings, "COZE_ANIMAL_WORKFLOW_ID", "") or ""
    if not pat:
        raise RuntimeError("Coze PAT 未配置，请在配置中心检查密钥设置")
    if not workflow_id:
        raise RuntimeError("动物素材工作流 ID 未配置，请在配置中心检查")
    api_url = "https://api.coze.cn/v1/workflow/run"
    payload = {"workflow_id": workflow_id, "parameters": {"style": prompt.strip()}}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_url, data=body,
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"调用 Coze 动物工作流失败（{type(e).__name__}）：{e}")
    code = result.get("code")
    if code != 0:
        msg = result.get("msg", "") or result.get("message", "")
        raise RuntimeError(f"Coze 动物工作流返回错误（code={code}）：{msg}")
    data_str = result.get("data", "")
    if isinstance(data_str, str):
        if not data_str.strip():
            raise RuntimeError("Coze 动物工作流返回为空，可能是生成超时，请稍后重试")
        data_obj = json.loads(data_str)
    else:
        data_obj = data_str
    output_list = (data_obj or {}).get("output", [])
    urls = [u for u in output_list if isinstance(u, str) and u.startswith("http")]
    if not urls:
        raise RuntimeError("Coze 动物工作流没有返回图片地址")
    return urls[0]


def _white_to_transparent(img):
    """纯白影棚背景 -> 透明RGBA（封边泛洪法）。
    思路：
    1) 明确前景屏障（暖色毛/彩色毛/深色毛尖）膨胀成封闭"堤坝"，挡住卷毛梢缝隙；
    2) 堤坝外（身体外）高亮低饱和区域从边界泛洪删除，地面影子用宽色温判据在环外清除；
    3) 堤坝内（身体内）毛褶阴影全部保留，仅清纯背景色的被困口袋（腿间雾斑）；
    4) 孤立暖色雾点（不邻近毛尖）先做密度过滤剔除，避免地面暖雾被误当毛保护。
    这样白毛（受光冷白也和背景同色）被堤坝围在体内不被灌空，影子/雾斑在体外被清除。"""
    import numpy as np
    from PIL import ImageFilter
    from collections import deque as _dq
    rgba = img.convert("RGBA")
    arr = np.asarray(rgba, dtype=np.uint8)
    h, w = arr.shape[0], arr.shape[1]
    R = arr[:, :, 0].astype(np.int16)
    G = arr[:, :, 1].astype(np.int16)
    B = arr[:, :, 2].astype(np.int16)
    mx = np.maximum(np.maximum(R, G), B)
    mn = np.minimum(np.minimum(R, G), B)
    sat = mx - mn

    is_bg_col = (sat <= 14) & ((B - R) >= -4) & (mn >= 150)
    is_sh_col = (sat <= 16) & ((B - R) >= -6) & (mn >= 50) & (mn < 225)

    # 屏障：暖毛/彩毛/深毛尖；暖点需邻近毛尖（5x5内屏障像素>=3），孤立暖雾点剔除
    warm = (R - B) >= 3
    strong = (sat > 14) | (mn < 150)
    barrier_raw = (warm | strong) & (~is_sh_col)
    dens = np.asarray(
        Image.fromarray((barrier_raw * 255).astype(np.uint8))
        .filter(ImageFilter.BoxBlur(2))
    )
    barrier = barrier_raw & (strong | (dens >= 31))  # 31/255≈3/25邻域密度

    # 封边环：屏障膨胀4px，堵死毛梢缝隙
    seal = np.asarray(
        Image.fromarray((barrier * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(9))
    ) > 0
    sh_out = (sat <= 18) & ((B - R) >= -12) & (mn >= 40) & (mn < 225)

    # 1) 环外背景泛洪：堤坝外高亮低饱和一律删（不卡色温），seal不可通过
    bg_flood = (sat <= 16) & (mn >= 190)
    visited = np.zeros((h, w), dtype=bool)
    dq = _dq()
    def _seed(y, x):
        if bg_flood[y, x] and not seal[y, x] and not visited[y, x]:
            visited[y, x] = True
            dq.append((y, x))
    for x in range(w):
        _seed(0, x); _seed(h - 1, x)
    for y in range(h):
        _seed(y, 0); _seed(y, w - 1)
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and not seal[ny, nx] and bg_flood[ny, nx]:
                visited[ny, nx] = True
                dq.append((ny, nx))
    transparent = visited.copy()

    # 2) 环外影子：冷灰/暖灰影子与外部连通，触边或游离小块删除（环内毛褶阴影被堤坝隔断保留）
    sh_lab = np.zeros((h, w), dtype=np.int32)
    cur = 0
    for sy in range(h):
        for sx in range(w):
            if sh_out[sy, sx] and not seal[sy, sx] and not transparent[sy, sx] and sh_lab[sy, sx] == 0:
                cur += 1
                q = _dq([(sy, sx)]); sh_lab[sy, sx] = cur; cnt = 0; touches = False
                while q:
                    y, x = q.popleft(); cnt += 1
                    if y == 0 or y == h - 1 or x == 0 or x == w - 1:
                        touches = True
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < h and 0 <= nx < w and not seal[ny, nx] and sh_out[ny, nx] and sh_lab[ny, nx] == 0:
                            sh_lab[ny, nx] = cur; q.append((ny, nx))
                if touches or cnt < 30000:
                    ys_l, xs_l = np.where(sh_lab == cur)
                    transparent[ys_l, xs_l] = True

    # 3) 环内被困口袋：堤坝内的冷白/冷灰/高亮雾连通域，纯背景(屏障占比<2%)且小块才删
    pocket_seed = (~transparent) & (~seal) & (is_bg_col | is_sh_col | ((sat <= 16) & (mn >= 190)))
    p_lab = np.zeros((h, w), dtype=np.int32)
    cur2 = 0
    for sy in range(h):
        for sx in range(w):
            if pocket_seed[sy, sx] and p_lab[sy, sx] == 0:
                cur2 += 1
                q = _dq([(sy, sx)]); p_lab[sy, sx] = cur2; cnt = 0; bcnt = 0
                while q:
                    y, x = q.popleft(); cnt += 1
                    if barrier[y, x]:
                        bcnt += 1
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < h and 0 <= nx < w and not seal[ny, nx] and pocket_seed[ny, nx] and p_lab[ny, nx] == 0:
                            p_lab[ny, nx] = cur2; q.append((ny, nx))
                if bcnt / max(cnt, 1) < 0.02 and cnt < 80000:
                    ys_l, xs_l = np.where(p_lab == cur2)
                    transparent[ys_l, xs_l] = True

    alpha = np.where(transparent, 0, 255).astype(np.uint8)
    # 腐蚀2px收掉封边环带出的外圈白边
    alpha = np.asarray(Image.fromarray(alpha).filter(ImageFilter.MinFilter(5)))
    out = np.dstack([arr[:, :, :3], alpha])
    return Image.fromarray(out, "RGBA")


def generate_animal_cutout(prompt: str, api_key: str = "") -> dict:
    """方案C：Coze工作流按描述生成纯白底动物图 -> 本地去白底 -> 透明PNG。
    AI路径失败时降级到旧图库/rembg路径（仅当旧路径真的产出透明底才采用）。"""
    result = {"status": "success", "log": [], "image_url": "", "cutout_ok": False}
    prompt = (prompt or "").strip()
    if not prompt:
        result["status"] = "failed"
        result["error"] = "请先填写动物描述后再生成"
        return result

    # 主路径：Coze AI 工作流
    ai_error = ""
    try:
        result["log"].append(f"[info] AI generating animal for prompt='{prompt}'")
        img_url = _call_coze_animal_workflow(prompt)
        result["log"].append(f"[ok] workflow returned: {img_url[:90]}")
        import httpx
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            resp = client.get(img_url)
            resp.raise_for_status()
            img_bytes = resp.content
        result["log"].append(f"[ok] downloaded {len(img_bytes)} bytes")
        from PIL import Image
        import io as _io
        import os as _os
        import time as _t
        base_img = Image.open(_io.BytesIO(img_bytes))
        cut_img = _white_to_transparent(base_img)
        # 优先保存到后端本地 uploads 静态目录（同域，无第三方依赖）；失败再降级 freeimage
        cutout_url = ""
        try:
            _uploads_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "uploads")
            _os.makedirs(_uploads_dir, exist_ok=True)
            _fname = f"animal_ai_{int(_t.time()*1000)}.png"
            cut_img.save(_os.path.join(_uploads_dir, _fname), format="PNG")
            cutout_url = f"/uploads/{_fname}"
            result["log"].append(f"[ok] white background removed, saved to local uploads: {_fname}")
        except Exception as _le:
            result["log"].append(f"[warn] local save failed ({_le}), trying freeimage")
            _buf = _io.BytesIO()
            cut_img.save(_buf, format="PNG")
            cutout_url = _upload_bytes_to_freeimage(_buf.getvalue(), f"animal_ai_{int(_t.time())}.png")
            result["log"].append(f"[ok] uploaded to freeimage: {cutout_url[:90]}")
        result["image_url"] = cutout_url
        result["cutout_ok"] = True
        return result
    except Exception as e:
        ai_error = f"AI 动物生成失败：{e}"
        result["log"].append(f"[error] {ai_error}")

    # 降级路径：旧硬编码图库 + rembg（只有真抠出透明底才算数）
    try:
        result["log"].append("[info] trying legacy image library fallback")
        legacy = _generate_animal_cutout_legacy(prompt=prompt, api_key=api_key)
        if legacy.get("cutout_ok") and legacy.get("image_url"):
            result["image_url"] = legacy["image_url"]
            result["cutout_ok"] = True
            result["log"].append("[ok] legacy fallback produced a transparent cutout")
            return result
        result["log"].append("[warn] legacy fallback did not produce a transparent cutout either")
    except Exception as e:
        result["log"].append(f"[warn] legacy fallback error: {e}")

    result["status"] = "failed"
    result["error"] = ai_error or "动物素材生成失败，请稍后重试"
    return result


def _generate_animal_cutout_legacy(prompt: str, api_key: str = "") -> dict:
    """Search animal image, remove background via rembg, upload transparent PNG."""
    import random
    result = {"status": "success", "log": [], "image_url": "", "cutout_ok": False}
    try:
        # Step 1: Translate to English for better matching
        query = _translate_to_english(prompt)
        result["log"].append(f"[info] searching animal (query='{query}')")

        # Step 2: Search - returns a LIST of URLs
        candidates = _search_animal_unsplash(query)
        if not candidates:
            result["status"] = "failed"
            result["log"].append("[error] no animal image found")
            return result
        result["log"].append(f"[ok] found {len(candidates)} candidates")

        # Step 3: Randomly pick one
        img_url = random.choice(candidates)
        result["log"].append(f"[ok] picked: {img_url[:80]}")

        # Step 4: Download the image
        import httpx
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(img_url)
            resp.raise_for_status()
            img_bytes = resp.content
        result["log"].append(f"[ok] downloaded {len(img_bytes)} bytes")

        # Step 5: Try rembg background removal
        cutout_url = None
        try:
            from rembg import remove
            from PIL import Image
            import io as _io
            input_img = Image.open(_io.BytesIO(img_bytes))
            output_img = remove(input_img)
            buf = _io.BytesIO()
            output_img.save(buf, format="PNG")
            cutout_bytes = buf.getvalue()
            result["log"].append(f"[ok] rembg cutout done, {len(cutout_bytes)} bytes")
            cutout_url = _upload_bytes_to_freeimage(cutout_bytes, f"animal_{int(__import__('time').time())}.png")
            result["log"].append(f"[ok] uploaded cutout: {cutout_url[:80]}")
        except ImportError:
            result["log"].append("[warn] rembg not installed, returning original image")
        except Exception as e:
            result["log"].append(f"[warn] cutout failed ({e}), returning original image")

        # Step 6: Return cutout or fallback original
        result["image_url"] = cutout_url or ""
        result["cutout_ok"] = bool(cutout_url)
    except Exception as e:
        result["status"] = "failed"
        result["log"].append(f"[error] {str(e)}")
        import traceback
        result["log"].append(traceback.format_exc())
    return result

def compose_image(
    animal: str = "",
    product_id: str = "",
    style: str = "professional",
    text: str = ""
) -> dict:

    """Compose product image with AI background via Coze workflow."""

    result = generate_ai_background(prompt=text, style=style)

    return result

