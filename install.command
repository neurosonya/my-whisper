#!/bin/zsh
# Установка NeuroDictate: окружение + зависимости + модель. Запускать один раз.
cd "$(dirname "$0")"

echo "=== NeuroDictate: установка ==="

if [ "$(uname -m)" != "arm64" ]; then
  echo "Увы, нужен Mac на Apple Silicon (M1-M4) - на этом Mac не заработает."
  exit 1
fi

if ! python3 -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "Нужен Python 3.10 или новее. Скачай с python.org/downloads"
  echo "(или: brew install python), потом запусти меня снова."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Создаю окружение Python..."
  python3 -m venv .venv
fi

echo "Ставлю зависимости..."
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q

echo "Скачиваю модель Whisper (~1.6 ГБ, только при первом запуске)..."
./.venv/bin/python - <<'PY'
import numpy as np
import mlx_whisper
mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32),
                       path_or_hf_repo="mlx-community/whisper-large-v3-turbo")
print("Модель на месте, распознавание работает.")
PY

echo ""
echo "=== Готово! Теперь запусти start.command ==="
