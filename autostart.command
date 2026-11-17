#!/bin/zsh
# Включить автозапуск диктовки при входе в систему (запустить один раз).
cd "$(dirname "$0")"

osascript -e 'tell application "System Events" to delete (every login item whose name is "start.command")' 2>/dev/null
osascript -e 'tell application "System Events" to delete (every login item whose name is "NeuroDictate")' 2>/dev/null

if [ -d "/Applications/NeuroDictate.app" ]; then
  osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/NeuroDictate.app", hidden:true}'
  echo "Готово! NeuroDictate.app будет запускаться сам при включении Mac."
else
  osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$PWD/start.command\", hidden:false}"
  echo "Готово! Диктовка будет запускаться при включении Mac (через start.command)."
  echo "Совет: собери приложение через build_app.command - тогда без окна Терминала."
fi
echo "Отключить: Настройки -> Основные -> Объекты входа."
