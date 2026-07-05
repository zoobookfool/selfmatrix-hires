#!/usr/bin/env bash
# One-shot provisioning of an OPTIONAL hi-res audio hub (JackTrip, hub mode) on
# a stateless VPS. This is an independent extension module — it is NOT part of
# the main SelfMatrix stack and does not talk to Synapse/LiveKit/Element Call
# in any way. See README.md for the participant/operator guide. The spike and
# design records live in the parent project:
# https://github.com/zoobookfool/selfmatrix (docs/hires-spike.md,
# docs/requirements.md §4/§9, docs/roadmap.md Phase 6 — decided 2026-07-05:
# hi-res audio ships as this standalone extension, not a client-fork feature).
#
# Usage:
#   sudo bash provision.sh [--max-clients 6] [--bind-port 4464] \
#     [--sample-rate 192000] [--dry-run]
#
#   sudo bash provision.sh --add-user alice [--password <pw>] [--dry-run]
#
# Every provisioning value can also come from the environment (MAX_CLIENTS,
# BIND_PORT, SAMPLE_RATE) instead of flags.
#
# --dry-run prints every step it would take (including generated file
# contents) without touching the system: no package installs, no user/dir
# creation, no TLS generation, no systemd unit writes, no ufw changes.
#
# Requires root (except --dry-run and --help, which run unprivileged so you
# can preview the plan before running for real).
set -euo pipefail

HIRES_USER="hires"
HIRES_CONF_DIR="/etc/selfmatrix-hires"
CREDS_FILE="${HIRES_CONF_DIR}/credentials"
TLS_CERT="${HIRES_CONF_DIR}/tls.crt"
TLS_KEY="${HIRES_CONF_DIR}/tls.key"

MAX_CLIENTS="${MAX_CLIENTS:-6}"
BIND_PORT="${BIND_PORT:-4464}"
SAMPLE_RATE="${SAMPLE_RATE:-192000}"
DRY_RUN=0

ADD_USER=""
ADD_USER_PASSWORD=""

usage() {
  cat >&2 <<'USAGE'
Usage: provision.sh [--max-clients N] [--bind-port P] \
         [--sample-rate R] [--dry-run]
       provision.sh --add-user NAME [--password PW] [--dry-run]

Provisioning options:
  --max-clients N   MAX_CLIENTS   Max simultaneous participants (default: 6)
  --bind-port P     BIND_PORT     JackTrip hub TCP control port (default: 4464)
  --sample-rate R   SAMPLE_RATE   jackd dummy-driver sample rate in Hz (default: 192000)
  --dry-run                       Print planned actions and generated file
                                   contents only; touch nothing on the system.
  -h, --help                      Show this help.

User management (independent of provisioning, no root steps beyond the
credentials file):
  --add-user NAME    Add NAME to the JackTrip credentials file. Generates a
                      random password unless --password is given, and prints
                      it once — copy it now and hand it to the participant
                      over a secure channel; it is not stored anywhere else.
  --password PW      Password to use with --add-user instead of a random one.
USAGE
  exit 1
}

require_value() {
  # require_value <flag> <argc-left> — fail with usage if the flag has no value.
  if [[ "$2" -lt 2 ]]; then
    echo "Missing value for $1" >&2
    usage
  fi
}

PROVISION_FLAGS_SET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-clients) require_value "$1" $#; MAX_CLIENTS="$2"; PROVISION_FLAGS_SET=1; shift 2 ;;
    --bind-port) require_value "$1" $#; BIND_PORT="$2"; PROVISION_FLAGS_SET=1; shift 2 ;;
    --sample-rate) require_value "$1" $#; SAMPLE_RATE="$2"; PROVISION_FLAGS_SET=1; shift 2 ;;
    --add-user) require_value "$1" $#; ADD_USER="$2"; shift 2 ;;
    --password) require_value "$1" $#; ADD_USER_PASSWORD="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

# Validate numeric values up front, before any step has side effects — a typo
# here must not surface as an arithmetic error after packages/units are in place.
if ! [[ "$MAX_CLIENTS" =~ ^[0-9]+$ && "$BIND_PORT" =~ ^[0-9]+$ && "$SAMPLE_RATE" =~ ^[0-9]+$ ]]; then
  echo "--max-clients / --bind-port / --sample-rate must be positive integers" >&2
  usage
fi

run() {
  # Print the command always; execute it unless --dry-run.
  echo "+ $*"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    "$@"
  fi
}

write_file() {
  # write_file <path> — writes stdin to <path>, or just prints it under --dry-run.
  local path="$1"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "+ would write ${path}:"
    sed 's/^/    /'
  else
    echo "+ writing ${path}"
    cat > "$path"
  fi
}

ufw_allow() {
  local rule="$1"
  if [[ "$DRY_RUN" -eq 0 ]] && ufw status | grep -qF "$rule"; then
    echo "ufw rule already present, skipping: ${rule}"
  else
    run ufw allow "$rule"
  fi
}

# --add-user is an independent subcommand: it only touches the credentials
# file and does not require the rest of provisioning to have run first (other
# than the conf dir already existing).
if [[ -n "$ADD_USER" ]]; then
  if [[ "$PROVISION_FLAGS_SET" -eq 1 ]]; then
    echo "NOTE: provisioning flags (--max-clients/--bind-port/--sample-rate) are ignored with --add-user." >&2
  fi
  if [[ "$DRY_RUN" -eq 0 && "$(id -u)" -ne 0 ]]; then
    echo "Run as root (or via sudo). Re-run with --dry-run to preview without root." >&2
    exit 1
  fi
  if [[ "$DRY_RUN" -eq 0 && ! -d "$HIRES_CONF_DIR" ]]; then
    echo "${HIRES_CONF_DIR} does not exist — run provisioning first." >&2
    exit 1
  fi

  echo "== SelfMatrix hires VPS: add user =="
  echo "ADD_USER=${ADD_USER}"
  echo "DRY_RUN=${DRY_RUN}"
  echo

  if [[ "$DRY_RUN" -eq 0 && -f "$CREDS_FILE" ]] && grep -q "^${ADD_USER}:" "$CREDS_FILE"; then
    echo "User '${ADD_USER}' already exists in ${CREDS_FILE}." >&2
    exit 1
  fi

  if [[ -n "$ADD_USER_PASSWORD" ]]; then
    PASSWORD="$ADD_USER_PASSWORD"
  elif [[ "$DRY_RUN" -eq 0 ]]; then
    PASSWORD="$(openssl rand -base64 12)"
  else
    PASSWORD="<generated by openssl rand -base64 12>"
  fi

  if [[ "$DRY_RUN" -eq 0 ]]; then
    # -stdin keeps the password out of the process argument list (visible in
    # ps/procfs to other local users while openssl runs).
    HASH="$(printf '%s' "$PASSWORD" | openssl passwd -6 -stdin)"
  else
    HASH="<generated by openssl passwd -6 -stdin>"
  fi

  echo "+ appending ${ADD_USER}:<hash>:* to ${CREDS_FILE}"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    echo "${ADD_USER}:${HASH}:*" >> "$CREDS_FILE"
    chown root:"$HIRES_USER" "$CREDS_FILE"
    chmod 640 "$CREDS_FILE"
  fi

  echo
  if [[ -z "$ADD_USER_PASSWORD" ]]; then
    echo "Generated password for '${ADD_USER}': ${PASSWORD}"
    echo "Copy this now and hand it to the participant over a secure channel —"
    echo "it is not stored anywhere and cannot be recovered from the credentials file."
  else
    echo "User '${ADD_USER}' added with the password you supplied via --password."
  fi
  exit 0
fi

echo "== SelfMatrix hires VPS provisioning =="
echo "MAX_CLIENTS=${MAX_CLIENTS}"
echo "BIND_PORT=${BIND_PORT}"
echo "SAMPLE_RATE=${SAMPLE_RATE}"
echo "DRY_RUN=${DRY_RUN}"
echo

# 1. Prerequisite checks -----------------------------------------------------

if [[ "$DRY_RUN" -eq 0 && "$(id -u)" -ne 0 ]]; then
  echo "Run as root (or via sudo). Re-run with --dry-run to preview without root." >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]] || ! grep -qi '^ID=ubuntu' /etc/os-release; then
  echo "This script targets Ubuntu. /etc/os-release does not report ID=ubuntu." >&2
  if [[ "$DRY_RUN" -eq 0 ]]; then
    exit 1
  fi
  echo "(continuing anyway: --dry-run)" >&2
fi

# 2. packages -----------------------------------------------------------------

echo
echo "-- [1/5] packages (jackd2, jacktrip, openssl) --"
if command -v jacktrip >/dev/null 2>&1 && command -v jackd >/dev/null 2>&1 && command -v openssl >/dev/null 2>&1; then
  echo "jackd2/jacktrip/openssl already installed, skipping."
else
  run apt-get update -y
  run apt-get install -y jackd2 jacktrip openssl
fi

# 3. system user + conf dir --------------------------------------------------

echo
echo "-- [2/5] system user + ${HIRES_CONF_DIR} --"
if [[ "$DRY_RUN" -eq 0 ]] && id "$HIRES_USER" >/dev/null 2>&1; then
  echo "user '${HIRES_USER}' already exists, skipping."
else
  run useradd --system --no-create-home "$HIRES_USER"
fi

run mkdir -p "$HIRES_CONF_DIR"
run chmod 750 "$HIRES_CONF_DIR"
run chown root:"$HIRES_USER" "$HIRES_CONF_DIR"

if [[ "$DRY_RUN" -eq 0 && -f "$CREDS_FILE" ]]; then
  echo "${CREDS_FILE} already exists, leaving it untouched."
else
  write_file "$CREDS_FILE" </dev/null
  run chown root:"$HIRES_USER" "$CREDS_FILE"
  run chmod 640 "$CREDS_FILE"
fi

# 4. self-signed TLS certificate ----------------------------------------------

echo
echo "-- [3/5] self-signed TLS certificate --"
if [[ "$DRY_RUN" -eq 0 && -f "$TLS_CERT" && -f "$TLS_KEY" ]]; then
  echo "${TLS_CERT} / ${TLS_KEY} already exist, leaving them untouched."
else
  run openssl req -x509 -newkey rsa:4096 -days 3650 -nodes \
    -keyout "$TLS_KEY" -out "$TLS_CERT" \
    -subj "/CN=selfmatrix-hires"
  run chown root:"$HIRES_USER" "$TLS_KEY" "$TLS_CERT"
  run chmod 640 "$TLS_KEY"
  # The certificate itself is public material; 644 is deliberate.
  run chmod 644 "$TLS_CERT"
fi

# 5. systemd units ------------------------------------------------------------

echo
echo "-- [4/5] systemd units (not enabled — start on demand) --"

JACK_UNIT="/etc/systemd/system/selfmatrix-hires-jack.service"
write_file "$JACK_UNIT" <<UNITEOF
# Generated by provision.sh. Hi-res audio is an optional
# module (see README.md) — start on demand, do not enable at boot.
[Unit]
Description=SelfMatrix hi-res audio hub - jackd (dummy driver)

[Service]
User=${HIRES_USER}
Environment=JACK_NO_AUDIO_RESERVATION=1
ExecStart=/usr/bin/jackd --no-realtime -d dummy -r ${SAMPLE_RATE} -p 128
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNITEOF

HUB_UNIT="/etc/systemd/system/selfmatrix-hires-hub.service"
write_file "$HUB_UNIT" <<UNITEOF
# Generated by provision.sh. Hi-res audio is an optional
# module (see README.md) — start on demand, do not enable at boot.
[Unit]
Description=SelfMatrix hi-res audio hub - jacktrip hub server
After=selfmatrix-hires-jack.service
Requires=selfmatrix-hires-jack.service

[Service]
User=${HIRES_USER}
# jackd (Type=simple) reports "active" before the JACK server is actually
# ready to accept clients; a short wait plus retry-on-failure covers the race.
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/jacktrip -S -p 2 -b 24 --udprt -q 8 -B ${BIND_PORT} -A \
  --certfile ${TLS_CERT} --keyfile ${TLS_KEY} --credsfile ${CREDS_FILE}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNITEOF

run systemctl daemon-reload

echo "Units written but not enabled (RAM stays free until you 'systemctl start' them;"
echo "see Next steps below)."

# Re-running with different flags rewrites the units, but a running service
# keeps its old configuration until restarted — say so instead of surprising.
for unit in selfmatrix-hires-jack.service selfmatrix-hires-hub.service; do
  if [[ "$DRY_RUN" -eq 0 ]] && systemctl is-active --quiet "$unit"; then
    echo "NOTE: ${unit} is currently running with its previous configuration;"
    echo "      run 'systemctl restart ${unit}' to apply the rewritten unit."
  fi
done

# 6. ufw ----------------------------------------------------------------------

echo
echo "-- [5/5] ufw firewall rules --"
if ! command -v ufw >/dev/null 2>&1; then
  run apt-get update -y
  run apt-get install -y ufw
fi

UDP_BASE_PORT=$((61002 + BIND_PORT - 4464))
UDP_RANGE_COUNT=$((MAX_CLIENTS * 2))
UDP_END_PORT=$((UDP_BASE_PORT + UDP_RANGE_COUNT - 1))

ufw_allow "${BIND_PORT}/tcp"
ufw_allow "${UDP_BASE_PORT}:${UDP_END_PORT}/udp"

echo "NOTE: re-running with a different --bind-port/--max-clients adds new rules but"
echo "      does NOT remove previously added ones — clean up stale rules with"
echo "      'ufw status numbered' and 'ufw delete <n>'."
echo "NOTE: not running 'ufw enable' — enable it yourself once you have confirmed"
echo "      the rules above are correct, so you don't lock yourself out over SSH"
echo "      (make sure 22/tcp is allowed first if ufw is not already active)."

cat <<NEXT

== Next steps ==

1. Add participants to the credentials file (independent of provisioning):
     sudo bash provision.sh --add-user alice
   Repeat per participant. The generated password is shown once — copy it
   and hand it to the participant over a secure channel.
2. Start the hub on demand (it is not enabled at boot to save RAM on small
   VPS instances):
     sudo systemctl start selfmatrix-hires-jack.service
     sudo systemctl start selfmatrix-hires-hub.service
   Stop it when nobody is using it:
     sudo systemctl stop selfmatrix-hires-hub.service selfmatrix-hires-jack.service
3. Client connect command (run by each participant, see README.md
   for install steps per OS):
     jacktrip -C <your DNS-only hires host> -T ${SAMPLE_RATE} -b 24 -n 2 --udprt -R -A \\
       --username <name> --password <password>
   Use the same DNS-only host you use for other DNS-only endpoints in this
   deployment (do not put this behind a CDN proxy — JackTrip's UDP media
   needs to reach this host directly).
4. Review firewall rules with 'ufw status' and run 'ufw enable' yourself once
   you have confirmed 22/tcp (SSH) is allowed.
5. Full participant/operator guide: README.md
NEXT
