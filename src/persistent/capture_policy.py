"""Validate a captured destination against the origins that admitted its request."""
from ..common.scrapper.orchestrator import canonical_uri
from ..common.scrapper.url_policy import URLPolicy

RULE_KEYS = ('allowed_domains', 'blocked_domains', 'allowed_url_patterns', 'blocked_url_patterns')

class OutsideScopeRedirect(RuntimeError):
    phase = 'access_blocked'
    retryable = False


def validate_destination(seeds, requested, final):
    requested, final = canonical_uri(requested), canonical_uri(final)
    if requested == final:
        return
    for seed in seeds:
        policy = URLPolicy(*(seed.get(k, []) for k in RULE_KEYS))
        def admitted(url):
            decision = policy.disposition(url)
            return not decision or (url == seed['url'] and decision == 'outside_allowlist')
        if admitted(requested) and admitted(final):
            return
    raise OutsideScopeRedirect(
        'La navegación fue redirigida fuera del alcance configurado. '
        'No se extrajo la página solicitada; revisa la captura para identificar login o verificación. '
        f'Destino: {final}')
