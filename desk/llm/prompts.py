"""Versioned prompt files in desk/prompts/.

Format:
    ---
    version: briefing.v1
    ---
    ## system
    ...
    ## user
    ... ${variables} ...

Variables use string.Template syntax (${name}) so the braces of fact placeholders, which
the model is asked to write as {fact_id}, pass through untouched. Changing a prompt means
changing its version string, which is recorded on every artifact it produces.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_FRONT_MATTER = re.compile(r"^---\s*\nversion:\s*(?P<version>\S+)\s*\n---\s*\n", re.MULTILINE)
_SECTION = re.compile(r"^## (system|user)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Prompt:
    version: str
    system: str
    user: str


def load_prompt(name: str, variables: dict[str, str], prompts_dir: Path = PROMPTS_DIR) -> Prompt:
    text = (prompts_dir / f"{name}.md").read_text(encoding="utf-8")
    header = _FRONT_MATTER.match(text)
    if header is None:
        raise ValueError(f"prompt {name} has no version front matter")
    parts = _SECTION.split(text[header.end() :])
    sections = dict(zip(parts[1::2], (p.strip() for p in parts[2::2]), strict=True))
    if set(sections) != {"system", "user"}:
        raise ValueError(f"prompt {name} needs exactly one system and one user section")
    return Prompt(
        version=header["version"],
        system=Template(sections["system"]).substitute(variables),
        user=Template(sections["user"]).substitute(variables),
    )
