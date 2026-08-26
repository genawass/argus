# TAM API discovery

TAM is the shared task database for agent work across projects. This file is
only a bootstrap; the authoritative agent instructions are served by the API.

Before operating TAM, starting long-running work, or recording progress:

1. Load the shared connection details without printing the token:

   ```sh
   source /mnt/datasets/tam/env.sh
   ```

2. Use a distinct actor name for this agent or workspace:

   ```sh
   export TAM_ACTOR="agent-name@workspace"
   ```

3. Read and follow the live instructions from the server that enforces them:

   ```sh
   curl -fsS -H "Authorization: Bearer $TAM_API_TOKEN" \
     "$TAM_API_URL/docs/ARGUS.md"
   ```

Useful discovery calls:

```sh
curl -fsS "$TAM_API_URL/health"
curl -fsS -H "Authorization: Bearer $TAM_API_TOKEN" "$TAM_API_URL/docs"
curl -fsS -H "Authorization: Bearer $TAM_API_TOKEN" "$TAM_API_URL/docs/API.md"
```

The HTTP API is the complete interface from remote nodes; do not open or copy
the SQLite database. Read an issue with comments and history before acting,
bind a watch to every launched job, and never leave an issue `in_progress` when
nothing is running. Never print, commit, or copy the bearer token into logs.
