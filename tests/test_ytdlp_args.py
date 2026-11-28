# -------------------------------------------------------
# yt-dlp argument tests
#
# Every yt-dlp call ends its options with `--` before the URL, so a stored URL is never read as an
# option. yt-dlp takes its proxy from the environment, where NO_PROXY keeps internal services such
# as the PO-token provider off the proxy; the worker passes no --proxy of its own.
# -------------------------------------------------------

import contextlib
import subprocess
from types import SimpleNamespace

import pytest
import yt_dlp
from yt_dlp.utils.networking import select_proxy

from youtube_manager import worker

# A URL that yt-dlp would read as an option without `--`.
OPTION_LIKE_URL = '--exec=touch /tmp/pwned'

URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'


# Record every command run_command receives, and fail each call as yt-dlp exiting 1 would.
@pytest.fixture
def commands(monkeypatch):
    seen = []

    def fake_run_command(command, timeout=None):
        seen.append(list(command))
        raise subprocess.CalledProcessError(1, command, '', 'ERROR: stopped by the test')

    monkeypatch.setattr(worker, 'run_command', fake_run_command)
    monkeypatch.setattr(worker, 'record_subscription_failure', lambda *args, **kwargs: None)
    return seen


# Each lookup ends with `--` and the URL, an option-like URL included.
@pytest.mark.parametrize('url', [URL, OPTION_LIKE_URL])
def test_every_lookup_puts_the_url_after_a_double_dash(commands, url):
    job = SimpleNamespace(id=1, url=url, request_type='video', title=None)
    subscription = SimpleNamespace(id=2, url=url)
    calls = [
        lambda: worker.resolve_subscription_target(None, subscription),
        lambda: worker.discover_metadata(job, fail_on_error=False, store_error=False),
        lambda: worker.scan_video_ids(url),
        lambda: worker.resolve_expected_output_path(job, worker.OUTPUT_TEMPLATE_FLAT),
    ]
    for call in calls:
        with contextlib.suppress(Exception):
            call()

    assert len(commands) == len(calls)
    assert [command[-2:] for command in commands] == [['--', url]] * len(calls)


# The download ends with `--` and the URL, and passes no --proxy even when HTTP_PROXY is set.
def test_download_command_puts_the_url_last_and_passes_no_proxy(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.example.test:3128')

    command = worker.build_download_command(OPTION_LIKE_URL, worker.OUTPUT_TEMPLATE_FLAT, 1)

    assert command[-2:] == ['--', OPTION_LIKE_URL]
    assert '--proxy' not in command


# yt-dlp reads the upper-case proxy variables a host sets, sends YouTube through the proxy, and
# reaches a host that NO_PROXY names, such as the PO-token provider, directly.
def test_ytdlp_takes_its_proxy_from_the_environment_and_honours_no_proxy(monkeypatch):
    for name in ('http_proxy', 'https_proxy', 'no_proxy', 'all_proxy', 'ALL_PROXY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.example.test:3128')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example.test:3128')
    monkeypatch.setenv('NO_PROXY', 'localhost,127.0.0.1,bgutil-provider')

    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        proxies = ydl.proxies

    assert select_proxy('https://www.youtube.com/watch?v=dQw4w9WgXcQ', proxies) == (
        'http://proxy.example.test:3128'
    )
    assert select_proxy('http://bgutil-provider:4416/get_pot', proxies) is None
