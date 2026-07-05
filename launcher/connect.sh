#!/usr/bin/env bash
# Semi-automatic launcher for selfmatrix-hires participants (macOS/Linux).
# Reads a key=value config file (see hires.conf.example) and execs jacktrip
# with the assembled connection command. See docs/requirements.md §4.4.
#
# Usage:
#   connect.sh [CONFIG_PATH] [--dry-run]
#
#   CONFIG_PATH defaults to hires.conf next to this script.
#   --dry-run prints the command that would be run (PASSWORD masked) and exits
#   without execing jacktrip.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"

CONFIG_PATH=""
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Usage: $0 [CONFIG_PATH] [--dry-run]" >&2
      exit 0
      ;;
    *)
      if [[ -n "$CONFIG_PATH" ]]; then
        echo "Unexpected extra argument: $arg" >&2
        exit 1
      fi
      CONFIG_PATH="$arg"
      ;;
  esac
done

if [[ -z "$CONFIG_PATH" ]]; then
  CONFIG_PATH="${SCRIPT_DIR}/hires.conf"
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "設定ファイルが見つかりません: ${CONFIG_PATH}" >&2
  echo "運用者から受け取った hires.conf をこのスクリプトと同じディレクトリに置くか、" >&2
  echo "パスを引数で指定してください。雛形は hires.conf.example を参照してください。" >&2
  exit 1
fi

# Warn if the config file (which may contain PASSWORD) is group/other readable.
PERM_BITS="$(stat -c '%a' "$CONFIG_PATH" 2>/dev/null || stat -f '%Lp' "$CONFIG_PATH" 2>/dev/null || true)"
if [[ -n "$PERM_BITS" && "${#PERM_BITS}" -ge 3 ]]; then
  GROUP_OTHER="${PERM_BITS: -2}"
  if [[ "$GROUP_OTHER" != "00" ]]; then
    echo "警告: ${CONFIG_PATH} は他ユーザーからも読み取り可能なパーミッションです (${PERM_BITS})。" >&2
    echo "      PASSWORD を書いている場合は 'chmod 600 ${CONFIG_PATH}' を推奨します。" >&2
  fi
fi

# Defaults (overridden by config file below).
HOST=""
SAMPLE_RATE=192000
BIT_RES=24
CHANNELS=2
QUEUE=8
USERNAME=""
PASSWORD=""
AUDIO_DEVICE=""
EXTRA_ARGS=""

while IFS='=' read -r raw_key raw_value || [[ -n "$raw_key" ]]; do
  # Strip comments/blank lines and surrounding whitespace.
  key="$(echo "$raw_key" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [[ -z "$key" || "$key" == \#* ]] && continue
  value="${raw_value#"${raw_value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  case "$key" in
    HOST) HOST="$value" ;;
    SAMPLE_RATE) SAMPLE_RATE="$value" ;;
    BIT_RES) BIT_RES="$value" ;;
    CHANNELS) CHANNELS="$value" ;;
    QUEUE) QUEUE="$value" ;;
    USERNAME) USERNAME="$value" ;;
    PASSWORD) PASSWORD="$value" ;;
    AUDIO_DEVICE) AUDIO_DEVICE="$value" ;;
    EXTRA_ARGS) EXTRA_ARGS="$value" ;;
    *) echo "警告: 未知の設定キーを無視します: ${key}" >&2 ;;
  esac
done < "$CONFIG_PATH"

if [[ -z "$HOST" ]]; then
  echo "設定ファイルに HOST が指定されていません: ${CONFIG_PATH}" >&2
  exit 1
fi
if [[ -z "$USERNAME" ]]; then
  echo "設定ファイルに USERNAME が指定されていません: ${CONFIG_PATH}" >&2
  exit 1
fi

if ! command -v jacktrip >/dev/null 2>&1; then
  echo "jacktrip コマンドが見つかりません。" >&2
  echo "インストール手順: https://jacktrip.github.io/jacktrip/Install/" >&2
  exit 1
fi

# Build the argument list. Each element is one argv entry, so values with
# spaces (e.g. AUDIO_DEVICE) do not need manual quoting here.
JACKTRIP_ARGS=(
  -C "$HOST"
  -T "$SAMPLE_RATE"
  -b "$BIT_RES"
  -n "$CHANNELS"
  --udprt
  -q "$QUEUE"
  -R
  -A
  --username "$USERNAME"
)

if [[ -n "$PASSWORD" ]]; then
  JACKTRIP_ARGS+=(--password "$PASSWORD")
fi
if [[ -n "$AUDIO_DEVICE" ]]; then
  JACKTRIP_ARGS+=(--audiodevice "$AUDIO_DEVICE")
fi
if [[ -n "$EXTRA_ARGS" ]]; then
  # Intentional word-splitting: EXTRA_ARGS is documented in
  # hires.conf.example as a space-separated argument list.
  # shellcheck disable=SC2206
  EXTRA_ARGS_ARR=($EXTRA_ARGS)
  JACKTRIP_ARGS+=("${EXTRA_ARGS_ARR[@]}")
fi

print_masked_command() {
  # Mask by position (the argument right after --password), not by value —
  # value matching would also hide other fields that happen to share the
  # same string.
  echo -n "jacktrip"
  local prev=""
  for a in "${JACKTRIP_ARGS[@]}"; do
    if [[ "$prev" == "--password" ]]; then
      printf ' %q' "*****"
    else
      printf ' %q' "$a"
    fi
    prev="$a"
  done
  echo
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  print_masked_command
  exit 0
fi

exec jacktrip "${JACKTRIP_ARGS[@]}"
