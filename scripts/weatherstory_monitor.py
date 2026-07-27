#!/usr/bin/env python3
"""Monitor Weather.gov WeatherStory images and notify when they change."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import smtplib
import subprocess
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen

WEATHER_PAGE = 'https://www.weather.gov/ctp/weatherstory'
STATE_FILE_DEFAULT = 'weatherstory_monitor_state.json'
IMAGE_PATTERN = re.compile(
    r'https?://www\.weather\.gov/images/ctp/WxStory/WeatherStory(?:[0-9])?\.png$',
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Monitor Weather.gov WeatherStory images and send notifications on changes.'
    )
    parser.add_argument('--state-file', default=STATE_FILE_DEFAULT, help='State file path to persist last known image metadata.')
    parser.add_argument('--email-to', help='Email address to notify when an update is detected.')
    parser.add_argument('--smtp-host', default='localhost', help='SMTP host for email notifications.')
    parser.add_argument('--smtp-port', type=int, default=587, help='SMTP port for email notifications.')
    parser.add_argument('--smtp-user', help='SMTP username for email notifications.')
    parser.add_argument('--smtp-password', help='SMTP password for email notifications.')
    parser.add_argument('--webhook-url', help='Webhook URL to POST a JSON notification to when an update is detected.')
    parser.add_argument('--notify-cmd', help='Shell command to run when an update is detected. Use {message} placeholder for the notification text.')
    parser.add_argument('--dry-run', action='store_true', help='Parse and compare image state without sending notifications.')
    parser.add_argument('--verbose', action='store_true', help='Print extra debug information.')
    parser.add_argument('--manifest-out', default='public/weatherstory-manifest.json', help='Path to write a JSON manifest of the current WeatherStory image URLs.')
    return parser.parse_args()


class ImgParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != 'img':
            return
        attr_dict = {name.lower(): value for name, value in attrs if value is not None}
        src = attr_dict.get('src')
        if src:
            self.urls.append(src)


def fetch_html(url: str, user_agent: str = 'Mozilla/5.0') -> str:
    request = Request(url, headers={'User-Agent': user_agent})
    with urlopen(request, timeout=30) as response:
        return response.read().decode('utf-8', errors='ignore')


def parse_image_urls(html: str, base_url: str) -> List[str]:
    parser = ImgParser()
    parser.feed(html)
    result: List[str] = []
    for src in parser.urls:
        full = urljoin(base_url, src)
        if IMAGE_PATTERN.match(full):
            result.append(full)
    return sorted(set(result))


def get_head_metadata(url: str) -> Dict[str, Optional[str]]:
    request = Request(url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urlopen(request, timeout=30) as response:
            return {
                'etag': response.headers.get('ETag'),
                'last_modified': response.headers.get('Last-Modified'),
                'content_length': response.headers.get('Content-Length'),
            }
    except HTTPError as exc:
        if exc.code in {405, 501}:
            return get_content_hash(url)
        raise


def get_content_hash(url: str) -> Dict[str, Optional[str]]:
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urlopen(request, timeout=30) as response:
        data = response.read()
    return {
        'etag': None,
        'last_modified': response.headers.get('Last-Modified'),
        'content_length': str(len(data)),
        'content_hash': hashlib.sha256(data).hexdigest(),
    }


def build_image_state(urls: List[str], verbose: bool = False) -> List[Dict[str, Any]]:
    state: List[Dict[str, Any]] = []
    for url in urls:
        metadata = get_head_metadata(url)
        if 'content_hash' not in metadata:
            metadata['content_hash'] = None
        image_state = {'url': url, **metadata}
        state.append(image_state)
        if verbose:
            print('Fetched metadata for', url)
            for key, value in metadata.items():
                print('  ', key, ':', value)
    return state


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open('r', encoding='utf-8') as fh:
        return json.load(fh)


def save_state(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2)


def compare_state(old_state: List[Dict[str, Any]], new_state: List[Dict[str, Any]]) -> List[str]:
    old_by_url = {item['url']: item for item in old_state}
    changes: List[str] = []

    if old_state and len(old_state) != len(new_state):
        changes.append('Number of WeatherStory images changed.')

    for item in new_state:
        url = item['url']
        old = old_by_url.get(url)
        if not old:
            changes.append(f'New image URL detected: {url}')
            continue
        if item.get('etag') and old.get('etag') and item['etag'] != old['etag']:
            changes.append(f'ETag changed for {url}')
        elif item.get('last_modified') and old.get('last_modified') and item['last_modified'] != old['last_modified']:
            changes.append(f'Last-Modified changed for {url}')
        elif item.get('content_hash') and old.get('content_hash') and item['content_hash'] != old['content_hash']:
            changes.append(f'Content hash changed for {url}')
        elif item.get('content_length') and old.get('content_length') and item['content_length'] != old.get('content_length'):
            changes.append(f'Content length changed for {url}')

    for item in old_state:
        if item['url'] not in {new['url'] for new in new_state}:
            changes.append(f'Image URL removed: {item["url"]}')

    return sorted(set(changes))


def build_notification_message(changes: List[str], new_state: List[Dict[str, Any]]) -> str:
    lines = [f'WeatherStory change detected at {datetime.now(timezone.utc).isoformat()} UTC', '']
    lines += ['Changes:']
    lines += [f'- {change}' for change in changes]
    lines += ['', 'Current WeatherStory image URLs:']
    lines += [f'- {item["url"]}' for item in new_state]
    return '\n'.join(lines)


def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: Optional[str],
    smtp_password: Optional[str],
    email_to: str,
    subject: str,
    body: str,
    verbose: bool = False,
) -> None:
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = smtp_user or f'weatherstory-monitor@{smtp_host}'
    message['To'] = email_to
    message.set_content(body)

    if verbose:
        print('Sending email notification to', email_to)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if smtp.has_extn('STARTTLS'):
            smtp.starttls()
            smtp.ehlo()
        if smtp_user and smtp_password:
            smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)


def send_webhook(url: str, payload: Dict[str, Any], verbose: bool = False) -> None:
    data = json.dumps(payload).encode('utf-8')
    request = Request(url, data=data, headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})
    if verbose:
        print('Sending webhook notification to', url)
    with urlopen(request, timeout=30) as response:
        response.read()


def run_notify_cmd(command_template: str, message: str, verbose: bool = False) -> None:
    command = command_template.replace('{message}', shlex.quote(message))
    if verbose:
        print('Running notification command:', command)
    subprocess.run(command, shell=True, check=False)


def write_manifest(path: Path, urls: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        'updatedAt': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'urls': urls,
    }
    with path.open('w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2)


def main() -> int:
    args = parse_args()
    state_path = Path(args.state_file)

    try:
        html = fetch_html(WEATHER_PAGE)
    except (HTTPError, URLError) as exc:
        print(f'Error fetching weather page: {exc}', file=sys.stderr)
        return 1

    urls = parse_image_urls(html, WEATHER_PAGE)
    if args.verbose:
        print('Found URLs:', urls)

    if args.manifest_out:
        write_manifest(Path(args.manifest_out), urls)

    if not urls:
        print('No WeatherStory image URLs found.', file=sys.stderr)
        return 1

    try:
        new_state = build_image_state(urls, verbose=args.verbose)
    except (HTTPError, URLError) as exc:
        print(f'Error fetching image metadata: {exc}', file=sys.stderr)
        return 1

    previous_state = load_state(state_path).get('images', [])
    changes = compare_state(previous_state, new_state)

    if not changes:
        if args.verbose:
            print('No changes detected.')
        return 0

    message = build_notification_message(changes, new_state)
    print(message)

    if args.dry_run:
        return 0

    if args.email_to:
        send_email(
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            smtp_user=args.smtp_user,
            smtp_password=args.smtp_password,
            email_to=args.email_to,
            subject='WeatherStory image update detected',
            body=message,
            verbose=args.verbose,
        )

    if args.webhook_url:
        send_webhook(
            args.webhook_url,
            {'text': message, 'changed_urls': [item['url'] for item in new_state], 'changes': changes},
            verbose=args.verbose,
        )

    if args.notify_cmd:
        run_notify_cmd(args.notify_cmd, message, verbose=args.verbose)

    save_state(state_path, {'images': new_state, 'last_checked': datetime.now(timezone.utc).isoformat()})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
