import os, json, re, secrets
from pathlib import Path
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db

BASE=Path(__file__).resolve().parents[1]
load_dotenv(BASE/".env")
db.init()
db.seed(os.getenv("ADMIN_USERNAME","admin"), os.getenv("ADMIN_PASSWORD","change-this-immediately"))

app=FastAPI(title="İtfaiye Yapay Zekâ Kılavuzu")
handler=app
app.mount("/static", StaticFiles(directory=str(BASE/"app/static")), name="static")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("APP_SECRET_KEY","change-me"), max_age=60*60*24*7)
templates=Jinja2Templates(directory=str(BASE/"app/templates"))

def render_template(name, context):
    context = dict(context)
    request = context.pop("request")
    return templates.TemplateResponse(
        name,
        {"request": request, **context}
    )


GEMINI_API_KEY=os.getenv("GEMINI_API_KEY","").strip()
MODEL=os.getenv("GEMINI_MODEL","gemini-2.5-flash-lite").strip()
GLOBAL_VS=os.getenv("GEMINI_FILE_SEARCH_STORE_ID","").strip()
DAILY=int(os.getenv("QUESTIONS_PER_DAY","30"))
MAXQ=int(os.getenv("MAX_QUESTION_CHARS","2000"))
GEMINI_API_BASE="https://generativelanguage.googleapis.com/v1beta"
GEMINI_UPLOAD_BASE="https://generativelanguage.googleapis.com/upload/v1beta"

SYSTEM="""Sen İtfaiye Yapay Zekâ Kılavuzu asistanısın.
Yalnızca file_search tarafından getirilen yüklenmiş dokümanlarla desteklenen
bilgileri kullan. Dokümanda yeterli bilgi yoksa bilgi uydurma ve açıkça
'Bu konuda yüklenen dokümanlarda yeterli bilgi bulunamadı.' de.
Türkçe cevap ver.
Araç kullanımı, acil işletim, bakım ve güvenlik sorularında dokümandaki
uyarıları atlama.
Cevap düzeni:
KISA CEVAP
UYGULANACAK İŞLEM
GÜVENLİK / DİKKAT
KAYNAK
Sayfa/bölüm bilgisi yalnızca getirilen içerikte destekleniyorsa yaz; uydurma.
"""

def current_user(request):
    return db.user_by_token(request.session.get("token"))

def redirect_login():
    return RedirectResponse("/login",status_code=303)

@app.get("/",response_class=HTMLResponse)
def home(request:Request):
    u=current_user(request)
    return render_template("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
                                                   "remaining": max(0,DAILY-(db.question_count(u["id"]) if u else 0))})

@app.get("/register",response_class=HTMLResponse)
def register_page(request:Request):
    return render_template("register.html",{"request":request,"error":None})

@app.post("/register")
def register(request:Request,username:str=Form(...),password:str=Form(...)):
    username=username.strip()
    if len(username)<3 or len(password)<8:
        return render_template("register.html",{"request":request,"error":"Kullanıcı adı en az 3, şifre en az 8 karakter olmalı."})
    if not db.create_user(username,password):
        return render_template("register.html",{"request":request,"error":"Bu kullanıcı adı zaten kullanılıyor."})
    return RedirectResponse("/login?registered=1",303)

@app.get("/login",response_class=HTMLResponse)
def login_page(request:Request):
    return render_template("login.html",{"request":request,"registered":request.query_params.get("registered"),"error":None})

@app.post("/login")
def login(request:Request,username:str=Form(...),password:str=Form(...)):
    u=db.auth(username.strip(),password)
    if not u:
        return render_template("login.html",{"request":request,"registered":None,"error":"Kullanıcı adı veya şifre hatalı."})
    request.session["token"]=db.new_session(u["id"])
    return RedirectResponse("/admin" if u["role"]=="admin" else "/",303)

@app.get("/logout")
def logout(request:Request):
    t=request.session.get("token")
    if t: db.delete_session(t)
    request.session.clear()
    return RedirectResponse("/",303)

def _vehicle_store(vehicle_id):
    store = GLOBAL_VS
    try:
        matches = []
        for d in db.documents():
            try:
                if d["vehicle_id"] == vehicle_id and d["vector_store_id"]:
                    matches.append(d["vector_store_id"])
            except (KeyError, TypeError):
                pass
        if matches:
            store = matches[-1]
    except Exception as exc:
        print("GEMINI STORE LOOKUP:", exc)
    return store


def _ask_gemini(question, vehicle_name, store_name):
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"parts": [{
            "text": f"Seçilen araç: {vehicle_name}\nSoru: {question}"
        }]}]
    }

    if store_name:
        payload["tools"] = [{
            "fileSearch": {
                "fileSearchStoreNames": [store_name]
            }
        }]

    r = requests.post(
        f"{GEMINI_API_BASE}/models/{MODEL}:generateContent",
        params={"key": GEMINI_API_KEY},
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=180
    )

    if not r.ok:
        try:
            detail = r.json().get("error", {}).get("message") or r.text
        except Exception:
            detail = r.text
        raise RuntimeError(
            f"Gemini API hata verdi ({r.status_code}): {str(detail)[:800]}"
        )

    obj = r.json()
    texts, sources = [], []

    for candidate in obj.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if isinstance(part, dict) and part.get("text"):
                texts.append(part["text"])

        grounding = candidate.get("groundingMetadata", {})
        for chunk in grounding.get("groundingChunks", []):
            ctx = chunk.get("retrievedContext", {})
            title = ctx.get("title")
            if title and title not in sources:
                sources.append(title)

    answer = "\n".join(texts).strip()
    return (
        answer or "Bu konuda yüklenen dokümanlarda yeterli bilgi bulunamadı.",
        sources
    )


@app.post("/ask",response_class=HTMLResponse)
def ask(request:Request,question:str=Form(...),vehicle_id:int=Form(...)):
    u=current_user(request)
    if not u:
        return redirect_login()

    question=question.strip()
    if not question:
        return RedirectResponse("/",303)

    if len(question)>MAXQ:
        return render_template("home.html", {
            "request":request,"user":u,"vehicles":db.vehicles(),
            "remaining":0,
            "error":f"Soru en fazla {MAXQ} karakter olabilir."
        })

    if db.question_count(u["id"])>=DAILY:
        return render_template("home.html", {
            "request":request,"user":u,"vehicles":db.vehicles(),
            "remaining":0,
            "error":"Günlük soru limitinize ulaştınız."
        })

    if not GEMINI_API_KEY:
        return render_template("home.html", {
            "request":request,"user":u,"vehicles":db.vehicles(),
            "remaining":max(0,DAILY-db.question_count(u["id"])),
            "error":"GEMINI_API_KEY Vercel'de bulunamadı."
        })

    vehicle_name=next(
        (v["name"] for v in db.vehicles() if v["id"]==vehicle_id),
        "Bilinmiyor"
    )
    store=_vehicle_store(vehicle_id)

    if store and not str(store).startswith("fileSearchStores/"):
        return render_template("home.html", {
            "request":request,"user":u,"vehicles":db.vehicles(),
            "remaining":max(0,DAILY-db.question_count(u["id"])),
            "error":"Bu aracın PDF kaydı eski OpenAI sistemine ait. PDF'yi yeniden yükleyin."
        })

    try:
        answer,sources=_ask_gemini(question,vehicle_name,store)
    except Exception as exc:
        print("GEMINI API HATASI:",exc)
        return render_template("home.html", {
            "request":request,"user":u,"vehicles":db.vehicles(),
            "remaining":max(0,DAILY-db.question_count(u["id"])),
            "error":str(exc)
        })

    db.save_question(
        u["id"],vehicle_id,question,answer,
        json.dumps(sources,ensure_ascii=False)
    )

    return render_template("home.html", {
        "request":request,"user":u,"vehicles":db.vehicles(),
        "remaining":max(0,DAILY-db.question_count(u["id"])),
        "answer":answer,"sources":sources,"question":question
    })


@app.get("/admin",response_class=HTMLResponse)
def admin(request:Request):
    u=current_user(request)
    if not u or u["role"]!="admin": return redirect_login()
    return render_template("admin.html",{"request":request,"user":u,"vehicles":db.vehicles(),
        "documents":db.documents(),"questions":db.recent_questions()})

@app.post("/admin/user")
def admin_user(request:Request,username:str=Form(...),password:str=Form(...)):
    u=current_user(request)
    if not u or u["role"]!="admin": return redirect_login()
    username=username.strip()
    if len(username) >= 3 and len(password) >= 8:
        db.create_user(username,password)
    return RedirectResponse("/admin",303)

@app.post("/admin/vehicle")
def admin_vehicle(request:Request,name:str=Form(...)):
    u=current_user(request)
    if not u or u["role"]!="admin": return redirect_login()
    db.add_vehicle(name.strip())
    return RedirectResponse("/admin",303)

@app.post("/admin/document")
async def admin_document(request:Request,vehicle_id:int=Form(...),file:UploadFile=File(...)):
    u=current_user(request)
    if not u or u["role"]!="admin":
        return redirect_login()

    if not file.filename.lower().endswith(".pdf"):
        return RedirectResponse("/admin",303)

    if not GEMINI_API_KEY:
        return RedirectResponse("/admin",303)

    content=await file.read()

    try:
        rr=requests.post(
            f"{GEMINI_API_BASE}/fileSearchStores",
            params={"key":GEMINI_API_KEY},
            headers={"Content-Type":"application/json"},
            json={"displayName":f"Itfaiye - {file.filename}"},
            timeout=60
        )
        if not rr.ok:
            raise RuntimeError(f"Store oluşturulamadı: {rr.text[:800]}")

        store_name=rr.json().get("name")
        if not store_name:
            raise RuntimeError("Gemini File Search store adı alınamadı.")

        rr=requests.post(
            f"{GEMINI_UPLOAD_BASE}/{store_name}:uploadToFileSearchStore",
            params={"key":GEMINI_API_KEY},
            headers={
                "X-Goog-Upload-Protocol":"resumable",
                "X-Goog-Upload-Command":"start",
                "X-Goog-Upload-Header-Content-Length":str(len(content)),
                "X-Goog-Upload-Header-Content-Type":"application/pdf",
                "Content-Type":"application/json"
            },
            json={"displayName":file.filename},
            timeout=60
        )
        if not rr.ok:
            raise RuntimeError(f"PDF yüklemesi başlatılamadı: {rr.text[:800]}")

        upload_url=rr.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise RuntimeError("Gemini yükleme URL'si alınamadı.")

        rr=requests.post(
            upload_url,
            headers={
                "Content-Length":str(len(content)),
                "X-Goog-Upload-Offset":"0",
                "X-Goog-Upload-Command":"upload, finalize"
            },
            data=content,
            timeout=300
        )
        if not rr.ok:
            raise RuntimeError(f"PDF yüklenemedi: {rr.text[:800]}")

        db.add_doc(vehicle_id,file.filename,"",store_name)
        print("GEMINI PDF YÜKLENDİ:",file.filename,store_name)

    except Exception as exc:
        print("GEMINI PDF YÜKLEME HATASI:",exc)

    return RedirectResponse("/admin",303)

