"""App-list formatting, escaping and message boundaries."""
import html
from html.parser import HTMLParser
import unittest

from support import make_config  # noqa: F401
from bot.app_list import app_list_messages
from bot.telegram_api import MESSAGE_LIMIT


class AppListTests(unittest.TestCase):
    def test_table_aligns_names_and_preserves_links(self):
        entries = [('a', 'running', 'a\nurl: https://a.example'),
                   ('long-name', 'stopped', 'long-name\nurl: not configured')]
        message, = app_list_messages(entries)
        self.assertIn('a          running\nlong-name  stopped', message)
        self.assertIn('https://a.example', message)

    def test_html_is_escaped(self):
        message, = app_list_messages([('a&b', 'failed', '<folder> & "url"')])
        self.assertIn('a&amp;b', message)
        self.assertIn('&lt;folder&gt;', message)
        self.assertNotIn('<folder>', message)

    def test_many_apps_repeat_headings_without_losing_entries(self):
        entries = [(f'app-{i:03}', 'running', f'app-{i:03}\nurl: https://app-{i:03}.example')
                   for i in range(150)]
        messages = list(app_list_messages(entries))
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), MESSAGE_LIMIT)
            self.assertTrue(message.startswith('Apps:\n<pre>'))
            self.assertEqual(message.count('<pre>'), message.count('</pre>'))
        for name, status, details in entries:
            self.assertEqual('\n'.join(messages).count(details), 1)

    def test_oversized_details_keep_complete_entities(self):
        details = '<&>' * 3000
        messages = list(app_list_messages([('app', 'failed', details)]))
        self.assertTrue(all(len(message) <= MESSAGE_LIMIT for message in messages))
        self.assertEqual(''.join(html.unescape(message) for message in messages[1:]), details)
        for message in messages:
            HTMLParser().feed(message)


if __name__ == '__main__':
    unittest.main()
