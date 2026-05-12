# CLAUDE.md — orientation for Claude Code working in this repo

You are an LLM coding agent. This file is the handoff brief. Read it before making changes. It encodes load-bearing decisions other sessions made and the rationale for them. If a section here contradicts a quick observation from the code, the doc is probably right (the code might have been mid-refactor) — but check, and update this file when the truth changes.

## What Dalila is

A single-user (initially) personal AI agent that delivers a daily 800–1500 word brief on humanitarian / development / philanthropy news, with a UAE-specific lens, over Telegram every morning at 06:30 GST. Built for a principal at the UAE Presidential Court's Office for Development Affairs (Ary, the user). Designed to live within the cost envelope of a single Claude Pro/Max subscription.

The full concept lives in `../dalila-concept-note.md` and `../dalila-sources.md` (the latter scopes which sources are in/out). The leadership pitch at `../dalila-leadership-pitch.pptx` is for context. The UAE Foreign Aid Policy 2026 (used to derive doctrine_topics.yaml) lives at `~/Documents/Foreign Aid Policy/` outside this repo and is internal-only — never commit it or extracts of it (the .gitignore blocks accidents).

## Architecture in one screen

```
  sources.yaml  ─►  ingestors/ ─►  prefilter ─►  items table (sqlite)
                  (rss, gdelt,    (keyword +
                   acled, scrape)  entity match,
                                   ~80% drop rate)
                                                       │
                                                       ▼
                                            ┌─── classifier (Haiku) ──┐
                                            │  category, entities,    │
                                 ┌──────────│  uae_relevance,         │
                                 │          │  doctrine_relation      │
                                 │          │  (batch=25 per call)    │
                                 │          └─────────────────────────┘
                                 │                     │
                                 ▼                     ▼
                  ┌─── editor (Haiku) ────┐   ┌─── doctrine (Haiku) ───┐
                  │ daily digest 06:30 GST│   │ doctrine_facts table:  │
                  │ + /more deep-dives    │   │ topic, position,       │
                  └───────────┬───────────┘   │ evolution_log,         │
                              │               │ confidence; runs every │
                              │               │ 15 min on flagged items│
                              ▼               └────────────────────────┘
                    Telegram broadcast
                    (every chat that
                     ran /start)
```

Storage: one SQLite file (`dalila.db`). Scheduling: APScheduler in-process. No Redis, no Postgres, no message queue — by design. The cost lever and complexity ceiling are both held by "every LLM call is a `claude` CLI subprocess".

## Critical invariants — DO NOT VIOLATE

- **LLM access is via the `claude` CLI, NOT the Anthropic API SDK.** Do not add `anthropic` to dependencies. Do not write `client.messages.create(...)`. All Claude calls go through `src/dalila/llm.py`. The user is on a Pro/Max plan; per-token API spend defeats the architecture. If you find yourself needing fine-grained features (structured outputs, prompt-cache introspection, parallel API calls), surface the tradeoff to the user before pivoting.
- **Free sources only in MVP.** No paid X API, no NewsAPI subscription, no Devex paid tier. Ask before adding a paid source.
- **Models**: Haiku 4.5 (`llm.HAIKU`) for classifier, editor, deep-dive, and doctrine in MVP. Originally specced Sonnet for the editor; pinned to Haiku-only to share one model/cache/rate-limit pool. To upgrade the editor back to Sonnet, change `model=llm.HAIKU` to `model=llm.SONNET` in `editor.py::compose_digest`.
- **Classifier system prompt is `instructions + entity watchlist`, sent identically every call.** This lets Claude Code's internal cache engage. Don't interpolate timestamps, request IDs, or per-call variables into the system prompt — that defeats caching.
- **Prefilter before classifying.** `pipeline._prefilter_match` drops ~80% of items based on keyword + entity match. Do not bypass it. UAE state/entity sources auto-pass (low volume, high signal — filtering doesn't help).
- **Migrations are forward-only, numbered, idempotent.** Drop new files as `migrations/00X_*.sql`; `db.init_db()` applies any not in `schema_migrations`. Never edit a migration that's been deployed.
- **Doctrine topic slugs are kebab-case.** The validator in `doctrine.py` enforces `^[a-z][a-z0-9-]{1,48}$`. Bad slugs are rejected as noop. Prefer slugs from `doctrine_topics.yaml`; only invent new ones for genuinely novel positions.

## File ownership (who edits what)

| To change… | Edit |
|---|---|
| Add a new source | `sources.yaml` |
| Add a new entity to track | `entities.yaml` |
| Add a doctrine topic to the seed vocabulary | `doctrine_topics.yaml` |
| Change classifier behaviour | `prompts/classifier.md` |
| Change digest format | `prompts/editor.md` |
| Change deep-dive synthesis style | `prompts/deepdive.md` |
| Change doctrine extraction rules | `prompts/doctrine.md` |
| Add a CLI subcommand | `src/dalila/cli.py` |
| Add a Telegram command | `src/dalila/bot.py` (also `USER_MENU_COMMANDS` if user-facing) |
| Add a new ingestor kind | new file in `src/dalila/ingestors/` + dispatch in `ingestors/base.py` |
| Schema change | new `migrations/00X_*.sql` — runs automatically via `db.init_db()` |
| Near-duplicate threshold | `is_near_duplicate(threshold=…)` in `src/dalila/simhash.py` |
| Rate-limit fallback duration | `_RATE_LIMIT_BACKOFF` in `src/dalila/scheduler.py` |
| Bot display name / `/` menu | `BOT_DISPLAY_NAME` and `USER_MENU_COMMANDS` in `src/dalila/bot.py` |

## What was built when (recent shipping log)

- **Bot + scheduler + ingestors + classifier + editor**: the original MVP.
- **Cross-platform CLI launch, batched classifier, smart rate-limit back-off**: early hardening.
- **Telegram timeout fix** (commit `7ac44b3`): bumped HTTPX timeouts and added exponential retry on `app.initialize()`.
- **Rate-limit exact-reset parsing** (commit `f448173`): parses "resets 5:30pm (Asia/Dubai)" from CLI errors and pauses precisely until then.
- **`verify-sources` command** (commit `f0604ee`): canary that probes every enabled source; catches dead URLs, selector rot, blank fetches.
- **SimHash dedup** (commit `13285f0`): 64-bit title hash, threshold 12, clustered at digest time; schema_migrations introduced to track applied migrations.
- **/more deep-dive** (commit `afd344e`): bot command + `prompts/deepdive.md` + `db.items_matching_topic`.
- **Doctrine tracker** (commit `18fd332`): `doctrine_facts` table, processing flag, `prompts/doctrine.md`, validation, scheduler job. 20-topic seed vocabulary (later expanded to 35).
- **Bot identity + `/` menu + read-only /digest** (commits `ccb57ba`, `46f8e7d`): `Dalila | دليلة` name, set_my_commands, `/digest` returns last persisted digest by default.
- **GCP free-tier deploy kit** (commit `e1f361d`): `deploy/install-on-vm.sh`, systemd template `dalila@.service`, walkthrough README.
- **Cross-distro Python detection + venv repair** (commits `3c43a5b`, `c941153`, `0b414ce`, `2fa15bd`): Debian bookworm uses python3.11; ensurepip needed after venv creation.
- **35-topic doctrine vocabulary + policy-aligned entities** (current): expanded doctrine_topics.yaml from 20 to 35 slugs; added 15 institutional entities from the UAE Foreign Aid Policy 2026 (ALTÉRRA, EIA, AD Ports, TAQA, ADNOC, MBZUAI, ICBA, IRENA, AMF, Sharjah Charity, Dar Al Ber, ICO, UAE FAST, Armed Forces Joint Command, IHC Dubai — the last split out from IHPC which is a different institution).

## Style / conventions

- Python 3.11+ (was 3.12; loosened so the codebase runs on Debian bookworm without external PPAs).
- Use the union syntax (`str | None`, not `Optional[str]`).
- Dataclasses for value objects (`models.py`). No Pydantic.
- Logging via `logging.getLogger(__name__)`, not `print()` (except in CLI handlers).
- DB access is always through helpers in `db.py` — don't write inline SQL in pipeline/bot/etc.
- All env config flows through `config.get_config()`. Don't read `os.getenv` elsewhere.
- Prompts live in `prompts/*.md` as version-controlled markdown, not inline strings.

## Classifier batch size (tuned 2026-05-12)

Default `batch_size = 25` was set after empirical measurement on Windows + Claude Code 2.1.126. Single-item calls ~33s/item (dominated by CLI spawn + auth overhead); size-25/30 batches drop to ~2.5s per item — ~13× speedup. Going beyond 30 hits diminishing returns because Haiku's output tokens scale linearly with batch size. Going below 10 leaves spawn overhead on the table.

Benchmark script: `benchmark_batch.py`. Re-run if you switch hosts (Linux likely has lower spawn overhead and a different sweet spot) or upgrade Claude Code.

## Source blockers (audited 2026-05-12)

Eight sources from the original spec are disabled in `sources.yaml` with reasons in inline comments:

- **WAM, MoFA UAE, ERC, UAE Aid Agency** — JS-rendered pages; static HTML has no article cards. Need a headless browser (Playwright) to pull. Tracked for v0.2.
- **Reuters** — `feeds.reuters.com` retired ~2020; no free RSS exists.
- **Devex, The New Humanitarian** — anti-bot protection (DataDome / Cloudflare) returns HTML to non-browser requesters even on documented RSS paths.
- **Erth Zayed Philanthropies** — splash page only, no news section. EZP mentions reach Dalila via the entity watchlist instead.

Working sources: The National, Gulf News, NYT (World/Africa/Middle East), WaPo, BBC World, Al Jazeera, ReliefWeb, GDELT 2.0, IDMC, ACLED (if creds provided). That's 9 active + ACLED.

When v0.2 adds Playwright, the fix is in `src/dalila/ingestors/scrape.py`: add a `renderer: playwright` option to source config, branch on it in `fetch()`, and have the new code path call Playwright's sync API to load the page, wait for hydration, then run BeautifulSoup over the rendered DOM.

## Common operations

```bash
dalila init                       # bootstrap DB (idempotent; applies pending migrations)
dalila check                      # verify env
dalila verify-sources             # probe every enabled source — catches dead URLs / selector rot
dalila ingest                     # one ingest pass
dalila classify --limit 50        # classify pending items (default batch=25)
dalila doctrine --limit 20        # extract doctrine facts from classified UAE-leadership items
dalila digest                     # compose + print today's digest
dalila bot                        # start bot + scheduler (production)
dalila set-name                   # push canonical bot identity + commands menu to Telegram
pytest -v                         # smoke tests
```

## Scheduler jobs (when `dalila bot` runs)

| Job | Cadence | Notes |
|---|---|---|
| ingest    | every `ingest_interval_minutes` (default 30m) | replace_existing |
| classify  | every 5m | pauses exactly until rate-limit window resets (parsed from CLI error) |
| doctrine  | every 15m | independent rate-limit back-off; cheap when queue is empty |
| digest    | daily at `digest_time` GST | also runs `classify(200)` first to flush backlog |

## Deployment paths

- **Local development**: `dalila bot` in a terminal. Dies when you close the window. Fine for iteration.
- **Windows production**: `scripts/install-service.ps1` installs an NSSM service. Needs Admin elevation, scoop-installed NSSM.
- **Cloud production**: `deploy/install-on-vm.sh` + `deploy/dalila@.service` on a free-tier Google Cloud e2-micro. Full walkthrough in `deploy/README.md`. Survives reboot/disconnect, $0/month.

## What NOT to do

- Don't add interactive Claude features (web search, computer use, file ops) to the classifier / editor / doctrine prompts. All are one-shot, no-tool-use calls. `--max-turns 1` is set in `llm._run_claude` for this reason.
- Don't store secrets in `entities.yaml`, `sources.yaml`, or `doctrine_topics.yaml` — they're committed to git.
- Don't commit `.env`, `*.db`, `*.docx` (policy docs are gitignored).
- Don't reach for the Anthropic SDK.
- Don't add `/status` to the user-facing menu — it's a hidden operator command per Ary's preference. Keep the handler but exclude from `USER_MENU_COMMANDS` and `HELP_TEXT`.
- Don't make `/digest` recompose by default. It returns the persisted daily digest; only `/digest fresh` triggers recomposition. This was a deliberate cost choice.

## Glossary

- **Classifier / editor / doctrine / deep-dive**: the four LLM call sites, each with its own prompt file.
- **Pre-filter**: cheap pre-LLM keyword+entity match (`pipeline._prefilter_match`).
- **doctrine_relation**: the classifier's tag on a single item — one of `reinforcing | refining | evolving | contradicting | new | null`. Drives whether the item gets sent to the doctrine pass.
- **doctrine_facts**: structured rows representing tracked UAE positions on topics. One row per topic. Evolution log appended on every update.
- **SimHash threshold**: 12 bits Hamming distance on 64-bit title hashes — empirically catches cross-outlet reposts (~6-10 bits apart) without merging unrelated stories (~28-40 bits apart).
- **IHPC** vs **IHC**: easy to confuse and used to be aliased together in entities.yaml. **IHPC** = International Humanitarian and Philanthropic Council (policy coordination). **IHC** = International Humanitarian City Dubai (logistics warehousing). Now separate entities.
