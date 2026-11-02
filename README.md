# osas — Muse bridge untuk 9Router

Bridge kecil berbasis Python (standard library, tanpa paket tambahan) untuk
mengantrekan chat dari 9Router dan menerima jawaban dari agent di Muse.

```text
Client → 9Router → bridge HTTP → antrean SQLite
                                  ↑         ↓
                              jawaban    claim job
                                  ↑         ↓
                              agent Muse + worker.py
```

**Status:** alur HTTP, helper worker, backend Muse2API, dan konfigurasi proxy
diuji lokal dengan jawaban sintetis.
Integrasi dengan akun Muse asli, pemakaian bonus 1B token, dan konfigurasi
9Router pengguna belum diuji. Proyek ini tidak mengasumsikan adanya API resmi
Muse. Mode default `queue` memakai `worker.py` untuk memindahkan pesan; agent
Muse yang menghasilkan jawaban. Mode opsional `muse2api` memanggil adapter
komunitas terpisah yang mengelola login/session Muse dan proxy keluar ke Muse.

**Untuk bridge di VPS berbeda dari 9Router, dengan proxy opsional ke Muse**, ikuti
[panduan mode Muse2API](deploy/REMOTE_MUSE.md). Dalam mode ini tidak perlu worker
atau scheduled task di Muse. Proxy HTTP/SOCKS5 diatur pada akun adapter.

## Jalankan bridge

Butuh Python **3.10+** dengan SQLite. Jalankan dari root checkout:

```bash
cd /workspace/osas  # sesuaikan jika disalin ke Muse/VPS lain
python3 scripts/init_env.py
set -a
. ./.env
set +a
python3 bridge.py serve
```

`init_env.py` membuat `.env` privat (mode 600) berisi dua kunci acak berbeda.
Perintah ini sengaja menolak menimpa `.env` yang sudah ada. Saat restart cukup
muat `.env` dan jalankan server; jangan generate ulang kunci.

Health check di terminal lain:

```bash
curl --fail http://127.0.0.1:8765/health
```

Hasil `{"ok": true, ...}` hanya membuktikan server aktif, bukan Muse terhubung.
Data runtime di `.local/` dan `.env` diabaikan Git. Jangan commit atau membagikan
file tersebut. Tidak perlu menaruh cookie akun Muse di bridge.

## Hubungkan 9Router

Di dashboard 9Router, tambahkan provider **OpenAI-compatible / custom**:

| Field | Nilai |
| --- | --- |
| Nama | Muse Bridge |
| Base URL | `http://127.0.0.1:8765/v1` jika satu mesin; `https://DOMAIN_BRIDGE/v1` jika beda VPS |
| API key | Nilai `BRIDGE_USER_KEY` dari `.env`, masukkan secara privat |
| Model ID upstream | `muse` |
| Timeout | Lebih dari 240 detik, jika versinya menyediakan pengaturan ini |

Nama menu/alias publik dapat berbeda antar versi 9Router. Pilih ID/alias model
yang ditampilkan 9Router saat memanggil endpoint milik 9Router. Endpoint bridge
sendiri selalu menerima ID `muse`. Gunakan API key **9Router** untuk mengakses
9Router, bukan worker key.

Bridge dan 9Router boleh berada di VPS yang sama atau terpisah. `127.0.0.1` selalu
merujuk ke mesin/container pemanggil. Jika Muse/9Router berada di mesin lain,
siapkan URL HTTPS bridge yang dapat dijangkau melalui reverse proxy milik lo;
jangan arahkan ke loopback mesin berbeda. Server ini default-nya hanya bind
loopback dan bukan server publik yang diperkeras. Gunakan HTTPS, batas koneksi,
batas ukuran request, dan rate limit di reverse proxy untuk akses jarak jauh.
Atur timeout reverse proxy/client lebih panjang dari `BRIDGE_TIMEOUT`.
Koneksi publik/tunnel belum disiapkan otomatis oleh proyek ini.

Untuk **Ubuntu 22 + 9Router via npm**, lihat
[panduan VPS dan systemd](deploy/UBUNTU22.md). Installer membuat kunci baru di
VPS; paket release tidak membawa `.env` atau database dari workspace ini.

## Pasang worker di Muse

Bagian ini hanya untuk `BRIDGE_BACKEND=queue` (default). Pada backend `muse2api`,
gunakan panduan REMOTE_MUSE di atas; endpoint worker nonaktif.

1. Salin `worker.py` dan [MUSE_WORKER.md](MUSE_WORKER.md) ke lingkungan Muse.
2. Siapkan `BRIDGE_URL` dan **hanya** `BRIDGE_WORKER_KEY` di environment worker
   secara privat. Gunakan loopback jika bridge satu mesin, HTTPS jika terpisah.
3. Berikan instruksi dari `MUSE_WORKER.md` ke agent Muse; uji sekali sebelum
   memasangnya sebagai scheduled task setiap menit. Jadwal ini membangunkan
   **agent Muse**, bukan cuma menjalankan Python berulang kali.
4. Saat request menunggu, Muse menjalankan `claim`, membaca pesan, menghasilkan
   jawaban, lalu menjalankan `answer`. Tanpa agent aktif, request berakhir 504.

Contoh perintah yang dijalankan agent (working directory berisi `worker.py`):

```bash
umask 077
python3 worker.py claim --output /tmp/muse-job-UNIQUE.json --wait 20
# Jika ada job: agent baca file, lalu tulis jawaban ke file privat.
python3 worker.py answer --job-file /tmp/muse-job-UNIQUE.json --text-file /tmp/muse-answer-UNIQUE.txt
```

Ganti `UNIQUE` untuk setiap invocation. Helper menghapus file job setelah
pengiriman berhasil; agent harus menghapus file jawaban setelah selesai.
Jangan menampilkan file job atau kunci pada chat/log publik.

Worker juga punya opsi **proxy untuk koneksi worker → bridge**, terpisah dari
proxy adapter → Muse. Lihat `worker.env.example`: set `BRIDGE_PROXY_URL` secara
privat untuk HTTP CONNECT dengan autentikasi opsional. Tidak diisi berarti
mewarisi proxy sistem dan `NO_PROXY`, bukan menonaktifkan proxy platform. Opsi
ini tidak merutekan request layanan Muse. SOCKS5 tersedia pada adapter Muse2API,
tetapi bukan pada helper worker stdlib. URL proxy berisi password jangan ditaruh
di argumen CLI atau log. Variabel alternatif dapat dipilih lewat `--proxy-env`.

## Coba request

Di terminal terpisah dari server, dari root checkout:

```bash
set -a
. ./.env
set +a
python3 - <<'PY'
import json, os, urllib.request
request = urllib.request.Request(
    'http://127.0.0.1:8765/v1/chat/completions',
    json.dumps({'model': 'muse', 'messages': [
        {'role': 'user', 'content': 'Jawab singkat: 2 + 2 berapa?'}]}).encode(),
    {'Content-Type': 'application/json',
     'Authorization': 'Bearer ' + os.environ['BRIDGE_USER_KEY']})
with urllib.request.urlopen(request, timeout=260) as response:
    print(json.load(response)['choices'][0]['message']['content'])
PY
```

Request menunggu hingga worker Muse mengirim jawaban. Untuk menguji lewat
9Router, ganti URL dengan endpoint 9Router, kunci dengan API key 9Router, dan
model dengan alias yang dibuat di dashboard. Jangan gunakan jawaban sintetis
sebagai bukti bahwa akun Muse sudah tersambung.

## Dukungan dan batasan

- `GET /health`, `GET /v1/models`, `POST /v1/chat/completions`.
- Worker: `POST /worker/claim`, `POST /worker/answer`, bearer key terpisah.
- Chat teks, histori multi-pesan, satu jawaban (`n=1`).
- `stream=true` menghasilkan **SSE setelah jawaban lengkap tersedia**. Tidak ada
  token live atau heartbeat selama menunggu. Client dengan idle timeout pendek
  bisa putus; gunakan non-streaming untuk uji pertama.
- Belum mendukung tools/function calling, gambar, Responses API, embeddings,
  structured output, atau pelaporan usage token. Cocok untuk chat teks awal;
  belum cukup untuk agent coding yang membutuhkan tool calls.
- Parameter temperature/token limit/stop diteruskan sebagai petunjuk ke worker;
  tidak ada jaminan Muse dapat menerapkannya secara persis.
- `muse` adalah alias bridge, bukan bukti identitas model dasar yang dipakai Muse.
- Queue SQLite persisten, claim atomik, lease dengan token, penolakan jawaban
  ganda/kedaluwarsa, kapasitas default 32 request. Restart server memutus request
  HTTP yang masih terbuka; client perlu retry. Tidak ada exactly-once inference.
- Pada `BRIDGE_BACKEND=muse2api`, antrean worker tidak dipakai. Konkurensi dibatasi
  `BRIDGE_MAX_PENDING`; installer mode ini memilih 1. Upstream key terpisah dari
  user key 9Router. Adapter bisa tetap menghasilkan jawaban setelah client
  timeout; periksa statusnya sebelum retry untuk menghindari pemakaian ganda.
- Request timeout default 240 detik; lease default 300 detik dibatasi deadline
  request. Tidak ada perpanjangan lease. Jadwal terlalu lambat/jawaban terlalu
  lama dapat menghabiskan timeout. Jangan aktifkan retry agresif karena dapat
  mengulang pekerjaan dan mengonsumsi kuota.
- Prompt/hasil dihapus secara logis setelah hasil diambil atau request timeout.
  Record lama dibersihkan saat enqueue/claim; SQLite/WAL dan backup bukan
  penyimpanan terenkripsi atau secure erase. File jawaban Muse perlu dibersihkan.
- Besar request maksimum 1 MiB; tidak ada key atau isi pesan di access log.

Pengaturan tersedia di `.env.example`. Server membaca environment, bukan memuat
`.env` sendiri. Tidak ada koneksi ke Muse sampai worker benar-benar dijalankan.

## Tes

```bash
python3 -m unittest discover -s tests -v
```

Tes menjalankan HTTP server loopback, adapter sintetis, dan proxy CONNECT lokal.
Mencakup auth, SSE, timeout, lease kedaluwarsa, reopen database, claim serentak,
kapasitas antrean, pemetaan upstream model/key, helper proxy akun, dan penolakan
fitur yang belum didukung. Tes proxy TLS membutuhkan executable `openssl` untuk
sertifikat sementara; tanpa OpenSSL, empat tes itu ditandai skipped. Tidak
membutuhkan akun Muse atau koneksi Internet.

Desain antrean terinspirasi dokumentasi publik
[Muse-on-9Router-Hermes-Agent-ai](https://github.com/Kutuyyy/Muse-on-9Router-Hermes-Agent-ai).
Implementasi ini menggunakan endpoint worker sendiri; jangan mencampur helper
dari repo lain. Panduan router: [decolua/9router](https://github.com/decolua/9router).
