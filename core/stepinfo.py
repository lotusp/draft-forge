"""STEP 文件元信息解析 —— 纯文本，不碰 OCCT，秒级返回。

设计要点：出图要跑 OCCT HLR（实测 2s~220s），但**关键信息全部来自文本解析**，
所以拆成两阶段：上传后立刻出元信息，图纸后台慢慢跑。

本模块的每条判据都来自本项目对 6 个真实模型的实测，不是教科书。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

RULES = json.loads((Path(__file__).parent / "parts_rules.json").read_text())

# 协议识别。schema 名 -> (显示名, 是否承载 PMI)
#   实测：LSB400=CONFIG_CONTROL_DESIGN(AP203)、100G DR1=AUTOMOTIVE_DESIGN(AP214)
#         NIST=AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF
SCHEMAS = [
    ("AP242", r"AP242|MANAGED_MODEL_BASED_3D_ENGINEERING", True),
    ("AP214", r"AUTOMOTIVE_DESIGN", False),
    ("AP203", r"CONFIG_CONTROL_DESIGN", False),
    ("AP209", r"STRUCTURAL_ANALYSIS_DESIGN", False),
]

_ENT_HEAD = re.compile(r"^#\d+\s*=", re.M)


def _count(txt: str, name: str) -> int:
    return len(re.findall(rf"\b{name}\s*\(", txt))


@dataclass
class Part:
    name: str
    pn_prefix: str = ""          # 料号前缀（如 220）
    pn_full: str = ""            # 料号原文（如 220-XXXX）
    kind: str = "未识别"
    confidence: str = "none"
    basis: str = ""              # 判据来源，必须可追溯


@dataclass
class StepInfo:
    # —— 文件与协议
    filename: str = ""
    size_mb: float = 0.0
    schema_raw: str = ""
    protocol: str = "未知"
    carries_pmi: bool = False
    source_cad: str = ""
    exported_at: str = ""
    # —— 结构
    is_assembly: bool = False
    n_products: int = 0
    n_asm_links: int = 0
    n_solids: int = 0
    n_faces: int = 0
    n_edges: int = 0
    # —— 器件清单
    parts: list = field(default_factory=list)
    # —— PMI
    pmi_dims: int = 0
    pmi_gtols: int = 0
    pmi_datums: int = 0
    pmi_note: str = ""
    # —— 可出图性
    has_brep: bool = True
    has_tessellated: bool = False
    drawable: bool = True
    drawable_note: str = ""
    # —— 几何体检（性能与质量）
    n_bspline_surf: int = 0
    n_entities: int = 0
    face_types: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def _classify(name: str, pn_prefix: str) -> tuple[str, str, str]:
    """器件分类。返回 (类别, 置信度, 判据)。

    优先级：器件名 > 料号前缀。
    因为器件名是**写在文件里的事实**，料号前缀含义是**推断**——
    没有任何标准规定 3 位前缀的语义（JB/T 5054.4 与 ASME Y14.100 均授权企业自定）。
    """
    low = name.lower()

    for r in RULES["name_rules"]:
        if re.search(r["pattern"], low, re.I):
            kind, conf = r["kind"], r["confidence"]
            # 后缀可细化：FA SM -> FA 的垫块
            for s in RULES["suffix_rules"]:
                if re.search(s["pattern"], name, re.I):
                    return f"{kind}（{s['kind']}）", conf, f"器件名 + 后缀规则"
            return kind, conf, "器件名匹配"

    for s in RULES["suffix_rules"]:
        if re.search(s["pattern"], name, re.I):
            return s["kind"], s["confidence"], "后缀规则"

    pm = RULES["prefix_map"].get(pn_prefix)
    if pm:
        return pm["kind"], "low", f"料号前缀 {pn_prefix}（{pm['source']}）"

    return "未识别", "none", "器件名与料号前缀均无匹配规则"


def split_name(name: str) -> tuple[str, str, str]:
    """拆解 PRODUCT 名称 -> (器件名, 料号前缀, 料号原文)。

    实测客户命名模式：{器件名}_{料号前缀}-{序号}_{产品代号}_{日期}
        Housing lid_304-XXXX_QQQ_20260101  -> ('Housing lid', '304', '304-XXXX')
        FA SM_285-XXXX_QQQ_20260101        -> ('FA SM',       '285', '285-XXXX')
    但实测 6/20 个零件无料号，必须能退化：
        PCB TOP_20260101                   -> ('PCB TOP',     '',    '')
        CB ASM_QQQ_20260101                -> ('CB ASM',      '',    '')
    ⚠️ 尾缀必须清干净：残留的 '_QQQ' 会让 `\\bASM\\b` 边界匹配失败（_ 是单词字符）。
    """
    s = name.strip()
    s = re.sub(r"_\d{8}$", "", s)            # 日期 _20260101
    s = re.sub(r"_[A-Z]{2,6}$", "", s)       # 产品代号 _QQQ / _QQQQ（客户已脱敏）
    m = re.search(r"_(\d{2,4})-([A-Za-z0-9]+)", s)
    if m:
        return s[:m.start()].strip(), m.group(1), f"{m.group(1)}-{m.group(2)}"
    return s, "", ""


def _parse_parts(txt: str, root_hint: str = "") -> list[Part]:
    """从 PRODUCT 实体提取器件清单。root_hint 用于标出装配体根节点。"""
    seen, out = set(), []
    for name, _desc in re.findall(r"PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'", txt):
        if not name or name in seen:
            continue
        seen.add(name)
        base, prefix, full = split_name(name)
        if root_hint and base and base in root_hint:
            out.append(Part(name=base, pn_prefix=prefix, pn_full=full,
                            kind="◆ 产品根节点", confidence="high",
                            basis="PRODUCT 名与文件名一致"))
            continue
        kind, conf, basis = _classify(base, prefix)
        out.append(Part(name=base or name, pn_prefix=prefix, pn_full=full,
                        kind=kind, confidence=conf, basis=basis))
    # 根节点排最前，其余按类别聚拢
    out.sort(key=lambda p: (not p.kind.startswith("◆"), p.kind, p.name))
    return out


def read(path: str | Path) -> StepInfo:
    p = Path(path)
    txt = p.read_text(errors="replace")
    i = StepInfo(filename=p.name, size_mb=round(p.stat().st_size / 1e6, 2))
    i.n_entities = len(_ENT_HEAD.findall(txt))

    # —— 协议
    m = re.search(r"FILE_SCHEMA\s*\(\(\s*'([^']+)'", txt)
    i.schema_raw = m.group(1).strip() if m else ""
    for name, pat, pmi in SCHEMAS:
        if re.search(pat, i.schema_raw, re.I):
            i.protocol, i.carries_pmi = name, pmi
            break

    # —— 来源 CAD / 导出时间（FILE_NAME 的第 6 个参数是 originating system）
    head = txt[:2000]
    src = re.findall(r"'([^']*(?:SolidWorks|CATIA|NX|Creo|Inventor|ZW3D|Siemens)[^']*)'", head, re.I)
    i.source_cad = src[0] if src else ""
    m = re.search(r"FILE_NAME\s*\(\s*'[^']*'\s*,\s*'([^']+)'", txt, re.S)
    i.exported_at = m.group(1) if m else ""

    # —— 结构
    i.n_products = _count(txt, "PRODUCT")
    i.n_asm_links = _count(txt, "NEXT_ASSEMBLY_USAGE_OCCURRENCE")
    i.n_solids = _count(txt, "MANIFOLD_SOLID_BREP")
    i.n_faces = _count(txt, "ADVANCED_FACE")
    i.n_edges = _count(txt, "EDGE_CURVE")
    # 装配体判据：看**产品树**（PRODUCT 数 / 装配关系），不看实体数。
    #   实测 LSB400: products=1, asm_links=0, solids=2 —— 单件含 2 个不相连实体，
    #   按实体数判会误判成装配体。
    i.is_assembly = i.n_asm_links > 0 or i.n_products > 1
    i.parts = [asdict(x) for x in _parse_parts(txt, p.stem)]

    # —— PMI
    i.pmi_dims = _count(txt, "DIMENSIONAL_SIZE")
    i.pmi_gtols = _count(txt, "GEOMETRIC_TOLERANCE")
    i.pmi_datums = _count(txt, "DATUM")
    if not i.carries_pmi:
        i.pmi_note = (f"{i.protocol} 协议本身不承载 PMI —— 这不是模型没标，"
                      f"是协议不支持。要做公差标注必须改导 STEP AP242。")
    elif i.pmi_dims == 0:
        i.pmi_note = ("协议是 AP242（支持 PMI），但本文件未携带语义 PMI。"
                      "AP242 只是『允许』带 PMI，不代表一定有。")
    else:
        i.pmi_note = "含语义 PMI，可驱动公差标注。"

    # —— 可出图性（B-rep 守卫）
    #   实测：把只含曲面细分的 AP242 喂给 OCCT HLR 会直接 SIGSEGV 段错误
    n_tess = _count(txt, "TESSELLATED_SHELL") + _count(txt, "COMPLEX_TRIANGULATED_FACE") \
        + _count(txt, "TRIANGULATED_FACE")
    i.has_tessellated = n_tess > 0
    i.has_brep = i.n_faces > 0
    if not i.has_brep:
        # ⚠️ 措辞注意：这段是给**客户**看的，不是给开发看的。
        #    实现细节（HLRBRep_Algo 需拓扑边、喂细分数据会 SIGSEGV、
        #    OCCT 另有 HLRBRep_PolyAlgo 但其 OCP 绑定实测无输出——
        #    拿正常三角化的 Box 对照：精确版出 5 条边、网格版出 0 条）
        #    一律留在代码注释里，不要往界面上堆。
        #
        #    也不要写成"HLR 无从下手"——那不准确。网格版 HLR 是存在的，
        #    真正的理由是**尺寸不可信**，而不是算法做不到。
        i.drawable = False
        i.drawable_note = (
            "**此模型只含曲面细分（三角网格）数据，不含精确 B-rep，无法生成工程图。**\n"
            "\n"
            "原因：网格用折线逼近曲面，端点不落在真实圆弧上 —— 标出的 `Ø32` "
            "实际可能是 `Ø31.87`。图纸上的尺寸要拿去加工和检验，"
            "**不可信的尺寸比不标更危险**。\n"
            "\n"
            "这是数据的问题，不是工具的问题 —— 细分数据里没有「真圆」，换任何工具都一样。\n"
            "\n"
            "→ **解决办法：导出时勾选 B-rep（而非 tessellated / mesh）。**")
    elif i.has_tessellated:
        i.drawable_note = "含 B-rep，同时带曲面细分数据（多为图形 PMI 的预渲染网格）。"
    else:
        i.drawable_note = "含精确 B-rep，可出图。"

    # —— 几何体检
    i.n_bspline_surf = _count(txt, "B_SPLINE_SURFACE_WITH_KNOTS")
    for k, label in [("PLANE", "平面"), ("CYLINDRICAL_SURFACE", "圆柱面"),
                     ("CONICAL_SURFACE", "锥面"), ("TOROIDAL_SURFACE", "环面"),
                     ("SPHERICAL_SURFACE", "球面"), ("B_SPLINE_SURFACE_WITH_KNOTS", "B样条面")]:
        n = _count(txt, k)
        if n:
            i.face_types[label] = n
    return i


if __name__ == "__main__":
    import sys
    info = read(sys.argv[1])
    d = info.to_dict()
    parts = d.pop("parts")
    for k, v in d.items():
        print(f"  {k:18} {v}")
    print(f"\n  器件清单 ({len(parts)}):")
    for p in parts:
        print(f"    {p['name']:26} {p['pn_full']:12} {p['kind']:16} "
              f"[{p['confidence']:6}] {p['basis']}")
