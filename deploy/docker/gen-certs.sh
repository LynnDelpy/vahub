#!/usr/bin/env bash
#
# Generate a throwaway CA, a server certificate for the proxy, and one client
# certificate, into ./certs.
#
# THIS IS NOT A PRODUCTION PKI.
#
# The CA private key is written next to the certificates it signs, unencrypted,
# on the same machine that serves the traffic. Anyone who can read that file can
# mint a client certificate and unlock whatever your policy allows. It is here so
# that a first install takes one command instead of an afternoon.
#
# For a deployment you keep:
#   * keep the CA key offline (a smartcard, an air-gapped machine, or a real CA)
#   * issue one client certificate per device, so a lost phone revokes one thing
#   * set client_auth to check a revocation list, or keep the certificate
#     lifetimes short enough that expiry is your revocation mechanism
#
# Usage:
#   ./gen-certs.sh                       # localhost
#   VAHUB_DOMAIN=hub.lan ./gen-certs.sh  # a name on your network
#   CLIENT_CN=phone ./gen-certs.sh --force
set -euo pipefail

cd "$(dirname "$0")"

DOMAIN="${VAHUB_DOMAIN:-localhost}"
CLIENT_CN="${CLIENT_CN:-vahub-client}"
DAYS_CA="${DAYS_CA:-3650}"
# Under 825 days, which is what browsers accept for a server certificate.
DAYS_LEAF="${DAYS_LEAF:-820}"

mkdir -p certs
cd certs

if [ -f ca.crt ] && [ "${1:-}" != "--force" ]; then
  echo "certificates already exist in $(pwd)"
  echo "use --force to replace them (every issued client certificate stops working)"
  exit 0
fi

umask 077

echo "== certificate authority =="
openssl req -x509 -newkey rsa:4096 -sha256 -nodes \
  -keyout ca.key -out ca.crt -days "$DAYS_CA" \
  -subj "/CN=vahub development CA/O=vahub" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

echo "== server certificate ($DOMAIN) =="
openssl req -newkey rsa:2048 -sha256 -nodes -keyout server.key -out server.csr \
  -subj "/CN=$DOMAIN/O=vahub"

# The SAN list is what a client actually checks; the common name is ignored by
# every current TLS stack. localhost and 127.0.0.1 stay in the list so the
# health check and a local curl keep working when DOMAIN is something else.
cat > server.ext <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$DOMAIN,DNS:localhost,IP:127.0.0.1
EOF

openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days "$DAYS_LEAF" -sha256 -extfile server.ext

echo "== client certificate ($CLIENT_CN) =="
openssl req -newkey rsa:2048 -sha256 -nodes -keyout client.key -out client.csr \
  -subj "/CN=$CLIENT_CN/O=vahub"

cat > client.ext <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
EOF

openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out client.crt -days "$DAYS_LEAF" -sha256 -extfile client.ext

# Browsers and phones import a PKCS#12 bundle, not a pair of PEM files. No
# passphrase, because this is a development bundle and a passphrase here would
# only be theatre. Add -passout pass:something before you carry it anywhere.
openssl pkcs12 -export -inkey client.key -in client.crt -certfile ca.crt \
  -name "$CLIENT_CN" -out client.p12 -passout pass:

rm -f server.csr client.csr
chmod 600 ca.key server.key client.key client.p12
chmod 644 ca.crt server.crt client.crt

cat <<EOF

done. files in $(pwd)

  ca.crt      give this to clients so they trust the proxy
  ca.key      SECRET. delete it once you have issued the certificates you need
  server.crt  served by the proxy for $DOMAIN
  client.p12  import into a browser or phone (empty password)

test it:
  curl --cacert $(pwd)/ca.crt --cert $(pwd)/client.crt --key $(pwd)/client.key \\
       https://$DOMAIN:8443/health

If a browser refuses the p12, its keychain may not accept the modern OpenSSL 3
encryption defaults. Re-export with: openssl pkcs12 -export -legacy ...

The audit log records the certificate subject as the acting principal, so give
each device its own CLIENT_CN. "who turned the lights on at 3am" is only
answerable if the certificates are not shared.
EOF
