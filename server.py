"""Server nội bộ — hệ thống tạo video tự động từ kho source clip.

1 process duy nhất: HTTP server (stdlib, không auth — chạy local solo) + 2 worker
thread (analyze + render) trên SQLite WAL. Kiến trúc copy từ dulich-pipeline,
rút gọn phần multi-user/publish.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import config
from tools.pipeline_store import (
    PipelineStore,
    PipelineStoreError,
    QueueLimitError,
    UploadValidationError,
)

ROOT = config.ROOT
OWNER = "me"

ERROR_LOG = config.OUTPUT_DIR / "server-errors.log"


def log_error(where: str) -> None:
    """Ghi traceback ra file. stderr của server hay bị nuốt khi chạy nền nên
    không có file này thì lỗi biến mất không dấu vết."""
    import traceback
    text = f"\n{'=' * 60}\n{time.strftime('%Y-%m-%d %H:%M:%S')} {where}\n{traceback.format_exc()}"
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass
    print(text, file=sys.stderr)

STORE = PipelineStore(config.DB_PATH, upload_root=config.OUTPUT_DIR / "temp_uploads")

MAX_UPLOAD_FILE_BYTES = 1024 ** 3            # 1 GB / file
MAX_UPLOAD_JOB_BYTES = 4 * 1024 ** 3         # 4 GB / phiên
MAX_UPLOAD_CHUNK_BYTES = 16 * 1024 ** 2
UPLOAD_DISK_RESERVE_BYTES = 2 * 1024 ** 3

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}


# ─────────────────────────────────────────────────────────────────────────────
# Job execution
# ─────────────────────────────────────────────────────────────────────────────

# Thiếu ít hơn ngần này giây thì không chèn clip: một clip chỉ hiện chưa tới
# một giây rồi cắt trông như lỗi dựng, để clip chính lặp nhẹ còn đỡ lộ hơn.
MIN_FILL_GAP = 1.5


def fill_short_segments(segments: list[dict], sources: list[dict],
                        wps: float) -> list[dict]:
    """Đoạn nào có lời đọc dài hơn clip thì nối thêm clip khác cho đủ.

    Chỉ lấy clip chưa dùng trong video này, ưu tiên clip ít xuất hiện nhất rồi
    tới clip dài nhất để cần ít mảnh ghép. Hết clip rảnh thì thôi, renderer sẽ
    lặp clip cuối cho đủ.
    """
    by_id = {str(s.get("id") or ""): s for s in sources}
    taken = {str(s.get("clip_id") or "") for s in segments if s.get("clip_id")}
    pool = sorted(
        (s for s in sources if str(s.get("id")) not in taken),
        key=lambda s: (s.get("times_used") or 0, -(s.get("duration") or 0)),
    )

    out = []
    for seg in segments:
        item = dict(seg)
        item["fill_ids"] = []
        main = by_id.get(str(seg.get("clip_id") or ""))
        words = len(str(seg.get("vo") or "").split())
        if main and words:
            need = words / max(1.5, wps) + 0.25
            have = float(main.get("duration") or 0)
            while have < need - MIN_FILL_GAP and pool:
                extra = pool.pop(0)
                item["fill_ids"].append(extra["id"])
                have += float(extra.get("duration") or 0)
        out.append(item)
    return out


def _brand_info(brand_id: str) -> tuple[str, str]:
    """(tên, bối cảnh) của brand — dùng nhồi vào prompt AI."""
    brand = STORE.get_resource("brand", brand_id) if brand_id else None
    if not brand:
        return "", ""
    return str(brand.get("name") or ""), str(brand.get("context") or "")


def _job_analyze(job: dict, payload: dict) -> dict:
    from tools.source_analyzer import analyze_source

    sid = str(payload.get("source_id") or "")
    src = STORE.get_resource("source", sid)
    if not src:
        raise RuntimeError(f"Không tìm thấy source {sid}.")
    path = config.SOURCES_DIR / str(src.get("file") or "")
    if not path.exists():
        STORE.update_resource("source", sid, status="error", error="Mất file clip.")
        raise RuntimeError("File clip không tồn tại.")

    STORE.update_resource("source", sid, status="analyzing")
    # Thumb trước khi gọi AI: clip phân tích lỗi vẫn xem được hình trong kho.
    thumb = config.SOURCES_DIR / f"{sid}.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-ss", "0.5", "-i", str(path), "-frames:v", "1",
         "-vf", "scale=360:-2", "-q:v", "5", str(thumb)],
        capture_output=True, timeout=60,
    )
    brand_name, brand_context = _brand_info(str(src.get("brand") or ""))
    try:
        info = analyze_source(str(path), brand_name, brand_context)
    except Exception as exc:
        STORE.update_resource("source", sid, status="error", error=str(exc)[:300])
        raise

    STORE.update_resource("source", sid, status="ready", error="", **info)
    return {"source_id": sid, "description": info["description"]}


def _job_render(job: dict, payload: dict) -> dict:
    from tools.montage_render import RenderCancelled, render_montage

    draft_id = str(payload.get("draft_id") or "")
    draft = STORE.get_resource("draft", draft_id)
    if not draft:
        raise RuntimeError(f"Không tìm thấy nháp {draft_id}.")

    def _clip_path(cid: str) -> str:
        src = STORE.get_resource("source", cid) if cid else None
        if not src or not src.get("file"):
            return ""
        p = config.SOURCES_DIR / src["file"]
        return str(p) if p.exists() else ""

    segments = []
    used_clip_ids = []
    for seg in draft.get("segments") or []:
        # clip chính trước, rồi các clip bù cho đoạn lời dài hơn clip
        ids = [str(seg.get("clip_id") or "")] + [
            str(x) for x in (seg.get("fill_ids") or [])]
        paths = []
        for cid in ids:
            p = _clip_path(cid)
            if p:
                paths.append(p)
                used_clip_ids.append(cid)
        segments.append({"vo": seg.get("vo") or "", "clip_paths": paths})

    voice = config.voice_choice(payload.get("voice") or draft.get("voice") or "")
    spec = {
        "job_id": job["id"][:12],
        "title": draft.get("title") or "",
        "hook_sub": draft.get("hook_sub") or "",
        "transition": payload.get("transition") or config.TRANSITION,
        "voice_provider": voice["provider"],
        "voice_id": voice["voice"],
        "voice_wps": voice["wps"],
        "bgm": payload.get("bgm") or "random",
        "target_seconds": int(draft.get("seconds") or 0),
        "segments": segments,
    }

    def cancel_check() -> bool:
        current = STORE.get_job(job["id"]) or {}
        return bool(current.get("cancel_requested"))

    try:
        result = render_montage(spec, cancel_check)
    except RenderCancelled:
        raise
    if not result.get("success"):
        raise RuntimeError(result.get("error") or "Render thất bại.")

    def _rel(p: str) -> str:
        try:
            return str(Path(p).resolve().relative_to(config.OUTPUT_DIR.resolve()))
        except ValueError:
            return p

    video = STORE.insert_resource("video", {
        "brand": draft.get("brand") or "",
        "title": draft.get("title") or "",
        "topic": draft.get("topic") or "",
        "draft_id": draft_id,
        "status": "done",
        "video": _rel(result["video_path"]),
        "preview": _rel(result["preview_path"]) if result.get("preview_path") else "",
        "thumb": _rel(result["thumb_path"]) if result.get("thumb_path") else "",
        "duration": result.get("duration") or 0,
        "bgm": result.get("bgm") or "",
    })
    STORE.update_resource("draft", draft_id, status="rendered", video_id=video["id"])
    for cid in used_clip_ids:
        src = STORE.get_resource("source", cid) or {}
        STORE.update_resource("source", cid,
                              times_used=int(src.get("times_used") or 0) + 1)
    return {"video_id": video["id"], "duration": result.get("duration")}


def _execute_job(job: dict) -> dict:
    payload = job.get("payload") or {}
    kind = job.get("kind")
    if kind == "analyze":
        return _job_analyze(job, payload)
    if kind == "render":
        return _job_render(job, payload)
    raise RuntimeError(f"Loại job không hỗ trợ: {kind}")


def _worker_loop(kinds: set[str], label: str) -> None:
    worker_id = f"{label}-{uuid.uuid4().hex[:8]}"
    while True:
        try:
            job = STORE.claim_next(worker_id, kinds=kinds)
        except Exception as exc:
            print(f"[jobs] claim lỗi: {exc}", file=sys.stderr)
            time.sleep(2)
            continue
        if not job:
            time.sleep(1)
            continue

        stop_heartbeat = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(10):
                try:
                    STORE.heartbeat(job["id"], worker_id)
                except Exception:
                    pass

        pulse = threading.Thread(target=heartbeat, daemon=True)
        pulse.start()
        try:
            print(f"[jobs] bắt đầu {job['id'][:8]} kind={job['kind']}", file=sys.stderr)
            result = _execute_job(job)
            STORE.complete_job(job["id"], worker_id, result)
            print(f"[jobs] ✓ {job['id'][:8]}", file=sys.stderr)
        except Exception as exc:
            retryable = "quá thời gian" in str(exc)
            STORE.fail_job(job["id"], worker_id, str(exc)[:500], retryable=retryable)
            payload = job.get("payload") or {}
            if job.get("kind") == "render" and payload.get("draft_id"):
                STORE.update_resource(
                    "draft", str(payload["draft_id"]),
                    status="draft", render_error=str(exc)[:300],
                )
            print(f"[jobs] ✗ {job['id'][:8]}: {exc}", file=sys.stderr)
        finally:
            stop_heartbeat.set()
            pulse.join(timeout=1)


def run_workers() -> list[threading.Thread]:
    recovery = STORE.recover_stale_jobs(120)
    if recovery.get("recovered") or recovery.get("failed"):
        print(f"[jobs] recovery {recovery}", file=sys.stderr)
    threads = []
    for kinds, label in (({"analyze"}, "analyze"), ({"render"}, "render")):
        t = threading.Thread(target=_worker_loop, args=(kinds, label), daemon=True)
        t.start()
        threads.append(t)
    return threads


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────

def _source_view(src: dict) -> dict:
    sid = src.get("id", "")
    return {
        "id": sid,
        "brand": src.get("brand", ""),
        "status": src.get("status", "pending"),
        "original_name": src.get("original_name", ""),
        "description": src.get("description", ""),
        "tags": src.get("tags") or [],
        "location_guess": src.get("location_guess", ""),
        "duration": src.get("duration") or 0,
        "width": src.get("width") or 0,
        "height": src.get("height") or 0,
        "times_used": src.get("times_used") or 0,
        "error": src.get("error", ""),
        "time": src.get("time") or 0,
        "thumb_url": f"/source-thumb/{sid}",
        "video_url": f"/source-video/{sid}",
    }


def _video_view(v: dict) -> dict:
    vid = v.get("id", "")
    return {
        "id": vid,
        "brand": v.get("brand", ""),
        "title": v.get("title", ""),
        "topic": v.get("topic", ""),
        "duration": v.get("duration") or 0,
        "bgm": v.get("bgm", ""),
        "time": v.get("time") or 0,
        "thumb_url": f"/media/thumb/{vid}",
        "preview_url": f"/media/preview/{vid}",
        "video_url": f"/media/video/{vid}",
    }


def _job_view(j: dict) -> dict:
    return {
        "id": j.get("id", ""),
        "kind": j.get("kind", ""),
        "status": j.get("status", ""),
        "error": j.get("error", ""),
        "payload": j.get("payload") or {},
        "result": j.get("result") or {},
        "created_at": j.get("created_at") or 0,
        "finished_at": j.get("finished_at") or 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        """Client đóng keep-alive không phải lỗi server; tránh in traceback nhiễu."""
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):  # bớt noise console
        pass

    # ── helpers ──────────────────────────────────────────────────────────

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 10 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _serve_file(self, path: Path, content_type: str = "",
                    download_name: str = "") -> None:
        if not path.is_file():
            self._json_response({"error": "Không tìm thấy file."}, 404)
            return
        if not content_type:
            ext = path.suffix.lower()
            content_type = {
                ".mp4": "video/mp4", ".jpg": "image/jpeg", ".png": "image/png",
                ".html": "text/html; charset=utf-8", ".mp3": "audio/mpeg",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(ext, "application/octet-stream")
        size = path.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_header and range_header.startswith("bytes="):
            try:
                spec = range_header[6:].split(",")[0]
                s, _, e = spec.partition("-")
                start = int(s) if s else max(0, size - int(e))
                end = int(e) if (s and e) else size - 1
                end = min(end, size - 1)
                if start <= end:
                    status = 206
                else:
                    start, end = 0, size - 1
            except ValueError:
                start, end = 0, size - 1
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{download_name}"')
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    # ── GET ──────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in {"/", "/index.html", "/app"}:
            self._serve_file(ROOT / "web" / "index.html", "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            name = path[len("/static/"):]
            target = (ROOT / "web" / name).resolve()
            if target.parent != (ROOT / "web").resolve():
                self._json_response({"error": "Đường dẫn không hợp lệ."}, 400)
                return
            self._serve_file(target)
        elif path == "/health":
            self._json_response({"status": "ok", "port": config.PORT})
        elif path == "/brands":
            self._json_response({"brands": STORE.list_resources("brand")})
        elif path == "/voices":
            has_vivibe = bool(config.vivibe_key())
            self._json_response({
                "voices": [
                    {**v, "ready": v["provider"] != "vivibe" or has_vivibe}
                    for v in config.VOICE_CHOICES
                ],
                "default": config.DEFAULT_VOICE,
            })
        elif path == "/sources":
            brand = (query.get("brand") or [""])[0]
            items = [
                _source_view(s) for s in STORE.list_resources("source")
                if not brand or str(s.get("brand") or "") == brand
            ]
            self._json_response({"sources": items})
        elif path.startswith("/source-thumb/"):
            sid = path.rsplit("/", 1)[-1]
            self._serve_file(config.SOURCES_DIR / f"{sid}.jpg")
        elif path.startswith("/source-video/"):
            sid = path.rsplit("/", 1)[-1]
            src = STORE.get_resource("source", sid) or {}
            self._serve_file(config.SOURCES_DIR / str(src.get("file") or "_none_"))
        elif path == "/drafts":
            items = STORE.list_resources("draft")
            self._json_response({"drafts": items})
        elif path == "/videos":
            items = [_video_view(v) for v in STORE.list_resources("video")]
            self._json_response({"videos": items})
        elif path.startswith("/media/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) != 3 or parts[1] not in {"video", "preview", "thumb"}:
                self._json_response({"error": "URL media không hợp lệ."}, 404)
                return
            record = STORE.get_resource("video", parts[2]) or {}
            rel = str(record.get(parts[1]) or "")
            if not rel:
                self._json_response({"error": "Không có file."}, 404)
                return
            name = ""
            if "download" in query:
                safe_title = "".join(
                    c for c in (record.get("title") or "video")
                    if c.isalnum() or c in " -_"
                ).strip() or "video"
                name = f"{safe_title}.mp4"
            self._serve_file(config.OUTPUT_DIR / rel, download_name=name)
        elif path == "/jobs":
            jobs = [_job_view(j) for j in STORE.list_jobs(limit=30)]
            self._json_response({"jobs": jobs})
        elif path == "/bgm-list":
            files = sorted(
                f.name for f in list(config.MUSIC_DIR.glob("*.mp3"))
                + list(config.MUSIC_DIR.glob("*.wav"))
            )
            self._json_response({"files": files})
        else:
            self._json_response({"error": "Not found"}, 404)

    # ── POST ─────────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/brands/save":
                self.handle_brand_save()
            elif path == "/brands/delete":
                self.handle_brand_delete()
            elif path == "/uploads/init":
                self.handle_upload_init()
            elif path.startswith("/uploads/") and path.endswith("/complete"):
                self.handle_upload_complete(path)
            elif path == "/sources/update":
                self.handle_source_update()
            elif path == "/sources/delete":
                self.handle_source_delete()
            elif path == "/sources/reanalyze":
                self.handle_source_reanalyze()
            elif path == "/draft-generate":
                self.handle_draft_generate()
            elif path == "/draft-update":
                self.handle_draft_update()
            elif path == "/draft-render":
                self.handle_draft_render()
            elif path == "/drafts/delete":
                self.handle_draft_delete()
            elif path == "/videos/delete":
                self.handle_video_delete()
            elif path == "/jobs/cancel":
                self.handle_job_cancel()
            else:
                self._json_response({"error": f"Unknown path: {path}"}, 404)
        except (QueueLimitError, UploadValidationError, PipelineStoreError) as exc:
            self._json_response({"success": False, "error": str(exc)}, 400)
        except BaseException as exc:
            # BaseException chứ không phải Exception: lỗi kiểu MemoryError hay
            # SystemExit trong thread làm request chết câm, client chỉ thấy
            # "connection closed" mà không có manh mối nào.
            log_error(f"POST {path}")
            self._json_response({"success": False, "error": str(exc) or repr(exc)}, 500)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/uploads/"):
            self.handle_upload_chunk(path)
            return
        self._json_response({"error": f"Unknown path: {path}"}, 404)

    # ── Brands ───────────────────────────────────────────────────────────

    def handle_brand_save(self):
        body = self._read_json_body()
        name = str(body.get("name") or "").strip()
        if not name:
            self._json_response({"success": False, "error": "Thiếu tên thương hiệu."}, 400)
            return
        bid = str(body.get("id") or "").strip()
        context = str(body.get("context") or "").strip()
        if bid and STORE.get_resource("brand", bid):
            item = STORE.update_resource("brand", bid, name=name, context=context)
        else:
            item = STORE.insert_resource("brand", {"name": name, "context": context})
        self._json_response({"success": True, "brand": item})

    def handle_brand_delete(self):
        body = self._read_json_body()
        bid = str(body.get("id") or "")
        in_use = [s for s in STORE.list_resources("source")
                  if str(s.get("brand") or "") == bid]
        if in_use:
            self._json_response(
                {"success": False,
                 "error": f"Còn {len(in_use)} clip thuộc thương hiệu này; xóa clip trước."},
                409)
            return
        ok = STORE.delete_resource("brand", bid)
        self._json_response({"success": ok}, 200 if ok else 404)

    # ── Upload ───────────────────────────────────────────────────────────

    def handle_upload_init(self):
        body = self._read_json_body()
        files = body.get("files") if isinstance(body.get("files"), list) else []
        session = STORE.create_upload_session(
            owner=OWNER,
            kind="source_clips",
            files=files,
            max_file_bytes=MAX_UPLOAD_FILE_BYTES,
            max_job_bytes=MAX_UPLOAD_JOB_BYTES,
            max_active_sessions=5,
            reserve_free_bytes=UPLOAD_DISK_RESERVE_BYTES,
        )
        self._json_response({"success": True, "upload": session}, 201)

    def handle_upload_chunk(self, path: str):
        parts = [p for p in path.split("/") if p]
        if len(parts) != 3:
            self._json_response({"success": False, "error": "URL upload không hợp lệ."}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            offset = int(self.headers.get("X-Upload-Offset") or 0)
            result = STORE.append_upload_chunk(
                session_id=parts[1],
                file_id=parts[2],
                owner=OWNER,
                offset=offset,
                length=length,
                source=self.rfile,
                max_chunk_bytes=MAX_UPLOAD_CHUNK_BYTES,
            )
            self._json_response({"success": True, **result})
        except UploadValidationError as exc:
            status = 409 if "Offset" in str(exc) else 400
            self._json_response({"success": False, "error": str(exc)}, status)

    def handle_upload_complete(self, path: str):
        parts = [p for p in path.split("/") if p]
        session_id = parts[1]
        brand = str((self._read_json_body() or {}).get("brand") or "")
        result = STORE.complete_upload(session_id, OWNER)

        created = []
        for item in result.get("files") or []:
            src_path = STORE.upload_root / str(item.get("relative_path") or "")
            if not src_path.is_file():
                continue
            ext = src_path.suffix.lower()
            if ext not in VIDEO_EXTS:
                continue
            sid = uuid.uuid4().hex[:12]
            dest = config.SOURCES_DIR / f"{sid}{ext}"
            shutil.move(str(src_path), str(dest))
            source = STORE.insert_resource("source", {
                "id": sid,
                "brand": brand,
                "file": dest.name,
                "original_name": item.get("original_name") or item.get("name") or "",
                "status": "pending",
                "times_used": 0,
            })
            STORE.create_job(kind="analyze", owner=OWNER,
                             payload={"source_id": sid}, max_attempts=2)
            created.append(_source_view(source))
        try:
            STORE.cancel_upload(session_id, OWNER)
        except Exception:
            pass
        self._json_response({"success": True, "sources": created})

    # ── Sources ──────────────────────────────────────────────────────────

    def handle_source_update(self):
        body = self._read_json_body()
        sid = str(body.get("id") or "")
        fields = {}
        if "description" in body:
            fields["description"] = str(body.get("description") or "").strip()
        if "tags" in body:
            raw = body.get("tags")
            if isinstance(raw, str):
                raw = [t.strip() for t in raw.split(",")]
            fields["tags"] = [str(t).strip() for t in (raw or []) if str(t).strip()]
        if "location_guess" in body:
            fields["location_guess"] = str(body.get("location_guess") or "").strip()
        item = STORE.update_resource("source", sid, **fields) if fields else None
        if not item:
            self._json_response({"success": False, "error": "Không tìm thấy source."}, 404)
            return
        self._json_response({"success": True, "source": _source_view(item)})

    def handle_source_delete(self):
        body = self._read_json_body()
        sid = str(body.get("id") or "")
        src = STORE.get_resource("source", sid)
        if not src:
            self._json_response({"success": False, "error": "Không tìm thấy source."}, 404)
            return
        for name in (str(src.get("file") or ""), f"{sid}.jpg"):
            if name:
                (config.SOURCES_DIR / name).unlink(missing_ok=True)
        STORE.delete_resource("source", sid)
        self._json_response({"success": True})

    def handle_source_reanalyze(self):
        body = self._read_json_body()
        sid = str(body.get("id") or "")
        src = STORE.get_resource("source", sid)
        if not src:
            self._json_response({"success": False, "error": "Không tìm thấy source."}, 404)
            return
        STORE.update_resource("source", sid, status="pending", error="")
        job, _ = STORE.create_job(kind="analyze", owner=OWNER,
                                  payload={"source_id": sid}, max_attempts=2)
        self._json_response({"success": True, "job_id": job["id"]})

    # ── Drafts ───────────────────────────────────────────────────────────

    def handle_draft_generate(self):
        from tools.clip_matcher import match_clips
        from tools.script_gen import generate_script, ground_segments

        body = self._read_json_body()
        topic = str(body.get("topic") or "").strip()
        if not topic:
            self._json_response({"success": False, "error": "Thiếu chủ đề."}, 400)
            return
        extra = str(body.get("extra_prompt") or "").strip()
        brand_id = str(body.get("brand") or "")
        if not brand_id or not STORE.get_resource("brand", brand_id):
            self._json_response(
                {"success": False, "error": "Hãy chọn một thương hiệu hợp lệ."}, 400)
            return
        brand_name, brand_context = _brand_info(brand_id)
        seconds = int(body.get("seconds") or 60)
        voice = config.voice_choice(body.get("voice") or "")

        # Chỉ lấy clip của đúng thương hiệu — clip brand khác không được lọt vào.
        sources = [
            s for s in STORE.list_resources("source")
            if s.get("status") == "ready" and str(s.get("brand") or "") == brand_id
        ]
        if len(sources) < 3:
            self._json_response(
                {"success": False,
                 "error": "Cần ít nhất 3 clip đã phân tích xong để tạo video."}, 400)
            return
        print(f"[draft] {len(sources)} clip, đang viết kịch bản...",
              file=sys.stderr, flush=True)
        script = generate_script(
            topic, extra, brand_name, brand_context, seconds,
            sources=[{"description": s.get("description", ""),
                      "tags": s.get("tags") or []} for s in sources],
            wps=voice["wps"],
        )
        # Script đã thấy cả kho nên tự chọn clip cho phần lớn đoạn; matcher chỉ
        # phải lo các đoạn nó bỏ trống, tránh hai bước chọn ngược nhau.
        matches = [
            {"clip_id": sources[seg["clip_index"]]["id"], "reason": "Kịch bản chọn"}
            if seg.get("clip_index", -1) >= 0 else {"clip_id": "", "reason": ""}
            for seg in script["segments"]
        ]
        if any(not m["clip_id"] for m in matches):
            taken = {m["clip_id"] for m in matches if m["clip_id"]}
            free = [s for s in sources if s["id"] not in taken]
            todo = [i for i, m in enumerate(matches) if not m["clip_id"]]
            last = len(script["segments"]) - 1
            filled = match_clips(
                [{**script["segments"][i],
                  "role": "mở đầu" if i == 0 else "KẾT" if i == last else "đoạn giữa"}
                 for i in todo],
                [{"id": s["id"], "description": s.get("description", ""),
                  "tags": s.get("tags") or [], "duration": s.get("duration") or 0,
                  "times_used": s.get("times_used") or 0} for s in free],
            )
            for i, m in zip(todo, filled):
                matches[i] = m

        # Đoạn không có clip sẽ render ra nền trơn — tệ hơn nhiều so với một clip
        # hơi lệch. Lấp bằng clip chưa dùng, ưu tiên clip ít xuất hiện nhất.
        for i, m in enumerate(matches):
            if m["clip_id"]:
                continue
            taken = {x["clip_id"] for x in matches if x["clip_id"]}
            spare = sorted((s for s in sources if s["id"] not in taken),
                           key=lambda s: s.get("times_used") or 0)
            if spare:
                matches[i] = {"clip_id": spare[0]["id"],
                              "reason": "Tự lấp vì không tìm được clip khớp"}
        grounded = ground_segments(
            topic, script["segments"], matches, sources, seconds=seconds,
            wps=voice["wps"],
        )
        source_by_id = {str(s.get("id") or ""): s for s in sources}
        segments = [
            {
                **seg,
                "clip_id": m["clip_id"],
                "match_reason": (
                    "Footage: " + str(
                        source_by_id.get(m["clip_id"], {}).get("description") or ""
                    )[:180]
                    if m["clip_id"] else m["reason"]
                ),
            }
            for seg, m in zip(grounded, matches)
        ]
        segments = fill_short_segments(segments, sources, voice["wps"])
        draft = STORE.insert_resource("draft", {
            "brand": brand_id,
            "topic": topic,
            "extra_prompt": extra,
            "seconds": seconds,
            "voice": voice["id"],
            "title": script["title"],
            "hook_sub": script["hook_sub"],
            "segments": segments,
            "status": "draft",
        })
        self._json_response({"success": True, "draft": draft})

    def handle_draft_update(self):
        body = self._read_json_body()
        did = str(body.get("id") or "")
        fields = {}
        for key in ("title", "hook_sub"):
            if key in body:
                fields[key] = str(body.get(key) or "").strip()
        if body.get("voice"):
            fields["voice"] = config.voice_choice(body["voice"])["id"]
        if isinstance(body.get("segments"), list):
            fields["segments"] = [
                {
                    "vo": str(s.get("vo") or "").strip(),
                    "scene": str(s.get("scene") or "").strip(),
                    "clip_id": str(s.get("clip_id") or ""),
                    "fill_ids": [str(x) for x in (s.get("fill_ids") or []) if x],
                    "match_reason": str(s.get("match_reason") or ""),
                }
                for s in body["segments"]
                if str(s.get("vo") or "").strip()
            ]
        item = STORE.update_resource("draft", did, **fields) if fields else None
        if not item:
            self._json_response({"success": False, "error": "Không tìm thấy nháp."}, 404)
            return
        self._json_response({"success": True, "draft": item})

    def handle_draft_render(self):
        body = self._read_json_body()
        did = str(body.get("id") or "")
        draft = STORE.get_resource("draft", did)
        if not draft:
            self._json_response({"success": False, "error": "Không tìm thấy nháp."}, 404)
            return
        segments = draft.get("segments") or []
        if not segments:
            self._json_response(
                {"success": False, "error": "Nháp chưa có đoạn nào để render."}, 400)
            return
        brand_id = str(draft.get("brand") or "")
        clip_ids: list[str] = []
        for index, segment in enumerate(segments, start=1):
            if not str(segment.get("vo") or "").strip():
                self._json_response(
                    {"success": False, "error": f"Đoạn {index} chưa có lời đọc."}, 400)
                return
            clip_id = str(segment.get("clip_id") or "")
            source = STORE.get_resource("source", clip_id) if clip_id else None
            if not source or str(source.get("brand") or "") != brand_id:
                self._json_response(
                    {"success": False,
                     "error": f"Đoạn {index} chưa chọn clip hợp lệ của thương hiệu."}, 400)
                return
            clip_ids.append(clip_id)
            # Clip bù cũng tính vào luật không trùng: cùng một clip xuất hiện hai
            # lần trong video trông như lỗi dựng.
            for extra in segment.get("fill_ids") or []:
                extra = str(extra)
                src = STORE.get_resource("source", extra) if extra else None
                if src and str(src.get("brand") or "") == brand_id:
                    clip_ids.append(extra)
        if len(set(clip_ids)) != len(clip_ids):
            self._json_response(
                {"success": False,
                 "error": "Có clip bị dùng ở hai chỗ. Đổi lại trước khi render."}, 400)
            return
        job, _ = STORE.create_job(
            kind="render",
            owner=OWNER,
            payload={
                "draft_id": did,
                "transition": str(body.get("transition") or ""),
                "bgm": str(body.get("bgm") or "random"),
                "voice": str(body.get("voice") or draft.get("voice") or ""),
            },
            max_attempts=1,
        )
        STORE.update_resource("draft", did, status="rendering", job_id=job["id"])
        self._json_response({"success": True, "job_id": job["id"],
                             "position": STORE.queue_position(job["id"])})

    def handle_draft_delete(self):
        body = self._read_json_body()
        ok = STORE.delete_resource("draft", str(body.get("id") or ""))
        self._json_response({"success": ok}, 200 if ok else 404)

    # ── Videos / jobs ────────────────────────────────────────────────────

    def handle_video_delete(self):
        body = self._read_json_body()
        vid = str(body.get("id") or "")
        record = STORE.get_resource("video", vid)
        if not record:
            self._json_response({"success": False, "error": "Không tìm thấy video."}, 404)
            return
        for key in ("video", "preview", "thumb"):
            rel = str(record.get(key) or "")
            if rel:
                (config.OUTPUT_DIR / rel).unlink(missing_ok=True)
        STORE.delete_resource("video", vid)
        self._json_response({"success": True})

    def handle_job_cancel(self):
        body = self._read_json_body()
        job = STORE.cancel_job(str(body.get("id") or ""), OWNER, is_admin=True)
        if not job:
            self._json_response({"success": False, "error": "Không hủy được job."}, 404)
            return
        payload = job.get("payload") or {}
        if job.get("kind") == "render" and payload.get("draft_id"):
            STORE.update_resource("draft", str(payload["draft_id"]), status="draft")
        self._json_response({"success": True, "job": _job_view(job)})


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_default_sfx() -> None:
    """Chưa có file whoosh nào → tạo 1 whoosh tổng hợp bằng ffmpeg làm mặc định."""
    if any(config.SFX_DIR.glob("*.mp3")) or any(config.SFX_DIR.glob("*.wav")):
        return
    out = config.SFX_DIR / "whoosh_default.mp3"
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anoisesrc=d=0.6:c=pink:a=0.8",
         "-af", "highpass=f=500,lowpass=f=4500,"
                "afade=t=in:st=0:d=0.22,afade=t=out:st=0.3:d=0.3,volume=1.4",
         str(out)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode == 0:
        print(f"[setup] tạo SFX mặc định: {out.name}", file=sys.stderr)


def _port_already_serving() -> bool:
    """Windows cho phép hai process bind cùng cổng qua SO_REUSEADDR, nên server
    thứ hai vẫn khởi động được và request rơi ngẫu nhiên vào bản cũ đang chạy
    code cũ. Kiểm tra trước để không mất công debug bóng ma."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{config.PORT}/health", timeout=2) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def main() -> None:
    if _port_already_serving():
        print(f"Đã có server chạy ở cổng {config.PORT}. Tắt bản cũ trước khi "
              f"chạy lại, nếu không hai bản sẽ tranh nhau request.", file=sys.stderr)
        raise SystemExit(1)
    _ensure_default_sfx()
    if not any(config.MUSIC_DIR.iterdir()):
        print("[setup] assets/music đang trống — thả file .mp3 vào để có nhạc nền.",
              file=sys.stderr)
    run_workers()
    server = ThreadingHTTPServer(("127.0.0.1", config.PORT), Handler)
    print(f"Server chạy tại http://localhost:{config.PORT}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] dừng.", file=sys.stderr)


if __name__ == "__main__":
    main()
