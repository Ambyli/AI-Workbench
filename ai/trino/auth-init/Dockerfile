# One-shot helper that materializes the Trino coordinator's auth material
# into the `trino_auth` volume before the coordinator starts:
#
#   /auth/tls/trino.pem   private key + self-signed cert (Trino keystore)
#   /auth/tls/trino.crt   cert only — export this for DBeaver truststores
#   /auth/password.db     bcrypt user:hash lines from TRINO_JDBC_USERS
#
# alpine + openssl (cert) + apache2-utils (htpasswd for bcrypt). Nothing
# network-facing; compose runs it with network_mode: none.

FROM alpine:3.20

RUN apk add --no-cache openssl apache2-utils

COPY entrypoint.sh /entrypoint.sh
# Strip CRLF in case the checkout happened on Windows — a `\r` after the
# shebang gives "/bin/sh^M: bad interpreter".
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
