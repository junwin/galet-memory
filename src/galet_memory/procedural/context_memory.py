from __future__ import annotations

from .interface import (
    ProceduralMemory,
    ProceduralMemoryRequest,
    ProceduralMemoryResult,
    ProceduralSkill,
)
from ..ports import ContextRepository


class ContextProceduralMemory(ProceduralMemory):
    def __init__(self, contexts: ContextRepository) -> None:
        self.contexts = contexts

    def recall(self, request: ProceduralMemoryRequest) -> ProceduralMemoryResult:
        if not request.context_name or request.context_name == "none":
            return ProceduralMemoryResult(
                account_name=request.account_name,
                metadata={"reason": "no_context"},
            )
        context = (
            self.contexts.get_or_create(
                request.account_name, request.context_name
            )
            if request.create_if_missing
            else self.contexts.get(
                request.account_name, request.context_name
            )
        )
        if context is None:
            return ProceduralMemoryResult(
                account_name=request.account_name,
                metadata={"reason": "context_not_found"},
            )

        skills = [
            ProceduralSkill(
                name=skill.name,
                text=skill.text,
                mandatory_tools=list(skill.mandatory_tools),
                metadata=dict(skill.metadata),
            )
            for skill in context.skills
        ] if request.include_skills else []

        return ProceduralMemoryResult(
            context_id=context.id,
            account_name=context.account_name,
            tag=context.tag,
            text=context.text,
            resolved_text=(
                context.resolved_text
                if request.include_resolved_text
                else ""
            ),
            imports=list(context.imports),
            skills=skills,
            missing_imports=list(context.missing_imports),
            required_tools=(
                list(context.required_tools)
                if request.include_required_tools
                else []
            ),
            search_namespaces=list(context.search_namespaces),
            metadata={
                **dict(context.metadata),
                "updated_at": (
                    context.updated_at.isoformat()
                    if context.updated_at
                    else None
                ),
            },
        )
