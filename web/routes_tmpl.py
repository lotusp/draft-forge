"""STEP + 图纸模板(DXF) → 成品图纸 路由（挂在 /api/tmpl 下）。

与另外两条管道的区别：**不生成标注**——标注是模板里画好的。
只做：换视图几何 + 填标题栏 + 出图。故不需要标注/公差那套逻辑。

端点：
  POST /inspect            上传 DXF -> 扫出占位符清单，前端据此**动态生成表单**
                           （不同模板字段不同，让模板自己说了算，不写死）
  POST /convert            上传 STEP + DXF -> 换视图几何 -> 出预览（占位符原样保留）
  POST /results/{id}/refill  只填字段重出图 —— **不重跑 HLR**

为什么分两步：换视图几何要跑 HLR（慢），填字段只是替换文字（快）。
convert 时把「几何已换、占位符未填」的中间结果存成 base.dxf，
之后每次改字段点「重新生成」只走 refill，秒出。
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))             # step2drawing（HLR 投影）
sys.path.insert(0, str(ROOT / "tmpl"))             # 模板填充引擎
sys.path.insert(0, str(ROOT / "ecad"))             # render（SVG->PDF，含中文字体）

import step2template                                # noqa: E402

RESULTS = ROOT / "results" / "tmpl"
RESULTS.mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/api/tmpl", tags=["tmpl"])


def _result_dirs():
    RESULTS.mkdir(parents=True, exist_ok=True)
    try:
        return sorted(RESULTS.iterdir(), reverse=True)
    except FileNotFoundError:
        return []


@router.post("/inspect")
async def inspect(dxf: UploadFile = File(...)):
    """上传模板 DXF -> 返回占位符清单，供前端动态渲染表单。"""
    tmp = RESULTS / f"_inspect_{datetime.now():%H%M%S%f}.dxf"
    try:
        tmp.write_bytes(await dxf.read())
        info = step2template.scan(tmp)
        info["filename"] = dxf.filename
        return info
    except Exception as e:
        raise HTTPException(400, f"模板解析失败：{e}")
    finally:
        tmp.unlink(missing_ok=True)


def _emit(outdir: Path, doc, meta: dict, vals: dict):
    """填字段 -> 渲染 -> 落盘 DXF/SVG/PDF，并更新 meta。convert 与 refill 共用。"""
    filled, missing = step2template.fill(doc, vals)
    doc.saveas(outdir / "drawing.dxf")
    (outdir / "drawing.svg").write_text(step2template.render(doc), encoding="utf-8")
    try:
        import render as pdf_render
        pdf_render.render(str(outdir / "drawing.svg"), str(outdir / "drawing.pdf"))
    except Exception:
        pass                          # PDF 失败不阻断，SVG 仍可看
    meta.update(fields=vals, filled=filled, missing=missing, status="done",
                has_pdf=(outdir / "drawing.pdf").exists())
    (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))


@router.post("/convert")
async def convert(step: UploadFile = File(...), dxf: UploadFile = File(...)):
    """换视图几何 -> 出预览。字段不在这里填：占位符原样保留，供用户对着图填。"""
    stem = Path(dxf.filename).stem[:40]
    jid = f"{stem}_{datetime.now():%m%d-%H%M%S}"
    outdir = RESULTS / jid
    outdir.mkdir(parents=True, exist_ok=True)

    step_path = outdir / f"model{Path(step.filename).suffix or '.step'}"
    dxf_path = outdir / "template.dxf"
    step_path.write_bytes(await step.read())
    dxf_path.write_bytes(await dxf.read())

    meta = {"id": jid, "filename": dxf.filename, "step_name": step.filename,
            "uploaded_at": datetime.now().isoformat(timespec="seconds")}
    try:
        doc, info = step2template.build(step_path, dxf_path)
        # 存「几何已换、占位符未填」的基准盘 —— refill 直接读它，不必重跑 HLR
        doc.saveas(outdir / "base.dxf")
        meta.update(info)
        meta["template_fields"] = [f["key"] for f in
                                   step2template.scan(dxf_path)["fields"]]
        _emit(outdir, doc, meta, {})           # 预览：不填字段
    except Exception as e:
        meta["status"] = "failed"
        meta["error"] = str(e) or repr(e)
        (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return {"id": jid}


@router.post("/results/{rid}/refill")
async def refill(rid: str, fields: str = Form("{}")):
    """只填字段重出图。读 base.dxf（几何已换好），**不重跑 HLR**，秒出。"""
    outdir = RESULTS / rid
    base = outdir / "base.dxf"
    f = outdir / "meta.json"
    if not (base.exists() and f.exists()):
        raise HTTPException(404, "结果不存在或缺少基准盘 base.dxf")
    try:
        vals = json.loads(fields) if fields else {}
    except json.JSONDecodeError:
        vals = {}
    import ezdxf
    meta = json.loads(f.read_text())
    _emit(outdir, ezdxf.readfile(str(base)), meta, vals)
    return {"id": rid, "filled": meta["filled"], "missing": meta["missing"]}


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
                    "step_name": m.get("step_name"), "status": m.get("status"),
                    "scale": m.get("scale"),
                    "uploaded_at": m.get("uploaded_at", "")})
    return out


@router.get("/results/{rid}")
def result(rid: str):
    f = RESULTS / rid / "meta.json"
    if not f.exists():
        raise HTTPException(404)
    return json.loads(f.read_text())


@router.get("/results/{rid}/{name}")
def artifact(rid: str, name: str):
    if name not in ("drawing.svg", "drawing.pdf", "drawing.dxf"):
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
