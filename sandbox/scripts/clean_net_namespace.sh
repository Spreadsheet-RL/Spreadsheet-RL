#!/bin/bash
set -e

usage() {
    echo "Usage: $0 <namespace_name> <subnet> [--no-bridge]"
    exit 1
}

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    usage
fi

NAMESPACE=$1
SUBNET=$2
CLEAN_BRIDGE=true

if [ "$#" -eq 3 ]; then
    if [ "$3" == "--no-bridge" ]; then
        CLEAN_BRIDGE=false
    else
        usage
    fi
fi

if $CLEAN_BRIDGE; then
    # Remove NAT rules
    EXTERNAL_IFACE=$(ip route | grep default | awk '{print $5}')
    sudo iptables -w -t nat -D POSTROUTING -s ${SUBNET}.0/24 -o $EXTERNAL_IFACE -j MASQUERADE
fi

# Delete namespace
sudo ip netns delete $NAMESPACE

echo "Namespace $NAMESPACE cleaned up"