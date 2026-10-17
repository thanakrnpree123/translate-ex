# CLAUDE.md

Local Flask web app (macOS only) that turns worksheet images and documents into narrated audio/video.
OCR the Thai prompts, translate them to answers, synthesize speech (optionally converted to the user's own voice),
and save small MP4s into an `ex/` folder next to each source image. The UI and user-facing strings are in Thai;
keep new UI text in Thai. The user speaks Thai.

## Run

```bash
./start.command                # first run builds .venv (Python 3.11) and installs everything
.venv/bin/python app.py        # server at http://127.0.0.1:5055
```

`.claude/launch.json` has a `doc-reader` preview config for the same server.
There are no automated tests. Verify changes by driving the API with `requests`/`curl`, or
in the browser pane. Restart the server after editing `app.py` (debug/reload is off).

## Layout

- `app.py` — every route and pipeline: OCR (`ocr_image`), item parsing (`parse_items`), translation
  (`translate`, `translate_lines`), TTS (`synthesize`, `speak_segment`, `concat_wavs`), video (`make_video`),
  export to `ex/` (`api_export_videos`), voice profiles (`/api/voices`).
- `voice.py` — OpenVoice V2 tone-color converter wrapper (`clean_sample`, `extract_embedding`, `convert`, `median_pitch`).
- `templates/index.html` — the whole frontend (vanilla JS, no build step). Jobs queue in `jobs`; files carry a
  `path` (folder-relative) that is sent as `relpath`.
- Runtime data (all git-ignored): `saved/`, `uploads/`, `voices/`, `models/`, `config.json`.

## Environment constraints (don't "upgrade" these)

- The dev machine is an **Intel Mac**. PyTorch's last x86_64 macOS wheel is **torch 2.2.2**, which needs
  **Python ≤ 3.12** and **numpy < 2**. The venv is Python 3.11.
- `llvmlite==0.43.0` / `numba==0.60.0` are pinned because newer llvmlite has no Intel-mac wheel and fails to build.
  `start.command` installs them with `--only-binary=:all:` first.
- OpenVoice is installed with `pip install --no-deps git+https://github.com/myshell-ai/OpenVoice.git`. Its
  setup.py pins (numpy 1.22, librosa 0.9.1, …) are incompatible and must be ignored. Pip's resulting
  "requires X, but you have Y" warnings are expected.
- OCR uses Apple Vision via `ocrmac` (pyobjc), so the app is macOS-only. Thai OCR works well.

## Known external-service quirks (handled; keep the handling)

- **Edge TTS** (`edge-tts`) randomly fails with `NoAudioReceived` (~10% of requests), and some sentence+voice
  pairs fail *every* time (e.g. "คุณต้องไปเยี่ยมปู่ย่าตายายคุณ" with `th-TH-PremwadeeNeural`).
  `synthesize` retries 3× and then falls back to the other voice of the same language (`ALT_VOICE`).
- **Google Translate** (keyless `translate.googleapis.com` `client=gtx`) returns 429 after heavy use.
  `translate` alternates with `clients5.google.com/translate_a/t` (`client=dict-chrome-ex`, POST) and backs off.
  `translate.google.com/m` and the `deep-translator` package are blocked here; don't switch back to them.
- The frontend retries translation for any job with empty answers before generating audio.

## Pipeline details worth knowing

- Worksheet mode: question = `"ข้อ {n}. {src}"` in the stock Thai female voice, then a pause, then the answer
  in the chosen language. With a voice profile, only answers are converted to the user's voice.
  Segments are concatenated with pydub at 22.05 kHz mono.
- Voice profiles: `clean_sample` removes silence and loudness-normalizes the sample. At least 10 s of speech is
  required. The profile's gender (from median pitch, < 165 Hz = male) picks the base TTS voice.
  `ToneColorConverter` is built manually because upstream passes `enable_watermark` to the base class and crashes.
- Video: 1 fps still image, width ≤ 1080 (even), x264 CRF 28 `-tune stillimage`, AAC mono 32 kbps 22.05 kHz,
  `+faststart`. Length is set with `-t <audio duration>` because `-shortest` overshoots at 1 fps.
  Keep H.264/AAC (Opus would be smaller but doesn't play everywhere). Target is about 0.4–0.8 MB per page for Google Drive.
- Export to `ex/`: indexes the export root (depth 4, skipping hidden dirs and `ex`) by filename. It uses `relpath`
  to disambiguate when the same filename exists in several folders, overwrites existing videos with the newest
  render, and with `remove_after` deletes all saved renders of that source from `saved/`.

## Conventions

- Match the existing style: Thai comments, short functions, no frameworks beyond Flask.
- Never write test output into the user's real folders (`~/Downloads/L1SPhoto …`). Copy them to a scratch dir
  and point `root` there, then restore `config.json`.
- `saved/`, `voices/`, and `uploads/` hold the user's real work and voice. Don't delete them in bulk.
