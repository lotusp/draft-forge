"""SVG -> PDF/PNG，并把含中文的文字换成中文字体（svglib 默认只用 Helvetica）。"""
import os
import sys

from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Group, String
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from svglib.svglib import svg2rlg

CN = None
for p in ("/System/Library/Fonts/STHeiti Light.ttc",
          "/System/Library/Fonts/PingFang.ttc",
          "/System/Library/Fonts/Hiragino Sans GB.ttc"):
    if os.path.exists(p):
        try:
            pdfmetrics.registerFont(TTFont("CN", p, subfontIndex=0))
            CN = "CN"
            break
        except Exception:
            continue


def _fix_fonts(node):
    for c in getattr(node, "contents", []):
        if isinstance(c, String):
            if CN and any("一" <= ch <= "鿿" for ch in (c.text or "")):
                c.fontName = CN
        elif isinstance(c, Group):
            _fix_fonts(c)


def render(svg_path, pdf_path):
    d = svg2rlg(svg_path)
    _fix_fonts(d)
    renderPDF.drawToFile(d, pdf_path)


if __name__ == "__main__":
    svg = sys.argv[1]
    pdf = sys.argv[2] if len(sys.argv) > 2 else svg.replace(".svg", ".pdf")
    render(svg, pdf)
    print(f"{'中文字体 ' + CN if CN else '未找到中文字体'} -> {pdf}")
