# TAM environment -- source this on any node:
#   source /mnt/datasets/tam/env.sh
#
# Deployed copy lives at /mnt/datasets/tam/env.sh. This is the only file TAM
# puts on shared storage, and it is deliberately not code: it distributes an
# address and a token, nothing else. Edit here, copy there.
#
# There is no TAM code on shared storage. The database is local disk on the
# serving host and every other node is an API client, so sourcing this does not
# give you a `tam` binary -- it gives you the address of the one that matters.
#
# What to do with it:
#   curl -H "Authorization: Bearer $TAM_API_TOKEN" "$TAM_API_URL/api/issues"
#   curl -H "Authorization: Bearer $TAM_API_TOKEN" "$TAM_API_URL/docs/ARGUS.md"
#   xdg-open "$TAM_API_URL/"               # the board UI, no install at all
#
# Agent instructions are served from /docs rather than staged next to this file,
# so they cannot drift from the API that enforces them. GET /docs lists them.
#
# On the serving host itself, the API and the timers address the database
# directly through TAM_HOME, which their systemd units set. Sourcing this file
# there puts you in API mode too, on purpose: one path for everybody means the
# thing you test is the thing other nodes get.
#
# Failover: this file is the one place the address is written, so promoting a
# standby is an edit here plus a re-source on each node.

export TAM_API_URL="http://192.168.10.184:8787"

# The token is a shared-storage artifact rather than a value baked in here, so
# rotating it is a one-file change that every node picks up on next source.
if [ -r /mnt/datasets/tam/api_token ]; then
    TAM_API_TOKEN="$(cat /mnt/datasets/tam/api_token)"
    export TAM_API_TOKEN
fi
