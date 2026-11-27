"""Offline marker trust/recovery contracts; GitHub and downloads are mocked."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import restore_marker as state

REPOSITORY_ID = 123
SHA = 'a' * 40
MARKER = 'twir/content/2026-11-18-this-week-in-rust.md\n'


def run(run_id=99, **updates):
    value = {'id': run_id, 'status': 'completed', 'conclusion': 'success',
             'event': 'schedule', 'path': '.github/workflows/prod.yml',
             'repository': {'id': REPOSITORY_ID}, 'head_repository': {'id': REPOSITORY_ID},
             'head_branch': 'main', 'head_sha': SHA}
    value.update(updates)
    return value


def artifact(producer, artifact_id=1, name='last-sent-prod'):
    return {'id': artifact_id, 'name': name, 'expired': False, 'size_in_bytes': 200,
            'workflow_run': {'id': producer['id'], 'repository_id': REPOSITORY_ID,
                             'head_repository_id': REPOSITORY_ID,
                             'head_branch': producer['head_branch'], 'head_sha': producer['head_sha']}}


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.destination = self.workspace / 'last_sent.txt'
        self.messages = io.StringIO()

    def archive(self, entries, name='marker.zip'):
        path = self.root / name
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            for entry, content in entries:
                bundle.writestr(entry, content)
        return path

    def restore(self, runs, artifacts_by_run, archives, scope='prod'):
        def api(endpoint):
            if endpoint.endswith('/actions/workflows/prod.yml/runs?per_page=100'):
                return {'workflow_runs': runs}
            producer_id = int(endpoint.split('/runs/')[1].split('/')[0])
            return {'artifacts': artifacts_by_run.get(producer_id, [])}

        def download(repository, candidate):
            self.assertEqual(repository, 'owner/repo')
            return state.read_marker(archives[candidate['id']], candidate.get('digest'))

        with patch.object(state, 'api', side_effect=api), \
                patch.object(state, 'download_marker', side_effect=download) as fetch, \
                redirect_stderr(self.messages), redirect_stdout(self.messages):
            result = state.restore_marker('owner/repo', REPOSITORY_ID, 'main', 100,
                                          scope, self.destination)
        return result, fetch

    def test_scheduled_and_manual_recovery_producers_remain_allowed(self):
        self.assertTrue(state.trusted_run(run(), REPOSITORY_ID, 'main', 100))
        recovery = run(event='workflow_dispatch', head_branch='zcode/marker-chain-unbreakable')
        self.assertTrue(state.trusted_run(recovery, REPOSITORY_ID, 'main', 100))
        self.assertTrue(state.trusted_run(run(head_branch='trunk'), REPOSITORY_ID, 'trunk', 100))

    def test_untrusted_events_forks_missing_provenance_and_current_run_are_rejected(self):
        changes = [
            {'event': event} for event in ('pull_request', 'pull_request_target', 'push', 'workflow_run', '')
        ] + [{'head_branch': 'unapproved-scheduled-branch'}, {'head_branch': ''},
             {'repository': {'id': 999}}, {'head_repository': {'id': 999}},
             {'repository': None}, {'head_repository': None}, {'id': 100},
             {'head_sha': 'short'}, {'head_sha': None}, {'status': 'in_progress'},
             {'conclusion': 'failure'}, {'path': '.github/workflows/other.yml'}]
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(state.trusted_run(run(**change), REPOSITORY_ID, 'main', 100))

    def test_scope_and_legacy_fallback_order_is_preserved(self):
        self.assertEqual(state.artifact_names('prod'), ['last-sent-prod', 'last-sent'])
        self.assertEqual(state.artifact_names('dev-prod'),
                         ['last-sent-dev-prod', 'last-sent-dev', 'last-sent-prod', 'last-sent'])
        self.assertEqual(state.artifact_names(''), ['last-sent'])

    def test_artifact_must_match_the_exact_run_branch_sha_and_repository(self):
        producer = run()
        trusted = artifact(producer)
        state.validate_artifact(trusted, producer, REPOSITORY_ID)
        for field in trusted['workflow_run']:
            altered = deepcopy(trusted)
            altered['workflow_run'][field] = 'different'
            with self.subTest(field=field), self.assertRaises(ValueError):
                state.validate_artifact(altered, producer, REPOSITORY_ID)
        for update in ({'workflow_run': None}, {'expired': True}, {'expired': None},
                       {'size_in_bytes': 0}, {'size_in_bytes': state.MAX_ARCHIVE_BYTES + 1}, {'id': -1}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                state.validate_artifact({**trusted, **update}, producer, REPOSITORY_ID)

    def test_realistic_regular_and_unspecified_zip_modes_and_newlines_are_accepted(self):
        for mode in (0, stat.S_IFREG | 0o644):
            for ending in ('', '\n', '\r\n'):
                entry = zipfile.ZipInfo('last_sent.txt')
                entry.create_system = 3
                entry.external_attr = (mode << 16) if mode else (0o600 << 16)
                archive = self.archive([(entry, MARKER.rstrip('\n') + ending)])
                digest = 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest()
                self.assertEqual(state.read_marker(archive, digest), MARKER)
        unicode_marker = 'twir/content/2026-11-18-this-week-in-rust-обзор.md\n'
        archive = self.archive([('last_sent.txt', unicode_marker)])
        self.assertEqual(state.read_marker(archive), unicode_marker)

    def test_extra_paths_duplicate_entries_and_links_are_rejected_without_extraction(self):
        for entries in ([('last_sent.txt', MARKER), ('extra.txt', 'inert data')],
                        [('nested/last_sent.txt', MARKER)], [('../last_sent.txt', MARKER)],
                        [('last_sent.txt', MARKER), ('last_sent.txt', MARKER)]):
            with self.subTest(entries=entries):
                with patch('warnings.warn'):
                    archive = self.archive(entries)
                with self.assertRaises(ValueError):
                    state.read_marker(archive)
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o755, stat.S_IFIFO | 0o600):
            entry = zipfile.ZipInfo('last_sent.txt')
            entry.create_system = 3
            entry.external_attr = mode << 16
            archive = self.archive([(entry, MARKER)])
            with self.assertRaises(ValueError):
                state.read_marker(archive)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_invalid_empty_oversized_and_multiline_markers_are_rejected(self):
        for content in ('', 'not a TWIR path\n', MARKER + MARKER, MARKER + '\n',
                        'twir/content/../2026-this-week-in-rust.md\n', b'\xff',
                        b'x' * (state.MAX_MARKER_BYTES + 1)):
            archive = self.archive([('last_sent.txt', content)])
            with self.subTest(content=content[:40]), self.assertRaises(ValueError):
                state.read_marker(archive)
        archive.write_bytes(b'x' * (state.MAX_ARCHIVE_BYTES + 1))
        with self.assertRaises(ValueError):
            state.read_marker(archive)

    def test_digest_mismatch_and_corrupt_zip_are_rejected(self):
        archive = self.archive([('last_sent.txt', MARKER)])
        with self.assertRaises(ValueError):
            state.read_marker(archive, 'sha256:' + '0' * 64)
        archive.write_bytes(b'not a zip archive')
        with self.assertRaises(zipfile.BadZipFile):
            state.read_marker(archive)

    def test_valid_marker_restores_atomically_and_preserves_noop_comparison(self):
        producer = run()
        archive = self.archive([('last_sent.txt', MARKER)])
        self.destination.write_text('old state')
        result, fetch = self.restore([producer], {99: [artifact(producer)]}, {1: archive})
        self.assertTrue(result)
        self.assertEqual(self.destination.read_text(), MARKER)
        self.assertEqual(self.destination.read_text().rstrip('\n'), MARKER.rstrip('\n'))
        self.assertEqual(list(self.workspace.iterdir()), [self.destination])
        fetch.assert_called_once()

    def test_untrusted_runs_do_not_consume_the_ten_trusted_candidate_budget(self):
        producer = run()
        untrusted = [run(i, event='pull_request') for i in range(110, 130)]
        archive = self.archive([('last_sent.txt', MARKER)])
        result, fetch = self.restore(untrusted + [producer], {99: [artifact(producer)]}, {1: archive})
        self.assertTrue(result)
        fetch.assert_called_once()

    def test_invalid_latest_marker_falls_back_to_an_older_manual_recovery_and_legacy_name(self):
        latest = run()
        recovery = run(98, event='workflow_dispatch', head_branch='zcode/marker-chain-unbreakable')
        invalid = self.archive([('last_sent.txt', MARKER), ('extra.txt', 'inert')], 'invalid.zip')
        valid = self.archive([('last_sent.txt', MARKER)], 'valid.zip')
        result, fetch = self.restore([latest, recovery],
                                     {99: [artifact(latest)], 98: [artifact(recovery, 2, 'last-sent')]},
                                     {1: invalid, 2: valid})
        self.assertTrue(result)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.destination.read_text(), MARKER)
        self.assertIn('Rejected marker', self.messages.getvalue())
        self.assertEqual(list(self.workspace.iterdir()), [self.destination])

    def test_dev_scope_prefers_its_own_marker_before_production_or_legacy(self):
        producer = run()
        candidates = [artifact(producer, i, name) for i, name in enumerate(
            ['last-sent', 'last-sent-prod', 'last-sent-dev', 'last-sent-dev-prod'], 1)]
        archive = self.archive([('last_sent.txt', MARKER)])
        result, fetch = self.restore([producer], {99: candidates}, {4: archive}, 'dev-prod')
        self.assertTrue(result)
        self.assertEqual(fetch.call_args.args[1]['id'], 4)

    def test_missing_expired_or_mismatched_artifacts_preserve_nonfatal_recovery(self):
        producer = run()
        expired = artifact(producer)
        expired['expired'] = True
        mismatched = artifact(producer, 2)
        mismatched['workflow_run']['head_sha'] = 'b' * 40
        for candidates in ([], [expired], [mismatched]):
            result, fetch = self.restore([producer], {99: candidates}, {})
            self.assertFalse(result)
            fetch.assert_not_called()
            self.assertFalse(self.destination.exists())
        self.assertIn('continuing without cached state', self.messages.getvalue())

    def test_api_failure_does_not_turn_into_a_marker_or_abort_recovery(self):
        with patch.object(state, 'api', side_effect=subprocess.CalledProcessError(1, ['gh', 'api'])), \
                redirect_stderr(self.messages):
            result = state.restore_marker('owner/repo', REPOSITORY_ID, 'main', 100, 'prod', self.destination)
        self.assertFalse(result)
        self.assertFalse(self.destination.exists())

    def test_download_isolated_archive_is_removed_after_reading(self):
        source = self.archive([('last_sent.txt', MARKER)])
        locations = []

        def download(command, stdout, **kwargs):
            self.assertEqual(command, ['gh', 'api', 'repos/owner/repo/actions/artifacts/1/zip'])
            locations.append(Path(stdout.name))
            self.assertNotEqual(Path(stdout.name).parent, self.workspace)
            stdout.write(source.read_bytes())

        with patch.object(state.subprocess, 'run', side_effect=download):
            self.assertEqual(state.download_marker('owner/repo', artifact(run())), MARKER)
        self.assertFalse(locations[0].exists())


if __name__ == '__main__':
    unittest.main()
