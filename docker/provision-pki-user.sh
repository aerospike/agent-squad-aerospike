#!/bin/sh
# Provisions the PKI user for the opt-in TLS fixture. The client certificate's
# CN becomes the Aerospike username; `nopassword` marks it as a PKI-only user
# (certificate auth, no password). Idempotent: "already exists" is fine, but
# any other asadm failure (container down, wrong admin creds, RBAC error)
# fails the script so a setup step can't silently skip provisioning.
set -eu

CONTAINER="${AEROSPIKE_TLS_CONTAINER:-agent-squad-aerospike-aerospike-tls-1}"
CN="${AEROSPIKE_TLS_PKI_USER:-agent-squad-client}"

output=$(docker exec "$CONTAINER" asadm --user admin --password admin \
  -e "enable; manage acl create user $CN password nopassword roles read-write" 2>&1) || {
  if ! printf '%s' "$output" | grep -qi "already exists"; then
    printf '%s\n' "$output" >&2
    echo "Failed to provision PKI user '$CN' on $CONTAINER" >&2
    exit 1
  fi
}
echo "PKI user '$CN' provisioned on $CONTAINER"
