#!/bin/sh
# Run as: sudo unshare -n sh chipmunk/test/run_mcast_sources.sh /absolute/test_binary
set -eu

if [ "$#" -ne 1 ] || [ ! -x "$1" ]; then
    echo "usage: $0 /absolute/test_mcast_sources" >&2
    exit 2
fi
case "$1" in /*) ;; *) echo "binary path must be absolute" >&2; exit 2 ;; esac
test_ns=$(readlink /proc/self/ns/net)
if [ "$test_ns" = "$(readlink /proc/1/ns/net)" ] ||
   [ "$(ip -o link show | awk -F': ' '{print $2}')" != "lo" ]; then
    echo "run this fixture inside a fresh 'unshare -n' namespace containing only lo" >&2
    exit 2
fi

sender_pid=
cleanup() {
    if [ -n "$sender_pid" ]; then
        kill "$sender_pid" 2>/dev/null || :
        wait "$sender_pid" 2>/dev/null || :
    fi
    ip link del upx_rx 2>/dev/null || :
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
ip link set lo up
unshare -n "$1" --sender &
sender_pid=$!
attempt=0
while [ "$(readlink "/proc/$sender_pid/ns/net" 2>/dev/null || :)" = "$test_ns" ]; do
    kill -0 "$sender_pid"
    attempt=$((attempt + 1))
    [ "$attempt" -lt 100 ] || { echo "sender namespace startup timed out" >&2; exit 1; }
    sleep 0.02
done
kill -0 "$sender_pid"
ip link add upx_rx type veth peer name upx_tx
ip link set upx_tx netns "$sender_pid"
ip addr add 198.18.0.2/24 dev upx_rx
ip link set upx_rx up
nsenter -t "$sender_pid" -n ip link set lo up
nsenter -t "$sender_pid" -n ip addr add 198.18.0.1/24 dev upx_tx
nsenter -t "$sender_pid" -n ip link set upx_tx up
"$1"
