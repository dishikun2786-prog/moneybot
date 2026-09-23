"""图形验证码生成: Pillow PNG (优先) / SVG (无依赖回退)
4 位字符 (剔除易混淆 0O1I), 旋转+噪点+干扰线
"""
import io
import random

_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Linux 服务器
    "C:/Windows/Fonts/arialbd.ttf",                            # Windows 本地
    "C:/Windows/Fonts/consolab.ttf",
]


def _rand_color(a, b):
    return tuple(random.randint(a, b) for _ in range(3))


def gen():
    """返回 (code, image_bytes, mime)"""
    code = "".join(random.choices(_CHARS, k=4))
    try:
        return code, _png(code), "image/png"
    except ImportError:
        return code, _svg(code).encode(), "image/svg+xml"


def _png(code):
    from PIL import Image, ImageDraw, ImageFont
    W, H = 120, 44
    img = Image.new("RGB", (W, H), (24, 28, 36))
    d = ImageDraw.Draw(img)
    font = None
    for fp in _FONT_PATHS:
        try:
            font = ImageFont.truetype(fp, 26)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    colors = [(247, 166, 0), (62, 189, 147), (236, 238, 243), (246, 70, 93)]
    for i, ch in enumerate(code):
        x = 10 + i * 27
        # 单字符小图旋转后粘贴
        cimg = Image.new("RGBA", (34, 40), (0, 0, 0, 0))
        cd = ImageDraw.Draw(cimg)
        cd.text((5, 4), ch, font=font, fill=colors[i % 4])
        cimg = cimg.rotate(random.uniform(-28, 28), expand=True, resample=Image.BICUBIC)
        img.paste(cimg, (x, random.randint(2, 8)), cimg)
    for _ in range(4):
        d.line([(random.randint(0, W), random.randint(0, H)),
                (random.randint(0, W), random.randint(0, H))],
               fill=_rand_color(60, 140), width=1)
    for _ in range(60):
        d.point((random.randint(0, W), random.randint(0, H)),
                fill=_rand_color(80, 200))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _svg(code):
    colors = ["#f7a600", "#3ebd93", "#eceef3", "#f6465d"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="44">',
             '<rect width="120" height="44" rx="8" fill="#181c24"/>']
    for i, ch in enumerate(code):
        rot = random.randint(-24, 24)
        x = 14 + i * 26
        y = 30
        parts.append(f'<text x="{x}" y="{y}" font-size="26" font-family="monospace" '
                     f'font-weight="bold" fill="{colors[i % 4]}" '
                     f'transform="rotate({rot} {x} {y})">{ch}</text>')
    for _ in range(4):
        x1, y1 = random.randint(0, 120), random.randint(0, 44)
        x2, y2 = random.randint(0, 120), random.randint(0, 44)
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     'stroke="rgba(154,163,178,.5)" stroke-width="1"/>')
    parts.append("</svg>")
    return "".join(parts)
