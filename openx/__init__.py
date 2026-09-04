"""OpenX - Agentic coding CLI inspired by Claude Code."""

__version__ = "0.1.1"
__all__ = [
    "__version__",
    "OpenXConfig",
    "SETTINGS_PATH",
    "load_instructions",
    "build_system_prompt",
    "scaffold_openx_md",
    "reload_instructions",
    "InstructionRegistry",
    "InstructionSet",
    "InstructionLevel",
    "ProjectInfo",
    "detect_project_type",
]

from .config import OpenXConfig, SETTINGS_PATH

from .instructions import (
    load_instructions,
    build_system_prompt,
    scaffold_openx_md,
    reload_instructions,
    InstructionRegistry,
    InstructionSet,
    InstructionLevel,
    ProjectInfo,
    detect_project_type,
)
