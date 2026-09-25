#!/bin/sh
# One pod's side of the network check: netcheck.sh CHECK...
#   dns:NAME             NAME resolves
#   open:HOST:PORT       a TCP connection to HOST:PORT is established
#   closed:HOST:PORT     it is not
#   settled:HOST:PORT    HOST resolves, and then HOST:PORT stops answering
# Prints PASS or FAIL per check and exits 1 if any failed.
#
# A CNI programs a new pod's rules a moment after the pod starts. Until then some (kube-router,
# in k3s) let the pod's traffic through unfiltered, and the destination's own ingress rules are
# all that stands in the way. The settled check waits that moment out on a canary that only
# the pod's egress rules can refuse, and runs first: an open that passes after it is the
# allow-list's answer, not the unfiltered start. Names, opens and the settled check are retried
# until they pass or NETCHECK_WAIT seconds have gone by; closed checks are tried once, last.

wait_seconds=${NETCHECK_WAIT:-60}
connect_seconds=${NETCHECK_CONNECT_TIMEOUT:-3}
deadline=$(($(date +%s) + wait_seconds))
failed=0

resolves() { nslookup "$1" >/dev/null 2>&1; }
connects() { nc -z -w "$connect_seconds" "${1%:*}" "${1##*:}" >/dev/null 2>&1; }

until_deadline() {
    until "$@"; do
        [ "$(date +%s)" -ge "$deadline" ] && return 1
        sleep 1
    done
}

verdict() {
    if [ "$1" -eq 0 ]; then
        echo "PASS $2"
    else
        echo "FAIL $2"
        failed=1
    fi
}

settled() {
    host=${1%:*}
    until_deadline resolves "$host" || return 1
    started=$(date +%s)
    until_deadline refuses "$1" || return 1
    open_for=$(($(date +%s) - started))
}
refuses() { ! connects "$1"; }

for check; do
    case $check in
        settled:*)
            if settled "${check#settled:}"; then
                verdict 0 "$check (egress unfiltered for ${open_for}s)"
            else
                verdict 1 "$check"
            fi
            ;;
    esac
done
for check; do
    case $check in
        settled:*) ;;
        dns:*) until_deadline resolves "${check#dns:}"; verdict $? "$check" ;;
        open:*) until_deadline connects "${check#open:}"; verdict $? "$check" ;;
        closed:*) ;;
        *) verdict 1 "$check (unknown check)" ;;
    esac
done
for check; do
    case $check in
        closed:*) ! connects "${check#closed:}"; verdict $? "$check" ;;
    esac
done
exit $failed
