#!/usr/bin/env python
"""
step2drawing —— STEP 三维模型 → 二维三视图工程图

技术路线（对应《技术方案评审 v1.0》的"架构 B"）：
    STEP → OCCT 精确 HLR 投影 → 自研 SVG 组装 → SVG / PDF / DXF

刻意不用 FreeCAD TechDraw：
  · 其整页 PDF/SVG 导出依赖 Qt GUI，headless 下不可用
  · 其内部本就调用同一个 OCCT HLRBRep_Algo，与前置投影层职责重复
本实现全链路无 GUI 依赖、无 Docker，pip 装完即可跑。

用法：
    python step2drawing.py parts/submount.step
    python step2drawing.py parts/submount.step --scale 20:1 --sheet A4
    python step2drawing.py parts/bracket.step --projection first
"""

from __future__ import annotations

import argparse
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import build123d as bd
from build123d import ColorIndex, ExportDXF, ExportSVG, LineType

import dimensions as dims
import pmi as pmi_mod

# ---------------------------------------------------------------- 图纸标准

# 标准比例系列 GB/T 14690-1993 / ISO 5455
SCALE_SERIES = [
    1 / 100, 1 / 50, 1 / 20, 1 / 10, 1 / 5, 1 / 2,
    1, 2, 5, 10, 20, 50, 100,
]

# 图幅 (宽, 高)，横放
SHEETS = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
}

FRAME_MARGIN = 10.0        # 图框到纸边
TITLE_W, TITLE_H = 180.0, 56.0   # 标题栏 GB/T 10609.1
VIEW_GAP = 14.0            # 视图间距（图纸 mm）
VIEW_PAD = 6.0             # 视图区内缩

# 相机定义：eye 方向 + up 方向
#   front: 从 -Y 看向 +Y  → 投影面 XZ（横=模型X，纵=模型Z）
#   top:   从 +Z 俯视     → 投影面 XY（横=模型X，纵=模型Y）
#   right: 从 +X 看       → 投影面 YZ（横=模型Y，纵=模型Z）
#   left:  从 -X 看       → 侧视图的备选（见 project_views 的回退逻辑）
VIEW_DEFS = {
    "front": ((0, -1, 0), (0, 0, 1)),
    "top":   ((0, 0, 1),  (0, 1, 0)),
    "right": ((1, 0, 0),  (0, 0, 1)),
    "left":  ((-1, 0, 0), (0, 0, 1)),
}


@dataclass
class View:
    name: str
    vis: list          # 可见边
    hid: list          # 隐藏边
    w: float = 0.0     # 投影宽（模型单位）
    h: float = 0.0     # 投影高
    cx: float = 0.0    # 投影包围盒中心
    cy: float = 0.0
    x: float = 0.0     # 在图纸上的目标中心
    y: float = 0.0
    dropped: int = 0   # 被越界过滤掉的 HLR 垃圾边数


# ---------------------------------------------------------------- 投影

def _sanity_bounds(shape, name, center, dist, tol=0.05):
    """正交投影的几何不变量：任何投影边必落在"模型包围盒的投影"之内。

    把包围盒本身当作一个 Box 投一次，得到参考边界。用于滤掉 OCCT HLR
    偶发吐出的越界垃圾边（实测 100G DR1 模块俯视图 6106 条边里有 1 条）。
    """
    bb = shape.bounding_box()
    box = bd.Pos(*bb.center().to_tuple()) * bd.Box(bb.size.X, bb.size.Y, bb.size.Z)
    eye_dir, up = VIEW_DEFS[name]
    eye = tuple(c + d * dist for c, d in zip(center, eye_dir))
    v, h = box.project_to_viewport(eye, up, center)
    rb = bd.Compound(children=list(v) + list(h)).bounding_box()
    return (rb.min.X - tol, rb.min.Y - tol, rb.max.X + tol, rb.max.Y + tol)


def _in_bounds(edge, bnd) -> bool:
    eb = edge.bounding_box()
    x0, y0, x1, y1 = bnd
    return eb.min.X >= x0 and eb.min.Y >= y0 and eb.max.X <= x1 and eb.max.Y <= y1


def _project_one(shape, name, center, dist) -> View | None:
    """跑一次 HLR。返回 None 表示该方向投影为空（OCCT HLR 已知缺陷，见 diagnose）。"""
    eye_dir, up = VIEW_DEFS[name]
    eye = tuple(c + d * dist for c, d in zip(center, eye_dir))
    vis, hid = shape.project_to_viewport(eye, up, center)
    if not list(vis) + list(hid):
        return None

    try:
        bnd = _sanity_bounds(shape, name, center, dist)
        vis2 = [e for e in vis if _in_bounds(e, bnd)]
        hid2 = [e for e in hid if _in_bounds(e, bnd)]
        dropped = (len(list(vis)) + len(list(hid))) - (len(vis2) + len(hid2))
    except Exception:      # 包围盒投影本身失败时不做过滤，宁可不滤也不误杀
        vis2, hid2, dropped = list(vis), list(hid), 0

    edges = vis2 + hid2
    if not edges:
        return None
    v = View(name=name, vis=vis2, hid=hid2, dropped=dropped)
    pb = bd.Compound(children=edges).bounding_box()
    v.w, v.h = pb.size.X, pb.size.Y
    v.cx, v.cy = pb.center().X, pb.center().Y
    return v


def diagnose_empty(shape, name, center, dist) -> list[tuple[int, int]]:
    """某方向投影为空时，逐实体定位罪魁。返回 [(实体序号, 面数)]。

    OCCT HLR 是"全有或全无"：装配体里只要一个实体在某方向上失败，
    整个 Compound 的投影就返回空。实测 100G DR1 模块即为此例。
    """
    bad = []
    for i, sol in enumerate(shape.solids()):
        if _project_one(sol, name, center, dist) is None:
            bad.append((i, len(sol.faces())))
    return bad


def assert_brep(shape: bd.Shape, src: Path):
    """HLR 前置守卫：模型必须含精确 B-rep。

    ⚠️ 这不是可选的健壮性检查 —— 实测把 STEP AP242 的**曲面细分(tessellated)**
    数据喂给 OCCT HLR 会**直接 SIGSEGV 段错误**（不是抛异常，try/except 兜不住，
    进程直接死）。必须在调用前拦截。

    技术根因：HLR 是「隐藏线消除」，作用对象是**拓扑边**。三角网格面在 OCCT
    拓扑里 edges=0 —— 没有边可消隐，算法拿到空拓扑后崩溃。

    这正是「精确 B-rep vs 三角网格」这条分水岭的硬证据：
      · 精确 B-rep → HLR → 解析曲线（圆是真圆、可标注）→ 可作工程图
      · 三角网格   → 无边可投影 → 根本出不了工程图
    """
    n_faces, n_edges, n_solids = len(shape.faces()), len(shape.edges()), len(shape.solids())
    if n_edges > 0:
        return

    schema = step_schema(src)
    raise SystemExit(
        f"\n✗ 无法出图：模型不含精确 B-rep，只有曲面细分(tessellated)数据。\n"
        f"\n  文件      {src.name}"
        f"\n  协议      {schema}"
        f"\n  实体      solids={n_solids}  faces={n_faces}  edges={n_edges}   ← 边数为 0"
        f"\n"
        f"\n  工程图的本质是隐藏线消除(HLR)，作用对象是**拓扑边**。"
        f"\n  三角网格没有边，HLR 无从下手（实测强行调用会 SIGSEGV 崩溃）。"
        f"\n"
        f"\n  ⚠️ 注意：本文件确实是 AP242，但 AP242 只是**允许**携带 B-rep 与 PMI，"
        f"\n     不代表一定有。导出时必须勾选 B-rep（而非 tessellated/mesh）。\n")


def _hlr_worker(args):
    """子进程入口：自己加载 STEP，跑一个视图的 HLR，把边集写成 BREP 返回路径。

    为什么走进程 + BREP 文件，而不是线程（实测数据）：
      · OCP **确实释放 GIL**，线程池测得 2.08x 真并行 —— 但每个线程从 62s 慢到 160s，
        内存带宽/缓存严重争抢，墙钟 159.8s vs 串行 165s = **仅 1.03x，等于无效**
      · 进程池墙钟 100.1s = **1.71x**，是真收益
      · OCP 的 Shape/Edge 不可 pickle，故边集走 BREP 文件往返 —— 实测单视图仅 0.22s
        （7.6MB，写 0.17s + 读 0.05s），三视图共 0.7s，相对省下的 65s 可忽略
    """
    path, name, tmpdir = args
    import build123d as bd
    import step2drawing as s2d

    shape = bd.import_step(path)
    bb = shape.bounding_box()
    center = (bb.center().X, bb.center().Y, bb.center().Z)
    dist = bb.diagonal * 10 or 1000.0
    v = s2d._project_one(shape, name, center, dist)
    if v is None:
        return name, None

    out = {"w": v.w, "h": v.h, "cx": v.cx, "cy": v.cy, "dropped": v.dropped}
    for key in ("vis", "hid"):
        edges = getattr(v, key)
        if edges:
            f = str(Path(tmpdir) / f"{name}_{key}.brep")
            bd.export_brep(bd.Compound(children=list(edges)), f)
            out[key] = f
        else:
            out[key] = None
    return name, out


def _load_view(name: str, d: dict) -> View:
    v = View(name=name, vis=[], hid=[], dropped=d["dropped"])
    for key in ("vis", "hid"):
        if d[key]:
            setattr(v, key, bd.import_brep(d[key]).edges())
    v.w, v.h, v.cx, v.cy = d["w"], d["h"], d["cx"], d["cy"]
    return v


def project_views_parallel(src: Path, shape: bd.Shape, side: str = "auto"):
    """并行版三视图 HLR。实测 LSB400: 165s -> ~101s (1.71x)。

    四个方向一起投（含备选的左视图）—— 多跑一个视图在并行下不占墙钟，
    却省掉「右视图失败后再串行补跑左视图」的一轮。
    """
    import tempfile
    from concurrent.futures import ProcessPoolExecutor

    names = ["front", "top", "right", "left"]
    with tempfile.TemporaryDirectory() as td:
        with ProcessPoolExecutor(max_workers=min(4, (os.cpu_count() or 4))) as ex:
            got = dict(ex.map(_hlr_worker, [(str(src), n, td) for n in names]))

        bb = shape.bounding_box()
        center = (bb.center().X, bb.center().Y, bb.center().Z)
        dist = bb.diagonal * 10 or 1000.0

        views = {}
        for n in ("front", "top"):
            if got.get(n) is None:
                bad = diagnose_empty(shape, n, center, dist)
                raise RuntimeError(
                    f"{n} 视图投影为空。OCCT HLR 在以下实体上失败："
                    f"{[f'#{i}({k}面)' for i, k in bad]}")
            views[n] = _load_view(n, got[n])

        order = {"right": ["right"], "left": ["left"], "auto": ["right", "left"]}[side]
        notes = []
        for cand in order:
            if got.get(cand) is not None:
                views["side"] = _load_view(cand, got[cand])
                return views, cand, notes
            notes.append((cand, diagnose_empty(shape, cand, center, dist)))
        raise RuntimeError(f"侧视图两个方向均投影为空。诊断：{notes}")


def project_views(shape: bd.Shape, side: str = "auto") -> tuple[dict[str, View], str, list]:
    """三视图精确 HLR（串行）。

    side: right / left / auto（先试右视图，空则回退左视图）
    返回 (views, 实际用的侧视图名, 诊断信息)
    """
    bb = shape.bounding_box()
    center = (bb.center().X, bb.center().Y, bb.center().Z)
    dist = bb.diagonal * 10 or 1000.0   # 正交投影，距离只需足够远

    views = {}
    for name in ("front", "top"):
        v = _project_one(shape, name, center, dist)
        if v is None:
            bad = diagnose_empty(shape, name, center, dist)
            raise RuntimeError(
                f"{name} 视图投影为空。OCCT HLR 在以下实体上失败："
                f"{[f'#{i}({n}面)' for i, n in bad]}")
        views[name] = v

    order = {"right": ["right"], "left": ["left"], "auto": ["right", "left"]}[side]
    notes = []
    for cand in order:
        v = _project_one(shape, cand, center, dist)
        if v is not None:
            views["side"] = v
            return views, cand, notes
        bad = diagnose_empty(shape, cand, center, dist)
        notes.append((cand, bad))

    raise RuntimeError(f"侧视图两个方向均投影为空。诊断：{notes}")


# ---------------------------------------------------------------- 比例

def snap_scale(s: float) -> float:
    """向下取到最近的标准比例，保证放得下。"""
    ok = [x for x in SCALE_SERIES if x <= s]
    return ok[-1] if ok else SCALE_SERIES[0]


def fmt_scale(s: float) -> str:
    if s >= 1:
        return f"{s:g}:1"
    return f"1:{1 / s:g}"


def parse_scale(txt: str) -> float:
    a, b = txt.split(":")
    return float(a) / float(b)


def auto_scale(views: dict[str, View], area_w: float, area_h: float) -> float:
    """按三视图布局占位反算最大可用比例，再向下取标准值。"""
    f, t, r = views["front"], views["top"], views["side"]
    need_w = f.w + r.w          # 主视图 + 侧视图（模型单位）
    need_h = f.h + t.h          # 主视图 + 俯视图
    s_w = (area_w - VIEW_GAP) / need_w if need_w else 1e9
    s_h = (area_h - VIEW_GAP) / need_h if need_h else 1e9
    return snap_scale(min(s_w, s_h))


# ---------------------------------------------------------------- 布局

def layout(views: dict[str, View], s: float, area: tuple, projection: str, side: str):
    """按第一角/第三角摆放三视图，并整体居中到视图区。

    第三角(ASME，客户在用)：      第一角(GB/ISO)：
            [俯]                      [侧] [主]
            [主] [右侧]                     [俯]
    左视图的摆位与右视图相反（第三角在左，第一角在右）。
    """
    f, t, r = views["front"], views["top"], views["side"]
    fw, fh = f.w * s, f.h * s
    tw, th = t.w * s, t.h * s
    rw, rh = r.w * s, r.h * s

    # 以主视图中心为布局原点
    f.x, f.y = 0.0, 0.0
    dy = fh / 2 + VIEW_GAP + th / 2
    dx = fw / 2 + VIEW_GAP + rw / 2

    # 第三角：右视图在右、左视图在左；第一角全部反过来
    side_sign = 1 if side == "right" else -1
    if projection == "first":
        side_sign = -side_sign

    if projection == "third":
        t.x, t.y = 0.0, dy      # 俯视图在上
    else:
        t.x, t.y = 0.0, -dy     # 俯视图在下
    r.x, r.y = dx * side_sign, 0.0

    # 整体居中
    xs = [f.x - fw / 2, f.x + fw / 2, t.x - tw / 2, t.x + tw / 2, r.x - rw / 2, r.x + rw / 2]
    ys = [f.y - fh / 2, f.y + fh / 2, t.y - th / 2, t.y + th / 2, r.y - rh / 2, r.y + rh / 2]
    gx, gy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    ax0, ay0, ax1, ay1 = area
    ox, oy = (ax0 + ax1) / 2 - gx, (ay0 + ay1) / 2 - gy
    for v in views.values():
        v.x += ox
        v.y += oy

    fits = (max(xs) - min(xs)) <= (ax1 - ax0) and (max(ys) - min(ys)) <= (ay1 - ay0)
    return fits, (max(xs) - min(xs), max(ys) - min(ys))


def place(edges: list, v: View, s: float):
    """把某视图的边集缩放到图纸比例，并平移到目标位置。"""
    if not edges:
        return None
    c = bd.Compound(children=edges)
    c = c.scale(s)                                   # 模型单位 → 图纸 mm
    return bd.Pos(v.x - v.cx * s, v.y - v.cy * s, 0) * c


# ---------------------------------------------------------------- 图框

def frame_geometry(sheet: tuple[float, float]) -> list:
    """图框 + 标题栏外框（作为几何，与图形走同一套导出管线）。"""
    W, H = sheet
    m = FRAME_MARGIN
    rects = [
        [(0, 0), (W, 0), (W, H), (0, H), (0, 0)],                       # 纸边
        [(m, m), (W - m, m), (W - m, H - m), (m, H - m), (m, m)],       # 图框
        [(W - m - TITLE_W, m), (W - m, m),
         (W - m, m + TITLE_H), (W - m - TITLE_W, m + TITLE_H),
         (W - m - TITLE_W, m)],                                          # 标题栏
    ]
    edges = []
    for pts in rects:
        edges += bd.Polyline(*pts).edges()
    # 标题栏内部分隔线
    x0, y0 = W - m - TITLE_W, m
    for yy in (y0 + 18, y0 + 36):
        edges += bd.Line((x0, yy), (W - m, yy)).edges()
    for xx in (x0 + 60, x0 + 120):
        edges += bd.Line((xx, y0), (xx, y0 + TITLE_H)).edges()
    return edges


def view_area(sheet: tuple[float, float]) -> tuple:
    W, H = sheet
    m = FRAME_MARGIN + VIEW_PAD
    return (m, FRAME_MARGIN + TITLE_H + VIEW_PAD, W - m, H - m)


# ---------------------------------------------------------------- 导出

def export_svg(path: Path, layers: dict, sheet, texts):
    """几何走 build123d ExportSVG（保留真实曲线），文字后注入 SVG。"""
    e = ExportSVG(scale=1, margin=0, fit_to_stroke=False)
    e.add_layer("Frame", line_weight=0.35, line_color=ColorIndex.BLACK,
                line_type=LineType.CONTINUOUS)
    e.add_layer("Visible", line_weight=0.50, line_color=ColorIndex.BLACK,
                line_type=LineType.CONTINUOUS)
    e.add_layer("Hidden", line_weight=0.25, line_color=ColorIndex.BLACK,
                line_type=LineType.HIDDEN)
    e.add_layer("Dim", line_weight=0.20, line_color=ColorIndex.BLACK,
                line_type=LineType.CONTINUOUS)
    e.add_layer("DimFill", line_weight=0.20, line_color=ColorIndex.BLACK,
                fill_color=ColorIndex.BLACK)
    for name in ("Frame", "Visible", "Hidden", "Dim", "DimFill"):
        if layers.get(name):
            e.add_shape(layers[name], layer=name)
    e.write(str(path))

    # 注入文字：viewBox 里 CAD 点 (x,y) → (x, -y)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree = ET.parse(path)
    root = tree.getroot()
    g = ET.SubElement(root, "{http://www.w3.org/2000/svg}g")
    g.set("font-family", "Helvetica, Arial, sans-serif")
    g.set("fill", "rgb(0,0,0)")
    for item in texts:
        x, y, txt, size, anchor = item[:5]
        rot = item[5] if len(item) > 5 else 0
        t = ET.SubElement(g, "{http://www.w3.org/2000/svg}text")
        t.set("x", f"{x:.3f}")
        t.set("y", f"{-y:.3f}")
        t.set("font-size", str(size))
        t.set("text-anchor", anchor)
        if rot:
            t.set("transform", f"rotate({-rot} {x:.3f} {-y:.3f})")
        t.text = txt
    tree.write(path, encoding="utf-8", xml_declaration=True)


def export_dxf(path: Path, layers: dict):
    e = ExportDXF(unit=bd.Unit.MM)
    e.add_layer("Frame", color=ColorIndex.BLACK, line_type=LineType.CONTINUOUS)
    e.add_layer("Visible", color=ColorIndex.BLACK, line_type=LineType.CONTINUOUS)
    e.add_layer("Hidden", color=ColorIndex.BLACK, line_type=LineType.HIDDEN)
    e.add_layer("Dim", color=ColorIndex.BLACK, line_type=LineType.CONTINUOUS)
    for name in ("Frame", "Visible", "Hidden", "Dim"):
        if layers.get(name):
            e.add_shape(layers[name], layer=name)
    e.write(str(path))


def export_pdf(svg_path: Path, pdf_path: Path, sheet):
    """纯 Python 的 SVG→PDF（svglib+reportlab），不依赖系统 cairo。"""
    from reportlab.graphics import renderPDF
    from svglib.svglib import svg2rlg

    drawing = svg2rlg(str(svg_path))
    W, H = sheet
    pt = 72 / 25.4                       # mm → pt
    drawing.scale(W * pt / drawing.width, H * pt / drawing.height)
    drawing.width, drawing.height = W * pt, H * pt
    renderPDF.drawToFile(drawing, str(pdf_path))


# ---------------------------------------------------------------- 主流程

def build_texts(sheet, s, projection, name, views, side, label_offs=None):
    """标题栏文字 + 视图标签。审核人/材料等留占位，待 PLM 接入。"""
    W, H = sheet
    x0, y0 = W - FRAME_MARGIN - TITLE_W, FRAME_MARGIN
    label = {"front": "FRONT", "top": "TOP", "right": "RIGHT", "left": "LEFT"}
    # 标题栏格宽 60mm，按字宽估算截断，避免压到相邻格
    title = name.upper()
    fs = 5.0
    while len(title) * fs * 0.58 > 56 and fs > 2.2:
        fs -= 0.25
    if len(title) * fs * 0.58 > 56:
        title = title[:int(56 / (fs * 0.58)) - 1] + "…"
    texts = [
        (x0 + 3, y0 + 41, "TITLE", 2.5, "start"),
        (x0 + 3, y0 + 30, title, fs, "start"),
        (x0 + 63, y0 + 41, "SCALE", 2.5, "start"),
        (x0 + 63, y0 + 30, fmt_scale(s), 5.0, "start"),
        (x0 + 123, y0 + 41, "PROJECTION", 2.5, "start"),
        (x0 + 123, y0 + 30, "THIRD ANGLE" if projection == "third" else "FIRST ANGLE", 4.0, "start"),
        (x0 + 3, y0 + 12, "DESIGNED / CHECKED / APPROVED", 2.5, "start"),
        (x0 + 3, y0 + 5, "<- 待 PLM 接入", 2.5, "start"),
        (x0 + 63, y0 + 12, "DWG NO.", 2.5, "start"),
        (x0 + 63, y0 + 5, "<- 待 PLM 接入", 2.5, "start"),
        (x0 + 123, y0 + 12, "MATERIAL / REV", 2.5, "start"),
        (x0 + 123, y0 + 5, "<- 待 PLM 接入", 2.5, "start"),
        (W / 2, H - FRAME_MARGIN - 5, "DIMENSIONS IN MILLIMETERS", 2.5, "middle"),
    ]
    for key, v in views.items():
        off = (label_offs or {}).get(key, 5.0)
        texts.append((v.x, v.y - v.h * s / 2 - off, label[v.name], 3.0, "middle"))
    return texts


# ---------------------------------------------------------------- 标注边界演示

def step_schema(path: Path) -> str:
    """读 STEP 头部的协议版本。AP203/AP214 均无 PMI，只有 AP242 承载语义 PMI。"""
    try:
        head = path.read_text(errors="replace")[:2000]
    except Exception:
        return "未知"
    import re
    m = re.search(r"FILE_SCHEMA\s*\(\(\s*'([^']+)'", head)
    d = re.search(r"FILE_DESCRIPTION\s*\(\(\s*'([^']+)'", head)
    schema = m.group(1) if m else "?"
    desc = d.group(1) if d else ""
    return f"{desc} ({schema})" if desc else schema


def measurable_features(shape: bd.Shape, views: dict[str, View]) -> dict:
    """从模型/投影里提取"可测量"的候选尺寸。

    这个函数的意义是划清边界：几何数值可以全自动提取（下面这些都是算出来的），
    但"标哪几个""公差多少"不在模型里——除非模型带 PMI（STEP AP242）。
    客户 Submount 图纸上 5 个尺寸有 3 个偏离默认公差表，即为佐证。
    """
    bb = shape.bounding_box()
    dims = {"总长 (X)": bb.size.X, "总宽 (Y)": bb.size.Y, "总高 (Z)": bb.size.Z}

    circles = {}
    for v in views.values():
        for e in v.vis:
            try:
                if e.geom_type == bd.GeomType.CIRCLE:
                    d = round(e.radius * 2, 4)
                    circles[d] = circles.get(d, 0) + 1
            except (AttributeError, ValueError):
                continue

    has_pmi = False
    try:  # STEP AP242 的语义 PMI 走 OCCT XDE，AP214 里必然没有
        has_pmi = bool(getattr(shape, "pmi", None))
    except Exception:
        pass
    return {"linear": dims, "circles": circles, "pmi": has_pmi}


def report_boundary(feat: dict, schema: str = "未知"):
    print("\n--- 标注可行性边界（自动分析） ---")
    print("  ✅ 可自动提取的几何数值：")
    for k, v in feat["linear"].items():
        print(f"       {k:10} = {v:.3f} mm")
    if feat["circles"]:
        print("     候选圆特征（跨三视图统计，含圆角投影）：")
        for d, n in sorted(feat["circles"].items()):
            print(f"       ⌀{d:<9.3f} x {n} 处")
    print("  ❌ 无法从几何推出（必须来自 PMI 或工程师）：")
    print(f"       · 这些尺寸里该标哪几个   —— 设计意图")
    print(f"       · 每个尺寸的公差         —— 功能决定")
    print(f"       · 基准 / 关键特性编号     —— 质量工程")
    ap242 = "242" in schema
    print(f"  STEP 协议：{schema}")
    if feat["pmi"]:
        print("  语义 PMI：检出 -> 公差可迁移")
    elif ap242:
        print("  语义 PMI：未检出（协议支持 AP242，但模型侧没做 MBD 标注）")
    else:
        print(f"  语义 PMI：不可能有 —— {schema} 协议本身不承载 PMI，需改导 AP242")


def main():
    ap = argparse.ArgumentParser(description="STEP → 2D 三视图工程图")
    ap.add_argument("step", help="输入 STEP 文件")
    ap.add_argument("-o", "--outdir", default="out")
    ap.add_argument("--sheet", default="A4", choices=list(SHEETS))
    ap.add_argument("--scale", default="auto", help="auto 或 20:1 / 1:2")
    ap.add_argument("--projection", default="third", choices=["third", "first"],
                    help="third=第三角(客户在用) / first=第一角(GB)")
    ap.add_argument("--side", default="auto", choices=["auto", "right", "left"],
                    help="侧视图取向。auto=先试右视图，若 OCCT HLR 返回空则回退左视图")
    ap.add_argument("--no-hidden", action="store_true",
                    help="不画隐藏线。装配体强烈建议开启——真实装配图不画隐藏线，"
                         "且可大幅提速（隐藏边通常占总量 90%%以上）")
    ap.add_argument("--dimensions", action="store_true",
                    help="标注外形包围尺寸（总长/总宽/总高）。公差留空——"
                         "AP214 输入无 PMI 数据源，不臆造")
    ap.add_argument("--feature-dims", action="store_true",
                    help="标注台阶/分隔点尺寸链（客户 Submount 图纸 0.44/0.70 那类）")
    ap.add_argument("--feature-max", type=int, default=6, metavar="N",
                    help="单轴分隔点上限，超过则整条链放弃（默认 6）")
    ap.add_argument("--feature-min-len", type=float, default=0.15, metavar="R",
                    help="分隔边的最小跨度（占视图尺寸比例）。调大=只取主结构台阶")
    ap.add_argument("--feature-min-gap", type=float, default=4.0, metavar="MM",
                    help="链上单段的最小图纸可读长度(mm)。设 0 = 关闭可读性过滤，"
                         "用于演示「强行标全部分隔点」的后果")
    ap.add_argument("--feature-merge", type=float, default=0.02, metavar="R",
                    help="分隔点合并容差(占视图尺寸比例)。倒角两侧合成理论尖角，"
                         "符合制图惯例（默认 0.02）")
    ap.add_argument("--pmi", action="store_true",
                    help="从 STEP AP242 读语义 PMI，把公差**迁移**到图上（非从几何生成）。"
                         "AP203/AP214 无此数据源")
    ap.add_argument("--serial", action="store_true",
                    help="串行跑三视图（默认并行。实测 LSB400 并行 1.71x）")
    ap.add_argument("--analyze", action="store_true",
                    help="附带输出标注可行性边界分析")
    args = ap.parse_args()

    src = Path(args.step)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    sheet = SHEETS[args.sheet]

    import time
    t0 = time.perf_counter()

    shape = bd.import_step(str(src))
    bb = shape.bounding_box()
    t_read = time.perf_counter()

    assert_brep(shape, src)          # 无 B-rep 直接拦截，否则 OCCT HLR 会段错误
    views, side, notes = (project_views(shape, args.side) if args.serial
                          else project_views_parallel(src, shape, args.side))
    t_hlr = time.perf_counter()

    area = view_area(sheet)
    aw, ah = area[2] - area[0], area[3] - area[1]
    s = auto_scale(views, aw, ah) if args.scale == "auto" else parse_scale(args.scale)
    fits, used = layout(views, s, area, args.projection, side)

    # 分层收集几何
    layers = {"Frame": frame_geometry(sheet), "Visible": [], "Hidden": [],
              "Dim": [], "DimFill": []}
    n_vis = n_hid = 0
    pairs = [("Visible", "vis")] + ([] if args.no_hidden else [("Hidden", "hid")])
    for v in views.values():
        for key, attr in pairs:
            c = place(getattr(v, attr), v, s)
            if c is not None:
                layers[key] += c.edges()
        n_vis += len(v.vis)
        n_hid += len(v.hid)

    stem = src.stem
    texts = []

    # 语义 PMI：有则把公差迁移到图上；无则尺寸只有数值（不臆造）
    P = pmi_mod.read(src) if args.pmi else pmi_mod.Pmi()
    tol_src = P.lookup if P else None

    # 先生成尺寸标注，再据此决定视图标签往下让多少
    f_stat = {k: ("", "", False) for k in views}
    if args.feature_dims:
        f_e, f_f, f_t, f_stat = dims.feature_dims(
            views, s, side, tol_source=tol_src,
            max_per_axis=args.feature_max, min_len_ratio=args.feature_min_len,
            min_gap_paper=args.feature_min_gap, merge_ratio=args.feature_merge)
        layers["Dim"] += f_e
        layers["DimFill"] += f_f
        texts += f_t
    # ① PMI 驱动优先：设计者标了什么关键特征，图上就标什么（不从几何猜）
    pmi_hits, pmi_covered = [], set()
    if args.pmi and P:
        p_e, p_f, p_t, pmi_hits, pmi_covered = dims.pmi_feature_dims(views, s, P)
        layers["Dim"] += p_e
        layers["DimFill"] += p_f
        texts += p_t

    # ② 几何驱动的外形尺寸让位：PMI 已覆盖的不再重复标
    if args.dimensions:
        d_e, d_f, d_t = dims.envelope_dims(views, s, side, bb.size,
                                           tol_source=tol_src, skip=pmi_covered)
        layers["Dim"] += d_e
        layers["DimFill"] += d_f
        texts += d_t

    # 视图标签逐个避让：只有下方真有尺寸线的视图才下移
    #   外形总长在主视图下方 off=16 -> 标签 21
    #   台阶链在有内部分隔点的视图下方 off=6.5 -> 标签 11
    label_offs = {}
    for key in views:
        off = 5.0
        if args.feature_dims and f_stat[key][2]:      # 该视图下方真画了水平链
            off = max(off, 11.0)
        if args.dimensions and key == "front":
            off = max(off, 21.0)
        label_offs[key] = off
    texts += build_texts(sheet, s, args.projection, stem, views, side, label_offs)
    svg, pdf, dxf = outdir / f"{stem}.svg", outdir / f"{stem}.pdf", outdir / f"{stem}.dxf"
    export_svg(svg, layers, sheet, texts)
    export_dxf(dxf, layers)
    export_pdf(svg, pdf, sheet)
    t_end = time.perf_counter()

    print(f"输入      {src}   {bb.size.X:.2f} x {bb.size.Y:.2f} x {bb.size.Z:.2f} mm")
    print(f"投影法    {'第三角 (ASME)' if args.projection == 'third' else '第一角 (GB/ISO)'}")
    print(f"图幅      {args.sheet} ({sheet[0]:.0f} x {sheet[1]:.0f})   视图区 {aw:.0f} x {ah:.0f}")
    print(f"比例      {fmt_scale(s)}   {'(自动)' if args.scale == 'auto' else '(指定)'}"
          f"   占位 {used[0]:.1f} x {used[1]:.1f} mm  {'✓ 放得下' if fits else '✗ 超出'}")
    print(f"侧视图    {side.upper()}" + (
        "" if not notes else
        f"   ⚠ 回退：{notes[0][0]} 视图 OCCT HLR 返回空，"
        f"失败实体 {[f'#{i}({n}面)' for i, n in notes[0][1]]}"))
    n_drop = sum(v.dropped for v in views.values())
    if n_drop:
        print(f"⚠ 过滤    剔除 {n_drop} 条越界垃圾边（OCCT HLR 缺陷，"
              f"投影超出模型包围盒，几何上不可能）")
    print(f"边数      可见 {n_vis}  隐藏 {n_hid}"
          + ("   (隐藏线未绘制 --no-hidden)" if args.no_hidden else ""))
    print(f"耗时      读STEP {t_read - t0:.3f}s | HLR投影 {t_hlr - t_read:.3f}s | "
          f"组装导出 {t_end - t_hlr:.3f}s | 合计 {t_end - t0:.3f}s")
    print(f"输出      {svg}\n          {pdf}\n          {dxf}")
    if args.feature_dims:
        print("\n--- 台阶/分隔点尺寸链 ---")
        for k, (rx, ry, _) in f_stat.items():
            print(f"   {k:6} 水平链 {rx:32} 垂直链 {ry}")
    if args.pmi:
        print(f"\n--- 语义 PMI（STEP AP242）---")
        if not P:
            print(f"   协议 {P.schema or '?'} -> 未检出任何 PMI")
        else:
            print(f"   协议 {P.schema[:46]}")
            print(f"   语义尺寸 {len(P.dims)} 条 | 几何公差 {len(P.gtols)} 条 | 基准 {P.datums}")
            for d in P.dims:
                print(f"     {d.label()}")
            for g in P.gtols:
                print(f"     {pmi_mod.GTOL_SYMBOL.get(g.kind, ('', g.kind))[1]:7} {g.label()}")
            amb = P.ambiguous()
            if amb:
                print(f"   ⚠ 同一公称值对应多种公差（按数值无法消歧，故不迁移）：")
                for n, ts in amb:
                    print(f"     {n:g} -> {sorted(t or '无' for t in ts)}")
            if pmi_hits:
                ok = [(d, v) for d, v, _ in pmi_hits if v]
                miss = [(d, r) for d, v, r in pmi_hits if not v]
                print(f"   PMI 驱动标注：{len(ok)}/{len(pmi_hits)} 条关键特征已落到图上")
                for d, v in ok:
                    print(f"     ✓ {d.label():18} -> {v} 视图")
                for d, r in miss:
                    print(f"     ✗ {d.label():18} -> {r}")
                if pmi_covered:
                    print(f"   几何驱动的外形尺寸已让位（避免重复）：{sorted(pmi_covered)}")
    if args.analyze:
        report_boundary(measurable_features(shape, views), step_schema(src))


if __name__ == "__main__":
    main()
