"""模型几何体检 —— 找出影响出图性能与质量的因素。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本模块的每条判据都来自实测，不是猜的：

  实测对比（同一台机器）：
    100G DR1 装配体   2502 面 / 125 个 B-spline / 平均 22 控制点每面  -> HLR 19.6s
    LSB400 单件        200 面 /  10 个 B-spline / 平均 1353 控制点每面 -> HLR 165s

  → **面数少 12 倍、B-spline 少 12 倍，却慢 8 倍。**
    HLR 的代价由**单个面的数学复杂度**决定，不是面的数量。

  控制点数与 HLR 耗时的实测关系（LSB400 俯视图）：
    12524 点 ->  85s        49716 点 -> 150s        83472 点 -> 195s

  已证伪的优化（不要再试）：
    ShapeUpgrade_UnifySameDomain  面数 200->159，HLR 1.01x（无效）
    ShapeCustom.BSplineRestriction 控制点 12524->83472，HLR 0.44x（更慢 2.3 倍）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

# 阈值（据实测标定）
CP_WARN = 200          # 单面控制点数告警线：100G DR1 最大 24，LSB400 达 3018
CP_BAD = 1000          # 单面控制点数严重线
TINY_AREA = 1.0        # 退化面面积阈值 mm²（LSB400 实测有 2 个 0.11mm² 却含 810 控制点）
TINY_AREA_CP = 100     # 小面却含大量控制点 -> 病态


def inspect(shape) -> dict:
    """对 build123d Shape 做几何体检。返回可 JSON 化的 dict。"""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp
    from OCP.Geom import Geom_BSplineSurface
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out = {
        "n_faces": 0, "n_bspline": 0, "n_rational": 0,
        "cp_total": 0, "cp_max": 0, "cp_avg_bspline": 0,
        "degenerate": [],      # 退化面
        "heavy": [],           # 高密度 B-spline 面
        "warnings": [],
        "perf_class": "轻",
    }

    ex = TopExp_Explorer(shape.wrapped, TopAbs_ShapeEnum.TopAbs_FACE)
    while ex.More():
        f = TopoDS.Face_s(ex.Current())
        out["n_faces"] += 1
        srf = BRep_Tool.Surface_s(f)
        if isinstance(srf, Geom_BSplineSurface):
            nu, nv = srf.NbUPoles(), srf.NbVPoles()
            cp = nu * nv
            rat = srf.IsURational() or srf.IsVRational()
            out["n_bspline"] += 1
            out["cp_total"] += cp
            out["cp_max"] = max(out["cp_max"], cp)
            if rat:
                out["n_rational"] += 1

            p = GProp_GProps()
            BRepGProp.SurfaceProperties_s(f, p)
            area = p.Mass()

            # 退化面：面积极小却含大量控制点 —— 几乎必是布尔残留或转换产物
            if area < TINY_AREA and cp > TINY_AREA_CP:
                out["degenerate"].append({
                    "area_mm2": round(area, 4), "control_points": cp,
                    "u_deg": srf.UDegree(), "v_deg": srf.VDegree(),
                    "poles": f"{nu}x{nv}", "rational": rat})
            elif cp >= CP_WARN:
                out["heavy"].append({
                    "area_mm2": round(area, 2), "control_points": cp,
                    "u_deg": srf.UDegree(), "v_deg": srf.VDegree(),
                    "poles": f"{nu}x{nv}", "rational": rat})
        ex.Next()

    if out["n_bspline"]:
        out["cp_avg_bspline"] = out["cp_total"] // out["n_bspline"]

    # —— 结论
    mx = out["cp_max"]
    if mx >= CP_BAD:
        out["perf_class"] = "重"
        out["warnings"].append(
            f"单个 B-spline 面最多含 {mx} 个控制点（正常应 <{CP_WARN}）。"
            f"HLR 的代价由单面数学复杂度决定 —— 实测同类模型 HLR 可达数分钟。"
            f"对照：100G DR1 装配体 2502 个面，单面最大仅 24 个控制点，HLR 仅 19.6 秒。")
    elif mx >= CP_WARN:
        out["perf_class"] = "中"
        out["warnings"].append(f"单个 B-spline 面含 {mx} 个控制点，偏高，HLR 会变慢。")

    if out["degenerate"]:
        # 退化面是**质量**问题，与性能评级是两条独立的轴：
        #   实测 100G DR1 装配体 cp_max 仅 120（性能"轻"、HLR 19.6s），
        #   却含 6 个退化面，最小的 0.0065mm²（0.08×0.08mm，比头发丝还细）用了 102 个控制点。
        d = min(out["degenerate"], key=lambda x: x["area_mm2"])
        s = d["area_mm2"] ** 0.5
        out["warnings"].append(
            f"🔴 检出 {len(out['degenerate'])} 个**退化面**（几何质量问题，与性能评级无关）。"
            f"最小的一个面积仅 {d['area_mm2']} mm²，约 {s:.2f}×{s:.2f} mm"
            f"{'，比头发丝还细' if s < 0.07 else '，比针尖还小'}，"
            f"却用 {d['control_points']} 个控制点描述。"
            f"这类面通常是布尔运算残留或数据转换产物，会拖慢 HLR、可能导致投影异常，"
            f"且在下游 CAM/CAE 里同样是隐患。建议在 CAD 侧清理。")

    if out["n_rational"]:
        out["warnings"].append(
            f"{out['n_rational']}/{out['n_bspline']} 个 B-spline 面是**有理 NURBS**"
            f"（权重非均匀）。有理曲面的求交比非有理贵得多。")

    # 高密度面的形态判读
    for h in out["heavy"] + out["degenerate"]:
        nu, nv = (int(x) for x in h["poles"].split("x"))
        if max(nu, nv) > 20 * max(min(nu, nv), 1):
            out["warnings"].append(
                f"高密度面呈 {h['poles']} 极点分布（一个方向极长）—— "
                f"典型的**沿复杂路径扫描/拉伸**特征。若为螺纹，"
                f"工程图本可用简化画法表示（GB/T 4459.1 / ASME Y14.6），"
                f"建模时无需真实螺旋。")
            break

    return out


if __name__ == "__main__":
    import json
    import sys

    import build123d as bd
    r = inspect(bd.import_step(sys.argv[1]))
    w = r.pop("warnings")
    deg, hv = r.pop("degenerate"), r.pop("heavy")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if deg:
        print(f"\n退化面 {len(deg)}:")
        for d in deg[:5]:
            print(f"   {d}")
    if hv:
        print(f"\n高密度面 {len(hv)}:")
        for d in hv[:5]:
            print(f"   {d}")
    print()
    for x in w:
        print(f"⚠ {x}\n")
