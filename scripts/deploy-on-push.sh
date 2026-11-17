#!/bin/sh
# Deploys the newest main into a server's git checkout of UniScale and restarts the service.
#
# Meant for a server that hosts both the git repository and the app: a systemd path unit watches the
# repository's refs and starts this (as root) on every push. See docs/health_service.md, "Deploy on push".
#
# For each new main it:
#   1. installs any new Python dependencies into the app's venv,
#   2. runs the test suite against the new commit in a temporary worktree (deploys nothing if it fails),
#   3. fast-forwards the checkout and restarts the service,
#   4. rolls back to the previous commit if the service doesn't answer /health.
# Untracked files in the checkout (config.toml, data/) are never touched.
#
# Install a root-owned copy (e.g. /usr/local/sbin/uniscale-deploy) rather than running it from the
# checkout, so a push can't change what runs as root.
set -eu

APP=${APP:-/opt/uniscale}
OWNER=${OWNER:-uniscale}                # user that owns the checkout and runs the service
SERVICE=${SERVICE:-uniscale}
BRANCH=${BRANCH:-main}
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:8088/health}
PY="$APP/venv/bin/python"

UP_TO_DATE=0
DEPLOYED=10
REJECTED=20                             # tests failed or rolled back; the old version keeps running

log() { echo "uniscale-deploy: $*"; }
as_owner() { runuser -u "$OWNER" -- "$@"; }
git_app() { as_owner git -C "$APP" "$@"; }
short() { echo "$1" | cut -c1-8; }

healthy() {
    i=0
    while [ $i -lt 30 ]; do
        curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1 && return 0
        i=$((i + 1))
        sleep 1
    done
    return 1
}

install_deps() { # <dir containing pyproject.toml>
    deps=$(as_owner "$PY" -c 'import sys, tomllib; print("\n".join(tomllib.load(open(sys.argv[1], "rb"))["project"]["dependencies"]))' "$1/pyproject.toml")
    # shellcheck disable=SC2086
    [ -z "$deps" ] || as_owner "$PY" -m pip install -q $deps
}

deploy_once() {
    git_app fetch -q origin "$BRANCH"
    old=$(git_app rev-parse HEAD)
    new=$(git_app rev-parse "origin/$BRANCH")
    if [ "$old" = "$new" ]; then
        log "up to date at $(short "$new")"
        exit $UP_TO_DATE
    fi
    log "deploying $(short "$old") -> $(short "$new")"

    wt=$(as_owner mktemp -d)
    trap 'git_app worktree remove --force "$wt" 2>/dev/null || true' EXIT
    git_app worktree add -q --detach "$wt" "$new"
    if ! git_app diff --quiet "$old" "$new" -- pyproject.toml uv.lock; then
        log "dependencies changed, installing"
        install_deps "$wt"
    fi
    if ! as_owner sh -c "cd '$wt' && '$PY' -m pytest -q -p no:cacheprovider"; then
        log "tests failed for $(short "$new"); still running $(short "$old")"
        exit $REJECTED
    fi

    git_app merge -q --ff-only "$new"
    systemctl restart "$SERVICE"
    if healthy; then
        log "deployed $(short "$new")"
        exit $DEPLOYED
    fi

    log "$(short "$new") is not healthy, rolling back to $(short "$old")"
    git_app reset -q --hard "$old"
    systemctl restart "$SERVICE"
    if healthy; then log "rolled back to $(short "$old")"; else log "rollback is not healthy either: journalctl -u $SERVICE"; fi
    exit $REJECTED
}

# Each deploy runs in its own subshell with `set -e` (which sh ignores inside anything tested by
# `||`, `&&` or `if`, so the exit status is read from $? instead). A push that lands mid-deploy
# doesn't start a second run, so loop until the checkout is up to date.
n=0
while [ $n -lt 5 ]; do
    set +e
    (set -e; deploy_once)
    rc=$?
    set -e
    case $rc in
        "$UP_TO_DATE") exit 0 ;;
        "$DEPLOYED") n=$((n + 1)) ;;
        "$REJECTED") exit 1 ;;
        *) log "deploy failed (exit $rc)"; exit 1 ;;
    esac
done
