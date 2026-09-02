# TradeNex Capital (DEMO) — Projek Sekolah

⚠️ **Ini adalah laman web DEMO untuk projek sekolah / pembelajaran.**
Bukan platform pelaburan sebenar. Semua nama syarikat, data, dan nilai akaun
adalah rekaan. Jangan gunakan untuk transaksi kewangan sebenar.

## Apa yang ada dalam projek ni

- `index.html` (dalam `templates/`) — laman utama (landing page) asal anda
- Sistem **login user** — user log masuk dengan ID & password yang **admin** bagi (tiada daftar sendiri)
- **Dashboard user** — papar baki akaun, untung/rugi, dan graf trading berdasarkan entry yang admin masukkan
- **Panel admin** (`/admin`) — admin login sendiri, boleh:
  - Cipta / kemaskini / padam akaun user
  - Tetapkan baki & untung-rugi setiap user
  - Tambah/padam "entry" data untuk graf trading setiap user
- Database **SQLite** (local) / **PostgreSQL** (di Railway)

## Struktur Fail

```
tradenex-demo/
├── app.py                  # Logic utama Flask (semua routes)
├── models.py               # Model database (User, AdminUser, TradeEntry)
├── requirements.txt        # Dependency Python
├── Procfile                # Arahan untuk Railway/gunicorn
├── railway.json            # Config Railway
├── .env.example            # Contoh fail .env (copy jadi .env)
├── .gitignore
└── templates/
    ├── index.html          # Laman utama (asal anda, sudah diubah suai)
    ├── login.html          # Halaman login user
    ├── dashboard.html       # Dashboard user (graf + baki)
    ├── admin_login.html     # Halaman login admin
    └── admin_dashboard.html # Panel admin
```

---

## 1. Setup di Komputer Sendiri (Local)

### a) Install Python & dependency

Pastikan **Python 3.10+** sudah ada. Buka terminal dalam folder projek:

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### b) Setup fail .env

```bash
cp .env.example .env
```

Buka `.env`, tukar `SECRET_KEY` kepada rentetan rawak, dan tukar `DEFAULT_ADMIN_PASSWORD`
kepada password admin yang anda nak guna.

### c) Cipta database & akaun admin pertama

```bash
flask init-db
```

Ini akan cipta semua jadual database, dan satu akaun admin lalai:
- **Username:** `admin`
- **Password:** (ikut nilai `DEFAULT_ADMIN_PASSWORD` dalam `.env`, default `admin123`)

### d) Jalankan server

```bash
python app.py
```

Buka browser:
- Laman utama: `http://localhost:5000/`
- Login user: `http://localhost:5000/login`
- Login admin: `http://localhost:5000/admin/login`

---

## 2. Cara Guna Panel Admin

1. Pergi ke `http://localhost:5000/admin/login` (atau `https://domain-anda.up.railway.app/admin/login` selepas deploy)
2. Log masuk dengan akaun admin (`admin` / password dalam `.env`)
3. Di panel admin, anda boleh:
   - **Cipta User Baru** — isi NOVA ID (untuk login user), nama, password, dan baki awal
   - Klik **"Urus"** pada mana-mana user untuk:
     - Kemaskini nama, baki, untung/rugi, tukar password, aktif/non-aktifkan akaun
     - **Tambah Entry Graf** — setiap entry (tarikh + nilai) akan muncul sebagai titik dalam graf trading user tersebut
     - Padam entry atau padam akaun terus

Bila user log masuk di `/login` dengan ID & password yang anda bagi, mereka akan
nampak dashboard dengan baki, untung/rugi, dan graf yang anda set.

---

## 3. Push ke GitHub

```bash
git init
git add .
git commit -m "Setup TradeNex demo dengan login & admin panel"
git branch -M main
git remote add origin https://github.com/USERNAME-ANDA/NAMA-REPO.git
git push -u origin main
```

> `.env` dan `*.db` sudah disenaraikan dalam `.gitignore` supaya tidak ter-push
> secara tidak sengaja (rahsia password tidak boleh ada dalam GitHub).

---

## 4. Deploy ke Railway

1. Pergi ke [railway.app](https://railway.app) → log masuk dengan GitHub
2. **New Project** → **Deploy from GitHub repo** → pilih repo yang baru di-push
3. Railway akan auto-detect Python (Nixpacks) dan install `requirements.txt`
4. **Tambah Database (disyorkan untuk production):**
   - Dalam project Railway → **+ New** → **Database** → **PostgreSQL**
   - Railway automatik akan masukkan `DATABASE_URL` ke environment variables app anda
5. **Set Environment Variables** (tab *Variables* dalam service app):
   - `SECRET_KEY` = rentetan rawak panjang
   - `DEFAULT_ADMIN_PASSWORD` = password admin pilihan anda
6. **Jalankan migration/init-db sekali** selepas deploy pertama:
   - Buka tab **Settings → Deploy**, atau guna Railway CLI:
     ```bash
     railway run flask init-db
     ```
   - (Procfile sudah ada baris `release: flask init-db` supaya ini auto-jalan setiap deploy — selamat untuk dijalankan berulang kali sebab kod check dulu sebelum cipta admin baru)
7. Railway akan bagi URL awam (contoh `https://nama-projek.up.railway.app`)
   - Laman utama: `/`
   - Login user: `/login`
   - Login admin: `/admin/login`

---

## 5. Deploy ke Northflank

Projek ini sudah boleh deploy di Northflank menggunakan `Dockerfile`.

1. Push kod terkini ke GitHub:

```bash
git add .
git commit -m "Add Dockerfile for Northflank deployment"
git push
```

2. Di Northflank:
   - `Create Project`
   - `Add Service` -> `From Git Repository`
   - Pilih repo projek ini
   - Build method: `Dockerfile`

3. Set environment variables di service Northflank:
   - `SECRET_KEY` = string rawak panjang
   - `DEFAULT_ADMIN_PASSWORD` = password admin pilihan anda
   - `DATABASE_URL` = URL PostgreSQL (guna add-on PostgreSQL Northflank, atau external DB)
   - `WHATSAPP_NUMBER` = nombor WhatsApp pegawai dalam format antarabangsa tanpa `+` atau ruang, contoh `60123456789`

4. Networking/Port:
   - Exposed HTTP port: `5000` (app juga sokong `PORT` env dari platform)

5. Deploy service. Lepas status `Healthy`, buka URL service:
   - Laman utama: `/`
   - Login user: `/login`
   - Login admin: `/admin/login`

6. Init database (sekali sahaja selepas deploy pertama):
   - Buka terminal Northflank service dan jalankan:

```bash
flask init-db
```

Nota:
- Kalau `DATABASE_URL` tiada, app fallback ke SQLite dalam container (data tidak kekal jika redeploy/restart).
- Untuk penggunaan stabil, wajib guna PostgreSQL di Northflank.

---

## 6. Nota Keselamatan (Penting untuk Projek Sekolah)

- Ini **demo/latihan** — *jangan* gunakan untuk data kewangan sebenar atau letak akaun ini secara terbuka untuk orang awam guna sebagai produk sebenar.
- Tukar `DEFAULT_ADMIN_PASSWORD` dan `SECRET_KEY` — jangan kekalkan nilai default.
- Semua password user/admin disimpan dalam bentuk **hash** (bukan plain text) menggunakan `werkzeug.security`.
- Tiada had kadar (rate limiting) pada login — untuk projek sekolah ini cukup, tapi untuk production sebenar perlu ditambah.

---

## 7. Troubleshooting

| Masalah | Penyelesaian |
|---|---|
| `flask init-db` error "No such command" | Pastikan `FLASK_APP=app.py` (biasanya auto-detect sebab nama fail `app.py`) |
| Login user sentiasa "ID tidak sah" | Pastikan admin sudah cipta akaun user tersebut di panel admin |
| Graf tidak muncul | Pastikan admin sudah tambah sekurang-kurangnya 1 entry untuk user itu |
| Deploy Railway gagal | Semak log build — selalunya disebabkan `requirements.txt` atau environment variable yang tertinggal |
