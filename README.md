# IDX Signal Parser

Dashboard trade journal untuk sinyal saham IDX — parsing export Telegram (JSON) dan dashboard hasil (XLSX), digabung jadi satu jurnal trading dengan sinkronisasi cloud (Supabase).

**Live app:** _(isi setelah deploy, misal `https://idx-signal-parser.pages.dev`)_

## Fitur
- Parsing export Telegram (`result.json`) → sinyal, hasil (TP HIT/LOCKED/RUNNING), market regime, pesan lain
- Merge dengan dashboard XLSX → melengkapi status final (TP HIT / **SL HIT** / **EXPIRED**) yang tidak diposting publik di Telegram
- Login & sinkronisasi cloud lintas device (Supabase)
- Cache lokal (IndexedDB) sebagai fallback offline
- Export ke Excel/CSV

## Struktur file
```
.
├── index.html   # seluruh aplikasi (HTML+CSS+JS dalam 1 file, tanpa build step)
└── README.md
```

## Menjalankan lokal
Tidak perlu build tool apa pun — cukup buka `index.html` langsung di browser, atau serve dengan:
```bash
python3 -m http.server 8000
```
lalu buka `http://localhost:8000`.

## Konfigurasi Supabase
URL project dan anon key ada langsung di `index.html` (aman untuk sisi client, karena akses data dilindungi Row Level Security — lihat `HOSTING_GUIDE.md`). Untuk pindah ke project Supabase lain, cari dan ganti:
```js
const SUPABASE_URL = '...';
const SUPABASE_ANON_KEY = '...';
```

## Deploy
Lihat `HOSTING_GUIDE.md` untuk panduan lengkap deploy ke GitHub + Cloudflare Pages.
