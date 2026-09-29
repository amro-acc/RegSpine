"""
Applicability Agent

Evaluates regulatory obligations against a specific bank profile to determine
applicability before moving into the control mapping phase.

The bank profile configuration defines the entity's jurisdiction, licenses, 
and business lines. The agent filters the obligations based on these attributes 
and returns typed Pydantic models. 

It intentionally returns both the filtered applicable list and the complete set of 
applicability records (including negative verdicts) to ensure a complete audit trail.
The agent remains database-agnostic, leaving persistence to the calling service.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import yaml

from src.agents.ingestion_agent import parse_json_response
from src.core.schemas import ObligationApplicability, RegulatoryObligation
from src.llm.gateway import LLMGateway

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "applicability" / "v1.md"
DEFAULT_BANK_PROFILE_PATH = REPO_ROOT / "config" / "bank_profile.yaml"


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def load_bank_profile(path: Path = DEFAULT_BANK_PROFILE_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def bank_profile_from_entity_row(row: dict) -> dict:
    """
    Transforms a live `bank_entities` database row into the standardized 
    dictionary shape expected by the applicability prompt template.

    Maps database-specific column names to the expected profile attributes 
    (e.g., `name` to `entity_name`, `product_lines` to `business_lines`). 
    Missing optional fields, such as `entity_tier`, default to "unknown" 
    to provide informational context without breaking the evaluation logic.
    """
    return {
        "entity_name": row["name"],
        "jurisdiction": row["jurisdiction"],
        "entity_tier": row.get("entity_tier") or "unknown",
        "licences": row.get("licences") or [],
        "business_lines": row.get("product_lines") or [],
    }


class ApplicabilityAgent:
    """
    Handles high-volume applicability filtering using the standard extractor role.
    """

    PROMPT_VERSION = "v1"

    def __init__(self, gateway: LLMGateway | None = None, bank_profile: dict | None = None):
        self.gateway = gateway or LLMGateway()
        self._prompt_template = _load_prompt_template()
        self.bank_profile = bank_profile or load_bank_profile()

    def _model_id(self) -> str:
        return self.gateway.config["extractor"]["model"]

    def _render_prompt(self, obligation: RegulatoryObligation) -> str:
        profile = self.bank_profile
        prompt = self._prompt_template
        replacements = {
            "{{obligation_text}}": obligation.obligation_text,
            "{{obligation_type}}": obligation.obligation_type,
            "{{entity_name}}": str(profile["entity_name"]),
            "{{jurisdiction}}": str(profile["jurisdiction"]),
            "{{entity_tier}}": str(profile["entity_tier"]),
            "{{licences}}": ", ".join(profile.get("licences", [])),
            "{{business_lines}}": ", ".join(profile.get("business_lines", [])),
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    def evaluate(
        self,
        obligations: list[RegulatoryObligation],
        entity_id: uuid.UUID,
    ) -> tuple[list[RegulatoryObligation], list[ObligationApplicability]]:
        """
        Evaluates a list of obligations against the bank profile.

        Returns:
            A tuple containing:
            1. A filtered list of obligations that apply to the entity.
            2. The complete list of applicability records, including both 
               positive and negative verdicts for auditability.
        """
        applicable: list[RegulatoryObligation] = []
        records: list[ObligationApplicability] = []

        for obligation in obligations:
            prompt = self._render_prompt(obligation)
            response = self.gateway.call(
                role="extractor", 
                prompt=prompt, 
                schema_version="applicability_v1"
            )
            parsed = parse_json_response(response.get("content", ""))

            record = ObligationApplicability(
                obligation_id=obligation.id,
                entity_id=entity_id,
                applies=parsed["applies"],
                driver=parsed["driver"],
                rationale=parsed["rationale"],
                cited_profile_attribute=parsed["cited_profile_attribute"],
                confidence=parsed["confidence"],
            )
            records.append(record)
            
            if record.applies:
                applicable.append(obligation)

        return applicable, records