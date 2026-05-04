# Session State - Claude Code / Codex Handoff

Rolling handoff for the Polymarket V2 ARB migration.

**Last updated:** 2026-05-04 by Claude

---

## Gate State

State: `blocked`

Gate record:

- `C:\tmp\decision_gates\2026-04-20_migration-gate_polymarket-v2.md`

Do not run signed-order smoke, unpause ARB, create `/root/arb_v2_ready`, or remove
`/root/arb_paused` without explicit operator authorization. The collateral account
mismatch is reconciled, but the authorized direct signed submit smoke was rejected by
CLOB geoblock before acceptance.

---

## Last Completed

- Root-caused the CLOB/raw collateral mismatch:
  - Deployed ARB resolver was calling `getPolyProxyWalletAddress(address)` on V2
    exchange `0xE111180000d2663C0091e4f400237545B87B996B`.
  - That call reverts, causing the old code to silently fall back to configured
    `POLYMARKET_ADDRESS` `0xaeA2bb57baF02E5148C478a7d7D5dBF851ceA7Ae`.
  - The configured address has `0` pUSD, `0` allowances, and no CTF approvals.
  - The working helper path is old CTF exchange
    `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`, which resolves signer
    `0xcECEEb57accF34ED2e21D25d6C2037F6109751f0` to actual proxy wallet
    `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`.
- Patched `C:\tmp\arb_poly_executor.py` and redeployed only
  `/root/kalshiedge_arb/poly_executor.py`:
  - V2 SDK/order path still uses V2 exchange `0xE111...`.
  - Proxy-wallet resolution uses old helper `0x4bFb...`.
  - No silent fallback to `POLYMARKET_ADDRESS`.
  - Live initialization fails closed unless the resolved proxy has pUSD balance,
    pUSD allowances, and CTF approvals for exchange_v2, neg-risk adapter, and
    neg-risk exchange_v2.
- Added tests:
  - `C:\tmp\tests\test_arb_v2_proxy_funder.py`
  - Covers V2/helper revert no-fallback, old-helper funded proxy resolution, and
    unfunded proxy readiness block.
- Verification:
  - Red test run against old code failed for the expected resolver/readiness gaps.
  - Isolated VPS test run passed:
    `python3 -m pytest -q tests/test_arb_v2_proxy_funder.py tests/test_v2_collateral_address.py tests/test_arb_v2_cutover_gate.py`
    -> `8 passed`.
  - Local `py_compile` passed for `arb_poly_executor.py`, `arb_main.py`, and the new test.
  - VPS isolated-copy `py_compile` passed.
  - Deployed `/root/kalshiedge_arb/poly_executor.py` and `/root/kalshiedge_arb/arb_main.py`
    compile.
- Non-order readiness after deploy:
  - resolved runtime funder `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`
  - raw pUSD balance raw `50668374` = `50.668374` pUSD
  - raw pUSD allowances max-uint to exchange_v2, neg-risk adapter, and neg-risk exchange_v2
  - raw CTF approvals `true` for exchange_v2, neg-risk adapter, and neg-risk exchange_v2
  - CLOB `/balance-allowance` returned balance raw `50668374` and same max allowances
  - direct `get_open_orders()` returned list length `0`
  - `/root/arb_paused` exists
  - `/root/arb_v2_ready` absent
  - `arb_main` not running
  - dashboard `POST /api/bot/arb/start` returned `409 Conflict` with missing ready flag
  - no signed-order smoke run, no order placed, no unpause, no ready flag created
- Signed submit/cancel smoke attempt (2026-05-03):
  - Preconditions before submit passed:
    `/root/arb_paused` present, `/root/arb_v2_ready` absent, `arb_main` not running,
    dashboard ARB start returned `409 Conflict`, and `get_open_orders()` returned `[]`.
  - Runtime funder resolved to actual proxy wallet
    `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`.
  - Raw readiness for that funder reconciled: pUSD balance raw `50668374`, max pUSD
    allowances, and CTF approvals true for exchange_v2, neg-risk adapter, and
    neg-risk exchange_v2.
  - CLOB `/balance-allowance` matched with balance raw `50668374`.
  - One post-only GTC BUY limit was attempted on token
    `78433024518676680431174478322854148606578065650008220678402966840627347604025`
    at price `0.01`, size `5.0`; best ask was `0.16`.
  - Direct CLOB submit was rejected before acceptance:
    `PolyApiException status_code=403` / `Trading restricted in your region`.
  - Accepted: no. Order id: none. Cancel: not applicable because no order id was returned.
  - Final `get_open_orders()` returned `[]`; `test_orders_remaining=0`.
  - Safety state after smoke: `/root/arb_paused` present, `/root/arb_v2_ready` absent,
    `arb_main` not running. No unpause, no ready flag, no pause removal.
- V2 proxy-path parity pass (2026-05-03):
  - The failed signed smoke path was direct NYC egress; the smoke script called
    `client.post_order(...)` directly and bypassed the ARB CLOB proxy wrapper.
  - Active ARB/D2/W config contains `CLOB_PROXY_URL` with host suffix `181.139`.
    Strict non-secret config search found no active IPRoyal/Ireland proxy entry
    outside the probe scripts created during this pass.
  - Old V1 ARB and current V2 production order paths both wrap `post_order()` by
    swapping the SDK module-level CLOB HTTP client around the write call.
  - Patched `C:\tmp\arb_poly_executor.py`; deployed only
    `/root/kalshiedge_arb/poly_executor.py`.
  - Missing `CLOB_PROXY_URL` now fails closed for live BUY/SELL order submission
    instead of falling back to direct egress.
  - Added proxy-path tests to `C:\tmp\tests\test_arb_v2_proxy_funder.py`.
  - Isolated VPS tests passed:
    `python3 -m pytest -q tests/test_arb_v2_proxy_funder.py test_v2_collateral_address.py test_arb_v2_cutover_gate.py`
    -> `10 passed`.
  - Local, isolated VPS, and deployed `py_compile` checks passed.
  - Non-order egress/preflight:
    direct VPS egress = US / New Jersey / AS14061 DigitalOcean; configured proxy
    egress = DE / Hesse / Frankfurt am Main / AS14061 DigitalOcean.
  - CLOB public book returned HTTP 200 direct and through the configured proxy.
  - Runtime funder remained `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`;
    raw pUSD balance remained `50668374`; raw pUSD allowances and CTF approvals
    remained ready.
  - Direct SDK `get_open_orders()` returned `[]`; direct SDK `/balance-allowance`
    returned balance/allowances.
  - Through the exact V2 helper proxy path, `get_open_orders()` returned `[]`,
    but `/balance-allowance` returned proxy-side `502 Bad Gateway` twice.
  - Safety remained: `/root/arb_paused` present, `/root/arb_v2_ready` absent,
    `arb_main` not running, dashboard ARB start returned `409`.
  - No signed smoke was run in this pass because proxy preflight did not fully
    reconcile and the active configured proxy is Frankfurt/DE, not IPRoyal/Ireland.
- Read the handoff and current V2 gate record.
- Rechecked local and deployed ARB code:
  - `C:\tmp\arb_main.py` and VPS `/root/kalshiedge_arb/arb_main.py` use pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`.
  - `C:\tmp\arb_poly_executor.py` and VPS `/root/kalshiedge_arb/poly_executor.py` use pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`.
  - No stale `0x8ca194...` collateral constant appears in active ARB V2 order/collateral paths; that old token resolves as `jCAD`.
- Rechecked safe-off state:
  - `/root/arb_paused` exists.
  - `/root/arb_v2_ready` is absent.
  - `arb_main` is not running.
  - Dashboard `POST /api/bot/arb/start` returns `409 Conflict` with missing ready flag.
  - Direct auth-derived `get_open_orders()` returned list length `0`.
- Recorded non-secret runtime config:
  - host `https://clob.polymarket.com`
  - chain_id `137`
  - signer `0xcECEEb57accF34ED2e21D25d6C2037F6109751f0`
  - configured funder `0xaeA2bb57baF02E5148C478a7d7D5dBF851ceA7Ae`
  - resolved proxy-funder `0xaeA2bb57baF02E5148C478a7d7D5dBF851ceA7Ae`
  - signature_type `1`
  - exchange_v2 `0xE111180000d2663C0091e4f400237545B87B996B`
  - pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`
  - CTF `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
  - neg-risk exchange `0xe2222d279d744050d28e00520010520000310F59`
  - neg-risk adapter `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`
- Forced CLOB balance/allowance refresh:
  - `update_balance_allowance(asset_type=COLLATERAL)` returned `""`.
  - Direct runtime `get_balance_allowance(asset_type=COLLATERAL)` still returned collateral balance raw `50668374` = `50.668374` pUSD and max-uint allowances to exchange_v2, neg-risk adapter, and neg-risk exchange_v2.
  - Configured Frankfurt proxy balance/allowance path failed on the latest run with `PolyApiException` / proxy `502 Bad Gateway`.
- Built raw Polygon address matrix for signer, configured funder, and resolved proxy-funder:
  - signer pUSD balance `0`, pUSD allowances `0`, CTF approvals `false`.
  - configured funder and resolved proxy-funder are the same address; pUSD balance `0`, pUSD allowances `0`, CTF approvals `false`.
- Recent event scan:
  - Polygon publicnode blocks `86248622` through `86318622` found no pUSD Transfer, pUSD Approval, or CTF ApprovalForAll events involving signer/configured/resolved addresses.
  - Wider 300k-block log scan was attempted but publicnode pruned that older history.
  - No separate Polymarket UI wallet/deposit wallet is discoverable from the bot env or SDK without UI/session evidence.
- SDK behavior inspected:
  - `get_balance_allowance()` and `update_balance_allowance()` send L2-auth headers plus `signature_type`, `asset_type`, and optional `token_id`.
  - No funder/account parameter is sent on the balance/allowance request.
  - L2 headers carry `POLY_ADDRESS = signer.address()` and API-key auth.
  - The `funder` constructor argument is stored in the order builder, but is not transmitted by the balance/allowance endpoint call.
- Updated Claim 2 and Claim 11 evidence in:
  - `C:\tmp\decision_gates\2026-04-20_migration-gate_polymarket-v2.md`

### 2026-05-04 read-only proxy reconciliation pass (Claude)

- Searched for any IPRoyal/Ireland config in scope: `C:\tmp\**`,
  `C:\Users\gabri\**` (excluding AppData/conversation logs), PowerShell
  `ConsoleHost_history.txt`, `C:\Users\gabri\Documents\Codex\**`, and the
  deployed `/root/kalshiedge_arb/.env` (variable names only, no values).
  No active IPRoyal config found anywhere; all hits were historical
  documentation noting the 2026-04-13 IPRoyal removal. No `HTTP_PROXY`,
  `HTTPS_PROXY`, `POLY_PROXY`, or `POLYMARKET_PROXY_URL` exist in the
  deployed `.env`. Only `CLOB_PROXY_URL` is set, host suffix `181.139`
  (Frankfurt droplet). Local mirror `C:\tmp\github\bot\arb\.env` exists
  with real secrets and is not gitignored — flagged.
- Traced loader chain for `CLOB_PROXY_URL`: dashboard
  (`/root/dashboard.py:57`) spawns
  `cd /root/kalshiedge_arb && set -a && source .env && set +a && nohup python3 arb_main.py …`;
  `arb_main.py:15-16` calls `load_dotenv()`; `poly_executor.py` reads
  `os.getenv("CLOB_PROXY_URL", "")`. No systemd unit, no shell rc, no
  wrapper. Watchdog does not auto-revive ARB.
- Probe A (proxy swap installed AFTER `ClobClient(...)` and
  `create_or_derive_api_key()`, matching deployed control flow at
  `poly_executor.py:1111-1113`): SDK logged
  `request error status=403 url=https://clob.polymarket.com/auth/api-key`
  with Cloudflare interstitial citing client_ip `68.183.55.155` (NYC)
  and CF-RAY `9f63774f8b806da2`. Subsequent
  `client.get_balance_allowance(asset_type=COLLATERAL)` raised
  `PolyApiException[status_code=None, error_message=Request exception!]`
  and never reached the wire. `client.get_open_orders()` through the
  same swapped helper returned `[]` HTTP 200 from CF-RAY
  `9f637761fecc34ac-FRA`.
- Probe B (identical script, swap moved BEFORE `ClobClient`
  construction): all-clean. `GET /auth/derive-api-key` 200,
  `GET /balance-allowance?signature_type=1&asset_type=COLLATERAL` 200
  with balance raw `50668374` and max-uint allowances for exchange_v2,
  neg-risk adapter, and neg-risk exchange_v2,
  `GET /data/orders?next_cursor=MA%3D%3D` 200 (0 open orders). All
  Server cloudflare, all CF-RAY `...-FRA`. Runtime funder
  `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`.
- Conclusion: the proxy `/balance-allowance` 502 reported in the
  2026-05-03 pass is a deterministic consequence of bootstrap call
  ordering in the deployed executor, not an upstream Polymarket failure
  and not a Frankfurt-egress block. Frankfurt egress is fully functional
  for all V2 endpoints exercised when the helper-module HTTP client
  swap is installed before bootstrap.
- Static analysis cross-check on
  `/root/kalshiedge_arb/poly_executor.py`: `ClobClient(...)` and
  `create_or_derive_api_key()` co-located at lines 1111-1113, neither
  inside `_run_with_clob_proxy` (defined line 784). A second
  proxy-bypass exists at `self._http = httpx.AsyncClient(timeout=20.0)`
  (line 362), used in six call sites (lines 749 [`/fee-rate`], 838,
  932, 1948, 2623, 2691); all six egress direct from NYC and are not
  covered by `_run_with_clob_proxy`. Contract-address matrix in
  deployed code aligns exactly with `/balance-allowance` response:
  `POLY_CLOB_V2_EXCHANGE` (152), `POLY_NEG_RISK_ADAPTER` (158),
  `POLY_NEG_RISK_EXCHANGE_V2` (157), `POLY_PUSD` (155). Side-flag:
  line 498 declares `usdc = "0xC011…"` (the pUSD contract); name is
  stale, functionally correct.
- Fee-rate endpoint reachability through proxy:
  `curl -x http://138.197.181.139:8083 https://clob.polymarket.com/fee-rate`
  (no params) returned HTTP 400 from `Server: cloudflare`,
  CF-RAY `9f639119db1fd274-FRA`. Healthy 400 (missing `token_id`);
  endpoint is reachable, not WAF-blocked.
- Updated decision gate
  `C:\tmp\decision_gates\2026-04-20_migration-gate_polymarket-v2.md`
  with evidence appendices to Claim 2, Claim 5 (filling previously
  empty `resolution_note`), and Claim 9, plus rewritten Adjudicator
  Verdict `reason` and `next_action`. No claim was flipped from
  `open` to `resolved_by_evidence`. Gate file grew 696 → 854 lines;
  `### Claim 1`-`### Claim 11` headers all present in order;
  code-fence count 28 (balanced).
- Mirrored Claude Code command/agent scaffold from user-level
  `C:\Users\gabri\.claude\` into project `C:\tmp\.claude\` (7 commands,
  5 agents) and prepended a Coordination Layer section to
  `C:\tmp\CLAUDE.md` declaring the project copy authoritative.
- Hard rails respected: no `.env` edit, no code deploy, no signed
  order, no ARB unpause, no `/root/arb_v2_ready` creation, no
  `*_paused` flag removal, no secret values printed
  (api_key/secret/passphrase appeared in raw probe captures and were
  redacted before being recorded here or in the gate).

---

## Current Blocker

Signed submit/cancel smoke has not passed, and the bootstrap-proxy fix has not yet
shipped.

The prior CLOB/raw collateral mismatch is reconciled for the runtime funder after
the resolver patch. The 2026-05-03 proxy `/balance-allowance` 502 is now traced
(2026-05-04 read-only probes) to bootstrap call ordering at
`/root/kalshiedge_arb/poly_executor.py` lines 1111-1113, which run outside
`_run_with_clob_proxy` and egress direct from NYC where Cloudflare WAF blocks
`/auth/api-key` (CF-RAY `9f63774f8b806da2`, client_ip `68.183.55.155`). When the
proxy is installed before bootstrap, all three V2 endpoints exercised
(`/auth/derive-api-key`, `/balance-allowance`, `/data/orders`) return `200` from
Cloudflare-FRA. There is also a second proxy-bypass at the bot's own
`self._http = httpx.AsyncClient(timeout=20.0)` (line 362) used in six call sites
that egress direct from NYC; a code-only wrap of the bootstrap site would not
cover those.

Claim 2 remains open because signed-order acceptance has not been verified.
Claim 9 preflight side is now covered. Claim 5 has a partial update (endpoint
identified at `clob.polymarket.com/fee-rate`, reachability through proxy
confirmed) but still needs a numeric `base_fee` capture for a known crypto
`token_id`. No active IPRoyal/Ireland config exists in the bot codebase or the
deployed `.env`; IPRoyal is not required because the Frankfurt egress is fully
functional for all V2 endpoints when the bootstrap ordering is correct.

Gate state: `blocked`.

---

## Next Step

Ship the bootstrap-proxy fix. Recommended primary fix (fix ii in the gate
appendix): set `HTTP_PROXY=$CLOB_PROXY_URL` and `HTTPS_PROXY=$CLOB_PROXY_URL` in
`/root/kalshiedge_arb/.env`. httpx defaults to `trust_env=True`, so every Client
and AsyncClient in the process inherits the proxy automatically — covering both
the SDK bootstrap path and the six `self._http` call sites in one config line,
zero code change. Optional secondary (fix i): wrap `poly_executor.py` lines
1111-1113 inside `_run_with_clob_proxy`.

This is a runtime trading-config change to a deployed `.env`, which falls under
the gate's hard rails. Requires explicit operator authorization before
execution. After the fix is deployed, re-run non-order preflight against the
deployed code path (no monkey-patching) and confirm `/auth/derive-api-key`,
`/balance-allowance`, and `get_open_orders()` all return `200` through the
proxy. Then, only with separate explicit authorization, attempt a signed
submit/cancel smoke through the proxy to close Claim 2.

Throughout, keep:

- `/root/arb_paused` present
- `/root/arb_v2_ready` absent
- ARB not otherwise unpaused
- no ready flag creation
- no removal of pause flag

Side cleanups available in parallel: continue docs work for Claims 5, 6, 7, 8;
add a `.gitignore` to `C:\tmp` so `C:\tmp\github\bot\arb\.env` cannot be staged
with secrets; push the gate edit and `.claude/` scaffold to the GitHub remote
so Codex sees them.

---

## Files Changed

Local:

- `C:\tmp\arb_poly_executor.py`
- `C:\tmp\tests\test_arb_v2_proxy_funder.py`
- `C:\tmp\vps_v2_account_identity_probe.py`
- `C:\tmp\vps_v2_non_order_readiness_probe.py`
- `C:\tmp\vps_v2_signed_submit_cancel_smoke.py`
- `C:\tmp\decision_gates\2026-04-20_migration-gate_polymarket-v2.md`
- `C:\tmp\session_state.md`
- `C:\tmp\vps_v2_collateral_contradiction_probe.py`
- `C:\tmp\vps_v2_recent_events_probe.py`
- `C:\tmp\vps_proxy_env_probe.py`
- `C:\tmp\vps_proxy_config_search.py`
- `C:\tmp\vps_iproyal_strict_search.py`
- `C:\tmp\vps_v2_proxy_preflight_readiness.py`

Local (added 2026-05-04 by Claude):

- `C:\tmp\CLAUDE.md` (Coordination Layer section prepended; rest unchanged)
- `C:\tmp\.claude\commands\handoff-start.md`
- `C:\tmp\.claude\commands\handoff-end.md`
- `C:\tmp\.claude\commands\v2-gate.md`
- `C:\tmp\.claude\commands\safeoff-check.md`
- `C:\tmp\.claude\commands\strategy-gate.md`
- `C:\tmp\.claude\commands\weather-review.md`
- `C:\tmp\.claude\commands\postmortem.md`
- `C:\tmp\.claude\agents\polymarket-v2-gate.md`
- `C:\tmp\.claude\agents\deploy-smoke-and-safeoff.md`
- `C:\tmp\.claude\agents\strategy-suite-gate.md`
- `C:\tmp\.claude\agents\weather-research-runner.md`
- `C:\tmp\.claude\agents\trading-postmortem-writer.md`

User-level (Claude Code scaffold; project copy is authoritative, these are a
convenience fallback for sessions whose cwd is not `C:\tmp`):

- `C:\Users\gabri\CLAUDE.md` (Coordination Layer section prepended)
- `C:\Users\gabri\.claude\commands\` (same seven files as project copy)
- `C:\Users\gabri\.claude\agents\` (same five files as project copy)

Remote probe scripts:

- `/tmp/vps_v2_account_identity_probe.py`
- `/tmp/vps_v2_non_order_readiness_probe.py`
- `/tmp/vps_v2_signed_submit_cancel_smoke.py`
- `/tmp/vps_v2_collateral_contradiction_probe.py`
- `/tmp/vps_v2_recent_events_probe.py`
- `/tmp/vps_proxy_env_probe.py`
- `/tmp/vps_proxy_config_search.py`
- `/tmp/vps_iproyal_strict_search.py`
- `/tmp/vps_v2_proxy_preflight_readiness.py`

Remote ARB code:

- `/root/kalshiedge_arb/poly_executor.py`

---

## Safety State

- `/root/arb_paused`: present
- `/root/arb_v2_ready`: absent
- `arb_main`: not running
- Dashboard ARB start: blocked with `409 Conflict`
- `get_open_orders()`: list length `0`
- Signed-order smoke: direct submit rejected with CLOB `403` geoblock before acceptance;
  proxy-routed smoke not run after proxy preflight mismatch/502
- Live order status: no order placed, no unpause, no ready flag created
