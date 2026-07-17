"""Draft-Forge Demo 后端入口（thin）。

三条完全独立的功能，各自一个路由模块、各自的结果目录：
  · STEP → 2D 工程图      web/routes_step.py     /api/step/*     results/step/
  · ECAD → 制程图纸        web/routes_process.py  /api/process/*  results/process/
  · STEP+模板 → 成品图纸   web/routes_tmpl.py     /api/tmpl/*     results/tmpl/

本文件只负责组装 app：挂 router + 静态资源 + 首页。业务逻辑都在各自模块里。

运行：  uvicorn web.server:app --reload --port 8000     (cwd = demo_3d2d/)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from web import routes_process, routes_step, routes_tmpl

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Draft-Forge Demo")
app.include_router(routes_step.router)
app.include_router(routes_process.router)
app.include_router(routes_tmpl.router)


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


app.mount("/static", StaticFiles(directory=STATIC), name="static")
