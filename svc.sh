#!/usr/bin/env bash
# Install or remove the bot as a service on this Mac: the transcription worker
# under launchd, the bot itself under Docker Compose.
#
#   ./svc.sh install     provision and (re)start both — safe to re-run
#   ./svc.sh uninstall   stop and remove both; keeps .env, models/ and output/
set -eo pipefail
cd "$(dirname "$0")"

LABEL=pro.kyxap.whisper-mlx.worker
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
COMPOSE="bot/docker-compose.yml"

_check_env() {
  if [ -s bot/.env ]; then return 0; fi
  # umask in a subshell: the file holds a bot token, the rest of the run should
  # keep the default mask.
  (umask 077 && cp bot/.env.example bot/.env)
  echo
  echo "Created bot/.env. Fill these in and run this again:"
  echo "  TELEGRAM_BOT_TOKEN   @BotFather -> /newbot"
  echo "  ALLOWED_USER_IDS     your numeric id from @userinfobot"
  echo "  TELEGRAM_API_ID      my.telegram.org -> API development tools"
  echo "  TELEGRAM_API_HASH    same page"
  exit 1
}

_ensure_docker() {
  if docker info >/dev/null 2>&1; then return 0; fi
  echo "==> starting Docker Desktop"
  if ! open -a Docker 2>/dev/null; then
    echo "Docker Desktop is not installed: https://www.docker.com/products/docker-desktop"
    exit 1
  fi
  local _
  for _ in $(seq 60); do
    sleep 1
    if docker info >/dev/null 2>&1; then return 0; fi
  done
  echo "Docker did not come up in 60s. Start it by hand, then run this again."
  exit 1
}

_stop_worker() {
  if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LABEL"
  fi
  # A worker started by hand does not answer to launchd, and two of them would
  # race for the same job files. The argv pattern alone would also fit another
  # clone's worker (and Apple's mdworker.shared), so keep only the processes
  # whose working directory is this checkout.
  local pids pid killed=
  pids=$(pgrep -f '[/ ]worker\.sh$') || return 0
  for pid in $pids; do
    if [ "$(lsof -a -d cwd -Fn -p "$pid" 2>/dev/null | sed -n 's/^n//p')" = "$PWD" ]; then
      kill "$pid"
      killed=1
    fi
  done
  if [ -n "$killed" ]; then sleep 1; fi
}

install() {
  ./install.sh
  _check_env
  _ensure_docker

  # Docker creates missing bind-mount sources as root; launchd refuses to start
  # an agent whose log directory does not exist.
  mkdir -p jobs media output "$(dirname "$PLIST")"

  echo "==> worker"
  _stop_worker
  # & and \ are substitution operators in sed's replacement text, # is the
  # delimiter here -- a repo path holding any of them would corrupt the plist.
  repo_path=$(printf '%s' "$PWD" | sed 's/[&\\#]/\\&/g')
  sed "s#REPO_PATH#$repo_path#g" "bot/deploy/$LABEL.plist" > "$PLIST"
  launchctl bootstrap "gui/$UID" "$PLIST"

  echo "==> bot"
  docker compose -f "$COMPOSE" up -d --build

  echo
  echo "Running. Worker log: jobs/worker.log"
  echo "Bot log: docker compose -f $COMPOSE logs -f"
}

uninstall() {
  echo "==> worker"
  _stop_worker
  rm -f "$PLIST"

  echo "==> bot"
  if docker info >/dev/null 2>&1; then
    docker compose -f "$COMPOSE" down --rmi local
  else
    echo "Docker is not running, so neither is the bot; nothing to stop."
  fi

  echo
  echo "Removed. Kept bot/.env and the media/, jobs/, output/, models/ folders."
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    install) install ;;
    uninstall) uninstall ;;
    *)
      echo "usage: $0 {install|uninstall}"
      exit 1
      ;;
  esac
fi
