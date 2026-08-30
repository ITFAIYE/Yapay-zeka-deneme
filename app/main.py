import os, json, secrets, csv, io
from datetime import date, datetime, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, StreamingResponse, JSONResponse
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

VAPID_PUBLIC_KEY=os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY=os.getenv("VAPID_PRIVATE_KEY")
VAPID_CLAIM_EMAIL=os.getenv("VAPID_CLAIM_EMAIL","mailto:admin@example.com")

SYSTEM="""Sen İtfaiye Yapay Zekâ Kılavuzu asistanısın.
Üç bilgi kaynağın olabilir:
1) file_search ile getirilen yüklenmiş PDF kılavuzları.
2) Sana ayrıca metin olarak verilebilecek "YÖNETİCİ TARAFINDAN DAHA ÖNCE
   CEVAPLANMIŞ SORULAR" listesi (gerçek yöneticiler tarafından, kılavuzu
   okuyarak veya kurum prosedürüne göre cevaplanmıştır).
3) Sana ayrıca metin olarak verilebilecek "PERSONEL / YÖNETİCİ NOTLARI"
   listesi (yöneticilerin saha tecrübesine dayanarak veya araç üzerinde
   kılavuz basıldıktan SONRA yapılmış değişiklikleri anlatmak için serbest
   metin olarak girdiği notlar).
Bu üç kaynak da güvenilir kabul edilir. Notlar (3) kılavuzdan (1) SONRA
eklenmiştir; bir konuda not ile PDF çelişiyorsa (örn. araçta sonradan
değişiklik yapılmış), notu esas al ve PDF'in bu noktada güncel olmayabile-
ceğini kısaca belirt. Yönetici cevaplı sorular (2) için: kullanıcının sorusu
o listedeki bir soruyla FARKLI KELİMELERLE ama ANLAMCA AYNIYSA (eş anlamlı
ifadeler, kısaltma/uzatma, soru/olumlu cümle farkı vb.), o soruya verilmiş
cevabı doğrudan kullan. Emin değilsen (soru gerçekten başka bir konudaysa)
zorla eşleştirme yapma.
KAYNAK bölümünde bilgiyi nereden aldığını belirt: "Kılavuz", "Yönetici
tarafından daha önce cevaplanmış soru" veya "Personel notu".
Hiçbir kaynakta desteklenen bir bilgi yoksa bilgi uydurma ve açıkça
'Bu konuda yüklenen dokümanlarda yeterli bilgi bulunamadı.' de. Özellikle
güvenlik değerleri (eğim, basınç, yük, mesafe vb.) için: kılavuz farklı bir
birimde veriyorsa (örn. derece) ve soru başka bir birimde soruluyorsa (örn.
yüzde), birimler arası kendi kafanda kesin bir dönüşüm yapıp "evet/hayır"
gibi kesin cevap verme; kılavuzdaki değeri olduğu gibi belirt ve dönüşümün
senin hesabın olduğunu, doğrulanmadığını açıkça söyle.
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

def _daily_limit_for(u):
    """Kullanıcıya özel günlük limit varsa onu, yoksa genel DAILY'yi kullanır,
    ayrıca admin bugüne özel verdiyse ekstra hakkı üstüne ekler."""
    limit = u.get("daily_limit")
    limit = limit if limit is not None else DAILY
    if u.get("bonus_date") and str(u.get("bonus_date")) == str(date.today()):
        limit += u.get("bonus_questions") or 0
    return limit

def _remaining_for(u):
    return max(0, _daily_limit_for(u) - db.question_count(u["id"]))

def _vehicle_stores(vehicle_id):
    """Bir araca ait TÜM aktif Gemini File Search store'larını döner (yalnızca
    en yenisini değil) — bir araca birden fazla PDF (örn. ayrı ayrı 'araç' ve
    'üstyapı' kılavuzları) yüklenip hepsi aktif bırakılmışsa, hepsi birlikte
    aranır. Hiç aktif belge yoksa genel (global) store'a düşer."""
    ids=[d["vector_store_id"] for d in db.documents()
         if d.get("vehicle_id")==vehicle_id and d.get("active",1) and d.get("vector_store_id")]
    if ids:
        return ids
    return [GLOBAL_VS] if GLOBAL_VS else []

def _delete_gemini_store(store_name):
    """Bir belge kalıcı silindiğinde Gemini tarafındaki File Search store'unu da siler."""
    try:
        requests.delete(
            f"{GEMINI_API_BASE}/{store_name}",
            headers={"x-goog-api-key":API},
            params={"force":"true"},
            timeout=60)
    except requests.RequestException as exc:
        print("GEMINI STORE SİLME HATASI:",exc)

def _send_push(user_id,title,body):
    """Kullanıcının kayıtlı tüm tarayıcı push aboneliklerine bildirim gönderir.
    VAPID anahtarları tanımlı değilse veya gönderim başarısız olursa uygulamayı
    durdurmadan sessizce loglar. Süresi dolmuş/geçersiz abonelikler silinir."""
    if not VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("pywebpush kurulu değil, push bildirimi atlanıyor.")
        return
    payload=json.dumps({"title":title,"body":body,"url":"/gecmisim"},ensure_ascii=False)
    for sub in db.push_subscriptions_for_user(user_id):
        try:
            webpush(
                subscription_info={
                    "endpoint":sub["endpoint"],
                    "keys":{"p256dh":sub["p256dh"],"auth":sub["auth"]}
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub":VAPID_CLAIM_EMAIL},
                timeout=10,
            )
        except WebPushException as exc:
            status=getattr(exc.response,"status_code",None)
            if status in (404,410):
                db.delete_push_subscription(sub["endpoint"])
            else:
                print("PUSH GÖNDERİM HATASI:",exc)
        except Exception as exc:
            print("PUSH GÖNDERİM HATASI:",exc)


# ---------- home / ask ----------

@app.get("/",response_class=HTMLResponse)
def home(request:Request):
    u=current_user(request)
    if u and u.get("must_change_password"):
        return RedirectResponse("/change-password",303)
    notifications=[]
    if u:
        rows=db.unnotified_answers(u["id"])
        for r in rows:
            r=dict(r)
            notifications.append(r)
        if rows:
            db.mark_notified([r["id"] for r in rows])
    return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
        "csrf":csrf_token(request),
        "remaining": _remaining_for(u) if u else 0,
        "notifications":notifications})

def _fix_symbols(text):
    """Gemini bazen matematik/para sembollerini literal Unicode yerine HTML
    entity biçiminde ('&times;' gibi) yazabiliyor; bunları gerçek karaktere
    çevirir. HTML olarak render edilmediği için aksi halde ekranda olduğu
    gibi ('&times;') görünür kalırlardı."""
    if not text:
        return text
    replacements={
        "&times;":"×","&divide;":"÷","&radic;":"√","&pi;":"π","&euro;":"€",
        "&pound;":"£","&plusmn;":"±","&deg;":"°","&le;":"≤","&ge;":"≥",
        "&ne;":"≠","&asymp;":"≈","&sup2;":"²","&sup3;":"³","&frac12;":"½",
        "&micro;":"µ","&amp;":"&",
    }
    for k,v in replacements.items():
        text=text.replace(k,v)
    return text

def _admin_qa_context_text(vehicle_id):
    """Bu araç için yöneticinin daha önce cevapladığı soruları, AI'ya prompt
    içinde ek bir 'kaynak' olarak verilecek metin haline getirir. Boşsa None
    döner (prompt'u gereksiz büyütmemek için)."""
    rows=db.admin_answered_for_vehicle(vehicle_id)
    if not rows:
        return None
    blocks=[]
    for r in rows:
        q=(r["question"] or "").strip()
        a=(r["admin_answer"] or "").strip()
        if not q or not a:
            continue
        if len(a)>800:
            a=a[:800]+"…"
        blocks.append(f"S: {q}\nC: {a}")
    if not blocks:
        return None
    return ("YÖNETİCİ TARAFINDAN DAHA ÖNCE CEVAPLANMIŞ SORULAR "
            "(bu araç için, en yeniden eskiye):\n\n" + "\n---\n".join(blocks))

def _vehicle_notes_context_text(vehicle_id):
    """Yöneticinin bu araç için girdiği serbest metin notlarını (kılavuzda
    olmayan bilgiler, sonradan yapılan değişiklikler), AI'ya prompt içinde
    ek bir 'kaynak' olarak verilecek metin haline getirir. Boşsa None döner."""
    rows=db.vehicle_notes(vehicle_id)
    if not rows:
        return None
    blocks=[]
    for r in rows:
        n=(r["note"] or "").strip()
        if not n:
            continue
        if len(n)>800:
            n=n[:800]+"…"
        blocks.append(f"- {n}")
    if not blocks:
        return None
    return ("PERSONEL / YÖNETİCİ NOTLARI (bu araç için, kılavuzdan sonra "
            "eklenmiş, PDF ile çelişirse bu notlar güncel kabul edilir):\n\n"
            + "\n".join(blocks))

def _extra_context_text(vehicle_id):
    """Yönetici cevaplı sorular + personel notlarını tek bir bağlam
    metninde birleştirir. Hiçbiri yoksa None döner (prompt'u gereksiz
    büyütmemek için)."""
    parts=[t for t in (_admin_qa_context_text(vehicle_id),_vehicle_notes_context_text(vehicle_id)) if t]
    return "\n\n".join(parts) if parts else None

def _format_fallback_answer(rows):
    """Gemini API'ye ulaşılamadığında (yapılandırma eksik veya ağ/servis
    hatası) kullanılan yedek yanıt: CBS geçmişi + personel notları üzerinde
    anahtar kelime bazlı arama sonuçlarını, KESİN CEVAP OLMADIĞINI açıkça
    belirterek listeler."""
    lines=["⚠️ Yapay zekâ servisine şu an ulaşılamıyor. Aşağıda sorunuzla anahtar "
           "kelime bazında EN YAKIN eşleşen kayıtlı bilgiler listeleniyor — bunlar "
           "doğrulanmış/kesin bir cevap DEĞİLDİR, sadece ilgili olabilir. Emin "
           "olmak için bir yöneticiye danışın."]
    for r in rows:
        if r["kind"]=="cbs":
            lines.append(f"\n• (Daha önce sorulmuş) \"{r['baslik']}\"\n  Cevap: {r['icerik']}")
        else:
            lines.append(f"\n• (Personel notu) {r['icerik']}")
    return "\n".join(lines)

def _call_ai(question,vehicle_name,system,vector_store_ids=None,restrict_to_docs=True,extra_context=None):
    prompt=f"{system}\n\nSeçilen araç: {vehicle_name}\nSoru: {question}"
    if extra_context:
        prompt=f"{system}\n\n{extra_context}\n\nSeçilen araç: {vehicle_name}\nSoru: {question}"
    parts=[{"text":prompt}]
    payload={"contents":[{"role":"user","parts":parts}]}
    if restrict_to_docs and vector_store_ids:
        # Gemini File Search bir çağrıda birden fazla store'u aynı anda arayabilir;
        # bir araca birden fazla aktif PDF yüklenmişse (örn. "araç" + "üstyapı"
        # kılavuzları ayrı dosyalarsa) hepsi tek seferde taranır, sadece en
        # sonuncusu değil.
        payload["tools"]=[{"file_search":{"file_search_store_names":vector_store_ids}}]
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
    # Gemini yanıtı UTF-8'dir; requests bazen Content-Type başlığında charset
    # belirtilmediği için encoding'i YANLIŞ TAHMİN edip €,£,√,π,×,÷ gibi özel
    # karakterleri bozabiliyor. r.json() yerine ham baytları doğrudan UTF-8
    # olarak çözüyoruz — bu, requests'in tahmin mekanizmasını tamamen devre dışı
    # bırakıp karakterlerin bozulmadan gelmesini garanti eder.
    try:
        obj=json.loads(r.content.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:
        print("GEMINI YANIT ÇÖZÜMLEME HATASI:",exc)
        return None,[]
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
    return _fix_symbols("\n".join(texts).strip()),sources

@app.post("/ask",response_class=HTMLResponse)
def ask(request:Request,question:str=Form(...),vehicle_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u: return redirect_login()
    if u.get("must_change_password"): return RedirectResponse("/change-password",303)
    if not csrf_ok(request,csrf):
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":_remaining_for(u),
            "error":"Oturum doğrulaması başarısız, sayfayı yenileyip tekrar deneyin."})
    question=question.strip()
    if not question: return RedirectResponse("/",303)
    remaining_ctx=lambda: _remaining_for(u)
    if len(question)>MAXQ:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":remaining_ctx(),"error":f"Soru en fazla {MAXQ} karakter olabilir."})
    if db.question_count(u["id"])>=_daily_limit_for(u):
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":0,"error":"Günlük soru limitinize ulaştınız."})

    vehicle_name=next((v['name'] for v in db.vehicles() if v['id']==vehicle_id),'Bilinmiyor')

    # Bu araca aynı soru daha önce BİREBİR AYNI ŞEKİLDE sorulup admin
    # tarafından cevaplanmış mı? Öyleyse Gemini'ye hiç sormadan (API
    # yapılandırılmamış olsa bile) hafızadaki admin cevabını doğrudan kullan.
    # Bu bir hızlı yoldur; ifadesi farklı ama anlamca aynı sorular aşağıda
    # AI'ya bağlam olarak verilen admin_qa_context ile çözülür.
    remembered=db.find_admin_answer(vehicle_id,question)
    if remembered:
        answer=("Bu konuda yüklenen dokümanlarda doğrudan bilgi bulunamadı, ancak bu soru "
                "daha önce sorulmuş ve yönetici tarafından şu şekilde yanıtlanmıştı:\n\n"
                +remembered["admin_answer"])
        sources=[]
        qid=db.save_question(u["id"],vehicle_id,question,answer,json.dumps(sources,ensure_ascii=False),
                          username=u["username"],vehicle_name=vehicle_name,unresolved=False)
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":remaining_ctx(),"answer":answer,"sources":sources,
            "question":question,"vehicle_id":vehicle_id,"not_found":False,"from_admin_memory":True,
            "question_id":qid})

    if not API or not MODEL:
        fb=db.local_fallback_search(vehicle_id,question)
        if fb:
            answer=_format_fallback_answer(fb)
            qid=db.save_question(u["id"],vehicle_id,question,answer,json.dumps([],ensure_ascii=False),
                              username=u["username"],vehicle_name=vehicle_name,unresolved=True)
            return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
                "csrf":csrf_token(request),"remaining":remaining_ctx(),"answer":answer,"sources":[],
                "question":question,"vehicle_id":vehicle_id,"not_found":False,"is_fallback":True,
                "question_id":qid})
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":remaining_ctx(),"error":"Sunucu AI yapılandırması tamamlanmamış."})

    store=_vehicle_stores(vehicle_id)
    extra_context=_extra_context_text(vehicle_id)
    answer,sources=_call_ai(question,vehicle_name,SYSTEM,vector_store_ids=store,restrict_to_docs=True,
                             extra_context=extra_context)
    if answer is None:
        fb=db.local_fallback_search(vehicle_id,question)
        if fb:
            answer=_format_fallback_answer(fb)
            qid=db.save_question(u["id"],vehicle_id,question,answer,json.dumps([],ensure_ascii=False),
                              username=u["username"],vehicle_name=vehicle_name,unresolved=True)
            return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
                "csrf":csrf_token(request),"remaining":remaining_ctx(),"answer":answer,"sources":[],
                "question":question,"vehicle_id":vehicle_id,"not_found":False,"is_fallback":True,
                "question_id":qid})
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":remaining_ctx(),"error":"AI servisine ulaşılamadı."})
    answer=answer or NOT_FOUND_MSG
    not_found = NOT_FOUND_MSG in answer
    qid=db.save_question(u["id"],vehicle_id,question,answer,json.dumps(sources,ensure_ascii=False),
                      username=u["username"],vehicle_name=vehicle_name,unresolved=not_found)
    return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
        "csrf":csrf_token(request),"remaining":remaining_ctx(),"answer":answer,"sources":sources,
        "question":question,"vehicle_id":vehicle_id,"not_found":not_found,"question_id":qid})

@app.post("/ask/speculate",response_class=HTMLResponse)
def ask_speculate(request:Request,question:str=Form(...),vehicle_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u: return redirect_login()
    if not csrf_ok(request,csrf):
        return RedirectResponse("/",303)
    vehicle_name=next((v['name'] for v in db.vehicles() if v['id']==vehicle_id),'Bilinmiyor')
    remaining_ctx=lambda: _remaining_for(u)
    if not API or not MODEL:
        return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
            "csrf":csrf_token(request),"remaining":remaining_ctx(),"error":"Sunucu AI yapılandırması tamamlanmamış."})
    answer,_=_call_ai(question,vehicle_name,SPECULATE_SYSTEM,restrict_to_docs=False)
    answer=answer or NOT_FOUND_MSG
    return templates.TemplateResponse("home.html",{"request":request,"user":u,"vehicles":db.vehicles(),
        "csrf":csrf_token(request),"remaining":remaining_ctx(),"answer":answer,"sources":[],
        "question":question,"vehicle_id":vehicle_id,"speculative":True})


@app.get("/push/public-key",response_class=PlainTextResponse)
def push_public_key():
    return VAPID_PUBLIC_KEY or ""

@app.post("/push/subscribe")
async def push_subscribe(request:Request):
    u=current_user(request)
    if not u: return JSONResponse({"ok":False},status_code=401)
    try:
        body=await request.json()
        endpoint=body["endpoint"]
        keys=body["keys"]
        db.save_push_subscription(u["id"],endpoint,keys["p256dh"],keys["auth"])
        return JSONResponse({"ok":True})
    except (KeyError,ValueError,json.JSONDecodeError):
        return JSONResponse({"ok":False},status_code=400)

@app.post("/push/unsubscribe")
async def push_unsubscribe(request:Request):
    u=current_user(request)
    if not u: return JSONResponse({"ok":False},status_code=401)
    try:
        body=await request.json()
        db.delete_push_subscription(body["endpoint"])
        return JSONResponse({"ok":True})
    except (KeyError,ValueError,json.JSONDecodeError):
        return JSONResponse({"ok":False},status_code=400)


@app.post("/feedback")
def feedback(request:Request,question_id:int=Form(...),value:str=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u: return JSONResponse({"ok":False},status_code=401)
    if not csrf_ok(request,csrf):
        return JSONResponse({"ok":False},status_code=403)
    ok=db.set_feedback(question_id,u["id"],value)
    return JSONResponse({"ok":ok})


# ---------- geçmişim ----------

@app.get("/gecmisim",response_class=HTMLResponse)
def gecmisim(request:Request):
    u=current_user(request)
    if not u: return redirect_login()
    qs=[]
    for q in db.user_questions(u["id"]):
        q=dict(q)
        try:
            q["sources"]=json.loads(q["sources"]) if q["sources"] else []
        except (TypeError,ValueError):
            q["sources"]=[]
        qs.append(q)
    return templates.TemplateResponse("history.html",{"request":request,"user":u,"questions":qs})


# ---------- auth ----------

@app.get("/register",response_class=HTMLResponse)
def register_page(request:Request):
    return templates.TemplateResponse("register.html",{"request":request,"error":None,"csrf":csrf_token(request)})

@app.post("/register")
def register(request:Request,username:str=Form(...),email:str=Form(...),password:str=Form(...),csrf:str=Form(None)):
    username=username.strip()
    email=email.strip()
    if not csrf_ok(request,csrf):
        return templates.TemplateResponse("register.html",{"request":request,"csrf":csrf_token(request),
            "error":"Oturum doğrulaması başarısız, sayfayı yenileyip tekrar deneyin."})
    if len(username)<3 or len(password)<8:
        return templates.TemplateResponse("register.html",{"request":request,"csrf":csrf_token(request),
            "error":"Kullanıcı adı en az 3, şifre en az 8 karakter olmalı."})
    if "@" not in email or "." not in email.split("@")[-1]:
        return templates.TemplateResponse("register.html",{"request":request,"csrf":csrf_token(request),
            "error":"Geçerli bir e-posta adresi girin."})
    if not db.create_user(username,password,email):
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
    lock=db.login_lock_status(username)
    if lock:
        kalan_dk=max(1,int((lock-datetime.now(timezone.utc)).total_seconds()//60)+1)
        err=(f"Çok fazla başarısız giriş denemesi nedeniyle bu hesap geçici olarak "
             f"kilitlendi. Yaklaşık {kalan_dk} dakika sonra tekrar deneyin.")
        return templates.TemplateResponse("login.html",{"request":request,"csrf":csrf_token(request),"error":err})
    u=db.auth(username,password)
    if not u:
        db.register_failed_login(username)
        lock=db.login_lock_status(username)
        if lock:
            kalan_dk=max(1,int((lock-datetime.now(timezone.utc)).total_seconds()//60)+1)
            err=(f"Çok fazla başarısız giriş denemesi nedeniyle bu hesap geçici olarak "
                 f"kilitlendi. Yaklaşık {kalan_dk} dakika sonra tekrar deneyin.")
            return templates.TemplateResponse("login.html",{"request":request,"csrf":csrf_token(request),"error":err})
        blocked=db.user_exists_but_blocked(username,password)
        if blocked and not blocked.get("approved"):
            err="Hesabınız henüz bir yönetici tarafından onaylanmadı."
        elif blocked and not blocked.get("active"):
            err="Hesabınız pasifleştirilmiş. Bir yöneticiyle iletişime geçin."
        else:
            err="Kullanıcı adı veya şifre hatalı."
        return templates.TemplateResponse("login.html",{"request":request,"csrf":csrf_token(request),"error":err})
    db.clear_failed_logins(username)
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

PAGE_SIZE=20

def _parsed_questions(rows):
    out=[]
    for q in rows:
        q=dict(q)
        try:
            q["sources"]=json.loads(q["sources"]) if q["sources"] else []
        except (TypeError,ValueError):
            q["sources"]=[]
        out.append(q)
    return out

def _admin_ctx(request,u,qpage=1,apage=1):
    qpage=max(1,qpage); apage=max(1,apage)
    q_total=db.count_questions(); a_total=db.count_audit()
    return {"request":request,"user":u,
        "vehicles":db.vehicles(active_only=False),"documents":db.documents(),
        "questions":_parsed_questions(db.recent_questions(limit=PAGE_SIZE,offset=(qpage-1)*PAGE_SIZE)),
        "users":db.users(),
        "audit":db.audit(limit=PAGE_SIZE,offset=(apage-1)*PAGE_SIZE),
        "cbs":db.unanswered_questions(),"cbs_answered":db.recent_cbs_answers(),
        "notes":db.vehicle_notes(),
        "csrf":csrf_token(request),"questions_per_day":DAILY,"today_str":str(date.today()),
        "now":datetime.now(timezone.utc),"stats":db.stats(),
        "qpage":qpage,"qpages":max(1,-(-q_total//PAGE_SIZE)),
        "apage":apage,"apages":max(1,-(-a_total//PAGE_SIZE))}

@app.get("/admin",response_class=HTMLResponse)
def admin(request:Request,qpage:int=1,apage:int=1):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    return templates.TemplateResponse("admin.html",_admin_ctx(request,u,qpage,apage))

@app.post("/admin/cbs/answer")
def admin_cbs_answer(request:Request,question_id:int=Form(...),admin_answer:str=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    admin_answer=admin_answer.strip()
    if csrf_ok(request,csrf) and admin_answer:
        q=db.get_question(question_id)
        db.answer_cbs(question_id,admin_answer,u["username"],u["id"])
        if q and q.get("user_id"):
            _send_push(q["user_id"],"CBS Sorunuz Cevaplandı",
                       "Yapay zekânın cevaplayamadığı sorunuz yönetici tarafından yanıtlandı.")
    return RedirectResponse("/admin#cbs",303)

@app.post("/admin/cbs/delete")
def admin_cbs_delete(request:Request,question_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        db.delete_question(question_id,u["username"],u["id"])
    return RedirectResponse("/admin#cbs",303)

@app.get("/admin/export/questions.csv")
def export_questions_csv(request:Request):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    buf=io.StringIO()
    w=csv.writer(buf)
    w.writerow(["id","kullanici","arac","soru","cevap","yonetici_cevabi","tarih"])
    for r in db.recent_questions(limit=1000000):
        w.writerow([r["id"],r["username"],r["vehicle"],r["question"],r["answer"],
                    r["admin_answer"] or "",r["created_at"].strftime("%d.%m.%Y %H:%M")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]),media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=sorular.csv"})

@app.get("/admin/export/audit.csv")
def export_audit_csv(request:Request):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    buf=io.StringIO()
    w=csv.writer(buf)
    w.writerow(["kullanici","islem","detay","tarih"])
    for r in db.audit(limit=1000000):
        w.writerow([r["username"] or "sistem",r["action"],r["detail"] or "",r["created_at"].strftime("%d.%m.%Y %H:%M")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]),media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=islem-kayitlari.csv"})

@app.post("/admin/user")
def admin_user_add(request:Request,username:str=Form(...),password:str=Form(...),email:str=Form(None),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        username=username.strip()
        email=(email or "").strip() or None
        if len(username)>=3 and len(password)>=8:
            db.admin_create_user(username,password,u["username"],u["id"],email)
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

@app.post("/admin/user/delete")
def admin_user_delete(request:Request,user_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf) and user_id!=u["id"]:
        db.delete_user(user_id,u["username"],u["id"])
    return RedirectResponse("/admin",303)

@app.post("/admin/user/daily-limit")
def admin_user_daily_limit(request:Request,user_id:int=Form(...),daily_limit:str=Form(""),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        daily_limit=daily_limit.strip()
        limit=int(daily_limit) if daily_limit.isdigit() else None
        db.set_daily_limit(user_id,limit,u["username"],u["id"])
    return RedirectResponse("/admin",303)

@app.post("/admin/user/bonus")
def admin_user_bonus(request:Request,user_id:int=Form(...),amount:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf) and amount>=0:
        db.grant_bonus(user_id,amount,u["username"],u["id"])
    return RedirectResponse("/admin",303)

@app.post("/admin/user/reset-password",response_class=HTMLResponse)
def admin_user_reset_password(request:Request,user_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    temp_password=None; temp_password_for=None
    if csrf_ok(request,csrf):
        temp_password,temp_password_for=db.reset_password(user_id,u["username"],u["id"])
    ctx=_admin_ctx(request,u)
    ctx["temp_password"]=temp_password; ctx["temp_password_for"]=temp_password_for
    return templates.TemplateResponse("admin.html",ctx)

@app.post("/admin/note")
def admin_note_add(request:Request,vehicle_id:int=Form(...),note:str=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        note=note.strip()
        if note:
            db.add_vehicle_note(vehicle_id,note,u["username"],u["id"])
    return RedirectResponse("/admin#notlar",303)

@app.post("/admin/note/edit")
def admin_note_edit(request:Request,note_id:int=Form(...),note:str=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        note=note.strip()
        if note:
            db.update_vehicle_note(note_id,note,u["username"],u["id"])
    return RedirectResponse("/admin#notlar",303)

@app.post("/admin/note/delete")
def admin_note_delete(request:Request,note_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        db.delete_vehicle_note(note_id,u["username"],u["id"])
    return RedirectResponse("/admin#notlar",303)

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

@app.post("/admin/vehicle/delete")
def admin_vehicle_delete(request:Request,vehicle_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        # Bu araca bağlı belgelerin Gemini File Search store'larını da temizle.
        for d in db.documents():
            if d.get("vehicle_id")==vehicle_id and d.get("vector_store_id") and API:
                _delete_gemini_store(d["vector_store_id"])
        db.delete_vehicle(vehicle_id,u["username"],u["id"])
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


@app.post("/admin/document/delete")
def admin_document_delete(request:Request,document_id:int=Form(...),csrf:str=Form(None)):
    u=current_user(request)
    if not u or not is_admin(u): return redirect_login()
    if csrf_ok(request,csrf):
        doc=db.get_document(document_id)
        if doc and doc.get("vector_store_id") and API:
            _delete_gemini_store(doc["vector_store_id"])
        db.delete_document(document_id,u["username"],u["id"])
    return RedirectResponse("/admin",303)


@app.get("/healthz",response_class=PlainTextResponse)
def healthz():
    return "ok"
