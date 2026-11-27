"""Restore one data-only marker from an authorized production workflow run."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile

WORKFLOW = '.github/workflows/prod.yml'
MAX_ARCHIVE_BYTES = 64 * 1024
MAX_MARKER_BYTES = 512
MARKER_PATTERN = re.compile(r'twir/content/[^/\\\x00-\x1f\x7f]*-this-week-in-rust[^/\\\x00-\x1f\x7f]*\.md')


def warning(message):
    escaped = str(message).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print('::warning::' + escaped, file=sys.stderr)


def api(endpoint):
    result = subprocess.run(['gh', 'api', endpoint], check=True, capture_output=True, timeout=60)
    return json.loads(result.stdout)


def trusted_run(run, repository_id, default_branch, current_run_id):
    """Dispatch on a same-repository recovery branch requires existing write access."""
    return (
        isinstance(run.get('id'), int) and run['id'] > 0 and run['id'] != current_run_id
        and run.get('status') == 'completed' and run.get('conclusion') == 'success'
        and run.get('path') == WORKFLOW
        and (run.get('repository') or {}).get('id') == repository_id
        and (run.get('head_repository') or {}).get('id') == repository_id
        and isinstance(run.get('head_sha'), str)
        and re.fullmatch(r'[0-9a-f]{40}', run['head_sha']) is not None
        and isinstance(run.get('head_branch'), str) and bool(run['head_branch'])
        and (run.get('event') == 'workflow_dispatch'
             or (run.get('event') == 'schedule' and run['head_branch'] == default_branch))
    )


def artifact_names(scope):
    names = ['last-sent-' + scope] if scope else []
    names.extend('last-sent-' + part for part in scope.split('-') if part)
    return list(dict.fromkeys([*names, 'last-sent']))


def validate_artifact(artifact, run, repository_id):
    provenance = artifact.get('workflow_run') or {}
    expected = {'id': run['id'], 'repository_id': repository_id,
                'head_repository_id': repository_id, 'head_branch': run['head_branch'],
                'head_sha': run['head_sha']}
    if any(provenance.get(key) != value for key, value in expected.items()):
        raise ValueError('artifact does not belong to the trusted run and source SHA')
    if not isinstance(artifact.get('id'), int) or artifact['id'] <= 0:
        raise ValueError('invalid artifact ID')
    if artifact.get('expired') is not False:
        raise ValueError('artifact is expired or expiration is unknown')
    size = artifact.get('size_in_bytes')
    if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE_BYTES:
        raise ValueError('artifact archive exceeds the marker size budget')


def read_marker(archive, digest=None):
    """Never extract archive paths or copy archive permissions into the checkout."""
    if not 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError('downloaded archive exceeds the marker size budget')
    if digest is not None:
        expected = 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError('artifact digest mismatch')
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) != 1 or entries[0].orig_filename != 'last_sent.txt':
            raise ValueError('marker archive must contain only root-level last_sent.txt')
        entry = entries[0]
        file_type = stat.S_IFMT(entry.external_attr >> 16)
        if entry.is_dir() or entry.external_attr & 0x10 or file_type not in (0, stat.S_IFREG) or entry.flag_bits & 1:
            raise ValueError('marker must be an ordinary, unencrypted file')
        if not 0 < entry.file_size <= MAX_MARKER_BYTES:
            raise ValueError('marker is empty or too large')
        with bundle.open(entry) as source:
            raw = source.read(MAX_MARKER_BYTES + 1)
        if len(raw) > MAX_MARKER_BYTES:
            raise ValueError('marker is too large')
    marker = raw.decode('utf-8')
    if marker.endswith('\r\n'):
        marker = marker[:-2]
    elif marker.endswith('\n'):
        marker = marker[:-1]
    if MARKER_PATTERN.fullmatch(marker) is None:
        raise ValueError('marker must contain exactly one TWIR content path')
    return marker + '\n'


def download_marker(repository, artifact):
    # TemporaryDirectory defaults to the runner's temporary area, outside checkout.
    with tempfile.TemporaryDirectory(prefix='twir-marker-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
        archive = Path(temporary) / 'marker.zip'
        with archive.open('wb') as target:
            subprocess.run(['gh', 'api', f'repos/{repository}/actions/artifacts/{artifact["id"]}/zip'],
                           stdout=target, stderr=subprocess.PIPE, check=True, timeout=60)
        return read_marker(archive, artifact.get('digest'))


def restore_marker(repository, repository_id, default_branch, current_run_id, scope, destination):
    # Preserve the unfiltered REST lookup: server-side status/branch indexes have
    # returned stale runs. Keep up to ten authorized successes in API order.
    try:
        runs = api(f'repos/{repository}/actions/workflows/prod.yml/runs?per_page=100')['workflow_runs']
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        warning(f'Production run lookup unavailable: {error}')
        runs = []
    candidates = [run for run in runs if trusted_run(run, repository_id, default_branch, current_run_id)][:10]
    for run in candidates:
        try:
            artifacts = api(f'repos/{repository}/actions/runs/{run["id"]}/artifacts?per_page=100')['artifacts']
        except (subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
            warning(f'Cannot list marker artifacts for run {run["id"]}: {error}')
            continue
        for name in artifact_names(scope):
            for artifact in artifacts:
                if artifact.get('name') != name:
                    continue
                try:
                    validate_artifact(artifact, run, repository_id)
                    marker = download_marker(repository, artifact)
                except (subprocess.SubprocessError, OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
                    warning(f'Rejected marker from run {run["id"]} ({name}): {error}')
                    continue
                # Only validated text reaches the workspace; archive paths never do.
                with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=destination.parent,
                                                 prefix='.last-sent-', delete=False) as output:
                    temporary = Path(output.name)
                    try:
                        output.write(marker)
                        output.close()
                        temporary.replace(destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                print(f'Restored last_sent marker from run {run["id"]} (artifact {name}).')
                return True
    # Missing cached state keeps the existing recovery behavior. The durable
    # checkpoint and production pin guard remain responsible for duplicate safety.
    warning('No recent authorized production run yielded a marker; continuing without cached state.')
    return False


if __name__ == '__main__':
    restore_marker(os.environ['GITHUB_REPOSITORY'], int(os.environ['GITHUB_REPOSITORY_ID']),
                   os.environ['DEFAULT_BRANCH'], int(os.environ['GITHUB_RUN_ID']),
                   sys.argv[1], Path('last_sent.txt'))
