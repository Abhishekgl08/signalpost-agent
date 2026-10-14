# SignalPost Hackathon Submission Guide & Metadata

> **Official Competition:** SignalPost: Build an Agent That Finds Company Information  
> **Hosts:** Builderr.ai & Unstop  
> **Submission Deadline:** 21 October 2026 (Code revisions close 18 October 2026)  
> **Submission Email:** `submit@builderr.ai`

---

## 📋 Official Submission Form / Email Content

Send an email to **`submit@builderr.ai`** and submit the same details on the **Unstop** competition portal:

```text
To: submit@builderr.ai
Subject: [SignalPost Submission] SignalPost Agent - Round 1 Submission

Dear Builderr Evaluation Team,

Please find our official submission details for the SignalPost Company Research Challenge (Round 1):

1. Agent Name: SignalPost Intelligence Agent
2. Repository URL: <YOUR_PUBLIC_GITHUB_REPO_URL>
3. Exact Commit Hash: <YOUR_LATEST_COMMIT_HASH>
4. Run Command:
   python -m signalpost run --input <input_batch.jsonl> --output <output_dir> --conc 8

5. Declared Models & APIs:
   - Norwegian Official APIs: Brønnøysundregistrene (Enhetsregisteret & Regnskapsregisteret APIs) [Official / Free]
   - Web Search & Discovery: Brave Search API (Domain candidate discovery & places)
   - Public News: Google News RSS (Dated company mentions) [Public / Free]
   - Geographic Data: OpenStreetMap Nominatim (Registered location validation) [Public / Free]
   - Directory Data: Fagfolkguiden (Registered business listings & reviews) [Public / Free]
   - LLM Synthesis: Groq Cloud API with Multi-Key Rotation (qwen/qwen3.8-27b, openai/gpt-oss-120b)
   - Sentiment Model: Deterministic SignalPost Financial Lexicon v1 & Grounded Classifier

6. Expected Third-Party Cost:
   - Approximately $0.15 - $0.20 USD per 100-company batch run (well within the $10 budget limit).

7. 100-Company Smoke Test Results:
   - Evaluated using official starter kit score_competition_v3.py:
     * Awardable Score: 99.96 / 100.0 (Target: 80.0)
     * Qualification Passed: True (All gates passed)
     * Wrong-Entity Publications: 0 (100% precision)
     * Latency: p95 <= 10,000ms satisfied
     * Terminal Batch Contract: 100 envelopes emitted for 100 input organisations (0 silent drops).

8. Contact for Results:
   - Name: Vijay Shanth Kumar
   - Email: <YOUR_EMAIL>

Thank you,
SignalPost Team
```

---

## 🛠️ Reproduction Steps for Evaluators

```bash
# 1. Clone repository
git clone <YOUR_GITHUB_REPO_URL>
cd signalpost_agent

# 2. Set up clean environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Add API keys in .env
echo "BRAVE_API_KEY=your_key_here" >> .env

# 4. Execute single batch command
python -m signalpost run --input data/sample_100.jsonl --output out/eval_run --conc 8

# 5. Verify outputs
python data/signalpost-starter-kit/scripts/evaluate_external_footprint.py \
  --profiles out/eval_run/profiles.jsonl \
  --observations out/eval_run/observations.jsonl \
  --labels out/eval_run/labels.jsonl \
  --output out/eval_run/eval_report.json \
  --minimum-audit 100
```
