import os, sqlite3, hashlib, secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
DB = Path(os.getenv("DATABASE_PATH", "/tmp/itfaiye.db"))
DB.parent.mkdir(parents=True, exist_ok=True)

def conn():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    return c

def init():
    c=conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'personel',
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS vehicles(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS documents(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      vehicle_id INTEGER NOT NULL,
      filename TEXT NOT NULL,
      file_id TEXT,
      vector_store_id TEXT,
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(vehicle_id) REFERENCES vehicles(id)
    );
    CREATE TABLE IF NOT EXISTS questions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      vehicle_id INTEGER,
      question TEXT NOT NULL,
      answer TEXT NOT NULL,
      sources TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(user_id) REFERENCES users(id),
      FOREIGN KEY(vehicle_id) REFERENCES vehicles(id)
    );
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)
    c.commit(); c.close()

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()

def seed(admin_user, admin_pass):
    c=conn()
    row=c.execute("SELECT id FROM users WHERE username=?", (admin_user,)).fetchone()
    if not row:
        c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                  (admin_user,hash_password(admin_pass),"admin"))
    if not c.execute("SELECT id FROM vehicles LIMIT 1").fetchone():
        c.execute("INSERT INTO vehicles(name) VALUES(?)",("Rosenbauer L42A-XS",))
    c.commit(); c.close()

def create_user(u,p):
    c=conn()
    try:
        c.execute("INSERT INTO users(username,password_hash) VALUES(?,?)",(u,hash_password(p)))
        c.commit(); return True
    except sqlite3.IntegrityError: return False
    finally: c.close()

def auth(u,p):
    c=conn()
    row=c.execute("SELECT * FROM users WHERE username=? AND password_hash=? AND active=1",
                  (u,hash_password(p))).fetchone()
    c.close(); return row

def new_session(uid):
    t=secrets.token_urlsafe(32)
    c=conn(); c.execute("INSERT INTO sessions(token,user_id) VALUES(?,?)",(t,uid)); c.commit(); c.close()
    return t

def user_by_token(t):
    if not t: return None
    c=conn()
    row=c.execute("""SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
                     WHERE s.token=? AND u.active=1""",(t,)).fetchone()
    c.close(); return row

def delete_session(t):
    c=conn(); c.execute("DELETE FROM sessions WHERE token=?",(t,)); c.commit(); c.close()

def vehicles():
    c=conn(); rows=c.execute("SELECT * FROM vehicles WHERE active=1 ORDER BY name").fetchall(); c.close(); return rows

def add_vehicle(name):
    c=conn()
    try:
        c.execute("INSERT INTO vehicles(name) VALUES(?)",(name,)); c.commit(); return True
    except sqlite3.IntegrityError: return False
    finally: c.close()

def documents():
    c=conn()
    rows=c.execute("""SELECT d.*,v.name vehicle FROM documents d JOIN vehicles v ON v.id=d.vehicle_id
                      ORDER BY d.created_at DESC""").fetchall()
    c.close(); return rows

def add_doc(vehicle_id,filename,file_id,vs_id):
    c=conn(); c.execute("""INSERT INTO documents(vehicle_id,filename,file_id,vector_store_id)
                           VALUES(?,?,?,?)""",(vehicle_id,filename,file_id,vs_id)); c.commit(); c.close()

def question_count(uid):
    c=conn()
    row=c.execute("""SELECT COUNT(*) n FROM questions
                     WHERE user_id=? AND created_at >= datetime('now','start of day')""",(uid,)).fetchone()
    c.close(); return row["n"]

def save_question(uid,vid,q,a,s):
    c=conn(); c.execute("""INSERT INTO questions(user_id,vehicle_id,question,answer,sources)
                           VALUES(?,?,?,?,?)""",(uid,vid,q,a,s)); c.commit(); c.close()

def recent_questions(limit=100):
    c=conn()
    rows=c.execute("""SELECT q.*,u.username,v.name vehicle FROM questions q
                      LEFT JOIN users u ON u.id=q.user_id
                      LEFT JOIN vehicles v ON v.id=q.vehicle_id
                      ORDER BY q.created_at DESC LIMIT ?""",(limit,)).fetchall()
    c.close(); return rows
