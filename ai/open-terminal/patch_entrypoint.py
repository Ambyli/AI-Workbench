"""Build-time patch for open-terminal's /app/entrypoint.sh.

Run by ai/open-terminal/Dockerfile.open-terminal as root inside the image
build. Applies upstream PR open-webui/open-terminal#118 (fix for issue #119,
"Egress filtering is non-functional") to the pinned release, and REFUSES to
build if the script no longer contains the exact lines it rewrites — either
the PR merged (delete this file and the Dockerfile, point the compose at the
upstream image) or the entrypoint changed shape (re-read it before touching
the strings below).

Two hunks:

1. `capsh --drop=cap_net_admin` was run as the unprivileged `user`. Dropping
   a capability from the bounding set needs CAP_SETPCAP in the caller's
   EFFECTIVE set, and a non-root process in Docker has an empty effective set
   regardless of `cap_add`, so the container died at boot with
   "unable to raise CAP_SETPCAP for BSET changes: Operation not permitted".
   Fix: run capsh via `sudo -E` (root: has SETPCAP; `-E` keeps the
   OPEN_TERMINAL_* environment through sudoers' env_reset — SETENV is implied
   because the sudoers rule is `ALL`), then `--user=user` so the server still
   runs as the unprivileged account with CAP_NET_ADMIN gone from its bounding
   set for good. open-terminal itself is installed system-wide by upstream's
   Dockerfile (`pip install .` as root → /usr/local/bin), so sudo's
   secure_path still finds it.

2. The blanket `--dport 53 DROP` had no exception for dnsmasq's own forward
   to the upstream resolver. On Docker/Linux the upstream is 127.0.0.11,
   which the earlier `-o lo ACCEPT` already covers, so this hunk is a no-op
   here — it is applied anyway so the image behaves the same on a host
   whose resolver is not loopback.
"""

from __future__ import annotations

import pathlib
import sys

ENTRYPOINT = pathlib.Path("/app/entrypoint.sh")

CAPSH_OLD = 'exec capsh --drop=cap_net_admin -- -c "exec open-terminal $*"'
CAPSH_NEW = (
    'exec sudo -E capsh --drop=cap_net_admin --user=user -- -c "exec open-terminal $*"'
    "  # zeo: open-terminal#118"
)

DNS_DROP_OLD = "        sudo iptables -A OUTPUT -p udp --dport 53 -j DROP"
DNS_DROP_NEW = (
    '        sudo iptables -A OUTPUT -p udp -d "$UPSTREAM_DNS" --dport 53 -j ACCEPT'
    "  # zeo: open-terminal#118 — let dnsmasq reach its upstream\n"
    '        sudo iptables -A OUTPUT -p tcp -d "$UPSTREAM_DNS" --dport 53 -j ACCEPT\n'
    + DNS_DROP_OLD
)


def main() -> int:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    for label, needle in (("capsh line", CAPSH_OLD), ("dns drop line", DNS_DROP_OLD)):
        count = text.count(needle)
        if count != 1:
            print(
                f"patch_entrypoint: expected exactly one '{label}' in {ENTRYPOINT}, "
                f"found {count}. Upstream changed — re-read entrypoint.sh (or drop this "
                f"patch if open-webui/open-terminal#118 has merged).",
                file=sys.stderr,
            )
            return 1
    if "--user=user" in text:
        print("patch_entrypoint: entrypoint already carries the fix — drop this patch.", file=sys.stderr)
        return 1

    patched = text.replace(CAPSH_OLD, CAPSH_NEW).replace(DNS_DROP_OLD, DNS_DROP_NEW)
    # Explicit LF: the file is a bash script and must not pick up platform newlines.
    ENTRYPOINT.write_text(patched, encoding="utf-8", newline="\n")
    print("patch_entrypoint: applied open-terminal#118 (capsh via sudo -E --user=user; dnsmasq upstream ACCEPT)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
