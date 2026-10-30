# -------------------------------------------------------
# Web security tests
#
# The Host pinning, cross-origin guard and security headers app.py wraps every request in, and the
# templates' tojson-rendered script values. Requests go through the conftest client, which sends
# the UI's own Host and Origin unless a test overrides them.
# -------------------------------------------------------

import logging
import re
from datetime import datetime
from html.parser import HTMLParser

import pytest
from fastapi.routing import APIRoute

from app import SecurityHeaders, app, load_allowed_hosts, templates
from conftest import APP_ORIGIN
from database import Download, Subscription

VIDEO_ID = 'dQw4w9WgXcQ'
ADD_FORM = {'url': f'https://www.youtube.com/watch?v={VIDEO_ID}', 'request_type': 'video'}
REFUSED_TEXT = 'Cross-origin request refused'
DEFAULT_HOSTS = ['youtube-manager', 'localhost', '127.0.0.1']

# The headers every response carries, pinned here so a change to the policy shows up as a diff.
EXPECTED_HEADERS = {
    'content-security-policy': (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'"
    ),
    'x-frame-options': 'DENY',
    'x-content-type-options': 'nosniff',
    'cross-origin-resource-policy': 'same-origin',
    'referrer-policy': 'same-origin',
}

# An id no row has, for posting to every per-job route without touching a real job.
MISSING_ID = 999999


# The rows in the downloads table as (url, status) pairs, for checking that a request changed none.
def download_rows(db) -> list[tuple[str, str]]:
    with db() as session:
        return [(row.url, row.status) for row in session.query(Download).all()]


# Every POST route the app serves, as (path template, path with each parameter set to `item_id`).
def post_routes(item_id: int) -> list[tuple[str, str]]:
    return [
        (route.path, re.sub(r'\{[^}]+\}', str(item_id), route.path))
        for route in app.routes
        if isinstance(route, APIRoute) and 'POST' in route.methods
    ]


# Form fields that get a POST to `template` past validation; bulk routes select job `item_id`.
def form_for(template: str, item_id: int) -> dict:
    if template == '/add':
        return ADD_FORM
    if template == '/subscriptions/add':
        return {'url': 'https://www.youtube.com/@example', 'subscription_type': 'channel'}
    if template.startswith('/bulk/'):
        return {'job_ids': [str(item_id)]}
    return {}


# A POST from another site's page is refused before the route runs, and the refusal is logged once
# with the method, path and Origin.
def test_cross_site_post_rejected(client, db, caplog):
    with caplog.at_level(logging.WARNING):
        response = client.post(
            '/add', data=ADD_FORM, headers={'Origin': 'http://evil.example'}, follow_redirects=False
        )

    assert response.status_code == 403
    assert response.text == REFUSED_TEXT
    assert download_rows(db) == []
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1
    for part in ('POST', "'/add'", "'http://evil.example'"):
        assert part in warnings[0]


# A sandboxed frame or a privacy-stripped request sends Origin: null, which names no host.
def test_null_origin_rejected(client, db):
    response = client.post(
        '/add', data=ADD_FORM, headers={'Origin': 'null'}, follow_redirects=False
    )
    assert response.status_code == 403
    assert download_rows(db) == []


# Every form here is a browser form, so a POST without Origin is refused rather than trusted.
def test_missing_origin_rejected(client, db):
    del client.headers['Origin']
    response = client.post('/add', data=ADD_FORM, follow_redirects=False)
    assert response.status_code == 403
    assert download_rows(db) == []


# The UI's own form post, with Origin naming the Host it was sent to, reaches the route.
def test_same_origin_post_allowed(client, db):
    response = client.post('/add', data=ADD_FORM, follow_redirects=False)
    assert response.status_code == 303
    assert download_rows(db) == [(ADD_FORM['url'], 'queued')]


# Origin and Host are compared port included, so a page on another port of the same host is a
# different origin.
def test_origin_netloc_with_port_matches_host_with_port(client):
    url = 'http://localhost:8085/add'
    same = client.post(
        url, data=ADD_FORM, headers={'Origin': 'http://localhost:8085'}, follow_redirects=False
    )
    assert same.status_code == 303

    for origin in ('http://localhost', 'http://localhost:8080'):
        other = client.post(url, data=ADD_FORM, headers={'Origin': origin}, follow_redirects=False)
        assert other.status_code == 403, origin


# When the browser sends Sec-Fetch-Site, it decides alone: cross-site and same-site are refused
# even with an Origin that matches Host.
@pytest.mark.parametrize('fetch_site', ['cross-site', 'same-site'])
def test_sec_fetch_site_cross_site_rejected(client, db, fetch_site):
    response = client.post(
        '/add', data=ADD_FORM, headers={'Sec-Fetch-Site': fetch_site}, follow_redirects=False
    )
    assert response.status_code == 403
    assert download_rows(db) == []


# same-origin and none (typed or bookmarked) pass on Sec-Fetch-Site alone, without an Origin.
@pytest.mark.parametrize('fetch_site', ['same-origin', 'none'])
def test_sec_fetch_site_same_origin_allowed(client, fetch_site):
    del client.headers['Origin']
    response = client.post(
        '/add', data=ADD_FORM, headers={'Sec-Fetch-Site': fetch_site}, follow_redirects=False
    )
    assert response.status_code == 303


# Reads change nothing, so another site may still load a page or poll an API.
def test_get_with_foreign_origin_allowed(client):
    headers = {'Origin': 'http://evil.example', 'Sec-Fetch-Site': 'cross-site'}
    assert client.get('/', headers=headers).status_code == 200
    assert client.get('/api/queue', headers=headers).status_code == 200


# A Host not on the list gets 400, even when Origin matches it, which is what a DNS-rebinding page
# sends: its own name as both.
def test_unknown_host_rejected(client, db):
    page = client.get('http://evil.example/')
    assert page.status_code == 400
    assert page.text == 'Invalid host header'

    rebound = client.post(
        'http://evil.example/add',
        data=ADD_FORM,
        headers={'Origin': 'http://evil.example'},
        follow_redirects=False,
    )
    assert rebound.status_code == 400
    assert download_rows(db) == []


# The nginx name (from YTM_ALLOWED_HOSTS) and the blackbox probe's container name serve the
# dashboard, and the healthcheck names serve /health.
@pytest.mark.parametrize(
    'url',
    [
        f'{APP_ORIGIN}/',
        'http://youtube-manager:8080/',
        'http://localhost/health',
        'http://127.0.0.1/health',
    ],
)
def test_probe_and_healthcheck_hosts_allowed(client, url):
    assert client.get(url).status_code == 200


# The compose healthcheck sends Host 127.0.0.1:8080; TrustedHost ignores the port.
@pytest.mark.parametrize('url', ['http://127.0.0.1:8080/health', 'http://localhost:8080/health'])
def test_healthcheck_host_with_port_allowed(client, url):
    response = client.get(url)
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


# YTM_ALLOWED_HOSTS adds trimmed, non-empty entries after the defaults; unset, only the defaults.
def test_allowed_hosts_env_extends_defaults():
    assert load_allowed_hosts({}) == DEFAULT_HOSTS
    assert load_allowed_hosts({'YTM_ALLOWED_HOSTS': ''}) == DEFAULT_HOSTS
    assert load_allowed_hosts({'YTM_ALLOWED_HOSTS': ' 192.0.2.20 , ,ui.example.test,'}) == [
        *DEFAULT_HOSTS,
        '192.0.2.20',
        'ui.example.test',
    ]


# Pages, JSON, and both refusals carry every header, the refusals because SecurityHeaders is the
# outermost middleware.
def test_security_headers_present(client):
    responses = {
        'page': client.get('/'),
        'json': client.get('/health'),
        'bad host': client.get('http://evil.example/'),
        'cross-origin': client.post('/add', data=ADD_FORM, headers={'Origin': 'http://evil.dev'}),
    }
    assert [response.status_code for response in responses.values()] == [200, 200, 400, 403]
    for label, response in responses.items():
        for name, value in EXPECTED_HEADERS.items():
            assert response.headers.get_list(name) == [value], (label, name)


# A header the response already set is kept as it is, not replaced or duplicated.
def test_security_headers_keep_existing_value():
    from fastapi.testclient import TestClient

    # A bare ASGI app answering 200 with its own X-Frame-Options.
    async def inner(scope, receive, send):
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [(b'x-frame-options', b'SAMEORIGIN')],
        })
        await send({'type': 'http.response.body', 'body': b''})

    response = TestClient(SecurityHeaders(inner)).get('/')
    assert response.headers.get_list('x-frame-options') == ['SAMEORIGIN']
    assert response.headers['x-content-type-options'] == 'nosniff'


# Collects the attributes of every <form>, as the browser would parse them.
class FormAttributes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, str | None]] = []

    # Records one <form> tag's attributes; every other tag is ignored.
    def handle_starttag(self, tag, attrs) -> None:
        if tag == 'form':
            self.forms.append(dict(attrs))


# Script values are JSON literals: timestamps as double-quoted strings, the job id as a number, and
# the history confirm() text inside a single-quoted attribute that still parses as HTML.
def test_js_values_rendered_with_tojson(client, make_job):
    # The queued job puts a baseline on the dashboard; the completed one is the history entry.
    make_job(url='https://www.youtube.com/watch?v=aaaaaaaaaaa')
    job = make_job(status='completed')

    # Each page's baseline is a JSON string holding an ISO timestamp.
    baselines = [
        ('/', 'queueLastUpdate'),
        (f'/job/{job.id}', 'jobLastUpdate'),
        (f'/job/{job.id}/log', 'jobLastUpdate'),
    ]
    for path, name in baselines:
        match = re.search(rf'const {name} = "([^"]*)";', client.get(path).text)
        assert match, path
        datetime.fromisoformat(match.group(1))

    assert f'const jobId = {job.id};' in client.get(f'/job/{job.id}').text
    assert 'const jobLastUpdate = "";' in client.get(f'/job/{MISSING_ID}/log').text

    parser = FormAttributes()
    parser.feed(client.get('/history').text)
    onsubmit = [
        form['onsubmit'] for form in parser.forms if form.get('action') == '/history/redownload-all'
    ]
    assert onsubmit == [
        'return confirm("Fetch fresh metadata for all 1 history entry across every page, '
        'including every child video?");'
    ]

    # A value that would end the string, the attribute or the script comes out escaped.
    hostile = '\'"</script><script>alert(1)</script>'
    html = templates.env.get_template('log.html').render(
        job=None, log_content='x', job_last_update=hostile
    )
    assert '<script>alert(1)' not in html
    assert (
        'const jobLastUpdate = "\\u0027\\"\\u003c/script\\u003e\\u003cscript\\u003ealert(1)'
        '\\u003c/script\\u003e";'
    ) in html


# Every POST route is refused without Origin, with nothing changed, and reaches its handler (a 303
# redirect) with the UI's own Origin.
def test_every_post_route_requires_same_origin(client, db, library, make_job):
    job = make_job()
    templates_seen = {template for template, _ in post_routes(job.id)}
    assert {'/add', '/bulk/delete', '/cancel/{job_id}', '/subscriptions/add'} <= templates_seen

    del client.headers['Origin']
    for template, path in post_routes(job.id):
        response = client.post(path, data=form_for(template, job.id), follow_redirects=False)
        assert (response.status_code, response.text) == (403, REFUSED_TEXT), path

    with db() as session:
        stored = session.get(Download, job.id)
        assert (stored.status, stored.cancel_requested) == ('queued', False)
        assert session.query(Download).count() == 1
        assert session.query(Subscription).count() == 0

    client.headers['Origin'] = APP_ORIGIN
    for template, path in post_routes(MISSING_ID):
        response = client.post(path, data=form_for(template, MISSING_ID), follow_redirects=False)
        assert response.status_code == 303, path
