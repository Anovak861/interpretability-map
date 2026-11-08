# Interpretability Map

A local database for organizing the mechanistic interpretability and alignment literature
around **claims**: what each paper, post or thread proposes, assumes, tests, replicates or
refutes. The goal is to see which parts of the field are solid, which are murky, and which
rest on assumptions that haven't been tested rigorously.

## Data model (short version)

| Object | What it is |
|---|---|
| **document** | A paper, blog post, essay, tweet thread, talk... Status: `stub` (only known because something cites it) -> `fetched` -> `extracted` -> `reviewed`. |
| **concept** | A node in a DAG (not a tree). Concepts can have several parents, e.g. `sparse-autoencoders` sits under both `dictionary-learning` and `superposition`. |
| **claim** | A standalone statement, independent of any one document, e.g. `features-are-one-dimensional`. Claims can depend on other claims. |
| **claim_evidence** | How a document bears on a claim: `proposes`, `assumes`, `supports`, `refutes`, `mixed`, `replicates`, `fails_to_replicate`, `extends`, `critiques`, with strength, experiment summary and models studied. |
| **document_links** | Lineage. `explicit_citation` (from Semantic Scholar or links in the text) is kept separate from `inferred_influence` (LLM-guessed, with confidence and rationale). |
| **annotations** | Your notes on any document, claim or concept: `note`, `idea`, `critique`, `question`, `next_step`, `dead_end`. Multi-user and private/public from day one. |

Schema: [interp_map/schema.sql](interp_map/schema.sql). The extraction JSON format: [interp_map/models.py](interp_map/models.py).

### How claim status is derived ([report.py](interp_map/report.py))

Status is computed from the evidence edges, never typed in. A human can override it with
`claims.status_override`.

- **replicated**: positive evidence from at least 2 *independent* groups, meaning no shared authors. Weak evidence doesn't count toward replication.
- **supported**: several positive results, but all from one group.
- **single_source**: one positive result.
- **untested**: proposed or assumed, never tested.
- **contested**: positive and negative results both exist, or only mixed ones.
- **refuted**: only negative results.

A paper that proposes a claim *and* presents evidence for it counts as the claim's first test.

**Fragile foundations** are claims that other work leans on (documents that assume them, plus
claims that transitively depend on them) but that are only untested, single-source or
contested. This is the main "where is the field on shaky ground" signal.

## Setup

```bash
py -3.10 -m venv .venv
```
```bash
.venv/Scripts/python -m pip install -r requirements.txt
```

## Web interface

```bash
.venv/Scripts/python -m interp_map.web.server
```

Then open http://127.0.0.1:8765. It runs locally only and has no login.

- **Overview**: the share of claims at each status, health per concept (status mix, share replicated or supported, fragile foundations), the claims the field leans on most but has tested least, recent notes, and the last automated update.
- **Concepts**: an outline of the concept graph (a concept with several parents shows in full once, with "also under …" elsewhere), or a graph view with two layouts:
  - **Map**: concepts cluster by region (see below), and each region is shaded as its own territory: a Voronoi cell around every concept, coloured by region, inside a smooth coastline. Zoomed out, it reads as a map of the field, with each region's name, size and share of solid claims. Click a region to zoom in.
  - **Hierarchy**: the top-down tree.

  Dot size is the number of claims, dot colour is how solid they are, and an orange ring marks fragile foundations.
- **Claims**: filter by status, by concept (sub-concepts included), or to fragile foundations only; sort by how much other work relies on a claim. Each claim page has an evidence timeline, its dependencies in both directions, notes, and a status override with a reason.
- **Documents**: filter by source, type, review state or author. Each document page shows its summary, the claims it bears on grouped by relation, its concepts and lineage, and a button to mark the extraction reviewed. "Show in lineage" on a card or document page opens the lineage graph centred on that document.
- **Lineage**: documents left to right by publication date, in horizontal bands by region; regions that cite each other sit next to each other.
  - **Size:** node size is influence, meaning how much later work builds on the document.
  - **Labels:** the most influential documents keep their labels when zoomed out, and more appear as you zoom in. Each label is measured and checked against the ones already placed, so none overlap.
  - **Bands:** a band's name appears when you hover over it.
  - **Pinning:** click a document to pin it and see its influence numbers. **Trace lineage** highlights two generations back (what it built on) and two forward (what built on it), and **Reading list** opens the list view below.
  - **Options:** switch to a single timeline, show links on hover only (the default when there are many), or highlight one region.
- **Reading list** (from a pinned document, or "Reading list →" on a document page): a document's lineage as a list.
  - **Background:** everything it builds on, meaning works it cites and documents whose claims it uses, then what those built on, up to 5 generations.
  - **Built on it:** everything that builds on it.
  - **Ordering:** reading order (oldest first), most essential first (influence divided by distance), or grouped by generation.
  - **Works not yet in the map** are listed too, with a **Queue** button that adds them to the next update run.
  - **Tracking and export:** tick works off as you read (saved in this browser), or copy the list as Markdown.
- **Notes**: every note, filterable by kind. Add notes from any document, claim or concept page.
- **Review**: documents awaiting review, likely duplicate labels shown side by side (keep either one, or mark them as different ideas), and the history of every hand edit.

Search (press `/`) covers concepts, claims, documents and authors.

### Foundational works

Automated discovery finds new work. Older foundational work enters through citations, or directly through [seed/canon.json](seed/canon.json), a hand-picked list of core papers and posts:

```bash
.venv/Scripts/python cli.py add-canon
```

The next update fetches these first. Each fetched title is checked against the expected one, so a wrong URL or arXiv ID is rejected rather than ingesting the wrong paper. arXiv requests are spaced about 3 seconds apart and retried with backoff, as arXiv asks.

```bash
.venv/Scripts/python cli.py resolve-stubs
```
Fills in real titles, authors and dates for cited arXiv works that aren't ingested yet. Updates run this automatically.

```bash
.venv/Scripts/python cli.py update --max 60 --workers 4 --wave-size 10
```
Overrides config.json for one bigger run.

### Regions and influence ([regions.py](interp_map/regions.py))

**Regions** are a small set of concepts (up to 10) that each cover many documents but overlap little with each other. They're chosen greedily:
- **Coverage** counts the documents whose *primary* concepts fall under a concept.
- **Gain** for each candidate is the documents it newly covers, minus a penalty for documents already covered.
- **Exclusions:** umbrella concepts covering over 60% of documents can't be regions, and neither can anything above or below a chosen region.

Each document's home band is the region its concepts sit in (primary concepts count double). On the concept map, leftover concepts are grouped under their top-level area as dashed "minor" regions.

```bash
.venv/Scripts/python cli.py regions
```
Shows the chosen regions, how many documents each covers, their pairwise overlap, and how many documents fall in no region.

**Influence** is PageRank over "later document builds on earlier document" links. A link counts if the later document cites the earlier one, or if it engages with (assumes, tests, extends…) a claim the earlier one proposed. The lineage graph uses its percentile for node size and label priority.

To see how the views behave at scale before the real map gets there, generate a synthetic map in a separate database and point the server at it:

```bash
.venv/Scripts/python scripts/make_synthetic.py data/synthetic.db 350
```
```bash
INTERP_MAP_DB=data/synthetic.db .venv/Scripts/python -m interp_map.web.server
```

### Claim granularity and consolidation

The map works only if evidence from different papers lands on the **same** claims. Two things
keep it that way.

**Extraction ([extract.py](interp_map/extract.py), [db.relevant_vocabulary](interp_map/db.py)).**
- **Relevant labels only:** each document is shown the existing claims and concepts that matter to it, not the whole list. That means claims on the papers it cites, claims matching its title, abstract and text, and the most widely shared claims; every concept slug is still listed. At around 7,800 claims this cut prompts from about 600k tokens to about 30k per document.
- **Granularity rules:** a claim must be something other work could test, rely on or dispute. Paper-specific results (numbers, models, datasets, tables) go in the evidence description under a broader claim, with strength reflecting how much of it they cover. Each document proposes at most 4 new claims. The source auditor applies the same rules.

**Consolidation ([consolidate.py](interp_map/consolidate.py)).** For claims already in the map:

```bash
.venv/Scripts/python cli.py consolidate --workers 4
```

This groups claims into batches of about 140 related claims (by concept subtree) and asks a model, per batch, which claims are:
- **duplicates** of one another, which get merged;
- **instances** of a broader claim, existing or newly proposed, which become evidence on that claim. The specific claim's text is kept in the evidence description, so nothing is lost.

The plan is saved to `data/consolidation/` and **nothing changes until you apply groups** under "Claim consolidation" on the Review page. Applied claims keep resolving through aliases, so later extractions that use an old label land on the broader claim.

### Editing during review

- **Claims** (Edit / Merge… / Delete on a claim page): change the statement, type, scope, concepts, and the claims it depends on.
- **Concepts**: change the name, kind, description and parent concepts.
- **Documents**: *Edit* on a document's claim list lets you change how the document bears on each claim (relation, strength, what was tested), remove a link, or add a claim, existing or new. Concepts can be edited too; ★ marks primary ones.
- **Duplicates**: claim and concept pages show "Possible duplicates" with a one-click merge.
- **Pickers**: type to find an existing claim or concept, or create a new one inline.

Edits are validated: a concept can't become its own ancestor, and a claim can't depend on itself through a chain. Every edit goes into the history, with the user recorded, ready for when others can edit too.

**Edits survive later imports.**
- **Merges:** a merged label becomes an alias, so a future extraction that uses the old slug attaches to the survivor.
- **Deletions:** a deleted label is skipped by future imports.
- **Document edits:** editing a document's claims or concepts marks it "edited by hand", and `cli.py import` then skips it unless you pass `--force`. Even then, merged and deleted labels stay merged and deleted.

Set `INTERP_MAP_DB` to point the server or CLI at another database file, for example a scratch copy to experiment on.

The server ([interp_map/web/server.py](interp_map/web/server.py)) is a JSON API over the same database, using the same health calculations as `cli.py report`. The frontend lives in [interp_map/web/static/](interp_map/web/static/). This is also the layout a hosted version would use.

## Workflow

```bash
.venv/Scripts/python cli.py add https://arxiv.org/abs/2309.08600
```

`add` fetches metadata, full text and explicit citations, then queues the document for
extraction. Supported sources: arXiv, LessWrong / Alignment Forum (via GraphQL),
transformer-circuits.pub, and generic web pages.

### Extraction: four backends, all on the same prompt ([extract.py](interp_map/extract.py))

1. **`--backend queue` (default): in a Claude Code session, on your plan.** The prompt goes to
   `data/queue/<slug>.md`. In Claude Code, ask: *"process the extraction queue in
   data/queue"*. Claude reads each prompt and writes `data/extractions/<slug>.json`. Then run:
   ```bash
   .venv/Scripts/python cli.py import
   ```
2. **`--backend claude_cli`: headless Claude Code (`claude -p`), on your plan.** Scriptable,
   no session needed, and what automated updates use. Install Claude Code on its own (it
   lands in `~/.local/bin/claude.exe`, which is found automatically; otherwise set
   `CLAUDE_BIN`), then run `claude` once in a terminal and `/login` with your Claude account.
   Each call runs with no tools, in a neutral folder, and with `ANTHROPIC_*` variables
   removed, so it always bills your plan rather than an API key. Headless runs count
   against your plan's usage limits.
3. **`--backend api`: Anthropic API** (needs `ANTHROPIC_API_KEY`). The natural choice once
   this is a hosted service.
4. **`--backend codex_cli`: headless Codex workers (`codex exec`).** Install a current
   Codex CLI, then run `codex login` and `codex login status` in a terminal. The CLI must
   support `--ignore-user-config`, `--ephemeral`, and `--output-schema`. Set `CODEX_BIN`
   to the executable path if it isn't on PATH (the Windows npm location is also checked).
   Workers use saved CLI authentication; inherited `OPENAI_API_KEY`, `CODEX_API_KEY`,
   and `OPENAI_BASE_URL` variables are removed. Saved authentication can be a ChatGPT
   login or API login; usage is charged according to that authentication.

### Using Codex workers

Extract documents already fetched into the database:

```powershell
.venv/Scripts/python cli.py extract-queue --backend codex_cli --workers 3 --wave-size 6 --limit 15
```

Or fetch and extract a new document, or run the full discovery/update workflow:

```powershell
.venv/Scripts/python cli.py add https://arxiv.org/abs/2309.08600 --backend codex_cli
.venv/Scripts/python cli.py update --backend codex_cli --max 15 --workers 3 --wave-size 6
.venv/Scripts/python cli.py triage --backend codex_cli
```

`add-manual` and `seed` also accept `--backend codex_cli`. For scheduled updates, set
`update.backend` in `config.json` to `"codex_cli"`; the scheduler uses that setting.
Triage follows the update backend for Codex runs. An optional `triage.backend` setting
(`"claude_cli"` or `"codex_cli"`) overrides this if you want separate providers.
Existing defaults remain unchanged: `add` queues prompts and `extract-queue` uses Claude
unless you pass a backend explicitly.

Each Codex call runs in a separate temporary directory with a read-only sandbox,
shell tools, apps, plugins, and subagents disabled, and web search disabled. The prompt
is sent through stdin, and the final JSON is requested with a strict output schema.
The parent process validates it, restores fetched metadata, and imports it serially
through the same concept, claim, and lineage ingestion used by Claude.

User `config.toml` is intentionally ignored so worker behavior doesn't inherit personal
MCP servers or project-specific settings. Set `$env:CODEX_MODEL` and
`$env:CODEX_REASONING_EFFORT` to select an exact model and reasoning level. The pinned
defaults are `gpt-6.1-sol` and `high`; the worker passes both explicitly and records
`codex-cli:<model>:reasoning=<effort>` on each extraction. Per-call token usage is saved
under `data/codex_runs/`, including failed validation attempts when usage is available.
Historical `6.1 sol no reasoning` badges mean **no explicit reasoning setting was supplied**,
not verified zero reasoning. These records use `reasoning=unspecified`; the default control
in the comparison actually reported nonzero reasoning tokens. Sol rejects explicit `none`.
Calls time out after 30 minutes, retry
invalid extraction output once, and stop batches on authentication or usage-limit errors.
Temporary worker files are removed after each call; validated extractions are retained
in `data/extractions/`.

The implementation follows the official [Codex non-interactive mode documentation](https://developers.openai.com/codex/noninteractive).
The manual `queue` prompts can also be processed from an interactive Codex session,
then loaded with `cli.py import`.

If `.venv/Scripts/python` reports a missing base Python, recreate that environment with
an installed Python 3.10+ and reinstall `requirements.txt`, or use a working interpreter
with the project's dependencies. Test the worker integration without provider calls:

```powershell
python -m unittest discover -s tests -v
```

Every extraction records its provenance. Treat extractions as drafts until reviewed.
Codex extractions also carry a model-and-reasoning badge in the Documents list.

### Parallel extraction in waves

```bash
.venv/Scripts/python cli.py extract-queue --workers 3 --wave-size 6
```

This extracts every `fetched` document. Within a wave, extractions run in parallel against
one snapshot of the existing concept and claim labels. Results are imported one at a
time, and the next wave sees everything the previous wave added. Smaller waves mean fewer
duplicate labels; more workers mean faster runs and faster use of your plan's allowance.

For a curated batch, pass `--document-ids 236 444 235` to select fetched documents in
that priority order (repeated IDs are ignored). Run one wave at a time to review and
merge equivalent concept labels before launching the next wave.
The reviewed additions in `seed/mechanistic_priority.json` include influential research
posts as well as papers; `scripts/prepare_priority_batch.py` fetches them with identifier
and title checks, reusing existing documents. Document merges preserve URL and normalized
title aliases so later citation imports do not recreate the same title variant.

Persona and character research is an explicit collection priority, including persona
vectors, the assistant axis, emergent misalignment, and conceptual foundations such as
*Simulators* and *Role-Play with Large Language Models*. Discovery terms and triage include
this direction. Extraction keeps conceptual arguments and hypotheses distinct from
experimental evidence. The curated source lists are recorded in
`data/persona-priority.json`, `data/persona-priority-additions.json`, and
`data/persona-second-priority.json`.

If the plan's usage limit is hit, the run stops cleanly: jobs not yet started are
cancelled and the rest stay queued for next time. A document that fails 3 times is
skipped, and its errors are kept in `extraction_failures`.

```bash
.venv/Scripts/python cli.py vocab-dupes
```
Lists concept and claim labels that look like the same idea under two names, which
parallel extractions tend to create. Fix them with `merge claim ...` / `merge concept ...`.

## Automated updates

```bash
.venv/Scripts/python cli.py update
```

One update run ([pipeline.py](interp_map/pipeline.py), tuned in [config.json](config.json)):

1. **Discover** new candidates since the last run:
   - **arXiv:** keyword search in cs.LG / cs.CL / cs.AI.
   - **LessWrong / Alignment Forum:** posts tagged Interpretability, above a karma bar, and at least 3 days old so their karma has settled. Linkposts to arXiv pull in the underlying paper.
   - **transformer-circuits.pub:** new papers on the index.
   - **Citation stubs:** works cited by at least 3 ingested documents. This is how older foundational work enters the map.
2. **Triage**: one headless call per batch of 40 rates each candidate's relevance from 0 to 3 using its title and abstract. Transformer Circuits papers skip this step. Candidates rated below 2 are rejected.
3. **Select** up to `max_new_documents_per_run`, round-robin across sources so one large backlog doesn't crowd out new work. Highly rated, high-karma and often-cited candidates go first.
4. **Fetch, then extract** in parallel waves.
5. **Log**: `data/logs/update-*.md` lists what was selected and extracted, **claims whose status changed**, and possible duplicate labels.

Each stage is safe to re-run, and a lock prevents overlapping runs.

```bash
.venv/Scripts/python cli.py update --dry-run
```
Shows what would be selected, without fetching or extracting.

```bash
.venv/Scripts/python cli.py candidates --status new
```
Shows candidates with their triage scores. `cli.py accept <id> ...` puts a candidate at
the front of the next run; `cli.py runs` shows recent run history.

### Scheduling (Windows Task Scheduler)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Every Weekly -DayOfWeek Sunday -At 03:00
```

This registers a task that runs [scripts/run_update.ps1](scripts/run_update.ps1), with output
going to `data/logs/scheduler.log`. The task:
- runs only while you're logged in, because headless CLI workers use your login;
- catches up on runs missed while the PC was asleep;
- shows as failed in Task Scheduler if the selected CLI isn't logged in.

Remove it with `-Remove`.

### Tweets / X threads

X can't be fetched without its paid API, and scraping breaks its terms. Save the thread's
text to a file and run:

```bash
.venv/Scripts/python cli.py add-manual thread.txt --title "..." --url https://x.com/... --authors "Name" --date 2025-01-01
```

### Other commands

```bash
.venv/Scripts/python cli.py report
```
Per-concept health (including sub-concepts), fragile foundations, and every claim's status.

```bash
.venv/Scripts/python cli.py show features-are-one-dimensional
```
Any document, claim or concept by slug, with its evidence, lineage and your notes.

```bash
.venv/Scripts/python cli.py note claim features-are-one-dimensional "..." --kind next_step
```

```bash
.venv/Scripts/python cli.py inbox
```
Upkeep queue: documents waiting for extraction, plus the most-cited stubs not yet ingested.

```bash
.venv/Scripts/python cli.py dupes
```
Likely duplicate documents (mirrors, title variants), each with the `merge` command to fix it.
Merge also works for claims, concepts and authors, e.g. `merge author "Christopher Olah" "Chris Olah"`.
Author identity matters because the independence check depends on it.

```bash
.venv/Scripts/python cli.py backfill-refs
```
Retries Semantic Scholar citations. The free pool is rate-limited often; set `S2_API_KEY` (free on request) to fix that.

## Seed set

[seed/seed_list.json](seed/seed_list.json) covers 10 documents across Transformer Circuits,
arXiv and LessWrong/AF: the circuits framework, induction heads, toy models of superposition,
IOI, two SAE papers, the linear representation hypothesis, multi-dimensional features, the
refusal direction, and the SAE-probing critique. They were chosen to overlap on claims. The
seed extractions in `data/extractions/` were written in a Claude Code session and are
**unreviewed**.

## Known limitations / next steps

- **Single user**: edits are recorded as `me`. Accounts and permissions come with hosting.
- **Vocabulary drift**: the extractor sees the existing concept and claim slugs, but duplicates will still appear. Review the run log's duplicate list and `merge` periodically.
- **Vocabulary size in prompts**: every extraction prompt includes all existing labels. That's fine for hundreds of claims; at thousands, it should send only labels related to the document's likely concepts.
- **Triage is title + abstract only**, so a strong paper with a vague abstract can be under-rated. `cli.py accept` overrides it.
- **Author identity** relies on name strings plus manual aliases. ORCID / Semantic Scholar author IDs would be better.
- **Link-harvested citations** from pages that were later updated can point forward in time. `show` labels these "later link (page update)".
- **Web UI / hosting**: the schema is plain SQL that ports to Postgres. The likely path is FastAPI plus an interactive graph view, with sign-in to enable public annotations.
