import unittest
from unittest.mock import AsyncMock, MagicMock
import posting_controls
from profit_funnel import ProfitFunnel
from unittest.mock import patch


class PostingControlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_command_advances_offset_so_it_is_not_replayed(self):
        c=AsyncMock();acquire=AsyncMock();acquire.__aenter__.return_value=c
        pool=MagicMock();pool.acquire.return_value=acquire
        worker=ProfitFunnel(pool,None);worker.state=AsyncMock(return_value={'value':0})
        worker.telegram=AsyncMock(return_value={'result':[{'update_id':101,'message':{'chat':{'id':42,'type':'private'},'from':{'id':42},'text':'/posting_status'}}]})
        worker.send=AsyncMock();worker.put=AsyncMock()
        with patch.dict('os.environ',{'OWNER_CHAT_ID':'42'}),patch('posting_controls.handle',AsyncMock(return_value='status')):
            await worker.commands()
        worker.put.assert_awaited_once_with(c,'telegram_offset',{'value':102})
        worker.send.assert_awaited_once_with('command:101','status')

    def test_blacklist_input_cannot_inject_sql_or_omit_reason(self):
        self.assertEqual(posting_controls.blacklist_target('/posting_blacklist @valid_group жалоба администратора'),('valid_group','жалоба администратора'))
        for text in ['/posting_blacklist valid_group', "/posting_blacklist bad';drop жалоба",'/posting_blacklist https://evil.test/group жалоба']:
            with self.assertRaises(ValueError):posting_controls.blacklist_target(text)

    def test_non_owner_and_group_chat_cannot_reach_controls(self):
        worker=ProfitFunnel(None,None)
        with patch.dict('os.environ',{'OWNER_CHAT_ID':'42'}):
            self.assertFalse(worker.owner_allowed({'message':{'chat':{'id':42,'type':'private'},'from':{'id':9},'text':'/posting_pause'}}))
            self.assertFalse(worker.owner_allowed({'message':{'chat':{'id':42,'type':'group'},'from':{'id':42},'text':'/posting_pause'}}))

    async def test_missing_tables_fail_closed_without_write(self):
        c=AsyncMock();c.fetchval.return_value=False
        result=await posting_controls.handle(c,'posting_pause','/posting_pause')
        self.assertIn('Изменения не внесены',result);self.assertEqual(c.fetchval.await_count,1)
        c.execute.assert_not_awaited();c.fetchrow.assert_not_awaited()

    async def test_resume_only_clears_pause_never_activates_or_unblacklists(self):
        c=AsyncMock();c.fetchval.side_effect=[True,'handles_media']
        result=await posting_controls.handle(c,'posting_resume','/posting_resume')
        sql,paused=c.fetchval.await_args.args
        self.assertEqual(paused,'false');self.assertIn('external_telegram_paused',sql)
        self.assertNotIn('handle_autopost_platforms',sql)
        self.assertIn('не активируется',result)

    async def test_blacklist_revokes_lease_and_warns_about_inflight_without_resend(self):
        c=AsyncMock();c.fetchval.side_effect=[True,True];c.fetchrow.return_value={'id':9,'name':'group','username':'valid_group'}
        result=await posting_controls.handle(c,'posting_blacklist','/posting_blacklist valid_group жалоба')
        sql,user,reason=c.fetchrow.await_args.args
        self.assertIn('lease_owner=NULL',sql);self.assertIn("status='BLACKLISTED'",sql)
        self.assertEqual((user,reason),('valid_group','жалоба'));self.assertIn('повтор запрещён',result)
        c.execute.assert_not_awaited()


if __name__=='__main__':unittest.main()
