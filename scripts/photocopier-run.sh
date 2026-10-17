#!/usr/bin/env bash
# Run one photocopier pipeline pass (ingest, process, flush) unattended.
#
# Meant for launchd. It adds what a bare `photocopier` call lacks when nobody is watching:
# a lock shared with other NAS jobs, a mount check that treats "NAS not here" as normal
# (ingest still runs and spools; flush waits for next time), step timeouts, de-duplicated
# failure emails, and one summary email the first time it runs on each calendar day.
#
# Settings come from the environment, with defaults:
#   PHOTOCOPIER_BIN     the photocopier executable   ($HOME/.local/share/photocopier/venv/bin/photocopier)
#   PHOTOCOPIER_SPOOL   spool directory, as in config ($HOME/.local/share/photocopier/spool)
#   NAS_MOUNT           where the share is mounted   (/Volumes/media)
#   NAS_URL             smb://host/share, enables one remount attempt and a reachability test ()
#   STATE_DIR           state and logs               ($HOME/.local/state/photocopier)
#   NAS_JOBS_LOCK       lock shared with other NAS jobs ($HOME/.local/state/nas-jobs/lock)
#   NOTIFY_CMD          run as: $NOTIFY_CMD "<subject>" "<message>"; empty disables email ()
#   BACKLOG_WARN_DAYS   warn in the summary when undelivered files are older than this (3)
#   PHOTOCOPIER_DRY_RUN set to 1 to pass --dry-run to every step and send no email ()
#
# Always exits 0 unless it cannot start; problems are reported by email and in the log.
set -uo pipefail

BIN="${PHOTOCOPIER_BIN:-$HOME/.local/share/photocopier/venv/bin/photocopier}"
SPOOL="${PHOTOCOPIER_SPOOL:-$HOME/.local/share/photocopier/spool}"
NAS_MOUNT="${NAS_MOUNT:-/Volumes/media}"
NAS_URL="${NAS_URL:-}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/photocopier}"
LOCK="${NAS_JOBS_LOCK:-$HOME/.local/state/nas-jobs/lock}"
NOTIFY_CMD="${NOTIFY_CMD:-}"
BACKLOG_WARN_DAYS="${BACKLOG_WARN_DAYS:-3}"
DRY="${PHOTOCOPIER_DRY_RUN:-}"
dry=(); [ "$DRY" = 1 ] && dry=(--dry-run)

mkdir -p "$STATE_DIR" "$(dirname "$LOCK")"
run_log="$STATE_DIR/run-$(date +%Y%m%d-%H%M%S).log"
find "$STATE_DIR" -name 'run-*.log' -mtime +30 -delete 2>/dev/null || true
log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$run_log"; }

[ -x "$BIN" ] || { echo "photocopier not found at $BIN" >&2; exit 127; }

with_timeout() { local secs="$1"; shift; perl -e 'alarm shift; exec @ARGV' "$secs" "$@"; }
notify() { [ -n "$NOTIFY_CMD" ] && [ "$DRY" != 1 ] && { $NOTIFY_CMD "$1" "$2" || true; }; }

# -- lock shared with the other NAS jobs. Skip this pass if another job holds it. --------
have_lock=""
release_lock() { [ -n "$have_lock" ] && rm -rf "$LOCK"; }
trap release_lock EXIT
if ! mkdir "$LOCK" 2>/dev/null; then
  holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    log "skipped: another NAS job ($(cat "$LOCK/job" 2>/dev/null || echo '?'), pid $holder) holds the lock"
    exit 0
  fi
  rm -rf "$LOCK"; mkdir "$LOCK" || { log "could not take the lock"; exit 0; }
fi
have_lock=1; echo $$ > "$LOCK/pid"; echo photocopier > "$LOCK/job"

# -- NAS availability ----------------------------------------------------------------
is_mounted() { mount | grep -q " on $NAS_MOUNT (smbfs"; }
if ! is_mounted && [ -n "$NAS_URL" ]; then
  log "NAS not mounted; trying $NAS_URL"
  with_timeout 60 osascript -e "mount volume \"$NAS_URL\"" >/dev/null 2>&1 || true
  sleep 2
fi
nas_note=""
is_mounted || nas_note="NAS not mounted at $NAS_MOUNT, so delivery was skipped. Files are safe in the spool and OneDrive."

# -- the pipeline --------------------------------------------------------------------
failures=()
step_out=""
step() {  # name, timeout seconds, args...
  local name="$1" secs="$2"; shift 2
  log "== $name"
  local out rc=0
  out="$(caffeinate -i perl -e 'alarm shift; exec @ARGV' "$secs" "$BIN" "$@" 2>&1)" || rc=$?
  printf '%s\n' "$out" | tee -a "$run_log" >/dev/null
  step_out+="[$name]"$'\n'"$out"$'\n\n'
  if [ "$rc" -eq 142 ]; then failures+=("$name timed out after ${secs}s")
  elif [ "$rc" -ne 0 ]; then failures+=("$name exited $rc: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"); fi
}

step ingest  21600 ingest  ${dry[@]+"${dry[@]}"}
step process  3600 process ${dry[@]+"${dry[@]}"}
if [ -z "$nas_note" ]; then
  step flush 21600 flush ${dry[@]+"${dry[@]}"}
else
  log "flush skipped: $nas_note"
fi

# -- reporting ------------------------------------------------------------------------
status_out="$("$BIN" status 2>&1 || true)"
oldest=""
if command -v sqlite3 >/dev/null && [ -f "$SPOOL/ledger.db" ]; then
  oldest="$(sqlite3 -readonly "$SPOOL/ledger.db" "select min(ingested_at) from items where state in ('ingested','processed');" 2>/dev/null || true)"
fi
warn=""
if [ -n "$oldest" ]; then
  age_days=$(( ( $(date +%s) - $(date -j -u -f '%Y-%m-%dT%H:%M:%S' "${oldest%%+*}" +%s 2>/dev/null || date +%s) ) / 86400 ))
  [ "$age_days" -ge "$BACKLOG_WARN_DAYS" ] && warn="WARNING: undelivered files have been waiting $age_days days."
fi
triaged="$(printf '%s' "$status_out" | awk '/triaged/ {print $2; exit}')"

today="$(date +%F)"
summary_due=""; [ "$(cat "$STATE_DIR/summary-date" 2>/dev/null || true)" != "$today" ] && summary_due=1

problem=""
[ ${#failures[@]} -gt 0 ] && problem=1
[ -n "$warn" ] && problem=1
[ "${triaged:-0}" != 0 ] && problem=1

compose() {
  [ ${#failures[@]} -gt 0 ] && { echo "FAILURES:"; printf ' - %s\n' "${failures[@]}"; echo; }
  [ -n "$warn" ] && { echo "$warn"; echo; }
  [ -n "$nas_note" ] && { echo "$nas_note"; echo; }
  echo "STATUS"; echo "$status_out"; echo
  echo "THIS RUN"; printf '%s' "$step_out"
  echo "Log: $run_log"
}

if [ -n "$summary_due" ]; then
  subj="photocopier daily summary on $(hostname -s): ${problem:+needs attention}${problem:-OK}"
  if [ "$DRY" != 1 ] && [ -n "$NOTIFY_CMD" ]; then
    $NOTIFY_CMD "$subj" "$(compose)" && echo "$today" > "$STATE_DIR/summary-date"
  fi
elif [ ${#failures[@]} -gt 0 ]; then
  # Same failure at most once per 24 h, so a six-hourly job cannot flood the inbox.
  key="$(printf '%s' "${failures[*]}" | shasum | cut -c1-16)"
  last="$(cat "$STATE_DIR/last-failure" 2>/dev/null || true)"
  if [ "${last%% *}" != "$key" ] || [ $(( $(date +%s) - ${last##* } )) -gt 86400 ]; then
    notify "photocopier failed on $(hostname -s)" "$(compose)" && echo "$key $(date +%s)" > "$STATE_DIR/last-failure"
  else
    log "failure repeated within 24 h; email suppressed"
  fi
fi
log "done${failures[*]:+ with ${#failures[@]} failure(s)}"
exit 0
