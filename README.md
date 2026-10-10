# adventurelog-mcp

An [MCP](https://modelcontextprotocol.io) server for [AdventureLog](https://github.com/seanmorley15/AdventureLog),
the self-hosted travel planner. It lets an AI assistant (Claude, or any MCP client) plan trips with you:
build the day-by-day itinerary, add places, stays, flights, notes and checklists, and rearrange days,
all directly in your AdventureLog.

- **Itinerary-aware.** It works with AdventureLog's days, ordering and trip-wide entries, not just raw records.
- **Uses only the public REST API** and your personal API key. Every change goes through AdventureLog's own
  validation and sharing permissions, so collaborators see exactly what they'd see from the web UI.
- **Speaks local time.** Flights, stays and visits are read and written in each item's own timezone.
- **Read-only mode** for safely giving an assistant view-only access.

> Status: early (0.1). Tested against AdventureLog v0.13.0. Upstream discussion:
> [seanmorley15/AdventureLog#1303](https://github.com/seanmorley15/AdventureLog/issues/1303).

## Tools

| Area | Tools |
|---|---|
| Read | `list_trips`, `get_trip` (whole trip, grouped by day), `get_item`, `search_places` (geocoding), `search` |
| Trips & days | `create_trip`, `update_trip`, `delete_trip` (requires the exact trip name), `upsert_day` |
| Items | `upsert_location`, `upsert_lodging`, `upsert_transportation`, `upsert_note`, `upsert_checklist`, `delete_item` |
| Itinerary | `schedule_item`, `move_item`, `reorder_day`, `unschedule_item` |
| Sharing | `share_trip` (sends an invite), `unshare_trip` |

Each `upsert_*` tool creates the item when you omit its id and edits it when you pass one. Pass `trip_id` and a
`date` (or `trip_wide`) to put the item on the itinerary in the same call. Lodging and transportation are
scheduled on their local start date automatically.

## Setup

1. In AdventureLog, go to **Settings → Security → API keys** and create a key (`al_…`).
   On v0.13.0 the button can fail with a 403 ([#1223](https://github.com/seanmorley15/AdventureLog/issues/1223)).
   If it does, create the key from the backend container instead:
   ```sh
   docker exec -it <adventurelog-backend> python manage.py shell -c \
     "from users.models import APIKey, CustomUser; print(APIKey.generate(CustomUser.objects.get(username='YOU'), 'mcp')[1])"
   ```
2. Point your MCP client at the server.

### Claude Code / Claude Desktop (stdio)

```sh
claude mcp add adventurelog \
  -e ADVENTURELOG_URL=https://adventurelog.example.com \
  -e ADVENTURELOG_API_KEY=al_xxx \
  -- uvx --from git+https://github.com/jw-dickson/adventurelog-mcp adventurelog-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "adventurelog": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jw-dickson/adventurelog-mcp", "adventurelog-mcp"],
      "env": { "ADVENTURELOG_URL": "https://adventurelog.example.com", "ADVENTURELOG_API_KEY": "al_xxx" }
    }
  }
}
```

### Remote (streamable HTTP), next to your AdventureLog stack

Add the service to AdventureLog's `docker-compose.yml`:

```yaml
  mcp:
    image: ghcr.io/jw-dickson/adventurelog-mcp:latest
    environment:
      ADVENTURELOG_URL: http://server:80   # the backend service, on the internal network
    ports:
      - "8020:8000"
```

Clients send their own key with each request, so one server serves every user and stores no secrets:

```sh
claude mcp add --transport http adventurelog https://adventurelog-mcp.example.com/mcp \
  --header "Authorization: Bearer al_xxx"
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ADVENTURELOG_URL` | `http://localhost:8016` | Base URL of the AdventureLog **backend** API |
| `ADVENTURELOG_API_KEY` | | Key for stdio mode |
| `ADVENTURELOG_READ_ONLY` | `false` | Only register read tools |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | HTTP bind address |
| `ADVENTURELOG_ALLOW_ENV_KEY_OVER_HTTP` | `false` | Let HTTP requests with no key fall back to `ADVENTURELOG_API_KEY`. Only for a single user on a private network. |

## Security notes

- AdventureLog API keys have full access to their owner's account. Give each client its own key so you can
  revoke it separately, and use `ADVENTURELOG_READ_ONLY` where writes aren't needed.
- In HTTP mode the server never uses a server-side key unless you explicitly opt in. Without that, anyone who
  could reach `/mcp` would act as that user.
- Destructive tools carry MCP `destructiveHint` annotations, so clients can ask for confirmation.

## Known upstream quirks

- Every API-key request costs ~0.3 s server-side (PBKDF2 with 600k iterations per request). A full `get_trip`
  of a large trip takes 1–3 s.
- Editing a checklist recreates its items, so item ids change after `upsert_checklist`.
- Invites only work for users with a public profile.

## Development

```sh
uv sync
uv run pytest tests/unit                       # offline
ADVENTURELOG_URL=… ADVENTURELOG_API_KEY=… uv run pytest tests/integration   # creates and deletes a scratch trip
```

## Credits

Prior art: [Marlon154/AdventureLogMCP](https://github.com/Marlon154/AdventureLogMCP) and
[AdventureLog#1011](https://github.com/seanmorley15/AdventureLog/pull/1011).

## License

GPL-3.0, the same as AdventureLog.
