# IDE Agent Stats - Implementation Requirements

## Main Pipeline Orchestration

- Orchestrate complete analysis pipeline from chat markdown and usage CSV input to final report output
- Accept command-line arguments for input file paths with default values (--md-file, --csv-file, --output)
- Automatically derive output report filename from input markdown filename (<md_file_base>-REPORT.md)
- Validate input file existence before processing
- Execute processing steps sequentially: parse chats → parse usage (optional) → correlate → embed → cluster → generate summaries → generate specs → generate task sequence (optional)
- Stream subprocess output to stdout in real-time for progress visibility
- Filter and capture relevant output sections from each processing step
- Append filtered output sections to report file in correct order
- Display high-level progress information to stdout for each step (Step X/Y)
- Handle subprocess errors and exit with appropriate error codes
- Generate report header with input file metadata (file size, line count)
- Append report sections with page break markers for document formatting
- Insert specifications before task summaries in final report
- Merge group summaries into final report file
- Clean up temporary files after report generation
- Support --force flag to re-process steps even if data already exists

## Chat Markdown Parsing

- Parse markdown files containing exported chat sessions
- Detect chat session boundaries using header pattern with title and datetime
- Extract chat metadata including title and timestamp
- Identify message boundaries using message header patterns
- Distinguish between user and agent message types
- Parse message timestamps from headers
- Extract message content from message blocks
- Handle continuation messages after message boundaries
- Detect and parse agent message content types (think, text, tool_call)
- Extract tool call metadata including tool type and name
- Extract summary text from tool call content when present
- Parse content blocks while respecting code block boundaries
- Track line numbers for all parsed entities
- Handle invalid or malformed blocks with error reporting
- Skip chats and messages based on optional limits
- Support incremental parsing by detecting and removing broken data
- Store parsed data in normalized database tables
- Calculate and store content length statistics
- Truncate user message content to configurable line limit (USER_TEXT_MAX_LINES)
- Truncate agent text message summaries to configurable line limit (AGENT_TEXT_MAX_LINES)
- Truncate agent command summaries to configurable line limit (AGENT_COMMAND_MAX_LINES)
- Generate statistics table with counts and length metrics
- Display parsing statistics after completion

## Usage CSV Parsing

- Parse CSV files containing API usage statistics
- Extract usage record fields including date, model, token counts, and cost
- Convert date strings to timestamps for temporal queries
- Store usage records in database with indexed timestamp field
- Calculate overall token statistics including totals and averages
- Compute tokens-per-second metrics for consecutive requests within time windows
- Calculate average TPS across multiple requests within windows
- Calculate overall TPS using total tokens divided by total time
- Support multiple time window sizes for TPS analysis
- Generate unified statistics table with all metrics
- Display usage statistics in formatted table after parsing

## Task Building

- Extract user messages with timestamps from database
- Group agent messages that follow each user message within same chat
- Extract full user message content text
- Extract agent message summaries preserving original content structure
- Build agent summaries from database fields when available
- Fall back to extracting summaries from content when database fields missing
- Handle agent messages with different content types appropriately
- Calculate task timestamps including start and end times
- Format tasks as text with user and agent sections
- Calculate task length metrics for context size checking
- Filter error messages from agent summaries
- Remove duplicate adjacent agent summaries using normalization
- Consolidate rapid file operation tasks to reduce redundancy
- Handle special cases like todo operations with frequency notation
- Apply window-based deduplication to prevent redundant summaries
- Support both batch task extraction and single task lookup
- Preserve task ordering by timestamp
- Calculate correlation-specific fields for usage matching

## Chat-Usage Correlation

- Match message tasks to API usage requests by timestamp proximity
- Use two-pass matching strategy with strict and relaxed time windows
- Apply strict matching window (10 minutes) with limited request sharing
- Apply relaxed matching window (2 hours) with more flexible sharing
- Track which usage requests are claimed by which tasks
- Prevent excessive sharing of usage requests across tasks
- Calculate correlation statistics between content size and token counts
- Compute correlations for different token types (input, output, total)
- Compute correlations for different content types (user, agent, text-only)
- Calculate daily statistics aggregating tasks and usage by date
- Identify unmatched tasks that have no corresponding usage
- Identify unmatched usage requests that have no corresponding tasks
- Generate daily breakdown table with matched and unmatched counts
- Calculate percentage metrics for matched vs unmatched data
- Generate correlation metrics table with correlation coefficients
- Report sample of unmatched requests with identifying information
- Support multiple usage database files by merging data
- Handle timestamp timezone conversions correctly

## Embedding Extraction

- Extract message tasks from database
- Format tasks as text with user content and agent summaries
- Call embedding API to generate vector embeddings for tasks
- Handle API authentication using configurable API keys
- Store embeddings in compressed format in database (base64-encoded, gzip-compressed)
- Reuse existing embeddings when available
- Update stored task metadata when needed
- Detect when task text exceeds embedding model context size
- Apply progressive deduplication when context overflow occurs
- Use maximum deduplication strategies to fit tasks within limits
- Track embedding extraction progress with periodic status updates
- Store task metadata including message count and formatted length
- Support fetching model context size from API
- Support environment variable override for embedding model context size (EMB_CONTEXT_LIMIT, in tokens)
- Estimate token counts when API context size unavailable (using CHAR_TOKEN_RATIO)
- Calculate cosine similarity between embeddings for clustering

## Task Clustering

- Load embeddings and task metadata from database
- Compute pairwise distance matrix for all tasks once and reuse across operations
- Calculate cosine distances between consecutive task embeddings only (O(n) complexity)
- Find optimal clustering threshold using percentile of consecutive distances (default: 0.85 for 85th percentile, configurable via CLUSTER_THRESHOLD)
- Support minimum group size ratio to allow merging even when groups would exceed max_size (CLUSTER_MIN_GROUP_SIZE_RATIO)
- Use Sequential Clustering: merge consecutive tasks if similar (distance <= threshold) and group fits within size limit
- Ensure 100% task coverage (all tasks assigned to groups)
- Recursively split clusters that exceed LLM context size limits (80% of overall limit)
- Use `max_tokens` from `SUMMARY_PARAMS` as overall content size limit if provided, apply 0.8 coefficient to it (no API fetch). Otherwise fetch LLM context size from API and apply 0.8 coefficient to it
- Apply recursive splitting with tighter thresholds when needed
- Use size-based splitting as fallback when clustering cannot split large groups
- Prevent infinite recursion with depth limits and minimum thresholds
- Store clustering results in database with group assignments (threshold = -1.0 for final groups)
- Generate clustering statistics including group counts and sizes
- Calculate group size statistics (min, avg, max tasks per group)
- Calculate group content length statistics (min, avg, max characters)
- Generate markdown report with clustering results
- Display clustering progress during recursive operations
- Handle edge cases like single-task groups and empty datasets

## Group Summarization

- Load clustered task groups from database
- Order tasks within groups by timestamp and line number
- Format group content with all tasks for LLM processing
- Call LLM API to generate summaries for each group
- Handle API authentication using configurable API keys
- Support configurable OpenAI chat completions API parameters via JSON environment variable (SUMMARY_PARAMS, defaults: temperature=0.1, frequency_penalty=1.3). See OpenAI API documentation for available parameters
- Generate 5-7 word title for each group
- Generate user request summary in imperative voice (up to USER_SUMMARY_MAX_TOKENS tokens)
- Generate agent actions summary in past tense (up to AGENT_SUMMARY_MAX_TOKENS tokens)
- Include all tasks from group in summaries without omission
- Enforce maximum token limits for summaries via API max_tokens parameter
- Use configurable system prompt for summarization (SUMMARY_SYSTEM_PROMPT)
- Support custom prompt via environment variable
- Parse LLM response to extract title, user, and agent sections
- Store summaries in group_summaries database table
- Track summarization progress with periodic updates
- Order groups by first message timestamp in output
- Generate numbered table of contents before summaries
- Generate markdown report with numbered group summaries
- Format group titles with timestamps
- Write summaries incrementally to output file
- Normalize blank lines in formatted content

## Specification Generation

- Load group summaries from database ordered by timestamp
- Load existing specs and processed group IDs from database to support resuming
- Process summaries in batches that fill 50% of LLM context size
- Each batch includes existing specs and new summaries to extract new requirements
- Accumulate all requirements from all task groups by merging new requirements into existing specs
- Merge requirements from each batch response into accumulated specs (not replace)
- Automatically run deduplication when specs size exceeds 25% of context size
- Use `max_tokens` from `SPEC_PARAMS` and `DEDUP_PARAMS` as overall content size limit if provided (no API fetch). Otherwise use LLM context size from API (automatically detected)
- Generate technical requirements (what technology the project uses and how)
- Generate domain requirements (what user value the project delivers and how)
- Output specs with first-level titled sections (##) for different categories
- List atomic requirements as bullet points under each category
- Only extract NEW requirements not already covered in existing specs
- Merge new requirements properly by combining sections and categories
- Perform final deduplication pass to merge and clean up requirements
- Save specs and processed group IDs to database after each batch
- Support resuming from database (skip already processed summaries)
- Support --force flag to clear existing specs and reprocess all summaries
- Support custom prompts via SPEC_SYSTEM_PROMPT and DEDUP_SYSTEM_PROMPT environment variables
- Support configurable OpenAI chat completions API parameters via JSON environment variables (SPEC_PARAMS, DEDUP_PARAMS, defaults: temperature=0.3). See OpenAI API documentation for available parameters
- Track specification generation progress with periodic updates
- Output specs before task summaries in final report

## Task Sequence Generation

- Load project specifications and group summaries from database
- Generate detailed, actionable task sequence for implementing or rewriting project
- Each task must be 200-500 words with specific implementation details
- Include file names, function signatures, data structures, API endpoints, database schemas in each task
- Use decision memory to maintain consistent tech stack and architectural decisions
- Process summaries in batches that fit within LLM context limits (50% of context for summaries)
- Account for decision memory size in all context window calculations
- Generate tasks that build logically upon previous tasks (dependencies first)
- Unify task sequence to ensure consistent architecture adhering to KISS, DRY, and SOLID principles
- Design compound architecture: cohesive modules with clear boundaries, well-defined interfaces, logical composition
- Update decision memory with new architectural decisions during unification
- Preserve full detail and size of each task during unification (do not compress)
- Support configurable task generation goal via TASK_GENERATION_EXTRA environment variable
- Support initial decision memory via INITIAL_DECISION_MEMORY environment variable
- Enforce decision memory length limit (default: 2500 chars ≈ 500 words, configurable via DECISION_MEMORY_MAX_CHARS)
- Truncate decision memory with warning if it exceeds limit during extraction
- Store task sequence and decision memory in database (task_sequences table)
- Support resuming from database (skip generation if sequence exists, unless --force flag used)
- Support custom prompts via TASK_GENERATION_SYSTEM_PROMPT and UNIFICATION_SYSTEM_PROMPT environment variables
- Support configurable OpenAI chat completions API parameters via JSON environment variables (TASK_GENERATION_PARAMS, UNIFY_PARAMS, defaults: temperature=0.3). See OpenAI API documentation for available parameters
- Handle large task sequences by processing in chunks when needed
- Track task generation progress with periodic updates
- Output task sequence with decision memory to markdown file

## Database Schema

- Store chat metadata in normalized table structure
- Store message records with type, timestamp, and content metadata
- Store message content text in separate table for efficiency
- Support foreign key relationships between tables
- Index timestamp fields for efficient temporal queries
- Support incremental schema updates without data loss
- Store task embeddings with compression (base64-encoded, gzip-compressed)
- Store clustering group assignments with threshold metadata (threshold = -1.0 for final groups)
- Store group summaries in group_summaries table (group_id, title, user_summary, agent_summary, first_timestamp, task_count)
- Store generated specifications in specs table (id, specs_text, last_updated)
- Store processed summaries tracking in processed_summaries table (group_id, processed_at)
- Store task sequences and decision memory in task_sequences table (id, sequence_text, decision_memory, last_updated)
- Store usage statistics with indexed timestamp field
- Track line numbers for parsed entities
- Support querying by chat, message, and task relationships

## Output and Reporting

- Generate markdown-formatted report files
- Include input file metadata at report start (file size, line count)
- Format statistics tables with proper markdown syntax
- Include page break markers for document formatting before major sections
- Separate report sections with clear headers
- Display progress information to stdout during processing
- Filter relevant output sections from subprocess execution
- Preserve formatting of statistics tables in report
- Generate correlation reports with daily breakdowns
- Generate clustering reports with group statistics
- Generate specifications before task summaries (Domain requirements and Technical requirements)
- Generate task summaries with numbered groups and table of contents
- Include timestamps in group summaries
- Format numeric values with appropriate precision and separators
- Support custom output file paths
- Output only meaningful report content to markdown file, keep progress/utility info on stdout

## Error Handling and Validation

- Validate input file existence before processing
- Validate required environment variables are set
- Handle database connection errors gracefully
- Handle API errors with informative messages and retry logic
- Retry embedding API calls with exponential backoff on failures
- Retry LLM API calls with exponential backoff on failures
- Support configurable retry counts and delays for API calls
- Report parsing errors for malformed blocks
- Handle missing data gracefully with defaults
- Validate timestamp formats and handle conversion errors
- Handle empty datasets without errors
- Provide meaningful error messages for configuration issues
- Exit with appropriate error codes on failures

## Configuration and Environment

- Support environment variables for API endpoints and models (EMB_URL, EMB_MODEL, LLM_URL, LLM_MODEL)
- Support optional API keys for authentication (EMB_API_KEY, LLM_API_KEY, default: 'not-needed')
- Support configurable context limits via environment (EMB_CONTEXT_LIMIT, LLM_CONTEXT_LIMIT, in tokens, override API-fetched values)
- LLM context limit logic: If `max_tokens` is set in `SUMMARY_PARAMS`, `SPEC_PARAMS`, or `DEDUP_PARAMS` and is > 0, use it as context limit (overall token limit: input + output) and send it as parameter. If `max_tokens` is set to <= 0, retrieve context length from API, use it as context limit and send it as parameter. If `LLM_CONTEXT_LIMIT` is set, use it as context limit but do not send it as parameter (only send `max_tokens` if specified). If `LLM_CONTEXT_LIMIT` is not set, retrieve context length from API, use it as context limit, but do not send as parameter. Note: When `max_tokens` is passed to the LLM API, it represents the overall token limit (input + output tokens combined), not just the output limit.
- Support configurable truncation limits via environment (USER_TEXT_MAX_LINES, AGENT_TEXT_MAX_LINES, AGENT_COMMAND_MAX_LINES)
- Support configurable summary token limits via environment (USER_SUMMARY_MAX_TOKENS, AGENT_SUMMARY_MAX_TOKENS)
- Support configurable clustering parameters via environment (CLUSTER_THRESHOLD, CLUSTER_MIN_GROUP_SIZE_RATIO)
- Support configurable character to token ratio via environment (CHAR_TOKEN_RATIO, default: 3.6)
- Support configurable retry parameters via environment (EMB_MAX_RETRIES, EMB_RETRY_DELAY, LLM_MAX_RETRIES, LLM_RETRY_DELAY)
- Support custom LLM prompts via environment variables (SUMMARY_SYSTEM_PROMPT, SPEC_SYSTEM_PROMPT, DEDUP_SYSTEM_PROMPT, TASK_GENERATION_SYSTEM_PROMPT, UNIFICATION_SYSTEM_PROMPT)
- Support PROMPT_EXTRA for additional instructions appended to all system prompts
- Support task generation configuration via environment (TASK_GENERATION_EXTRA, INITIAL_DECISION_MEMORY, DECISION_MEMORY_MAX_CHARS)
- Support configurable OpenAI chat completions API parameters for task generation via JSON environment variables (TASK_GENERATION_PARAMS, UNIFY_PARAMS, defaults: temperature=0.3)
- Load configuration from .env file
- Use default values when environment variables not set
- Support command-line arguments for file paths
- Support database path specification via --db-file argument (consistent across all scripts)
- Auto-detect database files when not specified (searches for most recent *.db file)

## Utility and Debug Tools

- Analyze longest tasks in database by character count
- Display detailed breakdown of task composition
- Show message-level statistics for tasks
- Display task content previews for debugging
- Calculate and display cosine similarity matrix for embeddings
- Show pairwise similarities between all tasks
- Display task metadata including timestamps and content previews
- Support configurable limits for analysis output

## Code Organization

- Shared utility modules eliminate duplicate logic across scripts
- `db_utils.py` provides database file finding, connection helpers, and schema migration utilities
- `llm_utils.py` provides LLM/API client creation, retry logic, context size calculation, and parameter defaults
- `embedding_utils.py` provides embedding compression/decompression and cosine similarity calculations
- `common_utils.py` provides general utilities like progress reporting
- All scripts use consistent database file auto-detection with default error messages
- Error messages and help text are centralized in utility modules to avoid duplication
