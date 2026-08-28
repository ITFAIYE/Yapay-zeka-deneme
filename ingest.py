import os, sys, time
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("OPENAI_API_KEY")
if not key:
    raise SystemExit("OPENAI_API_KEY eksik.")
if len(sys.argv) != 2:
    raise SystemExit('Kullanım: python ingest.py "dosya.pdf"')

pdf = Path(sys.argv[1])
if not pdf.exists():
    raise SystemExit(f"Dosya bulunamadı: {pdf}")

H = {"Authorization": f"Bearer {key}"}
JH = {**H, "Content-Type":"application/json"}

with pdf.open("rb") as f:
    r = requests.post(
        "https://api.openai.com/v1/files",
        headers=H,
        files={"file": (pdf.name, f, "application/pdf")},
        data={"purpose":"assistants"},
        timeout=180
    )
r.raise_for_status()
fid = r.json()["id"]

r = requests.post(
    "https://api.openai.com/v1/vector_stores",
    headers=JH,
    json={"name": f"Itfaiye - {pdf.stem}", "file_ids":[fid]},
    timeout=120
)
r.raise_for_status()
vs = r.json()["id"]

for _ in range(180):
    r = requests.get(f"https://api.openai.com/v1/vector_stores/{vs}", headers=H, timeout=30)
    r.raise_for_status()
    obj = r.json()
    status = obj.get("status")
    if status == "completed":
        print("\nVector Store hazır.")
        print(f"OPENAI_VECTOR_STORE_ID={vs}")
        break
    if status == "expired":
        raise SystemExit("Vector Store expired.")
    time.sleep(2)
else:
    raise SystemExit("İndeksleme zaman aşımına uğradı.")
