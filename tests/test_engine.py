"""Test download completion; browser checks require Playwright and Chromium."""
import ast
import importlib.util
import os
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'engine' / 'copilot_repo_roundtrip.py'
spec = importlib.util.spec_from_file_location('roundtrip', SCRIPT)
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)


class CompletionTests(unittest.TestCase):
    def test_prompts_include_repository_maintenance_instructions(self):
        for template in (rt.BASE_PROMPT, rt.FOLLOWUP_PROMPT):
            with self.subTest(template=template):
                prompt = template.format(request='Update the tests')
                self.assertIn('AGENTS.md', prompt)
                self.assertIn('.github/copilot-instructions.md', prompt)
                self.assertIn('README', prompt)
                self.assertIn('comments in all repository files', prompt)
                self.assertIn('no historical references', prompt)
                self.assertIn('git log', prompt)
                self.assertIn('Do not include a .git directory in the returned ZIP', prompt)
                self.assertTrue(prompt.endswith('To fix: Update the tests'))

    def test_fresh_prompt_omits_git_history_instructions(self):
        prompt = rt.FRESH_PROMPT.format(request='Build it')
        self.assertNotIn('git log', prompt)
        self.assertIn('Do not include a .git directory in the returned ZIP', prompt)
        self.assertIn('AGENTS.md', prompt)
        self.assertTrue(prompt.endswith('To fix: Build it'))

    def test_final_answer_is_the_last_segment_only(self):
        answers = [{'after': 0, 'text': 'Starting.'}, {'after': 2, 'text': 'Copilot said: Done.'},
                   {'after': 1, 'text': 'Interim.'}]
        self.assertEqual(rt.final_answer(answers, 'code()\nDone.'), 'Done.')
        self.assertEqual(rt.final_answer([], 'Copilot said: Fallback.'), 'Fallback.')

    def test_final_merge_clears_active_flags_and_preserves_steps(self):
        block = {'title': 'Coding and executing', 'active': True,
                 'steps': [{'title': 'Coding and executing', 'kind': 'coding'}]}
        blocks, answers = rt.merge_live([block], [], [block],
                                       [{'after': 1, 'text': 'Done.'}], final=True)
        self.assertFalse(blocks[0]['active'])
        self.assertTrue(block['active'])
        self.assertEqual(blocks[0]['steps'], block['steps'])
        self.assertEqual(answers, [{'after': 1, 'text': 'Done.'}])

    def test_current_layout_replaces_the_previous_activity_layout(self):
        previous = [
            {'title': 'Reasoning', 'active': False, 'layout': 'working', 'steps': []},
            {'title': 'Creating file', 'active': True, 'layout': 'working', 'steps': []},
        ]
        current = [{'title': 'Reasoned for 8s', 'active': False, 'layout': 'section', 'steps': []}]
        blocks, _ = rt.merge_live(previous, [], current, [])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['layout'], 'section')
        self.assertEqual(blocks[0]['title'], 'Reasoned for 8s')

    def test_zip_labels_without_filename(self):
        for label in ('Download the complete fixed repository ZIP', 'repo-fixed.zip'):
            self.assertRegex(label, rt.ZIP_NAME)
        for label in ('repo.zip.txt', 'zipper', 'gzip'):
            self.assertNotRegex(label, rt.ZIP_NAME)

    def test_download_retries_remain_available_after_six_attempts(self):
        self.assertTrue(rt.download_retry_due(0, 0, 0))
        self.assertFalse(rt.download_retry_due(1, 100, 109))
        self.assertTrue(rt.download_retry_due(1, 100, 110))
        for count in (6, 7, 100):
            self.assertFalse(rt.download_retry_due(count, 100, 159))
            self.assertTrue(rt.download_retry_due(count, 100, 160))

    def test_temporary_chat_is_switched_to_the_requested_state(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                page, button = Mock(), Mock()
                with patch.object(rt, 'temporary_chat_active', side_effect=[not enabled, enabled]), \
                        patch.object(rt, 'visible_temporary_toggle', return_value=button):
                    rt.set_temporary_chat(page, rt.selectors_from_env(), enabled)
                button.click.assert_called_once_with()

    def test_missing_temporary_chat_toggle_is_reported_after_the_timeout(self):
        page = Mock()
        with patch.object(rt, 'temporary_chat_active', return_value=False), \
                patch.object(rt, 'visible_temporary_toggle', return_value=None), \
                patch.object(rt, 'open_temporary_chat_menu', return_value=None) as menus, \
                self.assertRaisesRegex(SystemExit, 'Temporary chat button was not found'):
            rt.set_temporary_chat(page, rt.selectors_from_env(), True, timeout_seconds=0)
        menus.assert_not_called()

    def test_temporary_chat_is_not_clicked_when_state_already_matches(self):
        page = Mock()
        with patch.object(rt, 'temporary_chat_active', return_value=False):
            rt.set_temporary_chat(page, rt.selectors_from_env(), False)
        page.locator.assert_not_called()

    def test_every_new_run_opens_a_new_chat_before_setting_the_chat_type(self):
        source = SCRIPT.read_text()
        start = source.index('page.goto(args.url, wait_until="domcontentloaded", timeout=120000)',
                             source.index('def run_roundtrip'))
        opened = source.index('open_new_chat(page, sel, args.url)', start)
        self.assertLess(opened, source.index('set_temporary_chat(page, sel, args.temporary_chat)', start))
        self.assertLess(opened, source.index('attach_file(page, sel, source_txt)', start))

    def test_new_chat_failure_reloads_once_and_is_reported(self):
        page = Mock()
        with patch.object(rt, 'chat_has_history', return_value=True), \
                patch.object(rt, 'click_new_chat', return_value=False), \
                patch.object(rt, 'wait_for_ui'), \
                self.assertRaisesRegex(SystemExit, 'A new Copilot chat could not be opened'):
            rt.open_new_chat(page, rt.selectors_from_env(), 'https://chat.test/chat/', timeout_seconds=0)
        page.goto.assert_called_once()

    def test_conversation_url_counts_as_history(self):
        page = Mock(url='https://chat.test/chat/conversation/123')
        self.assertTrue(rt.chat_has_history(page, 'https://chat.test/chat/'))

    def test_visible_browser_falls_back_to_hidden_without_a_display(self):
        self.assertTrue(rt.browser_is_headless(True, {}))
        self.assertFalse(rt.browser_is_headless(True, {'DISPLAY': ':0'}))
        self.assertFalse(rt.browser_is_headless(True, {'WAYLAND_DISPLAY': 'wayland-0'}))
        self.assertTrue(rt.browser_is_headless(False, {'DISPLAY': ':0'}))

    @staticmethod
    def stop_page(composer=(), task=None):
        """A page mock whose composer and optional current task card expose the given Stop buttons."""
        def locator(buttons):
            loc = Mock()
            loc.count.return_value = len(buttons)
            loc.nth.side_effect = lambda index: buttons[index]
            return loc
        page = Mock()
        cards = locator([task] if task is not None else [])
        if task is not None:
            task.is_visible.return_value = True
        page.locator.side_effect = lambda selector: cards if selector == '[data-roundtrip-task-card]' else locator(composer)
        return page

    @staticmethod
    def stop_button(visible=True):
        button = Mock()
        button.is_visible.return_value = visible
        button.evaluate.return_value = True
        return button

    def test_visible_stop_controls_define_running_state(self):
        hidden, visible = self.stop_button(False), self.stop_button()
        page = self.stop_page([hidden, visible])
        with patch.object(rt, 'read_task_cards'):
            self.assertTrue(rt.response_is_running(page, rt.selectors_from_env()))
            visible.is_visible.return_value = False
            self.assertFalse(rt.response_is_running(page, rt.selectors_from_env()))
        self.assertIn('data-icon-name', rt.selectors_from_env()['stop'])

    def test_task_card_is_hovered_and_its_stop_is_listed_last(self):
        card, task_stop, composer_stop = Mock(), self.stop_button(), self.stop_button()
        card.locator.return_value.count.return_value = 1
        card.locator.return_value.nth.return_value = task_stop
        page = self.stop_page([composer_stop], card)
        with patch.object(rt, 'read_task_cards'), patch.object(rt, 'is_prior', return_value=False):
            self.assertEqual(rt.visible_stop_buttons(page, rt.selectors_from_env()), [composer_stop, task_stop])
            card.hover.assert_called_once_with(timeout=1500)
            # Page-wide controls exclude task card controls, which are read only through the hovered card.
            self.assertIs(composer_stop.evaluate.call_args.args[1], True)
            self.assertIs(task_stop.evaluate.call_args.args[1], False)

    def test_either_task_or_composer_stop_means_running(self):
        for task_visible, composer_visible in ((True, False), (False, True)):
            with self.subTest(task=task_visible, composer=composer_visible):
                card, task_stop = Mock(), self.stop_button(task_visible)
                card.locator.return_value.count.return_value = 1
                card.locator.return_value.nth.return_value = task_stop
                page = self.stop_page([self.stop_button(composer_visible)], card)
                with patch.object(rt, 'read_task_cards'), patch.object(rt, 'is_prior', return_value=False):
                    self.assertTrue(rt.response_is_running(page, rt.selectors_from_env()))

    def test_failed_hover_still_checks_the_composer(self):
        card, composer_stop = Mock(), self.stop_button()
        card.hover.side_effect = RuntimeError('hover failed')
        card.locator.return_value.count.return_value = 0
        page = self.stop_page([composer_stop], card)
        with patch.object(rt, 'read_task_cards'), patch.object(rt, 'is_prior', return_value=False):
            self.assertEqual(rt.visible_stop_buttons(page, rt.selectors_from_env()), [composer_stop])

    def test_cancel_clicks_the_same_hover_revealed_task_stop(self):
        card, task_stop, composer_stop = Mock(), self.stop_button(), self.stop_button()
        card.locator.return_value.count.return_value = 1
        card.locator.return_value.nth.return_value = task_stop
        task_stop.is_visible.side_effect = [True, False]
        composer_stop.is_visible.side_effect = [True, False]
        page = self.stop_page([composer_stop], card)
        with patch.object(rt, 'read_task_cards'), patch.object(rt, 'is_prior', return_value=False):
            self.assertTrue(rt.stop_generation(page, rt.selectors_from_env()))
        task_stop.click.assert_called_once_with(timeout=3000)
        composer_stop.click.assert_not_called()
        self.assertEqual(card.hover.call_count, 2)

    def test_heartbeat_reports_running_prompt_or_timeout_countdown(self):
        self.assertEqual(rt.heartbeat_message(True, 120.4, 0, 30),
                         'Prompt still running; 120s elapsed.')
        self.assertEqual(rt.heartbeat_message(False, 60, 4, 30),
                         'Prompt still running (output changed 4s ago); 60s elapsed.')
        self.assertEqual(rt.heartbeat_message(False, 200, 20.9, 30),
                         'No new output for 20s of 30s allowed; 200s elapsed.')

    def test_prompt_without_stop_control_or_output_fails_after_thirty_seconds(self):
        self.assertEqual(rt.NOT_RESPONDING_SECONDS, 30)
        self.assertEqual(rt.NOT_RESPONDING_MESSAGE,
                         'Copilot failed to provide an answer, the running prompt was not responding for 30s')
        tree = ast.parse(SCRIPT.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_roundtrip')
        source = ast.unparse(fn)
        self.assertIn('idle_limit = NOT_RESPONDING_SECONDS', source)
        self.assertIn('raise SystemExit(NOT_RESPONDING_MESSAGE)', source)
        self.assertNotIn('args.timeout', SCRIPT.read_text())

    def test_task_status_message_describes_visible_plan(self):
        tasks = [{'title': 'Add browser games', 'state': 'In progress', 'active': True,
                  'steps': [
                      {'title': 'Extract repository', 'state': 'completed'},
                      {'title': 'Package repository', 'state': 'active'},
                  ]}]
        self.assertEqual(rt.task_status_message(tasks),
                         'Copilot task "Add browser games" is in progress; 2 plan items visible.')
        self.assertEqual(rt.task_status_message([]), '')

    def test_download_search_is_gated_by_stop_control(self):
        tree = ast.parse(SCRIPT.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_roundtrip')
        loop = next(n for n in ast.walk(fn) if isinstance(n, ast.While) and ast.unparse(n.test) == 'downloaded is None')
        calls = {name: min(n.lineno for n in ast.walk(loop) if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name) and n.func.id == name)
                 for name in ('response_is_running', 'download_candidates', 'read_screen')}
        self.assertLess(calls['response_is_running'], calls['download_candidates'])
        self.assertLess(calls['download_candidates'], calls['read_screen'])
        gates = [n for n in loop.body if isinstance(n, ast.If) and ast.unparse(n.test) == 'response_finished']
        self.assertEqual(len(gates), 1)
        self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                            and n.func.id == 'download_candidates' for n in ast.walk(gates[0])))
        self.assertEqual(sum(1 for n in ast.walk(loop) if isinstance(n, ast.Call)
                             and isinstance(n.func, ast.Name) and n.func.id == 'download_candidates'), 1)

    def test_stop_control_changes_reset_the_inactivity_timer(self):
        tree = ast.parse(SCRIPT.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_roundtrip')
        loop = next(n for n in ast.walk(fn) if isinstance(n, ast.While) and ast.unparse(n.test) == 'downloaded is None')
        source = ast.unparse(loop)
        self.assertRegex(source, r'if last_running is not None and running != last_running:\n\s+last_activity = now')

    def test_visible_stop_control_holds_off_the_timeout_and_heartbeat_is_periodic(self):
        tree = ast.parse(SCRIPT.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_roundtrip')
        loop = next(n for n in ast.walk(fn) if isinstance(n, ast.While) and ast.unparse(n.test) == 'downloaded is None')
        source = ast.unparse(loop)
        self.assertRegex(source, r'if running:\n\s+last_activity = now')
        self.assertIn('if now - last_update >= HEARTBEAT_SECONDS:', source)
        self.assertIn("emit_status('heartbeat', heartbeat_message(running, now - started, idle, idle_limit))", source)

    def test_onedrive_cleanup_removes_copilot_folder_then_empties_the_bin_and_never_raises(self):
        ctx, page = Mock(), Mock()
        ctx.new_page.return_value = page
        with patch.object(rt, 'open_signed_in') as opened, \
                patch.object(rt, 'delete_copilot_folder', return_value=1), \
                patch.object(rt, 'empty_recycle_bin', return_value=True), \
                patch.object(rt, 'emit_status') as status:
            rt.clean_onedrive(ctx)
        self.assertEqual([call.args[1] for call in opened.call_args_list],
                         ['https://kurwalabb-my.sharepoint.com/my', 'https://kurwalabb-my.sharepoint.com/recycle'])
        messages = [call.args[1] for call in status.call_args_list]
        self.assertIn('Deleted the OneDrive folder Microsoft Copilot Chat Files.', messages)
        self.assertEqual(messages[-1], 'Emptied the OneDrive recycle bin.')
        page.close.assert_called_once_with()
        with patch.object(rt, 'open_signed_in', side_effect=RuntimeError('sign-in required')), \
                patch.object(rt, 'emit_status') as status:
            rt.clean_onedrive(ctx)
        self.assertEqual(status.call_args_list[-1].args,
                         ('cleanup', 'OneDrive cleanup did not finish: sign-in required'))

    def test_onedrive_recycle_bin_is_emptied_when_folder_deletion_fails(self):
        ctx, page = Mock(), Mock()
        ctx.new_page.return_value = page
        with patch.object(rt, 'open_signed_in') as opened, \
                patch.object(rt, 'delete_copilot_folder', side_effect=RuntimeError('no Delete command')), \
                patch.object(rt, 'empty_recycle_bin', return_value=False) as emptied, \
                patch.object(rt, 'emit_status') as status:
            rt.clean_onedrive(ctx)
        self.assertEqual(opened.call_args_list[-1].args[1], 'https://kurwalabb-my.sharepoint.com/recycle')
        emptied.assert_called_once_with(page)
        messages = [call.args[1] for call in status.call_args_list]
        self.assertIn('OneDrive cleanup did not finish: no Delete command', messages)
        self.assertEqual(messages[-1], 'The OneDrive recycle bin is already empty.')

    def test_every_submitted_prompt_cleans_onedrive_before_the_browser_closes(self):
        tree = ast.parse(SCRIPT.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_roundtrip')
        source = ast.unparse(fn)
        self.assertRegex(source, r'if submitted:\n\s+clean_onedrive\(ctx\)')
        self.assertNotIn('ctx.close()\n            raise SystemExit', source)
        self.assertEqual(source.count('ctx.close()'), 1)
        self.assertEqual(sum(1 for n in ast.walk(fn) if isinstance(n, ast.Call)
                             and isinstance(n.func, ast.Name) and n.func.id == 'close_browser'), 4)
        handler = next(n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)
                       and ast.unparse(n.type) == '(Exception, SystemExit)')
        self.assertEqual([ast.unparse(s) for s in handler.body], ['close_browser()', 'raise'])

    def test_signature_survives_a_failing_activity_parser(self):
        page = Mock()
        page.evaluate.side_effect = lambda script, *args: '10:1' if script == rt.SIGNATURE_SCRIPT else 1 / 0
        self.assertEqual(rt.read_screen(page, rt.selectors_from_env()), ('10:1', None, None))

    def test_output_signature_is_initialized_after_submission(self):
        source = SCRIPT.read_text()
        filled = source.index('composer.fill(prompt)')
        submit = source.index('send_with_retry(page, composer, sel["send"])')
        initialize = source.index('last_signature = None', submit)
        first_snapshot = source.index('signature, blocks, answers = read_screen(page, sel, prompt)', initialize)
        self.assertNotIn('read_screen(page, sel, prompt)', source[filled:submit])
        self.assertLess(submit, initialize)
        self.assertLess(initialize, first_snapshot)


class ArchiveTests(unittest.TestCase):
    def test_archive_includes_history_without_remotes_hooks_or_reflogs(self):
        import subprocess
        import zipfile

        def git(*args, cwd):
            return subprocess.run(['git', *args], cwd=cwd, check=True, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

        with TemporaryDirectory() as temporary:
            root = Path(temporary) / 'repo'
            root.mkdir()
            git('init', '-b', 'main', cwd=root)
            git('remote', 'add', 'origin', 'https://github.com/me/app.git', cwd=root)
            for index in range(2):
                (root / 'app.txt').write_text(f'version {index}\n')
                git('add', '-A', cwd=root)
                git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-m',
                    f'Copilot: change {index}\n\nCopilot output:\nDone {index}.', cwd=root)
            out = Path(temporary) / 'repo.zip.txt'
            _, count, history = rt.make_archive(root, out)
            self.assertEqual(count, 1)
            self.assertGreater(history, 0)
            extracted = Path(temporary) / 'extracted'
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                z.extractall(extracted)
            self.assertIn('app.txt', names)
            self.assertFalse(any(n.startswith(('.git/hooks/', '.git/logs/')) for n in names))
            self.assertEqual(git('log', '--format=%B', cwd=extracted).count('Copilot output:'), 2)
            self.assertEqual(git('remote', cwd=extracted), '')
            self.assertEqual(git('status', '--porcelain', cwd=extracted), '')
            # Returned archives must still not carry Git metadata.
            with zipfile.ZipFile(out) as z, self.assertRaisesRegex(SystemExit, 'contains .git'):
                rt.safe_members(z)


class FreshArchiveTests(unittest.TestCase):
    def test_archive_without_history_has_no_git_metadata(self):
        import subprocess
        import zipfile
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / 'repo'
            root.mkdir()
            run = lambda *args: subprocess.run(['git', *args], cwd=root, check=True,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            run('init', '-b', 'main')
            (root / 'app.txt').write_text('v\n')
            run('add', '-A')
            run('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-m', 'Initial commit')
            out = Path(temporary) / 'repo.zip.txt'
            _, count, history = rt.make_archive(root, out, history=False)
            self.assertEqual((count, history), (1, 0))
            with zipfile.ZipFile(out) as z:
                self.assertEqual(z.namelist(), ['app.txt'])


class DownloadFlowTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock(url='https://chat.test/conversation')
        self.page.locator.return_value.locator.return_value.count.return_value = 0
        self.ctx = Mock(pages=[self.page])
        self.listeners = {}
        self.page.on.side_effect = lambda event, callback: self.listeners.update({event: callback})
        self.download = Mock(suggested_filename='fixed.zip')
        self.node = Mock()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def download_now(self):
        self.listeners['download'](self.download)

    def test_download_survives_interrupted_click(self):
        def click(**kwargs):
            self.download_now()
            raise RuntimeError('Download navigation interrupted')
        self.node.click.side_effect = click
        self.assertEqual(rt.click_download(self.ctx, self.page, self.node, self.work),
                         self.work / 'fixed.zip')
        self.download.save_as.assert_called_once_with(str(self.work / 'fixed.zip'))
        self.page.remove_listener.assert_called_once()
        self.ctx.remove_listener.assert_called_once()

    def test_preview_downloads_and_restores_chat(self):
        for popup in (False, True):
            with self.subTest(popup=popup):
                self.setUp()
                preview = Mock(url='https://chat.test/preview') if popup else self.page
                control = Mock()
                control.is_visible.return_value = True
                control.is_enabled.return_value = True
                control.get_attribute.return_value = 'Download'
                control.inner_text.return_value = 'Download'
                controls = Mock()
                controls.count.return_value = 1
                controls.nth.return_value = control
                preview.locator.return_value = controls
                def open_preview(**kwargs):
                    if popup:
                        self.ctx.pages.append(preview)
                    else:
                        self.page.url = 'https://chat.test/preview'
                self.node.click.side_effect = open_preview
                def download(**kwargs):
                    callback = next(call.args[1] for call in preview.on.call_args_list
                                    if call.args[0] == 'download')
                    callback(self.download)
                control.click.side_effect = download
                self.assertEqual(rt.click_download(self.ctx, self.page, self.node, self.work),
                                 self.work / 'fixed.zip')
                if popup:
                    preview.close.assert_called_once()
                    self.page.go_back.assert_not_called()
                else:
                    self.page.go_back.assert_called_once()

    def test_dialog_download(self):
        control = Mock()
        controls = self.page.locator.return_value.locator.return_value
        controls.count.return_value = 1
        controls.nth.return_value = control
        control.get_attribute.return_value = 'Download'
        control.inner_text.return_value = 'Download'
        control.click.side_effect = lambda **kwargs: self.download_now()
        self.assertEqual(rt.click_download(self.ctx, self.page, self.node, self.work),
                         self.work / 'fixed.zip')

    def test_cancellation_is_not_swallowed(self):
        self.node.click.side_effect = rt.QuitRequested()
        with self.assertRaises(rt.QuitRequested):
            rt.click_download(self.ctx, self.page, self.node, self.work)
        self.download.save_as.assert_not_called()
        self.ctx.remove_listener.assert_called_once()

    def test_no_download_returns_none_and_restores_chat(self):
        self.node.click.side_effect = lambda **kwargs: setattr(self.page, 'url', 'https://chat.test/preview')
        self.assertIsNone(rt.click_download(self.ctx, self.page, self.node, self.work, timeout_seconds=0))
        self.page.go_back.assert_called_once()
        self.download.save_as.assert_not_called()


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise unittest.SkipTest(str(e))
        cls.pw = sync_playwright().start()
        try:
            options = {'headless': True}
            if os.environ.get('ROUNDTRIP_TEST_CHROMIUM'):
                options['executable_path'] = os.environ['ROUNDTRIP_TEST_CHROMIUM']
            cls.browser = cls.pw.chromium.launch(**options)
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(str(e))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.sel = rt.selectors_from_env()

    def test_interim_answer_in_separate_assistant_message_precedes_coding(self):
        self.page.set_content('''<main>
          <article data-roundtrip-prior-msg="1" data-message-author-role="assistant">
            <p>Earlier answer.</p></article>
          <article data-message-author-role="assistant">
            <p>I'll add two standalone games while preserving the app.</p></article>
          <article data-message-author-role="assistant">
            <details><summary>Coding and executing</summary><pre>secret()</pre></details>
          </article></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(answers, [{'after': 0, 'text':
            "I'll add two standalone games while preserving the app."}])
        self.assertNotIn('Earlier answer', str(answers))
        self.assertNotIn('secret()', str((blocks, answers)))

    def test_interim_prose_outside_message_is_kept_when_segment_counts_match(self):
        self.page.set_content('''<main><p>Requested repository change</p><section>
          <p>I'll add two standalone games.</p>
          <article data-message-author-role="assistant">
            <p>Preserving the app.</p>
            <details><summary>Coding and executing</summary><pre>secret()</pre></details>
          </article></section></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel, 'Requested repository change')
        self.assertEqual(len(blocks), 1)
        self.assertEqual(answers, [{'after': 0, 'text':
            "I'll add two standalone games.\nPreserving the app."}])
        self.assertNotIn('Requested repository change', str(answers))

    def test_split_current_messages_keep_interim_order_in_saved_chat(self):
        self.page.set_content('''<main>
          <article data-message-author-role="assistant" data-roundtrip-prior-msg="1">
            <p>Earlier answer.</p></article>
          <p>Requested repository change</p>
          <article data-message-author-role="assistant"><p>Starting the games.</p></article>
          <article data-message-author-role="assistant">
            <details><summary>Coding and executing</summary><pre>secret()</pre></details>
          </article>
          <article data-message-author-role="assistant"><p>Games are ready.</p></article>
          </main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel, 'Requested repository change')
        self.assertEqual(len(blocks), 1)
        self.assertEqual(answers, [{'after': 0, 'text': 'Starting the games.'},
                                   {'after': 1, 'text': 'Games are ready.'}])

    def test_plain_reasoned_headers_capture_coding_cards_without_tasks(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <p>I am checking the implementation.</p>
          <div><span>Reasoned for 12s</span><span aria-hidden="true">⌄</span></div>
          <section><div>Coding and executing<pre>secret_one()</pre></div>
            <div>Coding and executing<pre>secret_two()</pre></div></section>
          <p>I found the cause.</p>
          <div>Reasoned for 24s</div>
          <section><div>Coding and executing<pre>secret_three()</pre></div></section>
          <p>Done.</p></article></main>''')
        self.assertEqual(rt.read_task_cards(self.page), [])
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertEqual([b['title'] for b in blocks], ['Reasoned for 12s', 'Reasoned for 24s'])
        self.assertEqual([b['layout'] for b in blocks], ['section', 'section'])
        self.assertEqual([len(b['steps']) for b in blocks], [2, 1])
        self.assertTrue(all(not b['active'] for b in blocks))
        self.assertTrue(all(s['kind'] == 'coding' and not s['lines']
                            for b in blocks for s in b['steps']))
        self.assertEqual([a['after'] for a in answers], [0, 1, 2])
        self.assertNotIn('secret_', str((blocks, answers)))
        self.assertIn('I found the cause.', answers[1]['text'])

    def test_plain_reasoning_header_requires_an_adjacent_step_panel(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <p>Reasoning</p><p>This is ordinary answer prose.</p>
          <p>Reasoned for 12s</p><p>Done.</p></article></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertEqual(blocks, [])
        self.assertIn('This is ordinary answer prose.', str(answers))

    def test_plain_reasoning_headers_ignore_prior_turns_and_code(self):
        self.page.set_content('''<main>
          <article data-message-author-role="assistant" data-roundtrip-prior-msg="1">
            <div>Reasoned for 12s</div><section>Coding and executing<pre>old()</pre></section>
          </article><article data-message-author-role="assistant">
            <pre><span>Reasoned for 24s</span></pre><p>Done.</p>
          </article></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertEqual(blocks, [])
        self.assertNotIn('old()', str(answers))

    def test_plain_running_reasoning_header_captures_coding_activity(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <div>Reasoning</div><section>Coding and executing<pre>secret()</pre></section>
          </article></main>''')
        _, blocks, _ = rt.read_screen(self.page, self.sel)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0]['active'])
        self.assertEqual(blocks[0]['steps'][0]['kind'], 'coding')

    def test_signature_reads_text_outside_main_and_in_open_shadow_roots(self):
        self.page.set_content('''<main><p>Static navigation</p></main><section id="chat"></section>
          <div id="host"></div><script>host.attachShadow({mode: 'open'}).innerHTML = '<p>Waiting</p>';</script>''')
        first = rt.read_signature(self.page)
        self.page.evaluate("document.querySelector('#chat').textContent = 'Copilot reply'")
        second = rt.read_signature(self.page)
        self.page.evaluate("host.shadowRoot.querySelector('p').textContent = 'Shadow reply'")
        third = rt.read_signature(self.page)
        self.assertEqual(len({first, second, third}), 3)

    def test_signature_ignores_elapsed_counters_and_clock_times(self):
        self.page.set_content('<main><p id="t">Working for 12s at 23:15</p></main>')
        first = rt.read_signature(self.page)
        self.page.evaluate("document.querySelector('#t').textContent = 'Working for 3m 4s at 23:16:05'")
        self.assertEqual(rt.read_signature(self.page), first)

    def test_unwrapped_response_after_the_prompt_bubble_is_streamed(self):
        self.page.set_content('''<main><section><div class="turn"><p>Earlier request</p></div>
            <div class="turn"><div class="bubble">Requested repository change</div></div>
            <div class="reply"><p>I am creating the app now.</p><div class="code-output">x = 1</div></div>
            </section><div class="composer"><textarea></textarea></div></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel, 'Requested repository change')
        self.assertEqual(blocks, [])
        self.assertEqual([a['after'] for a in answers], [0])
        self.assertIn('I am creating the app now.', answers[0]['text'])
        self.assertNotIn('Earlier request', str(answers))
        self.assertNotIn('Requested repository change', str(answers))

    def test_response_in_open_shadow_roots_is_streamed(self):
        self.page.set_content('''<main><p>Requested repository change</p><chat-turn id="turn"></chat-turn></main>
          <script>turn.attachShadow({mode: 'open'}).innerHTML =
            '<div data-message-author-role="assistant"><p>Checking the repository.</p>' +
            '<details open><summary>Reasoning</summary><p>Comparing files.</p></details>' +
            '<p>Found the cause.</p></div>';</script>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel, 'Requested repository change')
        self.assertEqual([b['title'] for b in blocks], ['Reasoning'])
        self.assertEqual(blocks[0]['lines'], ['Comparing files.'])
        self.assertEqual(answers, [{'after': 0, 'text': 'Checking the repository.'},
                                   {'after': 1, 'text': 'Found the cause.'}])

    def test_task_card_in_open_shadow_root_is_reported(self):
        self.page.set_content('''<main><task-host id="host"></task-host></main>
          <script>host.attachShadow({mode: 'open'}).innerHTML =
            '<details open><summary><span>Build app</span> <span>In progress</span></summary>' +
            '<ol><li aria-label="Completed">Inspect files</li><li aria-current="step">Build</li></ol></details>';
          </script>''')
        tasks = rt.read_task_cards(self.page)
        self.assertEqual([(t['title'], [s['state'] for s in t['steps']]) for t in tasks],
                         [('Build app', ['completed', 'active'])])

    def test_empty_assistant_wrapper_falls_back_to_text_after_the_prompt(self):
        self.page.set_content('''<main><section>
            <div class="turn"><div class="bubble">Requested repository change</div></div>
            <div class="reply"><div data-testid="copilot-message-avatar"><button>Copilot</button></div>
              <p>I am creating the app now.</p></div>
            </section></main>''')
        _, _, answers = rt.read_screen(self.page, self.sel, 'Requested repository change')
        self.assertEqual(answers, [{'after': 0, 'text': 'I am creating the app now.'}])

    def test_response_sharing_a_container_with_the_composer_is_streamed(self):
        self.page.set_content('''<main><div class="bubble"><span>Requested </span><b>repository</b>
            <span> change</span></div>
            <div class="bottom"><div class="reply"><p>Working on the change.</p></div>
              <div class="composer"><textarea></textarea><p>Copilot uses AI.</p></div></div></main>''')
        _, _, answers = rt.read_screen(self.page, self.sel, 'Requested repository change')
        self.assertEqual(answers, [{'after': 0, 'text': 'Working on the change.'}])

    def test_monospace_highlighted_and_multiline_code_is_not_streamed(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
            <p>Starting.</p>
            <div style="white-space: pre-wrap; font-family: Consolas, monospace">secret_mono()</div>
            <div class="hljs language-python">secret_hljs()</div>
            <code style="display: inline-block">secret_one()
secret_two()</code>
            <div role="code">secret_role()</div>
            <p>Use <code>make run</code> later.</p></article></main>''')
        _, _, answers = rt.read_screen(self.page, self.sel)
        text = str(answers)
        self.assertIn('Starting.', text)
        self.assertIn('make run', text)
        self.assertNotIn('secret_', text)

    def test_plan_rows_and_composer_do_not_create_extra_task_cards(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
            <details open><summary><span>Build app</span> <span>In progress</span></summary>
            <ol><li aria-label="Completed">Inspect files</li><li aria-current="step">Build</li></ol></details>
            </article><form><span>Done</span><button aria-label="Stop responding">Stop</button></form></main>''')
        tasks = rt.read_task_cards(self.page)
        self.assertEqual([task['title'] for task in tasks], ['Build app'])
        self.assertEqual(self.page.locator('main[data-roundtrip-task-card]').count(), 0)

    def test_standalone_coding_details_are_expanded_and_streamed(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <details id="coding"><summary>Coding and executing</summary><pre>secret()</pre></details>
          <p>Done.</p></article></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertEqual([(block['title'], block['active']) for block in blocks],
                         [('Coding and executing', True)])
        self.assertTrue(self.page.locator('#coding').evaluate('element => element.open'))
        self.assertIn('Done.', str(answers))
        self.assertNotIn('secret()', str(answers))

    def test_standalone_coding_button_is_expanded_and_streamed(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <button id="coding" aria-expanded="false" aria-controls="code-panel">Coding and executing</button>
          <section id="code-panel" hidden><pre>secret()</pre></section>
          <script>coding.onclick = () => {
            coding.setAttribute('aria-expanded', 'true');
            document.getElementById('code-panel').hidden = false;
          };</script></article></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertEqual([(block['title'], block['active']) for block in blocks],
                         [('Coding and executing', True)])
        self.assertEqual(self.page.locator('#coding').get_attribute('aria-expanded'), 'true')
        self.assertNotIn('secret()', str((blocks, answers)))

    def test_nested_coding_control_is_expanded_without_streaming_code(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <details open><summary>Reasoning</summary>
            <details id="coding"><summary>Coding and executing</summary><pre>secret()</pre></details>
          </details></article></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertTrue(self.page.locator('#coding').evaluate('element => element.open'))
        self.assertEqual(blocks[0]['steps'], [
            {'title': 'Coding and executing', 'kind': 'coding', 'total': 0, 'lines': []}
        ])
        self.assertNotIn('secret()', str((blocks, answers)))

    def test_unlabelled_code_in_activity_gets_a_coding_placeholder(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <details open><summary>Creating file</summary>
            <p>Preparing the file.</p><pre>secret_code()</pre>
          </details></article></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertEqual(blocks[0]['layout'], 'section')
        self.assertEqual(blocks[0]['steps'][-1],
                         {'title': 'Coding and executing', 'kind': 'coding', 'total': 0, 'lines': []})
        self.assertNotIn('secret_code', str((blocks, answers)))

    def test_explicit_working_timeline_keeps_coding_steps(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <section><h2>Working through the steps</h2>
          <details><summary>Coding and executing</summary><pre>secret()</pre></details>
          </section></article></main>''')
        _, blocks, _ = rt.read_screen(self.page, self.sel)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['title'], 'Coding and executing')
        self.assertEqual(blocks[0]['layout'], 'working')

    MODEL_PICKER = '''<style>[role="menu"][hidden] { display: none; }
      [role="menuitem"] { display: block; width: 240px; height: 40px; }</style>
      <button aria-label="Model" id="picker">Auto</button>
      <div role="menu" id="main" hidden>
        <div role="menuitem" aria-haspopup="menu" data-sub="claude">Claude</div>
        <div role="menuitem" aria-haspopup="menu" data-sub="gpt">GPT</div>
        <div role="menuitem">Auto</div>
      </div>
      <div role="menu" id="claude" hidden>
        <div role="menuitem">Sonnet 5.5<br><small>Fast</small></div>
        <div role="menuitem">Opus 5.5</div>
      </div>
      <div role="menu" id="gpt" hidden>
        <div role="menuitem">GPT-6</div>
        <div role="menuitem">GPT-6.1 Sol</div>
        <div role="menuitem" aria-disabled="true">GPT-5 Legacy</div>
      </div>
      <p id="chosen"></p>
      <script>
        const menus = () => document.querySelectorAll('[role="menu"]');
        picker.onclick = () => { main.hidden = false; };
        document.querySelectorAll('[data-sub]').forEach(row => row.addEventListener('mouseenter', () => {
          claude.hidden = gpt.hidden = true;
          document.getElementById(row.dataset.sub).hidden = false;
        }));
        document.querySelectorAll('#claude [role="menuitem"], #gpt [role="menuitem"]').forEach(item =>
          item.addEventListener('click', () => {
            chosen.textContent = item.firstChild.textContent;
            menus().forEach(menu => menu.hidden = true);
          }));
        document.addEventListener('keydown', event => {
          if (event.key === 'Escape') menus().forEach(menu => menu.hidden = true);
        });
      </script>'''

    def test_model_check_reads_enabled_entries_of_both_provider_submenus(self):
        self.page.set_content(self.MODEL_PICKER)
        self.assertEqual(rt.list_models(self.page, self.sel), [
            'Claude/Sonnet 5.5', 'Claude/Opus 5.5', 'GPT/GPT-6', 'GPT/GPT-6.1 Sol'])
        self.assertTrue(self.page.locator('#main').is_hidden())

    def test_model_check_requires_at_least_one_model(self):
        self.page.set_content('<button aria-label="Model">Auto</button>')
        with self.assertRaisesRegex(SystemExit, 'No Claude or GPT models'):
            rt.list_models(self.page, self.sel)

    def test_listed_model_can_be_selected(self):
        self.page.set_content(self.MODEL_PICKER)
        rt.select_model(self.page, 'GPT/GPT-6.1 Sol', self.sel)
        self.assertEqual(self.page.locator('#chosen').inner_text(), 'GPT-6.1 Sol')

    def test_hidden_duplicate_temporary_toggle_does_not_hide_the_visible_one(self):
        self.page.set_content('''<button id="hidden" aria-label="Temporary chat" hidden></button>
          <button id="shown" aria-label="Temporary chat" aria-pressed="false"
            onclick="this.setAttribute('aria-pressed', 'true')"></button>''')
        rt.set_temporary_chat(self.page, self.sel, True, timeout_seconds=5)
        self.assertEqual(self.page.locator('#shown').get_attribute('aria-pressed'), 'true')

    def test_text_labelled_temporary_toggle_is_not_read_as_active(self):
        self.page.set_content('''<button id="toggle" role="switch" aria-checked="false"
            onclick="this.setAttribute('aria-checked', String(this.getAttribute('aria-checked') !== 'true'))">
            Temporary chat</button>''')
        self.assertFalse(rt.temporary_chat_active(self.page, self.sel))
        rt.set_temporary_chat(self.page, self.sel, True, timeout_seconds=5)
        self.assertEqual(self.page.locator('#toggle').get_attribute('aria-checked'), 'true')
        rt.set_temporary_chat(self.page, self.sel, False, timeout_seconds=5)
        self.assertEqual(self.page.locator('#toggle').get_attribute('aria-checked'), 'false')

    def test_temporary_chat_heading_outside_controls_marks_the_active_state(self):
        self.page.set_content('<main><h2>Temporary chat</h2></main>')
        self.assertTrue(rt.temporary_chat_active(self.page, self.sel))

    def test_temporary_chat_entry_inside_a_popup_menu_is_used(self):
        self.page.set_content('''<button id="menu-button" aria-label="New chat options" aria-haspopup="menu"
            onclick="document.getElementById('menu').hidden = false"></button>
          <div role="menu" id="menu" hidden>
            <div role="menuitem" onclick="document.getElementById('menu').hidden = true;
              document.querySelector('textarea').placeholder = 'Temporarily chatting'">Temporary chat</div>
          </div><textarea placeholder="Message Copilot"></textarea>''')
        with patch.object(rt, 'TEMPORARY_MENU_DELAY_SECONDS', 0):
            rt.set_temporary_chat(self.page, self.sel, True, timeout_seconds=5)
        self.assertTrue(rt.temporary_chat_active(self.page, self.sel))
        self.assertTrue(self.page.locator('#menu').is_hidden())

    def test_new_chat_control_replaces_the_reopened_conversation(self):
        self.page.set_content('''<nav><a href="#">Here is my repository archive. The attached .txt file contains ZIP bytes.</a>
            <button id="menu" aria-label="New chat" aria-haspopup="menu" onclick="this.dataset.opened = 1"></button>
            <button id="new" aria-label="New chat" onclick="document.querySelector('main').innerHTML = ''"></button>
            </nav><main><div data-message-author-role="user">Here is my repository archive.
              The attached .txt file contains ZIP bytes. Rename it to .zip</div>
            <div data-message-author-role="assistant">Done.</div></main>''')
        self.assertTrue(rt.chat_has_history(self.page, 'https://chat.test/chat/'))
        self.assertTrue(rt.open_new_chat(self.page, self.sel, 'https://chat.test/chat/', timeout_seconds=5))
        self.assertFalse(rt.chat_has_history(self.page, 'https://chat.test/chat/'))
        self.assertIsNone(self.page.locator('#menu').get_attribute('data-opened'))

    def test_earlier_prompt_without_message_markup_counts_as_history(self):
        self.page.set_content('''<main><div><p>Here is my repository archive.
            The attached .txt file contains ZIP bytes.</p></div><p>Copilot uses AI.</p></main>''')
        self.assertTrue(rt.chat_has_history(self.page, 'https://chat.test/chat/'))

    def test_empty_chat_with_history_sidebar_and_greeting_is_new(self):
        self.page.set_content('''<nav><a href="#">Here is my repository archive. The attached .txt file contains
            ZIP bytes.</a></nav><main><div class="fai-CopilotMessage">Hi, how can I help?</div>
            <form><textarea></textarea></form></main>''')
        self.assertFalse(rt.chat_has_history(self.page, 'https://chat.test/chat/'))
        self.assertFalse(rt.open_new_chat(self.page, self.sel, 'https://chat.test/chat/', timeout_seconds=1))

    def test_descriptive_zip_link_with_opaque_target(self):
        self.page.set_content('<a href="/artifact/123">Download the complete fixed repository ZIP</a>')
        found = rt.download_candidates(self.page, self.sel, [])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], '/artifact/123')

    def test_prior_uploaded_hidden_and_unrelated_links_excluded(self):
        self.page.set_content('''<a data-roundtrip-prior="1" href="/old">Previous ZIP</a>
          <a href="/upload">repo.zip.txt</a><a href="/unrelated">zipper</a>
          <a hidden href="/hidden">Result ZIP</a>
          <a href="/new">Fixed repository ZIP</a>''')
        found = rt.download_candidates(self.page, self.sel, ['repo.zip.txt'])
        self.assertEqual([x[1] for x in found], ['/new'])

    def test_finished_coding_only_section_needs_no_reasoning_subsection(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <details><summary>Reasoned for 18s</summary>
            <div>Coding and executing<pre>secret_code()</pre></div>
          </details><p>Done.</p>
          <a href="/artifact/123">Download the complete fixed repository ZIP</a>
          </article></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertEqual(len(blocks), 1)
        self.assertFalse(blocks[0]['active'])
        self.assertEqual(blocks[0]['steps'], [
            {'title': 'Coding and executing', 'kind': 'coding', 'total': 0, 'lines': []}
        ])
        self.assertNotIn('secret_code', str((blocks, answers)))
        self.assertEqual(len(rt.download_candidates(self.page, self.sel, [])), 1)
        self.assertTrue(rt.wait_for_response_end(self.page, self.sel, timeout_seconds=1))

    def test_long_running_task_card_is_expanded_and_reported(self):
        self.page.set_content('''<main><form><article data-message-author-role="assistant">
          <details id="task"><summary><span>Add Beer and Dinosaur Browser Games</span>
            <span>In progress</span></summary><ol>
            <li aria-label="Completed">Extract and inspect the repository and its instructions</li>
            <li aria-current="step">Review documentation and comments, then test and verify the repository</li>
          </ol></details></article></form></main>''')
        expected = [{
            'title': 'Add Beer and Dinosaur Browser Games',
            'state': 'In progress',
            'active': True,
            'steps': [
                {'title': 'Extract and inspect the repository and its instructions', 'state': 'completed'},
                {'title': 'Review documentation and comments, then test and verify the repository', 'state': 'active'},
            ],
        }]
        self.assertEqual(rt.read_task_cards(self.page), expected)
        self.assertTrue(self.page.locator('#task').evaluate('e => e.open'))
        self.assertEqual(rt.read_task_cards(self.page), expected)
        self.assertTrue(self.page.locator('#task').evaluate('e => e.open'))

    def test_completed_titled_step_is_distinct_from_thought_details(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <details open><summary>Worked for 1m</summary><div>
            <details open><summary>Thought process</summary><p>Inspecting the implementation.</p></details>
            <div><span aria-label="Completed">&#10003;</span><span>Reviewing Python comments</span></div>
            <details open><summary>Thought process</summary><p>Preparing the tests.</p></details>
          </div></details></article></main>''')
        _, blocks, _ = rt.read_screen(self.page, self.sel)
        self.assertEqual(blocks[0]['layout'], 'section')
        self.assertEqual([(step['title'], step['kind']) for step in blocks[0]['steps']], [
            ('Thought process', 'thought'),
            ('Reviewing Python comments', 'completed'),
            ('Thought process', 'thought'),
        ])

    def test_working_steps_show_more_reveals_every_step(self):
        titles = [f'Reviewing item {chr(97 + i // 26)}{chr(97 + i % 26)}' for i in range(35)]
        rows = ''.join(f'<div>{title}</div>' for title in titles)
        self.page.set_content(f'''<main><article data-message-author-role="assistant">
          <section><h2>Working through the steps</h2>
            <details open><summary>Reasoned for 1m</summary><div>
              <div id="older" hidden>{rows}</div>
              <button onclick="document.querySelector('#older').hidden=false;this.remove()">Show more</button>
            </div></details>
          </section></article></main>''')
        _, blocks, _ = rt.read_screen(self.page, self.sel)
        self.assertFalse(self.page.locator('#older').get_attribute('hidden'))
        self.assertEqual([step['title'] for step in blocks[0]['steps']], titles)

    def test_stop_control_in_composer_or_task_card_is_running_signal(self):
        cases = (
            '<form><button aria-label="Stop responding"><svg></svg></button></form>',
            '<section><button><svg data-icon-name="Stop"></svg></button></section>',
        )
        for markup in cases:
            with self.subTest(markup=markup):
                self.page.set_content(markup)
                self.assertTrue(rt.response_is_running(self.page, self.sel))
                self.page.locator('button').evaluate('button => button.hidden = true')
                self.assertFalse(rt.response_is_running(self.page, self.sel))

    def test_hover_reveals_task_stop_for_detection_and_cancellation(self):
        self.page.set_content('''<style>
          #task button { opacity: 0; } #task:hover button { opacity: 1; }
          </style><main><article data-message-author-role="assistant">
          <details id="task" open><summary>Build repository <span>In progress</span></summary>
          <ol><li aria-current="step">Test repository</li></ol>
          <button aria-label="Stop task" onclick="this.remove()">Stop</button>
          </details></article></main>''')
        self.assertTrue(rt.response_is_running(self.page, self.sel))
        self.assertTrue(rt.stop_generation(self.page, self.sel))
        self.assertFalse(rt.response_is_running(self.page, self.sel))

    def test_composer_stop_counts_when_the_task_card_has_none(self):
        self.page.set_content('''<main><details open><summary>Build repository
          <span>Completed</span></summary><ol><li>Test repository</li></ol></details>
          <form><button aria-label="Stop responding">Stop</button></form></main>''')
        self.assertTrue(rt.response_is_running(self.page, self.sel))
        self.page.locator('form button').evaluate('button => button.hidden = true')
        self.assertFalse(rt.response_is_running(self.page, self.sel))

    def test_hover_revealed_task_stop_and_composer_stop_are_both_detected(self):
        self.page.set_content('''<style>
          #task button { opacity: 0; } #task:hover button { opacity: 1; }
          </style><main><article data-message-author-role="assistant">
          <details id="task" open><summary>Build repository <span>In progress</span></summary>
          <ol><li aria-current="step">Test repository</li></ol>
          <button id="task-stop" aria-label="Stop task" onclick="this.remove()">Stop</button>
          </details></article>
          <form><button id="composer-stop" aria-label="Stop responding" onclick="this.remove()">Stop</button></form>
          </main>''')
        buttons = rt.visible_stop_buttons(self.page, self.sel)
        self.assertEqual([button.get_attribute('id') for button in buttons], ['composer-stop', 'task-stop'])
        # Cancellation clicks the task card's Stop first and keeps going until no Stop control remains.
        self.assertTrue(rt.stop_generation(self.page, self.sel))
        self.assertEqual(self.page.locator('#task-stop, #composer-stop').count(), 0)
        self.assertFalse(rt.response_is_running(self.page, self.sel))

    def test_reused_composer_control_is_not_excluded_by_prior_marker(self):
        self.page.set_content('<form><button data-roundtrip-prior="1" aria-label="Stop responding">Stop</button></form>')
        self.assertTrue(rt.response_is_running(self.page, self.sel))

    MY_FILES = '''<style>.check { visibility: hidden; } [role="row"]:hover .check { visibility: visible; }</style>
      <div role="menubar" id="bar"><button id="copilot">Copilot</button></div>
      <div role="grid">
        <div role="row"><span role="columnheader">Name</span><span role="columnheader">File size</span></div>
        <div role="row" aria-selected="false"><span role="gridcell"><span class="check" role="checkbox"
          aria-checked="false" aria-label="Select row">&#9711;</span></span><span role="gridcell"><button>Documents</button>
          </span><span role="gridcell">1 item</span></div>
        <div role="row" aria-selected="false"><span role="gridcell"><span class="check" role="checkbox"
          aria-checked="false" aria-label="Select row">&#9711;</span></span><span role="gridcell"><button>Microsoft Copilot
          Chat Files</button></span><span role="gridcell">7 items</span></div>
        <div role="row" aria-selected="false"><span role="gridcell"><span class="check" role="checkbox"
          aria-checked="false" aria-label="Select row">&#9711;</span></span><span role="gridcell"><button>notes.docx</button>
          </span><span role="gridcell">12 KB</span></div>
      </div>
      <script>
        const bar = document.getElementById('bar');
        const selected = () => Array.from(document.querySelectorAll('[role="row"][aria-selected="true"]'));
        document.querySelectorAll('.check').forEach(check => check.addEventListener('click', () => {
          const row = check.closest('[role="row"]');
          const on = row.getAttribute('aria-selected') !== 'true';
          row.setAttribute('aria-selected', String(on));
          check.setAttribute('aria-checked', String(on));
          let del = document.getElementById('delete');
          if (selected().length && !del) {
            del = document.createElement('button'); del.id = 'delete'; del.textContent = 'Delete';
            del.onclick = () => {
              const dialog = document.createElement('div');
              dialog.setAttribute('role', 'dialog');
              dialog.innerHTML = '<h2>Delete?</h2><button id="confirm">Delete</button><button>Cancel</button>';
              dialog.querySelector('#confirm').onclick = () => {
                selected().forEach(r => r.remove()); dialog.remove(); del.remove();
              };
              document.body.appendChild(dialog);
            };
            bar.appendChild(del);
          }
        }));
      </script>'''
    RECYCLE_BIN = '''<div role="menubar" id="bar"><button id="empty">Empty recycle bin</button></div>
      <div role="grid"><div role="row"><span role="columnheader">Name</span></div>
        <div role="row" aria-selected="false"><span role="gridcell"><span class="check" role="checkbox"
          aria-checked="false" aria-label="Select row">&#9711;</span></span>
          <span role="gridcell">Microsoft Copilot Chat Files</span></div>
        <div role="row" aria-selected="false"><span role="gridcell"><span class="check" role="checkbox"
          aria-checked="false" aria-label="Select row">&#9711;</span></span>
          <span role="gridcell">Documents</span></div></div>
      <script>
        const bar = document.getElementById('bar');
        const selected = () => Array.from(document.querySelectorAll('[role="row"][aria-selected="true"]'));
        document.getElementById('empty').onclick = () => {
          const dialog = document.createElement('div');
          dialog.setAttribute('role', 'dialog');
          dialog.innerHTML = '<h2>Empty recycle bin?</h2><button id="confirm-empty">Yes</button><button>No</button>';
          dialog.querySelector('#confirm-empty').onclick = () => {
            document.querySelectorAll('[role="row"]:not(:first-child)').forEach(r => r.remove());
            dialog.remove();
          };
          document.body.appendChild(dialog);
        };
        document.querySelectorAll('.check').forEach(check => check.addEventListener('click', () => {
          const row = check.closest('[role="row"]');
          const on = row.getAttribute('aria-selected') !== 'true';
          row.setAttribute('aria-selected', String(on));
          check.setAttribute('aria-checked', String(on));
          let del = document.getElementById('delete');
          if (selected().length && !del) {
            del = document.createElement('button'); del.id = 'delete'; del.textContent = 'Delete';
            del.onclick = () => {
              const dialog = document.createElement('div');
              dialog.setAttribute('role', 'dialog');
              dialog.innerHTML = '<h2>Permanently delete?</h2><button id="yes">Yes</button><button>No</button>';
              dialog.querySelector('#yes').onclick = () => {
                selected().forEach(r => r.remove()); dialog.remove(); del.remove();
              };
              document.body.appendChild(dialog);
            };
            bar.appendChild(del);
          }
        }));
      </script>'''

    def test_onedrive_cleanup_deletes_only_the_copilot_folder(self):
        self.page.set_content(self.MY_FILES)
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        rows = self.page.locator('[role="row"]').all_inner_texts()
        self.assertEqual(len(rows), 3)
        self.assertIn('Documents', rows[1])
        self.assertIn('notes.docx', rows[2])
        self.assertEqual(self.page.locator('[role="dialog"]').count(), 0)

    # Row checks whose transparent inputs sit under their visible indicators.
    TABLE_FILES = '''<style>
        .check { position: relative; display: inline-block; width: 20px; height: 20px; }
        .check input { position: absolute; inset: 0; margin: 0; opacity: 0; }
        .check i { position: absolute; inset: 0; border-radius: 10px; border: 1px solid #333; }
        tbody .check { visibility: hidden; } tbody tr:hover .check, tbody tr.sel .check { visibility: visible; }
      </style>
      <div id="bar"><button>Share</button></div>
      <table><thead><tr><th><span class="check"><input type="checkbox" aria-label="Select all items"><i></i></span></th>
        <th>Name</th><th>File size</th></tr></thead><tbody>{rows}</tbody></table>
      <script>
        const bar = document.getElementById('bar');
        const rows = () => Array.from(document.querySelectorAll('tbody tr'));
        const sync = () => {
          rows().forEach(r => r.classList.toggle('sel', r.querySelector('input').checked));
          const any = rows().some(r => r.querySelector('input').checked);
          let del = document.getElementById('delete');
          if (any && !del) {
            del = document.createElement('button'); del.id = 'delete'; del.textContent = 'Delete';
            del.onclick = () => {
              const dialog = document.createElement('div');
              dialog.setAttribute('role', 'dialog');
              dialog.innerHTML = '<h2>Delete?</h2><button id="confirm">Delete</button><button>Cancel</button>';
              dialog.querySelector('#confirm').onclick = () => {
                rows().filter(r => r.querySelector('input').checked).forEach(r => r.remove());
                dialog.remove(); del.remove();
              };
              document.body.appendChild(dialog);
            };
            bar.appendChild(del);
          } else if (!any && del) del.remove();
        };
        document.querySelectorAll('tbody input').forEach(input => input.addEventListener('change', sync));
      </script>'''
    FOLDER = ('<tr><td><span class="check"><input type="checkbox" aria-label="Select row"><i></i></span></td>'
              '<td><img alt="" src="data:,folder"><button>%s</button></td><td>%s</td></tr>')
    FILE = ('<tr><td><span class="check"><input type="checkbox" aria-label="Select row"><i></i></span></td>'
            '<td><button>%s</button></td><td>12 KB</td></tr>')

    def test_onedrive_deletes_the_only_copilot_folder(self):
        self.page.set_content(self.TABLE_FILES.replace('{rows}', self.FOLDER % ('Microsoft Copilot Chat Files', '7 items')))
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual(self.page.locator('tbody tr').count(), 0)
        self.assertEqual(self.page.locator('[role="dialog"]').count(), 0)

    def test_onedrive_folder_with_a_new_item_marker_is_deleted(self):
        # The name follows a "new" marker icon and its screen-reader label, as in OneDrive My files.
        row = ('<tr><td><span class="check"><input type="checkbox" aria-label="Select row"><i></i></span></td>'
               '<td><img alt="" src="data:,folder"><button><i data-icon-name="Glimmer" aria-hidden="true">&#xF4A5;</i>'
               '<span style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)">New</span>'
               'Microsoft Copilot Chat Files</button></td><td>2 hours ago</td><td>copilotuser</td><td>13 items</td></tr>')
        rows = self.FOLDER % ('Documents', '1 item') + row
        self.page.set_content(self.TABLE_FILES.replace('{rows}', rows))
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual(self.page.locator('tbody tr button').all_inner_texts(), ['Documents'])

    def test_onedrive_preselected_items_are_cleared_before_deleting_the_copilot_folder(self):
        rows = (self.FOLDER % ('Documents', '1 item') + self.FILE % 'notes.docx' +
                self.FOLDER % ('Microsoft Copilot Chat Files', '7 items'))
        self.page.set_content(self.TABLE_FILES.replace('{rows}', rows))
        self.page.evaluate('''() => document.querySelectorAll('tbody input').forEach((input, index) => {
            if (index < 2) { input.checked = true; input.dispatchEvent(new Event('change')); }
        })''')
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual(self.page.locator('tbody tr button').all_inner_texts(), ['Documents', 'notes.docx'])
        self.assertEqual(self.page.locator('tbody input:checked').count(), 0)

    def test_onedrive_without_copilot_folder_deletes_nothing(self):
        rows = self.FOLDER % ('Documents', '1 item') + self.FILE % 'Microsoft Copilot Chat Files'
        self.page.set_content(self.TABLE_FILES.replace('{rows}', rows))
        self.assertEqual(rt.delete_copilot_folder(self.page), 0)
        self.assertEqual(self.page.locator('tbody tr').count(), 2)

    def test_onedrive_recycle_bin_is_emptied_after_confirmation(self):
        self.page.set_content(self.RECYCLE_BIN)
        self.assertTrue(rt.empty_recycle_bin(self.page))
        self.assertEqual(self.page.evaluate(rt.ITEM_ROWS_SCRIPT), 0)
        self.assertEqual(self.page.locator('[role="dialog"]').count(), 0)

    def test_onedrive_delete_is_confirmed_in_a_native_dialog(self):
        markup = self.TABLE_FILES.replace('{rows}', self.FOLDER % ('Documents', '1 item') +
                                          self.FOLDER % ('Microsoft Copilot Chat Files', '7 items'))
        markup = markup.replace("const dialog = document.createElement('div');\n              dialog.setAttribute('role', 'dialog');",
                                "const dialog = document.createElement('dialog');")
        markup = markup.replace("document.body.appendChild(dialog);", "document.body.appendChild(dialog); dialog.showModal();")
        self.assertIn("createElement('dialog')", markup)
        self.page.set_content(markup)
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual(self.page.locator('tbody tr button').all_inner_texts(), ['Documents'])

    def test_onedrive_recycle_bin_confirmation_without_dialog_role_is_pressed(self):
        markup = self.RECYCLE_BIN.replace(
            "dialog.setAttribute('role', 'dialog');\n          dialog.innerHTML = '<h2>Empty recycle bin?</h2><button id=\"confirm-empty\">Yes</button>",
            "dialog.innerHTML = '<h2>Empty recycle bin?</h2><button id=\"confirm-empty\">Delete</button>")
        self.assertNotEqual(markup, self.RECYCLE_BIN)
        self.page.set_content(markup)
        self.assertTrue(rt.empty_recycle_bin(self.page))
        self.assertEqual(self.page.evaluate(rt.ITEM_ROWS_SCRIPT), 0)
        self.assertEqual(self.page.locator('#confirm-empty').count(), 0)

    def test_empty_onedrive_recycle_bin_needs_no_action(self):
        self.page.set_content('<div role="grid"><div role="row"><span role="columnheader">Name</span></div></div>'
                              '<button disabled onclick="this.textContent=\'clicked\'">Empty recycle bin</button>')
        with patch.object(rt, 'CLEANUP_LOAD_SECONDS', 0):
            self.assertFalse(rt.empty_recycle_bin(self.page))
        self.assertEqual(self.page.locator('button').inner_text(), 'Empty recycle bin')

    def test_onedrive_home_view_switches_to_my_files_and_scrolls_to_the_folder(self):
        rows = ''.join(self.FOLDER % (f'Folder {i:02d}', '1 item') for i in range(40))
        rows += self.FOLDER % ('Microsoft Copilot Chat Files', '7 items')
        files = self.TABLE_FILES.replace('{rows}', rows).replace(
            '<table>', '<div id="list" style="height:300px;overflow:auto"><table>').replace('</table>', '</table></div>')
        # My files replaces Home; its list renders only rows near the visible area, like OneDrive's virtualized lists.
        self.page.set_content('<nav><a href="#" id="myfiles">My files</a></nav><main id="home">Recent</main>'
                              '<div id="files" hidden>' + files + '</div>' + '''<script>
          const list = document.getElementById('list'), all = rows();
          const render = () => {
            const first = Math.max(0, Math.floor(list.scrollTop / 30) - 2);
            all.forEach((r, i) => { r.style.height = '30px'; r.style.display = i >= first && i < first + 14 ? '' : 'none'; });
            const table = list.querySelector('table');
            table.style.marginTop = first * 30 + 'px';
            table.style.marginBottom = Math.max(0, all.length - first - 14) * 30 + 'px';
          };
          list.addEventListener('scroll', render);
          document.getElementById('myfiles').onclick = e => {
            e.preventDefault(); document.getElementById('home').remove();
            document.getElementById('files').hidden = false; render();
          };
        </script>''')
        with patch.object(rt, 'CLEANUP_LOAD_SECONDS', 1), patch.object(rt, 'UI_SETTLE_MS', 100):
            self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual(self.page.locator('tbody tr').count(), 40)
        self.assertNotIn('Microsoft Copilot Chat Files', self.page.locator('tbody').text_content())

    def test_onedrive_context_menu_deletes_the_folder_without_a_confirmation_dialog(self):
        self.page.set_content('''<div role="grid">
          <div role="row"><span role="columnheader">Name</span></div>
          <div role="row" aria-selected="false" class="item"><span role="gridcell">Documents</span>
            <span role="gridcell">1 item</span></div>
          <div role="row" aria-selected="false" class="item"><span role="gridcell">Microsoft Copilot Chat Files</span>
            <span role="gridcell">7 items</span></div></div>
          <script>
            document.querySelectorAll('.item').forEach(row => row.addEventListener('contextmenu', e => {
              e.preventDefault();
              document.querySelectorAll('.item').forEach(r => r.setAttribute('aria-selected', String(r === row)));
              const menu = document.createElement('div'); menu.setAttribute('role', 'menu');
              menu.innerHTML = '<button role="menuitem">Share</button><button role="menuitem">Delete</button>';
              menu.lastChild.onclick = () => { row.remove(); menu.remove(); };
              document.body.appendChild(menu);
            }));
          </script>''')
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual([t.split()[0] for t in self.page.locator('.item').all_inner_texts()], ['Documents'])

    def test_onedrive_list_inside_a_frame_is_cleaned(self):
        markup = self.TABLE_FILES.replace('{rows}', self.FOLDER % ('Documents', '1 item') +
                                          self.FOLDER % ('Microsoft Copilot Chat Files', '7 items'))
        self.page.set_content('<iframe style="width:900px;height:600px"></iframe>')
        self.page.frames[1].set_content(markup)
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual(self.page.frames[1].locator('tbody tr button').all_inner_texts(), ['Documents'])

    # My files as shown by OneDrive: div rows without grid roles, a check circle shown only while the row is
    # hovered that reacts to real pointer presses, an "N selected" indicator and a command-bar Delete.
    POINTER_FILES = '''<style>
        body { margin: 0; font: 14px sans-serif; } #bar { height: 40px; } .list { margin-left: 440px; width: 900px; }
        .head, .item { display: flex; align-items: center; height: 70px; }
        .check { width: 80px; } .item .circle { visibility: hidden; display: inline-block; width: 18px; height: 18px;
          border: 1px solid #333; border-radius: 9px; margin-left: 30px; }
        .item:hover .circle, .item.sel .circle { visibility: visible; }
        .icon { width: 44px; height: 36px; } .name { width: 330px; } .size { width: 120px; }
      </style>
      <div id="bar"><button>Share</button><span id="count"></span></div>
      <h1>My files</h1>
      <div class="list"><div class="head"><span class="check"></span><span>Name</span><span>Modified</span></div>
        {rows}</div>
      <script>
        const items = () => Array.from(document.querySelectorAll('.item'));
        const sync = () => {
          const picked = items().filter(r => r.classList.contains('sel'));
          document.getElementById('count').textContent = picked.length ? picked.length + ' selected' : '';
          let del = document.getElementById('delete');
          if (picked.length && !del) {
            del = document.createElement('button'); del.id = 'delete';
            del.innerHTML = '<i aria-hidden="true">&#xE74D;</i>Delete';
            del.onclick = () => {
              const dialog = document.createElement('div');
              dialog.setAttribute('role', 'dialog');
              dialog.innerHTML = '<h2>Delete?</h2><button id="confirm">Delete</button><button>Cancel</button>';
              dialog.querySelector('#confirm').onclick = () => {
                items().filter(r => r.classList.contains('sel')).forEach(r => r.remove());
                dialog.remove(); sync();
              };
              document.body.appendChild(dialog);
            };
            document.getElementById('bar').appendChild(del);
          } else if (!picked.length && del) del.remove();
        };
        items().forEach(row => {
          row.querySelector('.circle').addEventListener('pointerdown', e => {
            e.preventDefault(); row.classList.toggle('sel'); sync();
          });
          row.querySelector('.name').addEventListener('click', () => { location.hash = 'opened'; });
        });
      </script>'''
    POINTER_FOLDER = ('<div class="item"><span class="check"><span class="circle"></span></span>'
                      '<span class="icon" role="img" aria-label="Folder"></span>'
                      '<span class="name"><i data-icon-name="Glimmer" aria-hidden="true">&#xF4A5;</i>%s</span>'
                      '<span class="size">%s</span></div>')

    def test_onedrive_hover_check_is_clicked_with_the_pointer_and_deleted(self):
        rows = (self.POINTER_FOLDER % ('Documents', '1 item') +
                self.POINTER_FOLDER % ('Microsoft Copilot Chat Files', '13 items'))
        self.page.set_content(self.POINTER_FILES.replace('{rows}', rows))
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual(self.page.locator('.item .name').all_inner_texts(), ['\uf4a5Documents'])
        self.assertEqual(self.page.evaluate('location.hash'), '')
        self.assertEqual(self.page.locator('[role="dialog"]').count(), 0)

    def test_onedrive_selection_count_blocks_deleting_other_items(self):
        rows = (self.POINTER_FOLDER % ('Documents', '1 item') +
                self.POINTER_FOLDER % ('Microsoft Copilot Chat Files', '13 items'))
        self.page.set_content(self.POINTER_FILES.replace('{rows}', rows) +
                              '<script>document.getElementById("count").remove();'
                              'const c = document.createElement("span"); c.textContent = "2 selected";'
                              'document.getElementById("bar").appendChild(c);</script>')
        with self.assertRaises(RuntimeError):
            rt.delete_copilot_folder(self.page)
        self.assertEqual(self.page.locator('.item').count(), 2)

    def test_onedrive_delete_key_is_used_when_no_delete_command_appears(self):
        markup = self.POINTER_FILES.replace('{rows}', self.POINTER_FOLDER % ('Microsoft Copilot Chat Files', '7 items'))
        markup = markup.replace("if (picked.length && !del) {", "if (false) {") + '''<script>
          document.addEventListener('keydown', e => {
            if (e.key !== 'Delete') return;
            const dialog = document.createElement('div');
            dialog.setAttribute('role', 'dialog');
            dialog.innerHTML = '<h2>Delete?</h2><button id="confirm">Delete</button>';
            dialog.querySelector('#confirm').onclick = () => {
              document.querySelectorAll('.item.sel').forEach(r => r.remove()); dialog.remove();
            };
            document.body.appendChild(dialog);
          });
        </script>'''
        self.page.set_content(markup)
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual(self.page.locator('.item').count(), 0)

    def test_onedrive_list_inside_a_shadow_root_is_cleaned(self):
        markup = self.TABLE_FILES.replace('{rows}', self.FOLDER % ('Documents', '1 item') +
                                          self.FOLDER % ('Microsoft Copilot Chat Files', '7 items'))
        self.page.set_content('<div id="host"></div>')
        self.page.evaluate('''(markup) => {
            const root = document.getElementById('host').attachShadow({mode: 'open'});
            const holder = document.createElement('div'); holder.innerHTML = markup; root.appendChild(holder);
            const script = holder.querySelector('script'); script.remove();
            const scoped = {getElementById: id => root.getElementById(id), body: root,
              querySelectorAll: selector => root.querySelectorAll(selector),
              createElement: tag => document.createElement(tag)};
            new Function('document', script.textContent)(scoped);
        }''', markup)
        self.assertEqual(rt.delete_copilot_folder(self.page), 1)
        self.assertEqual(self.page.locator('#host tbody tr button').all_inner_texts(), ['Documents'])

    def test_prior_long_running_task_card_is_ignored(self):
        self.page.set_content('''<main><article data-roundtrip-prior-msg="1">
          <details open><summary>Old task <span>Completed</span></summary><p>Old step</p></details>
          </article></main>''')
        self.assertEqual(rt.read_task_cards(self.page), [])

    def test_chat_response_omits_code_blocks(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <p>Created the app.</p><pre><code>secret_code()
more()</code></pre><div class="code-block">secret_block()</div>
          <p>Use <code>make run</code>.</p></article></main>''')
        text = rt.read_chat_response(self.page)
        self.assertIn('Created the app.', text)
        self.assertIn('make run', text)
        self.assertNotIn('secret_', text)

    def test_task_plan_is_not_flattened_into_live_answer_text(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <p>Created the implementation plan.</p>
          <details open><summary><span>Create Two Browser Games</span><span>In progress</span></summary>
            <ol><li aria-label="Completed">Inspect repository</li><li aria-current="step">Build games</li></ol>
          </details></article></main>''')
        self.assertEqual(len(rt.read_task_cards(self.page)), 1)
        _, _, answers = rt.read_screen(self.page, self.sel)
        text = '\n'.join(item['text'] for item in answers)
        self.assertIn('Created the implementation plan.', text)
        self.assertNotIn('Inspect repository', text)
        self.assertNotIn('Build games', text)


if __name__ == '__main__':
    unittest.main()
