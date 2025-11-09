# Agentic Chats Reporter

Reads Cursor or GitHub Copilot chat history and API usage stats, builds report with user task summaries and domain and tech requirements.

## Quick Start

Install dependencies:

```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
```

Create a `.env` file in the project root:

```bash
EMB_URL=http://your-embedding-api-url
EMB_MODEL=your-embedding-model
LLM_URL=http://your-llm-api-url
LLM_MODEL=your-llm-model
```

Run the complete pipeline with `EXAMPLE.md` and `EXAMPLE.csv` files:

```bash
python3 test.py
```

Or specify custom files:

```bash
python3 main.py --md-file your-chats.md --csv-file your-usage.csv
```

Run without usage stats (skips usage correlation):

```bash
python3 main.py --md-file your-chats.md
```

This runs all processing steps and generates a report file (`<md_name>-REPORT.md`).

## Data Export

### Export Your Chats

**This tool does NOT export chats for you.** You must export manually:

1. Use the [SpecStory extension](https://marketplace.visualstudio.com/items?itemName=SpecStory.specstory-vscode) to export chats from Cursor or GitHub Copilot
2. **Export all chats into a single markdown file** (required)
3. The exported file should follow the format shown in `EXAMPLE.md`

### Get Usage CSV (Optional)

Usage CSV is optional. If provided, it enables correlation analysis between chat messages and API usage:

1. Go to [Cursor Dashboard - Usage](https://cursor.com/dashboard?tab=usage)
2. Export your usage data as CSV
3. The CSV should contain columns: Date, Kind, Model, Max Mode, Input (w/ Cache Write), Input (w/o Cache Write), Cache Read, Output Tokens, Total Tokens, Cost

## Environment Setup

Optional variables:
```bash
EMB_API_KEY=your-embedding-api-key
LLM_API_KEY=your-llm-api-key
LLM_CONTEXT_LIMIT=32000
EMB_CONTEXT_LIMIT=32000
USER_TEXT_MAX_LINES=20
AGENT_TEXT_MAX_LINES=1
AGENT_COMMAND_MAX_LINES=3
USER_REPORT_MAX_LINES=15
AGENT_REPORT_MAX_LINES=10
CLUSTER_SEQUENCE_WEIGHT=1.0
CHAR_TOKEN_RATIO=3.6
EMB_MAX_RETRIES=3
EMB_RETRY_DELAY=1.0
LLM_MAX_RETRIES=3
LLM_RETRY_DELAY=1.0
SUMMARY_TEMPERATURE=0.3
SPEC_TEMPERATURE=0.3
DEDUP_TEMPERATURE=0.3
SUMMARY_SYSTEM_PROMPT=<custom prompt, see generate_group_summaries.py>
SPEC_SYSTEM_PROMPT=<custom prompt, see generate_specs.py>
DEDUP_SYSTEM_PROMPT=<custom prompt, see generate_specs.py>
PROMPT_EXTRA=<extra instructions appended to all system prompts, e.g., report language>
```

## Main Pipeline

**`main.py`** - Run complete analysis pipeline
- Parses chat markdown and usage CSV (optional)
- Correlates chats with usage data (if CSV provided)
- Extracts embeddings for user task message sequences
- Clusters user tasks by similarity into groups
- Generates LLM summaries for task groups
- Generates technical and domain specifications from group summaries
- Outputs statistics, specifications, and summaries to `<md_file>-REPORT.md`
- Streams progress information to stdout, writes only report content to output file

**Arguments:**
```bash
--md-file PATH    Path to chat markdown file (default: EXAMPLE.md)
--csv-file PATH   Path to usage CSV file (optional, enables usage correlation)
--output PATH     Output report file (default: <md_file_base>-REPORT.md)
--force           Force re-processing of all steps even if data already exists
```

**Environment Variables (required):**
- `EMB_URL` - OpenAI-compatible embedding API base URL
- `EMB_MODEL` - Embedding model name
- `LLM_URL` - OpenAI-compatible LLM API base URL
- `LLM_MODEL` - LLM model name

**Environment Variables (optional):**
- `EMB_API_KEY` - API key for embedding API (defaults to 'not-needed')
- `LLM_API_KEY` - API key for LLM API (defaults to 'not-needed')
- `LLM_CONTEXT_LIMIT` - Override LLM model context size in tokens (if not set, fetched from API)
- `EMB_CONTEXT_LIMIT` - Override embedding model context size in tokens (if not set, fetched from API)
- `CHAR_TOKEN_RATIO` - Character to token ratio for context size calculations (default: 3.6)
- `EMB_MAX_RETRIES` - Maximum retry attempts for embedding API calls (default: 3)
- `EMB_RETRY_DELAY` - Base delay in seconds for exponential backoff on embedding API retries (default: 1.0)
- `LLM_MAX_RETRIES` - Maximum retry attempts for LLM API calls (default: 3)
- `LLM_RETRY_DELAY` - Base delay in seconds for exponential backoff on LLM API retries (default: 1.0)
- `SUMMARY_TEMPERATURE` - Temperature for group summary generation (default: 0.3)
- `SPEC_TEMPERATURE` - Temperature for specification generation (default: 0.3)
- `DEDUP_TEMPERATURE` - Temperature for specification deduplication (default: 0.3)
- `PROMPT_EXTRA` - Extra instructions appended to the end of all system prompts (default: empty, e.g., report language, formatting preferences)

## Core Scripts

**`parse_chats.py`** - Parse exported chat markdown into SQLite database
- Creates `chats`, `messages`, and `content` tables
- Extracts chat titles, timestamps, message types, and content
- Truncates message summaries for embedding based on configurable line limits

```bash
python3 parse_chats.py md_file [--db-file PATH] [--max-lines N] [--max-chats N] [--max-messages N] [--start-chat N]
```

**Environment Variables:**
- `USER_TEXT_MAX_LINES` (default: 20)
- `AGENT_TEXT_MAX_LINES` (default: 1)
- `AGENT_COMMAND_MAX_LINES` (default: 3)

All these truncate messages for task summaries used later for embedding and thus task grouping.

---

**`parse_usage.py`** - Parse usage CSV into SQLite database
- Creates `usage` table
- Calculates TPS (Tokens Per Second) statistics

```bash
python3 parse_usage.py csv_file [--db-file PATH] [--force]
```

---

**`correlate_chats_usage.py`** - Correlate chat message sequences with API usage requests
- Groups user messages with their agent responses into task message sequences
- Matches task message sequences to usage API requests within time windows
- Calculates correlations between message content size and token counts

```bash
python3 correlate_chats_usage.py [--db-file PATH] [--usage-db-file PATH]
```

Uses two-pass matching: strict 10-minute window, then relaxed 2-hour window.

---

**`embed_tasks.py`** - Extract embeddings for message sequences
- Extracts user task message sequences with full user content and agent summaries
- Calls embedding API to generate embeddings with automatic retry logic (exponential backoff)
- Stores embeddings as base64-encoded, gzip-compressed strings
- Automatically deduplicates agent summaries if task exceeds context size
- Retries failed API calls with configurable retry count and delay

```bash
python3 embed_tasks.py [--db-file PATH]
```

**Environment Variables:**
- `EMB_CONTEXT_LIMIT` (optional) - Override embedding model context size in tokens (if not set, fetched from API)
- `EMB_MAX_RETRIES` (default: 3) - Maximum retry attempts for embedding API calls
- `EMB_RETRY_DELAY` (default: 1.0) - Base delay in seconds for exponential backoff

**Note:** Uses `task_builder.py` module to build tasks from chat data.

---

**`cluster_tasks.py`** - Cluster tasks by similarity using Agglomerative Hierarchical Clustering
- Uses agglomerative clustering with average linkage to ensure all tasks are grouped
- Computes pairwise distance matrix once and reuses it across operations
- Applies sequence-based penalty to cosine distance to preserve temporal order semantics
- Finds optimal distance threshold based on distance distribution (targets ~85% clustering ratio)
- Recursively splits large clusters until they fit within LLM context size (80% of model limit)
- Uses size-based splitting as fallback when clustering cannot split large groups
- Ensures 100% task coverage (no noise points left ungrouped)
- Generates markdown report with clustering statistics

```bash
python3 cluster_tasks.py [--db-file PATH] [--force]
```

**Environment Variables:**
- `LLM_CONTEXT_LIMIT` (optional) - Override LLM model context size in tokens (if not set, fetched from API)
- `CLUSTER_SEQUENCE_WEIGHT` (default: 0.3) - Weight for sequence distance penalty in clustering

---

**`generate_group_summaries.py`** - Generate LLM summaries for clustered task groups
- Loads clustered task groups from database
- Formats group content with all tasks ordered by timestamp and source markdown line number
- Calls LLM to generate concise user and agent summaries with titles
- Retries failed API calls with configurable retry count and exponential backoff delay
- Stores summaries in `group_summaries` database table
- Writes summaries incrementally to output file as they're generated
- Prepends a numbered table of contents with all group titles before the summaries

```bash
python3 generate_group_summaries.py [--db-file PATH] [--output PATH] [--force]
```

**Environment Variables:**
- `LLM_CONTEXT_LIMIT` (optional) - Override LLM model context size in tokens (if not set, fetched from API)
- `USER_REPORT_MAX_LINES` (default: 15) - Maximum lines for user summary
- `AGENT_REPORT_MAX_LINES` (default: 10) - Maximum lines for agent summary
- `SUMMARY_SYSTEM_PROMPT` (optional) - Custom system prompt for group summarization
- `PROMPT_EXTRA` (optional, default: empty) - Extra instructions appended to the end of system prompts (e.g., report language, formatting preferences)
- `SUMMARY_TEMPERATURE` (default: 0.3) - Temperature for LLM generation
- `LLM_MAX_RETRIES` (default: 3) - Maximum retry attempts for LLM API calls
- `LLM_RETRY_DELAY` (default: 1.0) - Base delay in seconds for exponential backoff

Limits are used to control verbosity of user and agent summaries in resulting report.

See default `SUMMARY_SYSTEM_PROMPT` in `generate_group_summaries.py`.

---

**`generate_specs.py`** - Generate technical and domain specifications from group summaries

```bash
python3 generate_specs.py [--db-file PATH] [--output PATH] [--force]
```

This script reads group summaries from the database and generates specifications iteratively:
- Processes summaries in batches that fill 50% of LLM context size
- Each batch includes existing specs and new summaries to extract new requirements
- Accumulates all requirements from all task groups (merges new requirements into existing specs)
- Automatically runs deduplication when specs size exceeds 25% of context size
- Performs final deduplication pass to merge and clean up requirements
- Retries failed API calls with configurable retry count and exponential backoff delay
- Outputs Technical requirements and Domain requirements sections
- Each spec contains first-level titled sections (##) with atomic requirements as bullet points
- Supports resuming from database (tracks processed summaries)

**Environment Variables:**
- `LLM_CONTEXT_LIMIT` (optional) - Override LLM model context size in tokens (if not set, fetched from API)
- `SPEC_SYSTEM_PROMPT` (optional) - Custom system prompt for specification generation
- `DEDUP_SYSTEM_PROMPT` (optional) - Custom system prompt for final deduplication pass
- `PROMPT_EXTRA` (optional, default: empty) - Extra instructions appended to the end of system prompts (e.g., report language, formatting preferences)
- `SPEC_TEMPERATURE` (default: 0.3) - Temperature for specification generation
- `DEDUP_TEMPERATURE` (default: 0.3) - Temperature for specification deduplication
- `LLM_MAX_RETRIES` (default: 3) - Maximum retry attempts for LLM API calls
- `LLM_RETRY_DELAY` (default: 1.0) - Base delay in seconds for exponential backoff

---

## Database Structure

- **`chats`** - Chat metadata (id, title, chat_datetime, start_line, end_line)
- **`messages`** - Message records (id, chat_id, message_type, message_datetime, summary, content_type, content_length, agent_summary, ...)
- **`content`** - Message content text (message_id, content_text)
- **`usage`** - Usage statistics (id, date, kind, model, tokens, cost, timestamp, ...)
- **`task_embeddings`** - Task embeddings (user_msg_id, embedding_data, message_count, formatted_length)
- **`task_groups`** - Clustering results (id, threshold, group_id, user_msg_id)
- **`group_summaries`** - Group summaries (group_id, title, user_summary, agent_summary, first_timestamp, task_count)
- **`specs`** - Generated specifications (id, specs_text, last_updated)
- **`processed_summaries`** - Tracks which group summaries have been processed for spec generation (group_id, processed_at)

## Testing

Test scripts are available for individual components:
```bash
python3 test_parse_chats.py
python3 test_parse_usage.py
python3 test_correlate_chats_usage.py
python3 test_embed_tasks.py
python3 test_cluster_tasks.py
```

## Debug/Utility Scripts

- `debug_long_tasks.py` - Analyze longest task summaries in database
- `show_similarity_matrix.py` - Display cosine similarity matrix for embeddings
- `analyze_group_continuity.py` - Analyze how consecutive tasks are within groups (shows gap statistics and continuity ratios)
- `task_builder.py` - Core module for building user task message sequences (used by other scripts)

All utility scripts support `--db-file PATH` argument (defaults to auto-detecting most recent *.db file).
