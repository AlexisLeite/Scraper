"""Generic URL allow/deny rules, compiled once and shared read-only by workers."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def domain(value: str) -> str:
    value = value.strip().lower().rstrip('.')
    if value.startswith('*.'):
        value = value[2:]
    try:
        value = value.encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise ValueError(f'Dominio inválido: {value}') from exc
    if len(value) > 253 or not value or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
        for label in value.split('.')
    ):
        raise ValueError(f'Dominio inválido: {value}. Usa un dominio sin protocolo, puerto ni ruta.')
    return value


class URLPolicy:
    def __init__(self, allowed_domains=(), blocked_domains=(),
                 allowed_url_patterns=(), blocked_url_patterns=()):
        self.allowed_domains = tuple(domain(value) for value in allowed_domains)
        self.blocked_domains = tuple(domain(value) for value in blocked_domains)
        self.allowed_patterns = self._compile(allowed_url_patterns)
        self.blocked_patterns = self._compile(blocked_url_patterns)

    @staticmethod
    def _compile(values):
        compiled = []
        for value in values:
            if not value.strip():
                raise ValueError('Las expresiones regulares no pueden estar vacías.')
            try:
                compiled.append(re.compile(value))
            except re.error as exc:
                raise ValueError(f'Expresión regular inválida: {value}: {exc}') from exc
        return tuple(compiled)

    @staticmethod
    def _matches(host, domains, uri, patterns):
        return (any(host == item or host.endswith('.' + item) for item in domains)
                or any(pattern.search(uri) for pattern in patterns))

    def disposition(self, uri):
        host = (urlsplit(uri).hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        if self._matches(host, self.blocked_domains, uri, self.blocked_patterns):
            return 'blocked_url'
        if ((self.allowed_domains and not self._matches(host, self.allowed_domains, uri, ()))
                or (self.allowed_patterns and not self._matches(host, (), uri, self.allowed_patterns))):
            return 'outside_allowlist'
        return None
