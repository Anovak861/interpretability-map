"""Sizing the literature: a cheap keyword classifier for interpretability / alignment work on
language models. It is a prefilter only; its precision and the share of hits that report
experiments are measured on an LLM-judged sample (scripts/census_estimate.py)."""
from __future__ import annotations

import re


def _rx(pattern: str) -> re.Pattern:
    return re.compile("".join(line.strip() for line in pattern.splitlines()), re.I)

# what the work is about
INTERP = _rx(r"""interpretab|explainab|mechanistic|sparse auto-?encoder|dictionary learning|superposition|
    \bcircuits?\b|attention heads?|induction heads?|\bprob(e|es|ing)\b|activation patching|path patching|
    causal (tracing|mediation|abstraction|intervention)|logit lens|tuned lens|steering vectors?|activation steering|
    representation engineering|feature attribution|attribution graph|transcoder|monosemantic|polysemantic|
    linear representation|\bneurons?\b|saliency|concept (activation|bottleneck)|model editing|knowledge editing|
    grokking|internal representations?|latent (space|representations?|directions?)|residual stream|
    explanations?|latent knowledge|task vectors?|function vectors?|attention (weights|patterns)|information flow|
    feature visuali[sz]ation|disentangl|representational similarity""")
ALIGN = _rx(r"""\balign(ment|ed|ing)\b|\brlhf\b|human feedback|reward model|reward hacking|specification gaming|
    goal misgeneralization|decepti(ve|on)|sycophan|jailbreak|red[- ]teaming|\brefus(al|e|es)\b|harmful|
    safety (training|alignment|fine-?tuning|evaluation|behavio)|scheming|sandbag|situational awareness|power-seeking|
    corrigib|scalable oversight|\bdebate\b|weak-to-strong|constitutional ai|honest(y)?|truthful|backdoor|sleeper agent|
    emergent misalignment|\bpersonas?\b|unlearning|model organisms?|ai safety|ai control|\bmonitor(ing|s)?\b|
    faithful(ness)?|evaluation awareness|dangerous capabilit|catastrophic risk|existential risk""")
# what it is about it on: language models (or neural nets in general, for interpretability)
LM = _rx(r"""language models?|\bllms?\b|transformers?|\bgpt|\bbert\b|chatbots?|foundation models?|
    large models?|\bclaude\b|\bllama|instruction[- ]tun|chat models?|ai (systems?|models?|assistants?)""")
NN = re.compile(r"neural networks?|deep (learning|networks?)|\bmodels?\b", re.I)
# keyword hits that are usually something else ("alignment" of sequences, images, ...)
NOISE = _rx(r"""sequence alignment|image alignment|point cloud|registration|cross-modal alignment|
    word alignment|entity alignment|domain alignment|feature alignment|traffic|medical imag|protein""")


def classify(title: str | None, abstract: str | None) -> str | None:
    """'interp', 'align', 'both' (about language models), 'interp_nn' (interpretability of other
    neural networks, mostly vision; foundational for the field but outside its core) or None."""
    t = f"{title or ''}. {abstract or ''}"
    i = bool(INTERP.search(t)) and bool(LM.search(t))
    a = bool(ALIGN.search(t)) and bool(LM.search(t)) and not (NOISE.search(t) and not re.search(r"\balignment (of|with) (human|values|preferences)|ai safety", t, re.I))
    if i or a:
        return "both" if i and a else "interp" if i else "align"
    return "interp_nn" if INTERP.search(t) and NN.search(t) else None


# The first round of judging sampled the crawl when it held this many works. Works added later
# came from less-cited frontier items, so hop 2 is split at this line and judged separately.
FIRST_JUDGED_LINES = 170156


def hop_tier(hop: int, line: int) -> str:
    """Crawl-distance stratum of a work, given its line number in openalex_works.jsonl."""
    if hop == 2:
        return "2a" if line < FIRST_JUDGED_LINES else "2b"
    return str(min(hop, 3))
