"""ECAD → 制程图纸 路由（挂在 /api/process 下）。

与 STEP 路由完全独立的一条链路：
  输入  Pick&Place（必需） + Gerber 板框（可选） + Gerber 锡膏焊盘（可选）
  处理  纯 Python 标准库解析 + 坐标变换 + 规则判公差 —— 无 OCCT、无 AI
  输出  坐标式标注的制程图纸 SVG / PDF（含中文）

与 STEP 不同：这里全程亚秒级、纯 Python，不需要子进程隔离，也不需要两阶段。
上传即同步生成，直接返回。
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent.parent      # project root
sys.path.insert(0, str(ROOT / "ecad"))             # 制程图纸引擎

import ecad2process                                 # noqa: E402
import render as pdf_render                         # noqa: E402

RESULTS = ROOT / "results" / "process"              # 制程图纸结果独立子目录
RESULTS.mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/api/process", tags=["process"])


def _result_dirs():
    RESULTS.mkdir(parents=True, exist_ok=True)
    try:
        return sorted(RESULTS.iterdir(), reverse=True)
    except FileNotFoundError:
        return []


@router.post("/convert")
async def convert(
    pnp: UploadFile = File(...),
    outline: UploadFile | None = File(None),
    paste: UploadFile | None = File(None),
    datum_ref: str = Form(""),
    proc: str = Form("RX DA (Die Attach)"),
):
    """上传 ECAD 文件 -> 同步生成制程图纸。

    pnp 必需（Pick&Place CSV）；outline/paste 可选（Gerber 板框 / 锡膏层）。
    datum_ref 空则自动取最靠左下的元件为基准。
    """
    stem = Path(pnp.filename).stem[:40]
    jid = f"{stem}_{datetime.now():%m%d-%H%M%S}"
    outdir = RESULTS / jid
    outdir.mkdir(parents=True, exist_ok=True)

    # 落盘上传文件（原始名保留在 meta，磁盘名固定，避免路径注入）
    pnp_path = outdir / f"pnp{Path(pnp.filename).suffix or '.csv'}"
    pnp_path.write_bytes(await pnp.read())

    outline_path = paste_path = None
    if outline is not None and outline.filename:
        outline_path = outdir / f"outline{Path(outline.filename).suffix or '.gbr'}"
        outline_path.write_bytes(await outline.read())
    if paste is not None and paste.filename:
        paste_path = outdir / f"paste{Path(paste.filename).suffix or '.gbr'}"
        paste_path.write_bytes(await paste.read())

    meta = {
        "id": jid,
        "filename": pnp.filename,
        "outline_name": outline.filename if outline_path else None,
        "paste_name": paste.filename if paste_path else None,
        "proc": proc,
        "datum_ref": datum_ref or None,
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        svg, info = ecad2process.generate(
            str(pnp_path),
            outline_path=str(outline_path) if outline_path else None,
            paste_path=str(paste_path) if paste_path else None,
            datum_ref=datum_ref or None,
            proc=proc,
        )
        (outdir / "drawing.svg").write_text(svg, encoding="utf-8")
        try:
            pdf_render.render(str(outdir / "drawing.svg"), str(outdir / "drawing.pdf"))
        except Exception:
            pass                          # PDF 失败不阻断，SVG 仍可看
        meta.update(info)
        meta["status"] = "done"
        meta["has_pdf"] = (outdir / "drawing.pdf").exists()
    except Exception as e:
        meta["status"] = "failed"
        meta["error"] = str(e) or repr(e)

    (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return {"id": jid}


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
                    "proc": m.get("proc"), "status": m.get("status"),
                    "n_comps": m.get("n_comps"),
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
    if name not in ("drawing.svg", "drawing.pdf"):
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
