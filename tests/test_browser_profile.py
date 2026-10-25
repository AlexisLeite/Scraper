from src.common.browser_profile import configure_desktop_page


def test_profile_keeps_session_and_consistent_browser_version():
    calls=[]
    class Session:
        def send(self, method, params=None):
            calls.append((method, params))
            if method=='Browser.getVersion':
                return {'product':'Chrome/154.0.8037.57', 'userAgent':'Mozilla/5.0 HeadlessChrome/154.0.0.0 Safari/537.36'}
        def detach(self):
            raise AssertionError('Detaching removes the page override')
    session=Session()
    class Context:
        def new_cdp_session(self,page): return session
    assert configure_desktop_page(Context(), object()) is session
    payload=calls[-1][1]
    assert 'HeadlessChrome' not in payload['userAgent']
    assert payload['acceptLanguage']=='es-UY,es,en'
    assert payload['userAgentMetadata']['fullVersion']=='154.0.8037.57'
