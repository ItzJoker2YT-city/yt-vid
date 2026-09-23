#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  YT Studio — one-click installer
#
#  Usage:   bash install.sh
#
#  Installs everything the app needs (Python deps, ffmpeg, a JS runtime),
#  creates an isolated virtual environment, and writes a run.sh launcher.
#  Safe to re-run: it upgrades existing pieces to the latest versions.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Pretty output ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YEL=$'\033[33m'; BLU=$'\033[36m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YEL=""; BLU=""; RST=""
fi
step() { printf "\n${BOLD}${BLU}==>${RST} ${BOLD}%s${RST}\n" "$1"; }
ok()   { printf "  ${GRN}✓${RST} %s\n" "$1"; }
warn() { printf "  ${YEL}!${RST} %s\n" "$1"; }
die()  { printf "\n${RED}✗ %s${RST}\n" "$1" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/yt"
VENV_DIR="$SCRIPT_DIR/.venv"

printf "${BOLD}\n  🎵  YT Studio installer${RST}\n"
printf "${DIM}  ---------------------------------------------${RST}\n"

[ -d "$APP_DIR" ] || die "Could not find the app folder at: $APP_DIR"
[ -f "$APP_DIR/requirements.txt" ] || die "Missing $APP_DIR/requirements.txt"

# ── Privilege helper ───────────────────────────────────────────────────────
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
fi

# ── Detect package manager ─────────────────────────────────────────────────
PM=""
if   command -v apt-get >/dev/null 2>&1; then PM="apt"
elif command -v dnf     >/dev/null 2>&1; then PM="dnf"
elif command -v yum     >/dev/null 2>&1; then PM="yum"
elif command -v pacman  >/dev/null 2>&1; then PM="pacman"
elif command -v zypper  >/dev/null 2>&1; then PM="zypper"
elif command -v apk     >/dev/null 2>&1; then PM="apk"
elif command -v brew    >/dev/null 2>&1; then PM="brew"
fi

pm_install() {
  # pm_install <pkg...>  — best effort; never aborts the whole script
  local pkgs=("$@")
  [ "${#pkgs[@]}" -eq 0 ] && return 0
  case "$PM" in
    apt)    $SUDO apt-get update -y >/dev/null 2>&1 || true
            $SUDO apt-get install -y --no-install-recommends "${pkgs[@]}" ;;
    dnf)    $SUDO dnf install -y "${pkgs[@]}" ;;
    yum)    $SUDO yum install -y "${pkgs[@]}" ;;
    pacman) $SUDO pacman -Sy --noconfirm --needed "${pkgs[@]}" ;;
    zypper) $SUDO zypper --non-interactive install "${pkgs[@]}" ;;
    apk)    $SUDO apk add "${pkgs[@]}" ;;
    brew)   brew install "${pkgs[@]}" ;;
    *)      return 1 ;;
  esac
}

step "Checking system packages"
if [ -z "$PM" ]; then
  warn "No supported package manager found — will rely on pip fallbacks."
else
  ok "Using package manager: $PM"
fi

# ── Python 3 ───────────────────────────────────────────────────────────────
PYTHON=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PYTHON="$c"; break; fi
done
if [ -z "$PYTHON" ]; then
  step "Installing Python 3"
  case "$PM" in
    apt)    pm_install python3 python3-venv python3-pip ;;
    dnf|yum)pm_install python3 python3-pip ;;
    pacman) pm_install python ;;
    zypper) pm_install python3 python3-pip ;;
    apk)    pm_install python3 py3-pip ;;
    brew)   pm_install python ;;
  esac
  for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 && { PYTHON="$c"; break; }
  done
fi
[ -n "$PYTHON" ] || die "Python 3 is required but could not be installed automatically."
ok "Python: $($PYTHON --version 2>&1)"

# Make sure the venv module exists (Debian ships it separately).
if ! "$PYTHON" -m venv --help >/dev/null 2>&1; then
  step "Installing Python venv support"
  [ "$PM" = "apt" ] && pm_install python3-venv || true
fi

# ── ffmpeg (required for MP3 conversion / trimming / merged video) ──────────
step "Setting up ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg already present: $(ffmpeg -version 2>/dev/null | head -1)"
else
  if pm_install ffmpeg && command -v ffmpeg >/dev/null 2>&1; then
    ok "Installed ffmpeg via $PM"
  else
    warn "System ffmpeg unavailable — will install the pip 'imageio-ffmpeg' fallback."
  fi
fi

# ── JavaScript runtime (yt-dlp needs it for current YouTube formats) ────────
step "Setting up a JavaScript runtime (for yt-dlp)"
if command -v node >/dev/null 2>&1 || command -v deno >/dev/null 2>&1; then
  ok "JS runtime present: $(command -v node >/dev/null 2>&1 && node --version || deno --version | head -1)"
else
  NODE_PKG="nodejs"
  [ "$PM" = "brew" ] && NODE_PKG="node"
  if pm_install "$NODE_PKG" && command -v node >/dev/null 2>&1; then
    ok "Installed Node.js via $PM ($(node --version))"
  else
    warn "Could not install Node automatically. Most downloads still work; if"
    warn "YouTube asks to 'confirm you are not a bot', install Node.js manually."
  fi
fi

# ── curl (used by health checks / tunneling helpers) ───────────────────────
command -v curl >/dev/null 2>&1 || pm_install curl || true

# ── Virtual environment ────────────────────────────────────────────────────
step "Creating the Python virtual environment"
if [ ! -x "$VENV_DIR/bin/python" ] && [ ! -x "$VENV_DIR/Scripts/python.exe" ]; then
  "$PYTHON" -m venv "$VENV_DIR" || die "Failed to create virtualenv at $VENV_DIR"
  ok "Created $VENV_DIR"
else
  ok "Reusing existing environment at $VENV_DIR"
fi
# Resolve the venv python (Linux/macOS vs Git-Bash on Windows)
VENV_PY="$VENV_DIR/bin/python"; [ -x "$VENV_PY" ] || VENV_PY="$VENV_DIR/Scripts/python.exe"

# ── Python dependencies (latest) ───────────────────────────────────────────
step "Installing Python dependencies (latest versions)"
"$VENV_PY" -m pip install --upgrade pip setuptools wheel
"$VENV_PY" -m pip install --upgrade -r "$APP_DIR/requirements.txt"
ok "Core dependencies installed"

# ffmpeg pip fallback only when there is no system ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1; then
  "$VENV_PY" -m pip install --upgrade imageio-ffmpeg && ok "Installed imageio-ffmpeg fallback"
fi

# ── Runtime folders ────────────────────────────────────────────────────────
step "Preparing data folders"
mkdir -p "$APP_DIR/data"
mkdir -p "${DOWNLOAD_DIR:-$HOME/Music/YT-Downloads}"
ok "Ready"

# ── Launcher ───────────────────────────────────────────────────────────────
step "Writing run.sh launcher"
cat > "$SCRIPT_DIR/run.sh" <<'RUN_EOF'
#!/usr/bin/env bash
# Start YT Studio. Override with env vars, e.g.  PORT=8080 bash run.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/.venv/bin/python"; [ -x "$PY" ] || PY="$DIR/.venv/Scripts/python.exe"
[ -x "$PY" ] || { echo "Environment missing — run: bash install.sh"; exit 1; }
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-5000}"
cd "$DIR/yt"
echo "  🎵  YT Studio starting on http://${HOST}:${PORT}"
exec "$PY" app.py
RUN_EOF
chmod +x "$SCRIPT_DIR/run.sh"
ok "Created run.sh"

# ── Done ───────────────────────────────────────────────────────────────────
printf "\n${BOLD}${GRN}  ✅  Installation complete!${RST}\n\n"
printf "  Start the app with:\n"
printf "      ${BOLD}bash run.sh${RST}\n\n"
printf "  Then open:  ${BOLD}http://localhost:${PORT:-5000}${RST}\n"
printf "  ${DIM}Change the port with:  PORT=8080 bash run.sh${RST}\n\n"
