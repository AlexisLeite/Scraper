"""Consistent desktop Chrome identity, independent of the destination website."""

def configure_desktop_page(context, page):
    session = context.new_cdp_session(page)
    version = session.send('Browser.getVersion')
    full = version['product'].split('/')[-1]
    major = full.split('.')[0]
    brands = [{'brand': 'Chromium', 'version': major},
              {'brand': 'Google Chrome', 'version': major},
              {'brand': 'Not_A Brand', 'version': '99'}]
    session.send('Network.setUserAgentOverride', {
        'userAgent': version['userAgent'].replace('HeadlessChrome/', 'Chrome/'),
        'acceptLanguage': 'es-UY,es,en', 'platform': 'Linux x86_64',
        'userAgentMetadata': {
            'brands': brands,
            'fullVersionList': [{'brand': b['brand'], 'version': full if b['version'] == major else '99.0.0.0'} for b in brands],
            'fullVersion': full, 'platform': 'Linux', 'platformVersion': '',
            'architecture': 'x86', 'model': '', 'mobile': False, 'bitness': '64',
        },
    })
    return session  # Keep overrides alive until the owning context closes.
