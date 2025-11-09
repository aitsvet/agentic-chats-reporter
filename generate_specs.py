#!/usr/bin/env python3
import sqlite3
import argparse
import sys
import os
import time
from typing import List, Dict, Tuple
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from llm_utils import get_model_context_size, tokens_to_chars


SPEC_SYSTEM_PROMPT = """Maintain requirements specification by preserving existing requirements and extending with insights from new task group summaries.

**Task:**
1. PRESERVE meaningful requirements from existing specs
2. Extract new requirements: WHAT (domain) and HOW (tech stack)
3. Adjust/extend existing with new insights
4. Merge only true duplicates, preserve meaningful distinctions
5. Output COMPLETE specification (existing + new together)

**Output Format (ready for report insertion):**
## Domain requirements

### [Category]
- [Requirement: WHAT project does, 1 sentence]
- [Another requirement]

### [Another Category]
- [Requirement]

## Technical requirements

### [Category]
- [Requirement: HOW project works, 1 sentence]
- [Another requirement]

### [Another Category]
- [Requirement]

**Rules:**
- Domain: WHAT (purpose, user value), NOT how/tech/UI/implementation
- Technical: HOW (tech stack, architecture), NOT what/domain/code details
- Each requirement: 1 sentence, generalized principles
- Categories: general to specific
- Preserve distinctions; merge only true duplicates
- Remove: UI specifics, debugging, code structure, function/field names
- Keep: project purpose, user value, tech stack, architecture, distinctions
- Do your best to keep existing requirements describing the projects as a whole, adjust and extend to match given new summaries only if they contain really important points
Output ONLY the above format. No explanations or additional text.

{PROMPT_EXTRA}"""


DEDUP_SYSTEM_PROMPT = """Deduplicate and consolidate requirements into a focused project digest while preserving meaningful distinctions.

**Task:**
1. Merge true duplicates and redundancies
2. Consolidate overlaps, preserve distinctions
3. Remove implementation details, UI specifics, debugging, code-level info
4. Generalize specific requirements into high-level principles
5. Reduce size through consolidation (target 30-50% reduction)
6. Maintain Domain and Technical structure

**Output Format (ready for report insertion):**
## Domain requirements

### [Category]
- [Requirement: WHAT project does, 1 sentence]
- [Another requirement]

### [Another Category]
- [Requirement]

## Technical requirements

### [Category]
- [Requirement: HOW project works, 1 sentence]
- [Another requirement]

### [Another Category]
- [Requirement]

**Rules:**
- Domain: WHAT (purpose, user value), NOT how/tech/UI/implementation
- Technical: HOW (tech stack, architecture), NOT what/domain/code details
- Each requirement: 1 sentence, generalized principles
- Categories: merge similar, order general to specific
- Preserve distinctions; merge only true duplicates
- Remove: UI specifics, debugging, code structure, function/field names
- Keep: project purpose, user value, tech stack, architecture, distinctions

Output ONLY the above format. No explanations or additional text.

{PROMPT_EXTRA}"""


class SpecGenerator:
    def __init__(self, chats_db: str, llm_url: str, llm_model: str, llm_api_key: str = None):
        self.chats_conn = sqlite3.connect(chats_db)
        self.chats_cursor = self.chats_conn.cursor()
        api_key = llm_api_key or os.getenv('LLM_API_KEY', 'not-needed')
        self.llm_client = OpenAI(base_url=llm_url, api_key=api_key)
        self.llm_model = llm_model
        self.llm_context_size = self._get_llm_context_size()
        spec_prompt_base = os.getenv('SPEC_SYSTEM_PROMPT', SPEC_SYSTEM_PROMPT)
        dedup_prompt_base = os.getenv('DEDUP_SYSTEM_PROMPT', DEDUP_SYSTEM_PROMPT)
        prompt_extra = os.getenv('PROMPT_EXTRA', '').strip()
        self.spec_prompt_template = spec_prompt_base.format(PROMPT_EXTRA=prompt_extra)
        self.dedup_prompt_template = dedup_prompt_base.format(PROMPT_EXTRA=prompt_extra)
        self.spec_temperature = float(os.getenv('SPEC_TEMPERATURE', '0.3'))
        self.dedup_temperature = float(os.getenv('DEDUP_TEMPERATURE', '0.3'))
        self._create_tables()
    
    def _create_tables(self):
        self.chats_cursor.execute("""
            CREATE TABLE IF NOT EXISTS specs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                specs_text TEXT,
                last_updated TEXT
            )
        """)
        self.chats_cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_summaries (
                group_id INTEGER PRIMARY KEY,
                processed_at TEXT
            )
        """)
        self.chats_conn.commit()
    
    def _get_llm_context_size(self) -> int:
        return get_model_context_size(self.llm_client, self.llm_model, model_type='llm')
    
    def load_specs_from_db(self) -> Tuple[str, set]:
        """Load existing specs and processed group IDs from database."""
        row = self.chats_cursor.execute("""
            SELECT specs_text FROM specs ORDER BY id DESC LIMIT 1
        """).fetchone()
        
        if row:
            specs_text = row[0] or ""
        else:
            specs_text = ""
        
        processed_group_ids = set()
        for row in self.chats_cursor.execute("SELECT group_id FROM processed_summaries"):
            processed_group_ids.add(row[0])
        
        return specs_text, processed_group_ids
    
    def save_specs_to_db(self, specs: str):
        """Save specs to database."""
        now = datetime.now().isoformat()
        self.chats_cursor.execute("""
            INSERT INTO specs (specs_text, last_updated) VALUES (?, ?)
        """, (specs, now))
        self.chats_conn.commit()
    
    def mark_summaries_processed(self, group_ids: List[int]):
        """Mark summaries as processed in database."""
        now = datetime.now().isoformat()
        for group_id in group_ids:
            self.chats_cursor.execute("""
                INSERT OR REPLACE INTO processed_summaries (group_id, processed_at) VALUES (?, ?)
            """, (group_id, now))
        self.chats_conn.commit()
    
    def clear_specs_data(self):
        """Clear all specs and processed summaries data."""
        self.chats_cursor.execute("DELETE FROM specs")
        self.chats_cursor.execute("DELETE FROM processed_summaries")
        self.chats_conn.commit()
    
    def get_group_summaries(self) -> List[Dict]:
        summaries = self.chats_cursor.execute("""
            SELECT group_id, title, user_summary, agent_summary, first_timestamp
            FROM group_summaries
            ORDER BY first_timestamp ASC
        """).fetchall()
        
        result = []
        for group_id, title, user_summary, agent_summary, first_timestamp in summaries:
            result.append({
                'group_id': group_id,
                'title': title,
                'user_summary': user_summary or '',
                'agent_summary': agent_summary or '',
                'first_timestamp': first_timestamp
            })
        
        return result
    
    def format_summaries_for_prompt(self, summaries: List[Dict]) -> str:
        parts = []
        for summary in summaries:
            parts.append(f"## {summary['title']} ({summary['first_timestamp']})")
            parts.append("")
            if summary['user_summary']:
                parts.append("**User:**")
                parts.append(summary['user_summary'])
                parts.append("")
            if summary['agent_summary']:
                parts.append("**Agent:**")
                parts.append(summary['agent_summary'])
                parts.append("")
            parts.append("---")
            parts.append("")
        
        return "\n".join(parts)
    
    def _estimate_text_size(self, text: str) -> int:
        return len(text)
    
    def _merge_specs(self, existing_specs: str, new_specs: str) -> str:
        """Merge new requirements into existing specs by combining sections and categories."""
        if not existing_specs.strip():
            return new_specs
        if not new_specs.strip():
            return existing_specs
        
        def parse_specs(specs_text: str) -> Dict[str, Dict[str, List[str]]]:
            """Parse specs into structured format: {section: {category: [requirements]}}"""
            result = {'domain': {}, 'technical': {}}
            current_section = None
            current_category = None
            
            for line in specs_text.split('\n'):
                line_stripped = line.strip()
                if line_stripped.startswith('## Domain requirements'):
                    current_section = 'domain'
                    continue
                elif line_stripped.startswith('## Technical requirements'):
                    current_section = 'technical'
                    continue
                elif line_stripped.startswith('###'):
                    if current_section:
                        current_category = line_stripped
                        if current_category not in result[current_section]:
                            result[current_section][current_category] = []
                    continue
                elif line_stripped.startswith('-') and current_section and current_category:
                    result[current_section][current_category].append(line.rstrip())
            
            return result
        
        existing_parsed = parse_specs(existing_specs)
        new_parsed = parse_specs(new_specs)
        
        merged_parsed = {'domain': {}, 'technical': {}}
        
        for section in ['domain', 'technical']:
            all_categories = set(existing_parsed[section].keys()) | set(new_parsed[section].keys())
            for category in all_categories:
                existing_reqs = existing_parsed[section].get(category, [])
                new_reqs = new_parsed[section].get(category, [])
                all_reqs = existing_reqs + new_reqs
                merged_parsed[section][category] = all_reqs
        
        result_parts = []
        
        if merged_parsed['domain']:
            result_parts.append('## Domain requirements')
            result_parts.append('')
            for category, reqs in sorted(merged_parsed['domain'].items()):
                result_parts.append(category)
                result_parts.append('')
                result_parts.extend(reqs)
                result_parts.append('')
        
        if merged_parsed['technical']:
            result_parts.append('## Technical requirements')
            result_parts.append('')
            for category, reqs in sorted(merged_parsed['technical'].items()):
                result_parts.append(category)
                result_parts.append('')
                result_parts.extend(reqs)
                result_parts.append('')
        
        return '\n'.join(result_parts).strip()
    
    def generate_specs_from_summaries(self, summaries: List[Dict], existing_specs: str = "", processed_group_ids: set = None, save_after_batch: bool = True) -> str:
        context_size_chars = tokens_to_chars(self.llm_context_size)
        max_batch_size = int(context_size_chars * 0.5)
        max_specs_size = int(context_size_chars * 0.25)
        output_reserve = int(context_size_chars * 0.25)
        current_specs = existing_specs
        processed_group_ids = processed_group_ids or set()
        
        idx = 0
        while idx < len(summaries) and summaries[idx]['group_id'] in processed_group_ids:
            idx += 1
        while idx < len(summaries):
            current_specs_size = self._estimate_text_size(current_specs)
            
            if current_specs_size > max_specs_size:
                print(f"  Warning: Specs size ({current_specs_size:,} chars) exceeds threshold ({max_specs_size:,} chars)")
                print(f"  Running deduplication pass to reduce size...")
                sys.stdout.flush()
                current_specs = self.deduplicate_specs(current_specs)
                new_specs_size = self._estimate_text_size(current_specs)
                reduction = ((current_specs_size - new_specs_size) / current_specs_size * 100) if current_specs_size > 0 else 0
                print(f"  After deduplication: {new_specs_size:,} chars (reduced by {reduction:.1f}%)")
                sys.stdout.flush()
                current_specs_size = new_specs_size
                if save_after_batch:
                    self.save_specs_to_db(current_specs)
            
            prompt_overhead = len(self.spec_prompt_template) + 200
            user_content_prefix = len(f"Existing specs:\n\n\n\n---\n\nNew task group summaries to analyze:\n\n")
            available_for_batch = context_size_chars - current_specs_size - prompt_overhead - user_content_prefix - output_reserve
            actual_batch_size = min(max_batch_size, available_for_batch)
            
            if actual_batch_size <= 0:
                print(f"  Warning: No space left for batch processing (specs: {current_specs_size:,} chars, available: {available_for_batch:,} chars)")
                print(f"  Skipping remaining summaries. Consider running deduplication or processing in smaller chunks.")
                break
            
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
                    print(f"  Warning: Cannot fit next summary into batch, forcing single summary")
                    batch_summaries = [summaries[idx]]
                    batch_size = self._estimate_text_size(self.format_summaries_for_prompt(batch_summaries))
                    idx += 1
                else:
                    break
            
            batch_text = self.format_summaries_for_prompt(batch_summaries)
            
            user_content = f"Existing specs:\n\n{current_specs}\n\n---\n\nNew task group summaries to analyze:\n\n{batch_text}"
            
            batch_start_idx = idx - len(batch_summaries) + 1
            print(f"  Processing batch: summaries {batch_start_idx}-{idx} ({len(batch_summaries)} summaries)...")
            sys.stdout.flush()
            
            if os.getenv('DEBUG_LLM') == '1':
                print(f"\n{'='*80}")
                print(f"LLM Request (batch {idx - len(batch_summaries) + 1}-{idx}):")
                print(f"{'='*80}")
                print(f"\n[SYSTEM PROMPT]\n{self.spec_prompt_template}")
                print(f"\n{'='*80}")
                print(f"[USER CONTENT] ({len(user_content)} characters)\n{user_content[:500]}...")
                print(f"{'='*80}\n")
                sys.stdout.flush()
            
            max_retries = int(os.getenv('LLM_MAX_RETRIES', '3'))
            base_delay = float(os.getenv('LLM_RETRY_DELAY', '1.0'))
            success = False
            
            for attempt in range(max_retries):
                if attempt > 0:
                    print(f"    Retrying LLM API call (attempt {attempt + 1}/{max_retries})...")
                else:
                    print(f"    Calling LLM API (attempt {attempt + 1}/{max_retries})...")
                sys.stdout.flush()
                try:
                    response = self.llm_client.chat.completions.create(
                        model=self.llm_model,
                        messages=[
                            {"role": "system", "content": self.spec_prompt_template},
                            {"role": "user", "content": user_content}
                        ],
                        temperature=self.spec_temperature
                    )
                    
                    response_text = response.choices[0].message.content.strip()
                    
                    if os.getenv('DEBUG_LLM') == '1':
                        print(f"\n{'='*80}")
                        print(f"LLM Response:")
                        print(f"{'='*80}")
                        print(f"\n{response_text}")
                        print(f"{'='*80}\n")
                        sys.stdout.flush()
                    
                    current_specs = response_text
                    success = True
                    
                    batch_group_ids = [s['group_id'] for s in batch_summaries]
                    self.mark_summaries_processed(batch_group_ids)
                    
                    if save_after_batch:
                        self.save_specs_to_db(current_specs)
                    
                    print(f"  Processed {idx}/{len(summaries)} summaries")
                    sys.stdout.flush()
                    break
                    
                except Exception as e:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        print(f"    LLM API error (attempt {attempt + 1}/{max_retries}): {e}, retrying in {delay:.1f}s...")
                        sys.stdout.flush()
                        time.sleep(delay)
                    else:
                        print(f"    Error generating specs after {max_retries} attempts: {e}")
                        break
            
            if not success:
                break
        
        return current_specs
    
    def deduplicate_specs(self, specs: str) -> str:
        max_retries = int(os.getenv('LLM_MAX_RETRIES', '3'))
        base_delay = float(os.getenv('LLM_RETRY_DELAY', '1.0'))
        
        print(f"    Calling LLM for deduplication ({len(specs):,} chars)...")
        sys.stdout.flush()
        
        for attempt in range(max_retries):
            try:
                if os.getenv('DEBUG_LLM') == '1':
                    print(f"\n{'='*80}")
                    print(f"LLM Deduplication Request:")
                    print(f"{'='*80}")
                    print(f"\n[SYSTEM PROMPT]\n{self.dedup_prompt_template}")
                    print(f"\n{'='*80}")
                    print(f"[SPECS TO DEDUP] ({len(specs)} characters)\n{specs[:500]}...")
                    print(f"{'='*80}\n")
                    sys.stdout.flush()
                
                response = self.llm_client.chat.completions.create(
                    model=self.llm_model,
                    messages=[
                        {"role": "system", "content": self.dedup_prompt_template},
                        {"role": "user", "content": specs}
                    ],
                    temperature=self.dedup_temperature
                )
                
                response_text = response.choices[0].message.content.strip()
                
                if os.getenv('DEBUG_LLM') == '1':
                    print(f"\n{'='*80}")
                    print(f"LLM Deduplication Response:")
                    print(f"{'='*80}")
                    print(f"\n{response_text}")
                    print(f"{'='*80}\n")
                    sys.stdout.flush()
                
                return response_text
                
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    if os.getenv('DEBUG_LLM') == '1':
                        print(f"    LLM API error (attempt {attempt + 1}/{max_retries}): {e}, retrying in {delay:.1f}s...")
                        sys.stdout.flush()
                    time.sleep(delay)
                else:
                    print(f"    Error deduplicating specs after {max_retries} attempts: {e}")
                    return specs
        
        return specs
    
    def generate_all_specs(self, force: bool = False, final_dedup: bool = True) -> str:
        summaries = self.get_group_summaries()
        context_size_chars = tokens_to_chars(self.llm_context_size)
        print(f"Found {len(summaries)} group summaries")
        print(f"LLM context size: {self.llm_context_size:,} tokens ({context_size_chars:,} characters)")
        print(f"Batch size limit: {int(context_size_chars * 0.5):,} characters (50% of context for summaries)")
        print(f"Specs size limit: {int(context_size_chars * 0.25):,} characters (25% of context for collected requirements)")
        print(f"Output reserve: {int(context_size_chars * 0.25):,} characters (25% of context for LLM output)")
        print()
        
        if not summaries:
            print("No summaries found in database")
            return ""
        
        if force:
            print("Force mode: clearing existing specs data")
            self.clear_specs_data()
            existing_specs = ""
            processed_group_ids = set()
        else:
            existing_specs, processed_group_ids = self.load_specs_from_db()
            if existing_specs or processed_group_ids:
                processed_count = len(processed_group_ids)
                print(f"Resuming from database: {processed_count}/{len(summaries)} summaries already processed")
                if existing_specs:
                    print(f"Loaded existing specs ({len(existing_specs):,} characters)")
                print()
        
        specs = self.generate_specs_from_summaries(summaries, existing_specs, processed_group_ids, save_after_batch=True)
        
        if specs and final_dedup:
            print("  Running final deduplication pass...")
            sys.stdout.flush()
            specs = self.deduplicate_specs(specs)
            self.save_specs_to_db(specs)
        
        specs = specs.replace('---', '<div style="page-break-before: always;"></div>')
        return specs
    
    def close(self):
        self.chats_conn.close()


def main():
    load_dotenv()
    
    llm_url = os.getenv('LLM_URL')
    llm_model = os.getenv('LLM_MODEL')
    llm_api_key = os.getenv('LLM_API_KEY')
    
    if not llm_url or not llm_model:
        raise ValueError("LLM_URL and LLM_MODEL must be set in environment or .env file")
    
    parser = argparse.ArgumentParser(description='Generate specifications from group summaries using LLM')
    parser.add_argument('--db-file', default=None, help='Path to database file (default: searches for *.db files, uses most recent)')
    parser.add_argument('--output', default='specs.md', help='Output markdown file (default: specs.md)')
    parser.add_argument('--force', action='store_true', help='Force re-generation of all specs even if they exist')
    
    args = parser.parse_args()
    
    chats_db = args.db_file
    if chats_db is None:
        import glob
        db_files = glob.glob('*.db')
        if not db_files:
            raise ValueError("No database files found. Please specify --db-file or create a database with parse_chats.py")
        chats_db = max(db_files, key=os.path.getmtime)
        print(f"Using most recent database: {chats_db}")
    
    generator = SpecGenerator(chats_db, llm_url, llm_model, llm_api_key)
    try:
        specs = generator.generate_all_specs(force=args.force)
        
        if specs:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(specs)
            print(f"\nSpecs written to: {args.output}")
        else:
            print("\nNo specs generated")
    finally:
        generator.close()


if __name__ == '__main__':
    main()

