# sandbox-egress — allowlist HTTP forward proxy for sandboxed containers.
#
# Built ourselves (alpine + tinyproxy) rather than pulled from
# dannydirect/tinyproxy because that image ships an entrypoint script
# that sed-edits the config file to inject an "Allow ANY" line, fights
# a read-only bind-mount, drops privileges to `nobody` before it can
# write /dev/stdout or the PID file, and — after those workarounds —
# still crash-loops with "Could not create the pool of children"
# because it can't allocate shared memory as an unprivileged UID.
#
# Owning the image lets us:
#   * skip the sed hack entirely (config is trusted, not mutated),
#   * pick a writable PID path,
#   * log to stdout without the /dev/stderr symlink race,
#   * stay as the tinyproxy uid the apk package sets up.
#
# Built by:   docker compose -f ai/sandbox/docker-compose.sandbox.yml build sandbox-egress
# Runs as:    sandbox-egress service (see docker-compose.sandbox.yml).

FROM alpine:3.20

# tinyproxy from the mainline alpine repo — small, well-maintained, and
# tracks upstream reasonably closely (alpine 3.20 ships tinyproxy 1.11.x).
RUN apk add --no-cache tinyproxy && \
    # /var/log/tinyproxy is where the default package config points its
    # log file; make it writable by the tinyproxy user so the process
    # doesn't need root to open a log fd.
    mkdir -p /var/log/tinyproxy /var/run/tinyproxy && \
    chown -R tinyproxy:tinyproxy /var/log/tinyproxy /var/run/tinyproxy && \
    # Remove the package default config — we mount our own read-only in
    # compose. Leaving the default around is harmless but confusing when
    # someone shells in to debug.
    rm -f /etc/tinyproxy/tinyproxy.conf

USER tinyproxy

EXPOSE 8888

# tinyproxy -d = don't daemonize; runs as PID 1 so docker sees crashes.
# Config path matches the mount in docker-compose.sandbox.yml.
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
