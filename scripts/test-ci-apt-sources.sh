#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fixture_dir="$(mktemp -d)"
trap 'rm -rf -- "$fixture_dir"' EXIT
mkdir -p "$fixture_dir/sources"
cat > "$fixture_dir/sources/google-chrome.list" <<'EOF'
# Standalone optional Chrome repository from the runner image.
deb [arch=amd64] https://dl.google.com/linux/chrome-stable/deb/ stable main
EOF
cat > "$fixture_dir/sources/google-chrome.sources" <<'EOF'
Types: deb
URIs: https://dl.google.com/linux/chrome-stable/deb
Suites: stable
Components: main
EOF
cat > "$fixture_dir/sources/ubuntu.sources" <<'EOF'
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble
Components: main
EOF
cat > "$fixture_dir/sources/commented.list" <<'EOF'
# deb https://dl.google.com/linux/chrome-stable/deb/ stable main
deb https://apt.postgresql.org/pub/repos/apt noble-pgdg main
EOF
cp "$fixture_dir/sources/ubuntu.sources" "$fixture_dir/ubuntu.expected"
cp "$fixture_dir/sources/commented.list" "$fixture_dir/commented.expected"
bash "$script_dir/ci-isolate-chrome-apt-source.sh" "$fixture_dir/sources" "$fixture_dir/disabled"
test ! -e "$fixture_dir/sources/google-chrome.list"
test ! -e "$fixture_dir/sources/google-chrome.sources"
test -f "$fixture_dir/disabled/google-chrome.list"
test -f "$fixture_dir/disabled/google-chrome.sources"
cmp "$fixture_dir/sources/ubuntu.sources" "$fixture_dir/ubuntu.expected"
cmp "$fixture_dir/sources/commented.list" "$fixture_dir/commented.expected"
# Running twice is harmless and never disables a non-Chrome repository.
bash "$script_dir/ci-isolate-chrome-apt-source.sh" "$fixture_dir/sources" "$fixture_dir/disabled"
for extension in list sources; do
  mkdir -p "$fixture_dir/mixed-$extension"
  if [[ "$extension" == list ]]; then
    cat > "$fixture_dir/mixed-$extension/mixed.list" <<'EOF'
deb https://dl.google.com/linux/chrome-stable/deb/ stable main
deb http://archive.ubuntu.com/ubuntu/ noble main
EOF
  else
    cat > "$fixture_dir/mixed-$extension/mixed.sources" <<'EOF'
Types: deb
URIs: https://dl.google.com/linux/chrome-stable/deb/ http://archive.ubuntu.com/ubuntu/
Suites: stable
Components: main
EOF
  fi
  cp "$fixture_dir/mixed-$extension/mixed.$extension" "$fixture_dir/mixed-$extension.expected"
  if bash "$script_dir/ci-isolate-chrome-apt-source.sh" "$fixture_dir/mixed-$extension" "$fixture_dir/rejected-$extension"; then
    echo 'Mixed repository source must fail without being moved' >&2
    exit 1
  fi
  cmp "$fixture_dir/mixed-$extension/mixed.$extension" "$fixture_dir/mixed-$extension.expected"
  test ! -d "$fixture_dir/rejected-$extension"
done
printf '%s\n' 'CI APT source isolation checks passed.'
