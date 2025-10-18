"""
Progress indicator utilities for CLI.
"""

import time
from typing import Optional, Callable
from datetime import datetime
import click


class ProgressTracker:
    """
    Progress tracker with stages and ETA estimation.
    
    Stages:
    1. Dependency Analysis (40% of time)
    2. Module Clustering (20% of time)
    3. Documentation Generation (30% of time)
    4. HTML Generation (5% of time, optional)
    5. Finalization (5% of time)
    """
    
    # Stage weights (percentage of total time)
    STAGE_WEIGHTS = {
        1: 0.40,  # Dependency Analysis
        2: 0.20,  # Module Clustering
        3: 0.30,  # Documentation Generation
        4: 0.05,  # HTML Generation (optional)
        5: 0.05,  # Finalization
    }
    
    STAGE_NAMES = {
        1: "Dependency Analysis",
        2: "Module Clustering",
        3: "Documentation Generation",
        4: "HTML Generation",
        5: "Finalization",
    }
    
    def __init__(self, total_stages: int = 5, verbose: bool = False):
        """
        Initialize progress tracker.
        
        Args:
            total_stages: Number of stages
            verbose: Enable verbose output
        """
        self.total_stages = total_stages
        self.current_stage = 0
        self.stage_progress = 0.0
        self.start_time = time.time()
        self.verbose = verbose
        self.current_stage_start = self.start_time
    
    def start_stage(self, stage: int, description: Optional[str] = None):
        """
        Start a new stage.
        
        Args:
            stage: Stage number (1-5)
            description: Optional custom description
        """
        self.current_stage = stage
        self.stage_progress = 0.0
        self.current_stage_start = time.time()
        
        stage_name = description or self.STAGE_NAMES.get(stage, f"Stage {stage}")
        
        if self.verbose:
            elapsed = self._format_elapsed()
            click.secho(
                f"\n[{elapsed}] Phase {stage}/{self.total_stages}: {stage_name}",
                fg="blue",
                bold=True
            )
        else:
            click.secho(
                f"[{stage}/{self.total_stages}] {stage_name}",
                fg="blue",
                bold=True
            )
    
    def update_stage(self, progress: float, message: Optional[str] = None):
        """
        Update progress within current stage.
        
        Args:
            progress: Progress percentage (0.0 to 1.0)
            message: Optional progress message
        """
        self.stage_progress = min(1.0, max(0.0, progress))
        
        if self.verbose and message:
            elapsed = self._format_elapsed()
            click.echo(f"[{elapsed}]   {message}")
    
    def complete_stage(self, message: Optional[str] = None):
        """
        Complete current stage.
        
        Args:
            message: Optional completion message
        """
        self.stage_progress = 1.0
        
        if self.verbose:
            elapsed = self._format_elapsed()
            stage_time = time.time() - self.current_stage_start
            stage_name = self.STAGE_NAMES.get(self.current_stage, f"Stage {self.current_stage}")
            click.secho(
                f"[{elapsed}]   {stage_name} complete ({stage_time:.1f}s)",
                fg="green"
            )
            if message:
                click.echo(f"[{elapsed}]   {message}")
    
    def get_overall_progress(self) -> float:
        """
        Get overall progress percentage.
        
        Returns:
            Progress (0.0 to 1.0)
        """
        completed_weight = sum(
            self.STAGE_WEIGHTS.get(s, 0)
            for s in range(1, self.current_stage)
        )
        
        current_weight = self.STAGE_WEIGHTS.get(self.current_stage, 0) * self.stage_progress
        
        return completed_weight + current_weight
    
    def _format_elapsed(self) -> str:
        """Format elapsed time."""
        elapsed = time.time() - self.start_time
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        
        if minutes > 0:
            return f"{minutes:02d}:{seconds:02d}"
        else:
            return f"00:{seconds:02d}"
    
    def get_eta(self) -> Optional[str]:
        """
        Estimate time remaining.
        
        Returns:
            ETA string or None if cannot estimate
        """
        elapsed = time.time() - self.start_time
        progress = self.get_overall_progress()
        
        if progress <= 0.0:
            return None
        
        total_estimated = elapsed / progress
        remaining = total_estimated - elapsed
        
        if remaining < 0:
            return "< 1 min"
        
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        
        if minutes > 60:
            hours = minutes // 60
            minutes = minutes % 60
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"


class ModuleProgressBar:
    """Progress bar for module-by-module generation."""
    
    def __init__(self, total_modules: int, verbose: bool = False):
        """
        Initialize module progress bar.
        
        Args:
            total_modules: Total number of modules to process
            verbose: Enable verbose output
        """
        self.total_modules = total_modules
        self.current_module = 0
        self.verbose = verbose
        self.bar = None
        
        if not verbose:
            self.bar = click.progressbar(
                length=total_modules,
                label="Generating modules",
                show_eta=True,
                show_percent=True,
            )
            self.bar.__enter__()
    
    def update(self, module_name: str, cached: bool = False):
        """
        Update progress for a module.
        
        Args:
            module_name: Name of the module
            cached: Whether the module was loaded from cache
        """
        self.current_module += 1
        
        if self.verbose:
            status = "✓ (cached)" if cached else "⟳ (generating)"
            click.echo(f"  [{self.current_module}/{self.total_modules}] {module_name}... {status}")
        elif self.bar:
            self.bar.update(1)
    
    def finish(self):
        """Finish progress bar."""
        if self.bar:
            self.bar.__exit__(None, None, None)
            self.bar = None

