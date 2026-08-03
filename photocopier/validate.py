"""Validate date resolution against a library that is already filed correctly.

An existing, hand-filed photo library is a labelled dataset: the folder a file sits in
is the answer a human gave. Running the resolver over it and comparing is the only way
to know the rules work on real material rather than on fixtures written to match them.

This is how the current chain was tuned. On 2268 files it agreed with the manual filing
on 99.16% and disagreed on none, with the remainder having no recoverable date. An
earlier filename-only version disagreed on 3, all of them photos taken within two hours
of a month boundary, which is what first exposed that Pixel filenames are UTC.

Nothing here writes anything. It reads a directory and reports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ClassifyConfig
from .dates import resolve

YEAR = re.compile(r"^\d{4}$")
MONTH = re.compile(r"^\d{1,2}$")

IGNORED = {".DS_Store", "Thumbs.db", "desktop.ini"}


@dataclass
class ValidationResult:
    agree: int = 0
    disagree: int = 0
    unresolved: int = 0
    skipped: int = 0
    tiers: dict[str, int] = field(default_factory=dict)
    disagreements: list[str] = field(default_factory=list)
    unresolved_examples: list[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return self.agree + self.disagree + self.unresolved

    @property
    def agreement(self) -> float:
        return self.agree / self.checked if self.checked else 0.0


def _walk(root: Path):
    """Yield (file, expected_year, expected_month_or_None) for a YYYY[/MM] tree."""
    for year_dir in sorted(p for p in root.iterdir() if p.is_dir() and YEAR.match(p.name)):
        year = int(year_dir.name)
        month_dirs = [p for p in year_dir.iterdir() if p.is_dir() and MONTH.match(p.name)]
        for entry in sorted(year_dir.iterdir()):
            if entry.is_file():
                yield entry, year, None  # flat year layout, as the video share uses
        for month_dir in sorted(month_dirs):
            for entry in sorted(month_dir.iterdir()):
                if entry.is_file():
                    yield entry, year, int(month_dir.name)


def validate(root: Path, classify: ClassifyConfig, *, limit: int = 0) -> ValidationResult:
    result = ValidationResult()

    for index, (path, year, month) in enumerate(_walk(root)):
        if limit and index >= limit:
            break
        if path.name in IGNORED or path.name.startswith("."):
            result.skipped += 1
            continue

        kind = classify.kind(path.name)
        if kind == "unknown":
            result.skipped += 1
            continue

        # The source path is deliberately empty: this measures what EXIF and the
        # filename can do on their own. Passing the folder being validated against
        # would let the srcpath tier read the answer off the label.
        when, tier = resolve(path, "", path.name, try_exif=kind == "photo")
        result.tiers[tier] = result.tiers.get(tier, 0) + 1

        if when is None:
            result.unresolved += 1
            if len(result.unresolved_examples) < 10:
                result.unresolved_examples.append(str(path.relative_to(root)))
            continue

        matches = when.year == year and (month is None or when.month == month)
        if matches:
            result.agree += 1
        else:
            result.disagree += 1
            if len(result.disagreements) < 10:
                filed = f"{year}/{month:02d}" if month else f"{year}"
                result.disagreements.append(
                    f"filed {filed} -> resolved {when} via {tier} : {path.name}"
                )

    return result


def render(result: ValidationResult, root: Path) -> str:
    lines = [
        f"validated {root}",
        "",
        f"  files checked   {result.checked}",
        f"  agree           {result.agree}  ({result.agreement * 100:.2f}%)",
        f"  disagree        {result.disagree}",
        f"  unresolved      {result.unresolved}  -> would go to triage",
    ]
    if result.skipped:
        lines.append(f"  skipped         {result.skipped}  (not a known photo or video type)")

    if result.tiers:
        mix = ", ".join(f"{k}={v}" for k, v in sorted(result.tiers.items()))
        lines.append(f"  resolved by     {mix}")

    if result.disagreements:
        lines.append("\ndisagreements:")
        lines += [f"  {d}" for d in result.disagreements]
    if result.unresolved_examples:
        lines.append("\nunresolved examples:")
        lines += [f"  {u}" for u in result.unresolved_examples]

    return "\n".join(lines)
