"""Model-facing contracts. Schema validation never substitutes for provenance checks."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Citation(Contract):
    source_type: Literal["turn", "document", "file", "feedback"]
    source_id: str = Field(min_length=1, max_length=128)
    quote: str = Field(default="", max_length=10000)
    start: int | None = Field(default=None, ge=0)
    locator: dict | None = None

    @model_validator(mode="after")
    def valid_citation(self):
        if self.source_type != "file" and not self.quote:
            raise ValueError("text sources require a verbatim quote")
        if self.source_type == "file" and not self.locator:
            raise ValueError("file sources require a fixed locator")
        return self


class FieldEvidence(Citation):
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class GapUpdate(Contract):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=300)
    required: bool = True
    status: Literal["open", "covered", "deferred", "conflict", "not_applicable"] = "open"
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[Citation] = Field(default_factory=list, max_length=20)
    case_id: str | None = None
    decision_id: str | None = None


class ExperienceContext(Contract):
    case_id: str = Field(min_length=1, max_length=128)
    decision_id: str | None = None


class ContentField(Contract):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    text: str = Field(min_length=1, max_length=10000)
    status: Literal["supported", "unknown", "hypothesis"]


class InterviewAction(Contract):
    action: Literal["ask_expert", "search_sources", "read_source", "draft_experience", "suggest_finish", "pause",
                    "create_case", "link_case", "create_decision", "link_turn", "suggest_focus", "propose_relation", "defer_topic"]
    reason: str = Field(min_length=1, max_length=3000)
    gaps: list[GapUpdate] = Field(default_factory=list, max_length=20)
    gap_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    question: str | None = Field(default=None, min_length=1, max_length=2000)
    expected_information: str | None = Field(default=None, min_length=1, max_length=2000)
    hypothesis: str = Field(default="", max_length=2000)
    query: str | None = Field(default=None, min_length=1, max_length=500)
    source_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_type: Literal["turn", "document", "file", "feedback"] = "document"
    offset: int = Field(default=0, ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    kind: Literal["conditional", "perceptual", "case_note"] = "conditional"
    content: list[ContentField] = Field(default_factory=list, max_length=20)
    evidence: list[FieldEvidence] = Field(default_factory=list, max_length=100)
    case_id: str | None = None
    decision_id: str | None = None
    turn_id: str | None = None
    gap_id: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=1)
    description: str = Field(default="", max_length=10000)
    summary: str = Field(default="", max_length=6000)
    remaining: list[str] = Field(default_factory=list, max_length=40)
    contexts: list[ExperienceContext] = Field(default_factory=list, max_length=30)
    native: bool = False
    locator: dict | None = None
    relation_kind: Literal["duplicate", "supplement", "conflict"] | None = None
    left_id: str | None = None
    left_revision: int | None = Field(default=None, ge=1)
    right_id: str | None = None
    right_revision: int | None = Field(default=None, ge=1)
    conditions: str = Field(default="", max_length=10000)
    relation_evidence: list[dict] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def valid_action(self):
        if not self.reason.strip():
            raise ValueError("reason cannot be blank")
        needs = {"ask_expert": ("question", "expected_information"),
                 "search_sources": ("query",), "read_source": ("source_id",),
                 "draft_experience": ("title", "content"), "create_case": ("title", "description"),
                 "link_case": ("case_id",), "create_decision": ("case_id", "title", "description"),
                 "link_turn": ("case_id", "turn_id", "end"), "suggest_focus": ("case_id", "summary"),
                 "defer_topic": ("title",),
                 "propose_relation": ("relation_kind", "left_id", "left_revision", "right_id", "right_revision", "relation_evidence")}
        for field in needs.get(self.action, ()):
            value = getattr(self, field)
            if not value or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"{self.action} requires {field}")
        if len({(g.decision_id, g.key) for g in self.gaps}) != len(self.gaps):
            raise ValueError("duplicate gap keys")
        if self.action != "draft_experience" and (self.content or self.evidence):
            raise ValueError("content/evidence only allowed for draft_experience")
        return self


class Limits(Contract):
    max_calls: int = Field(default=40, ge=1, le=200)
    max_turns: int = Field(default=30, ge=1, le=100)
    max_tokens: int = Field(default=60000, ge=1000, le=1000000)
    steps_per_run: int = Field(default=5, ge=1, le=10)


class StartInterview(Contract):
    goal: str = Field(min_length=1, max_length=4000)
    domain_id: str = Field(default="general", max_length=64)
    request_id: str = Field(min_length=1, max_length=128)
    limits: Limits = Field(default_factory=Limits)
    start_only: bool = False
    case_ids: list[str] = Field(default_factory=list, max_length=30)
    case_id: str | None = None
    decision_id: str | None = None


class AnswerInterview(Contract):
    question_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(min_length=1, max_length=20000)
    speaker: str = Field(min_length=1, max_length=200)


class ControlInterview(Contract):
    command: Literal["continue", "pause", "close", "skip", "draft"]
    expected_revision: int = Field(ge=1)
    note: str = Field(default="", max_length=2000)


class SupplementInterview(Contract):
    request_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(min_length=1, max_length=20000)
    speaker: str = Field(min_length=1, max_length=200)
    correction_of: str | None = None
    continue_interview: bool = False


class FocusInterview(Contract):
    expected_revision: int = Field(ge=1)
    case_id: str | None = None
    decision_id: str | None = None
    proposal_id: str | None = None
    decision: Literal["accept", "reject"] = "accept"
    note: str = Field(default="", max_length=2000)


class LinkCase(Contract):
    expected_revision: int = Field(ge=1)
    case_id: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=2000)


class TurnContext(Contract):
    expected_revision: int = Field(ge=1)
    turn_id: str = Field(min_length=1, max_length=128)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    case_id: str = Field(min_length=1, max_length=128)
    decision_id: str | None = None
    gap_id: str | None = None
    actor: str = Field(min_length=1, max_length=200)


class QualityFeedback(Contract):
    expected_revision: int = Field(ge=1)
    question_id: str = Field(min_length=1, max_length=128)
    kind: Literal["leading", "repeated", "irrelevant", "wrong_premise", "other"]
    note: str = Field(min_length=1, max_length=2000)
    actor: str = Field(min_length=1, max_length=200)
