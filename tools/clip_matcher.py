"""Ghép đoạn script với clip trong kho theo mô tả — LLM chọn, có rule chống lặp."""
from __future__ import annotations

import config
from tools.llm import chat_json

SYSTEM = (
    "Bạn ghép clip quay sẵn vào từng đoạn kịch bản video ngắn.\n"
    "Nhận: danh sách đoạn (mỗi đoạn có lời đọc + cảnh cần có) và kho clip "
    "(id + mô tả + thời lượng + số lần đã dùng).\n"
    "LUẬT:\n"
    "- Mỗi đoạn chọn ĐÚNG 1 clip khớp nội dung cảnh nhất.\n"
    "- KHÔNG dùng 1 clip cho 2 đoạn trong cùng video.\n"
    "- Nếu 2 clip khớp ngang nhau, ưu tiên clip có used thấp hơn.\n"
    "- GIỮ MẠCH: mỗi đoạn có ghi vai trò trong ngoặc vuông. Đoạn mở đầu lấy cảnh "
    "thô/nguyên liệu; đoạn giữa lấy cảnh đang làm; đoạn KẾT nói về kết quả nên "
    "BẮT BUỘC lấy cảnh đã xong, đang dùng được, CẤM lấy cảnh còn dở dang, chưa "
    "hoàn thiện, đang thi công. Đọc cả danh sách trước khi chọn.\n"
    "- Chỉ dựa trên mô tả được cung cấp; CẤM suy diễn logo, khách hàng, máy móc, "
    "thành phẩm hoặc hành động không có trong mô tả.\n"
    "- Không có clip nào thực sự hợp → để clip_id là chuỗi rỗng. Không chọn đại.\n"
    "- reason phải nhắc đúng chi tiết trong mô tả nguồn làm bằng chứng.\n"
    "Trả DUY NHẤT JSON: {\"matches\": [{\"segment\": 0, \"clip_id\": \"...\", \"reason\": \"1 câu ngắn\"}]}"
)


def match_clips(segments: list[dict], sources: list[dict]) -> list[dict]:
    """segments: [{vo, scene}], sources: [{id, description, tags, duration, times_used}].
    Trả list cùng độ dài segments: [{clip_id, reason}] (clip_id có thể rỗng)."""
    if not sources:
        return [{"clip_id": "", "reason": "Kho chưa có clip."} for _ in segments]

    seg_lines = "\n".join(
        f"{i}. [{s.get('role') or 'đoạn giữa'}] "
        f"cảnh cần: {s.get('scene') or '(chưa mô tả)'} | lời đọc: {s['vo']}"
        for i, s in enumerate(segments)
    )
    src_lines = "\n".join(
        f"- id={s['id']} | {s['description']} | tags: {', '.join(s.get('tags') or [])} "
        f"| dài {s.get('duration', 0):.0f}s | used {s.get('times_used', 0)}"
        for s in sources
    )
    data = chat_json(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"CÁC ĐOẠN:\n{seg_lines}\n\nKHO CLIP:\n{src_lines}"},
        ],
        # Ghép hình cần suy luận "cảnh này đã xong hay còn dở" — model rẻ hay chọn
        # nhầm cảnh dang dở cho đoạn kết, nên dùng model mạnh hơn ở bước này.
        model=config.VISION_MODEL,
        temperature=0.2,
    )
    valid_ids = {s["id"] for s in sources}
    result = [{"clip_id": "", "reason": ""} for _ in segments]
    used: set[str] = set()
    for m in data.get("matches") or []:
        try:
            idx = int(m.get("segment"))
        except (TypeError, ValueError):
            continue
        cid = str(m.get("clip_id") or "").strip()
        if 0 <= idx < len(segments) and cid in valid_ids and cid not in used:
            result[idx] = {"clip_id": cid, "reason": str(m.get("reason") or "").strip()}
            used.add(cid)

    return result
