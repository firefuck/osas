# osas — Muse bridge untuk 9Router dan client OpenAI

Bridge untuk akun Muse milik lo, dengan Chat Completions, Responses API, dan
siklus function/tool calling. Bridge bisa dipasang di VPS terpisah dari 9Router.

**Status:** protokol tool/agent, streaming, dan SDK OpenAI diuji menggunakan
backend sintetis lokal. Login Muse asli, kualitas emulasi model, kompatibilitas
9Router/Codex pada deployment pengguna, dan pemakaian bonus 1B token belum diuji.
Ini bukan API resmi Muse dan bukan implementasi seluruh produk OpenAI.

```text
Client agent / 9Router → HTTPS + user key → osas bridge
                                              │
                         ┌────────────────────┴────────────────────┐
                         ▼                                         ▼
                backend=muse2api                            backend=queue
                Muse2API + Chromium                          worker di Muse
                proxy HTTP/SOCKS5 opsional                    mengambil job
                         │                                         │
                         └────────────────── Muse ─────────────────┘
```

## Fitur API

- Chat: `/v1/chat/completions`, `/v1/models`, `/v1/models/muse`.
- Responses: `/v1/responses`, retrieve/delete `/v1/responses/{id}`,
  `previous_response_id`, atau replay riwayat tanpa penyimpanan server.
- `tools` function, `tool_choice` auto/none/required/named, beberapa call,
  `parallel_tool_calls`, hasil `role=tool`, serta input legacy `functions`.
- JSON Schema untuk argumen dan structured output (`json_object`/`json_schema`).
- SSE Chat dan lifecycle Responses, termasuk delta argumen function.
  **Output masih buffered:** SSE dikirim setelah generasi lengkap, bukan token live.
- Mode default `BRIDGE_TOOL_MODE=emulate`: kontrak prompt dibungkus envelope unik,
  diterjemahkan menjadi tool calls, dan divalidasi. Output yang tidak sesuai
  menghasilkan 502; tidak dianggap tool call yang boleh dieksekusi.
- Mode `native` tersedia untuk upstream yang benar-benar sudah diverifikasi
  mendukung protokol tools. Belum diverifikasi pada Muse. `disabled` menonaktifkan
  tools/structured output.
- `/v1/capabilities` menjelaskan kemampuan dan batasan aktual.

**Tool dieksekusi oleh client agent**, bukan oleh server bridge. Function seperti
baca file, edit file, terminal, atau browser harus disediakan oleh client yang
menggunakan API. Bridge tidak memberi model akses shell otomatis di VPS.
Lihat [panduan lengkap tool calling](TOOL_CALLING.md) dan contoh agent siap jalan.

## Jalankan lokal

Python **3.10+**, SQLite, dan dependencies di `requirements.txt`:

```bash
cd /workspace/osas  # sesuaikan lokasi checkout
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
python3 scripts/init_env.py
set -a
. ./.env
set +a
.venv/bin/python bridge.py serve
```

`init_env.py` membuat kunci acak di `.env` (mode 600), menolak overwrite.
Untuk backend adapter langsung pada instalasi baru gunakan
`python3 scripts/init_env.py --backend muse2api` sebagai pengganti perintah init.
Saat restart, cukup muat `.env` dan jalankan server lagi. Jangan regenerate key.
Server membaca environment, tidak memuat `.env` sendiri.

`GET /health` mengembalikan `ok=true` bila server aktif; itu tidak membuktikan
koneksi Muse. Default bind `127.0.0.1:8765`, bukan server HTTP publik yang
sudah diperkeras. Gunakan reverse proxy HTTPS untuk akses dari mesin berbeda.

## Deploy VPS dan login Muse

- [Ubuntu 22 + 9Router npm + systemd](deploy/UBUNTU22.md).
- [Bridge di VPS terpisah + Muse2API + proxy keluar ke Muse](deploy/REMOTE_MUSE.md).

Adapter Muse2API memakai Chromium headless. Login dilakukan di browser lo dan
session diimpor secara privat ke adapter; `osas` tidak punya form password Muse.
Adapter memiliki key administrasi yang terpisah dari key client bridge.
Jangan kirim cookie, session, atau key ke chat. Jangan expose panel admin/CDP.

Untuk backend `queue`, ikuti [instruksi worker Muse](MUSE_WORKER.md).
`worker.py` sendiri tetap standard-library-only. Ia memindahkan pesan, bukan
menghasilkan jawaban. Proxy `BRIDGE_PROXY_URL` pada worker hanya merutekan
**worker → bridge**, berbeda dari proxy **adapter → Muse**. Lihat
`worker.env.example` untuk opsi HTTP CONNECT. Secara default proxy sistem dan
trust TLS diwarisi; tidak ada opsi mematikan verifikasi sertifikat.

## Isi provider 9Router

| Pengaturan | Nilai |
| --- | --- |
| Tipe | OpenAI-compatible / custom |
| Base URL, satu mesin | `http://127.0.0.1:8765/v1` |
| Base URL, beda VPS | `https://DOMAIN_BRIDGE/v1` |
| API key provider | `BRIDGE_USER_KEY` |
| Model upstream | `muse` |
| Timeout | Lebih dari 240 detik, bila tersedia |

Client yang menghubungi 9Router menggunakan key dan alias model dari dashboard
9Router. `127.0.0.1` selalu berarti mesin/container pemanggil. Contoh Nginx/Caddy
membuka endpoint API yang diperlukan, tanpa meneruskan panel admin adapter.

## Penyimpanan dan batasan

- Tools yang didukung adalah **function tools**, bukan hosted web search, computer
  use, code interpreter, custom grammar tools, atau MCP otomatis. Client dapat
  mengimplementasikan fungsi tersebut sendiri dan mendaftarkannya sebagai tool.
- Belum ada input gambar/audio, embeddings, speech, fine-tuning, atau upload file
  ala OpenAI. Unsupported modality/tool type ditolak, bukan disimulasikan.
- JSON Schema memakai Draft 2020-12. `$ref` lokal/`$defs`, kombinator, required,
  additionalProperties, dan format yang didukung validator diperiksa. Remote refs dan resource `$id`
  ditolak agar schema tidak mengakses jaringan; gunakan schema self-contained.
- Tidak ada token count/usage palsu. Usage saat ini tidak tersedia (`null` pada
  Responses dan chunk include_usage); alias `muse` bukan bukti nama model dasar.
- Temperature/token limits diteruskan sebagai petunjuk; penerapan persis oleh
  Muse belum dijamin. Tidak ada automatic retry/repair yang diam-diam memakai kuota.
- Responses menyimpan histori secara default untuk chaining. Gunakan `store:false`
  untuk opt-out dan kirim ulang seluruh riwayat tool. TTL default 1 jam, maksimum
  128 respons tersimpan, lalu yang tertua dievikt. Hanya output yang lengkap dan
  tervalidasi yang disimpan. GET/DELETE tetap memerlukan user key yang sama.
  Record kedaluwarsa dibersihkan saat akses get/save, bukan lewat daemon periodik.
- `.env`, SQLite/WAL di `.local/`, dan volume akun adapter bersifat privat dan
  diabaikan Git. SQLite tidak terenkripsi; TTL/delete bukan secure erase.
  Database response berada di sebelah `BRIDGE_DB`, bernama `responses.sqlite3`.
- Queue worker memakai lease atomik dan menolak jawaban ganda/kedaluwarsa. Restart
  memutus request HTTP yang masih terbuka. Adapter bisa tetap bekerja setelah
  client putus; periksa status sebelum retry agar tidak mengonsumsi kuota ganda.
- Max body 1 MiB, 256 pesan, 128 function tools. Request timeout 240 detik.
  Reverse proxy, CDN dan client harus menoleransi waktu tunggu tanpa token live.

## Validasi

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

Tes menguji alur HTTP, tool loop, read/write file lokal dari client contoh,
JSON Schema, Responses chaining/TTL, streaming, SDK OpenAI, lease, dan proxy
CONNECT lokal. Tidak memakai akun Muse atau Internet saat tes. OpenSSL diperlukan
untuk empat tes TLS; SDK test memerlukan `requirements-dev.txt`. Skipped bukan passed.

Arsip source-only dibuat dengan `scripts/package_release.py`; manifest eksplisit
mengecualikan `.env`, database, venv, dan source/cache adapter.

Referensi komunitas: [Muse worker bridge](https://github.com/Kutuyyy/Muse-on-9Router-Hermes-Agent-ai)
dan [Muse2API](https://github.com/czg86389-hub/muse2api). Source adapter tidak
vendored di repo ini; commit yang digunakan dijelaskan di panduan deployment.
