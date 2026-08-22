# İtfaiye Yapay Zekâ Kılavuzu — İnternete Açık Üretim Temeli

Bu paket, itfaiye araçları için PDF tabanlı soru-cevap web uygulamasının
tamamlanmış ilk üretim temelidir.

## İçerik
- Personel kayıt/giriş
- Oturum yönetimi
- Araç seçimi
- Soru-cevap
- Günlük soru limiti
- Yönetici paneli
- Araç ekleme/silme
- PDF yükleme ve araca bağlama
- OpenAI Vector Store yükleme
- Doküman üzerinden File Search
- Soru geçmişi
- Basit güvenlik kontrolleri
- Mobil uyumlu arayüz

## Kurulum

Python 3.11+:

    python -m venv .venv
    # Windows: .venv\Scripts\activate
    # Linux/macOS: source .venv/bin/activate
    pip install -r requirements.txt

`.env.example` dosyasını `.env` yapın ve:
- OPENAI_API_KEY
- OPENAI_MODEL
- APP_SECRET_KEY
- ADMIN_USERNAME
- ADMIN_PASSWORD

alanlarını doldurun.

## İlk PDF

PDF'yi `data/` klasörüne koyabilirsiniz.

Vector Store oluşturmak için:

    python ingest.py "data/Kullanım açıklamaları_A2CA005_PR11EA00502_TR_001_compressed.pdf"

Komut size Vector Store ID verir. Bunu `.env` içindeki
OPENAI_VECTOR_STORE_ID alanına yazın.

## Uygulama

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Tarayıcı:
http://127.0.0.1:8000

Üretimde reverse proxy/HTTPS kullanın.

## Yönetici

    /login

`.env` içindeki ADMIN_USERNAME ve ADMIN_PASSWORD ile giriş yapın.

Yönetici:
- araç ekler,
- PDF yükler,
- PDF'yi Vector Store'a aktarır,
- araca doküman atar,
- dokümanları aktif/pasif yapar.

## İnternete yayın

Bu paket doğrudan bir hosting hesabına atılacak nihai "alan adı" değildir.
Sunucu + HTTPS + domain gerekir. Örnek dağıtım:
- Ubuntu VPS + Nginx + Uvicorn
- Render / Railway / Fly.io benzeri bir Python hostu
- PostgreSQL (üretimde önerilir)

OpenAI anahtarını asla frontend'e koymayın. Sadece backend ortam değişkeninde
tutun.

## Güvenlik notu

Bu uygulama teknik bir yazılım prototipidir. İtfaiye aracının kullanımı,
acil işletimi, bakım ve güvenlik kararlarında üretici kılavuzu ve kurum
prosedürleri esas alınmalıdır.

Rosenbauer gibi üçüncü taraf dokümanları kamuya açmadan önce kullanım/lisans
hakkının kurumunuzda olduğundan emin olun.
