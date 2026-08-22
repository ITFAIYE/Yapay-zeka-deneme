import os
import json
import re
import secrets
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db


# ---------------------------------------------------------
# AYARLAR
# ---------------------------------------------------------

BASE = Path(__file__).resolve().parents[1]
load_dotenv(BASE / ".env")

db.init()
db.seed(
    os.getenv("ADMIN_USERNAME", "admin"),
    os.getenv("ADMIN_PASSWORD", "change-this-immediately")
)

app = FastAPI(title="İtfaiye Yapay Zekâ Kılavuzu")

app.mount(
    "/static",
    StaticFiles(directory=str(BASE / "app/static")),
    name="static"
)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("APP_SECRET_KEY", "change-me"),
    max_age=60 * 60 * 24 * 7
)

templates = Jinja2Templates(
    directory=str(BASE / "app/templates")
)


# ---------------------------------------------------------
# GEMINI
# ---------------------------------------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Ücretsiz katmanı bulunan model
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash-lite"
).strip()

# İsteğe bağlı global Gemini File Search Store
GLOBAL_GEMINI_STORE = os.getenv(
    "GEMINI_FILE_SEARCH_STORE_ID",
    ""
).strip()

GEMINI_API_BASE = (
    "https://generativelanguage.googleapis.com/v1beta"
)

GEMINI_UPLOAD_BASE = (
    "https://generativelanguage.googleapis.com/upload/v1beta"
)

DAILY = int(
    os.getenv("QUESTIONS_PER_DAY", "30")
)

MAXQ = int(
    os.getenv("MAX_QUESTION_CHARS", "2000")
)


# ---------------------------------------------------------
# SİSTEM TALİMATI
# ---------------------------------------------------------

SYSTEM = """
Sen İtfaiye Yapay Zekâ Kılavuzu asistanısın.

Yalnızca yüklenmiş itfaiye dokümanlarından
getirilen bilgilerle cevap ver.

Dokümanlarda yeterli bilgi yoksa bilgi uydurma.

Bu durumda açıkça:

"Bu konuda yüklenen dokümanlarda yeterli bilgi bulunamadı."

de.

Türkçe cevap ver.

Araç kullanımı, acil işletim, bakım ve güvenlik
sorularında dokümandaki uyarıları atlama.

Cevap düzeni:

KISA CEVAP

UYGULANACAK İŞLEM

GÜVENLİK / DİKKAT

KAYNAK

Sayfa veya bölüm bilgisi yalnızca getirilen
dokümanda destekleniyorsa yaz.
Uydurma sayfa numarası yazma.
"""


# ---------------------------------------------------------
# YARDIMCI FONKSİYONLAR
# ---------------------------------------------------------

def current_user(request):
    return db.user_by_token(
        request.session.get("token")
    )


def redirect_login():
    return RedirectResponse(
        "/login",
        status_code=303
    )


def home_error(request, user, message):
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "user": user,
            "vehicles": db.vehicles(),
            "remaining": max(
                0,
                DAILY - db.question_count(user["id"])
            ),
            "error": message
        }
    )


def selected_vehicle_name(vehicle_id):
    return next(
        (
            v["name"]
            for v in db.vehicles()
            if v["id"] == vehicle_id
        ),
        "Bilinmiyor"
    )


# ---------------------------------------------------------
# GEMINI FILE SEARCH STORE BUL
# ---------------------------------------------------------

def get_gemini_store_for_vehicle(vehicle_id):
    """
    Seçilen araca ait en son Gemini File Search
    deposunu bulur.

    Eski OpenAI vector store kayıtlarını kullanmaz.
    """

    store = GLOBAL_GEMINI_STORE

    try:
        docs = db.documents()

        vehicle_docs = []

        for document in docs:
            if document.get("vehicle_id") != vehicle_id:
                continue

            store_id = str(
                document.get("vector_store_id") or ""
            ).strip()

            if store_id.startswith("fileSearchStores/"):
                vehicle_docs.append(
                    document
                )

        if vehicle_docs:
            store = vehicle_docs[-1].get(
                "vector_store_id"
            )

    except Exception as exc:
        print(
            "Gemini store aranırken hata:",
            exc
        )

    return store


# ---------------------------------------------------------
# GEMINI SORU SOR
# ---------------------------------------------------------

def ask_gemini(
    question,
    vehicle_name,
    store_name
):
    """
    Gemini generateContent + File Search.
    """

    url = (
        f"{GEMINI_API_BASE}/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    prompt = (
        f"Seçilen itfaiye aracı: "
        f"{vehicle_name}\n\n"
        f"Kullanıcının sorusu:\n"
        f"{question}"
    )

    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": SYSTEM
                }
            ]
        },

        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],

        "tools": [
            {
                "fileSearch": {
                    "fileSearchStoreNames": [
                        store_name
                    ]
                }
            }
        ]
    }

    response = requests.post(
        url,
        params={
            "key": GEMINI_API_KEY
        },
        headers={
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=180
    )

    if not response.ok:

        try:
            data = response.json()

            detail = (
                data
                .get("error", {})
                .get("message")
                or response.text
            )

        except Exception:
            detail = response.text

        raise RuntimeError(
            "Gemini API hata verdi "
            f"({response.status_code}): "
            f"{str(detail)[:800]}"
        )

    data = response.json()

    texts = []
    sources = []

    candidates = data.get(
        "candidates",
        []
    )

    for candidate in candidates:

        content = candidate.get(
            "content",
            {}
        )

        parts = content.get(
            "parts",
            []
        )

        for part in parts:

            if not isinstance(
                part,
                dict
            ):
                continue

            text = part.get(
                "text"
            )

            if text:
                texts.append(text)

        grounding = (
            candidate.get(
                "groundingMetadata"
            )
            or candidate.get(
                "grounding_metadata"
            )
            or {}
        )

        chunks = (
            grounding.get(
                "groundingChunks"
            )
            or grounding.get(
                "grounding_chunks"
            )
            or []
        )

        for chunk in chunks:

            context = (
                chunk.get(
                    "retrievedContext"
                )
                or chunk.get(
                    "retrieved_context"
                )
                or {}
            )

            title = context.get(
                "title"
            )

            if (
                title
                and title not in sources
            ):
                sources.append(
                    title
                )

    answer = "\n".join(
        texts
    ).strip()

    if not answer:
        answer = (
            "Bu konuda yüklenen "
            "dokümanlarda yeterli "
            "bilgi bulunamadı."
        )

    return answer, sources


# ---------------------------------------------------------
# ANA SAYFA
# ---------------------------------------------------------

@app.get(
    "/",
    response_class=HTMLResponse
)
def home(request: Request):

    user = current_user(
        request
    )

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "user": user,
            "vehicles": db.vehicles(),
            "remaining": max(
                0,
                DAILY - (
                    db.question_count(
                        user["id"]
                    )
                    if user
                    else 0
                )
            )
        }
    )


# ---------------------------------------------------------
# KAYIT
# ---------------------------------------------------------

@app.get(
    "/register",
    response_class=HTMLResponse
)
def register_page(
    request: Request
):

    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "error": None
        }
    )


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):

    username = username.strip()

    if (
        len(username) < 3
        or len(password) < 8
    ):

        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error":
                    "Kullanıcı adı en az 3, "
                    "şifre en az 8 karakter "
                    "olmalı."
            }
        )

    if not db.create_user(
        username,
        password
    ):

        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error":
                    "Bu kullanıcı adı "
                    "zaten kullanılıyor."
            }
        )

    return RedirectResponse(
        "/login?registered=1",
        303
    )


# ---------------------------------------------------------
# GİRİŞ
# ---------------------------------------------------------

@app.get(
    "/login",
    response_class=HTMLResponse
)
def login_page(
    request: Request
):

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "registered":
                request.query_params.get(
                    "registered"
                ),
            "error": None
        }
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):

    user = db.auth(
        username.strip(),
        password
    )

    if not user:

        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "registered": None,
                "error":
                    "Kullanıcı adı veya "
                    "şifre hatalı."
            }
        )

    request.session["token"] = (
        db.new_session(
            user["id"]
        )
    )

    if user["role"] == "admin":
        return RedirectResponse(
            "/admin",
            303
        )

    return RedirectResponse(
        "/",
        303
    )


# ---------------------------------------------------------
# ÇIKIŞ
# ---------------------------------------------------------

@app.get("/logout")
def logout(
    request: Request
):

    token = request.session.get(
        "token"
    )

    if token:
        db.delete_session(
            token
        )

    request.session.clear()

    return RedirectResponse(
        "/",
        303
    )


# ---------------------------------------------------------
# SORU SOR
# ---------------------------------------------------------

@app.post(
    "/ask",
    response_class=HTMLResponse
)
def ask(
    request: Request,
    question: str = Form(...),
    vehicle_id: int = Form(...)
):

    user = current_user(
        request
    )

    if not user:
        return redirect_login()

    question = question.strip()

    if not question:
        return RedirectResponse(
            "/",
            303
        )

    if len(question) > MAXQ:

        return home_error(
            request,
            user,
            f"Soru en fazla "
            f"{MAXQ} karakter olabilir."
        )

    if (
        db.question_count(
            user["id"]
        ) >= DAILY
    ):

        return home_error(
            request,
            user,
            "Günlük soru limitinize "
            "ulaştınız."
        )

    # Gemini anahtarı kontrolü
    if not GEMINI_API_KEY:

        return home_error(
            request,
            user,
            "GEMINI_API_KEY "
            "yapılandırılmamış."
        )

    # Seçilen araç
    vehicle_name = (
        selected_vehicle_name(
            vehicle_id
        )
    )

    # Gemini File Search deposu
    store_name = (
        get_gemini_store_for_vehicle(
            vehicle_id
        )
    )

    if not store_name:

        return home_error(
            request,
            user,
            "Bu araç için Gemini "
            "doküman deposu bulunamadı. "
            "Yönetim panelinden PDF'yi "
            "yeniden yükleyin."
        )

    if not str(
        store_name
    ).startswith(
        "fileSearchStores/"
    ):

        return home_error(
            request,
            user,
            "Bu aracın mevcut dokümanı "
            "eski sistemde kayıtlı. "
            "PDF'yi Yönetim panelinden "
            "yeniden yükleyin."
        )

    try:

        answer, sources = ask_gemini(
            question,
            vehicle_name,
            store_name
        )

    except Exception as exc:

        print(
            "GEMINI API HATASI:",
            exc
        )

        return home_error(
            request,
            user,
            str(exc)
        )

    # Soruyu kaydet
    db.save_question(
        user["id"],
        vehicle_id,
        question,
        answer,
        json.dumps(
            sources,
            ensure_ascii=False
        )
    )

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "user": user,
            "vehicles": db.vehicles(),
            "remaining": max(
                0,
                DAILY - db.question_count(
                    user["id"]
                )
            ),
            "answer": answer,
            "sources": sources,
            "question": question
        }
    )


# ---------------------------------------------------------
# YÖNETİM PANELİ
# ---------------------------------------------------------

@app.get(
    "/admin",
    response_class=HTMLResponse
)
def admin(
    request: Request
):

    user = current_user(
        request
    )

    if (
        not user
        or user["role"] != "admin"
    ):
        return redirect_login()

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": user,
            "vehicles": db.vehicles(),
            "documents": db.documents(),
            "questions":
                db.recent_questions()
        }
    )


# ---------------------------------------------------------
# KULLANICI EKLE
# ---------------------------------------------------------

@app.post("/admin/user")
def admin_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):

    user = current_user(
        request
    )

    if (
        not user
        or user["role"] != "admin"
    ):
        return redirect_login()

    username = username.strip()

    if (
        len(username) >= 3
        and len(password) >= 8
    ):

        db.create_user(
            username,
            password
        )

    return RedirectResponse(
        "/admin",
        303
    )


# ---------------------------------------------------------
# ARAÇ EKLE
# ---------------------------------------------------------

@app.post("/admin/vehicle")
def admin_vehicle(
    request: Request,
    name: str = Form(...)
):

    user = current_user(
        request
    )

    if (
        not user
        or user["role"] != "admin"
    ):
        return redirect_login()

    db.add_vehicle(
        name.strip()
    )

    return RedirectResponse(
        "/admin",
        303
    )


# ---------------------------------------------------------
# PDF YÜKLE
# ---------------------------------------------------------

@app.post("/admin/document")
async def admin_document(
    request: Request,
    vehicle_id: int = Form(...),
    file: UploadFile = File(...)
):

    user = current_user(
        request
    )

    if (
        not user
        or user["role"] != "admin"
    ):
        return redirect_login()

    # Sadece PDF
    if not file.filename.lower().endswith(
        ".pdf"
    ):

        return RedirectResponse(
            "/admin",
            303
        )

    if not GEMINI_API_KEY:

        return RedirectResponse(
            "/admin",
            303
        )

    content = await file.read()

    if not content:

        return RedirectResponse(
            "/admin",
            303
        )

    store_name = None

    try:

        # -------------------------------------------------
        # 1. Gemini File Search Store oluştur
        # -------------------------------------------------

        create_url = (
            f"{GEMINI_API_BASE}"
            f"/fileSearchStores"
        )

        create_response = requests.post(
            create_url,
            params={
                "key": GEMINI_API_KEY
            },
            headers={
                "Content-Type":
                    "application/json"
            },
            json={
                "displayName":
                    f"Itfaiye - "
                    f"{file.filename}",
                "embeddingModel":
                    "models/gemini-embedding-2"
            },
            timeout=60
        )

        if not create_response.ok:

            raise RuntimeError(
                "Gemini File Search "
                "deposu oluşturulamadı: "
                + create_response.text[:800]
            )

        store_data = (
            create_response.json()
        )

        store_name = store_data.get(
            "name"
        )

        if not store_name:

            raise RuntimeError(
                "Gemini File Search "
                "deposu adı alınamadı."
            )

        # -------------------------------------------------
        # 2. PDF'yi File Search Store'a yükle
        # -------------------------------------------------

        upload_url = (
            f"{GEMINI_UPLOAD_BASE}/"
            f"{store_name}"
            f":uploadToFileSearchStore"
        )

        start_response = requests.post(
            upload_url,
            params={
                "key": GEMINI_API_KEY
            },
            headers={
                "X-Goog-Upload-Protocol":
                    "resumable",

                "X-Goog-Upload-Command":
                    "start",

                "X-Goog-Upload-Header-Content-Length":
                    str(len(content)),

                "X-Goog-Upload-Header-Content-Type":
                    "application/pdf",

                "Content-Type":
                    "application/json"
            },
            json={
                "displayName":
                    file.filename
            },
            timeout=60
        )

        if not start_response.ok:

            raise RuntimeError(
                "Gemini PDF yükleme "
                "başlatılamadı: "
                + start_response.text[:800]
            )

        resumable_url = (
            start_response.headers.get(
                "X-Goog-Upload-URL"
            )
        )

        if not resumable_url:

            raise RuntimeError(
                "Gemini yükleme URL'si "
    
