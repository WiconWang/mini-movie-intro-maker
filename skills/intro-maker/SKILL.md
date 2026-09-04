---
name: mini-movie-intro-maker
description: Use when generating video cover images and markdown summaries for MiniMovie. Overlays artistic text on game screenshots.
version: 0.9.0
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
用户提供背景图 + 标题信息
         ↓
    分析图片特征
    ├─ 计算内容密度（边缘 + 颜色变化）
    ├─ 计算左右内容比例
    └─ 文字放内容少的一边
         ↓
    Playwright 渲染 HTML → 截图
         ↓
    输出 1920×1080 的 JPG 封面
```

## 脚本用法

```bash
# 从项目根目录执行
python3 skills/intro-maker/scripts/make_cover.py \
  --bg /path/to/screenshot.png \
  --title "原神精简版本" \
  --subtitle "V1.6 盛夏！海岛？大冒险！" \
  --output output/cover.jpg
```

## 智能布局规则

### 1. 文字位置选择

**核心原则：计算内容重心，文字放对面（内容少的一边）**

| 内容分布 | 文字位置 | CSS 属性 | 对齐方式 |
|----------|----------|----------|----------|
| 内容偏左（左 > 右 + 10%） | 右侧 | `right: 100px; top: 50%; transform: translateY(-50%)` | 右对齐，垂直居中 |
| 内容偏右（右 > 左 + 10%） | 左侧 | `left: 100px; top: 100px` | 左对齐 |
| 内容居中（左右差 < 10%） | 底部 | `bottom: 100px; left: 50%; transform: translateX(-50%)` | 底部居中 |

### 2. 内容检测方法

用「边缘密度 + 颜色变化」作为内容密度的代理指标：
- 角色/文字：边缘多、颜色变化大
- 空白背景：边缘少、颜色单一

### 3. 字号计算

- 目标：文字宽度占图片宽度 65%
- 主标题字号 = 目标宽度 / 字数
- 副标题字号 = 主标题 × 45%

### 4. 颜色选择

根据文字区域的背景亮度和饱和度选择：

| 背景类型 | 条件 | 主标题色 | 副标题色 |
|----------|------|----------|----------|
| 暗背景 | 亮度 < 80 | 亮色（黄/红/青） | 白色 |
| 彩色背景 | 饱和度 > 0.3 | 白色 | 白色 |
| 灰色背景 | 亮度 80-160 | 白色 | 白色 |
| 亮且不饱和 | 亮度 > 160 且饱和度 < 0.3 | 深色 #1A1A2E | 深色 |

**饱和度计算**：`saturation = (max(R,G,B) - min(R,G,B)) / max(R,G,B)`

### 5. 文字规范

- **主标题/副标题都不换行**（`white-space: nowrap`）
- **位置**：大体离开主体即可，不需要完全避让
- **主标题**：黑色描边 3-5px + 外发光
- **副标题**：黑色描边 2-3px

### 6. 尺寸要求

- 背景图必须 ≥ 1920×1080，低于则报错
- 高于则按 `background: cover` 裁剪（短边对齐+居中裁剪）

## 项目结构

```
mini-movie-intro-maker/
├── .gitignore
├── skills/
│   └── intro-maker/
│       ├── SKILL.md            # Skill 定义
│       ├── scripts/
│       │   └── make_cover.py   # 封面生成脚本
│       └── templates/
│           └── cover.html      # HTML 模板
├── docs/                       # 文档
└── output/                     # 生成的封面图
```

## 字体

安装得意黑（推荐）：
```bash
brew install --cask font-smiley-sans
```

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
