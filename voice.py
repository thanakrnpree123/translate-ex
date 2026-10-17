"""แปลงโทนเสียงให้เป็นเสียงผู้พูด ด้วยตัวแปลงเสียงของ OpenVoice V2 (ทำงานในเครื่อง)"""

import subprocess
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
CKPT = BASE / "models" / "openvoice" / "converter"

_lock = threading.Lock()
_model = None


def _converter():
    global _model
    if _model is None:
        if not (CKPT / "checkpoint.pth").exists():
            from huggingface_hub import hf_hub_download

            for f in ("converter/config.json", "converter/checkpoint.pth"):
                hf_hub_download("myshell-ai/OpenVoiceV2", f, local_dir=str(CKPT.parent))
        from openvoice.api import OpenVoiceBaseClass, ToneColorConverter

        # ToneColorConverter ส่ง enable_watermark ต่อให้คลาสแม่จนพัง จึงสร้างเองโดยไม่ใช้ลายน้ำ
        _model = ToneColorConverter.__new__(ToneColorConverter)
        OpenVoiceBaseClass.__init__(_model, str(CKPT / "config.json"), device="cpu")
        _model.watermark_model = None
        _model.version = getattr(_model.hps, "_version_", "v1")
        _model.load_ckpt(str(CKPT / "checkpoint.pth"))
    return _model


def to_wav(src: Path, dst: Path) -> None:
    """แปลงไฟล์เสียงทุกชนิด (webm/m4a/mp3/...) เป็น wav โมโน 22.05 kHz"""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "22050", str(dst)],
                   check=True)


def clean_sample(src: Path, dst: Path) -> None:
    """เสียงตัวอย่าง: ตัดช่วงเงียบ (ยาวกว่า 0.4 วินาที) และปรับความดังให้สม่ำเสมอ"""
    filters = ("highpass=f=70,"
               "silenceremove=start_periods=1:start_threshold=-45dB:"
               "stop_periods=-1:stop_duration=0.4:stop_threshold=-45dB,"
               "loudnorm=I=-20:TP=-2")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", filters,
                    "-ac", "1", "-ar", "22050", str(dst)], check=True)


def median_pitch(path: Path) -> float:
    import librosa
    import numpy as np

    y, sr = librosa.load(str(path), sr=16000, duration=30)
    f0, _, _ = librosa.pyin(y, fmin=60, fmax=400, sr=sr)
    return float(np.nanmedian(f0))


def duration(path: Path) -> float:
    import soundfile as sf

    info = sf.info(str(path))
    return info.frames / info.samplerate


def extract_embedding(wav: Path, out: Path) -> None:
    """สร้างลายเสียง (speaker embedding) จากเสียงตัวอย่าง แล้วบันทึกเป็นไฟล์ .pth"""
    import torch

    with _lock:
        se = _converter().extract_se([str(wav)])
    torch.save(se.cpu(), out)


def convert(src_audio: Path, target_se: Path, out_wav: Path, strength: float = 0.3) -> None:
    """เปลี่ยนเสียงใน src_audio ให้เป็นโทนเสียงของ target_se (ภาษาและคำพูดคงเดิม)"""
    import torch

    tmp = out_wav.with_suffix(".src.wav")
    to_wav(src_audio, tmp)
    try:
        with _lock:
            model = _converter()
            src_se = model.extract_se([str(tmp)])
            tgt = torch.load(target_se, map_location="cpu")
            model.convert(audio_src_path=str(tmp), src_se=src_se, tgt_se=tgt,
                          output_path=str(out_wav), tau=strength)
    finally:
        tmp.unlink(missing_ok=True)
