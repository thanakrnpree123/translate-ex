"""Doc Reader — อัปโหลดเอกสาร/รูป แล้วให้อ่านออกเสียงเป็นภาษาที่เลือก พร้อมบันทึกเสียงคู่กับไฟล์ต้นฉบับ"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import edge_tts
from flask import Flask, abort, jsonify, render_template, request, send_from_directory

BASE = Path(__file__).resolve().parent
SAVED = BASE / "saved"
UPLOADS = BASE / "uploads"
VOICES = BASE / "voices"
for d in (SAVED, UPLOADS, VOICES):
    d.mkdir(exist_ok=True)

MIN_SAMPLE_SEC = 10

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
TEXT_EXT = {".txt", ".md", ".csv"}

# code: (ชื่อที่แสดง, เสียงผู้หญิง, เสียงผู้ชาย, รหัสสำหรับแปล, รหัส OCR)
LANGUAGES = {
    "th": ("ไทย", "th-TH-PremwadeeNeural", "th-TH-NiwatNeural", "th", "th-TH"),
    "en": ("English", "en-US-JennyNeural", "en-US-GuyNeural", "en", "en-US"),
    "zh": ("中文 (จีน)", "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN", "zh-Hans"),
    "ja": ("日本語 (ญี่ปุ่น)", "ja-JP-NanamiNeural", "ja-JP-KeitaNeural", "ja", "ja-JP"),
    "ko": ("한국어 (เกาหลี)", "ko-KR-SunHiNeural", "ko-KR-InJoonNeural", "ko", "ko-KR"),
    "vi": ("Tiếng Việt (เวียดนาม)", "vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural", "vi", "vi-VT"),
    "fr": ("Français (ฝรั่งเศส)", "fr-FR-DeniseNeural", "fr-FR-HenriNeural", "fr", "fr-FR"),
    "de": ("Deutsch (เยอรมัน)", "de-DE-KatjaNeural", "de-DE-ConradNeural", "de", "de-DE"),
    "es": ("Español (สเปน)", "es-ES-ElviraNeural", "es-ES-AlvaroNeural", "es", "es-ES"),
    "ru": ("Русский (รัสเซีย)", "ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural", "ru", "ru-RU"),
    "id": ("Bahasa Indonesia", "id-ID-GadisNeural", "id-ID-ArdiNeural", "id", "id-ID"),
    "ms": ("Bahasa Melayu", "ms-MY-YasminNeural", "ms-MY-OsmanNeural", "ms", "ms-MY"),
}

ALT_VOICE = {v1: v2 for _, f, m, _, _ in LANGUAGES.values() for v1, v2 in ((f, m), (m, f))}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


# ---------- อ่านข้อความจากไฟล์ ----------

def ocr_image(path: Path) -> str:
    from ocrmac import ocrmac

    langs = ["th-TH", "en-US"]
    results = ocrmac.OCR(str(path), language_preference=langs).recognize()
    # Vision ให้พิกัดจากมุมล่างซ้าย: เรียงจากบนลงล่าง แล้วซ้ายไปขวา
    results.sort(key=lambda r: (-round(r[2][1] + r[2][3], 2), r[2][0]))
    return "\n".join(r[0] for r in results)


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        text = "\n\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
        if not text.strip():
            raise ValueError("PDF นี้ไม่มีข้อความ (น่าจะเป็นไฟล์สแกน) ลองแปลงเป็นรูปแล้วอัปโหลดแทน")
        return text
    if ext == ".docx":
        import docx

        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    if ext in TEXT_EXT:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext in IMAGE_EXT:
        return ocr_image(path)
    raise ValueError(f"ยังไม่รองรับไฟล์ {ext}")


ITEM_RE = re.compile(r"^\s*(\d{1,3})\s*[.)]\s*(.+)$")


def parse_items(text: str) -> list[dict]:
    """หาข้อที่มีเลขกำกับ เช่น "1. หนูต้องกลับบ้าน..." (ข้ามบรรทัดที่มีแต่จุดไข่ปลา)"""
    items, seen = [], set()
    for line in text.splitlines():
        m = ITEM_RE.match(line)
        if not m or not re.search(r"[A-Za-z\u0E00-\u0E7F]", m.group(2)):
            continue
        n = int(m.group(1))
        if n in seen:
            continue
        seen.add(n)
        items.append(dict(n=n, src=m.group(2).strip()))
    return sorted(items, key=lambda i: i["n"])


def clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------- แปลภาษาและสร้างเสียง ----------

def translate_lines(lines: list[str], target: str) -> list[str]:
    """แปลทีละบรรทัดโดยให้ผลลัพธ์ตรงกับบรรทัดเดิม"""
    out = translate("\n".join(lines), target).split("\n")
    if len(out) != len(lines):
        out = [translate(line, target) for line in lines]
    return [o.strip() for o in out]


def translate(text: str, target: str) -> str:
    import requests

    chunks, buf = [], ""
    for para in text.split("\n"):
        if len(buf) + len(para) > 4000:
            chunks.append(buf)
            buf = ""
        buf += para + "\n"
    chunks.append(buf)
    def via_gtx(chunk: str):
        r = requests.post("https://translate.googleapis.com/translate_a/single",
                          params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t"},
                          data={"q": chunk}, timeout=30)
        return r.status_code, (lambda: "".join(seg[0] for seg in r.json()[0] if seg[0])), r

    def via_clients5(chunk: str):
        r = requests.post("https://clients5.google.com/translate_a/t",
                          params={"client": "dict-chrome-ex", "sl": "auto", "tl": target},
                          data={"q": chunk}, timeout=30)
        return r.status_code, (lambda: r.json()[0][0] if isinstance(r.json()[0], list) else r.json()[0]), r

    out = []
    for chunk in (c for c in chunks if c.strip()):
        # Google จำกัดจำนวนครั้ง (429) เมื่อแปลหลายไฟล์ติดกัน: สลับไปอีกช่องทางหนึ่ง แล้วรอและลองใหม่
        result = None
        for wait in (3, 10, 20, 0):
            for endpoint in (via_gtx, via_clients5):
                status, parse, r = endpoint(chunk)
                if status == 200:
                    result = parse()
                    break
                if status != 429:
                    r.raise_for_status()
            if result is not None or not wait:
                break
            time.sleep(wait)
        if result is None:
            raise RuntimeError("Google แปลภาษาให้ไม่ทัน (ใช้งานถี่เกินไป) รอสักครู่แล้วกดสร้างเสียงอีกครั้ง")
        out.append(result)
    return "\n".join(out).strip()


def synthesize(text: str, voice: str, rate: int, out: Path) -> None:
    rate_str = f"{rate:+d}%"
    # บริการเสียงของ Microsoft ตอบ "No audio was received" เป็นครั้งคราว และบางประโยคพังกับบางเสียงเสมอ
    # (เช่น "คุณต้องไปเยี่ยมปู่ย่าตายายคุณ" กับเสียง Premwadee) จึงลองซ้ำ แล้วค่อยสลับไปใช้อีกเสียงของภาษาเดียวกัน
    attempts = [voice] * 3 + [ALT_VOICE.get(voice, voice)] * 2
    for k, v in enumerate(attempts):
        try:
            asyncio.run(asyncio.wait_for(edge_tts.Communicate(text, v, rate=rate_str).save(str(out)), 60))
            return
        except Exception:  # noqa: BLE001
            if k == len(attempts) - 1:
                raise RuntimeError(f"บริการเสียงอ่านประโยคนี้ไม่ได้: {text[:60]}")
            time.sleep(1)


def to_mp3(src: Path, out: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-b:a", "160k", str(out)], check=True)


def speak_segment(text: str, voice: str, rate: int, out_wav: Path, speaker: Path | None) -> None:
    """สร้างเสียงหนึ่งช่วงเป็น wav (ถ้ามี speaker จะเปลี่ยนเป็นเสียงผู้พูด)"""
    import voice as vc

    base = out_wav.with_suffix(".base.mp3")
    synthesize(text, voice, rate, base)
    if speaker:
        vc.convert(base, speaker / "se.pth", out_wav)
    else:
        vc.to_wav(base, out_wav)
    base.unlink()


def concat_wavs(parts: list, out_mp3: Path) -> None:
    """parts คือรายการไฟล์ wav หรือตัวเลข (วินาทีของช่วงเงียบ)"""
    from pydub import AudioSegment

    audio = AudioSegment.silent(duration=300, frame_rate=22050)
    for part in parts:
        if isinstance(part, (int, float)):
            audio += AudioSegment.silent(duration=int(part * 1000), frame_rate=22050)
        else:
            audio += AudioSegment.from_wav(part).set_frame_rate(22050).set_channels(1)
    audio.export(out_mp3, format="mp3", bitrate="160k")


def make_video(image: Path, audio: Path, out: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    # ทำให้ไฟล์เล็กที่สุด (ประหยัดพื้นที่ Google Drive) แต่ยังเปิดได้ทุกเครื่อง:
    # ภาพนิ่ง 1 เฟรม/วินาที กว้างไม่เกิน 1080 px (ตัวหนังสือยังอ่านชัด)
    # เสียง AAC โมโน 32 kbps พอสำหรับเสียงพูด — คลิป 2.5 นาทีเหลือราว 0.8 MB จากเดิม 6.5 MB
    # -shortest ตัดความยาวพลาดเมื่อใช้ 1 เฟรม/วินาที จึงกำหนดความยาวตามเสียงเอง
    secs = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                           str(audio)], capture_output=True, text=True).stdout.strip()
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-framerate", "1", "-loop", "1", "-t", secs, "-i", str(image),
        "-i", str(audio),
        "-vf", "scale='min(1080,trunc(iw/2)*2)':-2,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryslow", "-tune", "stillimage", "-crf", "28", "-r", "1", "-g", "600",
        "-c:a", "aac", "-ac", "1", "-ar", "22050", "-b:a", "32k",
        "-shortest", "-movflags", "+faststart", str(out),
    ]
    return subprocess.run(cmd).returncode == 0


# ---------- เส้นทางของเว็บ ----------

@app.get("/")
def index():
    return render_template("index.html", languages=LANGUAGES)


@app.post("/api/extract")
def api_extract():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="กรุณาเลือกไฟล์"), 400
    ext = Path(f.filename).suffix.lower()
    token = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = UPLOADS / f"{token}{ext}"
    f.save(path)
    try:
        text = clean(extract_text(path))
    except Exception as e:  # noqa: BLE001
        path.unlink(missing_ok=True)
        return jsonify(error=str(e)), 400
    relpath = (request.form.get("relpath") or f.filename).strip("/")[:500]
    return jsonify(token=token, ext=ext, original=Path(f.filename).name, relpath=relpath, text=text,
                   is_image=ext in IMAGE_EXT, items=parse_items(text))


@app.post("/api/translate-items")
def api_translate_items():
    data = request.get_json(force=True)
    lang = data.get("lang", "en")
    if lang not in LANGUAGES:
        return jsonify(error="ภาษาไม่ถูกต้อง"), 400
    srcs = [str(s) for s in data.get("items", [])]
    if not srcs:
        return jsonify(items=[])
    try:
        return jsonify(items=translate_lines(srcs, LANGUAGES[lang][3]))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"แปลภาษาไม่สำเร็จ: {e}"), 502


@app.post("/api/speak")
def api_speak():
    data = request.get_json(force=True)
    token, ext = data.get("token", ""), data.get("ext", "")
    upload = UPLOADS / f"{token}{ext}"
    if not re.fullmatch(r"[\d-]+", token) or not upload.exists():
        return jsonify(error="ไม่พบไฟล์ที่อัปโหลด กรุณาอัปโหลดใหม่"), 400
    lang = data.get("lang", "th")
    if lang not in LANGUAGES:
        return jsonify(error="ภาษาไม่ถูกต้อง"), 400
    label, female, male, tr_code, _ = LANGUAGES[lang]
    voice = male if data.get("gender") == "male" else female
    rate = max(-50, min(100, int(data.get("rate", 0))))
    text = clean(data.get("text", ""))
    speaker = None
    if data.get("voice_id"):
        speaker = voice_folder(data["voice_id"])
        if not speaker or not (speaker / "se.pth").exists():
            return jsonify(error="ไม่พบเสียงผู้พูดที่เลือก"), 400
        # ใช้เสียงต้นแบบเพศเดียวกับผู้พูด จะได้เสียงที่ใกล้เคียงกว่า
        voice = male if speaker_info(speaker).get("gender") == "male" else female

    worksheet = data.get("mode") == "worksheet"
    items = [dict(n=int(i.get("n", k + 1)), src=str(i.get("src", "")).strip(), dst=str(i.get("dst", "")).strip())
             for k, i in enumerate(data.get("items") or [])]
    items = [i for i in items if i["dst"]]
    if worksheet and not items:
        return jsonify(error="ไม่มีข้อให้อ่าน (ต้องมีคำตอบอย่างน้อย 1 ข้อ)"), 400
    if not worksheet and not text:
        return jsonify(error="ไม่มีข้อความให้อ่าน"), 400

    if worksheet:
        spoken = "\n".join(f"{i['n']}. {i['src']}\n   {i['dst']}" for i in items)
    else:
        try:
            spoken = translate(text, tr_code) if data.get("translate") else text
        except Exception as e:  # noqa: BLE001
            return jsonify(error=f"แปลภาษาไม่สำเร็จ: {e}"), 502

    original = data.get("original") or f"file{ext}"
    stem = re.sub(r"[^\w\u0E00-\u0E7F-]+", "_", Path(original).stem)[:40].strip("_") or "file"
    name = f"{datetime.now():%Y-%m-%d_%H%M%S}_{stem}_{lang}"
    folder, k = SAVED / name, 1
    while folder.exists():
        k += 1
        folder = SAVED / f"{name}_{k}"
    folder.mkdir()
    try:
        source = folder / f"original{ext}"
        shutil.copy2(upload, source)
        (folder / "text.txt").write_text(spoken, encoding="utf-8")
        if data.get("translate") and not worksheet:
            (folder / "text_original.txt").write_text(text, encoding="utf-8")
        audio = folder / "audio.mp3"
        tmp = folder / "tmp"
        tmp.mkdir()
        if worksheet:
            pause = max(0.0, min(15.0, float(data.get("pause", 3))))
            read_question = data.get("read_question", True)
            thai_voice = LANGUAGES["th"][1]
            parts = []
            for i in items:
                if read_question and i["src"]:
                    q = tmp / f"q{i['n']}.wav"
                    speak_segment(f"ข้อ {i['n']}. {i['src']}", thai_voice, rate, q, None)
                    parts += [q, pause]
                a = tmp / f"a{i['n']}.wav"
                speak_segment(i["dst"], voice, rate, a, speaker)
                parts += [a, 1.5]
            concat_wavs(parts, audio)
        else:
            whole = tmp / "all.wav"
            speak_segment(spoken, voice, rate, whole, speaker)
            to_mp3(whole, audio)
        shutil.rmtree(tmp)
        video = ext in IMAGE_EXT and make_video(source, audio, folder / "video.mp4")
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error=f"สร้างเสียงไม่สำเร็จ: {e}"), 502

    speaker_name = speaker_info(speaker)["name"] if speaker else None
    meta = dict(original=original, relpath=str(data.get("relpath") or original)[:500], lang=lang, language=label, voice=voice, rate=rate, speaker=speaker_name,
                translated=bool(data.get("translate")) or worksheet, mode="worksheet" if worksheet else "full",
                items=len(items) if worksheet else None, created=datetime.now().isoformat(timespec="seconds"),
                video=bool(video))
    (folder / "info.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify(item=describe(folder))


def describe(folder: Path) -> dict:
    meta = json.loads((folder / "info.json").read_text(encoding="utf-8"))
    files = {p.name: f"/saved/{folder.name}/{p.name}" for p in folder.iterdir()}
    source = next((n for n in files if n.startswith("original")), None)
    return dict(id=folder.name, **meta, files=files, source=source,
                is_image=bool(source) and Path(source).suffix.lower() in IMAGE_EXT,
                text=(folder / "text.txt").read_text(encoding="utf-8")[:400])


# ---------- บันทึกวิดีโอลงโฟลเดอร์ ex ข้างไฟล์ต้นฉบับ ----------

CONFIG = BASE / "config.json"


def load_config() -> dict:
    cfg = {"export_root": str(Path.home() / "Downloads")}
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    return cfg


def index_files(root: Path, max_depth: int = 4) -> dict[str, list[Path]]:
    """ชื่อไฟล์ -> โฟลเดอร์ที่มีไฟล์นั้น (ค้นลึกไม่เกิน max_depth ชั้น ข้ามโฟลเดอร์ ex และโฟลเดอร์ซ่อน)"""
    found: dict[str, list[Path]] = {}
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "ex"
                       and len(here.parts) - base_depth < max_depth]
        for f in filenames:
            found.setdefault(f, []).append(here)
    return found


@app.get("/api/export-config")
def api_export_config():
    return jsonify(load_config())


@app.post("/api/export-videos")
def api_export_videos():
    data = request.get_json(force=True)
    root = Path(os.path.expanduser(str(data.get("root") or load_config()["export_root"]))).resolve()
    if not root.is_dir():
        return jsonify(error=f"ไม่พบโฟลเดอร์ {root}"), 400
    CONFIG.write_text(json.dumps({"export_root": str(root)}, ensure_ascii=False, indent=2), encoding="utf-8")

    index = index_files(root)
    remove_after = bool(data.get("remove_after"))
    # จัดกลุ่มตามไฟล์ต้นฉบับ (path ในโฟลเดอร์ที่อัปโหลด): รายการแรกคืออันใหม่สุด
    groups: dict[str, list[tuple[Path, dict]]] = {}
    for d in sorted(SAVED.iterdir(), reverse=True):
        if not (d / "info.json").exists() or not (d / "video.mp4").exists():
            continue
        meta = json.loads((d / "info.json").read_text(encoding="utf-8"))
        groups.setdefault(meta.get("relpath") or meta["original"], []).append((d, meta))

    results, removed = [], 0
    for relpath, entries in groups.items():
        newest, meta = entries[0]
        original = meta["original"]
        dirs = index.get(original, [])
        # ถ้ารู้ว่าอัปโหลดมาจากโฟลเดอร์ไหน ให้เลือกโฟลเดอร์ที่ชื่อตรงกันก่อน
        parent = Path(relpath).parent.parts
        if parent and len(dirs) > 1:
            dirs = [x for x in dirs if x.parts[-len(parent):] == parent] or dirs
        if not dirs:
            results.append(dict(original=relpath, ok=False, message="ไม่พบไฟล์ต้นฉบับในโฟลเดอร์ที่เลือก"))
            continue
        if len(dirs) > 1:
            results.append(dict(original=relpath, ok=False,
                                message="พบไฟล์ชื่อนี้หลายที่: " + ", ".join(str(x.relative_to(root)) for x in dirs)))
            continue
        ex = dirs[0] / "ex"
        ex.mkdir(exist_ok=True)
        dest = ex / (Path(original).stem + ".mp4")
        replaced = dest.exists()
        shutil.copy2(newest / "video.mp4", dest)
        results.append(dict(original=relpath, ok=True, replaced=replaced,
                            path=str(dest.relative_to(root)), size_kb=dest.stat().st_size // 1024))
        if remove_after:  # บันทึกสำเร็จแล้ว: ลบออกจากรายการ รวมถึงรุ่นเก่าของไฟล์เดียวกัน
            for folder, _ in entries:
                shutil.rmtree(folder)
                removed += 1
    results.sort(key=lambda r: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", r["original"])])
    return jsonify(root=str(root), results=results, removed=removed)


@app.post("/api/reveal")
def api_reveal():
    """เปิด Finder ไปที่ไฟล์ที่เพิ่งบันทึก"""
    root = Path(load_config()["export_root"]).resolve()
    target = (root / str(request.get_json(force=True).get("path", ""))).resolve()
    if root not in target.parents or not target.exists():
        abort(404)
    subprocess.run(["open", "-R", str(target)])
    return jsonify(ok=True)


@app.get("/api/library")
def api_library():
    items = [describe(d) for d in sorted(SAVED.iterdir(), reverse=True)
             if d.is_dir() and (d / "info.json").exists()]
    return jsonify(items=items)


@app.delete("/api/library/<item_id>")
def api_delete(item_id):
    folder = (SAVED / item_id).resolve()
    if folder.parent != SAVED or not folder.is_dir():
        abort(404)
    shutil.rmtree(folder)
    return jsonify(ok=True)


# ---------- เสียงผู้พูด ----------

def speaker_info(folder: Path) -> dict:
    info_path = folder / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if "gender" not in info:  # โปรไฟล์เก่า: วัดระดับเสียงแล้วจำไว้
        import voice as vc

        info["pitch"] = round(vc.median_pitch(folder / "sample.wav"))
        info["gender"] = "male" if info["pitch"] < 165 else "female"
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def voice_folder(voice_id: str) -> Path | None:
    folder = (VOICES / voice_id).resolve()
    return folder if folder.parent == VOICES and folder.is_dir() else None


@app.get("/api/voices")
def api_voices():
    items = []
    for d in sorted(VOICES.iterdir(), reverse=True):
        if (d / "info.json").exists() and (d / "se.pth").exists():
            items.append(dict(id=d.name, **speaker_info(d),
                              sample=f"/voices/{d.name}/sample.wav"))
    return jsonify(items=items)


@app.post("/api/voices")
def api_voice_create():
    import voice as vc

    name = (request.form.get("name") or "").strip()[:40]
    f = request.files.get("audio")
    if not name:
        return jsonify(error="กรุณาตั้งชื่อเสียง"), 400
    if request.form.get("consent") != "yes":
        return jsonify(error="กรุณายืนยันว่าเป็นเสียงของคุณ หรือได้รับอนุญาตจากเจ้าของเสียงแล้ว"), 400
    if not f:
        return jsonify(error="กรุณาอัดเสียงหรือเลือกไฟล์เสียง"), 400

    folder = VOICES / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir()
    try:
        raw = folder / ("raw" + (Path(f.filename or "").suffix.lower() or ".webm"))
        f.save(raw)
        sample = folder / "sample.wav"
        vc.clean_sample(raw, sample)
        raw.unlink()
        secs = vc.duration(sample)
        if secs < MIN_SAMPLE_SEC:
            raise ValueError(f"มีเสียงพูดจริงแค่ {secs:.0f} วินาที (ตัดช่วงเงียบออกแล้ว) "
                             f"ต้องพูดอย่างน้อย {MIN_SAMPLE_SEC} วินาที แนะนำ 30–60 วินาที")
        vc.extract_embedding(sample, folder / "se.pth")
        pitch = round(vc.median_pitch(sample))
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error=f"บันทึกเสียงไม่สำเร็จ: {e}"), 400
    (folder / "info.json").write_text(json.dumps(dict(
        name=name, seconds=round(secs), pitch=pitch, gender="male" if pitch < 165 else "female", created=datetime.now().isoformat(timespec="seconds")),
        ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify(ok=True)


@app.delete("/api/voices/<voice_id>")
def api_voice_delete(voice_id):
    folder = voice_folder(voice_id)
    if not folder:
        abort(404)
    shutil.rmtree(folder)
    return jsonify(ok=True)


@app.get("/voices/<voice_id>/sample.wav")
def voice_sample(voice_id):
    folder = voice_folder(voice_id)
    if not folder:
        abort(404)
    return send_from_directory(folder, "sample.wav")


@app.get("/saved/<item_id>/<name>")
def saved_file(item_id, name):
    folder = (SAVED / item_id).resolve()
    if folder.parent != SAVED:
        abort(404)
    return send_from_directory(folder, name)


if __name__ == "__main__":
    print("เปิดเบราว์เซอร์ที่ http://127.0.0.1:5055")
    app.run(host="127.0.0.1", port=5055, debug=False)
