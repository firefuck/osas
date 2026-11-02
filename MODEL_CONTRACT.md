# Reasoning replay, context window, dan batas output

Perbaikan ini menangani poin 4/5 pada sisi bridge. Kapabilitas model asli tetap
harus tersedia di upstream. Muse2API web adapter yang dipatok belum membuktikan
reasoning replay, context window tertentu, atau enforcement output token; mode
default tidak mengiklankan angka context dan tidak diam-diam mengabaikan budget.

## Reasoning dan provider state

Bridge mempertahankan field assistant `reasoning`, `reasoning_content`,
`reasoning_details`, dan `provider_data` sebagai JSON, termasuk string opaque,
signature, null, array, dan objek bersarang. Nilai tidak diparse menjadi instruksi,
diringkas, dihapus, atau dikarang dari jawaban teks. Metadata asli dari upstream
dipertahankan saat normalisasi output, tool calls, refusal, legacy function calls,
dan SSE. SSE menaruh metadata sekali pada delta awal, sehingga client dapat
menyimpannya bersama assistant message dan mereplay persis pada turn berikutnya.

Metadata pada assistant tool-call history tetap menjadi field terpisah dalam
request upstream. Metadata tidak disisipkan dalam JSON teks prompt emulasi.
Menempelkan token opaque ke teks prompt tidak sama dengan mengembalikan state
melalui protokol yang dipahami provider.

Responses chaining menyimpan assistant metadata utuh. Replay stateless juga
mempertahankan extra metadata pada message/function-call items. Item Responses
`type:reasoning` asli (misalnya `id`, `summary`, `encrypted_content`) dipetakan ke
field assistant `reasoning` tanpa mengubah item tersebut dan dikembalikan sebagai
item asli bila upstream mengirim bentuk itu. Provider/adapter yang digunakan
harus mendukung representasi replay tersebut; dukungan format bridge bukan bukti
bahwa Muse web agent mengonsumsi state itu.

Jika histori berisi state replay nonempty tetapi upstream belum dinyatakan
mendukungnya melalui profil terverifikasi, request ditolak dengan
`422 reasoning_replay_unsupported` **sebelum generasi**. Ini mencegah state
kelihatan berhasil dikirim padahal dibuang adapter. Field kosong/null tetap
dipertahankan, tanpa dianggap sebagai state yang membutuhkan resume.

State opaque dapat mengandung continuation token. Jangan membagikan log/file
histori ke publik. Penyimpanan Responses mengikuti auth, TTL dan opt-out yang
dijelaskan di README; SQLite bukan penyimpanan terenkripsi.

## Profil model yang terverifikasi

`BRIDGE_MODEL_PROFILE` menunjuk file JSON dengan struktur pada
`deploy/model-profile.example.json`. Contoh sengaja berisi `null`/`false`, bukan
angka token tebakan. Tanpa profil, kapabilitas tetap unknown/unsupported.

Untuk deployment systemd, letakkan **metadata publik tanpa key** misalnya di
`/opt/osas-bridge/model-profile.json` dengan mode 644, lalu isi
`BRIDGE_MODEL_PROFILE=/opt/osas-bridge/model-profile.json` dalam environment
privat. Jangan meletakkan file profil di direktori root-only `/etc/osas-bridge`
karena process DynamicUser tidak dapat membacanya. EnvironmentFile tetap dibaca
systemd secara privat seperti sebelumnya.

Kontrak baru boleh diaktifkan setelah operator memverifikasi upstream:

| Field | Arti |
| --- | --- |
| `upstream_model` | Harus sama persis dengan `MUSE_UPSTREAM_MODEL`; tidak boleh menempelkan batas model lain pada alias Muse |
| `verified` | Operator sudah memeriksa kapabilitas, bukan hasil auto-discovery bridge |
| `source` | Referensi publik/dokumen atau deskripsi bukti verifikasi; tanpa credential |
| `context_window` | Total context token yang benar-benar didukung, atau null bila tidak diketahui |
| `max_output_tokens` | Batas completion yang didukung, atau null |
| `output_parameter` | Parameter yang benar-benar ditegakkan upstream: `max_tokens` atau `max_completion_tokens`; null bila tidak didukung |
| `count_tokens_path` | Endpoint count dengan host/base yang sama, misalnya `/count_tokens`; null jika tidak ada |
| `context_enforced_upstream` | Upstream sendiri memeriksa total context secara otoritatif |
| `reasoning_replay` | Upstream sudah diuji menerima dan menggunakan replay field/state yang diperlukan client |

Klaim kapabilitas/batas tanpa `verified:true` dan `source` ditolak saat startup.
Ini adalah deklarasi operator, bukan verifikasi kriptografis atau jaminan dari
bridge bahwa konfigurasi yang diisi benar. Untuk alias yang autorouting atau
modelnya tidak diketahui, jangan mengisi profil model 1M secara sembarang.

`GET /v1/models`, `/v1/models/muse`, dan `/v1/capabilities` melaporkan angka yang
dikonfigurasi, asal deklarasi, metode enforcement, dan dukungan replay. Tanpa
profil, `context_window`/`max_output_tokens` adalah null. Tidak ada angka 1M default.

## Batas output dan context

- `max_tokens` dan `max_completion_tokens` harus sama jika dikirim bersamaan.
  Bridge memetakan alias ke parameter yang dinyatakan ditegakkan upstream.
- Tanpa output contract, request yang meminta budget ditolak dengan
  `422 output_limit_unsupported`, bukan diterima lalu diabaikan. Request biasa
  tanpa budget masih boleh berjalan, dengan limit tidak diketahui.
- Budget yang melampaui batas model ditolak `400 output_limit_exceeded` sebelum
  generasi. Jika contract ada dan client tidak memberi budget, batas model
  dikirim eksplisit sebagai budget default.
- Untuk local admission berdasarkan context, endpoint counter harus menerima
  request chat **final yang sama** (model upstream, messages, tools, state, dan
  prompt emulasi yang telah disisipkan) dan mengembalikan
  `{"input_tokens": INTEGER}`. Endpoint tersebut harus menghitung seluruh input
  yang diperlukan provider, termasuk overhead/template yang relevan. Bridge tidak
  menebak tokenizer Muse atau menghitung berdasarkan karakter.
- Count dipanggil dengan key adapter pada host/base yang sama. Redirect tidak
  diikuti. `count_tokens_path` bukan URL eksternal dan tidak boleh mengandung
  query/fragment. Endpoint fixture tes bukan implementasi counter untuk Muse.
- Jika `input_tokens + output_budget > context_window`, bridge menolak dengan
  `400 context_length_exceeded` sebelum generasi. Count gagal/invalid tidak
  diteruskan menjadi generasi yang mengonsumsi kuota.
- Sebagai alternatif, gunakan `context_enforced_upstream:true` hanya bila upstream
  benar-benar memeriksa total context sendiri. Tanpa counter atau kontrak itu,
  profil context yang diisi tidak boleh diperlakukan sebagai enforcement.
- Jika usage upstream yang otoritatif menunjukkan completion melampaui budget,
  hasil ditolak sebagai `502 upstream_output_limit_violation`. Pengecekan ini
  tidak mengubah placeholder nol dari web adapter menjadi metering yang akurat.
  Saat memverifikasi profil, pastikan semantik parameter juga mencakup reasoning
  token apabila client meminta total completion budget.

## Truncation

`finish_reason:length` asli dipertahankan pada Chat Completions dan SSE.
Responses mengembalikannya sebagai `status:incomplete`, dengan
`incomplete_details.reason:max_output_tokens`. Bridge tidak mengganti sinyal itu
dengan `stop` atau 502 hanya karena JSON/tool envelope ikut terpotong.

Tool calls yang belum lengkap tidak ditempatkan di assistant `tool_calls` yang
dapat dieksekusi. Fragmen native bisa tersedia dalam
`bridge_details.incomplete_tool_calls` pada respons non-stream, ditandai
`executable:false`. Client harus menangani `length` sebagai output belum selesai,
bukan menganggap eksekusi tool berhasil. Output model yang dilaporkan selesai
tetapi JSON/schema-nya salah tetap menghasilkan `502 invalid_model_output`.

Bridge tidak memotong berdasarkan karakter lalu mengklaim itu batas token model.
Jika upstream mengabaikan budget dan tidak memberi bukti truncation/usage,
wrapper tidak dapat mengarang sinyal length yang akurat.

## Upgrade dan pengujian

Tambahkan `model_contract.py` bersama file runtime lain di `/opt/osas-bridge` pada
upgrade dari v0.3, lalu restart service. Installer baru sudah menyalinnya. Profil
tetap opsional; jangan menyalin example unknown lalu mengubah verified ke true
tanpa verifikasi aktual. Tidak ada dependency runtime baru untuk perubahan ini.

Tes lokal mencakup replay lossless antarturn dan SSE, Responses state opaque,
penolakan kapabilitas yang tidak diketahui, pemetaan alias budget, counter pada
payload final, batas context, pelanggaran kontrak output, serta length pada JSON
tool yang belum lengkap. Backend dan counter tes bersifat sintetis; login Muse,
context nyata, opaque-state resume, dan budget Muse asli belum diverifikasi.
