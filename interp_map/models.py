"""The extraction format: what an LLM (or a person) produces for one document.

The same JSON shape is used whether an extraction comes from the Claude API,
is written by hand, or is edited during review, so everything funnels through
one ingest path (`db.ingest_extraction`).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

DocType = Literal["paper", "blog_post", "essay", "tweet_thread", "talk", "code", "dataset", "other"]
Source = Literal["arxiv", "lesswrong", "alignmentforum", "transformer_circuits", "twitter", "web", "manual"]
ConceptKind = Literal["topic", "method", "phenomenon", "hypothesis", "tool", "model_component", "benchmark"]
ClaimType = Literal["empirical", "theoretical", "methodological", "hypothesis"]
EvidenceRelation = Literal[
    "proposes", "assumes", "supports", "refutes", "mixed",
    "replicates", "fails_to_replicate", "extends", "critiques",
]


class DocumentMeta(BaseModel):
    title: str
    doc_type: DocType = "paper"
    source: Source = "web"
    url: Optional[str] = None
    arxiv_id: Optional[str] = None
    published_date: Optional[str] = None
    authors: list[str] = Field(default_factory=list)
    abstract: Optional[str] = None


class ConceptRef(BaseModel):
    slug: str = Field(description="kebab-case id; reuse an existing slug when the concept already exists")
    name: str
    description: Optional[str] = None
    kind: ConceptKind = "topic"
    parents: list[str] = Field(default_factory=list, description="slugs of broader / foundational concepts")
    role: Literal["primary", "secondary"] = "secondary"


class ClaimRef(BaseModel):
    slug: str = Field(description="kebab-case id; reuse an existing slug when the claim already exists")
    statement: str
    claim_type: ClaimType = "empirical"
    scope: Optional[str] = None
    concepts: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list, description="slugs of claims this one presupposes")
    relation: EvidenceRelation = Field(description="how THIS document bears on the claim")
    strength: Optional[Literal["weak", "moderate", "strong"]] = None
    experiment_summary: Optional[str] = None
    models_studied: list[str] = Field(default_factory=list)
    location: Optional[str] = None
    # for NEW claims only; ignored when the slug already exists
    level: Optional[Literal["thesis", "finding"]] = Field(
        None, description="thesis = a broad claim other papers could test; finding = one result of this document")
    under: Optional[str] = Field(None, description="for a finding: slug of the existing broad claim or paradigm it bears on")
    stance: Optional[Literal["supports", "contradicts", "qualifies", "applies"]] = Field(
        None, description="for a finding with `under`: how it bears on that claim")


class ReferenceRef(BaseModel):
    title: str
    url: Optional[str] = None
    arxiv_id: Optional[str] = None
    link_type: Literal["explicit_citation", "inferred_influence"] = "explicit_citation"
    confidence: Optional[float] = None
    rationale: Optional[str] = None


class Extraction(BaseModel):
    document: DocumentMeta
    summary: str
    concepts: list[ConceptRef] = Field(default_factory=list)
    claims: list[ClaimRef] = Field(default_factory=list)
    references: list[ReferenceRef] = Field(default_factory=list)
    provenance: str = "manual"
