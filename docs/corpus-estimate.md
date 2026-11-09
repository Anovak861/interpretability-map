# How big is the interpretability / alignment experimental literature, and what would ingesting it cost?

Estimated 2026-11-07; crawl, judging and recall updated 2026-11-08/09 with an OpenAlex key and the full arXiv CS harvest. Everything here is reproducible from the scripts named in each step; raw
data is in `data/census/`.

**Short answer.**

| | Point estimate | Plausible range |
|---|---|---|
| Experimental papers in scope (all years) | ~18,000 | 9,000 – 50,000 |
| Experimental LessWrong / AF posts | ~1,400 | 900 – 2,100 |
| Not yet in the map (~770 already ingested) | ~18,600 | 9,000 – 53,000 |
| Pro-plan throughput (Opus extraction, incl. ~15% overhead) | ~175 docs/week | 175 – 260 |
| **Backlog on Pro alone** | **~105 weeks** | 35 – 300 weeks |
| New experimental work published | ~150 papers + ~7 posts / week | 100 – 185 papers / week |
| **Keeping up with the flow on Pro** | **~90% of each week's limit** | 40% – 110% |

The range's high end comes almost entirely from one thing: 609,000 later-hop works with no
keyword hit, where 0 of 300 sampled were in scope but the sample can't rule out ~1.8%.

Pro can only just keep up with new work, and can't clear the backlog; see the last section for ways
to change that.

---

## 1. Cost per document, in plan terms

**Telemetry** (`data/logs/usage.jsonl`, written by every headless call):

| call type | calls | avg API-equivalent cost | avg cache-write tokens | avg output+thinking |
|---|---|---|---|---|
| extraction (full text) | 56 | $0.93 | 90,316 | 9,009 |
| triage (40 items) | 27 | $0.03 | 1,690 | 1,054 |
| gap re-check (3 docs) | 32 | $0.19 | 20,431 | 1,925 |
| consolidation batch | 46 | $0.13 | 7,277 | 5,513 |
| census judging (40 items) | 30 | $0.13 | — | — |

Extraction cost is dominated by putting the paper's full text into the prompt (~90k tokens).

**Plan meter.** I read the app's usage meter before and after two calibration batches
(16 documents, $12.77 API-equivalent; your other instances were also running, so this
*overstates* the per-document cost):

| | 5-hour window | weekly cap |
|---|---|---|
| before | 9% | 2% |
| after 16 docs (+ other usage) | 97% | 10% |
| per document (upper bound) | 5.5 pts | 0.5 pts |

So a 5-hour window holds at most ~18 extractions and a week at least ~200. The weekly cap is
the binding one: ~200 docs is ~11 full windows, about a third of the 33 windows in a week.
Earlier unattended runs agree: they hit the session limit after 11–30 documents.

Ingestion also needs triage (~$0.001/doc), consolidation and gap re-checks; together these
were ~15% on top of extraction in this project. **Throughput ≈ 175 documents/week on Pro**,
or up to ~260 if a third of the measured usage came from the other instances.

## 2. Gathering the corpus: recursive citation crawl

`scripts/census_citations.py` follows citations on OpenAlex, starting from the map's 1,741
documents that have arXiv ids:

- **Backward:** references cited by at least two relevant works.
- **Forward:** every work citing a relevant work. Hubs with more than 2,000 citations are
  skipped because their citers are mostly unrelated.

"Relevant" here means a cheap keyword prefilter (`interp_map/census.py`). It checks
interpretability or alignment terms together with language-model terms; interpretability of
other networks gets its own label. On the map's own papers that have abstracts, it flags 89%,
or 68% counting only language-model work.

| hop | how | works | keyword-relevant |
|---|---|---|---|
| 0 | seeds (map) | 1,741 | 1,006 |
| 1 | cite a seed | 33,326 | 10,861 |
| 1 | cited by ≥2 relevant | 885 | 207 |
| 2 | cite a hop-1 work | 192,176 | 44,547 |
| 2 | cited by ≥2 relevant | 47,224 | 8,421 |
| 3 | cite a hop-2 work (~29% of frontier expanded) | 387,804 | 55,810 |
| 3 | cited by ≥2 relevant | 217,084 | 20,462 |
| **total** | | **880,240** | **141,314** |

Hop 2 is complete. Hop 3 stopped at the keyed daily limit of 10,000 requests. It expands the
most-cited works first, and the yield is already falling, as the next table shows.

## 3. How many of those are actually in scope and experimental?

The keyword hits are noisy, so I had Claude judge a stratified sample of works with
`scripts/census_estimate.py` (700 of them for the later crawl, via `judge-later`). Each item is
labelled *core / adjacent / out*, and core items get a type: experiment, method, benchmark,
theory, survey or position. "Experimental" below means core and one of experiment, method or
benchmark.

**Is the judge's bar the same as the map's?** Of 80 random documents already in the map, it
called 90% core and 82.5% experimental. So the judge is about as inclusive as the map has been.

Judged rates by crawl stratum (`scripts/census_totals.py`, Wilson 90% intervals):

Hop "2a" is the part of hop 2 judged in the first round. "2b" is what the keyed crawl added,
from less-cited frontier works, and was sampled separately.

| stratum (hop, keyword label) | works | judged experimental | rate (90% CI) | estimate |
|---|---|---|---|---|
| 1, alignment | 3,384 | 26/70 | 37% (28–47%) | 1,257 |
| 1, both | 949 | 31/92 | 34% (26–42%) | 320 |
| 1, interpretability | 2,623 | 10/39 | 26% (16–39%) | 673 |
| 1, other-network interp | 4,112 | 1/50 | 2% (0–9%) | 82 |
| 1, no keyword hit | 23,143 | 2/53 | 4% (1–11%) | 873 |
| 2a, alignment | 5,611 | 19/89 | 21% (15–29%) | 1,198 |
| 2a, both | 1,542 | 13/66 | 20% (13–29%) | 304 |
| 2a, interpretability | 5,690 | 6/79 | 8% (4–14%) | 432 |
| 2a, other-network interp | 15,390 | 0/109 | 0% (0–2%) | 0 |
| 2a, no keyword hit | 105,971 | 3/208 | 1.4% (0.6–3.6%) | 1,528 |
| 2b, alignment | 6,472 | 8/50 | 16% (9–26%) | 1,036 |
| 2b, both | 2,144 | 15/50 | 30% (21–42%) | 643 |
| 2b, interpretability | 5,308 | 4/50 | 8% (4–17%) | 425 |
| 2b, other-network interp | 10,811 | 0/50 | 0% (0–5%) | 0 |
| 2b, no keyword hit | 80,461 | 0/150 | 0% (0–1.8%) | 0 |
| 3, alignment | 12,956 | 4/50 | 8% (4–17%) | 1,036 |
| 3, both | 3,129 | 8/50 | 16% (9–26%) | 501 |
| 3, interpretability | 11,243 | 1/50 | 2% (0–9%) | 225 |
| 3, other-network interp | 48,944 | 0/50 | 0% (0–5%) | 0 |
| 3, no keyword hit | 528,616 | 0/150 | 0% (0–1.8%) | 0 |
| **non-seed total** | | | | **10,532 (5,865 – 34,134)** |

Adding the seeds (622 experimental map papers plus roughly 450 of the 987 other seeds) gives
**~11,600 experimental papers found** (range ~6,900–35,200). The 0/150 rows contribute nothing
to the point estimate, which is probably a little low: even 0.3% of those 609,000 works would
add ~1,800. Their upper bounds supply most of the range's high end.

## 4. How much is the crawl missing?

**Independent check.** I took arXiv papers that LessWrong/AF posts with ≥10 karma link to, not
already in the map. That's a separate discovery mechanism: what the community chose to discuss.
`scripts/census_recall.py` finds 212 such papers. **The crawl found 155 of them, a recall of
73%** (90% CI 68–78%).

| arXiv year | LW-linked papers | first crawl (170k works) | current crawl |
|---|---|---|---|
| 2022 | 17 | 10 | 12 |
| 2023 | 42 | 28 | 34 |
| 2024 | 44 | 23 | 37 |
| 2025 | 46 | 9 | 27 |
| 2026 | 46 | 1 | 31 |

On this script's count the first crawl scores 80/212 (38%). The original ad-hoc count was 59/141
(42%), and I couldn't reproduce its exact filter, so the 212-paper set is used throughout now.
The deeper crawl fixed most of the collapse in recent recall that the first crawl showed.

**Total ≈ found ÷ recall = 11,600 ÷ 0.73 ≈ 16,000 papers** (about 9,000 at the optimistic ends
of both ranges, about 50,000 at the pessimistic ends).

This leans on the LW-linked papers being a fair sample. They skew toward prominent work, which
a citation crawl finds more easily, so the true total is more likely above 16,000 than below it.

**Second check: the full arXiv CS harvest.** `scripts/census_overlap.py` takes the 59,878 arXiv
CS papers matching the language-model keyword labels and counts how many the crawl reached:

| first submitted | matching on arXiv | in crawl | coverage |
|---|---|---|---|
| 2019–2024 | 13,642 | 9,539 | 70% |
| 2025 | 16,981 | 9,244 | 54% |
| 2026 to Nov 8 | 29,126 | 12,915 | 44% |

For 2019–2024 this agrees with the LessWrong check (~73%). For recent papers it is lower. The
keyword set is noisier than the set of truly in-scope papers, and off-topic matches are less
likely to be cited by relevant work, so this probably understates recall. The LessWrong set skews
prominent, so it probably overstates recall. The two bracket the answer.

Applying these rates by year (crawl-found non-seed works: 3,785 through 2024, 2,941 in 2025,
3,625 in 2026) gives ~19,100, or **~20,200 with the seeds**, against ~16,000 from the LessWrong
check. **I use ~18,000, the midpoint.**

## 5. LessWrong / Alignment Forum

`scripts/census_lesswrong.py` paged through all 40,774 posts since 2016; **16,023 carry the AI
tag**, 1,159 the Interpretability tag. Judged rates:

- **All AI-tagged posts:** 26% core, but only 8.7% (13/149; CI 5.6–13.3%) report experiments.
  That's **~1,400 experimental posts** (900–2,100). Most posts are arguments, agendas or
  commentary.
- **Interpretability-tagged posts:** 94% core and 53% experimental (n=89).

AI-tagged posts by year: 2022: 1,845 · 2023: 3,060 · 2024: 2,095 · 2025: 3,348 · 2026 (to
Nov 7): 3,913.

## 6. Ongoing flow

| source | found | ÷ recall for that year | per week |
|---|---|---|---|
| Crawl-found experimental papers, 2025 | 2,941 | 27/46 = 59% → ~5,000 | ~96 |
| Crawl-found experimental papers, 2026 to Nov 8 | 3,625 | 31/46 = 67% (56–77%) → ~5,400 | ~120 (105–145) |
| Experimental LW/AF posts | | 3,913 × 8.7% in 2026 | ~7 |

The ranges reflect recall only, not uncertainty in the judged rates, and map seeds are left out
of these year counts, a small undercount. Sanity check: the map's own arXiv search uses 20
phrases, mostly interpretability, so it's narrow. In October 2026 it found 119 papers, and
triage kept 64%. That's ~76 per month from that slice alone.

The arXiv coverage check gives a higher figure for 2026: 3,625 ÷ 44% ≈ 8,200, or **~185/week**
(2025: 2,941 ÷ 54% ≈ 5,400, ~105/week).

**Flow ≈ 120–185 papers/week (central ~150) + ~7 posts/week**, which is about 90% of Pro's
~175 docs/week (about 60% if throughput is 260/week; ~110% at the high end of flow).

Rough split, from the keyword labels of judged-experimental works in the first round: about 55% alignment/safety
(jailbreaks, refusal, RLHF failure modes, evals), 30% interpretability, and 15% unlabelled.
If you only want interpretability plus the alignment work that tests mechanisms, the totals
roughly halve.

## 7. What would change the numbers

| lever | effect on the ~105-week backlog |
|---|---|
| Max 5x plan | ~21 weeks |
| Max 20x plan | ~5 weeks |
| Cap full text at ~30k tokens (abstract, intro, results), from ~90k | likely ~2x cheaper per doc; measure quality first with `scripts/compare_extraction_models.py` |
| Two tiers: full extraction for important/cited works, abstract-only for the long tail | most of the long tail costs ~1/10 per doc |
| Cheaper model for extraction or triage | measure the same way; I didn't assume a price ratio |
| Narrow scope to interpretability + mechanistic alignment | roughly halves both backlog and flow |
| Split load with the Codex backend you already use | adds that plan's capacity |

## 8. Not done / caveats

- **Crawl incomplete:** hop 3 is about 29% through its forward frontier. Rerunning
  `scripts/census_citations.py` resumes it (10,000 requests/day). New hop-3 works are less
  cited and probably lower-yield than those sampled here, so the recall check is the better
  guide to whether more crawling is worth it.
- **arXiv cross-check is keyword-level:** the comparison covers keyword matches, not judged
  in-scope papers. Judging a sample of arXiv matches the crawl missed would replace the bracket
  with a single recall figure. Papers whose OpenAlex record has no arXiv id count as misses here.
- **Few positives in key strata:** the later "no keyword hit" strata had no positives in 150
  samples each. Larger samples there would narrow the high end most, at ~$0.13 per 40 items.
- **Judge vs. map scope:** "in scope" is the judge's call. It matches the map's inclusion rate,
  but your intended scope may be narrower or broader.
