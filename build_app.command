#!/bin/zsh
# Сборка NeuroDictate.app и установка в «Программы». Запускать после install.command.
#
# Устройство приложения: главный исполняемый файл - сам интерпретатор Python
# (копия из Python.framework, только arm64, подписан ad-hoc). Библиотеки лежат
# в Contents/lib, Python находит их через Contents/pyvenv.cfg. При запуске без
# аргументов sitecustomize.py стартует neurodictate.py. Никаких скриптов-
# прослоек: macOS видит обычное подписанное приложение и выдает разрешения ему.
#
# ВАЖНО: после каждой пересборки macOS считает приложение новым - разрешения
# (микрофон, универсальный доступ, мониторинг ввода) нужно выдать заново.
set -e
cd "$(dirname "$0")"
SRC="$PWD"
APP_NAME="NeuroDictate"
BUNDLE_ID="com.neuralprofit.neurodictate"
BUILD="$SRC/build"
APP="$BUILD/$APP_NAME.app"
CONTENTS="$APP/Contents"
RES="$CONTENTS/Resources"

echo "=== $APP_NAME: сборка приложения ==="

if [ "$(uname -m)" != "arm64" ]; then
  echo "Нужен Mac на Apple Silicon (M1-M4)."
  exit 1
fi

rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$RES"

# ---------- иконка ----------
echo "Рисую иконку..."
ICONSET="$BUILD/$APP_NAME.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
cat > "$BUILD/make_icon.swift" <<'SWIFT'
import AppKit

let size = 1024.0
let image = NSImage(size: NSSize(width: size, height: size))
image.lockFocus()

// Скругленный квадрат с фиолетово-желтым градиентом в стиле NeuralProfit
let rect = NSRect(x: 0, y: 0, width: size, height: size).insetBy(dx: 60, dy: 60)
let path = NSBezierPath(roundedRect: rect, xRadius: 220, yRadius: 220)
let gradient = NSGradient(colors: [
    NSColor(calibratedRed: 0.47, green: 0.36, blue: 0.93, alpha: 1),
    NSColor(calibratedRed: 0.64, green: 0.50, blue: 0.98, alpha: 1),
])!
gradient.draw(in: path, angle: -60)

// Микрофон
let config = NSImage.SymbolConfiguration(pointSize: 520, weight: .semibold)
if let mic = NSImage(systemSymbolName: "mic.fill", accessibilityDescription: nil)?
    .withSymbolConfiguration(config) {
    let tinted = NSImage(size: mic.size, flipped: false) { r in
        mic.draw(in: r)
        NSColor(calibratedRed: 1.0, green: 0.86, blue: 0.30, alpha: 1).set()
        r.fill(using: .sourceAtop)
        return true
    }
    let w = tinted.size.width, h = tinted.size.height
    tinted.draw(in: NSRect(x: (size - w) / 2, y: (size - h) / 2, width: w, height: h))
}

image.unlockFocus()
let tiff = image.tiffRepresentation!
let png = NSBitmapImageRep(data: tiff)!.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
SWIFT
swift "$BUILD/make_icon.swift" "$BUILD/icon_1024.png"
for s in 16 32 64 128 256 512; do
  sips -z $s $s "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}.png" > /dev/null
  d=$((s * 2))
  sips -z $d $d "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" > /dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/$APP_NAME.icns"

# ---------- Python-окружение внутрь бандла ----------
echo "Ставлю библиотеки внутрь приложения (1-2 минуты)..."
rm -rf "$BUILD/venv"
python3 -m venv "$BUILD/venv"
"$BUILD/venv/bin/pip" install --upgrade pip -q
"$BUILD/venv/bin/pip" install -r "$SRC/requirements.txt" -q

PY_BASE="$("$BUILD/venv/bin/python" -c 'import sys; print(sys.base_prefix)')"
PY_VER="$("$BUILD/venv/bin/python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
PY_TAG="python$PY_VER"

mkdir -p "$CONTENTS/lib"
cp -R "$BUILD/venv/lib/$PY_TAG" "$CONTENTS/lib/$PY_TAG"
cat > "$CONTENTS/pyvenv.cfg" <<CFG
home = $PY_BASE/bin
include-system-site-packages = false
version = $PY_VER
CFG

cp "$SRC/neurodictate.py" "$SRC/config.json" "$SRC/requirements.txt" "$RES/"

# При запуске без аргументов (так запускает Finder/Launchpad) стартуем приложение.
# Дочерние процессы Python (-c ...) сюда не попадают.
# Через sitecustomize нельзя: homebrew-питон кладет свой sitecustomize в stdlib,
# а stdlib в sys.path стоит раньше site-packages бандла - наш файл затирается
# (на питоне с python.org такой проблемы нет). .pth исполняется site.py при
# добавлении site-packages, до всяких sitecustomize, поэтому работает всегда.
cat > "$CONTENTS/lib/$PY_TAG/site-packages/neurodictate_boot.py" <<'PY'
import os
import sys

if sys.argv == [""]:
    os.environ["NEURODICTATE_BUNDLED"] = "1"
    os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
    import runpy

    runpy.run_path(os.path.join(sys.prefix, "Resources", "neurodictate.py"), run_name="__main__")
    os._exit(0)  # sys.exit здесь нельзя - Python еще инициализируется
PY
echo "import neurodictate_boot" > "$CONTENTS/lib/$PY_TAG/site-packages/zz_neurodictate.pth"

# ---------- главный исполняемый файл = интерпретатор ----------
# Берем «графический» бинарник из Python.app (консольный python3.11 при старте
# сам перезапускается через него). Оставляем только arm64: без Intel-среза
# macOS не запустит его через Rosetta и не будет пугать предупреждениями.
PY_BIN="$PY_BASE/Resources/Python.app/Contents/MacOS/Python"
[ -f "$PY_BIN" ] || PY_BIN="$(readlink -f "$BUILD/venv/bin/python")"
lipo "$PY_BIN" -thin arm64 -output "$CONTENTS/MacOS/$APP_NAME" 2>/dev/null || cp "$PY_BIN" "$CONTENTS/MacOS/$APP_NAME"
chmod +x "$CONTENTS/MacOS/$APP_NAME"

# ---------- Info.plist ----------
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>$APP_NAME</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>$APP_NAME</string>
    <key>CFBundleIconFile</key><string>$APP_NAME</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
    <key>LSArchitecturePriority</key><array><string>arm64</string></array>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>NeuroDictate записывает голос, чтобы превратить его в текст. Все обрабатывается локально.</string>
</dict>
</plist>
PLIST

# ---------- подпись ----------
# Ad-hoc подпись всего бандла с идентификатором = bundle id: по нему macOS
# сверяет выданные разрешения. Без аккаунта разработчика Apple подпись
# действует только на этой сборке - после пересборки разрешения выдаются заново.
echo "Подписываю..."
codesign --force --deep -s - -i "$BUNDLE_ID" "$APP" 2>&1 | grep -v "replacing existing signature" || true
codesign --verify --deep "$APP" && echo "Подпись в порядке."

# ---------- установка ----------
echo "Устанавливаю в Программы..."
pkill -f "$APP_NAME.app/Contents" 2>/dev/null || true
pkill -f "neurodictate.py" 2>/dev/null || true
sleep 1
rm -rf "/Applications/$APP_NAME.app"
cp -R "$APP" "/Applications/$APP_NAME.app"

# Автозапуск: убираем старый ручной вариант, добавляем приложение
osascript -e 'tell application "System Events" to delete (every login item whose name is "start.command")' > /dev/null 2>&1 || true
osascript -e "tell application \"System Events\" to delete (every login item whose name is \"$APP_NAME\")" > /dev/null 2>&1 || true
osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"/Applications/$APP_NAME.app\", hidden:true}" > /dev/null

open -a "$APP_NAME"

echo ""
echo "=== Готово! $APP_NAME установлен в Программы и запущен ==="
echo "Значок 🎤 появится в меню-баре через 10-20 секунд."
echo "Выдай разрешения приложению $APP_NAME в Настройках -> Конфиденциальность:"
echo "  Универсальный доступ, Мониторинг ввода (и Микрофон при первой записи)."
