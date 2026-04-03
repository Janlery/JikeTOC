import os
import io
import re
import uuid
import time
import shutil
import fitz
import requests
from PIL import Image
from urllib.parse import quote
from pydantic import BaseModel
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

app = FastAPI(title="即刻目录 API")

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
file_store = {}  # file_id → {"path": str, "filename": str, "mtime": float}

CLEANUP_INTERVAL = 1800  # 30 minutes

def cleanup_old_uploads():
    now = time.time()
    for fid, info in list(file_store.items()):
        if now - info["mtime"] > CLEANUP_INTERVAL:
            try:
                os.unlink(info["path"])
            except OSError:
                pass
            file_store.pop(fid, None)

# Static files
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(os.path.join(static_dir, "css"), exist_ok=True)
os.makedirs(os.path.join(static_dir, "js"), exist_ok=True)

# ---- API Routes ----

class RecognizeReq(BaseModel):
    api_key: str
    api_base: str
    model_name: str
    images_base64: List[str]


def build_content_disposition(prefix: str, original_filename: str) -> str:
    original = os.path.basename(original_filename or "document.pdf")
    output_name = f"{prefix}{original}"
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", output_name)
    fallback = fallback or f"{prefix}document.pdf"
    encoded = quote(output_name, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    cleanup_old_uploads()
    file_id = uuid.uuid4().hex
    dest_path = os.path.join(UPLOAD_DIR, f"{file_id}.pdf")
    with open(dest_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        doc = fitz.open(dest_path)
        pages = doc.page_count
        doc.close()
    except Exception:
        os.unlink(dest_path)
        return {"status": "error", "message": "Invalid or corrupted PDF"}
    file_store[file_id] = {"path": dest_path, "filename": file.filename or "document.pdf", "mtime": time.time()}
    return {"status": "ok", "file_id": file_id, "filename": file.filename, "pages": pages}


@app.get("/api/page/{file_id}/{page_num:int}")
async def get_page_image(file_id: str, page_num: int):
    if file_id not in file_store:
        return {"status": "error", "message": "File not found"}
    info = file_store[file_id]
    info["mtime"] = time.time()
    try:
        doc = fitz.open(info["path"])
        page = doc[page_num - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        img_bytes = pix.tobytes("png")
        doc.close()
        return StreamingResponse(io.BytesIO(img_bytes), media_type="image/png")
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/recognize")
async def recognize_toc(req: RecognizeReq):
    headers = {
        "Authorization": f"Bearer {req.api_key}",
        "Content-Type": "application/json"
    }
    base_url = req.api_base.rstrip("/")
    if not base_url.endswith("/v1"):
        if not base_url.endswith("/chat/completions"):
            base_url = base_url + "/v1"
    api_url = f"{base_url}/chat/completions" if not base_url.endswith("/chat/completions") else base_url

    content = [
        {
            "type": "text",
            "text": (
                "You are an expert OCR system. Extract the Table of Contents (TOC) from these images.\n"
                "Output ONLY the plain text structure with NO markdown wrapping, NO backticks, and NO extra conversational text.\n"
                "Format rules:\n"
                "1. Each line is an entry: [Title] [Page Number]\n"
                "2. Use 4 spaces of indentation to indicate sub-levels.\n"
                "Example:\n"
                "Chapter 1 Introduction 1\n"
                "    1.1 Background 3\n"
                "        1.1.1 History 5"
            )
        }
    ]
    for b64 in req.images_base64:
        content.append({"type": "image_url", "image_url": {"url": b64}})

    payload = {
        "model": req.model_name,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 4096,
        "temperature": 0.2
    }
    try:
        resp = requests.post(api_url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        toc_text = data["choices"][0]["message"]["content"]
        return {"status": "ok", "text": toc_text}
    except Exception as e:
        return {"status": "error", "message": str(e), "details": resp.text if 'resp' in locals() else ""}


@app.post("/api/generate_pdf")
async def generate_pdf(
    file: UploadFile = File(None),
    file_id: Optional[str] = Form(None),
    toc_text: str = Form(...),
    toc_start_page: Optional[int] = Form(None),
    base_offset: Optional[int] = Form(None)
):
    tmp_source = None
    try:
        if file_id and file_id in file_store:
            source_path = file_store[file_id]["path"]
        elif file:
            tmp_source = os.path.join(UPLOAD_DIR, f"tmp_{uuid.uuid4().hex}.pdf")
            with open(tmp_source, "wb") as f:
                f.write(file.file.read())
            source_path = tmp_source
        else:
            return {"status": "error", "message": "No file provided"}

        doc = fitz.open(source_path)
        if toc_start_page is not None:
            effective_toc_start_page = toc_start_page
        elif base_offset is not None:
            effective_toc_start_page = base_offset + 1
        else:
            effective_toc_start_page = 1
        toc = []
        current_local_offset = 0
        offset_pattern = re.compile(r"---\s*OFFSET:\s*(-?\d+)\s*---", re.IGNORECASE)
        for line in toc_text.splitlines():
            if not line.strip() or line.strip().startswith("//"):
                continue
            match = offset_pattern.search(line)
            if match:
                current_local_offset = int(match.group(1))
                continue
            leading_spaces = len(line) - len(line.lstrip(' '))
            level = (leading_spaces // 4) + 1
            content = line.strip()
            page_match = re.search(r'\s+(\d+)$', content)
            if page_match:
                title = content[:page_match.start()].strip()
                printed_page = int(page_match.group(1))
            else:
                title = content
                printed_page = 1
            abs_page = (printed_page - 1) + effective_toc_start_page + current_local_offset
            if abs_page < 1: abs_page = 1
            if abs_page > doc.page_count: abs_page = doc.page_count
            toc.append([level, title, abs_page])
        doc.set_toc(toc)
        out_pdf = io.BytesIO()
        doc.save(out_pdf)
        doc.close()
        out_pdf.seek(0)
        fallback_name = (file_store.get(file_id, {}).get("filename", "document.pdf") if file_id else "document.pdf")
        disposition = build_content_disposition("toc_", (file.filename if file else None) or fallback_name)
        return StreamingResponse(out_pdf, media_type='application/pdf',
            headers={'Content-Disposition': disposition})
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if tmp_source:
            try:
                os.unlink(tmp_source)
            except OSError:
                pass


@app.post("/api/split_pdf")
async def split_pdf(
    file: UploadFile = File(None),
    file_id: Optional[str] = Form(None),
    start_page: int = Form(...),
    end_page: int = Form(...)
):
    tmp_source = None
    try:
        if file_id and file_id in file_store:
            source_path = file_store[file_id]["path"]
            filename = file_store[file_id]["filename"]
        elif file:
            tmp_source = os.path.join(UPLOAD_DIR, f"tmp_{uuid.uuid4().hex}.pdf")
            with open(tmp_source, "wb") as f:
                f.write(file.file.read())
            source_path = tmp_source
            filename = file.filename or "document.pdf"
        else:
            return {"status": "error", "message": "No file provided"}

        doc = fitz.open(source_path)
        sp = max(0, start_page - 1)
        ep = min(doc.page_count - 1, end_page - 1)
        if sp > ep:
            sp, ep = ep, sp
        out_doc = fitz.open()
        out_doc.insert_pdf(doc, from_page=sp, to_page=ep)
        out_pdf = io.BytesIO()
        out_doc.save(out_pdf)
        out_pdf.seek(0)
        out_doc.close()
        disposition = build_content_disposition(
            f"split_{start_page}_{end_page}_",
            filename
        )
        return StreamingResponse(out_pdf, media_type='application/pdf',
            headers={'Content-Disposition': disposition})
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if tmp_source:
            try:
                os.unlink(tmp_source)
            except OSError:
                pass


@app.post("/api/compress_pdf")
async def compress_pdf(
    file: UploadFile = File(None),
    file_id: Optional[str] = Form(None),
    image_quality: int = Form(80)
):
    tmp_source = None
    try:
        if file_id and file_id in file_store:
            source_path = file_store[file_id]["path"]
            filename = file_store[file_id]["filename"]
        elif file:
            tmp_source = os.path.join(UPLOAD_DIR, f"tmp_{uuid.uuid4().hex}.pdf")
            with open(tmp_source, "wb") as f:
                f.write(file.file.read())
            source_path = tmp_source
            filename = file.filename or "document.pdf"
        else:
            return {"status": "error", "message": "No file provided"}

        doc = fitz.open(source_path)
        original_size = os.path.getsize(source_path)

        image_quality = max(10, min(100, image_quality))

        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    base_image = doc.extract_image(xref)
                    if not base_image or not base_image["image"]:
                        continue
                    pil_img = Image.open(io.BytesIO(base_image["image"]))
                    if pil_img.mode in ("RGBA", "P"):
                        pil_img = pil_img.convert("RGB")
                    buf = io.BytesIO()
                    pil_img.save(buf, format="JPEG", quality=image_quality, optimize=True)
                    buf.seek(0)
                    new_img = fitz.open(stream=buf.read(), filetype="jpeg")
                    rect = page.get_image_rects(xref)
                    if rect:
                        page.delete_image(xref)
                        page.insert_image(rect[0], stream=new_img.tobytes())
                        new_img.close()
                except Exception:
                    continue

        out_pdf = io.BytesIO()
        doc.save(out_pdf, deflate=True, garbage=4, clean=True)
        doc.close()
        out_pdf.seek(0)
        compressed_size = out_pdf.getbuffer().nbytes

        disposition = build_content_disposition("compressed_", filename)
        return StreamingResponse(out_pdf, media_type='application/pdf',
            headers={
                'Content-Disposition': disposition,
                'X-Original-Size': str(original_size),
                'X-Compressed-Size': str(compressed_size),
            })
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if tmp_source:
            try:
                os.unlink(tmp_source)
            except OSError:
                pass


# ---- Serve index.html at root ----
@app.get("/")
async def root():
    return RedirectResponse(url="static/index.html")

# ---- Mount static AFTER API routes to avoid path conflicts ----
app.mount("/static", StaticFiles(directory=static_dir), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5060, reload=True)
