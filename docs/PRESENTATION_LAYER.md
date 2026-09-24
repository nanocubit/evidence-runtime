# Presentation Layer — Design Note (frozen for review)

**Status:** design only — not implemented in v0.3.x  
**Depends on:** stable Fact / Evidence / Conflict (L1 done)  
**Renderer candidate:** [Takumi](https://github.com/kane50613/takumi) (Rust, MIT/Apache-2.0)  
**Last updated:** 2026-08-17

---

## 1. Problem

Evidence Runtime returns machine facts with provenance. Humans and agents still need a **compact visual form** that:

- does not screenshot the source site (noise, ads, unstable layout);
- does not invent values (only verified inputs);
- shows confidence, conflict, and source at a glance;
- can be cached, versioned, and regenerated when facts change.

## 2. Principle

```text
Evidence Runtime never renders.
Presentation only consumes verified inputs.
```

| Layer | Responsibility |
|-------|----------------|
| L0–L1 (this repo) | acquire → extract → Fact + Evidence + Conflict |
| L2/L3 (planned) | LLM fill / browser escalate |
| **Presentation Compiler** | *what* to show (EditorialCard) |
| **Template Registry** | *how* to layout (rules, not LLM CSS) |
| **Takumi (or equivalent)** | *pixels* from a deterministic node tree |

Browser (Playwright) is for **acquisition**, not for drawing our cards.

## 3. Pipeline

```text
ExtractionRun
    → Editorial Composer   (select facts, intent, visual_kind)
    → EditorialCard JSON   (semantic, renderer-agnostic)
    → Template.render()    (→ EditorialNodeTree)
    → Takumi / backend     (PNG | WebP | GIF | SVG | frame)
    → cache by card_hash
```

### 3.1 EditorialCard (semantic)

Conceptual contract (Pydantic later):

```text
EditorialCard
  visual_kind: price_summary | price_drop | article_brief | comparison | conflict | timeline
  headline: str
  locale: str
  sections: [EditorialSection]
  fact_ids: [UUID]
  evidence_ids: [UUID]
  warnings: [str]          # e.g. conflict_detected
  template_version: str
  card_hash: str           # hash(card body + template_version + tokens)
```

Composer rules (deterministic first):

- prefer facts with `validation_status=valid` and non-empty `evidence_ids`;
- if `Conflict.status != none` → `visual_kind=conflict` or conflict strip;
- never put a value on the card without a backing fact id;
- optional fields missing → empty slot or omit, never fabricate.

### 3.2 EditorialNodeTree

Serializable tree compatible with Takumi-style input (JSON):

```text
Node = container | text | image
  style: flex/grid/typography/color tokens only from design system
  children: [Node]
```

Produced **only** by templates, not by LLM.

### 3.3 Design tokens (evidence-native)

```text
--bg, --paper, --ink
--verified     # high confidence, multi-source
--changed      # delta / price drop
--conflict     # competing values
--inference    # low confidence or L2-filled (future)
--accent
```

Visual mapping:

| Signal | Treatment |
|--------|-----------|
| confidence ≥ 0.95 | verified style + optional "N sources" |
| conflict | dual values, warning color |
| extraction_method | small method glyph (json-ld, meta, css, …) |
| missing required | dashed placeholder, not guessed text |
| retrieved_at | footer timestamp |

## 4. Template registry (MVP set)

| Kind | Inputs | Purpose |
|------|--------|---------|
| `price_summary` | name, price, currency, brand? | default product card |
| `price_drop` | old/new price, delta% | change-detection |
| `article_brief` | title, author?, date?, source | article / docs |
| `conflict` | field, values[], methods | explicit disagreement |
| `comparison` | 2–3 entities × shared fields | later |

MVP vertical slice: **product + price_summary only**.

## 5. Caching

```text
card_hash = H(EditorialCard canonical JSON + template_version + token_set_id)
if cache hit → return bytes
else render → store (MinIO/disk) keyed by card_hash
```

Align with L0 content_signature: fact change → new card_hash → re-render only then.

Animation (optional): same node tree, Takumi sampled at timestamps `t` for price_drop / timeline — not N full browser screenshots.

## 6. Integration options (Python core)

| Option | When |
|--------|------|
| JSON NodeTree → Node/Rust **sidecar** | default for v1 |
| Rust crate + PyO3 | if latency/ops demand single binary |
| WASM edge | public OG URLs only |

Core package stays Python; renderer is a **backend**, swappable.

## 7. Non-goals

- Rendering arbitrary third-party pages
- LLM-authored CSS or free-form node trees in production
- Replacing Playwright snapshots used as **acquisition evidence**
- Full design system before one golden template + tests

## 8. Acceptance criteria (when implemented)

1. Given an `ExtractionRun` with valid product price, produce 1200×630 WebP without Chromium.
2. Card bytes are stable for identical `card_hash` (golden test).
3. Conflict run surfaces both values; no silent pick on the image.
4. Every numeric/text value on the card resolves to a `fact_id` in the run.
5. Benchmark: p95 render latency budget documented; no regression on L1 extract metrics.

## 9. Roadmap position

```text
Done:    L0–L1 extraction, evidence, GT, PR-1 structured
Next:    L1 polish (full baseline, structured/legacy dedupe) → L3 escalate
Later:   Presentation MVP (this doc) — after Fact API remains stable
Research: own agent browser runtime (separate from presentation)
```

**Do not start Takumi integration until** this note is accepted and L1 baseline after PR-1 is recorded.

## 10. References

- Takumi: https://github.com/kane50613/takumi
- Internal: `VISION.md`, `CHANGELOG.md`, `evidence_runtime/models.py` (Fact, Evidence, Conflict)
