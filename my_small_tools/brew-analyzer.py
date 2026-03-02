#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "rich>=13.0.0",
# ]
# ///

"""
Homebrew Storage Analyzer
Analyzes brew installations to identify storage hogs, dependency bloat, and safe removal candidates.
"""

import json
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional
from rich.console import Console
from rich.table import Table
from rich.tree import Tree
from rich.panel import Panel
from rich.progress import Progress
from rich import box

console = Console()

@dataclass
class Package:
    name: str
    version: str
    desc: str
    direct_size: int = 0  # bytes
    deps: List[str] = field(default_factory=list)
    reverse_deps: List[str] = field(default_factory=list)
    installed_as_dependency: bool = False
    installed_on_request: bool = False
    is_cask: bool = False
    
    @property
    def human_size(self) -> str:
        return format_size(self.direct_size)
    
    @property
    def is_leaf(self) -> bool:
        """Nothing depends on this package"""
        return len(self.reverse_deps) == 0
    
    @property
    def is_orphan(self) -> bool:
        """Auto-installed dependency that nothing depends on (shouldn't happen but can with broken brew)"""
        return self.installed_as_dependency and not self.installed_on_request and self.is_leaf

def format_size(bytes_size: int) -> str:
    """Convert bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} TB"

def run_brew(*args) -> str:
    """Run brew command and return stdout"""
    result = subprocess.run(
        ['brew'] + list(args),
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        console.print(f"[red]Error running brew {' '.join(args)}: {result.stderr}[/red]")
        return ""
    return result.stdout

def get_directory_size(path: Path) -> int:
    """Calculate total size of directory in bytes"""
    if not path.exists():
        return 0
    
    total = 0
    try:
        # If it's a symlink in cellar pointing elsewhere, count the target
        if path.is_symlink():
            path = path.resolve()
        
        result = subprocess.run(
            ['du', '-sk', str(path)],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            size_kb = int(result.stdout.split()[0])
            return size_kb * 1024
    except Exception:
        # Fallback to manual calculation
        try:
            for item in path.rglob('*'):
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
        except Exception:
            pass
    return total

def collect_formulae() -> Dict[str, Package]:
    """Collect all installed formulae with metadata"""
    console.print("[bold blue]Collecting formula information...[/bold blue]")
    
    # Get list of installed formulae
    output = run_brew('list', '--formula', '-1')
    if not output:
        return {}
    
    formulae = [f for f in output.strip().split('\n') if f]
    
    # Get detailed info in batches to avoid command line length limits  
    packages = {}
    batch_size = 100
    
    with Progress(console=console) as progress:
        task = progress.add_task("Fetching metadata...", total=len(formulae))
        
        for i in range(0, len(formulae), batch_size):
            batch = formulae[i:i+batch_size]
            json_output = run_brew('info', '--json=v2', *batch)
            
            if json_output:
                data = json.loads(json_output)
                for formula_data in data.get('formulae', []):
                    name = formula_data['name']
                    installed = formula_data.get('installed', [{}])[0]
                    
                    pkg = Package(
                        name=name,
                        version=installed.get('version', 'unknown'),
                        desc=formula_data.get('desc', 'No description'),
                        deps=formula_data.get('dependencies', []),
                        installed_as_dependency=installed.get('installed_as_dependency', False),
                        installed_on_request=installed.get('installed_on_request', False),
                        is_cask=False
                    )
                    packages[name] = pkg
            
            progress.update(task, advance=len(batch))
    
    # Calculate reverse dependencies
    console.print("[bold blue]Building dependency graph...[/bold blue]")
    for name, pkg in packages.items():
        for dep in pkg.deps:
            if dep in packages and name not in packages[dep].reverse_deps:
                packages[dep].reverse_deps.append(name)
    
    # Get sizes
    console.print("[bold blue]Calculating package sizes...[/bold blue]")
    cellar = Path(run_brew('--cellar').strip())
    
    with Progress(console=console) as progress:
        task = progress.add_task("Sizing packages...", total=len(packages))
        
        for name, pkg in packages.items():
            pkg_path = cellar / name
            if pkg_path.exists():
                # Get the latest installed version
                versions = sorted(pkg_path.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                if versions:
                    pkg.direct_size = get_directory_size(versions[0])
            progress.update(task, advance=1)
    
    return packages

def collect_casks() -> Dict[str, Package]:
    """Collect installed casks (limited sizing capability)"""
    console.print("[bold blue]Checking for casks...[/bold blue]")
    
    output = run_brew('list', '--cask', '-1')
    if not output:
        return {}
    
    cask_names = [c for c in output.strip().split('\n') if c]
    packages = {}
    
    if not cask_names:
        return packages
    
    # Get cask info
    batch_size = 50
    for i in range(0, len(cask_names), batch_size):
        batch = cask_names[i:i+batch_size]
        json_output = run_brew('info', '--json=v2', '--cask', *batch)
        
        if json_output:
            data = json.loads(json_output)
            for cask_data in data.get('casks', []):
                name = cask_data['token']
                
                # Try to estimate size from app locations
                size = 0
                artifacts = cask_data.get('artifacts', [])
                for artifact in artifacts:
                    if isinstance(artifact, dict):
                        for key in ['app', 'pkg', 'binary']:
                            if key in artifact:
                                paths = artifact[key] if isinstance(artifact[key], list) else [artifact[key]]
                                for p in paths:
                                    if isinstance(p, str):
                                        app_path = Path("/Applications") / p
                                        if app_path.exists():
                                            size += get_directory_size(app_path)
                
                packages[name] = Package(
                    name=name,
                    version=cask_data.get('version', 'unknown'),
                    desc=cask_data.get('desc', 'No description'),
                    direct_size=size,
                    installed_on_request=True,
                    is_cask=True
                )
    
    return packages

def analyze_storage(packages: Dict[str, Package]) -> None:
    """Storage usage breakdown"""
    # Sort by size
    sorted_pkgs = sorted(packages.values(), key=lambda x: x.direct_size, reverse=True)
    
    table = Table(
        title="📦 Top Storage Consumers",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        caption="Largest packages by disk usage"
    )
    table.add_column("Package", style="bold")
    table.add_column("Size", justify="right")
    table.add_column("Type", justify="center")
    table.add_column("Description", max_width=50)
    
    total_size = 0
    for pkg in sorted_pkgs[:15]:
        total_size += pkg.direct_size
        pkg_type = "🍺 Cask" if pkg.is_cask else "⚗️ Formula"
        table.add_row(
            pkg.name,
            pkg.human_size,
            pkg_type,
            pkg.desc[:50] + "..." if len(pkg.desc) > 50 else pkg.desc
        )
    
    console.print(table)
    console.print(f"[bold]Total displayed:[/bold] {format_size(total_size)}")
    console.print()

def analyze_leaves(packages: Dict[str, Package]) -> None:
    """Find packages safe to remove (nothing depends on them)"""
    leaves = [p for p in packages.values() if p.is_leaf]
    leaves.sort(key=lambda x: x.direct_size, reverse=True)
    
    if not leaves:
        console.print("[yellow]No leaf packages found.[/yellow]")
        return
    
    table = Table(
        title="🍃 Safe to Remove (Leaf Packages)",
        box=box.ROUNDED,
        caption="Nothing depends on these. Uninstalling frees up exact size shown."
    )
    table.add_column("Package", style="bold green")
    table.add_column("Size", justify="right")
    table.add_column("Auto-installed?", justify="center")
    table.add_column("Description", max_width=40)
    
    candidates = []
    for pkg in leaves:
        auto = "✅ Yes" if pkg.installed_as_dependency else "👤 No"
        table.add_row(pkg.name, pkg.human_size, auto, (pkg.desc or "")[:40])
        if pkg.installed_as_dependency:
            candidates.append(pkg.name)
    
    console.print(table)
    
    if candidates:
        cmd = f"brew uninstall {' '.join(candidates[:10])}"
        console.print(f"[dim]Quick clean command:[/dim] [bold]{cmd}[/bold]")
    console.print()

def analyze_dependency_bloat(packages: Dict[str, Package]) -> None:
    """Find small packages that bring in heavy dependencies"""
    bloat_risk = []
    
    for pkg in packages.values():
        if pkg.is_cask or pkg.direct_size == 0:
            continue
            
        dep_total_size = sum(packages[d].direct_size for d in pkg.deps if d in packages)
        
        # Small package (< 10MB) with heavy deps (> 100MB)
        if pkg.direct_size < 10 * 1024 * 1024 and dep_total_size > 100 * 1024 * 1024:
            ratio = dep_total_size / pkg.direct_size if pkg.direct_size > 0 else 0
            bloat_risk.append((pkg, dep_total_size, ratio))
    
    if not bloat_risk:
        return
        
    bloat_risk.sort(key=lambda x: x[2], reverse=True)
    
    table = Table(
        title="⚠️  Dependency Bloat Alert",
        box=box.ROUNDED,
        caption="Small tools pulling in large dependency trees"
    )
    table.add_column("Package", style="bold yellow")
    table.add_column("Own Size", justify="right")
    table.add_column("Deps Size", justify="right")
    table.add_column("Ratio", justify="right")
    table.add_column("Dependencies", max_width=30)
    
    for pkg, dep_size, ratio in bloat_risk[:10]:
        deps_str = ", ".join(pkg.deps[:3]) + ("..." if len(pkg.deps) > 3 else "")
        table.add_row(
            pkg.name,
            pkg.human_size,
            format_size(dep_size),
            f"{ratio:.1f}x",
            deps_str
        )
    
    console.print(table)
    console.print()

def analyze_heavy_deps(packages: Dict[str, Package]) -> None:
    """Show packages with most dependents (critical infrastructure)"""
    # Skip casks for this analysis
    formulae = {k: v for k, v in packages.items() if not v.is_cask}
    
    heavy_used = sorted(formulae.values(), key=lambda x: len(x.reverse_deps), reverse=True)
    
    if not heavy_used or not heavy_used[0].reverse_deps:
        return
    
    table = Table(
        title="🔗 Critical Dependencies (High Impact)",
        box=box.ROUNDED,
        caption="These packages are required by many others. Avoid uninstalling."
    )
    table.add_column("Package", style="bold red")
    table.add_column("Used By", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Reverse Dependencies", max_width=40)
    
    for pkg in heavy_used[:10]:
        if not pkg.reverse_deps:
            break
        users = ", ".join(pkg.reverse_deps[:5])
        if len(pkg.reverse_deps) > 5:
            users += f" (+{len(pkg.reverse_deps) - 5} more)"
        table.add_row(
            pkg.name,
            str(len(pkg.reverse_deps)),
            pkg.human_size,
            users
        )
    
    console.print(table)
    console.print()

def show_cleanup_suggestions(packages: Dict[str, Package]) -> None:
    """Generate actionable cleanup commands"""
    console.print(Panel.fit(
        "[bold cyan]🧹 Quick Cleanup Suggestions[/bold cyan]",
        border_style="cyan"
    ))
    
    suggestions = []
    
    # 1. Prune orphaned dependencies
    orphans = [p.name for p in packages.values() if p.is_orphan]
    if orphans:
        suggestions.append(("Unused dependencies", f"brew uninstall {' '.join(orphans)}", 
                          f"Frees ~{format_size(sum(packages[o].direct_size for o in orphans))}"))
    
    # 2. Cleanup cache
    suggestions.append(("Clean download cache", "brew cleanup -s", "Removes old downloads & versions"))
    
    # 3. Autoremove suggestion (built into newer brew)
    suggestions.append(("Autoremove unused", "brew autoremove -n", "Dry-run: see what would be removed"))
    
    for title, cmd, desc in suggestions:
        console.print(f"[bold]{title}:[/bold] [dim]{desc}[/dim]")
        console.print(f"   [green]❯[/green] [bold white]{cmd}[/bold white]")
        console.print()

def show_dependency_tree_for_heavy_hitters(packages: Dict[str, Package]) -> None:
    """Show tree view of largest packages and their dependencies"""
    formulae = {k: v for k, v in packages.items() if not v.is_cask}
    top_heavy = sorted(formulae.values(), key=lambda x: x.direct_size, reverse=True)[:3]
    
    if not top_heavy:
        return
    
    console.print(Panel.fit("[bold]🌳 Dependency Trees for Storage Hogs[/bold]", border_style="blue"))
    
    for pkg in top_heavy:
        if pkg.direct_size < 50 * 1024 * 1024:  # Only show if > 50MB
            continue
            
        tree = Tree(f"[bold]{pkg.name}[/bold] ({pkg.human_size})")
        
        def add_deps(parent_tree, pkg_name, visited, depth=0):
            if depth > 2 or pkg_name in visited:  # Limit depth to avoid spam
                return
            visited.add(pkg_name)
            
            if pkg_name not in formulae:
                return
                
            for dep in formulae[pkg_name].deps[:5]:  # Limit to first 5
                if dep in formulae:
                    dep_pkg = formulae[dep]
                    size_info = f" [dim]{dep_pkg.human_size}[/dim]"
                    branch = parent_tree.add(f"{dep}{size_info}")
                    if depth < 2:
                        add_deps(branch, dep, visited.copy(), depth + 1)
                
            remaining = len(formulae[pkg_name].deps) - 5
            if remaining > 0:
                parent_tree.add(f"[dim]... and {remaining} more[/dim]")
        
        add_deps(tree, pkg.name, set())
        console.print(tree)
        console.print()

def main():
    console.print(Panel.fit(
        "[bold green]🍺 Homebrew Storage Analyzer[/bold green]\n"
        "[dim]Deep analysis of your brew installation to save storage[/dim]",
        border_style="green"
    ))
    
    # Check if brew is installed
    if not run_brew('--version'):
        console.print("[red]Error: Homebrew not found in PATH[/red]")
        return
    
    # Collect data
    formulae = collect_formulae()
    casks = collect_casks()
    all_packages = {**formulae, **casks}
    
    if not all_packages:
        console.print("[yellow]No packages found![/yellow]")
        return
    
    # Total size
    total_bytes = sum(p.direct_size for p in all_packages.values())
    console.print(f"\n[bold]Total Installation Size:[/bold] {format_size(total_bytes)}")
    console.print(f"[dim]Formulae: {len(formulae)}, Casks: {len(casks)}[/dim]\n")
    
    # Analyses
    analyze_storage(all_packages)
    analyze_leaves(all_packages)
    analyze_dependency_bloat(formulae)
    analyze_heavy_deps(formulae)
    show_dependency_tree_for_heavy_hitters(formulae)
    show_cleanup_suggestions(all_packages)
    
    # Summary stats
    autop_count = sum(1 for p in formulae.values() if p.installed_as_dependency)
    console.print(Panel.fit(
        f"[bold]Summary:[/bold] {autop_count} auto-installed deps, "
        f"{len([p for p in formulae.values() if p.is_leaf])} removable leaf packages",
        border_style="dim"
    ))

if __name__ == "__main__":
    main()