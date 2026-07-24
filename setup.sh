#!/usr/bin/env bash

echo "================================================"
echo "  DOWNLOAD TOOLKIT — SETUP"
echo "================================================"

# ─── HELPER ───────────────────────────────────────
ok()   { echo "[✓] $1"; }
fail() { echo "[✗] $1"; }
info() { echo "[*] $1"; }
warn() { echo "[!] $1"; }

# winget_install <package-id> — install a winget package non-interactively.
# On a fresh machine winget prompts to accept its source/package agreements;
# with stdin/stdout redirected that prompt blocks forever and looks like a
# hang. --accept-*-agreements answers it, --disable-interactivity refuses any
# other prompt instead of waiting, and </dev/null guarantees it can never sit
# on stdin. Output stays visible so a slow download doesn't look frozen.
winget_install() {
    winget install --id "$1" -e \
        --accept-source-agreements --accept-package-agreements \
        --disable-interactivity </dev/null
}

IS_TERMUX=0
if [ -d "/data/data/com.termux" ] || echo "${PREFIX:-}" | grep -q "com.termux"; then
    IS_TERMUX=1
fi

if [ "$IS_TERMUX" -ne 1 ]; then
    info "Non-Termux shell detected — using Git Bash/Linux setup"

    command -v python >/dev/null 2>&1 || {
        fail "python not found. Install Python first, then rerun setup."
        exit 1
    }
    command -v git >/dev/null 2>&1 || warn "git not found — update/clone may fail"

    info "Installing Python packages..."
    if [ -f "$HOME/download-toolkit/requirements.txt" ]; then
        python -m pip install -r "$HOME/download-toolkit/requirements.txt" -q \
          && ok "Python packages installed" || warn "Some Python packages failed to install"
    else
        python -m pip install requests beautifulsoup4 yt-dlp curl_cffi aiohttp -q \
          && ok "Python packages installed" || warn "Some Python packages failed to install"
    fi

    info "Installing ffmpeg..."
    if command -v ffmpeg >/dev/null 2>&1; then
        ok "ffmpeg already installed"
    elif command -v winget >/dev/null 2>&1; then
        info "  (winget download — this can take a couple of minutes)"
        winget_install Gyan.FFmpeg \
          && ok "ffmpeg installed via winget" || warn "ffmpeg install failed — install manually from https://ffmpeg.org/download.html"
    else
        warn "ffmpeg not found — install from https://ffmpeg.org/download.html and add to PATH"
    fi

    info "Installing aria2c (fast downloads + torrents)..."
    if command -v aria2c >/dev/null 2>&1; then
        ok "aria2c already installed"
    elif command -v winget >/dev/null 2>&1; then
        info "  (winget download — this can take a minute)"
        winget_install aria2.aria2 \
          && ok "aria2c installed via winget" || warn "aria2c install failed — torrents/fast downloads unavailable. Install from https://github.com/aria2/aria2/releases"
    else
        warn "aria2c not found — torrents and fast multi-connection downloads need it. Install from https://github.com/aria2/aria2/releases and add to PATH"
    fi

    if [ -d "$HOME/download-toolkit" ]; then
        info "Toolkit already installed - updating..."
        cd "$HOME/download-toolkit" && git fetch --all -q && git reset --hard origin/main 2>&1 | tee /tmp/gitpull.log
        if grep -q "Already up to date" /tmp/gitpull.log; then
            ok "Toolkit already up to date"
        elif [ ${PIPESTATUS[0]} -eq 0 ]; then
            ok "Toolkit updated"
        else
            warn "Update failed — $(cat /tmp/gitpull.log | tail -1)"
        fi
    else
        info "Downloading toolkit..."
        git clone https://github.com/anonrode/download-toolkit.git "$HOME/download-toolkit" \
          && ok "Toolkit downloaded" || {
            fail "Download failed — check your internet connection"
            exit 1
          }
    fi

    if [ ! -f "$HOME/download-toolkit/main.py" ]; then
        fail "main.py not found — setup cannot continue"
        exit 1
    fi

    cat > "$HOME/download-toolkit/run.sh" << 'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")" && python main.py
EOF
    chmod +x "$HOME/download-toolkit/run.sh"
    ok "Launcher created: ~/download-toolkit/run.sh"

    # Add Python Scripts to PATH so yt-dlp, aria2c etc work as commands
    PYTHON_SCRIPTS=$(python -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2>/dev/null | tr '\\' '/')
    if [ -n "$PYTHON_SCRIPTS" ] && ! echo "$PATH" | grep -qF "$PYTHON_SCRIPTS"; then
        echo "export PATH=\"\$PATH:$PYTHON_SCRIPTS\"" >> ~/.bashrc
        export PATH="$PATH:$PYTHON_SCRIPTS"
        ok "Python Scripts added to PATH — yt-dlp and friends now work"
    else
        ok "Python Scripts already in PATH"
    fi

    # Create desktop shortcut
    DESKTOP=$(python -c "import os; print(os.path.join(os.path.expanduser('~'), 'Desktop'))" 2>/dev/null | tr '\\' '/')
    if [ -d "$DESKTOP" ]; then
        cat > "$DESKTOP/Anonrode.bat" << 'BATEOF'
@echo off
cd /d "%USERPROFILE%\download-toolkit"
python main.py
pause
BATEOF
        ok "Desktop shortcut created: Anonrode.bat"
    else
        warn "Could not find Desktop folder — shortcut not created"
    fi

    echo ""
    echo "================================================"
    echo "  SETUP COMPLETE!"
    echo "  Start with: bash ~/download-toolkit/run.sh"
    echo "  Or double-click Anonrode.bat on your Desktop"
    echo "================================================"
    exit 0
fi

# ─── BOOTSTRAP + SELF-UPDATE (first pass only) ────
# bash reads this whole script into memory at launch. If we pulled a newer
# setup.sh partway through and kept executing, the rest of THIS run would still
# be the OLD in-memory logic (that bug wrote a stale .bashrc). So on the first
# pass we do only the minimum needed to pull — update pkgs, install git+python,
# fetch the repo — then re-exec the freshly pulled script exactly once. The
# guard var (ANONRODE_SETUP_REEXEC) prevents an infinite loop, and the heavy
# installs below run only on the second pass, so nothing is done twice.
if [ "$ANONRODE_SETUP_REEXEC" != "1" ]; then
    info "Updating package lists..."
    DEBIAN_FRONTEND=noninteractive pkg update -y \
      -o Dpkg::Options::="--force-confnew" 2>/dev/null
    ok "Packages updated"

    info "Installing Python..."
    DEBIAN_FRONTEND=noninteractive pkg install python -y \
      -o Dpkg::Options::="--force-confnew" 2>/dev/null \
      && ok "Python installed" || warn "Python install failed — may already be installed"

    info "Installing Git..."
    DEBIAN_FRONTEND=noninteractive pkg install git -y \
      -o Dpkg::Options::="--force-confnew" 2>/dev/null \
      && ok "Git installed" || warn "Git install failed — may already be installed"

    echo ""
    if [ -d "$HOME/download-toolkit" ]; then
        info "Toolkit already installed - updating..."
        cd "$HOME/download-toolkit"
        git fetch --all -q
        git reset --hard origin/main 2>&1 | tee /tmp/gitpull.log
        if grep -q "Already up to date" /tmp/gitpull.log; then
            ok "Toolkit already up to date"
        else
            ok "Toolkit updated"
        fi
    else
        info "Downloading toolkit..."
        git clone https://github.com/anonrode/download-toolkit.git "$HOME/download-toolkit" \
          && ok "Toolkit downloaded" || {
            fail "Download failed — check your internet connection"
            exit 1
          }
    fi

    if [ ! -f "$HOME/download-toolkit/main.py" ]; then
        fail "main.py not found — setup cannot continue"
        exit 1
    fi

    export ANONRODE_SETUP_REEXEC=1
    info "Re-running with the updated setup script..."
    exec bash "$HOME/download-toolkit/setup.sh" "$@"
fi

# ─── FULL INSTALL (second pass — fresh script) ────
# Reached only after the re-exec above, so this is the just-pulled logic and
# runs exactly once. git+python are already installed; the repo is already
# up to date. Install the remaining tools and Python deps.
info "Installing aria2..."
DEBIAN_FRONTEND=noninteractive pkg install aria2 -y \
  -o Dpkg::Options::="--force-confnew" 2>/dev/null \
  && ok "aria2 installed" || warn "aria2 install failed"

info "Installing tmux..."
DEBIAN_FRONTEND=noninteractive pkg install tmux -y \
  -o Dpkg::Options::="--force-confnew" 2>/dev/null \
  && ok "tmux installed" || warn "tmux install failed"

info "Installing termux-api..."
DEBIAN_FRONTEND=noninteractive pkg install termux-api -y \
  -o Dpkg::Options::="--force-confnew" 2>/dev/null \
  && ok "termux-api installed" || warn "termux-api install failed"

info "Installing ffmpeg..."
DEBIAN_FRONTEND=noninteractive pkg install ffmpeg -y \
  -o Dpkg::Options::="--force-confnew" 2>/dev/null \
  && ok "ffmpeg installed" || warn "ffmpeg install failed"

# ─── INSTALL PYTHON DEPENDENCIES ─────────────────
echo ""
info "Installing Python packages..."

pip install requests --break-system-packages -q \
  && ok "requests installed" || warn "requests install failed"

pip install beautifulsoup4 --break-system-packages -q \
  && ok "beautifulsoup4 installed" || warn "beautifulsoup4 install failed"

pip install yt-dlp --break-system-packages -q \
  && ok "yt-dlp installed" || warn "yt-dlp install failed"

pip install curl_cffi --break-system-packages -q \
  && ok "curl_cffi installed" || warn "curl_cffi install failed (wildshare/naijaprey may not work)"

pip install aiohttp --break-system-packages -q \
  && ok "aiohttp installed" || warn "aiohttp install failed (search falls back to classic mode)"

# cryptography requires Rust to compile on Termux — skipped (openssl handles decryption instead)

cd "$HOME/download-toolkit"

# ─── SET UP AUTO-LAUNCH ──────────────────────────
echo ""
info "Setting up auto-launch..."

# Backup existing .bashrc if it has content beyond our launcher
if [ -f "$HOME/.bashrc" ]; then
    existing=$(cat "$HOME/.bashrc")
    our_content=$(cat << 'CHECKEOF'
# Anonrode auto-launch
if [ -n "$TMUX" ]; then
    # Already inside tmux — shell is ready, do nothing
    :
else
    # Kill any existing session and start fresh.
    # NOTE: no git fetch/reset here — that hit GitHub over the network on EVERY
    # launch (2-5s delay before the app even started). The app self-updates
    # in-app via schedule_auto_update() on a 7-day cadence, so the launch path
    # stays offline and instant. Run setup.sh (or the in-app `update`) to pull.
    tmux kill-session -t download 2>/dev/null
    cd ~/download-toolkit
    tmux new-session -s download python main.py
fi
CHECKEOF
)
    if [ "$existing" != "$our_content" ] && [ -n "$existing" ]; then
        cp "$HOME/.bashrc" "$HOME/.bashrc.backup"
        info "Existing .bashrc backed up to .bashrc.backup"
    fi
fi

# .bashrc logic:
# - If already inside a tmux session (e.g. the download session itself), do nothing
# - Otherwise kill any existing session and start fresh
cat > "$HOME/.bashrc" << 'EOF'
# Anonrode auto-launch
if [ -n "$TMUX" ]; then
    # Already inside tmux — shell is ready, do nothing
    :
else
    # Kill any existing session and start fresh.
    # NOTE: no git fetch/reset here — that hit GitHub over the network on EVERY
    # launch (2-5s delay before the app even started). The app self-updates
    # in-app via schedule_auto_update() on a 7-day cadence, so the launch path
    # stays offline and instant. Run setup.sh (or the in-app `update`) to pull.
    tmux kill-session -t download 2>/dev/null
    cd ~/download-toolkit
    tmux new-session -s download python main.py
fi
EOF
ok "Auto-launch configured"

# ─── DONE ─────────────────────────────────────────
echo ""
echo "================================================"
echo "  SETUP COMPLETE!"
echo "  Close and reopen Termux to start downloading"
echo "================================================"
