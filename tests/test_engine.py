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
                self.assertTrue(prompt.endswith('To fix: Update the tests'))

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
                page.locator.return_value.first = button
                with patch.object(rt, 'temporary_chat_active', side_effect=[not enabled, enabled]):
                    rt.set_temporary_chat(page, rt.selectors_from_env(), enabled)
                button.wait_for.assert_called_once_with(state='visible', timeout=30000)
                button.click.assert_called_once_with()

    def test_temporary_chat_is_not_clicked_when_state_already_matches(self):
        page = Mock()
        with patch.object(rt, 'temporary_chat_active', return_value=False):
            rt.set_temporary_chat(page, rt.selectors_from_env(), False)
        page.locator.assert_not_called()

    def test_visible_browser_falls_back_to_hidden_without_a_display(self):
        self.assertTrue(rt.browser_is_headless(True, {}))
        self.assertFalse(rt.browser_is_headless(True, {'DISPLAY': ':0'}))
        self.assertFalse(rt.browser_is_headless(True, {'WAYLAND_DISPLAY': 'wayland-0'}))
        self.assertTrue(rt.browser_is_headless(False, {'DISPLAY': ':0'}))

    def test_visible_stop_controls_define_running_state(self):
        page = Mock()
        locator = Mock()
        hidden, visible = Mock(), Mock()
        hidden.is_visible.return_value = False
        visible.is_visible.return_value = True
        locator.count.return_value = 2
        locator.nth.side_effect = lambda index: (hidden, visible)[index]
        cards = Mock()
        cards.count.return_value = 0
        page.locator.side_effect = lambda selector: cards if selector == '[data-roundtrip-task-card]' else locator
        for button in (hidden, visible):
            button.evaluate.side_effect = lambda script: 'getComputedStyle' in script
        self.assertTrue(rt.response_is_running(page, rt.selectors_from_env()))
        visible.is_visible.return_value = False
        self.assertFalse(rt.response_is_running(page, rt.selectors_from_env()))
        self.assertIn('data-icon-name', rt.selectors_from_env()['stop'])

    def test_task_card_is_hovered_and_takes_priority_over_composer(self):
        page, cards, card, controls, button = (Mock() for _ in range(5))
        page.locator.return_value = cards
        cards.count.return_value = 1
        cards.nth.return_value = card
        card.is_visible.return_value = True
        card.locator.return_value = controls
        controls.count.return_value = 1
        controls.nth.return_value = button
        button.is_visible.return_value = True
        with patch.object(rt, 'read_task_cards'), patch.object(rt, 'is_prior', return_value=False):
            self.assertEqual(rt.visible_stop_buttons(page, rt.selectors_from_env()), [button])
            card.hover.assert_called_once_with(timeout=1500)
            button.is_visible.return_value = False
            self.assertFalse(rt.response_is_running(page, rt.selectors_from_env()))
            self.assertTrue(all(call.args == ('[data-roundtrip-task-card]',)
                                for call in page.locator.call_args_list))

    def test_cancel_clicks_the_same_hover_revealed_task_stop(self):
        page, cards, card, controls, button = (Mock() for _ in range(5))
        page.locator.return_value = cards
        cards.count.return_value = 1
        cards.nth.return_value = card
        card.is_visible.return_value = True
        card.locator.return_value = controls
        controls.count.return_value = 1
        controls.nth.return_value = button
        button.is_visible.side_effect = [True, False]
        with patch.object(rt, 'read_task_cards'), patch.object(rt, 'is_prior', return_value=False):
            self.assertTrue(rt.stop_generation(page, rt.selectors_from_env()))
        button.click.assert_called_once_with(timeout=3000)
        self.assertEqual(card.hover.call_count, 2)

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

    def test_output_signature_uses_a_pre_submit_baseline(self):
        source = SCRIPT.read_text()
        baseline = source.index('baseline_signature, _, _ = read_screen(page, sel, prompt)')
        submit = source.index('send_with_retry(page, composer, sel["send"])')
        initialize = source.index('last_signature = baseline_signature')
        self.assertLess(baseline, submit)
        self.assertLess(submit, initialize)


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

    def test_task_without_stop_does_not_use_composer_stop(self):
        self.page.set_content('''<main><details open><summary>Build repository
          <span>Completed</span></summary><ol><li>Test repository</li></ol></details>
          <form><button aria-label="Stop responding">Stop</button></form></main>''')
        self.assertFalse(rt.response_is_running(self.page, self.sel))

    def test_reused_composer_control_is_not_excluded_by_prior_marker(self):
        self.page.set_content('<form><button data-roundtrip-prior="1" aria-label="Stop responding">Stop</button></form>')
        self.assertTrue(rt.response_is_running(self.page, self.sel))

    def test_prior_long_running_task_card_is_ignored(self):
        self.page.set_content('''<main><article data-roundtrip-prior-msg="1">
          <details open><summary>Old task <span>Completed</span></summary><p>Old step</p></details>
          </article></main>''')
        self.assertEqual(rt.read_task_cards(self.page), [])

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
