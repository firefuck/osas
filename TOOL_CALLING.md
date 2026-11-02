# Tool calling dan alur agent

Implementasi ini menyediakan **protokol function calling** untuk client OpenAI,
bukan jaminan kualitas/reliabilitas yang sama dengan tool calling native model.
Default Muse memakai **emulasi lewat prompt**. Hanya output berformat benar,
dengan fungsi yang dideklarasikan dan argumen sesuai schema, diteruskan ke client.
Output salah menghasilkan `502 invalid_model_output`; tidak ada tool yang
dijalankan server atau retry generasi otomatis.

## Chat Completions: siklus lengkap

Kirim ke `/v1/chat/completions` dengan key client bridge:

```json
{
  "model": "muse",
  "messages": [{"role": "user", "content": "Jumlahkan 2 dan 3 dengan fungsi add"}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "add",
      "description": "Jumlahkan dua integer",
      "strict": true,
      "parameters": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": false
      }
    }
  }],
  "tool_choice": "required",
  "parallel_tool_calls": false
}
```

Respons berisi assistant message seperti:

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [{
    "id": "call_ID_UNIK",
    "type": "function",
    "function": {"name": "add", "arguments": "{\"a\":2,\"b\":3}"}
  }]
}
```

`finish_reason` adalah `tool_calls`. Client melakukan langkah berikut:

1. Tambahkan **assistant message utuh** tadi ke `messages`.
2. Parse `function.arguments` sebagai JSON, dispatch fungsi yang memang tersedia
   pada client, lalu eksekusi. Jangan eval string argumen menjadi kode.
3. Tambahkan `{"role":"tool","tool_call_id":"call_ID_UNIK","content":"5"}`.
4. Kirim ulang histori lengkap dan tools. Pilih `tool_choice:"auto"` agar model
   boleh menyelesaikan jawaban; `required` memaksa call baru pada tiap request.
5. Ulangi hingga `finish_reason:"stop"`, refusal, error, atau batas langkah client.

Untuk beberapa call, client mengirim **satu hasil untuk setiap ID**. Hasil boleh
datang dengan urutan berbeda, tetapi semuanya harus ada sebelum pesan berikutnya.
ID tidak cocok, hasil ganda, atau call yang belum dijawab ditolak dengan 400.
Error dari eksekusi tool juga bisa dikirim sebagai JSON string pada content hasil
tool, sehingga model dapat menyesuaikan rencana tanpa mengaku tool berhasil.

`tool_choice` mendukung `auto`, `none`, `required`, dan
`{"type":"function","function":{"name":"add"}}`. `parallel_tool_calls:false`
membatasi satu call per respons. `functions`/`function_call` legacy juga didukung;
output legacy hanya satu function call.

## Responses API: Codex-style function loop

Responses memakai bentuk tool yang datar:

```python
import json, os
from openai import OpenAI

client = OpenAI(base_url=os.environ['BRIDGE_URL'],
                api_key=os.environ['BRIDGE_USER_KEY'], max_retries=0, timeout=270)
tools = [{"type": "function", "name": "add", "strict": True,
          "parameters": {"type": "object", "properties": {
              "a": {"type": "integer"}, "b": {"type": "integer"}},
              "required": ["a", "b"], "additionalProperties": False}}]
response = client.responses.create(model='muse', input='Hitung 2 + 3', tools=tools)
for _ in range(8):
    calls = [item for item in response.output if item.type == 'function_call']
    if not calls:
        print(response.output_text)
        break
    outputs = []
    for call in calls:
        if call.name != 'add':
            raise ValueError('Unknown tool')
        args = json.loads(call.arguments)
        outputs.append({'type': 'function_call_output', 'call_id': call.call_id,
                        'output': str(args['a'] + args['b'])})
    response = client.responses.create(model='muse', previous_response_id=response.id,
                                       input=outputs, tools=tools)
else:
    raise RuntimeError('Agent step limit reached')
```

`BRIDGE_URL` untuk SDK harus berakhir `/v1`. Response disimpan default-nya agar
`previous_response_id` bekerja. `instructions` pada request sebelumnya tidak
diwariskan. Tools dan pilihan tool sebaiknya dikirim pada tiap request.

Untuk mode stateless, kirim `store=False`, lalu replay pesan awal, seluruh item
`function_call` dari output, dan setiap `function_call_output` pada `input`.
Retrieve/delete memakai `GET`/`DELETE /v1/responses/{id}`. ID yang tidak disimpan,
kedaluwarsa, dievikt, atau sudah dihapus menghasilkan 404. Histori disimpan
plaintext di SQLite; gunakan opt-out jika tidak membutuhkan chaining server.

## Streaming dan structured output

- Chat `stream:true`: delta tool berisi `index`, `id`, nama fungsi, lalu fragmen
  `function.arguments`. Gabungkan fragmen per index sebelum parse/eksekusi.
  Jangan mengeksekusi tool sebelum stream selesai dengan `finish_reason:tool_calls`.
- Responses `stream:true`: lifecycle `response.created`, `response.in_progress`,
  item added, function-arguments delta/done atau text delta/done, item done, lalu
  `response.completed` atau `response.incomplete`. Cocok dengan stream manager SDK.
- Ini **buffered SSE**. Model menyelesaikan output dahulu, baru event dikirim;
  tidak ada klaim token live atau heartbeat palsu. Timeout client/proxy perlu cukup.
- Chat `response_format:{"type":"json_object"}` atau
  `{"type":"json_schema","json_schema":{"name":"result","strict":true,"schema":...}}`.
  Responses memakai `text.format` dengan type/name/strict/schema pada objek datar.
  Jawaban final divalidasi terhadap schema; schema hasil tool juga divalidasi.
- Token usage belum tersedia, bukan angka perkiraan. Penolakan model native
  diteruskan sebagai refusal; output tool terpotong/difilter tidak boleh dieksekusi.

## Contoh agent yang betul-betul baca/tulis file

```bash
mkdir demo-workspace
printf '%s\n' 'Halo dari input.txt' > demo-workspace/input.txt
# Set BRIDGE_USER_KEY secara privat dan BRIDGE_URL ke endpoint /v1.
.venv/bin/python examples/agent_loop.py --workspace demo-workspace \
  'Baca input.txt lalu tulis versi huruf besarnya ke output.txt'
```

Client contoh menyediakan `read_file` dan `write_file`, membatasi path pada folder
pilihan dan isi 100 KB, menolak overwrite, dan membatasi 8 langkah default.
Ini contoh client lokal, bukan sandbox untuk pengguna tak tepercaya. Jangan
memilih folder berisi secret. Agent client lain boleh menyediakan fungsi terminal,
browser, Git, dsb. dengan kontrol aksesnya sendiri; bridge hanya mengangkut call.

## Konfigurasi, upgrade, dan batas kompatibilitas

`BRIDGE_TOOL_MODE=emulate` adalah default, berlaku untuk backend queue maupun
Muse2API. `native` hanya bila upstream telah diuji mengembalikan tool_calls sesuai
protokol; tidak otomatis berarti Muse punya dukungan native. Dalam native mode,
worker dapat submit JSON `{"message":...,"finish_reason":"tool_calls"}` lewat
`worker.py answer --response-file FILE --job-file JOB`.

Instalasi lama perlu dependencies dan file baru. Dari checkout terbaru di VPS:

1. Simpan backup privat source, unit service, dan environment. Pertahankan key
   `/etc/osas-bridge/bridge.env` dan direktori state; jangan regenerate key.
2. Siapkan venv di `/opt/osas-bridge/.venv`, lalu install `requirements.txt`.
   `python3-venv` harus tersedia. Direktori venv harus bisa dibaca service
   DynamicUser; jangan membuat config berisi key di venv tersebut.
3. Stop service dan salin `bridge.py`, `chat_protocol.py`, `responses_api.py`,
   `model_contract.py`, `muse_backend.py`, dan `worker.py` ke `/opt/osas-bridge`.
4. Update unit dari `deploy/osas-bridge.service` (ExecStart sekarang memakai venv),
   jalankan daemon-reload lalu start service. Update route reverse proxy agar
   `/v1/responses`, `/v1/responses/{id}`, dan `/v1/capabilities` juga diteruskan.
5. Cek health dan capabilities, lalu lakukan satu tool loop sungguhan dengan
   akun Muse lo. Tes lokal dan health saja belum memvalidasi deployment.

Ini melengkapi **text + function-tool agent workflow**. Hosted web search, MCP,
computer use, custom grammar tools, gambar/audio, Responses background jobs,
embeddings, dan seluruh API OpenAI lainnya belum diimplementasikan. Tool calling
Muse nyata dan client 9Router/Codex pengguna belum dites; jangan menganggap SDK
fixture test membuktikan model Muse akan selalu mematuhi protokol emulasi.

Untuk reasoning/provider state dan batas context/output, lihat
[MODEL_CONTRACT.md](MODEL_CONTRACT.md). Output terpotong mempertahankan `length`
atau Responses `incomplete`, dengan tool yang belum lengkap tidak dapat dieksekusi.
Default policy best-effort meneruskan budget/replay yang belum terverifikasi.
Set `BRIDGE_CAPABILITY_POLICY=strict` bila ingin menolak kondisi itu sebelum generasi.
