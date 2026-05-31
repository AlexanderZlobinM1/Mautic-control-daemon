import unittest

from mcd_agent.mode import (
    _comment_empty_leads_cleanup,
    _comment_mautic_email_fetch,
    _managed_empty_leads_cleanup_schedules,
    _restore_mautic_email_fetch_comments,
)


class EmptyLeadsCleanupScheduleTests(unittest.TestCase):
    def test_monthly_legacy_cron_migrates_as_cron_expr(self):
        content = '0 2 1 * * php /var/www/mautic/bin/console doctrine:query:sql "DELETE FROM ss_leads WHERE email IS NULL AND mobile IS NULL;" > /dev/null 2>&1\n'
        updated, changed, interval_sec, cron_exprs = _comment_empty_leads_cleanup(content, 'ts')
        self.assertEqual(changed, 1)
        self.assertEqual(interval_sec, 900)
        self.assertEqual(cron_exprs, ['0 2 1 * *'])
        self.assertIn('# MCD_MANAGED ts', updated)

    def test_interval_legacy_cron_migrates_as_interval(self):
        content = '*/15 * * * * php /var/www/mautic/bin/console doctrine:query:sql "DELETE FROM ss_leads WHERE email IS NULL AND mobile IS NULL;" > /dev/null 2>&1\n'
        _updated, changed, interval_sec, cron_exprs = _comment_empty_leads_cleanup(content, 'ts')
        self.assertEqual(changed, 1)
        self.assertEqual(interval_sec, 900)
        self.assertEqual(cron_exprs, [])

    def test_existing_managed_comment_can_recover_cron_expr(self):
        content = '# MCD_MANAGED ts: disabled empty leads cleanup by mcd profile=active\n# 0 2 1 * * php /var/www/mautic/bin/console doctrine:query:sql "DELETE FROM ss_leads WHERE email IS NULL AND mobile IS NULL;" > /dev/null 2>&1\n'
        interval_sec, cron_exprs = _managed_empty_leads_cleanup_schedules(content)
        self.assertEqual(interval_sec, 0)
        self.assertEqual(cron_exprs, ['0 2 1 * *'])

    def test_mautic_email_fetch_cron_is_commented_for_monitored_parser(self):
        content = '*/5 * * * * php /var/www/mautic/bin/console mautic:email:fetch > /dev/null 2>&1\n'
        updated, changed = _comment_mautic_email_fetch(content, 'ts')
        self.assertEqual(changed, 1)
        self.assertIn('disabled mautic email fetch by mcd monitored-email parser', updated)
        self.assertIn('# */5 * * * * php /var/www/mautic/bin/console mautic:email:fetch', updated)

    def test_mautic_email_fetch_cron_restore_is_scoped(self):
        content = '# MCD_MANAGED ts: disabled mautic email fetch by mcd monitored-email parser\n# */5 * * * * php /var/www/mautic/bin/console mautic:email:fetch > /dev/null 2>&1\n# MCD_MANAGED ts: disabled empty leads cleanup by mcd profile=active\n# 0 2 * * * php /var/www/mautic/bin/console doctrine:query:sql "DELETE FROM ss_leads WHERE email IS NULL AND mobile IS NULL;"\n'
        updated, changed = _restore_mautic_email_fetch_comments(content)
        self.assertEqual(changed, 2)
        self.assertIn('*/5 * * * * php /var/www/mautic/bin/console mautic:email:fetch', updated)
        self.assertIn('disabled empty leads cleanup by mcd profile=active', updated)


if __name__ == '__main__':
    unittest.main()
