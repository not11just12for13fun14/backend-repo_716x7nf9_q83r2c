import io
import math
from typing import Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from PIL import Image, ImageDraw
import pypdfium2 as pdfium
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

app = FastAPI(title="Poster Tiler API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0
PT_PER_MM = PT_PER_INCH / MM_PER_INCH

PAPER_PRESETS_MM = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "Letter": (215.9, 279.4),  # 8.5 x 11 in
    "Legal": (215.9, 355.6),   # 8.5 x 14 in
}


def parse_paper(paper: str) -> Tuple[float, float]:
    """Return (width_pt, height_pt) for a paper string.
    Supports presets (A4, A3, Letter, Legal) and custom:W_mm,H_mm
    """
    if not paper:
        paper = "A4"
    paper = paper.strip()
    if paper in PAPER_PRESETS_MM:
        w_mm, h_mm = PAPER_PRESETS_MM[paper]
        return w_mm * PT_PER_MM, h_mm * PT_PER_MM
    if paper.lower().startswith("custom:"):
        try:
            rest = paper.split(":", 1)[1]
            w_str, h_str = rest.split(",")
            w_mm = float(w_str)
            h_mm = float(h_str)
            return w_mm * PT_PER_MM, h_mm * PT_PER_MM
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid custom paper format. Use custom:W_mm,H_mm")
    raise HTTPException(status_code=400, detail="Unsupported paper size")


def render_pdf_first_page_to_image(pdf_bytes: bytes, max_pixels: int) -> Image.Image:
    """Rasterize the first page of a PDF to a PIL Image, keeping within a pixel budget.
    max_pixels is an approximate budget for width*height.
    """
    try:
        pdf = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
        if len(pdf) == 0:
            raise HTTPException(status_code=400, detail="PDF has no pages")
        page = pdf[0]
        w_pt, h_pt = page.get_size()  # in points
        # scale so that (w*s) * (h*s) ~= max_pixels
        if w_pt <= 0 or h_pt <= 0:
            raise HTTPException(status_code=400, detail="Invalid PDF page size")
        scale = math.sqrt(max(1.0, max_pixels) / (w_pt * h_pt))
        # clamp scale to reasonable range
        scale = max(0.25, min(scale, 6.0))
        bitmap = page.render(scale=scale)
        pil = bitmap.to_pil()
        # Ensure RGB
        if pil.mode not in ("RGB", "RGBA"):
            pil = pil.convert("RGB")
        return pil
    except pdfium.PdfiumError:
        raise HTTPException(status_code=400, detail="Failed to read PDF. Is the file valid?")


def draw_grid_overlay(img: Image.Image, rows: int, cols: int) -> Image.Image:
    overlay = img.copy()
    draw = ImageDraw.Draw(overlay)
    w, h = overlay.size
    # Grid line style
    color = (0, 255, 255, 255)
    # Vertical lines
    for c in range(1, cols):
        x = round(c * w / cols)
        draw.line([(x, 0), (x, h)], fill=color, width=max(1, round(min(w, h) * 0.002)))
    # Horizontal lines
    for r in range(1, rows):
        y = round(r * h / rows)
        draw.line([(0, y), (w, y)], fill=color, width=max(1, round(min(w, h) * 0.002)))
    return overlay


def cut_into_tiles(img: Image.Image, rows: int, cols: int):
    w, h = img.size
    tile_w = w / cols
    tile_h = h / rows
    tiles = []
    for r in range(rows):
        for c in range(cols):
            left = int(round(c * tile_w))
            upper = int(round(r * tile_h))
            right = int(round((c + 1) * tile_w))
            lower = int(round((r + 1) * tile_h))
            tiles.append(img.crop((left, upper, right, lower)))
    return tiles


@app.get("/")
async def root():
    return {"message": "Poster Tiler API running"}


@app.get("/test")
async def test():
    return {"backend": "✅ Running"}


@app.post("/api/preview")
async def api_preview(
    file: UploadFile = File(...),
    rows: int = Form(1),
    cols: int = Form(1),
    max_preview_px: int = Form(1200000),
):
    if rows < 1 or cols < 1:
        raise HTTPException(status_code=400, detail="rows and cols must be >= 1")
    if file.content_type not in ("application/pdf", "application/x-pdf", "application/acrobat"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file")
    pdf_bytes = await file.read()
    base_img = render_pdf_first_page_to_image(pdf_bytes, max_pixels=max_preview_px)
    overlaid = draw_grid_overlay(base_img, rows, cols)
    buf = io.BytesIO()
    overlaid.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.post("/api/export")
async def api_export(
    file: UploadFile = File(...),
    rows: int = Form(1),
    cols: int = Form(1),
    paper: str = Form("A4"),
    margin_mm: float = Form(5.0),
):
    if rows < 1 or cols < 1:
        raise HTTPException(status_code=400, detail="rows and cols must be >= 1")
    if file.content_type not in ("application/pdf", "application/x-pdf", "application/acrobat"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file")

    pdf_bytes = await file.read()
    # Render with higher pixel budget for export quality
    base_img = render_pdf_first_page_to_image(pdf_bytes, max_pixels=16_000_000)
    tiles = cut_into_tiles(base_img, rows, cols)

    pw_pt, ph_pt = parse_paper(paper)
    margin_pt = max(0.0, float(margin_mm)) * PT_PER_MM
    printable_w = max(1.0, pw_pt - 2 * margin_pt)
    printable_h = max(1.0, ph_pt - 2 * margin_pt)

    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(pw_pt, ph_pt))

    for tile in tiles:
        # Ensure RGB for ReportLab
        if tile.mode != "RGB":
            tile = tile.convert("RGB")
        tw, th = tile.size
        # Compute scale to fit within printable area (keep aspect)
        sx = printable_w / tw
        sy = printable_h / th
        s = min(sx, sy)
        draw_w = tw * s
        draw_h = th * s
        x = (pw_pt - draw_w) / 2.0
        y = (ph_pt - draw_h) / 2.0
        c.drawImage(ImageReader(tile), x, y, width=draw_w, height=draw_h)
        c.showPage()

    c.save()
    out.seek(0)

    headers = {
        "Content-Disposition": 'attachment; filename="poster-tiles.pdf"'
    }
    return StreamingResponse(out, media_type="application/pdf", headers=headers)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
