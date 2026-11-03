#!/bin/sh
# shellcheck disable=SC3028
# Generates throwaway PKI material for the opt-in TLS test fixture.
# Output (all under docker/tls-certs/):
#   ca.crt/ca.key          - CA that signs the server and client certs
#   server.crt/server.key  - server cert, CN=aerospike-tls, SAN DNS:aerospike-tls,IP:127.0.0.1
#   client.crt/client.key  - client cert, CN=agent-squad-client (PKI user CN)
#   other-ca.crt           - unrelated CA (wrong-CA negative tests)
#   untrusted-client.*     - client cert signed by other-ca (mTLS negative test)
#   other-user.crt/.key    - CA-signed cert with an unprovisioned CN (PKI negative)
#   mismatch.key           - keypair that does not match client.crt
set -eu
umask 077

ROOT="${AEROSPIKE_TLS_CERT_DIR:-$(dirname "$0")/tls-certs}"
FILES="ca.crt ca.key server.crt server.key client.crt client.key other-ca.crt other-ca.key untrusted-client.crt untrusted-client.key other-user.crt other-user.key mismatch.key"

fail() {
  echo "$* Stop the TLS services before an explicit fixture reset; see README.md." >&2
  exit 1
}

if [ -n "${AEROSPIKE_TLS_UID:-}" ] || [ -n "${AEROSPIKE_TLS_GID:-}" ]; then
  case "${AEROSPIKE_TLS_UID:-}" in ''|*[!0-9]*) fail "Set both numeric AEROSPIKE_TLS_UID and AEROSPIKE_TLS_GID." ;; esac
  case "${AEROSPIKE_TLS_GID:-}" in ''|*[!0-9]*) fail "Set both numeric AEROSPIKE_TLS_UID and AEROSPIKE_TLS_GID." ;; esac
fi
[ ! -L "$ROOT" ] || fail "Certificate directory must not be a symlink."
mkdir -p "$ROOT"
mkdir "$ROOT/.init-lock" 2>/dev/null || fail "Cannot acquire certificate initialization lock. Another initializer may be running. If no initializer is active, remove only the stale lock with: rmdir \"$ROOT/.init-lock\". Never remove an active initializer's lock."
trap 'status=$?; rmdir "$ROOT/.init-lock"; if [ "$status" -ne 0 ] && [ -d "$ROOT/.staging" ]; then echo "Certificate generation incomplete. Stop the TLS services before an explicit fixture reset; see README.md." >&2; fi; exit "$status"' EXIT
trap 'exit 1' HUP INT TERM
[ ! -e "$ROOT/.staging" ] || fail "Interrupted certificate generation found."

public_key() {
  openssl pkey -in "$1" -pubout -outform DER | openssl dgst -sha256
}

certificate_key() {
  openssl x509 -in "$1" -pubkey -noout | openssl pkey -pubin -outform DER | openssl dgst -sha256
}

validate() {
  for file in $FILES; do
    [ -f "$OUT/$file" ] && [ ! -L "$OUT/$file" ] || fail "Missing or unsafe certificate material: $file."
  done
  for key in "$OUT"/*.key; do
    openssl pkey -in "$key" -check -noout >/dev/null || fail "Invalid private key: $key."
  done
  for identity in ca server client other-ca untrusted-client other-user; do
    openssl x509 -in "$OUT/$identity.crt" -checkend 0 -noout >/dev/null || fail "Expired or invalid certificate: $identity."
    openssl x509 -in "$OUT/$identity.crt" -noout -text | grep -Fq 'Version: 3 (0x2)' || fail "Certificate must be X.509 v3 for SDK compatibility: $identity."
    [ "$(public_key "$OUT/$identity.key")" = "$(certificate_key "$OUT/$identity.crt")" ] || fail "Certificate/key mismatch: $identity."
  done
  for identity in ca server client other-user; do
    openssl verify -CAfile "$OUT/ca.crt" "$OUT/$identity.crt" >/dev/null || fail "Invalid trusted certificate: $identity."
  done
  for identity in other-ca untrusted-client; do
    openssl verify -CAfile "$OUT/other-ca.crt" "$OUT/$identity.crt" >/dev/null || fail "Invalid negative-test chain: $identity."
    if openssl verify -CAfile "$OUT/ca.crt" "$OUT/$identity.crt" >/dev/null 2>&1; then
      fail "Negative-test identity unexpectedly trusted: $identity."
    fi
  done
  for pair in 'ca:agent-squad-test-ca' 'server:aerospike-tls' 'client:agent-squad-client' 'other-ca:untrusted-ca' 'untrusted-client:untrusted-client' 'other-user:not-a-provisioned-user'; do
    identity=${pair%%:*}
    cn=${pair#*:}
    [ "$(openssl x509 -in "$OUT/$identity.crt" -noout -subject -nameopt RFC2253)" = "subject=CN=$cn" ] || fail "Unexpected certificate identity: $identity."
  done
  sans=$(openssl x509 -in "$OUT/server.crt" -noout -ext subjectAltName)
  printf '%s\n' "$sans" | grep -Fq 'DNS:aerospike-tls' || fail "Server DNS SAN missing."
  printf '%s\n' "$sans" | grep -Fq 'IP Address:127.0.0.1' || fail "Server IP SAN missing."
  [ "$(public_key "$OUT/mismatch.key")" != "$(certificate_key "$OUT/client.crt")" ] || fail "Negative-test key unexpectedly matches."
}

present=0
for entry in "$ROOT"/* "$ROOT"/.[!.]* "$ROOT"/..?*; do
  [ -e "$entry" ] || [ -L "$entry" ] || continue
  name=${entry##*/}
  case "$name" in
    .init-lock) continue ;;
    server) [ -d "$entry" ] && [ ! -L "$entry" ] || fail "Unsafe server directory." ;;
    .complete) [ -f "$entry" ] && [ ! -L "$entry" ] || fail "Unsafe completion marker."; present=1 ;;
    *)
      case " $FILES " in
        *" $name "*) present=1 ;;
        *) fail "Unexpected certificate directory entry: $name." ;;
      esac ;;
  esac
done

if [ "$present" = 0 ]; then
  for entry in "$ROOT/server"/* "$ROOT/server"/.[!.]*; do
    [ ! -e "$entry" ] && [ ! -L "$entry" ] || fail "Partial server material found."
  done
  OUT="$ROOT/.staging"
  mkdir "$OUT"

# --- CA ---
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign" \
  -keyout "$OUT/ca.key" -out "$OUT/ca.crt" \
  -subj "/CN=agent-squad-test-ca"

# --- Server cert: CN + SAN must match the configured tls-name 'aerospike-tls' ---
openssl req -newkey rsa:2048 -nodes \
  -keyout "$OUT/server.key" -out "$OUT/server.csr" \
  -subj "/CN=aerospike-tls"
printf 'subjectAltName=DNS:aerospike-tls,IP:127.0.0.1\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n' > "$OUT/server.ext"
openssl x509 -req -in "$OUT/server.csr" -days 30 \
  -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
  -out "$OUT/server.crt" \
  -extfile "$OUT/server.ext"

# --- Client cert (CN is the PKI username) ---
openssl req -newkey rsa:2048 -nodes \
  -keyout "$OUT/client.key" -out "$OUT/client.csr" \
  -subj "/CN=agent-squad-client"
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n' > "$OUT/client.ext"
openssl x509 -req -in "$OUT/client.csr" -days 30 \
  -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
  -out "$OUT/client.crt" -extfile "$OUT/client.ext"

# --- Untrusted CA for negative tests ---
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign" \
  -keyout "$OUT/other-ca.key" -out "$OUT/other-ca.crt" \
  -subj "/CN=untrusted-ca"

# --- Client cert signed by the untrusted CA (mTLS negative test) ---
openssl req -newkey rsa:2048 -nodes \
  -keyout "$OUT/untrusted-client.key" -out "$OUT/untrusted-client.csr" \
  -subj "/CN=untrusted-client"
openssl x509 -req -in "$OUT/untrusted-client.csr" -days 30 \
  -CA "$OUT/other-ca.crt" -CAkey "$OUT/other-ca.key" -CAcreateserial \
  -out "$OUT/untrusted-client.crt" -extfile "$OUT/client.ext"

# --- CA-signed client cert whose CN is not a provisioned user (PKI negative) ---
openssl req -newkey rsa:2048 -nodes \
  -keyout "$OUT/other-user.key" -out "$OUT/other-user.csr" \
  -subj "/CN=not-a-provisioned-user"
openssl x509 -req -in "$OUT/other-user.csr" -days 30 \
  -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
  -out "$OUT/other-user.crt" -extfile "$OUT/client.ext"

# --- Client keypair that does not match client.crt ---
openssl genrsa -out "$OUT/mismatch.key" 2048

  validate
  rm -f "$OUT"/*.csr "$OUT"/*.srl "$OUT"/*.ext
  for file in $FILES; do mv "$OUT/$file" "$ROOT/$file"; done
  rmdir "$OUT"
else
  OUT="$ROOT"
  validate
fi

mkdir -p "$ROOT/server"
for file in server.crt server.key ca.crt; do
  target="$ROOT/server/$file"
  if [ -e "$target" ] || [ -L "$target" ]; then
    [ -f "$target" ] && [ ! -L "$target" ] && cmp -s "$ROOT/$file" "$target" || fail "Inconsistent server material: $file."
  else
    [ ! -e "$ROOT/.complete" ] || fail "Missing server material: $file."
    cp "$ROOT/$file" "$target"
  fi
done
for entry in "$ROOT/server"/* "$ROOT/server"/.[!.]*; do
  [ -e "$entry" ] || [ -L "$entry" ] || continue
  case "${entry##*/}" in server.crt|server.key|ca.crt) ;; *) fail "Unexpected server material." ;; esac
done
chmod 700 "$ROOT" "$ROOT/server"
chmod 600 "$ROOT"/*.key "$ROOT/server/server.key"
chmod 644 "$ROOT"/*.crt "$ROOT/server"/*.crt
printf '1\n' > "$ROOT/.complete"
if [ -n "${AEROSPIKE_TLS_UID:-}" ] || [ -n "${AEROSPIKE_TLS_GID:-}" ]; then
  chown "$AEROSPIKE_TLS_UID:$AEROSPIKE_TLS_GID" "$ROOT" "$ROOT/.complete" "$ROOT"/*.crt "$ROOT"/*.key "$ROOT/server" "$ROOT/server"/* || fail "Cannot set fixture ownership."
fi
echo "TLS test certs ready in $ROOT"
