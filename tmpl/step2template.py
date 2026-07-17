"""图纸模板(DXF) + 真实模型(STEP) -> 成品图纸。

与另外两条管道的区别：这里**不生成标注**——标注是模板里画好的、写死的。
我们只做三件事：把模板里的旧视图几何换成新模型的投影、填标题栏、出图。

链路：
  ① 读模板 DXF，按图层分离「旧视图几何」与「标注/图框/标题栏」
  ② 从旧视图几何反推每个视图的**中心**与**比例**（不写死，模板说了算）
  ③ 读 STEP，HLR 生成三视图
  ④ 删旧视图几何，把新投影按 ②的中心/比例落位 —— 标注纹丝不动
  ⑤ 填标题栏占位符（<KEY> 形式）
  ⑥ 校验：模板写死的尺寸 vs 模型实测，不一致则告警

⚠️ 边界：模板里的标注是**静态文字**，换模型时不会跟着变。⑥ 只能校验
   「被标注覆盖且能用包围盒测量」的尺寸；特征尺寸（如台阶 0.44）测不了。
"""
from __future__ import annotations

import re
from pathlib import Path

import build123d as bd
import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext, layout, svg
from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration

import step2drawing as s2d

VIEW_LAYER = "VIEW_GEOM"          # 模板里存放「旧视图几何」的图层（待客户真实导出后再定）
BG_COLOR = "#eaeae2"              # 出图底色（客户图纸观感）。背景不在 DXF 里，是渲染器设置。
FILL_RGB = (230, 120, 0)          # 程序填入字段的标记色（橙）——便于演示时区分模板/填充
PLACEHOLDER = re.compile(r"<([A-Z_]+)>")


# ── 扫描模板里的占位符：不同模板字段不同，由模板自己决定表单长什么样 ──
def scan(template_path) -> dict:
    doc = ezdxf.readfile(str(template_path))
    msp = doc.modelspace()
    fields, seen = [], set()
    for e in msp:
        if e.dxftype() != "TEXT":
            continue
        m = PLACEHOLDER.fullmatch(e.dxf.text.strip())
        if not m or m.group(1) in seen:
            continue
        key = m.group(1)
        seen.add(key)
        p = tuple(e.dxf.insert)[:2]
        fields.append({"key": key, "x": round(p[0], 2), "y": round(p[1], 2),
                       "height": round(e.dxf.height, 2),
                       "is_date": "DATE" in key})       # 含 DATE -> 前端给日期控件
    fields.sort(key=lambda f: (-f["y"], f["x"]))        # 按图面自上而下、自左而右
    boxes = cluster_views(msp)
    layers = sorted({e.dxf.layer for e in msp})
    return {"fields": fields, "n_views": len(boxes), "layers": layers,
            "has_view_layer": VIEW_LAYER in layers}


# ── ① 聚类：把旧视图几何分成若干视图框 ──
def cluster_views(msp, layer=VIEW_LAYER, gap=8.0) -> list:
    """返回 [[x1,x2,y1,y2,n_edges], ...]，每个 = 一个视图在图纸上的范围。

    ⚠️ 现在靠图层名认「旧视图几何」。真实 SolidWorks 导出若把视图放进 BLOCK，
       按块分离会更稳——等拿到客户真实 DXF 再定。
    """
    items = []
    for e in msp:
        if e.dxf.layer != layer:
            continue
        t = e.dxftype()
        if t == "LINE":
            a, b = tuple(e.dxf.start)[:2], tuple(e.dxf.end)[:2]
            xs, ys = (a[0], b[0]), (a[1], b[1])
        elif t == "CIRCLE":
            # ⚠️ 必须纳入圆/圆弧：否则外轮廓是圆的视图（如法兰盘俯视图）包围盒会算小，
            #    进而把反推比例算错。只看 LINE 会漏掉整圈边界。
            c, r = tuple(e.dxf.center)[:2], e.dxf.radius
            xs, ys = (c[0] - r, c[0] + r), (c[1] - r, c[1] + r)
        elif t == "ARC":
            # ⚠️ 圆弧不能用整圆外接框：一段大半径小圆弧（环面倒角投影）的 圆心±半径
            #    会伸到视图外很远，把聚类框撑大。按实际起止角采样端点求真实范围。
            import math
            c, r = tuple(e.dxf.center)[:2], e.dxf.radius
            a0, a1 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
            if a1 < a0:
                a1 += 2 * math.pi
            angs = [a0 + (a1 - a0) * k / 8 for k in range(9)]
            px = [c[0] + r * math.cos(a) for a in angs]
            py = [c[1] + r * math.sin(a) for a in angs]
            xs, ys = (min(px), max(px)), (min(py), max(py))
        else:
            continue
        items.append([min(xs), max(xs), min(ys), max(ys)])
    groups = []
    for it in sorted(items):
        for g in groups:
            if not (it[0] > g[1] + gap or it[1] < g[0] - gap or
                    it[2] > g[3] + gap or it[3] < g[2] - gap):
                g[0], g[1] = min(g[0], it[0]), max(g[1], it[1])
                g[2], g[3] = min(g[2], it[2]), max(g[3], it[3])
                g[4] += 1
                break
        else:
            groups.append([it[0], it[1], it[2], it[3], 1])
    return groups


# ── ② 模板视图框 × 新视图 配对，并反推比例 ──
def match(boxes, views, tol=0.06) -> list:
    """按投影宽高比配对。返回 [(view_name, box, scale), ...]。

    比例由「模板框宽 ÷ 模型投影宽」推出——模板说了算，不写死。
    两个方向(X/Y)独立推出的比例应当一致，不一致说明配错了。
    """
    pairs, used = [], set()
    for name, v in views.items():
        if v.w <= 0 or v.h <= 0:
            continue
        best, best_err, best_s = None, 1e9, 0.0
        for i, g in enumerate(boxes):
            if i in used:
                continue
            sx, sy = (g[1] - g[0]) / v.w, (g[3] - g[2]) / v.h
            err = abs(sx - sy) / max(sx, sy)
            if err < best_err:
                best, best_err, best_s = i, err, (sx + sy) / 2
        if best is not None and best_err <= tol:
            used.add(best)
            pairs.append((name, boxes[best], best_s))
    return pairs


# ── ⑥ 校验：模板写死的尺寸 vs 模型实测包围盒 ──
def verify_dims(msp, shape) -> tuple[list, list]:
    bb = shape.bounding_box()
    measured = sorted([round(bb.size.X, 3), round(bb.size.Y, 3), round(bb.size.Z, 3)])
    nominal = []
    for e in msp:
        if e.dxf.layer == "DIM" and e.dxftype() == "TEXT":
            m = re.match(r"([\d.]+)\s*±\s*([\d.]+)", e.dxf.text.strip())
            if m:
                nominal.append((float(m.group(1)), float(m.group(2))))
    out = []
    for val, tol in sorted(nominal, reverse=True):
        hit = next((mv for mv in measured if abs(mv - val) <= tol), None)
        near = min(measured, key=lambda mv: abs(mv - val)) if measured else 0.0
        out.append({"nominal": val, "tol": tol, "matched": hit,
                    "nearest": near, "delta": round(abs(near - val), 3),
                    "ok": hit is not None})
    return out, measured


def check_scale_text(msp, derived: float):
    """模板里若写死了比例文字（如 "10:1"），校验它与**从视图几何反推的比例**是否一致。

    为什么要查：标题栏的比例是静态文字，改了视图画法它不会跟着变。客户实际图纸上
    就出现过标题栏写 20:1、图面实为 ~10:1 的矛盾 —— 这类「模板说谎」只能靠比对发现。
    """
    for e in msp:
        if e.dxftype() != "TEXT":
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*:\s*1", e.dxf.text.strip())
        if not m:
            continue
        written = float(m.group(1))
        return {"written": written, "derived": round(derived, 3),
                "ok": abs(written - derived) / max(written, derived) < 0.01}
    return None


def fill(doc, fields: dict) -> tuple[list, list]:
    """把 <KEY> 占位符替换成字段值。**很快**，与 build() 分开是为了「重新生成」时
    不必重跑 HLR —— 换视图几何只做一次，改字段只走这里。

    填入的字段刷成橙色，便于在图上区分「模板原有内容」与「程序填充」。
    未提供值的占位符**保持原样**（预览时能直接看到 <DWG_NO> 这种占位符）。
    """
    filled, missing = [], []
    for e in doc.modelspace():
        if e.dxftype() != "TEXT":
            continue
        m = PLACEHOLDER.fullmatch(e.dxf.text.strip())
        if not m:
            continue
        key = m.group(1)
        val = fields.get(key)
        if val:
            e.dxf.text = str(val)
            e.rgb = FILL_RGB
            filled.append(key)
        else:
            missing.append(key)
    return filled, missing


def render(doc, sheet=(297.0, 210.0)) -> str:
    """渲染成 SVG。背景色是**渲染器设置**，不在 DXF 里；
    DXF 的 7 号色随背景反转，浅底自动显黑，故图框/视图线自己变黑。"""
    cfg = Configuration(background_policy=BackgroundPolicy.CUSTOM, custom_bg_color=BG_COLOR)
    b = svg.SVGBackend()
    Frontend(RenderContext(doc), b, config=cfg).draw_layout(doc.modelspace())
    return b.get_string(layout.Page(sheet[0], sheet[1], layout.Units.mm))


def _write_edge(msp, e, layer=VIEW_LAYER, seg=48) -> int:
    """把一条投影边写进 DXF，返回写入的实体数。

    按边的真实类型写对应的 DXF 实体，**不要一律离散成折线**：

    · 直线 -> LINE。注意不能对所有边都「取首尾顶点连直线」：**圆等闭合边只有
      1 个顶点**（实测 build123d 圆边 vertices() 长度为 1），那样整条边会被丢掉，
      带孔的零件孔就没了。
    · 圆   -> CIRCLE。离散成 48 边形有两个害处：① 拿回 CAD 一量不是真圆，与
      「精确 B-rep 出图、圆是数学真圆」的前提自相矛盾；② 体积爆炸（实测某复杂件
      859 条可见边 -> 36,062 条线 / 6.1MB）。
    · 圆弧 -> ARC。同理。
    · 其余（椭圆/样条…）才退化为折线。
    """
    gt = e.geom_type
    if gt == bd.GeomType.LINE:
        vs = e.vertices()
        if len(vs) >= 2:
            a, b = tuple(vs[0]), tuple(vs[-1])
            msp.add_line((a[0], a[1]), (b[0], b[1]), dxfattribs={"layer": layer})
            return 1
        return 0

    if gt == bd.GeomType.CIRCLE:
        try:
            c = e.arc_center
            r = e.radius
            # 投影后仍是圆 => 其所在平面平行于纸面，可安全写成 DXF 真圆/圆弧
            if e.is_closed:
                msp.add_circle((c.X, c.Y), r, dxfattribs={"layer": layer})
            else:
                p0, p1 = tuple(e @ 0), tuple(e @ 1)
                import math
                a0 = math.degrees(math.atan2(p0[1] - c.Y, p0[0] - c.X))
                a1 = math.degrees(math.atan2(p1[1] - c.Y, p1[0] - c.X))
                msp.add_arc((c.X, c.Y), r, a0, a1, dxfattribs={"layer": layer})
            return 1
        except Exception:
            pass                            # 拿不到圆心/半径就退化为折线

    try:                                    # 其余曲线：离散成折线（闭合边首尾自然相接）
        pts = [tuple(e @ (i / seg)) for i in range(seg + 1)]
    except Exception:
        return 0
    n = 0
    for p, q in zip(pts, pts[1:]):
        if abs(p[0] - q[0]) > 1e-9 or abs(p[1] - q[1]) > 1e-9:
            msp.add_line((p[0], p[1]), (q[0], q[1]), dxfattribs={"layer": layer})
            n += 1
    return n


def build(step_path, template_path, side_pref="right"):
    """换视图几何：模板 + 模型 -> (doc, info)。占位符**保持原样**，字段另由 fill() 填。

    这步含 HLR 投影，是慢的一步，故与 fill() 分开：只在换模型/模板时跑一次。
    """
    doc = ezdxf.readfile(str(template_path))
    msp = doc.modelspace()

    boxes = cluster_views(msp)
    if not boxes:
        raise ValueError(f"模板里找不到图层 {VIEW_LAYER} 的视图几何 —— "
                         f"现有图层: {sorted({e.dxf.layer for e in msp})}")

    shape = bd.import_step(str(step_path))
    solids = shape.solids()
    if not solids:
        raise ValueError("STEP 里没有实体（可能是纯网格模型），无法投影出图")
    draw = bd.Compound(children=solids)      # 只投影实体，剔除 PMI 折线/构造几何
    views, side, _ = s2d.project_views(draw, side_pref)

    pairs = match(boxes, views)
    if not pairs:
        raise ValueError("模板视图框与模型投影无法配对（宽高比对不上）—— "
                         "可能模板与模型不是同一个零件")
    scales = [s for _, _, s in pairs]
    spread = (max(scales) - min(scales)) / max(scales) if scales else 0.0

    checks, measured = verify_dims(msp, draw)

    # ④ 剥旧视图 -> 落新投影
    doomed = [e for e in msp if e.dxf.layer == VIEW_LAYER]
    for e in doomed:
        msp.delete_entity(e)
    n_new = 0
    placed_info = []
    for name, g, s in pairs:
        v = views[name]
        v.x, v.y = (g[0] + g[1]) / 2, (g[2] + g[3]) / 2
        c = s2d.place(v.vis, v, s)
        if c is None:
            continue
        for e in c.edges():
            n_new += _write_edge(msp, e)
        placed_info.append({"view": name, "cx": round(v.x, 2), "cy": round(v.y, 2),
                            "scale": round(s, 3),
                            "w": round(v.w * s, 2), "h": round(v.h * s, 2)})

    derived = sum(scales) / len(scales)
    info = {
        "side": side,
        "n_template_views": len(boxes),
        "views": placed_info,
        "scale": round(derived, 3),
        "scale_spread_pct": round(spread * 100, 3),
        "scale_consistent": spread < 0.01,
        "scale_text": check_scale_text(msp, derived),   # 模板写死的比例 vs 反推比例
        "n_old_edges": len(doomed),
        "n_new_edges": n_new,
        "measured_bbox": measured,
        "dim_checks": checks,
        "n_dim_ok": sum(1 for c in checks if c["ok"]),
        "n_dim_unverifiable": sum(1 for c in checks if not c["ok"]),
    }
    return doc, info
