#!/usr/bin/env python3
import sqlite3
import argparse
import os
import glob
from contextlib import contextmanager
from typing import Optional


DEFAULT_DB_FILE_ERROR = "No database files found. Please specify --db-file or create a database with parse_chats.py"
DEFAULT_DB_FILE_HELP = "Path to database file (default: searches for *.db files, uses most recent)"


def find_db_file(db_file_arg: Optional[str] = None, error_msg: str = None) -> str:
    """Find database file from argument or auto-detect most recent .db file.
    
    Args:
        db_file_arg: Explicitly provided database file path (takes precedence)
        error_msg: Custom error message if no database found (uses default if None)
    
    Returns:
        Path to database file
    
    Raises:
        ValueError: If no database file is found and none was provided
    """
    if db_file_arg:
        return db_file_arg
    
    db_files = glob.glob('*.db')
    if not db_files:
        raise ValueError(error_msg or DEFAULT_DB_FILE_ERROR)
    
    result = max(db_files, key=os.path.getmtime)
    print(f"Using most recent database: {result}")
    return result


@contextmanager
def db_connection(db_path: str):
    """Context manager for database connections.
    
    Args:
        db_path: Path to database file
    
    Yields:
        Tuple of (connection, cursor)
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        yield conn, cursor
    finally:
        conn.close()


def add_db_file_argument(parser: argparse.ArgumentParser, help_suffix: str = ""):
    """Add --db-file argument to parser with standard help text.
    
    Args:
        parser: ArgumentParser instance to add argument to
        help_suffix: Optional suffix to append to help text
    """
    help_text = DEFAULT_DB_FILE_HELP
    if help_suffix:
        help_text += f" {help_suffix}"
    parser.add_argument('--db-file', default=None, help=help_text)


def derive_db_path_from_file(input_file: str, db_file_arg: Optional[str] = None) -> str:
    """Derive database file path from input file or use provided argument.
    
    Args:
        input_file: Path to input file (e.g., markdown or CSV file)
        db_file_arg: Explicitly provided database file path (takes precedence)
    
    Returns:
        Path to database file
    """
    if db_file_arg:
        return db_file_arg
    return os.path.splitext(input_file)[0] + '.db'


def safe_add_column(cursor, table_name: str, column_def: str):
    """Safely add column to table if it doesn't exist.
    
    Args:
        cursor: Database cursor
        table_name: Name of the table
        column_def: Column definition (e.g., "column_name INTEGER")
    """
    try:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_def}")
    except sqlite3.OperationalError:
        pass

