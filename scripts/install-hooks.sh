#!/usr/bin/env sh
# Install a local pre-push hook that blocks any push containing secrets or forbidden files.
# Hooks live in .git/hooks and are never pushed; run this once per clone.
set -e
root=$(git rev-parse --show-toplevel)
hook="$root/.git/hooks/pre-push"
cat > "$hook" <<'HOOK'
#!/usr/bin/env sh
root=$(git rev-parse --show-toplevel)
for py in "$root/backend/.venv/Scripts/python.exe" "$root/backend/.venv/bin/python" python3 python; do
  if command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; then
    exec "$py" "$root/scripts/check_secrets.py"
  fi
done
echo "pre-push: no Python found to run scripts/check_secrets.py; refusing to push." >&2
exit 1
HOOK
chmod +x "$hook"
echo "Installed $hook"
