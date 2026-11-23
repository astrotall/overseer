#!/bin/sh
set -eu

fail() {
    echo "redis-entrypoint: $1" >&2
    exit 1
}

url_safe() {
    case "$1" in
        *[!A-Za-z0-9._~-]*) return 1 ;;
    esac
    return 0
}

[ -n "${REDIS_PASSWORD:-}" ] || fail "REDIS_PASSWORD не задан"

executor_user=""
if [ -n "${REDIS_EXECUTOR_PASSWORD:-}" ]; then
    url_safe "$REDIS_EXECUTOR_PASSWORD" \
        || fail "REDIS_EXECUTOR_PASSWORD: допустимы только символы A-Z a-z 0-9 . _ ~ -"
    [ "${#REDIS_EXECUTOR_PASSWORD}" -ge 32 ] \
        || fail "REDIS_EXECUTOR_PASSWORD: нужно не меньше 32 символов (openssl rand -hex 32)"
    [ "$REDIS_EXECUTOR_PASSWORD" != "$REDIS_PASSWORD" ] \
        || fail "REDIS_EXECUTOR_PASSWORD совпадает с REDIS_PASSWORD: у executor свой пароль"
    rules_file="${OVERSEER_EXECUTOR_ACL:-$(dirname "$0")/executor.acl}"
    [ -r "$rules_file" ] || fail "не найден файл ACL-правил executor: $rules_file"
    rules=$(tr '\n' ' ' < "$rules_file")
    executor_user="user overseer-executor reset on >$REDIS_EXECUTOR_PASSWORD $rules"
fi

exec docker-entrypoint.sh redis-server - "$@" <<CONFIG
requirepass $REDIS_PASSWORD
$executor_user
CONFIG
