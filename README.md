# TradeNex Capital (DEMO) — Projek Sekolah

⚠️ **Ini adalah laman web DEMO untuk projek sekolah / pembelajaran.**
Bukan platform pelaburan sebenar. Semua nama syarikat, data, dan nilai akaun
adalah rekaan. Jangan gunakan untuk transaksi kewangan sebenar.

## Apa yang ada dalam projek ni

- `index.html` (dalam `templates/`) — laman utama (landing page) asal anda
- Sistem **login user** — user log masuk dengan ID & password yang **admin** bagi (tiada daftar sendiri)
- **English** ialah bahasa lalai. Pengguna boleh memilih **Bahasa Malaysia** pada halaman pelanggan; pilihan disimpan pada akaun dan digunakan semula selepas log masuk
- **Dashboard user** — papar baki akaun, untung/rugi, graf trading, profil dengan kod referral unik berformat `E-` diikuti tujuh digit, dan genealogi referral dua level dengan carian serta penapis
- Borang pendaftaran menerima kod referral pilihan; kod yang dimasukkan mesti milik akaun aktif
- Komisen referral dijadual sekali bagi setiap akaun yang diterima melalui referral selepas ciri ini dipasang. Kadarnya mengikut rank penerima komisen: No Rank RM15 (Level 1) / RM5 (Level 2), Elite Member RM30/RM15, Ambassador Member RM50/RM25, Senior Ambassador RM80/RM40, dan Territory Partner RM100/RM50. Komisen dibayar pada 00:05 MYT pada hari terakhir bulan; akaun penerima dan akaun referral mesti aktif ketika kredit dibuat. Komisen yang dibayar masuk ke Wallet dan tidak mengubah modal akaun. Komisen tertangguh dikira semula apabila rank penerima dinaikkan. Akaun referral lama tidak menerima backfill.
- Semasa sesi dagangan berjalan, akaun belum matang hanya boleh mengeluarkan komisen referral yang sudah dibayar. Akaun matang boleh mengeluarkan semua baki termasuk modal dan komisen tanpa menutup sesi dagangan. Selepas sesi ditutup, untung/rugi juga boleh dimohon.
- Dashboard memaparkan komisen setiap referral, status akaun dan sebab komisen masih tertangguh, serta sejarah kredit Wallet. Popup admin menunjukkan laluan referral dan status payout untuk semakan.
- Render Cron menjalankan `flask --app app process-referral-commissions` setiap hari pada 16:05 UTC (00:05 MYT) dan mengejar bayaran tertunggak jika satu jadual terlepas.
- **Panel admin** (`/admin`) — admin login sendiri, boleh:
  - Cipta / kemaskini / nyahaktifkan akaun user; butang Padam memadam akaun dan sejarah berkait secara kekal
  - Pautkan akaun kepada rekod pendaftaran dan maklumat bank yang tepat
  - Semak permohonan pengeluaran serta simpan nombor rujukan transaksi bayaran
  - Tandakan akaun matang untuk membenarkan pengeluaran semua baki termasuk modal dan komisen walaupun sesi masih berjalan
  - Tetapkan baki & untung-rugi setiap user
  - Tambah/padam "entry" data untuk graf trading setiap user
- Database **SQLite** (local) / **PostgreSQL** (Render)

## Struktur Fail

```
tradenex-demo/
├── app.py                  # Logic utama Flask (semua routes)
├── models.py               # Model database (User, AdminUser, TradeEntry)
├── requirements.txt        # Dependency Python
├── Procfile                # Arahan server Gunicorn
├── railway.json            # Config deployment Railway alternatif
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
- **Password development setempat:** ikut nilai `DEFAULT_ADMIN_PASSWORD` dalam `.env` (default lama `admin123`; tukar sebelum login).

Production tidak mempunyai password fallback: `SECRET_KEY`, PostgreSQL `DATABASE_URL`
dan `DEFAULT_ADMIN_PASSWORD` perlu ditetapkan. Password admin pertama mesti sekurang-
kurangnya 12 aksara. Tukar melalui panel admin selepas login pertama jika perlu.

### d) Jalankan server

```bash
python app.py
```

Buka browser:
- Laman utama: `http://localhost:5000/`
- Login user: `http://localhost:5000/login`
- Login admin: `http://localhost:5000/admin/login`

## Ujian Automatik

Jalankan ujian aliran pengeluaran dan komisen menggunakan SQLite sementara:

```bash
python -m unittest discover -s tests -v
```

GitHub Actions menjalankan ujian yang sama secara automatik pada setiap push dan pull request. Ujian tidak menggunakan database production.

---

## 2. Cara Guna Panel Admin

1. Pergi ke `http://localhost:5000/admin/login` (atau `/admin/login` pada URL Render selepas deploy)
2. Log masuk dengan akaun admin (`admin` / password dalam `.env`)
3. Di panel admin, anda boleh:
   - **Cipta User Baru** — isi NOVA ID (untuk login user), nama, password, dan baki awal
   - Klik **"Urus"** pada mana-mana user untuk:
     - Kemaskini nama, baki, untung/rugi, tukar password, aktif/non-aktifkan akaun
     - **Tambah Entry Graf** — setiap entry (tarikh + nilai) akan muncul sebagai titik dalam graf trading user tersebut
     - Padam entry atau nyahaktifkan akaun tanpa memadam sejarah kewangan

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

> `.env` dan `*.db` disenaraikan dalam `.gitignore`. Jika `.env` pernah
> ditrack, buang daripada index Git dan putarkan semua rahsia yang pernah
> digunakan; `.gitignore` tidak memadam salinan dalam sejarah Git.

---

## 4. Deploy ke Render (utama)

Fail `render.yaml` ialah Blueprint projek ini. Ia mentakrifkan Web Service,
PostgreSQL, Key Value untuk rate limiting, dan Cron Job untuk komisen referral
di region Singapore.

1. Push kod ke GitHub dan pastikan `render.yaml` ada di root repository.
2. Dalam [Render Dashboard](https://dashboard.render.com/), pilih **New**
   → **Blueprint**, sambungkan repository, kemudian deploy Blueprint.
3. Semak resources yang akan dibuat daripada `render.yaml`. Fail itu menetapkan
   `start.sh` sebagai arahan web, `/health` sebagai health check, serta
   PostgreSQL untuk `DATABASE_URL`.
4. Selepas resources dibuat, isi environment variables yang ditandakan manual
   dalam Web Service:
   - `DEFAULT_ADMIN_PASSWORD` — password admin pertama, sekurang-kurangnya 12 aksara.
   - `WHATSAPP_NUMBER` — nombor antarabangsa tanpa `+` atau ruang, contoh `60123456789`.
   `SECRET_KEY` dijana oleh Blueprint, `DATABASE_URL` dipautkan kepada
   PostgreSQL dan `REDIS_URL` kepada Key Value. Pastikan Key Value tersedia
   dan jangan tukar `APP_ENV=production` atau `FLASK_DEBUG=0`.
5. Tunggu Web Service berstatus **Live** dan health check `/health` berjaya.
   Endpoint ini menguji sambungan database; status HTTP 503 bermaksud
   perkhidmatan belum sihat dan butiran ralat ada dalam log aplikasi.
6. Aplikasi menyemak skema database dan mencipta admin pertama semasa startup.
   Buka URL Web Service untuk laman utama, kemudian `/admin/login` untuk log
   masuk. Tukar password admin melalui panel selepas login pertama.
7. Semak Cron Job `tradenex-referral-commissions`. Ia berjalan setiap hari
   pada 16:05 UTC (00:05 MYT). Buka log job untuk mengesahkan
   `Referral commission job completed` dan nilai `paid_count`; job memproses
   payout yang sudah sampai tarikh bayar, termasuk yang tertangguh.

Semak harga, had dan sokongan resource semasa dalam Render Dashboard sebelum
deploy; pelan `starter` dan `basic-1gb` yang dinyatakan dalam Blueprint mungkin
melibatkan caj mengikut tawaran akaun dan polisi Render semasa.

### Staging berasingan di Render

`render-staging.yaml` mentakrifkan web, PostgreSQL, Key Value dan cron staging
yang berasingan daripada Blueprint production. Pastikan perubahan kod dan fail
Blueprint ini telah di-commit dan di-push ke branch GitHub yang dikhaskan untuk
staging. Jangan commit fail `.env` atau rahsia lain.

1. Dalam Render Dashboard, pilih **New** → **Blueprint** dan sambungkan
   repository serta branch staging. Semasa setup, tetapkan path Blueprint
   kepada `render-staging.yaml`, bukan `render.yaml`.
2. Semak nama semua resource yang dicadangkan bermula dengan `tradenex-staging-`,
   harga dan caj sebelum deploy. Jangan pautkan URL database, Key Value atau
   rahsia production kepada staging.
3. Isi `DEFAULT_ADMIN_PASSWORD` yang unik dan kuat serta `WHATSAPP_NUMBER` untuk
   ujian sahaja melalui Render, bukan Git atau chat. `SECRET_KEY` staging dijana
   secara berasingan. Kekalkan `APP_ENV=production` dan `FLASK_DEBUG=0` supaya
   konfigurasi keselamatan dan storan sama seperti production.
4. Selepas deploy, sahkan web dan `/health`, login admin, operasi Redis, dan log
   cron staging. `/health` menguji database sahaja dan tidak membuktikan Redis
   atau keseluruhan flow berfungsi. Uji pendaftaran, trade, referral dan
   withdrawal dengan data serta transaksi simulasi sahaja.

URL staging boleh diakses umum dan tidak mempunyai sekatan akses tambahan.
Jangan gunakan data peribadi sebenar, nombor hubungan production, atau wang
sebenar; cron staging akan memproses komisen pada database staging. Blueprint
staging perlu dikemas kini seiring perubahan konfigurasi production.

---

## 5. Deploy ke Railway (alternatif)

Konfigurasi Railway masih disimpan dalam repository, tetapi Render ialah
platform deploy utama projek ini. Di Railway, deploy repository, tambah
PostgreSQL, kemudian tetapkan `SECRET_KEY`, `DEFAULT_ADMIN_PASSWORD` dan
`WHATSAPP_NUMBER`. Pastikan `DATABASE_URL` dipautkan kepada PostgreSQL. Aplikasi
menyemak skema dan mencipta admin pertama semasa startup; jika setup manual
diperlukan, jalankan `railway run flask init-db`.

---

## 6. Deploy ke Northflank (alternatif)

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
- SQLite hanya digunakan untuk development setempat.
- Dalam production, aplikasi gagal startup jika `DATABASE_URL` PostgreSQL atau `SECRET_KEY` tiada; tiada fallback SQLite.
- `DEFAULT_ADMIN_PASSWORD` diperlukan untuk mencipta admin pertama. Jika admin sudah wujud, tukar password melalui panel admin.

---

## 7. Nota Keselamatan (Penting untuk Projek Sekolah)

- Ini **demo/latihan** — *jangan* gunakan untuk data kewangan sebenar atau letak akaun ini secara terbuka untuk orang awam guna sebagai produk sebenar.
- Production wajib mempunyai `DEFAULT_ADMIN_PASSWORD`, `SECRET_KEY` dan `DATABASE_URL`; jangan gunakan nilai development.
- Semua password user/admin disimpan dalam bentuk **hash** (bukan plain text) menggunakan `werkzeug.security`.
- Login user/admin dan pendaftaran mempunyai had kadar. Production memerlukan
  `REDIS_URL` untuk storan kadar bersama; development menggunakan memori jika
  URL tidak ditetapkan.
- Pendaftaran demo menyimpan maklumat peribadi dan bank yang dihantar. Gunakan data rekaan sahaja sehingga notis privasi, asas pemprosesan, kawalan akses dan tempoh penyimpanan sebenar ditetapkan.

---

## 8. Pangkalan Data, Sandaran dan Pemulihan

Blueprint `render.yaml` memautkan `DATABASE_URL` Web Service kepada pangkalan
data PostgreSQL `tradenex-postgres`. Selepas deploy, semak di Render Dashboard bahawa:

- Nilai wang akaun, pengeluaran dan komisen disimpan sebagai `NUMERIC(20,2)`
  dan dikira dengan Decimal. Input wang dibundarkan ke sen menggunakan
  round-half-even. Nilai candle kekal menggunakan formula asal.
- Jika jadual kewangan PostgreSQL lama mengandungi data dan masih menggunakan
  float, startup akan berhenti dan meminta migrasi disahkan. Jangan padam
  data untuk mengatasi masalah ini; sandarkan, uji migrasi pada salinan,
  semak pembundaran dan rekonsiliasi dahulu. Jadual lama yang kosong boleh
  ditukar secara automatik. SQLite setempat tidak menyamai jaminan ketepatan
  penyimpanan PostgreSQL.
- Sesi baharu Entry–Close dihadkan kepada 30 hari; sesi lama tidak dipotong.
- Setiap worker menyimpan cache candle terhad dalam memori dan menyambung
  janaan pada hujung sesi. Cache akan diisi semula selepas restart atau apabila
  ruang diperlukan; formula kewangan dan nilai Close tidak berubah.
- Candle live dikira di server dan dihantar setiap saat tanpa menghantar candle
  masa depan. Ia bergerak menuju candle lengkap yang sama selepas refresh.
  Jika sambungan terganggu, paparan mengekalkan nilai terakhir dan cuba semula.
  Jika Close jatuh di tengah minit, carta menambah candle separa menggunakan
  penutup kewangan sedia ada tanpa mengubah candle lengkap sebelumnya. Pengiraan
  baki dan untung/rugi akhir tidak berubah; bentuk carta sesi lama yang tamat
  di tengah minit boleh berbeza.
- Harga minimum candle kekal RM1 atau 5% Entry (mana lebih tinggi). Jika Close
  admin terlalu rendah, harga minimum ini boleh mengatasi toleransi Close.
- Carta boleh ditukar antara candle dan garisan bergradien ungu ke biru; garisan memaparkan nilai
  candle lengkap terakhir dalam setiap tempoh yang dipilih dan dikemas kini
  apabila candle satu minit lengkap diterima. Header dan untung/rugi live
  masih mengikuti candle live. Pilihan paparan tidak mengubah kiraan kewangan.
- Ketika janaan carta terlalu sibuk, request boleh menerima HTTP 503 sementara
  dan carta akan cuba lagi secara automatik. Semak amaran `Candle generation
  saturated` dalam log Render dan kapasiti memori/CPU jika kerap berlaku.
- PostgreSQL menggunakan pemeriksaan sambungan sebelum digunakan semula serta
  timeout; Redis juga mempunyai timeout. Jika storan rate limiter gagal,
  request yang terlibat menerima HTTP 503 dan kegagalan direkodkan, bukan
  melangkau had keselamatan.
- Senarai pengguna, pendaftaran, pengeluaran dan resit menggunakan pagination
  database. Dalam borang penciptaan atau kemas kini akaun, cari rekod bank dan
  gunakan butang halaman untuk memilih pendaftaran yang betul.

- Web Service menggunakan `DATABASE_URL` daripada pangkalan data yang betul, bukan SQLite fallback.
- Pangkalan data dan Web Service berada di region yang sama supaya sambungan dalaman digunakan.
- Status pangkalan data tersedia dan aplikasi berjaya melepasi health check.

Semak tab **Backups** pada halaman database Render untuk mengetahui sandaran
automatik dan pilihan pemulihan yang tersedia bagi database/plan anda. Jangan
anggap sandaran automatik tersedia tanpa menyemak tetapan akaun. Untuk salinan
tambahan, jalankan `pg_dump` dari persekitaran dipercayai yang mempunyai
`DATABASE_URL` dan PostgreSQL client tools:

```powershell
pg_dump $env:DATABASE_URL --format=custom --file=tradenex-backup.dump
```

Simpan fail sandaran dalam storan sulit di luar repository. Uji pemulihan secara
berkala ke database ujian yang berasingan—jangan restore terus ke production.
Selepas restore, pastikan `/health` berjaya dan rekod yang dijangka wujud.
Jangan masukkan `DATABASE_URL`, fail sandaran atau maklumat pelanggan ke Git.

## 9. Troubleshooting

| Masalah | Penyelesaian |
|---|---|
| `flask init-db` error "No such command" | Pastikan `FLASK_APP=app.py` (biasanya auto-detect sebab nama fail `app.py`) |
| Login user sentiasa "ID tidak sah" | Pastikan admin sudah cipta akaun user tersebut di panel admin |
| Graf tidak muncul | Pastikan admin sudah tambah sekurang-kurangnya 1 entry untuk user itu |
| Render deploy atau health check gagal | Semak build/deploy logs, `DATABASE_URL`, dan sama ada `/health` boleh mencapai PostgreSQL |
| Cron job Render tidak berjalan | Semak status job, jadual UTC dalam `render.yaml` dan log job untuk `paid_count` |
