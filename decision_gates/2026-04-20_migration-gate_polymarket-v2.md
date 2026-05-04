# Gate Record: Polymarket CLOB V2 Migration

**Gate ID:** `2026-04-20_migration-gate_polymarket-v2`
**Gate Type:** `migration_gate`
**Date Opened:** `2026-04-20`
**Hard Deadline:** `2026-04-28 ~11:00 UTC` - V1 clients stop working, all open orders wiped
**Current Adjudicator State:** `blocked` - Claims 2-9 and 11 remain open
**Governing Framework:** `decision_gate_system_spec.md`

---

## Proposal Summary

Migrate the ARB bot from `py-clob-client` (V1) to `py-clob-client-v2`. This is a mandatory
cutover - Polymarket's CLOB V2 goes live April 28. V1 clients stop functioning at that point.

Primary bot affected: ARB (`/root/kalshiedge_arb/`). W and D2 are currently paused but share
the same executor pattern and will need the same migration applied.

Key changes required:
- SDK import path rename
- `order_to_json` signature update (`api_key` removed in V2)
- Verify `_execute_live_sell()` against the documented V2 SELL-path API; official V2 docs still show market-order support, so the original removal assumption was stale
- Replace hardcoded `TAKER_FEE=0.01` with live per-market fee lookup
- Update EV formula in `arb_main.py` to use V2 fee semantics
- Replace USDC.e address with pUSD address in auto-claimer
- Verify drain timestamp is correct for 11:00 UTC cutover
- Define explicit safe-off behavior across cutover, downtime, smoke validation, and fallback

---

## Active Roles

| Role | Active | Reason |
|------|--------|--------|
| Proposer | Yes | Frames migration plan and known risks |
| Financial Code Skeptic | Yes | Fee arithmetic, order signing, pUSD, settlement logic |
| Execution Skeptic | Yes | SDK swap behavior, proxy compatibility, drain timing, restart behavior |
| Quant Skeptic | No | Migration - no statistical claims to evaluate |
| Risk Skeptic | No | Not a sizing or exposure change |
| Adjudicator | Yes | Policy engine |

---

## Required Inputs

- `v2_handoff.md` - migration spec with exact line numbers (`C:\tmp\v2_handoff.md`)
- `arb_main.py` - EV formula, auto-claimer, drain logic (`C:\tmp\arb_main.py`)
- `arb_poly_executor.py` - imports, order signing, fee calc, sell path (`C:\tmp\arb_poly_executor.py`)
- `py-clob-client-v2` SDK source - verified after `pip3 install py-clob-client-v2 --break-system-packages` on VPS
- V2 API / migration docs - official Polymarket docs for fees, migration, and redemption
- Current `.env` - full parameter set (see `v2_handoff.md` Environment section)
- `watchdog.py` or the actual ARB restart controller used on VPS - restart and auto-reenable behavior
- ARB process-manager config - systemd unit, supervisor config, cron entry, or launcher script that can restart the bot
- Telegram alert path/config - script, module, env vars, or notifier path used for cutover failure alerts

---

## Grounding Verification

All roles must consume and cite the required artifacts before producing claims.
The Adjudicator may not render `paper_only`, `min_size_only`, or `approved` if any
critical claim lacks grounding in a listed artifact.

| Artifact | Type | Version / Timestamp | Required For | Consumed | Cited In Claims | Verified By | Verified On | Notes |
|----------|------|---------------------|--------------|----------|-----------------|-------------|-------------|-------|
| `v2_handoff.md` | migration spec | `2026-04-19` | Financial Code Skeptic, Execution Skeptic | - | - | - | - | Line numbers for V1 break points already documented |
| `arb_main.py` | source file | `current VPS HEAD` | Financial Code Skeptic, Execution Skeptic | - | - | - | - | EV formula, auto-claimer, drain gate, cutover pause logic |
| `arb_poly_executor.py` | source file | `current VPS HEAD` | Financial Code Skeptic | - | - | - | - | Imports, order signing, fee path, live sell path |
| `py-clob-client-v2` SDK source | installed package | `post-install on VPS` | Financial Code Skeptic, Execution Skeptic | - | - | - | - | Must be installed before Claims 1-4 can close |
| `V2 API / migration docs` | external docs | `Polymarket official` | Financial Code Skeptic, Execution Skeptic | - | - | - | - | Required for Claims 5, 7, 8, and 10 |
| `.env` | config | `current VPS` | Proposer, Execution Skeptic | - | - | - | - | Required to verify live constructor/auth assumptions |
| `watchdog.py` or actual ARB restart controller | source file or runtime controller | `current VPS HEAD` | Execution Skeptic | - | - | - | - | Required for Claim 11; must prove no unsafe auto-reenable |
| `ARB process-manager config` | runtime config | `current VPS` | Execution Skeptic | - | - | - | - | systemd, supervisor, cron, tmux launcher, or equivalent restart path |
| `Telegram alert path/config` | source file or env config | `current VPS` | Execution Skeptic | - | - | - | - | Required to prove cutover failure alerts can actually fire |

*(Update all columns as gate session runs. Adjudicator is blocked above `insufficient_evidence` until `Verified By` and `Verified On` are populated for all critical artifacts.)*

---

## Claim Schema

```text
claim:                 specific assertion about what could break
why_it_matters:        dollar cost or failure mode if wrong
falsifiable_threshold: exact condition that resolves this claim
evidence_needed:       concrete artifact or test - never abstract
owner:                 Gabriel | named script | named VPS command
status:                open | resolved_by_evidence | accepted_risk | invalidated | superseded
resolved_by:           person or artifact that closed this claim
resolved_on:           date resolved
resolution_note:       what was found
```

---

## Claims

### Claim 1 - SDK Import Path

```text
claim:                 `py_clob_client_v2` is the correct top-level module name after
                       installing py-clob-client-v2; all occurrences of `py_clob_client`
                       in poly_executor.py are replaced with `py_clob_client_v2`
why_it_matters:        All imports break silently at startup if the module name is wrong;
                       bot crashes before placing any order
falsifiable_threshold: `python3 -c "from py_clob_client_v2 import ClobClient"` exits 0
                       on VPS after install
evidence_needed:       Install py-clob-client-v2 on VPS; run import test; inspect module name
owner:                 Gabriel
status:                resolved_by_evidence
resolved_by:           VPS smoke 2026-05-02
resolved_on:           2026-05-02
resolution_note:       `python3 -c "from py_clob_client_v2 import ClobClient"` exited 0
                       on VPS and printed `<class 'py_clob_client_v2.client.ClobClient>`.
                       Deployed `/root/kalshiedge_arb/poly_executor.py` imports
                       `py_clob_client_v2`; deployed ARB files pass `python3 -m py_compile`.
```

---

### Claim 2 - V2 Proxy-Wallet Auth and Order Signing

```text
claim:                 V2 ClobClient accepts `signature_type=1` and `funder=PROXY_WALLET_ADDRESS`;
                       API credentials derive correctly; and a signed test order is accepted
                       by the V2 endpoint - proving effective proxy-wallet auth, not just
                       client construction
why_it_matters:        A bot that instantiates cleanly but fails at the auth layer on the
                       first live order loses the entry window silently; fills never happen
falsifiable_threshold: All three pass:
                       1. ClobClient instantiates with current .env args without error
                       2. API creds derive successfully - confirmed by a successful call to
                          an auth-required endpoint such as `client.get_orders()` or
                          `client.get_balance()`; public reads like `get_order_book()` do
                          not satisfy this step
                       3. A signed test order is accepted by the V2 CLOB endpoint
                          (any rejection or auth error on step 3 = claim open)
evidence_needed:       VPS smoke test covering all three steps; results logged explicitly
owner:                 Gabriel
status:                open
resolved_by:
resolved_on:
resolution_note:       2026-05-02 partial smoke: VPS non-order auth smoke constructed
                       `ClobClient`, derived API creds, resolved funder
                       `0xaeA2bb57baF02E5148C478a7d7D5dBF851ceA7Ae`, and
                       `client.get_open_orders()` returned an empty list. The SDK logged
                       a Cloudflare 403 while attempting create-api-key, then fell back
                       successfully to derived credentials.

                       2026-05-02 collateral readiness probe resolved the runtime proxy
                       funder to `0xaeA2bb57baF02E5148C478a7d7D5dBF851ceA7Ae`
                       (same as configured `POLYMARKET_ADDRESS`). V2 SDK config and
                       exchange contract reads agree that exchange_v2 is
                       `0xE111180000d2663C0091e4f400237545B87B996B`, collateral is
                       pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, and CTF is
                       `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`. The prior staged
                       `0x8ca194A3b22077359b5732DE53373D4afC11DeE3` token resolves as
                       `jCAD`, not pUSD; `arb_main.py` and `poly_executor.py` were patched
                       and redeployed to use `0xC011...`.

                       CLOB `/balance-allowance` via the staged client/proxy reports
                       collateral balance raw `50668374` (50.668374 pUSD) and max-uint
                       allowances for exchange_v2, neg-risk adapter, and neg-risk
                       exchange_v2. Raw Polygon RPC checks for the same resolved funder
                       report pUSD `balanceOf=0`, pUSD `allowance(funder, exchange_v2)=0`,
                       and ConditionalTokens `isApprovedForAll(funder, exchange_v2)=false`
                       (also false for neg-risk exchange_v2). Signer wallet checks also
                       show zero. Because CLOB API readiness and raw on-chain readiness
                       disagree, signed-order smoke was not attempted. Claim remains open:
                       direct and Frankfurt signed submit/cancel have not passed, and
                       `get_open_orders()` currently confirms zero open orders.

                       2026-05-02 contradiction root-cause pass:
                       local and deployed `/root/kalshiedge_arb/arb_main.py` and
                       `/root/kalshiedge_arb/poly_executor.py` were rechecked. Active ARB
                       V2 collateral/order paths use pUSD
                       `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`; no stale
                       `0x8ca194...` collateral constant appears in those active files
                       (the old token still resolves as `jCAD`, not pUSD).

                       Non-secret runtime config recorded from the staged VPS client:
                       host `https://clob.polymarket.com`, chain_id `137`, signer
                       `0xcECEEb57accF34ED2e21D25d6C2037F6109751f0`, configured funder
                       `0xaeA2bb57baF02E5148C478a7d7D5dBF851ceA7Ae`, resolved proxy
                       funder `0xaeA2bb57baF02E5148C478a7d7D5dBF851ceA7Ae`,
                       signature_type `1`, exchange_v2
                       `0xE111180000d2663C0091e4f400237545B87B996B`, pUSD
                       `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, CTF
                       `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`, neg-risk exchange
                       `0xe2222d279d744050d28e00520010520000310F59`, and neg-risk
                       adapter `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`.

                       Force-refresh evidence: direct runtime `ClobClient` call to
                       `update_balance_allowance(asset_type=COLLATERAL)` returned `""`;
                       immediate `get_balance_allowance(asset_type=COLLATERAL)` still
                       returned balance raw `50668374` and max-uint allowances for
                       exchange_v2, neg-risk adapter, and neg-risk exchange_v2. The same
                       call through the configured Frankfurt proxy failed with
                       `PolyApiException[status_code=None, error_message=Request exception!]`
                       and a proxy-side `502 Bad Gateway`, so proxy readiness is not
                       proven.

                       Address matrix from raw Polygon RPC:
                       signer/EOA `0xcECE...` has pUSD balance `0`, pUSD allowances `0`
                       to exchange_v2, neg-risk adapter, and neg-risk exchange_v2, and
                       CTF approvals `false` to all three operators. Configured funder
                       and resolved proxy-funder are the same address `0xaeA2...`; that
                       address also has pUSD balance `0`, pUSD allowances `0` to all three
                       operators, and CTF approvals `false` to all three operators.

                       Recent public-chain event scan: using Polygon publicnode over
                       blocks `86248622` through `86318622` found no pUSD Transfer, pUSD
                       Approval, or CTF ApprovalForAll events involving the signer,
                       configured funder, or resolved proxy-funder. A wider 300k-block
                       scan was attempted but publicnode pruned that older log history.
                       No separate Polymarket UI wallet/deposit wallet is discoverable
                       from the bot env or SDK without UI/session evidence.

                       SDK source inspection: `get_balance_allowance()` and
                       `update_balance_allowance()` send L2-auth headers plus
                       `signature_type`, `asset_type`, and optional `token_id`; no
                       funder/account parameter is sent. L2 headers carry
                       `POLY_ADDRESS = signer.address()` and API-key auth. The `funder`
                       constructor argument is stored in the order builder, but the
                       balance/allowance endpoint call itself does not transmit it.
                       Therefore the exact account behind `/balance-allowance` is inferred
                       server-side from signer/API-key/signature_type, not explicitly from
                       the resolved runtime funder. Because raw on-chain state for both
                       signer and resolved funder remains zero/false while CLOB reports
                       balance/max allowances, collateral readiness is not reconciled.
                       Signed-order smoke remains blocked.

                       2026-05-03 resolver root cause and patch: the deployed ARB
                       resolver had been calling `getPolyProxyWalletAddress(address)` on
                       V2 exchange `0xE111180000d2663C0091e4f400237545B87B996B`.
                       That call reverts, so the old code silently fell back to
                       configured `POLYMARKET_ADDRESS`
                       `0xaeA2bb57baF02E5148C478a7d7D5dBF851ceA7Ae`, which has zero
                       pUSD and no V2 approvals. The working helper path is the old CTF
                       exchange `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`, which
                       resolves signer `0xcECEEb57accF34ED2e21D25d6C2037F6109751f0`
                       to actual proxy wallet
                       `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`.

                       Local `C:\tmp\arb_poly_executor.py` and deployed
                       `/root/kalshiedge_arb/poly_executor.py` were patched so V2 orders
                       still use the V2 SDK/exchange path while proxy-wallet resolution
                       uses the old helper. The resolver now fails closed: no silent
                       fallback to `POLYMARKET_ADDRESS`; unresolved signer/zero/EOA
                       results raise; live initialization requires raw Polygon pUSD
                       balance, pUSD allowances, and CTF approvals for exchange_v2,
                       neg-risk adapter, and neg-risk exchange_v2 before CLOB client
                       initialization.

                       Tests added in `C:\tmp\tests\test_arb_v2_proxy_funder.py` cover:
                       V2/helper revert does not fall back to env funder; old-helper
                       resolution returns the funded proxy wallet; and unfunded proxy
                       blocks readiness. Isolated VPS test run:
                       `python3 -m pytest -q tests/test_arb_v2_proxy_funder.py
                       tests/test_v2_collateral_address.py
                       tests/test_arb_v2_cutover_gate.py` returned `8 passed`.
                       Local and VPS `py_compile` checks passed, and only ARB
                       `/root/kalshiedge_arb/poly_executor.py` was redeployed.

                       2026-05-03 non-order readiness after deploy reconciled:
                       runtime resolver returns
                       `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`; raw Polygon pUSD
                       balance raw `50668374`, max pUSD allowances to exchange_v2,
                       neg-risk adapter, and neg-risk exchange_v2, and CTF approvals
                       `true` for all three operators. CLOB `/balance-allowance` for
                       the same runtime funder returns balance raw `50668374` and the
                       same max allowances. `get_open_orders()` returned list length
                       `0`. No signed-order smoke was run and no order was placed.
                       Claim remains open until signed submit/cancel smoke is explicitly
                       authorized and passes.

                       2026-05-03 signed submit/cancel smoke attempt:
                       preconditions passed before submit: `/root/arb_paused` present,
                       `/root/arb_v2_ready` absent, `arb_main` not running, dashboard
                       ARB start returned `409 Conflict`, `get_open_orders()` returned
                       `[]`, runtime funder resolved to
                       `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`, raw pUSD balance
                       raw `50668374`, pUSD allowances max-uint, CTF approvals true,
                       and CLOB `/balance-allowance` balance raw `50668374`.
                       Smoke order parameters were one post-only GTC BUY limit on token
                       `78433024518676680431174478322854148606578065650008220678402966840627347604025`
                       at price `0.01`, size `5.0`; candidate order book best ask was
                       `0.16`, min order size `5.0`, tick size `0.01`.
                       The direct CLOB submit was rejected before acceptance with
                       `PolyApiException status_code=403` / `Trading restricted in your
                       region`. No `order_id` was returned, so there was no accepted
                       order to cancel. Final `get_open_orders()` returned `[]`;
                       `test_orders_remaining=0`; `/root/arb_paused` remained present,
                       `/root/arb_v2_ready` remained absent, and `arb_main` was not
                       running. Claim remains open because signed order acceptance did
                       not pass.

                       2026-05-03 V2 proxy-path parity pass: the failed signed smoke
                       was confirmed to have used direct NYC VPS egress because the
                       smoke script called `client.post_order(...)` directly and did not
                       use the ARB executor's CLOB proxy wrapper. Active ARB/D2/W config
                       contains `CLOB_PROXY_URL` with host suffix `181.139`; a strict
                       config search found no active IPRoyal/Ireland proxy entry outside
                       the non-secret probe scripts created during this pass. The old
                       V1 ARB order path and current V2 production order path both wrap
                       `post_order()` by swapping the SDK module-level CLOB HTTP client
                       around the write call. Local `C:\tmp\arb_poly_executor.py` and
                       deployed `/root/kalshiedge_arb/poly_executor.py` were patched so
                       missing `CLOB_PROXY_URL` now fails closed for live BUY/SELL order
                       submissions instead of falling back to direct egress.

                       Tests added/adjusted in
                       `C:\tmp\tests\test_arb_v2_proxy_funder.py` prove the V2 order
                       helper installs the configured proxy client and fails closed when
                       the proxy is absent. Isolated VPS run:
                       `python3 -m pytest -q tests/test_arb_v2_proxy_funder.py
                       test_v2_collateral_address.py test_arb_v2_cutover_gate.py`
                       returned `10 passed`; isolated and deployed `py_compile` checks
                       passed. Only `/root/kalshiedge_arb/poly_executor.py` was
                       redeployed.

                       Non-order egress/preflight after deploy: direct VPS egress
                       reported country `US`, region `New Jersey`, ASN
                       `AS14061 DigitalOcean`; configured proxy egress reported country
                       `DE`, region `Hesse`, city `Frankfurt am Main`, ASN
                       `AS14061 DigitalOcean`. Public CLOB book preflight returned
                       HTTP 200 direct and HTTP 200 through the configured proxy.
                       Runtime funder remained
                       `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`; raw pUSD balance
                       remained `50668374`; raw pUSD allowances and CTF approvals
                       remained ready. Direct SDK `get_open_orders()` returned list
                       length `0`; direct SDK `/balance-allowance` returned balance and
                       allowances. Through the exact V2 helper proxy path,
                       `get_open_orders()` returned list length `0`, but
                       `/balance-allowance` returned proxy-side `502 Bad Gateway` on two
                       consecutive runs. Safety checks remained: `/root/arb_paused`
                       present, `/root/arb_v2_ready` absent, `arb_main` not running,
                       dashboard ARB start returned `409`. No signed smoke was run in
                       this pass because proxy preflight did not fully reconcile and the
                       active configured proxy is Frankfurt/DE, not IPRoyal/Ireland.

                       2026-05-04 proxy `/balance-allowance` 502 root cause and fix
                       path identified (read-only probes, no signed order, no `.env`
                       or code change deployed). Probe A reproduced the documented
                       failure mode: helper-module HTTP client swap installed AFTER
                       `ClobClient(...)` construction and `create_or_derive_api_key()`,
                       matching the deployed control flow at
                       `/root/kalshiedge_arb/poly_executor.py` lines 1111-1113. SDK
                       logged
                       `request error status=403 url=https://clob.polymarket.com/auth/api-key`
                       with a Cloudflare interstitial citing client_ip
                       `68.183.55.155` (NYC droplet, AS14061 DigitalOcean) and
                       CF-RAY `9f63774f8b806da2`, confirming the bootstrap request
                       egressed direct from NYC, not through the configured
                       Frankfurt proxy. Subsequent
                       `client.get_balance_allowance(asset_type=COLLATERAL)` raised
                       `PolyApiException[status_code=None, error_message=Request exception!]`
                       and never appeared in the response-hook capture, indicating
                       the SDK aborted before the wire. `client.get_open_orders()`
                       through the same swapped helper returned `[]` with HTTP 200,
                       Server cloudflare, CF-RAY `9f637761fecc34ac-FRA`, so the
                       helper-module proxy path itself is healthy; the failure is
                       upstream-of-helper.

                       Probe B (identical script, swap moved BEFORE `ClobClient`
                       construction) returned all-clean. Captures, all egressing
                       CF-RAY `...-FRA` (Frankfurt POP), all Server cloudflare:
                       `GET /auth/derive-api-key` status `200` (creds populated;
                       body redacted - contains api_key/secret/passphrase);
                       `GET /balance-allowance?signature_type=1&asset_type=COLLATERAL`
                       status `200`, body balance raw `50668374` (50.668374 pUSD)
                       and max-uint allowances for exchange_v2
                       `0xE111180000d2663C0091e4f400237545B87B996B`, neg-risk
                       adapter `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`, and
                       neg-risk exchange_v2
                       `0xe2222d279d744050d28e00520010520000310F59`;
                       `GET /data/orders?next_cursor=MA%3D%3D` status `200`
                       (0 open orders). Runtime funder confirmed
                       `0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF`. The CLOB
                       `/balance-allowance` 502 reported in the prior pass is
                       therefore not an upstream Polymarket-side failure and not a
                       Frankfurt-egress block; it is a deterministic consequence of
                       the bootstrap call ordering in the deployed executor.

                       Static analysis of
                       `/root/kalshiedge_arb/poly_executor.py`: `ClobClient(...)`
                       construction and `create_or_derive_api_key()` appear at a
                       single co-located block, lines 1111-1113. Neither call is
                       inside `_run_with_clob_proxy` (defined at line 784);
                       `_run_with_clob_proxy` is only used at order-placement call
                       sites later in the file. A second proxy-bypass exists at the
                       bot's own httpx layer:
                       `self._http = httpx.AsyncClient(timeout=20.0)` at line 362
                       has no proxy configured, used in six call sites (lines 749
                       [`/fee-rate`], 838, 932, 1948, 2623, 2691). All six egress
                       direct from NYC and are not covered by
                       `_run_with_clob_proxy`. A code-path fix at line 1111 alone
                       leaves these six sites bypassing the proxy. Contract-address
                       matrix in deployed code aligns exactly with the
                       `/balance-allowance` response: `POLY_CLOB_V2_EXCHANGE`
                       (line 152), `POLY_NEG_RISK_ADAPTER` (line 158),
                       `POLY_NEG_RISK_EXCHANGE_V2` (line 157), `POLY_PUSD`
                       (line 155, `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`).
                       No V2 collateral mismatch remains. Side-flag: line 498
                       declares `usdc = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"`
                       inside `_get_live_wallet_usdc_balance()`; the hex is the
                       pUSD contract, not USDC. Functionally correct, name is
                       stale; cosmetic only.

                       Two fixes are gate-eligible. Fix (i): wrap
                       `poly_executor.py` lines 1111-1113 inside
                       `_run_with_clob_proxy`; smallest diff, only addresses SDK
                       bootstrap. Fix (ii): set `HTTP_PROXY=$CLOB_PROXY_URL` and
                       `HTTPS_PROXY=$CLOB_PROXY_URL` in
                       `/root/kalshiedge_arb/.env`; httpx defaults to
                       `trust_env=True`, so every Client and AsyncClient in the
                       process inherits the proxy automatically, covering (i) plus
                       all six `self._http` call sites with zero code change.
                       Recommend (ii) as primary because it is the more
                       comprehensive change with smaller blast radius; (i) becomes
                       nice-to-have for explicitness but is no longer load-bearing
                       once (ii) is in.

                       Claim 2 remains open: signed-order acceptance has still not
                       been verified. What this evidence closes is the proxy-side
                       preflight blocker called out in the 2026-05-03 adjudicator
                       `next_action`; signed submit/cancel through the proxy
                       remains gated on (a) authorization and (b) the
                       bootstrap-proxy fix shipping first. No `.env` edit, no code
                       deploy, no signed call was made in this pass.
```

---

### Claim 3 - `order_to_json` Signature Change

```text
claim:                 V2 removes `api_key` from `order_to_json`; the `builder` arg
                       is absent or accepts an empty string as default
why_it_matters:        L1993 in poly_executor.py passes api_key - call fails or signs
                       incorrectly; orders are rejected by the CLOB
falsifiable_threshold: V2 SDK source for `order_to_json` inspected; updated call compiles
                       and a test order is created without error
evidence_needed:       Read V2 SDK source after install; update call; verify in smoke test
owner:                 Gabriel
status:                open
resolved_by:
resolved_on:
resolution_note:       2026-05-02 runtime evidence corrected the staged assumption:
                       V2 SDK config and exchange_v2 `getCollateral()` identify pUSD as
                       `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`; the previously staged
                       `0x8ca194A3b22077359b5732DE53373D4afC11DeE3` token reports symbol
                       `jCAD`. ARB code was patched and redeployed to use `0xC011...`.
                       Claim left open until the required external source/on-chain
                       transaction cross-reference is recorded.
```

---

### Claim 4 - SELL Path V2 Compatibility

```text
claim:                 `_execute_live_sell()` uses a SELL-path API that is actually
                       supported by the current V2 Python client docs and submits a valid
                       FOK sell order without SDK or auth errors
why_it_matters:        Profit-lock sell path broken; winning positions cannot close;
                       capital locked until expiry; MAX_OPEN_POSITIONS cap fills up
falsifiable_threshold: Test sell order created via the documented V2 SELL path without
                       SDK error; order type confirmed as valid FOK SELL
evidence_needed:       Run sell path smoke test with `PAPER_TRADING=1` or equivalent
                       low-risk staging mode on VPS after migration
owner:                 Gabriel
status:                open
resolved_by:
resolved_on:
resolution_note:       2026-04-29 local doc review found the original claim text stale:
                       official V2 docs still document market-order support, so the
                       validation target is documented SELL-path compatibility, not a
                       forced rewrite away from `MarketOrderArgs`.
```

---

### Claim 5 - Live Fee Rate Endpoint

```text
claim:                 V2 exposes a per-market fee rate via a callable API endpoint;
                       0.072 is a safe and documented fallback for crypto markets
why_it_matters:        Hardcoded TAKER_FEE=0.01 in V1 is wrong for V2; incorrect fee
                       means cost_usd is mistracked and EV is overstated from the first trade
falsifiable_threshold: Fee rate endpoint identified in V2 SDK or docs and returning a
                       numeric value for a known crypto market token_id;
                       OR V2 docs explicitly state 0.072 as the crypto default
evidence_needed:       V2 API docs or SDK source for fee lookup; test call on VPS
owner:                 Gabriel
status:                open
resolved_by:
resolved_on:
resolution_note:       2026-05-04 V2 fee-rate endpoint identified and reachability
                       confirmed (read-only, no deploy). Deployed
                       `/root/kalshiedge_arb/poly_executor.py` line 749 calls
                       `GET https://clob.polymarket.com/fee-rate?token_id=<id>`,
                       parses `response.json()["base_fee"]` as integer bps, and
                       falls back to `DEFAULT_FEE_RATE_BPS=720` on any exception
                       (line 760). Endpoint reachability through the Frankfurt
                       proxy verified:
                       `curl -x http://138.197.181.139:8083 https://clob.polymarket.com/fee-rate`
                       (no params) returned HTTP `400 Bad Request`, Server
                       cloudflare, CF-RAY `9f639119db1fd274-FRA`, content-type
                       `application/json`. `400` is the expected response to a
                       missing required `token_id` param, confirming the endpoint
                       exists, accepts requests through the proxy, and is not
                       WAF-blocked. Claim remains open: a `token_id`-bearing call
                       returning a numeric `base_fee` for a known crypto market
                       still needs to be captured and recorded, and the value
                       cross-checked against the documented V2 crypto fee for
                       Claim 6 EV arithmetic.
```

**Note:** This claim must be `resolved_by_evidence`. `accepted_risk` is not allowed here.

---

### Claim 6 - EV Gate Validity Under V2 Fee Formula

```text
claim:                 Under the V2 fee formula and actual V2 fee accounting semantics,
                       trades at MIN_ASK_PRICE=0.75 still clear MIN_EV=0.10 after fees
why_it_matters:        If the EV gate no longer clears in the target ask band, the bot
                       enters negative-EV trades without any circuit breaker
falsifiable_threshold: V2 fee formula confirmed from official docs or SDK source;
                       calculation verified at ask=0.75 with confirmed feeRate;
                       result confirms EV > MIN_EV=0.10; MIN_EV adjusted in .env if needed
evidence_needed:       V2 fee formula and feeRate sourced from official docs or SDK source;
                       arithmetic verified against MIN_EV threshold
owner:                 Gabriel
status:                open
resolved_by:
resolved_on:
resolution_note:
```

---

### Claim 7 - pUSD Contract Address

```text
claim:                 The correct pUSD contract address on Polygon is confirmed from
                       official V2 docs or Polymarket source - not assumed from
                       USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
why_it_matters:        Auto-claimer calls wrong contract; redemptions fail silently;
                       won positions never claimed; MAX_OPEN_POSITIONS cap fills up
falsifiable_threshold: pUSD address sourced from official V2 docs or Polymarket GitHub
                       and cross-referenced against at least one known pUSD transaction
                       on Polygon
evidence_needed:       V2 migration docs from Polymarket; confirmed address before hardcoding
owner:                 Gabriel
status:                open
resolved_by:
resolved_on:
resolution_note:
```

**Note:** This claim must be `resolved_by_evidence`. `accepted_risk` is not allowed here.

---

### Claim 8 - `redeemPositions` ABI and pUSD Redemption Path

```text
claim:                 `redeemPositions(pUSD, bytes32(0), conditionId, [indexSet])` call
                       on ConditionalTokens (0x4D97DCd97eC945f40cF65F87097ACe5EA0476045)
                       works with pUSD in V2 and does not strand winning positions
why_it_matters:        Redemption failure strands capital, blocks slot reuse, and can make
                       live P&L look better than withdrawable P&L
falsifiable_threshold: Minimum acceptable evidence - one of the following, in priority order:
                       1. (preferred) Test `redeemPositions` call with pUSD on Polygon
                          succeeds without revert
                       2. (acceptable) ConditionalTokens ABI confirms the function signature
                          is unchanged, and at least one verified on-chain pUSD redemption
                          transaction proves pUSD is accepted in practice
                       3. (minimum) Official V2 migration docs explicitly state
                          ConditionalTokens ABI is unchanged and pUSD is a valid
                          collateral token, with source URL recorded in `resolution_note`
                       One of the three above must be satisfied. "If possible" is not a
                       valid closure path.
evidence_needed:       ConditionalTokens ABI, V2 migration docs, on-chain redemption tx
                       or test call result, and source URL recorded in `resolution_note`
owner:                 Gabriel
status:                open
resolved_by:
resolved_on:
resolution_note:
```

**Note:** This claim must be `resolved_by_evidence`. `accepted_risk` is not allowed here.

---

### Claim 9 - Frankfurt CLOB Proxy Compatibility

```text
claim:                 Frankfurt CLOB CONNECT proxy (138.197.181.139:8083) requires no
                       configuration changes for V2 - same host, port, and path passthrough
why_it_matters:        All order submissions route through this proxy; if V2 uses different
                       endpoint paths, every order fails
falsifiable_threshold: V2 CLOB host confirmed as `clob.polymarket.com` (same as V1);
                       test order submitted successfully through Frankfurt proxy
evidence_needed:       V2 SDK default host value; test order through proxy in smoke test
owner:                 Gabriel
status:                open
resolved_by:
resolved_on:
resolution_note:       2026-05-03 proxy-path parity checks found the active configured
                       CLOB proxy host suffix `181.139`; egress through that path reports
                       country `DE`, region `Hesse`, city `Frankfurt am Main`, ASN
                       `AS14061 DigitalOcean`. Public CLOB book returned HTTP 200 through
                       the proxy and SDK `get_open_orders()` returned `[]` through the
                       V2 helper proxy path. SDK `/balance-allowance` through the same
                       helper path returned proxy-side `502 Bad Gateway` twice, and no
                       signed submit/cancel through the proxy was attempted. Claim
                       remains open.

                       2026-05-04 proxy-side preflight reconciled (read-only, no
                       deploy). Frankfurt CLOB proxy `138.197.181.139:8083`
                       confirmed compatible with V2 endpoints when reached through
                       a proxied client. Three V2 endpoints exercised through the
                       helper-module proxy path with the swap installed BEFORE
                       `ClobClient` construction:
                       `GET /auth/derive-api-key` status `200`, Server cloudflare,
                       CF-RAY `9f638267dba1d345-FRA`;
                       `GET /balance-allowance?signature_type=1&asset_type=COLLATERAL`
                       status `200`, Server cloudflare, CF-RAY
                       `9f638268cd09d345-FRA`;
                       `GET /data/orders?next_cursor=MA%3D%3D` status `200`,
                       Server cloudflare, CF-RAY `9f6382698e3ad345-FRA`. The 502
                       reported on `/balance-allowance` in the 2026-05-03 pass was
                       caused by SDK bootstrap call ordering (see Claim 2 update
                       2026-05-04), not by proxy or upstream pathology. Public CLOB
                       `/markets` and direct
                       `curl -x http://138.197.181.139:8083` to
                       `/balance-allowance` (no auth) returned upstream `401`
                       cleanly via Cloudflare-FRA, confirming the CONNECT layer is
                       healthy for both auth and non-auth endpoints. Claim 9
                       remains open until a signed order is also confirmed accepted
                       through the same proxy path; the preflight side of this
                       claim is now covered.
```

---

### Claim 10 - Drain Logic Timestamp

```text
claim:                 CUTOVER_TS in v2_handoff.md (1745834400) is wrong on two axes -
                       wrong year (2025 not 2026) and wrong hour (10:00 not 11:00 UTC).
                       Correct value for the 2026-04-28 11:00 UTC cutover is 1777374000.
why_it_matters:        Year-off timestamp puts the bot in permanent drain mode the moment
                       it starts (1745838000 has been in the past since April 2025);
                       hour-off timestamp leaves the bot trading into the cutover window;
                       either way orders get wiped mid-trade
falsifiable_threshold: 2026-04-28 00:00 UTC = 1777334400; +11h = 1777374000;
                       correct CUTOVER_TS = 1777374000.
                       Prior wrong values, both rejected:
                         - 1745834400 = 2025-04-28 10:00 UTC (v2_handoff.md original)
                         - 1745838000 = 2025-04-28 11:00 UTC (earlier draft of this claim)
evidence_needed:       Epoch arithmetic verified via Python datetime; arb_main.py
                       patched with CUTOVER_TS = 1777374000, DRAIN_SECS = 3600 and
                       drain gates in main() scan loop and arb_one() entry-watch loop
owner:                 Gabriel
status:                resolved_by_evidence
resolved_by:           Claude session 2026-04-27 (arithmetic verified; code patched)
resolved_on:           2026-04-27
resolution_note:       datetime(2026,4,28,11,0,0,UTC).timestamp() = 1777374000 confirmed
                       via Python. Both prior candidates resolved to 2025 (1745834400 =
                       2025-04-28 10:00 UTC; 1745838000 = 2025-04-28 11:00 UTC) and would
                       have put the bot in permanent drain on its first 2026 run.
                       arb_main.py now hardcodes CUTOVER_TS = 1777374000 (no env override
                       during cutover week) and DRAIN_SECS = 3600. Drain gate added in
                       two places: main() scan loop (skips market scheduling) and
                       arb_one() top of entry-watch while loop (aborts in-flight watches).
                       py_compile passes. v2_handoff.md still shows 1745834400 - it is
                       no longer authoritative for code; if it is republished it must
                       be corrected separately.
```

**Note:** This claim must be `resolved_by_evidence`. `accepted_risk` is not allowed here.

---

### Claim 11 - Cutover Runbook and Safe-Off Behavior

```text
claim:                 The bot has explicit, tested behavior for the full April 28 cutover
                       sequence: pre-cutover drain, forced disable during Polymarket
                       downtime, no automatic re-enable until V2 smoke checks pass,
                       and a defined fallback if V2 validation fails on cutover day
why_it_matters:        Without a runbook, the bot can enter positions into a dead exchange,
                       restart automatically into the wrong state, or re-enable before V2
                       auth and smoke checks are actually confirmed
falsifiable_threshold: All four are documented and grounded in real runtime artifacts:
                       1. DRAIN_SECS gate stops new entries before CUTOVER_TS with
                          correct timestamp (resolved by Claim 10)
                       2. The actual restart controller and process-manager config prove
                          the bot does not auto-restart into trading after downtime
                          without an explicit re-enable step
                       3. V2 smoke test must pass before the pause flag or equivalent
                          runtime gate is removed
                       4. If V2 validation fails on April 28, bot stays paused and the
                          fallback action is documented and alertable
evidence_needed:       arb_main.py drain logic, watchdog.py or actual restart controller,
                       process-manager config, pause-flag behavior, Telegram alert path,
                       and a written fallback procedure in this record
owner:                 Gabriel
status:                open
resolved_by:
resolved_on:
resolution_note:       2026-05-02 local patch replaced the permanent post-cutover drain
                       with a post-cutover safe-off gate:
                       `POST_CUTOVER_READY_FLAG=/root/arb_v2_ready` must exist before
                       new entries can schedule after CUTOVER_TS, and `/root/arb_paused`
                       remains the first runtime stop gate. Dashboard `/api/bot/arb/start`
                       also refuses to remove `/root/arb_paused` after cutover unless the
                       same ready flag exists. Local regression tests
                       `tests/test_arb_v2_cutover_gate.py` and
                       `tests/test_dashboard_v2_start_gate.py` cover pre-drain, drain,
                       post-cutover missing-ready, post-cutover ready-flag, and dashboard
                       start-bypass cases. VPS deploy smoke on 2026-05-02 confirmed:
                       `/root/arb_paused` exists, `/root/arb_v2_ready` is absent,
                       no `arb_main` process is running, watchdog cron has no ARB restart
                       path, deployed files compile, and live
                       `POST /api/bot/arb/start` returns `409 Conflict` without removing
                       `/root/arb_paused`.
                       Follow-up VPS checks on 2026-05-02 reconfirmed
                       `/root/arb_paused` exists, `/root/arb_v2_ready` is absent, no
                       `arb_main` process is running, dashboard start returns
                       `409 Conflict` with the missing-ready-flag detail, direct
                       auth-derived `get_open_orders()` returns a list length of `0`, and
                       no signed order or live order was placed.
                       This is partial runtime evidence only. Claim remains open until
                       V2 smoke passes, the ready-flag creation procedure is documented,
                       and the alert/fallback paths are proven.
```

**Note:** This claim must be `resolved_by_evidence`. `accepted_risk` is not allowed here.

---

## Adjudicator Verdict

```text
state:       blocked
date:        2026-05-04
reason:      Claims 1 and 10 are closed; Claims 2-9 and 11 remain open. Claim 2's
             prior collateral-account mismatch is reconciled after the resolver patch:
             runtime funder now resolves to `0xEf5750...`, and raw Polygon pUSD
             balance/allowances/CTF approvals match CLOB `/balance-allowance`. The
             authorized direct signed smoke was rejected with CLOB HTTP 403 geoblock
             before acceptance; no order id was created and final open orders were [].
             The failed smoke path was direct NYC egress; production ARB V2 live order
             writes now fail closed without configured `CLOB_PROXY_URL`. The active
             configured proxy path egresses from Frankfurt/DE, not IPRoyal/Ireland;
             2026-05-04 read-only probes traced the prior proxy `/balance-allowance`
             `502 Bad Gateway` to SDK bootstrap call ordering at
             `/root/kalshiedge_arb/poly_executor.py` lines 1111-1113, which run
             outside `_run_with_clob_proxy` and egress direct from NYC where
             Cloudflare WAF blocks `/auth/api-key` (CF-RAY `9f63774f8b806da2`,
             client_ip `68.183.55.155`). With the helper-module HTTP client swap
             installed before bootstrap, all three V2 endpoints
             (`/auth/derive-api-key`, `/balance-allowance`, `/data/orders`) returned
             `200` from Cloudflare-FRA; balance raw `50668374` and max-uint
             allowances reconciled. The proxy preflight side of Claim 9 is now
             covered. The bot also has a second proxy-bypass at
             `self._http = httpx.AsyncClient(timeout=20.0)` (line 362) used in six
             call sites that egress direct from NYC; a code-only fix at the
             bootstrap site would not cover those. Claim 2 remains open because
             signed-order acceptance has still not been verified, and the
             bootstrap-proxy fix has not yet shipped.
next_action: Ship the bootstrap-proxy fix. Recommended primary: set
             `HTTP_PROXY=$CLOB_PROXY_URL` and `HTTPS_PROXY=$CLOB_PROXY_URL` in
             `/root/kalshiedge_arb/.env` so every httpx client (sync, async,
             SDK-helper, and the bot's own `self._http`) inherits the proxy via
             httpx `trust_env=True`; one config line, zero code change, covers
             all seven proxy-bypass sites identified 2026-05-04. Optional
             secondary for explicitness: wrap `poly_executor.py` lines 1111-1113
             inside `_run_with_clob_proxy`. After the fix is deployed, re-run
             non-order preflight against the deployed code path (no monkey-patch)
             and confirm `/auth/derive-api-key`, `/balance-allowance`, and
             `get_open_orders()` all return `200` through the proxy. Then, only
             with explicit gate authorization, attempt a signed submit/cancel
             smoke through the proxy to close Claim 2. Keep ARB paused and
             `/root/arb_v2_ready` absent throughout. Continue docs work for
             Claims 5, 6, 7, and 8; Claim 5 has a partial 2026-05-04 update
             (endpoint identified and reachable) but still needs a
             `token_id`-bearing numeric `base_fee` capture. Claim 11 still needs
             ready-flag procedure, alert/fallback proof, and final smoke evidence.
```

---

## Resolution Checklist

- [x] Claim 10: Verify drain timestamp - correct value is `1777374000` (2026-04-28 11:00 UTC). Closed 2026-04-27.
- [ ] Claim 6: Source V2 fee formula and feeRate; verify EV clears `MIN_EV=0.10` at ask `0.75`
- [ ] Claim 5: Identify fee rate endpoint from V2 SDK or docs
- [ ] Claim 7: Source pUSD address from official V2 docs - required before any code ships
- [ ] Claim 8: Confirm pUSD redemption path using test call, on-chain redemption, or official docs
- [ ] Claim 11: Document cutover runbook - local post-cutover safe-off gate added 2026-05-02; still requires VPS restart suppression, smoke-check gate evidence, alert path, and fallback if V2 validation fails
- [ ] Install `py-clob-client-v2` on VPS
- [x] Run import test -> closes Claim 1. Closed 2026-05-02.
- [ ] Run extended auth smoke test (all 3 steps including signed order accepted) -> closes Claim 2
- [ ] Read V2 `order_to_json` source -> closes Claim 3
- [ ] Run sell path smoke test -> closes Claim 4
- [ ] Test order submission through Frankfurt proxy -> closes Claim 9
- [ ] Run V2 order-path smoke test proving buy path, auth, and sell path behavior
- [ ] Record separate redemption-path evidence for Claim 8; `PAPER_TRADING` alone does not satisfy redemption verification

---

## Go-Live Criteria

Adjudicator may render `min_size_only` or `approved` only when:

1. All 11 claims have status != `open`
2. Claims 5, 7, 8, 10, and 11 are `resolved_by_evidence` - `accepted_risk` not allowed
3. Claim 2 closes only after all three auth steps pass including signed order accepted - client construction and public reads are insufficient
4. Claim 8 closes via one of the three defined evidence tiers - "if possible" is not valid
5. A V2 order-path smoke test is completed and logged: buy-side order creation, auth-required API access, signed order acceptance, and sell-path validation
6. Redemption-path verification is closed separately via Claim 8 evidence; if `PAPER_TRADING=1` bypasses real fill, settlement, or redemption behavior, it does not satisfy redemption verification
7. Claim 11 is grounded in the actual restart controller, process-manager config, and alert path used on the VPS

---

## Claim Closure Log

| Claim | Closed Date | Status | Resolved By | Resolution Note |
|-------|-------------|--------|-------------|-----------------|
| 1 | 2026-05-02 | resolved_by_evidence | VPS smoke | `py_clob_client_v2.ClobClient` import passed on VPS; deployed ARB files import V2 module and compile. |
| 10 | 2026-04-27 | resolved_by_evidence | Claude session | Correct CUTOVER_TS = 1777374000 (2026-04-28 11:00 UTC). Prior values 1745834400 and 1745838000 both resolved to 2025 — would have put bot in permanent drain. arb_main.py patched with drain gates in main() and arb_one(); py_compile passes. |

*(Updated as evidence is collected.)*

---

## Override Record

```text
override_used:                false
override_owner:
override_reason:
override_risk_accepted:
required_followup_review_by:
```
