#!/usr/bin/env python
"""
ecad2process —— ECAD 数据 → 制程图纸(贴装作业图)CLI 原型

验证目标：证明「从 ECAD 的 Pick&Place 坐标，能自动生成一张 RX DA 那样的
制程图纸」这条链路技术可行。核心逻辑（来自对客户 RX DA 图纸的逐元素拆解）：

    Pick&Place 绝对坐标
      → 减去工艺基准坐标
    相对贴装尺寸
      → 按器件类型 × 是否在光路 查公差表
    带公差的坐标式（ordinate）标注
      → 叠加元件轮廓 + 图框 + 标题栏 + 工艺 Notes
    制程图纸(SVG)

⚠️ 边界说明（哪些是真数据、哪些是规则/模拟）：
  · 元件坐标：ECAD Pick&Place 的**真实数据**（MosaicBus 开源工程）
  · 器件分类：designator 前缀规则（C*/R*/U*…），确定性，无需 AI
  · 公差：一条主规则 + 光路器件例外表。默认 ±0.05 来自 IPC-7351 行业标准，
          与客户 RX DA 图纸默认公差吻合；「光路器件」清单是**演示假设**，
          真实项目需客户工艺确认
  · 基准：这里取「最靠左下的元件」为基准做演示；真实项目基准规则需客户提供
  · 工序名/图号/审核链：**模板占位**，真实项目从 PLM 取

用法（输入文件由外部提供，不随工程分发，示例数据见 ../ecad_proto/data/）：
    python ecad2process.py <某处>/pick-place.csv -o out
    python ecad2process.py <某处>/pick-place.csv --datum J6   # 指定基准元件
"""

from __future__ import annotations

import argparse
import csv
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# ── 器件分类：designator 前缀 → 类别 + 是否在光路（决定公差） ──
#   「光路器件从严」是从客户 RX DA 图纸归纳的规律（TIA/PD ±0.02，电容 ±0.05）。
#   MosaicBus 是 GNSS 板、无光器件，这里把「有源芯片 U*」当作关键器件演示从严逻辑。
CLASSES = {
    "U": ("IC 芯片",   True,  "有源器件，位置关键"),
    "D": ("二极管/LED", False, ""),
    "R": ("电阻",      False, ""),
    "C": ("电容",      False, "去耦，非关键"),
    "L": ("电感",      False, ""),
    "J": ("连接器",    True,  "对外接口，位置关键"),
    "Q": ("晶体管",    False, ""),
    "Y": ("晶振",      True,  "时钟源，位置关键"),
}

# 公差规则（mm）。默认值 0.05 = IPC-7351 设备默认贴装公差，与客户 RX DA 默认表吻合。
TOL_DEFAULT = 0.05
TOL_CRITICAL = 0.02          # 关键/光路器件从严（客户 RX DA 的 TIA 即 ±0.02）


@dataclass
class Comp:
    designator: str
    x: float
    y: float
    rot: float
    footprint: str
    desc: str
    kind: str = ""
    critical: bool = False

    @property
    def prefix(self) -> str:
        m = re.match(r"[A-Za-z]+", self.designator)
        return m.group(0)[0].upper() if m else "?"


# ── 读 Gerber 板框外形层（Profile）——只取折线顶点，得到真实 PCB 轮廓 ──
def read_outline(path: Path) -> list[tuple[float, float]]:
    """解析 Gerber Profile 层，返回板框多边形顶点 [(x,y), ...]（mm）。

    只需处理外形层用到的极小子集：坐标格式(%FS)、单位(%MO)、D01/D02 走线。
    外形层通常就是几条直线段围成的闭合轮廓。
    """
    txt = path.read_text(errors="replace")
    # 格式：%FSLAX45Y45*% -> X/Y 小数位数（这里都是 5）
    m = re.search(r"%FSLAX(\d)(\d)Y(\d)(\d)", txt)
    xdec = int(m.group(2)) if m else 5
    ydec = int(m.group(4)) if m else 5
    to_mm = "%MOMM" in txt
    pts, cx, cy = [], 0.0, 0.0
    for line in txt.splitlines():
        mm = re.match(r"(?:X(-?\d+))?(?:Y(-?\d+))?D0([12])", line.strip())
        if not mm:
            continue
        if mm.group(1) is not None:
            cx = int(mm.group(1)) / (10 ** xdec)
        if mm.group(2) is not None:
            cy = int(mm.group(2)) / (10 ** ydec)
        x, y = (cx, cy) if to_mm else (cx * 25.4, cy * 25.4)
        pts.append((x, y))
    return pts


# ── 读 Gerber 锡膏层（Paste）——提取焊盘（aperture flash）──
def read_pads(path: Path) -> list[dict]:
    """解析 Gerber 锡膏层，返回焊盘列表。每个焊盘 = 一次 flash(D03)。

    只处理常见 aperture：R(矩形 w×h)、C(圆 d)、O(椭圆按矩形近似）。
    每个焊盘: {shape:'R'|'C', x, y, w, h}（mm）。
    """
    txt = path.read_text(errors="replace")
    m = re.search(r"%FSLAX(\d)(\d)Y(\d)(\d)", txt)
    xdec = int(m.group(2)) if m else 5
    ydec = int(m.group(4)) if m else 5
    to_mm = "%MOMM" in txt

    # aperture 定义：%ADD<code><shape>,<params>*%
    aps = {}
    for mm in re.finditer(r"%ADD(\d+)([RCO]),([\d.X]+)\*%", txt):
        code, shape, params = mm.group(1), mm.group(2), mm.group(3).split("X")
        vals = [float(v) for v in params]
        if shape == "C":
            aps[code] = ("C", vals[0], vals[0])
        else:                                 # R / O
            aps[code] = ("R", vals[0], vals[1] if len(vals) > 1 else vals[0])

    # 坐标可能与 D03 同行(X..Y..D03*)或分行(X..Y..D02* 后跟单独 D03*)——都要支持
    pads, cur, cx, cy = [], None, 0.0, 0.0
    for line in txt.splitlines():
        s = line.strip()
        dm = re.match(r"D(\d+)\*$", s)
        if dm and dm.group(1) in aps:         # 选中 aperture
            cur = aps[dm.group(1)]
            continue
        cm = re.match(r"(?:X(-?\d+))?(?:Y(-?\d+))?D0([123])\*", s)
        if not cm:
            continue
        if cm.group(1) is not None:
            cx = int(cm.group(1)) / (10 ** xdec)
        if cm.group(2) is not None:
            cy = int(cm.group(2)) / (10 ** ydec)
        if cm.group(3) == "3" and cur:        # D03 = 在当前坐标 flash 一个焊盘
            x, y = (cx, cy) if to_mm else (cx * 25.4, cy * 25.4)
            pads.append({"shape": cur[0], "x": x, "y": y, "w": cur[1], "h": cur[2]})
    return pads


# ── 读 Altium Pick&Place CSV（跳过文件头，定位到 Designator 列头行）──
def read_pnp(path: Path, layer: str = "TopLayer") -> list[Comp]:
    rows = path.read_text(errors="replace").splitlines()
    hdr_i = next(i for i, r in enumerate(rows) if "Designator" in r and "Center-X" in r)
    reader = csv.reader(rows[hdr_i:])
    header = next(reader)
    col = {name: i for i, name in enumerate(header)}
    out = []
    for r in reader:
        if len(r) <= col.get("Center-Y(mm)", 99):
            continue
        if layer and r[col["Layer"]] != layer:
            continue
        try:
            c = Comp(
                designator=r[col["Designator"]],
                x=float(r[col["Center-X(mm)"]]),
                y=float(r[col["Center-Y(mm)"]]),
                rot=float(r[col["Rotation"]]),
                footprint=r[col.get("Footprint", 2)],
                desc=r[col.get("Description", 6)],
            )
        except (ValueError, KeyError):
            continue
        kind, crit, _ = CLASSES.get(c.prefix, ("未识别", False, ""))
        c.kind, c.critical = kind, crit
        out.append(c)
    return out


# ── SVG 制程图纸 ──
FRAME_MARGIN = 10.0
TITLE_W, TITLE_H = 180.0, 40.0


def build_svg(comps: list[Comp], datum: Comp, sheet=(420.0, 297.0),
              proc="RX DA (Die Attach)", notes=None, outline=None, pads=None) -> str:
    W, H = sheet
    NS = "http://www.w3.org/2000/svg"
    ET.register_namespace("", NS)
    svg = ET.Element(f"{{{NS}}}svg", {
        "width": f"{W}mm", "height": f"{H}mm",
        "viewBox": f"0 0 {W} {H}", "version": "1.1"})

    def line(x1, y1, x2, y2, **kw):
        a = {"x1": f"{x1:.3f}", "y1": f"{y1:.3f}", "x2": f"{x2:.3f}", "y2": f"{y2:.3f}",
             "stroke": "black", "stroke-width": "0.2"}
        a.update(kw)
        ET.SubElement(svg, f"{{{NS}}}line", a)

    def rect(x, y, w, h, **kw):
        a = {"x": f"{x:.3f}", "y": f"{y:.3f}", "width": f"{w:.3f}", "height": f"{h:.3f}",
             "fill": "none", "stroke": "black", "stroke-width": "0.2"}
        a.update(kw)
        ET.SubElement(svg, f"{{{NS}}}rect", a)

    def text(x, y, s, size=3.0, anchor="start", **kw):
        a = {"x": f"{x:.3f}", "y": f"{y:.3f}", "font-size": f"{size}",
             "font-family": "Helvetica, Arial, sans-serif", "text-anchor": anchor}
        a.update(kw)
        t = ET.SubElement(svg, f"{{{NS}}}text", a)
        t.text = s

    # 图框 + 标题栏
    m = FRAME_MARGIN
    rect(m, m, W - 2 * m, H - 2 * m)
    tx, ty = W - m - TITLE_W, H - m - TITLE_H
    rect(tx, ty, TITLE_W, TITLE_H)
    text(tx + 3, ty + 10, "TITLE", 2.2, fill="#555")
    text(tx + 3, ty + 20, proc, 5.0)
    text(tx + 3, ty + 32, "DWG NO. <PLM>   REV <PLM>   贴装作业图", 2.5, fill="#555")
    text(tx + 3, ty - 3, "DIMENSIONS IN MILLIMETERS · 贴装坐标相对基准", 2.4, fill="#555")

    # —— 元件布局：PCB 坐标 → 图纸坐标。
    # 绘图区四周留白：顶部留 X 尺寸带，左侧留 Y 尺寸带，底部留 Notes。
    DIM_TOP, DIM_LEFT = 30.0, 26.0        # 尺寸标注带宽度
    ax0 = m + DIM_LEFT
    ay0 = m + DIM_TOP
    ax1 = W - m - 6
    ay1 = ty - 22                          # 底部给 Notes/图例
    # 坐标基准范围：优先用**板框**（元件应落在板框内），无板框时退回元件包围盒
    ext = outline if outline else [(c.x, c.y) for c in comps]
    exs = [p[0] for p in ext]
    eys = [p[1] for p in ext]
    pw, ph = (max(exs) - min(exs)) or 1, (max(eys) - min(eys)) or 1
    scale = min((ax1 - ax0) / pw, (ay1 - ay0) / ph) * 0.92
    cx0, cy0 = (min(exs) + max(exs)) / 2, (min(eys) + max(eys)) / 2
    ox, oy = (ax0 + ax1) / 2, (ay0 + ay1) / 2

    def Pxy(px, py):               # PCB 坐标 -> 图纸坐标（Y 翻转：PCB 上为 +Y，SVG 下为 +Y）
        return ox + (px - cx0) * scale, oy - (py - cy0) * scale

    def P(c):
        return Pxy(c.x, c.y)

    # —— PCB 板框（来自 Gerber Profile 层）——
    if outline:
        pts = " ".join(f"{Pxy(x, y)[0]:.2f},{Pxy(x, y)[1]:.2f}" for x, y in outline)
        ET.SubElement(svg, f"{{{NS}}}polyline", {
            "points": pts, "fill": "#eef7ee", "stroke": "#2a7", "stroke-width": "0.4"})

    # —— 焊盘（来自 Gerber 锡膏层）——画在板框之上、元件之下，橙色 ——
    for pd in (pads or []):
        cxp, cyp = Pxy(pd["x"], pd["y"])
        w, h = pd["w"] * scale, pd["h"] * scale
        if pd["shape"] == "C":
            ET.SubElement(svg, f"{{{NS}}}circle", {
                "cx": f"{cxp:.2f}", "cy": f"{cyp:.2f}", "r": f"{w/2:.2f}",
                "fill": "#e8992e", "stroke": "none"})
        else:
            ET.SubElement(svg, f"{{{NS}}}rect", {
                "x": f"{cxp - w/2:.2f}", "y": f"{cyp - h/2:.2f}",
                "width": f"{w:.2f}", "height": f"{h:.2f}",
                "fill": "#e8992e", "stroke": "none"})

    # 元件标记：有焊盘时只给关键器件描红框（焊盘已表达位置）；无焊盘时画方块占位
    for c in comps:
        px, py = P(c)
        if pads:
            if c.critical:
                r = 2.8
                ET.SubElement(svg, f"{{{NS}}}rect", {
                    "x": f"{px - r:.2f}", "y": f"{py - r:.2f}",
                    "width": f"{2*r:.2f}", "height": f"{2*r:.2f}",
                    "fill": "none", "stroke": "#c00", "stroke-width": "0.35"})
            text(px, py - 3.4, c.designator, 1.8, anchor="middle",
                 fill="#c00" if c.critical else "#555")
        else:
            r = 2.6 if c.prefix in ("U", "J") else 1.5
            col = "#c00" if c.critical else "#999"
            rect(px - r, py - r, 2 * r, 2 * r, stroke=col, stroke_width="0.3")
            text(px, py - r - 0.8, c.designator, 2.0, anchor="middle", fill=col)

    # 基准标记
    dpx, dpy = P(datum)
    ET.SubElement(svg, f"{{{NS}}}circle",
                  {"cx": f"{dpx:.3f}", "cy": f"{dpy:.3f}", "r": "1.4",
                   "fill": "none", "stroke": "#08f", "stroke-width": "0.4"})
    text(dpx - 2, dpy + 4, f"DATUM {datum.designator}", 2.2, anchor="end", fill="#08f")

    # ── 坐标式标注（ordinate）：像客户 RX DA 那样，从基准拉一条基准线，
    #    每个关键器件的坐标投影到基准轴上顺序标，尺寸界线垂直、零交叉。
    #    只标关键器件（U/J），普通电阻电容不标 —— 这正是「标哪几个」的规则。
    crit = [c for c in comps if c.critical]

    def fmt(v, tol):
        return f"{abs(v):.2f}±{tol}"

    # 图形（板框+元件）在图纸上的实际包围盒 —— 基准线紧贴它外缘，尺寸链才紧凑
    gx = [Pxy(x, y)[0] for x, y in ext]
    gy = [Pxy(x, y)[1] for x, y in ext]
    g_top, g_bot = min(gy), max(gy)
    g_left, g_right = min(gx), max(gx)

    # —— 顶部水平基准线：标各元件的 X 坐标（尺寸文字竖排在基准线上方）——
    base_y = g_top - 10                   # 基准线在图形上边缘上方 10mm
    line(dpx, dpy, dpx, base_y, stroke="#08f", stroke_width="0.15")   # 基准 0 界线
    for c in crit:
        px, py = P(c)
        dx = c.x - datum.x
        tol = TOL_CRITICAL if c.critical else TOL_DEFAULT
        line(px, py, px, base_y, stroke="#08f", stroke_width="0.12")   # 尺寸界线
        text(px + 0.7, base_y - 1, fmt(dx, tol), 1.9, anchor="start",
             transform=f"rotate(-90 {px + 0.7:.3f} {base_y - 1:.3f})")
    text(dpx, base_y - 1.2, "0", 2.2, anchor="middle", fill="#08f")

    # —— 左侧垂直基准线：标各元件的 Y 坐标 ——
    base_x = g_left - 10                  # 基准线在图形左边缘左侧 10mm
    line(dpx, dpy, base_x, dpy, stroke="#08f", stroke_width="0.15")   # 基准 0 界线
    for c in crit:
        px, py = P(c)
        dy = c.y - datum.y
        tol = TOL_CRITICAL if c.critical else TOL_DEFAULT
        line(px, py, base_x, py, stroke="#08f", stroke_width="0.12")
        text(base_x - 0.8, py + 0.6, fmt(dy, tol), 1.9, anchor="end")
    text(base_x - 0.8, dpy + 0.6, "0", 2.2, anchor="end", fill="#08f")

    # 图例 + Notes：放到底部标题栏左侧的空白带（避开尺寸标注区）
    lx = m + 3
    ly = H - m - TITLE_H + 2
    ET.SubElement(svg, f"{{{NS}}}rect", {"x": f"{lx}", "y": f"{ly:.1f}",
                  "width": "3", "height": "3", "fill": "none", "stroke": "#c00", "stroke-width": "0.3"})
    text(lx + 4, ly + 2.5, f"关键器件(IC/连接器)  公差 ±{TOL_CRITICAL}", 2.2)
    ET.SubElement(svg, f"{{{NS}}}rect", {"x": f"{lx+62}", "y": f"{ly:.1f}",
                  "width": "3", "height": "3", "fill": "none", "stroke": "#999", "stroke-width": "0.3"})
    text(lx + 66, ly + 2.5, f"普通器件  公差 ±{TOL_DEFAULT}（不逐个标注）", 2.2)
    text(lx, ly + 8, "Notes:", 2.4, fill="#333")
    for i, n in enumerate(notes or []):
        text(lx, ly + 12 + i * 3.4, f"{i+1}. {n}", 2.2, fill="#333")

    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(svg, encoding="unicode")


def generate(pnp_path, outline_path=None, paste_path=None,
             datum_ref=None, proc="RX DA (Die Attach)"):
    """核心入口：读 ECAD 文件 -> 生成制程图 SVG + 基本信息。

    CLI 与后端共用。返回 (svg: str, info: dict)。info 供前端展示基本信息。
    """
    from collections import Counter

    comps = read_pnp(Path(pnp_path))
    if not comps:
        raise ValueError("未解析到任何顶层元件（检查 Pick&Place 文件格式）")
    outline = read_outline(Path(outline_path)) if outline_path else None
    pads = read_pads(Path(paste_path)) if paste_path else None

    # 选基准
    if datum_ref:
        datum = next((c for c in comps if c.designator == datum_ref), None)
        if not datum:
            raise ValueError(f"找不到基准元件 {datum_ref}")
    else:
        # 演示规则：关键器件群左下角。真实项目应按工艺基准规范。
        crit_comps = [c for c in comps if c.critical] or comps
        datum = min(crit_comps, key=lambda c: (c.x + c.y))

    notes = [
        "贴装坐标均相对基准 %s，单位 mm。" % datum.designator,
        "关键器件（IC/连接器，红框）公差 ±%.2f，其余 ±%.2f（IPC-7351 默认）。"
        % (TOL_CRITICAL, TOL_DEFAULT),
        "基准选择与关键器件清单为演示假设，实际以工艺规范为准。",
    ]
    svg = build_svg(comps, datum, proc=proc, notes=notes, outline=outline, pads=pads)

    crit = [c for c in comps if c.critical]
    info = {
        "proc": proc,
        "n_comps": len(comps),
        "kinds": dict(Counter(c.kind for c in comps)),
        "datum": {"designator": datum.designator, "x": round(datum.x, 3), "y": round(datum.y, 3)},
        "has_outline": outline is not None,
        "n_pads": len(pads) if pads else 0,
        "critical": [
            {"designator": c.designator, "kind": c.kind,
             "dx": round(c.x - datum.x, 3), "dy": round(c.y - datum.y, 3),
             "tol": TOL_CRITICAL}
            for c in crit],
        "tol_default": TOL_DEFAULT,
        "tol_critical": TOL_CRITICAL,
        "parts": [
            {"designator": c.designator, "kind": c.kind, "critical": c.critical,
             "x": round(c.x, 3), "y": round(c.y, 3)}
            for c in comps],
    }
    return svg, info


def main():
    ap = argparse.ArgumentParser(description="ECAD Pick&Place → 制程图纸原型")
    ap.add_argument("pnp", help="Altium Pick&Place CSV")
    ap.add_argument("-o", "--outdir", default="out")
    ap.add_argument("--datum", help="基准元件 designator（默认取器件群左下角）")
    ap.add_argument("--proc", default="RX DA (Die Attach)")
    ap.add_argument("--outline", help="Gerber 板框外形层(Profile).gbr")
    ap.add_argument("--paste", help="Gerber 锡膏层(Paste).gbr")
    args = ap.parse_args()

    svg, info = generate(args.pnp, args.outline, args.paste, args.datum, args.proc)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "process_drawing.svg").write_text(svg)

    print(f"输入      {args.pnp}")
    print(f"元件      {info['n_comps']} 个（顶层）")
    print(f"分类      " + " · ".join(f"{k}×{v}" for k, v in info["kinds"].items()))
    print(f"焊盘      {info['n_pads']} 个   板框: {'有' if info['has_outline'] else '无'}")
    print(f"基准      {info['datum']['designator']} @ "
          f"({info['datum']['x']:.2f}, {info['datum']['y']:.2f})")
    print(f"关键器件  {len(info['critical'])} 个")
    print(f"输出      {outdir / 'process_drawing.svg'}")


if __name__ == "__main__":
    main()
