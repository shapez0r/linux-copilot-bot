"""App-list cards, status summary, escaping and message boundaries."""
import html
from html.parser import HTMLParser
import unittest

from support import make_config  # noqa: F401
from bot.app_list import AppEntry, app_list_messages
from bot.telegram_api import MESSAGE_LIMIT


class AppListTests(unittest.TestCase):
    def test_summary_counts_statuses_and_cards_show_icons_and_links(self):
        entries = [AppEntry('a', 'running', 'https://a.example'),
                   AppEntry('long-name', 'stopped', 'not configured'),
                   AppEntry('broken', 'failed'),
                   AppEntry('booting', 'activating (start)'), AppEntry('warming', 'starting')]
        message, = app_list_messages(entries)
        self.assertTrue(message.startswith('📱 <b>Apps</b>\n🟢 1 running · ⚪ 1 stopped · 🟡 1 starting · 🔴 1 failed · 🟡 1 changing\n\n'))
        self.assertIn('🟢 <b>a</b> · running\n<blockquote>🔗 https://a.example</blockquote>',
                      message)
        self.assertIn('⚪ <b>long-name</b> · stopped\n<blockquote>🔗 not configured</blockquote>', message)
        self.assertIn('🔴 <b>broken</b> · failed\n\n', message)
        self.assertIn('🟡 <b>booting</b> · activating (start)', message)

    def test_admin_fields_are_shown_only_when_provided(self):
        entry = AppEntry('app', 'running (pid 5)', 'https://app.example', '8080', '/repos/app')
        message, = app_list_messages([entry])
        for expected in ('running (pid 5)', '🔌 Port 8080', '📁 <code>/repos/app</code>'):
            self.assertIn(expected, message)
        bot, = app_list_messages([AppEntry('bot', 'running', folder='/repos/bot', note='this bot · svc.service')])
        self.assertIn('🤖 this bot · svc.service', bot)
        self.assertNotIn('🔗', bot)
        self.assertNotIn('🔌', bot)
        public, = app_list_messages([AppEntry('app', 'running', 'https://app.example')])
        self.assertNotIn('📁', public)
        self.assertNotIn('🔌', public)

    def test_html_is_escaped(self):
        message, = app_list_messages([AppEntry('a&b', 'failed (<x>)', 'https://a.example/?q="1"&r=2', '', '<folder> & "dir"')])
        self.assertIn('<b>a&amp;b</b>', message)
        self.assertIn('&lt;folder&gt;', message)
        self.assertIn('https://a.example/?q=&quot;1&quot;&amp;r=2', message)
        self.assertNotIn('<a ', message)
        self.assertNotIn('<folder>', message)
        HTMLParser().feed(message)

    def test_many_apps_split_into_bounded_pages_without_losing_entries(self):
        entries = [AppEntry(f'app-{i:03}', 'running', f'https://app-{i:03}.example') for i in range(150)]
        messages = list(app_list_messages(entries))
        self.assertGreater(len(messages), 1)
        self.assertTrue(messages[0].startswith('📱 <b>Apps</b>\n🟢 150 running'))
        for message in messages:
            self.assertLessEqual(len(message), MESSAGE_LIMIT)
        for message in messages[1:]:
            self.assertTrue(message.startswith('📱 <b>Apps</b> (continued)\n\n'))
        for entry in entries:
            self.assertEqual('\n'.join(messages).count(f'<b>{entry.name}</b>'), 1)

    def test_oversized_details_keep_complete_entities(self):
        status = '<&>' * 3000
        messages = list(app_list_messages([AppEntry('app', status, folder='/repos/app')]))
        self.assertTrue(all(len(message) <= MESSAGE_LIMIT for message in messages))
        self.assertEqual(''.join(html.unescape(message) for message in messages[1:]),
                         f'Status: {status}\nFolder: /repos/app')
        for message in messages:
            HTMLParser().feed(message)


if __name__ == '__main__':
    unittest.main()
