import os, json, re, secrets
from pathlib import Path
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db

BASE=Path(__file__).resolve().parents[1]
load_dotenv(BASE/".env")
db.init()
db.seed(os.getenv("ADMIN_USERNAME","admin"), os.getenv("ADMIN_PASSWORD","change-this-immediately"))

app=FastAPI(title="İtfaiye Yapay Zekâ Kılavuzu")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("APP_SECRET_KEY","change-me"), max_age=60*60*24*7)
templates=Jinja2Templates(directory=str(BASE/"app/templates"))

API=os.getenv("OPENAI_API_KEY")
MODEL=os.getenv("OPENAI_MODEL","")
GLOBAL_VS=os.getenv("OPENAI_VECTOR_STORE_ID")
DAILY=int(os.getenv("QUESTIONS_PER_DAY","30"))
MAXQ=int(os.getenv("MAX_QUESTION_CHARS","2000"))

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
    return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
                                                   "remaining": max(0,DAILY-(db.question_count(u["id"]) if u else 0))})

@app.get("/register",response_class=HTMLResponse)
def register_page(request:Request):
    return templates.TemplateResponse("register.html",{"request":request,"error":None})

@app.post("/register")
def register(username:str=Form(...),password:str=Form(...)):
    username=username.strip()
    if len(username)<3 or len(password)<8:
        return templates.TemplateResponse("register.html",{"request":None,"error":"Kullanıcı adı en az 3, şifre en az 8 karakter olmalı."})
    if not db.create_user(username,password):
        return templates.TemplateResponse("register.html",{"request":None,"error":"Bu kullanıcı adı zaten kullanılıyor."})
    return RedirectResponse("/login?registered=1",303)

@app.get("/login",response_class=HTMLResponse)
def login_page(request:Request):
    return templates.TemplateResponse("login.html",{"request":request,"error":request.query_params.get("registered")})

@app.post("/login")
def login(request:Request,username:str=Form(...),password:str=Form(...)):
    u=db.auth(username.strip(),password)
    if not u:
        return templates.TemplateResponse("login.html",{"request":request,"error":"Kullanıcı adı veya şifre hatalı."})
    request.session["token"]=db.new_session(u["id"])
    return RedirectResponse("/admin" if u["role"]=="admin" else "/",303)

@app.get("/logout")
def logout(request:Request):
    t=request.session.get("token")
    if t: db.delete_session(t)
    request.session.clear()
    return RedirectResponse("/",303)

@app.post("/ask",response_class=HTMLResponse)
def ask(request:Request,question:str=Form(...),vehicle_id:int=Form(...)):
    u=current_user(request)
    if not u: return redirect_login()
    question=question.strip()
    if not question: return RedirectResponse("/",303)
    if len(question)>MAXQ:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "remaining":0,"error":f"Soru en fazla {MAXQ} karakter olabilir."})
    if db.question_count(u["id"])>=DAILY:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "remaining":0,"error":"Günlük soru limitinize ulaştınız."})

    # For the pilot, use the global vector store. The production extension can
    # assign separate vector stores per vehicle/document.
    vs=GLOBAL_VS
    if not API or not vs or not MODEL:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "remaining":max(0,DAILY-db.question_count(u["id"])),"error":"Sunucu AI yapılandırması tamamlanmamış."})

    payload={"model":MODEL,"input":[
        {"role":"system","content":SYSTEM},
        {"role":"user","content":f"Seçilen araç: {next((v['name'] for v in db.vehicles() if v['id']==vehicle_id),'Bilinmiyor')}\nSoru: {question}"}
    ],"tools":[{"type":"file_search","vector_store_ids":[vs],"max_num_results":8}]}
    r=requests.post("https://api.openai.com/v1/responses",
        headers={"Authorization":f"Bearer {API}","Content-Type":"application/json"},
        json=payload,timeout=180)
    if not r.ok:
        err="AI servisine ulaşılamadı."
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "remaining":max(0,DAILY-db.question_count(u["id"])),"error":err})
    obj=r.json(); texts=[]; sources=[]
    for item in obj.get("output",[]):
        if item.get("type")!="message": continue
        for c in item.get("content",[]):
            if c.get("type")=="output_text":
                texts.append(c.get("text",""))
                for a in c.get("annotations",[]):
                    if a.get("type")=="file_citation" and a.get("filename") and a["filename"] not in sources:
                        sources.append(a["filename"])
    answer="\n".join(texts).strip() or "Bu konuda yüklenen dokümanlarda yeterli bilgi bulunamadı."
    db.save_question(u["id"],vehicle_id,question,answer,json.dumps(sources,ensure_ascii=False))
    return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
        "remaining":max(0,DAILY-db.question_count(u["id"])),"answer":answer,"sources":sources,"question":question})

@app.get("/admin",response_class=HTMLResponse)
def admin(request:Request):
    u=current_user(request)
    if not u or u["role"]!="admin": return redirect_login()
    return templates.TemplateResponse("admin.html",{"request":request,"user":u,"vehicles":db.vehicles(),
        "documents":db.documents(),"questions":db.recent_questions()})

@app.post("/admin/vehicle")
def admin_vehicle(request:Request,name:str=Form(...)):
    u=current_user(request)
    if not u or u["role"]!="admin": return redirect_login()
    db.add_vehicle(name.strip())
    return RedirectResponse("/admin",303)

@app.post("/admin/document")
async def admin_document(request:Request,vehicle_id:int=Form(...),file:UploadFile=File(...)):
    u=current_user(request)
    if not u or u["role"]!="admin": return redirect_login()
    if not file.filename.lower().endswith(".pdf"):
        return RedirectResponse("/admin",303)
    content=await file.read()
    # Upload directly to OpenAI and attach to a fresh Vector Store for this document.
    if API:
        h={"Authorization":f"Bearer {API}"}
        jh={**h,"Content-Type":"application/json"}
        rr=requests.post("https://api.openai.com/v1/files",headers=h,
                         files={"file":(file.filename,content,"application/pdf")},
                         data={"purpose":"assistants"},timeout=180)
        if rr.ok:
            fid=rr.json()["id"]
            rr=requests.post("https://api.openai.com/v1/vector_stores",headers=jh,
                             json={"name":f"Itfaiye - {file.filename}","file_ids":[fid]},timeout=120)
            if rr.ok:
                vs=rr.json()["id"]
                db.add_doc(vehicle_id,file.filename,fid,vs)
    return RedirectResponse("/admin",303)
