import os
import io
import re
import fitz
import requests
from urllib.parse import quote
from pydantic import BaseModel
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

app = FastAPI(title="即刻目录 API")

# Static files
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(os.path.join(static_dir, "css"), exist_ok=True)
os.makedirs(os.path.join(static_dir, "js"), exist_ok=True)

# ---- API Routes (must be registered BEFORE the static mount) ----

class RecognizeReq(BaseModel):
    api_key: str
    api_base: str
    model_name: str
    images_base64: List[str]


def build_content_disposition(prefix: str, original_filename: str) -> str:
    # HTTP headers must be latin-1; keep ASCII fallback and provide UTF-8 filename*.
    original = os.path.basename(original_filename or "document.pdf")
    output_name = f"{prefix}{original}"
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", output_name)
    fallback = fallback or f"{prefix}document.pdf"
    encoded = quote(output_name, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'

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
    file: UploadFile = File(...),
    toc_text: str = Form(...),
    toc_start_page: Optional[int] = Form(None),
    base_offset: Optional[int] = Form(None)
):
    try:
        pdf_bytes = await file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        # New semantic:
        # toc_start_page means "TOC page 1 corresponds to which PDF page".
        # Backward compatible with old base_offset (where old abs = printed + offset).
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
        out_pdf.seek(0)
        disposition = build_content_disposition("toc_", file.filename or "document.pdf")
        return StreamingResponse(out_pdf, media_type='application/pdf',
            headers={'Content-Disposition': disposition})
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/split_pdf")
async def split_pdf(
    file: UploadFile = File(...),
    start_page: int = Form(...),
    end_page: int = Form(...)
):
    try:
        pdf_bytes = await file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
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
            file.filename or "document.pdf"
        )
        return StreamingResponse(out_pdf, media_type='application/pdf',
            headers={'Content-Disposition': disposition})
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---- Serve index.html at root ----
@app.get("/")
async def root():
    # Use relative redirect so deployments under a path prefix still work.
    return RedirectResponse(url="static/index.html")

# ---- Mount static AFTER API routes to avoid path conflicts ----
app.mount("/static", StaticFiles(directory=static_dir), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
