# Support assist: panel data access findings

Date: 2026-11-07. Checked on polite with read-only GETs from inside the `remnawave_bot` container, for bot user 14.
No mutating call was made and nothing was written to any DB (one `SELECT remnawave_id` on the bot DB).

## Verdict

The panel API covers every spike signal. No read-only panel DB role is needed.

## Signal map

| Signal | Endpoint (bot method) | Field | Status |
|---|---|---|---|
| Subscription request history (time, user agent) | `GET /api/users/{id}/subscription-request-history` (`get_subscription_request_history`) | `records[].requestAt`, `records[].userAgent` | OK |
| Total request count | same | `total` | OK. Equalled `len(records)` (24) for the test user, so it may be capped. See caveats |
| Traffic per day, last 2 days | `GET /api/bandwidth-stats/users/{id}?start=&end=` (`get_user_stats_usage`) | `categories` (dates), `sparklineData` (bytes per day, same order); per-node split in `series[].data` | OK. Range `today-2 .. today` returned 3 days, the last one is the current day (partial) |
| Devices with model, OS, created_at | `GET /api/hwid/devices/{id}` (`get_user_devices`) | `devices[].platform`, `osVersion`, `deviceModel`, `userAgent`, `createdAt`, `updatedAt` | OK |
| Accessible nodes | `GET /api/users/{id}/accessible-nodes` (`get_user_accessible_nodes`) | `activeNodes[].uuid`, `nodeName`, `countryCode` | OK, but NO online/disabled status here |
| Node online / disabled status | `GET /api/nodes` (`get_all_nodes`) | `isConnected`, `isDisabled`, `lastStatusChange`, `name`, `uuid` | OK. Join with accessible nodes by `uuid` |
| Last connected node, online_at, first connected | `GET /api/users/{id}` (`get_user_by_id`) | `userTraffic.onlineAt`, `userTraffic.lastConnectedNodeUuid` (resolve name via `/api/nodes`), `userTraffic.firstConnectedAt` | OK |
| Panel status, expiry, HWID limit | same user call | `status`, `expireAt`, `hwidDeviceLimit` | OK |

## Caveats for Task 3 (not missing signals, but traps)

1. `get_subscription_request_history` swallows `RemnaWaveAPIError` and returns `{'total': 0, 'records': []}`. A panel failure would look like "never fetched" and raise a false `never_fetched` flag. In `collect_snapshot` call `api._make_request('GET', ...)` directly (or add a strict variant) so failures land in `missing` as `sub_requests`.
2. `get_user_by_id` runs `enrich_user_with_happ_link`, which can issue a `POST /api/system/tools/happ/encrypt` and an external Happ call. Not wanted for a read-only snapshot, and it handles subscription URLs. Prefer a raw GET of `/api/users/{id}` and read only `userTraffic`, `status`, `expireAt`, `hwidDeviceLimit`.
3. `get_user_accessible_nodes` raises on non-404 errors and returns `[]` on 404. Treat both as unknown (`missing`) rather than "no nodes".
4. History total: 24 for the test user, equal to the record count. The spike rule (`total > 24` with empty records means `sub_history_unavailable`) still holds; if the endpoint caps at 24, `never_fetched` stays safe because it needs `total == 0`.
5. Panel user id: `users.remnawave_id` (int) on the bot user. `coerce_panel_user_id` rejects anything else; a user without it means no panel user (`panel_user = None`).
6. The history, devices and node payloads also carry `requestIp`, `hwid`, `userId`, `uuid`. These must be dropped by the Task 4 allowlist.
7. Probe output printed only structure, timestamps, user agents and device models. No IPs or tokens were logged.

## Future read-only DB role

Not needed. Scope: none.
