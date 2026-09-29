"""ObligationRelationAgent: pairwise comparison of two RegulatoryObligation
records — possibly from different regulators — to detect a relationship
worth tracking (cross-regulation overlap and contradiction detection).

Overlap and conflict detection are one comparison call apart: "do these
obligations overlap" and "do these obligations conflict" are two possible
verdicts of the exact same question, so one agent/prompt covers both rather
than building two near-duplicate ones. Conflict detection needs enough
corpus breadth to be meaningful, which the golden set built for the eval
suite (evals/golden/golden_obligations.json: DORA, NIS2, Basel III) already
supplies.

Most obligation pairs are unrelated — that's the prompt's own stated
default, not a fallback this agent adds defensively. A "no relation" verdict
returns None (never fabricates an ObligationRelation row), matching
MappingAgent.map()'s "zero candidates -> None, don't force a mapping"
precedent.

DB-agnostic like the other agents in this package: returns a typed
ObligationRelation or None, does not call src/database/supabase_client.py
itself.
"""

from __future__ import annotations

import itertools
import uuid
from pathlib import Path

from src.agents.ingestion_agent import parse_json_response
from src.core.schemas import ObligationRelation, RegulatoryObligation, RelationType
from src.llm.gateway import LLMGateway

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "obligation_relation" / "v1.md"

_VALID_RELATION_TYPES = {rt.value for rt in RelationType}


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


class ObligationRelationAgent:
    """Role: EXTRACTOR — obligation-pair-count volume, same cost profile as
    mapping_agent.py, not REASONER-tier low-volume judgement."""

    PROMPT_VERSION = "v1"

    def __init__(self, gateway: LLMGateway | None = None):
        self.gateway = gateway or LLMGateway()
        self._prompt_template = _load_prompt_template()

    def _model_id(self) -> str:
        return self.gateway.config["extractor"]["model"]

    def _render_prompt(
        self,
        obligation_a: RegulatoryObligation,
        obligation_b: RegulatoryObligation,
        regulator_a: str,
        regulator_b: str,
    ) -> str:
        prompt = self._prompt_template
        replacements = {
            "{{obligation_a_regulator}}": regulator_a,
            "{{obligation_a_text}}": obligation_a.obligation_text,
            "{{obligation_b_regulator}}": regulator_b,
            "{{obligation_b_text}}": obligation_b.obligation_text,
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    def compare(
        self,
        obligation_a: RegulatoryObligation,
        obligation_b: RegulatoryObligation,
        regulator_a: str = "unknown",
        regulator_b: str = "unknown",
    ) -> ObligationRelation | None:
        """Returns None for relation_type="unrelated" (the common case) or
        an unparseable/invalid verdict — never fabricates a relation the
        model didn't actually assert. A model-invented relation_type string
        is treated the same as "unrelated" (fall back, don't crash or
        persist unknown data — same pattern as audit_agent.py's gap_class
        validation)."""
        prompt = self._render_prompt(obligation_a, obligation_b, regulator_a, regulator_b)
        response = self.gateway.call(role="EXTRACTOR", prompt=prompt, schema_version="obligation_relation_v1")
        parsed = parse_json_response(response.get("content", ""))

        relation_type = parsed.get("relation_type")
        if relation_type not in _VALID_RELATION_TYPES or relation_type == "unrelated":
            return None

        return ObligationRelation(
            obligation_a=obligation_a.id,
            obligation_b=obligation_b.id,
            relation_type=RelationType(relation_type),
            dimension=parsed.get("dimension"),
            rationale=parsed.get("rationale", ""),
            severity=parsed.get("severity"),
            resolution_hint=parsed.get("resolution_hint"),
            confidence=parsed.get("confidence", 0.5),
        )

    def compare_across_groups(
        self,
        groups: list[list[RegulatoryObligation]],
        regulators: list[str],
    ) -> list[ObligationRelation]:
        """Compares every obligation pair drawn from *different* groups
        (e.g. different runs/regulations) — never within the same group,
        since same-source obligations are already covered by the main
        pipeline (mapping/audit), not this cross-source feature. `groups`
        and `regulators` are parallel lists (groups[i] came from
        regulators[i])."""
        relations: list[ObligationRelation] = []
        for (i, group_a), (j, group_b) in itertools.combinations(enumerate(groups), 2):
            for obligation_a, obligation_b in itertools.product(group_a, group_b):
                relation = self.compare(obligation_a, obligation_b, regulators[i], regulators[j])
                if relation is not None:
                    relations.append(relation)
        return relations
