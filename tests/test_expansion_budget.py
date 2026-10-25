from tests.legacy.config import ScrapperConfig as LegacyConfig
from src.common.scrapper import Scrapper, ScrapperConfig, PageCapture, WebPageEntities, WebPageEntity


class Planner:
    def __init__(self, calls, fail_child=False):
        self.calls = calls
        self.fail_child = fail_child

    def __call__(self, capture):
        if self.fail_child and 'expanded' in capture.html:
            raise RuntimeError('Clasificación fallida después del clic')
        entity = WebPageEntity('content', 'attributes', ('content',), .99, ('g1',),
            '#content', 'main', '', capture.html)
        plan = {'effect': 'planned', 'control': {'selector': '#more', 'text': 'Más',
            'predicted_role': 'load_reviews', 'confidence': .99}, 'review_selector': '#reviews',
            'primary_selector': None}
        return WebPageEntities('product', .99, 'fixture', (), (entity,), actions=(plan,))

    def verify(self, uri, control, *args, **kwargs):
        self.calls.append(('action', uri))
        snapshot = {'html': '<main>expanded</main>', 'url': uri, 'title': 'Expanded',
                    'screenshot_base64': None}
        return {'effect': 'loaded_reviews', 'control': control, 'before_reviews': 1, 'after_reviews': 2,
                'network': [{'type': 'fetch', 'status': 'finished', 'http_status': 200}],
                'captures': {'before': snapshot, 'after': snapshot}}


def make_scraper(tmp_path, max_sites, max_hops, calls, fail_child=False):
    def capture(uri):
        calls.append(('page', uri))
        return PageCapture(uri, uri, 'Original', '<main>original</main>', interactive=True)
    return Scrapper(LegacyConfig(max_threads=2, max_sites=max_sites, max_hops=max_hops,
        max_retries=3, traversal="bfs", audit_dir=tmp_path, follow_navigation=False, uri_to_html=capture,
        html_to_entities=Planner(calls, fail_child)))


def test_site_limit_is_checked_before_any_expansion_click(tmp_path):
    calls = []
    scraper = make_scraper(tmp_path, 1, 4, calls)
    result = scraper.run(['https://fixture.test/'])
    assert result.scheduled == 1 and not any(kind == 'action' for kind, uri in calls)
    assert any(e['via_entity_id'] == 'action:0' and e['disposition'] == 'max_sites'
               for e in scraper.audit.list_edges(result.run_id))
    visit = scraper.audit.list_visits(result.run_id)[0]
    assert scraper.audit.get_visit(visit['id'])['result']['actions'][0]['budget_disposition'] == 'max_sites'


def test_hop_limit_is_checked_before_any_expansion_click(tmp_path):
    calls = []
    scraper = make_scraper(tmp_path, 4, 0, calls)
    result = scraper.run(['https://fixture.test/'])
    assert result.scheduled == 1 and not any(kind == 'action' for kind, uri in calls)
    assert any(e['disposition'] == 'max_hops' for e in scraper.audit.list_edges(result.run_id))


def test_concurrent_expansions_share_the_global_budget(tmp_path):
    calls = []
    scraper = make_scraper(tmp_path, 4, 1, calls)
    result = scraper.run(['https://fixture.test/a', 'https://fixture.test/b'])
    visits = scraper.audit.list_visits(result.run_id)
    assert result.scheduled == result.succeeded == 4
    assert [v['depth'] for v in visits] == [0, 0, 1, 1]
    assert [v['kind'] for v in visits] == ['page', 'page', 'expansion', 'expansion']
    assert sum(kind == 'page' for kind, uri in calls) == 2
    assert sum(kind == 'action' for kind, uri in calls) == 2
    # Strategies expand in their active sessions; worker timing determines
    # which expansion reserves first, rather than a separate BFS action queue.
    for visit in visits[2:]:
        child = scraper.audit.get_visit(visit['id'])
        assert 'expanded' in scraper.audit.read_html(child['id'])
        parent = scraper.audit.get_visit(child['parent_visit_id'])
        assert parent['requested_uri'] == child['requested_uri']
        assert parent['result']['actions'][0]['child_visit_id'] == child['id']


def test_failed_expansion_does_not_repeat_network_click_on_retry(tmp_path):
    calls = []
    scraper = make_scraper(tmp_path, 2, 2, calls, fail_child=True)
    result = scraper.run(['https://fixture.test/'])
    assert (result.scheduled, result.succeeded, result.failed) == (2, 1, 1)
    assert sum(kind == 'action' for kind, uri in calls) == 1
    visits = scraper.audit.list_visits(result.run_id)
    assert len(scraper.audit.get_visit(visits[1]['id'])['attempts']) == 1


def test_balanced_traversal_shares_budget_between_pages_and_expansions(tmp_path):
    from dataclasses import replace
    calls = []
    planner = Planner(calls)
    def classify(capture):
        content = planner(capture)
        links = tuple({'text': str(i), 'url': f'https://fixture.test/{i}'} for i in range(20))
        navigation = WebPageEntity('nav', 'categories', ('navigation',), .99, ('g2',),
            '#nav', 'main', '', '', links)
        return replace(content, entities=(*content.entities, navigation))
    class Mixed:
        def __call__(self, capture): return classify(capture)
        def verify(self, *args, **kwargs): return planner.verify(*args, **kwargs)
    def fetch(uri):
        calls.append(('page', uri))
        return PageCapture(uri, uri, 'Original', 'original', interactive=True)
    scraper = Scrapper(LegacyConfig(max_threads=1, max_sites=5, max_hops=2,
        audit_dir=tmp_path, uri_to_html=fetch, html_to_entities=Mixed()))
    summary = scraper.run(['https://fixture.test/'])
    kinds = [kind for kind, _ in calls]
    assert summary.scheduled == 5
    assert kinds.count('page') == 3 and kinds.count('action') == 2
    assert any(v['depth'] == 2 for v in scraper.audit.list_visits(summary.run_id))
