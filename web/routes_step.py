"""STEP → 2D 工程图 路由（挂在 /api/step 下）。

两阶段设计（关键）：
  阶段1『即时』 上传 -> 文本解析元信息 -> 立刻返回（<1s）
  阶段2『后台』 OCCT HLR -> 三视图 -> SVG/PDF/DXF（实测 3s ~ 147s）

为什么必须两阶段：出图最慢实测 147s（LSB400），但关键信息（协议/料号/分类/PMI）
全部来自纯文本解析，1 秒内可得。让用户上传后立刻有东西看。

⚠️ 进度只能报**阶段**不能报百分比 —— OCCT HLR 官方短板之一就是不提供进度回报。

本模块只定义 APIRouter；由 web/server.py 挂载。转换跑在独立子进程里（见 _run），
spawn 子进程会重新 import 本模块（web.routes_step），故 ROOT/core 的 sys.path 注入
放在模块顶层，子进程 import 时即生效。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent.parent      # project root
sys.path.insert(0, str(ROOT / "core"))             # engine + logic modules live here

import geomcheck                                    # noqa: E402
import stepinfo                                     # noqa: E402

RESULTS = ROOT / "results" / "step"                 # STEP 结果独立子目录
RESULTS.mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/api/step", tags=["step"])


def _write(outdir: Path, **kw):
    """把 kw 合并进 meta.json。stage 与最终状态都经此落盘。

    ⚠️ 转换跑在**独立子进程**里（见 _run），进度不能走内存 —— 必须落盘，
    父进程的 API 才读得到。meta.json 是转换状态的唯一真相来源。
    """
    f = outdir / "meta.json"
    try:
        m = json.loads(f.read_text())
    except Exception:
        m = {}
    m.update(kw)
    f.write_text(json.dumps(m, ensure_ascii=False, indent=2))


def _result_dirs():
    """列出结果目录，容忍 results/ 不存在或子项在遍历中途被删。

    演示时用户会删记录、清目录，这些操作可能与请求撞车 —— 遍历前确保目录存在，
    遍历中忽略已消失的子项，避免 500。
    """
    RESULTS.mkdir(parents=True, exist_ok=True)
    try:
        return sorted(RESULTS.iterdir(), reverse=True)
    except FileNotFoundError:
        return []

# 全局串行闸门：一次只跑一个转换，避免多个转换子进程抢 CPU。
_gate = threading.Semaphore(1)

STAGES = ["排队中", "读取模型", "几何体检", "3D 预览", "HLR 投影", "组装出图", "完成"]


# ─────────────────────────────────────────────────────────── 转换

def _run(outdir: Path, step_path: Path, opts: dict, title: str):
    """父进程侧：排队 -> 起一个**独立子进程**跑整个转换 -> 监控它。

    ⚠️ 为什么整个转换必须在子进程里（这是稳定性的核心）：
      OCCT 在个别模型上会段错误（实测 nist_ftc_07）。只要它在服务进程内运行，
      一次段错误就带死整个服务（exit 139）。放进独立子进程后：
        · 子进程崩溃只是本任务失败，服务进程毫发无损；
        · 父线程 join 子进程时释放 GIL，uvicorn 事件循环全程不冻（哪怕 LSB400
          跑 200 秒，界面照样能上传别的模型、旋转 3D）。
    """
    import multiprocessing as mp

    with _gate:
        ctx = mp.get_context("spawn")
        p = ctx.Process(target=_convert, args=(step_path, outdir, opts, title), daemon=True)
        p.start()
        p.join()
        # 子进程异常退出（段错误 exitcode<0 / 非零）且没来得及写终态 -> 标记失败
        if p.exitcode != 0:
            try:
                m = json.loads((outdir / "meta.json").read_text())
            except Exception:
                m = {}
            if m.get("status") not in ("done", "failed"):
                _write(outdir, status="failed",
                       error=(f"转换进程异常退出（exitcode={p.exitcode}，"
                              f"多为 OCCT 在此模型上段错误）。已隔离，服务未受影响。"),
                       stage="失败")


def _convert(step_path: Path, outdir: Path, opts: dict, title: str):
    """转换主体 —— **在独立子进程里运行**（由 _run 启动）。

    进度与结果都经 _write() 落盘（meta.json），父进程的 API 从盘上读。
    这里所有 OCCT 调用都在本子进程内串行执行；本子进程即隔离边界，崩了不影响服务，
    故投影直接用本进程串行 project_views()，无需再套子进程。
    """
    import build123d as bd

    import dimensions as dims
    import pmi as pmi_mod
    import step2drawing as s2d

    t0 = time.perf_counter()
    try:
        _write(outdir, stage="读取模型")
        shape = bd.import_step(str(step_path))
        bb = shape.bounding_box()
        t_read = time.perf_counter() - t0

        _write(outdir, stage="几何体检")
        gc = geomcheck.inspect(shape)

        # —— 3D 预览（glTF）：供前端与 2D 图纸并排比对，验证投影是否正确
        _write(outdir, stage="3D 预览")
        t = time.perf_counter()
        try:
            # ⚠️ import_step 返回的是**没有子节点的 Solid**，而 export_gltf 靠
            #    PreOrderIter 遍历节点树 —— 直接导出会得到空 glTF（只有 240 字节，
            #    accessors 为空）。必须用 Compound(children=solids()) 重建节点树。
            mesh = bd.Compound(children=shape.solids())
            bd.export_gltf(mesh, str(outdir / "model.glb"), binary=True,
                           linear_deflection=0.08, angular_deflection=0.4)
        except Exception:
            pass                      # 3D 预览失败不应阻断出图主流程
        t_glb = time.perf_counter() - t

        _write(outdir, stage="HLR 投影")
        t = time.perf_counter()
        views, side, notes = s2d.project_views(shape, "auto")
        t_hlr = time.perf_counter() - t

        _write(outdir, stage="组装出图")
        t = time.perf_counter()
        sheet = s2d.SHEETS[opts["sheet"]]
        area = s2d.view_area(sheet)
        s = s2d.auto_scale(views, area[2] - area[0], area[3] - area[1])
        s2d.layout(views, s, area, opts["projection"], side)

        layers = {"Frame": s2d.frame_geometry(sheet), "Visible": [], "Hidden": [],
                  "Dim": [], "DimFill": []}
        pairs = [("Visible", "vis")] + ([] if opts["no_hidden"] else [("Hidden", "hid")])
        n_vis = n_hid = 0
        for v in views.values():
            for key, attr in pairs:
                c = s2d.place(getattr(v, attr), v, s)
                if c is not None:
                    layers[key] += c.edges()
            n_vis += len(v.vis)
            n_hid += len(v.hid)

        texts = []
        P = pmi_mod.read(step_path)
        pmi_hits, covered = [], set()
        if P:
            p_e, p_f, p_t, pmi_hits, covered = dims.pmi_feature_dims(views, s, P)
            layers["Dim"] += p_e
            layers["DimFill"] += p_f
            texts += p_t
        d_e, d_f, d_t = dims.envelope_dims(views, s, side, bb.size,
                                           tol_source=(P.lookup if P else None),
                                           skip=covered)
        layers["Dim"] += d_e
        layers["DimFill"] += d_f
        texts += d_t
        # 标题用**原始文件名**：存盘名是 source.xxx，直接用会在标题栏印出 "SOURCE"
        texts += s2d.build_texts(sheet, s, opts["projection"], title,
                                 views, side, {k: 21.0 for k in views})

        svg, pdf, dxf = outdir / "drawing.svg", outdir / "drawing.pdf", outdir / "drawing.dxf"
        s2d.export_svg(svg, layers, sheet, texts)
        s2d.export_dxf(dxf, layers)
        s2d.export_pdf(svg, pdf, sheet)
        t_exp = time.perf_counter() - t

        meta = json.loads((outdir / "meta.json").read_text())
        meta["drawing"] = {
            "sheet": opts["sheet"], "scale": s2d.fmt_scale(s), "side": side.upper(),
            "projection": "第三角 (ASME)" if opts["projection"] == "third" else "第一角 (GB)",
            "n_visible": n_vis, "n_hidden": n_hid, "hidden_drawn": not opts["no_hidden"],
            "dropped_edges": sum(v.dropped for v in views.values()),
            "side_fallback": ([{"tried": c, "bad_solids": [f"#{i}({k}面)" for i, k in b]}
                               for c, b in notes] or None),
        }
        meta["pmi_applied"] = [
            {"label": d.label(), "view": v, "note": r} for d, v, r in pmi_hits]
        meta["geomcheck"] = gc
        meta["timing"] = {"read": round(t_read, 2), "glb": round(t_glb, 2),
                          "hlr": round(t_hlr, 2), "export": round(t_exp, 2),
                          "total": round(time.perf_counter() - t0, 2)}
        meta["has_3d"] = (outdir / "model.glb").exists()
        # 三视图的相机方向（供前端 3D 视角与 2D 视图对齐）
        meta["view_dirs"] = {k: s2d.VIEW_DEFS[v.name][0] for k, v in views.items()}
        meta["status"] = "done"
        meta["stage"] = "完成"
        (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    except BaseException as e:                      # SystemExit(B-rep 守卫) 也要接住
        err = str(e) or traceback.format_exc(limit=3)
        _write(outdir, status="failed", stage="失败", error=err,
               timing={"total": round(time.perf_counter() - t0, 2)})


# ─────────────────────────────────────────────────────────── API

@router.post("/convert")
async def convert(file: UploadFile):
    raw = await file.read()
    digest = hashlib.sha256(raw).hexdigest()[:12]

    # 缓存：同一文件内容复用历史结果（demo 反复上传同一模型的场景）。
    #   同时命中**在途**任务 —— 否则连点两次会排两遍队，各自再起 4 个 HLR 进程。
    #   删除历史记录即可强制重转（演示时的常用动作）。
    for d in _result_dirs():
        f = d / "meta.json"
        if d.is_dir() and f.exists():
            try:
                m = json.loads(f.read_text())
            except Exception:
                continue
            if m.get("digest") != digest:
                continue
            if m.get("status") == "done":
                return {"id": d.name, "cached": True}
            if m.get("status") == "converting":       # 在途：同文件不重复排队
                return {"id": d.name, "cached": True, "in_flight": True}

    stem = Path(file.filename).stem[:40]
    jid = f"{stem}_{datetime.now():%m%d-%H%M%S}"
    outdir = RESULTS / jid
    outdir.mkdir(parents=True, exist_ok=True)
    src = outdir / f"source{Path(file.filename).suffix}"
    src.write_bytes(raw)

    # —— 阶段1：即时元信息
    info = stepinfo.read(src).to_dict()
    info["filename"] = file.filename          # 存盘名是 source.xxx，展示要用原始名
    info.update(digest=digest, id=jid, uploaded_at=datetime.now().isoformat(timespec="seconds"),
                status="converting")

    if not info["drawable"]:
        info["status"] = "failed"
        info["stage"] = "失败"
        info["error"] = info["drawable_note"]
        (outdir / "meta.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
        return {"id": jid, "cached": False}

    # 智能默认：装配体不画隐藏线（35 零件实测 16438 条隐藏边，图糊且导出慢 18 倍）
    opts = {"sheet": "A3" if info["is_assembly"] else "A4",
            "projection": "third",              # 客户图纸实测全部为第三角
            "no_hidden": bool(info["is_assembly"])}
    info["options"] = opts
    info["stage"] = "排队中"
    (outdir / "meta.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))

    # 父线程只负责起子进程 + 监控；转换本身在独立子进程里跑（见 _run）
    threading.Thread(target=_run,
                     args=(outdir, src, opts, Path(file.filename).stem),
                     daemon=True).start()
    return {"id": jid, "cached": False}


@router.get("/results")
def results():
    out = []
    for d in _result_dirs():
        f = d / "meta.json"
        if not (d.is_dir() and f.exists()):
            continue
        try:
            m = json.loads(f.read_text())
        except Exception:
            continue
        out.append({"id": d.name, "filename": m.get("filename", d.name),
                    "protocol": m.get("protocol"), "status": m.get("status"),
                    "is_assembly": m.get("is_assembly"),
                    "uploaded_at": m.get("uploaded_at", "")})
    return out


@router.get("/results/{rid}")
def result(rid: str):
    f = RESULTS / rid / "meta.json"
    if not f.exists():
        raise HTTPException(404)
    m = json.loads(f.read_text())
    if m.get("status") == "converting":
        m["stages"] = STAGES            # stage 已在 meta.json 里（子进程写的）
    return m


@router.get("/results/{rid}/{name}")
def artifact(rid: str, name: str):
    if name not in ("drawing.svg", "drawing.pdf", "drawing.dxf", "model.glb"):
        raise HTTPException(404)
    f = RESULTS / rid / name
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f)


@router.delete("/results/{rid}")
def drop(rid: str):
    d = RESULTS / rid
    if d.is_dir():
        shutil.rmtree(d)
    return {"ok": True}


