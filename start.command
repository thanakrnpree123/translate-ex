#!/bin/zsh
# ดับเบิลคลิกไฟล์นี้เพื่อเปิดโปรแกรม
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import openvoice" 2>/dev/null; then
  echo "กำลังติดตั้งครั้งแรก อาจใช้เวลาหลายนาที ..."
  python3.11 -m venv --clear .venv || { echo "ต้องมี Python 3.11 (brew install python@3.11)"; exit 1; }
  .venv/bin/pip install -q --only-binary=:all: llvmlite==0.43.0 numba==0.60.0
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/pip install -q --no-deps git+https://github.com/myshell-ai/OpenVoice.git
fi
(sleep 3 && open http://127.0.0.1:5055) &
.venv/bin/python app.py
