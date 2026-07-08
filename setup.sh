#!/usr/bin/env bash

echo "================================================"
echo "  DOWNLOAD TOOLKIT — SETUP"
echo "================================================"

# ─── HELPER ───────────────────────────────────────
ok()   { echo "[✓] $1"; }
fail() { echo "[✗] $1"; }
info() { echo "[*] $1"; }
warn() { echo "[!] $1"; }

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
    python -m pip install requests beautifulsoup4 yt-dlp curl_cffi -q \
      && ok "Python packages installed" || warn "Some Python packages failed to install"

    info "Installing ffmpeg..."
    if command -v winget >/dev/null 2>&1; then
        winget install --id Gyan.FFmpeg -e --silent >/dev/null 2>&1 \
          && ok "ffmpeg installed via winget" || warn "ffmpeg install failed — install manually from https://ffmpeg.org/download.html"
    elif command -v ffmpeg >/dev/null 2>&1; then
        ok "ffmpeg already installed"
    else
        warn "ffmpeg not found — install from https://ffmpeg.org/download.html and add to PATH"
    fi

    if [ -d "$HOME/download-toolkit" ]; then
        info "Toolkit already installed — updating..."
        cd "$HOME/download-toolkit" && git pull 2>&1 | tee /tmp/gitpull.log
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

# ─── UPDATE PACKAGES (non-interactive) ────────────
info "Updating package lists..."
DEBIAN_FRONTEND=noninteractive pkg update -y \
  -o Dpkg::Options::="--force-confnew" 2>/dev/null
ok "Packages updated"

# ─── INSTALL SYSTEM PACKAGES ONE BY ONE ──────────
info "Installing Python..."
DEBIAN_FRONTEND=noninteractive pkg install python -y \
  -o Dpkg::Options::="--force-confnew" 2>/dev/null \
  && ok "Python installed" || warn "Python install failed — may already be installed"

info "Installing Git..."
DEBIAN_FRONTEND=noninteractive pkg install git -y \
  -o Dpkg::Options::="--force-confnew" 2>/dev/null \
  && ok "Git installed" || warn "Git install failed — may already be installed"

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

# cryptography requires Rust to compile on Termux — skipped (openssl handles decryption instead)

# ─── CLONE OR UPDATE REPO ────────────────────────
echo ""
if [ -d "$HOME/download-toolkit" ]; then
    info "Toolkit already installed — updating..."
    cd "$HOME/download-toolkit"
    git stash -q 2>/dev/null
    git pull 2>&1 | tee /tmp/gitpull.log
    git stash pop -q 2>/dev/null
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

# Verify main.py exists before setting up launcher
if [ ! -f "$HOME/download-toolkit/main.py" ]; then
    fail "main.py not found — setup cannot continue"
    exit 1
fi

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
    # Kill any existing session and start fresh
    tmux kill-session -t download 2>/dev/null
    cd ~/download-toolkit
    git stash -q 2>/dev/null
    git pull -q
    git stash pop -q 2>/dev/null
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
    # Kill any existing session and start fresh
    tmux kill-session -t download 2>/dev/null
    cd ~/download-toolkit
    git stash -q 2>/dev/null
    git pull -q
    git stash pop -q 2>/dev/null
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
