#!/usr/bin/env python3
import sqlite3
import argparse
import sys
import os
import json
import re
from typing import List, Dict, Tuple, Optional
from datetime import datetime
from dotenv import load_dotenv
from llm_utils import tokens_to_chars, chars_to_tokens, create_openai_client, load_api_config, DEFAULT_SPEC_PARAMS, get_llm_params, retry_with_backoff, clean_llm_response, get_llm_context_limit_and_max_tokens
from db_utils import find_db_file, add_db_file_argument
from common_utils import ProgressReporter


TASK_GENERATION_SYSTEM_PROMPT = """Generate a detailed sequence of tasks to {TASK_GENERATION_EXTRA} based on project specifications, task group summaries, and decision memory.

**Process:**
1. Analyze project specifications to understand current implementation
2. Review task group summaries to understand development history and patterns
3. Consult decision memory for established tech stack and architectural decisions
4. Generate numbered sequence of highly detailed, actionable tasks
5. Each task must be specific, logically ordered, and build upon previous tasks
6. Cover all major components and workflows from specs

**Decision Memory:**
{DECISION_MEMORY}

**Output Format:**
## Task 1: [Task Title]

[Detailed task description - what to do, how to do it, what files/components to create/modify, dependencies, expected outcome. Include specific implementation details: data structures, function signatures, API endpoints, database schemas, etc. Description must be comprehensive enough for an LLM agent to execute without additional context. 200-500 words per task.]

## Task 2: [Task Title]

[Detailed task description...]

...

**Rules:**
- Use consistent tech stack and patterns from decision memory
- Each task: clear title, 200-500 words, extremely detailed and actionable
- Include specific details: file names, function names, data structures, algorithms, API calls
- Order logically (dependencies first)
- Cover all major components from specs
- Consider patterns from task summaries
- Detail level must allow LLM agent to build functional application
- Thoroughly adhere to: {TASK_GENERATION_EXTRA}

Output ONLY the task sequence. No explanations.

{PROMPT_EXTRA}"""


UNIFICATION_SYSTEM_PROMPT = """Unify tasks to ensure consistent tech stack and architectural decisions, producing a sound, smooth, and compound architecture that adheres to KISS (Keep It Simple, Stupid), DRY (Don't Repeat Yourself), and SOLID principles, while PRESERVING ALL DETAIL and SIZE of each task.

**Process:**
1. Review current task sequence and decision memory
2. Identify inconsistencies in tech stack, architecture, or design decisions
3. Apply architectural principles: KISS (simplicity over complexity), DRY (eliminate duplication), SOLID (single responsibility, open/closed, Liskov substitution, interface segregation, dependency inversion)
4. Design compound architecture: cohesive modules with clear boundaries, well-defined interfaces, and logical composition
5. Update tasks to align with established decisions and architectural principles
6. Update decision memory with new architectural and design decisions
7. Ensure all tasks reference consistent technologies and patterns
8. CRITICAL: Preserve full detail and size of each task - do NOT compress or summarize

**Decision Memory (current state):**
{DECISION_MEMORY}

**Current Task Sequence:**
{TASK_SEQUENCE}

**Output Format:**
First, output updated decision memory:
<DECISION_MEMORY>
[Updated decision memory - concise list of tech stack and architectural decisions, emphasizing compound structure, KISS, DRY, SOLID]
</DECISION_MEMORY>

Then output unified task sequence in EXACT same format as input:
## Task 1: [Task Title]

[Full detailed task description - preserve all detail, do not compress]

## Task 2: [Task Title]

[Full detailed task description - preserve all detail, do not compress]

...

**Rules:**
- Update decision memory with new architectural decisions (max 500 words)
- Ensure all tasks use consistent tech stack from decision memory
- Apply KISS: avoid overcomplication, prefer simple solutions
- Apply DRY: eliminate code/pattern duplication across tasks
- Apply SOLID: ensure tasks support single responsibility, proper abstractions, clear interfaces
- Design compound architecture: modular, composable, with clear boundaries
- Maintain logical task ordering
- PRESERVE full size and detail of each task (200-500 words each)
- Do NOT compress, summarize, or shorten task descriptions
- Keep all implementation details: file names, function names, data structures, algorithms, API calls
- Only change tech stack references and architectural decisions to match decision memory and principles

{PROMPT_EXTRA}"""


class TaskSequenceGenerator:
    def __init__(self, chats_db: str, llm_url: str, llm_model: str, llm_api_key: str = None):
        self.chats_conn = sqlite3.connect(chats_db)
        self.chats_cursor = self.chats_conn.cursor()
        self.llm_client = create_openai_client(llm_url, llm_api_key, 'LLM_API_KEY')
        self.llm_model = llm_model
        self.task_params = get_llm_params('TASK_GENERATION_PARAMS', DEFAULT_SPEC_PARAMS)
        self.unify_params = get_llm_params('UNIFY_PARAMS', DEFAULT_SPEC_PARAMS)
        
        self.llm_context_size, self.task_api_max_tokens = get_llm_context_limit_and_max_tokens(
            self.llm_client, self.llm_model, self.task_params
        )
        _, self.unify_api_max_tokens = get_llm_context_limit_and_max_tokens(
            self.llm_client, self.llm_model, self.unify_params
        )
        
        task_gen_extra = os.getenv('TASK_GENERATION_EXTRA', 'rewrite the project')
        task_prompt_base = os.getenv('TASK_GENERATION_SYSTEM_PROMPT', TASK_GENERATION_SYSTEM_PROMPT)
        unify_prompt_base = os.getenv('UNIFICATION_SYSTEM_PROMPT', UNIFICATION_SYSTEM_PROMPT)
        prompt_extra = os.getenv('PROMPT_EXTRA', '').strip()
        
        self.task_prompt_template_base = task_prompt_base
        self.task_gen_extra = task_gen_extra
        self.prompt_extra = prompt_extra
        
        self.unify_prompt_base = unify_prompt_base.replace('{PROMPT_EXTRA}', prompt_extra)
        
        self.decision_memory = os.getenv('INITIAL_DECISION_MEMORY', '').strip()
        decision_memory_limit_chars = os.getenv('DECISION_MEMORY_MAX_CHARS')
        if decision_memory_limit_chars:
            try:
                self.decision_memory_limit = int(decision_memory_limit_chars)
            except (ValueError, TypeError):
                self.decision_memory_limit = None
        else:
            decision_memory_limit_words = 500
            self.decision_memory_limit = int(decision_memory_limit_words * 5)
        
        self._create_tables()
    
    def _create_tables(self):
        self.chats_cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_sequences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_text TEXT,
                decision_memory TEXT,
                last_updated TEXT
            )
        """)
        self.chats_cursor.execute("""
            CREATE TABLE IF NOT EXISTS specs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                specs_text TEXT,
                last_updated TEXT
            )
        """)
        self.chats_conn.commit()
    
    def load_specs_from_db(self) -> str:
        """Load latest specs from database."""
        row = self.chats_cursor.execute("""
            SELECT specs_text FROM specs ORDER BY id DESC LIMIT 1
        """).fetchone()
        
        if row and row[0]:
            return row[0]
        return ""
    
    def get_group_summaries(self) -> List[Dict]:
        """Get all group summaries ordered by timestamp."""
        summaries = self.chats_cursor.execute("""
            SELECT group_id, title, summary, first_timestamp
            FROM group_summaries
            ORDER BY first_timestamp ASC
        """).fetchall()
        
        result = []
        for group_id, title, summary, first_timestamp in summaries:
            result.append({
                'group_id': group_id,
                'title': title,
                'summary': summary or '',
                'first_timestamp': first_timestamp
            })
        
        return result
    
    def format_summaries_for_prompt(self, summaries: List[Dict]) -> str:
        """Format summaries for prompt."""
        parts = []
        for summary in summaries:
            parts.append(f"## {summary['title']} ({summary['first_timestamp']})")
            parts.append("")
            if summary['summary']:
                parts.append(summary['summary'])
                parts.append("")
            parts.append("---")
            parts.append("")
        
        return "\n".join(parts)
    
    def _estimate_text_size(self, text: str) -> int:
        return len(text)
    
    def _extract_task_sequence_from_response(self, response_text: str) -> str:
        """Extract task sequence from LLM response, preserving full task content."""
        response_text = clean_llm_response(response_text)
        response_text = response_text.strip()
        
        if re.search(r'^##\s+Task\s+\d+:', response_text, re.MULTILINE):
            return response_text
        
        lines = response_text.split('\n')
        task_lines = []
        in_task = False
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if in_task:
                    task_lines.append("")
                continue
            
            if re.match(r'^##\s+Task\s+\d+:', stripped, re.IGNORECASE):
                if task_lines and task_lines[-1]:
                    task_lines.append("")
                task_lines.append(line)
                in_task = True
            elif re.match(r'^\d+[\.\)]\s+', stripped):
                if task_lines and task_lines[-1] and not task_lines[-1].startswith('##'):
                    task_lines.append("")
                task_lines.append(line)
                in_task = True
            elif in_task:
                task_lines.append(line)
            elif re.match(r'^[-*]\s+', stripped):
                task_lines.append(line)
                in_task = True
        
        if task_lines:
            result = '\n'.join(task_lines)
            if not re.search(r'^##\s+Task\s+\d+:', result, re.MULTILINE):
                result = self._convert_numbered_to_headed_tasks(result)
            return result
        
        return response_text
    
    def _extract_task_sequence(self, response_text: str) -> str:
        """Extract task sequence from LLM response, preserving full task content."""
        return self._extract_task_sequence_from_response(response_text)
    
    def _convert_numbered_to_headed_tasks(self, text: str) -> str:
        """Convert numbered list format to headed task format."""
        lines = text.split('\n')
        result = []
        task_num = 1
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            if re.match(r'^\d+[\.\)]\s+', stripped):
                title_match = re.match(r'^\d+[\.\)]\s+(.+)', stripped)
                if title_match:
                    title = title_match.group(1).strip()
                    if len(title) > 100:
                        title = title[:97] + "..."
                    result.append(f"## Task {task_num}: {title}")
                    task_num += 1
                else:
                    result.append(line)
            else:
                result.append(line)
        
        return '\n'.join(result)
    
    @retry_with_backoff(retry_env_prefix='LLM')
    def _call_llm_api(self, system_prompt: str, user_prompt: str, params: dict, max_tokens: Optional[int], request_type: str = "LLM") -> str:
        """Call LLM API with given parameters."""
        if os.getenv('DEBUG_LLM') == '1':
            print(f"\n{'='*80}")
            print(f"LLM {request_type} Request:")
            print(f"{'='*80}")
            print(f"\n[SYSTEM PROMPT]\n{system_prompt}")
            print(f"\n{'='*80}")
            print(f"[USER CONTENT] ({len(user_prompt)} characters)\n{user_prompt[:500]}...")
            print(f"{'='*80}\n")
            sys.stdout.flush()
        
        api_params = {
            'model': self.llm_model,
            'messages': [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        }
        
        for key, value in params.items():
            if value is not None and key != 'max_tokens':
                api_params[key] = value
        
        if max_tokens is not None:
            api_params['max_tokens'] = max_tokens
        
        response = self.llm_client.chat.completions.create(**api_params)
        
        response_text = response.choices[0].message.content.strip()
        response_text = clean_llm_response(response_text)
        
        if os.getenv('DEBUG_LLM') == '1':
            print(f"\n{'='*80}")
            print(f"LLM {request_type} Response:")
            print(f"{'='*80}")
            print(f"\n{response_text}")
            print(f"{'='*80}\n")
            sys.stdout.flush()
        
        return response_text
    
    @retry_with_backoff(retry_env_prefix='LLM')
    def _call_task_generation_api(self, system_prompt: str, user_prompt: str) -> str:
        """Call LLM API for task generation."""
        return self._call_llm_api(system_prompt, user_prompt, self.task_params, self.task_api_max_tokens, "Task Generation")
    
    def generate_task_sequence(self, specs: str, summaries: List[Dict]) -> str:
        """Generate task sequence from specs and summaries in batches."""
        overall_limit_chars = tokens_to_chars(self.llm_context_size)
        max_batch_size = int(overall_limit_chars * 0.5)
        specs_size = self._estimate_text_size(specs)
        
        decision_memory_text = self.decision_memory or "No decisions established yet."
        if self.decision_memory_limit and len(decision_memory_text) > self.decision_memory_limit:
            print(f"  Warning: Decision memory ({len(decision_memory_text):,} chars) exceeds limit ({self.decision_memory_limit:,} chars)")
            print(f"  Using truncated version for context calculations")
            decision_memory_text = decision_memory_text[:self.decision_memory_limit]
            sys.stdout.flush()
        
        sample_task_prompt = self.task_prompt_template_base.format(
            TASK_GENERATION_EXTRA=self.task_gen_extra,
            DECISION_MEMORY=decision_memory_text,
            PROMPT_EXTRA=self.prompt_extra
        )
        prompt_overhead = len(sample_task_prompt) + 200
        user_content_prefix = len("Project Specifications:\n\n\n\n---\n\nTask Group Summaries:\n\n")
        available_for_batch = overall_limit_chars - specs_size - prompt_overhead - user_content_prefix
        
        if available_for_batch <= 0:
            memory_size = len(self.decision_memory)
            raise RuntimeError(f"Context too small: specs ({specs_size:,} chars) + decision memory ({memory_size:,} chars) + prompt overhead ({prompt_overhead:,} chars) exceeds context limit ({overall_limit_chars:,} chars)")
        
        actual_batch_size = min(max_batch_size, available_for_batch)
        
        all_tasks = []
        idx = 0
        
        while idx < len(summaries):
            batch_summaries = []
            batch_size = 0
            
            while idx < len(summaries):
                summary = summaries[idx]
                formatted = self.format_summaries_for_prompt([summary])
                summary_size = self._estimate_text_size(formatted)
                
                if batch_size + summary_size > actual_batch_size and batch_summaries:
                    break
                
                batch_summaries.append(summary)
                batch_size += summary_size
                idx += 1
            
            if not batch_summaries:
                if idx < len(summaries):
                    batch_summaries = [summaries[idx]]
                    idx += 1
                else:
                    break
            
            batch_text = self.format_summaries_for_prompt(batch_summaries)
            user_content = f"Project Specifications:\n\n{specs}\n\n---\n\nTask Group Summaries:\n\n{batch_text}"
            
            task_prompt = self.task_prompt_template_base.format(
                TASK_GENERATION_EXTRA=self.task_gen_extra,
                DECISION_MEMORY=self.decision_memory or "No decisions established yet.",
                PROMPT_EXTRA=self.prompt_extra
            )
            
            batch_start_idx = idx - len(batch_summaries) + 1
            print(f"  Processing batch: summaries {batch_start_idx}-{idx} ({len(batch_summaries)} summaries)...")
            sys.stdout.flush()
            
            try:
                response_text = self._call_task_generation_api(task_prompt, user_content)
                batch_tasks = self._extract_task_sequence(response_text)
                
                if batch_tasks:
                    if all_tasks:
                        all_tasks.append("")
                    all_tasks.append(batch_tasks)
                
                print(f"  Processed {idx}/{len(summaries)} summaries")
                sys.stdout.flush()
            except RuntimeError as e:
                print(f"    Error generating tasks: {e}")
                break
        
        return '\n'.join(all_tasks)
    
    def _extract_decision_memory(self, response_text: str) -> Tuple[str, str]:
        """Extract decision memory and updated tasks from unification response, preserving full task content."""
        response_text = clean_llm_response(response_text)
        
        memory_match = re.search(r'<DECISION_MEMORY>(.*?)</DECISION_MEMORY>', response_text, re.DOTALL)
        if memory_match:
            decision_memory = memory_match.group(1).strip()
            task_sequence = response_text[memory_match.end():].strip()
        else:
            decision_memory = self.decision_memory
            task_sequence = response_text.strip()
        
        if self.decision_memory_limit and len(decision_memory) > self.decision_memory_limit:
            print(f"  Warning: Decision memory ({len(decision_memory):,} chars) exceeds limit ({self.decision_memory_limit:,} chars)")
            print(f"  Truncating to limit...")
            decision_memory = decision_memory[:self.decision_memory_limit].rsplit('\n', 1)[0]
            if not decision_memory.endswith('.'):
                decision_memory += "..."
            print(f"  Truncated to {len(decision_memory):,} chars")
            sys.stdout.flush()
        
        task_sequence = self._extract_task_sequence_from_response(task_sequence)
        return decision_memory, task_sequence
    
    @retry_with_backoff(retry_env_prefix='LLM')
    def _call_unification_api(self, system_prompt: str, user_prompt: str) -> str:
        """Call LLM API for task unification."""
        return self._call_llm_api(system_prompt, user_prompt, self.unify_params, self.unify_api_max_tokens, "Unification")
    
    def unify_task_sequence(self, task_sequence: str) -> Tuple[str, str]:
        """Unify task sequence with consistent decisions."""
        if not task_sequence.strip():
            return self.decision_memory, task_sequence
        
        overall_limit_chars = tokens_to_chars(self.llm_context_size)
        max_content_size = int(overall_limit_chars * 0.7)
        
        sample_unify_prompt = self.unify_prompt_base.format(
            DECISION_MEMORY=self.decision_memory or "No decisions established yet.",
            TASK_SEQUENCE=""
        )
        user_content_prefix = len("Unify the following task sequence:\n\n")
        prompt_overhead = len(sample_unify_prompt) + user_content_prefix + 200
        
        task_size = self._estimate_text_size(task_sequence)
        available_for_tasks = max_content_size - prompt_overhead
        
        if task_size > available_for_tasks:
            print(f"  Warning: Task sequence ({task_size:,} chars) exceeds available space ({available_for_tasks:,} chars)")
            print(f"  Decision memory: {len(self.decision_memory):,} chars, Prompt overhead: {prompt_overhead:,} chars")
            print(f"  Processing in chunks...")
            sys.stdout.flush()
            
            return self._unify_in_chunks(task_sequence, max_content_size)
        
        print(f"  Unifying task sequence ({task_size:,} chars)...")
        sys.stdout.flush()
        
        decision_memory, unified_tasks = self._unify_single_chunk(task_sequence)
        self.decision_memory = decision_memory
        
        print(f"  Decision memory updated ({len(decision_memory):,} chars)")
        sys.stdout.flush()
        
        return decision_memory, unified_tasks
    
    def _split_into_task_blocks(self, task_sequence: str) -> List[str]:
        """Split task sequence into individual task blocks preserving full content."""
        lines = task_sequence.split('\n')
        task_blocks = []
        current_task = []
        
        for line in lines:
            if re.match(r'^##\s+Task\s+\d+:', line.strip(), re.IGNORECASE):
                if current_task:
                    task_blocks.append('\n'.join(current_task))
                current_task = [line]
            elif current_task:
                current_task.append(line)
            elif re.match(r'^\d+[\.\)]\s+', line.strip()):
                if current_task:
                    task_blocks.append('\n'.join(current_task))
                current_task = [line]
        
        if current_task:
            task_blocks.append('\n'.join(current_task))
        
        return task_blocks
    
    def _unify_in_chunks(self, task_sequence: str, max_chunk_size: int) -> Tuple[str, str]:
        """Unify task sequence in chunks when it's too large, preserving full task content."""
        task_blocks = self._split_into_task_blocks(task_sequence)
        
        if not task_blocks:
            return self.decision_memory, task_sequence
        
        sample_unify_prompt = self.unify_prompt_base.format(
            DECISION_MEMORY=self.decision_memory or "No decisions established yet.",
            TASK_SEQUENCE=""
        )
        user_content_prefix = len("Unify the following task sequence:\n\n")
        prompt_overhead = len(sample_unify_prompt) + user_content_prefix + 200
        chunk_size = max_chunk_size - prompt_overhead
        
        unified_tasks = []
        current_chunk = []
        current_chunk_size = 0
        
        for task_block in task_blocks:
            task_size = len(task_block) + 2
            
            if current_chunk_size + task_size > chunk_size and current_chunk:
                chunk_text = '\n\n'.join(current_chunk)
                chunk_memory, chunk_unified = self._unify_single_chunk(chunk_text)
                self.decision_memory = chunk_memory
                
                unified_tasks.append(chunk_unified)
                current_chunk = [task_block]
                current_chunk_size = task_size
            else:
                current_chunk.append(task_block)
                current_chunk_size += task_size
        
        if current_chunk:
            chunk_text = '\n\n'.join(current_chunk)
            chunk_memory, chunk_unified = self._unify_single_chunk(chunk_text)
            self.decision_memory = chunk_memory
            unified_tasks.append(chunk_unified)
        
        final_sequence = '\n\n'.join(unified_tasks)
        
        print(f"  Final unification pass...")
        sys.stdout.flush()
        decision_memory, final_unified = self._unify_single_chunk(final_sequence)
        self.decision_memory = decision_memory
        
        return decision_memory, final_unified
    
    def _unify_single_chunk(self, task_sequence: str) -> Tuple[str, str]:
        """Unify a single chunk of tasks with current decision memory."""
        unify_prompt = self.unify_prompt_base.format(
            DECISION_MEMORY=self.decision_memory or "No decisions established yet.",
            TASK_SEQUENCE=task_sequence
        )
        
        user_content = f"Unify the following task sequence:\n\n{task_sequence}"
        
        try:
            response_text = self._call_unification_api(unify_prompt, user_content)
            decision_memory, unified_tasks = self._extract_decision_memory(response_text)
            return decision_memory, unified_tasks
        except RuntimeError as e:
            print(f"    Error unifying chunk: {e}")
            return self.decision_memory, task_sequence
    
    def save_to_db(self, sequence: str, decision_memory: str):
        """Save task sequence and decision memory to database."""
        now = datetime.now().isoformat()
        self.chats_cursor.execute("""
            INSERT INTO task_sequences (sequence_text, decision_memory, last_updated)
            VALUES (?, ?, ?)
        """, (sequence, decision_memory, now))
        self.chats_conn.commit()
    
    def generate_all_tasks(self, force: bool = False) -> Tuple[str, str]:
        """Generate complete task sequence with unification."""
        specs = self.load_specs_from_db()
        if not specs:
            raise ValueError("No specs found in database. Run generate_specs.py first.")
        
        summaries = self.get_group_summaries()
        if not summaries:
            raise ValueError("No group summaries found in database. Run generate_group_summaries.py first.")
        
        context_size_chars = tokens_to_chars(self.llm_context_size)
        print(f"Found {len(summaries)} group summaries")
        print(f"LLM context size: {self.llm_context_size:,} tokens ({context_size_chars:,} characters)")
        print(f"Batch size limit: {int(context_size_chars * 0.5):,} characters (50% of context for summaries)")
        print()
        
        if not force:
            existing = self.chats_cursor.execute("""
                SELECT sequence_text, decision_memory FROM task_sequences ORDER BY id DESC LIMIT 1
            """).fetchone()
            
            if existing:
                print(f"Found existing task sequence in database")
                print(f"Use --force to regenerate")
                return existing[0] or "", existing[1] or ""
        
        print("Generating initial task sequence...")
        sys.stdout.flush()
        task_sequence = self.generate_task_sequence(specs, summaries)
        
        if not task_sequence.strip():
            print("No tasks generated")
            return "", ""
        
        print(f"\nGenerated {len(task_sequence.split(chr(10)))} lines of tasks")
        print("\nUnifying task sequence with consistent decisions...")
        sys.stdout.flush()
        
        decision_memory, unified_sequence = self.unify_task_sequence(task_sequence)
        
        self.save_to_db(unified_sequence, decision_memory)
        
        return unified_sequence, decision_memory
    
    def close(self):
        self.chats_conn.close()


def main():
    load_dotenv()
    
    config = load_api_config(['llm_url', 'llm_model'])
    
    parser = argparse.ArgumentParser(description='Generate task sequence from specs and summaries using LLM')
    add_db_file_argument(parser)
    parser.add_argument('--output', default=None, help='Output markdown file (default: <db_file_base>-TASKS.md)')
    parser.add_argument('--force', action='store_true', help='Force re-generation even if sequence exists')
    
    args = parser.parse_args()
    
    chats_db = find_db_file(args.db_file)
    
    if args.output:
        output_file = args.output
    else:
        db_base = os.path.splitext(os.path.basename(chats_db))[0]
        output_file = f"{db_base}-TASKS.md"
    
    generator = TaskSequenceGenerator(chats_db, config['llm_url'], config['llm_model'], config['llm_api_key'])
    try:
        sequence, decision_memory = generator.generate_all_tasks(force=args.force)
        
        if sequence:
            output_content = f"# Task Sequence\n\n"
            if decision_memory:
                output_content += f"## Decision Memory\n\n{decision_memory}\n\n---\n\n"
            output_content += f"## Tasks\n\n{sequence}\n"
            
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(output_content)
            print(f"\nTask sequence written to: {output_file}")
            if decision_memory:
                print(f"Decision memory: {len(decision_memory):,} characters")
        else:
            print("\nNo task sequence generated")
    finally:
        generator.close()


if __name__ == '__main__':
    main()

