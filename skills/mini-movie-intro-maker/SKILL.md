---
name: mini-movie-intro-maker
description: Use when generating video cover images and markdown summaries for MiniMovie. Overlays artistic text on game artwork (official posters, PV stills, or screenshots).
version: 0.12.0
author: Wicon
license: MIT
trigger:
  keywords:
    - 生成封面
    - 做封面
    - 封面图
    - 视频封面
    - 生成简介
    - 写简介
    - 做个介绍
  intents:
    - create_video_cover
    - create_video_intro

context:
  required:
    - video_title
    - background_image
  optional:
    - game_name
    - version_number
    - background_music
    - video_duration

---

# MiniMovie 视频封面/简介生成工具

## 核心流程

```
调用方提供背景图（底图）+ 标题信息
         ↓
    分析图片特征
    ├─ 计算内容密度（边缘 + 颜色变化）
    ├─ 计算左右内容比例 → 文字放内容少的一边
    ├─ 按对齐方式采样文字落点亮度 → 选颜色
    └─ 按实际标题逐字估值宽度 → 初始字号
         ↓
    Playwright 渲染 HTML → 实测收敛标题字号 → 截图
         ↓
    输出 1920×1080 的 JPG 封面
```

## 脚本用法

```bash
# 从项目根目录执行
python3 skills/mini-movie-intro-maker/scripts/make_cover.py \
  --bg /path/to/background.jpg \
  --title "原神精简版本" \
  --subtitle "V1.6 盛夏！海岛？大冒险！" \
  --output output/cover.jpg
```

> 本 skill 不规定背景图与产出的落盘位置——独立使用时自定；作为 game-storyline-pipeline 一环时，底图由用户每次显式 `--bg` 指定（官方海报，不入库），成品登记为版本级 cover/outro 资产：
> ```bash
> mmm add-asset --game <code> --version <no> --slug <quest_slug> \
>     --kind cover --src output/cover.jpg
> ```
> 登记后 `task-create --claim` 自动以 asset_id 引用（落 `{game}/{version}/_version/cover/`）。

## 智能布局规则

### 1. 文字位置选择

**核心原则：文字放「最不容易压到脸/人」的区域。** 用真实动漫脸检测得到脸框，再对每个候选「整块内容矩形（含副标题）」算它与脸框的交集面积，选交集最小的——**优先避脸**、身体可容忍（人物常占满画面高度，很难全避），且**默认放底部前景**（实测最干净）。

1. 真实检脸（`_detect_face_boxes`）：`opencv` + `models/lbpcascade_animeface.xml`，返回 1920×1080 坐标系下的脸框
2. 候选矩形（`_candidate_rects`）：top / mid / bottom × left / center / right，共 7 个，**高度盖满「主标题 + 副标题 + 装饰线」整块内容**（不只主标题高，避免副标题压脸）；主键=与脸框交集面积（越小越避脸），次键=区域显著度（内容密度 + 皮肤权重 `SKIN_PENALTY`，让平滑但显眼的脸/身体也计为重要）
3. **底部优先**（`BOTTOM_PREF_TOL`）：bottom-center 的避让重叠比最优差 ≤（重叠范围 × 此比例）时选底部前景；仅当某角落的脸重叠显著更小才偏离（如中央大脸图用顶排避让）
4. 检测不到脸时主键全 0 → 靠显著度 + 底部优先落到前景，不因瞎猜乱挪反而压脸

| 竖带 | 锚点 | CSS 属性 | 对齐方式 |
|------|------|----------|----------|
| 上 | 左 | `left: 100px; top: 100px` | 左对齐 |
| 上 | 右 | `right: 100px; top: 100px` | 右对齐 |
| 上 | 中 | `top: 100px; left: 50%; transform: translateX(-50%)` | 顶部居中 |
| 中 | 左/右 | `left/right: 100px; top: 50%; transform: translateY(-50%)` | 垂直居中 |
| 中 | 中 | `top: 50%; left: 50%; transform: translate(-50%,-50%)` | 完全居中 |
| 下 | 中 | `bottom: 100px; left: 50%; transform: translateX(-50%)` | 底部居中 |

### 2. 内容检测方法

用「边缘密度 + 颜色变化」作为内容密度的代理指标：
- 角色/文字：边缘多、颜色变化大
- 空白背景：边缘少、颜色单一

### 3. 字号计算（动态，不依赖标题长度）

- **目标**：主标题文字宽度占图片宽度 60% ~ 80%，默认 70%
- 按实际标题**逐字估值宽度**得出初始字号（CJK/全角 ≈1em，数字/小写 ≈0.55em，大写 ≈0.62em，空格 ≈0.4em）
- **主标题 ≈ 副标题的 2 倍**（副标题 = 主标题 × 50%）
- 最终由 Playwright 载入页面后**实测实际渲染宽度并自动收敛**到目标占比——任意字体、任意图都能稳定落在区间内
- **短标题封顶**：文字太宽时缩小到 70%；文字不足时最多放大到图高约 30%（`min(360, 高度×0.30)`），**不强迫短标题撑满 70%**，避免字号爆炸

### 4. 颜色选择（自适应，不写死）

**采样位置 = 文字真实落点的那条横带**（不是整图）。用「有效亮度」判断：`有效亮度 = 采样带亮度 × (1 - 暗化层alpha)`，暗化层按文字竖向位置插值（底部 alpha 最大，约 0.45~0.65）。

按有效亮度 + 色散（采样带灰度标准差，反映细节杂乱程度）决定深浅，**再按落点背景主色派生同色系文字色**（色相取自场景本身 → 和谐）：

| 落点特征 | 条件 | 主标题色 | 副标题色 |
|----------|------|----------|----------|
| 亮而杂 | 有效亮度 ≥150 且色散 ≥38 | 白色 | 白色 |
| 均匀亮 | 有效亮度 ≥150 且色散 <38 | 场景主色的深色版 | 其上浅调 |
| 中灰 | 有效亮度 100~150 | 白色 | 白色 |
| 暗区 | 有效亮度 <100 | 场景主色的浅色版 | 其上浅调 |

> 这样不同图会得到不同位置、不同颜色（白 / 各自场景色）。**同色系、高饱和、靠明度做对比**——文字色与画面一个调子，既不刻板、又不撞色。全封面配黑色描边保证可读。

**选色原理**：不是像素级反色。用 `colorsys` 取落点背景的**色相 H**，按暗/亮设目标明度（暗底浅字 `SCENE_LIGHT_L`≈0.73 / 亮底深字 `SCENE_DARK_L`≈0.13），饱和度按场景放大但保底/封顶（`SCENE_SAT_FLOOR`≈0.68 / `SCENE_SAT_CAP`≈0.90），得出 `#RRGGBB`。**副标题**由主标题色派生（同色系、往白调约 35%），主副标题色差可控。

**饱和度计算**：`saturation = (max(R,G,B) - min(R,G,B)) / max(R,G,B)`

### 5. 文字规范

- **主标题/副标题都不换行**（`white-space: nowrap`）
- **位置**：大体离开主体即可，不需要完全避让
- **主标题**：黑色描边 3-5px + 外发光
- **副标题**：黑色描边 2-3px

### 6. 尺寸要求

- 背景图必须 ≥ 1920×1080，低于则报错
- 高于则按 `background: cover` 裁剪（短边对齐+居中裁剪）

## 可调参数（`make_cover.py` 顶部常量）

嫌太白 / 太花 / 变化不够时改这几个即可：

| 常量 | 默认 | 作用 |
|------|------|------|
| `FIT_TARGET_RATIO` | 0.70 | 主标题宽度占图宽目标（60~80% 区间都行） |
| `SHORT_TITLE_H_RATIO` | 0.30 | 短标题字号封顶 = 图高 × 此比例 |
| `COLOR_BRIGHT_LUMA` | 150 | 有效亮度 ≥ 此值为「亮区」，之上再按色散分白/深 |
| `COLOR_MID_LUMA` | 100 | 有效亮度 ≥ 此值为「中灰」→ 白字；低于进暗区 accent |
| `COLOR_BUSY_SPREAD` | 38 | 色散 ≥ 此值为「杂乱」→ 亮区杂时强制白字兜底 |
| `ACCENT_ON_DARK` | True | 暗区是否用「场景同色浅字」；设 False 则暗区也回白字（更素净） |
| `SUB_LIGHTEN_RATIO` | 0.35 | 副标题由主标题派生时往白调的比例（<0.5 色差更小） |
| `SCENE_SAT_FLOOR` | 0.68 | 同色系文字色饱和度下限（越高越醒目） |
| `SCENE_SAT_BOOST` | 1.7 | 场景饱和度放大倍率（低饱和场景也长得出鲜明色相） |
| `SCENE_SAT_CAP` | 0.90 | 饱和度上限（防刺眼） |
| `SCENE_LIGHT_L` | 0.73 | 暗底同色系浅字的目标明度 |
| `SCENE_DARK_L` | 0.13 | 亮底同色系深字的目标明度 |
| `SKIN_PENALTY` | 25.0 | 皮肤显著度权重：让平滑但显眼的脸/身体也计入内容 |
| `BOTTOM_PREF_TOL` | 0.6 | 底部优先：底部避让重叠比最优差 ≤（重叠范围 × 此比例）就选底部前景 |

## 项目结构

```
mini-movie-intro-maker/
├── .gitignore
├── skills/
│   └── mini-movie-intro-maker/
│       ├── SKILL.md            # Skill 定义
│       ├── scripts/
│       │   └── make_cover.py   # 封面生成脚本
│       ├── templates/
│       │   └── cover.html      # HTML 模板
│       └── models/
│           └── lbpcascade_animeface.xml   # 动漫脸检测级联
├── docs/                       # 文档
└── output/                     # 生成的封面图
```

## 依赖

- Python：`pillow`、`numpy`
- 渲染：`playwright`（含 chromium，见运行环境）
- 检脸：`opencv-python-headless`（注意：须用 **<5.0**，5.0 移除了 `CascadeClassifier`）+ `models/lbpcascade_animeface.xml`（已内置）

## 字体

模板首选字体为 **得意黑（Smiley Sans）**，`font-family: 'Smiley Sans', ...`。
Linux 安装（供 Playwright/Chromium 通过 fontconfig 识别）：

```bash
curl -sL -o /tmp/smiley.zip https://github.com/atelier-anchor/smiley-sans/releases/download/v2.0.1/smiley-sans-v2.0.1.zip
unzip -o /tmp/smiley.zip -d /tmp/smiley_extracted
mkdir -p ~/.fonts
cp /tmp/smiley_extracted/SmileySans-Oblique.ttf ~/.fonts/
fc-cache -f
fc-match "Smiley Sans"   # 期望命中「得意黑」
```

macOS：`brew install --cask font-smiley-sans`

## Markdown 简介模板

```markdown
# {游戏} {版本} {副标题}

> {一句话概括，20字以内}

## 剧情概要

{2-3 段}

## 角色

- **{角色A}** — {一句话定位}

## 标签

#{游戏} #{版本} #剧情解说 #游戏视频
```
