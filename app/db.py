import os
import secrets
from datetime import datetime, timedelta

import bcrypt
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()


def conn():
    database_url = (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("POSTGRES_URL_NON_POOLING")
        or os.getenv("DATABASE_URL_UNPOOLED")
    )

    if database_url:
        return psycopg.connect(database_url, row_factory=dict_row)

    # Neon/Vercel may expose individual PG* variables instead of a URL.
    host = os.getenv("PGHOST")
    database = os.getenv("PGDATABASE")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD")
    port = os.getenv("PGPORT", "5432")

    if host and database and user and password:
        return psycopg.connect(
            host=host,
            port=port,
            dbname=database,
            user=user,
            password=password,
            sslmode=os.getenv("PGSSLMODE", "require"),
            row_factory=dict_row,
        )

    raise RuntimeError(
        "PostgreSQL bağlantısı bulunamadı. "
        "Vercel/Neon ortam değişkenlerinde DATABASE_URL veya "
        "POSTGRES_URL ya da PGHOST/PGDATABASE/PGUSER/PGPASSWORD bulunmalı."
    )


def init():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users(
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'personel',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS vehicles(
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS documents(
                    id BIGSERIAL PRIMARY KEY,
                    vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
                    filename TEXT NOT NULL,
                    file_id TEXT,
                    vector_store_id TEXT,
                    version INTEGER DEFAULT 1,
                    uploaded_by BIGINT REFERENCES users(id),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS questions(
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id),
                    vehicle_id BIGINT REFERENCES vehicles(id),
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    sources TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sessions(
                    token TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    expires_at TIMESTAMPTZ NOT NULL DEFAULT (CURRENT_TIMESTAMP + interval '7 days'),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS activity_log(
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id),
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS user_feedback(
                    id BIGSERIAL PRIMARY KEY,
                    question_id BIGINT REFERENCES questions(id),
                    feedback TEXT,
                    is_correct BOOLEAN,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Add missing columns to existing tables
            try:
                cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ DEFAULT (CURRENT_TIMESTAMP + interval '7 days');")
            except:
                pass
            
            try:
                cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;")
            except:
                pass
            
            try:
                cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS uploaded_by BIGINT REFERENCES users(id);")
            except:
                pass
            
            c.commit()


def hash_password(p):
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(p, h):
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except Exception:
        return False


def seed(admin_user, admin_pass):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE username=%s",
                (admin_user,)
            )
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(%s,%s,%s)",
                    (admin_user, hash_password(admin_pass), "admin")
                )

            cur.execute("SELECT id FROM vehicles LIMIT 1")
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO vehicles(name) VALUES(%s)",
                    ("Rosenbauer L42A-XS",)
                )


def create_user(u, p):
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users(username,password_hash) VALUES(%s,%s)",
                    (u, hash_password(p))
                )
        return True
    except psycopg.errors.UniqueViolation:
        return False


def auth(u, p):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE username=%s AND active=1",
                (u,)
            )
            user = cur.fetchone()
            if user and verify_password(p, user["password_hash"]):
                return user
            return None


def new_session(uid):
    t = secrets.token_urlsafe(32)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions(token,user_id) VALUES(%s,%s)",
                (t, uid)
            )
    return t


def user_by_token(t):
    if not t:
        return None
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT u.* FROM users u
                   JOIN sessions s ON s.user_id=u.id
                   WHERE s.token=%s AND u.active=1 AND s.expires_at > CURRENT_TIMESTAMP""",
                (t,)
            )
            return cur.fetchone()


def delete_session(t):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token=%s", (t,))


def vehicles():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM vehicles WHERE active=1 ORDER BY name"
            )
            return cur.fetchall()


def add_vehicle(name):
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO vehicles(name) VALUES(%s)",
                    (name,)
                )
        return True
    except psycopg.errors.UniqueViolation:
        return False


def documents():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT d.*, v.name AS vehicle
                   FROM documents d
                   JOIN vehicles v ON v.id=d.vehicle_id
                   ORDER BY d.created_at DESC"""
            )
            return cur.fetchall()


def add_doc(vehicle_id, filename, file_id, vs_id, uploaded_by=None):
    with conn() as c:
        with c.cursor() as cur:
            # Deactivate old versions of this document for this vehicle
            cur.execute(
                "UPDATE documents SET active=0 WHERE vehicle_id=%s AND filename=%s AND active=1",
                (vehicle_id, filename)
            )
            
            # Get next version number
            cur.execute(
                "SELECT MAX(version) AS max_v FROM documents WHERE vehicle_id=%s AND filename=%s",
                (vehicle_id, filename)
            )
            max_v = cur.fetchone().get("max_v") or 0
            next_version = max_v + 1
            
            cur.execute(
                """INSERT INTO documents(
                       vehicle_id, filename, file_id, vector_store_id, version, uploaded_by
                   ) VALUES(%s,%s,%s,%s,%s,%s)""",
                (vehicle_id, filename, file_id, vs_id, next_version, uploaded_by)
            )
            
            log_activity(uploaded_by, "PDF_UPLOADED", f"Vehicle {vehicle_id}: {filename} v{next_version}")


def log_activity(user_id, action, details=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO activity_log(user_id, action, details) VALUES(%s,%s,%s)",
                (user_id, action, details)
            )


def save_feedback(question_id, is_correct, feedback=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO user_feedback(question_id, is_correct, feedback) VALUES(%s,%s,%s)",
                (question_id, is_correct, feedback)
            )


def get_user_questions(user_id, limit=50):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT q.*, v.name AS vehicle
                   FROM questions q
                   LEFT JOIN vehicles v ON v.id=q.vehicle_id
                   WHERE q.user_id=%s
                   ORDER BY q.created_at DESC
                   LIMIT %s""",
                (user_id, limit)
            )
            return cur.fetchall()


def get_activity_log(limit=100):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT a.*, u.username
                   FROM activity_log a
                   LEFT JOIN users u ON u.id=a.user_id
                   ORDER BY a.created_at DESC
                   LIMIT %s""",
                (limit,)
            )
            return cur.fetchall()


def get_document_versions(vehicle_id):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT d.*, u.username
                   FROM documents d
                   LEFT JOIN users u ON u.id=d.uploaded_by
                   WHERE d.vehicle_id=%s
                   ORDER BY d.filename, d.version DESC""",
                (vehicle_id,)
            )
            return cur.fetchall()


def question_count(uid):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS n
                   FROM questions
                   WHERE user_id=%s
                     AND created_at >= CURRENT_DATE""",
                (uid,)
            )
            return cur.fetchone()["n"]


def save_question(uid, vid, q, a, s):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO questions(
                       user_id, vehicle_id, question, answer, sources
                   ) VALUES(%s,%s,%s,%s,%s)""",
                (uid, vid, q, a, s)
            )


def recent_questions(limit=100):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT q.*, u.username, v.name AS vehicle
                   FROM questions q
                   LEFT JOIN users u ON u.id=q.user_id
                   LEFT JOIN vehicles v ON v.id=q.vehicle_id
                   ORDER BY q.created_at DESC
                   LIMIT %s""",
                (limit,)
            )
            return cur.fetchall()
