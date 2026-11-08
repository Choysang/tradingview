"""Pure LazyFrame transforms; declared dependencies determine one execution order.

A factor is a file dropped into `plugins/`: it registers itself with
`@register_factor`, and the engine discovers it once. That keeps parallel work
possible — a new factor never edits the core pipeline.
"""
from collections.abc import Callable
from importlib import util
import logging
from pathlib import Path
import polars as pl

logger = logging.getLogger(__name__)

FACTORS: dict[str, tuple[list[str], list[str], Callable]] = {}
PLUGIN_DIR = Path(__file__).resolve().parent / "plugins"
_DISCOVERED = False


def register_factor(name: str, depends_on: list[str], output_cols: list[str]):
    def decorate(function):
        if name in FACTORS:
            raise ValueError(f"Duplicate factor {name}")
        FACTORS[name] = (depends_on, output_cols, function)
        return function
    return decorate


def discover_plugins(force: bool = False) -> list[str]:
    """Import every module in plugins/. Errors are logged, never fatal."""
    global _DISCOVERED
    if _DISCOVERED and not force:
        return sorted(FACTORS)
    _DISCOVERED = True
    if not PLUGIN_DIR.is_dir():
        return sorted(FACTORS)
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"pulse_factor_{path.stem}"
        try:
            spec = util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                continue
            module = util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as error:  # noqa: BLE001 - a broken plugin must not stop the app
            logger.warning("Factor plugin %s failed to load: %s", path.name, error)
    return sorted(FACTORS)


def execute_factors(frame: pl.LazyFrame) -> pl.LazyFrame:
    discover_plugins()
    completed = set()
    while len(completed) < len(FACTORS):
        ready = [name for name, (deps, _, _) in FACTORS.items() if name not in completed and set(deps) <= completed]
        if not ready:
            raise ValueError("Factor graph contains a cycle or unknown dependency")
        for name in ready:
            _, columns, function = FACTORS[name]
            frame = function(frame)
            if not set(columns) <= set(frame.collect_schema().names()):
                raise ValueError(f"Factor {name} did not emit its declared columns")
            completed.add(name)
    return frame
