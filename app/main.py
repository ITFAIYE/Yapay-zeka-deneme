import os, json, secrets
from pathlib import Path
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db

BASE=Path(__file__).resolve().parents[1]
load_dotenv(BASE/".env")
db.init()
db.seed(os.getenv("ADMIN_USERNAME","admin"), os.getenv("ADMIN_PASSWORD","change-this-immediately"))

app=FastAPI(title="İtfaiye Yapay Zekâ Kılavuzu")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("APP_SECRET_KEY","change-me"),
    max_age=60*60*24*7,
    https_only=os.getenv("COOKIE_SECURE","1")=="1",
)
app.mount("/static", StaticFiles(directory=str(BASE/"app/static")), name="static")
templates=Jinja2Templates(directory=str(BASE/"app/templates"))

API=os.getenv("GEMINI_API_KEY")
MODEL=os.getenv("GEMINI_MODEL","gemini-3.5-flash-lite").strip()
GLOBAL_VS=os.getenv("GEMINI_FILE_SEARCH_STORE_ID")
GEMINI_API_BASE="https://generativelanguage.googleapis.com/v1beta"
DAILY=int(os.getenv("QUESTIONS_PER_DAY","30"))
MAXQ=int(os.getenv("MAX_QUESTION_CHARS","2000"))
NOT_FOUND_MSG="Bu konuda yüklenen dokümanlarda yeterli bilgi bulunamadı."

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

SPECULATE_SYSTEM="""Sen İtfaiye Yapay Zekâ Kılavuzu asistanısın.
Yüklenen dokümanlarda bu soruya yeterli yanıt bulunamadı. Kullanıcı yine de
genel bilginle tahmini bir yanıt istedi. Yanıtın başında bunun doğrulanmamış,
tahmini bir bilgi olduğunu açıkça belirt. Türkçe cevap ver. Emin olmadığın
kritik güvenlik adımlarını uydurma; şüpheliysen üretici kılavuzuna ve kurum
prosedürüne başvurulmasını söyle.
"""

# ---------- helpers ----------

def current_user(request):
    return db.user_by_token(request.session.get("token"))

def redirect_login():
    return RedirectResponse("/login",status_code=303)

def csrf_token(request):
    t=request.session.get("csrf")
    if not t:
        t=secrets.token_urlsafe(24)
        request.session["csrf"]=t
    return t

def csrf_ok(request,submitted):
    return submitted and submitted==request.session.get("csrf")

def is_admin(u):
    return bool(u) and u.get("role")=="admin"

def _vehicle_store(vehicle_id):
    """Get the most recent active Gemini File Search store for a vehicle, fallback to global."""
    for d in db.documents():
        if d.get("vehicle_id")==vehicle_id and d.get("active",1) and d.get("vector_store_id"):
            return d["vector_store_id"]
    return GLOBAL_VS


# ---------- home / ask ----------

@app.get("/",response_class=HTMLResponse)
def home(request:Request):
    u=current_user(request)
    if u and u.get("must_change_password"):
        return RedirectResponse("/change-password",303)
    return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
        "csrf":csrf_token(request),
        "remaining": max(0,DAILY-(db.question_count(u["id"]) if u else 0))})

def _call_ai(question,vehicle_name,system,vector_store_id=None,restrict_to_docs=True):
    parts=[{"text":f"{system}\n\nSeçilen araç: {vehicle_name}\nSoru: {question}"}]
    payload={"contents":[{"role":"user","parts":parts}]}
    if restrict_to_docs and vector_store_id:
        payload["tools"]=[{"file_search":{"file_search_store_names":[vector_store_id]}}]
    try:
        r=requests.post(
            f"{GEMINI_API_BASE}/models/{MODEL}:generateContent",
            headers={"x-goog-api-key":API,"Content-Type":"application/json"},
            json=payload,timeout=180)
    except requests.RequestException:
        return None,[]
    if not r.ok:
        print("GEMINI API HATASI:",r.status_code,r.text[:500])
        return None,[]
    obj=r.json()
    texts=[]; sources=[]
    for cand in obj.get("candidates",[]):
        content=cand.get("content",{})
        for part in content.get("parts",[]):
            if "text" in part:
                texts.append(part["text"])
        gm=cand.get("groundingMetadata",{})
        for chunk in gm.get("groundingChunks",[]):
            title=chunk.get("retrievedContext",{}).get("title") or chunk.get("web",{}).get("title")
            if title and title not in sources:
                sources.append(title)
    return "\n".join(texts).strip(),sources

@app.post("/ask",response_class=HTMLResponse)
def ask(request:Request,question:str=Form(...),vehicle_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u: return redirect_login()
    if u.get("must_change_password"): return RedirectResponse("/change-password",303)
    if not csrf_ok(request,csrf):
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":max(0,DAILY-db.question_count(u["id"])),
            "error":"Oturum doğrulaması başarısız, sayfayı yenileyip tekrar deneyin."})
    question=question.strip()
    if not question: return RedirectResponse("/",303)
    remaining_ctx=lambda: max(0,DAILY-db.question_count(u["id"]))
    if len(question)>MAXQ:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":0,"error":f"Soru en fazla {MAXQ} karakter olabilir."})
    if db.question_count(u["id"])>=DAILY:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":0,"error":"Günlük soru limitinize ulaştınız."})

    vehicle_name=next((v['name'] for v in db.vehicles() if v['id']==vehicle_id),'Bilinmiyor')
    if not API or not MODEL:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":remaining_ctx(),"error":"Sunucu AI yapılandırması tamamlanmamış."})

    store=_vehicle_store(vehicle_id)
    answer,sources=_call_ai(question,vehicle_name,SYSTEM,vector_store_id=store,restrict_to_docs=True)
    if answer is None:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":remaining_ctx(),"error":"AI servisine ulaşılamadı."})
    answer=answer or NOT_FOUND_MSG
    not_found = NOT_FOUND_MSG in answer
    db.save_question(u["id"],vehicle_id,question,answer,json.dumps(sources,ensure_ascii=False))
    return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
        "csrf":csrf_token(request),"remaining":remaining_ctx(),"answer":answer,"sources":sources,
        "question":question,"vehicle_id":vehicle_id,"not_found":not_found})

@app.post("/ask/speculate",response_class=HTMLResponse)
def ask_speculate(request:Request,question:str=Form(...),vehicle_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u: return redirect_login()
    if not csrf_ok(request,csrf):
        return RedirectResponse("/",303)
    vehicle_name=next((v['name'] for v in db.vehicles() if v['id']==vehicle_id),'Bilinmiyor')
    remaining_ctx=lambda: max(0,DAILY-db.question_count(u["id"]))
    if not API or not MODEL:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":remaining_ctx(),"error":"Sunucu AI yapılandırması tamamlanmamış."})
    answer,_=_call_ai(question,vehicle_name,SPECULATE_SYSTEM,restrict_to_docs=False)
    answer=answer or NOT_FOUND_MSG
    return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
        "csrf":csrf_token(request),"remaining":remaining_ctx(),"answer":answer,"sources":[],
        "question":question,"vehicle_id":vehicle_id,"speculative":True})


# ---------- auth ----------

@app.get("/register",response_class=HTMLResponse)
def register_page(request:Request):
    return templates.TemplateResponse("register.html",{"request":request,"error":None,"csrf":csrf_token(request)})

@app.post("/register")
def register(request:Request,username:str=Form(...),password:str=Form(...),csrf:str=Form(None)):
    username=username.strip()
    if not csrf_ok(request,csrf):
        return templates.TemplateResponse("register.html",{"request":request,"csrf":csrf_token(request),
            "error":"Oturum doğrulaması başarısız, sayfayı yenileyip tekrar deneyin."})
    if len(username)<3 or len(password)<8:
        return templates.TemplateResponse("register.html",{"request":request,"csrf":csrf_token(request),
            "error":"Kullanıcı adı en az 3, şifre en az 8 karakter olmalı."})
    if not db.create_user(username,password):
        return templates.TemplateResponse("register.html",{"request":request,"csrf":csrf_token(request),
            "error":"Bu kullanıcı adı zaten kullanılıyor."})
    return RedirectResponse("/login?registered=1",303)

@app.get("/login",response_class=HTMLResponse)
def login_page(request:Request):
    return templates.TemplateResponse("login.html",{"request":request,"csrf":csrf_token(request),
        "registered":request.query_params.get("registered")})

@app.post("/login")
def login(request:Request,username:str=Form(...),password:str=Form(...),csrf:str=Form(None)):
    username=username.strip()
    if not csrf_ok(request,csrf):
        return templates.TemplateResponse("login.html",{"request":request,"csrf":csrf_token(request),
            "error":"Oturum doğrulaması başarısız, sayfayı yenileyip tekrar deneyin."})
    u=db.auth(username,password)
    if not u:
        blocked=db.user_exists_but_blocked(username,password)
        if blocked and not blocked.get("approved"):
            err="Hesabınız henüz bir yönetici tarafından onaylanmadı."
        elif blocked and not blocked.get("active"):
            err="Hesabınız pasifleştirilmiş. Bir yöneticiyle iletişime geçin."
        else:
            err="Kullanıcı adı veya şifre hatalı."
        return templates.TemplateResponse("login.html",{"request":request,"csrf":csrf_token(request),"error":err})
    request.session["token"]=db.new_session(u["id"])
    if u.get("must_change_password"):
        return RedirectResponse("/change-password",303)
    return RedirectResponse("/admin" if u["role"]=="admin" else "/",303)

@app.get("/logout")
def logout(request:Request):
    t=request.session.get("token")
    if t: db.delete_session(t)
    request.session.clear()
    return RedirectResponse("/",303)

@app.get("/change-password",response_class=HTMLResponse)
def change_password_page(request:Request):
    u=current_user(request)
    if not u: return redirect_login()
    return templates.TemplateResponse("change-password.html",{"request":request,"user":u,
        "csrf":csrf_token(request),"forced":bool(u.get("must_change_password")),"error":None})

@app.post("/change-password",response_class=HTMLResponse)
def change_password(request:Request,current_password:str=Form(...),new_password:str=Form(...),
                     new_password2:str=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u: return redirect_login()
    forced=bool(u.get("must_change_password"))
    if not csrf_ok(request,csrf):
        return templates.TemplateResponse("change-password.html",{"request":request,"user":u,
            "csrf":csrf_token(request),"forced":forced,"error":"Oturum doğrulaması başarısız, tekrar deneyin."})
    if not db.auth(u["username"],current_password) and not db.user_exists_but_blocked(u["username"],current_password):
        return templates.TemplateResponse("change-password.html",{"request":request,"user":u,
            "csrf":csrf_token(request),"forced":forced,"error":"Mevcut şifre hatalı."})
    if len(new_password)<8 or new_password!=new_password2:
        return templates.TemplateResponse("change-password.html",{"request":request,"user":u,
            "csrf":csrf_token(request),"forced":forced,"error":"Yeni şifreler eşleşmiyor veya çok kısa (en az 8 karakter)."})
    db.change_password(u["id"],new_password)
    return RedirectResponse("/admin" if u["role"]=="admin" else "/",303)


# ---------- admin ----------

@app.get("/admin",response_class=HTMLResponse)
def admin(request:Request):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    return templates.TemplateResponse("admin.html",{"request":request,"user":u,
        "vehicles":db.vehicles(active_only=False),"documents":db.documents(),
        "questions":db.recent_questions(),"users":db.users(),"audit":db.audit(),
        "csrf":csrf_token(request)})

@app.post("/admin/user")
def admin_user_add(request:Request,username:str=Form(...),password:str=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        username=username.strip()
        if len(username)>=3 and len(password)>=8:
            db.admin_create_user(username,password,u["username"],u["id"])
    return RedirectResponse("/admin",303)

@app.post("/admin/user/approve")
def admin_user_approve(request:Request,user_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        db.approve_user(user_id,u["username"],u["id"])
    return RedirectResponse("/admin",303)

@app.post("/admin/user/role")
def admin_user_role(request:Request,user_id:int=Form(...),role:str=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf) and user_id!=u["id"]:
        db.set_role(user_id,role,u["username"],u["id"])
    return RedirectResponse("/admin",303)

@app.post("/admin/user/active")
def admin_user_active(request:Request,user_id:int=Form(...),active:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf) and user_id!=u["id"]:
        db.set_user_active(user_id,active,u["username"],u["id"])
    return RedirectResponse("/admin",303)

@app.post("/admin/user/reset-password",response_class=HTMLResponse)
def admin_user_reset_password(request:Request,user_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    temp_password=None; temp_password_for=None
    if csrf_ok(request,csrf):
        temp_password,temp_password_for=db.reset_password(user_id,u["username"],u["id"])
    return templates.TemplateResponse("admin.html",{"request":request,"user":u,
        "vehicles":db.vehicles(active_only=False),"documents":db.documents(),
        "questions":db.recent_questions(),"users":db.users(),"audit":db.audit(),
        "csrf":csrf_token(request),"temp_password":temp_password,"temp_password_for":temp_password_for})

@app.post("/admin/vehicle")
def admin_vehicle(request:Request,name:str=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        db.add_vehicle(name.strip(),u["username"],u["id"])
    return RedirectResponse("/admin",303)

@app.post("/admin/vehicle/active")
def admin_vehicle_active(request:Request,vehicle_id:int=Form(...),active:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        db.set_vehicle_active(vehicle_id,active,u["username"],u["id"])
    return RedirectResponse("/admin",303)

@app.post("/admin/document")
async def admin_document(request:Request,vehicle_id:int=Form(...),file:UploadFile=File(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if not csrf_ok(request,csrf) or not file.filename.lower().endswith(".pdf"):
        return RedirectResponse("/admin",303)
    if not API:
        return RedirectResponse("/admin",303)

    content=await file.read()

    try:
        rr=requests.post(
            f"{GEMINI_API_BASE}/fileSearchStores",
            headers={"x-goog-api-key":API,"Content-Type":"application/json"},
            json={"displayName":f"Itfaiye - {file.filename}"},
            timeout=60
        )
        if not rr.ok:
            raise RuntimeError(f"Store oluşturulamadı: {rr.text[:800]}")

        store_name=rr.json().get("name")
        if not store_name:
            raise RuntimeError("Gemini File Search store adı alınamadı.")

        rr=requests.post(
            f"https://generativelanguage.googleapis.com/upload/v1beta/{store_name}:uploadToFileSearchStore",
            headers={
                "x-goog-api-key":API,
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

        db.add_doc(vehicle_id,file.filename,"",store_name,u["username"],u["id"])
    except Exception as exc:
        print("GEMINI PDF YÜKLEME HATASI:",exc)

    return RedirectResponse("/admin",303)

@app.post("/admin/document/active")
def admin_document_active(request:Request,document_id:int=Form(...),active:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        db.set_document_active(document_id,active,u["username"],u["id"])
    return RedirectResponse("/admin",303)


@app.get("/healthz",response_class=PlainTextResponse)
def healthz():
    return "ok"
