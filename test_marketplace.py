import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import marketplace_helper as helper
from test_weread import FIXTURE
from weread_import import CLI


class MarketplaceTests(unittest.TestCase):
    def test_preview_apply_verified_once_and_bound_to_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = 'marketplace-test'
            def cli(*args):
                return json.loads(subprocess.check_output([CLI, *args, '--root-dir', tmp, '--graph', graph, '-o', 'json'],text=True))['data']
            cli('graph', 'create')
            cli('upsert', 'page', '--page', 'Integration fixture')
            source = root/'fixture.json'
            source.write_text(json.dumps(FIXTURE))
            real_run = subprocess.run
            def isolated(args, **kwargs):
                if str(helper.BASE/'weread_import.py') in args:
                    args = [*args, '--root-dir', tmp]
                    if '--apply' not in args:
                        args += ['--input', str(source)]
                return real_run(args, **kwargs)
            try:
                with patch.object(helper, 'RUNS', root/'runs'), patch.object(helper,'graph_name',return_value=graph), patch.object(helper.subprocess,'run',side_effect=isolated):
                    previews = {}
                    preview = {'action':'preview', 'repo':graph}
                    plan = helper.run_job(preview,'first',previews)
                    self.assertEqual([plan[k] for k in ('new_books','new_highlights','new_reviews')],[1,1,2])
                    with self.assertRaises(ValueError):
                        helper.run_job({'repo':'other','action':'apply','preview':'first'},'wrong',previews)
                    helper.run_job(preview,'second',previews)
                    apply = {'repo':graph,'action':'apply','preview':'second'}
                    helper.run_job(apply,'confirmed',previews)
                    self.assertTrue((helper.RUNS/'confirmed/backup.json').is_file())
                    with self.assertRaises(ValueError):
                        helper.run_job(apply,'replay',previews)
                    plan = helper.run_job(preview,'again',previews)
                    self.assertEqual(plan['new_books']+plan['new_highlights']+plan['new_reviews'],0)
            finally:
                cli('server','stop')
