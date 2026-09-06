#!/usr/bin/env python3
"""
MiniMovie 封面生成器 v18
核心：
  1. 真实检脸（animeface 级联）+ 皮肤显著度 → 按「整块内容矩形 vs 脸框」交集避让定落点；**底部优先**（前景最干净），仅在角落避让显著更优时才偏离（允许 2.1 式中央大脸用顶排）
  2. 采样「文字真实落点」的亮度/饱和度，计入底部暗化层得到有效亮度
  3. 按有效亮度 + 色散定深浅，再按落点背景主色派生「同色系·高饱和」文字色（和谐又醒目，不写死）
  4. 主标题定色，副标题由主标题派生（同色系、明度微调 → 色差可控）
  5. 字号按实际文字动态计算，Playwright 实测收敛到目标占比
"""

import argparse
import base64
import os
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np

MIN_WIDTH = 1920
MIN_HEIGHT = 1080
SCRIPT_DIR = Path(__file__).parent
TEMPLATE_DIR = SCRIPT_DIR.parent / "templates"
MODELS_DIR = SCRIPT_DIR.parent / "models"
FACE_CASCADE = MODELS_DIR / "lbpcascade_animeface.xml"   # 真实动漫脸检测级联（animeface）

# ===== 自适应调参（嫌太白 / 太花 / 变化不够时，改这里即可）=====
FIT_TARGET_RATIO = 0.70      # 主标题宽度占图宽目标（60~80% 区间都行）
SHORT_TITLE_H_RATIO = 0.30   # 短标题字号封顶 = 图高 × 此比例
COLOR_BRIGHT_LUMA = 150      # 有效亮度 ≥ 此值为「亮区」（之上再按色散分白/深）
COLOR_MID_LUMA = 100         # 有效亮度 ≥ 此值为「中灰」→ 白字；低于则进暗区 accent
COLOR_BUSY_SPREAD = 38       # 色散 ≥ 此值为「杂乱」→ 亮区杂时强制白字兜底
ACCENT_ON_DARK = True        # 暗区是否用亮 accent（False 则暗区也回白字，更素净）
SUB_LIGHTEN_RATIO = 0.35     # 副标题往白调比例（<0.5 则与主标题色差更小）
SCENE_SAT_FLOOR = 0.68       # 同色系文字色饱和度下限（越高越醒目）
SCENE_SAT_BOOST = 1.7        # 场景饱和度放大倍率（低饱和场景也能长出鲜明色相）
SCENE_SAT_CAP = 0.90         # 饱和度上限（防止过艳刺眼）
SCENE_LIGHT_L = 0.73         # 暗底同色系浅字的目标明度
SCENE_DARK_L = 0.13          # 亮底同色系深字的目标明度
SKIN_PENALTY = 25.0          # 皮肤显著度权重：把「平滑但显眼」的脸/身体也计入内容（防级联漏检时字压肤）
BOTTOM_PREF_TOL = 0.35       # 底部优先（安全默认）：bottom-center 避让重叠比最优差 ≤（重叠范围×此比例）就选底部，避免「密度反了」导致贴角色


def check_image_size(path):
    from PIL import Image
    img = Image.open(path)
    return img.size


def _cover_canvas(bg_path):
    """background: cover → 1920x1080 画布。尺寸不足则报错。分析与渲染共用同一坐标系。"""
    from PIL import Image

    img = Image.open(bg_path).convert("RGB")
    w, h = img.size

    if w < MIN_WIDTH or h < MIN_HEIGHT:
        print(f"错误: 图片尺寸 {w}×{h} 低于 {MIN_WIDTH}×{MIN_HEIGHT}，请换图", file=sys.stderr)
        sys.exit(1)

    scale = max(MIN_WIDTH / w, MIN_HEIGHT / h)
    new_w = int(w * scale)
    new_h = int(h * scale)

    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - MIN_WIDTH) // 2
    top = (new_h - MIN_HEIGHT) // 2
    return img.crop((left, top, left + MIN_WIDTH, top + MIN_HEIGHT))


def prepare_background(bg_path, output_path):
    """background: cover → 导出 PNG"""
    _cover_canvas(bg_path).save(output_path, "PNG")


def compute_content_map(pixels):
    """计算 5x3 网格的内容密度"""
    import numpy as np

    gray = 0.299 * pixels[:,:,0].astype(np.float32) + \
           0.587 * pixels[:,:,1].astype(np.float32) + \
           0.114 * pixels[:,:,2].astype(np.float32)

    grad_x = np.abs(np.diff(gray, axis=1))
    grad_y = np.abs(np.diff(gray, axis=0))
    edge = np.zeros((270, 480), dtype=np.float32)
    edge[:269, :479] = grad_x[:269, :479] + grad_y[:269, :479]

    r, g, b = pixels[:,:,0].astype(np.float32), pixels[:,:,1].astype(np.float32), pixels[:,:,2].astype(np.float32)
    color_var = (np.abs(r - g) + np.abs(g - b) + np.abs(b - r)) / 3

    content = edge * 0.5 + color_var * 0.5

    grid = np.zeros((3, 5), dtype=np.float32)
    for gy in range(3):
        for gx in range(5):
            y1, y2 = gy * 90, min((gy + 1) * 90, 269)
            x1, x2 = gx * 96, min((gx + 1) * 96, 479)
            grid[gy, gx] = np.mean(content[y1:y2, x1:x2])

    return grid


def _char_units(ch):
    """估算单个字符的宽度（以 em 为单位），用于动态计算字号。

    CJK 汉字 / 全角标点 ≈ 1em；数字 / 小写字母 ≈ 0.55em；大写 ≈ 0.62em；空格 ≈ 0.4em。
    """
    if ch == " ":
        return 0.4
    if ch.isascii():
        return 0.55 if (ch.isdigit() or ch.islower()) else 0.62
    return 1.0


def _darken_alpha(frac):
    """模板底部暗化层的 alpha，按文字竖向位置 frac = 文字中心 y / 图高 做线性插值。

    梯度（cover.html .darken）：
      0%:0.05  20%:0  50%:0  80%:0.45  100%:0.65
    """
    if frac <= 0.20:
        return 0.05 * (1 - frac / 0.20)
    if frac <= 0.50:
        return 0.0
    if frac <= 0.80:
        return 0.45 * (frac - 0.50) / 0.30
    return 0.45 + 0.20 * (frac - 0.80) / 0.20


def _mix_white(hex_color, ratio):
    """把十六进制颜色向白色按 ratio(0~1) 混合，得到同色系但更亮的颜色。"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    nr = int(round(r + (255 - r) * ratio))
    ng = int(round(g + (255 - g) * ratio))
    nb = int(round(b + (255 - b) * ratio))
    return f"#{nr:02X}{ng:02X}{nb:02X}"


def _subtitle_color(title_color):
    """副标题与主标题同一色系，明度略调以区分层级，色差可控（区别于「主标题 accent / 副标题纯白」的大反差）。"""
    if title_color == "#FFFFFF":
        return "#FFFFFF"                 # 白主标 → 白副标
    if title_color == "#1A1A2E":
        return "#333333"                 # 深藏蓝 → 稍亮深灰（同属冷深色系）
    return _mix_white(title_color, SUB_LIGHTEN_RATIO)  # accent → 同色系浅调，仍可读、与主标题色差小


def _scene_hex(rgb):
    """(r,g,b) 0~255 → '#RRGGBB'"""
    r, g, b = rgb
    return f"#{int(round(r)):02X}{int(round(g)):02X}{int(round(b)):02X}"


def _scene_text_color(rgb, light):
    """从背景主色派生「同色系」文字色：色相取自场景本身（和谐），对比靠明度——暗底提亮、亮底压暗。
    light=True → 提亮（暗底用的浅字）；light=False → 压暗（亮底用的深字）。
    高饱和（醒目）但保底不过艳：低饱和场景也能长出鲜明的色相，低明度避免浮白。"""
    import colorsys
    r, g, b = rgb[0] / 255, rgb[1] / 255, rgb[2] / 255
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    target_l = SCENE_LIGHT_L if light else SCENE_DARK_L
    sat = min(max(s * SCENE_SAT_BOOST, SCENE_SAT_FLOOR), SCENE_SAT_CAP)
    nr, ng, nb = colorsys.hls_to_rgb(h, target_l, sat)
    return (nr * 255, ng * 255, nb * 255)


def _detect_face_boxes(canvas):
    """真实动漫脸检测（lbpcascade_animeface），返回 1920x1080 坐标系的 (x,y,w,h) 列表。"""
    import cv2
    import numpy as np
    cascade = cv2.CascadeClassifier(str(FACE_CASCADE))
    g = cv2.cvtColor(np.array(canvas), cv2.COLOR_RGB2GRAY)
    # 放宽参数（更小 scaleFactor / 更小 minNeighbors / 更大输入）以抓到更多动漫脸，
    # 少量误检会带来保守避让（宁可多避不进脸），可接受。
    scale = 2400 / max(g.shape)
    gs = cv2.resize(g, (int(g.shape[1] * scale), int(g.shape[0] * scale)))
    boxes = cascade.detectMultiScale(gs, 1.05, 3)
    return [(x / scale, y / scale, w / scale, h / scale) for x, y, w, h in boxes]


def _rect_face_overlap(rect, boxes):
    """候选标题矩形与所有脸框的「交集面积」之和（越小越避脸）。"""
    x0, y0, x1, y1 = rect
    area = 0.0
    for bx, by, bw, bh in boxes:
        ix = max(0, min(x1, bx + bw) - max(x0, bx))
        iy = max(0, min(y1, by + bh) - max(y0, by))
        area += ix * iy
    return area


def _rect_content(rect, content_grid):
    """候选矩形覆盖的 3x5 网格内容密度均值（次级键）。"""
    x0, y0, x1, y1 = rect
    gx0, gx1 = min(4, int(x0 / MIN_WIDTH * 5)), min(4, int(x1 / MIN_WIDTH * 5))
    gy0, gy1 = min(2, int(y0 / MIN_HEIGHT * 3)), min(2, int(y1 / MIN_HEIGHT * 3))
    vals = [content_grid[r, c] for r in range(gy0, gy1 + 1) for c in range(gx0, gx1 + 1)]
    return float(np.mean(vals)) if vals else 0.0


def _skin_grid(pixels):
    """肤色占比网格（3x5）：把「平滑但显眼」的脸/身体标成重要区域，防级联漏检时字压到皮肤。"""
    R = pixels[:,:,0].astype(np.float32)
    G = pixels[:,:,1].astype(np.float32)
    B = pixels[:,:,2].astype(np.float32)
    skin = ((R > G + 8) & (G > B + 4) & ((R - B) > 18) & ((R - B) < 110) & (R > 120)).astype(np.float32)
    grid = np.zeros((3, 5), dtype=np.float32)
    for gy in range(3):
        for gx in range(5):
            grid[gy, gx] = skin[gy*90:(gy+1)*90, gx*96:(gx+1)*96].mean()
    return grid


def _candidate_rects(content_w, content_h):
    """各候选落点、「整块内容（主标题+副标题+装饰线）」矩形（1920x1080 坐标），与最终渲染位置对齐。"""
    W, H = MIN_WIDTH, MIN_HEIGHT
    cx0, cx1 = (W - content_w) / 2, (W - content_w) / 2 + content_w
    lx0, lx1 = 100, 100 + content_w
    rx1, rx0 = W - 100, W - 100 - content_w
    midy = H / 2

    def top(x0, x1):
        return (x0, 100, x1, 100 + content_h)      # 顶对齐 top:100px

    def mid(x0, x1):
        return (x0, midy - content_h / 2, x1, midy + content_h / 2)

    return {
        "top-left": top(lx0, lx1),
        "top-center": top(cx0, cx1),
        "top-right": top(rx0, rx1),
        "mid-left": mid(lx0, lx1),
        "mid-center": mid(cx0, cx1),
        "mid-right": mid(rx0, rx1),
        "bottom-center": (cx0, H - 100 - content_h, cx1, H - 100),   # 底对齐 bottom:100px
    }


def _empty_side(prominence_grid):
    """判断画面哪一侧更空（角色集中在对侧 → 文字放此空侧）。
    返回 'right' / 'left' / 'none'。用左右各半显著度对比，避免「内容密度反了」的坑。"""
    left_p = float(np.mean(prominence_grid[:, 0:2]))
    right_p = float(np.mean(prominence_grid[:, 3:5]))
    if left_p <= 0 or right_p <= 0:
        return "none"
    if right_p < left_p * 0.65:
        return "right"
    if left_p < right_p * 0.65:
        return "left"
    return "none"


def _side_rects(side, content_h, max_w):
    """在空侧生成「窄化、贴边对齐」的候选矩形，宽度裁剪到不压到对侧角色。
    返回 (rects_dict, width_px)。"""
    W = MIN_WIDTH
    width = int(min(max_w, W * 0.60))
    width = max(int(W * 0.42), width)
    if side == "right":
        x1 = W - 100
        x0 = x1 - width
    else:
        x0 = 100
        x1 = x0 + width

    def top(): return (x0, 100, x1, 100 + content_h)
    def mid(): return (x0, 1080 / 2 - content_h / 2, x1, 1080 / 2 + content_h / 2)
    def bot(): return (x0, 1080 - 100 - content_h, x1, 1080 - 100)
    rects = {
        f"{side}-top": top(),
        f"{side}-mid": mid(),
        f"{side}-bottom": bot(),
    }
    return rects, width


def analyze_image(bg_path, title):
    """分析图片：真实检脸 → 按「标题矩形 vs 脸框」交集避让定落点 → 采样该区 → 按有效亮度反推颜色。"""
    from PIL import Image
    import numpy as np

    canvas = _cover_canvas(bg_path)                     # 1920x1080 画面（与最终渲染同一坐标系）
    small = canvas.resize((480, 270), Image.LANCZOS)
    pixels = np.array(small, dtype=np.uint8)
    content_grid = compute_content_map(pixels)
    face_boxes = _detect_face_boxes(canvas)             # 真实人脸框（1920x1080 坐标）

    # 显著度 = 内容密度 + 皮肤权重：平滑的脸/身体（边缘少）也计为重要，防级联漏检时压到皮肤
    prominence_grid = content_grid + SKIN_PENALTY * _skin_grid(pixels)

    # 字号不依赖落点，先算（供候选矩形宽高）
    target_width = int(MIN_WIDTH * FIT_TARGET_RATIO)
    units = sum(_char_units(c) for c in title)
    title_size = max(48, min(320, int(target_width / max(units, 1))))
    sub_size = int(title_size * 0.5)
    content_w = target_width
    content_h = int(title_size * 1.8 + 60)              # 主标题 + 副标题 + 装饰线 + 边距，盖满整块

    # === 第一步：定落点。候选 = 整块内容矩形 + 「空侧窄贴边」矩形；主键=与脸框交集面积（越小越避脸），次键=区域显著度 ===
    seed = zlib.crc32(bg_path.encode())
    cands = _candidate_rects(content_w, content_h)
    side = _empty_side(prominence_grid)
    side_w = None
    if side != "none":
        svec, side_w = _side_rects(side, content_h, max_w=content_w)
        cands.update(svec)
    score = {
        k: (_rect_face_overlap(r, face_boxes), _rect_content(r, prominence_grid))
        for k, r in cands.items()
    }
    min_ov = min(v[0] for v in score.values())
    max_ov = max(v[0] for v in score.values())
    bottom_ov = score["bottom-center"][0]
    # 1) 明显空侧 → 优先把字放角色对侧（前提：空侧矩形避让不差于底部前景）
    align = None
    if side != "none" and side_w is not None:
        sk = [k for k in score if k.startswith(side)]
        bside = min(sk, key=lambda k: (score[k][0], score[k][1]))
        if score[bside][0] <= bottom_ov + 1e-6:
            align = bside
    # 2) 底部优先（安全默认，防「密度反了」贴角色），仅当某角落避让显著更优才偏离（如 2.1 中央大脸）
    if align is None:
        if bottom_ov <= min_ov + (max_ov - min_ov) * BOTTOM_PREF_TOL + 1.0:
            align = "bottom-center"
        else:
            eps = max(2.0, (max_ov - min_ov) * 0.15)
            sel = [k for k, v in score.items() if v[0] <= min_ov + eps]
            align = sorted(sel, key=lambda k: (score[k][0], score[k][1], seed % 7))[0]
    vb = align.split("-")[0]
    if vb not in ("top", "mid", "bottom"):
        vb = align.split("-")[1]

    # 渲染用主标题目标宽：侧放取窄宽，其余取全宽（autofit 依此收敛字号）
    render_w = side_w if (side != "none" and align.startswith(side)) else content_w
    title_size = max(48, min(320, int(render_w / max(units, 1))))
    sub_size = int(title_size * 0.5)

    # === 第二步：采样该落点横带的亮度 / 色散 / 颜色 ===
    row = {"top": 0, "mid": 1, "bottom": 2}[vb]
    band_px = pixels[row * 90:(row + 1) * 90, :]
    luma = 0.299 * band_px[:,:,0] + 0.587 * band_px[:,:,1] + 0.114 * band_px[:,:,2]
    region_brightness = float(np.mean(luma))
    spread = float(np.std(luma))             # 色散：越大说明越杂（角色/纹理/明暗交错）
    avg_r = float(np.mean(band_px[:,:,0]))
    avg_g = float(np.mean(band_px[:,:,1]))
    avg_b = float(np.mean(band_px[:,:,2]))
    max_c = max(avg_r, avg_g, avg_b)
    min_c = min(avg_r, avg_g, avg_b)
    saturation = (max_c - min_c) / max_c if max_c > 0 else 0

    frac = {"top": 0.16, "mid": 0.5, "bottom": 0.77}[vb]   # 文字竖向中心（供暗化层计算）
    effective = region_brightness * (1 - _darken_alpha(frac))

    # === 第三步：先定主标题色（同色系派生，对比靠明度），副标题由它派生（色差可控）===
    # 色相取自落点场景本身 → 与画面和谐；杂乱区退回白字兜底
    scene_rgb = (avg_r, avg_g, avg_b)
    if effective < COLOR_MID_LUMA:
        # 暗底 → 同色系浅字（提亮后的场景色），用明度拉开对比
        title_color = _scene_hex(_scene_text_color(scene_rgb, light=True)) if ACCENT_ON_DARK else "#FFFFFF"
    elif effective >= COLOR_BRIGHT_LUMA:
        # 亮区
        if spread < COLOR_BUSY_SPREAD:
            title_color = _scene_hex(_scene_text_color(scene_rgb, light=False))  # 均匀亮 → 同色系深字
        else:
            title_color = "#FFFFFF"   # 亮但杂乱（角色/纹理交错）→ 白字 + 黑描边最稳
    else:
        # 中灰 → 白字（ACCENT_ON_DARK=False 时暗区也回落到白字）
        title_color = "#FFFFFF"

    sub_color = _subtitle_color(title_color)   # 同一色系，与主标题差值可控

    left_ratio = np.sum(content_grid[:, :2]) / max(np.sum(content_grid), 1)
    right_ratio = np.sum(content_grid[:, 3:]) / max(np.sum(content_grid), 1)

    return {
        "align": align,
        "title_size": title_size,
        "sub_size": sub_size,
        "title_color": title_color,
        "sub_color": sub_color,
        "region_brightness": region_brightness,
        "effective": effective,
        "spread": spread,
        "title_width": render_w,     # 主标题目标宽（侧放为窄宽）
        "left_ratio": left_ratio,
        "right_ratio": right_ratio,
    }


# 载入页面后，依据实际渲染的文字宽度，将主标题字号收敛到目标占比（默认 70%）。
# 规则：文字太宽 → 缩小到 70%；文字不足 → 最多放大到封顶（不强迫短标题撑满 70%）。
AUTOFIT_JS = """
const title = document.querySelector('.main-title');
const sub = document.querySelector('.sub-title');
const W = __WIDTH__;
const H = __HEIGHT__;
const targetW = __TARGET_W__;          // 主标题目标宽度(px)：适合空白区（窄）/ 默认 70%
const MAX = Math.min(360, H * __MAX_H_RATIO__);   // 字号封顶（= 图高 × 比例），避免短标题爆炸
function textWd() {
  const r = document.createRange();
  r.selectNodeContents(title);
  return r.getBoundingClientRect().width || title.scrollWidth;
}
let cur = parseFloat(getComputedStyle(title).fontSize);
let w = textWd();
for (let i = 0; i < 10; i++) {
  if (w <= 0) break;
  let ns = cur * (targetW / w);        // 目标缩放（可能放大也可能缩小）
  if (ns > MAX) ns = MAX;              // 短标题撑不够 70% 时封顶，而非无限放大；缩小不受影响
  ns = Math.max(40, ns);
  if (Math.abs(ns - cur) < 0.5) break;
  cur = ns;
  title.style.fontSize = cur + 'px';
  sub.style.fontSize = Math.round(cur * 0.5) + 'px';
  w = textWd();
}
"""


def render_html_to_image(html_path, output_path, width=1920, height=1080, target_width=None):
    """Playwright 渲染，载入后按实际文字宽度自动收敛主标题字号到目标宽（默认 70% 图宽）"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"file://{html_path}", wait_until="networkidle")
        tw = int(target_width) if target_width else int(width * FIT_TARGET_RATIO)
        js_ctx = AUTOFIT_JS.replace("__WIDTH__", str(width)) \
                          .replace("__HEIGHT__", str(height)) \
                          .replace("__TARGET_W__", str(tw)) \
                          .replace("__MAX_H_RATIO__", repr(SHORT_TITLE_H_RATIO))
        page.evaluate(js_ctx)
        page.screenshot(path=output_path, type="jpeg", quality=95)
        browser.close()


def make_cover(bg_path, title, subtitle, output_path):
    """主流程"""

    w, h = check_image_size(bg_path)
    print(f"背景图尺寸: {w}×{h}")

    if w < MIN_WIDTH or h < MIN_HEIGHT:
        print(f"✗ 图片尺寸低于 {MIN_WIDTH}×{MIN_HEIGHT}，请换图", file=sys.stderr)
        sys.exit(1)

    analysis = analyze_image(bg_path, title)
    print(f"分析: 内容左={analysis['left_ratio']:.1%}, 右={analysis['right_ratio']:.1%}")
    print(f"落点: {analysis['align']}  有效亮度={analysis['effective']:.0f}")
    print(f"字号: 主标题初始={analysis['title_size']}px, 副标题={analysis['sub_size']}px")
    print(f"颜色: 主标题={analysis['title_color']}, 副标题={analysis['sub_color']}")

    processed_bg = None
    html_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            processed_bg = tmp.name

        prepare_background(bg_path, processed_bg)

        template_path = TEMPLATE_DIR / "cover.html"
        html = template_path.read_text(encoding="utf-8")

        with open(processed_bg, "rb") as f:
            bg_b64 = base64.b64encode(f.read()).decode()

        html = html.replace("SUBTITLE_PLACEHOLDER", subtitle)
        html = html.replace("TITLE_PLACEHOLDER", title)
        html = html.replace("BG_IMAGE_PLACEHOLDER", f"data:image/png;base64,{bg_b64}")
        html = html.replace("TITLE_COLOR_PLACEHOLDER", analysis["title_color"])

        # 位置（模板中是分两行的）
        align = analysis["align"]
        if align in ("bottom", "bottom-center"):
            content_style = f'bottom: 100px; left: 50%; transform: translateX(-50%); text-align: center;'
        elif align in ("top", "top-center"):
            content_style = f'top: 100px; left: 50%; transform: translateX(-50%); text-align: center;'
        elif align in ("mid", "mid-center"):
            content_style = f'top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center;'
        elif align in ("right-top", "right-mid", "right-bottom"):
            vert = {"right-top": "top: 100px", "right-mid": "top: 50%; transform: translateY(-50%)", "right-bottom": "bottom: 100px"}[align]
            content_style = f'right: 100px; {vert}; text-align: right;'
        elif align in ("left-top", "left-mid", "left-bottom"):
            vert = {"left-top": "top: 100px", "left-mid": "top: 50%; transform: translateY(-50%)", "left-bottom": "bottom: 100px"}[align]
            content_style = f'left: 100px; {vert}; text-align: left;'
        elif align == "top-left":
            content_style = f'left: 100px; top: 100px; text-align: left;'
        elif align == "top-right":
            content_style = f'right: 100px; top: 100px; text-align: right;'
        elif align == "mid-left":
            content_style = f'left: 100px; top: 50%; transform: translateY(-50%); text-align: left;'
        elif align == "mid-right":
            content_style = f'right: 100px; top: 50%; transform: translateY(-50%); text-align: right;'
        else:
            content_style = f'left: 100px; top: 100px; text-align: left;'

        # 匹配模板中的格式（分两行）
        html = html.replace('left: 100px;\n  top: 260px;', content_style)
        html = html.replace('font-size: 120px;', f'font-size: {analysis["title_size"]}px;')
        html = html.replace('font-size: 56px;', f'font-size: {analysis["sub_size"]}px;')
        html = html.replace('color: #FFFFFF;\n  text-shadow:', f'color: {analysis["sub_color"]};\n  text-shadow:')

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html",
                                         delete=False, encoding="utf-8") as f:
            f.write(html)
            html_path = f.name

        render_html_to_image(html_path, output_path, target_width=analysis["title_width"])
        print(f"✓ 封面已生成: {output_path}")

    finally:
        for p in [processed_bg, html_path]:
            if p and os.path.exists(p):
                os.unlink(p)


def main():
    parser = argparse.ArgumentParser(description="MiniMovie 封面生成器 v18")
    parser.add_argument("--bg", required=True, help="背景图片路径")
    parser.add_argument("--title", required=True, help="主标题")
    parser.add_argument("--subtitle", required=True, help="副标题")
    parser.add_argument("--output", default="cover.jpg", help="输出路径")

    args = parser.parse_args()

    if not os.path.exists(args.bg):
        print(f"错误: 文件不存在: {args.bg}", file=sys.stderr)
        sys.exit(1)

    make_cover(args.bg, args.title, args.subtitle, args.output)


if __name__ == "__main__":
    main()
