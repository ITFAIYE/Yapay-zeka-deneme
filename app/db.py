import os
import hashlib
import secrets

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
                    approved INTEGER NOT NULL DEFAULT 0,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS audit_log(
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id),
                    username TEXT,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Backfill columns for databases created before this update.
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS approved INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password INTEGER NOT NULL DEFAULT 0")
            # Any pre-existing accounts (from before the approval flow existed)
            # stay usable instead of being silently locked out.
            cur.execute("UPDATE users SET approved=1 WHERE approved IS NULL OR approved=0 AND role='admin'")


def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()


def seed(admin_user, admin_pass):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE username=%s",
                (admin_user,)
            )
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO users(username,password_hash,role,active,approved) VALUES(%s,%s,%s,1,1)",
                    (admin_user, hash_password(admin_pass), "admin")
                )

            cur.execute("SELECT id FROM vehicles LIMIT 1")
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO vehicles(name) VALUES(%s)",
                    ("Rosenbauer L42A-XS",)
                )


def record_audit(user_id, username, action, detail=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log(user_id,username,action,detail) VALUES(%s,%s,%s,%s)",
                (user_id, username, action, detail)
            )


def audit(limit=50):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT %s",
                (limit,)
            )
            return cur.fetchall()


# ---------- users / auth ----------

def create_user(u, p):
    """Self-registration: created unapproved, awaiting an admin."""
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users(username,password_hash,approved,active) VALUES(%s,%s,0,1)",
                    (u, hash_password(p))
                )
        record_audit(None, u, "kayıt", "Yeni kayıt, onay bekliyor")
        return True
    except psycopg.errors.UniqueViolation:
        return False


def admin_create_user(u, p, actor_username=None, actor_id=None):
    """Admin-added accounts are approved and active immediately."""
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users(username,password_hash,approved,active) VALUES(%s,%s,1,1)",
                    (u, hash_password(p))
                )
        record_audit(actor_id, actor_username, "kullanıcı eklendi", u)
        return True
    except psycopg.errors.UniqueViolation:
        return False


def auth(u, p):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT * FROM users
                   WHERE username=%s AND password_hash=%s
                     AND active=1 AND approved=1""",
                (u, hash_password(p))
            )
            return cur.fetchone()


def user_exists_but_blocked(u, p):
    """Used to give a clearer login error (wrong password vs pending approval)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE username=%s AND password_hash=%s",
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
                   WHERE s.token=%s AND u.active=1 AND u.approved=1""",
                (t,)
            )
            return cur.fetchone()


def delete_session(t):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token=%s", (t,))


def delete_sessions_for_user(uid):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE user_id=%s", (uid,))


def users():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY created_at DESC")
            return cur.fetchall()


def get_user(uid):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
            return cur.fetchone()


def approve_user(uid, actor_username=None, actor_id=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE users SET approved=1 WHERE id=%s RETURNING username", (uid,))
            row = cur.fetchone()
    if row:
        record_audit(actor_id, actor_username, "kullanıcı onaylandı", row["username"])


def set_role(uid, role, actor_username=None, actor_id=None):
    if role not in ("admin", "personel"):
        return
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE users SET role=%s WHERE id=%s RETURNING username", (role, uid))
            row = cur.fetchone()
    if row:
        record_audit(actor_id, actor_username, "rol değiştirildi", f"{row['username']} → {role}")


def set_user_active(uid, active, actor_username=None, actor_id=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE users SET active=%s WHERE id=%s RETURNING username", (active, uid))
            row = cur.fetchone()
    if not active:
        delete_sessions_for_user(uid)
    if row:
        record_audit(actor_id, actor_username, "kullanıcı pasifleştirildi" if not active else "kullanıcı aktif edildi", row["username"])


def reset_password(uid, actor_username=None, actor_id=None):
    """Generates a one-time temp password, forces change on next login,
    and invalidates the user's existing sessions."""
    temp = secrets.token_urlsafe(9)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """UPDATE users SET password_hash=%s, must_change_password=1
                   WHERE id=%s RETURNING username""",
                (hash_password(temp), uid)
            )
            row = cur.fetchone()
    delete_sessions_for_user(uid)
    if row:
        record_audit(actor_id, actor_username, "şifre sıfırlandı", row["username"])
    return temp, (row["username"] if row else None)


def change_password(uid, new_password):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """UPDATE users SET password_hash=%s, must_change_password=0
                   WHERE id=%s""",
                (hash_password(new_password), uid)
            )


# ---------- vehicles ----------

def vehicles(active_only=True):
    with conn() as c:
        with c.cursor() as cur:
            if active_only:
                cur.execute("SELECT * FROM vehicles WHERE active=1 ORDER BY name")
            else:
                cur.execute("SELECT * FROM vehicles ORDER BY name")
            return cur.fetchall()


def add_vehicle(name, actor_username=None, actor_id=None):
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute("INSERT INTO vehicles(name) VALUES(%s)", (name,))
        record_audit(actor_id, actor_username, "araç eklendi", name)
        return True
    except psycopg.errors.UniqueViolation:
        return False


def set_vehicle_active(vid, active, actor_username=None, actor_id=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE vehicles SET active=%s WHERE id=%s RETURNING name", (active, vid))
            row = cur.fetchone()
    if row:
        record_audit(actor_id, actor_username, "araç pasifleştirildi" if not active else "araç aktif edildi", row["name"])


# ---------- documents ----------

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


def add_doc(vehicle_id, filename, file_id, vs_id, actor_username=None, actor_id=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO documents(
                       vehicle_id, filename, file_id, vector_store_id
                   ) VALUES(%s,%s,%s,%s)""",
                (vehicle_id, filename, file_id, vs_id)
            )
    record_audit(actor_id, actor_username, "belge yüklendi", filename)


def set_document_active(did, active, actor_username=None, actor_id=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE documents SET active=%s WHERE id=%s RETURNING filename", (active, did))
            row = cur.fetchone()
    if row:
        record_audit(actor_id, actor_username, "belge pasifleştirildi" if not active else "belge aktif edildi", row["filename"])


# ---------- questions ----------

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


def recent_questions(limit=50):
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
