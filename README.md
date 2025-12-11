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

This runs all processing steps and generates a report file (`<md_file_base>-REPORT.md`).

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

### Required Variables

- `EMB_URL` - OpenAI-compatible embedding API base URL
- `EMB_MODEL` - Embedding model name
- `LLM_URL` - OpenAI-compatible LLM API base URL
- `LLM_MODEL` - LLM model name

### Optional Variables

**API Authentication:**
- `EMB_API_KEY` (default: 'not-needed') - API key for embedding API authentication
- `LLM_API_KEY` (default: 'not-needed') - API key for LLM API authentication

**Context Limits:**
- `EMB_CONTEXT_LIMIT` (optional) - Override embedding model context size in tokens (if not set, fetched from API)
- `LLM_CONTEXT_LIMIT` (optional) - Override LLM model context size in tokens for internal calculations (if not set, determined by `max_tokens` in params or fetched from API). This value is used for context limit calculations but is NOT sent as `max_tokens` parameter to the API (unless `max_tokens` is also set in params).

**LLM Context Limit Logic:**
- If `max_tokens` is set in `SUMMARY_PARAMS`, `SPEC_PARAMS`, or `DEDUP_PARAMS` and is > 0: use it as context limit (overall token limit: input + output) AND send it as `max_tokens` parameter to API
- If `max_tokens` is set to <= 0: retrieve context length from model API, use it as context limit AND send it as `max_tokens` parameter to API
- If `LLM_CONTEXT_LIMIT` is set: use it as context limit but do NOT send it as parameter (only send `max_tokens` if specified according to rules above)
- If `LLM_CONTEXT_LIMIT` is not set: retrieve context length from model API, use it as context limit, but do NOT send as parameter

**Note:** When `max_tokens` is passed to the LLM API, it represents the overall token limit (input + output tokens combined), not just the output limit.

**Retry Configuration:**
- `EMB_MAX_RETRIES` (default: 3) - Maximum retry attempts for embedding API calls
- `EMB_RETRY_DELAY` (default: 1.0) - Base delay in seconds for exponential backoff on embedding API calls
- `LLM_MAX_RETRIES` (default: 3) - Maximum retry attempts for LLM API calls
- `LLM_RETRY_DELAY` (default: 1.0) - Base delay in seconds for exponential backoff on LLM API calls

**Token Estimation:**
- `CHAR_TOKEN_RATIO` (default: 3.6) - Character to token ratio for token count estimation when API context size unavailable

**Prompt Customization:**
- `PROMPT_EXTRA` (optional, default: empty) - Extra instructions appended to the end of all system prompts (e.g., report language, formatting preferences)

**LLM request parameters**
- `SUMMARY_PARAMS` (optional, default: `{"temperature": 0.1}`) - JSON object with [OpenAI chat completions API parameters](https://platform.openai.com/docs/api-reference#chat#create-completion) for task group summaries generation.
- `SPEC_PARAMS` and `DEDUP_PARAMS` (optional, default: `{"temperature": 0.3}`) - JSON object with [OpenAI chat completions API parameters](https://platform.openai.com/docs/api-reference#chat#create-completion) for specification generation and deduplication.
- `TASK_GENERATION_PARAMS` and `UNIFY_PARAMS` (optional, default: `{"temperature": 0.3}`) - JSON object with [OpenAI chat completions API parameters](https://platform.openai.com/docs/api-reference#chat#create-completion) for task sequence generation and unification.

**Note:** All parameters (including `max_tokens`) are passed to the respective API calls according to the context limit logic described above.


**Example `.env` file:**
```bash
EMB_URL=http://your-embedding-api-url
EMB_MODEL=your-embedding-model
LLM_URL=http://your-llm-api-url
LLM_MODEL=your-llm-model
# Optional variables
EMB_API_KEY=your-embedding-api-key
LLM_API_KEY=your-llm-api-key
EMB_CONTEXT_LIMIT=32000
CHAR_TOKEN_RATIO=3.6
EMB_MAX_RETRIES=3
EMB_RETRY_DELAY=1.0
LLM_MAX_RETRIES=3
LLM_RETRY_DELAY=1.0
PROMPT_EXTRA=Generate report in English
SUMMARY_PARAMS='{"temperature": 0.1}'
SPEC_PARAMS='{"temperature": 0.3}'
DEDUP_PARAMS='{"temperature": 0.3}'
TASK_GENERATION_PARAMS='{"temperature": 0.3}'
UNIFY_PARAMS='{"temperature": 0.3}'
TASK_GENERATION_EXTRA=rewrite the project
DECISION_MEMORY_MAX_CHARS=2500
```

**Note:** Script-specific variables (message truncation limits, report line limits, clustering parameters, LLM parameters, custom prompts) are documented in their respective script sections below.

## Main Pipeline

**`main.py`** - Run complete analysis pipeline
- Parses chat markdown and usage CSV (optional)
- Correlates chats with usage data (if CSV provided)
- Extracts embeddings for user task message sequences
- Clusters user tasks by similarity into groups
- Generates LLM summaries for task groups
- Generates technical and domain specifications from group summaries
- Outputs statistics, specifications, and summaries to `<md_file_base>-REPORT.md`
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
- See "Environment Setup" section above for common optional variables
- See "Core Scripts" section below for script-specific environment variables

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
- `EMB_CONTEXT_LIMIT`, `EMB_MAX_RETRIES`, `EMB_RETRY_DELAY` (see Environment Setup section for descriptions)

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
- `CLUSTER_THRESHOLD` (optional) - Percentile value for threshold selection (default: 0.85). The threshold is calculated as the Nth percentile of consecutive task distances, where N = CLUSTER_THRESHOLD * 100. Must be a float between 0.0 and 1.0 (e.g., 0.85 for 85th percentile).
- `CLUSTER_MIN_GROUP_SIZE_RATIO` (optional) - Minimum group size ratio. If set, allows merging tasks into groups even when they would exceed max_size, as long as current_size < min_size (where min_size = max_size * ratio). Must be a float between 0 and 1 (e.g., 0.5).
- `SUMMARY_PARAMS` (optional) - JSON object with OpenAI API parameters. All parameters (including `max_tokens`) are passed to summary generation API calls. If `max_tokens` is set, it is used as the overall content size limit for clustering and prompt building, with coefficient 0.8 applied (no API fetch). If not set, LLM context size is fetched from API and coefficient 0.8 is applied to it.

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
- `LLM_MAX_RETRIES`, `LLM_RETRY_DELAY`, `PROMPT_EXTRA` (see Environment Setup section for descriptions)
- `USER_SUMMARY_MAX_TOKENS` (default: 480) - Maximum tokens for user summary output
- `AGENT_SUMMARY_MAX_TOKENS` (default: 320) - Maximum tokens for agent summary output
- `SUMMARY_SYSTEM_PROMPT` (optional) - Custom system prompt for group summarization. See default prompt in `generate_group_summaries.py`
- `SUMMARY_PARAMS` (optional) - JSON object with [OpenAI chat completions API parameters](https://platform.openai.com/docs/api-reference#chat#create-completion) (default: `{"temperature": 0.1`). See **LLM Context Limit Logic** above. Use `USER_SUMMARY_MAX_TOKENS` and `AGENT_SUMMARY_MAX_TOKENS` to control output length in summaries.

Limits are used to control verbosity of user and agent summaries in resulting report.

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
- `LLM_MAX_RETRIES`, `LLM_RETRY_DELAY`, `PROMPT_EXTRA` (see Environment Setup section for descriptions)
- `SPEC_SYSTEM_PROMPT` (optional) - Custom system prompt for specification generation. See default prompt in `generate_specs.py`
- `DEDUP_SYSTEM_PROMPT` (optional) - Custom system prompt for final deduplication pass. See default prompt in `generate_specs.py`
- `SPEC_PARAMS` (optional) - JSON object with [OpenAI chat completions API parameters](https://platform.openai.com/docs/api-reference#chat#create-completion) for specification generation (default: `{"temperature": 0.3}`).
- `DEDUP_PARAMS` (optional) - JSON object with [OpenAI chat completions API parameters](https://platform.openai.com/docs/api-reference#chat#create-completion) for specification deduplication (default: `{"temperature": 0.3}`).
- See **LLM Context Limit Logic** above. 

---

**`generate_task_sequence.py`** - Generate detailed task sequence from specifications and group summaries

```bash
python3 generate_task_sequence.py [--db-file PATH] [--output PATH] [--force]
```

This script generates a detailed, actionable task sequence for implementing or rewriting a project:
- Reads project specifications and task group summaries from database
- Generates numbered sequence of highly detailed tasks (200-500 words each)
- Each task includes specific implementation details: file names, function signatures, data structures, API endpoints, database schemas
- Uses decision memory to maintain consistent tech stack and architectural decisions across tasks
- Unifies tasks to ensure consistent architecture adhering to KISS, DRY, and SOLID principles
- Processes summaries in batches to fit within LLM context limits
- Automatically accounts for decision memory size in context window calculations
- Outputs task sequence with decision memory to `<db_file_base>-TASKS.md`

**Environment Variables:**
- `LLM_MAX_RETRIES`, `LLM_RETRY_DELAY`, `PROMPT_EXTRA` (see Environment Setup section for descriptions)
- `TASK_GENERATION_EXTRA` (optional, default: 'rewrite the project') - Description of what the task sequence should accomplish
- `INITIAL_DECISION_MEMORY` (optional, default: empty) - Initial decision memory with tech stack and architectural decisions
- `DECISION_MEMORY_MAX_CHARS` (optional, default: 2500 chars ≈ 500 words) - Maximum length for decision memory. If exceeded, decision memory is truncated with warning
- `TASK_GENERATION_SYSTEM_PROMPT` (optional) - Custom system prompt for task generation. See default prompt in `generate_task_sequence.py`
- `UNIFICATION_SYSTEM_PROMPT` (optional) - Custom system prompt for task unification. See default prompt in `generate_task_sequence.py`
- `TASK_GENERATION_PARAMS` (optional) - JSON object with [OpenAI chat completions API parameters](https://platform.openai.com/docs/api-reference#chat#create-completion) for task generation (default: `{"temperature": 0.3}`)
- `UNIFY_PARAMS` (optional) - JSON object with [OpenAI chat completions API parameters](https://platform.openai.com/docs/api-reference#chat#create-completion) for task unification (default: `{"temperature": 0.3}`)
- See **LLM Context Limit Logic** above

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
- **`task_sequences`** - Generated task sequences (id, sequence_text, decision_memory, last_updated)

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
- `task_builder.py` - Core module for building user task message sequences (used by other scripts)

All utility scripts support `--db-file PATH` argument (defaults to auto-detecting most recent *.db file).

## Utility Modules

The project includes shared utility modules to avoid code duplication:

- **`db_utils.py`** - Database operations (file finding, connection helpers, schema migration)
- **`llm_utils.py`** - LLM/API operations (client creation, retry logic, context size calculation, parameter defaults)
- **`embedding_utils.py`** - Embedding operations (compression/decompression, cosine similarity)
- **`common_utils.py`** - General utilities (progress reporting)
