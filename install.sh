#!/usr/bin/env bash
# Buzzer Party installer for SteamOS / Linux. Run from Desktop Mode in Konsole:
#   bash install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "==> Creating Python environment in $DIR/.venv"
if ! python3 -m venv .venv 2>/dev/null; then
  echo "python3 venv unavailable, trying virtualenv via pip --user"
  python3 -m ensurepip --user 2>/dev/null || true
  python3 -m pip install --user virtualenv
  python3 -m virtualenv .venv
fi
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install pygame-ce qrcode

echo "==> Writing launcher"
cat > run.sh <<EOF
#!/usr/bin/env bash
cd "$DIR"
exec "$DIR/.venv/bin/python" "$DIR/buzzer_party.py" "\$@"
EOF
chmod +x run.sh

echo "==> Desktop entry"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/buzzer-party.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Buzzer Party
Comment=Party quiz for Buzz! buzzers
Exec=$DIR/run.sh
Path=$DIR
Terminal=false
Categories=Game;
EOF

echo
read -r -p "Install udev rule for buzzer LEDs/RPCS3 passthrough? Needs sudo. [Y/n] " ans
if [[ "${ans:-Y}" =~ ^[Yy] ]]; then
  sudo cp "$DIR/99-buzzer-party.rules" /etc/udev/rules.d/
  sudo udevadm control --reload-rules && sudo udevadm trigger
  echo "Done - unplug and replug the buzzers."
fi

echo
echo "All set. To play in Game Mode: Steam > Add a Non-Steam Game > Buzzer Party."
echo "Test now with: $DIR/run.sh --window"
