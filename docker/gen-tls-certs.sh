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

OUT="$(dirname "$0")/tls-certs"
mkdir -p "$OUT"

# --- CA ---
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$OUT/ca.key" -out "$OUT/ca.crt" \
  -subj "/CN=agent-squad-test-ca" 2>/dev/null

# --- Server cert: CN + SAN must match the configured tls-name 'aerospike-tls' ---
openssl req -newkey rsa:2048 -nodes \
  -keyout "$OUT/server.key" -out "$OUT/server.csr" \
  -subj "/CN=aerospike-tls" 2>/dev/null
printf "subjectAltName=DNS:aerospike-tls,IP:127.0.0.1" > "$OUT/server.ext"
openssl x509 -req -in "$OUT/server.csr" -days 3650 \
  -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
  -out "$OUT/server.crt" \
  -extfile "$OUT/server.ext" 2>/dev/null

# --- Client cert (CN is the PKI username) ---
openssl req -newkey rsa:2048 -nodes \
  -keyout "$OUT/client.key" -out "$OUT/client.csr" \
  -subj "/CN=agent-squad-client" 2>/dev/null
openssl x509 -req -in "$OUT/client.csr" -days 3650 \
  -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
  -out "$OUT/client.crt" 2>/dev/null

# --- Untrusted CA for negative tests ---
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$OUT/other-ca.key" -out "$OUT/other-ca.crt" \
  -subj "/CN=untrusted-ca" 2>/dev/null

# --- Client cert signed by the untrusted CA (mTLS negative test) ---
openssl req -newkey rsa:2048 -nodes \
  -keyout "$OUT/untrusted-client.key" -out "$OUT/untrusted-client.csr" \
  -subj "/CN=untrusted-client" 2>/dev/null
openssl x509 -req -in "$OUT/untrusted-client.csr" -days 3650 \
  -CA "$OUT/other-ca.crt" -CAkey "$OUT/other-ca.key" -CAcreateserial \
  -out "$OUT/untrusted-client.crt" 2>/dev/null

# --- CA-signed client cert whose CN is not a provisioned user (PKI negative) ---
openssl req -newkey rsa:2048 -nodes \
  -keyout "$OUT/other-user.key" -out "$OUT/other-user.csr" \
  -subj "/CN=not-a-provisioned-user" 2>/dev/null
openssl x509 -req -in "$OUT/other-user.csr" -days 3650 \
  -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
  -out "$OUT/other-user.crt" 2>/dev/null

# --- Client keypair that does not match client.crt ---
openssl genrsa -out "$OUT/mismatch.key" 2048 2>/dev/null

rm -f "$OUT"/*.csr "$OUT"/*.srl "$OUT"/*.ext
chmod 644 "$OUT"/*
echo "TLS test certs written to $OUT"
