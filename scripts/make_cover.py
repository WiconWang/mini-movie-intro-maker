#!/usr/bin/env python3
"""
MiniMovie 封面生成器 v10
核心：计算内容重心，文字放对面（内容少的一边）
"""

import argparse
import base64
import os
import sys
import tempfile
from pathlib import Path

MIN_WIDTH = 1920
MIN_HEIGHT = 1080
SCRIPT_DIR = Path(__file__).parent
TEMPLATE_DIR = SCRIPT_DIR.parent / "templates"


def check_image_size(path):
    from PIL import Image
    img = Image.open(path)
    return img.size


def prepare_background(bg_path, output_path):
    """background: cover"""
    from PIL import Image

    img = Image.open(bg_path)
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
    img = img.crop((left, top, left + MIN_WIDTH, top + MIN_HEIGHT))
    img.save(output_path, "PNG")


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


def analyze_image(bg_path):
    """分析图片，返回文字放置建议"""
    from PIL import Image
    import numpy as np

    img = Image.open(bg_path).convert("RGB")
    w, h = img.size

    small = img.resize((480, 270), Image.LANCZOS)
    pixels = np.array(small, dtype=np.uint8)

    content_grid = compute_content_map(pixels)

    # 计算左右内容分布
    left_content = np.sum(content_grid[:, :2])   # 左 2 列 (0-192px)
    right_content = np.sum(content_grid[:, 3:])  # 右 2 列 (288-480px)
    center_content = np.sum(content_grid[:, 2:3])  # 中间 (192-288px)
    
    total = left_content + right_content + center_content
    left_ratio = left_content / total if total > 0 else 0.33
    right_ratio = right_content / total if total > 0 else 0.33

    # 计算上下内容分布
    top_content = np.sum(content_grid[:1, :])
    bottom_content = np.sum(content_grid[2:, :])

    print(f"内容分布: 左={left_ratio:.1%}, 右={right_ratio:.1%}")

    # 判断文字位置（文字放内容少的一边）
    if left_ratio > right_ratio + 0.1:
        # 内容偏左 → 文字放右边
        text_x = 1100
        text_y = 100
        align = "right"
    elif right_ratio > left_ratio + 0.1:
        # 内容偏右 → 文字放左边
        text_x = 80
        text_y = 100
        align = "left"
    else:
        # 左右差不多（满构图）→ 底部居中
        text_x = 80
        text_y = 800
        align = "bottom"

    # 计算文字区域的亮度
    text_grid_x = min(int((text_x + 200) / (MIN_WIDTH / 5)), 4)
    text_grid_y = min(int(text_y / (MIN_HEIGHT / 3)), 2)
    text_region = pixels[
        text_grid_y*90:(text_grid_y+1)*90,
        text_grid_x*96:(text_grid_x+1)*96
    ]
    region_brightness = np.mean(0.299 * text_region[:,:,0] + 
                                0.587 * text_region[:,:,1] + 
                                0.114 * text_region[:,:,2])

    # 颜色选择
    avg_r = np.mean(text_region[:,:,0])
    avg_g = np.mean(text_region[:,:,1])
    avg_b = np.mean(text_region[:,:,2])
    
    # 计算颜色饱和度（判断是否为彩色背景）
    max_c = max(avg_r, avg_g, avg_b)
    min_c = min(avg_r, avg_g, avg_b)
    saturation = (max_c - min_c) / max_c if max_c > 0 else 0
    
    if region_brightness < 80:
        # 暗背景 → 亮色文字
        if avg_b > avg_r and avg_b > avg_g:
            title_color = "#FFD93D"
        elif avg_g > avg_r:
            title_color = "#FF6B6B"
        else:
            title_color = "#00D4FF"
        sub_color = "#FFFFFF"
    elif saturation > 0.3:
        # 彩色背景（饱和度高）→ 亮色字+黑色描边（保证可读性）
        title_color = "#FFFFFF"
        sub_color = "#FFFFFF"
    elif region_brightness < 160:
        # 灰色/低饱和度背景 → 白色字
        title_color = "#FFFFFF"
        sub_color = "#FFFFFF"
    else:
        # 亮且不饱和背景（白/浅灰）→ 深色字
        title_color = "#1A1A2E"
        sub_color = "#333333"

    # 字号计算
    target_width = int(MIN_WIDTH * 0.65)
    chars_count = max(len([c for c in "原神精简版本" if '\u4e00' <= c <= '\u9fff']), 4)
    title_size = int(target_width / chars_count)
    sub_size = int(title_size * 0.45)

    return {
        "text_x": text_x,
        "text_y": text_y,
        "title_size": title_size,
        "sub_size": sub_size,
        "title_color": title_color,
        "sub_color": sub_color,
        "align": align,
        "left_ratio": left_ratio,
        "right_ratio": right_ratio,
        "region_brightness": region_brightness
    }


def render_html_to_image(html_path, output_path, width=1920, height=1080):
    """Playwright 渲染"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"file://{html_path}", wait_until="networkidle")
        page.screenshot(path=output_path, type="jpeg", quality=95)
        browser.close()


def make_cover(bg_path, title, subtitle, output_path):
    """主流程"""

    w, h = check_image_size(bg_path)
    print(f"背景图尺寸: {w}×{h}")

    if w < MIN_WIDTH or h < MIN_HEIGHT:
        print(f"✗ 图片尺寸低于 {MIN_WIDTH}×{MIN_HEIGHT}，请换图", file=sys.stderr)
        sys.exit(1)

    analysis = analyze_image(bg_path)
    print(f"分析: 内容左={analysis['left_ratio']:.1%}, 右={analysis['right_ratio']:.1%}")
    print(f"字号: 主标题={analysis['title_size']}px, 副标题={analysis['sub_size']}px")
    print(f"颜色: 主标题={analysis['title_color']}, 副标题={analysis['sub_color']}")
    print(f"对齐: {analysis['align']}")

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
        if analysis["align"] == "right":
            # 右对齐：垂直居中
            content_style = f'right: 100px; top: 50%; transform: translateY(-50%); text-align: right;'
        elif analysis["align"] == "bottom":
            # 底部对齐：水平居中
            content_style = f'bottom: 100px; left: 50%; transform: translateX(-50%); text-align: center;'
        else:
            # 左对齐：保持原样
            content_style = f'left: 100px; top: 100px;'
        
        # 匹配模板中的格式（分两行）
        html = html.replace('left: 100px;\n  top: 260px;', content_style)
        html = html.replace('font-size: 120px;', f'font-size: {analysis["title_size"]}px;')
        html = html.replace('font-size: 56px;', f'font-size: {analysis["sub_size"]}px;')
        html = html.replace('color: #FFFFFF;\n  text-shadow:', f'color: {analysis["sub_color"]};\n  text-shadow:')

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html",
                                         delete=False, encoding="utf-8") as f:
            f.write(html)
            html_path = f.name

        render_html_to_image(html_path, output_path)
        print(f"✓ 封面已生成: {output_path}")

    finally:
        for p in [processed_bg, html_path]:
            if p and os.path.exists(p):
                os.unlink(p)


def main():
    parser = argparse.ArgumentParser(description="MiniMovie 封面生成器 v10")
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
