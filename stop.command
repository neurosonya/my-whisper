#!/bin/zsh
# Остановка NeuroDictate.
if pkill -f "neurodictate.py"; then
  echo "NeuroDictate остановлен."
else
  echo "NeuroDictate не был запущен."
fi
