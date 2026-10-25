"""Validation shared by the rule editor and persisted seed configuration."""
from typing import Annotated
from pydantic import AfterValidator
from ..common.scrapper.url_policy import URLPolicy, domain


def validate_rule(kind: str, value: str) -> str:
    # Regex is a raw pattern, never /pattern/flags. JSON handles transport
    # escaping; re.compile needs no slash delimiter or additional escaping.
    if not value.strip():
        raise ValueError('La regla no puede estar vacía.')
    if len(value) > 2048:
        raise ValueError('Cada regla admite hasta 2048 caracteres.')
    if kind == 'domain':
        return domain(value)
    if kind != 'regex':
        raise ValueError('Tipo de regla inválido.')
    URLPolicy._compile([value])
    return value


DomainRule = Annotated[str, AfterValidator(lambda value: validate_rule('domain', value))]
RegexRule = Annotated[str, AfterValidator(lambda value: validate_rule('regex', value))]
