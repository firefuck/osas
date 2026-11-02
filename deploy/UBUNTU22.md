# Ubuntu 22.04 — 9Router via npm

Bridge bisa berjalan pada VPS yang sama dengan 9Router (melalui loopback), atau
VPS terpisah (melalui HTTPS + user key). Tidak perlu mengubah source 9Router
atau menambahkan paket npm untuk bridge.

Untuk **koneksi adapter ke Muse dengan proxy HTTP/SOCKS5 opsional**, gunakan
[REMOTE_MUSE.md](REMOTE_MUSE.md) dan pilihan installer `--backend muse2api`.
Langkah default di bawah memakai backend `queue`, dengan worker di Muse.

Paket dan installer ini belum dijalankan di VPS pengguna. Local staging bukan
bukti systemd, DNS, HTTPS, atau Muse sudah terpasang dan bekerja di VPS tersebut.

## Transfer dan pasang

Gunakan arsip release dan checksum yang dibuat oleh
`python3 scripts/package_release.py --output /path/osas-bridge-ubuntu22.tar.gz`.
Jangan mengarsipkan seluruh checkout karena berisi `.env` dan database privat.
Repo GitHub awalnya kosong; jangan mengandalkan `git clone` untuk mengambil kode
ini sebelum kode benar-benar di-push ke remote.

Salin kedua file ke VPS dengan metode transfer privat yang biasa lo pakai.
Contoh dari komputer yang menyimpan arsip (ganti `USER` dan `VPS_HOST`):

```bash
scp osas-bridge-ubuntu22.tar.gz osas-bridge-ubuntu22.tar.gz.sha256 USER@VPS_HOST:~/
```

Di VPS:

```bash
sha256sum -c osas-bridge-ubuntu22.tar.gz.sha256
# Pilih folder kosong/new; jangan extract di atas source milik lo yang lain.
mkdir osas-bridge-release
tar -xzf osas-bridge-ubuntu22.tar.gz -C osas-bridge-release
cd osas-bridge-release/osas
python3 -m unittest discover -s tests -v
sudo bash deploy/install-ubuntu.sh
```

Ubuntu 22.04 biasanya menyediakan `/usr/bin/python3` versi 3.10. Jika belum ada,
install `python3` melalui paket Ubuntu terlebih dahulu. Tidak perlu pip.
Installer membutuhkan systemd dan port 8765 yang kosong; tidak memasang atau
me-restart 9Router, tidak mengubah firewall, dan tidak memasang reverse proxy.
Jika bridge sebelumnya sudah terpasang, installer menolak overwrite; gunakan
prosedur upgrade terpisah agar kunci dan antrean tetap terjaga.

File runtime disalin ke `/opt/osas-bridge`. Kredensial baru dibuat di
`/etc/osas-bridge/bridge.env` (root-only, mode 600). Database dikelola oleh
`StateDirectory=osas-bridge` milik systemd. DynamicUser membatasi akses ke folder
state; tidak perlu membuat user Linux statis. Aplikasi bind `127.0.0.1:8765`.

```bash
systemctl is-active osas-bridge
curl --fail http://127.0.0.1:8765/health
sudo journalctl -u osas-bridge -n 30 --no-pager
```

Health check membuktikan proses bridge, bukan koneksi Muse.

## Hubungkan 9Router lokal

Tambahkan custom/OpenAI-compatible provider di dashboard 9Router:

| Field | Nilai |
| --- | --- |
| Base URL | `http://127.0.0.1:8765/v1` |
| API key provider | `BRIDGE_USER_KEY` dari `/etc/osas-bridge/bridge.env` |
| Model upstream | `muse` |
| Request timeout | Lebih dari 240 detik jika tersedia |

Buka config secara privat dengan `sudoedit /etc/osas-bridge/bridge.env` bila
perlu mengambil kunci. Jangan tempel isi file ke chat/log. 9Router via npm harus
berjalan pada host VPS yang sama, tanpa network namespace/container terpisah.
Gunakan API key dan alias model milik 9Router saat client memanggil 9Router.

Jika 9Router ada di VPS lain, ubah base URL menjadi
`https://DOMAIN_BRIDGE/v1`, tetap gunakan user key dari VPS bridge dan model
upstream `muse`. Konfigurasi HTTPS di bawah membuka endpoint chat/models untuk
kebutuhan itu.

## Domain untuk worker Muse

Pilih subdomain khusus, misalnya `muse.example.com`, dengan DNS mengarah ke VPS.
Gunakan reverse proxy HTTPS yang sudah dipakai VPS. Jangan menjalankan Nginx dan
Caddy bersamaan pada port 80/443 atau mengganti konfigurasi website yang ada.

- **Caddy:** contoh di `deploy/Caddyfile.example`. Ganti domain, gabungkan site
  ke konfigurasi Caddy yang ada, validasi dengan `sudo caddy validate --config
  /etc/caddy/Caddyfile`, lalu `sudo systemctl reload caddy`. Caddy dapat mengurus
  sertifikat jika DNS dan akses port 80/443 sudah benar.
- **Nginx:** contoh di `deploy/nginx.conf.example` untuk site terpisah. Ganti
  domain dan lokasi sertifikat HTTPS yang valid. Contoh itu memerlukan
  sertifikat sudah tersedia; tidak melakukan issuance. Jalankan `sudo nginx -t`
  sebelum `sudo systemctl reload nginx`. Jika site sudah memiliki sertifikat,
  gabungkan location API ke site tersebut tanpa membuat server duplikat.
- **Cloudflare:** tetap butuh origin HTTPS/reverse proxy yang benar. Jangan
  memakai mode Flexible untuk membawa key ke origin lewat HTTP. Gunakan Full
  (strict) jika mengaktifkan proxy Cloudflare.

Contoh proxy membuka `/v1/models` dan `/v1/chat/completions` (user key),
`/worker/claim` dan `/worker/answer` (worker key), serta `/health`. Endpoint admin
adapter tidak dibuka. Timeout upstream 270 detik memberi ruang bagi request chat
yang menunggu hingga 240 detik. CDN/edge proxy atau client bisa memiliki batas
lebih pendek; verifikasi jalur HTTPS sebenarnya.

Setelah domain siap, uji `/health` melalui HTTPS tanpa mematikan verifikasi TLS.
Di Muse, set `BRIDGE_URL=https://DOMAIN_BRIDGE` dan `BRIDGE_WORKER_KEY` melalui
konfigurasi privat. Jangan berikan user key atau isi file config lengkap kepada
worker. Salin `worker.py` dan `MUSE_WORKER.md`, lalu ikuti instruksinya.

Uji satu chat melalui 9Router sambil Muse melakukan claim dan answer. Baru setelah
itu aktifkan scheduled task dan periksa konsumsi token di dashboard Muse. Tidak
ada instalasi scheduler/akses akun Muse otomatis dalam installer ini.

## Hentikan service bila diperlukan

```bash
sudo systemctl disable --now osas-bridge
```

Ini tidak menghapus konfigurasi, kunci, atau database. Jangan menghapus data
sebelum memutuskan apakah antrean dan kredensial masih perlu disimpan.
