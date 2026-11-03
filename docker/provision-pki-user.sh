#!/bin/sh
# Provisions the PKI user for the opt-in TLS fixture. The client certificate's
# CN becomes the Aerospike username; `nopassword` marks it as a PKI-only user
# (certificate auth, no password). Idempotent: "already exists" is fine, but
# any other asadm failure (container down, wrong admin creds, RBAC error)
# fails the script so a setup step can't silently skip provisioning.
set -eu

HOST="${AEROSPIKE_TLS_HOST:-aerospike-tls}"
CERTS="${AEROSPIKE_TLS_CERT_DIR:-/certs}"
CN=agent-squad-client

fail() {
  echo "$*" >&2
  exit 1
}

admin() {
  timeout 15 asadm --no-config-file --no-color --single-node \
    --host "$HOST" --port 3000 --user admin --password admin -e "$1"
}

row() {
  printf '%s\n' "$users" | awk -F '|' -v user="$1" '
    { for (i=1;i<=NF;i++) gsub(/^[[:space:]]+|[[:space:]]+$/, "", $i) }
    NF == 3 && $1 == "User" && $2 == "Roles" && $3 == "Auth Mode" { table=1; next }
    NF != 3 { table=0 }
    table && $1 == user { print $2 "|" $3 }
  '
}

inspect() {
  users=$(admin 'show users' 2>&1) || fail "Cannot inspect fixture users: $users"
  headers=$(printf '%s\n' "$users" | awk -F '|' '
    { for (i=1;i<=NF;i++) gsub(/^[[:space:]]+|[[:space:]]+$/, "", $i) }
    NF == 3 && $1 == "User" && $2 == "Roles" && $3 == "Auth Mode" { count++ }
    END { print count+0 }
  ')
  [ "$headers" = 1 ] || fail "Unsupported asadm show users header; expected User|Roles|Auth Mode exactly once: $users"
  [ -n "$(row admin)" ] || fail "Admin user inspection failed: $users"
  [ -z "$(row not-a-provisioned-user)" ] || fail "Negative-test CN must remain unprovisioned."
}

inspect
if [ -z "$(row "$CN")" ]; then
  output=$(admin "enable; manage acl create user $CN password nopassword roles read-write" 2>&1) || fail "Failed to provision PKI user: $output"
  inspect
fi
[ "$(row "$CN")" = 'read-write|PKI' ] || fail "PKI user must have exactly the read-write role and PKI-only authentication; found: $(row "$CN")"

status=$(timeout 15 asinfo --no-config-file --host "$HOST" --port 4333 \
  --tls-enable --tls-name aerospike-tls --tls-cafile "$CERTS/ca.crt" \
  --tls-certfile "$CERTS/client.crt" --tls-keyfile "$CERTS/client.key" \
  --auth PKI --value status 2>&1) || fail "PKI authentication failed: $status"
[ "$status" = ok ] || fail "PKI authentication returned unexpected status: $status"
echo "TLS fixture ready: PKI authentication and read-write role verified; run host pytest for record write/read verification."
