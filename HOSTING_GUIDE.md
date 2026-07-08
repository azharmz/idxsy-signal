# Panduan Hosting: GitHub + Cloudflare Pages

Aplikasi ini cuma 1 file statis (`index.html`, HTML+CSS+JS jadi satu, tanpa build step). Jadi proses deploy-nya sangat sederhana — tidak perlu `npm install`, tidak perlu build command.

---

## Bagian 1 — Upload ke GitHub

### 1. Bikin repo baru
1. Buka [github.com/new](https://github.com/new)
2. Nama repo bebas, misal `idx-signal-parser`
3. Set **Private** kalau tidak mau file/kode-nya kelihatan publik (rekomendasi, karena ada konteks trading personal di dalamnya — meskipun kredensial Supabase aman berkat RLS, tetap lebih baik privat)
4. Jangan centang "Add README" (kita sudah punya sendiri)
5. Klik **Create repository**

### 2. Upload file
**Cara termudah (tanpa command line):** di halaman repo yang baru dibuat, klik **uploading an existing file**, drag & drop `index.html` dan `README.md`, lalu klik **Commit changes**.

**Cara pakai Git (kalau sudah terbiasa):**
```bash
git init
git add index.html README.md
git commit -m "Initial commit: IDX signal parser"
git branch -M main
git remote add origin https://github.com/USERNAME/idx-signal-parser.git
git push -u origin main
```

Pastikan nama filenya persis **`index.html`** (huruf kecil semua) — ini penting supaya nanti otomatis ke-load sebagai halaman utama tanpa perlu ketik nama file di URL.

---

## Bagian 2 — Deploy ke Cloudflare Pages

### 1. Connect repo
1. Buka [dash.cloudflare.com](https://dash.cloudflare.com) → login/daftar (gratis)
2. Sidebar kiri → **Workers & Pages** → **Create** → tab **Pages** → **Connect to Git**
3. Authorize Cloudflare ke akun GitHub kamu (kalau pertama kali), pilih repo `idx-signal-parser`
4. Klik **Begin setup**

### 2. Build settings
Karena tidak ada build step, isi seperti ini:

| Field | Isi |
|---|---|
| Production branch | `main` |
| Framework preset | **None** |
| Build command | *(kosongkan)* |
| Build output directory | `/` |

Klik **Save and Deploy**.

### 3. Selesai
Setelah build selesai (biasanya <1 menit karena tidak ada compile apa pun), kamu dapat URL:
```
https://idx-signal-parser-xxx.pages.dev
```
Buka URL itu — dashboard harusnya langsung muncul persis seperti versi lokal.

Tiap kali kamu push perubahan baru ke branch `main` di GitHub, Cloudflare Pages otomatis re-deploy tanpa perlu langkah manual apa pun.

---

## Bagian 3 — WAJIB: update konfigurasi Supabase

Supabase Auth membatasi ke URL mana saja proses login/konfirmasi email boleh redirect. Kalau langkah ini dilewat, pendaftaran akun baru (dan reset password) bisa gagal begitu diakses dari URL production.

1. Buka project Supabase kamu → **Authentication** → **URL Configuration**
2. **Site URL**: isi dengan URL Cloudflare Pages kamu, misal `https://idx-signal-parser-xxx.pages.dev`
3. **Redirect URLs**: tambahkan URL yang sama (dan `http://localhost:8000/*` juga kalau masih suka testing lokal)
4. Klik **Save**

---

## Opsional — Custom domain

Kalau punya domain sendiri:
1. Di project Cloudflare Pages kamu → tab **Custom domains** → **Set up a custom domain**
2. Ikuti instruksi (kalau domain sudah ada di Cloudflare, tinggal 1 klik; kalau di registrar lain, perlu tambah CNAME record)
3. Jangan lupa update lagi **Site URL**/**Redirect URLs** di Supabase pakai domain baru ini

---

## Troubleshooting singkat

| Gejala | Kemungkinan penyebab |
|---|---|
| Halaman blank / 404 | Nama file bukan `index.html`, atau build output directory salah (harus `/`, bukan `/public` dsb) |
| Badge sync selalu merah di production | Belum update **Redirect URLs** di Supabase (lihat Bagian 3) |
| Login/daftar tidak bisa, redirect ke halaman aneh | Sama seperti di atas — cek Site URL Supabase |
| Perubahan kode tidak muncul setelah push | Cek tab **Deployments** di Cloudflare Pages — build mungkin gagal, klik untuk lihat log |
