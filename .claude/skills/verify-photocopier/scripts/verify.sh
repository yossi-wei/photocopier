#!/usr/bin/env bash
# Sandbox harness for driving the real photocopier CLI without touching the real one.
#
#   verify.sh up                     build a sandbox run; prints its RUN directory
#   verify.sh doctor   RUN           read-only: is this sandbox safe and worth driving?
#   verify.sh pc       RUN ARGS...   run `photocopier ARGS` against the sandbox, logged as evidence
#   verify.sh snapshot RUN LABEL     record remote, spool, NAS trees and the ledger as evidence
#   verify.sh launcher RUN [VAR=val ...]  run scripts/photocopier-run.sh against the sandbox
#   verify.sh unmount  RUN           detach the sandbox NAS (to test the not-mounted guard)
#   verify.sh remount  RUN           attach it again
#   verify.sh down     RUN           tear down the sandbox; evidence is kept
#   verify.sh ls                     list runs and whether their NAS image is still attached
#
# A RUN lives under ${PHOTOCOPIER_VERIFY_HOME:-$HOME/.local/state/photocopier-verify}/runs/:
#   RUN/sandbox/   config.toml, venv/, remote/ (fake OneDrive), spool/, nas/ (a real mount
#                  of nas.sparseimage), state/, lock/, notify.sh   -- removed by `down`
#   RUN/evidence/  transcript.log, NN-<label>.txt snapshots, notify.log -- kept by `down`
#
# Nothing here reads or writes ~/.config/photocopier, the real spool, /Volumes/media, the
# real NAS-jobs lock, or the launchd job: every call passes -c with the sandbox config and
# scrubs the PHOTOCOPIER_* variables first.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$SKILL_DIR/../../.." && pwd)"
VERIFY_HOME="${PHOTOCOPIER_VERIFY_HOME:-$HOME/.local/state/photocopier-verify}"

die() { echo "verify: $*" >&2; exit 1; }

run_dir() {
  local run="${1:-}"
  [ -n "$run" ] || die "missing RUN directory (as printed by \`verify.sh up\`)"
  [ -f "$run/sandbox/run.env" ] || [ -d "$run/evidence" ] || die "$run is not a verify run"
  (cd "$run" && pwd)
}

is_attached() { mount | grep -qF " on $1 ("; }

# Fingerprint of uncommitted changes to the code the sandbox installs, so doctor can tell
# when the build no longer matches the checkout.
tree_hash() {
  (cd "$REPO" && { git diff HEAD -- photocopier scripts pyproject.toml
                   git ls-files -o --exclude-standard photocopier; } | shasum | cut -c1-12)
}

# Fingerprint of the photocopier package installed in a sandbox venv, so doctor can tell
# when something other than up replaced the build.
installed_hash() {
  local pkg
  pkg="$(find "$1/venv/lib" -maxdepth 4 -type d -path '*/site-packages/photocopier' 2>/dev/null | head -1)"
  [ -n "$pkg" ] || { echo none; return; }
  (cd "$pkg" && find . -name '*.py' -type f | LC_ALL=C sort | xargs shasum | shasum | cut -c1-12)
}

# The sandbox paths must never resolve to the real installation.
assert_sandboxed() {
  local sb="$1/sandbox" cfg="$1/sandbox/config.toml"
  [ -f "$cfg" ] || die "no sandbox config at $cfg"
  local p
  while IFS= read -r p; do
    case "$p" in
      "$sb"/*) ;;
      *) die "sandbox config path escapes the sandbox: $p" ;;
    esac
  done < <(sed -nE 's/^(path|photos_root|video_root|mount_point|binary) *= *"(\/[^"]*)".*/\2/p' "$cfg" \
             | grep -vxF "$REPO/tests/fake_rclone/rclone")
  # Relative [[source]] paths are folders on the fake remote, not local paths. The rclone
  # binary must be the fake one: the real rclone would reach the real OneDrive.
  grep -qxF "binary    = \"$REPO/tests/fake_rclone/rclone\"" "$cfg" \
    || die "sandbox config does not use the fake rclone"
}

# pc_env RUN CMD...: run CMD with the real installation's variables scrubbed.
pc_env() {
  local run="$1"; shift
  env -u PHOTOCOPIER_SPOOL -u PHOTOCOPIER_CONFIG -u PHOTOCOPIER_SCHEDULED \
      -u FAKE_RCLONE_FAIL_ON FAKE_RCLONE_ROOT="$run/sandbox/remote" "$@"
}

cmd_up() {
  command -v uv >/dev/null || die "uv is required (brew install uv)"
  command -v hdiutil >/dev/null || die "hdiutil is required (macOS only)"
  local run sb ev
  run="$VERIFY_HOME/runs/$(date +%Y%m%d-%H%M%S)-$$"
  sb="$run/sandbox"; ev="$run/evidence"
  mkdir -p "$sb/remote" "$sb/state" "$sb/lock" "$sb/nas" "$ev"
  # A failed `up` must not strand an attached image or a half-built sandbox.
  UP_RUN="$run"
  trap 'rc=$?; if [ $rc -ne 0 ]; then echo "verify: up failed; removing $UP_RUN" >&2; cmd_down "$UP_RUN" >/dev/null 2>&1 || true; rm -rf "$UP_RUN"; fi' EXIT

  # 1. Install the checkout the way production does: a non-editable copy in its own venv.
  uv venv -q "$sb/venv" >/dev/null 2>&1 || die "uv venv failed"
  uv pip install -q --python "$sb/venv/bin/python" "$REPO" || die "installing $REPO failed"

  # 2. The fake OneDrive: the repo's own fixtures, laid out as two phone folders.
  local fx="$REPO/tests/fixtures" p1="$sb/remote/Camera Roll" p2="$sb/remote/Pictures/Camera Roll"
  mkdir -p "$p1" "$p2"
  cp "$fx/1J1A3188.jpg" "$fx/only_datetime.jpg" "$fx/only_digitized.jpg" \
     "$fx/zeroed.jpg" "$fx/garbage_date.jpg" "$p1/"
  printf 'not really a video, dated by its name\n' > "$p1/VID_20260315_101500.mp4"
  printf 'unknown type\n' > "$p1/notes.xyz"
  : > "$p1/.DS_Store"
  cp "$fx/PXL_20261101_021009424.jpg" "$p2/"

  # 3. The NAS: a real mounted volume, so the mount guard is exercised unpatched.
  hdiutil create -quiet -size 64m -fs HFS+J -volname pcverify -type SPARSE "$sb/nas" \
    || die "hdiutil create failed"
  hdiutil attach -quiet -nobrowse -mountpoint "$sb/nas" "$sb/nas.sparseimage" \
    || die "hdiutil attach failed"
  mkdir -p "$sb/nas/photos" "$sb/nas/video"
  touch "$sb/nas/.photocopier-marker"

  # 4. Config: every path inside the sandbox; rclone is the repo's fake binary.
  cat > "$sb/config.toml" <<EOF
# verify-photocopier sandbox config. Generated; never point this at real paths.
[spool]
path   = "$sb/spool"
cap_gb = 1

[destination]
photos_root  = "$sb/nas/photos"
video_root   = "$sb/nas/video"
mount_point  = "$sb/nas"
mount_marker = ".photocopier-marker"

[rclone]
remote    = "onedrive"
binary    = "$REPO/tests/fake_rclone/rclone"
transfers = 2

[[source]]
id     = "phone-1"
path   = "Camera Roll"
suffix = "phone-1"

[[source]]
id     = "phone-2"
path   = "Pictures/Camera Roll"
suffix = "phone-2"
EOF

  # 5. NOTIFY_CMD is word-split by the launcher, so it must live at a path without spaces.
  cat > "$sb/notify.sh" <<EOF
#!/usr/bin/env bash
{ echo "=== SUBJECT: \$1"; echo "\$2"; echo; } >> "$ev/notify.log"
EOF
  chmod +x "$sb/notify.sh"

  {
    printf 'REPO=%q\n' "$REPO"
    printf 'HEAD=%q\n' "$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo none)"
    printf 'TREE=%q\n' "$(tree_hash)"
    printf 'BUILD=%q\n' "$(installed_hash "$sb")"
    printf 'CREATED=%q\n' "$(date -u +%FT%TZ)"
  } > "$sb/run.env"
  cp "$sb/run.env" "$ev/run.env"
  assert_sandboxed "$run"
  echo "$run"
}

cmd_doctor() {
  local run; run="$(run_dir "${1:-}")"; local sb="$run/sandbox" bad=0
  [ -d "$sb" ] || die "$run was torn down (evidence only)"
  assert_sandboxed "$run" && echo "[ok  ] sandbox: every config path is inside $sb"
  if is_attached "$sb/nas"; then echo "[ok  ] nas: image attached at $sb/nas"
  else echo "[FAIL] nas: $sb/nas is not mounted (verify.sh remount RUN)"; bad=1; fi
  [ -f "$sb/nas/.photocopier-marker" ] && echo "[ok  ] marker present" \
    || { echo "[warn] marker missing on the sandbox NAS"; }
  [ -x "$sb/venv/bin/photocopier" ] && echo "[ok  ] build: $("$sb/venv/bin/photocopier" --version)" \
    || { echo "[FAIL] build: no photocopier in $sb/venv"; bad=1; }
  # shellcheck disable=SC1091
  . "$sb/run.env"
  local now; now="$(tree_hash)"
  local head; head="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo none)"
  if [ "$head" = "$HEAD" ] && [ "$now" = "$TREE" ]; then echo "[ok  ] build matches the checkout ($HEAD)"
  else echo "[FAIL] checkout changed since this sandbox was built: tear down and run up again"; bad=1; fi
  if [ -z "${BUILD:-}" ]; then echo "[warn] run.env predates the installed-build check; run up again to get it"
  elif [ "$(installed_hash "$sb")" = "$BUILD" ]; then echo "[ok  ] installed build is the one up installed"
  else echo "[FAIL] installed build differs from the one up installed: something reinstalled the venv; tear down and run up again"; bad=1; fi
  echo "--- photocopier doctor (sandbox)"
  (cd "$sb" && pc_env "$run" "$sb/venv/bin/photocopier" -c "$sb/config.toml" doctor) || bad=1
  return $bad
}

cmd_pc() {
  local run; run="$(run_dir "${1:-}")"; shift
  local sb="$run/sandbox" ev="$run/evidence" rc=0 out
  assert_sandboxed "$run"
  out="$(cd "$sb" && pc_env "$run" "$sb/venv/bin/photocopier" -c "$sb/config.toml" "$@" 2>&1)" || rc=$?
  { echo "\$ photocopier $*"; echo "$out"; echo "[exit $rc]"; echo; } | tee -a "$ev/transcript.log"
  return $rc
}

cmd_snapshot() {
  local run; run="$(run_dir "${1:-}")"; local label="${2:?label required}"
  local sb="$run/sandbox" ev="$run/evidence" n
  n="$(printf '%02d' "$(find "$ev" -name '[0-9][0-9]-*.txt' | wc -l | tr -d ' ')")"
  local f="$ev/$n-$label.txt"
  {
    echo "# snapshot $label at $(date -u +%FT%TZ)"
    for part in remote spool nas; do
      echo; echo "## $part"
      if [ -d "$sb/$part" ]; then
        (cd "$sb/$part" && find . -type f ! -name '*.db-*' -exec ls -ln {} + 2>/dev/null \
          | awk '{s=$5; $1=$2=$3=$4=$5=$6=$7=$8=""; sub(/^ +/,""); print s"\t"$0}' | sort -k2)
      else echo "(absent)"; fi
    done
    echo; echo "## ledger items"
    if [ -f "$sb/spool/ledger.db" ]; then
      # The ledger is WAL-mode. After a clean close SQLite removes -wal/-shm and a read-only
      # open fails, so read it as immutable. After a killed run the -wal file holds committed
      # rows that immutable ignores, so read it read-only: the ledger and its -wal stay unchanged
      # (SQLite may rebuild the -shm index, which holds no ledger data).
      local db="file:$sb/spool/ledger.db?immutable=1"
      [ -f "$sb/spool/ledger.db-wal" ] && db="file:$sb/spool/ledger.db?mode=ro"
      sqlite3 -header -column "$db" \
        "select id, source_id, src_path, state, dest_path, resolved_date, date_source, triage_reason from items order by id;"
      echo; echo "## ledger runs"
      sqlite3 -header -column "$db" \
        "select id, command, scheduled, dry_run, exit_code from runs order by id;"
    else echo "(no ledger yet)"; fi
  } > "$f"
  echo "$f"
}

cmd_launcher() {
  local run; run="$(run_dir "${1:-}")"; shift
  local sb="$run/sandbox" ev="$run/evidence" rc=0
  assert_sandboxed "$run"
  echo "\$ photocopier-run.sh (sandbox) $*" >> "$ev/transcript.log"
  env -u FAKE_RCLONE_FAIL_ON \
    FAKE_RCLONE_ROOT="$sb/remote" \
    PHOTOCOPIER_BIN="$sb/venv/bin/photocopier" \
    PHOTOCOPIER_CONFIG="$sb/config.toml" \
    PHOTOCOPIER_SPOOL="$sb/spool" \
    NAS_MOUNT="$sb/nas" NAS_URL="" \
    STATE_DIR="$sb/state" NAS_JOBS_LOCK="$sb/lock/lock" \
    NOTIFY_CMD="$sb/notify.sh" \
    "$@" bash "$REPO/scripts/photocopier-run.sh" 2>&1 | tee -a "$ev/transcript.log" || rc=$?
  cp "$sb"/state/run-*.log "$ev/" 2>/dev/null || true
  echo "[exit $rc]" | tee -a "$ev/transcript.log"
  return $rc
}

cmd_unmount() { local run; run="$(run_dir "${1:-}")"; hdiutil detach -quiet "$run/sandbox/nas"; echo "detached $run/sandbox/nas"; }
cmd_remount() {
  local run; run="$(run_dir "${1:-}")"; local sb="$run/sandbox"
  is_attached "$sb/nas" || hdiutil attach -quiet -nobrowse -mountpoint "$sb/nas" "$sb/nas.sparseimage"
  echo "attached $sb/nas"
}

cmd_down() {
  local run; run="$(run_dir "${1:-}")"; local sb="$run/sandbox"
  if [ -d "$sb" ]; then
    if is_attached "$sb/nas"; then hdiutil detach -quiet "$sb/nas" || hdiutil detach -quiet -force "$sb/nas"; fi
    is_attached "$sb/nas" && die "could not detach $sb/nas; not deleting the sandbox"
    rm -rf "$sb"
  fi
  echo "torn down; evidence kept at $run/evidence"
}

cmd_ls() {
  [ -d "$VERIFY_HOME/runs" ] || { echo "no runs"; return 0; }
  local run
  for run in "$VERIFY_HOME"/runs/*/; do
    run="${run%/}"
    if [ ! -d "$run/sandbox" ]; then echo "$run  (torn down, evidence only)"
    elif is_attached "$run/sandbox/nas"; then echo "$run  LIVE (nas attached)"
    else echo "$run  sandbox present, nas detached"; fi
  done
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up) cmd_up ;;
  doctor) cmd_doctor "$@" ;;
  pc) cmd_pc "$@" ;;
  snapshot) cmd_snapshot "$@" ;;
  launcher) cmd_launcher "$@" ;;
  unmount) cmd_unmount "$@" ;;
  remount) cmd_remount "$@" ;;
  down) cmd_down "$@" ;;
  ls) cmd_ls ;;
  *) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
