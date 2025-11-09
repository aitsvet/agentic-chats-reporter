#!/usr/bin/env python3
import sys
import os
import argparse
import subprocess
from pathlib import Path


def get_file_stats(file_path):
    stats = {}
    if file_path.exists():
        stats['size'] = file_path.stat().st_size
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            stats['lines'] = sum(1 for _ in f)
    else:
        stats['size'] = 0
        stats['lines'] = 0
    return stats


def run_command(cmd, output_file=None, stdout_filter=None):
    if output_file:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', bufsize=1)
        output_lines = []
        for line in iter(process.stdout.readline, ''):
            print(line, end='')
            sys.stdout.flush()
            output_lines.append(line)
        process.wait()
        if process.returncode != 0:
            print(f"\nError: Command failed with return code {process.returncode}", file=sys.stderr)
            sys.exit(process.returncode)
        full_output = ''.join(output_lines)
        if stdout_filter:
            full_output = stdout_filter(full_output)
        if full_output:
            with open(output_file, 'a', encoding='utf-8') as f:
                f.write(full_output)
        return full_output
    else:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            sys.exit(result.returncode)
        return ""


def filter_parse_chats_output(output):
    lines = output.split('\n')
    result = []
    in_stats = False
    for line in lines:
        if '| Category | Count | Length | Avg Length |' in line:
            in_stats = True
        if in_stats:
            if line.strip().startswith('|'):
                result.append(line)
            elif line.strip() == '':
                if result:
                    result.append(line)
                in_stats = False
    return '\n'.join(result) + '\n' if result else ''


def filter_parse_usage_output(output):
    lines = output.split('\n')
    result = []
    in_stats = False
    for line in lines:
        if '| Metric | Requests |' in line:
            in_stats = True
        if in_stats:
            if line.strip().startswith('|'):
                result.append(line)
            elif line.strip() == '':
                if result:
                    result.append(line)
                in_stats = False
    return '\n'.join(result) + '\n' if result else ''


def filter_cluster_output(output):
    lines = output.split('\n')
    result = []
    capture = False
    for line in lines:
        if '## Task Clustering Report' in line:
            capture = True
        if capture:
            result.append(line)
    return '\n'.join(result) + '\n' if result else ''


def filter_correlation_output(output):
    lines = output.split('\n')
    result = []
    capture = False
    for line in lines:
        if '## Chat-Usage Correlation Report' in line:
            capture = True
        if capture:
            result.append(line)
    return '\n'.join(result) + '\n' if result else ''


def main():
    parser = argparse.ArgumentParser(description='Run full pipeline: parse chats, parse usage (optional), embed tasks, cluster, and generate summaries')
    parser.add_argument('--md-file', default='EXAMPLE.md', help='Path to chat markdown file (default: EXAMPLE.md)')
    parser.add_argument('--csv-file', default=None, help='Path to usage CSV file (optional, enables usage correlation)')
    parser.add_argument('--output', default=None, help='Output report file (default: <md_file_base>-REPORT.md)')
    parser.add_argument('--force', action='store_true', help='Force re-processing of all steps even if data already exists')
    
    args = parser.parse_args()
    
    md_file = Path(args.md_file)
    csv_file = Path(args.csv_file) if args.csv_file else None
    
    if not md_file.exists():
        print(f"Error: Markdown file not found: {md_file}", file=sys.stderr)
        sys.exit(1)
    
    if csv_file and not csv_file.exists():
        print(f"Error: CSV file not found: {csv_file}", file=sys.stderr)
        sys.exit(1)
    
    db_path = md_file.with_suffix('.db')
    output_file = args.output
    if output_file is None:
        output_file = md_file.stem + '-REPORT.md'
    
    script_dir = Path(__file__).parent
    
    md_stats = get_file_stats(md_file)
    csv_stats = get_file_stats(csv_file) if csv_file else {'size': 0, 'lines': 0}
    
    has_usage = csv_file is not None
    total_steps = 8 if has_usage else 6
    
    print(f"Starting analysis pipeline...")
    print(f"Input files:")
    print(f"  Markdown: {md_file} ({md_stats['size']:,} bytes, {md_stats['lines']:,} lines)")
    if has_usage:
        print(f"  CSV: {csv_file} ({csv_stats['size']:,} bytes, {csv_stats['lines']:,} lines)")
    else:
        print(f"  CSV: (not provided, skipping usage correlation)")
    print(f"Output: {output_file}")
    print()
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f"# Agent Chats Report for {md_file.name}\n\n")
        f.write(f"## Input Files\n\n")
        f.write(f"- **Chat markdown:** `{md_file}` ({md_stats['size']:,} bytes, {md_stats['lines']:,} lines)\n")
        if has_usage:
            f.write(f"- **Usage CSV:** `{csv_file}` ({csv_stats['size']:,} bytes, {csv_stats['lines']:,} lines)\n")
        f.write(f"- **Database:** `{db_path}`\n\n")
    
    step_num = 1
    print(f"Step {step_num}/{total_steps}: Parsing chat markdown...")
    with open(output_file, 'a', encoding='utf-8') as f:
        f.write("## Chat Statistics\n\n")
    run_command([
        sys.executable, '-u', str(script_dir / 'parse_chats.py'),
        str(md_file),
        '--db-file', str(db_path)
    ], output_file, filter_parse_chats_output)
    print(f"  ✓ Complete\n")
    
    if has_usage:
        step_num += 1
        print(f"Step {step_num}/{total_steps}: Parsing usage CSV...")
        with open(output_file, 'a', encoding='utf-8') as f:
            f.write('<div style="page-break-before: always;"></div>\n\n')
            f.write("## Usage Statistics\n\n")
        cmd = [
            sys.executable, '-u', str(script_dir / 'parse_usage.py'),
            str(csv_file),
            '--db-file', str(db_path)
        ]
        if args.force:
            cmd.append('--force')
        run_command(cmd, output_file, filter_parse_usage_output)
        print(f"  ✓ Complete\n")
        
        step_num += 1
        print(f"Step {step_num}/{total_steps}: Correlating chats with usage...")
        run_command([
            sys.executable, '-u', str(script_dir / 'correlate_chats_usage.py'),
            '--db-file', str(db_path)
        ], output_file, filter_correlation_output)
        print(f"  ✓ Complete\n")
    
    step_num += 1
    print(f"Step {step_num}/{total_steps}: Extracting embeddings...")
    run_command([
        sys.executable, '-u', str(script_dir / 'embed_tasks.py'),
        '--db-file', str(db_path)
    ])
    print(f"  ✓ Complete\n")
    
    step_num += 1
    print(f"Step {step_num}/{total_steps}: Clustering tasks...")
    cmd = [
        sys.executable, '-u', str(script_dir / 'cluster_tasks.py'),
        '--db-file', str(db_path)
    ]
    if args.force:
        cmd.append('--force')
    stdout = run_command(cmd, output_file, filter_cluster_output)
    print(f"  ✓ Complete\n")
    
    step_num += 1
    print(f"Step {step_num}/{total_steps}: Generating group summaries...")
    cmd = [
        sys.executable, '-u', str(script_dir / 'generate_group_summaries.py'),
        '--db-file', str(db_path),
        '--output', output_file + '.groups'
    ]
    if args.force:
        cmd.append('--force')
    run_command(cmd)
    print(f"  ✓ Complete\n")
    
    step_num += 1
    print(f"Step {step_num}/{total_steps}: Generating specifications...")
    specs_file = output_file + '.specs'
    cmd = [
        sys.executable, '-u', str(script_dir / 'generate_specs.py'),
        '--db-file', str(db_path),
        '--output', specs_file
    ]
    if args.force:
        cmd.append('--force')
    run_command(cmd)
    print(f"  ✓ Complete\n")
    
    with open(output_file, 'a', encoding='utf-8') as f:
        f.write('<div style="page-break-before: always;"></div>\n\n')
        
        if os.path.exists(specs_file):
            with open(specs_file, 'r', encoding='utf-8') as spec_f:
                specs_content = spec_f.read()
            if specs_content.strip():
                f.write(specs_content)
                f.write("\n\n")
            os.remove(specs_file)
        
        f.write('<div style="page-break-before: always;"></div>\n\n')
        f.write("# Task Summaries\n\n")
    
    if os.path.exists(output_file + '.groups'):
        with open(output_file + '.groups', 'r', encoding='utf-8') as f:
            groups_content = f.read()
        lines = groups_content.split('\n')
        if lines and lines[0].strip() == '# Task Summaries':
            groups_content = '\n'.join(lines[1:]).lstrip('\n')
        
        with open(output_file, 'a', encoding='utf-8') as f:
            f.write(groups_content)
        os.remove(output_file + '.groups')
    
    with open(output_file, 'a', encoding='utf-8') as f:
        f.write("\n---\n\n")
        f.write(f"*Report generated from {md_file.name}*\n")
    
    print(f"\nPipeline complete!")
    print(f"Report written to: {output_file}")


if __name__ == '__main__':
    main()
