"""尺寸标注引擎。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本模块只做「能确定性算出来」的那部分标注，边界如下：

  ✅ 外形包围尺寸（总长/总宽/总高）
       —— 来自 bounding box，无歧义、无需工程判断。
       —— 这正是光模块「外形图 / Outline Drawing」的核心内容
          （QSFP-DD / OSFP 等 MSA 规范就是按外形尺寸定义的）。

  ⚠️ 特征尺寸（孔径、孔位、间距）
       —— 数值可自动提取，但「标哪几个」需要规则库。

  ❌ 公差
       —— 几何里没有。只能来自模型 PMI（需 STEP AP242）或工程师。
          客户当前导出为 AP214，PMI 数据源不存在。
          实证：其 Submount 图纸 5 个尺寸有 3 个偏离默认公差表
          （1.86±0.02 严 2.5 倍、0.44±0.1 松 2 倍），因为 S1/S2 是键合面。

  ❌ 基准 / 关键特性编号（①②③）
       —— 质量工程决定，不是几何。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import build123d as bd

# 标注样式（图纸 mm，符合 ISO 129 常规比例）
ARROW_LEN = 2.4
ARROW_HALF_W = 0.42
EXT_GAP = 1.0          # 尺寸界线起点与实体的间隙
EXT_OVER = 1.5         # 尺寸界线超出尺寸线的长度
TEXT_SIZE = 2.8
TEXT_LIFT = 0.9        # 文字基线到尺寸线的距离


def _arrow(tip: tuple, dx: float, dy: float):
    """实心箭头。tip 为尖点，(dx,dy) 为指向（单位向量）。"""
    bx, by = tip[0] - dx * ARROW_LEN, tip[1] - dy * ARROW_LEN
    px, py = -dy * ARROW_HALF_W, dx * ARROW_HALF_W
    return bd.make_face(
        bd.Polyline(tip, (bx + px, by + py), (bx - px, by - py), close=True))


def linear_h(x1: float, x2: float, y_ref: float, offset: float, text: str):
    """水平线性尺寸。

    x1,x2   被测两点的 X
    y_ref   实体边界的 Y（尺寸界线起点）
    offset  尺寸线相对 y_ref 的偏移（负=画在下方）
    """
    y = y_ref + offset
    sgn = 1 if offset > 0 else -1
    edges, faces = [], []

    # 尺寸界线
    for x in (x1, x2):
        edges += bd.Line((x, y_ref + sgn * EXT_GAP), (x, y + sgn * EXT_OVER)).edges()
    # 尺寸线
    edges += bd.Line((x1, y), (x2, y)).edges()
    # 箭头（内向）
    faces.append(_arrow((x1, y), -1, 0))
    faces.append(_arrow((x2, y), 1, 0))

    txt = ((x1 + x2) / 2, y + TEXT_LIFT, text, TEXT_SIZE, "middle")
    return edges, faces, txt


def linear_v(y1: float, y2: float, x_ref: float, offset: float, text: str):
    """垂直线性尺寸。文字旋转 90°，符合 ISO 129 竖直尺寸的读法。"""
    x = x_ref + offset
    sgn = 1 if offset > 0 else -1
    edges, faces = [], []

    for y in (y1, y2):
        edges += bd.Line((x_ref + sgn * EXT_GAP, y), (x + sgn * EXT_OVER, y)).edges()
    edges += bd.Line((x, y1), (x, y2)).edges()
    faces.append(_arrow((x, y1), 0, -1))
    faces.append(_arrow((x, y2), 0, 1))

    txt = (x - TEXT_LIFT, (y1 + y2) / 2, text, TEXT_SIZE, "middle", 90)
    return edges, faces, txt


def fmt(v: float, tol: str | None = None) -> str:
    """尺寸文本。tol=None 表示无公差来源——刻意留空，不臆造。"""
    s = f"{v:.2f}"
    return f"{s} {tol}" if tol else s


# ────────────────────────────────────────────────────────────── 分隔点提取

def division_points(view, axis: str, min_len: float = 0.0, tol: float = 1e-3,
                    merge: float = 0.0):
    """从投影边集提取「分隔点」坐标。

    axis='v'：找垂直边 → 返回其 X 坐标（供水平尺寸链用）
    axis='h'：找水平边 → 返回其 Y 坐标（供垂直尺寸链用）

    原理：工程图上的「台阶尺寸」标的就是内部轴向边的位置。投影后这些边
    在 2D 里是轴对齐的直线段，其坐标即分隔点。相邻分隔点之差 = 台阶尺寸。
    客户 Submount 图纸的 0.44 / 0.70、RX DA 图纸的 0.40 / 0.70 / 4.17
    都是这么来的（前者链式、后者坐标式）。

    merge：分隔点合并容差。**这一条不是数值技巧，是工程制图的规则**——
    倒角/圆角会产生两条相距很近的长平行边（实测 LSB400 的 1mm 倒角，
    每条链首尾都冒出 1.0mm 的段）。而真实图纸标的是**理论尖角**，倒角
    另注 "C1"，绝不会给倒角两侧各标一道尺寸。故近距分隔点应合并为一个。
    """
    coords = []
    for e in view.vis:                       # 只取可见边，隐藏边不参与标注
        try:
            if e.geom_type != bd.GeomType.LINE:
                continue
        except (AttributeError, ValueError):
            continue
        bb = e.bounding_box()
        if axis == "v":
            if bb.size.X <= tol and bb.size.Y >= min_len:
                coords.append(bb.center().X)
        else:
            if bb.size.Y <= tol and bb.size.X >= min_len:
                coords.append(bb.center().Y)

    if not coords:
        return []
    coords.sort()
    thresh = max(merge, tol * 10, 1e-3)

    # 分组：距离小于 thresh 的连续坐标归为一组，取组内极值端（贴近理论尖角）
    groups, cur = [], [coords[0]]
    for c in coords[1:]:
        if c - cur[-1] <= thresh:
            cur.append(c)
        else:
            groups.append(cur)
            cur = [c]
    groups.append(cur)

    # 每组取靠外的那个端点：首组取最小、末组取最大、中间组取中位
    out = []
    for i, g in enumerate(groups):
        if i == 0:
            out.append(g[0])
        elif i == len(groups) - 1:
            out.append(g[-1])
        else:
            out.append(g[len(g) // 2])
    return out


def chain_dims(view, s: float, axis: str, pts: list, ref: float, offset: float,
               tol_source=None, min_gap_paper: float = 4.0):
    """相邻分隔点之间的链式尺寸（客户 Submount 图纸的标注方式）。

    min_gap_paper: 单段在图纸上的最小可读长度（mm）。

    尺寸链是「全画或不画」：缺一段则链条累加对不上总长，工程上是错的。
    因此只要有一段在当前比例下挤到不可读，整条链放弃。
    """
    gaps = [b - a for a, b in zip(pts, pts[1:])]
    if not gaps or min(gaps) * s < min_gap_paper:
        return [], [], []

    edges, faces, texts = [], [], []
    for a, b in zip(pts, pts[1:]):
        va, vb = view.x + (a - view.cx) * s, view.x + (b - view.cx) * s
        if axis == "v":                      # 水平尺寸
            va = view.x + (a - view.cx) * s
            vb = view.x + (b - view.cx) * s
            e, f, t = linear_h(va, vb, ref, offset,
                               fmt(b - a, tol_source(b - a) if tol_source else None))
        else:                                # 垂直尺寸
            va = view.y + (a - view.cy) * s
            vb = view.y + (b - view.cy) * s
            e, f, t = linear_v(va, vb, ref, offset,
                               fmt(b - a, tol_source(b - a) if tol_source else None))
        edges += e
        faces += f
        texts.append(t)
    return edges, faces, texts


def feature_dims(views: dict, s: float, side: str, tol_source=None,
                 max_per_axis: int = 6, min_len_ratio: float = 0.15,
                 min_gap_paper: float = 4.0, merge_ratio: float = 0.02):
    """三视图的台阶/分隔点尺寸链。

    max_per_axis: 单轴分隔点上限。超过则跳过——这正是全行业「规整零件」
                  限定的由来：分隔点一多，链式标注就挤成不可读的一团，
                  该标哪几个变回工程判断。
    min_len_ratio: 边长下限（占视图尺寸的比例），滤掉倒角/圆角碎边。
    merge_ratio:   分隔点合并容差（占视图尺寸的比例）。倒角两侧的边合成
                   一个理论尖角，符合制图惯例。
    """
    dim_sgn = 1 if side == "left" else -1
    off = 6.5                                 # 台阶尺寸贴近视图，外形尺寸在更外层
    edges, faces, texts = [], [], []
    stats = {}

    def run(v, axis, pts, ref, offs):
        """返回 (edges, faces, texts, 结论)。结论用于如实报告，不能只报点数。"""
        n = len(pts)
        if n < 3:
            # 恰好 2 个 = 只有两条外轮廓边，其链式尺寸等同外形尺寸，重复
            return [], [], [], f"{n}点 跳过:无内部台阶"
        if n > max_per_axis:
            return [], [], [], f"{n}点 跳过:过多({n - 1}段不可读)"
        e, f, t = chain_dims(v, s, axis, pts, ref, offs, tol_source, min_gap_paper)
        if not e:
            return [], [], [], f"{n}点 跳过:有段过密(比例{s:g}下不可读)"
        return e, f, t, f"{n}点 ✓标注{n - 1}段"

    for key in ("front", "top", "side"):
        v = views[key]
        vw, vh = v.w * s, v.h * s
        # 水平链（垂直边的 X 分隔点） / 垂直链（水平边的 Y 分隔点）
        px = division_points(v, "v", min_len=v.h * min_len_ratio,
                             merge=v.w * merge_ratio)
        py = division_points(v, "h", min_len=v.w * min_len_ratio,
                             merge=v.h * merge_ratio)

        e1, f1, t1, r1 = run(v, "v", px, v.y - vh / 2, -off)
        e2, f2, t2, r2 = run(v, "h", py, v.x + dim_sgn * vw / 2, dim_sgn * off)
        edges += e1 + e2
        faces += f1 + f2
        texts += t1 + t2
        stats[key] = (r1, r2, bool(e1))     # bool(e1)=水平链是否画了，供标签避让用

    return edges, faces, texts, stats


# ─────────────────────────────────────────── PMI 驱动标注（关键特征）

def _leader(cx: float, cy: float, r: float, text: str, ang_deg: float,
            shelf: float = 9.0):
    """圆特征的引线式标注：箭头落在圆周上指向圆心，斜引线 + 水平基准线 + 文字。

    适用于中孔/圆形特征的 ⌀ 与 R 标注（ISO 129 / ASME Y14.5 的 leader 画法）。
    """
    import math
    a = math.radians(ang_deg)
    tipx, tipy = cx + r * math.cos(a), cy + r * math.sin(a)      # 箭头尖在圆周
    kx, ky = cx + (r + shelf) * math.cos(a), cy + (r + shelf) * math.sin(a)
    sgn = 1 if math.cos(a) >= 0 else -1
    ex, ey = kx + sgn * 12.0, ky                                  # 水平基准线末端

    edges = bd.Line((tipx, tipy), (kx, ky)).edges() + bd.Line((kx, ky), (ex, ey)).edges()
    faces = [_arrow((tipx, tipy), -math.cos(a), -math.sin(a))]    # 箭头指向圆心
    anchor = "start" if sgn > 0 else "end"
    tx = kx + sgn * 1.5
    return edges, faces, (tx, ey + 1.2, text, TEXT_SIZE, anchor)


def _find_circles(views: dict, target_r: float, tol: float):
    """在三视图里找半径匹配的圆，按 (视图, 圆心) 去重。

    返回 [(view_key, view, cx, cy, r)]，按视图优先级（俯>主>侧）排序。
    """
    order = {"top": 0, "front": 1, "side": 2}
    out, seen = [], set()
    for key in sorted(views, key=lambda k: order.get(k, 9)):
        v = views[key]
        for e in v.vis:
            try:
                if e.geom_type != bd.GeomType.CIRCLE or abs(e.radius - target_r) > tol:
                    continue
                c = e.arc_center
            except (AttributeError, ValueError):
                continue
            sig = (key, round(c.X, 2), round(c.Y, 2))
            if sig in seen:
                continue
            seen.add(sig)
            out.append((key, v, c.X, c.Y, e.radius))
    return out


def pmi_feature_dims(views: dict, s: float, pmi, tol: float = 1e-2,
                     min_r_paper: float = 0.6):
    """PMI 驱动的关键特征标注 —— 与几何驱动是**本质不同**的路线。

    几何驱动的困境：投影里有一堆圆（实测 NIST FTC-11 的俯视图有 16 种直径、
    100G DR1 装配体有 33 种 227 处），「标哪几个」是设计意图，几何里没有。

    PMI 驱动直接跳过这个问题：**设计者在模型里标了 ⌀32，图上就标 ⌀32。**
    做法 = 拿 PMI 的公称值去投影视图里找半径匹配的圆 → 标注。

    这不是「从几何生成标注」，是「把设计意图渲染出来」。
    """
    import math
    from collections import defaultdict

    edges, faces, texts = [], [], []
    hits = []
    covered = set()          # 已由 PMI 标注的公称值 -> 几何驱动应让位，避免重复
    ang_cycle = [45, 135, -45, -135, 20, 160, -20, -160]

    # 按 (类型, 公称值) 分组 —— 同一公称值的多条 PMI 必须一起判定
    groups = defaultdict(list)
    for d in pmi.dims:
        if d.kind in ("diameter", "radius") and d.nominal is not None:
            groups[(d.kind, round(d.nominal, 4))].append(d)

    for (kind, nominal), entries in sorted(groups.items(), key=lambda x: x[0][1]):
        target_r = nominal / 2 if kind == "diameter" else nominal
        circles = _find_circles(views, target_r, tol)
        label_txt = entries[0].label()

        if not circles:
            hits.append((entries[0], None, "投影里无匹配的圆"))
            continue
        if circles[0][4] * s < min_r_paper:
            hits.append((entries[0], None,
                         f"图纸上仅 Ø{circles[0][4]*2*s:.2f}mm，当前比例下无法标注"))
            continue

        # ── 消歧判定 ──────────────────────────────────────────────
        # 实测 NIST CTC-01：每条 DIMENSIONAL_SIZE 指向的都是
        # COMPOSITE_SHAPE_ASPECT（复合形状要素，全文 18 个）——
        # **一条 PMI 可以合法地覆盖多个面**，这正是 ASME Y14.5 "4X ⌀25" 的语义。
        #
        # 由此：
        #   ① 该公称值只有 1 条 PMI -> 它管所有同径特征 -> 安全，标 "NX Ø.. ±.."
        #   ② 有多条 PMI 但公差完全一致 -> 标哪个都对 -> 安全
        #   ③ 有多条 PMI 且公差不同 -> 每条管一组，但**哪组是哪组无从知道**
        #      要消歧必须 DIMENSIONAL_SIZE -> SHAPE_ASPECT -> 面 -> 追溯回 2D 边，
        #      而 OCCT HLR 不提供 2D->3D 追溯（官方 GSoC 三大短板之一）-> 不标
        #
        # 实测 CTC-01 命中 ③：Ø20 两条公差不同、Ø35 四条公差不同。
        tol_set = {d.tol_text() for d in entries}
        single_pmi = len(entries) == 1
        same_tol = len(tol_set) == 1 and entries[0].tol_text() is not None

        if single_pmi or same_tol:
            targets = circles                       # 公差可靠 -> 全标
            # ASME Y14.5 记法：一条标注管 N 处，写 "NX"（明示共用，不逐个画引线）
            text = (f"{len(circles)}X {label_txt}" if len(circles) > 1 else label_txt)
            targets = circles[:1] if len(circles) > 1 else circles
        else:
            hits.append((entries[0], None,
                         f"歧义：几何有 {len(circles)} 个 Ø{nominal:g} 特征、"
                         f"PMI 有 {len(entries)} 条且公差不一致 {sorted(x or '无' for x in tol_set)}"
                         f" -> 无法确定哪条对应哪个特征，不标"))
            continue

        for key, v, pcx, pcy, pr in targets:
            sx = v.x + (pcx - v.cx) * s
            sy = v.y + (pcy - v.cy) * s
            ang = ang_cycle[len(texts) % len(ang_cycle)]
            e_, f_, tx_ = _leader(sx, sy, pr * s, text, ang)
            edges += e_
            faces += f_
            texts.append(tx_)
        hits.append((entries[0], targets[0][0],
                     f"{len(circles)}X 共用一条 PMI(COMPOSITE_SHAPE_ASPECT)"
                     if len(circles) > 1 else ""))
        covered.add(round(nominal, 3))

    return edges, faces, texts, hits, covered


def envelope_dims(views: dict, s: float, side: str, model_size, tol_source=None,
                  skip: set | None = None):
    """标注三视图的外形包围尺寸。

    每个尺寸只出现一次（ISO 129 要求）：
        总长 -> 主视图（水平，下方）
        总高 -> 主视图（垂直，远离侧视图的一侧）
        总宽 -> 俯视图（垂直，同侧）

    tol_source: 公差查询函数 f(dim_name) -> str|None。
                当前恒为 None —— 客户导出为 AP214，无 PMI，无处可查。
    """
    def tol(nominal: float):
        """tol_source 是按**公称值**查询的函数（见 pmi.Pmi.lookup）。"""
        return tol_source(nominal) if tol_source else None

    def dup(nominal: float) -> bool:
        """该外形尺寸是否已被 PMI 驱动标注覆盖。

        圆盘类零件的外形尺寸**就是**其直径 —— PMI 标了 ⌀63，外形再标一次 63
        就是重复。PMI 承载设计意图，优先级更高，几何驱动让位。
        """
        return bool(skip) and any(abs(nominal - k) < 1e-3 for k in skip)

    f, t = views["front"], views["top"]
    fw, fh = f.w * s, f.h * s
    tw, th = t.w * s, t.h * s

    # 尺寸放在没有其他视图的一侧：左视图在左 -> 尺寸走右侧，反之亦然
    # 分层：台阶尺寸 off=6.5 贴近视图，外形尺寸 off=16 在外层（ISO 129 惯例：小尺寸在内）
    dim_sgn = 1 if side == "left" else -1
    off = 16.0

    edges, faces, texts = [], [], []
    items = []
    if not dup(model_size.X):        # 总长：主视图下方
        items.append(linear_h(f.x - fw / 2, f.x + fw / 2, f.y - fh / 2, -off,
                              fmt(model_size.X, tol(model_size.X))))
    if not dup(model_size.Z):        # 总高：主视图侧向
        items.append(linear_v(f.y - fh / 2, f.y + fh / 2, f.x + dim_sgn * fw / 2,
                              dim_sgn * off, fmt(model_size.Z, tol(model_size.Z))))
    if not dup(model_size.Y):        # 总宽：俯视图侧向
        items.append(linear_v(t.y - th / 2, t.y + th / 2, t.x + dim_sgn * tw / 2,
                              dim_sgn * off, fmt(model_size.Y, tol(model_size.Y))))
    for e, fa, tx in items:
        edges += e
        faces += fa
        texts.append(tx)
    return edges, faces, texts
