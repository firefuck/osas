# VPS bridge terpisah + proxy keluar ke Muse

```text
VPS A: 9Router
    │ HTTPS + BRIDGE_USER_KEY
    ▼
VPS B: reverse proxy → osas bridge (backend=muse2api)
                           │ loopback + MUSE_UPSTREAM_KEY
                           ▼
                      Muse2API + Chromium
                           │ proxy HTTP/SOCKS5 opsional per akun
                           ▼
                        layanan Muse
```

**Proxy di bagian ini adalah koneksi adapter ke layanan Muse**, sesuai kebutuhan
lo. Ini berbeda dari reverse proxy HTTPS dan opsi proxy worker. Dalam mode ini
tidak perlu scheduled worker di Muse. Session/login Muse disimpan privat di VPS B
oleh adapter. API key osas di 9Router adalah kunci akses bridge, bukan API key
resmi Muse atau konversi langsung dari bonus token.

## Status yang sudah dan belum diverifikasi

- Mode backend, autentikasi terpisah, pemetaan model, penanganan error, dan
  helper pengaturan proxy diuji dengan adapter lokal sintetis.
- Adapter nyata yang dirujuk adalah
  [czg86389-hub/muse2api](https://github.com/czg86389-hub/muse2api), dipatok pada
  commit `1cd01682533dbf2c015fa879a8dc09534b50b4a3`. README dan kontrak PATCH
  proxy diperiksa dari source commit itu. Ini proyek komunitas, bukan API resmi.
- Browser, build Docker, login/session Muse asli, proxy milik pengguna, kuota 1B,
  dan koneksi 9Router pengguna **belum diuji**. Langkah adapter di bawah adalah
  petunjuk deployment, bukan laporan bahwa sudah terpasang.
- Bridge mendukung text + function tools, Responses API, dan buffered SSE.
  Default tools memakai emulasi yang divalidasi; tool dieksekusi client.
  Lihat [TOOL_CALLING.md](../TOOL_CALLING.md). Token usage belum tersedia.
  Upstream mengimplementasikan koneksi Muse; osas tidak menebak endpoint internal Muse.
- Default policy best-effort meneruskan replay reasoning dan token budget meski
  belum terverifikasi. Adapter web ini tetap dilaporkan unknown/unsupported;
  upstream dapat mengabaikannya. Mode strict mengembalikan guard 422. Lihat
  [MODEL_CONTRACT.md](../MODEL_CONTRACT.md), bukan mengisi context 1M berdasarkan alias.

## 1. Pasang bridge di VPS B

Ikuti transfer arsip dan checksum di [UBUNTU22.md](UBUNTU22.md). Pada instalasi
baru, gunakan pilihan backend berikut dari root release yang diekstrak:

```bash
sudo bash deploy/install-ubuntu.sh --backend muse2api
```

Installer membuat kunci baru untuk client dan adapter di
`/etc/osas-bridge/bridge.env`, mengatur model upstream `muse-spark`, serta
concurrency 1. Service bridge boleh hidup sebelum adapter; request akan gagal
502 sampai adapter dan akun Muse benar-benar siap. Health tetap hanya mengecek
proses bridge.

Jika bridge versi sebelumnya sudah terpasang, installer sengaja menolak overwrite.
Ikuti prosedur upgrade dan dependencies baru di TOOL_CALLING.md; jangan hanya
mengganti bridge.py karena ada modul protokol dan venv baru.
Jangan mengganti user key yang sudah dipasang di 9Router tanpa kebutuhan.

## 2. Siapkan adapter Muse2API di VPS B

Contoh ini memerlukan **Docker + Compose plugin** di VPS B. 9Router boleh tetap
berjalan via npm di VPS A. Jika Docker belum ada, gunakan petunjuk instalasi
resmi Docker untuk Ubuntu sebelum langkah ini. Jangan mengubah instalasi
9Router. Chromium dan browser state membutuhkan memori tambahan; cek RAM/disk
dan mulai dengan concurrency 1.

Jalankan dari root release osas di VPS B (jangan overwrite folder yang sudah ada):

```bash
git clone https://github.com/czg86389-hub/muse2api.git deploy/muse2api-src
git -C deploy/muse2api-src checkout --detach 1cd01682533dbf2c015fa879a8dc09534b50b4a3
sudo docker compose --env-file /etc/osas-bridge/bridge.env -f deploy/muse2api.compose.yml up -d --build
```

Compose menggunakan source yang dipatok, bukan image `latest` yang sudah dibangun
pihak lain. Base image dan requirements upstream belum dikunci seluruhnya;
build lintas waktu belum dijamin identik. Jangan menjalankan
`docker compose config` di output yang dibagikan karena hasilnya dapat memuat
kunci yang sudah diinterpolasi.

Adapter hanya dibuka pada `127.0.0.1:18610`. Data akun/proxy disimpan di volume
Docker `muse_state`. Jangan expose port adapter, CDP, atau panel admin ke Internet.
`MUSE_UPSTREAM_KEY` berbeda dari `BRIDGE_USER_KEY` agar user 9Router tidak punya
akses ke administrasi/session Muse. Jangan merotasi kunci adapter dari panel
tanpa memperbarui konfigurasi bridge dan Compose.

## 3. Login Muse dan pilih proxy opsional

Buka panel adapter melalui SSH forwarding privat dari komputer lo:

```bash
ssh -N -L 18610:127.0.0.1:18610 USER@VPS_B
```

Lalu buka `http://127.0.0.1:18610/admin` di browser lokal. Login panel dengan
adapter key secara privat, kemudian ikuti mekanisme import session milik adapter
untuk **akun Muse lo sendiri**. Jangan kirim cookie, session token, atau proxy
password lewat chat. File/data akun dan backup volume perlu dilindungi.

Di menu akun adapter:

1. Pilih akun Muse, buka **Proxy**.
2. Pilih **Use a proxy** bila diperlukan, isi server `http://HOST:PORT`,
   `socks5://HOST:PORT`, atau `socks5h://HOST:PORT`; username/password di kolom
   terpisah. Tanpa proxy, biarkan default network.
3. Simpan lalu pilih **Test saved proxy**. Cek konektivitas dan session Muse
   secara terpisah, lalu uji satu chat sungguhan. Test proxy dapat memperbarui
   status pemeriksaan akun; tidak melakukan content generation.

Per dokumentasi adapter pada commit yang dipatok, proxy akun mencakup browser
dan request session Muse. Proxy gagal dilaporkan sebagai error dan tidak sengaja
fallback ke direct. HTTP Basic dan SOCKS5 username/password didukung oleh adapter;
TLS-to-proxy (`https://proxy...`), VLESS/VMess, dan subscription URL tidak didukung
langsung. Proxy harus bisa dijangkau dari VPS B, bukan hanya dari komputer lo.

### Alternatif helper CLI tanpa mencetak kredensial

Salin `deploy/proxy.example.json` ke file privat di VPS B, misalnya
`/root/muse-proxy.private.json`, mode 600, lalu edit server/username/password
secara privat. Dari root release:

```bash
sudo bash
set -a
. /etc/osas-bridge/bridge.env
set +a
python3 scripts/configure_muse_proxy.py --account ACCOUNT_ID --proxy-file /root/muse-proxy.private.json
python3 scripts/configure_muse_proxy.py --account ACCOUNT_ID --test
exit
```

`ACCOUNT_ID` diambil dari panel akun. Helper memanggil PATCH pada adapter lokal
dan tidak menyimpan password di config bridge. File privat yang lo buat perlu
dibersihkan setelah tidak dibutuhkan. `--test` menampilkan dua flag boolean dan
exit nonzero bila konektivitas/session tidak lolos. Untuk kembali ke jaringan
default, jalankan helper dengan `--remove` sebagai pengganti `--proxy-file`.

## 4. HTTPS dan konfigurasi 9Router di VPS A

Gunakan subdomain di VPS B dengan sertifikat valid, contoh
`https://muse.example.com`. Contoh Nginx/Caddy di folder ini sekarang membuka
`/v1/models`, Chat Completions, Responses (termasuk retrieve/delete), capabilities,
plus worker/health. `/v1/*` memerlukan user
key; `/worker/*` memerlukan worker key dan akan ditolak saat backend=muse2api.
Panel admin adapter tidak diteruskan.

| Field provider OpenAI-compatible 9Router | Nilai |
| --- | --- |
| Base URL | `https://muse.example.com/v1` |
| API key | `BRIDGE_USER_KEY` dari config VPS B |
| Model upstream | `muse` |
| Timeout | Lebih dari 240 detik jika versi 9Router mendukungnya |

Client yang memanggil 9Router tetap memakai key dan alias dari 9Router. Gunakan
HTTPS langsung ke reverse proxy VPS B; CDN/edge proxy dapat punya timeout lebih
pendek daripada bridge walaupun timeout Nginx/Caddy 270 detik. Untuk uji awal,
hindari jalur edge yang memutus request panjang atau sesuaikan timeout terhadap
batas jalur tersebut. Tidak ada sertifikat, DNS, atau akses VPS yang diterapkan
oleh arsip ini.
