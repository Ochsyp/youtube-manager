#!/usr/bin/env python3
# -------------------------------------------------------
# Fake yt-dlp
#
# Stands in for yt-dlp in worker tests, so a download runs end to end with no network and no
# video. Honours -P home:/temp:, -o and --print-to-file the way yt-dlp does, and answers
# --get-filename and --dump-single-json. YTDLP_FAKE_MODE picks the download's outcome.
# -------------------------------------------------------

import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

# 'complete' writes the media and its .info.json and reports the final path; 'fragment-then-fail'
# leaves one format stream under the temp path and exits 1, as a download cut off mid-way does.
MODES = ('complete', 'fragment-then-fail')

# Options that consume the next argument; every other dashed argument is a flag.
VALUE_OPTIONS = {
    '-P', '--paths', '-o', '--output', '--extractor-args', '--convert-thumbnails', '--proxy',
}

# An output-template field, "%(title)s".
TEMPLATE_FIELD = re.compile(r'%\((\w+)\)s')


# The video's info dictionary: the id comes from the URL, the title from YTDLP_FAKE_TITLE.
def video_info(url: str) -> dict:
    parsed = urlparse(url)
    video_id = parse_qs(parsed.query).get('v', [parsed.path.rstrip('/').split('/')[-1]])[0]
    return {
        'id': video_id,
        'title': os.environ.get('YTDLP_FAKE_TITLE', 'Fake Video'),
        'uploader': 'Fake Uploader',
        'ext': 'mp4',
    }


# Fill an output template from `info`, writing NA for a missing field as yt-dlp does.
def render(template: str, info: dict) -> str:
    return TEMPLATE_FIELD.sub(lambda match: str(info.get(match.group(1), 'NA')), template)


# Split argv into options and the URL. -P values accumulate by type ('home' unless prefixed), and
# --print-to-file entries are (when, template, file) triples, WHEN defaulting to 'video'.
def parse_args(argv: list[str]) -> tuple[dict, dict, list, set, str]:
    paths, options, prints, flags, url = {}, {}, [], set(), ''
    args = iter(argv)
    for arg in args:
        if arg == '--print-to-file':
            spec, file_template = next(args), next(args)
            when, _, field = spec.rpartition(':')
            prints.append((when or 'video', field, file_template))
        elif arg in ('-P', '--paths'):
            value = next(args)
            key, sep, path = value.partition(':')
            if sep and key in ('home', 'temp'):
                paths[key] = path
            else:
                paths['home'] = value
        elif arg in VALUE_OPTIONS:
            options[arg] = next(args)
        elif arg.startswith('-'):
            flags.add(arg)
        else:
            url = arg
    return paths, options, prints, flags, url


# Append each after_move field to its file, as yt-dlp does once the media sits at `final_path`.
def print_after_move(prints: list, info: dict, final_path: str) -> None:
    fields = {**info, 'filepath': final_path}
    for when, field, file_template in prints:
        if when != 'after_move':
            continue
        with open(render(file_template, fields), 'a', encoding='utf-8', newline='') as fh:
            fh.write(str(fields.get(field, 'NA')) + os.linesep)


# Write `content` to `path`, creating its folder first.
def write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(content)


# Run one fake yt-dlp invocation and return its exit code.
def main(argv: list[str]) -> int:
    paths, options, prints, flags, url = parse_args(argv)
    mode = os.environ.get('YTDLP_FAKE_MODE', 'complete')
    if mode not in MODES:
        print(f'ERROR: unknown YTDLP_FAKE_MODE {mode!r}', file=sys.stderr)
        return 2

    info = video_info(url)
    if '--dump-single-json' in flags:
        print(json.dumps(info))
        return 0

    relative = render(options.get('-o') or options.get('--output') or '%(title)s [%(id)s].%(ext)s', info)
    home = paths.get('home', '')
    # os.path.join drops everything before an absolute part, which is how yt-dlp ignores -P
    # when -o is absolute, and how an absolute temp path escapes the home path.
    final_path = os.path.join(home, relative)
    temp_path = os.path.join(home, paths.get('temp', ''), relative)
    stem = os.path.splitext(final_path)[0]
    temp_stem = os.path.splitext(temp_path)[0]

    if '--get-filename' in flags:
        print(final_path)
        return 0

    # yt-dlp writes the .info.json into the home path before it fetches a single byte.
    write_file(stem + '.info.json', json.dumps(info))
    print(f'[info] Writing video metadata as JSON to: {stem}.info.json')

    if mode == 'fragment-then-fail':
        fragment = f'{temp_stem}.f137.{info["ext"]}'
        write_file(fragment, 'video stream')
        print(f'[download] Destination: {fragment}')
        print('[download]  40.0% of    1.00KiB at    1.00KiB/s ETA 00:01')
        print('ERROR: [download] fake failure after one format stream', file=sys.stderr)
        return 1

    write_file(temp_path, 'merged video')
    print(f'[download] Destination: {temp_path}')
    print('[download] 100% of    1.00KiB in 00:00:01 at    1.00KiB/s')
    os.makedirs(os.path.dirname(final_path) or '.', exist_ok=True)
    os.replace(temp_path, final_path)
    print(f'Moving file "{temp_path}" to "{final_path}"')
    print_after_move(prints, info, final_path)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
