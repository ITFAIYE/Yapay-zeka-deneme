import os
import hashlib
import secrets

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()


def conn():
    database_url = (
        os.getenv("POSTGRES_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL_NON_POOLING")
    )

    if not database_url:
        raise RuntimeError(
            "Veritabanı bağlantısı bulunamadı. "
            "POSTGRES_URL veya DATABASE_URL ortam değişkeni gerekli."
        )

    return psycopg.connect(database_url, row_factory=dict_row)


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
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)


def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()


def seed(admin_user, admin_pass):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE username=%s",
                (admin_user,)
            )
            row = cur.fetchone()

            if not row:
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
                """SELECT * FROM users
                   WHERE username=%s AND password_hash=%s AND active=1""",
                (u, hash_password(p))
            )
            return cur.fetchone()


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
                   WHERE s.token=%s AND u.active=1""",
                (t,)
            )
            return cur.fetchone()


def delete_session(t):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "DELETE FROM sessions WHERE token=%s",
                (t,)
            )


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


def add_doc(vehicle_id, filename, file_id, vs_id):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO documents(
                       vehicle_id, filename, file_id, vector_store_id
                   ) VALUES(%s,%s,%s,%s)""",
                (vehicle_id, filename, file_id, vs_id)
            )


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
