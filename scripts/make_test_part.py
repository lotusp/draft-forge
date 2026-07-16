"""生成测试用 STEP 零件。

Submount 尺寸取自客户真实图纸（DWG NO. 222-9999）：
    总长 5.47 / 总宽 1.86 / 总高 1.40，带台阶（0.70 / 0.44）
用真实尺寸是为了让 Demo 输出能和客户既有图纸直接对照。
"""

import build123d as bd

OUT = "parts"


def submount():
    """带台阶的 Submount：下层承载 + 上层键合台（S1/S2 两个键合面）。"""
    base = bd.Pos(0, 0, 0.35) * bd.Box(5.47, 1.86, 0.70)
    step = bd.Pos(0, (1.86 - 1.22) / 2, 1.05) * bd.Box(5.47, 1.22, 0.70)
    return base + step


def bracket():
    """对照件：带孔和倒角的支架，用于验证圆孔投影与隐藏线。"""
    body = bd.Box(40, 24, 8)
    body = bd.fillet(body.edges().filter_by(bd.Axis.Z), 3)
    holes = bd.Pos(0, 0, 0) * bd.Cylinder(3, 20)
    for x in (-14, 14):
        holes += bd.Pos(x, 0, 0) * bd.Cylinder(2.5, 20)
    slot = bd.Pos(0, 0, 4) * bd.Box(30, 6, 4)
    return body - holes - slot


def heatsink():
    """压力测试件：带散热鳍片 + 圆角 + 沉孔的壳体，贴近光模块结构件复杂度。

    这类零件是 HLR 的最坏情况——大量平行面互相遮挡，隐藏线爆炸。
    用它验证"复杂曲面 HLR 性能差"这条风险的真实概率。
    """
    body = bd.Box(30, 20, 6)
    body = bd.fillet(body.edges().filter_by(bd.Axis.Z), 2)
    fins = None
    for i in range(12):
        f = bd.Pos(-13.5 + i * 2.45, 0, 8) * bd.Box(1.2, 18, 10)
        fins = f if fins is None else fins + f
    part = body + fins
    # 四角沉孔
    for x in (-12, 12):
        for y in (-7.5, 7.5):
            part -= bd.Pos(x, y, 0) * bd.Cylinder(1.6, 20)
            part -= bd.Pos(x, y, 2.2) * bd.Cylinder(2.8, 2)
    return part


if __name__ == "__main__":
    import pathlib

    pathlib.Path(OUT).mkdir(exist_ok=True)
    for name, part in [("submount", submount()), ("bracket", bracket()),
                       ("heatsink", heatsink())]:
        path = f"{OUT}/{name}.step"
        bd.export_step(part, path)
        bb = part.bounding_box()
        print(f"{path:24}  {bb.size.X:6.2f} x {bb.size.Y:6.2f} x {bb.size.Z:6.2f} mm")
