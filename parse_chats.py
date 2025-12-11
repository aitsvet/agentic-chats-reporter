#!/usr/bin/env python3
import sys
import re
import sqlite3
import argparse
import os
from typing import Optional, List
from db_utils import safe_add_column


class ChatParser:
    def __init__(self, db_path: str = "chats.db", max_lines: Optional[int] = None, 
                 max_chats: Optional[int] = None, max_messages: Optional[int] = None,
                 start_chat: int = 0):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self._create_tables()
        self.current_chat_id: Optional[int] = None
        self.current_message_id: Optional[int] = None
        self.max_lines = max_lines
        self.max_chats = max_chats
        self.max_messages = max_messages
        self.start_chat = start_chat
        self.chat_count = 0
        self.message_count = 0
        self.chats_skipped = 0
        self.current_chat_start = None
        
        # Load truncation limits from environment with defaults
        self.user_text_max_lines = int(os.getenv('USER_TEXT_MAX_LINES', '20'))
        self.agent_text_max_lines = int(os.getenv('AGENT_TEXT_MAX_LINES', '1'))
        self.agent_command_max_lines = int(os.getenv('AGENT_COMMAND_MAX_LINES', '3'))
        
    def _create_tables(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                chat_datetime TEXT,
                start_line INTEGER,
                end_line INTEGER
            )
        """)
        
        existing_chats = self.cursor.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        if existing_chats > 0:
            safe_add_column(self.cursor, 'chats', 'start_line INTEGER')
            safe_add_column(self.cursor, 'chats', 'end_line INTEGER')
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                message_type TEXT,
                message_datetime TEXT,
                summary TEXT,
                agent_summary TEXT,
                data_tool_type TEXT,
                data_tool_name TEXT,
                content_type TEXT,
                content_length INTEGER,
                start_line INTEGER,
                end_line INTEGER,
                FOREIGN KEY (chat_id) REFERENCES chats(id)
            )
        """)
        
        existing_messages = self.cursor.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if existing_messages > 0:
            safe_add_column(self.cursor, 'messages', 'start_line INTEGER')
            safe_add_column(self.cursor, 'messages', 'end_line INTEGER')
            safe_add_column(self.cursor, 'messages', 'agent_summary TEXT')
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS content (
                message_id INTEGER PRIMARY KEY,
                content_text TEXT,
                FOREIGN KEY (message_id) REFERENCES messages(id)
            )
        """)
        
        self.conn.commit()
    
    def parse_file(self, filepath: str):
        self.filepath = filepath
        
        start_line = self._find_start_position(filepath)
        
        with open(filepath, 'r', encoding='utf-8') as f:
            if self.max_lines:
                lines = []
                for i, line in enumerate(f):
                    if i < start_line:
                        continue
                    if i >= self.max_lines:
                        break
                    lines.append(line)
            else:
                for i in range(start_line):
                    f.readline()
                lines = f.readlines()
        
        total_lines = len(lines) + start_line
        i = 0
        last_progress_report = 0
        while i < len(lines):
            if self.max_lines and i >= self.max_lines:
                break
            
            line = lines[i].strip()
            
            if re.match(r'^_\*\*', lines[i]):
                if self.chats_skipped < self.start_chat or self.current_chat_id is None:
                    i = self._skip_until_next_chat(lines, i)
                else:
                    i = self._handle_message(lines, i)
            elif line == '':
                i += 1
            else:
                chat_start_match = re.match(r'^# (.+?)\s*\((.+?)\)', lines[i])
                if chat_start_match:
                    if self.current_chat_id:
                        overlapping_msg = self.cursor.execute("""
                            SELECT id FROM messages 
                            WHERE chat_id = ? AND start_line <= ? AND (end_line IS NULL OR end_line > ?)
                        """, (self.current_chat_id, i + 1, i + 1)).fetchone()
                        if overlapping_msg:
                            i += 1
                            continue
                        self.cursor.execute("""
                            UPDATE chats SET end_line = ? WHERE id = ?
                        """, (i, self.current_chat_id))
                        self.conn.commit()
                    i = self._handle_chat_start(lines, i)
                    if self.chat_count >= last_progress_report + 100:
                        print(f"  Progress: {self.chat_count} chats parsed ({self.message_count} messages)")
                        sys.stdout.flush()
                        last_progress_report = self.chat_count
                else:
                    if self.chats_skipped < self.start_chat or self.current_chat_id is None:
                        i = self._skip_until_next_chat(lines, i)
                    else:
                        i = self._handle_unknown_block(lines, i)
            
            if self.max_chats and self.chat_count >= self.max_chats:
                if self.current_chat_id:
                    last_chat_end = i
                    self.cursor.execute("""
                        UPDATE chats SET end_line = ? WHERE id = ?
                    """, (last_chat_end, self.current_chat_id))
                    self.conn.commit()
                break
            if self.max_messages and self.message_count >= self.max_messages:
                if self.current_chat_id:
                    last_chat_end = i
                    self.cursor.execute("""
                        UPDATE chats SET end_line = ? WHERE id = ?
                    """, (last_chat_end, self.current_chat_id))
                    self.conn.commit()
                break
        
        if self.current_chat_id:
            self.cursor.execute("""
                UPDATE chats SET end_line = ? WHERE id = ?
            """, (len(lines), self.current_chat_id))
            self.conn.commit()
        
        if self.message_count > 0:
            print(f"\nParsing complete: {self.message_count} messages parsed in {self.chat_count} chats")
    
    def _skip_until_next_chat(self, lines: List[str], start_idx: int) -> int:
        for i in range(start_idx, len(lines)):
            if re.match(r'^# (.+?)\s*\((.+?)\)', lines[i]):
                return i
        return len(lines)
    
    def _handle_chat_start(self, lines: List[str], start_idx: int) -> int:
        if self.max_chats and self.chat_count >= self.max_chats:
            return len(lines)
        
        line = lines[start_idx]
        match = re.match(r'^# (.+?)\s*\((.+?)\)', line)
        if match:
            title = match.group(1).strip()
            dt = match.group(2).strip()
            
            if self.chats_skipped < self.start_chat:
                self.chats_skipped += 1
                self.current_chat_id = None
                return self._skip_until_next_chat(lines, start_idx + 1)
            
            self.current_chat_start = start_idx + 1
            
            self.cursor.execute(
                "INSERT INTO chats (title, chat_datetime, start_line) VALUES (?, ?, ?)",
                (title, dt, start_idx + 1)
            )
            self.current_chat_id = self.cursor.lastrowid
            self.chat_count += 1
            self.conn.commit()
            return start_idx + 1
        else:
            end_idx = self._find_next_break(lines, start_idx)
            first_line = lines[start_idx].strip()[:80]
            print(f"Invalid block: lines {start_idx + 1}-{end_idx + 1}: {first_line}")
            return end_idx
    
    def _handle_message(self, lines: List[str], start_idx: int) -> int:
        line = lines[start_idx]
        
        user_match = re.match(r'^_\*\*User\s*\((.+?)\)\*\*_', line)
        agent_match = re.match(r'^_\*\*Agent\s*(?:\((.+?)\))?.*\*\*_', line)
        
        if not user_match and not agent_match:
            end_idx = self._find_next_break(lines, start_idx)
            first_line = lines[start_idx].strip()[:80]
            print(f"Invalid block: lines {start_idx + 1}-{end_idx + 1}: {first_line}")
            return end_idx
        
        if self.max_messages and self.message_count >= self.max_messages:
            return len(lines)
        
        if self.current_chat_id is None:
            end_idx = self._find_next_break(lines, start_idx)
            first_line = lines[start_idx].strip()[:80]
            print(f"Invalid block: lines {start_idx + 1}-{end_idx + 1}: {first_line}")
            return end_idx
        
        if user_match:
            dt = user_match.group(1).strip()
            msg_type = 'User'
            existing_msg = self.cursor.execute("""
                SELECT id FROM messages 
                WHERE start_line = ? AND message_type = 'User' AND chat_id = ?
            """, (start_idx + 1, self.current_chat_id)).fetchone()
            if existing_msg:
                self.current_message_id = existing_msg[0]
                message_end_idx = self._find_message_end(lines, start_idx)
                return message_end_idx + 1
        else:
            captured = agent_match.group(1).strip() if agent_match.group(1) else None
            datetime_pattern = r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}Z'
            if captured and re.match(datetime_pattern, captured):
                dt = captured
            else:
                dt = None
            msg_type = 'Agent'
        
        message_end_idx = self._find_message_end(lines, start_idx)
        message_content_start = start_idx + 1
        
        self.cursor.execute("""
            INSERT INTO messages (chat_id, message_type, message_datetime, start_line)
            VALUES (?, ?, ?, ?)
        """, (self.current_chat_id, msg_type, dt, start_idx + 1))
        self.current_message_id = self.cursor.lastrowid
        self.message_count += 1
        
        content_lines = lines[message_content_start:message_end_idx]
        has_content_before_dash = any(l.strip() for l in content_lines if l.strip() != '---')
        
        if content_lines and has_content_before_dash:
            if msg_type == 'Agent':
                self._parse_agent_content(content_lines)
                self._update_message_content_length(self.current_message_id)
            else:
                content_text = ''.join([l for l in content_lines if l.strip() != '---'])
                # Truncate user content to USER_TEXT_MAX_LINES max
                user_lines = content_text.split('\n')
                if len(user_lines) > self.user_text_max_lines:
                    content_text = '\n'.join(user_lines[:self.user_text_max_lines]) + '\n... (truncated)'
                
                self.cursor.execute("""
                    INSERT INTO content (message_id, content_text)
                    VALUES (?, ?)
                """, (self.current_message_id, content_text))
                
                first_line = ''
                for line in content_lines:
                    stripped = line.strip()
                    if stripped and stripped != '---':
                        first_line = stripped
                        break
                
                summary = first_line[:140] if first_line else None
                
                self.cursor.execute("""
                    UPDATE messages SET content_type = ?, summary = ? WHERE id = ?
                """, ('text', summary, self.current_message_id))
                self._update_message_content_length(self.current_message_id)
        
        self.cursor.execute("""
            UPDATE messages SET end_line = ? WHERE id = ?
        """, (message_end_idx + 1, self.current_message_id))
        
        self.conn.commit()
        
        next_idx = message_end_idx + 1
        
        while next_idx < len(lines):
            while next_idx < len(lines) and lines[next_idx].strip() == '':
                next_idx += 1
            
            if next_idx >= len(lines):
                return next_idx
            
            if re.match(r'^# (.+?)\s*\((.+?)\)', lines[next_idx]):
                return next_idx
            
            if re.match(r'^_\*\*', lines[next_idx]):
                if msg_type == 'User':
                    return next_idx
                elif msg_type == 'Agent':
                    user_match = re.match(r'^_\*\*User', lines[next_idx])
                    if user_match:
                        return next_idx
            
            if msg_type == 'Agent' or (msg_type == 'User' and not re.match(r'^_\*\*Agent', lines[next_idx] if next_idx < len(lines) else '')):
                continuation_msg_type = 'Agent'
                continuation_dt = dt if msg_type == 'Agent' else None
                
                content_start_line = next_idx
                next_message_end = self._find_message_end_from_content(lines, content_start_line)
                
                if next_message_end < next_idx:
                    break
                
                self.cursor.execute("""
                    INSERT INTO messages (chat_id, message_type, message_datetime, start_line)
                    VALUES (?, ?, ?, ?)
                """, (self.current_chat_id, continuation_msg_type, continuation_dt, content_start_line + 1))
                self.current_message_id = self.cursor.lastrowid
                self.message_count += 1
                
                if self.max_messages and self.message_count >= self.max_messages:
                    self.conn.commit()
                    return next_idx
                
                continuation_content = lines[next_idx:next_message_end]
                if continuation_content:
                    self._parse_agent_content(continuation_content)
                    self._update_message_content_length(self.current_message_id)
                
                self.cursor.execute("""
                    UPDATE messages SET end_line = ? WHERE id = ?
                """, (next_message_end + 1, self.current_message_id))
                
                self.conn.commit()
                next_idx = next_message_end + 1
                msg_type = continuation_msg_type
                dt = continuation_dt
            else:
                break
        
        return next_idx
    
    def _find_message_end(self, lines: List[str], start_idx: int) -> int:
        content_start = start_idx + 1
        first_line = lines[content_start].strip() if content_start < len(lines) else ''
        
        # Check if this is a User message - if so, look for next Agent message first
        user_match = re.match(r'^_\*\*User\s*\((.+?)\)\*\*_', lines[start_idx])
        if user_match:
            # For User messages, first find the next Agent message boundary
            # (to allow embedded chats with User headers in the content)
            for i in range(start_idx + 1, len(lines)):
                if re.match(r'^_\*\*Agent', lines[i]):
                    return i - 1
            # If no Agent found, look for next User message (next chat)
            for i in range(start_idx + 1, len(lines)):
                if re.match(r'^_\*\*User', lines[i]):
                    return i - 1
            return len(lines) - 1
        
        # For Agent messages, use the original logic
        if '<tool-use' in first_line:
            for i in range(content_start, len(lines)):
                if '</tool-use>' in lines[i]:
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip() == '---':
                            return j
                        elif re.match(r'^_\*\*', lines[j]):
                            return j - 1
                    return len(lines) - 1
        
        in_code_block = False
        code_fence_pattern = None
        for i in range(start_idx + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            
            if stripped.startswith('```'):
                if code_fence_pattern is None:
                    code_fence_pattern = stripped
                    in_code_block = True
                elif stripped == code_fence_pattern or stripped == '```':
                    in_code_block = False
                    code_fence_pattern = None
            elif not in_code_block:
                if stripped == '---':
                    return i
                elif re.match(r'^_\*\*', line):
                    return i - 1
        
        return len(lines) - 1
    
    def _find_message_end_from_content(self, lines: List[str], content_start: int) -> int:
        first_line = lines[content_start].strip() if content_start < len(lines) else ''
        
        if '<tool-use' in first_line:
            for i in range(content_start, len(lines)):
                if '</tool-use>' in lines[i]:
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip() == '---':
                            return j
                        elif re.match(r'^_\*\*', lines[j]):
                            return j - 1
                    return len(lines) - 1
        
        in_code_block = False
        code_fence_pattern = None
        for i in range(content_start, len(lines)):
            line = lines[i]
            stripped = line.strip()
            
            if stripped.startswith('```'):
                if code_fence_pattern is None:
                    code_fence_pattern = stripped
                    in_code_block = True
                elif stripped == code_fence_pattern or stripped == '```':
                    in_code_block = False
                    code_fence_pattern = None
            elif not in_code_block:
                if stripped == '---':
                    return i
                elif re.match(r'^_\*\*', line):
                    return i - 1
        
        return len(lines) - 1
    
    def _find_next_break(self, lines: List[str], start_idx: int) -> int:
        for i in range(start_idx + 1, len(lines)):
            if re.match(r'^# (.+?)\s*\((.+?)\)', lines[i]) or re.match(r'^_\*\*', lines[i]):
                return i
            if lines[i].strip() == '---':
                return i + 1
        return len(lines)
    
    def _parse_agent_content(self, content_lines: List[str]):
        content_text = ''.join(content_lines)
        
        if '<think>' in content_text or 'thought process' in content_text.lower():
            content_type = 'think'
            summary = self._extract_summary(content_text)
            self.cursor.execute("""
                INSERT INTO content (message_id, content_text)
                VALUES (?, ?)
            """, (self.current_message_id, content_text))
            self.cursor.execute("""
                UPDATE messages SET content_type = ? WHERE id = ?
            """, (content_type, self.current_message_id))
            if summary:
                self.cursor.execute("""
                    UPDATE messages SET summary = ? WHERE id = ?
                """, (summary, self.current_message_id))
        
        elif '<tool-use' in content_text:
            content_type = 'tool_call'
            data_tool_type = self._extract_attr(content_text, 'data-tool-type')
            data_tool_name = self._extract_attr(content_text, 'data-tool-name')
            summary = self._extract_summary(content_text)
            
            # Truncate summary for command/tool_call messages
            if summary and (data_tool_type == 'command' or (data_tool_name and data_tool_name in ['run_terminal_cmd', 'command'])):
                summary_lines = summary.split('\n')
                if len(summary_lines) > self.agent_command_max_lines:
                    summary = '\n'.join(summary_lines[:self.agent_command_max_lines]) + '\n... (truncated)'
            
            self.cursor.execute("""
                INSERT INTO content (message_id, content_text)
                VALUES (?, ?)
            """, (self.current_message_id, content_text))
            self.cursor.execute("""
                UPDATE messages SET content_type = ? WHERE id = ?
            """, (content_type, self.current_message_id))
            
            if data_tool_type:
                self.cursor.execute("""
                    UPDATE messages SET data_tool_type = ? WHERE id = ?
                """, (data_tool_type, self.current_message_id))
            if data_tool_name:
                self.cursor.execute("""
                    UPDATE messages SET data_tool_name = ? WHERE id = ?
                """, (data_tool_name, self.current_message_id))
            if summary:
                self.cursor.execute("""
                    UPDATE messages SET summary = ? WHERE id = ?
                """, (summary, self.current_message_id))
        
        else:
            content_type = 'text'
            # Use first AGENT_TEXT_MAX_LINES for agent text message summary
            summary_lines = []
            for line in content_lines:
                stripped = line.strip()
                if stripped:
                    summary_lines.append(stripped)
                    if len(summary_lines) >= self.agent_text_max_lines:
                        break
            
            summary = '\n'.join(summary_lines) if summary_lines else None
            
            self.cursor.execute("""
                INSERT INTO content (message_id, content_text)
                VALUES (?, ?)
            """, (self.current_message_id, content_text))
            self.cursor.execute("""
                UPDATE messages SET content_type = ? WHERE id = ?
            """, ('text', self.current_message_id))
            
            if summary:
                self.cursor.execute("""
                    UPDATE messages SET summary = ? WHERE id = ?
                """, (summary, self.current_message_id))
    def _update_message_content_length(self, message_id: int):
        self.cursor.execute("""
            SELECT SUM(LENGTH(content_text)) FROM content WHERE message_id = ?
        """, (message_id,))
        result = self.cursor.fetchone()
        total_length = result[0] if result[0] is not None else 0
        self.cursor.execute("""
            UPDATE messages SET content_length = ? WHERE id = ?
        """, (total_length, message_id))
    
    def _extract_summary(self, text: str) -> Optional[str]:
        summary_match = re.search(r'<summary>(.+?)</summary>', text, re.DOTALL)
        if summary_match:
            return summary_match.group(1).strip()
        return None
    
    def _extract_attr(self, text: str, attr_name: str) -> Optional[str]:
        pattern = f'{attr_name}="([^"]+)"'
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
        return None
    
    def _handle_unknown_block(self, lines: List[str], start_idx: int) -> int:
        block_start = start_idx
        
        dash_idx = None
        for i in range(start_idx + 1, len(lines)):
            if lines[i].strip() == '---':
                dash_idx = i
                break
            if re.match(r'^# (.+?)\s*\((.+?)\)', lines[i]) or re.match(r'^_\*\*', lines[i]):
                block_end = i
                first_line = lines[block_start].strip()[:80]
                print(f"Invalid block: lines {block_start + 1}-{block_end}: {first_line}")
                return block_end
        
        if dash_idx is not None:
            block_lines = lines[start_idx:dash_idx]
            non_empty_lines = [l.strip() for l in block_lines if l.strip()]
            
            if len(non_empty_lines) == 1 and re.match(r'^# (.+?)\s*\((.+?)\)', non_empty_lines[0]):
                return self._handle_chat_start(lines, start_idx)
        
        block_end = self._find_next_break(lines, start_idx)
        first_line = lines[block_start].strip()[:80]
        print(f"Invalid block: lines {block_start + 1}-{block_end}: {first_line}")
        return block_end
    
    def print_stats(self):
        print("\n## Parsing Statistics\n")
        
        stats_rows = []
        
        total_chats = self.cursor.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        total_messages = self.cursor.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        user_messages = self.cursor.execute("SELECT COUNT(*) FROM messages WHERE message_type = 'User'").fetchone()[0]
        agent_messages = self.cursor.execute("SELECT COUNT(*) FROM messages WHERE message_type = 'Agent'").fetchone()[0]
        
        user_total = self.cursor.execute("SELECT SUM(content_length) FROM messages WHERE message_type = 'User'").fetchone()[0] or 0
        user_avg = self.cursor.execute("SELECT AVG(content_length) FROM messages WHERE message_type = 'User'").fetchone()[0] or 0
        user_avg_int = int(round(user_avg)) if user_avg else 0
        
        avg_messages_per_chat = total_messages / total_chats if total_chats > 0 else 0
        avg_user_per_chat = user_messages / total_chats if total_chats > 0 else 0
        avg_agent_per_chat = agent_messages / total_chats if total_chats > 0 else 0
        
        agent_total = self.cursor.execute("SELECT SUM(content_length) FROM messages WHERE message_type = 'Agent'").fetchone()[0] or 0
        agent_avg = self.cursor.execute("SELECT AVG(content_length) FROM messages WHERE message_type = 'Agent'").fetchone()[0] or 0
        agent_avg_int = int(round(agent_avg)) if agent_avg else 0
        
        overall_total_precalc = user_total + agent_total
        
        overall_total = self.cursor.execute("SELECT SUM(content_length) FROM messages").fetchone()[0] or 0
        overall_avg = self.cursor.execute("SELECT AVG(content_length) FROM messages").fetchone()[0] or 0
        overall_avg_int = int(round(overall_avg)) if overall_avg else 0
        
        avg_content_per_chat = overall_total / total_chats if total_chats > 0 else 0
        avg_content_per_chat_int = int(round(avg_content_per_chat)) if avg_content_per_chat else 0
        
        stats_rows.append(("Total chats", total_chats, None, avg_content_per_chat_int))
        stats_rows.append(("Total messages", total_messages, overall_total, overall_avg_int))
        stats_rows.append(("User messages", user_messages, user_total, user_avg_int))
        stats_rows.append(("Agent messages", agent_messages, agent_total, agent_avg_int))
        stats_rows.append(("Avg messages per chat", int(round(avg_messages_per_chat)), None, None))
        stats_rows.append(("Avg user messages per chat", int(round(avg_user_per_chat)), None, None))
        stats_rows.append(("Avg agent messages per chat", int(round(avg_agent_per_chat)), None, None))
        
        agent_content_rows = []
        for content_type in ['think', 'tool_call', 'text']:
            count = self.cursor.execute(
                "SELECT COUNT(*) FROM messages WHERE content_type = ? AND message_type = 'Agent'", (content_type,)
            ).fetchone()[0]
            if count > 0:
                total_len = self.cursor.execute(
                    "SELECT SUM(content_length) FROM messages WHERE content_type = ? AND message_type = 'Agent'", (content_type,)
                ).fetchone()[0] or 0
                avg_len = self.cursor.execute(
                    "SELECT AVG(content_length) FROM messages WHERE content_type = ? AND message_type = 'Agent'", (content_type,)
                ).fetchone()[0] or 0
                avg_len_int = int(round(avg_len)) if avg_len else 0
                agent_content_rows.append((f"Agent {content_type}", count, total_len, avg_len_int))
        agent_content_rows.sort(key=lambda x: x[1])
        stats_rows.extend(agent_content_rows)
        
        tool_stats = self.cursor.execute("""
            SELECT data_tool_type, COUNT(*) as cnt, SUM(content_length) as total_len, AVG(content_length) as avg_len
            FROM messages 
            WHERE data_tool_type IS NOT NULL
            GROUP BY data_tool_type
            ORDER BY cnt DESC
        """).fetchall()
        
        tool_type_rows = []
        for tool_type, cnt, total_len, avg_len in tool_stats:
            total_len = total_len or 0
            avg_len = avg_len or 0
            avg_len_int = int(round(avg_len)) if avg_len else 0
            tool_type_rows.append((f"Tool type: {tool_type}", cnt, total_len, avg_len_int))
        tool_type_rows.sort(key=lambda x: x[1], reverse=True)
        stats_rows.extend(tool_type_rows)
        
        tool_name_stats = self.cursor.execute("""
            SELECT data_tool_name, COUNT(*) as cnt, SUM(content_length) as total_len, AVG(content_length) as avg_len
            FROM messages 
            WHERE data_tool_name IS NOT NULL
            GROUP BY data_tool_name
            ORDER BY cnt DESC
        """).fetchall()
        
        tool_name_rows = []
        for tool_name, cnt, total_len, avg_len in tool_name_stats:
            total_len = total_len or 0
            avg_len = avg_len or 0
            avg_len_int = int(round(avg_len)) if avg_len else 0
            tool_name_rows.append((f"Tool: {tool_name}", cnt, total_len, avg_len_int))
        tool_name_rows.sort(key=lambda x: x[1], reverse=True)
        stats_rows.extend(tool_name_rows)
        
        daily_stats = self.cursor.execute("""
            SELECT 
                SUBSTR(c.chat_datetime, 1, 10) as chat_date,
                COUNT(m.id) as msg_count,
                SUM(m.content_length) as daily_total
            FROM chats c
            LEFT JOIN messages m ON c.id = m.chat_id
            WHERE c.chat_datetime IS NOT NULL
            GROUP BY SUBSTR(c.chat_datetime, 1, 10)
        """).fetchall()
        
        if daily_stats:
            msg_counts = [msg_count for _, msg_count, _ in daily_stats if msg_count]
            daily_totals = [total for _, _, total in daily_stats if total]
            
            if msg_counts and daily_totals:
                msg_avg = sum(msg_counts) / len(msg_counts)
                msg_min = min(msg_counts)
                msg_max = max(msg_counts)
                
                length_avg = sum(daily_totals) / len(daily_totals)
                length_min = min(daily_totals)
                length_max = max(daily_totals)
                
                stats_rows.append(("Daily min messages", msg_min, int(length_min), None))
                stats_rows.append(("Daily avg messages", int(round(msg_avg)), int(round(length_avg)), None))
                stats_rows.append(("Daily max messages", msg_max, int(length_max), None))
        
        print("| Category | Count | Length | Avg Length |")
        print("|----------|-------|--------|------------|")
        for category, count, total_len, avg_len in stats_rows:
            if count is not None:
                count_str = str(count)
            else:
                count_str = "-"
            if total_len is not None:
                total_str = f"{total_len:,}"
            else:
                total_str = "-"
            if avg_len is not None:
                avg_str = f"{avg_len:,}"
            else:
                avg_str = "-"
            print(f"| {category} | {count_str} | {total_str} | {avg_str} |")
        print()
    
    def _read_line_at(self, filepath: str, line_num: int) -> str:
        """Read a specific line number from file (1-indexed)."""
        with open(filepath, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f, 1):
                if i == line_num:
                    return line
        return None
    
    def _find_start_position(self, filepath: str) -> int:
        """Find the starting position for incremental parsing by checking if user messages at given lines already exist."""
        existing_chats = self.cursor.execute("SELECT COUNT(*) FROM chats WHERE start_line IS NOT NULL").fetchone()[0]
        if existing_chats == 0:
            print("No existing chats found, starting from beginning")
            return 0
        
        print(f"Found {existing_chats} existing chats, comparing source file to database content...")
        first_broken = self._find_first_broken_chat(filepath)
        if first_broken is not None:
            broken_chat_info = self.cursor.execute("""
                SELECT id, title, start_line FROM chats WHERE id = ?
            """, (first_broken,)).fetchone()
            broken_msg_count = self.cursor.execute("""
                SELECT COUNT(*) FROM messages WHERE chat_id >= ?
            """, (first_broken,)).fetchone()[0]
            broken_msg_ids = self.cursor.execute("""
                SELECT id FROM messages WHERE chat_id >= ?
            """, (first_broken,)).fetchall()
            broken_msg_ids_list = [row[0] for row in broken_msg_ids]
            
            orphaned_groups_count = 0
            if broken_msg_ids_list:
                placeholders = ','.join('?' * len(broken_msg_ids_list))
                orphaned_groups_count = self.cursor.execute(f"""
                    SELECT COUNT(DISTINCT group_id) FROM task_groups 
                    WHERE user_msg_id IN ({placeholders})
                """, broken_msg_ids_list).fetchone()[0]
            
            print(f"Found broken chat at ID {first_broken} (title: {broken_chat_info[1] if broken_chat_info else 'unknown'}, line {broken_chat_info[2] if broken_chat_info else 'unknown'})")
            print(f"  This will delete {broken_msg_count} messages from chat {first_broken} onwards")
            if orphaned_groups_count > 0:
                print(f"  Warning: This will orphan {orphaned_groups_count} task groups (will be cleaned up during clustering)")
            
            if broken_msg_ids_list:
                placeholders = ','.join('?' * len(broken_msg_ids_list))
                self.cursor.execute(f"""
                    DELETE FROM task_groups WHERE user_msg_id IN ({placeholders})
                """, broken_msg_ids_list)
                deleted_groups = self.cursor.rowcount
                if deleted_groups > 0:
                    print(f"  Deleted {deleted_groups} task_groups entries referencing deleted messages")
            
            self.cursor.execute("DELETE FROM chats WHERE id >= ?", (first_broken,))
            deleted_chats = self.cursor.rowcount
            self.cursor.execute("DELETE FROM messages WHERE chat_id >= ?", (first_broken,))
            deleted_messages = self.cursor.rowcount
            self.conn.commit()
            print(f"  Deleted {deleted_chats} chats and {deleted_messages} messages")
            
            if first_broken > 1:
                last_good = self.cursor.execute("SELECT end_line FROM chats WHERE id = ?", (first_broken - 1,)).fetchone()
                if last_good and last_good[0]:
                    print(f"  Resuming from line {last_good[0]} (end of last good chat)")
                    return last_good[0] - 1
            print("  Starting from beginning")
            return 0
        
        last_chat = self.cursor.execute("""
            SELECT id, end_line, title FROM chats 
            WHERE start_line IS NOT NULL 
            ORDER BY start_line DESC 
            LIMIT 1
        """).fetchone()
        
        if last_chat and last_chat[1]:
            start_from_line = last_chat[1] - 1
            print(f"Last chat ends at line {last_chat[1]}, checking for new content from line {start_from_line + 1}...")
            return self._find_new_content_start(filepath, start_from_line)
        else:
            print("No end_line found for last chat, checking from beginning...")
            return self._find_new_content_start(filepath, 0)
    
    def _find_new_content_start(self, filepath: str, start_from_line: int) -> int:
        """Find the start of new content by scanning from a given line."""
        checked_count = 0
        with open(filepath, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f, 1):
                if i <= start_from_line:
                    continue
                
                user_match = re.match(r'^_\*\*User\s*\((.+?)\)\*\*_', line)
                if user_match:
                    chat_match = None
                    chat_line_idx = None
                    for j in range(max(1, i - 20), i):
                        check_line = self._read_line_at(filepath, j)
                        if check_line:
                            potential_chat = re.match(r'^# (.+?)\s*\((.+?)\)', check_line)
                            if potential_chat:
                                chat_match = potential_chat
                                chat_line_idx = j
                                break
                    
                    if chat_match:
                        chat_title = chat_match.group(1).strip()
                        chat_dt = chat_match.group(2).strip()
                        existing_chat = self.cursor.execute("""
                            SELECT id FROM chats 
                            WHERE title = ? AND chat_datetime = ? AND start_line = ?
                        """, (chat_title, chat_dt, chat_line_idx)).fetchone()
                        
                        if existing_chat:
                            existing_msg = self.cursor.execute("""
                                SELECT id FROM messages 
                                WHERE start_line = ? AND message_type = 'User' AND chat_id = ?
                            """, (i, existing_chat[0])).fetchone()
                            if existing_msg is None:
                                self.current_chat_id = existing_chat[0]
                                if checked_count > 0:
                                    print(f"  Checked {checked_count} lines, found new user message at line {i}")
                                return i - 1
                        else:
                            if checked_count > 0:
                                print(f"  Checked {checked_count} lines, found new chat at line {chat_line_idx}")
                            return chat_line_idx - 1
                
                checked_count += 1
                if checked_count % 1000 == 0:
                    print(f"  Checked {checked_count} lines for new content...")
                
                chat_match = re.match(r'^# (.+?)\s*\((.+?)\)', line)
                if chat_match:
                    chat_title = chat_match.group(1).strip()
                    chat_dt = chat_match.group(2).strip()
                    existing_chat = self.cursor.execute("""
                        SELECT id FROM chats 
                        WHERE title = ? AND chat_datetime = ? AND start_line = ?
                    """, (chat_title, chat_dt, i)).fetchone()
                    if existing_chat is None:
                        if checked_count > 0:
                            print(f"  Checked {checked_count} lines, found new chat at line {i}")
                        return i - 1
        
        if checked_count > 0:
            print(f"  Checked {checked_count} lines, no new content found")
        
        with open(filepath, 'r', encoding='utf-8') as f:
            line_count = sum(1 for _ in f)
        return line_count
    
    def _find_first_broken_chat(self, filepath: str) -> Optional[int]:
        chats = self.cursor.execute("""
            SELECT id, start_line, end_line, title FROM chats 
            WHERE start_line IS NOT NULL 
            ORDER BY chat_datetime, start_line
        """).fetchall()
        
        total_chats = len(chats)
        for idx, (chat_id, start_line, end_line, expected_title) in enumerate(chats, 1):
            if idx % 100 == 0:
                print(f"  Checked {idx}/{total_chats} chats...")
                sys.stdout.flush()
            stored_line = self._read_line_at(filepath, start_line)
            if stored_line is None:
                print(f"  Chat {chat_id}: line {start_line} is missing (file too short)")
                return chat_id
            
            stored_line = stored_line.strip()
            if not stored_line:
                print(f"  Chat {chat_id}: line {start_line} is empty")
                return chat_id
            
            expected_match = re.match(r'^# (.+?)\s*\((.+?)\)', stored_line)
            if not expected_match:
                print(f"  Chat {chat_id}: line {start_line} doesn't match chat header pattern: {stored_line[:100]}")
                return chat_id
            
            actual_title = expected_match.group(1).strip()
            if actual_title != expected_title:
                print(f"  Chat {chat_id}: title mismatch at line {start_line}")
                print(f"    Expected: {expected_title[:100]}")
                print(f"    Found: {actual_title[:100]}")
                return chat_id
            
            if end_line is not None:
                end_line_content = self._read_line_at(filepath, end_line)
                if end_line_content is None:
                    print(f"  Chat {chat_id}: end_line {end_line} is missing (file too short)")
                    return chat_id
        
        if total_chats > 0:
            print(f"  Checked {total_chats}/{total_chats} chats, all match")
        return None
    
    def close(self):
        self.conn.close()


def main():
    parser = argparse.ArgumentParser(description='Parse chat markdown file into SQLite database')
    parser.add_argument('md_file', help='Path to markdown file to parse')
    parser.add_argument('--db-file', default=None, help='SQLite database path (default: same as md_file with .db extension)')
    parser.add_argument('--max-lines', type=int, default=None, help='Maximum number of lines to parse')
    parser.add_argument('--max-chats', type=int, default=None, help='Maximum number of chats to parse')
    parser.add_argument('--max-messages', type=int, default=None, help='Maximum number of messages to parse')
    parser.add_argument('--start-chat', type=int, default=0, help='Skip first N chats before parsing')
    
    args = parser.parse_args()
    
    from db_utils import derive_db_path_from_file
    db_path = derive_db_path_from_file(args.md_file, args.db_file)
    
    parser_obj = ChatParser(db_path, max_lines=args.max_lines, max_chats=args.max_chats, max_messages=args.max_messages, start_chat=args.start_chat)
    try:
        parser_obj.parse_file(args.md_file)
        parser_obj.print_stats()
    finally:
        parser_obj.close()


if __name__ == '__main__':
    main()
