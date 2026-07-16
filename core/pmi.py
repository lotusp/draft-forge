"""STEP AP242 语义 PMI 读取。

这是《技术方案评审》里一直预留的 `tol_source` 的真正实现——
把模型里的公差**迁移**到图纸上（不是从几何"生成"，几何里没有这个信息）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么直接解析 STEP 文本，而不用 OCCT XDE？

  正规路线是 STEPCAFControl_Reader(SetGDTMode=True) + XCAFDoc_DimTolTool。
  实测该路线在 OCP(pybind11) 绑定下**不可用**：

    · GetDimensionLabels() 报告 21 条，seq.Value(i).IsNull() 也返回 False
    · 但 lab = seq.Value(i) 存成变量后调用即报 "A null Label has no attribute"
      —— seq.Value() 返回的是 C++ 临时对象，存成 Python 变量即悬垂
    · 改为链式 seq.Value(i).FindAttribute(...) 能返回 True，
      但随后 attr.GetObject() 仍崩（属性内部持有的还是那个临时 label）
    · TDF_Label 拷贝构造未暴露；TDF_Tool.Label_s 反查失败；
      改走 TDF_ChildIterator 遍历 DGTs 根（38 个子标签 = 21尺寸+6公差+11基准，
      数量对得上）但 FindAttribute 全部返回 False
    · XCAFDoc_Dimension.Set_s(label) 是**创建**属性（会覆盖原数据），不能用于读取

  STEP 是文本格式，PMI 的实体链清晰且稳定，直接解析反而更可控。
  若将来换 pythonocc-core 或 OCP 修复该绑定，可平滑切回 XDE 路线。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AP242 语义尺寸的实体链（实测自 NIST CTC-01）：

    DIMENSIONAL_CHARACTERISTIC_REPRESENTATION(#dim, #rep)
      ├─ #dim = DIMENSIONAL_SIZE(#feature, 'diameter')        ← 尺寸类型
      └─ #rep = SHAPE_DIMENSION_REPRESENTATION('', (#m), #ctx)
                 └─ #m = ( ... MEASURE_WITH_UNIT(LENGTH_MEASURE(35.), #u)
                               REPRESENTATION_ITEM('nominal value') )   ← 公称值

    PLUS_MINUS_TOLERANCE(#tv, #dim)
      └─ #tv = TOLERANCE_VALUE(#lower, #upper)
                 ├─ #lower = ( ... LENGTH_MEASURE(-0.2) ... )   ← 下偏差
                 └─ #upper = ( ... LENGTH_MEASURE(0.)   ... )   ← 上偏差

注：OCCT 原生只支持 STEP AP242 一条 PMI 通路，读不了 CATIA/NX/Creo 原生 PMI
    （那需要 HOOPS Exchange / CAD Exchanger 这类商业组件）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# GD&T 类型 -> (图纸符号, 中文名)
#
# ⚠️ 键必须用 **STEP 实体名**，不是制图术语——两者对不上，是个坑：
#      制图叫「圆度 Circularity」，STEP 实体名是 ROUNDNESS_TOLERANCE
#      制图叫「圆跳动 Circular Runout」，STEP 实体名是 CIRCULAR_RUNOUT_TOLERANCE
#    实测 NIST FTC-11 就用了这两个，按制图术语写映射会静默漏掉。
#    对照 ISO 10303-47 / AP242 MIM 的 geometric_tolerance 子类型清单。
GTOL_SYMBOL = {
    # 形状公差
    "FLATNESS_TOLERANCE": ("⏥", "平面度"),
    "STRAIGHTNESS_TOLERANCE": ("⏤", "直线度"),
    "ROUNDNESS_TOLERANCE": ("○", "圆度"),          # ← STEP 用 ROUNDNESS 非 CIRCULARITY
    "CYLINDRICITY_TOLERANCE": ("⌭", "圆柱度"),
    # 轮廓公差
    "SURFACE_PROFILE_TOLERANCE": ("⌓", "面轮廓度"),
    "LINE_PROFILE_TOLERANCE": ("⌒", "线轮廓度"),
    # 方向公差
    "PARALLELISM_TOLERANCE": ("∥", "平行度"),
    "PERPENDICULARITY_TOLERANCE": ("⊥", "垂直度"),
    "ANGULARITY_TOLERANCE": ("∠", "倾斜度"),
    # 位置公差
    "POSITION_TOLERANCE": ("⌖", "位置度"),
    "CONCENTRICITY_TOLERANCE": ("◎", "同轴度"),
    "SYMMETRY_TOLERANCE": ("⌯", "对称度"),
    # 跳动公差
    "CIRCULAR_RUNOUT_TOLERANCE": ("↗", "圆跳动"),  # ← STEP 名带 CIRCULAR_ 前缀
    "TOTAL_RUNOUT_TOLERANCE": ("⌰", "全跳动"),
}


@dataclass
class Dim:
    """一条语义尺寸 PMI。"""
    kind: str                       # diameter / length / radius ...
    nominal: float | None = None
    lower: float | None = None      # 下偏差
    upper: float | None = None      # 上偏差

    def tol_text(self) -> str | None:
        if self.lower is None or self.upper is None:
            return None
        if abs(self.lower + self.upper) < 1e-9 and abs(self.upper) > 1e-9:
            return f"±{abs(self.upper):g}"          # 对称公差
        return f"{self.upper:+g}/{self.lower:+g}"   # 非对称，如 +0/-0.2

    def label(self) -> str:
        # Ø(U+00D8) 而非 ⌀(U+2300)：后者多数字体缺字形，图纸上会变成 ■
        pre = "Ø" if self.kind == "diameter" else ("R" if self.kind == "radius" else "")
        t = self.tol_text()
        return f"{pre}{self.nominal:g}" + (f" {t}" if t else "")


@dataclass
class GTol:
    """一条几何公差 PMI。"""
    kind: str
    value: float | None = None
    datums: list[str] = field(default_factory=list)

    def label(self) -> str:
        sym, cn = GTOL_SYMBOL.get(self.kind, ("⌓", self.kind))
        d = "|" + "|".join(self.datums) if self.datums else ""
        return f"{sym} {self.value:g}{d}" if self.value is not None else f"{sym}{d}"


@dataclass
class Pmi:
    dims: list[Dim] = field(default_factory=list)
    gtols: list[GTol] = field(default_factory=list)
    datums: list[str] = field(default_factory=list)
    schema: str = ""

    def __bool__(self):
        return bool(self.dims or self.gtols)

    def lookup(self, nominal: float, tol: float = 1e-3) -> str | None:
        """按公称值查公差文本 —— 供 dimensions.fmt() 的 tol_source 用。

        这是「标注迁移」而非「标注生成」：图上标什么公差，取决于模型里存了什么。

        ⚠️ 按数值查是**有歧义的**，只是权宜之计。实测 NIST CTC-01 里 ⌀35 出现
        4 次，公差分别是 +0/-0.2、+0.2/+0、以及两条无公差 —— 同一公称值、
        不同功能、不同公差。歧义时返回 None（宁缺毋滥，不猜）。

        正解是走 DIMENSIONAL_SIZE -> SHAPE_ASPECT -> 具体面 的关联，再把投影
        出的 2D 边追溯回该面。但 **OCCT HLR 不提供 2D 输出到 3D 输入的追溯**
        （官方 GSoC issue 明列的三大短板之一），这条路在 OCCT 上是断的。
        """
        hits = [d for d in self.dims
                if d.nominal is not None and abs(d.nominal - nominal) <= tol
                and d.tol_text()]
        if not hits:
            return None
        texts = {d.tol_text() for d in hits}
        return hits[0].tol_text() if len(texts) == 1 else None

    def ambiguous(self) -> list[tuple[float, set]]:
        """找出「同一公称值对应多种公差」的条目 —— 按数值查不出来的那些。"""
        by_nom: dict[float, set] = {}
        for d in self.dims:
            if d.nominal is None:
                continue
            by_nom.setdefault(round(d.nominal, 4), set()).add(d.tol_text())
        return [(n, t) for n, t in sorted(by_nom.items()) if len(t) > 1]


# ─────────────────────────────────────────────────────────── STEP 文本解析

_ENT = re.compile(r"#(\d+)\s*=\s*(.*?);", re.S)
_MEASURE = re.compile(r"LENGTH_MEASURE\s*\(\s*([-\d.eE+]+)\s*\)")
_NAMED = re.compile(r"REPRESENTATION_ITEM\s*\(\s*'([^']*)'\s*\)")


def _refs(s: str) -> list[str]:
    return re.findall(r"#(\d+)", s)


def _measure_of(ents: dict, eid: str) -> float | None:
    m = _MEASURE.search(ents.get(eid, ""))
    return float(m.group(1)) if m else None


def read(path: str | Path) -> Pmi:
    """解析 STEP 文件里的语义 PMI。无 PMI 时返回空 Pmi（bool 为 False）。"""
    p = Path(path)
    try:
        txt = p.read_text(errors="replace")
    except Exception:
        return Pmi()

    out = Pmi()
    m = re.search(r"FILE_SCHEMA\s*\(\(\s*'([^']+)'", txt)
    out.schema = m.group(1).strip() if m else ""

    ents = {k: v.strip() for k, v in _ENT.findall(txt)}

    # 1) 公称值：DIMENSIONAL_CHARACTERISTIC_REPRESENTATION(#dim, #rep)
    nominal_of: dict[str, float] = {}
    kind_of: dict[str, str] = {}
    for v in ents.values():
        if not v.startswith("DIMENSIONAL_CHARACTERISTIC_REPRESENTATION"):
            continue
        r = _refs(v)
        if len(r) < 2:
            continue
        dim_id, rep_id = r[0], r[1]

        ds = ents.get(dim_id, "")
        km = re.search(r"DIMENSIONAL_SIZE\s*\(\s*#\d+\s*,\s*'([^']*)'", ds)
        kind_of[dim_id] = km.group(1) if km else "length"

        # SHAPE_DIMENSION_REPRESENTATION('', (#m1,#m2...), #ctx) —— 取标了
        # 'nominal value' 的那个；只有一个成员时直接用
        rep = ents.get(rep_id, "")
        cand = _refs(rep)
        for mid in cand:
            body = ents.get(mid, "")
            nm = _NAMED.search(body)
            if nm and nm.group(1) == "nominal value":
                if (val := _measure_of(ents, mid)) is not None:
                    nominal_of[dim_id] = val
                break
        else:
            for mid in cand:
                if (val := _measure_of(ents, mid)) is not None:
                    nominal_of[dim_id] = val
                    break

    # 2) 偏差：PLUS_MINUS_TOLERANCE(#tv, #dim) -> TOLERANCE_VALUE(#lower, #upper)
    tol_of: dict[str, tuple[float | None, float | None]] = {}
    for v in ents.values():
        if not v.startswith("PLUS_MINUS_TOLERANCE"):
            continue
        r = _refs(v)
        if len(r) < 2:
            continue
        tv_id, dim_id = r[0], r[1]
        tv = ents.get(tv_id, "")
        if not tv.startswith("TOLERANCE_VALUE"):
            continue
        tr = _refs(tv)
        if len(tr) < 2:
            continue
        tol_of[dim_id] = (_measure_of(ents, tr[0]), _measure_of(ents, tr[1]))

    for dim_id, nom in nominal_of.items():
        lo, up = tol_of.get(dim_id, (None, None))
        out.dims.append(Dim(kind=kind_of.get(dim_id, "length"),
                            nominal=nom, lower=lo, upper=up))
    out.dims.sort(key=lambda d: (d.kind, d.nominal or 0))

    # 3) 几何公差
    #    注意：GD&T 多以**复合实体**出现，按开头匹配会漏掉：
    #      #21 = ( GEOMETRIC_TOLERANCE('Position.1','',#95,#235)
    #              GEOMETRIC_TOLERANCE_WITH_DATUM_REFERENCE((#52))
    #              POSITION_TOLERANCE() )
    #    故需在整条实体文本里扫类型关键字。
    datum_name = {}
    for k, v in ents.items():
        if v.startswith("DATUM("):
            dm = re.findall(r"'([^']*)'", v)
            if dm and dm[-1].strip():
                datum_name[k] = dm[-1].strip()

    for v in ents.values():
        head = v.split("(", 1)[0].strip()
        kinds = [t for t in GTOL_SYMBOL if re.search(rf"\b{t}\s*\(", v)]
        if head not in GTOL_SYMBOL and not (v.startswith("(") and kinds):
            continue
        kind = head if head in GTOL_SYMBOL else kinds[0]

        # 公差值：GEOMETRIC_TOLERANCE(name, desc, #magnitude, #shape_aspect)
        val = None
        gm = re.search(r"GEOMETRIC_TOLERANCE\s*\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*#(\d+)", v)
        if gm:
            val = _measure_of(ents, gm.group(1))
        if val is None:
            for rid in _refs(v):
                if (val := _measure_of(ents, rid)) is not None:
                    break

        # 基准参照：GEOMETRIC_TOLERANCE_WITH_DATUM_REFERENCE((#52,...))
        ds = []
        dm = re.search(r"GEOMETRIC_TOLERANCE_WITH_DATUM_REFERENCE\s*\(\s*\((.*?)\)\s*\)", v, re.S)
        if dm:
            for rid in re.findall(r"#(\d+)", dm.group(1)):
                # #52 通常是 DATUM_REFERENCE(precedence, #datum)
                for sub in [rid] + _refs(ents.get(rid, "")):
                    if sub in datum_name:
                        ds.append(datum_name[sub])
                        break
        out.gtols.append(GTol(kind=kind, value=val, datums=ds))

    out.datums = sorted(set(datum_name.values()))
    return out


if __name__ == "__main__":
    import sys

    p = read(sys.argv[1])
    print(f"协议     {p.schema}")
    print(f"语义尺寸 {len(p.dims)} 条 | 几何公差 {len(p.gtols)} 条 | 基准 {p.datums}")
    print()
    for d in p.dims:
        print(f"  {d.kind:10} {d.label()}")
    for g in p.gtols:
        sym, cn = GTOL_SYMBOL.get(g.kind, ("?", g.kind))
        print(f"  {cn:8} {g.label()}")
