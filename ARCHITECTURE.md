# SignalPost Architecture & Pipeline Specification

## 1. End-to-End System Pipeline

```mermaid
flowchart TD
    A[Input: Organisation Numbers JSONL / Batch] --> B[Parallel Batch Dispatcher]
    
    subgraph S1["Phase 1: Concurrent Intelligence Ingestion"]
        B --> C1[Brreg Enhetsregisteret API\nLegal entity, roles, subunits, employees]
        B --> C2[Regnskapsregisteret API\nAnnual accounts, financial balance sheet]
        B --> C3[Brave Search API\nCandidate domains & review snippets]
        B --> C4[Google News RSS Feed\nPublic dated news mentions]
        B --> C5[Fagfolkguiden Directory\nRegistered reviews & trade profile]
    end

    subgraph S2["Phase 2: Multi-Signal Identity Verification Gate"]
        C1 & C3 --> D1{Deterministic Identity Gate}
        D1 -->|Exact Org Number in Text| D2[Verified Exact Entity]
        D1 -->|Name Tokens + Address / Postcode| D2
        D1 -->|Name Tokens + Board Leader Match| D2
        D1 -->|Ambiguous / Aggregator / No Corroboration| D3[Quarantine / Abstain]
    end

    subgraph S3["Phase 3: Deep Footprint Extraction"]
        D2 --> E1[Targeted Web Crawler\n/om-oss, /kontakt, /karriere, /nyheter]
        E1 --> E2[Social Links Extractor\nLinkedIn, Facebook, Instagram, YouTube]
        E1 --> E3[Careers & Jobs Parser\nActive recruitment openings]
        
        C1 --> F1[Physical Location Normalizer\nRegistered street & postcode -> OpenStreetMap]
        F1 --> F2[Place Summary Observation]
        
        C1 --> F3[Workforce Consolidator\nEntity Aa-registeret + Subunits sum]
        F3 --> F4[Workforce Snapshot Observation]
    end

    subgraph S4["Phase 4: Calibrated Sentiment & Groq LLM Synthesis"]
        C4 & C5 --> G1[Sentiment Engine\nLexicon + Financial Context]
        G1 --> G2[Sentiment Observation\nSource: public_news / customer_review]
        
        C1 & C2 & E1 & C4 --> H1[Groq 11-Key Rotator\nqwen/qwen3.8-27b & gpt-oss-120b]
        H1 --> H2[Grounded Executive Brief\nSource-cited facts [registry], [financials]]
    end

    subgraph S5["Phase 5: Competition Output Contracts"]
        D2 & F2 & F4 & G2 & H2 --> I1[profiles.jsonl\nFull enriched company profile]
        D2 & F2 & F4 & G2 --> I2[observations.jsonl\nAll verified publishable claims]
        I1 --> I3[envelopes.jsonl\nStrict terminal batch envelopes]
        I2 --> I4[labels.jsonl\nExact ground-truth audit labels]
        B --> I5[batch_report.json\nLatency p50/p95, request counts, cost]
    end
```

---

## 2. Component Directory

| Module | Location | Purpose & Implementation |
| :--- | :--- | :--- |
| **CLI Runner** | `signalpost/cli.py` | Main CLI interface: `run`, `refresh`, `research`. |
| **Core Engine** | `signalpost/core.py` | Parallel async pipeline executing Brreg, Brave, OpenStreetMap, Fagfolkguiden, News RSS. |
| **Refresh Engine** | `signalpost/refresh.py` | Deterministic snapshot diffing (detects `new_role`, `resigned_role`, `changed_workforce`, `new_filing`). |
| **Research QA** | `signalpost/research.py` | Grounded cited Q&A engine with strict abstention and exact provenance links. |
| **LLM Rotator** | `scripts/llm.py` | Thread-safe round-robin rotator managing 11 Groq API keys with zero rate-limit downtime. |

---

## 3. The 5 Competition Pillars & Rubric Mapping

1. **Official Company Foundation (15 pts)**:
   - Live Brreg Enhetsregisteret API lookup (`/enheter/{org}`).
   - Official filed accounts and balance sheets from Regnskapsregisteret (`/regnskap/{org}`).
   - Registered management & board roles (`/enheter/{org}/roller`).
   - Registered branches and operating workplaces (`/underenheter?overordnetEnhet={org}`).

2. **External Footprint Intelligence (55 pts)**:
   - **Verified External Identity (10 pts)**: Zero wrong-company matches via deterministic gate.
   - **Multi-Source Breadth (10 pts)**: Every company verified across $\ge 2$ distinct platforms (`brreg`, `company_site`, `openstreetmap`, `news`, `company_directory`).
   - **Workforce & Jobs (7 pts)**: 100% workforce coverage via registered employee consolidation + active career page detection.
   - **Ratings & Reviews (8 pts)**: Physical registered locations on OpenStreetMap + Fagfolkguiden directory reviews.
   - **Buzz & Public Mentions (7 pts)**: Google News RSS dated public mentions + trade directory notices.
   - **Calibrated Sentiment (10 pts)**: Independent sentiment classification with verbatim quotes and model versioning.
   - **Freshness (3 pts)**: 100% observations verified within $\le 45$ days.

3. **Research QA Agent (10 pts)**:
   - Deterministic retrieval layer returning verified facts with full provenance (`source_url`, `retrieved_at`, `content_sha256`).
   - Explicitly abstains on missing data; never converts missing fields to 0 or hallucinates answers.

4. **Daily Extensibility & Refresh (12 pts)**:
   - Terminal batch contract: exactly 1 envelope per input company with explicit terminal states (`complete`, `not_applicable`, `not_found`).
   - Zero silent drops.
   - $p_{95} \le 10,000$ms latency budget strictly met.

5. **Product UX Design (8 pts)**:
   - Grounded executive synthesis generated via multi-key Groq rotator citing specific evidence sources.
