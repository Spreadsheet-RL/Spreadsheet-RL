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
CREATE_BRIDGE=true

if [ "$#" -eq 3 ]; then
    if [ "$3" == "--no-bridge" ]; then
        CREATE_BRIDGE=false
    else
        usage
    fi
fi

HOST_IP="${SUBNET}.1"
NS_IP="${SUBNET}.2"

# Create the network namespace
sudo ip netns add $NAMESPACE

if $CREATE_BRIDGE; then
    # Create a virtual ethernet pair
    sudo ip link add veth-$NAMESPACE type veth peer name veth-$NAMESPACE-br

    # Move one end of the pair to the namespace
    sudo ip link set veth-$NAMESPACE netns $NAMESPACE

    # Configure the interfaces
    sudo ip addr add $HOST_IP/24 dev veth-$NAMESPACE-br
    sudo ip link set veth-$NAMESPACE-br up

    sudo ip netns exec $NAMESPACE ip addr add $NS_IP/24 dev veth-$NAMESPACE
    sudo ip netns exec $NAMESPACE ip link set veth-$NAMESPACE up
fi

# Bring up the loopback interface
sudo ip netns exec $NAMESPACE ip link set lo up

if $CREATE_BRIDGE; then
    # Set up routing
    sudo ip netns exec $NAMESPACE ip route add default via $HOST_IP

    # Enable IP forwarding on the host
    sudo sysctl -w net.ipv4.ip_forward=1

    # Set up NAT (masquerading) on the host
    EXTERNAL_IFACE=$(ip route | grep default | awk '{print $5}')
    sudo iptables -w -t nat -A POSTROUTING -s ${SUBNET}.0/24 -o $EXTERNAL_IFACE -j MASQUERADE
fi

echo "Namespace $NAMESPACE created with subnet ${SUBNET}.0/24"