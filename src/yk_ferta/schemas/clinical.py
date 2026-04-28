"""Clinical input and intermediate domain models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PatientProfile:
    """Structured patient input for the yk-FERTA pipeline."""

    patient_id: str
    chief_complaint: str = ""
    present_illness: str = ""
    history: str = ""
    physical_exam: str = ""
    laboratory_findings: str = ""
    imaging_findings: str = ""
    treatments: str = ""
    raw_note: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def narrative(self) -> str:
        """Build a compact free-text summary for downstream retrieval and reasoning."""
        fields = [
            self.chief_complaint,
            self.present_illness,
            self.history,
            self.physical_exam,
            self.laboratory_findings,
            self.imaging_findings,
            self.treatments,
            self.raw_note,
        ]
        return "\n".join(part.strip() for part in fields if part and part.strip())


@dataclass(slots=True)
class PhenotypeItem:
    """Normalized phenotype representation."""

    label: str
    code: str | None = None
    source: str = ""
    confidence: float | None = None
    notes: str = ""


@dataclass(slots=True)
class CandidateCondition:
    """A candidate diagnosis or etiology produced by the reasoning stack."""

    name: str
    rank: int
    score: float | None = None
    rationale: str = ""
    supporting_phenotypes: list[str] = field(default_factory=list)
