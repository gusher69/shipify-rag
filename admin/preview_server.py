"""
Standalone preview server — ไม่ต้องการ Supabase หรือ OpenAI
ใช้แค่ดูหน้าตา UI ก่อน
"""
import os, shutil
from pathlib import Path
from fastapi import FastAPI, Request, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import LOCAL_STORAGE_ROOT

KNOWLEDGE_DIR = Path(LOCAL_STORAGE_ROOT)
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory="admin/templates")
app = FastAPI()

ADMIN_USER = "admin"
ADMIN_PASS = "admin"
sessions: set = set()


def check_auth(request: Request):
    if request.cookies.get("session_id") not in sessions:
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
    return True


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@app.post("/admin/login")
async def login(username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        import uuid
        sid = str(uuid.uuid4())
        sessions.add(sid)
        r = RedirectResponse(url="/admin/documents", status_code=302)
        r.set_cookie("session_id", sid)
        return r
    return RedirectResponse(url="/admin/login?error=1", status_code=302)


@app.get("/admin/documents", response_class=HTMLResponse)
async def documents(request: Request, _=Depends(check_auth)):
    files = []
    for path in KNOWLEDGE_DIR.rglob("*"):
        if path.suffix in [".pdf", ".docx", ".md", ".txt"]:
            files.append({"name": path.name, "path": str(path), "size": path.stat().st_size})
    return templates.TemplateResponse(request=request, name="documents.html", context={"files": files})


@app.post("/admin/upload")
async def upload(file: UploadFile = File(...), _=Depends(check_auth)):
    dest = KNOWLEDGE_DIR / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return RedirectResponse(url="/admin/documents?uploaded=1", status_code=302)


@app.post("/admin/sync")
async def sync(_=Depends(check_auth)):
    return RedirectResponse(url="/admin/documents?synced=1", status_code=302)


@app.get("/admin/preview", response_class=HTMLResponse)
async def preview(request: Request, q: str = "", _=Depends(check_auth)):
    results = []
    if q:
        results = [
            {"text": f"ตัวอย่าง chunk ที่เกี่ยวข้องกับ '{q}' — นโยบายการคืนสินค้าของ Shipify ระบุว่าลูกค้าสามารถแจ้งคืนได้ภายใน 7 วันหลังได้รับสินค้า กรณีสินค้าชำรุดหรือผิดรุ่น",
             "source": "policy_return.md", "intent": "นโยบาย", "score": 0.94},
            {"text": "ค่าส่งคำนวณตามน้ำหนักและปริมาตร ช่องทางเรือ 15-25 วัน ช่องทางอากาศ 5-7 วัน ราคาแตกต่างกันตามขนาดพัสดุ",
             "source": "shipping_guide.md", "intent": "ทั่วไป", "score": 0.81},
            {"text": "หากสินค้าเสียหายระหว่างขนส่ง กรุณาถ่ายรูปพร้อมกล่องพัสดุและแจ้งทีม CS ภายใน 48 ชั่วโมงหลังได้รับ",
             "source": "faq.md", "intent": "นโยบาย", "score": 0.73},
        ]
    context = "\n\n".join([f"[{i+1}] {r['text']}" for i, r in enumerate(results)])
    return templates.TemplateResponse(request=request, name="preview.html", context={
        "question": q, "results": results, "context": context
    })


@app.get("/")
async def root():
    return RedirectResponse(url="/admin/login")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
