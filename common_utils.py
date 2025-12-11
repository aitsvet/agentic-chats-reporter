#!/usr/bin/env python3
import sys


class ProgressReporter:
    """Helper class for reporting progress during long-running operations."""
    
    def __init__(self, total: int, interval: int = 100, show_first: bool = True):
        """Initialize progress reporter.
        
        Args:
            total: Total number of items to process
            interval: Report progress every N items (default: 100)
            show_first: Whether to show progress for first item (default: True)
        """
        self.total = total
        self.interval = interval
        self.show_first = show_first
        self.count = 0
    
    def update(self, **kwargs):
        """Update progress with optional status information.
        
        Args:
            **kwargs: Optional status metrics to display (e.g., skipped=5, processed=10)
        """
        self.count += 1
        should_report = (self.count % self.interval == 0) or (self.show_first and self.count == 1)
        
        if should_report:
            status_parts = [f"{k}: {v}" for k, v in kwargs.items() if v is not None]
            status_str = ", ".join(status_parts) if status_parts else ""
            print(f"  Progress: {self.count}/{self.total}" + (f" ({status_str})" if status_str else ""))
            sys.stdout.flush()

