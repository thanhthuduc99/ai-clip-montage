# ai-clip-montage

Đưa kho clip quay sẵn của một thương hiệu vào. Nhận lại video dọc 9:16 hoàn chỉnh.

AI xem từng clip và tự mô tả, viết kịch bản theo chủ đề bạn nhập, chọn clip cho từng
đoạn, đọc voice, ghép phụ đề khớp từng từ, thêm chuyển cảnh, sound effect và nhạc nền.

Chạy local trên máy bạn. Python + FFmpeg. Ngoài một key OpenRouter thì không cần tài
khoản dịch vụ nào khác. Giọng đọc mặc định dùng Edge TTS, miễn phí.

Đây là app nội bộ mình dùng thật cho việc sản xuất video thương hiệu, mở nguồn nguyên
trạng. Không phải demo dựng riêng để show.

## Nó xuất ra cái gì

- File mp4 1080x1920, kèm bản preview 480p để xem nhanh trong trình duyệt
- Hook chữ lớn ở đầu video
- Phụ đề khớp từng từ, lấy word timestamps thẳng từ Edge TTS nên không cần Whisper
- Chuyển cảnh `xfade` cho hình và `acrossfade` cho tiếng
- Whoosh chèn đúng điểm chuyển cảnh
- Nhạc nền bốc ngẫu nhiên từ thư mục của bạn, có fade out cuối video

## Cần gì

- Python 3.10 trở lên
- FFmpeg và ffprobe trên PATH. Đây là thứ duy nhất không cài qua pip được
- Key OpenRouter, lấy tại https://openrouter.ai/keys

Mặc định dùng `google/gemini-2.5-flash` để xem clip và `openai/gpt-4o-mini` để viết
kịch bản. Đổi model trong `.env`.

## Chạy

Windows:

```powershell
git clone https://github.com/thanhthuduc99/ai-clip-montage.git
cd ai-clip-montage
copy .env.example .env    # mở ra điền OPENROUTER_KEY
.\start.ps1
```

`start.ps1` tự tạo `.venv`, cài requirements lần đầu, rồi mở http://localhost:7799

macOS và Linux chưa có script sẵn, chạy tay:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # điền OPENROUTER_KEY
.venv/bin/python server.py
```

Chưa test trên macOS và Linux. FFmpeg và stdlib thì chạy được, nhưng nếu bạn gặp lỗi
thì mở issue.

## Dùng thế nào

1. **Tạo thương hiệu.** Tên cộng một đoạn context: brand này làm gì, kho clip quay
   cái gì, giọng kể nên ra sao. Context này đi vào cả prompt phân tích lẫn prompt viết
   kịch bản, nên viết cẩu thả là kịch bản lệch.
2. **Tải clip lên.** App trích 6 frame mỗi clip bằng ffmpeg, đưa qua vision model, nhận
   lại mô tả, tag và phỏng đoán bối cảnh. Upload chia chunk 8MB, resume được theo offset.
3. **Nhập chủ đề và thời lượng.** Bước viết kịch bản chia đoạn và tự gắn clip
   cho từng đoạn. Matcher chỉ điền vào chỗ kịch bản bỏ trống, chống lặp clip, ưu tiên
   clip ít dùng.
4. **Review.** Bảng sửa lời đọc và đổi clip từng đoạn trước khi render. Đây là điểm dừng
   duy nhất, cố ý để vậy.
5. **Render.** Chạy nền trong worker thread, hủy được giữa chừng.

Clip thuộc đúng một thương hiệu. Matcher chỉ lấy clip của thương hiệu đang chọn.

## Bên trong

```
server.py          HTTP server stdlib ThreadingHTTPServer :7799
                   2 worker thread (analyze, render) trên SQLite WAL, không auth
config.py          đọc .env, đường dẫn, model, tham số render
web/               SPA vanilla JS, 3 tab: Kho clip / Tạo video / Video đã xong
data/app.sqlite3   jobs, upload_sessions, resources (brand|source|draft|video)
data/sources/      clip đã upload
output/renders/    video thành phẩm
assets/            fonts, sfx, music
```

| tools/ | Vai trò |
|---|---|
| `pipeline_store.py` | SQLite WAL: job queue, upload chunk có resume, resources |
| `source_analyzer.py` | ffprobe + trích 6 frame, đẩy qua vision, trả mô tả và tag |
| `script_gen.py` | chủ đề + context + kho clip, trả kịch bản chia đoạn kèm bộ lọc văn sáo |
| `clip_matcher.py` | điền clip cho đoạn còn trống, chống lặp, giữ mạch kể |
| `voice_generator.py` | TTS, ghi kèm `.words.json` word timestamps |
| `subtitles.py` | dựng cue phụ đề từ words.json, vẽ PNG hook và caption |
| `montage_render.py` | render từng đoạn rồi xfade lại với nhau |
| `audio_mixer.py` | whoosh `adelay` + `amix` tại điểm chuyển, BGM có fade out |
| `llm.py` | gọi OpenRouter, ép JSON, log token usage |

Không có framework. Server là `http.server` của stdlib, front-end là vanilla JS, DB là
SQLite. Cài 5 package: `edge-tts`, `Pillow`, `requests`, `python-dotenv`, `filelock`.

## Phần đáng đọc nhất: chặn văn AI bằng code

Bản đầu tiên viết kịch bản rất dở, kiểu chú thích lại đúng cái hình đang chiếu. Cảnh
quay tấm gỗ thì lời đọc là "những tấm ván gỗ với đường vân tự nhiên". Người xem đã nhìn
thấy rồi, nói lại thành thừa.

Bốn luật đã cài, nằm trong `VOICEOVER_RULES` và `SLOP_PATTERNS` của
[`tools/script_gen.py`](tools/script_gen.py):

1. **Lời bổ sung hình, không lặp hình.** Vẫn cảnh tấm gỗ đó, lời phải nói thứ người xem
   không tự biết: gỗ phải khô tới mức nào mới ghép được, ghép sớm thì hỏng ra sao.

2. **Được dùng kiến thức nghề phổ thông, cấm mọi con số tự nghĩ.** "Gỗ chưa khô mà ghép
   là nứt" thì được. "Phơi gần một năm" thì cấm. Kèm một bẫy đã dính: đừng đặt ví dụ có
   số vào prompt, model sẽ chép nguyên xi con số đó ra.

3. **Quét văn sáo bằng regex, không tin model tự giác.** `SLOP_PATTERNS` là danh sách
   regex bắt "không chỉ X mà còn Y", "tâm huyết", "thổi hồn", "dấu ấn thời gian", "hãy
   cùng khám phá", "nâng tầm". `find_slop()` quét, `deslop_segments()` bắt model viết
   lại đúng đoạn dính, lặp tối đa 2 lượt, và chỉ nhận bản sửa khi nó thực sự sạch hơn
   bản cũ.

4. **Kịch bản tự chọn clip, matcher chỉ điền chỗ trống.** Trước đây hai bước chọn ngược
   nhau nên lời nói về phơi gỗ mà hình lại ra công trường.

Bẫy thứ hai đã dính, ghi lại vì ai làm bước LLM nhận subset cũng sẽ gặp: khi đưa cho
model danh sách đã lọc (chỉ các đoạn dính slop), model trả về `segment: 0,1,2` theo vị
trí trong danh sách chứ không theo số đoạn thật. Bản sửa bị gán nhầm đoạn rồi loại sạch.
`deslop_segments` giờ đánh số lại từ 0 rồi map ngược qua mảng `dirty`.

Sửa `SLOP_PATTERNS` theo văn phong của bạn. Danh sách hiện tại viết cho tiếng Việt.

## Giới hạn đã biết

Nói trước để bạn khỏi mất thời gian:

- Mỗi đoạn kịch bản dùng đúng 1 clip. Clip ngắn hơn lời đọc thì bị `-stream_loop` lặp
  lại cho đủ thời lượng. Kho nhiều clip 2 tới 5 giây mà đoạn voice 8 giây thì thấy rõ.
  Ghép nhiều clip cho 1 đoạn thì chưa làm.
- Hủy job chỉ kiểm tra giữa các bước, không kill ffmpeg đang chạy. Hủy đúng lúc đang
  concat thì chờ tới 1 tới 2 phút.
- Không auth, không multi-user. Thiết kế để một người chạy trên một máy.
- Đừng chạy hai server cùng lúc. Windows cho hai process bind chung cổng 7799, request
  rơi ngẫu nhiên vào bản cũ. Đã có `_port_already_serving()` chặn từ đầu nhưng biết vẫn
  hơn.
- Prompt vision và prompt kịch bản đều bắt model trả JSON. Model trả JSON hỏng thì
  `llm.chat_json` raise. Model rẻ hỏng nhiều hơn.
- Toàn bộ prompt và giao diện viết cho tiếng Việt.
- Lỗi trong request ghi vào `output/server-errors.log`. Xem file đó trước khi đoán.

## Đóng góp

Mở issue nếu chạy lỗi. PR thì mình đọc, nhưng đây là app một người dùng thật nên mình
sẽ giữ nó đơn giản: không thêm abstraction cho code dùng một lần, không thêm cấu hình
chưa ai cần.

## License

MIT. Xem [LICENSE](LICENSE).

Font Be Vietnam Pro trong `assets/fonts/` theo SIL OFL 1.1, xem
[NOTICE](assets/fonts/NOTICE.md). Thư mục `assets/music/` để trống, tự thả nhạc bạn có
quyền dùng vào.

Tác giả: Thành Vũ Đức (Contentta).
