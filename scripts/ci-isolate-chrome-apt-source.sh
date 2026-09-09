#!/usr/bin/env bash
# Ephemeral Ubuntu runners only. Playwright installs its own Chromium and does
# not require this Chrome repository, whose stale Packages index broke apt.
set -euo pipefail
source_dir="${1:-/etc/apt/sources.list.d}"
disabled_dir="${2:-${RUNNER_TEMP:-/tmp}/tiktok-ci-disabled-apt-sources}"
chrome_uri='https://dl.google.com/linux/chrome-stable/deb'
shopt -s nullglob
candidates=()
for source_file in "$source_dir"/*.list "$source_dir"/*.sources; do
  [[ -f "$source_file" ]] || continue
  has_chrome=false
  has_other_uri=false
  # Ignore comments; recognize deb/deb-src lines and deb822 URI continuation
  # lines conservatively. Any other active URI makes whole-file removal unsafe.
  while IFS= read -r uri; do
    if [[ "${uri%/}" == "$chrome_uri" ]]; then
      has_chrome=true
    else
      has_other_uri=true
    fi
  done < <(awk '
    { sub(/[[:space:]]*#.*/, "") }
    { while (match($0, /[[:alpha:]][[:alnum:]+.-]*:\/+[^[:space:]]+/)) {
        print substr($0, RSTART, RLENGTH)
        $0 = substr($0, RSTART + RLENGTH)
      }
    }' "$source_file")
  if [[ "$has_chrome" == true ]]; then
    if [[ "$has_other_uri" == true || -L "$source_file" ]]; then
      printf 'Refusing to move mixed or symlinked APT source: %s\n' "${source_file##*/}" >&2
      exit 1
    fi
    if [[ -e "$disabled_dir/${source_file##*/}" ]]; then
      printf 'APT quarantine destination already exists: %s\n' "${source_file##*/}" >&2
      exit 1
    fi
    candidates+=("$source_file")
  fi
done
# Validate all files before making any change. Sources outside this exact URL,
# including Ubuntu and PGDG, remain untouched; apt security checks stay enabled.
if (( ${#candidates[@]} )); then
  mkdir -p -- "$disabled_dir"
  for source_file in "${candidates[@]}"; do
    mv -- "$source_file" "$disabled_dir/"
    printf 'Isolated optional Chrome APT source: %s\n' "${source_file##*/}"
  done
fi
