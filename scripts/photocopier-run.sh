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
#   NAS_URL             smb://host/share, enables mounting the share when it is not mounted ()
#   NAS_MOUNT_TRIES     rounds of mount attempts before giving up for this pass (3)
#   NAS_MOUNT_WAIT      seconds between rounds, for a network still coming up (15)
#   STATE_DIR           state and logs               ($HOME/.local/state/photocopier)
#   NAS_JOBS_LOCK       lock shared with other NAS jobs ($HOME/.local/state/nas-jobs/lock)
#   NOTIFY_CMD          run as: $NOTIFY_CMD "<subject>" "<message>"; empty disables email ()
#   BACKLOG_WARN_DAYS   warn in the summary when undelivered files are older than this (3)
#   SOURCE_SILENT_DAYS  warn in the summary when a source has sent nothing for more days (7)
#   LOCK_ALERT_HOURS    email once a day when another job has held the lock this long (24)
#   STATUS_TIMEOUT      seconds the reporting `status` call may take (300)
#   STEP_KILL_GRACE     seconds a timed-out step gets to stop before it is killed (30)
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
SOURCE_SILENT_DAYS="${SOURCE_SILENT_DAYS:-}"
NAS_MOUNT_TRIES="${NAS_MOUNT_TRIES:-3}"
LOCK_ALERT_HOURS="${LOCK_ALERT_HOURS:-24}"
[[ "$LOCK_ALERT_HOURS" =~ ^[0-9]+$ ]] || LOCK_ALERT_HOURS=24
NAS_MOUNT_WAIT="${NAS_MOUNT_WAIT:-15}"
STATUS_TIMEOUT="${STATUS_TIMEOUT:-300}"
STEP_KILL_GRACE="${STEP_KILL_GRACE:-30}"
DRY="${PHOTOCOPIER_DRY_RUN:-}"
dry=(); [ "$DRY" = 1 ] && dry=(--dry-run)
# Marks every step as scheduled in the ledger's runs table, for the unattended streak (D13).
export PHOTOCOPIER_SCHEDULED=1

mkdir -p "$STATE_DIR" "$(dirname "$LOCK")"
run_log="$STATE_DIR/run-$(date +%Y%m%d-%H%M%S).log"
find "$STATE_DIR" -name 'run-*.log' -mtime +30 -delete 2>/dev/null || true
log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$run_log"; }
silent_days=()
if [[ "$SOURCE_SILENT_DAYS" =~ ^[1-9][0-9]*$ ]]; then
  silent_days=(--silent-days "$SOURCE_SILENT_DAYS")
elif [ -n "$SOURCE_SILENT_DAYS" ]; then
  log "SOURCE_SILENT_DAYS=\"$SOURCE_SILENT_DAYS\" is not a whole number of days, 1 or more; using the default"
fi

for knob in STATUS_TIMEOUT:300 STEP_KILL_GRACE:30; do
  name="${knob%%:*}"
  [[ "${!name}" =~ ^[1-9][0-9]*$ ]] && continue
  log "$name=\"${!name}\" is not a whole number of seconds, 1 or more; using the default"
  printf -v "$name" '%s' "${knob#*:}"
done

[ -x "$BIN" ] || { echo "photocopier not found at $BIN" >&2; exit 127; }

with_timeout() { local secs="$1"; shift; perl -e 'alarm shift; exec @ARGV' "$secs" "$@"; }
# Runs a command in its own process group under a time limit. On timeout the whole group
# gets TERM, then KILL after STEP_KILL_GRACE seconds, so rclone dies with photocopier
# instead of copying on after the lock is released. Exits 142 on timeout, as an alarm
# would, and otherwise with the command's own status.
LIMIT_PL='
  my ($secs, $grace) = (shift, shift);
  my $pid = fork() // die "fork: $!";
  if ($pid == 0) { setpgrp(0, 0); exec @ARGV or exit 127; }
  my $timed_out = 0;
  $SIG{ALRM} = sub {
    if ($timed_out++) { kill "KILL", -$pid } else { kill "TERM", -$pid; alarm $grace }
  };
  alarm $secs;
  1 while waitpid($pid, 0) == -1 && $!{EINTR};
  my $status = $?;
  alarm 0;
  if ($timed_out) {
    for (1 .. $grace) { last unless kill 0, -$pid; sleep 1 }
    kill "KILL", -$pid;
    exit 142;
  }
  exit($status & 127 ? 128 + ($status & 127) : $status >> 8);
'
run_limited() { perl -e "$LIMIT_PL" "$@"; }
notify() {
  [ -n "$NOTIFY_CMD" ] && [ "$DRY" != 1 ] || return 1
  local rc=0
  $NOTIFY_CMD "$1" "$2" || rc=$?
  [ "$rc" -eq 0 ] || log "could not send the email \"$1\", NOTIFY_CMD failed with exit $rc"
  return "$rc"
}

# -- lock shared with the other NAS jobs. Skip this pass if another job holds it. --------
# A pass killed without running its EXIT trap (a crash, a power cut) leaves the lock behind,
# and after a restart its pid often belongs to some other process. So a photocopier lock
# records its process's start time too, and counts as held only while that same process
# runs. Another job's lock is always honored, but a lock held for more than a day is
# emailed about, once a day, so a stuck job never silently stops every pass.
have_lock=""
release_lock() { [ -n "$have_lock" ] && rm -rf "$LOCK"; }
trap release_lock EXIT
started_of() { LC_ALL=C ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' | sed 's/^ //; s/ $//'; }
# The holder is a past photocopier pass whose pid now names an unrelated process.
reused_pid() {
  local pid="$1" recorded
  [ "$(cat "$LOCK/job" 2>/dev/null)" = photocopier ] || return 1
  recorded="$(cat "$LOCK/started" 2>/dev/null || true)"
  if [ -n "$recorded" ]; then [ "$(started_of "$pid")" != "$recorded" ]; return; fi
  # A lock from a launcher that predates the start-time record: check the command instead.
  case "$(ps -o command= -p "$pid" 2>/dev/null)" in *photocopier-run*) return 1 ;; esac
}
report_blocked() {
  local pid="$1" job="$2" age_h today
  age_h=$(( $(perl -e 'print int(time - (stat shift)[9])' "$LOCK" 2>/dev/null || echo 0) / 3600 ))
  today="$(date +%F)"
  [ "$age_h" -ge "$LOCK_ALERT_HOURS" ] || return 0
  [ "$(cat "$STATE_DIR/blocked-date" 2>/dev/null || true)" != "$today" ] || return 0
  notify "photocopier blocked on $(hostname -s)" \
    "Every photocopier pass for the last $age_h hours has been skipped, because the NAS lock at $LOCK is held by $job (pid $pid). Nothing is lost: OneDrive still holds every photo. If $job is not running, remove $LOCK." \
    && echo "$today" > "$STATE_DIR/blocked-date"
}
if ! mkdir "$LOCK" 2>/dev/null; then
  holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    if reused_pid "$holder"; then
      log "took over a stale lock (pid $holder, started $(cat "$LOCK/started" 2>/dev/null || echo 'unrecorded')): that pid is now another process"
    else
      job="$(cat "$LOCK/job" 2>/dev/null || echo '?')"
      log "skipped: another NAS job ($job, pid $holder) holds the lock"
      report_blocked "$holder" "$job"
      exit 0
    fi
  fi
  rm -rf "$LOCK"; mkdir "$LOCK" || { log "could not take the lock"; exit 0; }
fi
have_lock=1; echo $$ > "$LOCK/pid"; echo photocopier > "$LOCK/job"; started_of $$ > "$LOCK/started"

# -- NAS availability ----------------------------------------------------------------
# After a restart or a trip, a bare host name like `freenas` often has no cached address,
# and mounting by it hangs until the timeout; its Bonjour name, `freenas.local`, resolves
# at once. So each round tries NAS_URL and, for a bare host, the same URL with `.local`,
# mounting only through a name that resolves. The network may still be coming up when
# launchd runs a missed job at wake, hence more than one round. Away from home neither
# name resolves, and a round costs seconds rather than a minute. Resolution goes through
# the system resolver: a direct probe such as `nc` can be refused by macOS's Local Network
# privacy even while the share mounts fine.
is_mounted() { mount | grep -q " on $NAS_MOUNT (smbfs"; }
url_host() { printf '%s' "$1" | sed -E 's#^[a-z]+://([^@/]*@)?([^/:]+).*#\2#'; }
mount_urls() {
  local host; host="$(url_host "$NAS_URL")"
  printf '%s\n' "$NAS_URL"
  case "$host" in
    *.*) ;;
    *) printf '%s\n' "$NAS_URL" | sed -E "s#^([a-z]+://([^@/]*@)?)$host#\1$host.local#" ;;
  esac
}
resolves() { with_timeout 10 dscacheutil -q host -a name "$1" </dev/null 2>/dev/null | grep -q '^ip_address:'; }
try_mount() {
  local round url
  for (( round = 1; round <= NAS_MOUNT_TRIES; round++ )); do
    while IFS= read -r url; do
      resolves "$(url_host "$url")" || continue
      log "NAS not mounted; mounting $url (round $round of $NAS_MOUNT_TRIES)"
      with_timeout 30 osascript -e "mount volume \"$url\"" </dev/null >/dev/null 2>&1 || true
      sleep 2
      is_mounted && { log "NAS mounted at $NAS_MOUNT"; return 0; }
      [ -n "$(mounted_elsewhere)" ] && return 1  # retrying cannot fix that; the note says why
    done < <(mount_urls)
    [ "$round" -lt "$NAS_MOUNT_TRIES" ] && sleep "$NAS_MOUNT_WAIT"
  done
  log "NAS not reachable or would not mount after $NAS_MOUNT_TRIES round(s)"
  return 1
}
# macOS mounts at media-1 when a leftover folder already sits at /Volumes/media.
mounted_elsewhere() {
  local share="${NAS_URL##*/}"
  mount | sed -nE "s#^.*/$share on (/Volumes/$share-[0-9]+) \(smbfs.*#\1#p" | head -1
}

if ! is_mounted && [ -n "$NAS_URL" ]; then try_mount || true; fi
nas_note=""
if ! is_mounted; then
  nas_note="NAS not mounted at $NAS_MOUNT, so delivery was skipped. Files are safe in the spool and OneDrive."
  elsewhere="$(mounted_elsewhere)"
  [ -n "$elsewhere" ] && nas_note+=" The share is mounted at $elsewhere instead: a leftover folder at $NAS_MOUNT is probably in the way."
fi

# -- the pipeline --------------------------------------------------------------------
failures=()
step_out=""
step() {  # name, timeout seconds, args...
  local name="$1" secs="$2"; shift 2
  log "== $name"
  local out rc=0
  out="$(caffeinate -i perl -e "$LIMIT_PL" "$secs" "$STEP_KILL_GRACE" "$BIN" "$@" 2>&1)" || rc=$?
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
status_rc=0
status_out="$(run_limited "$STATUS_TIMEOUT" "$STEP_KILL_GRACE" "$BIN" status ${silent_days[@]+"${silent_days[@]}"} 2>&1)" || status_rc=$?
if [ "$status_rc" -eq 142 ]; then failures+=("status timed out after ${STATUS_TIMEOUT}s")
elif [ "$status_rc" -ne 0 ]; then failures+=("status exited $status_rc: $(printf '%s' "$status_out" | tail -2 | tr '\n' ' ')"); fi
oldest=""
if command -v sqlite3 >/dev/null && [ -f "$SPOOL/ledger.db" ]; then
  # Not -readonly: the ledger is in WAL mode, and a read-only connection cannot create the
  # -shm file SQLite removes on the last close, so it fails to open at all. A SELECT
  # writes nothing. Errors go to the log rather than silently disabling the warning.
  oldest="$(sqlite3 -cmd '.timeout 5000' "$SPOOL/ledger.db" "select min(ingested_at) from items where state in ('ingested','processed');" 2>>"$run_log" || true)"
fi
warn=""
if [ -n "$oldest" ]; then
  age_days=$(( ( $(date +%s) - $(date -j -u -f '%Y-%m-%dT%H:%M:%S' "${oldest%%+*}" +%s 2>/dev/null || date +%s) ) / 86400 ))
  [ "$age_days" -ge "$BACKLOG_WARN_DAYS" ] && warn="WARNING: undelivered files have been waiting $age_days days."
fi
status_warn="$(printf '%s\n' "$status_out" | grep '^WARNING: ' || true)"
# Each WARNING line is shown once, above STATUS, so the STATUS copy leaves those lines out.
status_body="$(printf '%s\n' "$status_out" | grep -v '^WARNING: ' || true)"
triaged="$(printf '%s' "$status_out" | awk '/triaged/ {print $2; exit}')"

today="$(date +%F)"
summary_due=""; [ "$(cat "$STATE_DIR/summary-date" 2>/dev/null || true)" != "$today" ] && summary_due=1

problem=""
[ ${#failures[@]} -gt 0 ] && problem=1
[ -n "$warn" ] && problem=1
[ -n "$status_warn" ] && problem=1
[ "${triaged:-0}" != 0 ] && problem=1

compose() {
  [ ${#failures[@]} -gt 0 ] && { echo "FAILURES:"; printf ' - %s\n' "${failures[@]}"; echo; }
  [ -n "$warn" ] && { echo "$warn"; echo; }
  [ -n "$status_warn" ] && { echo "$status_warn"; echo; }
  [ -n "$nas_note" ] && { echo "$nas_note"; echo; }
  echo "STATUS"; echo "$status_body"; echo
  echo "THIS RUN"; printf '%s' "$step_out"
  echo "Log: $run_log"
}

if [ -n "$summary_due" ]; then
  verdict="OK"; [ -n "$problem" ] && verdict="needs attention"
  subj="photocopier daily summary on $(hostname -s): $verdict"
  notify "$subj" "$(compose)" && echo "$today" > "$STATE_DIR/summary-date"
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
