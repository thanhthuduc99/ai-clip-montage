"""Sinh kịch bản montage: topic → các đoạn VO, mỗi đoạn kèm mô tả cảnh cần có
để clip_matcher chọn clip từ kho."""
from __future__ import annotations

import re

import config
from tools.llm import chat_json

# Model được dặn tránh văn sáo nhưng vẫn để lọt, nên quét lại bằng code rồi bắt
# viết lại đúng đoạn vi phạm. Chỉ liệt kê cụm sáo rõ ràng để tránh bắt nhầm.
SLOP_PATTERNS = [
    r"không chỉ\b[^.!?]{0,60}\bmà (còn|là|chính)",
    r"không (phải|đơn thuần)\b[^.!?]{0,50}\bmà (là|còn|chính)",
    r"không phải chỉ là|đâu chỉ là|chẳng những\b[^.!?]{0,40}\bmà còn",
    r"tâm huyết|tâm hồn|hồn cốt|thổi hồn|đam mê",
    r"dấu ấn (của )?thời gian|lưu giữ (một )?câu chuyện|câu chuyện riêng",
    r"nâng tầm|đỉnh cao|tuyệt hảo|kỳ diệu|hoàn mỹ|chạm đến trái tim",
    r"mang đến sự \w+|mang đến (một )?(sự sống|hơi thở)",
    r"hòa quyện|kết tinh|thăng hoa|trọn vẹn từng",
    r"bạn đồng hành|không thể thiếu|góp phần tạo nên",
    r"hãy cùng khám phá|cùng khám phá|khám phá hành trình",
    r"mọi thứ bắt đầu từ|điều đáng nói là|thành thật mà nói",
    r"\bnghệ thuật\b(?! sắp đặt)",
]
_SLOP_RE = [re.compile(p, re.IGNORECASE) for p in SLOP_PATTERNS]


def find_slop(text: str) -> list[str]:
    """Các cụm văn sáo tìm thấy trong 1 đoạn lời."""
    hits = []
    for rx in _SLOP_RE:
        for m in rx.finditer(text or ""):
            hits.append(m.group(0))
    return hits

# Luật dùng chung cho cả bước viết và bước kiểm chứng — để hai bước không đá nhau.
VOICEOVER_RULES = (
    "NGUYÊN TẮC VOICE-OVER (quan trọng nhất):\n"
    "- Người xem ĐANG NHÌN THẤY hình. Lời đọc phải nói thứ hình KHÔNG nói được: "
    "lý do, cách làm, thời gian bỏ ra, chuyện phía sau, điều đáng để ý.\n"
    "- CẤM câu chỉ gọi tên thứ đang hiện trên màn hình. Cảnh quay tấm gỗ mà nói "
    "'những tấm ván gỗ có đường vân tự nhiên' là sai, vì người xem đã thấy rồi. "
    "Cùng cảnh đó, hãy nói cái người xem không tự biết: gỗ đó phải khô tới mức nào "
    "mới ghép được, hỏng thì hỏng ra sao.\n"
    "- Lời và hình bổ sung nhau, không lặp nhau.\n\n"
    "ĐƯỢC PHÉP dùng KIẾN THỨC NGHỀ PHỔ THÔNG mà ai trong nghề cũng biết là đúng, "
    "vì đó không phải bịa: vì sao một bước bắt buộc phải làm, bỏ qua thì hỏng thế "
    "nào, chỗ nào dễ sai. Loại thông tin này làm lời đọc đáng nghe, HÃY ƯU TIÊN "
    "DÙNG, nhưng tự nghĩ ra chứ đừng chép lại ví dụ trong hướng dẫn này.\n"
    "TRÁNH nói chung chung kiểu 'cần sự tỉ mỉ', 'trải qua nhiều công đoạn', "
    "'sự kết hợp giữa thiết kế và chất liệu' — nói được điều gì cụ thể thì nói, "
    "không thì cắt ngắn câu.\n"
    "Thà nói 'gỗ chưa khô mà ghép là nứt' còn hơn 'gỗ mang dấu ấn thời gian'.\n"
    "CẤM BỊA bất cứ thứ gì kiểm chứng được mà bối cảnh không cho. Cụ thể là CẤM "
    "MỌI CON SỐ tự nghĩ ra: thời gian ('cả tháng trời', 'ba ngày', 'mười năm'), "
    "giá, số lượng, số năm kinh nghiệm, thành tích, giải thưởng, tên khách hàng, "
    "so sánh hơn thua với nơi khác, cam kết chất lượng.\n\n"
    "CẤM VĂN AI (rất quan trọng, vi phạm là hỏng cả bài):\n"
    "- Cấu trúc 'Không chỉ X mà còn Y', 'Không phải X. Là Y.'\n"
    "- Danh từ trừu tượng rỗng: tâm huyết, tâm hồn, dấu ấn thời gian, hồn cốt, "
    "nghệ thuật, sự sống, đam mê, hành trình (khi chỉ để cho kêu).\n"
    "- Tán dương sáo: nâng tầm, đỉnh cao, tuyệt hảo, kỳ diệu, hoàn mỹ, "
    "thổi hồn, chạm đến trái tim, mang đến sự sống.\n"
    "- Mở bài hắng giọng: 'Điều đáng nói là', 'Thành thật mà nói', "
    "'Hãy cùng khám phá', 'Mọi thứ bắt đầu từ'.\n"
    "- Câu tổng kết đại ngôn ở đoạn cuối kiểu 'không chỉ là nghề, mà là cả một...'.\n"
    "Thay vào đó: nói việc cụ thể, danh từ cụ thể, động từ cụ thể."
)

BASE = (
    "Bạn viết kịch bản video ngắn (TikTok/Reels) cho thương hiệu. "
    "Giọng người thật kể chuyện, câu ngắn, đọc lên nghe tự nhiên. "
    "Không dùng từ tiếng Anh trừ tên riêng. Không emoji.\n\n"
    + VOICEOVER_RULES +
    "\n\nCẤU TRÚC BẮT BUỘC:\n"
    "- Đoạn 1 là HOOK: một câu khiến người ta dừng lại. Nêu điều bất ngờ, một "
    "con số nghề nghiệp hiển nhiên, một nghịch lý, hoặc một câu hỏi. "
    "TUYỆT ĐỐI không mở bằng kiểu 'Mọi thứ bắt đầu từ...', 'Hãy cùng khám phá...'.\n"
    "- Các đoạn giữa: một mạch kể có tiến triển, đoạn sau nối ý đoạn trước, "
    "không phải danh sách rời rạc.\n"
    "- Đoạn cuối: chốt lại ý và mời người xem một cách tự nhiên.\n\n"
    "ĐỘ DÀI: video ~{secs} giây, chia CHÍNH XÁC {count} đoạn. Mỗi đoạn PHẢI có "
    "ÍT NHẤT {word_lo} từ, nhiều nhất {word_hi} từ. Cả bài khoảng {total_words} từ. "
    "Viết thiếu số từ là hỏng, video sẽ bị hụt thời lượng và trơ khoảng lặng. "
    "Đếm lại số từ từng đoạn trước khi trả JSON.\n\n"
    "CHỌN HÌNH: kho cảnh quay bên dưới được đánh số. Mỗi đoạn phải ghi 'clip' = "
    "số của clip sẽ chiếu kèm. Viết lời BÁM VÀO clip đã chọn: lời nói về công "
    "đoạn nào thì clip phải đang quay đúng công đoạn đó. Không có clip nào quay "
    "công đoạn bạn định kể thì ĐỔI SANG kể công đoạn mà kho có.\n"
    "Mỗi clip chỉ dùng cho một đoạn. Xếp các đoạn theo tiến triển tự nhiên của "
    "clip: cảnh thô/dang dở trước, cảnh hoàn thiện sau. ĐOẠN CUỐI nói về kết quả "
    "nên BẮT BUỘC chọn clip quay cảnh đã xong, đang dùng được, có người dùng; "
    "cấm chọn clip còn dở dang, đang thi công, đang lắp đặt.\n\n"
    "Trả DUY NHẤT JSON:\n"
    "{{\"title\": \"cụm 2-5 từ in trên khung hook\", "
    "\"hook_sub\": \"1 dòng phụ ngắn dưới title\", "
    "\"segments\": [{{\"vo\": \"lời đọc\", \"clip\": 3, "
    "\"scene\": \"cảnh minh họa, rút gọn từ mô tả clip đã chọn\"}}]}}"
)

CHECK_SYSTEM = (
    "Bạn là biên tập viên soát lời đọc video trước khi thu âm.\n\n"
    + VOICEOVER_RULES +
    "\n\nMỗi đoạn bạn nhận: LỜI ĐỌC và MÔ TẢ CLIP sẽ chiếu kèm.\n"
    "Việc của bạn là soát, KHÔNG phải viết lại cho khác đi. Duyệt theo thứ tự:\n"
    "0. ĐỘ DÀI: mỗi đoạn ghi rõ khoảng số từ bắt buộc. Dài hơn thì CẮT bớt ý phụ "
    "cho vừa, ngắn hơn thì thêm một chi tiết nghề cụ thể. Đây là ràng buộc cứng vì "
    "lời dài quá làm video vỡ thời lượng. Đếm lại số từ trước khi trả.\n"
    "1. CON SỐ: lời có nêu thời gian, số lượng, số năm, giá, thành tích nào không? "
    "Bối cảnh không cho con số đó thì BỎ hoặc thay bằng cách nói không định lượng. "
    "Ví dụ 'phơi cả tháng trời' → 'phơi tới khi khô hẳn'.\n"
    "2. VĂN AI: có 'không chỉ... mà còn', có danh từ rỗng (tâm huyết, tâm hồn, dấu ấn "
    "thời gian, nghệ thuật, sự sống, đam mê), có tán dương sáo (thổi hồn, nâng tầm, "
    "mang đến sự sống)? Có thì viết lại bằng việc cụ thể.\n"
    "3. KHỚP HÌNH: lời có nói tới vật/hành động KHÔNG có trong clip không? "
    "Có thì sửa cho khớp clip.\n"
    "4. LẶP HÌNH: lời có đang gọi tên đúng thứ đang hiện trên màn hình không? "
    "Có thì viết lại thành câu nói điều hình không nói được.\n"
    "5. Không vi phạm gì thì GIỮ NGUYÊN từng chữ.\n\n"
    "Giữ nguyên vai trò từng đoạn: đoạn hook vẫn phải là hook, đoạn cuối vẫn phải "
    "có lời mời. Giữ mạch nối giữa các đoạn. Tuân thủ khoảng số từ ghi ở mỗi đoạn.\n"
    "Trả DUY NHẤT JSON: {\"segments\": [{\"segment\": 0, \"vo\": \"lời sau soát\"}]}"
)

DESLOP_SYSTEM = (
    "Bạn sửa lời đọc video đang dính văn sáo rỗng.\n"
    "Mỗi đoạn kèm danh sách CỤM BỊ CẤM đang có trong lời. Viết lại đoạn đó sao cho:\n"
    "- Không còn cụm bị cấm nào, kể cả biến thể gần giống.\n"
    "- Thay bằng việc cụ thể, danh từ cụ thể, động từ cụ thể. Nếu không có gì cụ thể "
    "để nói thì nói ngắn hơn, đừng thay bằng một cụm sáo khác.\n"
    "- Giữ nguyên ý và vai trò của đoạn. Không vượt quá số từ tối đa ghi ở mỗi đoạn.\n"
    "- Chỉ được nói vật và hành động có trong MÔ TẢ CLIP kèm theo.\n"
    "Trả DUY NHẤT JSON: {\"segments\": [{\"segment\": 0, \"vo\": \"lời đã sửa\"}]}"
)


def _word_budget(seconds: int, count: int, wps: float = 3.3) -> tuple[int, int, int]:
    """(target, lo, hi) số từ mỗi đoạn. `wps` là tốc độ đọc của giọng đã chọn —
    Vivibe đọc nhanh gần gấp đôi Edge nên dùng chung một hằng số là video hụt."""
    wps = max(1.5, min(float(wps or 3.3), 8.0))
    target = round((seconds + max(0, count - 1) * 0.4) * wps / count)
    lo = max(12, target - 3)
    hi = max(lo + 3, target + 4)
    return target, lo, hi


def generate_script(topic: str, extra_prompt: str = "", brand_name: str = "",
                    brand_context: str = "", seconds: int = 60,
                    sources: list[dict] | None = None, wps: float = 3.3) -> dict:
    """Trả {title, hook_sub, segments:[{vo, scene}]}. Raise khi LLM lỗi/thiếu."""
    seconds = max(20, min(int(seconds or 60), 120))
    source_count = len(sources or [])
    count = max(3, min(10, round(seconds / 8)))
    if source_count:
        count = min(count, source_count)
    target, word_lo, word_hi = _word_budget(seconds, count, wps)
    system = BASE.format(secs=seconds, count=count, word_lo=word_lo,
                         word_hi=word_hi, total_words=target * count)
    if brand_name or brand_context:
        system += (
            f"\n\nTHƯƠNG HIỆU: {brand_name or '(chưa đặt tên)'}\n"
            f"Bối cảnh: {brand_context or '(chưa mô tả)'}"
        )
    if sources:
        inventory = "\n".join(
            f"- CLIP {i + 1}: {s.get('description') or '(chưa có mô tả)'}"
            f" | tags: {', '.join(s.get('tags') or [])}"
            for i, s in enumerate(sources)
        )
        system += (
            f"\n\nKHO CẢNH QUAY THỰC TẾ (chọn 'clip' theo số này):\n{inventory}"
        )

    user = f"Chủ đề: {topic.strip()}"
    if extra_prompt.strip():
        user += f"\nYêu cầu thêm: {extra_prompt.strip()}"

    data = chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.8,
    )
    # Không cắt cứng theo số từ — cắt giữa chừng làm title mất nghĩa
    # ("Từ tấm gỗ thô đến"). build_hook_png tự thu nhỏ font cho vừa khung.
    title = " ".join(str(data.get("title") or "").strip().split())
    if len(title) > 40:
        title = title[:41].rsplit(" ", 1)[0].rstrip(".,:;")
    hook_sub = " ".join(str(data.get("hook_sub") or "").strip().split())
    if find_slop(hook_sub):
        hook_sub = ""
    # 'clip' là số thứ tự 1-based trong inventory; đổi thành index để server tra
    # ngược ra source id. Model trả sai/trùng thì bỏ, clip_matcher lo phần còn lại.
    segments = []
    picked: set[int] = set()
    for s in data.get("segments") or []:
        vo = str(s.get("vo") or "").strip()
        if not vo:
            continue
        try:
            clip_index = int(s.get("clip")) - 1
        except (TypeError, ValueError):
            clip_index = -1
        if clip_index in picked or not (0 <= clip_index < source_count):
            clip_index = -1
        else:
            picked.add(clip_index)
        segments.append({
            "vo": vo,
            "scene": str(s.get("scene") or "").strip(),
            "clip_index": clip_index,
        })
    if not title or len(segments) < count:
        raise RuntimeError(
            f"Kịch bản AI trả về thiếu title hoặc không đủ {count} đoạn."
        )
    return {"title": title, "hook_sub": hook_sub, "segments": segments[:count]}


def ground_segments(topic: str, segments: list[dict], matches: list[dict],
                    sources: list[dict], seconds: int = 60,
                    wps: float = 3.3) -> list[dict]:
    """Soát lời đọc theo clip matcher đã chọn: sửa chỗ bịa hoặc chỗ mô tả lại hình,
    giữ nguyên phần đạt. scene lấy thẳng mô tả clip để người duyệt đối chiếu."""
    by_id = {str(s.get("id") or ""): s for s in sources}
    count = max(1, len(segments))
    _, word_lo, word_hi = _word_budget(seconds, count, wps)

    rows = []
    valid_indexes = []
    last = len(segments) - 1
    for index, (segment, match) in enumerate(zip(segments, matches)):
        source = by_id.get(str(match.get("clip_id") or ""))
        if not source:
            continue
        valid_indexes.append(index)
        role = ("HOOK mở đầu" if index == 0
                else "kết, có lời mời" if index == last else "đoạn giữa")
        current = len((segment.get("vo") or "").split())
        rows.append(
            f"ĐOẠN {index} (vai trò: {role} | đang {current} từ, "
            f"BẮT BUỘC sửa về {word_lo}-{word_hi} từ)\n"
            f"LỜI ĐỌC: {segment.get('vo') or ''}\n"
            f"MÔ TẢ CLIP: {source.get('description') or ''}"
        )
    if not rows:
        return [dict(segment) for segment in segments]

    data = chat_json(
        [
            {"role": "system", "content": CHECK_SYSTEM},
            {"role": "user", "content": f"Chủ đề chung: {topic}\n\n" + "\n\n".join(rows)},
        ],
        model=config.VISION_MODEL,
        temperature=0.3,
    )
    rewritten: dict[int, str] = {}
    for item in data.get("segments") or []:
        try:
            index = int(item.get("segment"))
        except (TypeError, ValueError):
            continue
        vo = str(item.get("vo") or "").strip()
        # Bỏ bản sửa vẫn vượt trần từ — giữ bản cũ còn hơn để video vỡ thời lượng
        # rồi renderer phải đọc gấp.
        if index in valid_indexes and vo and len(vo.split()) <= word_hi + 4:
            rewritten[index] = vo

    result = []
    for index, (segment, match) in enumerate(zip(segments, matches)):
        item = dict(segment)
        source = by_id.get(str(match.get("clip_id") or ""))
        if source:
            # Bước soát trả thiếu đoạn nào thì giữ lời gốc, không làm hỏng cả kịch bản.
            item["vo"] = rewritten.get(index, item.get("vo") or "")
            item["scene"] = str(source.get("description") or "").strip()
        result.append(item)
    return deslop_segments(result, matches, by_id, word_hi=word_hi)


def deslop_segments(segments: list[dict], matches: list[dict],
                    by_id: dict[str, dict], word_hi: int = 40,
                    passes: int = 2) -> list[dict]:
    """Quét văn sáo bằng regex rồi bắt model viết lại đúng đoạn dính.
    Model hay chỉ trả một đoạn nên lặp lại với phần còn sót; hết lượt mà vẫn còn
    thì thôi, người duyệt sửa tay ở bảng review."""
    for _ in range(passes):
        rows = []
        dirty = []
        for index, segment in enumerate(segments):
            hits = find_slop(segment.get("vo") or "")
            if not hits:
                continue
            source = by_id.get(str((matches[index] if index < len(matches) else {})
                                   .get("clip_id") or ""))
            # Đánh số lại từ 0 theo thứ tự gửi đi: model hay trả 0,1,2 theo vị trí
            # trong danh sách chứ không theo số đoạn thật.
            rows.append(
                f"ĐOẠN {len(dirty)} (đang {len((segment.get('vo') or '').split())} từ, "
                f"bản sửa tối đa {word_hi} từ)\n"
                f"LỜI: {segment.get('vo')}\n"
                f"CỤM BỊ CẤM: {', '.join(sorted(set(hits)))}\n"
                f"MÔ TẢ CLIP: {(source or {}).get('description') or '(không có)'}"
            )
            dirty.append(index)
        if not rows:
            break

        try:
            data = chat_json(
                [
                    {"role": "system", "content": DESLOP_SYSTEM},
                    {"role": "user", "content": "SỬA TẤT CẢ các đoạn dưới đây, "
                     "trả về đủ từng đoạn một.\n\n" + "\n\n".join(rows)},
                ],
                temperature=0.4,
            )
        except Exception:
            break

        changed = False
        for item in data.get("segments") or []:
            try:
                pos = int(item.get("segment"))
            except (TypeError, ValueError):
                continue
            vo = str(item.get("vo") or "").strip()
            if not (0 <= pos < len(dirty)) or not vo:
                continue
            index = dirty[pos]
            old = segments[index].get("vo") or ""
            # Bản sạch hẳn thì nhận kể cả dài hơn; bản mới đỡ bẩn thì phải vừa trần từ.
            if (len(find_slop(vo)) < len(find_slop(old))
                    and (not find_slop(vo)
                         or len(vo.split()) <= max(word_hi, len(old.split())))):
                segments[index]["vo"] = vo
                changed = True
        if not changed:
            break
    return segments
