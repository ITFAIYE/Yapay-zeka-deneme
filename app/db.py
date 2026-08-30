import os
import hashlib
import secrets
from datetime import date, timedelta, datetime, timezone

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


def _fix_fk_on_delete(cur, target_table, cascade_tables=frozenset()):
    """target_table'a (id) referans veren TÜM foreign key'leri bulur ve
    silme davranışını düzeltir — kod içinde bilmediğimiz, elle eklenmiş
    veya önceki bir sürümden kalma tablolar (ör. activity_log) dahil.

    cascade_tables içindeki kaynak tablolar için ON DELETE CASCADE
    (örn. sessions: kullanıcı silinince oturumları da silinsin),
    diğerleri için ON DELETE SET NULL (geçmiş kaydı korunsun, sadece
    referans NULL'lansın) uygulanır. SET NULL öncesi ilgili sütun
    NOT NULL ise bu kısıtlama da kaldırılır, aksi halde silme yine
    başarısız olur.
    """
    cur.execute(
        """SELECT tc.table_name AS src_table, kcu.column_name AS src_column,
                  tc.constraint_name AS cname
           FROM information_schema.table_constraints tc
           JOIN information_schema.key_column_usage kcu
             ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
           JOIN information_schema.constraint_column_usage ccu
             ON tc.constraint_name = ccu.constraint_name
            AND tc.table_schema = ccu.table_schema
           WHERE tc.constraint_type='FOREIGN KEY'
             AND tc.table_schema='public'
             AND ccu.table_name=%s""",
        (target_table,)
    )
    for row in cur.fetchall():
        src_table, src_column, cname = row["src_table"], row["src_column"], row["cname"]
        mode = "CASCADE" if src_table in cascade_tables else "SET NULL"
        if mode == "SET NULL":
            cur.execute(f'ALTER TABLE "{src_table}" ALTER COLUMN "{src_column}" DROP NOT NULL')
        cur.execute(f'ALTER TABLE "{src_table}" DROP CONSTRAINT "{cname}"')
        cur.execute(
            f'ALTER TABLE "{src_table}" ADD CONSTRAINT "{cname}" '
            f'FOREIGN KEY ("{src_column}") REFERENCES {target_table}(id) ON DELETE {mode}'
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

                CREATE TABLE IF NOT EXISTS push_subscriptions(
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    endpoint TEXT UNIQUE NOT NULL,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS vehicle_notes(
                    id BIGSERIAL PRIMARY KEY,
                    vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
                    note TEXT NOT NULL,
                    created_by TEXT,
                    created_by_id BIGINT REFERENCES users(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ
                );
            """)
            # Backfill columns for databases created before this update.
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS approved INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password INTEGER NOT NULL DEFAULT 0")
            # Any pre-existing accounts (from before the approval flow existed)
            # stay usable instead of being silently locked out.
            cur.execute("UPDATE users SET approved=1 WHERE approved IS NULL OR approved=0 AND role='admin'")

            # ---- e-posta + kişiye özel günlük soru hakkı ----
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_limit INTEGER")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_questions INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_date DATE")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_count INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ")

            # ---- silinen kullanıcı/araç sonrası geçmişin okunabilir kalması için anlık görüntü ----
            cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS username_snapshot TEXT")
            cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS vehicle_name_snapshot TEXT")

            # ---- CBS: PDF'de cevabı bulunamayan sorular admin tarafından
            #      cevaplanabilsin, personel bildirim alsın, aynı araca aynı
            #      soru tekrar sorulunca AI bu cevabı hafızadan kullansın ----
            cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS unresolved INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS admin_answer TEXT")
            cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS admin_answered_by TEXT")
            cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS admin_answered_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS notified INTEGER NOT NULL DEFAULT 1")

            # ---- geri bildirim (👍/👎) ----
            cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS feedback TEXT")
            cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS feedback_at TIMESTAMPTZ")

            # ---- kalıcı silme desteği: silinen kayda hâlâ referans veren
            #      HER tabloyu (documents/questions/sessions/audit_log dahil,
            #      ileride elle eklenmiş / önceki sürümden kalma tablolar dahil)
            #      otomatik bulup CASCADE/SET NULL kuralına çevirir. ----
            _fix_fk_on_delete(cur, "vehicles", cascade_tables={"documents","vehicle_notes"})
            _fix_fk_on_delete(cur, "users", cascade_tables={"sessions","push_subscriptions"})


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


def audit(limit=50, offset=0):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (limit, offset)
            )
            return cur.fetchall()


def count_audit():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM audit_log")
            return cur.fetchone()["n"]


# ---------- users / auth ----------

def create_user(u, p, email=None):
    """Self-registration: created unapproved, awaiting an admin."""
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users(username,password_hash,email,approved,active) VALUES(%s,%s,%s,0,1)",
                    (u, hash_password(p), email)
                )
        record_audit(None, u, "kayıt", f"Yeni kayıt, onay bekliyor ({email})" if email else "Yeni kayıt, onay bekliyor")
        return True
    except psycopg.errors.UniqueViolation:
        return False


def admin_create_user(u, p, actor_username=None, actor_id=None, email=None):
    """Admin-added accounts are approved and active immediately."""
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users(username,password_hash,email,approved,active) VALUES(%s,%s,%s,1,1)",
                    (u, hash_password(p), email)
                )
        record_audit(actor_id, actor_username, "kullanıcı eklendi", u)
        return True
    except psycopg.errors.UniqueViolation:
        return False


def delete_user(uid, actor_username=None, actor_id=None):
    """Kalıcı olarak siler. Geçmiş sorular korunur (user_id NULL olur,
    kullanıcı adı questions.username_snapshot içinde okunabilir kalır)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("DELETE FROM users WHERE id=%s", (uid,))
    record_audit(actor_id, actor_username, "kullanıcı silindi", row["username"])
    return True


LOGIN_LOCK_THRESHOLD = 5      # bu kadar başarısız denemeden sonra kilitlenir
LOGIN_LOCK_MINUTES = 15       # kilit süresi (dakika)

def login_lock_status(u):
    """Hesap şu an kilitli mi? Kilitliyse locked_until zamanını (UTC) döner,
    değilse None. Kilit süresi dolmuşsa otomatik açar (sayaç sıfırlanır)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT locked_until FROM users WHERE username=%s", (u,))
            row = cur.fetchone()
            if not row or not row["locked_until"]:
                return None
            if row["locked_until"] > datetime.now(timezone.utc):
                return row["locked_until"]
            cur.execute(
                "UPDATE users SET failed_login_count=0, locked_until=NULL WHERE username=%s",
                (u,)
            )
            return None

def register_failed_login(u):
    """Başarısız giriş denemesini sayar; eşiğe ulaşınca hesabı geçici
    kilitler (brute-force / şifre tahmin saldırılarına karşı)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """UPDATE users SET failed_login_count = failed_login_count + 1
                   WHERE username=%s RETURNING failed_login_count""",
                (u,)
            )
            row = cur.fetchone()
            if row and row["failed_login_count"] >= LOGIN_LOCK_THRESHOLD:
                cur.execute(
                    """UPDATE users
                       SET locked_until = CURRENT_TIMESTAMP + (%s * INTERVAL '1 minute')
                       WHERE username=%s""",
                    (LOGIN_LOCK_MINUTES, u)
                )

def clear_failed_logins(u):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE users SET failed_login_count=0, locked_until=NULL WHERE username=%s",
                (u,)
            )

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


def set_daily_limit(uid, limit, actor_username=None, actor_id=None):
    """limit=None -> genel varsayılana döner."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE users SET daily_limit=%s WHERE id=%s RETURNING username",
                (limit, uid)
            )
            row = cur.fetchone()
    if row:
        detail = f"{row['username']} → {limit if limit is not None else 'genel varsayılan'}"
        record_audit(actor_id, actor_username, "günlük soru limiti değiştirildi", detail)


def grant_bonus(uid, amount, actor_username=None, actor_id=None):
    """Bugüne özel ekstra soru hakkı verir (gece yarısı sıfırlanır)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """UPDATE users SET bonus_questions=%s, bonus_date=CURRENT_DATE
                   WHERE id=%s RETURNING username""",
                (amount, uid)
            )
            row = cur.fetchone()
    if row:
        record_audit(actor_id, actor_username, "bugüne ekstra soru hakkı verildi", f"{row['username']} → +{amount}")


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


def delete_vehicle(vid, actor_username=None, actor_id=None):
    """Kalıcı olarak siler; bu araca bağlı belgeler de DB'den silinir
    (Gemini tarafındaki store'ların ayrıca silinmesi main.py'de yapılır).
    Geçmiş sorular korunur (vehicle_id NULL olur, araç adı
    questions.vehicle_name_snapshot içinde okunabilir kalır)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT name FROM vehicles WHERE id=%s", (vid,))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("DELETE FROM vehicles WHERE id=%s", (vid,))
    record_audit(actor_id, actor_username, "araç silindi", row["name"])
    return True


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


def get_document(did):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM documents WHERE id=%s", (did,))
            return cur.fetchone()


def delete_document(did, actor_username=None, actor_id=None):
    """Kalıcı olarak siler. Gemini File Search store'unun silinmesi
    (varsa) main.py tarafında bu çağrıdan önce yapılır."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT filename FROM documents WHERE id=%s", (did,))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("DELETE FROM documents WHERE id=%s", (did,))
    record_audit(actor_id, actor_username, "belge silindi", row["filename"])
    return True


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


def get_question(question_id):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM questions WHERE id=%s", (question_id,))
            return cur.fetchone()


def save_question(uid, vid, q, a, s, username=None, vehicle_name=None, unresolved=False):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO questions(
                       user_id, vehicle_id, question, answer, sources,
                       username_snapshot, vehicle_name_snapshot, unresolved
                   ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (uid, vid, q, a, s, username, vehicle_name, 1 if unresolved else 0)
            )
            return cur.fetchone()["id"]


def recent_questions(limit=50, offset=0):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT q.*,
                          COALESCE(u.username, q.username_snapshot, 'silinmiş kullanıcı') AS username,
                          COALESCE(v.name, q.vehicle_name_snapshot, 'silinmiş araç') AS vehicle
                   FROM questions q
                   LEFT JOIN users u ON u.id=q.user_id
                   LEFT JOIN vehicles v ON v.id=q.vehicle_id
                   ORDER BY q.created_at DESC
                   LIMIT %s OFFSET %s""",
                (limit, offset)
            )
            return cur.fetchall()


def count_questions():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM questions")
            return cur.fetchone()["n"]


def user_questions(uid, limit=200):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT q.*, COALESCE(v.name, q.vehicle_name_snapshot, 'silinmiş araç') AS vehicle
                   FROM questions q
                   LEFT JOIN vehicles v ON v.id=q.vehicle_id
                   WHERE q.user_id=%s
                   ORDER BY q.created_at DESC
                   LIMIT %s""",
                (uid, limit)
            )
            return cur.fetchall()


# ---------- CBS: cevabı bulunamayan sorular ----------

def unanswered_questions(limit=200):
    """Dokümanda cevabı bulunamayıp admin cevabı bekleyen sorular (en eski önce)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT q.*, COALESCE(u.username, q.username_snapshot, 'silinmiş kullanıcı') AS username,
                          COALESCE(v.name, q.vehicle_name_snapshot, 'silinmiş araç') AS vehicle
                   FROM questions q
                   LEFT JOIN users u ON u.id=q.user_id
                   LEFT JOIN vehicles v ON v.id=q.vehicle_id
                   WHERE q.unresolved=1
                   ORDER BY q.created_at ASC
                   LIMIT %s""",
                (limit,)
            )
            return cur.fetchall()


def recent_cbs_answers(limit=20):
    """Admin tarafından yakın zamanda cevaplanmış CBS soruları (referans amaçlı)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT q.*, COALESCE(u.username, q.username_snapshot, 'silinmiş kullanıcı') AS username,
                          COALESCE(v.name, q.vehicle_name_snapshot, 'silinmiş araç') AS vehicle
                   FROM questions q
                   LEFT JOIN users u ON u.id=q.user_id
                   LEFT JOIN vehicles v ON v.id=q.vehicle_id
                   WHERE q.admin_answer IS NOT NULL
                   ORDER BY q.admin_answered_at DESC
                   LIMIT %s""",
                (limit,)
            )
            return cur.fetchall()


def answer_cbs(question_id, admin_answer, actor_username=None, actor_id=None):
    """CBS sorusunu cevaplar; personel için bildirim kuyruğuna düşer
    (notified=0) ve aynı araca aynı soru tekrar sorulunca bu cevap
    otomatik kullanılabilir hale gelir (admin_answer dolduğu için)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """UPDATE questions
                   SET admin_answer=%s, admin_answered_by=%s, admin_answered_at=CURRENT_TIMESTAMP,
                       unresolved=0, notified=0
                   WHERE id=%s
                   RETURNING question""",
                (admin_answer, actor_username, question_id)
            )
            row = cur.fetchone()
    if row:
        record_audit(actor_id, actor_username, "CBS sorusu cevaplandı", row["question"][:120])
    return bool(row)


def find_admin_answer(vehicle_id, question):
    """Aynı araca daha önce sorulup admin tarafından cevaplanmış aynı soruyu
    bulur (baş/son boşluk ve büyük/küçük harf farkı yok sayılır). Bu, birebir
    aynı soru için Gemini'ye hiç sormadan hızlı bir kısayoldur; ifadesi farklı
    ama anlamca aynı sorular için admin_answered_for_vehicle() kullanılır."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT admin_answer, admin_answered_at
                   FROM questions
                   WHERE vehicle_id=%s AND admin_answer IS NOT NULL
                     AND lower(trim(question))=lower(trim(%s))
                   ORDER BY admin_answered_at DESC
                   LIMIT 1""",
                (vehicle_id, question)
            )
            return cur.fetchone()


def admin_answered_for_vehicle(vehicle_id, limit=40):
    """Bu araç için yönetici tarafından daha önce cevaplanmış tüm soru-cevap
    çiftlerini döner (en yeniden eskiye). Bunlar, personelin sorusu farklı
    kelimelerle ama anlamca aynı şekilde sorulduğunda yapay zekânın PDF
    kılavuzuyla birlikte bir 'kaynak' olarak değerlendirmesi için AI'ya
    bağlam olarak verilir; eşleştirme kararını regex değil model verir."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT question, admin_answer
                   FROM questions
                   WHERE vehicle_id=%s AND admin_answer IS NOT NULL
                   ORDER BY admin_answered_at DESC
                   LIMIT %s""",
                (vehicle_id, limit)
            )
            return cur.fetchall()


def unnotified_answers(uid):
    """Kullanıcının henüz görmediği, admin tarafından cevaplanmış sorular."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT q.id, q.question, q.admin_answer, q.admin_answered_at, q.created_at,
                          COALESCE(v.name, q.vehicle_name_snapshot, 'silinmiş araç') AS vehicle
                   FROM questions q
                   LEFT JOIN vehicles v ON v.id=q.vehicle_id
                   WHERE q.user_id=%s AND q.admin_answer IS NOT NULL AND q.notified=0
                   ORDER BY q.admin_answered_at ASC""",
                (uid,)
            )
            return cur.fetchall()


def mark_notified(ids):
    if not ids:
        return
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE questions SET notified=1 WHERE id = ANY(%s)", (list(ids),))


def delete_question(question_id, actor_username=None, actor_id=None):
    """Bir soru/cevap kaydını (CBS'de bekleyen ya da cevaplanmış olsun)
    kalıcı olarak siler."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT question FROM questions WHERE id=%s", (question_id,))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("DELETE FROM questions WHERE id=%s", (question_id,))
    record_audit(actor_id, actor_username, "CBS sorusu silindi", row["question"][:120])
    return True


# ---------- geri bildirim (👍/👎) ----------

def set_feedback(question_id, uid, value):
    """Kullanıcı yalnızca kendi sorduğu bir soruya geri bildirim
    verebilir. 'down' verilip henüz admin cevabı yoksa soru otomatik
    olarak CBS kuyruğuna (unresolved) düşer."""
    if value not in ("up", "down"):
        return False
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT user_id, admin_answer FROM questions WHERE id=%s", (question_id,))
            row = cur.fetchone()
            if not row or row["user_id"] != uid:
                return False
            to_cbs = value == "down" and row["admin_answer"] is None
            cur.execute(
                """UPDATE questions
                   SET feedback=%s, feedback_at=CURRENT_TIMESTAMP,
                       unresolved = CASE WHEN %s THEN 1 ELSE unresolved END
                   WHERE id=%s""",
                (value, to_cbs, question_id)
            )
    return True


# ---------- istatistikler ----------

def stats():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM questions")
            total = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM questions WHERE created_at::date = CURRENT_DATE")
            today = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM questions WHERE unresolved=1")
            pending_cbs = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM questions WHERE admin_answer IS NOT NULL")
            answered_cbs = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM questions WHERE feedback='up'")
            feedback_up = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM questions WHERE feedback='down'")
            feedback_down = cur.fetchone()["n"]

            cur.execute(
                """SELECT COALESCE(v.name, q.vehicle_name_snapshot, 'silinmiş araç') AS vehicle, COUNT(*) AS n
                   FROM questions q LEFT JOIN vehicles v ON v.id=q.vehicle_id
                   GROUP BY vehicle ORDER BY n DESC LIMIT 5"""
            )
            top_vehicles = cur.fetchall()

            cur.execute(
                """SELECT (created_at AT TIME ZONE 'UTC')::date AS d, COUNT(*) AS n
                   FROM questions
                   WHERE created_at >= CURRENT_DATE - INTERVAL '6 days'
                   GROUP BY d ORDER BY d"""
            )
            last7_raw = {str(r["d"]): r["n"] for r in cur.fetchall()}

            cur.execute(
                """SELECT AVG(EXTRACT(EPOCH FROM (admin_answered_at - created_at))) AS avg_sec
                   FROM questions WHERE admin_answer IS NOT NULL"""
            )
            avg_sec = cur.fetchone()["avg_sec"]

    last7 = []
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        last7.append({"date": d, "count": last7_raw.get(str(d), 0)})

    return {
        "total": total, "today": today,
        "pending_cbs": pending_cbs, "answered_cbs": answered_cbs,
        "feedback_up": feedback_up, "feedback_down": feedback_down,
        "top_vehicles": top_vehicles, "last7": last7,
        "avg_cbs_hours": round(avg_sec / 3600, 1) if avg_sec else None,
    }


# ---------- tarayıcı push bildirimleri ----------

def save_push_subscription(uid, endpoint, p256dh, auth):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO push_subscriptions(user_id,endpoint,p256dh,auth)
                   VALUES(%s,%s,%s,%s)
                   ON CONFLICT (endpoint) DO UPDATE
                       SET user_id=EXCLUDED.user_id, p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth""",
                (uid, endpoint, p256dh, auth)
            )


def delete_push_subscription(endpoint):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM push_subscriptions WHERE endpoint=%s", (endpoint,))


def push_subscriptions_for_user(uid):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM push_subscriptions WHERE user_id=%s", (uid,))
            return cur.fetchall()


# ---------- personel notları / ek bilgiler ----------
# Kılavuzda yazmayan veya araç üzerinde sonradan yapılmış değişiklikleri
# anlatan, yöneticinin serbest metin olarak girdiği notlar. PDF ve CBS
# geçmişi gibi AI'ya bağlam olarak verilir (bkz. main.py _vehicle_notes_context_text).

def add_vehicle_note(vehicle_id, note, actor_username=None, actor_id=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO vehicle_notes(vehicle_id, note, created_by, created_by_id)
                   VALUES(%s,%s,%s,%s) RETURNING id""",
                (vehicle_id, note, actor_username, actor_id)
            )
            nid = cur.fetchone()["id"]
    record_audit(actor_id, actor_username, "ek not eklendi", note[:120])
    return nid


def update_vehicle_note(note_id, note, actor_username=None, actor_id=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """UPDATE vehicle_notes SET note=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s RETURNING note""",
                (note, note_id)
            )
            row = cur.fetchone()
    if row:
        record_audit(actor_id, actor_username, "ek not düzenlendi", note[:120])
    return bool(row)


def delete_vehicle_note(note_id, actor_username=None, actor_id=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT note FROM vehicle_notes WHERE id=%s", (note_id,))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("DELETE FROM vehicle_notes WHERE id=%s", (note_id,))
    record_audit(actor_id, actor_username, "ek not silindi", row["note"][:120])
    return True


def vehicle_notes(vehicle_id=None):
    """vehicle_id verilmezse (admin panelinde listelemek için) TÜM notları,
    araç adıyla birlikte döner. vehicle_id verilirse (AI'ya bağlam
    oluştururken main.py'den çağrılır) yalnızca o araca ait notları döner."""
    with conn() as c:
        with c.cursor() as cur:
            if vehicle_id is not None:
                cur.execute(
                    """SELECT * FROM vehicle_notes WHERE vehicle_id=%s
                       ORDER BY created_at DESC""",
                    (vehicle_id,)
                )
            else:
                cur.execute(
                    """SELECT n.*, v.name AS vehicle
                       FROM vehicle_notes n JOIN vehicles v ON v.id=n.vehicle_id
                       ORDER BY n.created_at DESC"""
                )
            return cur.fetchall()


def local_fallback_search(vehicle_id, question, limit=3):
    """Gemini API'ye ulaşılamadığında (yapılandırma eksik veya ağ/servis
    hatası) kullanılan YEDEK arama. AI'nın anlam bazlı eşleştirmesi burada
    yoktur; bu sadece Postgres'in Türkçe tam metin (full-text) araması ile
    CBS geçmişi + personel notları içinde anahtar kelime örtüşmesi arar.
    plainto_tsquery varsayılan olarak TÜM kelimelerin eşleşmesini ister
    (AND); bu, çekim ekleri farklı olan (ör. 'seviyesi' / 'seviyesini')
    doğal dil sorularında neredeyse hiç eşleşme bulamaz. Bunun yerine
    kelimeler ARASINDA 'veya' (OR) mantığı kullanılır: herhangi bir anlamlı
    kelime örtüşürse sonuç dönülür, en çok örtüşen ts_rank ile öne çıkar.
    Bu yüzden sonuçlar main.py tarafında her zaman 'kesin cevap değil,
    yaklaşık eşleşme' uyarısıyla sunulur."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """
                WITH q AS (
                    SELECT to_tsquery('turkish',
                        replace(plainto_tsquery('turkish', %(q)s)::text, ' & ', ' | ')
                    ) AS tsq
                )
                SELECT 'cbs' AS kind, question AS baslik, admin_answer AS icerik,
                       ts_rank(to_tsvector('turkish', question || ' ' || admin_answer), q.tsq) AS rank
                FROM questions, q
                WHERE vehicle_id=%(vid)s AND admin_answer IS NOT NULL
                  AND to_tsvector('turkish', question || ' ' || admin_answer) @@ q.tsq
                UNION ALL
                SELECT 'not' AS kind, NULL AS baslik, note AS icerik,
                       ts_rank(to_tsvector('turkish', note), q.tsq) AS rank
                FROM vehicle_notes, q
                WHERE vehicle_id=%(vid)s
                  AND to_tsvector('turkish', note) @@ q.tsq
                ORDER BY rank DESC
                LIMIT %(lim)s
                """,
                {"q":question,"vid":vehicle_id,"lim":limit}
            )
            return cur.fetchall()
