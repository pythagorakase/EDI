# EDI Memory System — Design Spec

**Status:** Draft v1
**Date:** 2026-02-03
**Authors:** Neil + EDI

---

## 1. Goals

Give EDI durable, queryable memory derived from Neil's ~854 chat histories (OpenAI + Anthropic, ~71M tokens raw). The system should:

1. **Import** chat exports incrementally (dedup-aware)
2. **Summarize** conversations in multiple passes (per-chat → cross-chat → personality)
3. **Embed** summaries for semantic search via pgvector
4. **Export** curated topic files to `memory/*.md` for EDI's boot context
5. **Serve** a query endpoint for on-demand deep recall

### Non-Goals (for now)
- Real-time chat streaming (batch import only)
- Reranker (evaluate baseline performance first)
- Multi-user support

---

## 2. Architecture Overview

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  JSON Export  │────▶│  Import Pipeline  │────▶│   PostgreSQL    │
│  (OpenAI +   │     │  (parse, dedup,   │     │   + pgvector    │
│   Anthropic)  │     │   incremental)    │     │   (edi-base)    │
└──────────────┘     └──────────────────┘     └────────┬────────┘
                                                        │
                              ┌──────────────────────────┤
                              │                          │
                     ┌────────▼────────┐     ┌──────────▼──────────┐
                     │ Processing Pipe │     │   Query Endpoint     │
                     │ Pass 1: Summary │     │   hybrid vector +    │
                     │ Pass 2: Synth   │     │   keyword + IDF      │
                     │ Pass 3: Profile │     └──────────┬──────────┘
                     └────────┬────────┘                │
                              │                         │
                     ┌────────▼────────┐     ┌──────────▼──────────┐
                     │  memory/*.md    │     │   EDI tool call      │
                     │  topic exports  │     │   "query_memory"     │
                     └─────────────────┘     └─────────────────────┘
```

---

## 3. Database

**Host:** edi-base (Intel NUC, 32GB RAM, always-on)
**Engine:** PostgreSQL 16 + pgvector 0.6.0 (max 16,000 dims)
**Database:** `edi_memory`

### 3.1 Schema

```sql
-- ENUMs
CREATE TYPE provider_type     AS ENUM ('openai', 'anthropic');
CREATE TYPE message_role      AS ENUM ('system', 'user', 'assistant', 'tool');
CREATE TYPE processing_status AS ENUM ('raw', 'summarized', 'synthesized', 'embedded');

-- Conversations
CREATE TABLE conversations (
    id                UUID PRIMARY KEY,       -- provider's native UUID
    provider          provider_type NOT NULL,
    title             TEXT,
    summary           TEXT,                   -- raw provider summary (Anthropic) or NULL
    summary_source    TEXT,                   -- 'anthropic_native' | 'generated' | NULL
    created_at        TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ,            -- used for incremental dedup
    message_count     INT,
    total_tokens      INT,
    processing_status processing_status DEFAULT 'raw',
    topic_tags        TEXT[],                 -- populated by Pass 2
    first_imported_at TIMESTAMPTZ DEFAULT NOW(),
    last_exported_at  TIMESTAMPTZ
);

-- Messages
CREATE TABLE messages (
    id              UUID PRIMARY KEY,         -- provider's native message UUID
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    parent_id       UUID REFERENCES messages(id), -- tree structure (OpenAI branches)
    role            message_role NOT NULL,
    content         TEXT,
    model           TEXT,                     -- per-message (NULL for user messages)
    created_at      TIMESTAMPTZ,
    message_index   INT,
    token_count     INT,
    is_branch       BOOLEAN DEFAULT FALSE     -- marks alternate generations (retries)
);

-- Summaries (separate for re-runnability)
CREATE TABLE conversation_summaries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    summary         TEXT NOT NULL,
    key_topics      TEXT[],
    embedding       vector(1024),             -- Octen-0.6B dims for NUC
    generated_by    TEXT,                      -- model that produced summary
    generated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Full-text search support (keyword + IDF)
ALTER TABLE conversation_summaries ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', summary)) STORED;

CREATE INDEX idx_summaries_tsv ON conversation_summaries USING gin(tsv);
CREATE INDEX idx_summaries_embedding ON conversation_summaries
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### 3.2 Hybrid Search Query Pattern

```sql
-- Reciprocal Rank Fusion (RRF) of vector + keyword results
WITH vector_results AS (
    SELECT conversation_id, summary,
           ROW_NUMBER() OVER (ORDER BY embedding <=> $1) AS vrank
    FROM conversation_summaries
    ORDER BY embedding <=> $1
    LIMIT 30
),
keyword_results AS (
    SELECT conversation_id, summary,
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(tsv, query) DESC) AS krank
    FROM conversation_summaries, plainto_tsquery('english', $2) query
    WHERE tsv @@ query
    LIMIT 30
)
SELECT COALESCE(v.conversation_id, k.conversation_id) AS conversation_id,
       COALESCE(v.summary, k.summary) AS summary,
       (1.0/(60+COALESCE(vrank,999))) + (1.0/(60+COALESCE(krank,999))) AS rrf_score
FROM vector_results v
FULL OUTER JOIN keyword_results k USING (conversation_id)
ORDER BY rrf_score DESC
LIMIT 10;
```

IDF weighting comes free via `ts_rank_cd` (cover density with inverse document frequency normalization). The `tsvector` column is auto-maintained by the `GENERATED ALWAYS` clause.

---

## 4. Import Pipeline

### 4.1 Source Formats

**Anthropic:** Flat structure. Clean UUIDs, timestamps, built-in summaries.
```
{uuid, name, summary, created_at, updated_at, chat_messages: [{uuid, text, sender, created_at, ...}]}
```

**OpenAI:** Tree structure. Messages in `mapping` dict with parent/child pointers. Multiple branches from retries/model switches. Must walk tree via `current_node`.
```
{id, title, create_time, update_time, mapping: {node_id: {id, message, parent, children}}, current_node}
```

### 4.2 Incremental Dedup Strategy

On each import run:

1. **Load all conversation UUIDs + `updated_at` from DB**
2. **For each conversation in export:**
   - **New** (UUID not in DB): Insert conversation + all messages
   - **Updated** (`updated_at` > DB's `updated_at`): Diff messages by UUID, insert new ones, update conversation metadata
   - **Unchanged**: Skip entirely
3. **Track import run** in a `import_runs` table for auditability

```sql
CREATE TABLE import_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        provider_type NOT NULL,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    conversations_new     INT DEFAULT 0,
    conversations_updated INT DEFAULT 0,
    conversations_skipped INT DEFAULT 0,
    messages_new          INT DEFAULT 0,
    source_file           TEXT
);
```

### 4.3 OpenAI Tree Handling

- Walk from `current_node` backwards via `parent` to reconstruct the "chosen" conversation path
- Also import branch nodes with `is_branch = TRUE` for completeness
- Extract `model` from `message.metadata.model_slug` per message
- Filter out hidden system messages (`is_visually_hidden_from_conversation`)
- Handle null `create_time` on some messages (use parent's time or conversation's `create_time`)

---

## 5. Processing Pipeline

### 5.1 Pass 1 — Per-Chat Summarization

**Input:** Raw messages from one conversation
**Output:** Structured summary + key topics → `conversation_summaries` table
**Model options:**
- `claude-sonnet-4-5` — auto-switches to extended context, up to 1M tokens
- `gpt-5.2` — 400K context window
- `gpt-4.1` — 1M context window
**Skip if:** Anthropic chat with `summary_source = 'anthropic_native'` (use as-is, just embed)

Prompt template:
```
Summarize this conversation between a user and an AI assistant.
Focus on:
- What was discussed (topics, questions, decisions)
- What the user revealed about their thinking, preferences, or expertise
- Any notable opinions, reactions, or personality traits
- Outcomes or conclusions reached

Output JSON: {summary: str, key_topics: str[]}
```

**Execution via Batch API** (50% cost reduction):

Both providers offer async batch processing for high-volume, non-time-sensitive work:

- **OpenAI Batch API:** Submit JSONL file of requests, poll for completion (up to 24h SLA). Docs: https://developers.openai.com/cookbook/examples/batch_processing.md
- **Anthropic Message Batches:** Similar model — submit batch, poll for results. Docs: https://platform.claude.com/docs/en/api/python/messages/batches/create.md

**Validation-first approach:** Before bulk processing:
1. Submit a single chat summary request via Batch API
2. Poll until complete
3. Verify the response parses correctly and meets quality bar
4. Only then scale to full corpus

**Batch sizing:** ~854 chats (minus Anthropic natives with summaries). Estimate ~400-600 need generated summaries.

### 5.2 Pass 2 — Cross-Chat Synthesis

**Input:** Batches of Pass 1 summaries, clustered by topic
**Output:** Topic-level narratives → `memory/*.md` files
**Model:** Opus (needs nuance for pattern recognition)

Steps:
1. Cluster summaries by `key_topics` overlap (semantic or keyword-based)
2. For each cluster (20-50 summaries): generate a topic narrative
3. Track opinion evolution: "In March said X, by June shifted to Y"
4. Output as chronologically-organized markdown

**Suggested topic files:**
- `memory/nexus.md` — Project decisions, architecture, what worked
- `memory/career.md` — Military, psychiatry, fellowship, clinical work
- `memory/cooking.md` — Recipes, techniques, preferences
- `memory/tech-opinions.md` — Model evals, tool preferences, ELO findings
- `memory/interests.md` — Keyboards, retrofuturism, investing, gaming, classical lit
- `memory/people.md` — Relationship context, people mentioned
- `memory/medical.md` — Clinical knowledge, forensic psych, AR 706 evals

(Topics will emerge from the data — this list is a starting hypothesis.)

### 5.3 Pass 3 — Preference & Personality Extraction

**Input:** Sampled raw conversations (not summaries — needs tone/reasoning)
**Output:** Updated `USER.md` and `MEMORY.md` enrichment
**Model:** Opus

Extract:
- Communication style and patterns
- Decision-making approach
- Aesthetic preferences
- Humor style
- Knowledge domains and depth
- Recurring frustrations or delights

This pass runs on a **sample** (~50-100 raw conversations) to capture what summaries lose.

---

## 6. Embedding Model

| Model                | Params | Dims | Context | Memory | RTEB Mean |
| -------------------- | ------ | ---- | ------- | ------ | --------- |
| Octen-Embedding-0.6B | 0.6B   | 1024 | 32K     | 1.1GB  | 73.79     |

**Why this model:** #1 among NUC-feasible models by a wide margin (73.79 vs next-best ~57). Fits comfortably alongside PostgreSQL and OpenClaw on the NUC's 32GB RAM.

**Validation plan:** Before committing, run Octen-0.6B + `inf-retriever-v1-1.5b` on a sample of real conversation summaries with hand-crafted test queries. Measure retrieval accuracy on Neil's actual data.

---

## 7. Query Endpoint

A local HTTP service (or direct psql function) that EDI can call:

```
POST /query
{
  "query": "when did I first come up with the judo architecture?",
  "limit": 10,
  "mode": "hybrid"  // "vector" | "keyword" | "hybrid"
}

Response:
{
  "results": [
    {
      "conversation_id": "uuid",
      "title": "NEXUS spatial reasoning approach",
      "summary": "Discussed using PostGIS for...",
      "provider": "anthropic",
      "created_at": "2025-06-15T...",
      "score": 0.847
    }
  ]
}
```

Could be:
- A lightweight Flask/FastAPI service on edi-base
- A PostgreSQL function called via `psql` from a tool
- Integrated into OpenClaw as a custom skill

Decision deferred — depends on what's simplest to wire into EDI's tool chain.

---

## 8. Export to Markdown

Periodic job (manual or cron) that:
1. Runs Pass 2 synthesis on any new/updated summaries
2. Regenerates `memory/*.md` topic files
3. Updates `MEMORY.md` index with internal links:

```markdown
## Detailed Context
- [[memory/nexus|NEXUS Project History]]
- [[memory/career|Career & Military]]
- [[memory/cooking|Cooking]]
- [[memory/tech-opinions|Tech & Model Opinions]]
```

EDI reads `MEMORY.md` on boot (main session only) and can follow links to topic files as needed via `memory_search` / `memory_get`.

---

## 9. Implementation Plan

### Phase 1: Import + Storage
- [ ] Build import script (Python, handles both providers)
- [ ] Incremental dedup logic
- [ ] OpenAI tree walker
- [ ] Initial bulk import of 854 chats
- **Delegate to:** Claude Code or Codex

### Phase 2: Pass 1 Summarization
- [ ] Summarize all conversations (skip Anthropic native summaries)
- [ ] Generate embeddings with Octen-0.6B
- [ ] Populate `conversation_summaries` with embeddings + tsvectors
- **Delegate to:** Pipeline script + Sonnet API

### Phase 3: Hybrid Search
- [ ] Implement RRF query (vector + keyword + IDF)
- [ ] Build query endpoint (Flask/FastAPI or psql function)
- [ ] Wire into EDI's toolchain
- [ ] Validate retrieval quality on test queries

### Phase 4: Pass 2 + Export
- [ ] Topic clustering
- [ ] Cross-chat synthesis → `memory/*.md`
- [ ] `MEMORY.md` index generation
- **Delegate to:** Opus batches

### Phase 5: Pass 3 + Refinement
- [ ] Personality/preference extraction from sampled raw chats
- [ ] USER.md enrichment
- [ ] Iterative quality tuning

---

## 10. Open Questions

1. **Query endpoint format** — REST service vs psql function vs OpenClaw skill?
2. **Embedding model validation** — How many test queries for the A/B comparison?
3. **Pass 2 topic clustering** — Semantic clustering (embed topics, k-means) vs keyword overlap vs manual seeding?
4. **Refresh cadence** — How often will Neil generate new export dumps?
5. **Branch handling** — Index all OpenAI branches or just the chosen path? Branches contain interesting "roads not taken" data but inflate volume.
